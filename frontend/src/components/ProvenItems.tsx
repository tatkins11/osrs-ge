import { useEffect, useState } from "react";
import { getProvenItems, type ProvenItem } from "../api";
import { pct, spct } from "../format";

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
    </div>
  );
}
