# book-search

Search AudioBookBay by title (and optionally author), get a magnet link for
each upload, and send the ones you want to TorBox. Works from the command line
or from a small search page on localhost.

```bash
./book_search.py "project hail mary" --author weir
```

```
 1. Project Hail Mary - Andy Weir  [cached]
    M4B | 128 Kbps | 881.82 MB | 14 Nov 2021
    https://audiobookbay.lu/abss/proaject-hail-mary-andy-weir/
    magnet:?xt=urn:btih:ad5fae5ffda056f9f45131045d140326bbafc4dc&dn=...
```

Stdlib only, so there is nothing to install. A TorBox key is optional: without
one you still get magnet links.

```bash
export TORBOX_API_KEY=...        # torbox.app -> Settings
```

---

## The search page

```bash
./book_search.py --serve
```

Opens `http://127.0.0.1:8765/` with a title box, an author box and the results.
Uploads TorBox already has are marked **Ready now**. Each result has a
**Send to TorBox** button and a **Copy magnet link** button. Searches go in
the URL, so `?title=dune&author=herbert` can be bookmarked.

The page only listens on 127.0.0.1. Your TorBox key stays in the server process
and never reaches the browser.

---

## Command line

```bash
./book_search.py "dune" -a herbert                  # title + author
./book_search.py "dune" -a herbert --torbox         # mark each result [cached] / [not cached]
./book_search.py "dune" -a herbert --add 1,3        # send #1 and #3 to TorBox
./book_search.py "dune" --add all --cached-only     # send every result TorBox already has
./book_search.py "dune" --magnets-only > magnets.txt
./book_search.py "dune" --json
./book_search.py "dune" -p 3                        # read 3 pages of results, not 1
```

`--add` takes `all`, single numbers, or ranges: `1,3`, `2-4`.

---

## How it works

**Search results are narrowed.** AudioBookBay's search also matches tags and
descriptions, so "project hail mary" comes back with unrelated books that are
merely tagged with it. Only hits whose title contains every word of the title
you typed (and of the author, if given) are kept. If that would leave nothing,
everything is shown instead.

**Magnets are built from the detail page.** Each result's page has the info
hash and tracker list in plain HTML, so no login is needed. The pages are
fetched in parallel, which is why a search takes a few seconds.

**"Cached" means ready now.** TorBox has already downloaded that exact torrent,
so adding it is instant and doesn't depend on anyone still seeding it.

---

## When it breaks

- **AudioBookBay changed domain.** It moves every so often. Set
  `ABB_DOMAIN=audiobookbay.<new-tld>`; nothing else needs to change.
- **No results, or every result is missing a magnet.** The site's HTML has
  changed. The regexes at the top of the script are the only things that read
  it, and `test_book_search.py` holds the markup they expect.

---

## Adding another library

`SOURCES` in the script maps a name to a function that takes a query and
returns `Result`s. A new library is one more function that fills in the title,
URL and whatever details it has. Results go through the same filtering, TorBox
check and page.

```bash
./test_book_search.py     # offline; nothing touches the network
```
