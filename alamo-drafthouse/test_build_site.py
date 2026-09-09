#!/usr/bin/env python3
"""Tests for build_site.py.

Nothing here touches the network. TMDB is replaced with canned payloads, which
is the only way to test the parts that actually go wrong -- a search that picks
the wrong remake, a trailer ranking that prefers a teaser, a cache that forgets
it already looked something up.
"""

import contextlib
import datetime as dt
import io
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import alamo_new_films as alamo
import build_site


def film(title, tier=alamo.TIER_REGULAR, label=None, hours=None, count=1):
    """A film shaped the way upcoming_films() returns them."""
    base = dt.datetime(2026, 9, 10, 20, 0)
    times = [base + dt.timedelta(hours=h) for h in (hours or [0])]
    return {
        "title": title,
        "first_showtime": times[0],
        "session_count": count,
        "tier": tier,
        "label": label,
        "showtimes": times,
    }


class SplitYearTests(unittest.TestCase):
    def test_pulls_a_trailing_year(self):
        self.assertEqual(build_site.split_year("Nosferatu (1922)"), ("Nosferatu", 1922))

    def test_leaves_a_plain_title_alone(self):
        self.assertEqual(build_site.split_year("Taxi Driver"), ("Taxi Driver", None))

    def test_ignores_a_parenthetical_that_is_not_a_year(self):
        self.assertEqual(
            build_site.split_year("Mean Girls (Quote-Along)"),
            ("Mean Girls (Quote-Along)", None),
        )


class SearchScoringTests(unittest.TestCase):
    """The remake problem: Alamo screens the 1922 Nosferatu, TMDB ranks 2024."""

    RESULTS = {
        "results": [
            {"id": 1, "title": "Nosferatu", "original_title": "Nosferatu",
             "release_date": "2024-12-25", "popularity": 500.0},
            {"id": 2, "title": "Nosferatu", "original_title": "Nosferatu",
             "release_date": "1922-03-04", "popularity": 20.0},
        ]
    }

    def test_year_hint_beats_popularity(self):
        with mock.patch.object(build_site, "fetch", return_value=self.RESULTS):
            hit = build_site.tmdb_search("Nosferatu", 1922, "k")
        self.assertEqual(hit["id"], 2)

    def test_without_a_hint_popularity_breaks_the_tie(self):
        with mock.patch.object(build_site, "fetch", return_value=self.RESULTS):
            hit = build_site.tmdb_search("Nosferatu", None, "k")
        self.assertEqual(hit["id"], 1)

    def test_no_results_is_none_not_an_error(self):
        with mock.patch.object(build_site, "fetch", return_value={"results": []}):
            self.assertIsNone(build_site.tmdb_search("CatVideoFest 2026", None, "k"))

    def test_a_loose_match_with_no_year_is_rejected(self):
        # "Dismember the Alamo 2026" must not become "The Alamo". A fuzzy title
        # match with nothing corroborating it is worse than no poster.
        payload = {"results": [{"id": 9, "title": "The Alamo",
                                "original_title": "The Alamo",
                                "release_date": "2004-04-09", "popularity": 30.0}]}
        with mock.patch.object(build_site, "fetch", return_value=payload):
            self.assertIsNone(build_site.tmdb_search("Dismember the Alamo", None, "k"))

    def test_missing_results_key_is_a_schema_error(self):
        with mock.patch.object(build_site, "fetch", return_value={"movies": []}):
            with self.assertRaises(build_site.SchemaError):
                build_site.tmdb_search("Taxi Driver", None, "k")


class TrailerTests(unittest.TestCase):
    def test_prefers_an_official_trailer_over_a_teaser(self):
        payload = {"videos": {"results": [
            {"site": "YouTube", "key": "teas", "type": "Teaser", "official": True},
            {"site": "YouTube", "key": "trai", "type": "Trailer", "official": True},
        ]}}
        self.assertTrue(build_site.trailer_from(payload).endswith("trai"))

    def test_skips_non_youtube_sites(self):
        payload = {"videos": {"results": [
            {"site": "Vimeo", "key": "nope", "type": "Trailer", "official": True},
        ]}}
        self.assertIsNone(build_site.trailer_from(payload))

    def test_skips_featurettes_and_clips(self):
        payload = {"videos": {"results": [
            {"site": "YouTube", "key": "clip", "type": "Clip", "official": True},
            {"site": "YouTube", "key": "beh", "type": "Behind the Scenes"},
        ]}}
        self.assertIsNone(build_site.trailer_from(payload))

    def test_no_videos_at_all(self):
        self.assertIsNone(build_site.trailer_from({"videos": {"results": []}}))
        self.assertIsNone(build_site.trailer_from({}))


class EnrichTests(unittest.TestCase):
    SEARCH = {"results": [{"id": 103, "title": "Taxi Driver",
                           "original_title": "Taxi Driver",
                           "release_date": "1976-02-08", "popularity": 30.0}]}
    DETAILS = {"id": 103, "title": "Taxi Driver", "release_date": "1976-02-08",
               "poster_path": "/abc.jpg",
               "videos": {"results": [{"site": "YouTube", "key": "xyz",
                                       "type": "Trailer", "official": True}]}}

    def _fetch(self, url):
        return self.SEARCH if "/search/" in url else self.DETAILS

    def test_populates_the_cache(self):
        films = {"taxi-driver": film("Taxi Driver")}
        cache = {}
        with mock.patch.object(build_site, "fetch", side_effect=self._fetch):
            looked_up, missing = build_site.enrich(films, cache, "k", verbose=False)
        self.assertEqual((looked_up, missing), (1, []))
        self.assertEqual(cache["taxi-driver"]["tmdb_id"], 103)
        self.assertTrue(cache["taxi-driver"]["poster"].endswith("/abc.jpg"))
        self.assertTrue(cache["taxi-driver"]["trailer"].endswith("xyz"))

    def test_a_cache_hit_makes_no_request(self):
        films = {"taxi-driver": film("Taxi Driver")}
        cache = {"taxi-driver": {"v": build_site.TEMPLATE_VERSION, "tmdb_id": 103}}
        with mock.patch.object(build_site, "fetch",
                               side_effect=AssertionError("should not fetch")):
            looked_up, missing = build_site.enrich(films, cache, "k", verbose=False)
        self.assertEqual((looked_up, missing), (0, []))

    def test_a_cached_miss_is_not_retried(self):
        # The whole point of negative caching: Alamo's one-off events are never
        # going to be in TMDB, and re-asking daily is the entire request budget.
        films = {"catvideofest-2026": film("CatVideoFest 2026")}
        cache = {"catvideofest-2026": {"v": build_site.TEMPLATE_VERSION, "tmdb_id": None}}
        with mock.patch.object(build_site, "fetch",
                               side_effect=AssertionError("should not fetch")):
            looked_up, missing = build_site.enrich(films, cache, "k", verbose=False)
        self.assertEqual(looked_up, 0)
        self.assertEqual(missing, ["CatVideoFest 2026"])

    def test_a_miss_is_recorded_so_it_is_not_asked_again(self):
        films = {"catvideofest-2026": film("CatVideoFest 2026")}
        cache = {}
        with mock.patch.object(build_site, "fetch", return_value={"results": []}):
            build_site.enrich(films, cache, "k", verbose=False)
        self.assertIn("catvideofest-2026", cache)
        self.assertIsNone(cache["catvideofest-2026"]["tmdb_id"])

    def test_refresh_all_ignores_a_hit(self):
        films = {"taxi-driver": film("Taxi Driver")}
        cache = {"taxi-driver": {"v": build_site.TEMPLATE_VERSION, "tmdb_id": 1}}
        with mock.patch.object(build_site, "fetch", side_effect=self._fetch):
            looked_up, _ = build_site.enrich(films, cache, "k", refresh_all=True,
                                             verbose=False)
        self.assertEqual(looked_up, 1)
        self.assertEqual(cache["taxi-driver"]["tmdb_id"], 103)

    def test_a_stale_template_version_is_refetched(self):
        films = {"taxi-driver": film("Taxi Driver")}
        cache = {"taxi-driver": {"v": build_site.TEMPLATE_VERSION - 1, "tmdb_id": 1}}
        with mock.patch.object(build_site, "fetch", side_effect=self._fetch):
            looked_up, _ = build_site.enrich(films, cache, "k", verbose=False)
        self.assertEqual(looked_up, 1)

    def test_one_bad_title_does_not_stop_the_rest(self):
        # Both are the title the canned search actually answers for; this is
        # testing isolation of a failure, not the matcher.
        films = {"a": film("Taxi Driver"), "b": film("Taxi Driver")}
        calls = {"n": 0}

        def flaky(url):
            calls["n"] += 1
            if calls["n"] == 1:
                raise build_site.FetchError("boom")
            return self._fetch(url)

        cache = {}
        with mock.patch.object(build_site, "fetch", side_effect=flaky):
            with contextlib.redirect_stderr(io.StringIO()):
                _, missing = build_site.enrich(films, cache, "k", verbose=False)
        self.assertEqual(len(missing), 1)
        self.assertEqual(len(cache), 1)

    def test_a_transient_failure_is_not_cached(self):
        # A network blip must leave no entry, so the next run tries again. A
        # cached miss here would blank the poster until someone noticed.
        films = {"a": film("Taxi Driver")}
        cache = {}
        with mock.patch.object(build_site, "fetch",
                               side_effect=build_site.FetchError("timeout")):
            with contextlib.redirect_stderr(io.StringIO()):
                build_site.enrich(films, cache, "k", verbose=False)
        self.assertEqual(cache, {})

    def test_without_a_key_nothing_is_looked_up(self):
        films = {"a": film("Taxi Driver")}
        cache = {}
        with mock.patch.object(build_site, "fetch",
                               side_effect=AssertionError("should not fetch")):
            looked_up, missing = build_site.enrich(films, cache, "", verbose=False)
        self.assertEqual((looked_up, missing), (0, ["Taxi Driver"]))


class AssembleTests(unittest.TestCase):
    TODAY = dt.date(2026, 9, 10)

    def _cards(self, ledger):
        films = {"a": film("Taxi Driver", alamo.TIER_EVENT, "Film Club")}
        return build_site.assemble(films, ledger, {}, "dc-metro-area", today=self.TODAY)

    def test_recent_first_seen_is_badged_new(self):
        cards = self._cards({"a": {"first_seen": "2026-09-08"}})
        self.assertTrue(cards[0]["new"])

    def test_an_old_first_seen_is_not(self):
        cards = self._cards({"a": {"first_seen": "2026-08-01"}})
        self.assertFalse(cards[0]["new"])

    def test_the_boundary_is_exclusive(self):
        # Exactly NEW_DAYS old has had its week; the badge is for this week.
        edge = (self.TODAY - dt.timedelta(days=build_site.NEW_DAYS)).isoformat()
        self.assertFalse(self._cards({"a": {"first_seen": edge}})[0]["new"])
        newer = (self.TODAY - dt.timedelta(days=build_site.NEW_DAYS - 1)).isoformat()
        self.assertTrue(self._cards({"a": {"first_seen": newer}})[0]["new"])

    def test_a_film_missing_from_the_ledger_is_not_new(self):
        self.assertFalse(self._cards({})[0]["new"])

    def test_a_corrupt_date_does_not_crash_the_build(self):
        self.assertFalse(self._cards({"a": {"first_seen": "not-a-date"}})[0]["new"])

    def test_tier_and_series_survive(self):
        card = self._cards({})[0]
        self.assertEqual(card["tier"], "event")
        self.assertEqual(card["label"], "Film Club")
        self.assertTrue(card["url"].endswith("/show/a"))

    def test_a_run_gets_a_span_and_a_single_date_gets_a_time(self):
        one = build_site.assemble({"a": film("X")}, {}, {}, "m", today=self.TODAY)
        self.assertIn(":", one[0]["when"])
        many = build_site.assemble({"a": film("X", hours=[0, 24, 48])}, {}, {}, "m",
                                   today=self.TODAY)
        self.assertIn("–", many[0]["when"])
        self.assertNotIn(":", many[0]["when"])


class TimelineTests(unittest.TestCase):
    LEDGER = {
        "a": {"title": "Taxi Driver", "first_seen": "2026-09-08"},
        "b": {"title": "Amadeus", "first_seen": "2026-09-08"},
        "c": {"title": "Network", "first_seen": "2026-08-27"},
        "d": {"title": "No date"},
    }

    def test_groups_by_day_newest_first(self):
        days = build_site.timeline(self.LEDGER, "m")
        self.assertEqual([d["date"] for d in days], ["2026-09-08", "2026-08-27"])
        self.assertEqual([f["title"] for f in days[0]["films"]], ["Amadeus", "Taxi Driver"])

    def test_entries_without_a_date_are_dropped(self):
        titles = [f["title"] for d in build_site.timeline(self.LEDGER, "m") for f in d["films"]]
        self.assertNotIn("No date", titles)

    def test_limit_caps_the_days(self):
        self.assertEqual(len(build_site.timeline(self.LEDGER, "m", limit=1)), 1)

    def test_an_empty_ledger_is_empty_not_an_error(self):
        self.assertEqual(build_site.timeline({}, "m"), [])


class RenderTests(unittest.TestCase):
    def _page(self, **kw):
        films = {"a": film("Taxi Driver", alamo.TIER_EVENT, "Film Club")}
        cards = build_site.assemble(films, {}, {}, "dc-metro-area")
        days = build_site.timeline({"a": {"title": "Taxi Driver",
                                          "first_seen": "2026-09-08"}}, "dc-metro-area")
        opts = {"missing": [], "has_key": True}
        opts.update(kw)
        return build_site.render_html(cards, days, "T", "Bryant", "dc-metro-area", **opts)

    def test_no_unsubstituted_placeholders(self):
        self.assertNotRegex(self._page(), r"\$\{?[a-z_]+\}?")

    def test_the_embedded_payloads_are_valid_json(self):
        page = self._page()
        data = json.loads(re.search(r"^const DATA = (.*);$", page, re.M).group(1))
        tl = json.loads(re.search(r"^const TIMELINE = (.*);$", page, re.M).group(1))
        self.assertEqual(data[0]["title"], "Taxi Driver")
        self.assertEqual(tl[0]["date"], "2026-09-08")

    def test_a_missing_key_is_said_out_loud(self):
        # A page that quietly lost its artwork must not look like one that
        # never had any.
        self.assertIn("no posters", self._page(has_key=False))

    def test_unmatched_titles_are_explained(self):
        self.assertIn("no TMDB match", self._page(missing=["CatVideoFest 2026"]))

    def test_a_hostile_title_cannot_break_out_of_the_page(self):
        films = {"a": film('</script><img src=x onerror=alert(1)>')}
        cards = build_site.assemble(films, {}, {}, "m")
        page = build_site.render_html(cards, [], "T", "L", "m", [], has_key=True)
        self.assertNotIn("</script><img", page)

    def test_declares_utf8_and_a_viewport(self):
        page = self._page()
        self.assertIn('<meta charset="utf-8">', page)
        self.assertIn("viewport", page)


class JsonIoTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nested", "cache.json")
            build_site.save_json(path, {"a": 1})
            self.assertEqual(build_site.load_json(path, {}), {"a": 1})

    def test_a_missing_file_gives_the_default(self):
        self.assertEqual(build_site.load_json("/nope/nope.json", {"d": 1}), {"d": 1})

    def test_corrupt_json_gives_the_default_rather_than_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{not json")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(build_site.load_json(path, {}), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
