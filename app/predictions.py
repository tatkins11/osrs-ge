"""Market Desk — the PREDICTION LEDGER and its auto-grader.

The analyst makes specific, falsifiable calls; every call is stored immutably with a target, a
horizon and a confidence, and is scored automatically once it matures. This is what turns the
write-ups from opinion into a *measurable* skill -- and any rule whose calls reliably hit becomes a
signal we can trade.

Grounding rules (the standing anti-fabrication covenant):
  * Calls come ONLY from edges we have already validated, never from vibes or momentum:
      - reversion_up   : undervalued vs a STABLE established level -> reverts toward fair
                         (the value-buy edge; 80+ confidence bucket was CI-positive).
      - reversion_down : trading rich to its own 90d median (ratio_90 > 1.40) -> mean-reverts down
                         (the 2026-07 spike study: >=1.45x went 0-for-4, median -23% fwd).
    Momentum / breakout follow-through is deliberately excluded (backtested 26% win).
  * The initial confidences are PRIORS. The scorecard recalibrates them against realised hit rates
    over time -- that is the point of tracking. Confidence is never invented to look good.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .db import (connect, get_predictions_df, insert_digest, insert_prediction,
                 record_digest_prose, update_prediction_outcome)

HORIZON_D = 7          # every call is a 7-day view (matches the reversion edge's window)
MAX_CALLS = 4          # a disciplined desk makes a few high-conviction calls, not a scattergun


def _clip(x, lo, hi):
    return float(max(lo, min(hi, x)))


def generate(packet: dict, con=None) -> list[dict]:
    """Turn a digest facts packet into a few grounded, falsifiable calls (unsaved dicts)."""
    ex = (packet or {}).get("extremes") or {}
    calls: list[dict] = []

    # --- reversion DOWN: rich vs its own 90d median (validated spike-decay edge) ---
    for r in ex.get("stretched_rich", []):
        mid = r.get("mid"); r90 = r.get("ratio_90"); lh = r.get("level_health")
        if not mid or not r90 or r90 <= 1.40:
            continue
        med90 = mid / r90
        # modest partial reversion: give back ~40% of the excess over the 90d level within a week
        target = round(mid - 0.40 * (mid - med90))
        if target >= mid:
            continue
        conf = _clip(0.55 + (r90 - 1.40) * 0.5, 0.50, 0.80)
        calls.append({
            "item_id": r["item_id"], "name": r.get("name"), "rule": "reversion_down",
            "direction": -1, "ref_price": round(mid), "target_price": target,
            "confidence": round(conf, 2),
            "rationale": (f"Trading {r90:.2f}x its own 90-day median (level_health "
                          f"{(lh if lh is not None else float('nan')):.2f}); a rich, extended print. "
                          f"Base case: mean-reversion toward ~{target:,} over {HORIZON_D}d."),
            "_score": r90,
        })

    # --- reversion UP: undervalued vs a STABLE level (validated value edge) ---
    for r in ex.get("deep_value", []):
        mid = r.get("mid"); vd = r.get("value_discount"); lh = r.get("level_health")
        if not mid or not vd or vd <= 0.06:
            continue
        if lh is None or not (0.85 <= lh <= 1.30):      # only from a SOUND level (falling knives excluded)
            continue
        est = mid / (1.0 - vd)                          # implied fair/established level
        target = round(mid + 0.50 * (est - mid))        # recover ~half the discount within a week
        if target <= mid:
            continue
        conf = _clip(0.50 + vd, 0.50, 0.80)
        calls.append({
            "item_id": r["item_id"], "name": r.get("name"), "rule": "reversion_up",
            "direction": 1, "ref_price": round(mid), "target_price": target,
            "confidence": round(conf, 2),
            "rationale": (f"{vd*100:.0f}% below its established level with a stable base "
                          f"(health {lh:.2f}); the validated value-reversion setup. "
                          f"Base case: recovery toward ~{target:,} over {HORIZON_D}d."),
            "_score": vd,
        })

    # Balance the slate: rank WITHIN each rule (their scores aren't comparable — ratio_90 ~1.4-2.5
    # vs value_discount ~0.06-0.3, so a raw sort would always be all-downs) and take a mix.
    half = max(1, MAX_CALLS // 2)
    downs = sorted([c for c in calls if c["rule"] == "reversion_down"], key=lambda c: -c["_score"])[:half]
    ups = sorted([c for c in calls if c["rule"] == "reversion_up"], key=lambda c: -c["_score"])[:half]
    out = (downs + ups)[:MAX_CALLS]
    for c in out:
        c.pop("_score", None)
    return out


def record(period: str, con=None, prose: str | None = None) -> dict:
    """Build the digest, generate calls, and persist both. Returns a summary. Nightly-safe."""
    from .digest import build_digest
    own = con is None
    con = con or connect(read_only=True)
    try:
        packet = build_digest(con=con, period=period)
        if not packet.get("ok"):
            return {"period": period, "ok": False, "reason": packet.get("reason")}
        calls = generate(packet, con=con)
        if prose is None:   # let Claude write it up (grounded in the packet); facts still stored if it can't
            try:
                from . import narrative
                prose = narrative.write(packet, calls=calls, scorecard=scorecard(), period=period)
            except Exception:  # noqa: BLE001
                prose = None
        digest_id = insert_digest(period, packet, prose=prose)
        made = 0
        for c in calls:
            if insert_prediction(digest_id, period, c, HORIZON_D):
                made += 1
        return {"period": period, "ok": True, "digest_id": digest_id, "calls": made,
                "has_prose": bool(prose), "regime": packet.get("regime")}
    finally:
        if own:
            con.close()


def grade(con=None) -> int:
    """Resolve matured predictions against realised prices. Deterministic; idempotent."""
    preds = get_predictions_df()
    if preds is None or preds.empty:
        return 0
    pend = preds[preds["outcome"].isna() | (preds["outcome"] == "pending")].copy()
    if pend.empty:
        return 0
    pend["created_ts"] = pd.to_datetime(pend["created_ts"])
    now = pd.Timestamp.utcnow().tz_localize(None)
    due = pend[pend["created_ts"] + pd.to_timedelta(pend["horizon_days"], unit="D") <= now]
    if due.empty:
        return 0
    own = con is None
    con = con or connect(read_only=True)
    graded = 0
    try:
        for r in due.itertuples():
            iid = int(r.item_id)
            t0 = pd.Timestamp(r.created_ts)
            t1 = t0 + pd.Timedelta(days=int(r.horizon_days))
            path = con.execute(
                """SELECT (avg_high+avg_low)/2.0 AS mid, avg_high, avg_low, ts FROM history
                   WHERE timestep='1h' AND item_id=? AND ts BETWEEN ? AND ?
                     AND avg_high IS NOT NULL AND avg_low IS NOT NULL ORDER BY ts""",
                [iid, t0, t1],
            ).df()
            if path.empty:
                continue
            ref = float(r.ref_price); direction = int(r.direction); target = float(r.target_price)
            end_mid = float(path["mid"].iloc[-1])
            fwd = end_mid / ref - 1.0 if ref > 0 else 0.0
            dir_ok = (fwd > 0) if direction > 0 else (fwd < 0)
            hit = bool(path["avg_high"].max() >= target) if direction > 0 else bool(path["avg_low"].min() <= target)
            outcome = "hit" if hit else ("dir" if dir_ok else "miss")   # hit target / right direction / wrong
            update_prediction_outcome(int(r.id), resolved_ts=t1, actual_price=round(end_mid),
                                      fwd_pct=round(fwd, 4), dir_ok=bool(dir_ok), hit=bool(hit), outcome=outcome)
            graded += 1
    finally:
        if own:
            con.close()
    return graded


def scorecard(con=None) -> dict:
    """Aggregate track record: directional accuracy, target-hit rate, calibration, edge."""
    preds = get_predictions_df()
    if preds is None or preds.empty:
        return {"n": 0, "resolved": 0}
    res = preds[preds["outcome"].isin(["hit", "dir", "miss"])].copy()
    n_open = int((preds["outcome"].isna() | (preds["outcome"] == "pending")).sum())
    if res.empty:
        return {"n": int(len(preds)), "resolved": 0, "open": n_open}
    res["dir_ok"] = res["dir_ok"].astype(bool)
    res["hit"] = res["hit"].astype(bool)
    # "edge": realised move measured in the PREDICTED direction (fwd*sign), median across calls
    signed = res["fwd_pct"].astype("float64") * res["direction"].astype("float64")
    # calibration buckets: stated confidence vs realised directional accuracy
    cal = []
    for lo, hi in [(0.5, 0.6), (0.6, 0.7), (0.7, 0.85)]:
        b = res[(res["confidence"] >= lo) & (res["confidence"] < hi)]
        if len(b):
            cal.append({"band": f"{int(lo*100)}-{int(hi*100)}%", "n": int(len(b)),
                        "realized_dir": round(float(b["dir_ok"].mean()) * 100, 1)})
    by_rule = []
    for rule, b in res.groupby("rule"):
        by_rule.append({"rule": rule, "n": int(len(b)),
                        "dir_acc": round(float(b["dir_ok"].mean()) * 100, 1),
                        "hit_rate": round(float(b["hit"].mean()) * 100, 1),
                        "edge_med_pct": round(float((b["fwd_pct"] * b["direction"]).median()) * 100, 2)})
    brier = float(((res["confidence"] - res["dir_ok"].astype(float)) ** 2).mean())
    return {
        "n": int(len(preds)), "resolved": int(len(res)), "open": n_open,
        "dir_accuracy": round(float(res["dir_ok"].mean()) * 100, 1),
        "target_hit_rate": round(float(res["hit"].mean()) * 100, 1),
        "edge_median_pct": round(float(signed.median()) * 100, 2),
        "edge_mean_pct": round(float(signed.mean()) * 100, 2),
        "brier": round(brier, 3),
        "calibration": cal, "by_rule": by_rule,
    }


def main() -> None:
    import json
    print("scorecard:", json.dumps(scorecard()))
    for p in ("daily", "weekly", "monthly"):
        print(p, "->", json.dumps(record(p)))


if __name__ == "__main__":
    main()
