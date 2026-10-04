#!/usr/bin/env python3
"""Offline tests for book_search.

Nothing touches the network. The HTML below is trimmed from real AudioBookBay
search and detail pages, so the parsers are checked against the markup the site
actually serves.
"""

import http.client
import json
import os
import sys
import threading
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import book_search as bs  # noqa: E402

SEARCH_PAGE = """
<div class="post"><div class="postTitle"><h2><a href="/abss/getv-lost-justin-halpern/" rel="bookmark">Get Lost - Justin Halpern</a></h2></div><div class="postInfo">Category: Humor&nbsp;<br /></div><div class="postContent">
<p style='text-align:center;'>Posted: 12 Jul 2026<br />Format: <span style='color:#a00;'>M4B</span> / Bitrate: <span style='color:#a00;'>?</span><br />File Size: <span style='color:#00f;'>205.01</span> MBs</p>
</div></div><div class="post"><div class="postTitle"><h2><a href="/abss/proaject-hail-mary-andy-weir/" rel="bookmark">Project Hail Mary - Andy Weir</a></h2></div><div class="postContent">
<p style='text-align:center;'>Posted: 14 Nov 2021<br />Format: <span style='color:#a00;'>M4B</span> / Bitrate: <span style='color:#a00;'>128 Kbps</span><br />File Size: <span style='color:#00f;'>881.82</span> MBs</p>
</div></div><div class="post"><div class="postTitle"><h2><a href="/abss/audilbles-top/" rel="bookmark">Audible&#8217;s Top Sci-Fi &amp; Fantasy</a></h2></div><div class="postContent">
<p style='text-align:center;'>Posted: 1 Jan 2022<br />Format: <span style='color:#a00;'>MP3</span> / Bitrate: <span style='color:#a00;'>64 Kbps</span><br />File Size: <span style='color:#00f;'>2.1</span> GBs</p>
</div></div><div class="navigation">
"""

DETAIL_PAGE = """
<tr>
<td>Tracker:</td>
<td>http://tracker2.dler.org:80/announce</td>
</tr>
<tr>
<td>Tracker:</td>
<td>udp://tracker.opentrackr.org:1337/announce</td>
</tr>
<tr>
<td>Tracker:</td>
<td>http://tracker2.dler.org:80/announce</td>
</tr>
<tr>
<td>Info Hash:</td>
<td>AD5FAE5FFDA056F9F45131045D140326BBAFC4DC</td>
</tr>
"""


class ParseSearchPage(unittest.TestCase):
    def setUp(self):
        self.results = bs.parse_search_page(SEARCH_PAGE)

    def test_finds_every_post(self):
        self.assertEqual(len(self.results), 3)

    def test_reads_title_url_and_details(self):
        r = self.results[1]
        self.assertEqual(r.title, "Project Hail Mary - Andy Weir")
        self.assertEqual(r.url, bs.ABB_BASE + "/abss/proaject-hail-mary-andy-weir/")
        self.assertEqual((r.format, r.bitrate, r.size, r.posted), ("M4B", "128 Kbps", "881.82 MB", "14 Nov 2021"))

    def test_unknown_bitrate_is_left_blank(self):
        self.assertEqual(self.results[0].bitrate, "")

    def test_unescapes_entities(self):
        self.assertEqual(self.results[2].title, "Audible’s Top Sci-Fi & Fantasy")
        self.assertEqual(self.results[2].size, "2.1 GB")


class DetailPage(unittest.TestCase):
    def test_builds_magnet_from_hash_and_trackers(self):
        r = bs.apply_detail_page(bs.Result("Project Hail Mary - Andy Weir", "u"), DETAIL_PAGE)
        self.assertEqual(r.info_hash, "ad5fae5ffda056f9f45131045d140326bbafc4dc")
        self.assertEqual(r.trackers, ["http://tracker2.dler.org:80/announce", "udp://tracker.opentrackr.org:1337/announce"])
        parsed = urllib.parse.parse_qs(r.magnet.removeprefix("magnet:?"))
        self.assertEqual(parsed["xt"], ["urn:btih:ad5fae5ffda056f9f45131045d140326bbafc4dc"])
        self.assertEqual(parsed["dn"], ["Project Hail Mary - Andy Weir"])
        self.assertEqual(parsed["tr"], r.trackers)

    def test_falls_back_to_public_trackers(self):
        page = DETAIL_PAGE.split("<tr>\n<td>Info Hash:")[0].replace("Tracker:", "Nope:") + "<td>Info Hash:</td>\n<td>" + "a" * 40 + "</td>"
        r = bs.apply_detail_page(bs.Result("x", "u"), page)
        self.assertEqual(r.trackers, bs.FALLBACK_TRACKERS)

    def test_no_hash_means_no_magnet(self):
        r = bs.apply_detail_page(bs.Result("x", "u"), "<td>Tracker:</td><td>udp://a</td>")
        self.assertEqual(r.magnet, "")


class Narrow(unittest.TestCase):
    def setUp(self):
        self.results = bs.parse_search_page(SEARCH_PAGE)

    def test_drops_hits_that_only_match_keywords(self):
        kept = bs.narrow(self.results, "project hail mary", None)
        self.assertEqual([r.title for r in kept], ["Project Hail Mary - Andy Weir"])

    def test_author_filters_by_whole_word(self):
        self.assertEqual(len(bs.narrow(self.results, "project hail mary", "weir")), 1)
        self.assertEqual(bs.narrow(self.results, "project hail mary", "wei"), [])

    def test_keeps_everything_when_no_title_matches(self):
        self.assertEqual(len(bs.narrow(self.results, "nothing like this", None)), 3)


class Selection(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(bs.parse_selection("all", 3), [0, 1, 2])
        self.assertEqual(bs.parse_selection("1,3", 3), [0, 2])
        self.assertEqual(bs.parse_selection("2-4,2", 5), [1, 2, 3])

    def test_out_of_range_exits(self):
        with self.assertRaises(SystemExit):
            bs.parse_selection("4", 3)


class Server(unittest.TestCase):
    """The page's API, with search and TorBox stubbed out."""

    def start(self, key):
        server = ThreadingHTTPServer(("127.0.0.1", 0), bs.make_handler(key))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return server.server_port

    def request(self, port, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, resp.read()

    def test_page_reports_whether_a_key_is_set(self):
        _, body = self.request(self.start(None), "GET", "/")
        self.assertIn(b"const HAS_KEY = false;", body)
        _, body = self.request(self.start("k"), "GET", "/")
        self.assertIn(b"const HAS_KEY = true;", body)

    def test_search_returns_results_without_trackers(self):
        r = bs.Result("Project Hail Mary - Andy Weir", "u", info_hash="h", magnet="magnet:?xt=urn:btih:h", trackers=["t"])
        with mock.patch.object(bs, "search", return_value=[r]) as search:
            status, body = self.request(self.start("k"), "GET", "/api/search?title=hail+mary&author=weir")
        self.assertEqual(status, 200)
        self.assertEqual(search.call_args.args, ("hail mary", "weir"))
        self.assertEqual(search.call_args.kwargs, {"torbox_key": "k"})
        out = json.loads(body)["results"][0]
        self.assertEqual(out["magnet"], "magnet:?xt=urn:btih:h")
        self.assertNotIn("trackers", out)

    def test_add_sends_magnet_to_torbox(self):
        port = self.start("k")
        payload = json.dumps({"magnet": "magnet:?xt=urn:btih:h", "name": "Book"})
        with mock.patch.object(bs, "torbox_add", return_value={"success": True}) as add:
            status, body = self.request(port, "POST", "/api/add", payload, {"Content-Type": "application/json"})
        self.assertEqual((status, json.loads(body)), (200, {"success": True}))
        add.assert_called_once_with("k", "magnet:?xt=urn:btih:h", "Book")

    def test_add_refuses_form_posts_and_non_magnets(self):
        port = self.start("k")
        with mock.patch.object(bs, "torbox_add") as add:
            status, _ = self.request(port, "POST", "/api/add", "magnet=x", {"Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual(status, 404)
            status, _ = self.request(port, "POST", "/api/add", json.dumps({"magnet": "http://x"}), {"Content-Type": "application/json"})
            self.assertEqual(status, 400)
        add.assert_not_called()

    def test_add_without_key_is_refused(self):
        status, body = self.request(self.start(None), "POST", "/api/add", json.dumps({"magnet": "magnet:?x"}), {"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        self.assertFalse(json.loads(body)["success"])

    def test_other_host_names_are_refused(self):
        status, _ = self.request(self.start("k"), "GET", "/", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
