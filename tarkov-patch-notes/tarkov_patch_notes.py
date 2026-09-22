#!/usr/bin/env python3
"""Watch for Escape from Tarkov patch notes and push them to Discord.

Three sources, because no single one catches everything:

  1. escapefromtarkov.com  -- Battlestate's own site API, category "Patch
     Notes". This is the good one: BSG classify the posts themselves, so
     hotfixes land here correctly titled ("Patch 1.1.5.1") even when the
     Steam post for the same content is titled "Leagues are live!".
  2. Steam (app 3932890)   -- catches patches the site does NOT file under
     Patch Notes. "Technical update" on 2026-01-14 shipped real gameplay
     changes, went out on Steam, and is not in the site's patch category.
  3. The Steam build id    -- the client build the store is actually serving.
     It changes when a patch goes live, which is usually before the notes are
     published, and it also catches silent hotfixes that never get notes.

Sources 1 and 2 overlap, so items are de-duplicated on the version number in
the title, falling back to a hash of the opening text when a title carries no
version. See dedupe_keys().

Standard library only. Run with --help for usage.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

APPID = 3932890
GAME = "Escape from Tarkov"

SITE_PATCH_TYPE = 2  # "Patch Notes" in /site/api/v1/news-types/en
SITE_LIST = "https://www.escapefromtarkov.com/site/api/v1/news-list/{type}/en?page={page}"
SITE_POST = "https://www.escapefromtarkov.com/news/id/{id}"
STEAM_NEWS = (
    "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
    "?appid={appid}&count={count}&maxlength=0&format=json"
)
STEAM_POST = "https://store.steampowered.com/news/app/{appid}/view/{gid}"
STEAM_BUILD = "https://api.steamcmd.net/v1/info/{appid}"
SITE_ROOT = "https://www.escapefromtarkov.com"
CLAN_IMAGE_BASE = "https://clan.cloudflare.steamstatic.com/images/"
USER_AGENT = "tarkov-patch-notes (+https://github.com/txrunn/scripts)"
# escapefromtarkov.com sits behind a WAF that returns 403 to a bare urllib
# request from a datacenter IP -- it works from a laptop and fails from a
# GitHub runner. These are the headers its own news page sends when it calls
# the endpoint, so the request looks like what the site expects rather than
# something it has never seen. Two requests every five minutes.
SITE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.escapefromtarkov.com/news",
}

SITE_PAGES = 2       # 20 patches a page; 2 is plenty of overlap for a poll
# Deep enough that a Steam-only patch does not age out of the window before it
# is first seen: "Technical update" (2026-01-14) fell past item 50 within eight
# months. The archive keeps anything already recorded, but a fresh seed only
# sees what this request returns.
STEAM_COUNT = 100
ARCHIVE_MAX = 150
ARCHIVE_FIELDS = ("source", "id", "title", "date", "url", "html", "image")
EMBED_COLOR = 0x9A8866
DISCORD_CHUNK = 3800
DISCORD_MAX_CHUNKS = 12

# Only applied to Steam posts. The site API needs no guessing -- BSG tell us
# what is a patch note. BSG ship real changes under a bare "Technical update",
# so "update" is an include rule and the forward-looking posts are excluded.
STEAM_INCLUDE = [
    r"\b(patch|hotfix|changelog|update)\b",
    r"^\s*v?\d+\.\d+",
]
STEAM_EXCLUDE = [
    r"\bplan(s|ned)?\b",
    r"\broadmap\b",
    r"\bdiary\b",
    r"\bsurvey\b",
    r"\btwitch drops?\b",
    r"\bsale\b",
    r"\bxp (bonus|boost)\b",
    r"\btarkovtv\b",
    r"\bstreamer challenge\b",
]

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "ci-state" / "state.json"
SITE_DIR = ROOT / "site"


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------


def http_get(url: str, timeout: int = 30, headers: dict | None = None) -> bytes:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_site_patches(pages: int = SITE_PAGES) -> list[dict]:
    """Patch notes from Battlestate's own site, newest first.

    Returns [] rather than raising if the site is unreachable. Losing this
    source costs the hotfixes it alone files as patch notes, but Steam still
    carries most patches, and a Discord notification from the source that is
    still up beats no run at all. The caller reports the degradation.
    """
    out: list[dict] = []
    for page in range(1, pages + 1):
        payload = json.loads(http_get(
            SITE_LIST.format(type=SITE_PATCH_TYPE, page=page), headers=SITE_HEADERS))
        rows = payload.get("list") or []
        for row in rows:
            out.append(
                {
                    "source": "eft",
                    "id": f"eft:{row['id']}",
                    "title": (row.get("name") or "").strip(),
                    "date": int(datetime.fromisoformat(row["date"]).timestamp()),
                    "url": SITE_POST.format(id=row["id"]),
                    # CRLF here would round-trip badly: write_text keeps it,
                    # read_text translates it back, so nothing ever compares
                    # equal and every poll looks like a change.
                    "html": (row.get("descr") or "").replace("\r\n", "\n").strip(),
                    # The site keeps the banner out of the body and in its own
                    # field, as a site-relative path.
                    "image": (SITE_ROOT + row["thumb"]) if row.get("thumb") else None,
                }
            )
        if len(rows) < 20:
            break
    return out


def fetch_site_patches_safe(pages: int = SITE_PAGES) -> tuple[list[dict], str | None]:
    try:
        return fetch_site_patches(pages), None
    except (urllib.error.URLError, ValueError, TimeoutError) as exc:
        return [], str(exc)


def fetch_steam_news(count: int = STEAM_COUNT) -> list[dict]:
    """Every Steam announcement, newest first."""
    payload = json.loads(http_get(STEAM_NEWS.format(appid=APPID, count=count)))
    out = []
    for row in payload["appnews"]["newsitems"]:
        gid = str(row["gid"])
        out.append(
            {
                "source": "steam",
                "id": f"steam:{gid}",
                "title": row["title"].strip(),
                "date": int(row["date"]),
                "url": STEAM_POST.format(appid=APPID, gid=gid),
                "html": bbcode_to_html(row.get("contents") or ""),
                "bbcode": row.get("contents") or "",
                "image": first_image(row.get("contents") or ""),
            }
        )
    return out


def fetch_build() -> dict | None:
    """The public branch build id, or None if the lookup fails.

    api.steamcmd.net is a third party, so a failure here must never take the
    run down -- the patch notes matter more than the build ping.
    """
    try:
        payload = json.loads(http_get(STEAM_BUILD.format(appid=APPID), timeout=20))
        branch = payload["data"][str(APPID)]["depots"]["branches"]["public"]
        return {
            "buildid": str(branch["buildid"]),
            "timeupdated": int(branch["timeupdated"]),
        }
    except (urllib.error.URLError, KeyError, ValueError, TimeoutError) as exc:
        print(f"  ! build lookup failed ({exc}); continuing", file=sys.stderr)
        return None


def is_steam_patch(title: str) -> bool:
    if any(re.search(p, title, re.I) for p in STEAM_EXCLUDE):
        return False
    return any(re.search(p, title, re.I) for p in STEAM_INCLUDE)


# --------------------------------------------------------------------------
# De-duplication across sources
# --------------------------------------------------------------------------


def plain_text(markup: str) -> str:
    t = re.sub(r"\[[^\]]*\]|<[^>]*>", " ", markup)
    t = html.unescape(t)
    t = re.sub(r"[^a-z0-9 ]", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()


def version_of(title: str) -> str | None:
    m = re.search(r"\b(\d+(?:\.\d+){2,})\b", title)
    return m.group(1) if m else None


def dedupe_keys(item: dict) -> list[str]:
    """Identities for one post. Sharing any one of them means same patch.

    A version in the title is the strong signal and survives the two sources
    wording a headline differently. Posts with no version fall back to a hash
    of the opening text, which is what catches Steam's "Leagues are live!"
    being the same post as the site's "Patch 1.1.5.1".
    """
    keys = [item["id"]]
    version = version_of(item["title"])
    if version:
        keys.append(f"v:{version}")
    body = plain_text(item.get("html") or "")[:300]
    if body:
        keys.append("c:" + hashlib.sha1(body.encode()).hexdigest()[:12])
    return keys


def merge_sources(site: list[dict], steam: list[dict],
                  include_all: bool = False) -> list[dict]:
    """Site entries win: better titles and BSG's own classification.

    include_all keeps the non-patch Steam announcements too. They still go
    through the same de-duplication, because a post that reads as marketing on
    Steam can be a patch on the site -- "Leagues are live!" is "Patch 1.1.5.1"
    -- and must not be posted a second time under its other headline.
    """
    merged: list[dict] = []
    claimed: set[str] = set()
    for item in site + [s for s in steam if include_all or is_steam_patch(s["title"])]:
        keys = dedupe_keys(item)
        if any(k in claimed for k in keys):
            continue
        claimed.update(keys)
        merged.append(item)
    return sorted(merged, key=lambda i: i["date"], reverse=True)


# --------------------------------------------------------------------------
# Markup conversion
#
# The site hands back HTML, Steam hands back bbcode, Discord wants Markdown,
# and RSS wants HTML -- so there are three conversions here, not one.
# --------------------------------------------------------------------------


def _resolve_image(url: str) -> str:
    return url.replace("{STEAM_CLAN_IMAGE}/", CLAN_IMAGE_BASE)


def first_image(bbcode: str) -> str | None:
    """The first Steam screenshot in a post, for the Discord embed."""
    m = re.search(r'\[img\s+src="([^"]+)"\]', bbcode, re.I)
    return _resolve_image(m.group(1)) if m else None


def _unquote(attr: str) -> str:
    return (attr or "").strip().strip('"').strip("'")


# Named explicitly rather than matched as "any [word]". A greedy pattern also
# eats the label of a Markdown link this module just produced, turning
# "[here](https://...)" into "(https://...)".
_BB_TAG_NAMES = (
    r"b|i|u|s|strike|spoiler|noparse|code|quote|h[1-6]|url|list|olist|img"
    r"|previewyoutube|dynamiclink|carousel|hr|p|table|tr|th|td|center|left"
    r"|right|video|audio|expand|randgroup|equation|\*"
)
_LEFTOVER_BB = re.compile(rf"\[/?(?:{_BB_TAG_NAMES})(?:[ =][^\]]*)?\]", re.I)


def _strip_leftover_tags(text: str) -> str:
    """Drop bbcode this module did not translate, leaving other brackets alone."""
    return _LEFTOVER_BB.sub("", text)


def _collapse_blanks(text: str) -> str:
    # The site's HTML arrives with CRLF line endings and separates paragraphs
    # with &nbsp;, which unescapes to \xa0. Neither is matched by a plain \n
    # or [ \t] pass, so both are normalised before the blank-line collapse.
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _tidy_markdown(text: str) -> str:
    """Tighten list spacing so Discord does not render a wall of gaps."""
    text = re.sub(r"(?m)^([ ]*)-[ ]+", r"\1- ", text)
    text = re.sub(r"(?m)^([ ]*-[ ].*)\n\n+(?=[ ]*-[ ])", r"\1\n", text)
    return text


_BB_STRUCTURE = re.compile(r"\[(/?)(list|olist|\*|p)\]", re.I)


def _convert_bb_structure(text: str, mode: str) -> str:
    """Rewrite [list]/[olist]/[*]/[p], tracking nesting depth.

    Steam nests lists inside list items, which a flat regex mangles: a
    non-greedy match closes the outer item on the inner [/*]. Walking tokens
    keeps the nesting and lets [p] stay inline inside an item.
    """
    out: list[str] = []
    pos = 0
    depth = 0
    for m in _BB_STRUCTURE.finditer(text):
        out.append(text[pos:m.start()])
        pos = m.end()
        closing = m.group(1) == "/"
        tag = m.group(2).lower()
        if tag in ("list", "olist"):
            block = "ul" if tag == "list" else "ol"
            if closing:
                depth = max(0, depth - 1)
                out.append("\n" if mode == "md" else f"</{block}>")
            else:
                depth += 1
                out.append("\n" if mode == "md" else f"<{block}>")
        elif tag == "*":
            if closing:
                out.append("\n" if mode == "md" else "</li>")
            elif mode == "md":
                out.append("\n" + "  " * max(0, depth - 1) + "- ")
            else:
                out.append("<li>")
        else:  # [p]
            if depth > 0:
                out.append(" ")
            elif mode == "md":
                out.append("\n\n")
            else:
                out.append("</p>" if closing else "<p>")
    out.append(text[pos:])
    return "".join(out)


def _convert_bb_inline(t: str, mode: str) -> str:
    md = mode == "md"
    pairs = [
        ("b", "**{}**", "<strong>{}</strong>"),
        ("i", "*{}*", "<em>{}</em>"),
        ("u", "__{}__", "<u>{}</u>"),
        ("strike", "~~{}~~", "<s>{}</s>"),
    ]
    for tag, m_fmt, h_fmt in pairs:
        fmt = m_fmt if md else h_fmt
        t = re.sub(
            rf"\[{tag}\](.*?)\[/{tag}\]",
            lambda m, f=fmt: f.format(m.group(1)),
            t,
            flags=re.S | re.I,
        )
    t = re.sub(r"\[h1\](.*?)\[/h1\]", r"\n## \1\n" if md else r"<h2>\1</h2>", t, flags=re.S | re.I)
    t = re.sub(r"\[h2\](.*?)\[/h2\]", r"\n## \1\n" if md else r"<h2>\1</h2>", t, flags=re.S | re.I)
    t = re.sub(r"\[h([3-6])\](.*?)\[/h\1\]", r"\n### \2\n" if md else r"<h3>\2</h3>", t, flags=re.S | re.I)
    t = re.sub(
        r"\[quote[^\]]*\](.*?)\[/quote\]",
        r"> \1" if md else r"<blockquote>\1</blockquote>",
        t,
        flags=re.S | re.I,
    )
    t = re.sub(r"\[code\](.*?)\[/code\]", r"```\1```" if md else r"<pre>\1</pre>", t, flags=re.S | re.I)
    if md:
        t = re.sub(r"\[spoiler\](.*?)\[/spoiler\]", r"||\1||", t, flags=re.S | re.I)

        def _link(m: re.Match) -> str:
            href, label = _unquote(m.group(1)), m.group(2).strip()
            return f"[{label}]({href})" if label else href
    else:

        def _link(m: re.Match) -> str:
            href, label = html.escape(_unquote(m.group(1))), m.group(2).strip()
            return f'<a href="{href}">{label}</a>'

    t = re.sub(r"\[url=([^\]]+)\](.*?)\[/url\]", _link, t, flags=re.S | re.I)
    t = re.sub(r"\[hr\]\[/hr\]|\[hr\]", "\n---\n" if md else "<hr />", t, flags=re.I)
    return t


def _convert_bb_media(t: str, mode: str) -> str:
    md = mode == "md"
    t = re.sub(
        r'\[img\s+src="([^"]+)"\]\s*\[/img\]',
        ""
        if md
        else (lambda m: f'<p><img src="{html.escape(_resolve_image(m.group(1)))}" /></p>'),
        t,
        flags=re.I,
    )
    t = re.sub(r"\[/?carousel[^\]]*\]", "", t, flags=re.I)
    t = re.sub(
        r'\[previewyoutube="?([^";\]]+)[^\]]*\]\s*\[/previewyoutube\]',
        r"https://youtu.be/\1" if md else r'<p><a href="https://youtu.be/\1">https://youtu.be/\1</a></p>',
        t,
        flags=re.I,
    )
    t = re.sub(
        r'\[dynamiclink\s+href="([^"]+)"\]\s*\[/dynamiclink\]',
        r"\1" if md else r'<p><a href="\1">\1</a></p>',
        t,
        flags=re.I,
    )
    return t


def bbcode_to_markdown(bbcode: str) -> str:
    t = _convert_bb_media(bbcode, "md")
    t = _convert_bb_structure(t, "md")
    t = _convert_bb_inline(t, "md")
    return _tidy_markdown(_collapse_blanks(html.unescape(_strip_leftover_tags(t))))


def bbcode_to_html(bbcode: str) -> str:
    t = _convert_bb_media(bbcode, "html")
    t = _convert_bb_structure(t, "html")
    t = _convert_bb_inline(t, "html")
    return _collapse_blanks(_strip_leftover_tags(t))


_HTML_BLOCK = re.compile(
    r"<\s*(/?)(h[1-6]|p|li|ul|ol|br|div|blockquote|hr)\b[^>]*>", re.I
)


def html_to_markdown(markup: str) -> str:
    """Convert the site's HTML to Discord Markdown, keeping list nesting."""
    t = re.sub(r"(?s)<(script|style)\b.*?</\1>", "", markup, flags=re.I)
    t = re.sub(
        r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        lambda m: f"[{re.sub(r'<[^>]+>', '', m.group(2)).strip()}]({m.group(1)})"
        if re.sub(r"<[^>]+>", "", m.group(2)).strip()
        else m.group(1),
        t,
        flags=re.S | re.I,
    )
    t = re.sub(r"<\s*img\b[^>]*>", "", t, flags=re.I)
    t = re.sub(r"<\s*(b|strong)\b[^>]*>(.*?)</\s*\1\s*>", r"**\2**", t, flags=re.S | re.I)
    t = re.sub(r"<\s*(i|em)\b[^>]*>(.*?)</\s*\1\s*>", r"*\2*", t, flags=re.S | re.I)

    out: list[str] = []
    pos = 0
    depth = 0
    for m in _HTML_BLOCK.finditer(t):
        out.append(t[pos:m.start()])
        pos = m.end()
        closing = m.group(1) == "/"
        tag = m.group(2).lower()
        if tag in ("ul", "ol"):
            depth = max(0, depth - 1) if closing else depth + 1
            out.append("\n")
        elif tag == "li":
            out.append("\n" if closing else "\n" + "  " * max(0, depth - 1) + "- ")
        elif tag.startswith("h") and len(tag) == 2:
            level = "## " if tag in ("h1", "h2") else "### "
            out.append("\n" if closing else "\n" + level)
        elif tag == "hr":
            out.append("\n---\n")
        elif tag == "br":
            out.append("\n")
        elif tag == "blockquote":
            out.append("\n" if closing else "\n> ")
        else:  # p, div
            out.append("\n\n")
    out.append(t[pos:])

    t = re.sub(r"<[^>]+>", "", "".join(out))
    return _tidy_markdown(_collapse_blanks(html.unescape(t)))


def markdown_of(item: dict) -> str:
    if item["source"] == "steam":
        return bbcode_to_markdown(item.get("bbcode") or "")
    return html_to_markdown(item.get("html") or "")


# --------------------------------------------------------------------------
# RSS
# --------------------------------------------------------------------------


def rfc822(ts: int) -> str:
    return format_datetime(datetime.fromtimestamp(ts, tz=timezone.utc))


def cdata(text: str) -> str:
    """Wrap in CDATA, splitting any literal ]]> that would close it early."""
    return "<![CDATA[" + text.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def build_rss(items: list[dict], title: str, description: str, self_url: str) -> str:
    # Pinned to the newest item, not to "now": a wall-clock value here would
    # make every poll produce a diff and a commit, forever.
    built = rfc822(items[0]["date"]) if items else rfc822(0)
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"'
        ' xmlns:content="http://purl.org/rss/1.0/modules/content/">',
        "<channel>",
        f"<title>{html.escape(title)}</title>",
        "<link>https://www.escapefromtarkov.com/news</link>",
        f"<description>{html.escape(description)}</description>",
        "<language>en-us</language>",
        f"<lastBuildDate>{built}</lastBuildDate>",
        "<ttl>5</ttl>",
        f'<atom:link href="{html.escape(self_url)}" rel="self" type="application/rss+xml" />',
    ]
    for it in items:
        body = it.get("html") or ""
        parts += [
            "<item>",
            f"<title>{html.escape(it['title'])}</title>",
            f"<link>{html.escape(it['url'])}</link>",
            f"<guid isPermaLink=\"false\">{html.escape(it['id'])}</guid>",
            f"<pubDate>{rfc822(it['date'])}</pubDate>",
            f"<source url=\"{html.escape(it['url'])}\">"
            f"{'escapefromtarkov.com' if it['source'] == 'eft' else 'Steam'}</source>",
            f"<description>{cdata(body)}</description>",
            f"<content:encoded>{cdata(body)}</content:encoded>",
            "</item>",
        ]
    parts += ["</channel>", "</rss>", ""]
    return "\n".join(parts)


# --------------------------------------------------------------------------
# Discord
# --------------------------------------------------------------------------


def chunk_markdown(text: str, size: int = DISCORD_CHUNK) -> list[str]:
    """Split on line boundaries so headings and bullets stay intact."""
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > size:  # pathological single line
            chunks.append(line[:size])
            line = line[size:]
        if len(current) + len(line) + 1 > size:
            chunks.append(current.rstrip())
            current = ""
        current += line + "\n"
    if current.strip():
        chunks.append(current.rstrip())
    return [c for c in chunks if c.strip()]


def discord_send(webhook: str, payload: dict, attempt: int = 0) -> None:
    req = urllib.request.Request(
        webhook,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 429 and attempt < 5:
            try:
                wait = float(json.loads(exc.read() or b"{}").get("retry_after", 2))
            except ValueError:
                wait = 2.0
            print(f"    rate limited, retrying in {wait + 0.5:.1f}s", file=sys.stderr)
            time.sleep(wait + 0.5)
            return discord_send(webhook, payload, attempt + 1)
        raise


def post_patch(webhook: str, item: dict, dry_run: bool = False) -> None:
    chunks = chunk_markdown(markdown_of(item))
    truncated = len(chunks) > DISCORD_MAX_CHUNKS
    chunks = chunks[:DISCORD_MAX_CHUNKS] or ["(no body text)"]
    if truncated:
        chunks[-1] += f"\n\n**[Read the rest]({item['url']})**"

    where = "escapefromtarkov.com" if item["source"] == "eft" else "Steam"
    for i, chunk in enumerate(chunks):
        embed = {"color": EMBED_COLOR, "description": chunk}
        if i == 0:
            embed["title"] = item["title"]
            embed["url"] = item["url"]
            embed["timestamp"] = datetime.fromtimestamp(
                item["date"], tz=timezone.utc
            ).isoformat()
            embed["author"] = {"name": GAME}
            if item.get("image"):
                embed["image"] = {"url": item["image"]}
        if i == len(chunks) - 1:
            embed["footer"] = {"text": f"Battlestate Games, via {where}"}
        if dry_run:
            print(f"    [dry-run] embed {i + 1}/{len(chunks)} ({len(chunk)} chars)")
            continue
        discord_send(webhook, {"embeds": [embed]}, 0)
        time.sleep(1.0)


def post_build(webhook: str, build: dict, previous: dict | None, dry_run: bool) -> None:
    """A build ping, for the gap between the client updating and notes landing."""
    when = datetime.fromtimestamp(build["timeupdated"], tz=timezone.utc)
    lines = [
        f"A new client build is live on Steam: `{build['buildid']}`.",
        "",
        "Patch notes usually follow within the hour; they will be posted here "
        "when they land. A silent hotfix may never get notes at all.",
    ]
    if previous and previous.get("timeupdated"):
        gap = build["timeupdated"] - int(previous["timeupdated"])
        lines.insert(1, f"Previous build `{previous['buildid']}` was {gap // 3600}h earlier.")
    embed = {
        "title": "Client update detected",
        "description": "\n".join(lines),
        "color": 0x5A6B4F,
        "timestamp": when.isoformat(),
        "author": {"name": GAME},
        "footer": {"text": "Steam build manifest"},
    }
    if dry_run:
        print(f"    [dry-run] build ping for {build['buildid']}")
        return
    discord_send(webhook, {"embeds": [embed]}, 0)
    time.sleep(1.0)


# --------------------------------------------------------------------------
# State and output
# --------------------------------------------------------------------------


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"seen": [], "archive": [], "build": None}


def write_if_changed(path: Path, text: str) -> bool:
    """Write only when the bytes differ.

    Compared as bytes on purpose: read_text() applies universal-newline
    translation, so a payload containing CRLF never equals what write_text()
    put on disk, and an idle poll would commit on every run.
    """
    data = text.encode("utf-8")
    if path.exists() and path.read_bytes() == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return True


def save_state(state: dict) -> bool:
    """Write only on a real change, so idle polls leave the repo untouched."""
    return write_if_changed(STATE_PATH, json.dumps(state, indent=1, sort_keys=True) + "\n")


# Bodies come from Battlestate, not from us, so they are filtered before being
# inlined into a page rather than trusted wholesale.
_UNSAFE_BLOCK = re.compile(
    r"(?is)<\s*(script|style|iframe|object|embed|form)\b.*?<\s*/\s*\1\s*>")
_UNSAFE_OPEN = re.compile(
    r"(?i)<\s*/?\s*(script|style|iframe|object|embed|form|input|button)\b[^>]*>")
_EVENT_ATTR = re.compile(r"(?i)\son[a-z]+\s*=\s*([\x22\x27][^\x22\x27]*[\x22\x27]|[^\s>]+)")
_JS_URL = re.compile(r"(?i)(href|src)\s*=\s*[\x22\x27]\s*javascript:[^\x22\x27]*[\x22\x27]")


def sanitize_html(markup: str) -> str:
    t = _UNSAFE_BLOCK.sub("", markup)
    t = _UNSAFE_OPEN.sub("", t)
    t = _EVENT_ATTR.sub("", t)
    t = _JS_URL.sub(r'\1="#"', t)
    return t


PAGE_CSS = """
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { margin:0; padding:40px 20px 72px; background:#f6f5f3; color:#16150f;
         font:15px/1.65 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif; }
  main { max-width:44rem; margin:0 auto; }
  h1 { font-size:24px; margin:0 0 4px; letter-spacing:-.02em; }
  .sub { color:#6d6a61; margin:0 0 20px; }
  a { color:#b4441f; }
  .feeds { display:flex; gap:8px; flex-wrap:wrap; margin:0 0 24px; padding:0; list-style:none; }
  .feeds a { display:inline-block; padding:5px 11px; border:1px solid #d9d5cc;
             border-radius:999px; text-decoration:none; font-size:13px; }
  .feeds a:hover { border-color:#b4441f; }
  #filter { width:100%; padding:9px 12px; margin:0 0 20px; font:inherit; color:inherit;
            background:#fff; border:1px solid #d9d5cc; border-radius:8px; }
  .patch { border-top:1px solid #e2ded6; padding:2px 0; }
  .patch > summary { cursor:pointer; padding:12px 0; list-style:none;
                     display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; }
  .patch > summary::-webkit-details-marker { display:none; }
  .patch > summary::before { content:"\\25B8"; color:#a09a8e; font-size:12px;
                             transition:transform .12s; }
  .patch[open] > summary::before { transform:rotate(90deg); }
  .name { font-weight:600; font-size:16px; }
  .when { color:#6d6a61; font-size:13px; margin-left:auto; white-space:nowrap; }
  .src { font-size:11px; letter-spacing:.03em; text-transform:uppercase;
         color:#6d6a61; border:1px solid #e2ded6; border-radius:4px; padding:1px 6px; }
  .body { padding:2px 0 22px 22px; }
  .body h1, .body h2, .body h3, .body h4 { font-size:15px; margin:20px 0 6px; }
  .body ul { margin:6px 0; padding-left:20px; }
  .body li { margin:3px 0; }
  .body img { max-width:100%; height:auto; border-radius:6px; margin:10px 0; }
  .empty { color:#6d6a61; padding:24px 0; }
  footer { max-width:44rem; margin:40px auto 0; color:#6d6a61; font-size:13px;
           border-top:1px solid #e2ded6; padding-top:16px; }
  @media (prefers-color-scheme: dark) {
    body { background:#131211; color:#ece9e2; }
    .sub, .when, .src, .empty, footer { color:#97928a; }
    a { color:#e0713f; }
    .feeds a, .src, #filter { border-color:#302e2a; }
    .patch, footer { border-color:#242220; }
    #filter { background:#1b1917; }
  }
"""

PAGE_JS = """
  // Filter by patch name. Kept to the summary text so typing a version number
  // narrows the list without walking 200 KB of bodies on every keystroke.
  (function () {
    var box = document.getElementById('filter');
    var rows = Array.prototype.slice.call(document.querySelectorAll('.patch'));
    var none = document.getElementById('none');
    box.addEventListener('input', function () {
      var q = box.value.trim().toLowerCase();
      var shown = 0;
      rows.forEach(function (r) {
        var hit = !q || r.dataset.name.indexOf(q) !== -1;
        r.hidden = !hit;
        if (hit) shown++;
      });
      none.hidden = shown !== 0;
    });
  })();
"""


def build_page(items: list[dict], all_count: int) -> str:
    newest = items[0]["title"] if items else "nothing yet"
    rows = []
    for n, it in enumerate(items):
        when = datetime.fromtimestamp(it["date"], tz=timezone.utc)
        where = "site" if it["source"] == "eft" else "steam"
        rows.append(
            # The newest patch is open on arrival: it is what anyone visiting
            # this page came for.
            f'<details class="patch"{" open" if n == 0 else ""}'
            f' data-name="{html.escape(it["title"].lower())}">'
            f'<summary><span class="name">{html.escape(it["title"])}</span>'
            f'<span class="src">{where}</span>'
            f'<span class="when"><time datetime="{when:%Y-%m-%d}">'
            f'{when:%d %b %Y}</time></span></summary>'
            f'<div class="body">{sanitize_html(it.get("html") or "")}'
            f'<p><a href="{html.escape(it["url"])}">Read it at the source &rarr;</a></p>'
            f"</div></details>"
        )
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n"
        '<meta charset="utf-8" />\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        f"<title>{GAME} \u2014 patch notes</title>\n"
        '<link rel="alternate" type="application/rss+xml"'
        ' title="EFT patch notes" href="patches.xml" />\n'
        '<link rel="alternate" type="application/rss+xml"'
        ' title="EFT announcements" href="all.xml" />\n'
        f"<style>{PAGE_CSS}</style>\n</head>\n<body>\n<main>\n"
        f"<h1>{GAME} \u2014 patch notes</h1>\n"
        f'<p class="sub">{len(items)} patches and hotfixes, newest first. '
        f"Merged from Battlestate's own site and Steam, checked every few minutes.</p>\n"
        '<ul class="feeds">'
        '<li><a href="patches.xml">Patch notes RSS</a></li>'
        f'<li><a href="all.xml">All {all_count} announcements RSS</a></li>'
        '<li><a href="https://www.escapefromtarkov.com/news">Official news</a></li>'
        "</ul>\n"
        '<input id="filter" type="search" placeholder="Filter by version, e.g. 1.1.5"'
        ' autocomplete="off" />\n'
        + "\n".join(rows)
        + '\n<p class="empty" id="none" hidden>No patch matches that.</p>\n'
        "</main>\n<footer>Sources: the "
        '<a href="https://www.escapefromtarkov.com/news">official news API</a>'
        ' (category &ldquo;Patch Notes&rdquo;) and '
        '<a href="https://store.steampowered.com/news/app/3932890/">Steam app 3932890</a>. '
        'Built by <a href="https://github.com/txrunn/scripts">txrunn/scripts</a>; '
        "not affiliated with Battlestate Games.</footer>\n"
        f"<script>{PAGE_JS}</script>\n</body>\n</html>\n"
    )


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default=os.environ.get("FEED_BASE_URL", ""),
                    help="Public URL the site/ directory is served from.")
    ap.add_argument("--webhook", default=os.environ.get("DISCORD_WEBHOOK_URL", ""),
                    help="Discord webhook URL (or set DISCORD_WEBHOOK_URL).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Do everything except call Discord.")
    ap.add_argument("--post-existing", action="store_true",
                    help="On an empty ledger, post the backlog instead of seeding.")
    ap.add_argument("--all", action="store_true",
                    help="Push every announcement to Discord, not just patch notes.")
    ap.add_argument("--no-build-ping", action="store_true",
                    help="Do not announce Steam client builds, only notes.")
    ap.add_argument("--verify", action="store_true",
                    help="Check both source APIs and show what each one sees, "
                         "then exit without writing anything.")
    args = ap.parse_args(argv)

    site, site_error = fetch_site_patches_safe()
    if site_error:
        print(f"  ! escapefromtarkov.com unavailable ({site_error}); "
              f"continuing with Steam only. Hotfixes it alone files as patch "
              f"notes will be missed until it returns.", file=sys.stderr)
    steam = fetch_steam_news()
    patches = merge_sources(site, steam)
    print(f"Sources: {len(site)} site patch notes, {len(steam)} Steam announcements "
          f"-> {len(patches)} distinct patches.")

    if args.verify:
        by_source = {"eft": 0, "steam": 0}
        for p in patches:
            by_source[p["source"]] += 1
        print(f"  after de-duplication: {by_source['eft']} from the site, "
              f"{by_source['steam']} only on Steam")
        print("\n  Newest 12 patches:")
        for p in patches[:12]:
            day = datetime.fromtimestamp(p["date"], tz=timezone.utc).date()
            print(f"    {day}  [{p['source']:5}]  {p['title']}")
        steam_only = [p for p in patches if p["source"] == "steam"]
        if steam_only:
            print("\n  Found only on Steam (the site does not file these as patch notes):")
            for p in steam_only[:8]:
                day = datetime.fromtimestamp(p["date"], tz=timezone.utc).date()
                print(f"    {day}  {p['title']}")
        build = fetch_build()
        if build:
            when = datetime.fromtimestamp(build["timeupdated"], tz=timezone.utc)
            print(f"\n  Steam build {build['buildid']}, pushed {when:%Y-%m-%d %H:%M UTC}")
        return 0

    state = load_state()
    seen = set(state.get("seen", []))
    first_run = not seen

    # The archive is what lets the feed keep history past whatever the two
    # APIs are willing to hand back on any given day.
    archive = {a["id"]: a for a in state.get("archive", [])}
    for item in patches:
        archive[item["id"]] = {k: item.get(k) for k in ARCHIVE_FIELDS}
    ordered = sorted(archive.values(), key=lambda a: a["date"], reverse=True)[:ARCHIVE_MAX]

    base = args.base_url.rstrip("/")
    news_archive = sorted(
        {s["id"]: {k: s.get(k) for k in ARCHIVE_FIELDS} for s in steam}.values(),
        key=lambda a: a["date"], reverse=True,
    )
    all_items = sorted(
        {a["id"]: a for a in ordered + news_archive}.values(),
        key=lambda a: a["date"], reverse=True,
    )[:ARCHIVE_MAX]

    changed = []
    if write_if_changed(SITE_DIR / "patches.xml", build_rss(
            ordered, f"{GAME} — patch notes",
            "Official patch notes and hotfixes, merged from Battlestate's site and Steam.",
            f"{base}/patches.xml")):
        changed.append("patches.xml")
    if write_if_changed(SITE_DIR / "all.xml", build_rss(
            all_items, f"{GAME} — announcements",
            f"Every official {GAME} announcement, patch notes included.",
            f"{base}/all.xml")):
        changed.append("all.xml")
    if write_if_changed(SITE_DIR / "index.html", build_page(ordered, len(all_items))):
        changed.append("index.html")

    # Oldest first, so the channel reads in the order things happened.
    candidates = merge_sources(site, steam, include_all=True) if args.all else patches
    fresh = [c for c in reversed(candidates) if not any(k in seen for k in dedupe_keys(c))]
    if first_run and not args.post_existing:
        print(f"First run: seeding the ledger with {len(candidates)} posts, not notifying.")
        fresh = []
    elif not fresh:
        print("No new patch notes.")

    for item in fresh:
        if not args.webhook and not args.dry_run:
            print(f"  ! no webhook set, skipping: {item['title']}", file=sys.stderr)
            continue
        print(f"  -> Discord: {item['title']}  [{item['source']}]")
        post_patch(args.webhook, item, dry_run=args.dry_run)
        seen.update(dedupe_keys(item))

    build = None if args.no_build_ping else fetch_build()
    previous = state.get("build")
    if build and not first_run and previous and build["buildid"] != previous.get("buildid"):
        print(f"  -> Discord: client build {build['buildid']}")
        if args.webhook or args.dry_run:
            post_build(args.webhook, build, previous, args.dry_run)
    elif build and first_run:
        print(f"Recording current build {build['buildid']} without notifying.")

    for item in candidates:
        seen.update(dedupe_keys(item))
    state = {
        "seen": sorted(seen)[-2000:],
        "archive": ordered,
        "build": build or previous,
    }
    if save_state(state):
        changed.append("state.json")
    if changed:
        print("Updated: " + ", ".join(changed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
