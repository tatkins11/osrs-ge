"""The Daily Income Plan — the "20M/day method" made operational (validated 2026-07-04).

Capacity studies (honest constraints: 2% tax, buy-limit x2 windows/day, CAPTURE=10% of both
legs' flow, capital recycling):
  - CONVERSIONS (sets via GE clerk + decants via Bob Barter): ~15.5M/day theoretical across the
    top verified routes; capital-aware packing below realizes 8-12M/day on ~120-160M cycling.
  - The high-limit consumable "bulk lane" was TESTED AND REJECTED: 1-2 gp/unit net at daily
    average prices — the 2% tax consumes it. Not part of the method.
  - Overnight lowballs + deep-dip reserve and session flips stack on top (their EV comes from
    the 8-slot plan; realized clean-era: +4%/trip at 98% win).

THE METHOD (two-shift capital — the same gp works day AND night):
  Morning  (~45m): collect overnight fills -> clerk-convert -> list; place conversion PIECE/(3)
                   buys (the day shift fills while you're away).
  Evening  (~45m): collect day fills -> convert -> list; place the FULL-CASH overnight slate.
Median stack ≈ conversions 8-12M + flips 2-4M + overnight 1.5-3M + pattern plays ~1-2M
=> ~15-20M/day median at the current bankroll, 30-60M on tail days (reserve fills, crash
recoveries, basis spikes). Scales linearly-ish with bankroll until route capacity saturates.
"""
from __future__ import annotations

import time

import pandas as pd

from . import tax as taxmod
from .db import connect, connect_trades, utcnow
from .setarb import DECANTS, SETS, VERIFIED

CAPTURE = 0.10          # share of each leg's daily flow we take without moving the market
WINDOWS = 2             # buy-limit windows he can realistically refresh per day (2-touch)
_CACHE: dict = {"ts": 0.0, "out": None}
_TTL = 1800.0


def _limf(limit_of: dict, iid: int, dflt: float) -> float:
    v = limit_of.get(int(iid))
    return float(v) if v is not None and not pd.isna(v) and v > 0 else dflt


def build(cash: float) -> dict:
    """Pack conversion routes into the available cash (greedy by gp/day per gp of capital),
    respecting buy limits, both legs' flow, and per-route capital. Returns today's allocation."""
    now = time.time()
    c = _CACHE
    if c["out"] is not None and now - c["ts"] < _TTL and abs(c["out"].get("cash", 0) - cash) < cash * 0.15:
        return c["out"]
    con = connect(read_only=True)
    try:
        items = con.execute("SELECT item_id, name, buy_limit FROM items").df()
        byname = dict(zip(items["name"], items["item_id"]))
        limit_of = dict(zip(items["item_id"], items["buy_limit"]))
        snap = con.execute(
            "SELECT item_id, instabuy, instasell FROM snapshots "
            "QUALIFY row_number() OVER (PARTITION BY item_id ORDER BY ts DESC) = 1"
        ).df().set_index("item_id")
        all_ids: set[int] = set()
        for s, comps in SETS.items():
            if s in byname and all(cc in byname for cc in comps):
                all_ids |= {int(byname[s])} | {int(byname[cc]) for cc in comps}
        for fam, bd, sd in DECANTS:
            for f in (f"{fam}({bd})", f"{fam}({sd})"):
                if f in byname:
                    all_ids.add(int(byname[f]))
        ph = ",".join(str(i) for i in sorted(all_ids))
        v90 = con.execute(
            f"""SELECT item_id, avg(high_vol+low_vol) AS units FROM history
                WHERE timestep='24h' AND item_id IN ({ph}) AND ts >= now() - INTERVAL 90 DAY
                GROUP BY item_id"""
        ).df()
        v90 = dict(zip(v90.item_id, v90.units))
    finally:
        con.close()

    def px(iid, col):
        try:
            v = snap.loc[int(iid), col]
            return float(v) if pd.notna(v) else None
        except KeyError:
            return None

    routes = []
    for s, comps in SETS.items():
        if s not in VERIFIED or s not in byname or not all(cc in byname for cc in comps):
            continue
        sid = int(byname[s])
        cids = [int(byname[cc]) for cc in comps]
        bids = [px(cc, "instasell") for cc in cids]
        set_ask = px(sid, "instabuy")
        if None in bids or not set_ask:
            continue
        cost = sum(bids)
        net = taxmod.net_sell(int(set_ask), False) - cost
        if net <= 0:
            continue
        cyc = min(
            min(_limf(limit_of, cc, 8) * WINDOWS for cc in cids),
            min(CAPTURE * float(v90.get(cc) or 0) for cc in cids),
            CAPTURE * float(v90.get(sid) or 0),
        )
        if cyc < 1:
            continue
        routes.append({"route": s, "kind": "set", "buy_ids": cids, "unit_cost": cost,
                       "net_per_unit": net, "max_units": int(cyc), "roi": net / cost})
    for fam, bd, sd in DECANTS:
        bi, si = byname.get(f"{fam}({bd})"), byname.get(f"{fam}({sd})")
        if bi is None or si is None:
            continue
        b_bid, s_ask = px(bi, "instasell"), px(si, "instabuy")
        if not b_bid or not s_ask:
            continue
        cost = b_bid * sd / bd                      # cost per SELL-form unit
        net = taxmod.net_sell(int(s_ask), False) - cost
        if net <= 0:
            continue
        cyc = min(_limf(limit_of, bi, 2000) * WINDOWS * bd / sd,
                  CAPTURE * float(v90.get(bi) or 0) * bd / sd,
                  CAPTURE * float(v90.get(si) or 0))
        if cyc < 1:
            continue
        routes.append({"route": f"{fam} ({bd})→({sd})", "kind": "decant", "buy_ids": [int(bi)],
                       "unit_cost": cost, "net_per_unit": net, "max_units": int(cyc), "roi": net / cost})

    # capital-aware packing: capital recycles ~2x intra-day, so the effective budget is cash x 2;
    # greedy by ROI (gp/day per gp) fills the most profitable combination first
    routes.sort(key=lambda r: r["roi"], reverse=True)
    budget = cash * 2.0
    alloc = []
    projected = 0.0
    for r in routes:
        if budget <= r["unit_cost"]:
            continue
        units = int(min(r["max_units"], budget // r["unit_cost"]))
        if units < 1:
            continue
        cap_gp = units * r["unit_cost"]
        budget -= cap_gp
        projected += units * r["net_per_unit"]
        alloc.append({**{k: r[k] for k in ("route", "kind")},
                      "units_today": units, "capital": round(cap_gp),
                      "net_per_unit": round(r["net_per_unit"]), "roi": round(r["roi"], 4),
                      "projected_gp": round(units * r["net_per_unit"])})

    out = {"cash": cash, "routes": alloc[:12], "conversions_projected": round(projected),
           "built_at": str(utcnow())}
    _CACHE["ts"], _CACHE["out"] = now, out

    # feed the auto-tagger: log today's conversion BUY targets into plan_log (tag=convert) so his
    # in-game piece/form buys inherit attribution with zero workflow change
    try:
        tcon = connect_trades()
        try:
            ts = utcnow()
            for a, r in zip(alloc[:12], [r for r in routes if r["net_per_unit"] > 0][:12]):
                for bid_ in r["buy_ids"]:
                    p_ = px(bid_, "instasell")
                    if p_:
                        tcon.execute(
                            "INSERT INTO plan_log (ts, action, item_id, name, price, qty, margin, gp_day, "
                            "exp_net, recovery, target, cur_price, ev_score, tag) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)",
                            [ts, "BUY", int(bid_), a["route"], int(p_), int(a["units_today"]),
                             int(a["net_per_unit"]), int(a["projected_gp"]), 0, 0, 0, int(p_), "convert"],
                        )
        finally:
            tcon.close()
    except Exception:  # noqa: BLE001 — attribution feed is best-effort
        pass
    return out
