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
import datetime as dt
import gzip
import io
import json
import os
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

SCHEDULE_URL = "https://drafthouse.com/s/mother/v2/schedule/market/{market}"
SHOW_URL = "https://drafthouse.com/{market}/show/{slug}"

DEFAULT_STATE = "~/.local/state/alamo-drafthouse/dc-bryant-street.json"
DEFAULT_REPORT_DIR = "~/.local/state/alamo-drafthouse/reports"

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
        "Re-run with --dump /tmp/raw.json and inspect the payload."
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


def collect_cinemas(payload, sessions):
    """Every cinema we can see, as a list of {id, slug, name} dicts.

    Prefers an explicit cinema list in the payload; falls back to whatever
    identifiers the sessions themselves carry.
    """
    cinemas = []
    scopes = [payload.get("data"), payload]
    for scope in scopes:
        if not isinstance(scope, dict):
            continue
        listing = scope.get("cinemas")
        if isinstance(listing, list) and listing:
            for cinema in listing:
                if not isinstance(cinema, dict):
                    continue
                cinemas.append(
                    {
                        "id": str(cinema.get("id", cinema.get("cinemaId", ""))),
                        "slug": cinema.get("slug", cinema.get("cinemaSlug", "")),
                        "name": cinema.get("name", cinema.get("cinemaName", "")),
                    }
                )
            if cinemas:
                return cinemas

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
        return hit["id"], (hit["name"] or hit["slug"] or hit["id"])
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


def upcoming_films(sessions, titles, cinema_key, now=None):
    """Films with at least one future session at `cinema_key`.

    Returns slug -> {"title", "first_showtime", "session_count"}.
    """
    now = now or dt.datetime.now()
    films = {}
    unparseable = 0

    for session in sessions:
        _, key = session_cinema_key(session)
        if key != cinema_key:
            continue
        slug = session.get("presentationSlug") or session.get("slug")
        if not slug:
            continue
        showtime = parse_showtime(session.get("showTimeClt") or session.get("showTimeUtc"))
        if showtime is None:
            unparseable += 1
            continue
        if showtime < now:
            continue

        film = films.setdefault(
            slug,
            {"title": titles.get(slug, slug), "first_showtime": showtime, "session_count": 0},
        )
        film["session_count"] += 1
        if showtime < film["first_showtime"]:
            film["first_showtime"] = showtime

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


def format_report(new_films, market, label):
    """Human-readable report. Only called when there is something to say."""
    lines = [
        f"{len(new_films)} new film{'s' if len(new_films) != 1 else ''} "
        f"bookable at {label} ({dt.date.today().isoformat()})",
        "",
    ]
    for slug, film in sorted(new_films.items(), key=lambda kv: kv[1]["first_showtime"]):
        showtime = film["first_showtime"]
        lines.append(f"  {film['title']}")
        lines.append(
            f"    first showtime  {format_showtime(showtime)}"
            f"   ({film['session_count']} upcoming)"
        )
        lines.append(f"    {SHOW_URL.format(market=market, slug=slug)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


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
                "url": SHOW_URL.format(market=market, slug=slug),
            }
            for slug, film in sorted(new_films.items(), key=lambda kv: kv[1]["first_showtime"])
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
        films = upcoming_films(sessions, index_presentations(presentations), cinema_key)
        total = sum(f["session_count"] for f in films.values())
        check(
            "upcoming sessions at target",
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
    parser.add_argument("--state", default=DEFAULT_STATE, help=f"ledger path (default: {DEFAULT_STATE})")
    parser.add_argument(
        "--report-dir", default=DEFAULT_REPORT_DIR, help=f"JSON report dir (default: {DEFAULT_REPORT_DIR})"
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
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    state_path = os.path.expanduser(args.state)

    try:
        payload = load_payload(args)
        presentations, sessions = extract(payload)

        if args.list_cinemas:
            return mode_list_cinemas(payload, sessions)
        if args.verify:
            return mode_verify(payload, presentations, sessions, args)

        cinemas = collect_cinemas(payload, sessions)
        cinema_key, label = resolve_cinema(cinemas, sessions, args.cinema_id, args.match)
        films = upcoming_films(sessions, index_presentations(presentations), cinema_key)
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
        return 0

    if new_films:
        sys.stdout.write(format_report(new_films, args.market, label))

    if not args.dry_run:
        if new_films or args.json_always:
            path = write_report_json(args.report_dir, new_films, args.market, label)
            if new_films:
                print(f"JSON report: {path}")
        save_ledger(state_path, seen)

    return 0


if __name__ == "__main__":
    sys.exit(main())
