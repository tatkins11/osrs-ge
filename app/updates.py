"""Fetch OSRS game-update / blog-post entries from the wiki, for chart event markers.

The official news RSS blocks bots (403), but the OSRS Wiki's MediaWiki API exposes
the "Category:Game updates" pages with timestamps -- the same wiki we already use for
prices, with our descriptive User-Agent. Each member becomes {ts, title, url}.
"""
from __future__ import annotations

import logging
import ssl
from datetime import datetime

import httpx
import truststore

from .config import USER_AGENT

log = logging.getLogger("updates")
WIKI_API = "https://oldschool.runescape.wiki/api.php"


def fetch_updates(limit: int = 80) -> list[dict]:
    """Most recent `limit` game-update pages (newest first), as {ts, title, category, url}."""
    ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    params = {
        "action": "query", "format": "json", "list": "categorymembers",
        "cmtitle": "Category:Game updates", "cmlimit": str(limit),
        "cmsort": "timestamp", "cmdir": "descending", "cmprop": "title|timestamp",
    }
    r = httpx.get(WIKI_API, params=params, headers={"User-Agent": USER_AGENT}, timeout=30.0, verify=ctx)
    r.raise_for_status()
    members = r.json().get("query", {}).get("categorymembers", [])
    rows: list[dict] = []
    for m in members:
        full = m.get("title", "")
        title = full.split(":", 1)[1] if ":" in full else full   # strip the "Update:" prefix
        try:
            ts = datetime.fromisoformat(m["timestamp"].replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            continue
        rows.append({
            "ts": ts, "title": title, "category": "update",
            "url": "https://oldschool.runescape.wiki/w/" + full.replace(" ", "_"),
        })
    log.info("fetched %d updates from wiki", len(rows))
    return rows


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for u in fetch_updates(15):
        print(u["ts"].date(), "|", u["title"])


if __name__ == "__main__":
    main()
