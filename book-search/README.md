# book-search

Search for a book by title (and optionally author). Audiobooks come from
AudioBookBay as magnet links; ebooks come from Library Genesis as direct
downloads. Connect a TorBox account to send either straight to TorBox.

```bash
./book_search.py --serve
```

That opens a search page on `http://127.0.0.1:8765/`. Stdlib only, so there is
nothing to install.

---

## The search page

Pick **Audiobooks** or **Ebooks**, type a title, and optionally an author.

- **Audiobooks** each get **Copy magnet link** and **Open in torrent app**.
- **Ebooks** each get **Download**, which fetches the file from LibGen.

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

**Ebook links are fetched when you click.** LibGen's direct links carry a key
that expires, so the server asks LibGen for a fresh one at the moment you click
**Download** or **Send to TorBox**.

**"Ready now" means cached.** TorBox has already downloaded that exact torrent,
so adding it is instant and doesn't depend on anyone still seeding it.

---

## Sources

[open-slum.org](https://open-slum.org/) tracks which shadow libraries are up.
Of the ones it lists:

| Library | Here? | Why |
|---|---|---|
| Library Genesis | Yes | Search and downloads answer plain requests. |
| Anna's Archive | No | Search is only on its bot-protected domains; the mirrors that are up only serve download links. |
| Z-Library | No | Needs an account, and every domain is bot-protected. |
| Sci-Hub | No | Papers, not books. |

---

## When it breaks

- **AudioBookBay changed domain.** Set `ABB_DOMAIN=audiobookbay.<new-tld>`.
- **LibGen is down.** It tries `libgen.li`, `libgen.bz` and `libgen.vg` in
  turn. Set `LIBGEN_MIRRORS=host1,host2` if those all move; open-slum lists the
  live ones.
- **No results, or no magnets.** A site's HTML changed. The regexes at the top
  of the script are the only things that read it, and `test_book_search.py`
  holds the markup they expect.

---

## Adding another library

`SOURCES` in the script maps a kind of search to a `search` function, which
turns a query into `Result`s, and a `finish` function for anything that needs
a second request per result. Results go through the same filtering and page.

```bash
./test_book_search.py     # offline; nothing touches the network
```
