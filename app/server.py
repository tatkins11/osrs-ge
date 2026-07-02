"""FastAPI server: REST API over the signal engine, and serves the React UI.

Run (dev):  .\.venv\Scripts\python.exe -m uvicorn app.server:app --reload --port 8000
The built frontend (frontend/dist) is served at / when present.
"""
from __future__ import annotations

import logging

import pandas as pd
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import portfolio as pf
from .analytics import analyze_item, item_changes, item_series
from .sectors import sector_detail, sector_table
from .config import (
    DEFAULT_BANKROLL,
    DEFAULT_MIN_MARGIN,
    DEFAULT_MIN_VOLUME,
    DEMO_MARKER,
    INGEST_TOKEN,
    PROJECT_ROOT,
    TAX_CAP,
    TAX_MIN_PRICE,
    TAX_RATE,
)
from .db import add_order, delete_order, delete_trade, ensure_trades_db, get_free_gp, get_items_df, get_orders_df, get_updates_df, ingest_offers, insert_plan_log, insert_trade, purge_terminal_orders, record_net_worth, set_free_gp, stats, update_order_fields, update_trade
from .signals import (
    TABLE_COLS,
    Thresholds,
    _reasons,
    _records,
    crash_table,
    flip_table,
    full_table,
    invest_table,
    market_signals,
    overnight_table,
    reversion_table,
    slot_allocator,
    volume_table,
)
from .planner import build_plan
from .growth import compute_growth
from .tax import EXEMPT_ITEM_NAMES

log = logging.getLogger("server")

app = FastAPI(title="OSRS GE Terminal", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Trades use their own DB file (API-owned); create it up front so trade reads/writes
# never fight the collector for the prices-DB lock. The API only ever opens the prices
# DB read-only.
try:
    ensure_trades_db()
except Exception:
    log.exception("could not initialise trades DB")


def get_thresholds(
    bankroll: int = Query(DEFAULT_BANKROLL),   # free gp; clamped >=0 below (a stale negative must not 422)
    min_volume: int = Query(DEFAULT_MIN_VOLUME, ge=0),
    min_gp_volume: int = Query(25_000_000, ge=0),
    max_age_min: float = Query(360.0, ge=0),
    min_margin: int = Query(DEFAULT_MIN_MARGIN),
    min_roi: float = Query(0.004),
    min_profit: int = Query(500_000, ge=0),
    min_price: int = Query(1_000, ge=0),
    max_price: int = Query(2_147_483_647, ge=0),
    crash_pct: float = Query(0.18, gt=0, lt=1),
    vol_spike: float = Query(2.0, gt=1),
    value_min_discount: float = Query(0.08, ge=0, lt=1),
    value_min_confidence: int = Query(80, ge=0, le=100),  # raised 40->80: only 80+ has CI-positive edge
    overnight_disc: float = Query(0.10, gt=0, lt=1),
    z_buy: float = Query(-1.5),
    z_sell: float = Query(1.5),
    max_alloc_frac: float = Query(0.40, gt=0, le=1),  # 0.40: concentrated big bets per slot (Tristan's call)
    min_rt_profit: int = Query(350_000, ge=0),
) -> Thresholds:
    return Thresholds(
        min_volume=min_volume,
        min_gp_volume=min_gp_volume,
        max_price_age_min=max_age_min,
        min_net_margin=min_margin,
        min_roi=min_roi,
        min_profit=min_profit,
        min_price=min_price,
        max_price=max_price,
        crash_pct=crash_pct,
        vol_spike=vol_spike,
        value_min_discount=value_min_discount,
        value_min_confidence=value_min_confidence,
        overnight_disc=overnight_disc,
        z_buy=z_buy,
        z_sell=z_sell,
        bankroll=max(0, bankroll),
        max_alloc_frac=max_alloc_frac,
        min_rt_profit=min_rt_profit,
    )


@app.get("/healthz")
def healthz() -> dict:
    """Liveness probe for the container healthcheck — no DB touch, so it can't be
    blocked by collector write-locks; just confirms uvicorn is serving."""
    return {"status": "ok"}


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "data_mode": "demo" if DEMO_MARKER.exists() else "live", "coverage": stats()}


@app.get("/api/meta")
def meta() -> dict:
    return {
        "data_mode": "demo" if DEMO_MARKER.exists() else "live",
        "coverage": stats(),
        "tax": {"rate": TAX_RATE, "cap": TAX_CAP, "min_price": TAX_MIN_PRICE, "exempt_count": len(EXEMPT_ITEM_NAMES)},
        "defaults": {
            "bankroll": DEFAULT_BANKROLL,
            "min_volume": DEFAULT_MIN_VOLUME,
            "min_margin": DEFAULT_MIN_MARGIN,
        },
    }


@app.get("/api/updates")
def updates_endpoint(limit: int = Query(150, ge=1, le=500)) -> list[dict]:
    """OSRS game updates / blog posts (newest first) for chart event markers."""
    df = get_updates_df().head(limit)
    return [{"ts": str(r.ts), "title": r.title, "url": r.url} for r in df.itertuples()]


@app.get("/api/items")
def items(th: Thresholds = Depends(get_thresholds)) -> list[dict]:
    """Full enriched market table (every item, with signal + sizing)."""
    return full_table(th)


@app.get("/api/flips")
def flips(th: Thresholds = Depends(get_thresholds), limit: int = Query(100, ge=1, le=2000)) -> list[dict]:
    return flip_table(th, limit=limit)


@app.get("/api/signals")
def signals_endpoint(th: Thresholds = Depends(get_thresholds), limit: int = Query(100, ge=1, le=2000)) -> list[dict]:
    return reversion_table(th, limit=limit)


@app.get("/api/crashes")
def crashes_endpoint(th: Thresholds = Depends(get_thresholds), limit: int = Query(100, ge=1, le=2000)) -> list[dict]:
    return crash_table(th, limit=limit)


@app.get("/api/invest")
def invest_endpoint(th: Thresholds = Depends(get_thresholds), limit: int = Query(100, ge=1, le=2000)) -> dict:
    """Value buys (undervalued vs fair value, with confidence + horizon) + SELL signals
    for items you already hold that have gone rich (above fair value)."""
    buys = invest_table(th, limit=limit)
    sells: list[dict] = []
    port = pf.compute()
    open_pos = {int(p["item_id"]): p for p in port.get("open_positions", [])}
    if open_pos:
        ms = market_signals(th)
        if not ms.empty:
            held = ms[ms["item_id"].isin(open_pos.keys())]
            for rec in _records(held, TABLE_COLS):
                p = open_pos[rec["item_id"]]
                est, mid = rec.get("established"), rec.get("mid")
                z = rec.get("z_7d") or 0.0
                pct30 = rec.get("pct_30d") or 0.0
                rich = bool(est and mid and mid >= est * 1.02 and (z >= th.z_sell or pct30 >= 0.75))
                rec.update(qty=p.get("qty"), avg_cost=p.get("avg_cost"),
                           unrealized=p.get("unrealized"), unrealized_pct=p.get("unrealized_pct"), sell_ok=rich)
                if rich:
                    sells.append(rec)
    return {"buys": buys, "sells": sells}


@app.get("/api/volume")
def volume_endpoint(th: Thresholds = Depends(get_thresholds), limit: int = Query(100, ge=1, le=2000)) -> list[dict]:
    """Items with unusual recent volume — an early-warning 'in play' screen."""
    return volume_table(th, limit=limit)


@app.get("/api/overnight")
def overnight_endpoint(th: Thresholds = Depends(get_thresholds), limit: int = Query(100, ge=1, le=2000)) -> list[dict]:
    """Lowball buy offers to place overnight (dip-catch reversion via resting orders)."""
    return overnight_table(th, limit=limit)


@app.get("/api/sectors")
def sectors_endpoint(th: Thresholds = Depends(get_thresholds)) -> dict:
    """Sector grid: cap-weighted index move per sector (1h/6h/24h/7d) + sparkline."""
    return sector_table(th)


@app.get("/api/sector/{key}")
def sector_detail_endpoint(
    key: str,
    th: Thresholds = Depends(get_thresholds),
    timeframe: str = Query("2wk"),
) -> dict:
    """One sector's index time series (2wk/3mo/1yr) + ranked constituents."""
    if timeframe not in {"2wk", "3mo", "1yr"}:
        raise HTTPException(status_code=400, detail="invalid timeframe")
    d = sector_detail(key, th, timeframe=timeframe)
    if d is None:
        raise HTTPException(status_code=404, detail=f"unknown sector '{key}'")
    return d


@app.get("/api/item/{item_id}")
def item_detail(item_id: int, th: Thresholds = Depends(get_thresholds)) -> dict:
    detail = analyze_item(item_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"item {item_id} not found")
    detail["changes"] = item_changes(item_id)
    ms = market_signals(th)
    if not ms.empty:
        row = ms[ms["item_id"] == item_id]
        if not row.empty:
            sr = _records(row, TABLE_COLS)[0]
            sr["reasons"] = _reasons(sr)
            detail["signal_row"] = sr
    return detail


@app.get("/api/item/{item_id}/series")
def item_series_endpoint(item_id: int, timestep: str = Query("1h")) -> dict:
    if timestep not in {"5m", "1h", "6h", "24h"}:
        raise HTTPException(status_code=400, detail="invalid timestep")
    return {"timestep": timestep, "series": item_series(item_id, timestep)}


# --- personal portfolio / trade tracker -------------------------------------
class TradeIn(BaseModel):
    item_id: int
    side: str          # 'buy' | 'sell'
    qty: int
    price: int         # gp per unit you actually paid / received
    note: str | None = None


@app.get("/api/itemnames")
def itemnames() -> list[dict]:
    """Lightweight id+name list for the trade-entry item picker."""
    return [{"item_id": int(r.item_id), "name": r.name} for r in get_items_df().itertuples() if r.name]


@app.get("/api/portfolio")
def portfolio() -> dict:
    return pf.compute()


@app.post("/api/trades")
def add_trade(t: TradeIn) -> dict:
    if t.side not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="side must be 'buy' or 'sell'")
    if t.qty <= 0 or t.price < 0:
        raise HTTPException(status_code=400, detail="qty must be > 0 and price >= 0")
    insert_trade(t.item_id, t.side, t.qty, t.price, t.note or "")
    return {"ok": True}


class TradePatch(BaseModel):
    qty: int | None = None
    price: int | None = None
    note: str | None = None
    side: str | None = None


@app.patch("/api/trades/{trade_id}")
def edit_trade(trade_id: int, t: TradePatch) -> dict:
    if t.side is not None and t.side not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="side must be 'buy' or 'sell'")
    if t.qty is not None and t.qty <= 0:
        raise HTTPException(status_code=400, detail="qty must be > 0")
    if t.price is not None and t.price < 0:
        raise HTTPException(status_code=400, detail="price must be >= 0")
    update_trade(trade_id, qty=t.qty, price=t.price, note=t.note, side=t.side)
    return {"ok": True}


@app.delete("/api/trades/{trade_id}")
def remove_trade(trade_id: int) -> dict:
    delete_trade(trade_id)
    return {"ok": True}


# --- RuneLite live GE-offer ingest + open-orders view -----------------------
def require_ingest_token(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> None:
    if not INGEST_TOKEN:
        raise HTTPException(status_code=503, detail="ingest not configured")
    supplied = authorization[7:].strip() if (authorization and authorization.lower().startswith("bearer ")) else None
    if (supplied or x_api_key) != INGEST_TOKEN:
        raise HTTPException(status_code=401, detail="bad ingest token")


class GeOffer(BaseModel):
    order_id: str
    login: str | None = None
    slot: int | None = None
    item_id: int
    side: str | None = None
    price: int = 0
    total_qty: int = 0
    filled_qty: int = 0
    spent: int = 0
    state: str
    ts: str | None = None


class GeOffersIn(BaseModel):
    offers: list[GeOffer]
    coins: int | None = None    # ground-truth observation: coins in the player's inventory right now


@app.post("/api/ge-offers")
def ge_offers(body: GeOffersIn, _=Depends(require_ingest_token)) -> dict:
    """Ingest a batch of live GE offers from the RuneLite plugin (read-only tracking).
    The optional coins observation feeds the accounting drift detector (a LOWER bound —
    banked gp is invisible to it, so it flags undercounts but never auto-overwrites)."""
    return {"ok": True, **ingest_offers([o.model_dump() for o in body.offers], coins_observed=body.coins)}


@app.get("/api/orders")
def orders(limit: int = 100) -> list[dict]:
    df = get_orders_df()
    if df.empty:
        return []
    names = {int(r.item_id): r.name for r in get_items_df().itertuples() if r.name}

    def tss(v):
        return None if (v is None or pd.isna(v)) else str(v)

    out = []
    for r in df.head(limit).itertuples():
        filled, total, spent = int(r.filled_qty or 0), int(r.total_qty or 0), int(r.spent or 0)
        out.append({
            "order_id": r.order_id, "login": r.login,
            "slot": int(r.slot) if pd.notna(r.slot) else None,
            "item_id": int(r.item_id), "name": names.get(int(r.item_id), str(r.item_id)),
            "side": r.side, "price": int(r.price or 0),
            "total_qty": total, "filled_qty": filled,
            "fill_pct": (filled / total) if total > 0 else None,
            "avg_fill": int(round(spent / filled)) if filled > 0 else None,
            "spent": spent, "state": r.state,
            "opened_ts": tss(r.opened_ts), "updated_ts": tss(r.updated_ts), "completed_ts": tss(r.completed_ts),
            "open": r.state in ("BUYING", "SELLING"),
        })
    return out


class ResolveIn(BaseModel):
    action: str  # 'cancel' | 'complete'


@app.post("/api/orders/{order_id}/resolve")
def resolve_order(order_id: str, body: ResolveIn) -> dict:
    """Manually finalize a stuck order (the plugin missed its terminal event). Logs the
    filled amount as a trade, exactly as a live fill would have."""
    sel = get_orders_df()
    sel = sel[sel["order_id"] == order_id]
    if sel.empty:
        raise HTTPException(status_code=404, detail="order not found")
    r = sel.iloc[0]
    side = str(r.side)
    price, total, filled, spent = int(r.price or 0), int(r.total_qty or 0), int(r.filled_qty or 0), int(r.spent or 0)
    if body.action == "complete":
        state = "SOLD" if side == "sell" else "BOUGHT"
        spent += max(0, total - filled) * price   # assume the unseen remainder filled at the offer price
        filled = total
    elif body.action == "cancel":
        state = "CANCELLED_SELL" if side == "sell" else "CANCELLED_BUY"
    else:
        raise HTTPException(status_code=400, detail="action must be 'cancel' or 'complete'")
    ev = {"order_id": order_id, "item_id": int(r.item_id), "side": side, "price": price,
          "total_qty": total, "filled_qty": filled, "spent": spent, "state": state,
          "slot": int(r.slot) if pd.notna(r.slot) else -1}
    return {"ok": True, **ingest_offers([ev])}


class AddOrderIn(BaseModel):
    item_id: int
    side: str            # 'buy' | 'sell'
    price: int
    total_qty: int
    filled_qty: int = 0
    slot: int | None = None
    tag: str | None = None   # originating engine (overnight/range/crash/flip) for P&L attribution


@app.post("/api/orders/manual")
def add_manual_order(o: AddOrderIn) -> dict:
    """Create an order by hand (phone play, no RuneLite plugin). Also used by the 8-Slot Plan's
    quick-add. Lands in the same orders table the plugin feeds, so the plan/reconcile see it."""
    if o.side not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="side must be 'buy' or 'sell'")
    if o.price <= 0 or o.total_qty <= 0:
        raise HTTPException(status_code=400, detail="price and total_qty must be positive")
    oid = add_order(o.item_id, o.side, o.price, o.total_qty, max(0, o.filled_qty), o.slot, tag=o.tag)
    return {"ok": True, "order_id": oid}


class UpdateOrderIn(BaseModel):
    price: int | None = None
    total_qty: int | None = None
    filled_qty: int | None = None
    slot: int | None = None
    state: str | None = None


@app.patch("/api/orders/{order_id}")
def edit_order(order_id: str, o: UpdateOrderIn) -> dict:
    """Manually update an order (bump filled qty as it fills, reprice, etc.)."""
    update_order_fields(order_id, price=o.price, total_qty=o.total_qty, filled_qty=o.filled_qty, slot=o.slot, state=o.state)
    return {"ok": True}


@app.post("/api/orders/purge")
def purge_orders() -> dict:
    """Clear finished (bought/sold/cancelled) orders from the tracker; trades + P&L untouched."""
    return {"ok": True, "deleted": purge_terminal_orders()}


@app.delete("/api/orders/{order_id}")
def remove_order(order_id: str) -> dict:
    delete_order(order_id)
    return {"ok": True}


@app.get("/api/allocator")
def allocator(th: Thresholds = Depends(get_thresholds)) -> dict:
    """8-slot GE capital allocator. Reads your live open orders (used slots + gp committed in
    open buy offers), then recommends the best flips to fill your FREE slots to maximize total
    gp/day within remaining capital + per-item buy limits. Recommender only -- never auto-places."""
    odf = get_orders_df()
    used, committed, excl = 0, 0.0, []
    if not odf.empty:
        op = odf[odf["state"].isin(["BUYING", "SELLING"])]
        used = len(op)
        excl = [int(x) for x in op["item_id"].tolist()]
        buys = op[op["side"] == "buy"]
        if not buys.empty:
            remain = (buys["total_qty"].fillna(0) - buys["filled_qty"].fillna(0)).clip(lower=0)
            committed = float((remain * buys["price"].fillna(0)).sum())
    free = max(0, 8 - used)
    avail = max(0.0, float(th.bankroll) - committed)
    res = slot_allocator(th, free_slots=free, capital=avail, exclude_items=excl)
    res["used_slots"] = used
    res["committed_capital"] = round(committed)
    res["bankroll"] = round(float(th.bankroll))
    return res


@app.get("/api/patterns")
def patterns() -> dict:
    """Per-item chart-pattern rosters (range plays + repeat-crash-recovery plays) joined to live
    prices with actionable-now flags. First call after startup builds the rosters (~1 min on the
    droplet); cached 12h after. Recommender only."""
    from .patterns import rosters
    return rosters()


@app.get("/api/setarb")
def setarb() -> dict:
    """Set<->components conversion arb scan (GE clerk packs/unpacks sets for free). Validated
    edge: Barrows-family sets carry a persistent +4-10% premium over their pieces. Recommender
    only — lowball the pieces, combine at the clerk, list the set."""
    from .setarb import scan
    return {"rows": scan()}


@app.get("/api/plan")
def plan(th: Thresholds = Depends(get_thresholds),
         mode: str = Query("active", pattern="^(active|2touch)$")) -> dict:
    """The unified 8-slot decision engine: a SELL / HOLD / CUT verdict on every open position
    (with a competitive price + recovery read) plus BUYS for the free slots. mode=2touch swaps the
    presence-required fast flips for overnight-first allocation (place evening, collect morning).
    Reads live positions + open orders. Recommender only."""
    res = build_plan(th, mode=mode)
    try:
        insert_plan_log(res)   # ~hourly snapshot for later calibration; never breaks the response
    except Exception:  # noqa: BLE001
        pass
    try:  # auto-snapshot the growth curve daily (deduped by date) — the 8-Slot Plan is the most-viewed
        record_net_worth(res["net_worth"], res["free_gp"], res["holdings_value"],
                         res.get("realized_total", 0), res.get("unrealized_total", 0), res.get("invested", 0))
    except Exception:  # noqa: BLE001
        pass
    return res


@app.get("/api/growth")
def growth(th: Thresholds = Depends(get_thresholds)) -> dict:
    """Bankroll growth tracker: net worth (cash + holdings), realized growth rate from the trade
    log, the plan's modeled forward rate, an idle-capital flag, and the projected days to 1B/2B/5B."""
    return compute_growth(th)


@app.get("/api/proven-items")
def proven_items(min_n: int = Query(3), kind: str | None = Query(None)) -> list[dict]:
    """Per-item OUT-OF-SAMPLE leaderboard from signal_outcomes: which item+kind combos have actually
    paid (liquidity-floored, net of tax) — the 'proven winners' watchlist, fed by the nightly grading
    job. Ranked by median forward net return; min_n filters thin samples."""
    from .db import get_signal_outcomes_df
    o = get_signal_outcomes_df()
    if o.empty:
        return []
    if kind:
        o = o[o["kind"] == kind]
    g = (o.groupby(["item_id", "name", "kind"])
           .agg(n=("win", "size"), win_rate=("win", "mean"),
                median_ret=("ret_net", "median"), reached=("reached", "mean"))
           .reset_index())
    g = g[g["n"] >= int(min_n)].sort_values("median_ret", ascending=False)
    return [
        {"item_id": int(r.item_id), "name": str(r.name), "kind": str(r.kind), "n": int(r.n),
         "win_rate": float(r.win_rate), "median_ret": float(r.median_ret), "reached": float(r.reached)}
        for r in g.head(60).itertuples()
    ]


class FreeGpIn(BaseModel):
    value: float


@app.get("/api/account")
def account() -> dict:
    """Capital snapshot for syncing the Free gp field — the server-persisted free gp (source of
    truth, auto-adjusted as orders are placed/filled/cancelled) + gp committed in open buy offers."""
    fg = get_free_gp()
    odf = get_orders_df()
    committed = 0
    if not odf.empty:
        op = odf[(odf["state"] == "BUYING") & (odf["side"] == "buy")]
        if not op.empty:
            committed = int(((op["total_qty"].fillna(0) - op["filled_qty"].fillna(0)).clip(lower=0) * op["price"].fillna(0)).sum())
    return {"free_gp": (round(fg) if fg is not None else None), "committed": committed}


@app.post("/api/account/free_gp")
def set_account_free_gp(b: FreeGpIn) -> dict:
    """Set the free-gp baseline (re-anchors current orders as already accounted)."""
    set_free_gp(max(0.0, b.value))
    return {"ok": True, "free_gp": round(get_free_gp() or 0)}


# --- serve the built frontend (if present) ----------------------------------
_DIST = PROJECT_ROOT / "frontend" / "dist"
if _DIST.exists():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="static")
else:
    @app.get("/")
    def _no_ui() -> dict:
        return {"message": "API is up. Build the frontend (npm run build) to serve the dashboard here, or run the Vite dev server."}
