import { useEffect, useMemo, useState } from "react";
import { addOrder, deleteOrder, editOrder, getItemNames, resolveOrder, type ItemName, type Order } from "../api";
import { gp, gpShort } from "../format";
import { SortTh, useSortable } from "./sortable";

const stateLabel = (s: string) => s.replace("CANCELLED_", "CANCEL ").replace("_", " ");
const stateCls = (s: string) =>
  s === "BUYING" ? "ord-buying"
  : s === "SELLING" ? "ord-selling"
  : s === "BOUGHT" || s === "SOLD" ? "ord-done"
  : s.startsWith("CANCELLED") ? "ord-cancel"
  : "dim";

/** Live + manual Grand Exchange orders. The plugin streams them automatically; on mobile you add
 *  and update them by hand here. Adding/cancelling/completing also adjusts your tracked free gp. */
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

  const [names, setNames] = useState<ItemName[]>([]);
  useEffect(() => { getItemNames().then(setNames).catch(() => {}); }, []);
  const nameToId = useMemo(() => {
    const m = new Map<string, number>();
    for (const n of names) m.set(n.name.toLowerCase(), n.item_id);
    return m;
  }, [names]);

  // --- add-order form ---
  const [q, setQ] = useState("");
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [price, setPrice] = useState("");
  const [qty, setQty] = useState("");
  const [filled, setFilled] = useState("");
  const [formErr, setFormErr] = useState<string | null>(null);

  const submitAdd = () => {
    const item_id = nameToId.get(q.trim().toLowerCase());
    const p = Math.round(Number(price)), tq = Math.round(Number(qty)), fq = Math.max(0, Math.round(Number(filled) || 0));
    if (!item_id) return setFormErr("pick an item from the list");
    if (!(p > 0) || !(tq > 0)) return setFormErr("price and qty must be > 0");
    setFormErr(null);
    addOrder({ item_id, side, price: p, total_qty: tq, filled_qty: fq })
      .then(() => { setQ(""); setPrice(""); setQty(""); setFilled(""); }) // server reconciles free gp; reload re-syncs
      .catch((e) => setFormErr(String(e)))
      .finally(() => reload());
  };

  // --- inline edit ---
  const [editId, setEditId] = useState<string | null>(null);
  const [ef, setEf] = useState({ price: "", filled: "", total: "" });
  const startEdit = (o: Order) => { setEditId(o.order_id); setEf({ price: String(o.price), filled: String(o.filled_qty), total: String(o.total_qty) }); };
  const saveEdit = (o: Order) =>
    run(() => editOrder(o.order_id, { price: Math.round(Number(ef.price)), filled_qty: Math.round(Number(ef.filled)), total_qty: Math.round(Number(ef.total)) }))
      && setEditId(null);

  // the server reconciles free gp on resolve (cancel a buy returns the unfilled reserve; completing
  // a sell credits proceeds); reload re-syncs the value into the toolbar
  const resolveAdj = (o: Order, action: "cancel" | "complete") => run(() => resolveOrder(o.order_id, action));

  // GE 8-slot grid
  const SLOTS = 8;
  const bySlot = new Map<number, Order>();
  const slotCount = new Map<number, number>();
  let unslotted = 0;
  for (const o of rows) {
    if (!o.open) continue;
    if (o.slot == null || o.slot < 0 || o.slot >= SLOTS) { unslotted++; continue; }
    slotCount.set(o.slot, (slotCount.get(o.slot) ?? 0) + 1);
    const prev = bySlot.get(o.slot);
    if (!prev || (o.updated_ts ?? "") > (prev.updated_ts ?? "")) bySlot.set(o.slot, o);
  }
  const used = open; // count all open orders as used slots (manual orders may not carry a slot number)

  return (
    <div className="tbl-scroll">
      <div className="slot-head">GE slots · <b>{used}/{SLOTS}</b> in use · {Math.max(0, SLOTS - used)} free{unslotted > 0 ? ` · ${unslotted} without a slot #` : ""}</div>
      <div className="slot-grid">
        {Array.from({ length: SLOTS }, (_, i) => {
          const o = bySlot.get(i);
          const dup = (slotCount.get(i) ?? 0) > 1;
          return (
            <div key={i} className={`slot-tile ${o ? (o.side === "buy" ? "buy" : "sell") : "free"}`} onClick={o ? () => onSelect(o.item_id) : undefined}>
              <div className="slot-no">Slot {i + 1}{dup && <span className="slot-dup" title="More than one open order in this slot">⚠</span>}</div>
              {o ? (
                <>
                  <div className="slot-item" title={o.name}>{o.name}</div>
                  <div className="slot-meta">{o.side} · {o.filled_qty.toLocaleString()}/{o.total_qty.toLocaleString()}</div>
                  <div className="slot-bar"><span style={{ width: `${Math.round((o.fill_pct ?? 0) * 100)}%` }} /></div>
                </>
              ) : (<div className="slot-free">free</div>)}
            </div>
          );
        })}
      </div>

      <div className="ord-addbar">
        <input list="ordItems" value={q} onChange={(e) => setQ(e.target.value)} placeholder="item name…" style={{ minWidth: 160 }} />
        <datalist id="ordItems">{names.slice(0, 5000).map((n) => <option key={n.item_id} value={n.name} />)}</datalist>
        <select value={side} onChange={(e) => setSide(e.target.value as "buy" | "sell")}>
          <option value="buy">buy</option>
          <option value="sell">sell</option>
        </select>
        <input value={price} onChange={(e) => setPrice(e.target.value)} placeholder="price" inputMode="numeric" style={{ width: 90 }} />
        <input value={qty} onChange={(e) => setQty(e.target.value)} placeholder="qty" inputMode="numeric" style={{ width: 70 }} />
        <input value={filled} onChange={(e) => setFilled(e.target.value)} placeholder="filled" inputMode="numeric" style={{ width: 70 }} />
        <button className="ord-act" onClick={submitAdd}>＋ add order</button>
        {formErr && <span className="neg" style={{ fontSize: 11 }}>{formErr}</span>}
      </div>

      <div className="exp-banner">
        Add/update orders by hand here when you're on mobile (no RuneLite plugin) — adding a buy, cancelling, or
        completing a sell adjusts your <b>free gp</b> automatically. The plugin (when running) streams orders here too.
        {open} open · {rows.length} tracked.
      </div>

      <table className="tbl">
        <thead>
          <tr>
            <SortTh k="state" sort={sort} className="left">State</SortTh>
            <SortTh k="side" sort={sort} className="left">Side</SortTh>
            <SortTh k="name" sort={sort} className="left">Item</SortTh>
            <SortTh k="fill_pct" sort={sort} className="left">Fill</SortTh>
            <SortTh k="price" sort={sort}>Offer</SortTh>
            <SortTh k="spent" sort={sort}>Value</SortTh>
            <th className="left">Edit / fix</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((o) => {
            const editing = editId === o.order_id;
            return (
              <tr key={o.order_id} className={o.item_id === selectedId ? "selected" : ""} onClick={() => onSelect(o.item_id)}>
                <td className="left"><span className={`ord-badge ${stateCls(o.state)}`}>{stateLabel(o.state)}</span></td>
                <td className={`left ${o.side === "buy" ? "pos" : "neg"}`}>{o.side}</td>
                <td className="name left">{o.name}</td>
                <td className="left" onClick={(e) => editing && e.stopPropagation()}>
                  {editing ? (
                    <span className="ord-edit">
                      <input value={ef.filled} onChange={(e) => setEf({ ...ef, filled: e.target.value })} style={{ width: 64 }} />
                      <span className="dim"> / </span>
                      <input value={ef.total} onChange={(e) => setEf({ ...ef, total: e.target.value })} style={{ width: 64 }} />
                    </span>
                  ) : (
                    <div className="ord-fill" title={`${o.filled_qty.toLocaleString()} / ${o.total_qty.toLocaleString()}`}>
                      <span style={{ width: `${Math.round((o.fill_pct ?? 0) * 100)}%` }} />
                      <em>{o.filled_qty.toLocaleString()}/{o.total_qty.toLocaleString()}</em>
                    </div>
                  )}
                </td>
                <td onClick={(e) => editing && e.stopPropagation()}>
                  {editing ? <input value={ef.price} onChange={(e) => setEf({ ...ef, price: e.target.value })} style={{ width: 84 }} /> : gp(o.price)}
                </td>
                <td className="dim">{gpShort(o.spent)}</td>
                <td className="left ord-actions" onClick={(e) => e.stopPropagation()}>
                  {editing ? (
                    <>
                      <button className="ord-act" onClick={() => saveEdit(o)}>save</button>
                      <button className="ord-act" onClick={() => setEditId(null)}>cancel</button>
                    </>
                  ) : (
                    <>
                      <button className="ord-act" title="Edit price / filled / total" onClick={() => startEdit(o)}>✎</button>
                      {o.open && (
                        <>
                          <button className="ord-act" title="Mark fully bought/sold — logs the full quantity as a trade" onClick={() => resolveAdj(o, "complete")}>✓ filled</button>
                          <button className="ord-act" title="Mark cancelled — logs the amount already filled" onClick={() => {
                            const did = o.side === "buy" ? "bought" : "sold";
                            if (window.confirm(`Mark "${o.name}" cancelled? Logs the ${o.filled_qty.toLocaleString()} already ${did} as a trade.`)) resolveAdj(o, "cancel");
                          }}>✗ cancel</button>
                        </>
                      )}
                      <button className="ord-act del" title="Remove this order row" onClick={() => { if (window.confirm("Remove this order from the list?")) run(() => deleteOrder(o.order_id)); }}>🗑</button>
                    </>
                  )}
                </td>
              </tr>
            );
          })}
          {rows.length === 0 && (
            <tr><td colSpan={7} className="left muted">No orders yet — add one above (mobile) or place a GE offer with the RuneLite plugin running.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
