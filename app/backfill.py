"""One-time historical backfill from the wiki /timeseries endpoint.

Seeds the `history` table so analytics work immediately, before the live
collector accumulates enough fine-grained data. Re-runnable: existing rows are
skipped (ON CONFLICT DO NOTHING).

Approx coverage per timestep (the API returns ~365 points per item):
    5m  -> ~30 hours      1h  -> ~15 days
    6h  -> ~91 days       24h -> ~365 days

Usage (run on an UNFILTERED network — the OSRS API is firewall-blocked here):
    python -m app.backfill                 # all items, 1h  (~15 days)
    python -m app.backfill --timestep 6h   # all items, 6h  (~90 days)
    python -m app.backfill --limit 300     # only the 300 most valuable items
"""
from __future__ import annotations

import argparse
import logging
import time

import pandas as pd

from .api_client import VALID_TIMESTEPS, OsrsPricesClient
from .db import ensure_db, get_items_df, insert_history, upsert_items

log = logging.getLogger("backfill")

_HISTORY_COLS = ["item_id", "timestep", "ts", "avg_high", "avg_low", "high_vol", "low_vol"]


def timeseries_to_df(item_id: int, timestep: str, points: list[dict]) -> pd.DataFrame:
    """Convert a /timeseries response into history rows."""
    if not points:
        return pd.DataFrame(columns=_HISTORY_COLS)
    df = pd.DataFrame(points).rename(
        columns={
            "avgHighPrice": "avg_high",
            "avgLowPrice": "avg_low",
            "highPriceVolume": "high_vol",
            "lowPriceVolume": "low_vol",
        }
    )
    df["ts"] = pd.to_datetime(df["timestamp"], unit="s")
    df["item_id"] = item_id
    df["timestep"] = timestep
    return df[_HISTORY_COLS]


def backfill(timestep: str = "1h", limit: int | None = None, sleep: float = 0.25) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if timestep not in VALID_TIMESTEPS:
        raise ValueError(f"invalid timestep {timestep!r}; use one of {VALID_TIMESTEPS}")
    ensure_db()

    with OsrsPricesClient() as client:
        items = get_items_df()
        if items.empty:
            log.info("catalog empty; fetching /mapping first")
            upsert_items(client.get_mapping())
            items = get_items_df()

        ids = items.sort_values("value", ascending=False)["item_id"].astype(int).tolist()
        if limit:
            ids = ids[:limit]
        total = len(ids)
        log.info("backfilling %d items at %s (sleep %.2fs between calls)", total, timestep, sleep)

        rows = 0
        for i, iid in enumerate(ids, 1):
            try:
                points = client.get_timeseries(iid, timestep)
                rows += insert_history(timeseries_to_df(iid, timestep, points))
            except Exception:
                log.exception("timeseries failed for item %s", iid)
            if i % 50 == 0:
                log.info("  %d/%d items, %d rows so far", i, total, rows)
            if sleep:
                time.sleep(sleep)
        log.info("backfill complete: %d items, %d rows at %s", total, rows, timestep)


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill price history from the OSRS Wiki /timeseries API.")
    ap.add_argument("--timestep", default="1h", choices=sorted(VALID_TIMESTEPS))
    ap.add_argument("--limit", type=int, default=None, help="only the N most valuable items")
    ap.add_argument("--sleep", type=float, default=0.25, help="seconds between API calls (be polite)")
    args = ap.parse_args()
    backfill(args.timestep, args.limit, args.sleep)


if __name__ == "__main__":
    main()
