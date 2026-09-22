#!/usr/bin/env python3
"""Offline tests for tarkov_patch_notes.

Neither escapefromtarkov.com nor Steam is contacted. Every fixture below is a
trimmed copy of a real payload, so the de-duplication and the three markup
conversions are provable without the network.
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tarkov_patch_notes as tpn  # noqa: E402

# Battlestate published the same patch under two different headlines on the
# same day: "Patch 1.1.5.1" on their site, "Leagues are live!" on Steam. The
# opening text is identical, which is the only thing tying them together.
LEAGUES_BODY_HTML = (
    "<h3>League System</h3>\r\n<p>Added a new competitive system &ndash; Leagues, "
    "available only for Seasonal characters.</p>\r\n"
)
LEAGUES_BODY_BB = (
    "[h3]League System[/h3][p]Added a new competitive system – Leagues, "
    "available only for Seasonal characters.[/p]"
)


def site_item(id_, title, html_, image=None):
    return {"source": "eft", "id": f"eft:{id_}", "title": title,
            "date": 1789477740, "url": "https://x/", "html": html_, "image": image}


def steam_item(gid, title, bb):
    return {"source": "steam", "id": f"steam:{gid}", "title": title,
            "date": 1789477724, "url": "https://y/", "html": tpn.bbcode_to_html(bb),
            "bbcode": bb, "image": tpn.first_image(bb)}


class TestSteamClassifier(unittest.TestCase):
    def test_accepts_patches_and_bare_technical_updates(self):
        # "Technical update" shipped real gameplay changes and carries no
        # version number, so a version-only rule would have dropped it.
        for title in ["Patch 1.1.5.0", "Technical update", "Hotfix 1.0.2.1",
                      "Patch 0.16.9.5"]:
            self.assertTrue(tpn.is_steam_patch(title), title)

    def test_rejects_marketing_and_forward_looking_posts(self):
        for title in ["New in-game survey", "Twitch Drops for the launch of season one!",
                      "Weekend XP bonus!", "TarkovTV on August 10!",
                      "Preliminary update plan", "Developer's Diary: Anticheat",
                      "The Summer Sale has started!", "Streamer Challenge has begun!",
                      "Preliminary roadmap for Escape from Tarkov for 2026."]:
            self.assertFalse(tpn.is_steam_patch(title), title)


class TestDedupe(unittest.TestCase):
    def test_same_version_in_both_sources_collapses(self):
        merged = tpn.merge_sources(
            [site_item(408, "Patch 1.1.5.0", "<p>Lighthouse rework.</p>")],
            [steam_item("1", "Patch 1.1.5.0", "[p]Something else entirely.[/p]")],
        )
        self.assertEqual([m["id"] for m in merged], ["eft:408"])

    def test_differently_titled_same_post_collapses_on_body(self):
        merged = tpn.merge_sources(
            [site_item(409, "Patch 1.1.5.1", LEAGUES_BODY_HTML)],
            [steam_item("2", "Leagues are live!", LEAGUES_BODY_BB)],
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["title"], "Patch 1.1.5.1")

    def test_steam_only_patch_survives(self):
        merged = tpn.merge_sources(
            [site_item(409, "Patch 1.1.5.1", LEAGUES_BODY_HTML)],
            [steam_item("3", "Technical update", "[p]Zubr spots adjusted.[/p]")],
        )
        self.assertEqual({m["title"] for m in merged},
                         {"Patch 1.1.5.1", "Technical update"})

    def test_version_extraction(self):
        self.assertEqual(tpn.version_of("Patch 1.1.5.1"), "1.1.5.1")
        self.assertEqual(tpn.version_of("Patch 0.16.9.5"), "0.16.9.5")
        self.assertIsNone(tpn.version_of("Leagues are live!"))
        self.assertIsNone(tpn.version_of("Patch 1.1"))  # too short to be an identity


class TestBbcodeConversion(unittest.TestCase):
    def test_nested_lists_keep_their_nesting(self):
        # A non-greedy [*]...[/*] regex closes the outer item on the inner
        # [/*], which silently flattens the list. This is that case.
        bb = ("[list][*][p]Removed armor from:[/p]"
              "[list][*][p]Glorious E mask;[/p][/*][*][p]Shattered mask;[/p][/*][/list]"
              "[/*][/list]")
        md = tpn.bbcode_to_markdown(bb)
        self.assertIn("- Removed armor from:", md)
        self.assertIn("  - Glorious E mask;", md)
        self.assertNotIn("[", md)
        html_ = tpn.bbcode_to_html(bb)
        self.assertIn("<ul><li>", html_.replace("\n", ""))
        self.assertIn("</ul></li></ul>", html_.replace("\n", ""))

    def test_media_and_links(self):
        bb = ('[p][img src="{STEAM_CLAN_IMAGE}/45781996/abc.png"][/img][/p]'
              '[p][url="https://tarkov.com"]here[/url][/p]')
        self.assertIn("[here](https://tarkov.com)", tpn.bbcode_to_markdown(bb))
        html_ = tpn.bbcode_to_html(bb)
        self.assertIn(tpn.CLAN_IMAGE_BASE + "45781996/abc.png", html_)

    def test_no_bbcode_survives(self):
        bb = "[h2]T[/h2][p]a[/p][hr][/hr][b]b[/b][previewyoutube=\"abc;full\"][/previewyoutube]"
        self.assertNotIn("[", tpn.bbcode_to_markdown(bb))


class TestHtmlConversion(unittest.TestCase):
    def test_crlf_and_nbsp_do_not_leave_blank_line_runs(self):
        # The site's HTML is CRLF and pads paragraphs with &nbsp;. Neither is
        # matched by a plain \n or [ \t] collapse, so both used to survive as
        # three-blank-line gaps in Discord.
        html_ = "<h3>A</h3>\r\n<p>&nbsp;</p>\r\n<p>One.</p>\r\n\r\n<p>Two.</p>"
        md = tpn.html_to_markdown(html_)
        self.assertNotIn("\r", md)
        self.assertNotIn("\n\n\n", md)
        self.assertIn("### A", md)

    def test_lists_and_links(self):
        html_ = ('<ul><li>First;</li><li>Second.</li></ul>'
                 '<p><a href="https://tarkov.com">site</a></p>')
        md = tpn.html_to_markdown(html_)
        self.assertIn("- First;", md)
        self.assertIn("- Second.", md)
        self.assertIn("[site](https://tarkov.com)", md)
        self.assertNotIn("<", md)

    def test_markdown_of_dispatches_on_source(self):
        self.assertIn("League System",
                      tpn.markdown_of(site_item(409, "P", LEAGUES_BODY_HTML)))
        self.assertIn("League System",
                      tpn.markdown_of(steam_item("2", "L", LEAGUES_BODY_BB)))


class TestDiscordChunking(unittest.TestCase):
    def test_chunks_stay_under_the_embed_limit(self):
        text = "\n".join(f"- Fixed issue number {i};" for i in range(600))
        chunks = tpn.chunk_markdown(text)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), tpn.DISCORD_CHUNK)

    def test_split_happens_on_line_boundaries(self):
        text = "\n".join(f"- Item {i} " + "x" * 100 for i in range(120))
        for c in tpn.chunk_markdown(text):
            self.assertTrue(c.lstrip().startswith("- "), c[:40])

    def test_single_overlong_line_is_still_split(self):
        chunks = tpn.chunk_markdown("y" * (tpn.DISCORD_CHUNK * 2 + 50))
        self.assertEqual(len(chunks), 3)


class TestPostAll(unittest.TestCase):
    def test_all_keeps_marketing_posts(self):
        merged = tpn.merge_sources(
            [], [steam_item("4", "New in-game survey", "[p]A survey.[/p]")],
            include_all=True)
        self.assertEqual([m["title"] for m in merged], ["New in-game survey"])
        self.assertEqual(tpn.merge_sources(
            [], [steam_item("4", "New in-game survey", "[p]A survey.[/p]")]), [])

    def test_all_still_collapses_a_twin(self):
        # "Leagues are live!" reads as marketing, so --all pulls it in -- but
        # it is the same post as the site's "Patch 1.1.5.1" and must not be
        # posted twice under its other headline.
        merged = tpn.merge_sources(
            [site_item(409, "Patch 1.1.5.1", LEAGUES_BODY_HTML)],
            [steam_item("2", "Leagues are live!", LEAGUES_BODY_BB)],
            include_all=True)
        self.assertEqual([m["title"] for m in merged], ["Patch 1.1.5.1"])


class TestEmbedImage(unittest.TestCase):
    def test_steam_image_is_resolved_to_an_absolute_url(self):
        bb = '[p][img src="{STEAM_CLAN_IMAGE}/45781996/abc.png"][/img][/p]'
        self.assertEqual(tpn.first_image(bb),
                         tpn.CLAN_IMAGE_BASE + "45781996/abc.png")

    def test_no_image_is_none_not_a_broken_url(self):
        self.assertIsNone(tpn.first_image("[p]No pictures here.[/p]"))

    def test_image_survives_the_archive_roundtrip(self):
        item = site_item(409, "Patch 1.1.5.1", "<p>x</p>", image="https://e/t.jpg")
        stored = {k: item.get(k) for k in tpn.ARCHIVE_FIELDS}
        self.assertEqual(stored["image"], "https://e/t.jpg")


class TestPage(unittest.TestCase):
    def test_sanitiser_strips_active_content(self):
        # Bodies are Battlestate's HTML, inlined into a page we serve. They are
        # filtered rather than trusted, even though the source is reputable.
        dirty = ('<p>ok</p><script>alert(1)</script>'
                 '<img src=x onerror="steal()">'
                 '<a href="javascript:bad()">x</a><iframe src="e"></iframe>')
        clean = tpn.sanitize_html(dirty)
        for probe in ("<script", "onerror", "javascript:", "<iframe"):
            self.assertNotIn(probe, clean, probe)
        self.assertIn("<p>ok</p>", clean)

    def test_page_renders_every_patch_with_the_newest_open(self):
        items = [site_item(409, "Patch 1.1.5.1", "<p>New.</p>"),
                 site_item(408, "Patch 1.1.5.0", "<p>Older.</p>")]
        items[1]["date"] = items[0]["date"] - 86400
        page = tpn.build_page(items, all_count=90)
        self.assertEqual(page.count('<details class="patch"'), 2)
        self.assertEqual(page.count('<details class="patch" open'), 1)
        self.assertIn("Patch 1.1.5.1", page)
        self.assertIn("All 90 announcements RSS", page)

    def test_page_labels_the_source_of_each_patch(self):
        page = tpn.build_page(
            [site_item(409, "Patch 1.1.5.1", "<p>a</p>"),
             steam_item("9", "Technical update", "[p]b[/p]")], all_count=1)
        self.assertIn('<span class="src">site</span>', page)
        self.assertIn('<span class="src">steam</span>', page)

    def test_page_is_stable_for_unchanged_input(self):
        items = [site_item(409, "Patch 1.1.5.1", "<p>x</p>")]
        self.assertEqual(tpn.build_page(items, 5), tpn.build_page(items, 5))

    def test_filter_data_attribute_is_lowercased(self):
        page = tpn.build_page([site_item(409, "Patch 1.1.5.1", "<p>x</p>")], 1)
        self.assertIn('data-name="patch 1.1.5.1"', page)


class TestIdempotence(unittest.TestCase):
    def test_crlf_payload_does_not_look_changed_on_rewrite(self):
        # read_text() translates CRLF to LF, so a naive text comparison never
        # matches what write_text() wrote. Left alone, the poll would commit
        # on every run, forever.
        import tempfile

        text = "<p>one</p>\r\n<p>two</p>\r\n"
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "feed.xml"
            self.assertTrue(tpn.write_if_changed(path, text))
            self.assertFalse(tpn.write_if_changed(path, text))
            self.assertTrue(tpn.write_if_changed(path, text + "<p>three</p>"))

    def test_site_html_is_stored_without_carriage_returns(self):
        self.assertNotIn("\r", tpn.html_to_markdown("<p>a</p>\r\n<p>b</p>"))


class TestFeed(unittest.TestCase):
    def test_rss_is_wellformed_and_stable(self):
        import xml.etree.ElementTree as ET

        items = [site_item(409, "Patch 1.1.5.1", "<p>Body & more</p>")]
        xml = tpn.build_rss(items, "T", "D", "https://example/patches.xml")
        root = ET.fromstring(xml)
        entries = root.find("channel").findall("item")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].findtext("title"), "Patch 1.1.5.1")
        # Built twice with no input change must be byte-identical, or the
        # workflow would commit on every poll.
        self.assertEqual(xml, tpn.build_rss(items, "T", "D", "https://example/patches.xml"))

    def test_cdata_cannot_be_closed_early(self):
        xml = tpn.build_rss(
            [site_item(1, "T", "<p>a]]>b</p>")], "T", "D", "https://e/f.xml")
        import xml.etree.ElementTree as ET

        ET.fromstring(xml)  # raises if the CDATA section broke out


if __name__ == "__main__":
    unittest.main()
