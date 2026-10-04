# yt-wav

A YouTube link in, a WAV out, with the BPM and key already worked out and
written into the file.

```bash
./yt_wav.py 'https://www.youtube.com/watch?v=...'
```

```
Some Track
  file     ./Some Track - 128BPM - Amin.wav
  source   opus 48kHz 160kbps  ->  pcm_s24le
  trimmed  3.14s off the head, 2.32s off the tail (5.46s of silence)
  bpm      128
           fit 127.96, 85 beats
  markers  85 cue points embedded + ACID tempo/root chunk (FL Studio reads both)
  key      A minor  (8A)  clear
```

Needs `yt-dlp` and `ffmpeg`. The Python dependencies are declared inside the
script, so `uv run` fetches them into a throwaway environment — there is nothing
to install and nothing to keep up to date.

```bash
brew install yt-dlp ffmpeg uv
./yt_wav.py --verify
```

---

## The three commands you actually need

```bash
./yt_wav.py '<url>'                      # whole thing
./yt_wav.py '<url>' --section 1:04-1:32  # just the break
./yt_wav.py existing.wav --analyse-only  # what BPM and key is this?
```

---

## Why the tempo number is trustworthy

Beat trackers report the tempo of the grid they found, and that grid is
quantised to analysis frames. At an 11.6ms frame, a 174 BPM track reads as
**172.27** — close enough to look right, wrong enough to ruin a time-stretch.
Taking the median gap between beats does not help; it inherits the same error.

The quantisation is zero-mean, though, so fitting a straight line through *every*
beat averages it away. Measured against synthetic tracks at 90, 120, 128, 140 and
174 BPM, with and without noise, the worst error was **0.005 BPM**:

| | 90 | 120 | 128 | 140 | 174 |
|---|---|---|---|---|---|
| beat tracker | 90.67 | 120.19 | 129.20 | 139.67 | 172.27 |
| median gap | 90.67 | 120.19 | 127.60 | 139.67 | 172.27 |
| **line fit** | **90.001** | **120.000** | **127.999** | **139.995** | **173.998** |

A fit within 0.15 of a whole number is reported as that whole number, because
music is overwhelmingly written at integer tempos. Outside that, the decimal is
shown as-is rather than rounded into a precision the recording never had.

Half- and double-time errors are folded back into 70–180 BPM, and the report
says when it did that rather than quietly presenting 87 as 174.

---

## Takes that do not hold a tempo

Anything cut before click tracks breathes, and one BPM number for it is a lie
that looks authoritative. Two signals decide whether to trust a single number:

- **Residual spread** around the fit, judged against the **beat period**. 200ms
  of scatter is meaningless at 60 BPM and catastrophic at 170, and the
  quantisation floor alone cannot tell those apart. Past about 8% of a beat, the
  grid is not tracking the music.
- **First half against second half**, fitted separately. A take that speeds up
  steadily has small residuals against its own average yet is genuinely two
  tempos; only this catches that.

Either one alone disqualifies. An earlier version required both, and a real
track whose beats scattered by 60x the quantisation floor was reported steady
purely because its two halves averaged to the same tempo — so the markers were
written as an even grid matching nothing in the audio. That is the worst thing
this tool could do, and there is a regression test for it.

When it is not steady, the report says so and gives both halves:

```
  bpm      120
           fit 119.93, 89 beats
           tempo is not fixed: 117.94 -> 121.9 BPM across the track (drift 3.95)
```

and the markers written into the file follow the **real** beats rather than a
grid, because there the wobble is the information you need.

A grid that is badly off tends to mean the tracker latched onto the wrong pulse
rather than that the music is loose, so the report offers the half and double:

```
  bpm      171.65  (not a whole tempo -- live or drifting?)
           beats sit 58% of a beat off a straight grid -- loose timing, or the tracker is on the wrong pulse
           if it looks wrong, try 85.8 or 343.3 BPM
```

Octave errors are the commonest way automatic tempo detection goes wrong, and
nothing here resolves them for you — it only tells you when it is unsure.

---

## What gets written into the WAV

Two standard RIFF chunks, on by default. A reader that knows neither skips them
and sees an ordinary WAV.

| Chunk | Carries | Read by |
|---|---|---|
| `cue ` | A marker on every beat | **FL Studio** (Edison), Reaper, Audition, Sound Forge, Audacity |
| `acid` | Tempo, beat count, 4/4, root note | **FL Studio**, Acid, Sound Forge |

The ACID chunk is why a sample can drop into FL already stretched and in key
instead of having the tempo typed in off the filename.

**On a steady track the markers are a regenerated grid, not the detected beats.**
Detected positions carry up to half a frame of error, which slices with an
audible flam; the fitted tempo is more accurate than any individual beat. A
drifting take keeps its real positions, for the reason above.

`--beats` additionally writes `<name>.beats.txt`, an Audacity label track, for
anything that does not read WAV cues.

### Serato

Serato runs its own analysis and stores cue points in its own tags, so it will
not see the cue chunks above — that is a Serato design decision, not something
this script can write around. What it does get is the filename, which its browser
shows and sorts on, carrying a BPM and key accurate enough that Serato's own
analysis agrees. The key is given in Camelot notation too (`8A`), which is what
the Serato and Rekordbox libraries use for harmonic matching.

---

## Audio quality

`bestaudio[acodec=opus]/bestaudio/best` — Opus at 48kHz is what YouTube serves
for most modern uploads and is the highest-fidelity option available; AAC is the
fallback on older uploads.

That stream is decoded straight to **24-bit PCM**, once. Twenty-four bits add no
information to a lossy source — they avoid *removing* any. The decoder emits
float, so writing 16-bit would requantise and need dither before the sample has
even been touched.

**The sample rate is deliberately not forced.** Opus is always 48kHz and AAC
usually 44.1kHz; resampling here would be a second avoidable transformation on
material that has already been through one. Pass `--sample-rate` if the project
needs a specific rate and you would rather ffmpeg did it than your DAW.

---

## Trimming

Leading and trailing silence is cut by default, 50dB below peak, with a 20ms pad
either side so the first attack transient survives — that is the one part of a
sample you cannot get back.

It happens **before** analysis, because leading silence drags the first beat and
a long fade-out skews the tempo fit. `--no-trim` keeps everything;
`--silence-db` moves the threshold.

A file that is silent all the way through is left alone rather than trimmed to
nothing.

---

## Key detection

Average chroma correlated against all 24 Krumhansl-Schmuckler profiles — the
perceived stability of each scale degree, from the 1982 probe-tone experiments.
Reported with the Camelot number and a confidence:

```
  key      A minor  (8A)  clear
  key      D major  (10B) ambiguous
           close second: B minor  (margin 0.03)
```

The margin between the best and second-best fit is the honest confidence signal.
Modal material, anything that changes key, and bare triads all score several
candidates nearly equally — a C-E-G triad fits C major and its relative A minor
about as well, and saying so is more use than picking one.

---

## Options

| Flag | Purpose |
|---|---|
| `--section START-END` | Only this span, e.g. `1:04-1:32`. Trimmed at download, so only that span is fetched. |
| `-o, --out-dir DIR` | Where the WAV goes. |
| `--name STEM` | Override the filename stem. |
| `--no-tag` | Leave the BPM and key out of the filename. |
| `--no-trim` | Keep the silence at the head and tail. |
| `--silence-db DB` | How far below peak counts as silence (default 50). |
| `--no-markers` | Do not embed the cue and ACID chunks. |
| `--beats` | Also write `<name>.beats.txt` as an Audacity label track. |
| `--sample-rate HZ` | Force a rate; the default keeps the source's. |
| `--no-analyse` | Just the WAV, no BPM or key. |
| `--analyse-only` | Analyse a file already on disk; download nothing. |
| `--json` / `--sidecar` | Machine-readable output, to stdout or beside the WAV. |
| `--verify` | Check the external tools, then exit. |

Exit `0` on success, `1` on a missing tool, a bad URL or a bad argument, with
the reason on stderr — yt-dlp and ffmpeg both explain themselves well, and their
stderr is passed through rather than swallowed.

---

## Tests

```bash
./test_yt_wav.py
```

50 tests, no network. Audio is synthesised at known tempos in a known key, so
the estimators are checked against ground truth rather than against whatever
they returned last time.

Coverage: the tempo fit (exact recovery at five tempos, and beating the median
interval under simulated frame quantisation); octave folding; whole-number
snapping, including refusing to round a genuinely odd tempo; drift detection on
a synthesised take that ramps 116 to 124; key detection, Camelot numbering,
relative-key adjacency, and a bare triad honestly reported as ambiguous; silence
trimming, including a track with no silence and a track that is nothing but;
RIFF round-trips for both chunks, audio data provably unchanged by annotation,
no duplicate chunks on re-annotation, and a non-RIFF file rejected; and that a
steady track gets an even marker grid while a drifting one keeps its real beats.

---

## Caveats

- **Beat tracking is not infallible, and octave errors are the usual failure.**
  Sparse, rubato or heavily syncopated material lands on the wrong pulse often.
  The wobble report is the tell: a grid sitting a large fraction of a beat off
  usually means the wrong pulse rather than loose playing, and the half and
  double are printed for you to judge. It does not pick between them.
- **Key detection assumes one key throughout.** A track that modulates gets the
  best single answer and, usually, a small margin saying so.
- **The downbeat is assumed, not detected.** Markers label every fourth beat as
  a bar line because most of this material is in four; it is something to count
  against, not an analysis.
- **24-bit from a lossy source is not more fidelity**, just no further loss.
- **Serato will not see the embedded cues.** See above.
