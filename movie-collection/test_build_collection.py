#!/usr/bin/env python3
"""Offline tests for build_collection.

No network. Every test either drives pure functions or runs a full build against
a cache seeded in-memory, so the shelf rules and the "only when something
changed" behaviour are provable without a TMDB key.
"""

import csv
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import build_collection as bcol  # noqa: E402


def record(key, title=None, year=2000, directors=(), section="films", **extra):
    base = {
        "key": key, "section": section, "title": title or key, "year": year,
        "directors": list(directors), "runtime": 100, "genres": ["Drama"],
        "overview": "", "poster": "/p.jpg", "tmdb_id": abs(hash(key)) % 99999,
        "imdb_id": "tt0000001", "imdb_rating": 7.0, "rt_critic": 80,
        "rt_audience": None, "letterboxd": "https://letterboxd.com/film/x/",
        "source_notes": [], "verified": "2026-01-01",
    }
    base.update(extra)
    return base


class Workspace:
    """A throwaway directory holding an inventory, a cache and an output dir."""

    def __init__(self, inventory, records, overrides=""):
        self.dir = tempfile.TemporaryDirectory()
        path = self.dir.name
        self.collection = os.path.join(path, "collection.txt")
        self.overrides = os.path.join(path, "overrides.toml")
        self.cache = os.path.join(path, "metadata.json")
        self.ledger = os.path.join(path, "build.json")
        self.out = os.path.join(path, "site")
        self.write_inventory(inventory)
        with open(self.overrides, "w", encoding="utf-8") as handle:
            handle.write(overrides)
        bcol.save_json(self.cache, {"version": 1, "records": {r["key"]: r for r in records}})

    def write_inventory(self, text):
        with open(self.collection, "w", encoding="utf-8") as handle:
            handle.write(text)

    def run(self, *extra):
        argv = [
            "--collection", self.collection, "--overrides", self.overrides,
            "--cache", self.cache, "--ledger", self.ledger, "--out-dir", self.out,
            "--offline", *extra,
        ]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = bcol.main(argv)
        return code, out.getvalue(), err.getvalue()

    def rows(self):
        with open(os.path.join(self.out, "collection.csv"), encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def page(self):
        with open(os.path.join(self.out, "index.html"), encoding="utf-8") as handle:
            return handle.read()

    def close(self):
        self.dir.cleanup()


# --- Inventory parsing -------------------------------------------------------


class InventoryTests(unittest.TestCase):
    def parse(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                         encoding="utf-8") as handle:
            handle.write(text)
            path = handle.name
        self.addCleanup(os.unlink, path)
        return bcol.parse_inventory(path)

    def test_comments_and_blanks_ignored(self):
        entries = self.parse("# a note\n\nDie Hard\n\n   \nAlien\n")
        self.assertEqual([e["key"] for e in entries], ["Die Hard", "Alien"])

    def test_year_hint_split_off_the_title(self):
        entry = self.parse("It (2017)\n")[0]
        self.assertEqual(entry["query"], "It")
        self.assertEqual(entry["year_hint"], 2017)
        self.assertEqual(entry["key"], "It (2017)", "the raw line stays the cache key")

    def test_a_year_in_the_title_is_not_a_hint(self):
        # Otherwise "Blade Runner 2049" would search for "Blade Runner".
        entry = self.parse("Blade Runner 2049\n")[0]
        self.assertEqual(entry["query"], "Blade Runner 2049")
        self.assertIsNone(entry["year_hint"])

    def test_sections(self):
        entries = self.parse("[films]\nAlien\n[collections]\nBourne Box\n")
        self.assertEqual([e["section"] for e in entries], ["films", "collections"])

    def test_entries_before_any_header_are_films(self):
        self.assertEqual(self.parse("Alien\n")[0]["section"], "films")

    def test_unknown_section_is_an_error(self):
        with self.assertRaises(bcol.InventoryError):
            self.parse("[soundtracks]\nBlade Runner\n")

    def test_duplicate_entry_is_an_error(self):
        # Silently de-duplicating would hide a double purchase.
        with self.assertRaises(bcol.InventoryError):
            self.parse("Alien\nAlien\n")

    def test_same_title_different_years_is_allowed(self):
        entries = self.parse("It (2017)\nIt (1990)\n")
        self.assertEqual(len(entries), 2)

    def test_empty_inventory_is_an_error(self):
        with self.assertRaises(bcol.InventoryError):
            self.parse("# nothing but comments\n")


# --- Alphabetisation ---------------------------------------------------------


class SortTitleTests(unittest.TestCase):
    def test_leading_articles_ignored(self):
        self.assertEqual(bcol.sort_title("The Green Knight"), "green knight")
        self.assertEqual(bcol.sort_title("A Clockwork Orange"), "clockwork orange")
        self.assertEqual(bcol.sort_title("An Education"), "education")

    def test_article_only_dropped_when_leading(self):
        self.assertEqual(bcol.sort_title("Talk to Me"), "talk to me")

    def test_accents_folded(self):
        self.assertEqual(bcol.sort_title("Amélie"), "amelie")

    def test_punctuation_does_not_reorder(self):
        self.assertEqual(bcol.sort_title("Pan's Labyrinth"), "pans labyrinth")
        self.assertEqual(bcol.sort_title("E.T. the Extra-Terrestrial"),
                         "et the extraterrestrial")

    def test_the_documented_ordering(self):
        titles = ["The Green Knight", "Ghost in the Shell", "Gremlins",
                  "The Great Wall", "Glass", "Gone Girl"]
        self.assertEqual(
            sorted(titles, key=bcol.sort_title),
            ["Ghost in the Shell", "Glass", "Gone Girl", "The Great Wall",
             "The Green Knight", "Gremlins"],
        )

    def test_waterworld_before_the_witch(self):
        self.assertEqual(sorted(["The Witch", "Waterworld"], key=bcol.sort_title),
                         ["Waterworld", "The Witch"])


# --- Director blocks ---------------------------------------------------------


class BlockTests(unittest.TestCase):
    def test_three_films_makes_a_block(self):
        records = [record(f"F{i}", directors=["Jordan Peele"]) for i in range(3)]
        self.assertIn("Jordan Peele", bcol.director_blocks(records))

    def test_two_films_does_not(self):
        records = [record(f"F{i}", directors=["Stanley Kubrick"]) for i in range(2)]
        self.assertEqual(bcol.director_blocks(records), {})

    def test_blocks_are_discovered_not_hardcoded(self):
        # Nobody told the script about this director.
        records = [record(f"F{i}", directors=["Lynne Ramsay"]) for i in range(3)]
        self.assertIn("Lynne Ramsay", bcol.director_blocks(records))

    def test_box_sets_never_count_toward_a_block(self):
        records = [record(f"F{i}", directors=["Ridley Scott"], section="collections")
                   for i in range(4)]
        self.assertEqual(bcol.director_blocks(records), {})

    def test_co_directed_films_count_for_each_director(self):
        records = [record(f"F{i}", directors=["Joel Coen", "Ethan Coen"])
                   for i in range(3)]
        blocks = bcol.director_blocks(records)
        self.assertEqual(set(blocks), {"Joel Coen", "Ethan Coen"})

    def test_a_film_is_shelved_in_only_one_block(self):
        # Three Coens plus four solo Joel films: the film cannot appear twice.
        records = [record(f"C{i}", directors=["Joel Coen", "Ethan Coen"]) for i in range(3)]
        records += [record(f"J{i}", directors=["Joel Coen"]) for i in range(4)]
        shelf, _ = bcol.shelve(records)
        self.assertEqual(len(shelf), len(records))
        self.assertEqual(len({r["key"] for r in shelf}), len(records))

    def test_block_films_are_in_release_order(self):
        records = [
            record("Late", year=2023, directors=["Christopher Nolan"]),
            record("Early", year=2008, directors=["Christopher Nolan"]),
            record("Middle", year=2014, directors=["Christopher Nolan"]),
        ]
        shelf, _ = bcol.shelve(records)
        self.assertEqual([r["key"] for r in shelf], ["Early", "Middle", "Late"])


# --- Shelving ----------------------------------------------------------------


class ShelveTests(unittest.TestCase):
    def setUp(self):
        self.records = [
            record("The Witch", year=2015),
            record("Waterworld", year=1995),
            record("Nope", year=2022, directors=["Jordan Peele"]),
            record("Us", year=2019, directors=["Jordan Peele"]),
            record("Get Out", year=2017, directors=["Jordan Peele"]),
            record("Bourne Box", section="collections"),
            record("Seven Worlds", section="documentaries"),
        ]
        self.shelf, self.blocks = bcol.shelve(self.records)

    def test_director_blocks_come_first(self):
        self.assertEqual([r["key"] for r in self.shelf[:3]], ["Get Out", "Us", "Nope"])

    def test_alphabetical_section_follows(self):
        alpha = [r["key"] for r in self.shelf if r["shelf"] == "Alphabetical"]
        self.assertEqual(alpha, ["Waterworld", "The Witch"])

    def test_box_sets_are_last_and_not_scattered(self):
        self.assertEqual(self.shelf[-2]["key"], "Bourne Box")
        self.assertEqual(self.shelf[-1]["key"], "Seven Worlds")

    def test_shelf_order_is_dense_and_unique(self):
        self.assertEqual([r["order"] for r in self.shelf], list(range(len(self.shelf))))

    def test_every_record_is_shelved_exactly_once(self):
        self.assertEqual(len(self.shelf), len(self.records))


# --- Box sets ----------------------------------------------------------------


BOXED = """[films]
Waterworld

[collections]
Bourne Box
  The Bourne Identity
  The Bourne Supremacy
  The Bourne Ultimatum
"""

BOXED_RECORDS = [
    record("Waterworld", year=1995, directors=["Kevin Reynolds"]),
    record("Bourne Box", section="collections", directors=[], tmdb_id=None,
           runtime=None, rt_critic=None, imdb_rating=None, poster=None,
           role="item", parent=None),
    record("The Bourne Identity", year=2002, directors=["Doug Liman"],
           runtime=119, rt_critic=84, imdb_rating=7.9,
           section="collections", role="member", parent="Bourne Box"),
    record("The Bourne Supremacy", year=2004, directors=["Paul Greengrass"],
           runtime=108, rt_critic=82, imdb_rating=7.7,
           section="collections", role="member", parent="Bourne Box"),
    record("The Bourne Ultimatum", year=2007, directors=["Paul Greengrass"],
           runtime=115, rt_critic=None, imdb_rating=8.0,
           section="collections", role="member", parent="Bourne Box"),
]


class NestingTests(unittest.TestCase):
    def parse(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                         encoding="utf-8") as handle:
            handle.write(text)
            path = handle.name
        self.addCleanup(os.unlink, path)
        return bcol.parse_inventory(path)

    def test_indented_line_becomes_a_member_of_the_set_above(self):
        entries = self.parse("[collections]\nBourne Box\n  The Bourne Identity\n")
        self.assertEqual(entries[1]["role"], "member")
        self.assertEqual(entries[1]["parent"], "Bourne Box")

    def test_unindented_line_starts_a_new_set(self):
        entries = self.parse(
            "[collections]\nBox A\n  Film One\nBox B\n  Film Two\n")
        self.assertEqual(entries[3]["parent"], "Box B")

    def test_member_keeps_its_year_hint(self):
        entries = self.parse("[collections]\nBox\n  Psycho (1960)\n")
        self.assertEqual(entries[1]["query"], "Psycho")
        self.assertEqual(entries[1]["year_hint"], 1960)

    def test_indent_with_no_set_above_is_an_error(self):
        with self.assertRaises(bcol.InventoryError):
            self.parse("[collections]\n  Orphan Disc\n")

    def test_indent_inside_films_is_an_error(self):
        # Nesting means nothing there, and silently accepting it would hide a
        # stray space in front of a title.
        with self.assertRaises(bcol.InventoryError):
            self.parse("[films]\nAlien\n  Aliens\n")

    def test_a_section_header_ends_the_previous_set(self):
        with self.assertRaises(bcol.InventoryError):
            self.parse("[collections]\nBox\n[documentaries]\n  Orphan\n")


class AggregationTests(unittest.TestCase):
    def setUp(self):
        self.records = [dict(r) for r in BOXED_RECORDS]
        self.shelf, self.blocks = bcol.shelve(self.records)
        self.box = next(r for r in self.shelf if r["key"] == "Bourne Box")

    def test_film_count(self):
        self.assertEqual(self.box["film_count"], 3)

    def test_runtime_is_the_total(self):
        self.assertEqual(self.box["agg_runtime"], 119 + 108 + 115)

    def test_year_span(self):
        self.assertEqual(self.box["agg_years"], (2002, 2007))

    def test_score_averaged_only_over_discs_that_have_one(self):
        # 84 and 82 average to 83. The unscored third disc must not be counted
        # as a zero, which would drag the set down to 55.
        self.assertEqual(self.box["agg_rt"], 83)

    def test_a_partial_average_is_recorded_in_source_notes(self):
        notes = " ".join(self.box["source_notes"])
        self.assertIn("aggregate of 3 disc(s)", notes)
        self.assertIn("averaged over 2", notes)

    def test_discs_do_not_create_a_director_block(self):
        # Two Greengrass films here; a third would still not make a block,
        # because splitting a box across the shelf defeats owning the box.
        records = [dict(r) for r in BOXED_RECORDS]
        records.append(record("Bourne Legacy", year=2012, directors=["Paul Greengrass"],
                              section="collections", role="member", parent="Bourne Box"))
        _, blocks = bcol.shelve(records)
        self.assertNotIn("Paul Greengrass", blocks)

    def test_discs_are_shelved_immediately_after_their_box(self):
        keys = [r["key"] for r in self.shelf]
        box_at = keys.index("Bourne Box")
        self.assertEqual(keys[box_at + 1:box_at + 4],
                         ["The Bourne Identity", "The Bourne Supremacy",
                          "The Bourne Ultimatum"])

    def test_discs_are_in_release_order(self):
        years = [r["year"] for r in self.shelf if r.get("role") == "member"]
        self.assertEqual(years, sorted(years))

    def test_every_record_still_shelved_exactly_once(self):
        self.assertEqual(len(self.shelf), len(BOXED_RECORDS))

    def test_a_set_with_no_discs_aggregates_nothing(self):
        records = [record("Empty Box", section="collections", role="item")]
        shelf, _ = bcol.shelve(records)
        self.assertIsNone(shelf[0].get("film_count"))


class BoxSetOutputTests(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace(BOXED, BOXED_RECORDS)
        self.addCleanup(self.ws.close)
        self.ws.run()
        self.rows = {r["Title"]: r for r in self.ws.rows()}

    def test_box_row_reports_the_totals(self):
        box = self.rows["Bourne Box"]
        self.assertEqual(box["Runtime_Minutes"], "342")
        self.assertEqual(box["RT_Critic_Percent"], "83")
        self.assertEqual(box["Category"], "Collection")

    def test_disc_is_a_film_that_names_its_box(self):
        disc = self.rows["The Bourne Identity"]
        self.assertEqual(disc["Category"], "Film")
        self.assertEqual(disc["Collection"], "Bourne Box")
        self.assertEqual(disc["Director"], "Doug Liman")

    def test_discs_carry_no_director_block(self):
        self.assertEqual(self.rows["The Bourne Supremacy"]["Director_Block"], "")

    def test_summary_counts_boxes_not_the_discs_inside_them(self):
        page = self.ws.page()
        self.assertIn("1 box sets (3 discs)", page)
        self.assertNotIn("4 box sets", page)

    def test_searching_a_disc_title_finds_the_box(self):
        page = self.ws.page()
        raw = page.split("const DATA = ", 1)[1].split(";\nconst shelf", 1)[0]
        films = json.loads(raw.replace("<\\/", "</"))
        box = next(f for f in films if f["title"] == "Bourne Box")
        self.assertIn("ultimatum", box["haystack"])
        self.assertTrue(box["box"])
        self.assertEqual(box["films"], 3)


# --- Builds ------------------------------------------------------------------


INVENTORY = """[films]
Get Out
Us
Nope
Waterworld

[collections]
Bourne Box
"""

RECORDS = [
    record("Get Out", year=2017, directors=["Jordan Peele"], rt_critic=98),
    record("Us", year=2019, directors=["Jordan Peele"], rt_critic=93),
    record("Nope", year=2022, directors=["Jordan Peele"], rt_critic=83),
    record("Waterworld", year=1995, rt_critic=46, directors=["Kevin Reynolds"]),
    record("Bourne Box", section="collections", directors=[], tmdb_id=None,
           runtime=None, rt_critic=None, imdb_rating=None, poster=None),
]


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace(INVENTORY, RECORDS)
        self.addCleanup(self.ws.close)

    def test_first_build_writes_both_outputs(self):
        code, out, _ = self.ws.run()
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(os.path.join(self.ws.out, "index.html")))
        self.assertTrue(os.path.exists(os.path.join(self.ws.out, "collection.csv")))
        self.assertIn("Get Out", out)

    def test_second_build_is_silent_and_rewrites_nothing(self):
        self.ws.run()
        page = os.path.join(self.ws.out, "index.html")
        stamp = os.stat(page).st_mtime_ns
        code, out, _ = self.ws.run()
        self.assertEqual(code, 0)
        self.assertEqual(out, "", "an unchanged inventory must print nothing")
        self.assertEqual(os.stat(page).st_mtime_ns, stamp, "the page was rewritten")

    def test_force_rebuilds_an_unchanged_collection(self):
        self.ws.run()
        code, _, _ = self.ws.run("--force")
        self.assertEqual(code, 0)

    def test_adding_a_title_reports_only_that_title(self):
        self.ws.run()
        bcol.save_json(self.ws.cache, {"version": 1, "records": {
            **{r["key"]: r for r in RECORDS},
            "Hereditary": record("Hereditary", year=2018, directors=["Ari Aster"]),
        }})
        self.ws.write_inventory(INVENTORY.replace("Waterworld", "Waterworld\nHereditary"))
        code, out, _ = self.ws.run()
        self.assertEqual(code, 0)
        self.assertIn("Hereditary", out)
        self.assertNotIn("Get Out", out, "already-known titles must not re-report")
        self.assertIn("1 added", out)

    def test_removing_a_title_is_reported_and_drops_it(self):
        self.ws.run()
        self.ws.write_inventory(INVENTORY.replace("Waterworld\n", ""))
        code, out, _ = self.ws.run()
        self.assertEqual(code, 0)
        self.assertIn("removed", out)
        self.assertNotIn("Waterworld", [r["Title"] for r in self.ws.rows()])

    def test_editing_overrides_rebuilds_without_refetching(self):
        self.ws.run()
        with open(self.ws.overrides, "w", encoding="utf-8") as handle:
            handle.write('["Get Out"]\nrt_audience = 86\n')
        code, _, _ = self.ws.run()
        self.assertEqual(code, 0)
        row = next(r for r in self.ws.rows() if r["Title"] == "Get Out")
        self.assertEqual(row["RT_Audience_Percent"], "86")

    def test_offline_fails_loudly_on_an_uncached_title(self):
        # Silently shipping a page missing the film you just bought is the one
        # failure this whole script exists to avoid.
        self.ws.write_inventory(INVENTORY + "\nA Film Nobody Cached\n")
        code, _, err = self.ws.run()
        self.assertEqual(code, 1)
        self.assertIn("A Film Nobody Cached", err)

    def test_dry_run_writes_nothing(self):
        code, out, _ = self.ws.run("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("Get Out", out)
        self.assertFalse(os.path.exists(os.path.join(self.ws.out, "index.html")))

    def test_markdown_report_links_rather_than_fences(self):
        code, out, _ = self.ws.run("--format", "markdown")
        self.assertEqual(code, 0)
        self.assertIn("| Title |", out)
        self.assertIn("letterboxd.com", out)
        self.assertNotIn("```", out)


class CsvTests(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace(INVENTORY, RECORDS,
                            overrides='["Us"]\nrt_audience = 59\nhdr = "Dolby Vision"\n'
                                      'disc_notes = "steelbook"\nuhd = false\n')
        self.addCleanup(self.ws.close)
        self.ws.run()
        self.rows = {r["Title"]: r for r in self.ws.rows()}

    def test_schema_is_the_agreed_columns_in_order(self):
        with open(os.path.join(self.ws.out, "collection.csv"), encoding="utf-8") as handle:
            header = next(csv.reader(handle))
        self.assertEqual(header, bcol.CSV_COLUMNS)

    def test_rt_audience_is_blank_unless_overridden(self):
        self.assertEqual(self.rows["Get Out"]["RT_Audience_Percent"], "")
        self.assertEqual(self.rows["Us"]["RT_Audience_Percent"], "59")

    def test_theatrical_score_is_never_substituted(self):
        # It has no source. A number here would be invented, which is the one
        # thing the brief forbids.
        for row in self.rows.values():
            self.assertEqual(row["Theatrical_Score"], "")

    def test_critic_score_comes_through(self):
        self.assertEqual(self.rows["Get Out"]["RT_Critic_Percent"], "98")

    def test_dolby_vision_derived_from_hdr_format(self):
        self.assertEqual(self.rows["Us"]["Dolby_Vision"], "Yes")
        self.assertEqual(self.rows["Get Out"]["Dolby_Vision"], "")

    def test_uhd_override_changes_4k_status(self):
        self.assertEqual(self.rows["Us"]["4K_Status"], "Blu-ray")
        self.assertEqual(self.rows["Get Out"]["4K_Status"], "4K UHD")

    def test_box_set_is_categorised_as_a_collection(self):
        self.assertEqual(self.rows["Bourne Box"]["Category"], "Collection")
        self.assertEqual(self.rows["Bourne Box"]["Collection"], "Bourne Box")

    def test_director_block_column_only_on_blocked_films(self):
        self.assertEqual(self.rows["Get Out"]["Director_Block"], "Jordan Peele")
        self.assertEqual(self.rows["Waterworld"]["Director_Block"], "")

    def test_disc_notes_survive_a_rebuild(self):
        self.assertEqual(self.rows["Us"]["Disc_Notes"], "steelbook")


class HtmlTests(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace(INVENTORY, RECORDS)
        self.addCleanup(self.ws.close)
        self.ws.run()
        self.html = self.ws.page()

    def test_every_placeholder_substituted(self):
        self.assertNotIn("$data", self.html)
        self.assertNotIn("$page_title", self.html)

    def test_data_is_valid_json(self):
        raw = self.html.split("const DATA = ", 1)[1].split(";\nconst shelf", 1)[0]
        films = json.loads(raw.replace("<\\/", "</"))
        self.assertEqual(len(films), len(RECORDS))

    def test_titles_are_html_escaped(self):
        ws = Workspace("[films]\nA <script> Title\n",
                       [record("A <script> Title")])
        self.addCleanup(ws.close)
        ws.run()
        page = ws.page()
        self.assertNotIn("<script> Title", page.split("<script>")[0])

    def test_closing_script_tag_in_data_cannot_break_out(self):
        ws = Workspace("[films]\nEnd</script>Tag\n", [record("End</script>Tag")])
        self.addCleanup(ws.close)
        ws.run()
        self.assertNotIn("End</script>Tag", ws.page())

    def test_letterboxd_links_are_present(self):
        self.assertIn("letterboxd.com/film/", self.html)

    def test_shelf_names_offered_as_filters(self):
        self.assertIn("Director block: Jordan Peele", self.html)

    def test_shelf_organisation_toggle_is_present_and_off(self):
        # Off by default: the blocks are a curation layer, and the common case
        # is one A-Z run.
        self.assertIn('id="blocks"', self.html)
        self.assertNotIn('id="blocks" checked', self.html)

    def test_filter_options_are_built_by_the_script_not_baked_in(self):
        # They have to follow the toggle, so an empty select is correct here.
        self.assertIn('<select id="filter"></select>', self.html)

    def test_block_headings_carry_the_data_attribute_the_toggle_needs(self):
        self.assertIn("data-block=", self.html)
        self.assertIn("h2.click", self.html)

    def test_collapse_state_is_persisted(self):
        self.assertIn("localStorage", self.html)

    def test_shelf_label_prefix_matches_what_the_script_greps_for(self):
        # The page regroups on this exact prefix; renaming it in Python without
        # renaming BLOCK in the JS would silently disable the toggle.
        self.assertIn("const BLOCK = 'Director block: ';", self.html)
        shelves = {r["shelf"] for r in bcol.shelve(
            [record(f"F{i}", directors=["Jordan Peele"]) for i in range(3)])[0]}
        self.assertTrue(any(s.startswith("Director block: ") for s in shelves))


# --- Provider parsing (no network) ------------------------------------------


class ProviderParsingTests(unittest.TestCase):
    def test_rt_critic_pulled_from_omdb_ratings(self):
        payload = {"Ratings": [
            {"Source": "Internet Movie Database", "Value": "8.1/10"},
            {"Source": "Rotten Tomatoes", "Value": "91%"},
            {"Source": "Metacritic", "Value": "87/100"},
        ]}
        self.assertEqual(bcol.rt_critic_from(payload), 91)

    def test_metacritic_is_never_read_as_rotten_tomatoes(self):
        payload = {"Ratings": [{"Source": "Metacritic", "Value": "87/100"}]}
        self.assertIsNone(bcol.rt_critic_from(payload))

    def test_missing_rt_gives_none_not_zero(self):
        self.assertIsNone(bcol.rt_critic_from({"Ratings": []}))
        self.assertIsNone(bcol.rt_critic_from({}))

    def test_omdb_na_becomes_none(self):
        self.assertIsNone(bcol._number("N/A"))
        self.assertIsNone(bcol._number(""))
        self.assertEqual(bcol._number("7.3"), 7.3)

    def test_directors_taken_only_from_the_director_job(self):
        payload = {"credits": {"crew": [
            {"job": "Producer", "name": "Not A Director"},
            {"job": "Director", "name": "Lana Wachowski"},
            {"job": "Director", "name": "Lilly Wachowski"},
            {"job": "Director of Photography", "name": "Bill Pope"},
        ]}}
        self.assertEqual(bcol.directors_of(payload),
                         ["Lana Wachowski", "Lilly Wachowski"])

    def test_duplicate_director_credits_collapse(self):
        payload = {"credits": {"crew": [
            {"job": "Director", "name": "Ridley Scott"},
            {"job": "Director", "name": "Ridley Scott"},
        ]}}
        self.assertEqual(bcol.directors_of(payload), ["Ridley Scott"])

    def test_api_keys_are_redacted_from_errors(self):
        message = bcol._redact("https://api.themoviedb.org/3/movie/1?api_key=sekrit&x=1")
        self.assertNotIn("sekrit", message)
        self.assertIn("REDACTED", message)


class PinnedIdTests(unittest.TestCase):
    """A pinned tmdb_id must replace the search, not just repaint the title."""

    def setUp(self):
        self.searched = []
        self.fetched = []

        def fake_search(query, year, key):
            self.searched.append(query)
            return {"id": 766922, "title": "Spiral", "release_date": "2021-09-15"}

        def fake_movie(tmdb_id, key):
            self.fetched.append(tmdb_id)
            return {
                "id": tmdb_id, "title": "Spiral: From the Book of Saw",
                "release_date": "2021-05-12", "runtime": 93,
                "genres": [{"name": "Horror"}], "imdb_id": "tt10342730",
                "poster_path": "/p.jpg", "overview": "",
                "credits": {"crew": [{"job": "Director", "name": "Darren Lynn Bousman"}]},
            }

        self.addCleanup(setattr, bcol, "tmdb_search", bcol.tmdb_search)
        self.addCleanup(setattr, bcol, "tmdb_movie", bcol.tmdb_movie)
        self.addCleanup(setattr, bcol, "resolve_letterboxd", bcol.resolve_letterboxd)
        bcol.tmdb_search = fake_search
        bcol.tmdb_movie = fake_movie
        bcol.resolve_letterboxd = lambda t, i: "https://letterboxd.com/film/x/"

        self.entry = {
            "key": "Spiral (2021)", "query": "Spiral", "year_hint": 2021,
            "section": "films", "role": "item", "parent": None, "line": 1,
        }
        self.keys = {"tmdb": "k", "omdb": ""}

    def test_pin_skips_the_search_entirely(self):
        bcol.resolve(self.entry, self.keys, [],
                     {"Spiral (2021)": {"tmdb_id": 602734}})
        self.assertEqual(self.searched, [], "the search must not run at all")
        self.assertEqual(self.fetched, [602734])

    def test_pinned_record_carries_the_pinned_films_director(self):
        # The whole point: without this the director stayed wrong while the
        # title looked right.
        record = bcol.resolve(self.entry, self.keys, [],
                              {"Spiral (2021)": {"tmdb_id": 602734}})
        self.assertEqual(record["directors"], ["Darren Lynn Bousman"])
        self.assertEqual(record["tmdb_id"], 602734)

    def test_the_pin_is_recorded_in_source_notes(self):
        record = bcol.resolve(self.entry, self.keys, [],
                              {"Spiral (2021)": {"tmdb_id": 602734}})
        self.assertIn("pinned to 602734", " ".join(record["source_notes"]))

    def test_without_a_pin_the_search_still_runs(self):
        bcol.resolve(self.entry, self.keys, [], {})
        self.assertEqual(self.searched, ["Spiral"])
        self.assertEqual(self.fetched, [766922])

    def test_a_pin_the_cache_predates_is_warned_about(self):
        # Adding tmdb_id to overrides.toml does nothing until the film is
        # re-resolved. Silently ignoring it is how you end up believing a
        # mismatch is fixed when it is not.
        ws = Workspace("[films]\nSpiral (2021)\n",
                       [record("Spiral (2021)", tmdb_id=766922)],
                       overrides='["Spiral (2021)"]\ntmdb_id = 602734\n')
        self.addCleanup(ws.close)
        code, _, err = ws.run()
        self.assertEqual(code, 0)
        self.assertIn("--refresh", err)
        self.assertIn("Spiral (2021)", err)

    def test_no_warning_once_the_cache_matches_the_pin(self):
        ws = Workspace("[films]\nSpiral (2021)\n",
                       [record("Spiral (2021)", tmdb_id=602734)],
                       overrides='["Spiral (2021)"]\ntmdb_id = 602734\n')
        self.addCleanup(ws.close)
        _, _, err = ws.run()
        self.assertNotIn("--refresh", err)


class FingerprintTests(unittest.TestCase):
    def entries(self, *keys):
        return [{"key": k, "section": "films"} for k in keys]

    def test_same_inputs_same_fingerprint(self):
        a = bcol.fingerprint(self.entries("Alien"), {})
        b = bcol.fingerprint(self.entries("Alien"), {})
        self.assertEqual(a, b)

    def test_adding_a_title_changes_it(self):
        a = bcol.fingerprint(self.entries("Alien"), {})
        b = bcol.fingerprint(self.entries("Alien", "Aliens"), {})
        self.assertNotEqual(a, b)

    def test_editing_an_override_changes_it(self):
        a = bcol.fingerprint(self.entries("Alien"), {})
        b = bcol.fingerprint(self.entries("Alien"), {"Alien": {"rt_audience": 94}})
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main(verbosity=2)
