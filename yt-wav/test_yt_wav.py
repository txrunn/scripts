#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["librosa>=1.0", "numpy>=2.0"]
# ///
"""Offline tests for yt_wav.

Nothing is downloaded. Audio is synthesised in-memory at known tempos and in a
known key, so the estimators are checked against ground truth rather than
against whatever they happened to return last time.
"""

import os
import struct
import sys
import tempfile
import unittest
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import yt_wav as yw  # noqa: E402

SR = 44100


def render(beat_times, duration, chord=(220.0, 261.63, 329.63)):
    """Clicks on the beat over a sustained triad. Default chord is A minor."""
    t = np.arange(int(SR * duration)) / SR
    y = np.zeros_like(t)
    for n, when in enumerate(beat_times):
        i = int(when * SR)
        if i + 4000 >= len(y):
            break
        env = np.exp(-np.arange(4000) / (260.0 if n % 4 == 0 else 130.0))
        tone = np.sin(2 * np.pi * (70 if n % 4 == 0 else 1400) * np.arange(4000) / SR)
        y[i:i + 4000] += (1.0 if n % 4 == 0 else 0.55) * env * tone
    for f in chord:
        y += 0.10 * np.sin(2 * np.pi * f * t)
    return y


def steady_beats(bpm, duration):
    return [n * 60.0 / bpm for n in range(int(duration * bpm / 60.0))]


class Tmp:
    def __init__(self, audio, pad_head=0.0, pad_tail=0.0):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "a.wav")
        if pad_head or pad_tail:
            audio = np.concatenate([np.zeros(int(pad_head * SR)), audio,
                                    np.zeros(int(pad_tail * SR))])
        peak = max(1e-9, np.abs(audio).max())
        data = np.clip(audio / peak * 0.89, -1, 1)
        with wave.open(self.path, "w") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(SR)
            handle.writeframes((data * 32767).astype("<i2").tobytes())

    def close(self):
        self.dir.cleanup()


# --- Tempo -------------------------------------------------------------------


class TempoFitTests(unittest.TestCase):
    """The fit is the whole reason this tool is worth using over a guess."""

    def test_fit_recovers_the_exact_tempo(self):
        for bpm in (90.0, 120.0, 128.0, 140.0, 174.0):
            times = np.array(steady_beats(bpm, 60.0))
            self.assertAlmostEqual(yw.fit_tempo(times), bpm, places=6)

    def test_fit_beats_the_median_interval_under_quantisation(self):
        # Beat times quantised to an 11.6ms frame, as librosa returns them.
        bpm, frame = 174.0, yw.HOP / yw.ANALYSIS_SR
        true = np.array(steady_beats(bpm, 60.0))
        quantised = np.round(true / frame) * frame
        median = 60.0 / np.median(np.diff(quantised))
        fitted = yw.fit_tempo(quantised)
        self.assertLess(abs(fitted - bpm), 0.05)
        self.assertLess(abs(fitted - bpm), abs(median - bpm))

    def test_too_few_beats_gives_none_rather_than_a_guess(self):
        self.assertIsNone(yw.fit_tempo(np.array([0.0, 0.5, 1.0])))

    def test_non_increasing_times_give_none(self):
        self.assertIsNone(yw.fit_tempo(np.array([3.0, 2.0, 1.0, 0.0])))


class FoldTests(unittest.TestCase):
    def test_half_time_is_doubled_into_range(self):
        folded, factor = yw.fold_tempo(65.0)
        self.assertAlmostEqual(folded, 130.0)
        self.assertEqual(factor, 2)

    def test_double_time_is_halved_into_range(self):
        folded, _ = yw.fold_tempo(240.0)
        self.assertAlmostEqual(folded, 120.0)

    def test_a_tempo_already_in_range_is_untouched(self):
        folded, factor = yw.fold_tempo(128.0)
        self.assertAlmostEqual(folded, 128.0)
        self.assertEqual(factor, 1)

    def test_drum_and_bass_is_not_folded_down(self):
        folded, factor = yw.fold_tempo(174.0)
        self.assertAlmostEqual(folded, 174.0)
        self.assertEqual(factor, 1)

    def test_none_survives(self):
        self.assertEqual(yw.fold_tempo(None), (None, 1))


class SnapTests(unittest.TestCase):
    def test_close_enough_snaps_to_a_whole_number(self):
        self.assertEqual(yw.snap(127.997), 128)
        self.assertEqual(yw.snap(128.02), 128)

    def test_a_genuinely_odd_tempo_is_not_rounded(self):
        # A live take that sits at 123.4 should not be claimed as 123.
        self.assertIsNone(yw.snap(123.4))

    def test_none_survives(self):
        self.assertIsNone(yw.snap(None))


class StabilityTests(unittest.TestCase):
    def test_a_machine_steady_track_reads_as_steady(self):
        times = np.array(steady_beats(128.0, 60.0))
        result = yw.tempo_stability(times, 128.0)
        self.assertTrue(result["steady"])
        self.assertLess(result["drift_bpm"], 0.5)

    def test_a_drifting_take_is_flagged(self):
        times, now = [], 0.0
        for n in range(90):
            times.append(now)
            now += 60.0 / (116.0 + 8.0 * n / 90.0)
        result = yw.tempo_stability(np.array(times), 120.0)
        self.assertFalse(result["steady"])
        self.assertGreater(result["drift_bpm"], 2.0)
        self.assertLess(result["first_half_bpm"], result["second_half_bpm"])

    def test_scattered_beats_are_not_steady_even_when_the_halves_agree(self):
        # The real-world failure: a track whose beats scatter by most of a beat
        # while both halves average to the same tempo. Calling that steady made
        # the marker grid pure fiction.
        rng = np.random.default_rng(7)
        true = np.array(steady_beats(172.0, 120.0))
        scattered = true + rng.normal(0, 0.204, len(true))
        scattered.sort()
        result = yw.tempo_stability(scattered, 172.0)
        self.assertFalse(result["steady"])
        self.assertTrue(result["wobble"])
        self.assertLess(result["drift_bpm"], 2.0, "the halves do agree")

    def test_the_same_residual_is_judged_against_the_beat_period(self):
        # 100ms of scatter is nothing at 60 BPM and ruinous at 180.
        rng = np.random.default_rng(3)
        for bpm, expected_steady in ((60.0, True), (180.0, False)):
            times = np.array(steady_beats(bpm, 160.0))
            times = times + rng.normal(0, 0.030, len(times))
            times.sort()
            self.assertEqual(yw.tempo_stability(times, bpm)["steady"],
                             expected_steady, f"at {bpm} BPM")

    def test_too_few_beats_gives_none(self):
        self.assertIsNone(yw.tempo_stability(np.array([0.0, 0.5, 1.0]), 120.0))


# --- Key ---------------------------------------------------------------------


class KeyTests(unittest.TestCase):
    def chroma_for(self, pitch_classes, strength=1.0):
        chroma = np.full((12, 4), 0.05)
        for pc in pitch_classes:
            chroma[pc, :] = strength
        return chroma

    def test_a_minor_triad_reads_as_a_minor(self):
        result = yw.detect_key(self.chroma_for([9, 0, 4]))   # A C E
        self.assertEqual(result["key"], "A minor")
        self.assertEqual(result["camelot"], "8A")

    def test_a_c_major_triad_reads_as_c_major(self):
        result = yw.detect_key(self.chroma_for([0, 4, 7]))   # C E G
        self.assertEqual(result["key"], "C major")
        self.assertEqual(result["camelot"], "8B")

    def test_silence_gives_no_key_rather_than_c_major(self):
        self.assertIsNone(yw.detect_key(np.zeros((12, 4))))

    def test_the_margin_reports_ambiguity(self):
        # Weighted by the profile itself: as tonal as input gets.
        tonal = np.tile(np.asarray(yw.MAJOR_PROFILE, dtype=float)[:, None], (1, 4))
        flat = np.full((12, 4), 0.5)
        self.assertGreater(yw.detect_key(tonal)["margin"],
                           yw.detect_key(flat)["margin"])

    def test_featureless_chroma_has_no_margin_at_all(self):
        # Every profile correlates equally with a flat spectrum, so the tool
        # must not pretend one key won.
        self.assertEqual(yw.detect_key(np.full((12, 4), 0.5))["margin"], 0.0)

    def test_a_bare_triad_is_honestly_reported_as_ambiguous(self):
        # C-E-G fits C major and its relative A minor almost equally, and it
        # should say so rather than pick one confidently.
        result = yw.detect_key(self.chroma_for([0, 4, 7]))
        self.assertEqual(result["key"], "C major")
        self.assertLess(result["margin"], 0.08)

    def test_camelot_neighbours_are_relative_keys(self):
        # A minor and C major are relatives and share a Camelot number.
        minor = yw.detect_key(self.chroma_for([9, 0, 4]))
        major = yw.detect_key(self.chroma_for([0, 4, 7]))
        self.assertEqual(minor["camelot"][:-1], major["camelot"][:-1])

    def test_root_note_is_the_tonic_in_octave_four(self):
        self.assertEqual(yw.root_midi_note({"key": "C major"}), 60)
        self.assertEqual(yw.root_midi_note({"key": "A minor"}), 69)
        self.assertIsNone(yw.root_midi_note(None))


# --- Timestamps and naming ---------------------------------------------------


class TimestampTests(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(yw.parse_timestamp("83"), 83.0)
        self.assertEqual(yw.parse_timestamp("1:23"), 83.0)
        self.assertEqual(yw.parse_timestamp("1:02:03"), 3723.0)

    def test_section_range(self):
        self.assertEqual(yw.parse_section("1:04-1:32"), (64.0, 92.0))

    def test_a_backwards_section_is_rejected(self):
        with self.assertRaises(ValueError):
            yw.parse_section("2:00-1:00")

    def test_a_section_without_a_dash_is_rejected(self):
        with self.assertRaises(ValueError):
            yw.parse_section("1:04")


class NamingTests(unittest.TestCase):
    def test_bpm_and_key_go_in_the_filename(self):
        name = yw.build_name("Some Track", {"bpm_whole": 128, "bpm": 128.02,
                                            "key": {"short": "Amin"}})
        self.assertEqual(name, "Some Track - 128BPM - Amin.wav")

    def test_path_hostile_characters_are_removed(self):
        self.assertNotIn("/", yw.build_name("A/B:C?", None))
        self.assertNotIn(":", yw.build_name("A/B:C?", None))

    def test_no_tag_leaves_the_stem_alone(self):
        self.assertEqual(yw.build_name("Track", {"bpm_whole": 128}, tag=False),
                         "Track.wav")

    def test_an_empty_title_still_produces_a_filename(self):
        self.assertTrue(yw.build_name("...", None).endswith(".wav"))


# --- WAV chunks --------------------------------------------------------------


class ChunkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Tmp(render(steady_beats(128.0, 20.0), 20.0))
        self.addCleanup(self.tmp.close)

    def test_cue_points_round_trip(self):
        yw.annotate_wav(self.tmp.path, cue_positions=[0, 44100, 88200])
        cues, _ = yw.read_wav_markers(self.tmp.path)
        self.assertEqual(cues, [0, 44100, 88200])

    def test_acid_chunk_round_trips_tempo_and_root(self):
        yw.annotate_wav(self.tmp.path,
                        acid={"tempo": 128.0, "beats": 42, "root_note": 69})
        _, acid = yw.read_wav_markers(self.tmp.path)
        self.assertAlmostEqual(acid["tempo"], 128.0, places=3)
        self.assertEqual(acid["beats"], 42)
        self.assertEqual(acid["root_note"], 69)
        self.assertEqual(acid["meter"], (4, 4))

    def test_acid_flags_mark_stretch_and_root(self):
        yw.annotate_wav(self.tmp.path,
                        acid={"tempo": 120.0, "beats": 8, "root_note": 60})
        _, acid = yw.read_wav_markers(self.tmp.path)
        self.assertTrue(acid["flags"] & 0x10, "stretch bit")
        self.assertTrue(acid["flags"] & 0x02, "root-note bit")

    def test_the_file_is_still_a_readable_wav(self):
        yw.annotate_wav(self.tmp.path, cue_positions=[0, 1000],
                        acid={"tempo": 128.0, "beats": 4, "root_note": 60})
        with wave.open(self.tmp.path) as handle:
            self.assertEqual(handle.getframerate(), SR)
            self.assertGreater(handle.getnframes(), 0)

    def test_audio_data_is_unchanged_by_annotation(self):
        with wave.open(self.tmp.path) as handle:
            before = handle.readframes(handle.getnframes())
        yw.annotate_wav(self.tmp.path, cue_positions=[0, 1000])
        with wave.open(self.tmp.path) as handle:
            self.assertEqual(handle.readframes(handle.getnframes()), before)

    def test_annotating_twice_does_not_duplicate_chunks(self):
        # Two tempo chunks in one file is undefined; readers disagree.
        for _ in range(2):
            yw.annotate_wav(self.tmp.path, cue_positions=[0, 500],
                            acid={"tempo": 100.0, "beats": 2, "root_note": 60})
        with open(self.tmp.path, "rb") as handle:
            raw = handle.read()
        self.assertEqual(raw.count(b"acid"), 1)
        self.assertEqual(raw.count(b"cue "), 1)

    def test_sample_rate_is_read_from_the_fmt_chunk(self):
        self.assertEqual(yw.wav_sample_rate(self.tmp.path), SR)

    def test_a_non_riff_file_is_rejected(self):
        path = os.path.join(self.tmp.dir.name, "not.wav")
        with open(path, "wb") as handle:
            handle.write(b"this is not a wav")
        with self.assertRaises(ValueError):
            list(yw._chunks(open(path, "rb").read()))


# --- End to end on synthetic audio ------------------------------------------


class AnalysisTests(unittest.TestCase):
    def test_a_128_bpm_a_minor_track_is_identified(self):
        tmp = Tmp(render(steady_beats(128.0, 40.0), 40.0))
        self.addCleanup(tmp.close)
        result = yw.analyse(tmp.path)
        self.assertEqual(result["bpm_whole"], 128)
        self.assertEqual(result["key"]["key"], "A minor")
        self.assertTrue(result["stability"]["steady"])

    def test_silence_at_the_ends_is_found(self):
        tmp = Tmp(render(steady_beats(128.0, 30.0), 30.0), pad_head=3.0, pad_tail=2.0)
        self.addCleanup(tmp.close)
        start, finish, duration = yw.find_sound(tmp.path)
        self.assertAlmostEqual(start, 3.0, delta=0.25)
        self.assertAlmostEqual(duration - finish, 2.0, delta=0.35)

    def test_a_track_with_no_silence_is_left_alone(self):
        tmp = Tmp(render(steady_beats(128.0, 20.0), 20.0))
        self.addCleanup(tmp.close)
        start, finish, duration = yw.find_sound(tmp.path)
        self.assertLess(start, 0.2)
        self.assertGreater(finish, duration - 0.6)

    def test_pure_silence_is_not_trimmed_to_nothing(self):
        tmp = Tmp(np.zeros(int(SR * 5)))
        self.addCleanup(tmp.close)
        self.assertIsNone(yw.find_sound(tmp.path))

    def test_a_steady_track_gets_an_even_marker_grid(self):
        # Detected beats carry frame noise; a slicer wants the true grid.
        tmp = Tmp(render(steady_beats(128.0, 40.0), 40.0))
        self.addCleanup(tmp.close)
        result = yw.analyse(tmp.path)
        yw.embed_markers(tmp.path, result)
        cues, acid = yw.read_wav_markers(tmp.path)
        gaps = np.diff(np.array(cues) / yw.wav_sample_rate(tmp.path))
        self.assertAlmostEqual(gaps.mean(), 60.0 / 128.0, places=4)
        self.assertLess(gaps.std() * 1000, 0.5, "grid should be near-perfectly even")
        self.assertAlmostEqual(acid["tempo"], 128.0, places=2)

    def test_a_drifting_track_keeps_its_real_beat_positions(self):
        times, now = [], 0.0
        while now < 45.0:
            times.append(now)
            now += 60.0 / (116.0 + 8.0 * len(times) / 90.0)
        tmp = Tmp(render(times, now + 1.0))
        self.addCleanup(tmp.close)
        result = yw.analyse(tmp.path)
        self.assertFalse(result["stability"]["steady"])
        yw.embed_markers(tmp.path, result)
        cues, _ = yw.read_wav_markers(tmp.path)
        gaps = np.diff(np.array(cues) / yw.wav_sample_rate(tmp.path))
        self.assertGreater(gaps.std() * 1000, 2.0,
                           "a drifting take must not be flattened to a grid")

    def test_the_whole_result_is_json_serialisable(self):
        # numpy scalars compare and print exactly like Python ones, so this
        # only fails at the point something asks for a sidecar -- which is
        # after the WAV has already been written.
        import json
        tmp = Tmp(render(steady_beats(128.0, 30.0), 30.0))
        self.addCleanup(tmp.close)
        result = yw.analyse(tmp.path)
        json.dumps(result)                       # must not raise
        self.assertIsInstance(result["stability"]["steady"], bool)
        self.assertNotIsInstance(result["stability"]["steady"], np.bool_)
        for key in ("bpm", "bpm_raw_fit", "bpm_librosa"):
            if result[key] is not None:
                self.assertIsInstance(result[key], float)

    def test_too_short_to_analyse_says_so(self):
        tmp = Tmp(np.sin(2 * np.pi * 440 * np.arange(int(SR * 0.5)) / SR))
        self.addCleanup(tmp.close)
        self.assertIn("error", yw.analyse(tmp.path))


class BeatLabelTests(unittest.TestCase):
    def test_labels_are_tab_separated_with_bar_numbers(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "b.txt")
            yw.write_beat_labels(path, [0.0, 0.5, 1.0, 1.5, 2.0])
            rows = [line.split("\t") for line in
                    open(path, encoding="utf-8").read().splitlines()]
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0][2], "1.1")
        self.assertEqual(rows[1][2], "2")
        self.assertEqual(rows[4][2], "2.1", "fifth beat starts bar two")


if __name__ == "__main__":
    unittest.main(verbosity=2)
