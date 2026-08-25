#!/usr/bin/env python3
"""Report movies newly bookable at Alamo Drafthouse DC Bryant Street.

Run it daily. It prints nothing when nothing new has been added, so a cron job
only mails you on days that actually matter. Re-releases and repertory count the
same as first-run films -- no filtering by release date or format.

State lives in a cumulative ledger of every presentation slug ever seen bookable
at the target cinema, so skipping a day (or a week) never loses an addition, and
a film that drops off and comes back is not re-reported. Removals are never
reported by construction.

Stdlib only. Python 3.9+.
"""

import argparse
import collections
import datetime as dt
import gzip
import io
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request

# --- Configuration -----------------------------------------------------------

MARKET_SLUG = "dc-metro-area"

# Case-insensitive substring matched against a cinema's slug/name to pick the
# theater. "bryant" matches the "dc-bryant-street" / "DC Bryant Street" cinema.
# Once you know the numeric id (run --list-cinemas), pin it in CINEMA_ID to skip
# the lookup entirely.
CINEMA_MATCH = "bryant"
CINEMA_ID = None

# A session counts as bookable only in these statuses. Observed at DC Bryant
# Street: ONSALE, SOLDOUT, and PAST. Only ONSALE is purchasable -- a sold-out
# show is no use to a Season Pass -- so the other two stay out. --verify prints
# the live distribution, and --status widens the set without a code change.
BOOKABLE_STATUSES = frozenset({"ONSALE"})

# Report tiers, best first. Special events run once and their Season Pass seats
# go early, so they lead. Advance screenings trail: a new release with a preview
# almost always gets a regular run, so missing the preview costs little.
TIER_EVENT = 0
TIER_REGULAR = 1
TIER_ADVANCE = 2

TIER_HEADINGS = {
    TIER_EVENT: "SPECIAL EVENTS — one-offs, book early",
    TIER_REGULAR: "REGULAR RELEASES",
    # Ranked last because a regular run normally follows, but not dismissable:
    # advance screenings sometimes come with free merch, and the later regular
    # run does not substitute for that. The feed exposes no merch signal, so the
    # heading says to check rather than pretending the tier is redundant.
    TIER_ADVANCE: "ADVANCE SCREENINGS — check for merch; the film returns, the merch does not",
}

# Matched as substrings against slug + title + Alamo's structured event fields.
# Order matters: the first hit wins, so specific series come before the generic
# words they contain. Every marker here was observed in the live DC Bryant
# Street slate.
#
# Note on merch: the feed has no merch signal to match. Its whole attribute
# vocabulary is first-run / alamo-exclusive / advance-sales / family-friendly,
# so free-poster and giveaway screenings are indistinguishable here and are
# caught only insofar as they are also a named series.
PRIORITY_MARKERS = (
    ("film-club", "Film Club"),
    ("movie-party", "Movie Party"),
    ("quote-along", "Quote-Along"),
    ("sing-along", "Sing-Along"),
    ("terror-tuesday", "Terror Tuesday"),
    ("weird-wednesday", "Weird Wednesday"),
    ("epic-sunday", "Epic Sunday"),
    ("special-event", "Special event"),
    ("queer-film-theory", "Queer Film Theory 101"),
    ("sad-girl-cinema-club", "Sad Girl Cinema Club"),
    ("cinema-club", "Cinema Club"),
    ("anime-night", "Anime Night"),
    ("live-q-a", "Live Q&A"),
    ("q-and-a", "Live Q&A"),
    ("fan-screening", "Fan screening"),
    ("fan-event", "Fan event"),
    ("anniversary", "Anniversary"),
    ("catvideofest", "Special event"),
    ("feast", "Feast"),
    ("brunch", "Brunch"),
)

# Checked only when nothing above matched, so an Anime Night sneak peek still
# ranks as an event rather than a preview.
ADVANCE_MARKERS = (
    "advance-screening",
    "early-access",
    "sneak-peek",
    "insider-screening",
    "preview-screening",
)

SCHEDULE_URL = "https://drafthouse.com/s/mother/v2/schedule/market/{market}"
SHOW_URL = "https://drafthouse.com/{market}/show/{slug}"

# State lives next to the script, not in a per-user config dir, so everything the
# tracker owns sits in one directory you can see, back up, or delete. `state/` is
# gitignored. Both are overridable with --state / --report-dir.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE = os.path.join(SCRIPT_DIR, "state", "dc-bryant-street.json")
DEFAULT_REPORT_DIR = os.path.join(SCRIPT_DIR, "state", "reports")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
TIMEOUT = 20
RETRIES = 3


class SchemaError(Exception):
    """The API responded, but not in a shape we know how to read."""


class FetchError(Exception):
    """We could not get a response at all."""


# --- Fetching ----------------------------------------------------------------


def fetch(url, dump_path=None):
    """GET `url` and parse JSON, retrying transient failures.

    Retries network errors and 5xx with exponential backoff. A 4xx is not
    retried -- it means the endpoint or market slug is wrong, and hammering it
    will not change that.
    """
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/plain,*/*",
            "Accept-Encoding": "gzip",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )

    last_error = None
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                raw = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                break
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                raise FetchError(f"HTTP {exc.code} from {url}") from exc
            last_error = f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = str(exc)

        if attempt < RETRIES - 1:
            delay = 2 ** (attempt + 1)
            print(
                f"fetch failed ({last_error}); retrying in {delay}s",
                file=sys.stderr,
            )
            time.sleep(delay)
    else:
        raise FetchError(f"giving up on {url} after {RETRIES} attempts: {last_error}")

    if dump_path:
        with open(os.path.expanduser(dump_path), "wb") as handle:
            handle.write(raw)
        print(f"raw response written to {dump_path} ({len(raw)} bytes)", file=sys.stderr)

    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchemaError(f"response from {url} is not JSON: {exc}") from exc


def load_payload(args):
    """Get the schedule payload from the network or a saved file."""
    if args.from_file:
        with open(os.path.expanduser(args.from_file), "rb") as handle:
            raw = handle.read()
        if args.dump:
            with open(os.path.expanduser(args.dump), "wb") as handle:
                handle.write(raw)
        return json.loads(raw.decode("utf-8"))
    return fetch(SCHEDULE_URL.format(market=args.market), dump_path=args.dump)


# --- Parsing -----------------------------------------------------------------


def extract(payload):
    """Pull (presentations, sessions) out of the payload.

    Tolerates the list living at the top level or under "data". Anything else is
    a hard error naming the keys we actually found -- an Alamo API change should
    fail loudly, never as a quiet "nothing new" you would wrongly trust.
    """
    for scope in (payload.get("data"), payload):
        if not isinstance(scope, dict):
            continue
        presentations = scope.get("presentations")
        sessions = scope.get("sessions")
        if isinstance(presentations, list) and isinstance(sessions, list):
            return presentations, sessions

    found = sorted(payload) if isinstance(payload, dict) else type(payload).__name__
    nested = payload.get("data") if isinstance(payload, dict) else None
    detail = f"; data has keys {sorted(nested)}" if isinstance(nested, dict) else ""
    raise SchemaError(
        "could not find 'presentations' and 'sessions' in the response "
        f"(top-level keys: {found}{detail}). "
        "Re-run with --dump raw.json and inspect the payload."
    )


def presentation_title(presentation):
    """Best available human title for a presentation."""
    show = presentation.get("show")
    if isinstance(show, dict) and show.get("title"):
        return show["title"]
    for key in ("title", "name", "presentationTitle"):
        if presentation.get(key):
            return presentation[key]
    return presentation.get("slug", "<untitled>")


def index_presentations(presentations):
    """Map presentation slug -> title."""
    index = {}
    for presentation in presentations:
        slug = presentation.get("slug")
        if slug:
            index[slug] = presentation_title(presentation)
    return index


def hidden_presentation_slugs(presentations):
    """Slugs Alamo has flagged as hidden -- not meant to be surfaced."""
    return frozenset(
        p["slug"] for p in presentations if p.get("slug") and p.get("isHidden") is True
    )


def _text_values(value):
    """Flatten a field into its string parts.

    Alamo's shapes here are inconsistent -- superTitle is an object
    ({"superTitle": "Drafthouse Recommends", "type": ..., "slug": ...}),
    eventType is a bare string or null, presentationAttributeSlugs is a list --
    so unwrap all three rather than assuming any one of them.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [v for v in value.values() if isinstance(v, str)]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def _slug_text(*values):
    """Lowercase hyphenated haystack built from the given strings."""
    return re.sub(r"[^a-z0-9]+", "-", " ".join(v for v in values if v).lower())


def classification_text(presentation):
    """Identity text for a presentation: just its slug and title.

    Deliberately narrow. eventType's prose is boilerplate shared by every
    special event -- its description reads "a movie-inspired feast, an
    interactive party" -- so folding it in would let words from that one
    sentence decide a film's label. superTitle is excluded too: it doubles as a
    merchandising shelf ("Drafthouse Recommends"), which is not a series.
    """
    return _slug_text(presentation.get("slug", ""), presentation_title(presentation))


def event_type_of(presentation):
    """Alamo's declared event category, or None for an ordinary showing.

    Non-null on every special event in the live feed and null on every regular
    release, which makes it the primary signal -- the slug markers below only
    have to supply a nicer label and cover anything this field misses.
    """
    event_type = presentation.get("eventType")
    if isinstance(event_type, dict) and (event_type.get("slug") or event_type.get("title")):
        return event_type
    if isinstance(event_type, str) and event_type.strip():
        return {"title": event_type, "slug": event_type}
    return None


def super_title_name(presentation):
    """Alamo's display name for the collection a presentation sits in.

    Only meaningful as a series name when the presentation is actually an event
    -- for an ordinary film this is a shelf like "Drafthouse Recommends".
    """
    super_title = presentation.get("superTitle")
    if isinstance(super_title, dict):
        name = super_title.get("superTitle")
    else:
        name = super_title
    return name.strip() if isinstance(name, str) and name.strip() else None


def marker_label(presentation):
    """Series name inferred from the slug, for feeds where eventType is absent."""
    haystack = classification_text(presentation)
    for marker, label in PRIORITY_MARKERS:
        if marker in haystack:
            return label
    return None


def classify(presentation):
    """Sort a presentation into a priority tier. Returns (tier, label).

    Special events run once and their Season Pass seats go early, so they lead.
    Advance screenings trail: a new release with a preview gets a regular run
    anyway, so missing the preview costs little.

    eventType decides first because Alamo populates it on every special event
    and leaves it null on every ordinary release -- a declared field beats any
    inference from naming. superTitle then supplies the nicer label ("EPIC
    Sunday" over "Special Event"), but only for something already established as
    an event: on a regular film it is a merchandising shelf, not a series.

    The slug markers run next so that a feed with eventType missing still sorts
    correctly, and they sit above the advance check on purpose -- a one-off
    Anime Night sneak peek is an event, not a preview made redundant by a
    regular run that will never come.
    """
    event_type = event_type_of(presentation)
    if event_type:
        label = super_title_name(presentation) or marker_label(presentation)
        return TIER_EVENT, label or event_type.get("title") or "Special event"

    series = marker_label(presentation)
    if series:
        return TIER_EVENT, series

    haystack = classification_text(presentation)
    for marker in ADVANCE_MARKERS:
        if marker in haystack:
            return TIER_ADVANCE, "Advance screening"

    return TIER_REGULAR, None


def index_classifications(presentations):
    """Map presentation slug -> (tier, label)."""
    return {
        p["slug"]: classify(p) for p in presentations if p.get("slug")
    }


def session_cinema_key(session):
    """The value a session uses to identify its cinema, plus which field it came from."""
    for key in ("cinemaId", "cinemaSlug", "cinemaid", "cinema_id"):
        if session.get(key) is not None:
            return key, str(session[key])
    return None, None


def parse_showtime(value):
    """Parse a showTimeClt-style timestamp into a naive local datetime.

    Alamo's "Clt" timestamps are cinema-local wall time. We compare them against
    naive local now(), which is right for a DC-only tracker and avoids pulling in
    a tz database.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1]
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=None)


def _normalize_cinema(cinema):
    """Coerce one cinema-ish dict into {id, slug, name}, or None if it isn't one."""
    if not isinstance(cinema, dict):
        return None
    identifier = cinema.get("id", cinema.get("cinemaId", cinema.get("cinemaid")))
    slug = cinema.get("slug") or cinema.get("cinemaSlug") or ""
    name = cinema.get("name") or cinema.get("cinemaName") or cinema.get("title") or ""
    if identifier is None and not slug:
        return None
    return {"id": str(identifier) if identifier is not None else slug, "slug": slug, "name": name}


def _cinema_list_from(scope):
    """A cinemas-style list hanging directly off `scope`, normalized. None if absent."""
    if not isinstance(scope, dict):
        return None
    for key in ("cinemas", "theaters", "theatres", "locations"):
        listing = scope.get(key)
        if isinstance(listing, list) and listing:
            cinemas = [c for c in (_normalize_cinema(item) for item in listing) if c]
            if cinemas:
                return cinemas
    return None


def collect_cinemas(payload, sessions):
    """Every cinema we can see, as a list of {id, slug, name} dicts.

    `market` is a list in the live feed, holding either cinemas directly or
    market objects that contain them, so both are handled -- nested lists first,
    since a market wrapper has a slug of its own and would otherwise be mistaken
    for a cinema. Failing all that we fall back to whatever identifiers the
    sessions themselves carry. `--list-cinemas` reports what was actually found.
    """
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    scopes = [data, payload]

    for scope in scopes:
        found = _cinema_list_from(scope)
        if found:
            return found

    for scope in scopes:
        market = scope.get("market") if isinstance(scope, dict) else None
        if isinstance(market, dict):
            found = _cinema_list_from(market)
            if found:
                return found
        elif isinstance(market, list):
            nested = []
            for entry in market:
                nested.extend(_cinema_list_from(entry) or [])
            if nested:
                return nested
            direct = [c for c in (_normalize_cinema(item) for item in market) if c]
            if direct:
                return direct

    seen = {}
    for session in sessions:
        _, key = session_cinema_key(session)
        if key is None or key in seen:
            continue
        seen[key] = {
            "id": key,
            "slug": session.get("cinemaSlug", ""),
            "name": session.get("cinemaName", ""),
        }
    return list(seen.values())


def resolve_cinema(cinemas, sessions, explicit_id=None, match=CINEMA_MATCH):
    """Decide which cinema key identifies our theater.

    Returns (key, label). The key is compared against session_cinema_key values.
    """
    session_keys = {key for _, key in map(session_cinema_key, sessions) if key}

    if explicit_id is not None:
        key = str(explicit_id)
        if session_keys and key not in session_keys:
            raise SchemaError(
                f"--cinema-id {key} matches no session in this response. "
                f"Run --list-cinemas to see valid ids."
            )
        label = next(
            (c["name"] or c["slug"] for c in cinemas if c["id"] == key and (c["name"] or c["slug"])),
            key,
        )
        return key, label

    needle = match.lower()
    hits = [
        cinema
        for cinema in cinemas
        if needle in (cinema["slug"] or "").lower() or needle in (cinema["name"] or "").lower()
    ]
    if len(hits) == 1:
        hit = hits[0]
        label = hit["name"] or hit["slug"] or hit["id"]
        # The cinema list and the sessions do not have to agree on which
        # identifier they use, so pick whichever of the two the sessions
        # actually key on rather than trusting the list's `id`.
        for candidate in (hit["id"], hit["slug"]):
            if candidate and candidate in session_keys:
                return candidate, label
        if not session_keys:
            return hit["id"], label
        raise SchemaError(
            f"found cinema '{label}' (id={hit['id']}, slug={hit['slug'] or '-'}), but no "
            f"session references it. Sessions key on: {sorted(session_keys)[:8]}. "
            f"Pin the right one with --cinema-id."
        )
    if len(hits) > 1:
        names = ", ".join(f"{c['id']}={c['name'] or c['slug']}" for c in hits)
        raise SchemaError(
            f"'{match}' matched multiple cinemas ({names}). "
            f"Pin one with --cinema-id."
        )
    raise SchemaError(
        f"could not find a cinema matching '{match}'. "
        f"Run --list-cinemas to see what this market returns, then pass --cinema-id."
    )


def is_bookable(session, statuses=None):
    """Can you actually buy a ticket to this session right now?

    You want to hear about a film the day it goes on sale, not the day it is
    announced, so a session only counts when its status is on-sale and it is not
    hidden. Sessions with no status at all are counted -- absence of the field is
    not evidence that it cannot be booked.
    """
    if session.get("isHidden") is True:
        return False
    allowed = statuses or BOOKABLE_STATUSES
    if "ALL" in allowed:
        return True
    status = session.get("status")
    if status is None:
        return True
    return str(status).upper() in allowed


def upcoming_films(
    sessions,
    titles,
    cinema_key,
    now=None,
    statuses=None,
    hidden_slugs=frozenset(),
    classifications=None,
):
    """Films with at least one future, bookable session at `cinema_key`.

    Returns slug -> {"title", "first_showtime", "session_count", "tier", "label"}.
    """
    classifications = classifications or {}
    now = now or dt.datetime.now()
    films = {}
    unparseable = 0

    for session in sessions:
        _, key = session_cinema_key(session)
        if key != cinema_key:
            continue
        slug = session.get("presentationSlug") or session.get("slug")
        if not slug or slug in hidden_slugs:
            continue
        if not is_bookable(session, statuses):
            continue
        showtime = parse_showtime(session.get("showTimeClt") or session.get("showTimeUtc"))
        if showtime is None:
            unparseable += 1
            continue
        if showtime < now:
            continue

        tier, label = classifications.get(slug, (TIER_REGULAR, None))
        film = films.setdefault(
            slug,
            {
                "title": titles.get(slug, slug),
                "first_showtime": showtime,
                "session_count": 0,
                "tier": tier,
                "label": label,
                "showtimes": [],
            },
        )
        film["session_count"] += 1
        film["showtimes"].append(showtime)
        if showtime < film["first_showtime"]:
            film["first_showtime"] = showtime

    for film in films.values():
        film["showtimes"].sort()

    if unparseable:
        print(
            f"warning: skipped {unparseable} session(s) with an unreadable showtime",
            file=sys.stderr,
        )
    return films


# --- State -------------------------------------------------------------------


def load_ledger(path):
    """Read the seen-ledger. Missing file -> None (triggers baseline mode)."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as exc:
        raise SchemaError(
            f"state file {path} is unreadable ({exc}). "
            f"Delete it to start a fresh baseline."
        ) from exc

    seen = data.get("seen")
    if not isinstance(seen, dict):
        raise SchemaError(f"state file {path} has no 'seen' object. Delete it to reset.")
    return seen


def save_ledger(path, seen):
    """Write the ledger atomically so an interrupted run cannot corrupt it."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "version": 1,
        "updated": dt.datetime.now().isoformat(timespec="seconds"),
        "seen": seen,
    }
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=os.path.dirname(path), delete=False
    )
    try:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.close()
        os.replace(handle.name, path)
    except BaseException:
        handle.close()
        os.unlink(handle.name)
        raise


# --- Output ------------------------------------------------------------------


def format_showtime(showtime):
    """'Sat Oct 31, 7:30 PM'.

    Built by hand rather than with strftime's %-I / %#I, which are
    platform-specific -- glibc rejects %#I and Windows rejects %-I.
    """
    hour = showtime.hour % 12 or 12
    meridiem = "AM" if showtime.hour < 12 else "PM"
    return f"{showtime.strftime('%a %b')} {showtime.day}, {hour}:{showtime:%M} {meridiem}"


def sort_key(item):
    """Best tier first, then soonest showtime -- the order you should act in."""
    slug, film = item
    return (film.get("tier", TIER_REGULAR), film["first_showtime"], slug)


def format_report(new_films, market, label):
    """Human-readable report, grouped by tier. Only called when there is news."""
    lines = [
        f"{len(new_films)} new film{'s' if len(new_films) != 1 else ''} "
        f"bookable at {label} ({dt.date.today().isoformat()})",
    ]

    current_tier = None
    for slug, film in sorted(new_films.items(), key=sort_key):
        tier = film.get("tier", TIER_REGULAR)
        if tier != current_tier:
            current_tier = tier
            count = sum(1 for f in new_films.values() if f.get("tier", TIER_REGULAR) == tier)
            lines.append("")
            lines.append(f"{TIER_HEADINGS[tier]}  ({count})")
            lines.append("")

        heading = film["title"]
        if film.get("label"):
            heading += f"  [{film['label']}]"
        lines.append(f"  {heading}")
        lines.append(
            f"    first showtime  {format_showtime(film['first_showtime'])}"
            f"   ({film['session_count']} upcoming)"
        )
        lines.append(f"    {SHOW_URL.format(market=market, slug=slug)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def format_when(film):
    """When you can see it.

    A single date gets its clock time, because that is the whole offer. A run
    gets its span and no time -- quoting one showtime's time next to a date
    range implies every screening is at that hour.
    """
    days = sorted({s.date() for s in film.get("showtimes") or [film["first_showtime"]]})
    if len(days) == 1:
        return format_showtime(film["first_showtime"])
    first, last = days[0], days[-1]
    return f"{first:%a %b} {first.day} – {last:%a %b} {last.day}"


def format_report_markdown(new_films, market, label):
    """Rich markdown for a GitHub issue: real headings, tables, clickable links.

    The plain-text report is built for a terminal, and pasting it into a fenced
    block gives you monospace text with dead links -- the opposite of useful in
    something you are meant to act on.
    """
    count = len(new_films)
    lines = [
        f"**{count} new film{'s' if count != 1 else ''}** newly bookable at "
        f"{label} — {dt.date.today():%a %d %b %Y}",
        "",
    ]

    headings = {
        TIER_EVENT: "### ⭐ Special events\n\nOne-offs — seats go early.",
        TIER_REGULAR: "### Regular releases",
        TIER_ADVANCE: (
            "### Advance screenings\n\n"
            "The film returns in a regular run, but merch does not — worth a look."
        ),
    }

    for tier in (TIER_EVENT, TIER_REGULAR, TIER_ADVANCE):
        tier_films = {s: f for s, f in new_films.items() if f.get("tier", TIER_REGULAR) == tier}
        if not tier_films:
            continue
        lines += [headings[tier], ""]
        show_series = any(f.get("label") for f in tier_films.values())
        header = "| Film | Series | When | Shows |" if show_series else "| Film | When | Shows |"
        lines.append(header)
        lines.append("|---|---|---|---|" if show_series else "|---|---|---|")
        for slug, film in sorted(tier_films.items(), key=sort_key):
            url = SHOW_URL.format(market=market, slug=slug)
            title = film["title"].replace("|", "\\|")
            cells = [f"**[{title}]({url})**"]
            if show_series:
                cells.append((film.get("label") or "—").replace("|", "\\|"))
            cells += [format_when(film), str(film["session_count"])]
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")

    # Nothing issue-specific here: this same markdown is also used for the
    # Actions run summary, where a line about closing an issue makes no sense.
    lines += [
        f"Titles link to all showtimes for that film."
        f" · [Full DC Metro calendar](https://drafthouse.com/{market}?showCalendar=true)",
    ]
    return "\n".join(lines) + "\n"


def write_report_json(directory, new_films, market, label):
    """Write the diff as JSON. Returns the path written."""
    directory = os.path.expanduser(directory)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"new-films-{dt.date.today().isoformat()}.json")
    payload = {
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "market": market,
        "cinema": label,
        "count": len(new_films),
        "films": [
            {
                "slug": slug,
                "title": film["title"],
                "first_showtime": film["first_showtime"].isoformat(),
                "upcoming_sessions": film["session_count"],
                "tier": {TIER_EVENT: "event", TIER_REGULAR: "regular", TIER_ADVANCE: "advance"}[
                    film.get("tier", TIER_REGULAR)
                ],
                "event_label": film.get("label"),
                "url": SHOW_URL.format(market=market, slug=slug),
            }
            for slug, film in sorted(new_films.items(), key=sort_key)
        ],
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    return path


# --- Modes -------------------------------------------------------------------


def mode_list_cinemas(payload, sessions):
    cinemas = collect_cinemas(payload, sessions)
    if not cinemas:
        print("no cinemas found in this response", file=sys.stderr)
        return 1
    counts = {}
    for session in sessions:
        _, key = session_cinema_key(session)
        if key:
            counts[key] = counts.get(key, 0) + 1
    print(f"{'ID':<12} {'SLUG':<28} {'NAME':<28} SESSIONS")
    for cinema in sorted(cinemas, key=lambda c: c["id"]):
        print(
            f"{cinema['id']:<12} {cinema['slug'] or '-':<28} "
            f"{cinema['name'] or '-':<28} {counts.get(cinema['id'], 0)}"
        )
    return 0


def mode_verify(payload, presentations, sessions, args):
    """Check every field this script depends on, and say so line by line."""
    results = []

    def check(name, ok, detail=""):
        results.append((name, "PASS" if ok else "FAIL", detail))
        return ok

    check("data.presentations present", bool(presentations), f"{len(presentations)} items")
    check("data.sessions present", bool(sessions), f"{len(sessions)} items")

    with_slug = sum(1 for p in presentations if p.get("slug"))
    check("presentation has slug", with_slug == len(presentations), f"{with_slug}/{len(presentations)}")

    with_title = sum(
        1 for p in presentations if isinstance(p.get("show"), dict) and p["show"].get("title")
    )
    check(
        "presentation has show.title",
        with_title == len(presentations),
        f"{with_title}/{len(presentations)}",
    )

    with_pslug = sum(1 for s in sessions if s.get("presentationSlug") or s.get("slug"))
    check("session has presentationSlug", with_pslug == len(sessions), f"{with_pslug}/{len(sessions)}")

    keys_used = {key for key, _ in map(session_cinema_key, sessions) if key}
    with_cinema = sum(1 for s in sessions if session_cinema_key(s)[1] is not None)
    check(
        "session has cinema identifier",
        with_cinema == len(sessions),
        f"{'/'.join(sorted(keys_used)) or 'none'}, {with_cinema}/{len(sessions)}",
    )

    showtimes = [parse_showtime(s.get("showTimeClt") or s.get("showTimeUtc")) for s in sessions]
    parseable = [t for t in showtimes if t is not None]
    check(
        "session has parseable showtime",
        len(parseable) == len(sessions),
        f"{len(parseable)}/{len(sessions)}",
    )

    cinemas = collect_cinemas(payload, sessions)
    try:
        cinema_key, label = resolve_cinema(cinemas, sessions, args.cinema_id, args.match)
        check("target cinema resolvable", True, f"key={cinema_key} \"{label}\"")
    except SchemaError as exc:
        check("target cinema resolvable", False, str(exc))
        cinema_key, label = None, None

    if cinema_key is not None:
        at_target = [s for s in sessions if session_cinema_key(s)[1] == cinema_key]
        statuses = {}
        for session in at_target:
            statuses[str(session.get("status", "<none>"))] = (
                statuses.get(str(session.get("status", "<none>")), 0) + 1
            )
        results.append(
            (
                "session statuses at target",
                "INFO",
                ", ".join(f"{name}={count}" for name, count in sorted(statuses.items()))
                + f" (counted as bookable: {sorted(args.status or BOOKABLE_STATUSES)})",
            )
        )
        hidden_sessions = sum(1 for s in at_target if s.get("isHidden") is True)
        hidden_films = hidden_presentation_slugs(presentations)
        results.append(
            (
                "hidden entries excluded",
                "INFO",
                f"{hidden_sessions} session(s), {len(hidden_films)} presentation(s)",
            )
        )

        films = upcoming_films(
            sessions,
            index_presentations(presentations),
            cinema_key,
            statuses=args.status,
            hidden_slugs=hidden_films,
            classifications=index_classifications(presentations),
        )
        tiers = collections.Counter(f["tier"] for f in films.values())
        results.append(
            (
                "event classification",
                "INFO",
                f"{tiers[TIER_EVENT]} special event(s), {tiers[TIER_REGULAR]} regular, "
                f"{tiers[TIER_ADVANCE]} advance screening(s)",
            )
        )
        total = sum(f["session_count"] for f in films.values())
        check(
            "upcoming bookable sessions at target",
            bool(films),
            f"{total} sessions, {len(films)} distinct films",
        )
        titles = index_presentations(presentations)
        unresolved = [slug for slug in films if slug not in titles]
        check(
            "every session slug resolves to a film",
            not unresolved,
            f"{len(films) - len(unresolved)}/{len(films)}"
            + (f" -- unresolved: {unresolved[:5]}" if unresolved else ""),
        )

    if parseable:
        lo, hi = min(parseable), max(parseable)
        days = (hi.date() - dt.date.today()).days
        results.append(
            ("schedule window", "INFO", f"{lo.date()} .. {hi.date()} ({days} days out)")
        )

    width = max(len(name) for name, _, _ in results)
    for name, status, detail in results:
        print(f"{name:<{width}}  {status}  {detail}".rstrip())

    failed = [name for name, status, _ in results if status == "FAIL"]
    if failed:
        print(f"\n{len(failed)} check(s) failed. Re-run with --dump to inspect the payload.", file=sys.stderr)
        if presentations:
            print(f"sample presentation keys: {sorted(presentations[0])}", file=sys.stderr)
        if sessions:
            print(f"sample session keys: {sorted(sessions[0])}", file=sys.stderr)
        return 1

    print("\nAll checks passed -- the parser matches the live API.")
    return 0


# --- Entry point -------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        description="Report movies newly bookable at Alamo Drafthouse DC Bryant Street.",
        epilog="Prints nothing when nothing is new, so cron only mails you on interesting days.",
    )
    parser.add_argument("--market", default=MARKET_SLUG, help=f"market slug (default: {MARKET_SLUG})")
    parser.add_argument(
        "--match", default=CINEMA_MATCH, help=f"cinema name/slug substring (default: {CINEMA_MATCH})"
    )
    parser.add_argument("--cinema-id", default=CINEMA_ID, help="pin the cinema id, skipping the lookup")
    parser.add_argument(
        "--state", default=DEFAULT_STATE, help="ledger path (default: state/ next to this script)"
    )
    parser.add_argument(
        "--report-dir",
        default=DEFAULT_REPORT_DIR,
        help="JSON report dir (default: state/reports/ next to this script)",
    )
    parser.add_argument("--verify", action="store_true", help="check the API contract and exit")
    parser.add_argument("--list-cinemas", action="store_true", help="list cinemas in this market and exit")
    parser.add_argument("--dump", metavar="PATH", help="save the raw response for debugging")
    parser.add_argument("--from-file", metavar="PATH", help="read a saved response instead of fetching")
    parser.add_argument("--dry-run", action="store_true", help="report but do not write state or JSON")
    parser.add_argument("--json-always", action="store_true", help="write the JSON report even when empty")
    parser.add_argument(
        "--force-report", action="store_true", help="on the first run, list the whole slate instead of seeding quietly"
    )
    parser.add_argument(
        "--format",
        choices=("text", "markdown"),
        default="text",
        help="report style: plain text for a terminal (default), or markdown with "
        "tables and links for a GitHub issue",
    )
    parser.add_argument(
        "--skip-regular",
        "--events-only",
        dest="skip_regular",
        action="store_true",
        help="drop ordinary releases, keeping special events and advance screenings "
        "(advance screenings are kept because they may carry merch)",
    )
    parser.add_argument(
        "--status",
        action="append",
        metavar="STATUS",
        help=f"session status counted as bookable, repeatable "
        f"(default: {' '.join(sorted(BOOKABLE_STATUSES))}). "
        f"Use --status ALL to ignore status entirely.",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    state_path = os.path.expanduser(args.state)
    if args.status:
        args.status = frozenset(s.upper() for s in args.status)

    try:
        payload = load_payload(args)
        presentations, sessions = extract(payload)

        if args.list_cinemas:
            return mode_list_cinemas(payload, sessions)
        if args.verify:
            return mode_verify(payload, presentations, sessions, args)

        cinemas = collect_cinemas(payload, sessions)
        cinema_key, label = resolve_cinema(cinemas, sessions, args.cinema_id, args.match)
        films = upcoming_films(
            sessions,
            index_presentations(presentations),
            cinema_key,
            statuses=args.status,
            hidden_slugs=hidden_presentation_slugs(presentations),
            classifications=index_classifications(presentations),
        )
        seen = load_ledger(state_path)
    except (FetchError, SchemaError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    baseline = seen is None
    seen = seen or {}
    new_films = {slug: film for slug, film in films.items() if slug not in seen}
    if args.skip_regular:
        new_films = {s: f for s, f in new_films.items() if f["tier"] != TIER_REGULAR}

    # The ledger records every new film regardless of --events-only, so toggling
    # the flag never resurfaces a title the previous run already showed you.
    today = dt.date.today().isoformat()
    for slug, film in films.items():
        if slug not in seen:
            seen[slug] = {"title": film["title"], "first_seen": today}

    if baseline and not args.force_report:
        # Everything looks new on a cold start. Seed silently rather than
        # dumping the whole current slate as if it were news.
        if not args.dry_run:
            save_ledger(state_path, seen)
        print(
            f"Baseline established: {len(films)} film(s) now tracked at {label}. "
            f"Future runs report only additions."
        )
        print(f"State: {state_path}")
        return 0

    if new_films:
        render = format_report_markdown if args.format == "markdown" else format_report
        sys.stdout.write(render(new_films, args.market, label))

    if not args.dry_run:
        if new_films or args.json_always:
            path = write_report_json(args.report_dir, new_films, args.market, label)
            # A local convenience only. In markdown the output is a document
            # destined for an issue body, and a filesystem path from a
            # throwaway runner has no meaning to whoever reads it.
            if new_films and args.format == "text":
                print(f"JSON report: {path}")
        save_ledger(state_path, seen)

    return 0


if __name__ == "__main__":
    sys.exit(main())
