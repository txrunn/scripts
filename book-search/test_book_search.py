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


LIBGEN_PAGE = """
<table class="table  table-striped" id="tablelibgen">
<thead><tr><th>ID</th></tr></thead><tbody><tr>

<td><a data-toggle="tooltip" title="Add/Edit : 2021-06-18/2021-10-17; ID: 6663311<br>Andy Weir - Project Hail Mary" href="edition.php?id=6414692">Project Hail Mary <i></i></a><br><a href="edition.php?id=6414692"><i><font color="green"> 9780593135211</font></a></i>
<nobr><span class="badge badge-primary"><a title="Book">b</a></span></nobr>
</td>
<td>Andy Weir,  </td>
<td>Penguin Random House LLC</td>
<td><nobr>2021</nobr></td>
<td>English</td>
<td>0</td>
<td><nobr><a href="/file.php?id=6663311">9 MB</a></nobr></td>
<td>epub</td>
<td><a title="libgen" href="/ads.php?md5=F21EF754F3C4B986F3896807D80A6CE1"><span class="badge badge-primary">1</span></a></td>
</tr>
<tr><td>a row with no download link</td></tr>
</tbody></table>
"""

ADS_PAGE = """<td><a href="get.php?md5=f21ef754f3c4b986f3896807d80a6ce1&amp;key=4HWURK1DBHBGLJ7M"><h2>GET</h2></a></td>"""


class LibGen(unittest.TestCase):
    def test_parses_rows_with_a_download_link(self):
        results = bs.parse_libgen_page(LIBGEN_PAGE, "https://libgen.li")
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertEqual((r.title, r.author, r.year, r.language, r.size, r.format),
                         ("Project Hail Mary", "Andy Weir", "2021", "English", "9 MB", "epub"))
        self.assertEqual(r.md5, "f21ef754f3c4b986f3896807d80a6ce1")
        self.assertEqual(r.url, "https://libgen.li/edition.php?id=6414692")
        self.assertEqual(r.download_page, "https://libgen.li/ads.php?md5=f21ef754f3c4b986f3896807d80a6ce1")

    def test_no_table_means_no_results(self):
        self.assertEqual(bs.parse_libgen_page("<html>nothing found</html>", "https://libgen.li"), [])

    def test_direct_link_comes_from_the_download_page(self):
        with mock.patch.object(bs, "libgen_fetch", return_value=("https://libgen.bz", ADS_PAGE)):
            link = bs.libgen_direct_link("f21ef754f3c4b986f3896807d80a6ce1")
        self.assertEqual(link, "https://libgen.bz/get.php?md5=f21ef754f3c4b986f3896807d80a6ce1&key=4HWURK1DBHBGLJ7M")

    def test_direct_link_rejects_anything_but_an_md5(self):
        with self.assertRaises(ValueError):
            bs.libgen_direct_link("../etc/passwd")

    def test_tries_the_next_mirror(self):
        calls = []

        def fetch(url, **_):
            calls.append(url)
            if "libgen.li" in url:
                raise OSError("down")
            return "ok"

        with mock.patch.object(bs, "fetch", side_effect=fetch), mock.patch.object(bs, "LIBGEN_MIRRORS", ["libgen.li", "libgen.bz"]):
            self.assertEqual(bs.libgen_fetch("index.php"), ("https://libgen.bz", "ok"))
        self.assertEqual(len(calls), 2)


class DeviceToken(unittest.TestCase):
    def test_accepts_the_shapes_torbox_might_send(self):
        self.assertEqual(bs.torbox_device_token("abc"), "abc")
        self.assertEqual(bs.torbox_device_token({"access_token": "abc"}), "abc")
        self.assertEqual(bs.torbox_device_token({"token": "abc"}), "abc")
        self.assertIsNone(bs.torbox_device_token({}))
        self.assertIsNone(bs.torbox_device_token(None))


class Server(unittest.TestCase):
    """The page's API, with the sites and TorBox stubbed out."""

    def setUp(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), bs.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.port = server.server_port

    def request(self, method, path, body=None, key=None):
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if key:
            headers["X-TorBox-Key"] = key
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, (json.loads(raw) if resp.getheader("Content-Type") == "application/json" else raw), resp

    def test_page_holds_no_key(self):
        with mock.patch.dict(os.environ, {"TORBOX_API_KEY": "server-secret"}):
            _, body, _ = self.request("GET", "/")
        self.assertNotIn(b"server-secret", body)

    def test_search_passes_kind_and_drops_trackers(self):
        r = bs.Result("Project Hail Mary - Andy Weir", "u", info_hash="h", magnet="magnet:?xt=urn:btih:h", trackers=["t"])
        with mock.patch.object(bs, "search", return_value=[r]) as search:
            status, body, _ = self.request("GET", "/api/search?title=hail+mary&author=weir&kind=books")
        self.assertEqual(status, 200)
        self.assertEqual(search.call_args.args, ("hail mary", "weir", "books"))
        self.assertNotIn("trackers", body["results"][0])

    def test_search_rejects_unknown_kind(self):
        status, _, _ = self.request("GET", "/api/search?title=x&kind=films")
        self.assertEqual(status, 400)

    def test_torbox_calls_need_the_visitors_key(self):
        with mock.patch.dict(os.environ, {"TORBOX_API_KEY": "server-secret"}), mock.patch.object(bs, "torbox") as tb:
            for method, path, body in (("GET", "/api/torbox/me", None),
                                       ("POST", "/api/torbox/cached", {"hashes": []}),
                                       ("POST", "/api/torbox/add", {"magnet": "magnet:?x"})):
                status, _, _ = self.request(method, path, body)
                self.assertEqual(status, 401, path)
        tb.assert_not_called()

    def test_add_magnet_uses_the_visitors_key(self):
        with mock.patch.object(bs, "torbox_add_magnet", return_value={"success": True}) as add:
            status, body, _ = self.request("POST", "/api/torbox/add", {"magnet": "magnet:?xt=urn:btih:h", "name": "Book"}, key="visitor")
        self.assertEqual((status, body), (200, {"success": True}))
        add.assert_called_once_with("visitor", "magnet:?xt=urn:btih:h", "Book")

    def test_add_ebook_resolves_the_link_then_sends_it(self):
        with mock.patch.object(bs, "libgen_direct_link", return_value="https://libgen.li/get.php?md5=a&key=b") as link, \
             mock.patch.object(bs, "torbox_add_link", return_value={"success": True}) as add:
            status, _, _ = self.request("POST", "/api/torbox/add", {"md5": "a" * 32, "name": "Book.epub"}, key="visitor")
        self.assertEqual(status, 200)
        link.assert_called_once_with("a" * 32)
        add.assert_called_once_with("visitor", "https://libgen.li/get.php?md5=a&key=b", "Book.epub")

    def test_add_refuses_non_magnets(self):
        with mock.patch.object(bs, "torbox_add_magnet") as add:
            status, _, _ = self.request("POST", "/api/torbox/add", {"magnet": "http://x"}, key="visitor")
        self.assertEqual(status, 400)
        add.assert_not_called()

    def test_cached_filters_hashes_and_returns_a_list(self):
        good = "a" * 40
        with mock.patch.object(bs, "torbox_check_cached", return_value={good}) as check:
            status, body, _ = self.request("POST", "/api/torbox/cached", {"hashes": [good, "not-a-hash"]}, key="visitor")
        self.assertEqual((status, body), (200, {"cached": [good]}))
        check.assert_called_once_with("visitor", [good])

    def test_device_token_pending_and_done(self):
        with mock.patch.object(bs, "torbox", return_value={"success": False, "error": "DEVICE_CODE_NOT_USED"}):
            _, body, _ = self.request("POST", "/api/torbox/device/token", {"device_code": "d"})
        self.assertEqual(body, {"token": None})
        with mock.patch.object(bs, "torbox", return_value={"success": True, "data": {"access_token": "tok"}}):
            _, body, _ = self.request("POST", "/api/torbox/device/token", {"device_code": "d"})
        self.assertEqual(body, {"token": "tok"})

    def test_libgen_download_redirects_to_the_file(self):
        with mock.patch.object(bs, "libgen_direct_link", return_value="https://libgen.li/get.php?md5=a&key=b"):
            status, _, resp = self.request("GET", "/api/libgen/download?md5=" + "a" * 32)
        self.assertEqual(status, 302)
        self.assertEqual(resp.getheader("Location"), "https://libgen.li/get.php?md5=a&key=b")


if __name__ == "__main__":
    unittest.main()
