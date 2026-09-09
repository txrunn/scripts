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
#   5: favicon
TEMPLATE_VERSION = 5


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

    An **indented** line under a box set is one of the discs inside it. Those
    are looked up like any other film and their scores roll up onto the set, so
    a five-disc box stops being an empty row. They are shelved with their box
    rather than alphabetically, because the box is the thing on the shelf.
    """
    entries = []
    seen = {}
    section = "films"
    parent = None

    with open(os.path.expanduser(path), encoding="utf-8") as handle:
        for number, raw in enumerate(handle, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue

            header = SECTION_HEADER.match(line)
            if header:
                section = header.group(1)
                parent = None
                if section not in SECTIONS:
                    raise InventoryError(
                        f"{path}:{number}: unknown section [{section}]; "
                        f"expected one of {', '.join(SECTIONS)}"
                    )
                continue

            indented = raw[0] in " \t"
            if indented and parent is None:
                raise InventoryError(
                    f"{path}:{number}: {line!r} is indented but no box set "
                    f"precedes it; indent only the discs inside a set"
                )
            if indented and section == "films":
                raise InventoryError(
                    f"{path}:{number}: {line!r} is indented inside [films]; "
                    f"nesting only means something under a box set"
                )

            if line in seen:
                raise InventoryError(
                    f"{path}:{number}: duplicate entry {line!r} "
                    f"(first seen on line {seen[line]})"
                )
            seen[line] = number

            match = YEAR_HINT.match(line)
            entry = {
                "key": line,
                "query": match.group(1) if match else line,
                "year_hint": int(match.group(2)) if match else None,
                "section": section,
                "role": "member" if indented else "item",
                "parent": parent if indented else None,
                "line": number,
            }
            entries.append(entry)
            if not indented and section != "films":
                parent = line

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


def resolve(entry, keys, notes, overrides=None):
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

    record["role"] = entry.get("role", "item")
    record["parent"] = entry.get("parent")

    if entry["section"] != "films" and record["role"] != "member":
        # The box itself is not a TMDB movie. Fabricating a match for "Bourne:
        # The Ultimate Collection" would put one film's runtime and director on
        # a five-disc set, so the set carries only what its discs roll up plus
        # whatever overrides.toml says.
        record["source_notes"].append(
            "box set: metadata is aggregated from the discs listed under it"
        )
        record["verified"] = dt.date.today().isoformat()
        return record

    # A pinned id skips the search entirely. Without this the pin would only
    # repaint the title of whatever the search picked, leaving the director,
    # runtime, scores and Letterboxd link belonging to the wrong film -- which
    # is worse than an obvious mismatch, because it looks right.
    pinned = (overrides or {}).get(entry["key"], {}).get("tmdb_id")
    if pinned:
        detail = tmdb_movie(pinned, keys["tmdb"])
        record["source_notes"].append(f"TMDB id pinned to {pinned} in overrides.toml")
    else:
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


def aggregate_box_sets(records):
    """Roll each box set's discs up onto the box, and return the shelf items.

    A five-disc set becomes one row carrying a film count, a total runtime, a
    year span and the mean of whatever scores its discs actually have. Means are
    taken over the discs that have a score rather than over all of them, so one
    film OMDb has no Tomatometer for lowers the sample, not the average.

    The discs deliberately do NOT count toward director blocks. Owning the
    Hitchcock box would otherwise manufacture a Hitchcock block and scatter the
    box across the alphabetical shelf -- but the box is one object, and it sits
    in one place.
    """
    by_key = {r["key"]: r for r in records}
    items = []

    for record in records:
        parent = record.get("parent")
        if parent and parent in by_key:
            by_key[parent].setdefault("members", []).append(record)
        else:
            items.append(record)

    for record in items:
        members = record.get("members")
        if not members:
            continue
        members.sort(key=lambda m: (m["year"] or 9999, sort_title(m["title"])))

        years = [m["year"] for m in members if m["year"]]
        runtimes = [m["runtime"] for m in members if m["runtime"]]
        critics = [m["rt_critic"] for m in members if m["rt_critic"] is not None]
        imdbs = [m["imdb_rating"] for m in members if m["imdb_rating"] is not None]

        record["film_count"] = len(members)
        record["agg_runtime"] = sum(runtimes) if runtimes else None
        record["agg_years"] = (min(years), max(years)) if years else None
        record["agg_rt"] = round(sum(critics) / len(critics)) if critics else None
        record["agg_imdb"] = round(sum(imdbs) / len(imdbs), 1) if imdbs else None
        if record["year"] is None and years:
            record["year"] = min(years)

        unscored = len(members) - len(critics)
        note = "aggregate of {} disc(s)".format(len(members))
        if unscored:
            note += "; Tomatometer averaged over {} of them".format(len(critics))
        # Idempotent: shelve() can run more than once over the same records, and
        # a note that appends every time turns into a column of repeated
        # sentences in the CSV.
        if note not in record["source_notes"]:
            record["source_notes"].append(note)

    return items


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
    on their own shelf at the end, each immediately followed by its own discs,
    rather than being scattered through the As.
    """
    records = aggregate_box_sets(records)
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
            # The discs follow their box immediately. Sorting them into the
            # alphabetical run would break up the object you actually own.
            for member in record.get("members", []):
                member["block"] = ""
                member["shelf"] = CATEGORY[section] + " shelf"
                member["order"] = order
                order += 1
                shelf.append(member)

    return shelf, blocks


# --- Output: CSV -------------------------------------------------------------


def write_csv(path, shelf):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for record in shelf:
            hdr = record.get("hdr", "")
            member = record.get("role") == "member"
            # A box set has no runtime or score of its own -- it reports what
            # its discs rolled up. A disc is a Film that names its box.
            runtime = record["runtime"] if not record.get("members") else record.get("agg_runtime")
            critic = record["rt_critic"] if not record.get("members") else record.get("agg_rt")
            imdb = record["imdb_rating"] if not record.get("members") else record.get("agg_imdb")
            writer.writerow({
                "Title": record["title"],
                "Year": record["year"] or "",
                "Director": record["directors"][0] if record["directors"] else "",
                "Directors": "; ".join(record["directors"]),
                "Runtime_Minutes": runtime or "",
                "Genre": "; ".join(record["genres"]),
                "IMDb_Rating": imdb if imdb is not None else "",
                "RT_Critic_Percent": critic if critic is not None else "",
                "RT_Audience_Percent": record["rt_audience"] if record.get("rt_audience") is not None else "",
                "Theatrical_Score": record.get("theatrical_score", ""),
                "Category": "Film" if member else CATEGORY[record["section"]],
                "Director_Block": record.get("block", ""),
                "Franchise": record.get("franchise", ""),
                "Collection": record.get("parent") or (
                    record["title"] if record["section"] == "collections" else ""
                ),
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
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAzMiAzMiI+IDxjaXJjbGUgY3g9IjE2IiBjeT0iMTYiIHI9IjE1IiBmaWxsPSIjYjQ0NDFmIi8+IDxjaXJjbGUgY3g9IjE2IiBjeT0iMTYiIHI9IjgiIGZpbGw9Im5vbmUiIHN0cm9rZT0iI2Y2ZjVmMyIgc3Ryb2tlLXdpZHRoPSIxLjUiIG9wYWNpdHk9Ii41NSIvPiA8Y2lyY2xlIGN4PSIxNiIgY3k9IjE2IiByPSI0IiBmaWxsPSIjZjZmNWYzIi8+PC9zdmc+">
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
.toggle {
  display: inline-flex; align-items: center; gap: 7px; padding: 9px 12px;
  border: 1px solid var(--line); border-radius: 8px; background: var(--panel);
  font-size: 13.5px; color: var(--muted); cursor: pointer; user-select: none;
  white-space: nowrap;
}
.toggle input { margin: 0; cursor: pointer; accent-color: var(--accent); }
.count { color: var(--muted); font-size: 13px; margin-bottom: 26px; }
h2.click { cursor: pointer; display: flex; align-items: center; gap: 8px; }
h2.click:hover { color: var(--ink); }
h2 .chev {
  display: inline-block; width: 9px; transition: transform .15s ease;
  font-size: 10px; color: var(--accent);
}
h2.shut .chev { transform: rotate(-90deg); }
h2 .grow { flex: 1; }
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
.name {
  font-weight: 600; font-size: 14px; line-height: 1.3;
  /* Never truncated -- the title is the whole point of the card. Reserving two
     lines evens out the rows without hiding anything: on this shelf 90 of 102
     titles are one line and 12 are two, so the reserve costs nothing and stops
     cards jumping. The four Elm Street discs that need a third line simply get
     one. */
  min-height: 2.6em;
}
.name a { color: inherit; text-decoration: none; }
.name a:hover { color: var(--accent); text-decoration: underline; }
.meta { color: var(--muted); font-size: 12.5px; margin-top: 3px; }
.meta.gen { font-size: 11.5px; opacity: .78; }
.scores { display: flex; gap: 9px; margin-top: 5px; font-size: 12px; flex-wrap: wrap; }
.scores span { display: inline-flex; align-items: center; gap: 3px; }
.scores b { font-weight: 600; }
.ico { width: 13px; height: 13px; flex: none; }
.ico.imdb { width: 24px; height: 12px; border-radius: 2px; }
.fresh { color: #2e7d32; } .rotten { color: #b5501f; }
@media (prefers-color-scheme: dark) {
  .fresh { color: #7bc47f; }
  .rotten { color: #e08a5a; }
}
.boxset {
  border: 1px solid var(--line); border-radius: 10px; padding: 16px 16px 18px;
  background: var(--panel); margin-bottom: 18px;
}
.boxset-head {
  display: flex; flex-wrap: wrap; align-items: baseline; gap: 10px;
  margin-bottom: 14px;
}
.boxset-head .bt { font-weight: 600; font-size: 15px; }
.boxset-head .bm { color: var(--muted); font-size: 12.5px; }
.boxset .grid { grid-template-columns: repeat(auto-fill, minmax(118px, 1fr)); gap: 13px; }
.boxset .name { font-size: 12.5px; }
.boxset .meta, .boxset .scores { font-size: 11.5px; }
.empty { color: var(--muted); padding: 30px 0; }
footer {
  margin-top: 50px; padding-top: 20px; border-top: 1px solid var(--line);
  color: var(--muted); font-size: 12.5px;
}
footer a { color: var(--accent); }
</style>
</head>
<body>
<svg style="display:none" aria-hidden="true">
  <symbol id="i-imdb" viewBox="0 0 64 32">
    <rect width="64" height="32" rx="5" fill="#f5c518"/>
    <text x="32" y="24" text-anchor="middle" fill="#000"
          font-family="Helvetica,Arial,sans-serif" font-size="19" font-weight="700"
          letter-spacing="-1">IMDb</text>
  </symbol>
  <symbol id="i-fresh" viewBox="0 0 24 24">
    <circle cx="12" cy="14.6" r="8.6" fill="#fa320a"/>
    <path d="M11.9 8.2C11 6 8.9 4.8 6.6 5.1c1.1.8 1.8 1.9 2 3-1.5-.7-3.2-.5-4.5.4 1.5.2 2.8 1 3.7 2.1 1-1.2 2.5-2.1 4.1-2.4z" fill="#0d8a3e"/>
    <path d="M12.1 8.2C13 6 15.1 4.8 17.4 5.1c-1.1.8-1.8 1.9-2 3 1.5-.7 3.2-.5 4.5.4-1.5.2-2.8 1-3.7 2.1-1-1.2-2.5-2.1-4.1-2.4z" fill="#0d8a3e"/>
    <rect x="11.3" y="3.4" width="1.5" height="4.6" rx=".75" fill="#0d8a3e"/>
  </symbol>
  <symbol id="i-rotten" viewBox="0 0 24 24">
    <path d="M12 3.6c1 1.4 2.6 1.2 3.4 2.6.6 1.1.1 2.1.9 2.8.9.8 2.4.3 3 1.5.5 1.1-.5 2-.2 3.1.3 1.2 1.7 1.7 1.5 3-.2 1.2-1.7 1.3-2.4 2.3-.7 1-.3 2.4-1.5 2.9-1.1.5-2-.6-3.1-.4-1.2.2-1.8 1.6-3 1.5-1.2-.1-1.5-1.6-2.5-2.3-1-.7-2.5-.4-3-1.6-.5-1.1.7-2.1.5-3.2-.2-1.2-1.6-1.8-1.3-3 .3-1.2 1.8-1.2 2.6-2.1.8-.9.4-2.4 1.5-2.9 1.1-.5 2 .7 3.2.5 1.1-.2 1.7-1.4 2.4-2.7z" fill="#5a8f29"/>
    <circle cx="9.6" cy="12.4" r="1.3" fill="#3f6b1a"/>
    <circle cx="14.2" cy="15.4" r="1.6" fill="#3f6b1a"/>
  </symbol>
  <symbol id="i-aud" viewBox="0 0 24 24">
    <circle cx="8.4" cy="7.4" r="3.1" fill="#f2c94c"/>
    <circle cx="14.4" cy="6.4" r="2.7" fill="#f7dd7e"/>
    <circle cx="12" cy="9.6" r="2.9" fill="#f2c94c"/>
    <path d="M4.6 11h14.8l-1.7 10.4H6.3z" fill="#fa320a"/>
    <path d="M9.3 11h1.9l-.7 10.4H8.8zm4.4 0h1.9l-1.1 10.4h-1.6z" fill="#fff" opacity=".9"/>
  </symbol>
</svg>
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
    <select id="filter"></select>
    <select id="genre"></select>
    <label class="toggle">
      <input type="checkbox" id="blocks"> Shelf organisation
    </label>
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
const blocksOn = document.getElementById('blocks');
const genre = document.getElementById('genre');

const BLOCK = 'Director block: ';
const STORE = 'disc-inventory';

// Shelf organisation is off by default: the director blocks are a curation
// layer, and most of the time you just want one A-Z run. Both the toggle and
// which blocks you collapsed are remembered, since this is a page you come
// back to rather than land on once.
let prefs = {blocks: false, shut: []};
try { Object.assign(prefs, JSON.parse(localStorage.getItem(STORE) || '{}')); } catch (e) {}
blocksOn.checked = !!prefs.blocks;
let shut = new Set(prefs.shut || []);

function save() {
  prefs.blocks = blocksOn.checked;
  prefs.shut = [...shut];
  try { localStorage.setItem(STORE, JSON.stringify(prefs)); } catch (e) {}
}

// With blocks off, a director-block film rejoins the alphabetical run. Box sets
// keep their own shelf either way -- that is a physical division, not curation.
function shelfOf(f) {
  if (!blocksOn.checked && f.shelf.startsWith(BLOCK)) return 'Alphabetical';
  return f.shelf;
}

function shelfRank(name) {
  if (name.startsWith(BLOCK)) return [0, name];
  if (name === 'Alphabetical') return [1, ''];
  if (name === 'Collection shelf') return [2, ''];
  return [3, name];
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]
  ));
}

function ico(name, cls) {
  return '<svg class="ico' + (cls ? ' ' + cls : '') + '" aria-hidden="true">' +
    '<use href="#i-' + name + '"/></svg>';
}

function card(f) {
  const poster = f.poster
    ? '<img loading="lazy" src="' + esc(f.poster) + '" alt="">'
    : '<div class="none">' + esc(f.title) + '</div>';
  // Everything on this shelf is 4K, so a "4K" badge on all 133 films marked
  // nothing. Flag the exception instead.
  const badge = f.box
    ? '<div class="badge">' + f.films + ' FILMS</div>'
    : (f.uhd ? '' : '<div class="badge">BLU-RAY</div>');
  const bits = [];
  if (f.span && f.span[0] !== f.span[1]) bits.push(f.span[0] + '–' + f.span[1]);
  else if (f.year) bits.push(f.year);
  if (f.runtime) bits.push(f.box ? Math.round(f.runtime / 60) + 'h total' : f.runtime + ' min');
  const scores = [];
  if (f.rt_critic != null) {
    // Fresh gets the tomato, rotten gets the green splat -- the same signal
    // Rotten Tomatoes itself uses, so the icon carries the meaning and the
    // colour is not doing the job alone.
    const fresh = f.rt_critic >= 60;
    scores.push('<span class="' + (fresh ? 'fresh' : 'rotten') + '" title="' +
      'Tomatometer ' + f.rt_critic + '% (' + (fresh ? 'Fresh' : 'Rotten') + ')">' +
      ico(fresh ? 'fresh' : 'rotten') + '<b>' + f.rt_critic + '%</b></span>');
  }
  if (f.rt_audience != null) {
    scores.push('<span title="Rotten Tomatoes audience score">' +
      ico('aud') + '<b>' + f.rt_audience + '%</b></span>');
  }
  if (f.imdb != null) {
    scores.push('<span title="IMDb rating">' +
      ico('imdb', 'imdb') + '<b>' + f.imdb + '</b></span>');
  }
  const name = f.letterboxd
    ? '<a href="' + esc(f.letterboxd) + '" target="_blank" rel="noopener">' + esc(f.title) + '</a>'
    : esc(f.title);
  const genre = f.genres && f.genres.length ? f.genres.slice(0, 2).join(', ') : '';
  return '<div class="card">' +
    '<div class="poster">' + poster + badge + '</div>' +
    '<div class="name" title="' + esc(f.title) + '">' + name + '</div>' +
    '<div class="meta">' + esc(bits.join(' · ')) + '</div>' +
    (f.directors ? '<div class="meta">' + esc(f.directors) + '</div>' : '') +
    (genre ? '<div class="meta gen">' + esc(genre) + '</div>' : '') +
    '<div class="scores">' + scores.join('') + '</div>' +
    '</div>';
}

function fillFilter() {
  const names = [];
  for (const f of DATA) {
    const name = shelfOf(f);
    if (!f.member && !names.includes(name)) names.push(name);
  }
  names.sort((a, b) => {
    const ra = shelfRank(a), rb = shelfRank(b);
    return ra[0] - rb[0] || ra[1].localeCompare(rb[1]);
  });
  const keep = filter.value;
  filter.innerHTML = '<option value="all">Every shelf</option>' +
    names.map(n => '<option value="' + esc(n) + '">' + esc(n) + '</option>').join('');
  // A director block can vanish when the toggle goes off; fall back to all.
  filter.value = names.includes(keep) ? keep : 'all';
}

function fillGenre() {
  const seen = new Map();
  for (const f of DATA) {
    for (const g of f.genres || []) seen.set(g, (seen.get(g) || 0) + 1);
  }
  // Commonest first: on a shelf this size the tail is mostly one-offs.
  const names = [...seen.keys()].sort((a, b) => seen.get(b) - seen.get(a) || a.localeCompare(b));
  const keep = genre.value;
  genre.innerHTML = '<option value="all">Every genre</option>' +
    names.map(n => '<option value="' + esc(n) + '">' + esc(n) + ' (' + seen.get(n) + ')</option>').join('');
  // Set explicitly rather than leaning on a select defaulting to its first
  // option, so the filter cannot start in a state that matches nothing.
  genre.value = names.includes(keep) ? keep : 'all';
}

function render() {
  const term = q.value.trim().toLowerCase();
  const want = filter.value;
  const wantGenre = genre.value;
  let films = DATA.filter(f => {
    if (want !== 'all' && shelfOf(f) !== want) return false;
    // A box set has no genre of its own, so a genre filter would silently drop
    // every box. Keep them; their discs carry the genres.
    if (wantGenre !== 'all' && !f.box && !(f.genres || []).includes(wantGenre)) return false;
    if (!term) return true;
    return f.haystack.indexOf(term) !== -1;
  });

  const key = sortBy.value;
  const cmp = {
    // Within the merged alphabetical run, shelf order is meaningless -- a block
    // film carries a low order number and would jump the queue -- so sort those
    // by title and leave every other shelf in its built order.
    shelf: (a, b) => {
      const sa = shelfRank(shelfOf(a)), sb = shelfRank(shelfOf(b));
      if (sa[0] !== sb[0]) return sa[0] - sb[0];
      if (sa[1] !== sb[1]) return sa[1].localeCompare(sb[1]);
      if (shelfOf(a) === 'Alphabetical') return a.sortkey.localeCompare(b.sortkey);
      return a.order - b.order;
    },
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
    let inBox = false;
    let collapsed = false;
    const closeBox = () => { if (inBox) { html += '</div></div>'; inBox = false; } };
    for (const f of films) {
      const name = shelfOf(f);
      if (name !== current) {
        closeBox();
        if (open) html += '</div></section>';
        current = name;
        // Count boxes and loose titles, not the discs inside a box.
        const n = films.filter(x => shelfOf(x) === current && !x.member).length;
        const isBlock = current.startsWith(BLOCK);
        collapsed = isBlock && shut.has(current);
        const head = isBlock
          ? '<h2 class="click' + (collapsed ? ' shut' : '') + '" data-block="' +
            esc(current) + '"><span class="chev">▼</span>' +
            '<span class="grow">' + esc(current) + '</span><span>' + n + '</span></h2>'
          : '<h2>' + esc(current) + ' <span>' + n + '</span></h2>';
        html += '<section>' + head + '<div class="grid">';
        open = true;
      }
      if (collapsed) continue;
      if (f.box) {
        // A box set is one object on the shelf, so it gets its own panel with
        // its discs inside rather than N loose cards in the run.
        closeBox();
        html += '</div>';           // leave the section's own grid
        const meta = [];
        if (f.span) meta.push(f.span[0] === f.span[1] ? f.span[0] : f.span[0] + '–' + f.span[1]);
        meta.push(f.films + ' films');
        if (f.runtime) meta.push(Math.round(f.runtime / 60) + 'h total');
        if (f.rt_critic != null) meta.push('avg RT ' + f.rt_critic + '%');
        if (f.imdb != null) meta.push('avg IMDb ' + f.imdb);
        html += '<div class="boxset"><div class="boxset-head">' +
          '<span class="bt">' + esc(f.title) + '</span>' +
          '<span class="bm">' + esc(meta.join(' · ')) + '</span>' +
          '</div><div class="grid">';
        inBox = true;
      } else if (!f.member && inBox) {
        closeBox();
        html += '<div class="grid">';
      }
      if (!f.box) html += card(f);
    }
    closeBox();
    if (open) html += '</div></section>';
  } else {
    // Any other sort cuts across the boxes, so discs stand on their own and the
    // box itself is not a film to rank.
    html = '<section><div class="grid">' +
      films.filter(f => !f.box).map(card).join('') + '</div></section>';
  }
  shelf.innerHTML = html;
}

q.addEventListener('input', render);
sortBy.addEventListener('change', render);
filter.addEventListener('change', render);
genre.addEventListener('change', render);

blocksOn.addEventListener('change', () => {
  save();
  fillFilter();
  render();
});

// Delegated: the headings are rebuilt on every render.
shelf.addEventListener('click', event => {
  const head = event.target.closest('h2.click');
  if (!head) return;
  const name = head.dataset.block;
  if (shut.has(name)) shut.delete(name); else shut.add(name);
  save();
  render();
});

fillFilter();
fillGenre();
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
        # Searching "Freddy" should find the Elm Street box, not just the disc,
        # so a box set's haystack includes every title inside it.
        haystack = " ".join(filter(None, [
            record["title"], str(record["year"] or ""), directors,
            " ".join(record["genres"]), record["shelf"],
            record.get("parent") or "",
            " ".join(m["title"] for m in record.get("members", [])),
        ])).lower()
        boxed = bool(record.get("members"))
        data.append({
            "title": record["title"],
            "year": record["year"],
            "runtime": record.get("agg_runtime") if boxed else record["runtime"],
            "directors": directors,
            "genres": record["genres"],
            "rt_critic": record.get("agg_rt") if boxed else record["rt_critic"],
            "rt_audience": record.get("rt_audience"),
            "imdb": record.get("agg_imdb") if boxed else record["imdb_rating"],
            "poster": poster_url(record, embedded),
            "letterboxd": record["letterboxd"],
            "shelf": record["shelf"],
            "order": record["order"],
            "uhd": bool(record.get("uhd", True)),
            "sortkey": sort_title(record["title"]),
            "haystack": haystack,
            "box": bool(boxed),
            "member": record.get("role") == "member",
            "parent": record.get("parent"),
            "films": record.get("film_count"),
            "span": list(record["agg_years"]) if record.get("agg_years") else None,
        })

    # Count the boxes, not the discs inside them -- a five-disc set is one
    # object on the shelf, and counting its contents here would report 40 box
    # sets where there are seven.
    films = [r for r in shelf if r["section"] == "films"]
    boxes = [r for r in shelf if r["section"] == "collections"
             and r.get("role") != "member"]
    discs = sum(r.get("film_count") or 0 for r in boxes)
    summary = "{} films · {} box sets{} · {} director blocks".format(
        len(films),
        len(boxes),
        f" ({discs} discs)" if discs else "",
        len(blocks),
    )

    return PAGE.substitute(
        page_title=html.escape(title),
        summary=html.escape(summary),
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
    parser.add_argument("--title", default="4K Disc Inventory",
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
            records_cache[entry["key"]] = resolve(entry, keys, notes, overrides)
        except (FetchError, SchemaError) as exc:
            print(f"error: {entry['key']}: {exc}", file=sys.stderr)
            return 1

    # Rebuild the working records from the cache every time, so an overrides.toml
    # edit takes effect without re-fetching anything.
    records = []
    unpinned = []
    for entry in entries:
        # dict() is a shallow copy, so the lists inside are still the cache's
        # own. Anything downstream that appends -- the box-set aggregate note
        # does -- would otherwise mutate the cache and be written back, growing
        # the committed file by one duplicate line per box set per build.
        record = dict(records_cache[entry["key"]])
        for field in ("source_notes", "directors", "genres"):
            record[field] = list(record.get(field) or [])
        record.pop("members", None)
        record["section"] = entry["section"]
        record["key"] = entry["key"]
        record["role"] = entry.get("role", "item")
        record["parent"] = entry.get("parent")
        # A tmdb_id added to overrides.toml after the film was already cached
        # changes nothing until it is re-resolved. Saying so is the difference
        # between a pin that works and a pin you think works.
        wanted = overrides.get(entry["key"], {}).get("tmdb_id")
        if wanted and record.get("tmdb_id") != wanted:
            unpinned.append(entry["key"])
        records.append(apply_overrides(record, overrides))

    if unpinned:
        print(
            "warning: these have a pinned tmdb_id that the cache predates; "
            "re-resolve them with:\n  --refresh " +
            " --refresh ".join(f'"{k}"' for k in unpinned),
            file=sys.stderr,
        )

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
