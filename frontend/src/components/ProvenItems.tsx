import { useEffect, useState } from "react";
import { getPatterns, getProvenItems, type CrashPlay, type ProvenItem, type RangePlay, type SwingLane } from "../api";
import { gp, pct, spct } from "../format";

const KINDS = ["all", "overnight", "crash", "value", "flip"] as const;
const KIND_CLS: Record<string, string> = {
  overnight: "badge-BUY", crash: "badge-ILLIQUID", value: "badge-HOLD", flip: "badge-FLIP",
};

/** Per-item out-of-sample leaderboard from signal_outcomes — which item+signal combos have actually
 * paid (liquidity-floored, net of tax), fed by the nightly grading job. Measured history, not a live rec. */
export function ProvenItems({
  selectedId,
  onSelect,
  refreshNonce,
}: {
  selectedId: number | null;
  onSelect: (id: number) => void;
  refreshNonce: number;
}) {
  const [kind, setKind] = useState<string>("all");
  const [rows, setRows] = useState<ProvenItem[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [pat, setPat] = useState<{ range: RangePlay[]; crash: CrashPlay[]; swing?: SwingLane[] } | null>(null);

  useEffect(() => {
    let cancelled = false;
    setRows(null);
    setErr(null);
    getProvenItems(kind === "all" ? undefined : kind)
      .then((r) => !cancelled && setRows(r))
      .catch((e) => !cancelled && setErr(String(e)));
    return () => {
      cancelled = true;
    };
  }, [kind, refreshNonce]);

  // pattern rosters rebuild at most every 12h server-side; the first call after a deploy is slow
  useEffect(() => {
    let cancelled = false;
    getPatterns().then((p) => !cancelled && setPat(p)).catch(() => {});
    return () => { cancelled = true; };
  }, [refreshNonce]);

  if (err) return <div className="empty">Proven-items error: {err}</div>;

  return (
    <div className="tbl-scroll">
      <div className="exp-banner">
        <b>Proven items.</b> Per-item <b>out-of-sample</b> results from the nightly grading job — which
        item + signal combos have actually paid (liquidity-floored, net of the 2% tax), ranked by median
        forward net return. This is measured history, not a live recommendation: use it to know which
        items your signals reliably win on. <b>Overnight</b> is the only family with a proven aggregate edge.
      </div>
      <div style={{ display: "flex", gap: 4, margin: "8px 0" }}>
        {KINDS.map((k) => (
          <button key={k} className={`tab ${kind === k ? "active" : ""}`} onClick={() => setKind(k)}>{k}</button>
        ))}
      </div>
      {!rows ? (
        <div className="empty">Loading…</div>
      ) : rows.length === 0 ? (
        <div className="empty">No graded outcomes yet — the nightly job grades signals once they mature past their horizon.</div>
      ) : (
        <table className="tbl">
          <thead>
            <tr>
              <th className="left">Item</th>
              <th className="left">Signal</th>
              <th title="Number of matured signals graded">Samples</th>
              <th>Win rate</th>
              <th title="Liquidity-floored median forward net return, after the 2% tax">Median net</th>
              <th title="Fraction that reached the signal's target">Reached</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr
                key={`${r.item_id}-${r.kind}`}
                className={r.item_id === selectedId ? "selected" : ""}
                onClick={() => onSelect(r.item_id)}
              >
                <td className="name left">{r.name}</td>
                <td className="left"><span className={`badge ${KIND_CLS[r.kind] || ""}`}>{r.kind}</span></td>
                <td className="dim">{r.n}</td>
                <td className={r.win_rate >= 0.6 ? "pos" : ""}>{pct(r.win_rate, 0)}</td>
                <td className={r.median_ret > 0 ? "pos" : "neg"}>{spct(r.median_ret)}</td>
                <td className="dim">{pct(r.reached, 0)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {pat && pat.range.length > 0 && (
        <>
          <div className="slot-head" style={{ marginTop: 18 }}>
            📐 Range plays — items that reliably oscillate in their own band{" "}
            <span className="dim">· buy the item's OWN trailing-60d P20, sell its P70 · OOS-validated +7.7% median/cycle at 80% win, ~2-4 week holds · <b className="pos">AT BAND</b> = buyable now</span>
          </div>
          <table className="tbl">
            <thead>
              <tr>
                <th className="left">Item</th><th className="left">Status</th>
                <th title="Completed simulated cycles in the last year (walk-forward bands, execution-honest)">Cycles</th>
                <th title="Median net return per completed cycle, after tax">Med/cycle</th>
                <th>Win</th><th title="Average days per cycle">~Hold</th>
                <th title="The item's own buy band (trailing-60d 20th percentile)">Buy ≤</th>
                <th title="The sell band (trailing-60d 70th percentile)">Sell ≥</th><th>Now</th>
              </tr>
            </thead>
            <tbody>
              {pat.range.slice(0, 14).map((r) => (
                <tr key={r.item_id} className={r.item_id === selectedId ? "selected" : ""} onClick={() => onSelect(r.item_id)}>
                  <td className="name left">{r.name}</td>
                  <td className="left">{r.broken ? <span className="badge badge-ILLIQUID" title="Price collapsed far below the band — the range is breaking down; do NOT buy this as an oscillation">⚠ broken</span> : r.at_band ? <span className="badge badge-STRONG_BUY">AT BAND</span> : <span className="dim">wait</span>}</td>
                  <td className="dim">{r.cycles}</td>
                  <td className="pos">{spct(r.med_ret)}</td>
                  <td className={r.win >= 0.8 ? "pos" : ""}>{pct(r.win, 0)}</td>
                  <td className="dim">{r.avg_days.toFixed(0)}d</td>
                  <td>{gp(r.p20)}</td>
                  <td>{gp(r.p70)}</td>
                  <td className={r.cur <= r.p20 ? "pos" : "dim"}>{gp(r.cur)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {pat && (pat.swing?.length ?? 0) > 0 && (
        <>
          <div className="slot-head" style={{ marginTop: 18 }}>
            🌊 Swing lanes — items that oscillate WITHIN the day{" "}
            <span className="dim">· stand a bid at the item's own 24h P20, the ask at its P80 · 16d backtest, depth-aware, tax-net, 80-100% win · small capital each, extreme efficiency — set both orders at your sessions and let them cycle</span>
          </div>
          <table className="tbl">
            <thead>
              <tr>
                <th className="left">Item</th>
                <th title="Stand your buy offer here (trailing-24h 20th percentile of real bid-side prints)">Bid ≤</th>
                <th title="Stand your sell offer here (80th percentile of real ask-side prints)">Ask ≥</th>
                <th title="Per-cycle size (max 5% of daily flow / one buy-limit window)">Units</th>
                <th title="Completed cycles per day in the backtest">Cyc/day</th>
                <th>Win</th>
                <th title="Net gp/day at the sized units">gp/day</th>
                <th title="gp/day as % of the lane's capital — the compounding metric">Eff/day</th>
              </tr>
            </thead>
            <tbody>
              {(pat.swing ?? []).slice(0, 14).map((s) => (
                <tr key={s.item_id} className={s.item_id === selectedId ? "selected" : ""} onClick={() => onSelect(s.item_id)}>
                  <td className="name left">{s.name}</td>
                  <td>{s.buy_band == null ? "–" : gp(s.buy_band)}</td>
                  <td>{s.sell_band == null ? "–" : gp(s.sell_band)}</td>
                  <td className="dim">{s.units.toLocaleString()}</td>
                  <td className="dim">{s.cyc_day.toFixed(1)}</td>
                  <td className={s.win >= 0.9 ? "pos" : ""}>{pct(s.win, 0)}</td>
                  <td className="pos">{gp(s.gp_day)}</td>
                  <td className="pos">{s.eff_pct_day == null ? "–" : `${s.eff_pct_day.toFixed(1)}%`}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {pat && pat.crash.length > 0 && (
        <>
          <div className="slot-head" style={{ marginTop: 18 }}>
            🔪 Crash plays — items that have ALWAYS bounced{" "}
            <span className="dim">· 5d drop ≤−20% → buy next day, target +15%, 30d cap · pooled +17.2% median at 72% win, worst tails −28% (size ≤15% of net worth) · <b className="neg">CRASHING NOW</b> = the setup is live</span>
          </div>
          <table className="tbl">
            <thead>
              <tr>
                <th className="left">Item</th><th className="left">Status</th>
                <th title="Historical crash events (>=20% in 5d) in the last year">Crashes</th>
                <th title="Median net recovery per historical crash">Med bounce</th>
                <th>Win</th>
                <th title="Worst historical outcome — this is the risk you size for">Worst</th>
                <th title="Current 5-day return">5d now</th>
              </tr>
            </thead>
            <tbody>
              {pat.crash.slice(0, 14).map((r) => (
                <tr key={r.item_id} className={r.item_id === selectedId ? "selected" : ""} onClick={() => onSelect(r.item_id)}>
                  <td className="name left">{r.name}</td>
                  <td className="left">{r.broken ? <span className="badge badge-ILLIQUID" title="Collapse deeper than anything this item historically recovered from — a regime break, not the validated setup">⚠ regime break</span> : r.crashing_now ? <span className="badge badge-STRONG_SELL">CRASHING NOW</span> : <span className="dim">quiet</span>}</td>
                  <td className="dim">{r.crashes}</td>
                  <td className="pos">{spct(r.med_ret)}</td>
                  <td className={r.win >= 0.85 ? "pos" : ""}>{pct(r.win, 0)}</td>
                  <td className="neg">{spct(r.worst)}</td>
                  <td className={r.r5_now <= -0.2 ? "neg" : "dim"}>{spct(r.r5_now)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}
