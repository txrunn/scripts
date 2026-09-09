#!/usr/bin/env python3
"""Render the DC Bryant Street slate as a browsable page.

The tracker tells you what is *new*. This tells you what is *on*, which is a
different question and the one you have when you are deciding what to see this
week. Everything currently bookable, grouped by tier, with the recent arrivals
badged and a timeline of when each one turned up.

Posters and trailers come from TMDB and are cached in cache/metadata.json,
because runners keep nothing between jobs and the slate is ~80 films of which
maybe two change on a given day. A title TMDB has never heard of -- and Alamo
programs plenty of those, from CatVideoFest to Dismember the Alamo -- is cached
as a miss so it is not looked up again every morning.

    python build_site.py                    # fetch, enrich, write site/
    python build_site.py --verify           # is TMDB still shaped as expected?
    python build_site.py --refresh-all      # ignore the cache, re-look-up all

TMDB_API_KEY is required. Without it the page still builds, with no posters and
no trailers, and says so in the footer -- a page that quietly lost its artwork
should not look like a page that never had any.
"""

import argparse
import datetime as dt
import html
import json
import os
import re
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import alamo_new_films as alamo

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_STATE = os.path.join(SCRIPT_DIR, "ci-state", "dc-bryant-street.json")
DEFAULT_CACHE = os.path.join(SCRIPT_DIR, "cache", "metadata.json")
DEFAULT_OUT_DIR = os.path.join(SCRIPT_DIR, "site")

TMDB_API = "https://api.themoviedb.org/3"
TMDB_IMAGE = "https://image.tmdb.org/t/p/w342"
YOUTUBE_WATCH = "https://www.youtube.com/watch?v={key}"

USER_AGENT = alamo.USER_AGENT
TIMEOUT = 20
RETRIES = 3
# TMDB's published ceiling is far higher, but the whole slate is ~80 titles and
# almost all of them are cache hits. There is nothing to gain by going faster.
THROTTLE = 0.06

# How long a film wears the NEW badge. A week is roughly how long it takes to
# get round to booking something, and matches how often the slate turns over.
NEW_DAYS = 7

# Trailing "(2026)" in an Alamo title is a real year hint -- they use it to
# disambiguate remakes, which is exactly when TMDB search needs the help.
YEAR_SUFFIX = re.compile(r"^(.*?)\s*\((\d{4})\)\s*$")

TIER_NAME = {alamo.TIER_EVENT: "event", alamo.TIER_REGULAR: "regular",
             alamo.TIER_ADVANCE: "advance"}

TEMPLATE_VERSION = 1


class FetchError(Exception):
    pass


class SchemaError(Exception):
    pass


# --- TMDB --------------------------------------------------------------------


def _redact(url):
    """Strip the api key out of a URL before it reaches a log or an error."""
    return re.sub(r"(api_?key=)[^&]+", r"\1REDACTED", url, flags=re.I)


def fetch(url):
    """GET `url` and parse JSON, retrying transient failures.

    A 4xx other than 429 is not retried: the key or the id is wrong, and asking
    again will not change that.
    """
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })

    last_error = None
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                raw = response.read()
                break
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise FetchError(f"HTTP 401 from {_redact(url)} -- TMDB key rejected")
            if exc.code == 404:
                raise FetchError(f"HTTP 404 from {_redact(url)}")
            if exc.code == 429:
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
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchemaError(f"response from {_redact(url)} is not JSON: {exc}") from exc


def split_year(title):
    """"Nosferatu (1922)" -> ("Nosferatu", 1922). No suffix -> (title, None)."""
    match = YEAR_SUFFIX.match(title)
    if not match:
        return title, None
    return match.group(1), int(match.group(2))


def tmdb_search(query, year_hint, api_key):
    """Best TMDB match for a film title, or None.

    The year is scored rather than filtered, the same way the disc inventory
    does it: a festival-versus-release mismatch should still find the film.
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

    def norm(value):
        return re.sub(r"[^a-z0-9]+", "", (value or "").lower())

    target = norm(query)

    def score(result):
        points = 0.0
        if norm(result.get("title")) == target or norm(result.get("original_title")) == target:
            points += 100
        released = (result.get("release_date") or "")[:4]
        if year_hint and released.isdigit():
            gap = abs(int(released) - year_hint)
            points += 60 if gap == 0 else (25 if gap == 1 else 0)
        # Popularity breaks ties only. As a primary signal it would hand
        # "Nosferatu" to whichever version is trending rather than the 1922 one
        # Alamo is actually screening.
        points += min(result.get("popularity") or 0, 50) / 10.0
        return points

    best = max(results, key=score)
    # A title-only match with no year agreement is a coin flip on a repertory
    # slate full of remakes. Require either an exact title or a year signal.
    if norm(best.get("title")) != target and norm(best.get("original_title")) != target:
        if not year_hint:
            return None
    return best


def tmdb_details(tmdb_id, api_key):
    """Full record plus trailers in one request."""
    url = "{}/movie/{}?api_key={}&append_to_response=videos".format(
        TMDB_API, tmdb_id, api_key
    )
    payload = fetch(url)
    if "title" not in payload:
        raise SchemaError(f"TMDB movie {tmdb_id} response has no 'title'")
    if "videos" not in payload:
        raise SchemaError(f"TMDB movie {tmdb_id} response has no 'videos'")
    return payload


def trailer_from(payload):
    """The best YouTube trailer URL in a TMDB movie payload, or None."""
    videos = (payload.get("videos") or {}).get("results") or []

    def rank(video):
        kind = (video.get("type") or "").lower()
        return (
            0 if kind == "trailer" else (1 if kind == "teaser" else 2),
            0 if video.get("official") else 1,
            # Newest first among equals: a 4K restoration trailer beats the
            # original 1976 one for a film Alamo is showing this week.
            -(len(video.get("published_at") or "")),
            video.get("published_at") or "",
        )

    usable = [v for v in videos
              if (v.get("site") or "").lower() == "youtube" and v.get("key")
              and (v.get("type") or "").lower() in ("trailer", "teaser")]
    if not usable:
        return None
    best = sorted(usable, key=rank)[0]
    return YOUTUBE_WATCH.format(key=best["key"])


# --- Enrichment --------------------------------------------------------------


def enrich(films, cache, api_key, refresh_all=False, verbose=True):
    """Attach poster/trailer/year to each film, filling the cache as it goes.

    Returns (looked_up, missing). A film TMDB cannot place is recorded as a miss
    so tomorrow's run does not ask again -- Alamo's one-off events are never
    going to be in a movie database, and re-querying them every morning would be
    most of the request budget for no result.
    """
    looked_up = 0
    missing = []

    for slug, film in sorted(films.items()):
        entry = cache.get(slug)
        if entry is not None and not refresh_all and entry.get("v") == TEMPLATE_VERSION:
            if not entry.get("tmdb_id"):
                missing.append(film["title"])
            continue

        if not api_key:
            missing.append(film["title"])
            continue

        query, year = split_year(film["title"])
        if verbose:
            print(f"  looking up {film['title']}", file=sys.stderr)
        try:
            hit = tmdb_search(query, year, api_key)
            if hit is None:
                cache[slug] = {"v": TEMPLATE_VERSION, "tmdb_id": None,
                               "checked": dt.date.today().isoformat()}
                missing.append(film["title"])
                looked_up += 1
                continue
            details = tmdb_details(hit["id"], api_key)
        except (FetchError, SchemaError) as exc:
            # One bad title must not take down the page. Record nothing so the
            # next run retries it, and carry on.
            print(f"  warning: {film['title']}: {exc}", file=sys.stderr)
            missing.append(film["title"])
            continue

        poster = details.get("poster_path")
        cache[slug] = {
            "v": TEMPLATE_VERSION,
            "tmdb_id": details["id"],
            "tmdb_title": details.get("title"),
            "year": (details.get("release_date") or "")[:4] or None,
            "poster": (TMDB_IMAGE + poster) if poster else None,
            "trailer": trailer_from(details),
            "checked": dt.date.today().isoformat(),
        }
        looked_up += 1

    return looked_up, missing


# --- Assembly ----------------------------------------------------------------


def format_when(film):
    """A single date keeps its clock time; a run gets its span and no time.

    Quoting one showtime's hour next to a date range implies every screening is
    at that hour, which for a two-week run is simply false.
    """
    days = sorted({s.date() for s in film.get("showtimes") or [film["first_showtime"]]})
    if len(days) == 1:
        return alamo.format_showtime(film["first_showtime"])
    first, last = days[0], days[-1]
    return f"{first:%a %b} {first.day} – {last:%a %b} {last.day}"


def assemble(films, ledger, cache, market, today=None):
    """Turn the slate into the JSON the page renders from."""
    today = today or dt.date.today()
    cutoff = today - dt.timedelta(days=NEW_DAYS)

    cards = []
    for slug, film in films.items():
        meta = cache.get(slug) or {}
        seen = (ledger.get(slug) or {}).get("first_seen")
        is_new = False
        if seen:
            try:
                is_new = dt.date.fromisoformat(seen) > cutoff
            except ValueError:
                is_new = False
        cards.append({
            "slug": slug,
            "title": film["title"],
            "url": alamo.SHOW_URL.format(market=market, slug=slug),
            "tier": TIER_NAME[film.get("tier", alamo.TIER_REGULAR)],
            "label": film.get("label"),
            "when": format_when(film),
            "shows": film["session_count"],
            "sort": film["first_showtime"].isoformat(),
            "poster": meta.get("poster"),
            "trailer": meta.get("trailer"),
            "year": meta.get("year"),
            "seen": seen,
            "new": is_new,
        })

    cards.sort(key=lambda c: (c["sort"], c["title"]))
    return cards


def timeline(ledger, market, limit=40):
    """Recent arrivals grouped by the day they first showed up.

    Reads the committed ledger rather than the slate, so it remembers films that
    have since finished their run -- that history is the reason the ledger is in
    the repo at all.
    """
    by_date = {}
    for slug, entry in ledger.items():
        date = entry.get("first_seen")
        if not date:
            continue
        by_date.setdefault(date, []).append({
            "title": entry.get("title") or slug,
            "url": alamo.SHOW_URL.format(market=market, slug=slug),
        })

    days = []
    for date in sorted(by_date, reverse=True)[:limit]:
        days.append({"date": date, "films": sorted(by_date[date], key=lambda f: f["title"])})
    return days


# --- Page --------------------------------------------------------------------


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
.sub a { color: var(--accent); }
.controls { display: flex; flex-wrap: wrap; gap: 10px; margin: 20px 0 8px; }
input[type=search] {
  font: inherit; padding: 9px 12px; border: 1px solid var(--line);
  border-radius: 8px; background: var(--panel); color: var(--ink);
  flex: 1 1 260px; min-width: 0;
}
.toggle {
  display: inline-flex; align-items: center; gap: 7px; padding: 9px 12px;
  border: 1px solid var(--line); border-radius: 8px; background: var(--panel);
  font-size: 13.5px; color: var(--muted); cursor: pointer; user-select: none;
  white-space: nowrap;
}
.toggle input { margin: 0; cursor: pointer; accent-color: var(--accent); }
.count { color: var(--muted); font-size: 13px; margin-bottom: 26px; }
section { margin-bottom: 40px; }
h2 {
  font-size: 13px; text-transform: uppercase; letter-spacing: 0.09em;
  color: var(--muted); font-weight: 600; margin: 0 0 6px;
  padding-bottom: 8px; border-bottom: 1px solid var(--line);
}
h2 span { color: var(--accent); }
h2 + .note { color: var(--muted); font-size: 12.5px; margin: 0 0 14px; }
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
  position: absolute; top: 7px; right: 7px; background: var(--accent);
  color: #fff; font-size: 10px; font-weight: 700; letter-spacing: .05em;
  padding: 3px 6px; border-radius: 4px;
}
.name {
  font-weight: 600; font-size: 14px; line-height: 1.3;
  /* Two lines reserved so rows do not jag. Long repertory titles like "Star
     Trek: The Motion Picture - The Director's Edition" simply take a third
     rather than being cut -- the title is the whole point of the card. */
  min-height: 2.6em;
}
.name a { color: inherit; text-decoration: none; }
.name a:hover { color: var(--accent); text-decoration: underline; }
.meta { color: var(--muted); font-size: 12.5px; margin-top: 3px; }
.series { color: var(--accent); font-size: 12px; margin-top: 3px; }
.trailer {
  display: inline-flex; align-items: center; gap: 4px; margin-top: 6px;
  font-size: 12px; color: var(--muted); text-decoration: none;
}
.trailer:hover { color: var(--accent); }
.empty { color: var(--muted); font-size: 14px; padding: 30px 0; }
.tl { border-left: 2px solid var(--line); padding-left: 18px; margin-left: 4px; }
.tl-day { margin-bottom: 18px; position: relative; }
.tl-day::before {
  content: ""; position: absolute; left: -24px; top: 6px; width: 9px; height: 9px;
  border-radius: 50%; background: var(--accent);
}
.tl-date { font-size: 12px; color: var(--muted); letter-spacing: .04em; margin-bottom: 3px; }
.tl-films a { color: inherit; text-decoration: none; }
.tl-films a:hover { color: var(--accent); text-decoration: underline; }
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
  <div class="sub">$subtitle</div>
</header>

<div class="controls">
  <input type="search" id="q" placeholder="Search titles and series..." autocomplete="off">
  <label class="toggle"><input type="checkbox" id="events"> Special events only</label>
  <label class="toggle"><input type="checkbox" id="fresh"> Added this week</label>
</div>
<div class="count" id="count"></div>

<div id="slate"></div>

<section id="timeline-wrap">
  <h2>Recently added</h2>
  <p class="note">When each title first appeared on the schedule, from the tracker's ledger.
     Includes films whose run has since ended.</p>
  <div class="tl" id="timeline"></div>
</section>

<footer>$footer</footer>
</div>

<script>
const DATA = $data;
const TIMELINE = $timeline;

const slate = document.getElementById('slate');
const tl = document.getElementById('timeline');
const q = document.getElementById('q');
const eventsOnly = document.getElementById('events');
const freshOnly = document.getElementById('fresh');
const count = document.getElementById('count');

const STORE = 'alamo-slate';
const TIERS = [
  ['event', 'Special events', 'One-offs and series screenings. Seats go early.'],
  ['regular', 'Regular releases', ''],
  ['advance', 'Advance screenings', 'The film returns in a regular run; the merch does not.']
];

// Both filters are remembered: this is a page you come back to on a Friday
// rather than land on once, and most visits want the same view as last time.
let prefs = {events: false, fresh: false};
try { Object.assign(prefs, JSON.parse(localStorage.getItem(STORE) || '{}')); } catch (e) {}
eventsOnly.checked = !!prefs.events;
freshOnly.checked = !!prefs.fresh;

function save() {
  try {
    localStorage.setItem(STORE, JSON.stringify({
      events: eventsOnly.checked, fresh: freshOnly.checked
    }));
  } catch (e) {}
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]
  ));
}

function card(f) {
  const poster = f.poster
    ? '<img loading="lazy" src="' + esc(f.poster) + '" alt="">'
    : '<div class="none">' + esc(f.title) + '</div>';
  // Only the new ones get a badge. Badging all 78 would mark nothing.
  const badge = f.new ? '<div class="badge">NEW</div>' : '';
  const bits = [f.when];
  bits.push(f.shows + (f.shows === 1 ? ' show' : ' shows'));
  const trailer = f.trailer
    ? '<a class="trailer" href="' + esc(f.trailer) + '" rel="noopener">&#9654; Trailer</a>'
    : '';
  return '<div class="card">' +
    '<div class="poster">' + poster + badge + '</div>' +
    '<div class="name"><a href="' + esc(f.url) + '" rel="noopener">' + esc(f.title) + '</a></div>' +
    (f.label ? '<div class="series">' + esc(f.label) + '</div>' : '') +
    '<div class="meta">' + esc(bits.join(' \\u00b7 ')) + '</div>' +
    trailer +
    '</div>';
}

function render() {
  const term = q.value.trim().toLowerCase();
  const shown = DATA.filter(f => {
    if (eventsOnly.checked && f.tier !== 'event') return false;
    if (freshOnly.checked && !f.new) return false;
    if (!term) return true;
    return (f.title + ' ' + (f.label || '')).toLowerCase().includes(term);
  });

  let html = '';
  for (const [tier, heading, note] of TIERS) {
    const group = shown.filter(f => f.tier === tier);
    if (!group.length) continue;
    html += '<section><h2>' + heading + ' <span>' + group.length + '</span></h2>' +
      (note ? '<p class="note">' + note + '</p>' : '') +
      '<div class="grid">' + group.map(card).join('') + '</div></section>';
  }
  slate.innerHTML = html || '<p class="empty">Nothing matches that.</p>';

  const total = DATA.length;
  count.textContent = shown.length === total
    ? total + ' bookable now'
    : shown.length + ' of ' + total + ' bookable';
}

function renderTimeline() {
  tl.innerHTML = TIMELINE.map(day => {
    const links = day.films.map(f =>
      '<a href="' + esc(f.url) + '" rel="noopener">' + esc(f.title) + '</a>'
    ).join(', ');
    return '<div class="tl-day"><div class="tl-date">' + esc(day.date) + '</div>' +
      '<div class="tl-films">' + links + '</div></div>';
  }).join('') || '<p class="empty">No history yet.</p>';
}

q.addEventListener('input', render);
eventsOnly.addEventListener('change', () => { save(); render(); });
freshOnly.addEventListener('change', () => { save(); render(); });
render();
renderTimeline();
</script>
</body>
</html>
""")


def render_html(cards, days, title, label, market, missing, has_key):
    """Build the page. Data is injected as JSON and rendered client-side."""
    events = sum(1 for c in cards if c["tier"] == "event")
    fresh = sum(1 for c in cards if c["new"])
    calendar = f"https://drafthouse.com/{market}?showCalendar=true"

    subtitle = (
        f"{len(cards)} films bookable at {html.escape(label)} — "
        f"{events} special event{'s' if events != 1 else ''}, "
        f"{fresh} added in the last {NEW_DAYS} days. "
        f'<a href="{calendar}" rel="noopener">Full DC Metro calendar</a>'
    )

    notes = [f"Built {dt.date.today():%d %b %Y} from the "
             '<a href="https://drafthouse.com/s/mother/v2/schedule/market/dc-metro-area"'
             ' rel="noopener">Alamo schedule API</a>.']
    if has_key:
        notes.append(
            "Posters and trailers from "
            '<a href="https://www.themoviedb.org/" rel="noopener">TMDB</a>.'
        )
        if missing:
            # Alamo programs a lot of things a movie database has never heard
            # of. Saying so is the difference between a known gap and a page
            # that looks broken.
            notes.append(
                f"{len(missing)} title{'s' if len(missing) != 1 else ''} had no TMDB "
                "match — mostly festivals, live events and one-offs."
            )
    else:
        notes.append(
            "<strong>No TMDB key was set, so this build has no posters or "
            "trailers.</strong>"
        )
    notes.append(
        '<a href="https://github.com/txrunn/scripts/tree/main/alamo-drafthouse"'
        ' rel="noopener">Source and the tracker that feeds it</a>.'
    )

    return PAGE.substitute(
        page_title=html.escape(title),
        subtitle=subtitle,
        footer=" ".join(notes),
        # "</" is escaped because a film title containing "</script>" would
        # otherwise close the block it is embedded in. Alamo writes these
        # titles, so this is untrusted text. Same escape the disc inventory uses.
        data=json.dumps(cards, ensure_ascii=False).replace("</", "<\\/"),
        timeline=json.dumps(days, ensure_ascii=False).replace("</", "<\\/"),
    )


# --- IO ----------------------------------------------------------------------


def load_json(path, default):
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError) as exc:
        print(f"warning: could not read {path}: {exc}", file=sys.stderr)
        return default


def save_json(path, payload):
    path = os.path.expanduser(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)


# --- Modes -------------------------------------------------------------------


def mode_verify(api_key):
    """Fail loudly if TMDB changed shape.

    The schedule side is already covered by `alamo_new_films.py --verify`; this
    checks only the half that page-building adds.
    """
    ok = True

    def line(name, status, detail=""):
        print(f"{name:<44} {status}  {detail}")

    if not api_key:
        line("TMDB key", "SKIP", "TMDB_API_KEY is not set")
        return 0

    try:
        hit = tmdb_search("Taxi Driver", 1976, api_key)
    except (FetchError, SchemaError) as exc:
        line("TMDB search", "FAIL", str(exc))
        return 1
    if not hit or not hit.get("id"):
        line("TMDB search", "FAIL", "no match for a film that certainly exists")
        return 1
    line("TMDB search", "PASS", f"Taxi Driver -> id {hit['id']}")

    try:
        details = tmdb_details(hit["id"], api_key)
    except (FetchError, SchemaError) as exc:
        line("TMDB movie + videos", "FAIL", str(exc))
        return 1

    if not details.get("poster_path"):
        line("poster_path", "WARN", "absent on a film that has artwork")
    else:
        line("poster_path", "PASS", details["poster_path"])

    trailer = trailer_from(details)
    if trailer:
        line("trailer lookup", "PASS", trailer)
    else:
        # Not fatal: plenty of films genuinely have no trailer on TMDB, and the
        # card simply omits the link. Worth saying out loud all the same.
        line("trailer lookup", "WARN", "no YouTube trailer on this title")

    return 0 if ok else 1


def build_parser():
    parser = argparse.ArgumentParser(
        description="Render the Bryant Street slate as a browsable page.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--market", default=alamo.MARKET_SLUG)
    parser.add_argument("--match", default=alamo.CINEMA_MATCH,
                        help="substring identifying the cinema")
    parser.add_argument("--cinema-id", default=None)
    parser.add_argument("--from-file", default=None,
                        help="read the schedule from a saved payload instead of the network")
    parser.add_argument("--state", default=DEFAULT_STATE,
                        help="ledger read for first-seen dates (default: ci-state/)")
    parser.add_argument("--cache", default=DEFAULT_CACHE)
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument("--title", default="Alamo Bryant Street")
    parser.add_argument("--refresh-all", action="store_true",
                        help="ignore the cache and re-look-up every title")
    parser.add_argument("--verify", action="store_true",
                        help="check the TMDB contract and exit")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    api_key = os.environ.get("TMDB_API_KEY", "").strip()

    if args.verify:
        return mode_verify(api_key)

    if not api_key:
        print("warning: TMDB_API_KEY is not set -- building without posters or trailers",
              file=sys.stderr)

    try:
        payload = (json.load(open(os.path.expanduser(args.from_file), encoding="utf-8"))
                   if args.from_file
                   else alamo.fetch(alamo.SCHEDULE_URL.format(market=args.market)))
        presentations, sessions = alamo.extract(payload)
        cinemas = alamo.collect_cinemas(payload, sessions)
        cinema_key, label = alamo.resolve_cinema(
            cinemas, sessions, args.cinema_id, args.match
        )
        films = alamo.upcoming_films(
            sessions,
            alamo.index_presentations(presentations),
            cinema_key,
            hidden_slugs=alamo.hidden_presentation_slugs(presentations),
            classifications=alamo.index_classifications(presentations),
        )
    except (alamo.FetchError, alamo.SchemaError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not films:
        print("error: no bookable films found -- refusing to write an empty page",
              file=sys.stderr)
        return 1

    cache = load_json(args.cache, {})
    ledger = (load_json(args.state, {}) or {}).get("seen", {})

    looked_up, missing = enrich(films, cache, api_key, args.refresh_all,
                                verbose=not args.quiet)

    cards = assemble(films, ledger, cache, args.market)
    days = timeline(ledger, args.market)
    page = render_html(cards, days, args.title, label, args.market, missing,
                       has_key=bool(api_key))

    out_dir = os.path.expanduser(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "index.html")
    with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(page)

    if api_key:
        save_json(args.cache, cache)

    fresh = sum(1 for c in cards if c["new"])
    print(f"{len(cards)} films written to {out_path}")
    print(f"  {fresh} badged new, {looked_up} looked up, {len(missing)} without TMDB art")
    return 0


if __name__ == "__main__":
    sys.exit(main())
