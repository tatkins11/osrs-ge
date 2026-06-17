# OSRS GE Terminal

A local "trading desk" for Old School RuneScape Grand Exchange flipping. It
collects real-time price data, runs statistical analysis on every item, and
surfaces ranked **flip** and **mean-reversion buy/sell** signals — all net of
the 2% GE tax and respecting buy limits — in a dense dark dashboard.

The edge over typical flip sites: instead of just showing `high - low` spreads
(which are crowded and often stale/one-sided), it **quality-filters** on volume
and price freshness and adds a **time-of-day / mean-reversion layer** built from
your own accumulated price history.

> **This is decision-support only.** You place every trade yourself in-game. No
> game-client automation (that violates Jagex's rules). It uses only the public
> OSRS Wiki prices API and your own clicks.

---

## ⚠️ Network requirement (read this first)

The live data source — the OSRS Wiki prices API (`prices.runescape.wiki`) — is
**blocked on the Significant Wealth Partners corporate network** by the
FortiGuard DNS filter (both `prices.runescape.wiki` and `oldschool.runescape.wiki`
resolve to the FortiGuard sinkhole `208.91.112.55`). Nothing in the code can work
around that, and circumventing a corporate filter on a work device isn't advised.

**Run live data collection on personal infrastructure:**
- your **home/personal computer** on home internet, or
- a cheap **VPS you own** (~$4–6/mo) — bonus: it collects 24/7 with no gaps,
  which is exactly what the time-of-day analysis needs.

**The dashboard itself works on any machine** (including this one) because
`localhost` isn't filtered — see the demo mode below.

---

## Quick start

### A) Explore the UI now, with synthetic demo data (works anywhere)

```bat
scripts\seed_demo.bat        :: generate ~60 days of realistic synthetic history
scripts\run_server.bat       :: then open http://localhost:8000
```

The header shows a **DEMO DATA** badge so you never confuse it with live prices.

### B) Go live (on an unfiltered network — home or a VPS)

```bat
del data\osrs_ge.duckdb      :: clear the demo database first
scripts\backfill.bat         :: seed ~15 days of hourly history from the wiki
scripts\run_collector.bat    :: start the 5-min collector — LEAVE IT RUNNING
scripts\run_server.bat       :: open http://localhost:8000
```

Every day the collector runs, your fine-grained intraday history grows and the
time-of-day signals get sharper.

### Fresh machine

Install **Python 3.11+** and **Node 18+**, copy this folder over, then:

```bat
scripts\setup.bat            :: creates the venv, installs deps, builds the UI
```

On Linux/macOS (e.g. a VPS) run the equivalent commands the `.bat` files wrap:
`python -m venv .venv`, `pip install -r requirements.txt`,
`npm --prefix frontend install && npm --prefix frontend run build`,
`python -m uvicorn app.server:app --port 8000`, `python -m app.collector`.

---

## Verified GE mechanics (what the engine uses)

| Rule | Value |
|---|---|
| Tax | **2%** on the sell side, rounded **down** (raised from 1% on 29 May 2025) |
| Tax-free | items selling **< 50 gp**, plus a fixed exempt list (bonds, skilling staples) |
| Tax cap | **5,000,000 gp per item** (binds at a sale price ≥ 250,000,000) |
| Buy limits | per item, per account, rolling **4-hour** window; read from the API `limit` field |

Flip math: buy at `instasell` (low), sell at `instabuy` (high).
`net_margin = (instabuy − tax(instabuy)) − instasell`,
`profit_per_cycle = net_margin × buy_limit`.

Data source: OSRS Wiki Real-time Prices API — `/mapping`, `/latest`, `/5m`,
`/1h`, `/timeseries`. A descriptive `User-Agent` is required (see config).

---

## Architecture

```
app/
  config.py       settings (paths, User-Agent, tax constants, thresholds)
  api_client.py   OSRS Wiki API client (retry/backoff, OS-trust-store TLS)
  db.py           DuckDB storage (items, snapshots, history)
  tax.py          GE tax engine (+ vectorised + exempt list)
  collector.py    live 5-min poller  ->  python -m app.collector [once]
  backfill.py     /timeseries history seeding  ->  python -m app.backfill
  mockdata.py     synthetic demo data  ->  python -m app.mockdata
  analytics.py    market table + indicators (MA, Bollinger, RSI, z-score) + seasonality
  signals.py      flip-finder + buy/sell classification + position sizing
  server.py       FastAPI REST API + serves the built frontend
frontend/         React + TypeScript + Vite terminal UI (TradingView charts)
scripts/          Windows .bat launchers + selftest.py
data/             DuckDB database (created at runtime; git-ignored)
```

Run the no-network self-test any time: `.venv\Scripts\python.exe scripts\selftest.py`

---

## Configuration (environment variables)

| Var | Default | Purpose |
|---|---|---|
| `OSRS_GE_USER_AGENT` | contains `brian@swp360.com` | **set a real contact** the wiki can reach |
| `OSRS_GE_DB_PATH` | `data/osrs_ge.duckdb` | database location |
| `OSRS_GE_POLL_INTERVAL` | `300` | collector seconds between snapshots |
| `OSRS_GE_CA_BUNDLE` | – | path to a CA PEM, if behind a TLS-inspecting proxy |
| `OSRS_GE_INSECURE_SSL` | – | `1` disables TLS verification (last resort) |

The dashboard's bankroll, volume/margin/ROI thresholds and z-score cutoffs are
adjustable live in the toolbar.

---

## Keeping the collector always-on

- **Simplest:** leave `scripts\run_collector.bat` running in a terminal.
- **Survives reboots (Windows):** Task Scheduler → run `python -m app.collector once`
  every 5 minutes (the `once` mode takes a single snapshot and exits).
- **Best for the time-of-day edge:** a small always-on VPS running the loop 24/7.

---

## Roadmap (next passes)

- Set/component arbitrage (Barrows, godswords) and decombination spreads.
- Update/event awareness (demand spikes around game updates).
- Backtesting harness to validate signals against held-out history.
- Alerts (desktop/Discord) when a watched item hits a buy/sell band.
