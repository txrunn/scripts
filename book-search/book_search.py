#!/usr/bin/env python3
"""Search for audiobooks and ebooks, and send what you pick to TorBox.

Audiobooks come from AudioBookBay: each hit's page is opened for its info hash
and trackers, and a magnet link is built from those -- no account needed.
Ebooks come from Library Genesis, with a direct download link per file.

With a TorBox API key the results also say which audiobooks TorBox already has
cached (ready now, no seeders needed), and any result can be sent to TorBox:
audiobooks as torrents, ebooks as web downloads.

    ./book_search.py "project hail mary" --author weir
    ./book_search.py "project hail mary" --books           # ebooks instead
    ./book_search.py "dune" -a herbert --torbox            # cache status per result
    ./book_search.py "dune" -a herbert --add 1,3           # send #1 and #3 to TorBox
    ./book_search.py "dune" --links-only > magnets.txt
    ./book_search.py --serve                               # search page on localhost

Environment:
    TORBOX_API_KEY   command line only: needed for --torbox and --add. The search
                     page never reads it; each visitor connects their own account.
    ABB_DOMAIN       AudioBookBay moves domains; override it here (default audiobookbay.lu)
    LIBGEN_MIRRORS   comma-separated LibGen hosts to try in order
                     (default libgen.li,libgen.bz,libgen.vg)
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
LIBGEN_MIRRORS = [h.strip() for h in os.environ.get("LIBGEN_MIRRORS", "libgen.li,libgen.bz,libgen.vg").split(",") if h.strip()]
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

LIBGEN_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.S)
LIBGEN_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
# Attribute values can hold markup (the tooltip has a <br>), so step over quoted strings.
LIBGEN_TITLE_RE = re.compile(r'<a\b(?:[^>"]|"[^"]*")*?href="(edition\.php\?id=\d+)"(?:[^>"]|"[^"]*")*>(.*?)</a>', re.S)
MD5_RE = re.compile(r"ads\.php\?md5=([0-9a-fA-F]{32})")
GET_LINK_RE = re.compile(r'href="(get\.php\?md5=[0-9a-fA-F]{32}&(?:amp;)?key=[A-Za-z0-9]+)"')


@dataclass
class Result:
    title: str
    url: str
    source: str = "audiobookbay"
    author: str = ""
    posted: str = ""
    year: str = ""
    language: str = ""
    format: str = ""
    bitrate: str = ""
    size: str = ""
    info_hash: str = ""
    magnet: str = ""
    md5: str = ""
    download_page: str = ""
    cached: bool | None = None
    trackers: list[str] = field(default_factory=list, repr=False)

    def public(self) -> dict:
        return {k: v for k, v in asdict(self).items() if k != "trackers"}


def fetch(url: str, *, data: bytes | None = None, headers: dict | None = None, method: str | None = None) -> str:
    req = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


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


def abb_finish(results: list[Result]) -> list[Result]:
    with ThreadPoolExecutor(max_workers=8) as pool:
        return [r for r in pool.map(fill_magnet, results) if r.magnet]


# --- Library Genesis ----------------------------------------------------------

def libgen_fetch(path: str) -> tuple[str, str]:
    """Fetch a path from the first LibGen mirror that answers; return (base, body)."""
    last: Exception | None = None
    for host in LIBGEN_MIRRORS:
        base = f"https://{host}"
        try:
            return base, fetch(f"{base}/{path}")
        except Exception as e:  # noqa: BLE001 - try the next mirror
            last = e
    raise RuntimeError(f"no LibGen mirror answered ({last})")


def libgen_search(query: str, pages: int = 1) -> list[Result]:
    results: list[Result] = []
    for page in range(1, pages + 1):
        qs = urllib.parse.urlencode({"req": query, "res": 50, "page": page})
        base, body = libgen_fetch(f"index.php?{qs}")
        page_results = parse_libgen_page(body, base)
        results.extend(page_results)
        if len(page_results) < 50:
            break
    return results


def parse_libgen_page(body: str, base: str) -> list[Result]:
    start = body.find('id="tablelibgen"')
    if start < 0:
        return []
    table = body[start:body.find("</table>", start)]
    results = []
    for row in LIBGEN_ROW_RE.findall(table):
        cells = LIBGEN_CELL_RE.findall(row)
        md5 = MD5_RE.search(row)
        title = LIBGEN_TITLE_RE.search(cells[0]) if cells else None
        if len(cells) < 9 or not md5 or not title:
            continue
        results.append(Result(
            title=clean(title.group(2)),
            url=f"{base}/{title.group(1)}",
            source="libgen",
            author=clean(cells[1]).strip(" ,;"),
            year=clean(cells[3]),
            language=clean(cells[4]),
            size=clean(cells[6]),
            format=clean(cells[7]),
            md5=md5.group(1).lower(),
            download_page=f"{base}/ads.php?md5={md5.group(1).lower()}",
        ))
    return results


def libgen_direct_link(md5: str) -> str:
    """The download page carries a short-lived keyed link; fetch it on demand."""
    if not re.fullmatch(r"[0-9a-f]{32}", md5):
        raise ValueError("not an md5")
    base, body = libgen_fetch(f"ads.php?md5={md5}")
    m = GET_LINK_RE.search(body)
    if not m:
        raise RuntimeError("LibGen's download page had no download link")
    return f"{base}/{html.unescape(m.group(1))}"


# Each source turns a query into Results; `finish` fills in whatever needs a
# second request per result. New libraries slot in here.
SOURCES = {
    "audiobooks": {"search": abb_search, "finish": abb_finish},
    "books": {"search": libgen_search, "finish": lambda results: results},
}


# --- Search ------------------------------------------------------------------

def words(s: str) -> list[str]:
    return re.findall(r"\w+", s.lower())


def narrow(results: list[Result], title: str, author: str | None) -> list[Result]:
    """Both sites match far more than the title: AudioBookBay searches tags and
    descriptions, LibGen searches series and publishers. Keep hits whose title
    names the book, and whose title or author line names the author."""
    if author:
        author_words = words(author)
        results = [r for r in results if all(w in words(f"{r.title} {r.author}") for w in author_words)]
    title_words = [w for w in words(title) if len(w) > 2]
    return [r for r in results if all(w in words(r.title) for w in title_words)] or results


def search(title: str, author: str | None = None, kind: str = "audiobooks", pages: int = 1,
           limit: int = 0) -> list[Result]:
    source = SOURCES[kind]
    query = f"{title} {author}" if author else title
    results = narrow(source["search"](query, pages), title, author)
    if limit:
        results = results[:limit]
    return source["finish"](results)


# --- TorBox -------------------------------------------------------------------

class TorBoxError(Exception):
    pass


def torbox(key: str | None, method: str, path: str, *, query: list | None = None, form: dict | None = None,
           json_body: dict | None = None) -> dict:
    """Call TorBox and return its JSON envelope, whether it succeeded or not."""
    url = f"{TORBOX_BASE}/{path}" + (f"?{urllib.parse.urlencode(query)}" if query else "")
    headers, data = {}, None
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if form is not None:
        data = urllib.parse.urlencode({k: v for k, v in form.items() if v is not None}).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    try:
        raw = fetch(url, data=data, method=method, headers=headers)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"success": False, "detail": f"TorBox sent back something unexpected: {raw[:120]}"}


def torbox_check_cached(key: str, hashes: list[str]) -> set[str]:
    if not hashes:
        return set()
    resp = torbox(key, "GET", "torrents/checkcached", query=[("hash", h) for h in hashes] + [("format", "object")])
    if not resp.get("success"):
        raise TorBoxError(resp.get("detail") or "cache check failed")
    return {h.lower() for h in (resp.get("data") or {})}


def torbox_add_magnet(key: str, magnet: str, name: str) -> dict:
    return torbox(key, "POST", "torrents/createtorrent", form={"magnet": magnet, "name": name})


def torbox_add_link(key: str, link: str, name: str) -> dict:
    return torbox(key, "POST", "webdl/createwebdownload", form={"link": link, "name": name})


def torbox_device_token(data) -> str | None:
    """The device flow hands back the account's API token. Accept it bare or
    under any of the names TorBox might use."""
    if isinstance(data, str):
        return data or None
    if isinstance(data, dict):
        for k in ("access_token", "token", "api_key", "apikey"):
            if isinstance(data.get(k), str) and data[k]:
                return data[k]
    return None


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
[hidden] { display: none !important; }
body {
  margin: 0; background: var(--paper); color: var(--ink);
  font: 16px/1.5 "Atkinson Hyperlegible", system-ui, sans-serif;
}
main { max-width: 46rem; margin: 0 auto; padding: 2rem 1rem 5rem; }
.account { display: flex; gap: 0.75rem; align-items: center; justify-content: flex-end; min-height: 2.5rem; font-size: 0.92rem; color: var(--muted); }
.connect {
  background: var(--sheet); border: 1px solid var(--line); border-radius: 8px;
  padding: 1rem 1.1rem; margin: 0.5rem 0 1.5rem;
}
.connect h2 { font: 500 1.15rem/1.3 Literata, Georgia, serif; margin: 0 0 0.3rem; }
.connect p { margin: 0 0 0.8rem; color: var(--muted); font-size: 0.95rem; }
.code { font: 650 2rem/1 Literata, Georgia, serif; letter-spacing: 0.12em; color: var(--ink); margin: 0.2rem 0 0.6rem; }
.keyrow { display: flex; gap: 0.5rem; flex-wrap: wrap; }
.keyrow input { flex: 1 1 14rem; }
.or { margin: 1rem 0 0.5rem; font-size: 0.9rem; color: var(--muted); }
h1 {
  font: 650 2.4rem/1.1 Literata, Georgia, serif; letter-spacing: -0.01em;
  margin: 0.5rem 0 0.4rem;
}
.lede { color: var(--muted); margin: 0 0 1.5rem; }
.kinds { display: inline-flex; border: 1px solid var(--line); border-radius: 8px; padding: 3px; margin-bottom: 1rem; }
.kinds label { cursor: pointer; }
.kinds input { position: absolute; opacity: 0; pointer-events: none; }
.kinds span { display: block; padding: 0.35rem 0.9rem; border-radius: 5px; color: var(--muted); }
.kinds input:checked + span { background: var(--ink); color: var(--paper); font-weight: 700; }
.kinds input:focus-visible + span { outline: 3px solid var(--focus); outline-offset: 1px; }
form.search { display: grid; grid-template-columns: 2fr 1fr auto; gap: 0.6rem; margin-bottom: 0.75rem; }
.field { display: grid; gap: 0.25rem; font-size: 0.85rem; color: var(--muted); }
input[type=text], input[type=password] {
  font: inherit; font-size: 1.05rem; color: var(--ink); background: var(--sheet);
  border: 1px solid var(--line); border-radius: 6px; padding: 0.6rem 0.75rem; width: 100%;
}
button, .button {
  font: inherit; font-weight: 700; cursor: pointer; border-radius: 6px; text-decoration: none;
  border: 1px solid var(--ink); background: var(--ink); color: var(--paper);
  padding: 0.6rem 1.1rem; display: inline-block; line-height: 1.5;
}
form.search button { align-self: end; }
.quiet { background: transparent; color: var(--ink); border-color: var(--line); font-weight: 400; }
.link { background: none; border: 0; padding: 0; color: var(--ink); text-decoration: underline; text-underline-offset: 3px; font-weight: 400; }
button:disabled { opacity: 0.45; cursor: not-allowed; }
:focus-visible { outline: 3px solid var(--focus); outline-offset: 2px; }
.status { min-height: 1.5rem; color: var(--muted); margin: 0.5rem 0 1.5rem; }
.status.error, .note.error { color: var(--danger); }
ol { list-style: none; margin: 0; padding: 0; border-top: 1px solid var(--line); }
li { padding: 1.1rem 0; border-bottom: 1px solid var(--line); }
.head { display: flex; gap: 0.75rem; align-items: baseline; justify-content: space-between; }
h3 { font: 500 1.25rem/1.3 Literata, Georgia, serif; margin: 0; }
h3 a { color: inherit; text-decoration: none; }
h3 a:hover { text-decoration: underline; text-underline-offset: 3px; }
.by { color: var(--muted); margin-top: 0.1rem; }
.ready {
  flex: none; font-weight: 700; font-size: 0.85rem; color: var(--ready);
  background: var(--ready-wash); border-radius: 999px; padding: 0.15rem 0.7rem;
}
.slow { flex: none; font-size: 0.85rem; color: var(--muted); }
.meta { display: flex; flex-wrap: wrap; gap: 0.25rem 1.25rem; color: var(--muted); font-size: 0.92rem; margin: 0.35rem 0 0.8rem; }
.actions { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; }
.note { font-size: 0.88rem; color: var(--muted); }
.note.ok { color: var(--ready); font-weight: 700; }
@media (max-width: 560px) {
  h1 { font-size: 1.9rem; }
  form.search { grid-template-columns: 1fr; }
  .head { flex-direction: column; gap: 0.3rem; }
}
</style>
</head>
<body>
<main>
  <div class="account">
    <span id="who"></span>
    <button type="button" class="quiet" id="connect-open">Connect TorBox</button>
    <button type="button" class="link" id="disconnect" hidden>Disconnect</button>
  </div>

  <section class="connect" id="connect" hidden aria-labelledby="connect-title">
    <h2 id="connect-title">Connect your TorBox account</h2>
    <p>Lets you see which audiobooks are ready now and send anything here to TorBox. Your key stays in this browser and is only passed along when you use TorBox. Nothing is saved on the server.</p>
    <div id="device">
      <button type="button" id="device-start">Sign in with TorBox</button>
    </div>
    <div id="device-wait" hidden>
      <p>Go to <a id="device-link" target="_blank" rel="noreferrer"></a> and enter this code:</p>
      <div class="code" id="device-code"></div>
      <p class="note" id="device-note">Waiting for you to approve it…</p>
    </div>
    <p class="or">Or paste an API key from torbox.app, under Settings:</p>
    <form class="keyrow" id="key-form">
      <input type="password" id="key-input" autocomplete="off" aria-label="TorBox API key" placeholder="API key">
      <button class="quiet">Save key</button>
    </form>
    <p class="note" id="connect-note" role="status"></p>
  </section>

  <h1>Find a book</h1>
  <p class="lede" id="lede"></p>
  <div class="kinds" role="radiogroup" aria-label="What to search">
    <label><input type="radio" name="kind" value="audiobooks" checked><span>Audiobooks</span></label>
    <label><input type="radio" name="kind" value="books"><span>Ebooks</span></label>
  </div>
  <form class="search" id="search">
    <label class="field">Title <input type="text" name="title" required autofocus autocomplete="off"></label>
    <label class="field">Author (optional) <input type="text" name="author" autocomplete="off"></label>
    <button>Search</button>
  </form>
  <p class="status" id="status" role="status"></p>
  <ol id="results"></ol>
</main>
<script>
const $ = (id) => document.getElementById(id);
const store = {
  get() { try { return localStorage.getItem("torbox-key") || ""; } catch { return ""; } },
  set(v) { try { v ? localStorage.setItem("torbox-key", v) : localStorage.removeItem("torbox-key"); } catch {} },
};
let key = store.get();
let lastResults = [];

function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v; else n.setAttribute(k, v);
  }
  n.append(...kids.filter(k => k !== null && k !== undefined && k !== ""));
  return n;
}

async function api(path, body) {
  const opts = { headers: {} };
  if (key) opts.headers["X-TorBox-Key"] = key;
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.success === false) throw new Error(data.detail || data.error || "Something went wrong.");
  return data;
}

// --- Account ---------------------------------------------------------------

const kind = () => document.querySelector("input[name=kind]:checked").value;

function renderAccount(label) {
  $("who").textContent = key ? (label ? `TorBox: ${label}` : "TorBox connected") : "";
  $("connect-open").hidden = !!key;
  $("disconnect").hidden = !key;
  if (key) $("connect").hidden = true;
  renderLede();
}

function renderLede() {
  $("lede").textContent = kind() === "books"
    ? (key ? "Searches Library Genesis. Download a file directly, or send it to TorBox."
           : "Searches Library Genesis and gives you a direct download for each file.")
    : key ? "Searches AudioBookBay. Uploads marked Ready now are already on TorBox and download straight away."
          : "Searches AudioBookBay and gives you a magnet link for each upload. Connect TorBox to see which are ready now.";
}

async function useKey(candidate) {
  const prev = key;
  key = candidate;
  try {
    const me = await api("/api/torbox/me");
    store.set(key);
    renderAccount(me.email);
    if (lastResults.length) markCached(lastResults);
    return true;
  } catch (e) {
    key = prev;
    $("connect-note").className = "note error";
    $("connect-note").textContent = `TorBox didn't accept that key: ${e.message}`;
    return false;
  }
}

$("connect-open").addEventListener("click", () => {
  $("connect").hidden = !$("connect").hidden;
  if (!$("connect").hidden) $("device-start").focus();
});
$("disconnect").addEventListener("click", () => {
  key = ""; store.set("");
  renderAccount();
  if (lastResults.length) render(lastResults.map(r => ({ ...r, cached: null })));
});
$("key-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const v = $("key-input").value.trim();
  if (!v) return;
  if (await useKey(v)) $("key-input").value = "";
});

let polling = null;
$("device-start").addEventListener("click", async () => {
  $("connect-note").textContent = "";
  try {
    const d = await api("/api/torbox/device/start");
    $("device").hidden = true;
    $("device-wait").hidden = false;
    $("device-link").href = d.verification_url;
    $("device-link").textContent = d.friendly_verification_url || d.verification_url;
    $("device-code").textContent = d.code;
    const expires = Date.parse(d.expires_at) || Date.now() + 10 * 60e3;
    clearInterval(polling);
    polling = setInterval(async () => {
      if (Date.now() > expires) {
        clearInterval(polling);
        $("device").hidden = false; $("device-wait").hidden = true;
        $("connect-note").className = "note error";
        $("connect-note").textContent = "The code expired. Start again to get a new one.";
        return;
      }
      try {
        const t = await api("/api/torbox/device/token", { device_code: d.device_code });
        if (t.token) {
          clearInterval(polling);
          $("device").hidden = false; $("device-wait").hidden = true;
          await useKey(t.token);
        }
      } catch (e) {
        clearInterval(polling);
        $("device").hidden = false; $("device-wait").hidden = true;
        $("connect-note").className = "note error";
        $("connect-note").textContent = e.message;
      }
    }, Math.max(5, d.interval || 5) * 1000);
  } catch (e) {
    $("connect-note").className = "note error";
    $("connect-note").textContent = `Couldn't start sign-in: ${e.message}`;
  }
});

// --- Results ---------------------------------------------------------------

function sendButton(r) {
  if (!key) return null;
  const note = el("span", { class: "note", "aria-live": "polite" });
  const send = el("button", { type: "button" }, "Send to TorBox");
  send.addEventListener("click", async () => {
    send.disabled = true;
    note.className = "note"; note.textContent = "Sending…";
    try {
      await api("/api/torbox/add", r.source === "libgen"
        ? { md5: r.md5, name: `${r.title}${r.format ? "." + r.format : ""}` }
        : { magnet: r.magnet, name: r.title });
      note.className = "note ok"; note.textContent = "Sent to TorBox";
    } catch (e) {
      send.disabled = false;
      note.className = "note error"; note.textContent = e.message;
    }
  });
  return [send, note];
}

function row(r) {
  const actions = el("div", { class: "actions" });
  const sent = sendButton(r);
  if (r.source === "libgen") {
    actions.append(el("a", { class: "button" + (sent ? " quiet" : ""), href: `/api/libgen/download?md5=${r.md5}`, target: "_blank", rel: "noreferrer" }, "Download"));
    if (sent) actions.prepend(sent[0]), actions.append(sent[1]);
  } else {
    const copy = el("button", { type: "button", class: "quiet" }, "Copy magnet link");
    copy.addEventListener("click", async () => {
      await navigator.clipboard.writeText(r.magnet);
      copy.textContent = "Copied";
      setTimeout(() => (copy.textContent = "Copy magnet link"), 1500);
    });
    if (sent) actions.append(sent[0]);
    actions.append(copy, el("a", { class: "button quiet", href: r.magnet }, "Open in torrent app"));
    if (sent) actions.append(sent[1]);
  }
  const badge = r.cached === true ? el("span", { class: "ready" }, "Ready now")
    : r.cached === false ? el("span", { class: "slow" }, "Not cached, needs seeders") : null;
  return el("li", {},
    el("div", { class: "head" }, el("h3", {}, el("a", { href: r.url, target: "_blank", rel: "noreferrer" }, r.title)), badge),
    r.author ? el("div", { class: "by" }, r.author) : null,
    el("div", { class: "meta" },
      r.format && el("span", {}, r.format.toUpperCase()),
      r.bitrate && el("span", {}, r.bitrate),
      r.size && el("span", {}, r.size),
      r.year && el("span", {}, r.year),
      r.language && el("span", {}, r.language),
      r.posted && el("span", {}, "Posted " + r.posted)),
    actions);
}

function render(results) {
  lastResults = results;
  $("results").replaceChildren(...results.map(row));
  const n = results.length, noun = kind() === "books" ? "file" : "upload";
  const ready = results.filter(r => r.cached).length;
  $("status").className = "status";
  $("status").textContent = `${n} ${noun}${n === 1 ? "" : "s"}` +
    (results.some(r => r.cached !== null && r.cached !== undefined) ? `, ${ready} ready now on TorBox.` : ".");
}

async function markCached(results) {
  const hashes = results.filter(r => r.info_hash).map(r => r.info_hash);
  if (!key || !hashes.length) return render(results);
  try {
    const { cached } = await api("/api/torbox/cached", { hashes });
    render(results.map(r => r.info_hash ? { ...r, cached: cached.includes(r.info_hash) } : r));
  } catch (e) {
    render(results);
    $("status").textContent += ` Couldn't check TorBox: ${e.message}`;
  }
}

$("search").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const fd = new FormData(ev.target);
  const params = new URLSearchParams({ kind: kind(), title: fd.get("title").trim(), author: fd.get("author").trim() });
  history.replaceState(null, "", "?" + params);
  $("results").replaceChildren();
  lastResults = [];
  $("status").className = "status";
  $("status").textContent = kind() === "books" ? "Searching…"
    : "Searching… each result's page is opened for its magnet link, so this takes a few seconds.";
  try {
    const data = await api("/api/search?" + params);
    if (!data.results.length) {
      $("status").textContent = "Nothing matched. Try fewer words, or drop the author.";
      return;
    }
    await markCached(data.results);
  } catch (e) {
    $("status").className = "status error";
    $("status").textContent = e.message;
  }
});

for (const input of document.querySelectorAll("input[name=kind]")) {
  input.addEventListener("change", () => {
    renderLede();
    if ($("search").title.value.trim()) $("search").requestSubmit();
  });
}

const initial = new URLSearchParams(location.search);
if (initial.get("kind") === "books") document.querySelector("input[value=books]").checked = true;
renderAccount();
if (key) api("/api/torbox/me").then(me => renderAccount(me.email)).catch(() => {});
if (initial.get("title")) {
  $("search").title.value = initial.get("title");
  $("search").author.value = initial.get("author") || "";
  $("search").requestSubmit();
}
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    """The search page and a thin TorBox pass-through.

    The server holds no TorBox key. Each request that needs one carries the
    visitor's own key in X-TorBox-Key; it is forwarded to TorBox and dropped.
    """

    def log_message(self, *_):  # keep the terminal quiet, and keys out of logs
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _key(self) -> str | None:
        key = (self.headers.get("X-TorBox-Key") or "").strip()
        if not key:
            self._json(401, {"success": False, "detail": "Connect TorBox first."})
            return None
        return key

    def _body(self) -> dict:
        try:
            return json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        qs = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        if url.path == "/":
            return self._send(200, PAGE.encode(), "text/html; charset=utf-8")

        if url.path == "/api/search":
            title, author = qs.get("title", "").strip(), qs.get("author", "").strip() or None
            kind = qs.get("kind", "audiobooks")
            if not title:
                return self._json(400, {"error": "Enter a title."})
            if kind not in SOURCES:
                return self._json(400, {"error": "Unknown kind of search."})
            try:
                results = search(title, author, kind)
            except Exception as e:  # noqa: BLE001 - report it on the page
                site = "LibGen" if kind == "books" else "AudioBookBay (set ABB_DOMAIN if it moved)"
                return self._json(502, {"error": f"Couldn't reach {site}: {e}."})
            return self._json(200, {"results": [r.public() for r in results]})

        if url.path == "/api/libgen/download":
            try:
                link = libgen_direct_link(qs.get("md5", "").lower())
            except Exception as e:  # noqa: BLE001
                return self._json(502, {"error": f"Couldn't get a download link: {e}."})
            self.send_response(302)
            self.send_header("Location", link)
            self.send_header("Content-Length", "0")
            return self.end_headers()

        if url.path == "/api/torbox/me":
            if not (key := self._key()):
                return
            resp = torbox(key, "GET", "user/me")
            if not resp.get("success"):
                return self._json(401, {"success": False, "detail": resp.get("detail") or "TorBox refused the key."})
            return self._json(200, {"success": True, "email": (resp.get("data") or {}).get("email", "")})

        if url.path == "/api/torbox/device/start":
            resp = torbox(None, "GET", "user/auth/device/start", query=[("app", "Book search")])
            if not resp.get("success"):
                return self._json(502, resp)
            d = resp["data"]
            return self._json(200, {k: d.get(k) for k in ("device_code", "code", "verification_url",
                                                           "friendly_verification_url", "interval", "expires_at")})

        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        body = self._body()

        if path == "/api/torbox/device/token":
            resp = torbox(None, "POST", "user/auth/device/token", json_body={"device_code": str(body.get("device_code", ""))})
            if resp.get("success"):
                return self._json(200, {"token": torbox_device_token(resp.get("data"))})
            if resp.get("error") == "DEVICE_CODE_NOT_USED":
                return self._json(200, {"token": None})
            return self._json(400, {"success": False, "detail": resp.get("detail") or "Sign-in failed."})

        if path == "/api/torbox/cached":
            if not (key := self._key()):
                return
            hashes = [h.lower() for h in body.get("hashes", []) if re.fullmatch(r"[0-9a-fA-F]{40}", str(h))]
            try:
                return self._json(200, {"cached": sorted(torbox_check_cached(key, hashes))})
            except Exception as e:  # noqa: BLE001
                return self._json(502, {"success": False, "detail": str(e)})

        if path == "/api/torbox/add":
            if not (key := self._key()):
                return
            name = str(body.get("name", ""))
            if md5 := str(body.get("md5", "")).lower():
                try:
                    link = libgen_direct_link(md5)
                except Exception as e:  # noqa: BLE001
                    return self._json(502, {"success": False, "detail": f"Couldn't get a download link: {e}."})
                resp = torbox_add_link(key, link, name)
            elif (magnet := str(body.get("magnet", ""))).startswith("magnet:?"):
                resp = torbox_add_magnet(key, magnet, name)
            else:
                return self._json(400, {"success": False, "detail": "Nothing to send."})
            return self._json(200 if resp.get("success") else 502, resp)

        self._json(404, {"error": "not found"})


def serve(host: str, port: int, open_browser: bool) -> None:
    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{server.server_port}/"
    print(f"Book search on {url}  (ctrl-c to stop)")
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
    ap.add_argument("-b", "--books", action="store_true", help="search ebooks (LibGen) instead of audiobooks")
    ap.add_argument("-p", "--pages", type=int, default=1, help="result pages to scan (default 1)")
    ap.add_argument("-n", "--limit", type=int, default=0, help="keep at most N results")
    ap.add_argument("--links-only", "--magnets-only", action="store_true",
                    help="print only links, one per line: magnets for audiobooks, download pages for ebooks")
    ap.add_argument("--json", action="store_true", help="print JSON")
    ap.add_argument("--torbox", action="store_true", help="show TorBox cache status for each audiobook")
    ap.add_argument("--add", metavar="SEL", help="send to TorBox: 'all', or 1-based indexes like '1,3' or '2-4'")
    ap.add_argument("--cached-only", action="store_true", help="with --add, only send audiobooks TorBox already has cached")
    ap.add_argument("--serve", action="store_true", help="run the search page")
    ap.add_argument("--host", default="127.0.0.1", help="address for --serve (default 127.0.0.1, this machine only)")
    ap.add_argument("--port", type=int, default=8765, help="port for --serve (default 8765)")
    ap.add_argument("--no-browser", action="store_true", help="with --serve, don't open a browser tab")
    args = ap.parse_args()

    if args.serve:
        return serve(args.host, args.port, not args.no_browser)
    if not args.title:
        ap.error("give a title to search for, or --serve")

    kind = "books" if args.books else "audiobooks"
    key = require_key() if (args.torbox or args.add or args.cached_only) else None
    results = search(args.title, args.author, kind, args.pages, args.limit)
    if not results:
        sys.exit("no results")
    if key and kind == "audiobooks":
        try:
            cached = torbox_check_cached(key, [r.info_hash for r in results])
            for r in results:
                r.cached = r.info_hash in cached
        except Exception as e:  # noqa: BLE001 - results are still useful without it
            print(f"warn: TorBox cache check failed: {e}", file=sys.stderr)

    if args.json:
        print(json.dumps([r.public() for r in results], indent=2))
    elif args.links_only:
        print("\n".join(r.magnet or r.download_page for r in results))
    else:
        for i, r in enumerate(results, 1):
            meta = " | ".join(x for x in (r.author, r.format, r.bitrate, r.size, r.year, r.language, r.posted) if x)
            flag = "" if r.cached is None else ("  [cached]" if r.cached else "  [not cached]")
            print(f"{i:>2}. {r.title}{flag}\n    {meta}\n    {r.url}\n    {r.magnet or r.download_page}\n")

    if args.add:
        picks = [results[i] for i in parse_selection(args.add, len(results))]
        if args.cached_only:
            picks = [r for r in picks if r.cached]
        if not picks:
            sys.exit("nothing to add")
        for r in picks:
            if r.md5:
                resp = torbox_add_link(key, libgen_direct_link(r.md5), f"{r.title}.{r.format}" if r.format else r.title)
            else:
                resp = torbox_add_magnet(key, r.magnet, r.title)
            status = "ok" if resp.get("success") else "FAILED"
            print(f"torbox {status}: {r.title} - {resp.get('detail', '')}", file=sys.stderr)


if __name__ == "__main__":
    main()
