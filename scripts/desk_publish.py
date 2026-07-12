"""Market Desk publisher — the repeatable half of writing an issue.

Run inside the api container (pipe via ssh stdin; no rebuild needed):
    ssh -i ~/.ssh/osrs_ge_deploy root@159.203.178.90 \
      "cd ~/osrs-ge && docker compose exec -T api python - daily" < scripts/desk_publish.py

It grades matured calls, publishes a fresh digest for the period (call dedup prevents stacking),
and prints everything the analyst needs to write the column: the grounded facts packet, the new
digest_id, every open call with a LIVE mark (for an honest scorecard section), and the standing
track record. The prose is then written by Claude in-session (grounded in this output only) and
stored with db.record_digest_prose(digest_id, PROSE).
"""
import json
import sys
import warnings

warnings.filterwarnings("ignore")

from app import db, digest, predictions as pr  # noqa: E402

period = (sys.argv[1] if len(sys.argv) > 1 else "daily").strip().lower()
if period not in ("daily", "weekly", "monthly"):
    raise SystemExit(f"period must be daily|weekly|monthly, got {period!r}")

print("graded matured:", pr.grade())
rec = pr.record(period)
print(f"record {period} ->", json.dumps(rec))

con = db.connect(read_only=True)
pk = digest.build_digest(con=con, period=period)


def rows(lst, keys):
    return [{k: r.get(k) for k in keys} for r in (lst or [])]


mk = ["name", "chg", "mid", "vol_ratio", "ratio_90", "value_discount", "pct_30d"]
out = {
    "digest_id": rec.get("digest_id"),
    "period": period,
    "internals": pk.get("internals"),
    "regime": pk.get("regime"),
    "gainers": rows(pk.get("movers", {}).get("gainers"), mk)[:6],
    "losers": rows(pk.get("movers", {}).get("losers"), mk)[:6],
    "accumulation": rows(pk.get("volume", {}).get("accumulation"), mk)[:5],
    "distribution": rows(pk.get("volume", {}).get("distribution"), mk)[:5],
    "stretched_rich": rows(pk.get("extremes", {}).get("stretched_rich"), mk)[:5],
    "deep_value": rows(pk.get("extremes", {}).get("deep_value"), mk)[:5],
    "events": [e.get("title") for e in (pk.get("events") or [])][:5],
}
secs = (pk.get("sectors") or {}).get("sectors") or []
for hz in ("1d", "7d"):
    out[f"sectors_{hz}"] = sorted(
        [{"label": s.get("label"), hz: (s.get("changes") or {}).get(hz)}
         for s in secs if s.get("label") != "Whole Market" and (s.get("changes") or {}).get(hz) is not None],
        key=lambda x: -x[hz])[:8]

# open calls WITH live marks — the scorecard section must be honest about marks-against
preds = db.get_predictions_df()
op = preds[preds["outcome"] == "pending"]
marks = []
for r in op.itertuples():
    m = con.execute(
        f"""SELECT median((avg_high+avg_low)/2.0) FROM history
            WHERE timestep='1h' AND item_id={int(r.item_id)} AND ts > now() - INTERVAL 3 HOUR"""
    ).fetchone()[0]
    m = float(m) if m else None
    marks.append({"name": r.name, "rule": r.rule, "ref": int(r.ref_price), "target": int(r.target_price),
                  "conf": float(r.confidence), "created": str(r.created_ts)[:10], "mark": m,
                  "vs_ref%": round((m / r.ref_price - 1) * 100, 1) if m else None})
out["open_calls_marked"] = marks
out["scorecard"] = pr.scorecard()
con.close()
print(json.dumps(out, default=str, indent=1))
