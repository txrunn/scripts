# book-search

Search for a book by title (and optionally author). Audiobooks come from
AudioBookBay as magnet links. Ebooks come from LibGen and Z-Library together,
each file labelled with the libraries that have it. Connect a TorBox account to
send either straight to TorBox.

```bash
./book_search.py --serve
```

That opens a search page on `http://127.0.0.1:8765/`. Stdlib only, so there is
nothing to install.

---

## The search page

Pick **Audiobooks** or **Ebooks**, type a title, and optionally an author.

- **Audiobooks** each get **Copy magnet link** and **Open in torrent app**.
- **Ebooks** are tagged with the libraries that have that exact file (LibGen,
  Z-Library, or both). Files on LibGen get **Download**. Files only on
  Z-Library get **Open on Z-Library**, since Z-Library needs you signed in to
  download. Every ebook also gets **Find on Anna's Archive**, which opens that
  file's page there.

Each result shows its file type and size on the right, so duplicate uploads can
be compared at a glance. Above the results, **Language**, **File type** and
(for ebooks) **Library** filters are built from what the search found, with
counts. Your choices are remembered separately for audiobooks and ebooks.

**TorBox is per person.** The server holds no TorBox key. Anyone using the page
connects their own account with **Connect TorBox**, either:

- **Sign in with TorBox**: the page shows a code; enter it at
  [tor.box/link](https://tor.box/link) and the page connects itself, or
- **paste an API key** from torbox.app → Settings.

The key is kept in that browser only. It is sent along with TorBox requests,
passed through to TorBox, and never saved or logged by the server. Once
connected, audiobooks TorBox already has are marked **Ready now**, and every
result gets **Send to TorBox**: audiobooks as torrents, ebooks as web
downloads.

Searches go in the URL, so `?kind=books&title=dune&author=herbert` can be
bookmarked.

### Hosting it for other people

`--host 0.0.0.0` makes it reachable from other machines. Because visitors'
keys pass through the server, put it behind HTTPS (a reverse proxy such as
Caddy) before anyone pastes a key into it over a network.

---

## Command line

The command line is for your own account, so it reads `TORBOX_API_KEY` from
the environment.

```bash
./book_search.py "project hail mary" -a weir          # audiobooks
./book_search.py "project hail mary" -a weir --books  # ebooks
./book_search.py "dune" --books -l english -f epub,azw3  # only English EPUB/AZW3
./book_search.py "dune" -a herbert --torbox           # mark each audiobook [cached] / [not cached]
./book_search.py "dune" -a herbert --add 1,3          # send #1 and #3 to TorBox
./book_search.py "dune" --add all --cached-only       # send every audiobook TorBox already has
./book_search.py "dune" --links-only > links.txt      # magnets, or download pages with --books
./book_search.py "dune" --json
./book_search.py "dune" -p 3                          # read 3 pages of results, not 1
```

`--add` takes `all`, single numbers, or ranges: `1,3`, `2-4`.

---

## How it works

**Results are narrowed.** AudioBookBay's search also matches tags and
descriptions, and LibGen's matches series and publishers, so both return books
that only mention what you typed. Only hits whose title contains every word of
your title, and whose title or author contains every word of the author, are
kept. If that would leave nothing, everything is shown instead.

**Magnets are built from the detail page.** Each AudioBookBay result's page has
the info hash and tracker list in plain HTML, so no login is needed. The pages
are fetched in parallel, which is why an audiobook search takes a few seconds.

**Ebooks are merged by file hash.** LibGen and Z-Library are searched at the
same time, and a file both have becomes one result tagged with both. If one
library doesn't answer, the other's results are still shown, with a note saying
which is missing.

**Ebook links are fetched when you click.** LibGen's direct links carry a key
that expires, so the server asks LibGen for a fresh one at the moment you click
**Download** or **Send to TorBox**.

**"Ready now" means cached.** TorBox has already downloaded that exact torrent,
so adding it is instant and doesn't depend on anyone still seeding it.

---

## Sources

[open-slum.org](https://open-slum.org/) tracks which shadow libraries are up.
Of the ones it lists:

| Library | Here? | How |
|---|---|---|
| Library Genesis | Search and download | Plain HTML pages; no account. |
| Z-Library | Search | The website is behind a bot wall, but the API its apps use answers. Downloads need an account, so Z-Library-only files link to their page there. |
| Anna's Archive | Link per file | Its search is behind DDoS-Guard, which a script can't pass; your browser can, so every ebook links to its page there by file hash. Anna's Archive mirrors LibGen and Z-Library, so most files are there. |
| Sci-Hub | No | Papers, not books. |

---

## When it breaks

- **AudioBookBay changed domain.** Set `ABB_DOMAIN=audiobookbay.<new-tld>`.
- **LibGen or Z-Library is down.** Each tries its mirrors in turn: `libgen.li`,
  `libgen.bz`, `libgen.vg`, and `z-lib.gl`, `z-lib.gd`, `library-access.sk`.
  Set `LIBGEN_MIRRORS` or `ZLIB_MIRRORS` (comma-separated) if those all move,
  and `ANNAS_DOMAIN` for Anna's Archive links; open-slum lists the live ones.
- **No results, or no magnets.** A site's HTML changed. The regexes at the top
  of the script are the only things that read it, and `test_book_search.py`
  holds the markup they expect.

---

## Adding another library

An ebook library is one function in `EBOOK_LIBRARIES` that turns a query into
`Result`s with an MD5, so merging, labels and filters work with no other
change. A new kind of search goes in `SOURCES`, with a `search` function and a
`finish` function for anything that needs a second request per result.

```bash
./test_book_search.py     # offline; nothing touches the network
```
