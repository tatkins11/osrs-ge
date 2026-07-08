"""Market Desk — the NARRATIVE layer (Claude writes the analyst prose).

Takes the grounded facts packet from digest.py (+ the calls and the scorecard) and asks Claude to
write it up in the voice of a professional market analyst. The model is given the numbers and is
told, firmly, that the DATA block is the ONLY source of truth: it may interpret, connect and frame,
but it may not invent a price, a percentage, or an item name that isn't in the data. That keeps the
column honest -- the same covenant the rest of the system runs on.

Runs on the VPS (no work-firewall concern). Needs ANTHROPIC_API_KEY in the environment; if it's
absent the digest is still archived with its facts, just without prose (never blocks the pipeline).
Uses httpx (already a dependency) rather than pulling in the SDK.

TODO(verify when the tooling gate is back): confirm model id + anthropic-version against the
claude-api skill. Defaults: model 'claude-sonnet-5' (quality/cost sweet spot for a daily column),
version header '2023-06-01'.
"""
from __future__ import annotations

import logging
import os
import time

import httpx

log = logging.getLogger("narrative")

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = os.getenv("OSRS_GE_ANALYST_MODEL", "claude-sonnet-5")
_MAX_TOK = {"daily": 1500, "weekly": 2200, "monthly": 2800}

SYSTEM = (
    "You are the senior market analyst for a desk that trades the Old School RuneScape Grand "
    "Exchange. You write a recurring market column (daily / weekly / monthly) for one sophisticated "
    "reader who runs a quantitative flipping operation. Your voice is sharp, concise and "
    "quantitative -- the register of a top-tier sell-side desk note or a Matt Levine column: "
    "confident, a little wry, never fluffy.\n\n"
    "ABSOLUTE RULES:\n"
    "1. The DATA block is your ONLY source of truth. Every number, item name, sector and percentage "
    "you cite MUST come from it verbatim. Never invent or estimate a figure. If you want to say "
    "something the data doesn't support, don't.\n"
    "2. You MAY interpret, connect dots, and frame a narrative -- that's the job -- but label "
    "interpretation as such; keep facts and opinion distinguishable.\n"
    "3. Predictions come pre-computed in the CALLS block. Present them as the desk's calls with their "
    "targets, horizons and conviction; do not manufacture new ones.\n"
    "4. If the tape is quiet, say so plainly. A boring day honestly reported beats a dramatic day "
    "invented. Never hype.\n"
    "5. All prices are in gp; format large numbers readably (e.g. 45.0M, 3,450). Times are US "
    "Central.\n\n"
    "STRUCTURE (use markdown headings):\n"
    "# <a punchy title>\n"
    "**The Tape** — 2-4 sentence executive summary (regime, breadth, the one thing that mattered).\n"
    "## Market Internals — breadth, participation, volatility in prose.\n"
    "## Sector Watch — leaders/laggards and any rotation.\n"
    "## Standouts — the notable single-item moves, each with the likely why (a catalyst, a "
    "breakout, a crash). Volume-confirmed only.\n"
    "## Event Radar — recent game updates and what they may drive (only if present in data).\n"
    "## The Desk's Call — the predictions, each as: item, direction, target, horizon, conviction, "
    "one-line rationale.\n"
    "## Scorecard — how prior calls are tracking (only if resolved calls exist).\n"
)


def _slim(packet: dict) -> dict:
    """Trim the packet to what the column needs (keeps tokens/cost down, keeps every figure)."""
    def rows(lst, keys):
        out = []
        for r in (lst or []):
            out.append({k: r.get(k) for k in keys if r.get(k) is not None})
        return out
    mv = packet.get("movers") or {}
    vol = packet.get("volume") or {}
    ex = packet.get("extremes") or {}
    mkeys = ["name", "chg", "chg_1d", "chg_7d", "chg_30d", "mid", "vol_ratio", "z_7d", "ratio_90", "value_discount"]
    secs = None
    if packet.get("sectors") and packet["sectors"].get("sectors"):
        secs = [{"label": s.get("label"), "changes": s.get("changes")}
                for s in packet["sectors"]["sectors"][:10]]
    return {
        "period": packet.get("label"),
        "internals": packet.get("internals"),
        "regime": packet.get("regime"),
        "gainers": rows(mv.get("gainers"), mkeys),
        "losers": rows(mv.get("losers"), mkeys),
        "accumulation": rows(vol.get("accumulation"), mkeys),
        "distribution": rows(vol.get("distribution"), mkeys),
        "stretched_rich": rows(ex.get("stretched_rich"), mkeys),
        "deep_value": rows(ex.get("deep_value"), mkeys),
        "sectors": secs,
        "events": packet.get("events"),
    }


def write(packet: dict, calls: list | None = None, scorecard: dict | None = None,
          period: str = "daily", model: str | None = None) -> str | None:
    """Generate the analyst prose for one issue. Returns markdown, or None if unavailable."""
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        log.info("ANTHROPIC_API_KEY not set — archiving facts without analyst prose")
        return None
    import json as _json
    model = model or DEFAULT_MODEL
    user = (
        f"Write the {period.upper()} market column.\n\n"
        f"DATA (facts — the only source of truth):\n{_json.dumps(_slim(packet), default=str)}\n\n"
        f"CALLS (pre-computed predictions to present):\n{_json.dumps(calls or [], default=str)}\n\n"
        f"SCORECARD (track record of prior calls; omit the section if empty):\n{_json.dumps(scorecard or {}, default=str)}\n"
    )
    payload = {
        "model": model, "max_tokens": _MAX_TOK.get(period, 1600), "temperature": 0.6,
        "system": SYSTEM, "messages": [{"role": "user", "content": user}],
    }
    headers = {"x-api-key": key, "anthropic-version": API_VERSION, "content-type": "application/json"}
    for attempt in range(3):
        try:
            r = httpx.post(API_URL, headers=headers, json=payload, timeout=90.0)
            if r.status_code == 200:
                data = r.json()
                text = "\n".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
                return text.strip() or None
            if r.status_code in (429, 500, 502, 503, 529):
                time.sleep(min(2 ** attempt, 8)); continue
            log.warning("analyst API returned %s (attempt %d)", r.status_code, attempt + 1)
            return None
        except httpx.HTTPError as e:
            log.warning("analyst API request failed: %s", e)
            time.sleep(min(2 ** attempt, 8))
    return None
