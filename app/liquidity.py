"""Liquidity / fill-timing analytics from the 5-minute snapshot history.

The planner used to judge "will this fill?" by AVERAGE daily volume, which can't tell a
ghost (one daily dump, dead the other 23h) from a genuinely liquid item -- both can show the
same daily total. This module measures TRADE FREQUENCY instead: what fraction of 5-minute
windows actually had a trade on the side you need, plus the time-of-day liquidity clock that
tells you WHEN orders fill.

Side convention (OSRS wiki /5m feed, as stored on each snapshot):
- low_vol  = units traded at the LOW (insta-sell) price = SELLERS hitting the market. Your BUY
             offer fills against these  -> "buy-side" uptime.
- high_vol = units traded at the HIGH (insta-buy) price = BUYERS hitting the market. Your SELL
             offer fills against these  -> "sell-side" uptime.
"""
from __future__ import annotations

import time
from datetime import timedelta

from .db import connect, utcnow


def _cutoff(days: float):
    return utcnow() - timedelta(days=days)


def fill_uptime(item_ids, days: float = 7.0, con=None) -> dict[int, dict]:
    """Per item: the fraction of 5-minute windows (last `days`) that had ANY trade on each side.
    buy = sellers were present (your BUY can fill); sell = buyers were present (your SELL can fill).
    This is the real "will it fill" signal -- a 44%-uptime item fills; a 1%-uptime ghost sits."""
    ids = [int(i) for i in dict.fromkeys(item_ids)]   # de-dup, keep order
    if not ids:
        return {}
    own = con is None
    con = con or connect(read_only=True)
    try:
        ph = ",".join(["?"] * len(ids))
        rows = con.execute(
            f"""SELECT item_id, count(*) n,
                       sum(CASE WHEN low_vol  > 0 THEN 1 ELSE 0 END) buy_active,
                       sum(CASE WHEN high_vol > 0 THEN 1 ELSE 0 END) sell_active
                FROM snapshots
                WHERE item_id IN ({ph}) AND ts >= ?
                GROUP BY item_id""",
            [*ids, _cutoff(days)],
        ).fetchall()
    finally:
        if own:
            con.close()
    out: dict[int, dict] = {}
    for iid, n, ba, sa in rows:
        n = int(n or 0)
        out[int(iid)] = {
            "buy": (ba / n) if n else 0.0,    # buy-side uptime  (your BUY fills)
            "sell": (sa / n) if n else 0.0,   # sell-side uptime (your SELL fills)
            "n": n,
        }
    return out


def peak_hours(item_ids, days: float = 7.0, top: int = 3, con=None) -> dict[int, list[int]]:
    """Per item: the UTC hours with the most buy-side (seller) liquidity -- when your BUY is most
    likely to fill. Meant for small id lists (the items actually in the plan)."""
    ids = [int(i) for i in dict.fromkeys(item_ids)]
    if not ids:
        return {}
    own = con is None
    con = con or connect(read_only=True)
    try:
        ph = ",".join(["?"] * len(ids))
        rows = con.execute(
            f"""SELECT item_id, extract(hour FROM ts) h, sum(COALESCE(low_vol,0)) v
                FROM snapshots
                WHERE item_id IN ({ph}) AND ts >= ?
                GROUP BY item_id, h""",
            [*ids, _cutoff(days)],
        ).fetchall()
    finally:
        if own:
            con.close()
    by_item: dict[int, list] = {}
    for iid, h, v in rows:
        by_item.setdefault(int(iid), []).append((int(h), float(v or 0.0)))
    out: dict[int, list[int]] = {}
    for iid, hv in by_item.items():
        hv.sort(key=lambda x: x[1], reverse=True)
        out[iid] = sorted(h for h, v in hv[:top] if v > 0)
    return out


_CLOCK_CACHE: dict = {"ts": 0.0, "data": None}
_CLOCK_TTL = 1800.0   # 30 min -- a multi-day aggregate barely moves hour to hour


def market_clock(days: float = 7.0, con=None) -> list[dict]:
    """Market-wide liquidity by UTC hour: total units traded (both sides) per hour-of-day, with a
    0..1 relative scale so the UI can draw a 'best time to place orders' clock. Cached, since it's
    a whole-market scan that's near-static across the day."""
    now = time.time()
    cached = _CLOCK_CACHE["data"]
    if cached is not None and (now - _CLOCK_CACHE["ts"]) < _CLOCK_TTL:
        return cached
    own = con is None
    con = con or connect(read_only=True)
    try:
        rows = con.execute(
            """SELECT extract(hour FROM ts) h, sum(COALESCE(low_vol,0)+COALESCE(high_vol,0)) v
               FROM snapshots WHERE ts >= ? GROUP BY h ORDER BY h""",
            [_cutoff(days)],
        ).fetchall()
    finally:
        if own:
            con.close()
    vols = {int(h): float(v or 0.0) for h, v in rows}
    mx = max(vols.values()) if vols else 0.0
    data = [{"hour": h, "vol": vols.get(h, 0.0), "rel": (vols.get(h, 0.0) / mx if mx else 0.0)}
            for h in range(24)]
    _CLOCK_CACHE.update(ts=now, data=data)
    return data
