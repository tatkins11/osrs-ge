import type { Row } from "../api";
import { gp, gpShort, pct } from "../format";
import { SortTh, useSortable } from "./sortable";

/** Items currently crashed below their established level, with a recovery plan.
 *  Click a header to sort; click a row for the deep dive. */
export function CrashTable({
  rows,
  selectedId,
  onSelect,
}: {
  rows: Row[];
  selectedId: number | null;
  onSelect: (id: number) => void;
}) {
  const { sorted, sort } = useSortable(rows, "crash_score"); // server's update-aware rank (down-weights update-driven crashes)
  return (
    <div className="tbl-scroll">
      <table className="tbl">
        <thead>
          <tr>
            <SortTh k="name" sort={sort} className="left">Item</SortTh>
            <SortTh k="drawdown" sort={sort}>Drawdown</SortTh>
            <SortTh k="established" sort={sort}>Established</SortTh>
            <SortTh k="sell_price" sort={sort}>Buy now</SortTh>
            <SortTh k="crash_target" sort={sort}>Target</SortTh>
            <SortTh k="crash_exp_margin" sort={sort}>Exp / ea</SortTh>
            <SortTh k="crash_exp_roi" sort={sort}>Exp ROI</SortTh>
            <SortTh k="crash_exp_profit" sort={sort}>Profit / 4h</SortTh>
            <SortTh k="alch_support" sort={sort} title="Buy price vs its high-alch floor — low / 🛡 = alching caps the downside on this dip.">Downside</SortTh>
            <SortTh k="vol_daily_7d" sort={sort}>Vol / day</SortTh>
          </tr>
        </thead>
        <tbody>
          {sorted.map((r) => (
            <tr key={r.item_id} className={r.item_id === selectedId ? "selected" : ""} onClick={() => onSelect(r.item_id)}>
              <td className="name left">
                {r.name}
                {r.post_update_drop ? (
                  <span
                    style={{ marginLeft: 6, color: "#f5b53d", fontSize: "0.78em", whiteSpace: "nowrap" }}
                    title={`Crash coincided with a game update${r.post_update_title ? `: "${r.post_update_title}"` : ""}. Update-driven drops historically recover ~30% worse (PF 0.48 vs 0.67) — more likely a permanent repricing. Ranked lower.`}
                  >
                    ⚠ update
                  </span>
                ) : null}
              </td>
              <td className="neg">{pct(r.drawdown, 0)}</td>
              <td>{gp(r.established)}</td>
              <td>{gp(r.sell_price)}</td>
              <td>{gp(r.crash_target)}</td>
              <td className="pos">{gp(r.crash_exp_margin)}</td>
              <td className="pos">{pct(r.crash_exp_roi, 1)}</td>
              <td className="pos">{gpShort(r.crash_exp_profit)}</td>
              <td
                className={r.alch_support == null ? "dim" : r.alch_support <= 0.15 ? "pos" : "dim"}
                title="distance above the high-alch floor"
              >
                {r.alch_support == null ? "–" : (r.alch_support <= 0.15 ? "🛡 " : "") + pct(r.alch_support, 0)}
              </td>
              <td className="dim">{gpShort(r.vol_daily_7d)}</td>
            </tr>
          ))}
          {sorted.length === 0 && (
            <tr>
              <td colSpan={10} className="left muted">
                No crashes clearing the filters right now — they're intermittent. Lower "Min profit", or check back later.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
