# scripts

general scripts I use for various annoyances

## Published pages

Two of these build a web page. Both live under
**[txrunn.github.io/scripts](https://txrunn.github.io/scripts/)**, assembled and
deployed by [one workflow](.github/workflows/pages.yml).

| Page | What's on it |
|---|---|
| [**New at Bryant Street**](https://txrunn.github.io/scripts/alamo/) | Films that have just gone on sale at Alamo DC Bryant Street, newest morning first, plus the one-off screenings coming up. Deliberately not a copy of Alamo's schedule. |
| [**4K disc inventory**](https://txrunn.github.io/scripts/blu-ray-discs/) | The physical media shelf, with posters, ratings and box-set contents. |

## The scripts

| Script | What it does |
|---|---|
| [`alamo-drafthouse/`](alamo-drafthouse/) | Daily check for movies newly bookable at Alamo DC Bryant Street, so Season Pass seats get caught early. Runs as a [GitHub Action](.github/workflows/alamo-new-films.yml), opens an issue, pushes a phone notification, and rebuilds [the page](https://txrunn.github.io/scripts/alamo/). |
| [`movie-collection/`](movie-collection/) | Builds [a browsable page](https://txrunn.github.io/scripts/blu-ray-discs/) and a verified CSV of the 4K/Blu-ray shelf from a plain-text inventory. Add a line, push, and the [GitHub Action](.github/workflows/movie-collection.yml) looks up only the new titles and rebuilds the page. |
| [`gaming-services-toggle/`](gaming-services-toggle/) | Installs Microsoft Gaming Services and the Xbox Identity Provider before a Forza Horizon session and removes them properly afterwards, instead of leaving two background services running all the time. Fixes "Invalid Gaming Services Detected" on a debloated Windows. |
