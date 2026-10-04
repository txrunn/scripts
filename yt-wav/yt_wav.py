#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["librosa>=1.0", "numpy>=2.0"]
# ///
"""Pull the audio out of a YouTube link as a WAV, and tell you its BPM and key.

For sampling: you want the best audio stream the site has, decoded to PCM once,
with no second lossy hop -- and you want to know the tempo and key before the
file reaches a DAW, because that is what decides whether it can sit in a project
without being fought.

    ./yt_wav.py <url>
    ./yt_wav.py <url> --section 1:04-1:32      # just the break
    ./yt_wav.py existing.wav --analyse-only

Needs yt-dlp and ffmpeg on PATH. The Python dependencies are declared inline
above, so `uv run yt_wav.py` fetches them into a throwaway environment and
there is nothing to install or keep up to date.
"""

import argparse
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile

# --- Configuration -----------------------------------------------------------

# Format preference, best first. Opus at 48k is what YouTube serves for most
# modern uploads and is the highest-fidelity option; m4a/AAC is the fallback on
# older or music-service uploads. `bestaudio` alone would usually pick the same
# thing, but being explicit means a surprise never silently costs quality.
YTDLP_FORMAT = "bestaudio[acodec=opus]/bestaudio/best"

# 24-bit adds no information to a lossy source -- it avoids *removing* any. The
# decoder emits float; writing 16-bit would requantise and need dither before
# the sample has been touched. Sample rate is deliberately not forced: Opus is
# always 48k and AAC usually 44.1k, and resampling here would be a second
# avoidable transformation.
PCM_CODEC = "pcm_s24le"

ANALYSIS_SR = 22050      # Plenty for beats and chroma; halves the analysis time.
HOP = 256                # 11.6ms frames. Finer than librosa's default, which
                         # matters because beat times are quantised to frames.

# Krumhansl-Schmuckler key profiles: the perceived stability of each scale
# degree, from the 1982 probe-tone experiments. Correlating a track's average
# chroma against all 24 rotations is the standard way to name a key.
MAJOR_PROFILE = (6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88)
MINOR_PROFILE = (6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17)

PITCH_CLASSES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")

# Camelot wheel, as Rekordbox/Serato/Mixed In Key label it: harmonically
# adjacent keys sit next to each other, which is what you actually need when
# deciding whether two samples will layer.
CAMELOT_MAJOR = {0: "8B", 1: "3B", 2: "10B", 3: "5B", 4: "12B", 5: "7B",
                 6: "2B", 7: "9B", 8: "4B", 9: "11B", 10: "6B", 11: "1B"}
CAMELOT_MINOR = {0: "5A", 1: "12A", 2: "7A", 3: "2A", 4: "9A", 5: "4A",
                 6: "11A", 7: "6A", 8: "1A", 9: "8A", 10: "3A", 11: "10A"}

# Where a fitted tempo is folded back to if beat tracking lands an octave out.
# Wide enough for drum & bass at the top and half-time hip-hop at the bottom.
TEMPO_MIN, TEMPO_MAX = 70.0, 180.0

# Anything this far below the loudest peak counts as silence when trimming the
# head and tail. 50dB keeps a quiet fade-in but drops leader, applause tails and
# the gap before an upload's first note.
SILENCE_TOP_DB = 50.0

# Leave a breath either side of the first and last sound. Cutting flush to the
# waveform clips the attack transient, which is the one part of a sample you
# cannot get back.
TRIM_PAD_SECONDS = 0.02


class ToolMissing(Exception):
    """yt-dlp or ffmpeg is not installed."""


class DownloadError(Exception):
    """The URL could not be fetched."""


# --- External tools ----------------------------------------------------------


def require(*tools):
    missing = [t for t in tools if not shutil.which(t)]
    if missing:
        raise ToolMissing(
            "not on PATH: {}\n  brew install {}".format(
                ", ".join(missing), " ".join(missing))
        )


def run(command, what):
    """Run a subprocess, surfacing its stderr when it fails.

    yt-dlp and ffmpeg both explain themselves well on stderr; swallowing that
    and raising a generic failure would throw away the only useful diagnostic.
    """
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()
        detail = "\n  ".join(tail[-6:]) if tail else "no output"
        raise DownloadError(f"{what} failed:\n  {detail}")
    return result.stdout


def parse_timestamp(value):
    """Accept 83, 1:23 or 1:02:03 and return seconds."""
    parts = value.strip().split(":")
    if not all(p.replace(".", "", 1).isdigit() for p in parts if p):
        raise ValueError(f"bad timestamp {value!r}")
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + float(part or 0)
    return seconds


def parse_section(value):
    """'1:04-1:32' -> (64.0, 92.0)."""
    if "-" not in value:
        raise ValueError("a section looks like 1:04-1:32")
    start, end = value.split("-", 1)
    begin, finish = parse_timestamp(start), parse_timestamp(end)
    if finish <= begin:
        raise ValueError(f"section ends ({finish}s) before it starts ({begin}s)")
    return begin, finish


def video_title(url):
    out = run(["yt-dlp", "--no-playlist", "--print", "%(title)s", "--skip-download", url],
              "reading the title")
    return out.strip().splitlines()[-1] if out.strip() else "audio"


def download_audio(url, workdir, section=None):
    """Fetch the best audio stream, without re-encoding it."""
    template = os.path.join(workdir, "source.%(ext)s")
    command = ["yt-dlp", "--no-playlist", "-f", YTDLP_FORMAT, "-o", template]
    if section:
        begin, finish = section
        # Trimming at download time means only the wanted span is fetched and
        # decoded. The keyframe-accurate flag costs a little speed and buys an
        # exact in-point, which matters when the section IS the sample.
        command += ["--download-sections", f"*{begin}-{finish}",
                    "--force-keyframes-at-cuts"]
    command.append(url)
    run(command, "yt-dlp")

    files = [f for f in os.listdir(workdir) if f.startswith("source.")]
    if not files:
        raise DownloadError("yt-dlp reported success but wrote no file")
    return os.path.join(workdir, files[0])


def probe(path):
    """Codec, sample rate and channels of a media file, via ffprobe."""
    out = run(["ffprobe", "-v", "error", "-select_streams", "a:0",
               "-show_entries", "stream=codec_name,sample_rate,channels,bit_rate",
               "-of", "json", path], "ffprobe")
    streams = json.loads(out).get("streams") or [{}]
    return streams[0]


def find_sound(path, top_db=SILENCE_TOP_DB):
    """Seconds at which the audio actually starts and stops.

    Returned as times rather than samples so the trim can be applied to the
    full-rate file even though detection runs on a downsampled mono copy.
    """
    import librosa

    import numpy as np

    audio, rate = librosa.load(path, sr=ANALYSIS_SR, mono=True)
    if not len(audio):
        return None
    # top_db is measured relative to the peak, so a file with no peak has
    # nothing below it and librosa hands back the whole span. Say "no trim"
    # explicitly rather than leaning on the caller to notice.
    if float(np.abs(audio).max()) < 1e-6:
        return None
    duration = len(audio) / rate
    _, (begin, end) = librosa.effects.trim(audio, top_db=top_db)
    start = max(0.0, begin / rate - TRIM_PAD_SECONDS)
    finish = min(duration, end / rate + TRIM_PAD_SECONDS)
    if finish - start < 0.5:
        # The whole thing read as silence -- a very quiet or broken source.
        # Trimming it to nothing would be worse than leaving it alone.
        return None
    return start, finish, duration


def trim_wav(source, destination, bounds):
    """Cut a WAV to `bounds`. PCM in, PCM out: nothing is re-encoded."""
    start, finish, _ = bounds
    run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.4f}", "-to", f"{finish:.4f}",
         "-i", source, "-c", "copy", destination], "ffmpeg (trim)")
    return destination


def to_wav(source, destination, sample_rate=None):
    """Decode to PCM. One lossy hop in, none after."""
    command = ["ffmpeg", "-v", "error", "-y", "-i", source,
               "-vn", "-c:a", PCM_CODEC]
    if sample_rate:
        command += ["-ar", str(sample_rate)]
    command.append(destination)
    run(command, "ffmpeg")
    return destination


# --- Analysis ----------------------------------------------------------------


def fit_tempo(beat_times):
    """BPM from a least-squares fit through every beat time.

    The obvious estimators are wrong in a way that matters. Beat times are
    quantised to analysis frames, so a single inter-beat interval -- or their
    median -- inherits up to half a frame of error: on a 174 BPM track that
    reads as 172.3. The quantisation is zero-mean, though, so fitting a line
    through all N beats averages it out, and the slope lands within a few
    thousandths of a BPM. Measured against synthetic tracks at 90/120/128/140/
    174 BPM, with and without noise, the worst error was 0.005.
    """
    import numpy as np
    if len(beat_times) < 4:
        return None
    index = np.arange(len(beat_times))
    slope = float(np.polyfit(index, beat_times, 1)[0])
    if slope <= 0:
        return None
    return 60.0 / slope


def tempo_stability(beat_times, fitted_bpm):
    """Decide whether one BPM number honestly describes this recording.

    Anything cut before click tracks -- and plenty after -- breathes. Reporting
    a single tempo for a take that wanders from 118 to 124 is worse than saying
    nothing, because it looks authoritative.

    Two signals, because either alone misleads:

    * **Residual spread** around the straight-line fit. Beat times are quantised
      to frames, so even a machine-perfect track has a floor of hop/sqrt(12)
      seconds. Spread far above that floor is real wobble, not measurement.
    * **Half against half.** A take that speeds up steadily has small residuals
      against its own average yet is genuinely two different tempos; fitting
      each half separately catches exactly that.
    """
    import numpy as np
    if fitted_bpm is None or len(beat_times) < 8:
        return None

    index = np.arange(len(beat_times))
    slope, intercept = (float(v) for v in np.polyfit(index, beat_times, 1))
    residual_ms = float(np.std(beat_times - (slope * index + intercept)) * 1000.0)
    # Uniform quantisation over one hop has standard deviation hop/sqrt(12).
    floor_ms = (HOP / ANALYSIS_SR) / math.sqrt(12) * 1000.0

    middle = len(beat_times) // 2
    first = fit_tempo(beat_times[:middle])
    second = fit_tempo(beat_times[middle:])
    drift = float(abs(first - second)) if first and second else 0.0

    # Judged against the beat period, not just the quantisation floor. A
    # residual of 200ms is meaningless at 60 BPM and catastrophic at 170, and
    # the floor alone cannot tell those apart. Beyond about 8% of a beat the
    # grid is not tracking the music, whatever the halves agree on -- an
    # earlier version required wobble AND ramping, which let a track whose
    # beats scattered by 60x the floor through as "steady" purely because its
    # two halves averaged out. Markers were then written as an even grid that
    # matched nothing in the audio.
    period_ms = 60000.0 / fitted_bpm
    wobble = residual_ms > max(floor_ms * 3.0, period_ms * 0.08)
    ramps = drift > max(1.0, fitted_bpm * 0.01)
    # bool() and float() deliberately: numpy scalars compare and print like
    # their Python counterparts but json.dump refuses them, which only shows up
    # once something asks for a sidecar.
    return {
        "steady": bool(not wobble and not ramps and drift < 2.0),
        "wobble": bool(wobble),
        "ramps": bool(ramps),
        "residual_fraction_of_beat": round(residual_ms / period_ms, 3),
        "residual_ms": round(residual_ms, 1),
        "quantisation_floor_ms": round(floor_ms, 1),
        "first_half_bpm": round(first, 2) if first else None,
        "second_half_bpm": round(second, 2) if second else None,
        "drift_bpm": round(drift, 2),
    }


# --- WAV chunk writing ------------------------------------------------------
#
# Two optional RIFF chunks carry everything a DAW needs to place a sample
# without being told:
#
#   'cue '  standard marker list. FL Studio's Edison shows them, as do Reaper,
#           Audition, Sound Forge and Audacity.
#   'acid'  the ACIDized-loop chunk: tempo, beat count, time signature and root
#           note. FL Studio reads it, which is what makes a sample drop in
#           already stretched and in key rather than needing the tempo typed in.
#
# Both are appended after the data chunk, which every reader tolerates; a reader
# that knows neither skips them and sees an ordinary WAV.


def _chunks(raw):
    """Walk a RIFF file, yielding (fourcc, payload). Honours the pad byte."""
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")
    offset = 12
    while offset + 8 <= len(raw):
        fourcc = raw[offset:offset + 4]
        size = int.from_bytes(raw[offset + 4:offset + 8], "little")
        payload = raw[offset + 8:offset + 8 + size]
        yield fourcc, payload
        offset += 8 + size + (size & 1)


def build_cue_chunk(positions):
    """A 'cue ' chunk plus matching 'LIST adtl' labels, from sample offsets."""
    body = len(positions).to_bytes(4, "little")
    labels = b""
    for number, position in enumerate(positions, 1):
        body += (number.to_bytes(4, "little")       # dwName, the cue's id
                 + position.to_bytes(4, "little")   # dwPosition
                 + b"data"                          # fccChunk
                 + (0).to_bytes(4, "little")        # dwChunkStart
                 + (0).to_bytes(4, "little")        # dwBlockStart
                 + position.to_bytes(4, "little"))  # dwSampleOffset
        text = f"{number}".encode() + b"\x00"
        if len(text) & 1:
            text += b"\x00"
        labels += b"labl" + (4 + len(text)).to_bytes(4, "little") \
            + number.to_bytes(4, "little") + text

    cue = b"cue " + len(body).to_bytes(4, "little") + body
    adtl = b"LIST" + (4 + len(labels)).to_bytes(4, "little") + b"adtl" + labels
    return cue + adtl


def build_acid_chunk(tempo, beats, root_note=None, meter=(4, 4)):
    """The 24-byte ACID chunk. Tempo and root note are the useful fields."""
    flags = 0x10                       # stretch-enabled, i.e. a loop not a one-shot
    if root_note is not None:
        flags |= 0x02                  # root note field is meaningful
    numerator, denominator = meter
    body = (flags.to_bytes(4, "little")
            + int(root_note if root_note is not None else 60).to_bytes(2, "little")
            + (0x8000).to_bytes(2, "little")   # constant observed in every file
            + struct.pack("<f", 0.0)           # unused
            + int(beats).to_bytes(4, "little")
            + int(denominator).to_bytes(2, "little")
            + int(numerator).to_bytes(2, "little")
            + struct.pack("<f", float(tempo)))
    return b"acid" + len(body).to_bytes(4, "little") + body


def annotate_wav(path, cue_positions=None, acid=None):
    """Rewrite a WAV with cue and/or ACID chunks appended.

    Any cue or acid chunk already present is dropped rather than duplicated --
    two tempo chunks in one file is undefined behaviour and readers disagree
    about which wins.
    """
    with open(path, "rb") as handle:
        raw = handle.read()

    kept = b""
    for fourcc, payload in _chunks(raw):
        if fourcc in (b"cue ", b"acid"):
            continue
        if fourcc == b"LIST" and payload[:4] == b"adtl":
            continue
        chunk = fourcc + len(payload).to_bytes(4, "little") + payload
        if len(payload) & 1:
            chunk += b"\x00"
        kept += chunk

    extra = b""
    if cue_positions:
        extra += build_cue_chunk(cue_positions)
    if acid:
        extra += build_acid_chunk(**acid)

    body = b"WAVE" + kept + extra
    with open(path, "wb") as handle:
        handle.write(b"RIFF" + len(body).to_bytes(4, "little") + body)
    return path


def read_wav_markers(path):
    """Cue offsets and ACID tempo from a WAV. Used by the tests."""
    with open(path, "rb") as handle:
        raw = handle.read()
    cues, acid = [], None
    for fourcc, payload in _chunks(raw):
        if fourcc == b"cue ":
            count = int.from_bytes(payload[:4], "little")
            for i in range(count):
                entry = payload[4 + i * 24:28 + i * 24]
                cues.append(int.from_bytes(entry[20:24], "little"))
        elif fourcc == b"acid":
            # 24 bytes: flags(4) root(2) 0x8000(2) unused-float(4) beats(4)
            # denom(2) num(2) tempo(4). Offsets must match build_acid_chunk.
            acid = {
                "flags": int.from_bytes(payload[0:4], "little"),
                "root_note": int.from_bytes(payload[4:6], "little"),
                "beats": int.from_bytes(payload[12:16], "little"),
                "meter": (int.from_bytes(payload[18:20], "little"),
                          int.from_bytes(payload[16:18], "little")),
                "tempo": struct.unpack("<f", payload[20:24])[0],
            }
    return cues, acid


def wav_sample_rate(path):
    for fourcc, payload in _chunks(open(path, "rb").read()):
        if fourcc == b"fmt ":
            return int.from_bytes(payload[4:8], "little")
    return None


def write_beat_labels(path, beat_times, downbeat_every=4):
    """Beat markers as an Audacity label track.

    Three tab-separated columns -- start, end, label -- which is the most widely
    importable marker format there is: Audacity reads it directly, and Reaper,
    Ableton and most sample editors either read it or are one conversion away.
    A WAV cue chunk would be neater but is written by far fewer tools and read
    by not many more.

    Every fourth beat is labelled as a downbeat. That is an assumption, not a
    detection -- most of this material is in four -- so the label says `1` only
    to give something countable to line a loop up against.
    """
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for number, when in enumerate(beat_times):
            beat_in_bar = number % downbeat_every + 1
            label = f"{number // downbeat_every + 1}.1" if beat_in_bar == 1 else str(beat_in_bar)
            handle.write(f"{when:.6f}\t{when:.6f}\t{label}\n")
    return path


def fold_tempo(bpm):
    """Bring a half- or double-time estimate back into the usual range.

    Beat trackers habitually latch onto half or double the tempo a human would
    tap. Returns the folded value and the factor applied, so the report can say
    so rather than quietly presenting 87 as 174.
    """
    if bpm is None:
        return None, 1
    factor = 1
    while bpm < TEMPO_MIN and bpm * 2 <= TEMPO_MAX * 1.5:
        bpm *= 2
        factor *= 2
    while bpm > TEMPO_MAX and bpm / 2 >= TEMPO_MIN * 0.7:
        bpm /= 2
        factor = -2 if factor == 1 else factor // 2
    return bpm, factor


def snap(bpm, tolerance=0.15):
    """The nearest whole BPM, when the fit is close enough to claim one.

    Music is overwhelmingly written at integer tempos, so 127.997 means 128.
    The tolerance is deliberately tight: a genuinely live, unquantised take
    drifts, and rounding it would invent a precision the recording never had.
    """
    if bpm is None:
        return None
    nearest = round(bpm)
    return nearest if abs(bpm - nearest) <= tolerance else None


def detect_key(chroma):
    """Name the key by correlating average chroma with all 24 K-S profiles.

    Returns the best fit, the runner-up, and the gap between them. The gap is
    the honest confidence signal: a modal or key-changing track scores several
    candidates almost equally, and saying so is more use than a bare answer.
    """
    import numpy as np

    weights = chroma.mean(axis=1)
    if not np.any(weights):
        return None

    scored = []
    for tonic in range(12):
        for name, profile in (("major", MAJOR_PROFILE), ("minor", MINOR_PROFILE)):
            rotated = np.roll(np.asarray(profile, dtype=float), tonic)
            # Pearson correlation; both vectors are centred first so the result
            # reflects shape rather than overall loudness.
            a = weights - weights.mean()
            b = rotated - rotated.mean()
            denominator = math.sqrt(float((a * a).sum()) * float((b * b).sum()))
            scored.append((float((a * b).sum() / denominator) if denominator else 0.0,
                           tonic, name))

    scored.sort(reverse=True)
    score, tonic, mode = scored[0]
    runner_score, runner_tonic, runner_mode = scored[1]
    camelot = (CAMELOT_MAJOR if mode == "major" else CAMELOT_MINOR)[tonic]
    return {
        "key": f"{PITCH_CLASSES[tonic]} {mode}",
        "short": PITCH_CLASSES[tonic] + ("maj" if mode == "major" else "min"),
        "camelot": camelot,
        "correlation": round(score, 3),
        "runner_up": f"{PITCH_CLASSES[runner_tonic]} {runner_mode}",
        "margin": round(score - runner_score, 3),
    }


def analyse(path):
    """BPM and key for a WAV on disk."""
    import librosa
    import numpy as np

    audio, rate = librosa.load(path, sr=ANALYSIS_SR, mono=True)
    duration = float(len(audio)) / rate
    if duration < 2.0:
        return {"duration_seconds": round(duration, 2),
                "error": "too short to analyse (under 2s)"}

    coarse, beats = librosa.beat.beat_track(y=audio, sr=rate, hop_length=HOP)
    beat_times = librosa.frames_to_time(beats, sr=rate, hop_length=HOP)

    fitted = fit_tempo(beat_times)
    folded, factor = fold_tempo(fitted)
    whole = snap(folded)

    chroma = librosa.feature.chroma_cqt(y=audio, sr=rate, hop_length=HOP)
    stability = tempo_stability(beat_times, fitted)

    return {
        "duration_seconds": round(duration, 2),
        "beat_times": [round(float(t), 6) for t in beat_times],
        "stability": stability,
        "bpm": round(folded, 2) if folded else None,
        "bpm_whole": whole,
        "bpm_raw_fit": round(fitted, 3) if fitted else None,
        "bpm_librosa": round(float(np.atleast_1d(coarse)[0]), 2),
        "bpm_octave_factor": factor,
        "beats_found": int(len(beat_times)),
        "key": detect_key(chroma),
    }


# --- Naming and output -------------------------------------------------------


def root_midi_note(key):
    """MIDI note for a key's tonic, for the ACID chunk's root-note field.

    Octave 4 (C = 60) by convention. The mode is not encoded -- ACID stores a
    root, not a scale -- so a minor key contributes only its tonic.
    """
    if not key:
        return None
    try:
        tonic = PITCH_CLASSES.index(key["key"].split()[0])
    except (ValueError, KeyError, IndexError):
        return None
    return 60 + tonic


def embed_markers(path, result):
    """Put the beat grid and the tempo inside the WAV itself.

    This is the difference between a file you have to tell FL Studio about and
    one that explains itself: Edison shows the cue points, and the ACID chunk
    means the tempo and root note arrive with the sample rather than being typed
    in from the filename.
    """
    rate = wav_sample_rate(path)
    if not rate or not result:
        return None
    beat_times = result.get("beat_times") or []
    if not beat_times:
        return None

    # On a machine-timed track the detected beats are the true grid plus up to
    # half a frame of noise, so laying markers where they were *detected* puts
    # them up to 11ms out -- audible as flam when you slice. Regenerating an
    # even grid from the fitted tempo, anchored on the first beat, is both more
    # accurate and what a slicer expects. A drifting take gets its real
    # positions, because there the wobble is the information.
    stability = result.get("stability")
    bpm = result.get("bpm_whole") or result.get("bpm")
    if stability and stability.get("steady") and bpm:
        period = 60.0 / bpm
        anchor = beat_times[0]
        span = beat_times[-1] - anchor
        count = int(round(span / period)) + 1
        grid = [anchor + n * period for n in range(count)]
        positions = [int(round(t * rate)) for t in grid]
    else:
        positions = [int(round(t * rate)) for t in beat_times]
    acid = None
    bpm = result.get("bpm_whole") or result.get("bpm")
    if bpm:
        acid = {
            "tempo": float(bpm),
            "beats": len(positions),
            "root_note": root_midi_note(result.get("key")),
        }
    annotate_wav(path, cue_positions=positions, acid=acid)
    return {"cues": len(positions), "acid": bool(acid)}


def safe_name(text, limit=80):
    """A filename that survives every OS, keeping the title readable."""
    text = re.sub(r"[\\/:*?\"<>|]", "", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return (text[:limit].rstrip() or "audio")


def build_name(title, result, tag=True):
    """`Title - 128BPM - Amin.wav`, the convention sample libraries sort by."""
    stem = safe_name(title)
    if not tag or not result:
        return stem + ".wav"
    bits = []
    bpm = result.get("bpm_whole") or result.get("bpm")
    if bpm:
        bits.append(f"{bpm:g}BPM")
    key = result.get("key")
    if key:
        bits.append(key["short"])
    return " - ".join([stem] + bits) + ".wav"


def report(title, path, source, result, trimmed=None, beats_file=None,
           markers=None):
    lines = [f"{title}", f"  file     {path}"]
    codec = source.get("codec_name")
    if codec:
        rate = source.get("sample_rate")
        bitrate = source.get("bit_rate")
        detail = f"  source   {codec}"
        if rate:
            detail += f" {int(rate) / 1000:g}kHz"
        if bitrate and str(bitrate).isdigit():
            detail += f" {int(bitrate) // 1000}kbps"
        lines.append(detail + f"  ->  {PCM_CODEC}")

    if trimmed:
        start, finish, original = trimmed
        removed = original - (finish - start)
        if removed > 0.05:
            lines.append(f"  trimmed  {start:.2f}s off the head, "
                         f"{original - finish:.2f}s off the tail "
                         f"({removed:.2f}s of silence)")

    if not result:
        return "\n".join(lines) + "\n"

    if result.get("error"):
        lines.append(f"  analysis {result['error']}")
        return "\n".join(lines) + "\n"

    bpm = result.get("bpm")
    if bpm:
        whole = result.get("bpm_whole")
        shown = f"{whole:g}" if whole else f"{bpm:.2f}"
        note = "" if whole else "  (not a whole tempo -- live or drifting?)"
        if result.get("bpm_octave_factor", 1) != 1:
            note += f"  [folded x{result['bpm_octave_factor']}]"
        lines.append(f"  bpm      {shown}{note}")
        if whole and abs(bpm - whole) > 0.005:
            lines.append(f"           fit {bpm:.2f}, {result['beats_found']} beats")
    else:
        lines.append("  bpm      no steady pulse found")

    stability = result.get("stability")
    if stability and not stability["steady"]:
        first, second = stability["first_half_bpm"], stability["second_half_bpm"]
        if stability.get("ramps"):
            lines.append(f"           tempo is not fixed: {first:g} -> {second:g} BPM "
                         f"across the track (drift {stability['drift_bpm']:g})")
        if stability.get("wobble"):
            fraction = stability.get("residual_fraction_of_beat", 0)
            lines.append(f"           beats sit {fraction:.0%} of a beat off a straight "
                         f"grid -- loose timing, or the tracker is on the wrong pulse")
            if bpm:
                lines.append(f"           if it looks wrong, try {bpm / 2:.1f} or "
                             f"{bpm * 2:.1f} BPM")
        if not beats_file:
            lines.append("           --beats writes a marker per beat, which is "
                         "what you want here rather than one number")

    if markers:
        detail = f"{markers['cues']} cue points embedded"
        if markers["acid"]:
            detail += " + ACID tempo/root chunk (FL Studio reads both)"
        lines.append(f"  markers  {detail}")
    if beats_file:
        lines.append(f"  beats    {os.path.basename(beats_file)}  "
                     f"({result['beats_found']} labels)")

    key = result.get("key")
    if key:
        confidence = "clear" if key["margin"] >= 0.08 else "ambiguous"
        lines.append(f"  key      {key['key']}  ({key['camelot']})  {confidence}")
        if confidence == "ambiguous":
            lines.append(f"           close second: {key['runner_up']}"
                         f"  (margin {key['margin']})")
    return "\n".join(lines) + "\n"


# --- Entry point -------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        description="YouTube link -> WAV, with BPM and key.",
    )
    # Optional so --verify works on its own; main() enforces it otherwise.
    parser.add_argument("source", nargs="?",
                        help="a video URL, or an audio file with --analyse-only")
    parser.add_argument("-o", "--out-dir", default=".", help="where the WAV goes")
    parser.add_argument("--section", metavar="START-END",
                        help="grab only this span, e.g. 1:04-1:32")
    parser.add_argument("--sample-rate", type=int,
                        help="force a rate; default keeps the source's")
    parser.add_argument("--name", help="override the output filename stem")
    parser.add_argument("--no-tag", action="store_true",
                        help="do not put the BPM and key in the filename")
    parser.add_argument("--no-trim", action="store_true",
                        help="keep the silence at the head and tail")
    parser.add_argument("--silence-db", type=float, default=SILENCE_TOP_DB,
                        metavar="DB",
                        help=f"how far below peak counts as silence "
                             f"(default {SILENCE_TOP_DB:g})")
    parser.add_argument("--no-markers", action="store_true",
                        help="do not embed cue points and the ACID tempo chunk "
                             "in the WAV (they are on by default; FL Studio "
                             "reads both, and other readers ignore them)")
    parser.add_argument("--beats", action="store_true",
                        help="also write <name>.beats.txt, an Audacity label "
                             "track, for tools that do not read WAV cues")
    parser.add_argument("--no-analyse", action="store_true",
                        help="just the WAV, skip BPM and key")
    parser.add_argument("--analyse-only", action="store_true",
                        help="analyse an existing file, download nothing")
    parser.add_argument("--json", action="store_true",
                        help="print the analysis as JSON instead of text")
    parser.add_argument("--sidecar", action="store_true",
                        help="also write <name>.json next to the WAV")
    parser.add_argument("--verify", action="store_true",
                        help="check the external tools, then exit")
    return parser


def mode_verify():
    rows = []
    for tool in ("yt-dlp", "ffmpeg", "ffprobe"):
        path = shutil.which(tool)
        version = ""
        if path:
            try:
                # ffmpeg and ffprobe use -version; yt-dlp uses --version.
                flag = "--version" if tool == "yt-dlp" else "-version"
                version = subprocess.run([tool, flag], capture_output=True,
                                         text=True).stdout.strip().splitlines()[0]
            except (OSError, IndexError):
                version = "(no version output)"
        rows.append((tool, "PASS" if path else "FAIL", version[:48]))
    try:
        import librosa
        import numpy
        rows.append(("librosa", "PASS", librosa.__version__))
        rows.append(("numpy", "PASS", numpy.__version__))
    except ImportError as exc:
        rows.append(("librosa / numpy", "FAIL", str(exc)))
    for name, status, detail in rows:
        print(f"{name:<18} {status:<5} {detail}")
    return 0 if all(s == "PASS" for _, s, _ in rows) else 1


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.verify:
        return mode_verify()

    if not args.source:
        print("error: give a URL, or a file with --analyse-only", file=sys.stderr)
        return 1

    section = None
    if args.section:
        try:
            section = parse_section(args.section)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    os.makedirs(args.out_dir, exist_ok=True)

    try:
        if args.analyse_only:
            require("ffmpeg", "ffprobe")
            if not os.path.exists(args.source):
                print(f"error: no such file: {args.source}", file=sys.stderr)
                return 1
            source_info = probe(args.source)
            result = None if args.no_analyse else analyse(args.source)
            beats_file = None
            if args.beats and result and result.get("beat_times"):
                beats_file = os.path.splitext(args.source)[0] + ".beats.txt"
                write_beat_labels(beats_file, result["beat_times"])
            if args.json:
                print(json.dumps({"beats_file": beats_file,
                                  "analysis": result or {}}, indent=2))
            else:
                print(report(os.path.basename(args.source), args.source,
                             source_info, result, None, beats_file), end="")
            return 0

        require("yt-dlp", "ffmpeg", "ffprobe")
        with tempfile.TemporaryDirectory() as workdir:
            title = args.name or video_title(args.source)
            downloaded = download_audio(args.source, workdir, section)
            source_info = probe(downloaded)
            # Decode into the temp dir first: the final name depends on the
            # analysis, and analysing the finished WAV avoids a second decode.
            staged = to_wav(downloaded, os.path.join(workdir, "out.wav"),
                            args.sample_rate)

            # Trim before analysing: leading silence drags the first beat and
            # a long fade-out skews the tempo fit.
            trimmed = None
            if not args.no_trim:
                bounds = find_sound(staged, args.silence_db)
                if bounds and (bounds[0] > 0.01 or bounds[1] < bounds[2] - 0.01):
                    staged = trim_wav(staged, os.path.join(workdir, "trim.wav"),
                                      bounds)
                    trimmed = bounds

            result = None if args.no_analyse else analyse(staged)

            filename = build_name(title, result, tag=not args.no_tag)
            destination = os.path.join(args.out_dir, filename)
            shutil.move(staged, destination)

            markers = None
            if not args.no_markers and result:
                markers = embed_markers(destination, result)

            beats_file = None
            if args.beats and result and result.get("beat_times"):
                beats_file = os.path.splitext(destination)[0] + ".beats.txt"
                write_beat_labels(beats_file, result["beat_times"])

            if args.sidecar and result:
                sidecar = os.path.splitext(destination)[0] + ".json"
                with open(sidecar, "w", encoding="utf-8") as handle:
                    json.dump({"title": title, "url": args.source,
                               "source": source_info, "analysis": result},
                              handle, indent=2)
                    handle.write("\n")

            if args.json:
                print(json.dumps({"file": destination, "beats_file": beats_file,
                                  "analysis": result}, indent=2))
            else:
                print(report(title, destination, source_info, result,
                             trimmed, beats_file, markers), end="")
        return 0

    except ToolMissing as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (DownloadError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
