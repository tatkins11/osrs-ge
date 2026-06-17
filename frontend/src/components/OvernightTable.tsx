import type { Row } from "../api";
import { gp, gpShort, pct } from "../format";

/** Lowball buy offers to place overnight. Server ranks by expected ROI if filled.
 *  Click a row for the deep dive. */
export function OvernightTable({
  rows,
  selectedId,
  onSelect,
}: {
  rows: Row[];
  selectedId: number | null;
  onSelect: (id: number) => void;
}) {
  return (
    <div className="tbl-scroll">
      <table className="tbl">
        <thead>
          <tr>
            <th className="left">Item</th>
            <th>Buy offer (place tonight)</th>
            <th>Sell target (fair value)</th>
            <th>Margin / ea</th>
            <th>ROI if filled</th>
            <th>Daily swing</th>
            <th>Vol / day</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.item_id} className={r.item_id === selectedId ? "selected" : ""} onClick={() => onSelect(r.item_id)}>
              <td className="name left">{r.name}</td>
              <td>{gp(r.on_buy)}</td>
              <td>{gp(r.on_target)}</td>
              <td className="pos">{gp(r.on_margin)}</td>
              <td className="pos">{pct(r.on_roi, 1)}</td>
              <td className="dim" title="7-day volatility — higher = more likely to dip to your offer overnight">
                {pct(r.volatility_7d, 1)}
              </td>
              <td className="dim">{gpShort(r.vol_daily_7d)}</td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td colSpan={7} className="left muted">
                Nothing clears the filters — widen the price range or lower Min ROI.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
