"""Fetch OSRS game-update / blog-post entries from the wiki, for chart event markers.

The official news RSS blocks bots (403), so we use the OSRS Wiki's MediaWiki API.

IMPORTANT -- date source. Do NOT use the Category:Game updates *membership*
timestamp (`cmsort=timestamp`): it records when a page was added to the category,
not when the update shipped. A 2026-03-24 bulk re-categorisation stamped ~1000
old pages (back to the 2013 wiki-seed import) with that single date, which piled
every historical update onto one chart day. Instead we read NEW-PAGE events in the
wiki's "Update" namespace (id 112) via `recentchanges` -- those timestamps are the
real publish dates -- and intersect them with Category:Game updates so only curated
game updates are kept (drops podcasts, roadmaps, community spotlights, merch/PS5
announcements, polls). `recentchanges` only retains ~90 days, but the collector
upserts daily, so real-dated history accumulates over time. Each kept member
becomes {ts, title, category, url}.
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


def _category_titles(c: httpx.Client) -> set[str]:
    """Every page title in Category:Game updates (the curated set, ~1000 members)."""
    titles: set[str] = set()
    cont: dict = {}
    for _ in range(8):  # safety bound (~4000 members) against runaway pagination
        j = c.get(WIKI_API, params={
            "action": "query", "format": "json", "list": "categorymembers",
            "cmtitle": "Category:Game updates", "cmlimit": "500", "cmprop": "title", **cont,
        }).json()
        titles |= {m["title"] for m in j.get("query", {}).get("categorymembers", [])}
        cont = j.get("continue", {})
        if not cont:
            break
    return titles


def fetch_updates(days: int = 120) -> list[dict]:
    """Recent curated game updates with their TRUE publish dates (newest `days` window)."""
    ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    rows: list[dict] = []
    seen: set[str] = set()
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30.0, verify=ctx) as c:
        curated = _category_titles(c)  # empty on failure -> fall back to keeping all RC entries
        cont: dict = {}
        for _ in range(6):  # recentchanges retains ~90d; a few pages of 500 cover it
            j = c.get(WIKI_API, params={
                "action": "query", "format": "json", "list": "recentchanges",
                "rcnamespace": UPDATE_NS, "rctype": "new", "rcprop": "title|timestamp",
                "rclimit": "500", "rcdir": "older", **cont,
            }).json()
            for r in j.get("query", {}).get("recentchanges", []):
                full = r.get("title", "")
                if full in seen or (curated and full not in curated):
                    continue
                try:
                    ts = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")).replace(tzinfo=None)
                except Exception:
                    continue
                if ts < cutoff:
                    continue
                seen.add(full)
                title = full.split(":", 1)[1] if ":" in full else full   # strip the "Update:" prefix
                rows.append({
                    "ts": ts, "title": title, "category": "update",
                    "url": "https://oldschool.runescape.wiki/w/" + full.replace(" ", "_"),
                })
            cont = j.get("continue", {})
            if not cont:
                break
    log.info("fetched %d curated updates (real dates) from wiki", len(rows))
    return rows


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for u in sorted(fetch_updates(), key=lambda x: x["ts"], reverse=True):
        print(u["ts"].date(), "|", u["title"])


if __name__ == "__main__":
    main()
