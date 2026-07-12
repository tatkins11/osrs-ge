---
name: market-report
description: Write and publish the Market Desk analyst report (daily by default; pass "weekly" or "monthly"). Grades matured calls, publishes a fresh grounded digest, writes the column in the desk voice, stores it, and verifies it's live. Use whenever the user asks for the daily/weekly/monthly market report, market update, or "write today's column".
---

# Market Desk report — repeatable publishing procedure

Produce one issue of the Market Desk column. Period = the argument if given (`daily` | `weekly` | `monthly`), default `daily`.

## Step 1 — Publish the digest and collect the facts

Run the canonical publisher (piped from the local checkout — no image rebuild needed):

```
ssh -i ~/.ssh/osrs_ge_deploy root@159.203.178.90 "cd ~/osrs-ge && docker compose exec -T api python - <PERIOD>" < scripts/desk_publish.py
```

This grades matured calls, publishes a fresh digest (prediction dedup prevents call stacking), and prints JSON containing: `digest_id`, internals, regime, movers, volume anomalies, extremes, sectors, events, **every open call with a live mark**, and the scorecard. If it errors, fix the cause before writing anything.

## Step 2 — Write the column (the covenant)

Write the issue in markdown. HARD RULES — these are the product:

1. **The Step-1 JSON is the only source of truth for numbers.** Every price, %, multiple, count, and item name must come from it verbatim (or from prior issues' stored prose when explicitly citing "Wednesday's issue said…"). Never estimate, extrapolate, or invent a figure. If the data doesn't support a sentence, cut the sentence.
2. **Voice**: senior desk analyst — sharp, quantitative, a little wry (Matt Levine register). Interpretation is the job, but keep fact and opinion distinguishable. A boring tape honestly reported beats drama invented. No hype, no filler.
3. **Scorecard honesty is the franchise**: print marks that are AGAINST open calls just as prominently as ones in favor; note when the next grades mature. Never soften a miss.
4. gp prices formatted readably (`5,317`, `1.48M`, `4.43M`). Times US Central. Screen thin/penny spike-class movers out of the narrative focus (they may be named as noise being screened out).

Structure (markdown headings):

```
# <punchy title>
**The Tape** — 2-4 sentence executive summary (regime, breadth, the one thing that mattered).
## Market Internals — breadth, participation, volatility, near-highs/lows ratio (compare to the prior issue when it makes a trend).
## Sector Watch — leaders/laggards, rotation, tie to events if supported.
## Standouts — volume-confirmed single-item stories with the likely why; continuity with prior issues' stories where real.
## Event Radar — recent game updates from the packet and what they may drive (omit if empty).
## The Desk's Call — the NEW calls this issue: item, direction, ref, target, horizon, conviction, one-line rationale.
## Scorecard — open count, matured grades (hit/dir/miss + calibration once any exist), and honest live marks on standing calls.
```

## Step 3 — Store the prose

Write a scratchpad Python file with the column embedded (this avoids ssh quoting hell — the established pattern):

```python
# -*- coding: utf-8 -*-
from app import db
PROSE = r"""<the column>"""
db.record_digest_prose(<digest_id from Step 1>, PROSE)
d = db.get_digests_df(); row = d[d.id == <digest_id>].iloc[0]
print("stored:", len(row["prose"] or ""), "chars")
import urllib.request, json
r = json.load(urllib.request.urlopen("http://127.0.0.1:8000/api/market-desk", timeout=40))
print("endpoint", "<PERIOD>", "id:", r["latest"]["<PERIOD>"]["id"], "| prose:", bool(r["latest"]["<PERIOD>"].get("prose")))
```

Pipe it: `ssh -i ~/.ssh/osrs_ge_deploy root@159.203.178.90 "cd ~/osrs-ge && docker compose exec -T api python -" < <file>`

Verify the endpoint reports the SAME digest_id with `prose: True`. If the endpoint's latest id differs (a nightly run published after Step 1), re-run Step 1 rather than attaching prose to a stale issue.

## Step 4 — Report back

Give the user: the title, 2-3 headline findings, the new calls, notable scorecard movement (especially marks against), and note the issue is live on the Market Desk tab. Do not paste the whole column into chat.

## Context

- The pipeline (facts, calls, grading) is deterministic and validated: calls come only from the validated reversion edges with liquidity/regime guards; momentum is deliberately excluded. Do not add new call types here — that requires a backtest first.
- The collector auto-publishes facts-only digests nightly; this skill's fresh publish supersedes it for the day (call dedup keeps the ledger clean).
- Weekly runs Mondays / monthly on the 1st by convention, but publishing any period on demand is fine.
