import type { Row } from "../api";
import { gp, gpShort, pct } from "../format";

/** Items currently crashed below their established level, with a recovery plan.
 *  Server already sorts by expected profit. Click a row for the deep dive. */
export function CrashTable({
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
            <th>Drawdown</th>
            <th>Established</th>
            <th>Buy now</th>
            <th>Target</th>
            <th>Exp / ea</th>
            <th>Exp ROI</th>
            <th>Profit / 4h</th>
            <th title="Buy price vs its high-alch floor — low / 🛡 = alching caps the downside on this dip.">Downside</th>
            <th>Vol / day</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.item_id} className={r.item_id === selectedId ? "selected" : ""} onClick={() => onSelect(r.item_id)}>
              <td className="name left">{r.name}</td>
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
          {rows.length === 0 && (
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
