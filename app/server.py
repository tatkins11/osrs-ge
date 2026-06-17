"""FastAPI server: REST API over the signal engine, and serves the React UI.

Run (dev):  .\.venv\Scripts\python.exe -m uvicorn app.server:app --reload --port 8000
The built frontend (frontend/dist) is served at / when present.
"""
from __future__ import annotations

import logging

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import portfolio as pf
from .analytics import analyze_item, item_series
from .sectors import sector_detail, sector_table
from .config import (
    DEFAULT_BANKROLL,
    DEFAULT_MIN_MARGIN,
    DEFAULT_MIN_VOLUME,
    DEMO_MARKER,
    PROJECT_ROOT,
    TAX_CAP,
    TAX_MIN_PRICE,
    TAX_RATE,
)
from .db import delete_trade, ensure_db, get_items_df, insert_trade, stats
from .signals import (
    TABLE_COLS,
    Thresholds,
    _reasons,
    _records,
    crash_table,
    flip_table,
    full_table,
    market_signals,
    reversion_table,
)
from .tax import EXEMPT_ITEM_NAMES

log = logging.getLogger("server")

app = FastAPI(title="OSRS GE Terminal", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_thresholds(
    bankroll: int = Query(DEFAULT_BANKROLL, ge=0),
    min_volume: int = Query(DEFAULT_MIN_VOLUME, ge=0),
    max_age_min: float = Query(90.0, ge=0),
    min_margin: int = Query(DEFAULT_MIN_MARGIN),
    min_roi: float = Query(0.004),
    min_profit: int = Query(500_000, ge=0),
    min_price: int = Query(1_000, ge=0),
    crash_pct: float = Query(0.18, gt=0, lt=1),
    z_buy: float = Query(-1.5),
    z_sell: float = Query(1.5),
    max_alloc_frac: float = Query(0.15, gt=0, le=1),
) -> Thresholds:
    return Thresholds(
        min_volume=min_volume,
        max_price_age_min=max_age_min,
        min_net_margin=min_margin,
        min_roi=min_roi,
        min_profit=min_profit,
        min_price=min_price,
        crash_pct=crash_pct,
        z_buy=z_buy,
        z_sell=z_sell,
        bankroll=bankroll,
        max_alloc_frac=max_alloc_frac,
    )


@app.get("/api/health")
def health() -> dict:
    ensure_db()
    return {"status": "ok", "data_mode": "demo" if DEMO_MARKER.exists() else "live", "coverage": stats()}


@app.get("/api/meta")
def meta() -> dict:
    ensure_db()
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


@app.get("/api/sectors")
def sectors_endpoint(th: Thresholds = Depends(get_thresholds)) -> dict:
    """Sector grid: cap-weighted index move per sector (1h/6h/24h/7d) + sparkline."""
    return sector_table(th)


@app.get("/api/sector/{key}")
def sector_detail_endpoint(key: str, th: Thresholds = Depends(get_thresholds)) -> dict:
    """One sector's index time series + ranked constituents."""
    d = sector_detail(key, th)
    if d is None:
        raise HTTPException(status_code=404, detail=f"unknown sector '{key}'")
    return d


@app.get("/api/item/{item_id}")
def item_detail(item_id: int, th: Thresholds = Depends(get_thresholds)) -> dict:
    detail = analyze_item(item_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"item {item_id} not found")
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


@app.delete("/api/trades/{trade_id}")
def remove_trade(trade_id: int) -> dict:
    delete_trade(trade_id)
    return {"ok": True}


# --- serve the built frontend (if present) ----------------------------------
_DIST = PROJECT_ROOT / "frontend" / "dist"
if _DIST.exists():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="static")
else:
    @app.get("/")
    def _no_ui() -> dict:
        return {"message": "API is up. Build the frontend (npm run build) to serve the dashboard here, or run the Vite dev server."}
