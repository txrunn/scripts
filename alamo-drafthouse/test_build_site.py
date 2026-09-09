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

    def test_strips_a_presentation_format(self):
        # Alamo lists these twice, dubbed and subtitled. TMDB has neither
        # spelling, so both must reduce to the film.
        self.assertEqual(
            build_site.split_year("Princess Mononoke (Dubbed)"),
            ("Princess Mononoke", None),
        )
        self.assertEqual(
            build_site.split_year("Princess Mononoke (Subtitled)"),
            ("Princess Mononoke", None),
        )

    def test_strips_a_gimmick_suffix(self):
        self.assertEqual(
            build_site.split_year("Mean Girls (Quote-Along)"), ("Mean Girls", None)
        )
        self.assertEqual(build_site.split_year("Alien (70mm)"), ("Alien", None))

    def test_strips_stacked_suffixes_and_still_finds_the_year(self):
        self.assertEqual(
            build_site.split_year("Akira (1988) (Dubbed)"), ("Akira", 1988)
        )

    def test_leaves_a_parenthetical_that_could_be_part_of_the_title(self):
        # A wrong poster is worse than none, so anything that is not plainly a
        # format stays and simply fails to match.
        for title in ("Fallen Angels by Noel Coward (Live)",
                      "Dismember the Alamo 2026 - DC Area"):
            self.assertEqual(build_site.split_year(title), (title, None))


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
        self.assertTrue(cache["taxi-driver"]["trailer"].endswith("xyz"))
        # Posters are Alamo's; TMDB is here for the trailer and the id that
        # joins two bookings of one film.
        self.assertNotIn("poster", cache["taxi-driver"])

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


class PosterTests(unittest.TestCase):
    """Artwork comes from Alamo, which has one for every booking including the
    festivals and livestreams a film database has never heard of."""

    SHOW = {"posterImages": [{"uri": "https://img/x.jpg?auto=compress&h=1620&w=1080"}],
            "portraitHeroImage": {"uri": "https://img/hero.jpg?w=900&h=1200"}}

    def test_prefers_the_poster_and_resizes_it(self):
        url = build_site.poster_for(self.SHOW)
        self.assertIn("w=%d" % build_site.POSTER_W, url)
        self.assertIn("h=%d" % build_site.POSTER_H, url)
        self.assertNotIn("w=1080", url)

    def test_falls_back_to_the_portrait_hero(self):
        url = build_site.poster_for({"portraitHeroImage": self.SHOW["portraitHeroImage"]})
        self.assertIn("hero.jpg", url)

    def test_no_art_is_none_not_a_crash(self):
        self.assertIsNone(build_site.poster_for({}))
        self.assertIsNone(build_site.poster_for({"posterImages": []}))

    def test_maps_by_slug(self):
        out = build_site.posters_by_slug([{"slug": "a", "show": self.SHOW}, {"slug": "b"}])
        self.assertEqual(list(out), ["a"])


class AssembleTests(unittest.TestCase):
    TODAY = dt.date(2026, 9, 10)
    SEED = "2026-08-01"

    def _cards(self, ledger, films=None, cache=None, posters=None):
        films = films or {"a": film("Taxi Driver", alamo.TIER_EVENT, "Film Club")}
        ledger = dict(ledger)
        ledger.setdefault("_seed", {"first_seen": self.SEED})
        return build_site.assemble(films, ledger, cache or {}, "m",
                                   posters=posters, today=self.TODAY)

    def test_a_later_arrival_is_fresh(self):
        self.assertTrue(self._cards({"a": {"first_seen": "2026-09-09"}})[0]["fresh"])

    def test_the_seed_batch_is_not_fresh(self):
        # Those films were simply playing the day tracking started.
        self.assertFalse(self._cards({"a": {"first_seen": self.SEED}})[0]["fresh"])

    def test_a_film_absent_from_the_ledger_is_not_fresh(self):
        self.assertFalse(self._cards({})[0]["fresh"])

    def test_a_single_screening_is_a_oneoff(self):
        cards = self._cards({}, films={"a": film("X", alamo.TIER_REGULAR)})
        self.assertTrue(cards[0]["oneoff"])

    def test_a_couple_of_screenings_still_counts_as_limited(self):
        # "Screens once" was too literal: Spirited Away plays twice, dubbed and
        # subtitled, and is no less easy to miss than a single showing.
        for n in (2, 3, build_site.LIMITED_SHOWS):
            cards = self._cards({}, films={"a": film("X", alamo.TIER_REGULAR,
                                                     hours=list(range(0, 24 * n, 24)),
                                                     count=n)})
            self.assertTrue(cards[0]["oneoff"], "%d screenings should be limited" % n)

    def test_one_past_the_threshold_is_a_run(self):
        n = build_site.LIMITED_SHOWS + 1
        cards = self._cards({}, films={"a": film("X", alamo.TIER_REGULAR,
                                                 hours=list(range(0, 24 * n, 24)),
                                                 count=n)})
        self.assertFalse(cards[0]["oneoff"])

    def test_a_long_run_is_not_a_oneoff(self):
        # The Spider-Man case: a wide release playing all month is not something
        # you can miss, and it is why the page is not just the schedule.
        cards = self._cards({}, films={"a": film("X", alamo.TIER_REGULAR,
                                                 hours=list(range(0, 200, 24)), count=20)})
        self.assertFalse(cards[0]["oneoff"])

    def test_a_special_event_is_a_oneoff_even_with_several_showings(self):
        cards = self._cards({}, films={"a": film("X", alamo.TIER_EVENT, hours=[0, 24],
                                                 count=2)})
        self.assertTrue(cards[0]["oneoff"])

    def test_the_showing_carries_a_date_and_a_time(self):
        sh = self._cards({})[0]["showings"][0]
        self.assertEqual((sh["date"], sh["time"]), ("Thu 10 Sep", "8:00 PM"))
        self.assertIsNone(sh["run"])

    def test_a_run_says_where_it_ends(self):
        cards = self._cards({}, films={"a": film("X", hours=[0, 24, 48])})
        self.assertEqual(cards[0]["showings"][0]["run"], "12 Sep")

    def test_midnight_and_noon_read_correctly(self):
        self.assertEqual(build_site.clock(dt.datetime(2026, 9, 10, 0, 5)), "12:05 AM")
        self.assertEqual(build_site.clock(dt.datetime(2026, 9, 10, 12, 0)), "12:00 PM")

    def test_the_poster_comes_from_the_map(self):
        cards = self._cards({}, posters={"a": "https://img/x.jpg"})
        self.assertEqual(cards[0]["poster"], "https://img/x.jpg")


class GroupingTests(unittest.TestCase):
    """Alamo books one film more than once; the page must not read as a bug."""

    TODAY = dt.date(2026, 9, 10)

    def _cards(self, films, cache=None, ledger=None):
        ledger = ledger or {s: {"first_seen": "2026-09-09"} for s in films}
        ledger.setdefault("_seed", {"first_seen": "2026-08-01"})
        return build_site.assemble(films, ledger, cache or {}, "m", today=self.TODAY)

    def test_dubbed_and_subtitled_become_one_card(self):
        films = {"pm-dub": film("Princess Mononoke (Dubbed)", alamo.TIER_EVENT,
                                "Special event"),
                 "pm-sub": film("Princess Mononoke (Subtitled)", alamo.TIER_EVENT,
                                "Special event", hours=[48])}
        cards = self._cards(films)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["title"], "Princess Mononoke")
        self.assertEqual([sh["note"] for sh in cards[0]["showings"]],
                         ["Dubbed", "Subtitled"])

    def test_tmdb_id_joins_bookings_the_title_would_not(self):
        films = {"a": film("Forgotten Island"),
                 "b": film("Forgotten Island", label="Family Parties", hours=[48])}
        cache = {"a": {"tmdb_id": 7}, "b": {"tmdb_id": 7}}
        cards = self._cards(films, cache)
        self.assertEqual(len(cards), 1)
        self.assertEqual([sh["note"] for sh in cards[0]["showings"]],
                         [None, "Family Parties"])

    def test_different_films_stay_apart(self):
        films = {"a": film("Taxi Driver"), "b": film("Amadeus")}
        self.assertEqual(len(self._cards(films)), 2)

    def test_showings_are_ordered_soonest_first(self):
        films = {"a": film("X (Late)", hours=[48]), "b": film("X (Early)", hours=[0])}
        cards = self._cards(films, {"a": {"tmdb_id": 3}, "b": {"tmdb_id": 3}})
        dates = [sh["date"] for sh in cards[0]["showings"]]
        self.assertEqual(dates, sorted(dates, key=lambda d: d.split()[1]))


class BatchTests(unittest.TestCase):
    TODAY = dt.date(2026, 9, 10)

    def _cards(self, ledger, films):
        ledger = dict(ledger)
        ledger.setdefault("_seed", {"first_seen": "2026-08-01"})
        return build_site.assemble(films, ledger, {}, "m", today=self.TODAY)

    def test_added_batches_run_newest_first(self):
        films = {"a": film("A"), "b": film("B"), "c": film("C")}
        cards = self._cards({"a": {"first_seen": "2026-09-08"},
                             "b": {"first_seen": "2026-09-10"},
                             "c": {"first_seen": "2026-09-09"}}, films)
        out = build_site.added_batches(cards, today=self.TODAY)
        self.assertEqual([b["label"] for b in out], ["Today", "Yesterday", "Tue 8 Sep"])

    def test_the_seed_batch_never_appears(self):
        films = {"a": film("A")}
        cards = self._cards({"a": {"first_seen": "2026-08-01"}}, films)
        self.assertEqual(build_site.added_batches(cards, today=self.TODAY), [])

    def test_upcoming_holds_oneoffs_soonest_first(self):
        films = {"a": film("A", hours=[24]), "b": film("B", hours=[0])}
        cards = self._cards({}, films)
        out = build_site.upcoming_batches(cards, today=self.TODAY)
        self.assertEqual([c["title"] for c in out], ["B", "A"])

    def test_upcoming_skips_anything_already_listed_as_new(self):
        # Same film in both sections would be the page saying it twice.
        films = {"a": film("A")}
        cards = self._cards({"a": {"first_seen": "2026-09-10"}}, films)
        self.assertEqual(len(build_site.added_batches(cards, today=self.TODAY)), 1)
        self.assertEqual(build_site.upcoming_batches(cards, today=self.TODAY), [])

    def test_upcoming_skips_long_runs(self):
        films = {"a": film("A", hours=list(range(0, 200, 24)), count=20)}
        cards = self._cards({}, films)
        self.assertEqual(build_site.upcoming_batches(cards, today=self.TODAY), [])

    def test_upcoming_respects_the_horizon(self):
        films = {"a": film("A", hours=[24 * 400])}
        cards = self._cards({}, films)
        self.assertEqual(build_site.upcoming_batches(cards, today=self.TODAY), [])

    def test_every_upcoming_card_carries_its_own_date(self):
        # This section is a flat run, so the date has to live on the card --
        # that is exactly why it is not grouped by day.
        films = {"a": film("A", hours=[0]), "b": film("B", hours=[24])}
        out = build_site.upcoming_batches(self._cards({}, films), today=self.TODAY)
        self.assertEqual([c["showings"][0]["date"] for c in out],
                         ["Thu 10 Sep", "Fri 11 Sep"])


class SummaryTests(unittest.TestCase):
    """The header names what turned up, which is the question you arrive with."""

    def _batches(self, *groups):
        return [{"label": lbl, "sub": sub, "films": [{"title": t} for t in titles]}
                for lbl, sub, titles in groups]

    def test_names_the_two_most_recent_batches(self):
        out = build_site.recent_summary(self._batches(
            ("Today", "", ["A"]), ("Fri 4 Sep", "4 days ago", ["B", "C"]),
            ("Mon 1 Sep", "7 days ago", ["D"])))
        self.assertEqual(out, "Last found: A, today; then B and C, 4 days ago.")

    def test_caps_a_long_batch(self):
        out = build_site.recent_summary(self._batches(
            ("Today", "", ["A", "B", "C", "D", "E"])))
        self.assertIn("A, B, C and 2 others, today", out)

    def test_nothing_added_says_nothing(self):
        self.assertEqual(build_site.recent_summary([]), "")

    def test_one_title_reads_plainly(self):
        self.assertEqual(build_site.name_list(["A"]), "A")
        self.assertEqual(build_site.name_list(["A", "B"]), "A and B")


class RenderTests(unittest.TestCase):
    def _page(self, **kw):
        ledger = {"a": {"first_seen": "2026-09-09"}, "_seed": {"first_seen": "2026-08-01"}}
        films = {"a": film("Taxi Driver", alamo.TIER_EVENT, "Film Club")}
        cards = build_site.assemble(films, ledger, {}, "m", today=dt.date(2026, 9, 10))
        added = build_site.added_batches(cards, today=dt.date(2026, 9, 10))
        soon = build_site.upcoming_batches(cards, today=dt.date(2026, 9, 10))
        opts = {"has_key": True, "since": "2026-08-01"}
        opts.update(kw)
        return build_site.render_html(added, soon, "T", "Bryant", "m", **opts)

    def test_no_unsubstituted_placeholders(self):
        self.assertNotRegex(self._page(), r"\$\{?[a-z_]+\}?")

    def test_both_payloads_are_valid_json(self):
        page = self._page()
        added = json.loads(re.search(r"^const ADDED = (.*);$", page, re.M).group(1))
        soon = json.loads(re.search(r"^const SOON = (.*);$", page, re.M).group(1))
        self.assertEqual(added[0]["films"][0]["title"], "Taxi Driver")
        self.assertEqual(soon, [])

    def test_says_where_the_full_schedule_lives(self):
        # The page is deliberately not the schedule, so it must point at it.
        self.assertIn("showCalendar=true", self._page())

    def test_a_missing_key_only_costs_trailers(self):
        page = self._page(has_key=False)
        self.assertIn("no trailer links", page)
        self.assertIn("Artwork is Alamo's own", page)

    def test_a_hostile_title_cannot_break_out_of_the_page(self):
        films = {"a": film('</script><img src=x onerror=alert(1)>')}
        ledger = {"a": {"first_seen": "2026-09-09"}, "_s": {"first_seen": "2026-08-01"}}
        cards = build_site.assemble(films, ledger, {}, "m", today=dt.date(2026, 9, 10))
        page = build_site.render_html(build_site.added_batches(cards), [], "T", "L", "m",
                                      has_key=True)
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
