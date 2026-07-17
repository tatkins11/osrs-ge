# Dispatch brief — retiring the Market Desk (clean close)

*From the vault chief-of-staff session, 7/17. Decision confirmed by Tristan: retire the OSRS Market Desk to focus build energy on the MLB analytics flagship. This is a CLOSE, not an abandonment — the graded record is portfolio evidence and the infrastructure redeploys.*

## 1. The final column — the lifetime scorecard (the important part)
Publish one last Market Desk column that freezes the complete record:
- Every call ever made, graded: lifetime direction %, target-hit %, median edge, **final Brier score and calibration curve** — the full honest ledger, wins and losses.
- A closing self-assessment in the Desk's own voice: what the edge actually was (and wasn't), what N months of daily public grading taught. Same intellectual honesty as ever ("a data point, not an edge") — that voice IS the portfolio asset.
- Purpose: this record becomes evidence in Tristan's MLB front-office portfolio ("I ran a publicly graded prediction desk; here is its complete calibrated record"). Write it so a baseball R&D director could read it cold.

## 2. Stop the cadence
- End the daily morning column/grading routine (wherever it runs — local skill and/or droplet cron).
- Stop the GE price collector on the droplet (frees resources for the MLB stack). **Preserve the SQLite data** — history stays.

## 3. Archive the site
- Freeze the site as a **static snapshot** served by Caddy at the existing address (`https://ge.mapletree-ge.com/`) — lightest footprint, record stays publicly visible.
- Add a small "Desk closed <date> — final scorecard" banner/note so visitors see it's a completed record, not a dead project.

## 4. Droplet handoff (do NOT destroy anything)
- The DigitalOcean droplet stays — it becomes the always-on runner for the MLB pipeline.
- Document for the handoff: droplet specs/region, what's running post-archive (Caddy + static site only), disk usage.
- Future state: MLB engine deploys alongside on a new subdomain (e.g., `mlb.mapletree-ge.com`) — same Caddy, second site block. Subdomain name is Tristan's call at deploy time.
- ⚠️ Domain branding: fine for build phase; revisit a baseball-neutral domain before it appears on job applications (logged in the vault).

## 5. Preserve the code inheritance
- Repo stays intact. Explicitly flag (in a short INHERITANCE.md or the final commit message) the modules the MLB build will lift: grading/scoring logic, Brier + calibration computation, scorecard/column rendering, the daily-cadence pipeline pattern.

## 6. Write-back
When the wind-down is done, log it: one line to `C:\vault\Decisions Log.md` (retirement executed, date) and a status line to `C:\vault\Inbox\MLB Front Office Plan.md` (droplet free for MLB deploy). Per user-level rule 8.
