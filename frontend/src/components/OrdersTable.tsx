import { deleteOrder, resolveOrder, type Order } from "../api";
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
  reload,
}: {
  rows: Order[];
  selectedId: number | null;
  onSelect: (id: number) => void;
  reload: () => void;
}) {
  const { sorted, sort } = useSortable(rows, "updated_ts");
  const open = rows.filter((o) => o.open).length;
  const run = (fn: () => Promise<unknown>) => fn().catch(() => {}).finally(() => reload());

  // GE allows 8 open offers at once — map current open orders to their slots (0–7)
  const SLOTS = 8;
  const bySlot = new Map<number, Order>();
  const slotCount = new Map<number, number>();
  for (const o of rows) {
    if (!o.open || o.slot == null || o.slot < 0 || o.slot >= SLOTS) continue;
    slotCount.set(o.slot, (slotCount.get(o.slot) ?? 0) + 1);
    const prev = bySlot.get(o.slot);
    if (!prev || (o.updated_ts ?? "") > (prev.updated_ts ?? "")) bySlot.set(o.slot, o); // newest wins on a collision
  }
  const used = bySlot.size;

  return (
    <div className="tbl-scroll">
      <div className="slot-head">
        GE slots · <b>{used}/{SLOTS}</b> in use · {SLOTS - used} free
      </div>
      <div className="slot-grid">
        {Array.from({ length: SLOTS }, (_, i) => {
          const o = bySlot.get(i);
          const dup = (slotCount.get(i) ?? 0) > 1;
          return (
            <div
              key={i}
              className={`slot-tile ${o ? (o.side === "buy" ? "buy" : "sell") : "free"}`}
              onClick={o ? () => onSelect(o.item_id) : undefined}
            >
              <div className="slot-no">
                Slot {i + 1}
                {dup && (
                  <span className="slot-dup" title="More than one open order tracked in this slot — clear the extra in the table below">⚠</span>
                )}
              </div>
              {o ? (
                <>
                  <div className="slot-item" title={o.name}>{o.name}</div>
                  <div className="slot-meta">{o.side} · {o.filled_qty.toLocaleString()}/{o.total_qty.toLocaleString()}</div>
                  <div className="slot-bar"><span style={{ width: `${Math.round((o.fill_pct ?? 0) * 100)}%` }} /></div>
                </>
              ) : (
                <div className="slot-free">free</div>
              )}
            </div>
          );
        })}
      </div>
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
            <th className="left">Fix</th>
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
              <td className="left ord-actions" onClick={(e) => e.stopPropagation()}>
                {o.open && (
                  <>
                    <button
                      className="ord-act"
                      title="Mark fully bought/sold — logs the full quantity as a trade"
                      onClick={() => run(() => resolveOrder(o.order_id, "complete"))}
                    >
                      ✓ filled
                    </button>
                    <button
                      className="ord-act"
                      title="Mark cancelled — logs the amount already filled as a trade"
                      onClick={() => {
                        const did = o.side === "buy" ? "bought" : "sold";
                        if (window.confirm(`Mark "${o.name}" cancelled? Logs the ${o.filled_qty.toLocaleString()} already ${did} as a trade.`))
                          run(() => resolveOrder(o.order_id, "cancel"));
                      }}
                    >
                      ✗ cancel
                    </button>
                  </>
                )}
                <button
                  className="ord-act del"
                  title="Remove this order row (does not affect already-logged trades)"
                  onClick={() => {
                    if (window.confirm("Remove this order from the list?")) run(() => deleteOrder(o.order_id));
                  }}
                >
                  🗑
                </button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td colSpan={9} className="left muted">
                No orders yet — install the RuneLite plugin and place a GE offer; it'll appear here within seconds.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
