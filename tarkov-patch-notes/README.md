# tarkov-patch-notes

Escape from Tarkov patch notes as an RSS feed and a Discord push. A PatchBot
replacement that covers Tarkov and carries no ads for free games.

## Why three sources

No single source has all the patches.

| Source | What it gives | What it misses |
| --- | --- | --- |
| `escapefromtarkov.com` news API, category **Patch Notes** | Battlestate's own classification, correct titles, 104 patches of history | Patches they don't file under that category |
| Steam app **3932890** | Everything BSG announce on Steam | Patches whose Steam headline isn't patch-shaped |
| Steam **build id** | The client build the store is serving | Any notes at all — it's a bare signal |

Two concrete cases that motivated this:

- The 2026-09-15 hotfix is **"Patch 1.1.5.1"** on the site and
  **"Leagues are live!"** on Steam. Same body, different headline. Filtering
  Steam on the title alone misses it; the site's category catches it.
- **"Technical update"** is a title Battlestate reuse for hotfixes that go out
  on Steam and are *not* in the site's patch category — the 2026-01-14 one
  changed Zubr extraction spots and group transit. There are **10** such posts
  in Steam's history. Only Steam has them.

On the current data that is 40 patches from the site and 10 more that only
Steam has, for 50 in total. So both are polled and merged. The build id is the third signal: it changes
when the patch goes live, which is usually *before* the notes are published,
and it's the only way to see a silent hotfix that never gets notes at all.

## De-duplication

The same patch arriving from both sources must post once. Each item offers a
few identities and shares one with its twin:

1. the version in the title (`Patch 1.1.5.1` → `1.1.5.1`) — survives the two
   sources wording a headline differently;
2. a hash of the opening text, for posts with no version in the title — this
   is what ties `Leagues are live!` to `Patch 1.1.5.1`;
3. the source id, as a floor.

Site entries win, for the better titles. `--verify` shows the merge.

## Outputs

| Path | What it is |
| --- | --- |
| `site/patches.xml` | Patch notes and hotfixes only |
| `site/all.xml` | Every announcement, patches included |
| `site/index.html` | Browsable page: every patch, newest expanded, filter box |
| `ci-state/state.json` | What has already been posted, plus the last build id |

Both feeds carry the full patch text in `<description>` and
`<content:encoded>`, so a reader shows the notes inline.

Discord embeds carry the post's banner: Steam's first screenshot, or the site's
own `thumb` for site entries. Long notes are split across several embeds rather
than truncated.

The page inlines each patch body, so it is a readable archive rather than a
list of links — 50 patches in about 220 KB. Bodies are Battlestate's HTML and
are sanitised before being embedded (no script, iframe, `on*` handler or
`javascript:` URL survives), because we serve them from our own origin.

Published at
`https://txrunn.github.io/scripts/tarkov/`
— see [`pages.yml`](../.github/workflows/pages.yml), which owns the deploy for
the whole repo and watches this workflow by name.

## Setup

1. **Add the webhook.** Discord: *Channel settings → Integrations → Webhooks →
   New Webhook*, copy the URL. GitHub: *Settings → Secrets and variables →
   Actions*, new secret `DISCORD_WEBHOOK_URL`.
2. **Seed it.** Run the workflow once by hand. The first run records the
   current backlog as already-posted and sends nothing, so 41 patches don't
   land in your channel at once. Every run after that posts only new ones.

The feeds build with or without the secret, so step 2 works before step 1.

## Local use

```bash
python3 tarkov_patch_notes.py --verify         # what each source sees, writes nothing
python3 tarkov_patch_notes.py --dry-run        # full run, never calls Discord
python3 tarkov_patch_notes.py --post-existing  # post the backlog instead of seeding
python3 tarkov_patch_notes.py --no-build-ping  # notes only, no client-build pings
python3 tarkov_patch_notes.py --all            # push every announcement, not just patches
python3 -m unittest discover -s . -t .         # offline, no network
```

Standard library only — nothing to install.

## Timing

The poll runs every 5 minutes Mon–Fri 05:00–21:00 UTC, and hourly otherwise.
That is not arbitrary: of the 104 patches on record, 94% landed Mon–Fri, and
**none has ever been published between 22:00 and 04:00 UTC**. Battlestate ship
during Moscow office hours — median 07:00 UTC, Thursday the most common day.
Polling every five minutes through a 3am Sunday would be ~110 runs a day
covering a window that has never once produced a patch.

Both schedules deliberately avoid `:00`. GitHub delays scheduled runs under
load and names the start of every hour as a high-load window, where queued jobs
may be dropped outright — so the passes run at `:02,:07,…` and `:23`.

GitHub's cron floor is 5 minutes and scheduled runs are best-effort, so expect
delivery **5–20 minutes** after publication in-hours, up to an hour outside
them. The build ping often arrives first, since the client updates before the
notes go up. If that isn't tight enough, the script runs unchanged anywhere
with cron; only the state storage and the commit step would need swapping.

## The 60-day rule

GitHub disables scheduled workflows in a **public** repository after 60 days
with no repository activity, and only new commits reset the timer. This
workflow commits whenever a feed changes, which is what keeps it alive — but
patches alone would not be enough. The gap between patches has a median of 26
days and a maximum of 159, and was 62 days as recently as 2025: past the
cutoff. The watcher would have disabled itself mid-drought and gone quiet with
nothing appearing broken.

`site/all.xml` is what prevents that. It tracks every Steam announcement, not
just patches — median two days apart, worst case thirty — so the timer is reset
long before it expires. Dropping that feed to "simplify" would reintroduce the
failure silently.

## Notes

- Steam's news window is fetched 100 deep on purpose. A Steam-only patch ages
  out of a shallower window before it is ever seen — "Technical update" fell
  past item 50 within eight months. The archive keeps whatever has already been
  recorded, but a fresh seed only sees what the request returns.
- `api.steamcmd.net` (the build id) is a third party. A failure there is logged
  and ignored — the patch notes matter more than the ping.
- The site API is undocumented; it was read off the network tab of
  `escapefromtarkov.com/news`. If it moves, Steam still covers most patches and
  `--verify` will show the site returning nothing.
- EFT: Arena has no Steam app and isn't in the site's patch category, so its
  patch notes aren't covered.
