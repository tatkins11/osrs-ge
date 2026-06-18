import type { Row } from "../api";
import { gp, gpShort, pct } from "../format";

// fill chance: green if it usually fills, red if rarely
const fillCls = (p: number | null | undefined) => (p == null ? "dim" : p >= 0.5 ? "pos" : p >= 0.3 ? "" : "neg");

/** Lowball buy offers to place overnight. Ranked by historical fill chance, then
 *  win-rate when filled. Click a row for the deep dive. */
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
            <th title="How often this lowball has filled by morning over the last ~2 weeks">Fill chance</th>
            <th title="When it filled, how often selling next midday profited">Win rate</th>
            <th>Sell target</th>
            <th>Margin / ea</th>
            <th>ROI if filled</th>
            <th>Vol / day</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.item_id} className={r.item_id === selectedId ? "selected" : ""} onClick={() => onSelect(r.item_id)}>
              <td className="name left">{r.name}</td>
              <td>{gp(r.on_buy)}</td>
              <td
                className={fillCls(r.on_fill_prob)}
                title={r.on_nights ? `${Math.round((r.on_fill_prob ?? 0) * r.on_nights)}/${r.on_nights} nights filled` : ""}
              >
                {pct(r.on_fill_prob, 0)}
              </td>
              <td className={(r.on_win_rate ?? 0) >= 0.55 ? "pos" : "neg"}>{pct(r.on_win_rate, 0)}</td>
              <td>{gp(r.on_target)}</td>
              <td className="pos">{gp(r.on_margin)}</td>
              <td className="pos">{pct(r.on_roi, 1)}</td>
              <td className="dim">{gpShort(r.vol_daily_7d)}</td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td colSpan={8} className="left muted">
                No overnight setups clear the filters right now — good ones are infrequent. Check back later, or lower
                the overnight discount / widen the price range.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
