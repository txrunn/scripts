#!/usr/bin/env python3
"""Search for audiobooks, get magnet links, and send them to TorBox.

Searches AudioBookBay, opens each hit for its info hash and trackers, and builds
a magnet link from those -- no account needed. With a TorBox key it also says
which uploads TorBox already has cached (ready now, no seeders needed) and can
add any of them to the account.

    ./book_search.py "project hail mary" --author weir
    ./book_search.py "dune" -a herbert --torbox        # cache status per result
    ./book_search.py "dune" -a herbert --add 1,3       # send #1 and #3 to TorBox
    ./book_search.py "dune" --magnets-only > magnets.txt
    ./book_search.py --serve                           # search page on localhost

Environment:
    TORBOX_API_KEY   needed for --torbox, --add, and the page's send button
    ABB_DOMAIN       AudioBookBay moves domains; override it here (default audiobookbay.lu)
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --- Configuration -----------------------------------------------------------

ABB_BASE = f"https://{os.environ.get('ABB_DOMAIN', 'audiobookbay.lu')}"
TORBOX_BASE = "https://api.torbox.app/v1/api"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

# Only used when a detail page lists no trackers at all.
FALLBACK_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://open.stealth.si:80/announce",
    "udp://exodus.desync.com:6969/announce",
]

POST_RE = re.compile(r'<div class="post">(.*?)(?=<div class="post">|<div class="navigation"|$)', re.S)
TITLE_RE = re.compile(r'<div class="postTitle"><h2><a href="([^"]+)"[^>]*>(.*?)</a>', re.S)
POSTED_RE = re.compile(r"Posted:\s*([^<]+)")
FORMAT_RE = re.compile(r"Format:\s*<span[^>]*>([^<]*)</span>")
BITRATE_RE = re.compile(r"Bitrate:\s*<span[^>]*>([^<]*)</span>")
SIZE_RE = re.compile(r"File Size:\s*<span[^>]*>([^<]*)</span>\s*([KMGT]?B)s?", re.I)
HASH_RE = re.compile(r"<td>Info Hash:</td>\s*<td>\s*([0-9a-fA-F]{40})\s*</td>")
TRACKER_RE = re.compile(r"<td>Tracker:</td>\s*<td>([^<]+)</td>")


@dataclass
class Result:
    title: str
    url: str
    source: str = "audiobookbay"
    posted: str = ""
    format: str = ""
    bitrate: str = ""
    size: str = ""
    info_hash: str = ""
    magnet: str = ""
    cached: bool | None = None
    trackers: list[str] = field(default_factory=list, repr=False)

    def public(self) -> dict:
        return {k: v for k, v in asdict(self).items() if k != "trackers"}


def fetch(url: str, *, data: bytes | None = None, headers: dict | None = None, method: str | None = None) -> str:
    req = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def clean(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


# --- AudioBookBay -------------------------------------------------------------

def abb_search(query: str, pages: int = 1) -> list[Result]:
    results: list[Result] = []
    q = urllib.parse.quote_plus(query.lower())
    for page in range(1, pages + 1):
        url = f"{ABB_BASE}/?s={q}" if page == 1 else f"{ABB_BASE}/page/{page}/?s={q}"
        try:
            body = fetch(url)
        except urllib.error.HTTPError as e:
            if e.code == 404:  # past the last page
                break
            raise
        page_results = parse_search_page(body)
        if not page_results:
            break
        results.extend(page_results)
        if f"/page/{page + 1}/" not in body:
            break
    return results


def parse_search_page(body: str) -> list[Result]:
    return [r for block in POST_RE.findall(body) if (r := parse_post(block))]


def parse_post(block: str) -> Result | None:
    m = TITLE_RE.search(block)
    if not m:
        return None
    r = Result(title=clean(m.group(2)), url=urllib.parse.urljoin(ABB_BASE, m.group(1)))
    if m := POSTED_RE.search(block):
        r.posted = clean(m.group(1))
    if m := FORMAT_RE.search(block):
        r.format = clean(m.group(1))
    if (m := BITRATE_RE.search(block)) and clean(m.group(1)) != "?":
        r.bitrate = clean(m.group(1))
    if m := SIZE_RE.search(block):
        r.size = f"{clean(m.group(1))} {m.group(2).upper()}"
    return r


def apply_detail_page(r: Result, body: str) -> Result:
    """Fill in the hash, trackers and magnet from a detail page's HTML."""
    m = HASH_RE.search(body)
    if not m:
        return r
    r.info_hash = m.group(1).lower()
    r.trackers = list(dict.fromkeys(t.strip() for t in TRACKER_RE.findall(body))) or FALLBACK_TRACKERS
    params = [("dn", r.title)] + [("tr", t) for t in r.trackers]
    r.magnet = f"magnet:?xt=urn:btih:{r.info_hash}&" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    return r


def fill_magnet(r: Result) -> Result:
    try:
        body = fetch(r.url)
    except Exception as e:  # noqa: BLE001 - one bad page shouldn't sink the run
        print(f"warn: could not fetch {r.url}: {e}", file=sys.stderr)
        return r
    apply_detail_page(r, body)
    if not r.magnet:
        print(f"warn: no info hash on {r.url}", file=sys.stderr)
    return r


# Each source takes a query and returns Results with magnets still empty.
# New libraries slot in here.
SOURCES = {"audiobookbay": abb_search}


# --- Search ------------------------------------------------------------------

def words(s: str) -> list[str]:
    return re.findall(r"\w+", s.lower())


def narrow(results: list[Result], title: str, author: str | None) -> list[Result]:
    """AudioBookBay's search also matches keywords and descriptions, so
    searching "project hail mary" returns unrelated books that are merely
    tagged with it. Keep hits whose title names the book (and author)."""
    if author:
        author_words = words(author)
        results = [r for r in results if all(w in words(r.title) for w in author_words)]
    title_words = [w for w in words(title) if len(w) > 2]
    return [r for r in results if all(w in words(r.title) for w in title_words)] or results


def search(title: str, author: str | None = None, pages: int = 1, limit: int = 0,
           torbox_key: str | None = None) -> list[Result]:
    query = f"{title} {author}" if author else title
    results: list[Result] = []
    for source in SOURCES.values():
        results.extend(source(query, pages))
    results = narrow(results, title, author)
    if limit:
        results = results[:limit]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [r for r in pool.map(fill_magnet, results) if r.magnet]
    if torbox_key and results:
        try:
            cached = torbox_check_cached(torbox_key, [r.info_hash for r in results])
        except Exception as e:  # noqa: BLE001 - results are still useful without it
            print(f"warn: TorBox cache check failed: {e}", file=sys.stderr)
            return results
        for r in results:
            r.cached = r.info_hash in cached
    return results


# --- TorBox -------------------------------------------------------------------

def torbox_check_cached(key: str, hashes: list[str]) -> set[str]:
    qs = urllib.parse.urlencode([("hash", h) for h in hashes] + [("format", "object")])
    data = json.loads(fetch(f"{TORBOX_BASE}/torrents/checkcached?{qs}", headers={"Authorization": f"Bearer {key}"}))
    return {h.lower() for h in (data.get("data") or {})}


def torbox_add(key: str, magnet: str, name: str) -> dict:
    body = urllib.parse.urlencode({"magnet": magnet, "name": name}).encode()
    try:
        raw = fetch(
            f"{TORBOX_BASE}/torrents/createtorrent",
            data=body,
            method="POST",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/x-www-form-urlencoded"},
        )
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"success": False, "detail": raw[:200]}


# --- Local page ---------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="darkreader-lock">
<title>Book search</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible:wght@400;700&family=Literata:opsz,wght@7..72,500;7..72,650&display=swap" rel="stylesheet">
<style>
:root {
  --paper: #e9eef3;
  --sheet: #f7f9fb;
  --ink: #18263b;
  --muted: #5a6a80;
  --line: #c9d3de;
  --ready: #0b6e5a;
  --ready-wash: #d4ede6;
  --focus: #2f6fdb;
  --danger: #a3292b;
}
@media (prefers-color-scheme: dark) {
  :root {
    --paper: #121a26;
    --sheet: #1a2433;
    --ink: #e4eaf2;
    --muted: #93a2b8;
    --line: #2c3a4e;
    --ready: #5fd3b4;
    --ready-wash: #123a33;
    --focus: #7aa7ff;
    --danger: #ff8c86;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--paper); color: var(--ink);
  font: 16px/1.5 "Atkinson Hyperlegible", system-ui, sans-serif;
}
main { max-width: 46rem; margin: 0 auto; padding: 3rem 1rem 5rem; }
h1 {
  font: 650 2.4rem/1.1 Literata, Georgia, serif; letter-spacing: -0.01em;
  margin: 0 0 0.4rem;
}
.lede { color: var(--muted); margin: 0 0 2rem; }
form { display: grid; grid-template-columns: 2fr 1fr auto; gap: 0.6rem; margin-bottom: 0.75rem; }
label { display: grid; gap: 0.25rem; font-size: 0.85rem; color: var(--muted); }
input {
  font: inherit; font-size: 1.05rem; color: var(--ink); background: var(--sheet);
  border: 1px solid var(--line); border-radius: 6px; padding: 0.6rem 0.75rem;
}
button {
  font: inherit; font-weight: 700; cursor: pointer; border-radius: 6px;
  border: 1px solid var(--ink); background: var(--ink); color: var(--paper);
  padding: 0.6rem 1.1rem;
}
form button { align-self: end; }
button.quiet { background: transparent; color: var(--ink); border-color: var(--line); font-weight: 400; }
button:disabled { opacity: 0.45; cursor: not-allowed; }
:focus-visible { outline: 3px solid var(--focus); outline-offset: 2px; }
.status { min-height: 1.5rem; color: var(--muted); margin: 0.5rem 0 1.5rem; }
.status.error { color: var(--danger); }
ol { list-style: none; margin: 0; padding: 0; border-top: 1px solid var(--line); }
li { padding: 1.1rem 0; border-bottom: 1px solid var(--line); }
.head { display: flex; gap: 0.75rem; align-items: baseline; justify-content: space-between; }
h2 { font: 500 1.25rem/1.3 Literata, Georgia, serif; margin: 0; }
h2 a { color: inherit; text-decoration: none; }
h2 a:hover { text-decoration: underline; text-underline-offset: 3px; }
.ready {
  flex: none; font-weight: 700; font-size: 0.85rem; color: var(--ready);
  background: var(--ready-wash); border-radius: 999px; padding: 0.15rem 0.7rem;
}
.slow { flex: none; font-size: 0.85rem; color: var(--muted); }
.meta { display: flex; flex-wrap: wrap; gap: 0.25rem 1.25rem; color: var(--muted); font-size: 0.92rem; margin: 0.35rem 0 0.8rem; }
.actions { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; }
.note { font-size: 0.88rem; color: var(--muted); }
.note.ok { color: var(--ready); font-weight: 700; }
.note.error { color: var(--danger); }
@media (max-width: 560px) {
  h1 { font-size: 1.9rem; }
  form { grid-template-columns: 1fr; }
  .head { flex-direction: column; gap: 0.3rem; }
}
</style>
</head>
<body>
<main>
  <h1>Find an audiobook</h1>
  <p class="lede">Searches AudioBookBay. Uploads marked <strong>Ready now</strong> are already on TorBox and download straight away.</p>
  <form id="search">
    <label>Title <input name="title" required autofocus autocomplete="off"></label>
    <label>Author (optional) <input name="author" autocomplete="off"></label>
    <button>Search</button>
  </form>
  <p class="status" id="status" role="status"></p>
  <ol id="results"></ol>
</main>
<script>
const HAS_KEY = __HAS_KEY__;
const form = document.getElementById("search");
const statusEl = document.getElementById("status");
const list = document.getElementById("results");

function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v; else if (k.startsWith("on")) n.addEventListener(k.slice(2), v); else n.setAttribute(k, v);
  }
  n.append(...kids.filter(k => k !== null && k !== ""));
  return n;
}

function setStatus(text, error = false) {
  statusEl.textContent = text;
  statusEl.classList.toggle("error", error);
}

function row(r) {
  const note = el("span", { class: "note", "aria-live": "polite" });
  const send = HAS_KEY ? el("button", { type: "button" }, "Send to TorBox") : null;
  send?.addEventListener("click", async () => {
    send.disabled = true;
    note.className = "note"; note.textContent = "Sending…";
    try {
      const res = await fetch("/api/add", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ magnet: r.magnet, name: r.title }),
      });
      const data = await res.json();
      if (!data.success) throw new Error(data.detail || "TorBox refused the torrent.");
      note.className = "note ok"; note.textContent = "Sent to TorBox";
    } catch (e) {
      send.disabled = false;
      note.className = "note error"; note.textContent = e.message;
    }
  });
  const copy = el("button", { type: "button", class: "quiet" }, "Copy magnet link");
  copy.addEventListener("click", async () => {
    await navigator.clipboard.writeText(r.magnet);
    copy.textContent = "Copied";
    setTimeout(() => (copy.textContent = "Copy magnet link"), 1500);
  });
  const badge = r.cached === true ? el("span", { class: "ready" }, "Ready now")
    : r.cached === false ? el("span", { class: "slow" }, "Not cached, needs seeders") : null;
  return el("li", {},
    el("div", { class: "head" }, el("h2", {}, el("a", { href: r.url, target: "_blank", rel: "noreferrer" }, r.title)), badge),
    el("div", { class: "meta" },
      r.format && el("span", {}, r.format),
      r.bitrate && el("span", {}, r.bitrate),
      r.size && el("span", {}, r.size),
      r.posted && el("span", {}, "Posted " + r.posted)),
    el("div", { class: "actions" }, send, copy, note));
}

form.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const fd = new FormData(form);
  const params = new URLSearchParams({ title: fd.get("title").trim(), author: fd.get("author").trim() });
  history.replaceState(null, "", "?" + params);
  list.replaceChildren();
  setStatus("Searching… each result's page is opened for its magnet link, so this takes a few seconds.");
  try {
    const res = await fetch("/api/search?" + params);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Search failed.");
    if (!data.results.length) {
      setStatus("Nothing matched. Try fewer words, or drop the author.");
      return;
    }
    const ready = data.results.filter(r => r.cached).length;
    setStatus(`${data.results.length} upload${data.results.length === 1 ? "" : "s"}` + (HAS_KEY ? `, ${ready} ready now on TorBox.` : "."));
    list.replaceChildren(...data.results.map(row));
  } catch (e) {
    setStatus(e.message + " AudioBookBay may be down or on a new domain; set ABB_DOMAIN to change it.", true);
  }
});

const initial = new URLSearchParams(location.search);
if (!HAS_KEY) {
  document.querySelector(".lede").textContent =
    "Searches AudioBookBay. To see which uploads TorBox has ready and send them there, set TORBOX_API_KEY and restart this page.";
}
if (initial.get("title")) {
  form.title.value = initial.get("title");
  form.author.value = initial.get("author") || "";
  form.requestSubmit();
}
</script>
</body>
</html>
"""


def make_handler(key: str | None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):  # keep the terminal quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _local(self) -> bool:
            # Refuse requests addressed to any other host name, so a web page
            # can't reach this server through DNS rebinding.
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            if host in ("127.0.0.1", "localhost"):
                return True
            self._json(403, {"error": "forbidden"})
            return False

        def do_GET(self):
            if not self._local():
                return
            url = urllib.parse.urlparse(self.path)
            if url.path == "/":
                page = PAGE.replace("__HAS_KEY__", "true" if key else "false")
                self._send(200, page.encode(), "text/html; charset=utf-8")
            elif url.path == "/api/search":
                qs = urllib.parse.parse_qs(url.query)
                title = (qs.get("title") or [""])[0].strip()
                author = (qs.get("author") or [""])[0].strip() or None
                if not title:
                    return self._json(400, {"error": "Enter a title."})
                try:
                    results = search(title, author, torbox_key=key)
                except Exception as e:  # noqa: BLE001 - report it on the page
                    return self._json(502, {"error": f"Search failed: {e}."})
                self._json(200, {"results": [r.public() for r in results]})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._local():
                return
            # Requiring JSON means a cross-site form can't post here: browsers
            # preflight JSON requests, and this server never answers preflights.
            if self.path != "/api/add" or self.headers.get("Content-Type") != "application/json":
                return self._json(404, {"error": "not found"})
            if not key:
                return self._json(400, {"success": False, "detail": "TORBOX_API_KEY is not set."})
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            magnet, name = body.get("magnet", ""), body.get("name", "")
            if not magnet.startswith("magnet:?"):
                return self._json(400, {"success": False, "detail": "That isn't a magnet link."})
            self._json(200, torbox_add(key, magnet, name))

    return Handler


def serve(port: int, key: str | None, open_browser: bool) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(key))
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"Book search on {url}  (ctrl-c to stop)")
    if not key:
        print("TORBOX_API_KEY is not set: searching works, sending to TorBox is off.")
    if open_browser:
        threading.Timer(0.3, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


# --- CLI ----------------------------------------------------------------------

def parse_selection(sel: str, n: int) -> list[int]:
    if sel.lower() == "all":
        return list(range(n))
    out: list[int] = []
    for part in sel.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a) - 1, int(b)))
        elif part:
            out.append(int(part) - 1)
    bad = [i + 1 for i in out if not 0 <= i < n]
    if bad:
        sys.exit(f"error: selection out of range (1-{n}): {bad}")
    return list(dict.fromkeys(out))


def require_key() -> str:
    key = os.environ.get("TORBOX_API_KEY")
    if not key:
        sys.exit("error: set TORBOX_API_KEY (torbox.app -> Settings)")
    return key


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("title", nargs="?", help="book title to search for")
    ap.add_argument("-a", "--author", help="author; added to the query and used to filter results")
    ap.add_argument("-p", "--pages", type=int, default=1, help="result pages to scan (default 1)")
    ap.add_argument("-n", "--limit", type=int, default=0, help="keep at most N results")
    ap.add_argument("--magnets-only", action="store_true", help="print only magnet links, one per line")
    ap.add_argument("--json", action="store_true", help="print JSON")
    ap.add_argument("--torbox", action="store_true", help="show TorBox cache status for each result")
    ap.add_argument("--add", metavar="SEL", help="add to TorBox: 'all', or 1-based indexes like '1,3' or '2-4'")
    ap.add_argument("--cached-only", action="store_true", help="with --add, only add results TorBox already has cached")
    ap.add_argument("--serve", action="store_true", help="run the search page on localhost")
    ap.add_argument("--port", type=int, default=8765, help="port for --serve (default 8765)")
    ap.add_argument("--no-browser", action="store_true", help="with --serve, don't open a browser tab")
    args = ap.parse_args()

    if args.serve:
        return serve(args.port, os.environ.get("TORBOX_API_KEY"), not args.no_browser)
    if not args.title:
        ap.error("give a title to search for, or --serve")

    key = require_key() if (args.torbox or args.add or args.cached_only) else None
    results = search(args.title, args.author, args.pages, args.limit, key)
    if not results:
        sys.exit("no results")

    if args.json:
        print(json.dumps([r.public() for r in results], indent=2))
    elif args.magnets_only:
        print("\n".join(r.magnet for r in results))
    else:
        for i, r in enumerate(results, 1):
            meta = " | ".join(x for x in (r.format, r.bitrate, r.size, r.posted) if x)
            flag = "" if r.cached is None else ("  [cached]" if r.cached else "  [not cached]")
            print(f"{i:>2}. {r.title}{flag}\n    {meta}\n    {r.url}\n    {r.magnet}\n")

    if args.add:
        picks = [results[i] for i in parse_selection(args.add, len(results))]
        if args.cached_only:
            picks = [r for r in picks if r.cached]
        if not picks:
            sys.exit("nothing to add")
        for r in picks:
            resp = torbox_add(key, r.magnet, r.title)
            status = "ok" if resp.get("success") else "FAILED"
            print(f"torbox {status}: {r.title} - {resp.get('detail', '')}", file=sys.stderr)


if __name__ == "__main__":
    main()
