#!/usr/bin/env python3
"""Offline tests for alamo_new_films.

drafthouse.com is never contacted -- every test drives the script through
--from-file with a payload built in-memory, so the diff logic is provable
without the network.
"""

import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import alamo_new_films as anf  # noqa: E402

BRYANT = "2601"
CRYSTAL = "2602"

SOON = dt.datetime.now() + dt.timedelta(days=3)
LATER = dt.datetime.now() + dt.timedelta(days=10)
PAST = dt.datetime.now() - dt.timedelta(days=3)


def session(slug, cinema=BRYANT, when=SOON):
    return {
        "cinemaId": cinema,
        "presentationSlug": slug,
        "showTimeClt": when.isoformat(timespec="seconds"),
    }


def payload(films, sessions):
    """Build a schedule payload. `films` is slug -> title."""
    return {
        "data": {
            "cinemas": [
                {"id": BRYANT, "slug": "dc-bryant-street", "name": "DC Bryant Street"},
                {"id": CRYSTAL, "slug": "dc-crystal-city", "name": "DC Crystal City"},
            ],
            "presentations": [
                {"slug": slug, "show": {"title": title}} for slug, title in films.items()
            ],
            "sessions": sessions,
        }
    }


class ScriptTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state.json")
        self.reports = os.path.join(self.tmp.name, "reports")

    def run_script(self, data, *extra):
        """Run main() against `data`, returning (exit_code, stdout)."""
        feed = os.path.join(self.tmp.name, "feed.json")
        with open(feed, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = anf.main(
                ["--from-file", feed, "--state", self.state, "--report-dir", self.reports, *extra]
            )
        return code, buffer.getvalue()


class TestDiffing(ScriptTestCase):
    def test_first_run_seeds_baseline_without_listing_everything(self):
        data = payload(
            {"a": "Film A", "b": "Film B"},
            [session("a"), session("b")],
        )
        code, out = self.run_script(data)
        self.assertEqual(code, 0)
        self.assertIn("Baseline established: 2 film(s)", out)
        self.assertNotIn("Film A", out)
        self.assertTrue(os.path.exists(self.state))

    def test_force_report_lists_the_slate_on_a_cold_start(self):
        data = payload({"a": "Film A"}, [session("a")])
        code, out = self.run_script(data, "--force-report")
        self.assertEqual(code, 0)
        self.assertIn("Film A", out)

    def test_unchanged_schedule_prints_nothing(self):
        data = payload({"a": "Film A"}, [session("a")])
        self.run_script(data)
        code, out = self.run_script(data)
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_added_film_is_reported_once_then_never_again(self):
        before = payload({"a": "Film A"}, [session("a")])
        after = payload({"a": "Film A", "b": "Nosferatu"}, [session("a"), session("b")])

        self.run_script(before)

        code, out = self.run_script(after)
        self.assertEqual(code, 0)
        self.assertIn("1 new film bookable", out)
        self.assertIn("Nosferatu", out)
        self.assertNotIn("Film A", out)

        code, out = self.run_script(after)
        self.assertEqual(out, "")

    def test_removed_film_is_never_reported(self):
        before = payload({"a": "Film A", "b": "Film B"}, [session("a"), session("b")])
        after = payload({"a": "Film A"}, [session("a")])

        self.run_script(before)
        code, out = self.run_script(after)
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_returning_film_is_not_re_reported(self):
        full = payload({"a": "Film A", "b": "Film B"}, [session("a"), session("b")])
        reduced = payload({"a": "Film A"}, [session("a")])

        self.run_script(full)
        self.run_script(reduced)
        code, out = self.run_script(full)
        self.assertEqual(out, "", "a film that came back should not look new")

    def test_gap_in_runs_still_catches_the_addition(self):
        """Skipping days must not lose an addition -- the ledger is cumulative."""
        day1 = payload({"a": "Film A"}, [session("a")])
        day5 = payload({"a": "Film A", "z": "Late Addition"}, [session("a"), session("z")])

        self.run_script(day1)
        code, out = self.run_script(day5)
        self.assertIn("Late Addition", out)


class TestFiltering(ScriptTestCase):
    def test_other_cinemas_are_ignored(self):
        data = payload(
            {"a": "Bryant Film", "x": "Crystal Film"},
            [session("a", cinema=BRYANT), session("x", cinema=CRYSTAL)],
        )
        code, out = self.run_script(data, "--force-report")
        self.assertIn("Bryant Film", out)
        self.assertNotIn("Crystal Film", out)

    def test_past_showtimes_do_not_make_a_film_bookable(self):
        data = payload({"old": "Yesterday's Film"}, [session("old", when=PAST)])
        code, out = self.run_script(data, "--force-report")
        self.assertEqual(code, 0)
        self.assertNotIn("Yesterday's Film", out)

    def test_film_with_both_past_and_future_sessions_counts_only_future(self):
        data = payload(
            {"a": "Film A"},
            [session("a", when=PAST), session("a", when=SOON), session("a", when=LATER)],
        )
        code, out = self.run_script(data, "--force-report")
        self.assertIn("(2 upcoming)", out)

    def test_films_sorted_by_earliest_showtime(self):
        data = payload(
            {"late": "Later Film", "early": "Earlier Film"},
            [session("late", when=LATER), session("early", when=SOON)],
        )
        code, out = self.run_script(data, "--force-report")
        self.assertLess(out.index("Earlier Film"), out.index("Later Film"))

    def test_unparseable_showtime_is_skipped_not_fatal(self):
        data = payload({"a": "Film A", "b": "Film B"}, [session("a"), {
            "cinemaId": BRYANT, "presentationSlug": "b", "showTimeClt": "not-a-date"
        }])
        code, out = self.run_script(data, "--force-report")
        self.assertEqual(code, 0)
        self.assertIn("Film A", out)
        self.assertNotIn("Film B", out)


class TestOutputs(ScriptTestCase):
    def test_json_report_written_only_when_something_is_new(self):
        before = payload({"a": "Film A"}, [session("a")])
        after = payload({"a": "Film A", "b": "Film B"}, [session("a"), session("b")])

        self.run_script(before)
        self.run_script(before)
        self.assertFalse(os.path.isdir(self.reports), "quiet day should write no report")

        self.run_script(after)
        written = os.listdir(self.reports)
        self.assertEqual(len(written), 1)
        with open(os.path.join(self.reports, written[0]), encoding="utf-8") as handle:
            report = json.load(handle)
        self.assertEqual(report["count"], 1)
        self.assertEqual(report["films"][0]["slug"], "b")
        self.assertEqual(report["films"][0]["title"], "Film B")
        self.assertIn("dc-metro-area/show/b", report["films"][0]["url"])

    def test_dry_run_touches_no_state(self):
        data = payload({"a": "Film A"}, [session("a")])
        self.run_script(data, "--dry-run")
        self.assertFalse(os.path.exists(self.state))
        # Still a cold start, so the next real run seeds rather than reports.
        code, out = self.run_script(data)
        self.assertIn("Baseline established", out)


class TestRobustness(ScriptTestCase):
    def test_unknown_schema_fails_loudly(self):
        code, out = self.run_script({"something": "else"})
        self.assertEqual(code, 1, "an API change must not look like a quiet 'nothing new'")
        self.assertEqual(out, "")

    def test_corrupt_state_file_is_an_error_not_a_reset(self):
        with open(self.state, "w", encoding="utf-8") as handle:
            handle.write("{ this is not json")
        code, _ = self.run_script(payload({"a": "Film A"}, [session("a")]))
        self.assertEqual(code, 1)

    def test_unresolvable_cinema_is_an_error(self):
        data = payload({"a": "Film A"}, [session("a")])
        code, _ = self.run_script(data, "--match", "nonexistent-theater")
        self.assertEqual(code, 1)

    def test_explicit_cinema_id_overrides_matching(self):
        data = payload(
            {"a": "Bryant Film", "x": "Crystal Film"},
            [session("a", cinema=BRYANT), session("x", cinema=CRYSTAL)],
        )
        code, out = self.run_script(data, "--cinema-id", CRYSTAL, "--force-report")
        self.assertEqual(code, 0)
        self.assertIn("Crystal Film", out)
        self.assertNotIn("Bryant Film", out)

    def test_bad_cinema_id_is_rejected(self):
        data = payload({"a": "Film A"}, [session("a")])
        code, _ = self.run_script(data, "--cinema-id", "9999")
        self.assertEqual(code, 1)

    def test_top_level_payload_without_data_wrapper(self):
        wrapped = payload({"a": "Film A"}, [session("a")])
        code, out = self.run_script(wrapped["data"], "--force-report")
        self.assertEqual(code, 0)
        self.assertIn("Film A", out)


class TestUnits(unittest.TestCase):
    def test_title_falls_back_through_candidates(self):
        self.assertEqual(anf.presentation_title({"show": {"title": "A"}, "slug": "s"}), "A")
        self.assertEqual(anf.presentation_title({"title": "B", "slug": "s"}), "B")
        self.assertEqual(anf.presentation_title({"slug": "s"}), "s")

    def test_showtime_parsing_handles_z_suffix_and_junk(self):
        self.assertEqual(
            anf.parse_showtime("2026-09-01T19:30:00Z"), dt.datetime(2026, 9, 1, 19, 30)
        )
        self.assertEqual(
            anf.parse_showtime("2026-09-01T19:30:00"), dt.datetime(2026, 9, 1, 19, 30)
        )
        self.assertIsNone(anf.parse_showtime("soon"))
        self.assertIsNone(anf.parse_showtime(None))

    def test_sessions_can_identify_a_cinema_by_slug(self):
        key, value = anf.session_cinema_key({"cinemaSlug": "dc-bryant-street"})
        self.assertEqual((key, value), ("cinemaSlug", "dc-bryant-street"))

    def test_cinemas_discovered_from_sessions_when_no_cinema_list(self):
        sessions = [{"cinemaSlug": "dc-bryant-street", "presentationSlug": "a"}]
        cinemas = anf.collect_cinemas({"data": {}}, sessions)
        self.assertEqual(len(cinemas), 1)
        key, label = anf.resolve_cinema(cinemas, sessions, None, "bryant")
        self.assertEqual(key, "dc-bryant-street")

    def test_ambiguous_match_is_rejected(self):
        cinemas = [
            {"id": "1", "slug": "bryant-north", "name": "Bryant North"},
            {"id": "2", "slug": "bryant-south", "name": "Bryant South"},
        ]
        with self.assertRaises(anf.SchemaError):
            anf.resolve_cinema(cinemas, [], None, "bryant")


class TestFixture(unittest.TestCase):
    """The shipped sample must stay loadable and correctly shaped."""

    def test_sample_schedule_parses_and_filters(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "testdata", "sample_schedule.json")
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        presentations, sessions = anf.extract(data)
        cinemas = anf.collect_cinemas(data, sessions)
        key, label = anf.resolve_cinema(cinemas, sessions, None, "bryant")
        self.assertEqual(label, "DC Bryant Street")
        films = anf.upcoming_films(sessions, anf.index_presentations(presentations), key)
        self.assertEqual(set(films), {"the-thing-1982", "paddington-in-peru", "alien-1979"})
        self.assertEqual(films["the-thing-1982"]["session_count"], 2)
        self.assertEqual(films["alien-1979"]["session_count"], 1, "past session excluded")


if __name__ == "__main__":
    unittest.main()
