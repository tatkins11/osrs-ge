"""Live price collector.

Polls /latest + /5m every ``POLL_INTERVAL_SECONDS`` and appends one snapshot
per item to DuckDB, building the fine-grained history that powers the
time-of-day / mean-reversion analytics.

Usage:
    python -m app.collector            # run continuously (recommended)
    python -m app.collector once       # collect a single snapshot and exit
                                        # (use this from Windows Task Scheduler)
"""
from __future__ import annotations

import logging
import sys
import time

import pandas as pd

from .api_client import OsrsPricesClient
from .config import DEMO_MARKER, POLL_INTERVAL_SECONDS
from .db import ensure_db, ensure_log_db, insert_history, insert_signal_log, insert_snapshots, stats, upsert_items, utcnow

log = logging.getLogger("collector")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def refresh_catalog(client: OsrsPricesClient, con=None) -> int:
    mapping = client.get_mapping()
    n = upsert_items(mapping, con=con)
    log.info("catalog refreshed: %d items", n)
    return n


def build_snapshot_rows(latest: dict[int, dict], m5: dict[int, dict]) -> pd.DataFrame:
    """Merge /latest (insta prices) and /5m (averages + volume) into one frame."""
    ts = utcnow()
    recs = []
    for iid in set(latest) | set(m5):
        lat = latest.get(iid) or {}
        avg = m5.get(iid) or {}
        rec = {
            "ts": ts,
            "item_id": iid,
            "instabuy": lat.get("high"),
            "instasell": lat.get("low"),
            "high_time": lat.get("highTime"),
            "low_time": lat.get("lowTime"),
            "avg_high": avg.get("avgHighPrice"),
            "avg_low": avg.get("avgLowPrice"),
            "high_vol": avg.get("highPriceVolume"),
            "low_vol": avg.get("lowPriceVolume"),
        }
        if any(rec[k] is not None for k in ("instabuy", "instasell", "avg_high", "avg_low")):
            recs.append(rec)

    df = pd.DataFrame(recs)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"])
    df["high_time"] = pd.to_datetime(df["high_time"], unit="s")
    df["low_time"] = pd.to_datetime(df["low_time"], unit="s")
    return df


def _hist_rows_1h(data: dict[int, dict], ts_epoch: int) -> pd.DataFrame:
    """Build history rows (timestep='1h') from a /1h bucket payload."""
    bucket = pd.to_datetime(ts_epoch, unit="s")
    rows = [
        {"item_id": iid, "timestep": "1h", "ts": bucket,
         "avg_high": v.get("avgHighPrice"), "avg_low": v.get("avgLowPrice"),
         "high_vol": v.get("highPriceVolume"), "low_vol": v.get("lowPriceVolume")}
        for iid, v in data.items()
        if v.get("avgHighPrice") is not None or v.get("avgLowPrice") is not None
    ]
    return pd.DataFrame(rows)


def collect_history_1h(client: OsrsPricesClient, con=None) -> int:
    """Append the latest 1h bucket to history so short-horizon analytics (Movers,
    1d changes) stay fresh; the periodic full backfill still handles 6h/24h.
    Idempotent -- insert_history is ON CONFLICT DO NOTHING."""
    ts, data = client.get_1h_bucket()
    if not ts or not data:
        return 0
    return insert_history(_hist_rows_1h(data, ts), con=con)


def backfill_recent_1h(client: OsrsPricesClient, hours: int = 8) -> int:
    """One-time gap fill: pull the last `hours` hourly buckets (e.g. on startup, to
    bridge the gap since the last full backfill)."""
    now_h = int(time.time()) // 3600 * 3600
    total = 0
    for k in range(hours, 0, -1):
        bucket_ts = now_h - k * 3600
        try:
            _, data = client.get_1h_bucket(timestamp=bucket_ts)
            total += insert_history(_hist_rows_1h(data, bucket_ts))
        except Exception:
            log.exception("1h gap-fill bucket %s failed", bucket_ts)
    log.info("recent 1h history filled: %d rows over %dh", total, hours)
    return total


def collect_once(client: OsrsPricesClient | None = None, con=None) -> int:
    own = client is None
    client = client or OsrsPricesClient()
    try:
        latest = client.get_latest()
        m5 = client.get_5m()
        df = build_snapshot_rows(latest, m5)
        n = insert_snapshots(df, con=con)
        log.info("snapshot stored: %d items (latest=%d, 5m=%d)", n, len(latest), len(m5))
        try:
            collect_history_1h(client, con=con)
        except Exception:
            log.exception("1h history append failed")
        if DEMO_MARKER.exists():
            DEMO_MARKER.unlink(missing_ok=True)  # real data supersedes any demo seed
        return n
    finally:
        if own:
            client.close()


def _log_signals() -> int:
    """Hourly snapshot of the engine's current top signals into the signal-log DB.
    Reads the prices DB read-only; never raises into the collect loop (caller guards)."""
    from .signals import snapshot_signals  # local import keeps collector startup lean
    recs = snapshot_signals()
    if not recs:
        return 0
    df = pd.DataFrame(recs)
    df.insert(0, "ts", utcnow())
    return insert_signal_log(df)


def _sleep_to_next(interval: int, offset: int = 20) -> None:
    """Sleep until just after the next interval boundary (so the 5m bucket is ready)."""
    now = time.time()
    target = (now // interval + 1) * interval + offset
    time.sleep(max(1.0, target - now))


def run_once() -> None:
    _setup_logging()
    ensure_db()
    with OsrsPricesClient() as client:
        refresh_catalog(client)
        collect_once(client=client)
    log.info("coverage: %s", stats())


def run(interval: int = POLL_INTERVAL_SECONDS) -> None:
    _setup_logging()
    ensure_db()
    ensure_log_db()
    log.info("collector starting; interval=%ss", interval)
    with OsrsPricesClient() as client:
        refresh_catalog(client)
        try:
            backfill_recent_1h(client)
        except Exception:
            log.exception("startup 1h gap-fill failed")
        current_day = utcnow().date()
        current_hour = None
        while True:
            try:
                collect_once(client=client)
            except Exception:  # never let one bad cycle kill the loop
                log.exception("collect cycle failed")
            try:  # hourly signal-log snapshot (guarded; must never break collection)
                hour = utcnow().replace(minute=0, second=0, microsecond=0)
                if hour != current_hour:
                    log.info("signal-log snapshot: %d rows", _log_signals())
                    current_hour = hour
            except Exception:
                log.exception("signal-log snapshot failed")
            try:
                day = utcnow().date()
                if day != current_day:
                    refresh_catalog(client)
                    current_day = day
            except Exception:
                log.exception("catalog refresh failed")
            _sleep_to_next(interval)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "once":
        run_once()
    else:
        run()
