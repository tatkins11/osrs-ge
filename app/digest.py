"""Market Desk — the FACTS layer behind the analyst write-ups.

This module computes a structured, fully-grounded "market internals" packet (breadth, movers,
volume anomalies, sector rotation, breakouts, regime, event radar) for a daily / weekly / monthly
horizon. It contains ZERO prose and invents ZERO numbers: every field is a computed market fact.
The narrative layer (Claude, later) is fed this packet and may only phrase these numbers, never add
new ones -- that keeps the write-ups honest (the standing project covenant). Predictions + grading
live in predictions.py and consume this same packet.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .db import connect, get_updates_df
from .signals import Thresholds, market_signals

# horizon -> (primary % change field, human label, lookback days for the "recent updates" radar)
PERIODS = {
    "daily":   {"chg": "chg_1d",  "label": "Daily",   "lookback_d": 2},
    "weekly":  {"chg": "chg_7d",  "label": "Weekly",  "lookback_d": 8},
    "monthly": {"chg": "chg_30d", "label": "Monthly", "lookback_d": 32},
}
_MIN_GPV = 20_000_000     # a name must trade >=20M gp/day to be "notable" (keeps junk out of the column)


def _horizon_changes(con) -> pd.DataFrame:
    """Per-item mid now vs 1d / 7d / 30d ago -> % changes. 1d off the 1h series, 7d/30d off 6h
    (the 6h series is the derived-from-1h one that stays fresh since 2026-07-06)."""
    d1 = con.execute(
        """SELECT item_id,
               arg_max(m, ts) FILTER (WHERE ts > now() - INTERVAL 3 HOUR)                          AS mid_now,
               arg_max(m, ts) FILTER (WHERE ts <= now() - INTERVAL 21 HOUR AND ts > now() - INTERVAL 30 HOUR) AS mid_1d
           FROM (SELECT item_id, ts, (avg_high + avg_low) / 2.0 AS m
                 FROM history WHERE timestep='1h' AND avg_high IS NOT NULL AND avg_low IS NOT NULL
                   AND ts > now() - INTERVAL 32 HOUR)
           GROUP BY item_id"""
    ).df()
    d6 = con.execute(
        """SELECT item_id,
               arg_max(m, ts) FILTER (WHERE ts <= now() - INTERVAL 6 DAY  AND ts > now() - INTERVAL 8 DAY)  AS mid_7d,
               arg_max(m, ts) FILTER (WHERE ts <= now() - INTERVAL 28 DAY AND ts > now() - INTERVAL 33 DAY) AS mid_30d
           FROM (SELECT item_id, ts, (avg_high + avg_low) / 2.0 AS m
                 FROM history WHERE timestep='6h' AND avg_high IS NOT NULL AND avg_low IS NOT NULL
                   AND ts > now() - INTERVAL 33 DAY)
           GROUP BY item_id"""
    ).df()
    m = d1.merge(d6, on="item_id", how="outer")
    for c in ("mid_now", "mid_1d", "mid_7d", "mid_30d"):
        m[c] = m[c].astype("float64")
    m["chg_1d"] = np.where(m.mid_1d > 0, m.mid_now / m.mid_1d - 1.0, np.nan)
    m["chg_7d"] = np.where(m.mid_7d > 0, m.mid_now / m.mid_7d - 1.0, np.nan)
    m["chg_30d"] = np.where(m.mid_30d > 0, m.mid_now / m.mid_30d - 1.0, np.nan)
    return m


def _universe(con) -> pd.DataFrame:
    """Liquid, tradeable, non-event names + signal stats + horizon changes. market_signals already
    carries 'name' (TABLE_COLS), so only the horizon-change frame is merged on top."""
    d = market_signals(Thresholds(), con)
    if d.empty:
        return d
    ch = _horizon_changes(con)
    d = d.merge(ch, on="item_id", how="left")
    if "mid_now" not in d.columns:
        d["mid_now"] = d["mid"]
    d["mid_now"] = d["mid_now"].fillna(d["mid"]).astype("float64")
    gpv = (d["mid_now"].fillna(0) * d.get("vol_daily_7d", pd.Series(0, index=d.index)).fillna(0)).astype("float64")
    d["gpv"] = gpv
    from .planner import _eventy   # reuse the league/event name filter
    keep = d["tradeable"].fillna(False) & (gpv >= _MIN_GPV) & ~d["name"].map(lambda n: _eventy(n))
    return d[keep].copy()


def _row(r, chg_field) -> dict:
    """A compact, JSON-safe item fact-row for the packet."""
    def g(k):
        v = r.get(k)
        return None if v is None or (isinstance(v, float) and pd.isna(v)) else float(v)
    return {
        "item_id": int(r["item_id"]), "name": r.get("name"),
        "mid": g("mid_now"), "chg": g(chg_field),
        "chg_1d": g("chg_1d"), "chg_7d": g("chg_7d"), "chg_30d": g("chg_30d"),
        "gpv": g("gpv"), "vol_ratio": g("vol_ratio"), "z_7d": g("z_7d"),
        "pct_30d": g("pct_30d"), "drawdown": g("drawdown"), "ratio_90": g("ratio_90"),
        "level_health": g("level_health"), "value_discount": g("value_discount"),
    }


def market_internals(d: pd.DataFrame, chg: str) -> dict:
    """Breadth + participation for the chosen horizon."""
    c = d[chg].dropna()
    n = int(len(c))
    adv = int((c > 0.005).sum()); dec = int((c < -0.005).sum()); flat = n - adv - dec
    pct30 = d["pct_30d"].dropna()
    vol = d.get("volatility_7d", pd.Series(dtype=float)).dropna()
    return {
        "universe": n,
        "advancers": adv, "decliners": dec, "flat": flat,
        "ad_ratio": round(adv / dec, 2) if dec else None,
        "pct_positive": round(adv / n * 100, 1) if n else None,
        "median_move_pct": round(float(c.median()) * 100, 2) if n else None,
        "pct_above_30d_mid": round(float((pct30 > 0.5).mean()) * 100, 1) if len(pct30) else None,
        "near_30d_high": int((d["pct_30d"] > 0.9).sum()),
        "near_30d_low": int((d["pct_30d"] < 0.1).sum()),
        "avg_volatility_7d": round(float(vol.median()) * 100, 2) if len(vol) else None,
    }


def top_movers(d: pd.DataFrame, chg: str, n: int = 8) -> dict:
    """Biggest gainers/losers on the horizon, volume-confirmed (real activity, not a thin print)."""
    v = d[d[chg].notna() & (d.get("vol_ratio", pd.Series(1, index=d.index)).fillna(1) >= 0.6)]
    up = v.sort_values(chg, ascending=False).head(n)
    dn = v.sort_values(chg, ascending=True).head(n)
    return {"gainers": [_row(r, chg) for _, r in up.iterrows()],
            "losers":  [_row(r, chg) for _, r in dn.iterrows()]}


def volume_anomalies(d: pd.DataFrame, chg: str, n: int = 6) -> dict:
    """Unusual volume vs the 7d norm, split accumulation (up on volume) vs distribution (down)."""
    vr = d[d.get("vol_ratio").notna() & (d["vol_ratio"] >= 2.0)].sort_values("vol_ratio", ascending=False)
    accum = vr[vr[chg].fillna(0) >= 0].head(n)
    distrib = vr[vr[chg].fillna(0) < 0].head(n)
    return {"accumulation": [_row(r, chg) for _, r in accum.iterrows()],
            "distribution": [_row(r, chg) for _, r in distrib.iterrows()]}


_PRED_MIN_GPV = 50_000_000    # a call needs real exit liquidity, not just "notable" turnover
_PRED_MIN_PX = 5_000          # no penny junk (Jerboa tail @ 340 is not a tradeable reversion)


def extremes(d: pd.DataFrame, chg: str, n: int = 6) -> dict:
    """GENUINE reversion candidates: liquid, real price, SANE stretch. The ratio_90 cap is critical
    -- a newly-released item's 90d median includes near-zero pre-release prints, so its ratio_90
    blows up to 100x+ (Dual sai hit 129x). Those are data artifacts, not rich trades; cap at 2.5x.
    Deep-value side already requires a stable level (health) so it isn't a falling knife."""
    # A reversion candidate must be stretched over TIME yet STABLE right now -- never mid-knife.
    # Items in a violent intraday move (Iorwerth +118%, Mixed hide legs -44%) have stale refs and
    # are exactly the falling knives / rockets the regime shield exists to avoid.
    liq = d[(d["gpv"].fillna(0) >= _PRED_MIN_GPV) & (d["mid_now"].fillna(0) >= _PRED_MIN_PX)
            & (d["chg_1d"].abs().fillna(0) <= 0.15)]
    rich = liq[liq["ratio_90"].between(1.40, 2.5) & liq["level_health"].fillna(0).between(0.6, 1.6)]
    rich = rich.sort_values("ratio_90", ascending=False).head(n)
    # cap the discount at 30%: a "50% below fair" print is almost always a new/illiquid item whose
    # 7d 'established' level is itself noise, not a clean value setup. Real reversion is 6-30% cheap.
    cheap = liq[liq["value_discount"].fillna(0).between(0.06, 0.30) & liq["level_health"].fillna(0).between(0.85, 1.3)]
    cheap = cheap.sort_values("value_discount", ascending=False).head(n)
    return {"stretched_rich": [_row(r, chg) for _, r in rich.iterrows()],
            "deep_value": [_row(r, chg) for _, r in cheap.iterrows()]}


def event_radar(con, lookback_d: int) -> list[dict]:
    """Recent game updates / blog posts (the catalysts). From the wiki-sourced updates table."""
    try:
        u = get_updates_df()
    except Exception:
        return []
    if u is None or u.empty or "ts" not in u.columns:
        return []
    u = u.copy()
    u["ts"] = pd.to_datetime(u["ts"], errors="coerce")
    cutoff = pd.Timestamp.utcnow().tz_localize(None) - pd.Timedelta(days=lookback_d)
    recent = u[u["ts"] >= cutoff].sort_values("ts", ascending=False).head(8)
    return [{"ts": r["ts"].isoformat(), "title": r.get("title"), "category": r.get("category")}
            for _, r in recent.iterrows()]


def regime(internals: dict, sectors: dict | None) -> dict:
    """A plain-language market-regime read derived purely from the internals above."""
    pp = internals.get("pct_positive")
    vol = internals.get("avg_volatility_7d")
    if pp is None:
        return {"label": "unknown", "breadth": None, "volatility": None}
    tone = "risk-on" if pp >= 60 else ("risk-off" if pp <= 40 else "mixed")
    breadth = "broad" if pp >= 60 or pp <= 40 else "narrow"
    vlabel = "elevated" if (vol or 0) >= 6 else ("calm" if (vol or 0) <= 3 else "normal")
    lead = lag = None
    if sectors and sectors.get("sectors"):
        # sector_table stores per-horizon % under s["changes"]["7d"]; skip the whole-market aggregate
        cand = [s for s in sectors["sectors"] if (s.get("label") or "") != "Whole Market"]
        cand = [s for s in cand if (s.get("changes") or {}).get("7d") is not None]
        if cand:
            srt = sorted(cand, key=lambda s: -(s["changes"]["7d"]))
            lead = srt[0].get("label")
            lag = srt[-1].get("label")
    return {"label": tone, "breadth": breadth, "volatility": vlabel,
            "pct_positive": pp, "leading_sector": lead, "lagging_sector": lag}


def build_digest(con=None, period: str = "daily") -> dict:
    """The full facts packet for one issue. Pure market data; no prose, no invented numbers."""
    if period not in PERIODS:
        raise ValueError(f"period must be one of {list(PERIODS)}")
    own = con is None
    con = con or connect(read_only=True)
    try:
        meta = PERIODS[period]
        chg = meta["chg"]
        d = _universe(con)
        if d.empty:
            return {"period": period, "ok": False, "reason": "no liquid universe"}
        try:
            from .sectors import sector_table
            sectors = sector_table(con=con)
        except Exception:
            sectors = None
        internals = market_internals(d, chg)
        packet = {
            "period": period, "label": meta["label"], "ok": True,
            "generated_ts": pd.Timestamp.utcnow().tz_localize(None).isoformat(),
            "internals": internals,
            "movers": top_movers(d, chg),
            "volume": volume_anomalies(d, chg),
            "extremes": extremes(d, chg),
            "sectors": sectors,
            "events": event_radar(con, meta["lookback_d"]),
            "regime": regime(internals, sectors),
        }
        return packet
    finally:
        if own:
            con.close()


def main() -> None:
    import json
    for p in ("daily", "weekly", "monthly"):
        pk = build_digest(period=p)
        i = pk.get("internals", {})
        print(f"\n===== {pk.get('label')} =====")
        print("internals:", json.dumps(i))
        print("regime:", json.dumps(pk.get("regime")))
        g = pk.get("movers", {}).get("gainers", [])[:3]
        l = pk.get("movers", {}).get("losers", [])[:3]
        print("top gainers:", [(x["name"], round((x["chg"] or 0) * 100, 1)) for x in g])
        print("top losers :", [(x["name"], round((x["chg"] or 0) * 100, 1)) for x in l])
        acc = pk.get("volume", {}).get("accumulation", [])[:3]
        print("accumulation:", [(x["name"], x["vol_ratio"]) for x in acc])
        print("events:", [e["title"] for e in pk.get("events", [])[:3]])


if __name__ == "__main__":
    main()
