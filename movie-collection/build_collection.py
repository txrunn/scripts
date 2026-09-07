#!/usr/bin/env python3
"""Build a browsable web page from a physical-media collection inventory.

You edit one plain-text file, `collection.txt`, adding a line per disc you buy.
Everything else -- year, director, runtime, genre, IMDb rating, Rotten Tomatoes
Tomatometer, poster, Letterboxd link, shelf order, director blocks -- is looked
up once, cached in the repo, and reused forever after.

The network is touched only for titles that are not already in the cache, so a
rebuild after adding two discs makes a handful of requests, not two hundred. If
nothing in the inventory changed, the build is a no-op: the page is not rewritten
and nothing is printed, which is what makes it safe to run on a schedule.

No metadata is ever invented. A field that no source could supply is left empty
and the reason is recorded in Source_Notes, so a blank in the CSV always means
"nobody would tell us" rather than "the script forgot".

Stdlib only. Python 3.11+ (tomllib).
"""

import argparse
import base64
import csv
import datetime as dt
import gzip
import hashlib
import html
import io
import json
import os
import re
import string
import sys
import time
import tomllib
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

# --- Configuration -----------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_COLLECTION = os.path.join(SCRIPT_DIR, "collection.txt")
DEFAULT_OVERRIDES = os.path.join(SCRIPT_DIR, "overrides.toml")
# Committed, deliberately. Runners keep nothing between jobs, so the cache has
# to live in the repo or every scheduled build would re-fetch the whole shelf.
# It doubles as the audit trail: `git log cache/metadata.json` is a record of
# when each disc was added.
DEFAULT_CACHE = os.path.join(SCRIPT_DIR, "cache", "metadata.json")
DEFAULT_LEDGER = os.path.join(SCRIPT_DIR, "cache", "build.json")
DEFAULT_OUT_DIR = os.path.join(SCRIPT_DIR, "site")

TMDB_API = "https://api.themoviedb.org/3"
# w342 is the smallest poster that still looks right at the card size below;
# w500 quadruples the page weight for no visible gain at 190px wide.
TMDB_IMAGE = "https://image.tmdb.org/t/p/w342"
OMDB_API = "https://www.omdbapi.com/"
# /tmdb/<id>/ and /imdb/<id>/ are Letterboxd's own id-redirect routes; both 302
# to the canonical film page. We follow one at fetch time and cache the real
# slug so the page links straight there, but either form works in a browser.
LETTERBOXD_TMDB = "https://letterboxd.com/tmdb/{tmdb_id}/"
LETTERBOXD_IMDB = "https://letterboxd.com/imdb/{imdb_id}/"

# A director earns a shelf block at this many owned films. Nothing is hardcoded
# per-director: the count is taken from the resolved credits every build, so
# buying a third Kubrick creates the Kubrick block on its own.
BLOCK_THRESHOLD = 3

# Ignored when alphabetising, so "The Green Knight" files under G.
LEADING_ARTICLES = ("the ", "a ", "an ")

SECTIONS = ("films", "collections", "documentaries")
CATEGORY = {
    "films": "Film",
    "collections": "Collection",
    "documentaries": "Documentary",
}

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
TIMEOUT = 20
RETRIES = 3
# TMDB allows ~50 req/s and we are nowhere near it, but a cold build is ~300
# requests and there is no reason to be rude about it.
THROTTLE = 0.06

CSV_COLUMNS = [
    "Title", "Year", "Director", "Directors", "Runtime_Minutes", "Genre",
    "IMDb_Rating", "RT_Critic_Percent", "RT_Audience_Percent",
    "Theatrical_Score", "Category", "Director_Block", "Franchise",
    "Collection", "Shelf_Section", "Shelf_Order", "4K_Status", "HDR_Format",
    "Dolby_Vision", "Disc_Notes", "Source_Notes", "Metadata_Verified",
]

# Bump when the HTML template changes, so a template edit forces a rebuild even
# though the inventory is untouched.
TEMPLATE_VERSION = 1


class FetchError(Exception):
    """The network, or an API, would not cooperate."""


class SchemaError(Exception):
    """A response was not shaped the way the parser expects."""


class InventoryError(Exception):
    """collection.txt or overrides.toml is malformed."""


# --- Inventory ---------------------------------------------------------------

# A trailing "(1999)" is a disambiguator, not part of the title. Only a bare
# 4-digit year counts, so "Blade Runner 2049" and "Se7en (Director's Cut)" are
# left alone.
YEAR_HINT = re.compile(r"^(.*?)\s*\((\d{4})\)\s*$")
SECTION_HEADER = re.compile(r"^\[([a-z]+)\]$")


def parse_inventory(path):
    """Read collection.txt into a list of entry dicts, in file order.

    The raw line is kept as `key`: it is what the cache and overrides.toml are
    keyed by, so renaming a line is what re-resolves a film -- deliberately, as
    that is the only signal we get that you meant a different disc.
    """
    entries = []
    seen = {}
    section = "films"

    with open(os.path.expanduser(path), encoding="utf-8") as handle:
        for number, raw in enumerate(handle, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue

            header = SECTION_HEADER.match(line)
            if header:
                section = header.group(1)
                if section not in SECTIONS:
                    raise InventoryError(
                        f"{path}:{number}: unknown section [{section}]; "
                        f"expected one of {', '.join(SECTIONS)}"
                    )
                continue

            if line in seen:
                raise InventoryError(
                    f"{path}:{number}: duplicate entry {line!r} "
                    f"(first seen on line {seen[line]})"
                )
            seen[line] = number

            match = YEAR_HINT.match(line)
            entries.append({
                "key": line,
                "query": match.group(1) if match else line,
                "year_hint": int(match.group(2)) if match else None,
                "section": section,
                "line": number,
            })

    if not entries:
        raise InventoryError(f"{path} has no entries")
    return entries


def load_overrides(path):
    """Read overrides.toml. Absent is fine -- it is an optional file."""
    if not os.path.exists(os.path.expanduser(path)):
        return {}
    with open(os.path.expanduser(path), "rb") as handle:
        try:
            return tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise InventoryError(f"{path}: {exc}") from exc


def sort_title(title):
    """Alphabetisation key: leading article dropped, accents folded, cased down.

    "The Green Knight" files under G, "Pan's Labyrinth" next to "Parasite",
    and "E.T." sorts as written rather than after every other E title.
    """
    flat = unicodedata.normalize("NFKD", title)
    flat = "".join(c for c in flat if not unicodedata.combining(c)).lower()
    for article in LEADING_ARTICLES:
        if flat.startswith(article):
            flat = flat[len(article):]
            break
    # Keep digits and letters only, so ":" and "-" do not reorder a franchise.
    return re.sub(r"[^a-z0-9 ]", "", flat).strip()


# --- Fetching ----------------------------------------------------------------


def fetch(url, expect_json=True):
    """GET `url`, retrying transient failures with exponential backoff.

    A 4xx is not retried: it means the key or the id is wrong, and asking again
    will not change that. 401 and 429 get a message saying which, because those
    are the two that actually happen in practice.
    """
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json,text/plain,*/*",
        "Accept-Encoding": "gzip",
    })

    last_error = None
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                raw = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                break
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise FetchError(f"HTTP 401 from {_redact(url)} -- API key rejected")
            if exc.code == 404:
                raise FetchError(f"HTTP 404 from {_redact(url)}")
            if exc.code == 429:
                # The only 4xx worth waiting out.
                last_error = "HTTP 429 (rate limited)"
            elif exc.code < 500:
                raise FetchError(f"HTTP {exc.code} from {_redact(url)}")
            else:
                last_error = f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = str(exc)

        if attempt < RETRIES - 1:
            delay = 2 ** (attempt + 1)
            print(f"  fetch failed ({last_error}); retrying in {delay}s", file=sys.stderr)
            time.sleep(delay)
    else:
        raise FetchError(
            f"giving up on {_redact(url)} after {RETRIES} attempts: {last_error}"
        )

    time.sleep(THROTTLE)
    if not expect_json:
        return raw
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchemaError(f"response from {_redact(url)} is not JSON: {exc}") from exc


def _redact(url):
    """Strip api keys out of a URL before it reaches a log or an error."""
    return re.sub(r"(api_?key=)[^&]+", r"\1REDACTED", url, flags=re.I)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def resolve_letterboxd(tmdb_id, imdb_id):
    """Turn an id into the canonical letterboxd.com/film/<slug>/ URL.

    Letterboxd 302s /tmdb/<id>/ to the real page, so we read the Location header
    once and cache the slug -- the link in the page is then direct. Falling back
    to the redirect URL itself is harmless: it works in a browser either way,
    which is why a failure here never fails the build.
    """
    opener = urllib.request.build_opener(NoRedirect)
    for template, value in ((LETTERBOXD_TMDB, tmdb_id), (LETTERBOXD_IMDB, imdb_id)):
        if not value:
            continue
        url = template.format(tmdb_id=value, imdb_id=value)
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with opener.open(request, timeout=TIMEOUT) as response:
                location = response.headers.get("Location")
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location") if exc.code in (301, 302) else None
        except (urllib.error.URLError, TimeoutError, OSError):
            location = None
        time.sleep(THROTTLE)
        if location and "/film/" in location:
            return urllib.parse.urljoin(url, location)
    if tmdb_id:
        return LETTERBOXD_TMDB.format(tmdb_id=tmdb_id)
    return None


# --- Providers ---------------------------------------------------------------


def tmdb_search(query, year_hint, api_key):
    """Find the best TMDB match for an inventory line.

    The year is scored, not filtered. A hint that is a year off -- festival
    premiere versus wide release, which bites Talk to Me and Parasite -- should
    still match, so an exact year is worth a lot, adjacent years a little, and a
    wrong year merely loses the bonus rather than the film.
    """
    url = "{}/search/movie?api_key={}&query={}&include_adult=false".format(
        TMDB_API, api_key, urllib.parse.quote(query)
    )
    payload = fetch(url)
    results = payload.get("results")
    if results is None:
        raise SchemaError("TMDB search response has no 'results' key")
    if not results:
        return None

    def score(result):
        points = 0.0
        title = result.get("title") or ""
        original = result.get("original_title") or ""
        if sort_title(title) == sort_title(query) or sort_title(original) == sort_title(query):
            points += 100
        released = (result.get("release_date") or "")[:4]
        if year_hint and released.isdigit():
            gap = abs(int(released) - year_hint)
            points += 60 if gap == 0 else (25 if gap == 1 else 0)
        # Popularity only breaks ties. It is a terrible primary signal -- it
        # would hand "Alien" to whatever mockbuster is trending this week.
        points += min(result.get("popularity") or 0, 50) / 10.0
        return points

    return max(results, key=score)


def tmdb_movie(tmdb_id, api_key):
    """Full record for one film. One request: credits come along for the ride."""
    url = "{}/movie/{}?api_key={}&append_to_response=credits".format(
        TMDB_API, tmdb_id, api_key
    )
    payload = fetch(url)
    if "title" not in payload:
        raise SchemaError(f"TMDB movie {tmdb_id} response has no 'title'")
    return payload


def directors_of(payload):
    """Every credited director, in credit order.

    A list, not a string: the Wachowskis, the Coens and the Daniels are all
    genuinely co-directed, and collapsing them to one name would both misreport
    the film and skew the 3+ block counts.
    """
    crew = (payload.get("credits") or {}).get("crew") or []
    names = []
    for member in crew:
        if member.get("job") == "Director":
            name = member.get("name")
            if name and name not in names:
                names.append(name)
    return names


def omdb_lookup(imdb_id, api_key):
    """IMDb rating and the Rotten Tomatoes Tomatometer, by IMDb id.

    OMDb's `Ratings` array is the only free source for the Tomatometer. It does
    NOT carry the RT audience score -- that is why RT_Audience_Percent is filled
    from overrides.toml or left blank, never substituted from somewhere else.
    """
    url = "{}?i={}&apikey={}".format(OMDB_API, urllib.parse.quote(imdb_id), api_key)
    payload = fetch(url)
    if payload.get("Response") == "False":
        raise FetchError("OMDb: {}".format(payload.get("Error", "unknown error")))
    return payload


def rt_critic_from(payload):
    """Pull the Tomatometer percentage out of OMDb's Ratings array."""
    for rating in payload.get("Ratings") or []:
        if rating.get("Source") == "Rotten Tomatoes":
            match = re.match(r"(\d+)%", rating.get("Value", ""))
            if match:
                return int(match.group(1))
    return None


def _number(value, cast=float):
    """OMDb writes "N/A" where a real API would write null."""
    if value in (None, "", "N/A"):
        return None
    try:
        return cast(value)
    except (TypeError, ValueError):
        return None


# --- Resolution --------------------------------------------------------------


def resolve(entry, keys, notes):
    """Look one inventory entry up, or return what the cache already knows.

    Returns the record. `notes` accumulates per-film Source_Notes -- what could
    not be found and why -- which is the whole reason a blank field is
    trustworthy: it means a source was asked and declined, not that we guessed.
    """
    record = {
        "key": entry["key"],
        "section": entry["section"],
        "title": entry["query"],
        "year": entry["year_hint"],
        "directors": [],
        "runtime": None,
        "genres": [],
        "overview": "",
        "poster": None,
        "tmdb_id": None,
        "imdb_id": None,
        "imdb_rating": None,
        "rt_critic": None,
        "rt_audience": None,
        "letterboxd": None,
        "source_notes": [],
        "verified": None,
    }

    if entry["section"] != "films":
        # Box sets and the nature series are not TMDB movies. Fabricating a
        # match for "Bourne: The Ultimate Collection" would put a single film's
        # runtime and director on a five-disc set, so they stay unresolved by
        # design and carry only what overrides.toml says.
        record["source_notes"].append(
            "box set / non-film entry: not looked up against TMDB"
        )
        record["verified"] = dt.date.today().isoformat()
        return record

    found = tmdb_search(entry["query"], entry["year_hint"], keys["tmdb"])
    if not found:
        record["source_notes"].append("no TMDB match for this title")
        notes.append(f"{entry['key']}: no TMDB match")
        return record

    detail = tmdb_movie(found["id"], keys["tmdb"])

    record["tmdb_id"] = detail["id"]
    record["title"] = detail.get("title") or entry["query"]
    released = (detail.get("release_date") or "")[:4]
    record["year"] = int(released) if released.isdigit() else None
    record["directors"] = directors_of(detail)
    record["runtime"] = detail.get("runtime") or None
    record["genres"] = [g["name"] for g in detail.get("genres") or [] if g.get("name")]
    record["overview"] = detail.get("overview") or ""
    record["poster"] = detail.get("poster_path")
    record["imdb_id"] = detail.get("imdb_id") or None

    if not record["directors"]:
        record["source_notes"].append("TMDB lists no director credit")
    if not record["runtime"]:
        record["source_notes"].append("TMDB has no runtime")

    if keys["omdb"] and record["imdb_id"]:
        try:
            omdb = omdb_lookup(record["imdb_id"], keys["omdb"])
            record["imdb_rating"] = _number(omdb.get("imdbRating"))
            record["rt_critic"] = rt_critic_from(omdb)
            if record["rt_critic"] is None:
                record["source_notes"].append(
                    "Rotten Tomatoes has no Tomatometer for this title on OMDb"
                )
        except (FetchError, SchemaError) as exc:
            record["source_notes"].append(f"OMDb lookup failed: {exc}")
            notes.append(f"{entry['key']}: OMDb lookup failed ({exc})")
    elif not keys["omdb"]:
        record["source_notes"].append(
            "no OMDB_API_KEY set: IMDb rating and Tomatometer not fetched"
        )
    elif not record["imdb_id"]:
        record["source_notes"].append(
            "TMDB has no IMDb id, so OMDb could not be queried"
        )

    # Never filled by a provider. Kept as a column because it was asked for, and
    # left blank because inventing a number is worse than an empty cell.
    record["source_notes"].append(
        "RT audience score and Theatrical_Score have no free API source; "
        "set them in overrides.toml"
    )

    record["letterboxd"] = resolve_letterboxd(record["tmdb_id"], record["imdb_id"])
    record["verified"] = dt.date.today().isoformat()
    return record


def apply_overrides(record, overrides):
    """Lay overrides.toml on top of a record. Manual data always wins."""
    manual = overrides.get(record["key"])
    if not manual:
        record.setdefault("uhd", True)
        return record

    mapping = {
        "title": "title", "year": "year", "runtime": "runtime",
        "rt_critic": "rt_critic", "rt_audience": "rt_audience",
        "imdb_rating": "imdb_rating", "theatrical_score": "theatrical_score",
        "hdr": "hdr", "uhd": "uhd", "uhd_year": "uhd_year",
        "disc_notes": "disc_notes", "franchise": "franchise",
        "letterboxd": "letterboxd", "tmdb_id": "tmdb_id",
    }
    for source, target in mapping.items():
        if source in manual:
            record[target] = manual[source]
    if "directors" in manual:
        record["directors"] = list(manual["directors"])
    if "genres" in manual:
        record["genres"] = list(manual["genres"])
    record.setdefault("uhd", True)
    return record


# --- Shelf -------------------------------------------------------------------


def director_blocks(records):
    """Every director with BLOCK_THRESHOLD+ owned films, and their films.

    Counted from the resolved credits rather than a hardcoded list, so the third
    Kubrick disc creates the Kubrick block with no code change. Co-directed
    films count for each credited director, which is the honest reading of
    "films by that director" -- and cannot double-shelve anything, because a
    film is assigned to at most one block below.
    """
    counts = {}
    for record in records:
        if record["section"] != "films":
            continue
        for name in record["directors"]:
            counts.setdefault(name, []).append(record)
    return {
        name: films
        for name, films in counts.items()
        if len(films) >= BLOCK_THRESHOLD
    }


def _surname(name):
    return sort_title(name.split()[-1] if name.split() else name)


def shelve(records):
    """Assign every record a shelf section and a position on the shelf.

    Director blocks first, alphabetically by surname, each in release order.
    Then everything else alphabetically, ignoring leading articles. Box sets sit
    on their own shelf at the end rather than being scattered through the As.
    """
    blocks = director_blocks(records)

    # A film by two blocked directors goes to the one with fewer films, so the
    # smaller block stays intact rather than being hollowed out by the larger.
    assigned = {}
    for name in sorted(blocks, key=lambda n: (len(blocks[n]), _surname(n))):
        for record in blocks[name]:
            assigned.setdefault(id(record), name)

    order = 0
    shelf = []

    for name in sorted(blocks, key=_surname):
        films = [r for r in blocks[name] if assigned[id(r)] == name]
        if len(films) < BLOCK_THRESHOLD:
            # Emptied out by the tie-break above; its films live in the other
            # director's block and this one no longer meets the rule.
            continue
        for record in sorted(films, key=lambda r: (r["year"] or 9999, sort_title(r["title"]))):
            record["block"] = name
            record["shelf"] = f"Director block: {name}"
            record["order"] = order
            order += 1
            shelf.append(record)

    rest = [
        r for r in records
        if r["section"] == "films" and "order" not in r
    ]
    for record in sorted(rest, key=lambda r: sort_title(r["title"])):
        record["block"] = ""
        record["shelf"] = "Alphabetical"
        record["order"] = order
        order += 1
        shelf.append(record)

    for section in ("collections", "documentaries"):
        group = [r for r in records if r["section"] == section]
        for record in sorted(group, key=lambda r: sort_title(r["title"])):
            record["block"] = ""
            record["shelf"] = CATEGORY[section] + " shelf"
            record["order"] = order
            order += 1
            shelf.append(record)

    return shelf, blocks


# --- Output: CSV -------------------------------------------------------------


def write_csv(path, shelf):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for record in shelf:
            hdr = record.get("hdr", "")
            writer.writerow({
                "Title": record["title"],
                "Year": record["year"] or "",
                "Director": record["directors"][0] if record["directors"] else "",
                "Directors": "; ".join(record["directors"]),
                "Runtime_Minutes": record["runtime"] or "",
                "Genre": "; ".join(record["genres"]),
                "IMDb_Rating": record["imdb_rating"] if record["imdb_rating"] is not None else "",
                "RT_Critic_Percent": record["rt_critic"] if record["rt_critic"] is not None else "",
                "RT_Audience_Percent": record["rt_audience"] if record.get("rt_audience") is not None else "",
                "Theatrical_Score": record.get("theatrical_score", ""),
                "Category": CATEGORY[record["section"]],
                "Director_Block": record.get("block", ""),
                "Franchise": record.get("franchise", ""),
                "Collection": record["title"] if record["section"] == "collections" else "",
                "Shelf_Section": record["shelf"],
                "Shelf_Order": record["order"] + 1,
                "4K_Status": "4K UHD" if record.get("uhd", True) else "Blu-ray",
                "HDR_Format": hdr,
                "Dolby_Vision": "Yes" if "dolby vision" in str(hdr).lower() else "",
                "Disc_Notes": record.get("disc_notes", ""),
                "Source_Notes": " | ".join(record["source_notes"]),
                "Metadata_Verified": record.get("verified") or "",
            })


# --- Output: HTML ------------------------------------------------------------

PAGE = string.Template("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$page_title</title>
<style>
:root {
  color-scheme: light dark;
  --bg: #f6f5f3; --panel: #ffffff; --ink: #16150f; --muted: #6d6a61;
  --line: #e2ded6; --accent: #b4441f; --chip: #efece6;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #131211; --panel: #1c1b19; --ink: #ece9e2; --muted: #97928a;
    --line: #2c2a27; --accent: #e0713f; --chip: #262421;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 15px/1.5 ui-sans-serif, -apple-system, "Segoe UI", Roboto, sans-serif;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 1180px; margin: 0 auto; padding: 40px 20px 80px; }
header { border-bottom: 1px solid var(--line); padding-bottom: 22px; margin-bottom: 26px; }
h1 { margin: 0 0 6px; font-size: 30px; letter-spacing: -0.02em; }
.sub { color: var(--muted); font-size: 14px; }
.controls { display: flex; flex-wrap: wrap; gap: 10px; margin: 20px 0 8px; }
input[type=search], select {
  font: inherit; padding: 9px 12px; border: 1px solid var(--line);
  border-radius: 8px; background: var(--panel); color: var(--ink);
}
input[type=search] { flex: 1 1 260px; min-width: 0; }
.count { color: var(--muted); font-size: 13px; margin-bottom: 26px; }
section { margin-bottom: 40px; }
h2 {
  font-size: 13px; text-transform: uppercase; letter-spacing: 0.09em;
  color: var(--muted); font-weight: 600; margin: 0 0 14px;
  padding-bottom: 8px; border-bottom: 1px solid var(--line);
}
h2 span { color: var(--accent); }
.grid {
  display: grid; gap: 18px;
  grid-template-columns: repeat(auto-fill, minmax(158px, 1fr));
}
.card { display: flex; flex-direction: column; min-width: 0; }
.poster {
  position: relative; aspect-ratio: 2/3; border-radius: 8px; overflow: hidden;
  background: var(--chip); border: 1px solid var(--line); margin-bottom: 9px;
}
.poster img { width: 100%; height: 100%; object-fit: cover; display: block; }
.poster .none {
  display: flex; align-items: center; justify-content: center; height: 100%;
  padding: 12px; text-align: center; color: var(--muted); font-size: 12px;
}
.badge {
  position: absolute; top: 7px; right: 7px; background: rgba(0,0,0,.72);
  color: #fff; font-size: 10px; font-weight: 700; letter-spacing: .05em;
  padding: 3px 6px; border-radius: 4px;
}
.name { font-weight: 600; font-size: 14px; line-height: 1.3; }
.name a { color: inherit; text-decoration: none; }
.name a:hover { color: var(--accent); text-decoration: underline; }
.meta { color: var(--muted); font-size: 12.5px; margin-top: 3px; }
.scores { display: flex; gap: 9px; margin-top: 5px; font-size: 12px; flex-wrap: wrap; }
.scores b { font-weight: 600; }
.fresh { color: #2e7d32; } .rotten { color: #c33; }
@media (prefers-color-scheme: dark) { .fresh { color: #7bc47f; } }
.empty { color: var(--muted); padding: 30px 0; }
footer {
  margin-top: 50px; padding-top: 20px; border-top: 1px solid var(--line);
  color: var(--muted); font-size: 12.5px;
}
footer a { color: var(--accent); }
</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>$page_title</h1>
  <div class="sub">$summary</div>
  <div class="controls">
    <input type="search" id="q" placeholder="Search title, director, genre, year…" autocomplete="off">
    <select id="sort">
      <option value="shelf">Shelf order</option>
      <option value="title">Title A–Z</option>
      <option value="year">Year, newest</option>
      <option value="yearold">Year, oldest</option>
      <option value="rt">Tomatometer</option>
      <option value="imdb">IMDb rating</option>
      <option value="runtime">Runtime</option>
    </select>
    <select id="filter">$filter_options</select>
  </div>
  <div class="count" id="count"></div>
</header>
<main id="shelf"></main>
<footer>
  Built $built from <code>collection.txt</code> by
  <a href="https://github.com/txrunn/scripts/tree/main/movie-collection">build_collection.py</a>.
  Metadata from <a href="https://www.themoviedb.org/">TMDB</a> and
  <a href="https://www.omdbapi.com/">OMDb</a>; posters courtesy of TMDB.
  A blank score means no source would supply it — see <code>Source_Notes</code>
  in <a href="collection.csv">collection.csv</a>.
</footer>
</div>
<script>
const DATA = $data;
const shelf = document.getElementById('shelf');
const q = document.getElementById('q');
const sortBy = document.getElementById('sort');
const filter = document.getElementById('filter');
const count = document.getElementById('count');

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]
  ));
}

function card(f) {
  const poster = f.poster
    ? '<img loading="lazy" src="' + esc(f.poster) + '" alt="">'
    : '<div class="none">' + esc(f.title) + '</div>';
  const badge = f.uhd ? '<div class="badge">4K</div>' : '';
  const bits = [];
  if (f.year) bits.push(f.year);
  if (f.runtime) bits.push(f.runtime + ' min');
  const scores = [];
  if (f.rt_critic != null) {
    scores.push('<span class="' + (f.rt_critic >= 60 ? 'fresh' : 'rotten') +
      '">RT <b>' + f.rt_critic + '%</b></span>');
  }
  if (f.rt_audience != null) scores.push('<span>Aud <b>' + f.rt_audience + '%</b></span>');
  if (f.imdb != null) scores.push('<span>IMDb <b>' + f.imdb + '</b></span>');
  const name = f.letterboxd
    ? '<a href="' + esc(f.letterboxd) + '" target="_blank" rel="noopener">' + esc(f.title) + '</a>'
    : esc(f.title);
  return '<div class="card">' +
    '<div class="poster">' + poster + badge + '</div>' +
    '<div class="name">' + name + '</div>' +
    '<div class="meta">' + esc(bits.join(' · ')) + '</div>' +
    (f.directors ? '<div class="meta">' + esc(f.directors) + '</div>' : '') +
    '<div class="scores">' + scores.join('') + '</div>' +
    '</div>';
}

function render() {
  const term = q.value.trim().toLowerCase();
  const want = filter.value;
  let films = DATA.filter(f => {
    if (want !== 'all' && f.shelf !== want) return false;
    if (!term) return true;
    return f.haystack.indexOf(term) !== -1;
  });

  const key = sortBy.value;
  const cmp = {
    shelf: (a, b) => a.order - b.order,
    title: (a, b) => a.sortkey.localeCompare(b.sortkey),
    year: (a, b) => (b.year || 0) - (a.year || 0),
    yearold: (a, b) => (a.year || 9999) - (b.year || 9999),
    rt: (a, b) => (b.rt_critic == null ? -1 : b.rt_critic) - (a.rt_critic == null ? -1 : a.rt_critic),
    imdb: (a, b) => (b.imdb == null ? -1 : b.imdb) - (a.imdb == null ? -1 : a.imdb),
    runtime: (a, b) => (b.runtime || 0) - (a.runtime || 0),
  }[key];
  films = films.slice().sort(cmp);

  count.textContent = films.length + (films.length === 1 ? ' title' : ' titles');

  if (!films.length) {
    shelf.innerHTML = '<p class="empty">Nothing matches that.</p>';
    return;
  }

  // Headings only make sense in shelf order; any other sort cuts across them.
  let html = '';
  if (key === 'shelf') {
    let current = null;
    let open = false;
    for (const f of films) {
      if (f.shelf !== current) {
        if (open) html += '</div></section>';
        current = f.shelf;
        const n = films.filter(x => x.shelf === current).length;
        html += '<section><h2>' + esc(current) + ' <span>' + n + '</span></h2><div class="grid">';
        open = true;
      }
      html += card(f);
    }
    if (open) html += '</div></section>';
  } else {
    html = '<section><div class="grid">' + films.map(card).join('') + '</div></section>';
  }
  shelf.innerHTML = html;
}

q.addEventListener('input', render);
sortBy.addEventListener('change', render);
filter.addEventListener('change', render);
render();
</script>
</body>
</html>
""")


def poster_url(record, embedded):
    if embedded and record["key"] in embedded:
        return embedded[record["key"]]
    if record["poster"]:
        return TMDB_IMAGE + record["poster"]
    return None


def render_html(shelf, blocks, title, embedded=None):
    data = []
    for record in shelf:
        directors = ", ".join(record["directors"])
        haystack = " ".join(filter(None, [
            record["title"], str(record["year"] or ""), directors,
            " ".join(record["genres"]), record["shelf"],
        ])).lower()
        data.append({
            "title": record["title"],
            "year": record["year"],
            "runtime": record["runtime"],
            "directors": directors,
            "genres": record["genres"],
            "rt_critic": record["rt_critic"],
            "rt_audience": record.get("rt_audience"),
            "imdb": record["imdb_rating"],
            "poster": poster_url(record, embedded),
            "letterboxd": record["letterboxd"],
            "shelf": record["shelf"],
            "order": record["order"],
            "uhd": bool(record.get("uhd", True)),
            "sortkey": sort_title(record["title"]),
            "haystack": haystack,
        })

    shelves = []
    for record in shelf:
        if record["shelf"] not in shelves:
            shelves.append(record["shelf"])
    options = ['<option value="all">Every shelf</option>']
    options += [
        f'<option value="{html.escape(name, quote=True)}">{html.escape(name)}</option>'
        for name in shelves
    ]

    films = [r for r in shelf if r["section"] == "films"]
    summary = "{} films · {} box sets · {} director blocks".format(
        len(films),
        len([r for r in shelf if r["section"] == "collections"]),
        len(blocks),
    )

    return PAGE.substitute(
        page_title=html.escape(title),
        summary=html.escape(summary),
        filter_options="".join(options),
        # </script> inside a JSON string would close the block early; the rest
        # is ordinary JSON and safe between script tags.
        data=json.dumps(data, ensure_ascii=False).replace("</", "<\\/"),
        built=dt.date.today().isoformat(),
    )


def embed_posters(shelf):
    """Download every poster and inline it as a data: URI.

    Off by default. It makes the page a single portable file with no third-party
    requests, at the cost of a few megabytes and one request per film on the
    first build. Failures are skipped, not fatal -- a missing poster is a
    cosmetic problem, not a broken build.
    """
    embedded = {}
    for record in shelf:
        if not record["poster"]:
            continue
        try:
            raw = fetch(TMDB_IMAGE + record["poster"], expect_json=False)
        except (FetchError, SchemaError) as exc:
            print(f"  poster failed for {record['title']}: {exc}", file=sys.stderr)
            continue
        encoded = base64.b64encode(raw).decode("ascii")
        embedded[record["key"]] = "data:image/jpeg;base64," + encoded
    return embedded


# --- State -------------------------------------------------------------------


def load_json(path, default):
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError) as exc:
        print(f"{path} is unreadable ({exc}); starting fresh", file=sys.stderr)
        return default


def save_json(path, payload):
    path = os.path.expanduser(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    # Write-then-rename, so an interrupted build cannot leave a half-written
    # cache that the next run would treat as authoritative.
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


def fingerprint(entries, overrides):
    """What the outputs depend on, hashed.

    Covers the inventory, the overrides and the template version -- so adding a
    disc, correcting a score or editing the page design all force a rebuild,
    while a run that changes none of them does nothing at all.
    """
    digest = hashlib.sha256()
    digest.update(f"template:{TEMPLATE_VERSION}\n".encode())
    for entry in entries:
        digest.update(f"{entry['section']}\x1f{entry['key']}\n".encode())
    digest.update(json.dumps(overrides, sort_keys=True).encode())
    return digest.hexdigest()


# --- Report ------------------------------------------------------------------


def format_report(added, removed, shelf, blocks, style):
    """What changed, for a human. Empty when nothing did."""
    if not added and not removed:
        return ""

    lookup = {r["key"]: r for r in shelf}
    lines = []
    heading = "{} added to the collection".format(len(added)) if added else "Collection updated"

    if style == "markdown":
        lines.append(f"## {heading}")
        lines.append("")
        if added:
            lines.append("| Title | Year | Director | Runtime | RT | IMDb |")
            lines.append("|---|---|---|---|---|---|")
            for key in added:
                record = lookup.get(key)
                if not record:
                    continue
                name = record["title"].replace("|", "\\|")
                link = f"[{name}]({record['letterboxd']})" if record["letterboxd"] else name
                lines.append("| {} | {} | {} | {} | {} | {} |".format(
                    link,
                    record["year"] or "—",
                    ", ".join(record["directors"]).replace("|", "\\|") or "—",
                    f"{record['runtime']} min" if record["runtime"] else "—",
                    f"{record['rt_critic']}%" if record["rt_critic"] is not None else "—",
                    record["imdb_rating"] if record["imdb_rating"] is not None else "—",
                ))
            lines.append("")
        if removed:
            lines.append("**Removed:** " + ", ".join(sorted(removed)))
            lines.append("")
        lines.append("Director blocks: " + (", ".join(
            f"{name} ({len(films)})" for name, films in sorted(blocks.items())
        ) or "none"))
    else:
        lines.append(heading)
        lines.append("")
        for key in added:
            record = lookup.get(key)
            if not record:
                continue
            lines.append("  {} ({})".format(record["title"], record["year"] or "year unknown"))
            detail = ", ".join(record["directors"]) or "director unknown"
            if record["runtime"]:
                detail += f" · {record['runtime']} min"
            if record["rt_critic"] is not None:
                detail += f" · RT {record['rt_critic']}%"
            lines.append("    " + detail)
            if record["letterboxd"]:
                lines.append("    " + record["letterboxd"])
        if removed:
            lines.append("")
            lines.append("  removed: " + ", ".join(sorted(removed)))

    return "\n".join(lines) + "\n"


# --- Modes -------------------------------------------------------------------


def mode_verify(args, keys):
    """Prove the inventory parses and both APIs still answer the way we parse.

    Modelled on the alamo tracker's --verify: one PASS/FAIL line per thing the
    build depends on, so a provider changing shape is caught here rather than
    showing up as a page that quietly lost half its ratings.
    """
    results = []

    def check(label, ok, detail=""):
        results.append((label, "PASS" if ok else "FAIL", detail))
        return ok

    try:
        entries = parse_inventory(args.collection)
        check("inventory parses", True, f"{len(entries)} entries")
    except InventoryError as exc:
        check("inventory parses", False, str(exc))
        entries = []

    try:
        overrides = load_overrides(args.overrides)
        check("overrides parse", True, f"{len(overrides)} entries")
        unknown = sorted(set(overrides) - {e["key"] for e in entries})
        results.append((
            "overrides match inventory",
            "PASS" if not unknown else "WARN",
            "all matched" if not unknown else "no such entry: " + ", ".join(unknown),
        ))
    except InventoryError as exc:
        check("overrides parse", False, str(exc))

    if not check("TMDB_API_KEY set", bool(keys["tmdb"])):
        results.append(("TMDB reachable", "SKIP", "no key"))
    else:
        try:
            found = tmdb_search("Blade Runner", 1982, keys["tmdb"])
            check("TMDB search returns results", bool(found),
                  (found or {}).get("title", ""))
            if found:
                detail = tmdb_movie(found["id"], keys["tmdb"])
                check("TMDB movie has runtime", bool(detail.get("runtime")),
                      f"{detail.get('runtime')} min")
                check("TMDB movie has genres", bool(detail.get("genres")),
                      ", ".join(g["name"] for g in detail.get("genres", [])))
                check("TMDB movie has imdb_id", bool(detail.get("imdb_id")),
                      detail.get("imdb_id", ""))
                check("TMDB credits carry a Director", bool(directors_of(detail)),
                      ", ".join(directors_of(detail)))
                check("TMDB movie has poster_path", bool(detail.get("poster_path")))
                url = resolve_letterboxd(detail["id"], detail.get("imdb_id"))
                check("Letterboxd id redirect resolves",
                      bool(url and "/film/" in url), url or "")
        except (FetchError, SchemaError) as exc:
            check("TMDB reachable", False, str(exc))

    if not keys["omdb"]:
        results.append(("OMDB_API_KEY set", "WARN",
                        "unset: no IMDb ratings, no Tomatometer"))
    else:
        try:
            omdb = omdb_lookup("tt0083658", keys["omdb"])
            check("OMDb responds", omdb.get("Response") != "False", omdb.get("Title", ""))
            check("OMDb has imdbRating", _number(omdb.get("imdbRating")) is not None,
                  str(omdb.get("imdbRating")))
            critic = rt_critic_from(omdb)
            check("OMDb Ratings carry Rotten Tomatoes", critic is not None,
                  f"{critic}%" if critic is not None else "absent")
            results.append((
                "OMDb carries an RT audience score", "INFO",
                "no -- expected; audience scores come from overrides.toml",
            ))
        except (FetchError, SchemaError) as exc:
            check("OMDb responds", False, str(exc))

    cache = load_json(args.cache, {})
    results.append((
        "metadata cache", "INFO",
        "{} cached, {} to fetch".format(
            len(cache.get("records", {})),
            len([e for e in entries if e["key"] not in cache.get("records", {})]),
        ),
    ))

    for label, status, detail in results:
        print(f"{label:<38} {status:<5} {detail}")
    return 0 if not any(status == "FAIL" for _, status, _ in results) else 1


# --- Entry point -------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        description="Build a web page and CSV from a disc collection inventory.",
    )
    parser.add_argument("--collection", default=DEFAULT_COLLECTION,
                        help="inventory file (default: collection.txt)")
    parser.add_argument("--overrides", default=DEFAULT_OVERRIDES,
                        help="manual metadata (default: overrides.toml)")
    parser.add_argument("--cache", default=DEFAULT_CACHE,
                        help="metadata cache (default: cache/metadata.json)")
    parser.add_argument("--ledger", default=DEFAULT_LEDGER,
                        help="build ledger (default: cache/build.json)")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR,
                        help="where index.html and collection.csv go")
    parser.add_argument("--title", default="4K Collection",
                        help="page title")
    parser.add_argument("--force", action="store_true",
                        help="rebuild the outputs even if nothing changed")
    parser.add_argument("--offline", action="store_true",
                        help="never fetch; fail if any title is uncached")
    parser.add_argument("--refresh", action="append", default=[], metavar="TITLE",
                        help="drop TITLE from the cache and look it up again")
    parser.add_argument("--refresh-all", action="store_true",
                        help="ignore the cache entirely and re-fetch everything")
    parser.add_argument("--embed-posters", action="store_true",
                        help="inline posters as data URIs for a portable page")
    parser.add_argument("--format", choices=("text", "markdown"), default="text",
                        help="style of the what-changed report")
    parser.add_argument("--verify", action="store_true",
                        help="check the inventory and both API contracts, then exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="do everything except write files")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    keys = {
        "tmdb": os.environ.get("TMDB_API_KEY", "").strip(),
        "omdb": os.environ.get("OMDB_API_KEY", "").strip(),
    }

    try:
        if args.verify:
            return mode_verify(args, keys)

        entries = parse_inventory(args.collection)
        overrides = load_overrides(args.overrides)
    except (InventoryError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    cache = load_json(args.cache, {})
    records_cache = {} if args.refresh_all else dict(cache.get("records", {}))
    for key in args.refresh:
        records_cache.pop(key, None)

    ledger = load_json(args.ledger, {})
    known = set(ledger.get("titles", []))
    current = {e["key"] for e in entries}
    added = [e["key"] for e in entries if e["key"] not in known]
    removed = known - current

    stale = [e for e in entries if e["key"] not in records_cache]

    if stale and args.offline:
        print("error: --offline but these are not cached:", file=sys.stderr)
        for entry in stale:
            print(f"  {entry['key']}", file=sys.stderr)
        return 1

    if stale and not keys["tmdb"]:
        print(
            "error: TMDB_API_KEY is not set, and {} title(s) need looking up.\n"
            "       Get a free key at https://www.themoviedb.org/settings/api\n"
            "       then: export TMDB_API_KEY=...".format(len(stale)),
            file=sys.stderr,
        )
        return 1

    notes = []
    if stale:
        print(f"looking up {len(stale)} new title(s)…", file=sys.stderr)
    for entry in stale:
        print(f"  {entry['key']}", file=sys.stderr)
        try:
            records_cache[entry["key"]] = resolve(entry, keys, notes)
        except (FetchError, SchemaError) as exc:
            print(f"error: {entry['key']}: {exc}", file=sys.stderr)
            return 1

    # Rebuild the working records from the cache every time, so an overrides.toml
    # edit takes effect without re-fetching anything.
    records = []
    for entry in entries:
        record = dict(records_cache[entry["key"]])
        record["section"] = entry["section"]
        record["key"] = entry["key"]
        records.append(apply_overrides(record, overrides))

    shelf, blocks = shelve(records)

    stamp = fingerprint(entries, overrides)
    unchanged = (
        stamp == ledger.get("fingerprint")
        and not stale
        and os.path.exists(os.path.join(args.out_dir, "index.html"))
    )
    if unchanged and not args.force:
        # Silence means "nothing was added", which is what makes a daily run
        # safe: a quiet build is indistinguishable from not running at all.
        return 0

    embedded = embed_posters(shelf) if args.embed_posters and not args.dry_run else None
    page = render_html(shelf, blocks, args.title, embedded)

    if not args.dry_run:
        os.makedirs(args.out_dir, exist_ok=True)
        with open(os.path.join(args.out_dir, "index.html"), "w", encoding="utf-8") as handle:
            handle.write(page)
        write_csv(os.path.join(args.out_dir, "collection.csv"), shelf)
        save_json(args.cache, {"version": 1, "records": records_cache})
        save_json(args.ledger, {
            "fingerprint": stamp,
            "built": dt.date.today().isoformat(),
            "titles": sorted(current),
        })

    print(format_report(added, removed, shelf, blocks, args.format), end="")

    for note in notes:
        print(f"note: {note}", file=sys.stderr)

    missing = [r["title"] for r in shelf if r["section"] == "films" and not r["tmdb_id"]]
    if missing:
        print("unresolved titles: " + ", ".join(missing), file=sys.stderr)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
