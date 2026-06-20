import type { Row } from "../api";
import { gp, gpShort, pct } from "../format";
import { SortTh, useSortable } from "./sortable";

// fill chance: green if it usually fills, red if rarely
const fillCls = (p: number | null | undefined) => (p == null ? "dim" : p >= 0.5 ? "pos" : p >= 0.3 ? "" : "neg");

/** Lowball buy offers to place overnight. Click a header to sort (default: fill chance);
 *  click a row for the deep dive. */
export function OvernightTable({
  rows,
  selectedId,
  onSelect,
}: {
  rows: Row[];
  selectedId: number | null;
  onSelect: (id: number) => void;
}) {
  const { sorted, sort } = useSortable(rows, "on_ev"); // server's value-weighted rank (per-item margin × fill odds)
  return (
    <div className="tbl-scroll">
      <table className="tbl">
        <thead>
          <tr>
            <SortTh k="name" sort={sort} className="left">Item</SortTh>
            <SortTh k="mid" sort={sort} title="Value per item — overnight now leads with higher-value items">Value / ea</SortTh>
            <SortTh k="on_buy" sort={sort}>Buy offer (place tonight)</SortTh>
            <SortTh k="on_units" sort={sort} title="Units you can buy in one limit window (the GE buy limit)">Qty</SortTh>
            <SortTh k="on_exp_profit" sort={sort} title="Total if it fully fills and reverts: margin × qty (informational — ranking now favours per-item value)">Profit / fill</SortTh>
            <SortTh k="on_fill_prob" sort={sort} title="How often this lowball has filled by morning over the last ~2 weeks">Fill chance</SortTh>
            <SortTh k="on_win_rate" sort={sort} title="When it filled, how often selling next midday profited">Win rate</SortTh>
            <SortTh k="on_target" sort={sort}>Sell target</SortTh>
            <SortTh k="on_margin" sort={sort} title="gp captured per item if it reverts — overnight is ranked by this × fill odds">Margin / ea</SortTh>
            <SortTh k="on_roi" sort={sort}>ROI if filled</SortTh>
            <SortTh k="vol_daily_7d" sort={sort}>Vol / day</SortTh>
          </tr>
        </thead>
        <tbody>
          {sorted.map((r) => (
            <tr key={r.item_id} className={r.item_id === selectedId ? "selected" : ""} onClick={() => onSelect(r.item_id)}>
              <td className="name left">{r.name}</td>
              <td>{gp(r.mid)}</td>
              <td>{gp(r.on_buy)}</td>
              <td className="dim">{r.on_units ? Math.round(r.on_units).toLocaleString() : "–"}</td>
              <td className="pos">{gpShort(r.on_exp_profit)}</td>
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
          {sorted.length === 0 && (
            <tr>
              <td colSpan={11} className="left muted">
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
