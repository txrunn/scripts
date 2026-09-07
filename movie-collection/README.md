# Movie collection page

Turns a plain-text list of the discs you own into a browsable web page and a
verified CSV. **You edit one file; everything else is looked up once and cached.**

Adding the two discs you bought last weekend:

```bash
echo "Sinners" >> collection.txt
echo "The Substance" >> collection.txt
git commit -am "Add Sinners and The Substance" && git push
```

That's it. The GitHub Action sees the inventory changed, looks up only those two
titles, rebuilds the page, and commits it back. Nothing else on the shelf is
re-fetched.

Python 3.11+, standard library only. Nothing to install.

---

## The three commands you actually need

```bash
# Are my keys good and are the APIs still shaped the way the parser expects?
python build_collection.py --verify

# Build. Prints what was added; silent when nothing was.
python build_collection.py

# Rebuild the page without touching the network (after a design or override edit)
python build_collection.py --offline --force
```

Open `site/index.html` in a browser.

**A quiet build prints nothing and rewrites nothing.** Silence means "no new
discs", which is what makes it safe to run on every push.

---

## Setup

Two free API keys. The first is required, the second is worth two minutes.

| Key | Get it | Without it |
|---|---|---|
| `TMDB_API_KEY` | [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api) | Nothing works — no titles, years, directors, runtimes, genres or posters |
| `OMDB_API_KEY` | [omdbapi.com/apikey.aspx](https://www.omdbapi.com/apikey.aspx) | Page still builds, but with no IMDb ratings and no Tomatometer |

```bash
export TMDB_API_KEY=...
export OMDB_API_KEY=...
python build_collection.py --verify
```

For the Action, add both under **Settings → Secrets and variables → Actions →
New repository secret**, same names.

---

## Adding a disc

Open `collection.txt`, put the title on its own line under the right section,
save. Order within a section does not matter — the build sorts the shelf itself.

```
[films]
...
Sinners
The Substance
```

If the title is ambiguous, add the theatrical year:

```
It (2017)          <- not the 1990 miniseries
Superman (2025)    <- Gunn, not Donner
Talk to Me (2023)  <- not the 2007 Don Cheadle film
```

The year is a **preference, not a filter**. An exact match is worth a lot, a
year either side is worth a little, and a wrong year merely loses the bonus
rather than losing the film — festival premiere versus wide release disagree
often enough that a hard filter would cause more misses than it prevents.

Box sets go under `[collections]` and are never looked up against TMDB. Putting
one film's runtime and director on a five-disc set would be worse than an empty
row, so they carry only what you write in `overrides.toml`.

### If it picks the wrong film

Pin it, then re-resolve that one title:

```toml
# overrides.toml
["Anna (2019)"]
tmdb_id = 528086
```

```bash
python build_collection.py --refresh "Anna (2019)"
```

---

## What gets filled in, and what doesn't

| Field | Source |
|---|---|
| Title, year, director(s), runtime, genre, poster, overview | TMDB |
| IMDb rating | OMDb |
| `RT_Critic_Percent` — the Tomatometer | OMDb's `Ratings` array |
| Letterboxd link | Resolved from the TMDB id, cached as a real `/film/<slug>/` URL |
| `RT_Audience_Percent` | **You**, in `overrides.toml` |
| `Theatrical_Score` | **You**. Deliberately never filled |
| 4K status, HDR format, disc notes | **You**, in `overrides.toml` |

**No metadata is ever invented.** A blank field always means a source was asked
and declined, never that the script gave up quietly — and the reason is written
into `Source_Notes` on that film's CSV row.

### Why the audience score is manual

No free API exposes it. OMDb's `Ratings` array carries the Tomatometer only, and
TMDB has no Rotten Tomatoes data at all. Substituting IMDb, Metacritic or
TMDB's own vote average would silently answer a different question, so the field
stays empty until you fill it:

```toml
["Hereditary"]
rt_audience = 56
```

Worth doing for the films where the two scores disagree, which on this shelf is
most of the horror — Hereditary is 91% critics against 56% audience, and *The
Witch*, *Us* and *Nope* all run the same way. Several titles run the other
direction: audiences are considerably kinder to *47 Ronin*, *Van Helsing* and
*Waterworld* than critics were.

---

## How the shelf is ordered

```
Director blocks           3+ owned films by one director, alphabetical by
                          surname, each block in release order
   ↓
Alphabetical              everything else, ignoring a leading "The", "A", "An"
   ↓
Collection shelf          box sets, together, not scattered through the As
   ↓
Documentary shelf
```

**The 3+ rule is computed, not configured.** Every build counts directors across
the resolved credits, so buying a third Kubrick creates the Kubrick block on its
own and nobody has to remember to edit a list. It also finds blocks you did not
think to look for — the current shelf produces a Chad Stahelski block from the
three John Wicks, and pulls *Scott Pilgrim* into the Edgar Wright block.

Co-directed films count toward each credited director's total, since that is the
honest reading of "films by that director". A film still appears on the shelf
exactly once: where two blocked directors share a film, it goes to the one with
fewer films, so the smaller block stays intact rather than being hollowed out.

To suppress a block you disagree with, `BLOCK_THRESHOLD` is one constant at the
top of the script — or override the film's `directors` in `overrides.toml`.

---

## Only when something changes

Two independent mechanisms, doing different jobs:

**The metadata cache** (`cache/metadata.json`) is keyed by the exact line you
wrote in `collection.txt`. A title in it is never looked up again. This is what
makes adding two discs cost two lookups instead of two hundred.

**The build ledger** (`cache/build.json`) holds a fingerprint of the inventory,
the overrides and the template version. If it matches, the build exits without
rewriting the page or printing anything.

So:

| You did this | What happens |
|---|---|
| Added a title | That title only is looked up; page rebuilt; addition reported |
| Removed a title | Page rebuilt; removal reported; its cache entry is kept, so re-buying it costs no lookup |
| Edited `overrides.toml` | Page rebuilt from cache. No network at all |
| Renamed a line | Treated as a different disc and re-resolved — that rename is the only signal we get that you meant a different film |
| Nothing | Nothing. No output, no rewrite, exit 0 |

Both files are **committed to the repo**, deliberately. Runners keep nothing
between jobs, so state has to live somewhere, and the repo doubles as an audit
trail: `git log movie-collection/cache/metadata.json` is a record of when each
disc was added.

Force a rebuild with `--force`; ignore the cache entirely with `--refresh-all`.

---

## The Action

`.github/workflows/movie-collection.yml` runs on a **push that touches
`collection.txt`**, not on a schedule — the shelf only changes when you edit it,
so a nightly cron would be 364 no-op runs a year.

| Step | Does what | Skipped when |
|---|---|---|
| Run tests | Guards against a bad commit reaching the build | never |
| Check the inventory and both API contracts | Fails the run on schema drift or a bad key | never |
| Build the page | Looks up new titles, writes `site/` | never |
| Commit the page and the cache | Persists state | nothing changed |
| Write the run summary | What was added, plus the contract check | never |
| Upload the page artifact | For Pages | `PUBLISH_PAGES` unset |

### Publishing to GitHub Pages

Off by default, and the build does not depend on it — `site/index.html` is
committed either way, so you always have the page even if you never turn Pages
on.

1. **Settings → Pages → Source: GitHub Actions**
2. **Settings → Secrets and variables → Actions → Variables →** `PUBLISH_PAGES` = `true`

The page lands at `https://<user>.github.io/scripts/`.

---

## The page

One self-contained HTML file. Posters come from TMDB's CDN; `--embed-posters`
inlines them as data URIs instead, which makes the file portable at the cost of
a few megabytes.

- Search across title, director, genre and year
- Sort by shelf order, title, year, Tomatometer, IMDb rating or runtime
- Filter to one shelf section
- Every title links to its Letterboxd page
- Light and dark, following the system setting

`site/collection.csv` carries the full 22-column schema alongside it.

---

## Options

| Flag | Purpose |
|---|---|
| `--verify` | Check the inventory and both API contracts, then exit. |
| `--offline` | Never fetch. Fails loudly if any title is uncached. |
| `--force` | Rebuild the outputs even when nothing changed. |
| `--refresh TITLE` | Drop one title from the cache and look it up again. Repeatable. |
| `--refresh-all` | Ignore the cache entirely and re-fetch everything. |
| `--embed-posters` | Inline posters as data URIs for a fully portable page. |
| `--format text\|markdown` | Style of the what-changed report. `markdown` for the job summary. |
| `--dry-run` | Do everything except write files. |
| `--title TEXT` | Page title. |
| `--collection` / `--overrides` / `--cache` / `--ledger` / `--out-dir` | Path overrides. |

Exit `0` whether or not anything was added; `1` on a bad inventory, a rejected
key, or a network failure — so a broken run reaches you instead of looking like
a quiet day. API keys are stripped from every error message before it is
printed.

---

## When it breaks

Start here:

```bash
python build_collection.py --verify
```

One `PASS`/`FAIL` line per thing the build depends on:

```
inventory parses                       PASS  109 entries
overrides parse                        PASS  2 entries
overrides match inventory              PASS  all matched
TMDB_API_KEY set                       PASS
TMDB search returns results            PASS  Blade Runner
TMDB movie has runtime                 PASS  117 min
TMDB movie has genres                  PASS  Science Fiction, Drama, Thriller
TMDB movie has imdb_id                 PASS  tt0083658
TMDB credits carry a Director          PASS  Ridley Scott
TMDB movie has poster_path             PASS
Letterboxd id redirect resolves        PASS  https://letterboxd.com/film/blade-runner/
OMDb responds                          PASS  Blade Runner
OMDb has imdbRating                    PASS  8.1
OMDb Ratings carry Rotten Tomatoes     PASS  89%
OMDb carries an RT audience score      INFO  no -- expected; audience scores come from overrides.toml
metadata cache                         INFO  109 cached, 0 to fetch
```

| Symptom | Likely cause |
|---|---|
| `HTTP 401 -- API key rejected` | Wrong key, or TMDB's key not yet activated. |
| `no TMDB match for this title` | Typo, or a title TMDB files differently. Add a year hint, or pin `tmdb_id`. |
| Wrong film matched | Pin `tmdb_id` in `overrides.toml`, then `--refresh "<the exact line>"`. |
| A score is blank | Read that row's `Source_Notes` in the CSV. It says which source declined. |
| `unresolved titles: …` on stderr | Those entries have no TMDB match and are on the page with only their raw title. |
| Page did not rebuild | Nothing changed. `--force`. |
| Everything re-fetched at once | `cache/metadata.json` was deleted or the entries were renamed. |

Schema tolerance is confined to four small functions — `tmdb_search`,
`tmdb_movie`, `directors_of` and `rt_critic_from`. A provider change should only
ever need edits there.

---

## Tests

```bash
python -m unittest discover -s . -t . -v
```

61 tests, no network — every build test runs against a cache seeded in memory.

Coverage: inventory parsing (comments, sections, year hints, a year *in* a title
not being a hint, duplicates rejected); alphabetisation (leading articles,
accents, punctuation, and the exact orderings in this README); the 3+ director
rule (discovered not hardcoded, box sets excluded, co-directed films counted for
each director but shelved once, release order within a block); the change
detection (a second build silent and not rewriting, an addition reported alone,
a removal, an overrides edit rebuilding without the network, `--offline` failing
loudly on an uncached title); the CSV schema (column order, audience score blank
unless overridden, `Theatrical_Score` never substituted, Dolby Vision derived
from HDR format); the HTML (every placeholder substituted, valid JSON payload,
titles escaped, a `</script>` in a title unable to break out); and provider
parsing (Metacritic never read as Rotten Tomatoes, `"N/A"` becoming `None`,
only the `Director` job counted, keys redacted from errors).

---

## Caveats

- **Rotten Tomatoes audience scores are manual.** See above. This is the one
  field the brief asked for that no free source provides.
- **`Theatrical_Score` is always blank.** It was requested as a distinct field
  with no stated source. Rather than quietly filling it with a Tomatometer under
  another name, it is left empty for you to define.
- **The film's year and the disc's year are different things.** `Year` is the
  theatrical release; the UHD release goes in `uhd_year` in `overrides.toml`.
- **Box sets carry no metadata.** By design — see above.
- **Posters are hotlinked to TMDB's CDN** unless you pass `--embed-posters`.
- **This product uses the TMDB API but is not endorsed or certified by TMDB.**

---

## Links

- [TMDB API docs](https://developer.themoviedb.org/docs) · [get a key](https://www.themoviedb.org/settings/api)
- [OMDb API](https://www.omdbapi.com/) · [get a key](https://www.omdbapi.com/apikey.aspx)
- [Letterboxd id redirects](https://letterboxd.com/tmdb/346364/) — `/tmdb/<id>/` and `/imdb/<id>/` both 302 to the film page
- [Actions runs](https://github.com/txrunn/scripts/actions)
