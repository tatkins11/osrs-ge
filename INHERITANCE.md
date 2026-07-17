# Code inheritance — what the MLB analytics build lifts from the Market Desk

*Written at wind-down (2026-07-17). The OSRS Market Desk is retired but its machinery is
market-agnostic; these modules port directly to a baseball prediction/grading pipeline. Repo stays
intact — nothing here is deleted.*

## The reusable spine (highest value)
- **Grading / scoring engine** — `app/predictions.py::grade()`. Deterministic, idempotent, matures
  calls by CALENDAR day (so a once-daily runner grades that day's book), classifies each call
  hit / dir / miss where **HIT = reached target AND ended in the right direction** (the intraday-
  touch loophole was closed 7/17). Swap the price-path source and it grades any dated prediction.
- **Track-record math** — `app/predictions.py::scorecard()`. Direction accuracy, target-hit rate,
  winsorized median/mean edge, **Brier score**, calibration bands (stated confidence vs realized),
  and per-rule breakdown. Pure function of the predictions table; reuse verbatim.
- **Prediction ledger** — `app/db.py` `predictions` + `digests` tables and helpers
  (`insert_prediction` with open-call dedup, `record_digest_prose`, `get_predictions_df`). The
  schema (item/ref/target/horizon/confidence/rationale → resolved/actual/fwd/dir_ok/hit/outcome)
  is domain-neutral: rename "item" to "team/player/prop" and it holds.

## The cadence pattern
- **Publish → ground → write → store → grade** — `scripts/desk_publish.py` +
  `.claude/skills/market-report/SKILL.md`. The pipeline: compute a facts packet, generate calls
  ONLY from validated edges (never momentum/vibes), have the model write prose constrained to the
  packet's numbers, store, and auto-grade matured calls on the single daily run. The **covenant**
  (every figure traces to computed data; scorecard prints misses as loudly as hits) is the
  transferable methodology and the portfolio asset.

## The facts / rendering layer (adapt per domain)
- `app/digest.py::build_digest()` — market-internals engine (breadth, movers, volume anomalies,
  extremes, regime read, event radar). Structure generalizes; the specific signals are OSRS-specific.
- `frontend/src/components/MarketDesk.tsx` — scorecard tiles, open + graded ledger tables (with the
  "Graded at" price), and a dependency-free markdown-lite renderer for the column.

## Do NOT lift
- OSRS-specific signal definitions (ratio_90 vs 90-day median, GE tax, overnight/day-lane engines,
  the planner). Baseball has its own edges — the *framework* ports, the *signals* do not.
