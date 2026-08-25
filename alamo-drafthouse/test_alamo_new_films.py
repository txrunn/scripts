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


def session(slug, cinema=BRYANT, when=SOON, status="ONSALE", hidden=False):
    return {
        "cinemaId": cinema,
        "presentationSlug": slug,
        "status": status,
        "isHidden": hidden,
        "showTimeClt": when.isoformat(timespec="seconds"),
    }


def payload(films, sessions, hidden_films=()):
    """Build a schedule payload shaped like the live feed. `films` is slug -> title."""
    return {
        "data": {
            "market": [
                {"id": BRYANT, "slug": "dc-bryant-street", "name": "DC Bryant Street"},
                {"id": CRYSTAL, "slug": "dc-crystal-city", "name": "DC Crystal City"},
            ],
            "presentations": [
                {"slug": slug, "show": {"title": title}, "isHidden": slug in hidden_films}
                for slug, title in films.items()
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


class TestBookability(ScriptTestCase):
    """You want to hear about a film when it goes on sale, not when it is announced."""

    def test_announced_but_not_on_sale_is_not_reported(self):
        data = payload(
            {"soon": "Not Yet Bookable"}, [session("soon", status="ANNOUNCED")]
        )
        code, out = self.run_script(data, "--force-report")
        self.assertEqual(code, 0)
        self.assertNotIn("Not Yet Bookable", out)

    def test_film_is_reported_on_the_day_it_goes_on_sale(self):
        announced = payload({"a": "Dune Part Three"}, [session("a", status="ANNOUNCED")])
        onsale = payload({"a": "Dune Part Three"}, [session("a", status="ONSALE")])

        code, out = self.run_script(announced)
        self.assertIn("Baseline established: 0 film(s)", out)

        code, out = self.run_script(onsale)
        self.assertIn("Dune Part Three", out)

    def test_sold_out_is_not_bookable(self):
        """Observed live at Bryant. A sold-out show is no use to a Season Pass."""
        data = payload({"a": "Sold Out Film"}, [session("a", status="SOLDOUT")])
        code, out = self.run_script(data, "--force-report")
        self.assertEqual(code, 0)
        self.assertNotIn("Sold Out Film", out)

    def test_film_appears_when_a_sold_out_show_reopens(self):
        sold_out = payload({"a": "Popular Film"}, [session("a", status="SOLDOUT")])
        reopened = payload(
            {"a": "Popular Film"},
            [session("a", when=SOON, status="SOLDOUT"), session("a", when=LATER, status="ONSALE")],
        )
        self.run_script(sold_out)
        code, out = self.run_script(reopened)
        self.assertIn("Popular Film", out)
        self.assertIn("(1 upcoming)", out, "only the on-sale session counts")

    def test_past_status_is_not_bookable(self):
        data = payload({"a": "Finished Film"}, [session("a", status="PAST")])
        code, out = self.run_script(data, "--force-report")
        self.assertNotIn("Finished Film", out)

    def test_hidden_session_is_not_bookable(self):
        data = payload({"a": "Hidden Show"}, [session("a", hidden=True)])
        code, out = self.run_script(data, "--force-report")
        self.assertNotIn("Hidden Show", out)

    def test_hidden_presentation_is_excluded(self):
        data = payload({"a": "Hidden Film"}, [session("a")], hidden_films={"a"})
        code, out = self.run_script(data, "--force-report")
        self.assertNotIn("Hidden Film", out)

    def test_status_flag_widens_the_bookable_set(self):
        data = payload({"a": "Announced Film"}, [session("a", status="ANNOUNCED")])
        code, out = self.run_script(data, "--force-report", "--status", "ANNOUNCED")
        self.assertIn("Announced Film", out)

    def test_status_all_ignores_status_entirely(self):
        data = payload({"a": "Whatever Film"}, [session("a", status="SOMETHING_NEW")])
        code, out = self.run_script(data, "--force-report", "--status", "ALL")
        self.assertIn("Whatever Film", out)

    def test_session_without_status_is_assumed_bookable(self):
        """Absence of the field is not evidence a session cannot be booked."""
        bare = {"cinemaId": BRYANT, "presentationSlug": "a", "showTimeClt": SOON.isoformat()}
        data = payload({"a": "Film A"}, [bare])
        code, out = self.run_script(data, "--force-report")
        self.assertIn("Film A", out)

    def test_mixed_statuses_count_only_the_bookable_ones(self):
        data = payload(
            {"a": "Film A"},
            [
                session("a", when=SOON, status="ONSALE"),
                session("a", when=LATER, status="ANNOUNCED"),
            ],
        )
        code, out = self.run_script(data, "--force-report")
        self.assertIn("(1 upcoming)", out)


class TestClassification(unittest.TestCase):
    """Special events lead; advance screenings trail. Slugs are real ones."""

    def tier(self, slug, title="X", **extra):
        return anf.classify({"slug": slug, "show": {"title": title}, **extra})

    def test_event_series_are_prioritized(self):
        for slug, expected in [
            ("film-club-rear-window", "Film Club"),
            ("mean-girls-movie-party", "Movie Party"),
            ("movie-party-the-outsiders-the-complete-novel", "Movie Party"),
            ("epic-sunday-tenet", "Epic Sunday"),
            ("terror-tuesday-the-faculty", "Terror Tuesday"),
            ("quote-along-monty-python-and-the-holy-grail", "Quote-Along"),
            ("special-event-star-trek-ii-the-wrath-of-khan-space-seed", "Special event"),
            ("queer-film-theory-101-the-birdcage", "Queer Film Theory 101"),
            ("sad-girl-cinema-club-my-sassy-girl", "Sad Girl Cinema Club"),
            ("live-q-a-ernie-emma", "Live Q&A"),
            ("the-twilight-saga-twilight-2008-fan-event", "Fan event"),
            ("terminator-2-judgment-day-35th-anniversary", "Anniversary"),
        ]:
            with self.subTest(slug=slug):
                self.assertEqual(self.tier(slug), (anf.TIER_EVENT, expected))

    def test_advance_screenings_are_deprioritized(self):
        for slug in [
            "advance-screening-the-dog-stars",
            "advance-screening-hope-2026-the-big-show-early-access",
            "advance-screening-dune-part-three-the-big-show-insider-screening",
        ]:
            with self.subTest(slug=slug):
                self.assertEqual(self.tier(slug), (anf.TIER_ADVANCE, "Advance screening"))

    def test_plain_releases_are_regular(self):
        for slug in ["dune-part-three", "avengers-doomsday", "nacho-libre", "rear-window"]:
            with self.subTest(slug=slug):
                self.assertEqual(self.tier(slug), (anf.TIER_REGULAR, None))

    def test_an_event_that_is_also_a_sneak_peek_ranks_as_an_event(self):
        """A one-off Anime Night is not made redundant by a later regular run."""
        tier, label = self.tier("crunchyroll-anime-night-sneak-peek-9-21-2026")
        self.assertEqual((tier, label), (anf.TIER_EVENT, "Anime Night"))

    def test_super_title_object_is_read(self):
        """superTitle is an object in the live feed, not a string."""
        self.assertEqual(
            self.tier(
                "plain-slug",
                superTitle={"superTitle": "FILM CLUB", "type": "COLLECTION", "slug": "film-club"},
            ),
            (anf.TIER_EVENT, "Film Club"),
        )

    def test_structured_fields_classify_when_the_slug_is_plain(self):
        """Alamo's own event fields are used, not just slug substrings."""
        self.assertEqual(
            self.tier("plain-slug", eventType="Movie Party"), (anf.TIER_EVENT, "Movie Party")
        )
        self.assertEqual(
            self.tier("plain-slug", event={"name": "Terror Tuesday"}),
            (anf.TIER_EVENT, "Terror Tuesday"),
        )

    def test_live_attribute_vocabulary_does_not_misclassify(self):
        """The four real attribute slugs must not trip any marker.

        advance-sales in particular sits on ordinary first-run films like
        Avengers: Doomsday and must not read as an advance screening.
        """
        for slug, attrs in [
            ("avengers-doomsday", ["advance-sales", "first-run"]),
            ("dune-part-three", ["advance-sales", "first-run"]),
            ("paw-patrol-the-dino-movie", ["family-friendly", "first-run"]),
            ("tony", ["first-run"]),
        ]:
            with self.subTest(slug=slug):
                self.assertEqual(
                    self.tier(slug, presentationAttributeSlugs=attrs),
                    (anf.TIER_REGULAR, None),
                )

    def test_drafthouse_recommends_is_not_an_event(self):
        """A COLLECTION superTitle is a shelf label, not a one-off event."""
        self.assertEqual(
            self.tier(
                "teenage-sex-and-death-at-camp-miasma",
                superTitle={
                    "superTitle": "Drafthouse Recommends",
                    "type": "COLLECTION",
                    "slug": "drafthouse-recommends",
                },
                presentationAttributeSlugs=["first-run"],
            ),
            (anf.TIER_REGULAR, None),
        )


class TestReportOrdering(ScriptTestCase):
    def test_events_lead_advance_screenings_trail(self):
        data = payload(
            {
                "advance-screening-x": "Preview Film",
                "plain-film": "Regular Film",
                "film-club-y": "Club Film",
            },
            [
                session("advance-screening-x", when=SOON),
                session("plain-film", when=SOON),
                session("film-club-y", when=LATER),
            ],
        )
        code, out = self.run_script(data, "--force-report")
        self.assertLess(out.index("Club Film"), out.index("Regular Film"))
        self.assertLess(out.index("Regular Film"), out.index("Preview Film"))
        self.assertIn("SPECIAL EVENTS", out)
        self.assertIn("ADVANCE SCREENINGS", out)
        self.assertIn("[Film Club]", out)

    def test_tier_beats_showtime(self):
        """A special event months out still leads a regular release tomorrow."""
        data = payload(
            {"film-club-y": "Club Film", "plain-film": "Regular Film"},
            [session("film-club-y", when=LATER), session("plain-film", when=SOON)],
        )
        code, out = self.run_script(data, "--force-report")
        self.assertLess(out.index("Club Film"), out.index("Regular Film"))

    def test_events_only_drops_the_rest(self):
        data = payload(
            {"film-club-y": "Club Film", "plain-film": "Regular Film"},
            [session("film-club-y"), session("plain-film")],
        )
        code, out = self.run_script(data, "--force-report", "--events-only")
        self.assertIn("Club Film", out)
        self.assertNotIn("Regular Film", out)

    def test_events_only_still_ledgers_everything(self):
        """Toggling the flag must not resurface a title a previous run showed."""
        data = payload(
            {"film-club-y": "Club Film", "plain-film": "Regular Film"},
            [session("film-club-y"), session("plain-film")],
        )
        self.run_script(data, "--force-report", "--events-only")
        code, out = self.run_script(data)
        self.assertEqual(out, "", "the regular film was already recorded, not new")

    def test_json_report_carries_the_tier(self):
        data = payload({"film-club-y": "Club Film"}, [session("film-club-y")])
        self.run_script(data, "--force-report")
        written = os.listdir(self.reports)
        with open(os.path.join(self.reports, written[0]), encoding="utf-8") as handle:
            report = json.load(handle)
        self.assertEqual(report["films"][0]["tier"], "event")
        self.assertEqual(report["films"][0]["event_label"], "Film Club")


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

    def test_error_messages_suggest_no_platform_specific_paths(self):
        """A hint that tells a Windows user to write to /tmp is a hint that fails."""
        source = os.path.join(os.path.dirname(os.path.abspath(anf.__file__)), "alamo_new_films.py")
        with open(source, encoding="utf-8") as handle:
            self.assertNotIn("/tmp/", handle.read())

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

    def test_cinemas_from_market_as_a_list(self):
        """data.market is a list in the live feed, not an object."""
        data = {
            "data": {
                "market": [
                    {"id": "1102", "slug": "dc-bryant-street", "name": "DC Bryant Street"},
                    {"id": "1103", "slug": "dc-crystal-city", "name": "DC Crystal City"},
                ],
                "presentations": [],
                "sessions": [],
            }
        }
        cinemas = anf.collect_cinemas(data, [])
        self.assertEqual(len(cinemas), 2)
        key, label = anf.resolve_cinema(cinemas, [], None, "bryant")
        self.assertEqual((key, label), ("1102", "DC Bryant Street"))

    def test_cinemas_from_market_list_wrapping_a_cinemas_key(self):
        """Or a list of market objects that each hold a cinema list."""
        data = {
            "data": {
                "market": [
                    {
                        "slug": "dc-metro-area",
                        "cinemas": [
                            {"id": "1102", "slug": "dc-bryant-street", "name": "DC Bryant Street"}
                        ],
                    }
                ],
                "presentations": [],
                "sessions": [],
            }
        }
        cinemas = anf.collect_cinemas(data, [])
        self.assertEqual(cinemas[0]["name"], "DC Bryant Street")

    def test_cinemas_from_market_as_an_object(self):
        data = {
            "data": {
                "market": {
                    "cinemas": [
                        {"id": "1102", "slug": "dc-bryant-street", "name": "DC Bryant Street"}
                    ]
                },
                "presentations": [],
                "sessions": [],
            }
        }
        cinemas = anf.collect_cinemas(data, [])
        self.assertEqual(cinemas[0]["id"], "1102")

    def test_sessions_keying_on_slug_while_list_uses_numeric_ids(self):
        """Cinema list and sessions need not agree on the identifier."""
        cinemas = [{"id": "2601", "slug": "dc-bryant-street", "name": "DC Bryant Street"}]
        sessions = [{"cinemaSlug": "dc-bryant-street", "presentationSlug": "a"}]
        key, label = anf.resolve_cinema(cinemas, sessions, None, "bryant")
        self.assertEqual(key, "dc-bryant-street")
        self.assertEqual(label, "DC Bryant Street")

    def test_cinema_matched_but_referenced_by_no_session_is_an_error(self):
        cinemas = [{"id": "2601", "slug": "dc-bryant-street", "name": "DC Bryant Street"}]
        sessions = [{"cinemaId": "9999", "presentationSlug": "a"}]
        with self.assertRaises(anf.SchemaError):
            anf.resolve_cinema(cinemas, sessions, None, "bryant")

    def test_ambiguous_match_is_rejected(self):
        cinemas = [
            {"id": "1", "slug": "bryant-north", "name": "Bryant North"},
            {"id": "2", "slug": "bryant-south", "name": "Bryant South"},
        ]
        with self.assertRaises(anf.SchemaError):
            anf.resolve_cinema(cinemas, [], None, "bryant")


class TestDefaultPaths(unittest.TestCase):
    """State belongs next to the script, in a gitignored directory."""

    def test_defaults_live_beside_the_script(self):
        script_dir = os.path.dirname(os.path.abspath(anf.__file__))
        self.assertTrue(anf.DEFAULT_STATE.startswith(script_dir))
        self.assertTrue(anf.DEFAULT_REPORT_DIR.startswith(script_dir))

    def test_defaults_are_absolute_so_cron_cwd_does_not_matter(self):
        """A scheduled task runs from an arbitrary cwd; relative paths would scatter state."""
        self.assertTrue(os.path.isabs(anf.DEFAULT_STATE))
        self.assertTrue(os.path.isabs(anf.DEFAULT_REPORT_DIR))

    def test_state_dir_is_gitignored(self):
        script_dir = os.path.dirname(os.path.abspath(anf.__file__))
        with open(os.path.join(script_dir, ".gitignore"), encoding="utf-8") as handle:
            self.assertIn("state/", handle.read().split())


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
        films = anf.upcoming_films(
            sessions,
            anf.index_presentations(presentations),
            key,
            hidden_slugs=anf.hidden_presentation_slugs(presentations),
        )
        self.assertEqual(
            set(films),
            {"the-thing-1982", "paddington-in-peru", "alien-1979"},
            "other cinema, announced-only, and hidden films must all be excluded",
        )
        self.assertEqual(films["the-thing-1982"]["session_count"], 2)
        self.assertEqual(films["alien-1979"]["session_count"], 1, "past session excluded")

    def test_sample_matches_the_real_session_shape(self):
        """Guard the fields the live feed was observed to use."""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "testdata", "sample_schedule.json")
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        sample = data["data"]["sessions"][0]
        for field in ("cinemaId", "sessionId", "presentationSlug", "status", "showTimeClt", "isHidden"):
            self.assertIn(field, sample)
        self.assertIn("show", data["data"]["presentations"][0])
        self.assertIn("slug", data["data"]["presentations"][0])


if __name__ == "__main__":
    unittest.main()
