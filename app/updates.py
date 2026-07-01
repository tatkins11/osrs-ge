"""Fetch OSRS game-update / blog-post entries from the wiki, for chart event markers.

The official news RSS blocks bots (403), so we use the OSRS Wiki's MediaWiki API.

IMPORTANT -- date source. Do NOT use the category *membership* timestamp
(`cmsort=timestamp`): it records when a page was added to a category, not when the
post shipped. A 2026-03-24 bulk re-categorisation stamped ~1000 old pages (back to
the 2013 wiki-seed import) with that single date, which piled every historical
update onto one chart day. Instead we read NEW-PAGE events in the wiki's "Update"
namespace (id 112) via `recentchanges` -- those timestamps are the real publish
dates -- then keep only posts whose categories match ALLOWED_CATEGORIES (so the
chart shows game updates / dev blogs / content previews, and drops podcasts,
community spotlights, polls, merch/PS5 and other non-GE news that also live in the
Update namespace). `recentchanges` only retains ~90 days, but the collector upserts
daily, so real-dated history accumulates over time. Each kept post becomes
{ts, title, category, url} where category is one of update | blog | preview.
"""
from __future__ import annotations

import logging
import ssl
from datetime import datetime, timedelta, timezone

import httpx
import truststore

from .config import USER_AGENT

log = logging.getLogger("updates")
WIKI_API = "https://oldschool.runescape.wiki/api.php"
UPDATE_NS = "112"  # the wiki's "Update" namespace id

# Wiki type-categories we treat as chart-worthy. "Community updates" is deliberately
# excluded: it's a grab-bag that mixes GE-relevant previews with podcasts, community
# spotlights, merch and polls, so it can't be kept cleanly by category alone.
ALLOWED_CATEGORIES = {"Game updates", "Developer Blogs", "Future Updates"}


def _categories_for(c: httpx.Client, titles: list[str]) -> dict[str, set[str]]:
    """Map each page title -> its set of (non-hidden) category names."""
    out: dict[str, set[str]] = {}
    for i in range(0, len(titles), 40):  # batch titles; well under URL/length limits
        batch = titles[i:i + 40]
        j = c.get(WIKI_API, params={
            "action": "query", "format": "json", "titles": "|".join(batch),
            "prop": "categories", "cllimit": "500", "clshow": "!hidden",
        }).json()
        for pg in j.get("query", {}).get("pages", {}).values():
            out[pg.get("title", "")] = {
                x["title"].replace("Category:", "") for x in pg.get("categories", [])
            }
    return out


def fetch_updates(days: int = 120) -> list[dict]:
    """Recent curated game updates / dev blogs / previews with their TRUE publish dates."""
    ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30.0, verify=ctx) as c:
        # 1. recent new pages in the Update namespace -> (full title, real publish ts)
        recent: list[tuple[str, datetime]] = []
        cont: dict = {}
        for _ in range(6):  # recentchanges retains ~90d; a few pages of 500 cover it
            j = c.get(WIKI_API, params={
                "action": "query", "format": "json", "list": "recentchanges",
                "rcnamespace": UPDATE_NS, "rctype": "new", "rcprop": "title|timestamp",
                "rclimit": "500", "rcdir": "older", **cont,
            }).json()
            for r in j.get("query", {}).get("recentchanges", []):
                try:
                    ts = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")).replace(tzinfo=None)
                except Exception:
                    continue
                if ts >= cutoff:
                    recent.append((r.get("title", ""), ts))
            cont = j.get("continue", {})
            if not cont:
                break

        # 2. keep only posts tagged as game updates / dev blogs / content previews
        cats = _categories_for(c, [t for t, _ in recent])
        rows: list[dict] = []
        seen: set[str] = set()
        for full, ts in recent:
            cs = cats.get(full, set())
            if full in seen or not (cs & ALLOWED_CATEGORIES):
                continue
            seen.add(full)
            kind = ("blog" if "Developer Blogs" in cs
                    else "preview" if "Future Updates" in cs
                    else "update")
            title = full.split(":", 1)[1] if ":" in full else full   # strip the "Update:" prefix
            rows.append({
                "ts": ts, "title": title, "category": kind,
                "url": "https://oldschool.runescape.wiki/w/" + full.replace(" ", "_"),
            })
    log.info("fetched %d curated updates (real dates) from wiki", len(rows))
    return rows


_CAT_KIND = [("Developer Blogs", "blog"), ("Future Updates", "preview"), ("Game updates", "update")]
_DATE_RE = None  # compiled lazily


def backfill_updates(since: str = "2021-03-01") -> list[dict]:
    """DEEP one-time backfill: every curated update/blog/preview since `since`, with TRUE publish
    dates. recentchanges only retains ~90 days, so the live fetch can never recover history —
    instead enumerate the members of the type categories (Update: namespace) and read each page's
    own `{{Update|date=...}}` infobox, which records the real publish date even for pages that
    were bulk-imported into the wiki. ~15 batched queries for 5 years; idempotent via the URL PK."""
    import re
    from datetime import datetime as _dt

    ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    cutoff = _dt.fromisoformat(since)
    tpl_re = re.compile(r"\{\{Update\b(.*?)\}\}", re.S | re.I)
    date_re = re.compile(r"date\s*=\s*([^|\n}]+)")
    kind_of: dict[str, str] = {}
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30.0, verify=ctx) as c:
        for cat, kind in _CAT_KIND:                       # blog/preview claim first, update fills the rest
            cont: dict = {}
            while True:
                j = c.get(WIKI_API, params={
                    "action": "query", "format": "json", "list": "categorymembers",
                    "cmtitle": f"Category:{cat}", "cmnamespace": UPDATE_NS, "cmlimit": "500", **cont,
                }).json()
                for m in j.get("query", {}).get("categorymembers", []):
                    kind_of.setdefault(m["title"], kind)
                cont = j.get("continue", {})
                if not cont:
                    break
        titles = list(kind_of)
        log.info("backfill: %d candidate update pages across categories", len(titles))
        rows: list[dict] = []
        for i in range(0, len(titles), 50):
            batch = titles[i:i + 50]
            j = c.get(WIKI_API, params={
                "action": "query", "format": "json", "formatversion": "2",
                "titles": "|".join(batch), "prop": "revisions",
                "rvprop": "content", "rvslots": "main", "rvsection": "0",
            }).json()
            for pg in j.get("query", {}).get("pages", []):
                try:
                    txt = pg["revisions"][0]["slots"]["main"]["content"]
                except (KeyError, IndexError):
                    continue
                tm = tpl_re.search(txt)
                dm = date_re.search(tm.group(1)) if tm else None
                if not dm:
                    continue
                try:
                    ts = _dt.strptime(dm.group(1).strip(), "%d %B %Y")
                except ValueError:
                    continue
                if ts < cutoff:
                    continue
                full = pg.get("title", "")
                title = full.split(":", 1)[1] if ":" in full else full
                rows.append({
                    "ts": ts, "title": title, "category": kind_of.get(full, "update"),
                    "url": "https://oldschool.runescape.wiki/w/" + full.replace(" ", "_"),
                })
    log.info("backfill: %d dated posts since %s", len(rows), since)
    return rows


def main() -> None:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Fetch (or deep-backfill) curated OSRS updates.")
    ap.add_argument("--backfill", metavar="SINCE", nargs="?", const="2021-03-01",
                    help="deep-backfill posts since this ISO date (default 2021-03-01) and upsert into the DB")
    args = ap.parse_args()
    if args.backfill:
        from .db import upsert_updates
        rows = backfill_updates(args.backfill)
        n = upsert_updates(rows)
        print(f"upserted {n if n is not None else len(rows)} updates since {args.backfill}")
    else:
        for u in sorted(fetch_updates(), key=lambda x: x["ts"], reverse=True):
            print(u["ts"].date(), f"[{u['category']:7s}]", "|", u["title"])


if __name__ == "__main__":
    main()
