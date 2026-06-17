"""Overnight-flip study.

Thesis: items are cheaper in the quiet overnight hours (sellers dumping, few
buyers) and richer during peak daytime play. So place a BUY offer in the
evening, let it fill overnight, then sell during the next day. Proceeds taxed 2%.

We test a FIXED-time strategy (buy at hour B, sell at hour S on the next
appropriate day) so there's no hindsight cherry-picking of the daily low/high:

  * fill="spread" (CONSERVATIVE): buy at the evening insta-buy/ask, sell at the
    next-day insta-sell/bid -- you pay the full spread. A real overnight edge
    must clear that + the 2% tax.
  * fill="mid": buy at the evening bid, sell at the next-day ask -- optimistic
    (you capture the spread), the best case for a patient limit order.

All hours are UTC. Run on the VPS (1h history):
    python -m app.overnight --sweep
    python -m app.overnight --sweep --fill mid
"""
from __future__ import annotations

import argparse
import logging

import numpy as np
import pandas as pd

from . import tax as taxmod
from .db import connect, get_items_df

log = logging.getLogger("overnight")
WINSOR = 0.50


def _load(con, timestep: str = "1h") -> pd.DataFrame:
    return con.execute(
        """
        SELECT item_id, ts, avg_high, avg_low,
               CAST(date_part('hour', ts) AS INTEGER) AS hour,
               CAST(ts AS DATE) AS day,
               (COALESCE(high_vol, 0) + COALESCE(low_vol, 0)) AS vol
        FROM history
        WHERE timestep = '1h' AND avg_high IS NOT NULL AND avg_low IS NOT NULL
        ORDER BY item_id, ts
        """
    ).df()


def backtest_item(g: pd.DataFrame, buy_hour: int, sell_hour: int, fill: str, exempt: bool) -> list[dict]:
    """g = one item's 1h bars. Buy at buy_hour, sell at sell_hour on the next
    appropriate day (same day if sell_hour is later, else the following day)."""
    buys = g[g["hour"] == buy_hour]
    sells = g[g["hour"] == sell_hour].set_index("day")
    day_offset = pd.Timedelta(days=0 if sell_hour > buy_hour else 1)
    trades: list[dict] = []
    for b in buys.itertuples():
        sell_day = b.day + day_offset
        if sell_day not in sells.index:
            continue
        s = sells.loc[sell_day]
        if isinstance(s, pd.DataFrame):
            s = s.iloc[0]
        b_hi, b_lo, s_hi, s_lo = b.avg_high, b.avg_low, s["avg_high"], s["avg_low"]
        if any(pd.isna(x) for x in (b_hi, b_lo, s_hi, s_lo)):
            continue
        buy = float(b_hi if fill == "spread" else b_lo)     # evening: pay ask (cons) / place at bid (opt)
        sell = float(s_lo if fill == "spread" else s_hi)    # next day: hit bid (cons) / sell at ask (opt)
        if buy <= 0:
            continue
        net = taxmod.net_sell(int(round(sell)), exempt) - buy
        trades.append({"net": net, "ret": net / buy})
    return trades


def run(buy_hour: int, sell_hour: int, fill: str = "spread", min_gp_vol: float = 50_000_000.0,
        min_price: float = 1000.0, hist=None, items=None, con=None) -> pd.DataFrame:
    if hist is None or items is None:
        own = con is None
        con = con or connect(read_only=True)
        try:
            hist = _load(con)
            items = get_items_df(con).set_index("item_id")
        finally:
            if own:
                con.close()
    if hist.empty:
        return pd.DataFrame()
    rows: list[dict] = []
    for iid, g in hist.groupby("item_id"):
        avg_price = g["avg_high"].mean()
        if avg_price * g["vol"].mean() * 24.0 < min_gp_vol or avg_price < min_price:
            continue
        exempt = bool(items.loc[iid, "exempt"]) if iid in items.index else False
        for t in backtest_item(g, buy_hour, sell_hour, fill, exempt):
            t["item_id"] = int(iid)
            rows.append(t)
    return pd.DataFrame(rows)


def summarize(tr: pd.DataFrame) -> dict:
    if tr.empty:
        return {"trades": 0}
    wins = tr[tr["net"] > 0]["net"].sum()
    losses = tr[tr["net"] < 0]["net"].sum()
    return {
        "trades": len(tr),
        "items": tr["item_id"].nunique(),
        "win_rate": (tr["net"] > 0).mean(),
        "avg_return": tr["ret"].clip(-WINSOR, WINSOR).mean(),
        "median_return": tr["ret"].median(),
        "profit_factor": (wins / abs(losses)) if losses < 0 else float("inf"),
    }


def _pct(x):
    return "-" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x * 100:.1f}%"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Overnight-flip backtest.")
    ap.add_argument("--buy-hour", type=int, default=1)
    ap.add_argument("--sell-hour", type=int, default=14)
    ap.add_argument("--fill", default="spread", choices=["spread", "mid"])
    ap.add_argument("--min-gp-vol", type=float, default=50_000_000.0)
    ap.add_argument("--min-price", type=float, default=1000.0)
    ap.add_argument("--sweep", action="store_true")
    args = ap.parse_args()

    con = connect(read_only=True)
    try:
        hist = _load(con)
        items = get_items_df(con).set_index("item_id")
    finally:
        con.close()
    base = dict(fill=args.fill, min_gp_vol=args.min_gp_vol, min_price=args.min_price, hist=hist, items=items)
    print(f"Overnight flip -- fill={args.fill} "
          f"({'CONSERVATIVE: pay the spread' if args.fill == 'spread' else 'optimistic: capture the spread'}); "
          f"liquid >= {args.min_gp_vol:,.0f} gp/day, price >= {args.min_price:,.0f}. Hours are UTC.\n")

    if args.sweep:
        print(f"{'buy->sell (UTC)':>16}{'trades':>8}{'items':>7}{'win':>8}{'avg ret':>9}{'median':>9}{'PF':>7}")
        for bh in (23, 1, 3, 5):
            for sh in (13, 15, 17):
                s = summarize(run(buy_hour=bh, sell_hour=sh, **base))
                if s["trades"]:
                    pf = "inf" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
                    print(f"{f'{bh:02d}->{sh:02d}':>16}{s['trades']:>8}{s['items']:>7}{_pct(s['win_rate']):>8}"
                          f"{_pct(s['avg_return']):>9}{_pct(s['median_return']):>9}{pf:>7}")
        return

    s = summarize(run(buy_hour=args.buy_hour, sell_hour=args.sell_hour, **base))
    if not s["trades"]:
        print("No trades.")
        return
    print(f"buy {args.buy_hour:02d}:00 -> sell {args.sell_hour:02d}:00 next day: trades {s['trades']} / {s['items']} "
          f"items | win {_pct(s['win_rate'])} | avg {_pct(s['avg_return'])} median {_pct(s['median_return'])} | "
          f"PF {s['profit_factor']:.2f}")


if __name__ == "__main__":
    main()
