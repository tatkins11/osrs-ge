r"""Offline self-test — validates the non-network stack (tax, DuckDB storage,
snapshot building) so we can keep verifying the foundation even though the live
OSRS API is firewall-blocked on this machine.

Run:  .\.venv\Scripts\python.exe scripts\selftest.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Make the project importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from app import db, tax  # noqa: E402
from app.collector import build_snapshot_rows  # noqa: E402

_failures = 0


def check(name: str, ok: bool) -> None:
    global _failures
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        _failures += 1


def test_tax() -> None:
    print("tax engine:")
    check("2% of 1000 = 20", tax.sell_tax(1000) == 20)
    check("rounds down (149 -> 2)", tax.sell_tax(149) == 2)
    check("under 50 gp is untaxed", tax.sell_tax(49) == 0)
    check("cap at 5m (300m -> 5m)", tax.sell_tax(300_000_000) == 5_000_000)
    check("cap binds exactly at 250m", tax.sell_tax(250_000_000) == 5_000_000)
    check("exempt item untaxed", tax.sell_tax(1000, exempt=True) == 0)
    check("net_sell(1000) = 980", tax.net_sell(1000) == 980)
    check("margin(900,1000) = 80", tax.margin(900, 1000) == 80)
    check("roi(900,1000) ~ 8.9%", abs(tax.roi(900, 1000) - 80 / 900) < 1e-9)
    check("is_exempt('Mind rune')", tax.is_exempt("Mind rune"))
    check("not is_exempt('Abyssal whip')", not tax.is_exempt("Abyssal whip"))


def test_storage() -> None:
    print("storage layer (DuckDB):")
    db.DB_PATH = str(Path(tempfile.mkdtemp()) / "selftest.duckdb")
    db.init_schema()

    latest = {
        2: {"high": 200, "highTime": 1_718_000_000, "low": 190, "lowTime": 1_718_000_300},
        4151: {"high": 1_800_000, "highTime": 1_718_000_100, "low": 1_750_000, "lowTime": 1_718_000_200},
    }
    m5 = {
        2: {"avgHighPrice": 198, "highPriceVolume": 5000, "avgLowPrice": 191, "lowPriceVolume": 4800},
        4151: {"avgHighPrice": 1_790_000, "highPriceVolume": 120, "avgLowPrice": 1_760_000, "lowPriceVolume": 130},
    }

    df = build_snapshot_rows(latest, m5)
    check("snapshot frame has 2 rows", len(df) == 2)
    check("inserts 2 rows", db.insert_snapshots(df) == 2)
    # Re-insert with identical ts must be a no-op (ON CONFLICT DO NOTHING).
    db.insert_snapshots(df)
    latest_df = db.latest_snapshot_df()
    check("latest snapshot returns 2 items", len(latest_df) == 2)
    check("no duplicate rows after re-insert", db.stats()["snapshot_rows"] == 2)

    mapping = [
        {"id": 4151, "name": "Abyssal whip", "members": True, "value": 120001,
         "lowalch": 48000, "highalch": 72000, "limit": 70, "icon": "whip.png"},
        {"id": 561, "name": "Mind rune", "members": False, "value": 3,
         "lowalch": 1, "highalch": 2, "limit": 25000, "icon": "mind.png"},
    ]
    check("upserts 2 items", db.upsert_items(mapping) == 2)
    items = db.get_items_df().set_index("item_id")
    check("Mind rune flagged exempt", bool(items.loc[561, "exempt"]))
    check("Abyssal whip not exempt", not bool(items.loc[4151, "exempt"]))
    check("buy_limit stored", int(items.loc[4151, "buy_limit"]) == 70)


def test_backfill() -> None:
    print("backfill parser:")
    from app.backfill import timeseries_to_df

    pts = [
        {"timestamp": 1_718_000_000, "avgHighPrice": 100, "avgLowPrice": 95, "highPriceVolume": 10, "lowPriceVolume": 12},
        {"timestamp": 1_718_003_600, "avgHighPrice": 102, "avgLowPrice": 96, "highPriceVolume": 8, "lowPriceVolume": 9},
    ]
    df = timeseries_to_df(4151, "1h", pts)
    check("parses 2 points", len(df) == 2)
    check("has history columns", {"item_id", "timestep", "ts", "avg_high", "avg_low", "high_vol", "low_vol"}.issubset(df.columns))
    check("empty points -> empty df", timeseries_to_df(1, "1h", []).empty)


def main() -> int:
    print(f"Python {sys.version.split()[0]} | pandas {pd.__version__} | duckdb {db.duckdb.__version__}\n")
    test_tax()
    test_storage()
    test_backfill()
    print()
    if _failures:
        print(f"{_failures} CHECK(S) FAILED")
        return 1
    print("ALL OFFLINE TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
