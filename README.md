# scripts

general scripts I use for various annoyances

| Script | What it does |
|---|---|
| [`alamo-drafthouse/`](alamo-drafthouse/) | Daily check for movies newly bookable at Alamo DC Bryant Street, so Season Pass seats get caught early. Runs as a [GitHub Action](.github/workflows/alamo-new-films.yml) and opens an issue when something is added. |
| [`movie-collection/`](movie-collection/) | Builds a browsable web page and a verified CSV of the 4K/Blu-ray shelf from a plain-text inventory. Add a line, push, and the [GitHub Action](.github/workflows/movie-collection.yml) looks up only the new titles and rebuilds the page. |
