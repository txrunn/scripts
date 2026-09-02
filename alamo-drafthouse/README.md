# Alamo DC Bryant Street — new-film tracker

Tells you which movies have **newly become bookable** at Alamo Drafthouse DC
Bryant Street since you last looked, so you can spend a Season Pass on them
before the good seats go. Re-releases, repertory and one-off events all count.

It prints **nothing** when nothing is new. Silence means "no news", which is what
makes it safe to run daily from a scheduler.

Python 3.9+, standard library only. Nothing to install.

---

## The three commands you actually need

```powershell
# Is anything new? (this is the whole job)
python alamo_new_films.py

# Is the API still shaped the way the parser expects?
python alamo_new_films.py --verify

# Start over from scratch
Remove-Item -Recurse -Force state
python alamo_new_films.py          # re-seeds a baseline, reports nothing
```

On macOS/Linux use `./alamo_new_films.py` and `rm -rf state`.

**First run seeds a baseline.** It records everything currently bookable and
reports none of it — you already know what's on the calendar today. From then on
you only hear about additions.

---

## What a report looks like

```
3 new films bookable at DC Bryant Street (2026-08-25)

SPECIAL EVENTS — one-offs, book early  (1)

  Taxi Driver  [Film Club]
    first showtime  Sat Aug 29, 4:00 PM   (1 upcoming)
    https://drafthouse.com/dc-metro-area/show/film-club-taxi-driver

REGULAR RELEASES  (1)

  Avengers: Doomsday
    first showtime  Thu Sep 10, 8:00 PM   (12 upcoming)
    https://drafthouse.com/dc-metro-area/show/avengers-doomsday

ADVANCE SCREENINGS — check for merch; the film returns, the merch does not  (1)

  The Dog Stars  [Advance screening]
    first showtime  Fri Sep 4, 6:00 PM   (1 upcoming)
    https://drafthouse.com/dc-metro-area/show/advance-screening-the-dog-stars

JSON report: ...\alamo-drafthouse\state\reports\new-films-2026-08-25.json
```

Same data lands in `state/reports/new-films-<date>.json` with a `tier` and
`event_label` on each film, if you ever want to pipe it somewhere.

---

## How it works

```
GET drafthouse.com/s/mother/v2/schedule/market/dc-metro-area
        │
        ├─ data.presentations[]   one per film/event  (slug, show.title, eventType, superTitle)
        └─ data.sessions[]        one per showtime    (cinemaId, presentationSlug, showTimeClt, status)
        │
   keep sessions at cinemaId 1101 (Bryant), in the future, status ONSALE, not hidden
        │
   group into films, classify each into a tier
        │
   compare against the ledger  →  report only slugs never seen before
        │
   append new slugs to the ledger
```

### What counts as "bookable"

A film is reported when it first has a session at Bryant that is **in the
future**, **on sale** (`status == "ONSALE"` — not `SOLDOUT`, not `PAST`), and
**not hidden**.

The on-sale check is the point of the whole thing: Alamo lists sessions before
tickets go live, and you want to hear about a film the day you can *buy*, not the
day it's announced. A sold-out show is no use to a Season Pass either — if it
later opens seats, the new `ONSALE` session brings it in then.

### What counts as "new"

The ledger is **cumulative** — every slug ever seen bookable at Bryant — not a
yesterday-vs-today snapshot. Three consequences:

- **Skipping days is safe.** Laptop off, travel, a failed run: the addition is
  still caught next time rather than swallowed by the gap.
- **A film that drops off and comes back is not re-reported.** You knew about it.
- **Removals never surface**, by construction.

Each film is reported exactly once, the first time it becomes bookable.

Note that Alamo gives the same film a different slug per presentation, so
*Rear Window* appears both as `rear-window` and `film-club-rear-window`. Those
are genuinely different bookable events, so both are reported.

### Priority tiers

| Tier | What | Why there |
|---|---|---|
| 1. Special events | Film Club, Movie Party, EPIC Sunday, Terror Tuesday, Quote-Alongs, anniversaries, Q&As | Run once; seats go early |
| 2. Regular releases | Ordinary showings | The default |
| 3. Advance screenings | Previews, early access, insider screenings | The film returns in a regular run — **but the merch doesn't**, so check rather than skip |

Tier beats showtime: a Film Club screening two months out ranks above a regular
release tomorrow. `--skip-regular` drops tier 2 only, deliberately keeping tier 3.

Classification uses Alamo's own fields, in order:

1. **`eventType`** — an object on every special event, `null` on every ordinary
   release. A declared field, so a series Alamo invents next month is picked up
   with no code change here.
2. **`superTitle.superTitle`** supplies the label ("EPIC Sunday", "Film Club"),
   but only for something already established as an event — on a regular film
   it's a merchandising shelf. *Teenage Sex and Death at Camp Miasma* carries
   `superTitle: "Drafthouse Recommends"` with `eventType: null` and stays a
   regular release.
3. **Slug markers** (`film-club-…`, `mean-girls-movie-party`) as fallback if
   `eventType` is ever missing. On the live slate they agree with `eventType` on
   all 67 films.
4. **Advance markers** last, so an Anime Night *sneak peek* stays an event rather
   than being written off as a preview.

Only slug and title are matched against markers — `eventType.description` is
boilerplate shared by every event ("a movie-inspired feast, an interactive
party") and would otherwise supply labels from one shared sentence.

Live slate splits **32 events / 32 regular / 3 advance**. Add a new series in
`PRIORITY_MARKERS` (one line) if the fallback ever needs it.

---

## Where state lives

```
alamo-drafthouse/
  alamo_new_films.py
  state/                          ← gitignored, safe to delete
    dc-bryant-street.json         ← the ledger
    reports/
      new-films-2026-08-25.json   ← one per day something was added
  ci-state/
    dc-bryant-street.json         ← the GitHub Action's separate ledger (committed)
```

Paths are absolute and derived from the script's own location, so a scheduled
task running from any working directory writes here. Override with `--state` /
`--report-dir`.

> **Run local *or* CI, not both.** They keep independent ledgers, so each will
> report films the other already told you about.

---

## Scheduling

### GitHub Actions (recommended — nothing to keep running)

`.github/workflows/alamo-new-films.yml` runs daily and **opens an issue** when
films are added. GitHub emails you about issues in your own repo, so there's no
SMTP secret to manage.

Steps, in order — the names are what you see in the Actions UI:

| Step | Does what | Skipped when |
|---|---|---|
| Run tests | Guards against a bad commit reaching a scheduled run | never |
| Fetch schedule and check the API contract | One fetch, reused below; fails the run on schema drift | never |
| Find newly bookable films | The actual diff | never |
| Decide whether to alert | Sets the `new` output | never |
| Write the run summary | Renders the summary you read | never (`if: always()`) |
| Open an issue for the new films | The alert | nothing new |
| Commit the updated ledger | Persists state | nothing new |
| Push a phone notification | Optional ntfy push, see below | nothing new; no `NTFY_URL` |

A quiet run leaves the last three greyed out and the summary reads "Nothing new
today" — followed by the counts, so a healthy quiet day is distinguishable from
a run that silently stopped working.

The issue body is real markdown (`--format markdown`): a table per tier, film
titles linked to all showtimes for that film, and a date span rather than a
single showtime for films with a run. Dumping the terminal report into a code
fence would give monospace text and dead links.

Try it by hand first: **[Actions tab](https://github.com/txrunn/scripts/actions)
→ "Alamo new films" → Run workflow**, then read the job summary. That first run
also proves the runner can reach drafthouse.com.

Things that will bite you eventually:

- **Cron is UTC and ignores DST.** `0 13 * * *` is 9am EDT, 8am EST.
- **Runs can be delayed** up to an hour under GitHub load.
- **60 days of repo inactivity disables scheduled workflows.** The ledger commit
  resets that clock, but only on days something is added. GitHub emails a warning
  first; any commit re-arms it.
- State persists by being **committed back** to `ci-state/`, since runners keep
  nothing. Side benefit: `git log alamo-drafthouse/ci-state/` is a record of when
  each film went on sale.

### Local

**Windows Task Scheduler** (note: it doesn't mail you stdout, so either check
`state\reports\` or redirect to a log):

```powershell
schtasks /create /tn "Alamo new films" /sc daily /st 09:00 ^
  /tr "python C:\path\to\alamo-drafthouse\alamo_new_films.py"
```

**cron** — mails you stdout, and stdout is empty on quiet days, so you only hear
from it when there's something to book:

```
0 9 * * * /full/path/to/alamo_new_films.py
```

---

## Phone notifications

The workflow opens an issue and GitHub emails you about issues in your own repo,
so out of the box you already get an alert with nothing to configure. For a push
on your phone instead of an email you have to go and read, add
[ntfy](https://ntfy.sh) — free, open source, no account, one `curl` from the job.

1. Install ntfy on your phone
   ([iOS](https://apps.apple.com/us/app/ntfy/id1625396347),
   [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)).
2. Invent a topic name. **The topic name is the whole password** — anyone who
   guesses it can read your alerts and push things at you — so don't pick
   `alamo`:

   ```powershell
   python -c "import secrets; print('alamo-' + secrets.token_hex(8))"
   ```

3. Subscribe to that topic in the app.
4. Add the full URL as a repo secret named `NTFY_URL` — **Settings → Secrets and
   variables → Actions → New repository secret**:

   ```
   https://ntfy.sh/alamo-<the-hex-from-step-2>
   ```

Test it: **Actions → "Alamo new films" → Run workflow** with **test_alert**
ticked. The push should arrive in a second or two.

What arrives:

- **The title** — the counts, led by special events.
- **The new film titles**, so you can tell from the lock screen whether this is
  worth getting up for without opening anything.
- **Tapping the notification** opens the issue.
- **Up to three buttons** going straight to those films' showtimes on
  drafthouse.com, skipping the issue entirely. ntfy allows three; a day that
  adds one film gets a single button labelled "Showtimes".

Days with a special event go out at high priority, since those are the ones that
sell out. Everything else arrives at normal priority.

The list is capped at 10 titles with a `+N more` line, because nobody reads a
60-line notification and ntfy turns a body over 4KB into an attachment. A normal
day adds a handful of films and is never truncated; a `test_alert` run reports
the whole slate and always is. The issue has the full list either way.

It is one bare title per line — no bullets, no padding. **ntfy renders Markdown
in the web app only**, so on a phone a `- ` prefix is a literal hyphen rather
than a list, and iOS shows roughly four lines before truncating. Every character
spent on decoration is a character of film title that wraps or disappears.

Button labels are ASCII-folded and clipped to ~27 characters, because ntfy
splits the `Actions` header on commas and semicolons and film titles are full of
both. The notification body keeps the real title — only the button is folded.

Two deliberate choices in how it fails:

- **No secret, no problem.** The step prints that it skipped and the run stays
  green. The issue and its email are unaffected, so this is purely additive.
- **A broken push fails the run,** which GitHub emails you about. It runs last,
  after the ledger is committed, so a notifier outage can't strand state or make
  tomorrow re-report today's films. A push that quietly stopped working would be
  the same silent failure the rest of this workflow exists to prevent.

### Why a curl and not a notification setting

GitHub can push issue alerts through its own mobile app, and that is a fine
zero-setup option — but it welds the alert to GitHub. The `curl` doesn't. The
entire notification path is one step, one secret, and no forge-specific API:

- **Moving to Forgejo or GitLab** — copy the step and the secret. Forgejo Actions
  reads the same YAML; on GitLab it's the same `curl` in a `script:`.
- **Moving to a cron box** — pipe the report into the same `curl`.
- **Self-hosting ntfy** — point `NTFY_URL` at your own server. If you turn auth
  on, which is most of the reason to self-host, add an `NTFY_TOKEN` secret
  holding an access token (`ntfy token add <user>`) and it goes out as a bearer
  header. No token, no header — so ntfy.sh keeps working untouched.

  Two things to know before you move:

  - **iOS needs an upstream relay.** A self-hosted server has no APNs
    credentials, so set
    [`upstream-base-url`](https://docs.ntfy.sh/config/#ios-instant-notifications)
    to `https://ntfy.sh`. It forwards a poll request carrying only the message
    ID; the phone then fetches the real message from your server, so the content
    never touches ntfy.sh. Without it notifications still arrive, just late —
    sometimes hours late.
  - **Auth makes the topic name stop mattering.** On ntfy.sh the topic name is
    the only thing between your phone and anyone who guesses it. Behind a server
    with `auth-default-access: deny-all`, it is just a name.

---

## Options

| Flag | Purpose |
|---|---|
| `--verify` | Check the API contract and exit. |
| `--list-cinemas` | Every cinema in the market with its id and session count. |
| `--cinema-id ID` | Pin the cinema id, skipping name matching. |
| `--match TEXT` | Cinema name/slug substring (default `bryant`). |
| `--market SLUG` | Market slug (default `dc-metro-area`). |
| `--skip-regular` | Drop ordinary releases; keep events *and* advance screenings. (`--events-only` is an alias.) |
| `--status STATUS` | Status counted as bookable, repeatable (default `ONSALE`; `ALL` ignores status). |
| `--force-report` | On a cold start, list the whole slate instead of seeding quietly. |
| `--format text\|markdown` | Report style. `text` (default) for a terminal; `markdown` for tables and clickable links in a GitHub issue. |
| `--dry-run` | Report, but write no state and no JSON. |
| `--from-file PATH` | Read a saved response instead of fetching. Offline testing. |
| `--dump PATH` | Save the raw response for debugging. |
| `--json-always` | Write the JSON report even when nothing is new. |
| `--state PATH` | Ledger location (default `state/`). |
| `--report-dir PATH` | JSON report directory (default `state/reports/`). |

Exit `0` on a successful run whether or not anything is new; `1` on a network,
schema, or config error, with a message on stderr — so a broken scheduled run
reaches you instead of looking like a quiet day.

---

## When it breaks

**Start here:**

```powershell
python alamo_new_films.py --verify --dump raw.json
```

`--verify` checks every field the script depends on and prints a PASS/FAIL line
each. Real output from the live feed:

```
data.presentations present             PASS  72 items
data.sessions present                  PASS  662 items
presentation has slug                  PASS  72/72
presentation has show.title            PASS  72/72
session has presentationSlug           PASS  662/662
session has cinema identifier          PASS  cinemaId, 662/662
session has parseable showtime         PASS  662/662
target cinema resolvable               PASS  key=1101 "DC Bryant Street"
session statuses at target             INFO  ONSALE=220, PAST=1, SOLDOUT=1
hidden entries excluded                INFO  1 session(s), 0 presentation(s)
event classification                   INFO  32 special event(s), 32 regular, 3 advance
upcoming bookable sessions at target   PASS  218 sessions, 67 distinct films
every session slug resolves to a film  PASS  67/67
schedule window                        INFO  2026-08-25 .. 2026-12-24 (121 days out)
```

| Symptom | Likely cause |
|---|---|
| `could not find 'presentations' and 'sessions'` | Alamo changed the response shape. `raw.json` has the truth; fix `extract()`. |
| `could not find a cinema matching 'bryant'` | Cinema list moved. Run `--list-cinemas`, then pass `--cinema-id`. |
| Reports nothing for days | Cross-check the film count in `--verify` against the [calendar](https://drafthouse.com/dc-metro-area?showCalendar=true). If they disagree, the filter is wrong. |
| Reports films you knew about | Two ledgers in play (local *and* CI), or `state/` was deleted. |
| A status other than `ONSALE` is purchasable | `--status ONSALE --status THE_NEW_ONE` |

Schema tolerance is deliberately confined to four small functions in
`alamo_new_films.py`: `extract()`, `presentation_title()`, `session_cinema_key()`
and `parse_showtime()`. A feed change should only ever need edits there.

---

## Tests

```powershell
python -m unittest discover -s . -t . -v
```

72 tests, no network — every one drives the script through `--from-file`.

Coverage: diff logic (new / seen / removed / returning / gaps between runs);
bookability (`SOLDOUT`, `PAST`, announced-but-not-on-sale and hidden entries all
excluded; a film reported the day it flips to `ONSALE`; a sold-out film reported
when seats reopen); filtering (other cinemas, past showtimes, unparseable
timestamps); tiering (real event slugs and `eventType` objects from the live
slate, shelf labels not promoting, prose never supplying a label, tier beating
showtime, `--skip-regular` keeping advance screenings); outputs; and robustness
(an unknown schema fails loudly rather than looking like a quiet day, corrupt
state, ambiguous cinema match, the several shapes `market` can take, absolute
state paths), and markdown rendering (links not fences, the series column
dropped when unused, a clock time only for single-date films, pipes in titles
escaped).

`testdata/sample_schedule.json` mirrors field names observed on the live feed,
and a test asserts they stay present so the fixture can't drift into fiction.

---

## The API

Undocumented and unversioned. Everything below was observed on the live
`dc-metro-area` feed, not from documentation.

```
GET https://drafthouse.com/s/mother/v2/schedule/market/dc-metro-area
```

`data` holds `presentations`, `sessions`, `market`, `presentationAttributes`,
`sessionAttributes`, `formats`, `agePolicies`, `queues`, `relatedPresentations`.

**Session** — `cinemaId` `sessionId` `presentationSlug` `status` `showTimeClt`
`showTimeUtc` `businessDateClt` `cinemaTimeZoneName` `formatSlug`
`sessionAttributeSlugs` `agePolicySlug` `screenNumber` `reservedSeating`
`isHidden` `ticketTypes*Count`

**Presentation** — `slug` `show` `event` `eventType` `superTitle` `formatSlugs`
`presentationAttributeSlugs` `associatedPresentationSlugs` `openingDateClt`
`primaryCollectionSlug` `isHidden`

| Field | Observed values |
|---|---|
| `status` | `ONSALE`, `SOLDOUT`, `PAST` |
| `presentationAttributeSlugs` | `first-run`, `alamo-exclusive`, `advance-sales`, `family-friendly` — **that's the whole vocabulary** |
| `eventType` | `null` on regular films; `{id, slug: "special-event", title, description, collectionSlug}` on events |
| `superTitle` | `null`, or `{superTitle: "EPIC Sunday", type: "COLLECTION", slug: "epic-sunday"}` |
| Bryant's `cinemaId` | `1101` |

Watch out: `advance-sales` sits on **ordinary** first-run films (Avengers:
Doomsday, Dune: Part Three). It means pre-sale is open, *not* that it's an
advance screening.

Poke at it by hand — in PowerShell, `curl` is an alias for `Invoke-WebRequest`
and will prompt you for a `Uri:`, so use `curl.exe` or:

```powershell
$d = (Invoke-RestMethod 'https://drafthouse.com/s/mother/v2/schedule/market/dc-metro-area').data
$d.presentations[0] | ConvertTo-Json -Depth 4
$d.sessions | Group-Object status | Select-Object Name, Count
```

---

## Caveats

- **Season Pass eligibility is not modeled.** Alamo excludes some special events
  and premium formats from the pass and the feed doesn't reliably flag which.
  Everything bookable is reported; you judge.
- **Merch can't be detected.** The four attribute slugs above are the entire
  vocabulary — nothing about giveaways or posters. That's why advance screenings
  are ranked last but never dropped.
- **Showtimes are treated as cinema-local wall time**, correct for a DC-only
  tracker and avoids a timezone dependency.
- **Report links go to a film's page, not a checkout.** `SHOW_URL` builds
  `drafthouse.com/<market>/show/<slug>`, confirmed working against a live slug;
  Alamo also serves the shorter `drafthouse.com/show/<slug>`. The page lists
  every showtime for that film, so booking is one more click.
- **The retry/backoff path has only been exercised by a blocked-egress failure**,
  not a flaky or slow network. It retried three times with backoff and exited 1
  cleanly, which is the behaviour that matters.

---

## Links

**Alamo**
- [DC Metro calendar](https://drafthouse.com/dc-metro-area?showCalendar=true) — what the tracker is watching
- [DC Bryant Street](https://drafthouse.com/dc-metro-area/theater/dc-bryant-street)
- [The Highbinder (Bryant St bar)](https://drafthouse.com/dc-metro-area/theater-bar/dc-bryant-street)
- [Schedule endpoint](https://drafthouse.com/s/mother/v2/schedule/market/dc-metro-area) — raw JSON

**This repo**
- [Actions runs](https://github.com/txrunn/scripts/actions) — history and job summaries
- [Issues](https://github.com/txrunn/scripts/issues) — where new-film alerts land
- `git log alamo-drafthouse/ci-state/` — when each film went on sale

**Notifications**
- [ntfy docs](https://docs.ntfy.sh/publish/) — the push service, if you set `NTFY_URL`
- [ntfy for iOS](https://apps.apple.com/us/app/ntfy/id1625396347)

**Reference**
- [GitHub scheduled workflows](https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows#schedule) — cron syntax and the delay/inactivity rules
- [AlamoShowtimes.spoon](https://github.com/jamtur01/AlamoShowtimes.spoon) — where the endpoint shape was originally confirmed
- [spikegrobstein/alamo-drafthouse-movie-list](https://github.com/spikegrobstein/alamo-drafthouse-movie-list) — older `feeds.drafthouse.com` API
- [jroyal/drafthouse-api](https://github.com/jroyal/drafthouse-api)
