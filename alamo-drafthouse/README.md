# Alamo DC Bryant Street — new-film tracker

Run it daily; it tells you which movies have **newly become bookable** at Alamo
Drafthouse DC Bryant Street since you last looked. Re-releases and repertory
count the same as first-run films. Removals are never reported.

It prints **nothing** when nothing is new, so a cron job only mails you on days
that actually matter.

Python 3.9+, standard library only. Nothing to install.

## Quick start

```bash
# 1. Confirm the API still looks the way we expect (see "Verifying the API").
./alamo_new_films.py --verify

# 2. Seed the baseline. Prints a one-line summary, no film list.
./alamo_new_films.py

# 3. From here on, run it daily.
./alamo_new_films.py
```

Typical output on a day something was added:

```
2 new films bookable at DC Bryant Street (2026-08-25)

  The Thing (1982)
    first showtime  Fri Oct 31, 7:30 PM   (4 upcoming)
    https://drafthouse.com/dc-metro-area/show/the-thing-1982

  Paddington in Peru
    first showtime  Sun Nov 02, 1:00 PM   (11 upcoming)
    https://drafthouse.com/dc-metro-area/show/paddington-in-peru

JSON report: C:\...\alamo-drafthouse\state\reports\new-films-2026-08-25.json
```

## Scheduling

**cron (macOS / Linux)** — 9am daily. cron mails you stdout, and stdout is empty
on quiet days, so you only hear from it when there is something to book:

```
0 9 * * * /full/path/to/alamo_new_films.py
```

**Windows Task Scheduler:**

```powershell
schtasks /create /tn "Alamo new films" /tr "python C:\path\to\alamo_new_films.py" /sc daily /st 09:00
```

## What counts as "bookable"

A film is reported when it first has at least one session at Bryant that is:

- in the **future**, and
- **on sale** (`status == "ONSALE"`) — not `SOLDOUT`, not `PAST`, and
- **not hidden** (neither the session nor the presentation has `isHidden: true`).

The on-sale check is the point of the whole thing: Alamo lists sessions before
tickets go live, and you want to hear about a film the day you can *buy*, not the
day it appears. A sold-out show is no use to a Season Pass either, so `SOLDOUT`
does not make a film count as newly bookable — if that film later opens more
seats, the new `ONSALE` session brings it in then. `--verify` prints the full status distribution for the market so
you can see whether any status other than `ONSALE` is also purchasable; widen the
set with `--status ONSALE --status SOMETHING_ELSE`, or `--status ALL` to ignore
status entirely.

## Priority order

New films are grouped into three tiers, best first:

1. **Special events** — Film Club, Movie Party, Epic Sunday, Terror Tuesday,
   Quote-Alongs, anniversaries, Q&As, fan events, and anything flagged with free
   merch. These run once and their seats go early, so they lead.
2. **Regular releases** — ordinary showings.
3. **Advance screenings** — last, on the assumption that a new release with a
   preview will get a regular run anyway, so missing the preview costs little.
   Borne out by the live slate: all three advance screenings at Bryant
   (Dune: Part Three, Hope, The Dog Stars) already had a regular counterpart.

Tier beats showtime, so a Film Club screening two months out still ranks above a
regular release tomorrow. `--events-only` drops tiers 2 and 3 entirely.

Classification checks Alamo's structured fields (`superTitle`, `eventType`,
`event`, `presentationAttributeSlugs`) first and falls back to markers in the
slug (`film-club-…`, `mean-girls-movie-party`, `epic-sunday-…`). Ordering within
`PRIORITY_MARKERS` decides ties, so merch outranks the series name. An event that
is *also* a preview stays an event — a one-off Anime Night sneak peek is not made
redundant by a later regular run.

On the live slate this splits 32 special events / 32 regular / 3 advance.
`--verify` prints the same breakdown so you can sanity-check it.

Adding a series Alamo invents later is one line in `PRIORITY_MARKERS`.

**Note:** `--events-only` still records everything in the ledger, so turning the
flag off later will not resurface titles an earlier run already reported.

## How "new" is decided

State is a **cumulative ledger** of every film ever seen bookable at Bryant, not
a yesterday-vs-today snapshot. That matters in three cases a naive diff gets
wrong:

- **You skip days.** Laptop off, travel, cron failure — the addition is still
  caught on the next run instead of being silently swallowed by the gap.
- **A film drops off and comes back.** Not re-reported. You already knew about it.
- **Removals.** Never surface at all, by construction.

A film is reported exactly once: the first time it has at least one *future*
showtime at Bryant.

### Where state lives

Everything the tracker writes goes in `state/`, next to the script:

```
alamo-drafthouse/
  alamo_new_films.py
  state/                          # gitignored
    dc-bryant-street.json         # the ledger
    reports/
      new-films-2026-08-25.json   # one per day something was added
```

The paths are absolute and derived from the script's own location, so a
scheduled task running from an arbitrary working directory still writes here.
Override with `--state` / `--report-dir` if you want them elsewhere.

**To start over**, delete `state/` — the next run seeds a fresh baseline:

```powershell
Remove-Item -Recurse -Force alamo-drafthouse\state
```

```bash
rm -rf alamo-drafthouse/state
```

Earlier versions wrote to `~/.local/state/alamo-drafthouse/`. If you ran one of
those, that directory is now orphaned and safe to delete:

```powershell
Remove-Item -Recurse -Force "$env:USERPROFILE\.local\state\alamo-drafthouse"
```

## Verifying the API

Alamo's schedule endpoint is undocumented and unversioned in practice, so the
script ships with a mode that checks every field it depends on:

```bash
./alamo_new_films.py --verify
```

Real output from the live `dc-metro-area` feed on 2026-08-25:

```
data.presentations present             PASS  72 items
data.sessions present                  PASS  662 items
presentation has slug                  PASS  72/72
presentation has show.title            PASS  72/72
session has presentationSlug           PASS  662/662
session has cinema identifier          PASS  cinemaId, 662/662
session has parseable showtime         PASS  662/662
target cinema resolvable               PASS  key=1101 "DC Bryant Street"
session statuses at target             INFO  ONSALE=220, PAST=1, SOLDOUT=1 (counted as bookable: ['ONSALE'])
hidden entries excluded                INFO  1 session(s), 0 presentation(s)
upcoming bookable sessions at target   PASS  218 sessions, 67 distinct films
every session slug resolves to a film  PASS  67/67
schedule window                        INFO  2026-08-25 .. 2026-12-24 (121 days out)
```

Three lines are worth reading closely:

- **`upcoming bookable sessions at target`** — cross-check that distinct-film
  count against what <https://drafthouse.com/dc-metro-area?showCalendar=true>
  shows for Bryant. If they agree, the cinema filter and the session→film join
  are right.
- **`session statuses at target`** — the observed statuses at Bryant are
  `ONSALE`, `SOLDOUT`, and `PAST`; only `ONSALE` is counted. If a new status
  turns out to be purchasable, widen the set with `--status`.
- **`schedule window`** — how far ahead Alamo publishes, i.e. the maximum lead
  time this tracker can ever give you.

`--verify` exits non-zero on any FAIL and prints the real keys it found, which is
what you need to correct the field names if Alamo changes the API.

### Checking the endpoint by hand

```bash
curl -s 'https://drafthouse.com/s/mother/v2/schedule/market/dc-metro-area' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(list(d.get("data",d)))'
```

In **PowerShell**, `curl` is an alias for `Invoke-WebRequest` and will prompt you
for a `Uri:` instead of running the above. Use one of:

```powershell
curl.exe -s 'https://drafthouse.com/s/mother/v2/schedule/market/dc-metro-area' | python -c "import json,sys; d=json.load(sys.stdin); print(list(d.get('data',d)))"
```

```powershell
(Invoke-RestMethod 'https://drafthouse.com/s/mother/v2/schedule/market/dc-metro-area').data.PSObject.Properties.Name
```

## Options

| Flag | Purpose |
|---|---|
| `--verify` | Check the API contract and exit. |
| `--list-cinemas` | Print every cinema in the market with its id and session count. |
| `--cinema-id ID` | Pin the cinema id, skipping name matching. |
| `--match TEXT` | Cinema name/slug substring (default `bryant`). |
| `--market SLUG` | Market slug (default `dc-metro-area`). |
| `--dump PATH` | Save the raw response for debugging. |
| `--from-file PATH` | Read a saved response instead of fetching. Offline testing. |
| `--dry-run` | Report, but write no state and no JSON. |
| `--force-report` | On a cold start, list the whole current slate instead of seeding quietly. |
| `--status STATUS` | Session status counted as bookable, repeatable (default `ONSALE`; `ALL` ignores status). |
| `--events-only` | Report only special events; skip regular releases and advance screenings. |
| `--json-always` | Write the JSON report even when nothing is new. |
| `--state PATH` | Ledger location (default `state/` next to the script). |
| `--report-dir PATH` | JSON report directory (default `state/reports/`). |

Exit `0` on a successful run (new films or not), `1` on a network, schema, or
config error — with a message on stderr, so a broken cron job reaches you instead
of looking like a quiet day.

## When Alamo changes the API

`--verify` fails, naming the checks that broke and dumping the real keys:

```powershell
python alamo_new_films.py --verify --dump raw.json
```

`raw.json` has the actual payload. The fix is localized to `extract()`,
`presentation_title()`, `session_cinema_key()`, and `parse_showtime()` in
`alamo_new_films.py` — each is a small function whose only job is tolerating one
piece of the schema.

## Tests

```bash
python3 -m unittest discover -s . -t . -v
```

58 tests, no network. Covers the diff logic (new / seen / removed / returning /
gap in runs), bookability (`SOLDOUT`, `PAST`, and announced-but-not-on-sale
excluded, hidden sessions and presentations excluded, a film reported on the day
it flips to `ONSALE`, and a sold-out film reported when seats reopen),
filtering (other cinemas, past showtimes, unparseable timestamps), outputs (JSON
written only when non-empty, `--dry-run` writes nothing), and robustness (unknown
schema fails loudly rather than looking like a quiet day, corrupt state,
ambiguous cinema match, the several shapes `market` can take), priority tiering
(real event slugs from the live slate, structured-field classification, merch
outranking series, tier beating showtime, `--events-only`), and that the default
state paths are absolute and land beside the script.

`testdata/sample_schedule.json` mirrors the field names observed on the live
`dc-metro-area` feed, and one test asserts those fields are still present in the
fixture so it cannot drift back into fiction.

## Caveats

- **Season Pass eligibility is not modeled.** Alamo excludes some special events
  and premium formats from the pass, and the feed does not reliably flag which.
  Everything bookable is reported; you judge.
- Showtimes are treated as cinema-local wall time, which is correct for a
  DC-only tracker and avoids a timezone dependency.

## Running it as a GitHub Action

`.github/workflows/alamo-new-films.yml` runs the check daily and **opens an issue
when films are added**. GitHub emails you about issues in your own repo, so there
are no SMTP secrets to configure and nothing to keep running on your laptop.

The workflow, in order: runs the test suite, fetches the feed once and
`--verify`s it, checks for new films against the CI ledger, writes a job summary,
and — only when something was added — opens an issue and commits the updated
ledger.

### State on ephemeral runners

Runners keep nothing between runs, so the ledger is committed back to the repo at
`alamo-drafthouse/ci-state/dc-bryant-street.json`. That is deliberately *not*
`state/` (which is gitignored for local runs) — the CI ledger and your local one
are independent, so run one or the other, not both, or each will report films the
other already told you about.

The commit happens only on days something was added. The `seen` map is unchanged
otherwise, and a daily no-op commit would bury the real history. A side effect
worth having: `git log alamo-drafthouse/ci-state/` becomes a record of exactly
when each film went on sale.

### Things to know

- **Cron is UTC and ignores DST.** `0 13 * * *` is 9am EDT, 8am EST.
- **Scheduled runs can be delayed** by up to an hour under GitHub load. Fine
  daily, useless for a race.
- **60 days of repo inactivity disables scheduled workflows.** The ledger commit
  resets that clock, but only on days something is added. If Alamo goes two
  months without a new title, GitHub emails a warning before disabling; any
  commit re-arms it.
- **Verify it works before trusting it:** run the workflow by hand from the
  Actions tab (`workflow_dispatch`) and read the job summary. The first manual
  run also confirms the runner can reach `drafthouse.com` — if Alamo's CDN blocks
  datacenter IPs, the `--verify` step fails loudly rather than reporting nothing.
