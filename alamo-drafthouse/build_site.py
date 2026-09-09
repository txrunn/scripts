#!/usr/bin/env python3
"""Render what has just gone on sale at DC Bryant Street.

Two things earn a place on the page and nothing else does: films the tracker
watched go on sale, newest morning first, and limited runs coming up. A wide
release playing all month is neither, which is the whole point -- Alamo already
publishes their schedule and does it better than this would.

"Limited" is a programmed special, or a last remaining date, or a short run that
has not opened yet. That last clause is what keeps a blockbuster down to its
final three showtimes off a page about things you can miss.

Every booking of one film collapses into a single card carrying all its
showtimes, so Princess Mononoke is one entry offering the dubbed and the
subtitled date rather than two entries that look like a bug.

Artwork is Alamo's own, out of the same schedule payload the tracker already
fetches -- every presentation has one, including the festivals and livestreams a
film database has never heard of. TMDB supplies only trailer links and the id
that joins two bookings, cached in cache/metadata.json because runners keep
nothing between jobs.

    python build_site.py                    # fetch, enrich, write site/
    python build_site.py --verify           # is TMDB still shaped as expected?
    python build_site.py --refresh-all      # ignore the cache, re-look-up all

TMDB_API_KEY is optional. Without it the page keeps all its artwork and loses
only the trailer links, and says so in the footer.
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

# Where the page is published. Only the link-preview tags need it -- og:url has
# to be absolute, and a relative one makes the card fall back to a bare link.
SITE_URL = "https://txrunn.github.io/scripts/alamo/"

TMDB_API = "https://api.themoviedb.org/3"
YOUTUBE_WATCH = "https://www.youtube.com/watch?v={key}"

# Alamo serves its own key art through imgix, so the size is ours to ask for.
# The originals are ~470KB at 1080 wide, which is absurd for a 136px card.
POSTER_W, POSTER_H = 340, 510

# Alamo's mark, used to make an unofficial page about Alamo look like it is
# about Alamo. The footer says plainly that this is not their site.
LOGO = ("https://images.squarespace-cdn.com/content/v1/67c8ca97b8e01f608e7e617a/"
        "05efa451-5164-47c8-b1bb-89ede4bd5190/alamo_logos+%281%29.png")

USER_AGENT = alamo.USER_AGENT
TIMEOUT = 20
RETRIES = 3
# TMDB's published ceiling is far higher, but the whole slate is ~80 titles and
# almost all of them are cache hits. There is nothing to gain by going faster.
THROTTLE = 0.06

# How long a film wears the NEW badge. A week is roughly how long it takes to
# get round to booking something, and matches how often the slate turns over.
NEW_DAYS = 7

# What counts as limited rather than a run. The slate breaks cleanly here: 49 of
# 59 films screen four times or fewer and the next ones up are 13, 16, 17 and 39
# -- the wide releases you cannot miss. Spirited Away's two showings, dubbed and
# subtitled, are as gone-in-a-blink as a single one.
LIMITED_SHOWS = 4

# Trailing "(2026)" in an Alamo title is a real year hint -- they use it to
# disambiguate remakes, which is exactly when TMDB search needs the help.
YEAR_SUFFIX = re.compile(r"^(.*?)\s*\((\d{4})\)\s*$")

# How the print is being shown, not what the film is. Alamo lists Princess
# Mononoke twice, dubbed and subtitled, and TMDB has never heard of either
# spelling -- so these come off before the search and both cards land on the
# same film. Deliberately only formats and presentation gimmicks: anything that
# might be part of a real title stays, because a wrong poster is worse than
# none. The suffix is kept on the card, which is where it matters to you.
FORMAT_SUFFIX = re.compile(
    r"\s*\((?:dubbed|subtitled|dub|sub|subbed|"
    r"\d{2}mm|4k|imax|3d|"
    r"quote-?along|sing-?along|movie\s*party)\)\s*$",
    re.I,
)

TIER_NAME = {alamo.TIER_EVENT: "event", alamo.TIER_REGULAR: "regular",
             alamo.TIER_ADVANCE: "advance"}

# Bump to invalidate the cache when the matcher changes. Entries recorded as
# misses under an older, worse matcher are the whole reason this exists:
# nothing else would ever retry them.
#   2: format suffixes stripped before searching
TEMPLATE_VERSION = 2


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


def poster_for(show):
    """Alamo's own poster for a show, sized for a card.

    Their art beats a film database's for this page: it is the key art for the
    booking you are actually being sold -- the Quote-Along artwork, the festival
    poster -- and every presentation has one, including the festivals and
    livestreams TMDB has never heard of.
    """
    images = show.get("posterImages") or []
    uri = (images[0] or {}).get("uri") if images else None
    if not uri:
        uri = (show.get("portraitHeroImage") or {}).get("uri")
    if not uri:
        return None
    uri = re.sub(r"([?&])w=\d+", r"\g<1>w=%d" % POSTER_W, uri)
    uri = re.sub(r"([?&])h=\d+", r"\g<1>h=%d" % POSTER_H, uri)
    return uri


def openings_by_slug(presentations):
    """slug -> the date Alamo says it opens, or None if it is already playing.

    This is what separates a repertory one-off from a blockbuster running out of
    showtimes. Both can be down to three dates; only one of them is news. A film
    already in general release has no opening date left to give.
    """
    out = {}
    for presentation in presentations:
        slug = presentation.get("slug")
        if slug:
            out[slug] = presentation.get("openingDateClt")
    return out


def posters_by_slug(presentations):
    """slug -> poster URL, for every presentation that has one."""
    out = {}
    for presentation in presentations:
        slug = presentation.get("slug")
        art = poster_for(presentation.get("show") or {})
        if slug and art:
            out[slug] = art
    return out


def split_year(title):
    """Reduce an Alamo title to what TMDB would call it, plus a year hint.

    "Nosferatu (1922)"        -> ("Nosferatu", 1922)
    "Princess Mononoke (Dubbed)" -> ("Princess Mononoke", None)
    "Taxi Driver"             -> ("Taxi Driver", None)

    Format suffixes come off first and repeatedly, because Alamo stacks them
    ("Akira (Dubbed) (35mm)"), and a year can sit behind one.
    """
    while True:
        stripped = FORMAT_SUFFIX.sub("", title)
        if stripped == title:
            break
        title = stripped

    match = YEAR_SUFFIX.match(title)
    if not match:
        return title.strip(), None
    return match.group(1).strip(), int(match.group(2))


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
    """Look up trailers, filling the cache as it goes.

    Posters come from Alamo. TMDB is here for the trailer, and for the id that
    tells two bookings of one film apart -- though the title fallback handles
    that too, so a build with no key loses only the trailer links.

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

        cache[slug] = {
            "v": TEMPLATE_VERSION,
            "tmdb_id": details["id"],
            "tmdb_title": details.get("title"),
            "year": (details.get("release_date") or "")[:4] or None,
            "trailer": trailer_from(details),
            "checked": dt.date.today().isoformat(),
        }
        looked_up += 1

    return looked_up, missing


# --- Assembly ----------------------------------------------------------------


def clock(when):
    """"4:00 PM". strftime %-I is not portable, so build it by hand."""
    hour = when.hour % 12 or 12
    return f"{hour}:{when.minute:02d} {'AM' if when.hour < 12 else 'PM'}"


def screening_days(film):
    """The distinct calendar days this film has an upcoming showtime on."""
    return sorted({s.date() for s in film.get("showtimes") or [film["first_showtime"]]})


def variant_note(title, base, label):
    """What tells one booking of a film apart from another.

    "Princess Mononoke (Dubbed)" against the base title gives "Dubbed"; where
    the titles match, the series does it ("Family Parties"). None when only the
    date separates them, which the date already says.
    """
    if title.lower().startswith(base.lower()) and len(title) > len(base):
        extra = title[len(base):].strip().strip("()").strip()
        if extra:
            return extra
    return label or None


def group_key(slug, title, cache):
    """Identity of the film behind a booking.

    Alamo books the same film more than once -- dubbed and subtitled, a Family
    Party and a normal run -- each under its own slug. TMDB's id is the reliable
    join because the suffix stripping already collapses the spellings; the
    stripped title is the fallback for the one-offs TMDB has never heard of.
    """
    entry = cache.get(slug) or {}
    if entry.get("tmdb_id"):
        return "tmdb:%s" % entry["tmdb_id"]
    return "title:%s" % split_year(title)[0].lower()


def venue_now():
    """Now, in the cinema's clock where we can get it.

    The runner is UTC and the cinema is not. Left on UTC, a build at 23:53 EDT
    stamps itself 8 September and then files that evening's arrivals under
    "Yesterday", because the runner's date has already rolled over. Every day
    boundary on this page comes from here. zoneinfo needs tzdata the runner has
    and a bare Windows checkout may not, so it falls back to UTC.
    """
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return dt.datetime.now(dt.timezone.utc)


def venue_today():
    """Today's date at the cinema, which is the one the page reckons in."""
    return venue_now().date()


def build_stamp():
    now = venue_now()
    zone = now.strftime("%Z") or "UTC"
    return f"{now.day} {now:%b}, {clock(now)} {zone}"


def baseline_date(ledger):
    """The day the ledger was seeded, which is not a day anything was added.

    The tracker's first run records the whole slate at once and reports none of
    it -- those films were simply playing when tracking started. Without this
    the page opens with 42 films that are not news, which is the opposite of
    what it is for.
    """
    dates = [e.get("first_seen") for e in ledger.values() if e.get("first_seen")]
    return min(dates) if dates else None


def assemble(films, ledger, cache, market, posters=None, opens=None, today=None):
    """One card per film, carrying every booking of it.

    A film Alamo listed twice -- dubbed and subtitled, or a Family Party
    alongside a normal run -- is one entry offering both dates, not two entries
    that look like a bug.

    Nothing is filtered here. Each card is marked `fresh` (the tracker saw it
    appear) and `oneoff` (it screens once, or it is a programmed special), and
    the two sections of the page choose from those.
    """
    today = today or venue_today()
    seeded = baseline_date(ledger)
    posters = posters or {}
    opens = opens or {}

    bookings = {}
    for slug, film in films.items():
        seen = (ledger.get(slug) or {}).get("first_seen")
        meta = cache.get(slug) or {}
        first = film["first_showtime"]
        days = screening_days(film)
        last = days[-1]
        bookings.setdefault(group_key(slug, film["title"], cache), []).append({
            "slug": slug,
            "raw_title": film["title"],
            "base": split_year(film["title"])[0],
            "url": alamo.SHOW_URL.format(market=market, slug=slug),
            "tier": TIER_NAME[film.get("tier", alamo.TIER_REGULAR)],
            "label": film.get("label"),
            "first": first,
            "run": None if len(days) == 1 else f"{last.day} {last:%b}",
            "shows": film["session_count"],
            "opens": opens.get(slug),
            "poster": posters.get(slug),
            "trailer": meta.get("trailer"),
            "year": meta.get("year"),
            "seen": seen,
        })

    cards = []
    for gkey, group in bookings.items():
        group.sort(key=lambda b: b["first"])
        lead = group[0]
        shared = len(group) > 1
        name = min((b["base"] for b in group), key=len) if shared else lead["raw_title"]
        stamps = [b["seen"] for b in group if b["seen"]]
        added = min(stamps) if stamps else None

        showings = []
        for b in group:
            showings.append({
                "url": b["url"],
                # The formatted date is for reading; the planner needs to do
                # arithmetic, and reparsing "Sat 26 Sep" in the browser would
                # guess at the year.
                "iso": b["first"].date().isoformat(),
                "note": variant_note(b["raw_title"], name, b["label"]) if shared else None,
                "date": f"{b['first']:%a} {b['first'].day} {b['first']:%b}",
                "time": clock(b["first"]),
                "run": b["run"],
                "shows": b["shows"],
            })

        total = sum(sh["shows"] for sh in showings)
        cards.append({
            "gkey": gkey,
            "title": name,
            "url": lead["url"],
            "tier": lead["tier"],
            "label": None if shared else lead["label"],
            "added": added,
            # The seed batch is not an arrival: those films were simply playing
            # the day tracking started.
            "fresh": bool(added) and added != seeded,
            # What sells out. A programmed special or an advance screening
            # always -- the film comes back but the early date does not, and the
            # merch with it. A last remaining screening always. And a short run
            # that has not opened yet.
            #
            # The opening date is what keeps Spider-Man out. Down to three dates
            # it looks limited by count alone, but it has been in general release
            # for a month and Alamo gives it no opening date -- unlike Memento,
            # which also plays twice and opens next week. Both are short. Only
            # one of them is news.
            "oneoff": (
                lead["tier"] in ("event", "advance")
                or total == 1
                or (lead["opens"] is not None
                    and lead["opens"] >= today.isoformat()
                    and total <= LIMITED_SHOWS)
            ),
            "showings": showings,
            "day": lead["first"].date().isoformat(),
            "sort": lead["first"].isoformat(),
            "poster": lead["poster"],
            "trailer": lead["trailer"],
            "year": lead["year"],
        })

    cards.sort(key=lambda c: c["sort"])
    return cards


def added_batches(cards, today=None):
    """Newly-added films grouped into the mornings they arrived, newest first."""
    today = today or venue_today()
    groups = {}
    for card in cards:
        if not card["fresh"]:
            continue
        groups.setdefault(card["added"], []).append(card)

    out = []
    for date in sorted(groups, reverse=True):
        when = dt.date.fromisoformat(date)
        delta = (today - when).days
        if delta == 0:
            label, sub = "Today", ""
        elif delta == 1:
            label, sub = "Yesterday", f"{when:%a} {when.day} {when:%b}"
        else:
            label, sub = f"{when:%a} {when.day} {when:%b}", f"{delta} days ago"
        out.append({"label": label, "sub": sub, "films": groups[date]})
    return out


def upcoming_batches(cards, today=None):
    """Limited runs ahead, soonest first.

    Skips anything already listed as newly added -- it is the same film and the
    page would be telling you twice. Long runs never qualify: a wide release
    playing for a month is not something you can miss.

    No distance cutoff. There was a ten-week one and it was hiding exactly one
    film: the December advance screening of Dune, which is the single thing on
    this slate you would most want a quarter's notice of. Being limited is
    already the bound -- 33 films qualify at any distance -- and Alamo's own
    schedule window is the other.
    """
    today = today or venue_today()

    out = []
    for card in cards:
        if card["fresh"] or not card["oneoff"]:
            continue
        out.append(card)

    # A flat run rather than a day spine: each card already prints its own date,
    # so grouping by day would say it twice and stretch 29 films down 20 rows.
    out.sort(key=lambda c: c["sort"])
    return out


def name_list(names, cap=3):
    """"A, B and C", or "A, B and 4 others" past the cap."""
    if not names:
        return ""
    if len(names) > cap:
        rest = len(names) - cap
        return ", ".join(names[:cap]) + f" and {rest} other" + ("s" if rest != 1 else "")
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def when_phrase(batch):
    """"today", "yesterday", "4 days ago" -- however the batch labelled itself."""
    label = batch["label"]
    if label in ("Today", "Yesterday"):
        return label.lower()
    return batch["sub"] or label


def recent_summary(added):
    """The two most recent arrivals, named, with how long ago.

    The counts above say how much; this says what, which is the question you
    actually arrive with. Two batches because one is often a single film and
    tells you nothing about whether it has been a quiet week.
    """
    if not added:
        return ""
    parts = []
    for batch in added[:2]:
        names = name_list([f["title"] for f in batch["films"]])
        parts.append(f"{names}, {when_phrase(batch)}")
    return "Last found: " + "; then ".join(parts) + "."


def card_description(added, soon, since=None):
    """The sentence a link preview shows under the title.

    Plain text: the standfirst on the page carries <b> tags, and a meta
    attribute renders them literally. Leads with the counts, then names the
    most recent arrivals -- the counts say how much, the names say whether it
    is worth opening.
    """
    new_count = sum(len(b["films"]) for b in added)
    started = ""
    if since:
        when = dt.date.fromisoformat(since)
        started = f" since {when.day} {when:%B}"

    if not new_count and not soon:
        return "Nothing newly on sale at DC Bryant Street right now."

    # Each clause is dropped when its count is zero rather than rendered as
    # "0 films", which reads like the page is broken.
    parts = []
    if new_count:
        parts.append(f"{new_count} film{'' if new_count == 1 else 's'} newly on sale{started}")
    if soon:
        parts.append(f"{len(soon)} limited run{'' if len(soon) == 1 else 's'} coming up")
    head = " and ".join(parts) + "."

    summary = recent_summary(added)
    return f"{head} {summary}".strip()


# Poster size for the link-preview card. The page asks Alamo for 340x510,
# which is right for a grid tile and soft as the only image in a card, so the
# card asks the same CDN for the same art at double.
CARD_W, CARD_H = POSTER_W * 2, POSTER_H * 2


def card_image(added, soon):
    """Poster for the preview card, or None.

    The newest arrival, falling back to the soonest limited run -- whatever the
    page leads with is what the card should show.

    Alamo's image CDN takes the dimensions as query parameters, the same way
    poster_uri sets them, so this re-asks for a bigger crop of the same file
    rather than upscaling a thumbnail.
    """
    for film in [f for b in added for f in b["films"]] + list(soon):
        poster = film.get("poster")
        if not poster:
            continue
        poster = re.sub(r"([?&])w=\d+", r"\g<1>w=%d" % CARD_W, poster)
        poster = re.sub(r"([?&])h=\d+", r"\g<1>h=%d" % CARD_H, poster)
        # TMDB paths encode the width instead, and older cache entries hold
        # them. Harmless where it does not match.
        return poster.replace("/w342/", "/w780/")
    return None


def social_tags(url, title, description, image):
    """Open Graph and Twitter card tags.

    twitter:card is `summary`, not `summary_large_image`: the only art we have
    is a portrait poster, and a wide card centre-crops it to a band across the
    middle of someone's face. A summary card shows it whole, beside the text.
    """
    tags = [
        ("og:type", "website"),
        ("og:site_name", "Alamo DC Bryant Street tracker"),
        ("og:title", title),
        ("og:description", description),
        ("og:url", url),
        ("twitter:card", "summary"),
        ("twitter:title", title),
        ("twitter:description", description),
    ]
    if image:
        tags += [("og:image", image), ("twitter:image", image),
                 ("og:image:alt", "Poster for the most recent arrival")]

    # og:* is a property, twitter:* a name. Getting this wrong is the usual
    # reason a card silently does not render.
    out = []
    for key, value in tags:
        attr = "property" if key.startswith("og:") else "name"
        out.append(f'<meta {attr}="{key}" content="{html.escape(value, quote=True)}">')
    return "\n".join(out)


# --- Page --------------------------------------------------------------------


PAGE = string.Template("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$page_title</title>
$social
<link rel="icon" type="image/png" href="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAMAAACdt4HsAAAAYFBMVEX0siMHCAjqqyK4hxynexoyJwyacRlKOA8ZFQmMaBdZQxHLlB4jHAtzVhTVnCBmTBPCjh7coSGAXxY/MA4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA87wXQAAAAIHRSTlP//////////////////////////wAAAAAAAAAAAAAAALESzYUAAAH0SURBVHja7VXRjuMgDMQYxxgCJL3//9cb0/ZUurtS7+EeTspIbWGwx46N0xAuXLhw4cK/Ryw50wNZ0kculTe7tTFKKapZbEs8kWyQAqWM0ZrT/Tv3G+WcRWRaltHi62kaD10V2ZGaxjf3KplSfGPnfnLxja9Cv+rCZeXTf3t36/iU6v0pdP9Ep6Ysa14EqLojC5HWEGXn6a5EXr6WC4SSZJ4WebhEpVVgOyO4WfIYiZLnIXPPYTi5YVndAr3REM9tFcg7yjJIOp7OQp4CRpmRhLoAife0ohs9pkwVSa6PkA1pCaKFDRW+CxRIIascGppINOCnsIhB/dj2RUBgCz9jbiRYeA2UGjMyj41an2wt1FCa3Y9NFgGVFuPj5rlpMUO4OyAwPAIEGKeaYVGb6CJQ5Oa1a2YFx9MPAmrWIDCoeOe8nm1GqAECZRXANqbkzU6MBRA4+S0AWdPhTZnHNRnHp8cLjI74V6N2eIWX2aNsCLJV60c9jM2nyOpRN8xSOO3ghAUjiw3WqEN5D9gLytrKyKalaS8sGKjSmg4twVpqIkUt64ZLWyl9mccYEm5GsopAKZ2BT88AkbGz0JEBeOOKb56mX1Hp4yo0+ZYW2Vb8EeSVN9p+eInJ/gIhvb+QcBv3BWKfJdrl+U5s11/EhQsXLvwn+A0WDhGl39if1wAAAABJRU5ErkJggg==">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Jost:wght@400;500;600;700&display=swap"
      rel="stylesheet">
<style>
/* Alamo's own values, lifted from drafthouse.com: black #090909, the yellow
   #f5b324 they put on everything you are meant to press, #333 rules. Dark only,
   because the brand is dark and a light variant would be someone else's page.
   Jost stands in for Futura PT, which Alamo licenses and we cannot. */
:root {
  color-scheme: dark;
  --paper: #090909; --sunk: #1a1a1a; --ink: #f2f2f2;
  --soft: #a3a3a3; --rule: #333333; --brand: #f5b324;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--paper); color: var(--ink);
  font: 400 15px/1.45 Jost, "Futura", "Century Gothic", Avenir, sans-serif;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 1040px; margin: 0 auto; padding: 34px 22px 90px; }
a { color: inherit; }
:focus-visible { outline: 2px solid var(--brand); outline-offset: 2px; }

/* Alamo put yellow on the things you are meant to press; a solid band of it is
   the most Alamo the page can be in one element. It also means the mark needs
   no treatment: the source art is black, which is what it wants to be here. */
.bar { background: var(--brand); color: #090909; }
.bar-in {
  max-width: 1040px; margin: 0 auto; padding: 15px 22px;
  display: flex; align-items: center; gap: 16px;
}
/* 1496x600 of logo centred in a mostly transparent 2100 square. Scaled 3.5x
   (2100/600) behind a window of its own 2.49:1 so the padding does not become
   two thirds of the masthead. */
.logo {
  position: relative; flex: none; width: 80px; height: 32px; overflow: hidden;
}
.logo img {
  position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%);
  width: 112px; height: 112px;
}
h1 { margin: 0; font-size: 21px; font-weight: 600; letter-spacing: -0.01em; }
.stamp {
  margin-left: auto; font-size: 12px; font-weight: 500; opacity: 0.7;
  white-space: nowrap;
}
header { margin-bottom: 30px; }
.standfirst {
  margin: 12px 0 0; font-size: 25px; font-weight: 400; line-height: 1.25;
  letter-spacing: -0.02em; max-width: 34ch;
}
.standfirst b { font-weight: 600; color: var(--brand); }
.summary {
  margin: 10px 0 0; font-size: 13.5px; color: var(--soft); max-width: 62ch;
  line-height: 1.5;
}
.summary:empty { display: none; }
.controls { display: flex; flex-wrap: wrap; gap: 9px; margin: 24px 0 0; }
input[type=search] {
  font: inherit; padding: 9px 12px; border: 1px solid var(--rule);
  border-radius: 2px; background: transparent; color: var(--ink);
  flex: 1 1 240px; min-width: 0;
}
input[type=search]::placeholder { color: var(--soft); }
.toggle {
  display: inline-flex; align-items: center; gap: 7px; padding: 9px 12px;
  border: 1px solid var(--rule); border-radius: 2px;
  font-size: 14px; color: var(--soft); cursor: pointer; user-select: none;
  white-space: nowrap;
}
.toggle input { margin: 0; cursor: pointer; accent-color: var(--brand); }
.count { color: var(--soft); font-size: 13px; }
.count:not(:empty) { margin-top: 12px; }

/* One row per morning the tracker found something. The date is the spine. */
section { margin-bottom: 54px; }
section h2 {
  margin: 0; font-size: 13px; font-weight: 600; letter-spacing: 0.16em;
  text-transform: uppercase; color: var(--brand);
}
.lede { margin: 8px 0 0; color: var(--soft); font-size: 13px; max-width: 56ch; }
.batch {
  display: grid; grid-template-columns: 128px 1fr; gap: 26px;
  padding: 26px 0; border-top: 1px solid var(--rule);
}
section h2 + .batch, .lede + div > .batch:first-child { margin-top: 4px; }
.batch:last-child { border-bottom: 1px solid var(--rule); }
.when { font-size: 20px; font-weight: 600; letter-spacing: 0; }
.ago { font-size: 11px; letter-spacing: 0.12em; color: var(--soft); margin-top: 4px; }
.tally { font-size: 12px; color: var(--soft); margin-top: 10px; }
.films {
  display: grid; gap: 24px 16px;
  grid-template-columns: repeat(auto-fill, minmax(136px, 1fr));
}
.films.flat { margin-top: 22px; }

.film { min-width: 0; }
.poster {
  position: relative; aspect-ratio: 2/3; background: var(--sunk);
  border-radius: 2px; overflow: hidden; margin-bottom: 9px;
}
.poster img { width: 100%; height: 100%; object-fit: cover; display: block; }
.poster .none {
  display: flex; align-items: flex-end; height: 100%; padding: 10px;
  color: var(--soft); font-size: 12px; line-height: 1.25;
}
.title { font-size: 14px; font-weight: 500; line-height: 1.3; }
.title a { text-decoration: none; }
.title a:hover { color: var(--brand); text-decoration: underline; }
.series { font-size: 12.5px; color: var(--soft); margin-top: 2px; }
/* Each showing is its own link: they are separate tickets, so a card with two
   of them cannot have one destination. */
.show {
  display: block; font-size: 12.5px; color: var(--soft);
  margin-top: 3px; text-decoration: none;
}
.show b { font-weight: 500; color: var(--ink); }
.show:hover b { color: var(--brand); text-decoration: underline; }
.tr { display: inline-block; font-size: 12px; margin-top: 6px; color: var(--soft); }
.tr:hover { color: var(--brand); }

.empty { color: var(--soft); padding: 44px 0; max-width: 52ch; }

.plan-tools { display: flex; align-items: center; gap: 14px; margin: 20px 0 6px; }
#plan-clear {
  font: inherit; font-size: 13px; padding: 7px 14px; cursor: pointer;
  background: transparent; color: var(--ink);
  border: 1px solid var(--rule); border-radius: 2px;
}
#plan-clear:hover { border-color: var(--brand); color: var(--brand); }
.plan-note { font-size: 12.5px; color: var(--soft); }
.month { margin-top: 26px; }
.month h3 {
  margin: 0 0 10px; font-size: 12px; font-weight: 600; letter-spacing: 0.16em;
  text-transform: uppercase; color: var(--brand);
}
.grid7 { display: grid; grid-template-columns: repeat(7, 1fr); gap: 4px; }
.dow-head {
  font-size: 10px; letter-spacing: 0.1em; color: var(--soft); text-align: center;
  padding-bottom: 2px;
}
.cell {
  min-height: 74px; border: 1px solid var(--rule); border-radius: 2px;
  padding: 5px 6px; font-size: 11px; text-align: left; color: var(--ink);
  background: transparent; font-family: inherit; cursor: pointer;
  display: flex; flex-direction: column; gap: 3px; overflow: hidden;
}
.cell.void { border-color: transparent; cursor: default; }
.cell.past { opacity: 0.3; cursor: default; }
.cell .d { font-size: 13px; font-weight: 600; }
.cell .ev { color: var(--brand); line-height: 1.25; }
.cell .more { color: var(--soft); margin-left: 3px; }
.cell.booked { border-color: var(--brand); }
.cell.off { opacity: 0.42; }
.cell.off .d, .cell.off .ev { text-decoration: line-through; }
.cell:not(.void):not(.past):hover { border-color: var(--brand); }

@media (max-width: 640px) {
  .cell { min-height: 58px; font-size: 10px; padding: 4px; }
  .cell .d { font-size: 12px; }
}
footer {
  margin-top: 44px; padding-top: 18px; border-top: 1px solid var(--rule);
  color: var(--soft); font-size: 12.5px; max-width: 66ch;
}

@media (max-width: 640px) {
  .wrap { padding: 24px 16px 60px; }
  .standfirst { font-size: 22px; }
  .bar-in { padding: 12px 16px; gap: 12px; flex-wrap: wrap; }
  h1 { font-size: 17px; }
  .stamp { margin-left: 0; flex-basis: 100%; font-size: 11px; }
  .logo { width: 62px; height: 25px; }
  .logo img { width: 87px; height: 87px; }
  .batch { grid-template-columns: 1fr; gap: 14px; padding: 20px 0; }
  .head { display: flex; align-items: baseline; gap: 10px; }
  .ago, .tally { margin: 0; }
  .tally { margin-left: auto; }
  .films { grid-template-columns: repeat(auto-fill, minmax(104px, 1fr)); gap: 18px 12px; }
}
@media (prefers-reduced-motion: reduce) {
  * { animation: none !important; transition: none !important; }
}
</style>
</head>
<body>

<div class="bar">
  <div class="bar-in">
    <span class="logo"><img src="$logo" alt="Alamo Drafthouse"></span>
    <h1>$masthead</h1>
    <div class="stamp">Checked $stamp</div>
  </div>
</div>

<div class="wrap">

<header>
  <p class="standfirst">$standfirst</p>
  <p class="summary">$summary</p>
  <div class="controls">
    <input type="search" id="q" placeholder="Search a title or a series" autocomplete="off">
    <label class="toggle"><input type="checkbox" id="events"> Special events only</label>
  </div>
  <div class="count" id="count"></div>
</header>

<section id="added-wrap">
  <h2>Newly added</h2>
  <div id="added"></div>
</section>

<section id="soon-wrap">
  <h2>Limited runs ahead</h2>
  <p class="lede">Films screening a handful of times, and programmed specials, soonest
     first. These are the ones that sell out; a wide release playing all month is not
     here.</p>
  <div id="soon"></div>
</section>

<section id="plan-wrap">
  <h2>Booking planner</h2>
  <p class="lede">A limited run is close to a fixed point — miss the dates shown and
     it is gone. Each film sits on its first date; a title marked <b>+1</b> also plays
     that many other days, so it is movable, while a title on its own is your only
     chance. Cross off the days you have taken to see where a film with a month of
     showtimes still fits. Nothing here is saved; it is a scratchpad for while you are
     buying tickets.</p>
  <div class="plan-tools">
    <button type="button" id="plan-clear">Clear</button>
    <span class="plan-note" id="plan-count"></span>
  </div>
  <div id="cal"></div>
</section>

<footer>$footer</footer>
</div>

<script>
const ADDED = $added;
const SOON = $soon;
const ALL_FILMS = [].concat(...ADDED.map(b => b.films), SOON);

const addedEl = document.getElementById('added');
const soonEl = document.getElementById('soon');
const addedWrap = document.getElementById('added-wrap');
const soonWrap = document.getElementById('soon-wrap');
const q = document.getElementById('q');
const eventsOnly = document.getElementById('events');
const count = document.getElementById('count');

const STORE = 'alamo-added';
let prefs = {events: false};
try { Object.assign(prefs, JSON.parse(localStorage.getItem(STORE) || '{}')); } catch (e) {}
eventsOnly.checked = !!prefs.events;

function save() {
  try { localStorage.setItem(STORE, JSON.stringify({events: eventsOnly.checked})); } catch (e) {}
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]
  ));
}

// The date leads, because booking is the point. A run says where it ends; a
// single screening says so, which is most of what this venue programs.
function showLine(sh) {
  const tail = sh.run
    ? sh.shows + ' screenings through ' + sh.run
    : (sh.shows === 1 ? 'one screening' : sh.shows + ' screenings');
  // Date first, because booking is the point; what kind of showing it is rides
  // along on the second line where it does not push the date around.
  return '<a class="show" href="' + esc(sh.url) + '" rel="noopener">' +
    '<b>' + esc(sh.date) + ', ' + esc(sh.time) + '</b><br>' +
    esc(sh.note ? tail + ', ' + sh.note.toLowerCase() : tail) +
    '</a>';
}

function film(f) {
  const art = f.poster
    ? '<img loading="lazy" src="' + esc(f.poster) + '" alt="">'
    : '<div class="none">' + esc(f.title) + '</div>';
  return '<div class="film">' +
    '<div class="poster">' + art + '</div>' +
    '<div class="title"><a href="' + esc(f.url) + '" rel="noopener">' +
      esc(f.title) + '</a></div>' +
    (f.label ? '<div class="series">' + esc(f.label) + '</div>' : '') +
    f.showings.map(showLine).join('') +
    (f.trailer ? '<a class="tr" href="' + esc(f.trailer) + '" rel="noopener">Trailer</a>' : '') +
    '</div>';
}

function section(batches, keep) {
  let html = '', shown = 0, total = 0;
  for (const batch of batches) {
    total += batch.films.length;
    const films = batch.films.filter(keep);
    shown += films.length;
    if (!films.length) continue;
    html += '<div class="batch"><div class="head">' +
      '<div class="when">' + esc(batch.label) + '</div>' +
      (batch.sub ? '<div class="ago">' + esc(batch.sub) + '</div>' : '') +
      '<div class="tally">' + films.length +
        (films.length === 1 ? ' film' : ' films') + '</div>' +
      '</div><div class="films">' + films.map(film).join('') + '</div></div>';
  }
  return {html: html, shown: shown, total: total};
}

function render() {
  const term = q.value.trim().toLowerCase();
  const keep = f => {
    if (eventsOnly.checked && f.tier !== 'event') return false;
    if (!term) return true;
    return (f.title + ' ' + (f.label || '')).toLowerCase().includes(term);
  };

  const a = section(ADDED, keep);
  const soonFilms = SOON.filter(keep);
  const b = {
    html: soonFilms.length ? '<div class="films flat">' + soonFilms.map(film).join('') + '</div>' : '',
    shown: soonFilms.length,
    total: SOON.length,
  };

  // A section with nothing in it is noise, not information -- hide the heading
  // too rather than leaving a title over an empty space.
  addedEl.innerHTML = a.html ||
    '<p class="empty">Nothing new since the last check. The tracker looks every morning.</p>';
  soonEl.innerHTML = b.html;
  soonWrap.hidden = !b.html;

  const shown = a.shown + b.shown, total = a.total + b.total;
  count.textContent = shown === total ? '' : shown + ' of ' + total + ' films';
}

// --- booking planner --------------------------------------------------------
// Built from the data already on the page rather than a third payload: a day is
// spoken for if a one-off screens on it, and everything else is yours to fill.
const TODAY = '$today';
const struck = new Set();

function oneoffDays() {
  const byDay = new Map();
  for (const f of ALL_FILMS) {
    if (!f.oneoff) continue;
    // The earliest date only, marked with how many other days it plays. You
    // will see a film once, so putting Dune on both the 15th and the 17th read
    // as two commitments when it is one -- and the whole point of an advance
    // screening is the first date anyway. The marker is what says the day is
    // movable; the film's card upstairs lists every date.
    //
    // Counted in days, not showings: TENET plays twice on one afternoon and
    // that is still one evening of yours.
    const days = [...new Set(f.showings.map(sh => sh.iso))].sort();
    const first = days[0];
    if (!byDay.has(first)) byDay.set(first, []);
    byDay.get(first).push({title: f.title, others: days.length - 1});
  }
  return byDay;
}

function renderCalendar() {
  const events = oneoffDays();
  const dates = [...events.keys()].sort();
  const wrap = document.getElementById('cal');
  if (!dates.length) { document.getElementById('plan-wrap').hidden = true; return; }

  const start = new Date(TODAY + 'T00:00:00');
  const end = new Date(dates[dates.length - 1] + 'T00:00:00');
  const names = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
  let html = '';

  const cursor = new Date(start.getFullYear(), start.getMonth(), 1);
  while (cursor <= end) {
    const y = cursor.getFullYear(), m = cursor.getMonth();
    const label = cursor.toLocaleDateString('en-GB', {month: 'long', year: 'numeric'});
    const days = new Date(y, m + 1, 0).getDate();
    // Monday-first, which is how a week of cinema reads.
    let lead = (new Date(y, m, 1).getDay() + 6) % 7;
    let cells = names.map(n => '<div class="dow-head">' + n + '</div>').join('');
    for (let i = 0; i < lead; i++) cells += '<div class="cell void"></div>';
    for (let d = 1; d <= days; d++) {
      const iso = y + '-' + String(m + 1).padStart(2, '0') + '-' + String(d).padStart(2, '0');
      const evs = events.get(iso) || [];
      const past = iso < TODAY;
      const cls = ['cell'];
      if (past) cls.push('past');
      if (evs.length) cls.push('booked');
      if (struck.has(iso)) cls.push('off');
      cells += '<' + (past ? 'div' : 'button type="button"') + ' class="' + cls.join(' ') +
        '" data-iso="' + iso + '"' + (past ? '' : ' aria-pressed="' + struck.has(iso) + '"') + '>' +
        '<span class="d">' + d + '</span>' +
        evs.slice(0, 2).map(e => '<span class="ev">' + esc(e.title) +
          (e.others ? '<span class="more">+' + e.others + '</span>' : '') +
          '</span>').join('') +
        (evs.length > 2 ? '<span class="ev">+' + (evs.length - 2) + ' more</span>' : '') +
        '</' + (past ? 'div' : 'button') + '>';
    }
    html += '<div class="month"><h3>' + esc(label) + '</h3>' +
      '<div class="grid7">' + cells + '</div></div>';
    cursor.setMonth(m + 1);
  }
  wrap.innerHTML = html;
  document.getElementById('plan-count').textContent =
    struck.size ? struck.size + (struck.size === 1 ? ' day crossed off' : ' days crossed off') : '';
}

document.getElementById('cal').addEventListener('click', e => {
  const cell = e.target.closest('.cell');
  if (!cell || cell.classList.contains('void') || cell.classList.contains('past')) return;
  const iso = cell.dataset.iso;
  if (struck.has(iso)) struck.delete(iso); else struck.add(iso);
  renderCalendar();
});
document.getElementById('plan-clear').addEventListener('click', () => {
  struck.clear();
  renderCalendar();
});

q.addEventListener('input', render);
eventsOnly.addEventListener('change', () => { save(); render(); });
render();
renderCalendar();
</script>
</body>
</html>
""")


def render_html(added, soon, title, label, market, has_key, since=None, site_url=SITE_URL):
    """Build the page. Data is injected as JSON and rendered client-side."""
    new_count = sum(len(b["films"]) for b in added)
    soon_count = len(soon)
    # Counted over the films actually on the page, not the whole slate: "5 of
    # these" has to mean five of the ones you can see. A film that matched TMDB
    # but has no trailer counts too -- a missing link is a missing link.
    on_page = [f for b in added for f in b["films"]] + list(soon)
    no_trailer = sum(1 for f in on_page if not f["trailer"])
    calendar = f"https://drafthouse.com/{market}?showCalendar=true"

    started = ""
    if since:
        when = dt.date.fromisoformat(since)
        started = f" since {when.day} {when:%B}"

    if new_count or soon_count:
        standfirst = (
            f"<b>{new_count}</b> films newly on sale{started}, and "
            f"<b>{soon_count}</b> limited runs coming up."
        )
    else:
        standfirst = "Nothing new, and nothing on a limited run ahead."

    notes = [
        "Checked every morning. A film earns a place here by being newly on sale or by"
        f" screening no more than {LIMITED_SHOWS} times — a wide release playing all"
        " month is neither.",
        f'For everything currently showing, <a href="{calendar}" rel="noopener">Alamo\'s'
        " own calendar</a> is the place.",
    ]
    notes.append("Artwork is Alamo's own, for the booking they are actually selling.")
    if has_key:
        notes.append(
            'Trailers from <a href="https://www.themoviedb.org/" rel="noopener">TMDB</a>'
            + (f"; {no_trailer} of the {len(on_page)} films here have none, usually"
               " a festival, a livestream or a one-off." if no_trailer else ".")
        )
    else:
        notes.append("No TMDB key was set for this build, so there are no trailer links.")
    notes.append(
        '<a href="https://github.com/txrunn/scripts/tree/main/alamo-drafthouse"'
        ' rel="noopener">How this is built</a>.'
    )
    notes.append(
        "An unofficial personal tracker, not affiliated with or endorsed by Alamo"
        " Drafthouse; their name and mark are theirs."
    )

    # The tab is the only part of this you see without opening it, so it carries
    # the one fact that decides whether to: did anything land today. No number
    # means nothing did.
    today_count = sum(len(b["films"]) for b in added if b["label"] == "Today")
    tab = f"{today_count} new today · {title}" if today_count else title

    # The card title is the tab title without the trailing brand: a preview
    # already shows the site name on its own line, so repeating it there costs
    # room that a film name could have used.
    social = social_tags(
        url=site_url,
        title=tab,
        description=card_description(added, soon, since),
        image=card_image(added, soon),
    )

    return PAGE.substitute(
        page_title=html.escape(f"{tab} · Alamo Drafthouse"),
        social=social,
        masthead=html.escape(title),
        stamp=html.escape(build_stamp()),
        summary=html.escape(recent_summary(added)),
        today=venue_today().isoformat(),
        logo=LOGO,
        standfirst=standfirst,
        footer=" ".join(notes),
        # "</" is escaped because a film title containing "</script>" would
        # otherwise close the block it is embedded in. Alamo writes these
        # titles, so this is untrusted text.
        added=json.dumps(added, ensure_ascii=False).replace("</", "<\\/"),
        soon=json.dumps(soon, ensure_ascii=False).replace("</", "<\\/"),
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
        description="Render what has just gone on sale at Bryant Street.",
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
    # Same name the issues and the phone notifications use, so the whole flow
    # calls this one thing by one name.
    parser.add_argument("--title", default="New at Bryant Street")
    parser.add_argument("--site-url", default=SITE_URL,
                        help="canonical URL, used by the link-preview tags")
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
        print("warning: TMDB_API_KEY is not set -- artwork still comes from Alamo, "
              "but there will be no trailer links", file=sys.stderr)

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

    cards = assemble(films, ledger, cache, args.market,
                     posters=posters_by_slug(presentations),
                     opens=openings_by_slug(presentations))
    added = added_batches(cards)
    soon = upcoming_batches(cards)
    page = render_html(added, soon, args.title, label, args.market,
                       has_key=bool(api_key), since=baseline_date(ledger),
                       site_url=args.site_url)

    out_dir = os.path.expanduser(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "index.html")
    with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(page)

    if api_key:
        save_json(args.cache, cache)

    new_count = sum(len(b["films"]) for b in added)
    soon_count = len(soon)
    print(f"{new_count} newly added + {soon_count} limited runs -> {out_path}")
    print(f"  {looked_up} looked up, {len(missing)} without a TMDB trailer")
    return 0


if __name__ == "__main__":
    sys.exit(main())
