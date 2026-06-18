import type { Order } from "../api";
import { gp, gpShort } from "../format";
import { SortTh, useSortable } from "./sortable";

const stateLabel = (s: string) => s.replace("CANCELLED_", "CANCEL ").replace("_", " ");
const stateCls = (s: string) =>
  s === "BUYING" ? "ord-buying"
  : s === "SELLING" ? "ord-selling"
  : s === "BOUGHT" || s === "SOLD" ? "ord-done"
  : s.startsWith("CANCELLED") ? "ord-cancel"
  : "dim";

/** Live Grand Exchange orders streamed from the RuneLite plugin. Click a row for the chart. */
export function OrdersTable({
  rows,
  selectedId,
  onSelect,
}: {
  rows: Order[];
  selectedId: number | null;
  onSelect: (id: number) => void;
}) {
  const { sorted, sort } = useSortable(rows, "updated_ts");
  const open = rows.filter((o) => o.open).length;
  return (
    <div className="tbl-scroll">
      <div className="exp-banner">
        Live GE orders streamed from the <b>RuneLite plugin</b> (read-only). Filled orders auto-log as trades and feed
        your Portfolio round-trips — no manual entry. {open} open · {rows.length} tracked. No rows? Set up the plugin
        (see <code>runelite-plugin/README</code>).
      </div>
      <table className="tbl">
        <thead>
          <tr>
            <SortTh k="state" sort={sort} className="left">State</SortTh>
            <SortTh k="side" sort={sort} className="left">Side</SortTh>
            <SortTh k="name" sort={sort} className="left">Item</SortTh>
            <SortTh k="fill_pct" sort={sort} className="left">Fill</SortTh>
            <SortTh k="price" sort={sort}>Offer</SortTh>
            <SortTh k="avg_fill" sort={sort}>Avg fill</SortTh>
            <SortTh k="spent" sort={sort}>Value</SortTh>
            <SortTh k="updated_ts" sort={sort} className="left">Updated (UTC)</SortTh>
          </tr>
        </thead>
        <tbody>
          {sorted.map((o) => (
            <tr key={o.order_id} className={o.item_id === selectedId ? "selected" : ""} onClick={() => onSelect(o.item_id)}>
              <td className="left"><span className={`ord-badge ${stateCls(o.state)}`}>{stateLabel(o.state)}</span></td>
              <td className={`left ${o.side === "buy" ? "pos" : "neg"}`}>{o.side}</td>
              <td className="name left">{o.name}</td>
              <td className="left">
                <div className="ord-fill" title={`${o.filled_qty.toLocaleString()} / ${o.total_qty.toLocaleString()}`}>
                  <span style={{ width: `${Math.round((o.fill_pct ?? 0) * 100)}%` }} />
                  <em>{o.filled_qty.toLocaleString()}/{o.total_qty.toLocaleString()}</em>
                </div>
              </td>
              <td>{gp(o.price)}</td>
              <td>{o.avg_fill ? gp(o.avg_fill) : <span className="dim">–</span>}</td>
              <td className="dim">{gpShort(o.spent)}</td>
              <td className="left dim">{o.updated_ts ? o.updated_ts.slice(0, 16).replace("T", " ") : "–"}</td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td colSpan={8} className="left muted">
                No orders yet — install the RuneLite plugin and place a GE offer; it'll appear here within seconds.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
