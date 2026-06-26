import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  addTrade, deleteTrade, getItemNames, getPortfolio, updateTrade,
  type ItemName, type Portfolio as Pf, type TradePrefill,
} from "../api";
import { fmtTsCentral, gp, gpShort, pct } from "../format";
import { SortTh, useSortable } from "./sortable";

function Tile({ k, v, cls = "" }: { k: string; v: ReactNode; cls?: string }) {
  return (
    <div className="tile">
      <div className="k">{k}</div>
      <div className={`v ${cls}`}>{v}</div>
    </div>
  );
}

/** Cumulative realized P&L over time as a tiny inline sparkline. */
function EquitySpark({ data }: { data: { ts: string; cum: number }[] }) {
  if (data.length < 2) return null;
  const w = 280, h = 48, pad = 4;
  const ys = data.map((d) => d.cum);
  const min = Math.min(0, ...ys), max = Math.max(0, ...ys);
  const span = max - min || 1;
  const x = (i: number) => pad + (i / (data.length - 1)) * (w - 2 * pad);
  const y = (v: number) => pad + (1 - (v - min) / span) * (h - 2 * pad);
  const pts = data.map((d, i) => `${x(i).toFixed(1)},${y(d.cum).toFixed(1)}`).join(" ");
  const last = ys[ys.length - 1];
  return (
    <svg width={w} height={h} style={{ display: "block" }} aria-label="realized P&L curve">
      <line x1={pad} y1={y(0)} x2={w - pad} y2={y(0)} stroke="#1e2a39" strokeWidth={1} />
      <polyline points={pts} fill="none" stroke={last >= 0 ? "#25d07d" : "#ff5b6e"} strokeWidth={1.5} />
    </svg>
  );
}

export function Portfolio({
  refreshNonce = 0,
  prefill = null,
  onSelect,
}: {
  refreshNonce?: number;
  prefill?: (TradePrefill & { nonce: number }) | null;
  onSelect?: (id: number) => void;
}) {
  const [pf, setPf] = useState<Pf | null>(null);
  const [names, setNames] = useState<ItemName[]>([]);
  const [itemText, setItemText] = useState("");
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [qty, setQty] = useState("");
  const [price, setPrice] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const qtyRef = useRef<HTMLInputElement>(null);

  // inline edit state for the trade log (bump qty as an order fills, or fix a price)
  const [editId, setEditId] = useState<number | null>(null);
  const [eQty, setEQty] = useState("");
  const [ePrice, setEPrice] = useState("");

  const load = () => getPortfolio().then(setPf).catch(() => {});
  useEffect(() => { load(); }, [refreshNonce]);
  useEffect(() => { getItemNames().then(setNames).catch(() => {}); }, []);

  // prefill from a signal row's "＋ log" or a trade-log "repeat" (App bumps nonce each time)
  useEffect(() => {
    if (!prefill) return;
    setItemText(prefill.name);
    setSide(prefill.side);
    setPrice(prefill.price ? String(prefill.price) : "");
    setQty("");
    setErr(null);
    qtyRef.current?.focus();
  }, [prefill]);

  const nameToId = useMemo(() => {
    const m = new Map<string, number>();
    for (const n of names) m.set(n.name.toLowerCase(), n.item_id);
    return m;
  }, [names]);

  const submit = async () => {
    const id = nameToId.get(itemText.trim().toLowerCase());
    if (id == null) { setErr("Pick an item from the list."); return; }
    const q = Number(qty), p = Number(price);
    if (!(q > 0) || !(p >= 0)) { setErr("Enter a valid quantity and price."); return; }
    setBusy(true); setErr(null);
    try {
      await addTrade({ item_id: id, side, qty: q, price: p });
      setItemText(""); setQty(""); setPrice("");
      await load();
    } catch (e) { setErr(String(e)); } finally { setBusy(false); }
  };

  const startEdit = (id: number, q: number, p: number) => { setEditId(id); setEQty(String(q)); setEPrice(String(p)); };
  const saveEdit = async () => {
    if (editId == null) return;
    const q = Number(eQty), p = Number(ePrice);
    if (!(q > 0) || !(p >= 0)) { setErr("Enter a valid quantity and price."); return; }
    await updateTrade(editId, { qty: q, price: p }).catch((e) => setErr(String(e)));
    setEditId(null);
    await load();
  };
  const repeat = (name: string, s: string, p: number) => {
    setItemText(name); setSide(s === "sell" ? "sell" : "buy"); setPrice(String(p)); setQty("");
    setErr(null); qtyRef.current?.focus();
  };
  const del = async (id: number) => {
    if (!window.confirm("Delete this trade? P&L will recompute.")) return;
    await deleteTrade(id).catch(() => {});
    load();
  };

  const { sorted: sortedPos, sort: posSort } = useSortable(pf?.open_positions ?? [], "unrealized");
  const { sorted: sortedTrips, sort: tripSort } = useSortable(pf?.closed_trips ?? [], "sell_ts");
  const st = pf?.stats;
  const maxAbsItem = useMemo(
    () => Math.max(1, ...((pf?.realized_by_item ?? []).map((r) => Math.abs(r.net)))),
    [pf?.realized_by_item]
  );

  return (
    <div className="portfolio">
      <div className="panel-section">
        <h4>Log a trade</h4>
        <div className="trade-form">
          <input list="itemlist" placeholder="item name…" value={itemText} onChange={(e) => setItemText(e.target.value)} />
          <datalist id="itemlist">
            {names.map((n) => (<option key={n.item_id} value={n.name} />))}
          </datalist>
          <select value={side} onChange={(e) => setSide(e.target.value as "buy" | "sell")}>
            <option value="buy">Buy</option>
            <option value="sell">Sell</option>
          </select>
          <input ref={qtyRef} placeholder="qty" value={qty} onChange={(e) => setQty(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && submit()} />
          <input placeholder="price / ea" value={price} onChange={(e) => setPrice(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && submit()} />
          <button className="refresh" disabled={busy} onClick={submit}>Add trade</button>
          {err && <span className="neg" style={{ fontSize: 11 }}>{err}</span>}
        </div>
        <div className="note" style={{ marginTop: 8 }}>
          Sells are taxed 2% automatically; P&amp;L matches sells to your oldest buys (FIFO) for per-trade net.
          <b> Order filling in pieces?</b> Just <b>✎ edit</b> the buy's qty below as it fills — no need to re-enter it.
        </div>
      </div>

      {pf && st && (
        <>
          <div className="panel-section">
            <div className="tiles" style={{ gridTemplateColumns: "repeat(6, 1fr)" }}>
              <Tile k="Realized P&L" v={gp(pf.realized_total)} cls={pf.realized_total >= 0 ? "pos" : "neg"} />
              <Tile k="Unrealized P&L" v={gp(pf.unrealized_total)} cls={pf.unrealized_total >= 0 ? "pos" : "neg"} />
              <Tile k="Win rate" v={st.n_closed ? `${pct(st.win_rate, 0)} · ${st.n_closed}` : "–"} />
              <Tile k="Tax paid" v={gpShort(st.total_tax)} cls="neg" />
              <Tile k="Invested (open)" v={gpShort(pf.invested)} />
              <Tile k="Open / trades" v={`${pf.n_open} / ${pf.n_trades}`} />
            </div>
          </div>

          {pf.n_alerts > 0 && (
            <div className="crash-banner">
              ⚑ {pf.n_alerts} holding{pf.n_alerts > 1 ? "s have" : " has"} reverted to (or above) fair value —
              consider selling. Marked <b>SELL</b> below.
            </div>
          )}

          <div className="panel-section">
            <h4>
              Open positions{" "}
              <span className="dim" style={{ fontWeight: 400, textTransform: "none", letterSpacing: 0 }}>
                · net of the 2% sell tax; <b>Rec. sell</b> = where to place a sell offer now (green once it clears
                breakeven). Click a row for the chart.
              </span>
            </h4>
            <table className="tbl">
              <thead>
                <tr>
                  <SortTh k="name" sort={posSort} className="left">Item</SortTh>
                  <SortTh k="qty" sort={posSort}>Qty</SortTh>
                  <SortTh k="avg_cost" sort={posSort}>Avg cost</SortTh>
                  <SortTh k="breakeven" sort={posSort}>Breakeven</SortTh>
                  <SortTh k="cur_price" sort={posSort}>Rec. sell</SortTh>
                  <SortTh k="target" sort={posSort} title="7-day established fair value — the price to aim to sell at">Target</SortTh>
                  <SortTh k="market_value" sort={posSort}>Market value</SortTh>
                  <SortTh k="unrealized" sort={posSort}>Unrealized</SortTh>
                  <SortTh k="unrealized_pct" sort={posSort}>%</SortTh>
                  <SortTh k="status" sort={posSort} className="left">Status</SortTh>
                </tr>
              </thead>
              <tbody>
                {sortedPos.map((p) => (
                  <tr key={p.item_id} className="clickable" onClick={() => onSelect?.(p.item_id)}>
                    <td className="name left">{p.name}</td>
                    <td>{p.qty.toLocaleString()}</td>
                    <td>{gp(p.avg_cost)}</td>
                    <td className="dim">{gp(p.breakeven)}</td>
                    <td className={(p.cur_price ?? 0) >= (p.breakeven ?? 0) ? "pos" : "neg"}>{gp(p.cur_price)}</td>
                    <td className="dim">{gp(p.target)}</td>
                    <td>{gp(p.market_value)}</td>
                    <td className={(p.unrealized ?? 0) >= 0 ? "pos" : "neg"}>{gp(p.unrealized)}</td>
                    <td className={(p.unrealized_pct ?? 0) >= 0 ? "pos" : "neg"}>{pct(p.unrealized_pct, 1)}</td>
                    <td className="left">
                      {p.status === "sell" ? (<span className="badge badge-SELL">SELL</span>)
                        : p.status === "underwater" ? (<span className="neg">underwater</span>)
                        : (<span className="dim">hold</span>)}
                    </td>
                  </tr>
                ))}
                {pf.open_positions.length === 0 && (
                  <tr><td colSpan={10} className="left muted">No open positions — log a buy above.</td></tr>
                )}
              </tbody>
            </table>
          </div>

          <div className="panel-section">
            <h4>
              Closed trades (round-trips){" "}
              <span className="dim" style={{ fontWeight: 400, textTransform: "none", letterSpacing: 0 }}>
                {st.n_closed > 0
                  ? `· ${pct(st.win_rate, 0)} win · avg win ${gpShort(st.avg_win)} / loss ${gpShort(st.avg_loss)} · best ${gpShort(st.best)} / worst ${gpShort(st.worst)} · avg hold ${st.avg_hold_days?.toFixed(1)}d`
                  : "· sells matched FIFO to your buys, net of tax"}
              </span>
            </h4>
            {pf.equity_curve.length > 1 && (
              <div style={{ margin: "2px 0 10px" }}>
                <div className="dim" style={{ fontSize: 11, marginBottom: 2 }}>Cumulative realized P&L</div>
                <EquitySpark data={pf.equity_curve} />
              </div>
            )}
            <table className="tbl">
              <thead>
                <tr>
                  <SortTh k="sell_ts" sort={tripSort} className="left">Sold</SortTh>
                  <SortTh k="name" sort={tripSort} className="left">Item</SortTh>
                  <SortTh k="qty" sort={tripSort}>Qty</SortTh>
                  <SortTh k="buy_avg" sort={tripSort}>Buy avg</SortTh>
                  <SortTh k="sell_price" sort={tripSort}>Sell</SortTh>
                  <SortTh k="hold_days" sort={tripSort}>Hold</SortTh>
                  <SortTh k="tax" sort={tripSort}>Tax</SortTh>
                  <SortTh k="net" sort={tripSort}>Net P&L</SortTh>
                  <SortTh k="roi" sort={tripSort}>ROI</SortTh>
                </tr>
              </thead>
              <tbody>
                {sortedTrips.map((t, i) => (
                  <tr key={i} className="clickable" onClick={() => onSelect?.(t.item_id)}>
                    <td className="left dim">{t.sell_ts.slice(0, 10)}</td>
                    <td className="name left">{t.name}</td>
                    <td>{t.qty.toLocaleString()}</td>
                    <td>{gp(t.buy_avg)}</td>
                    <td>{gp(t.sell_price)}</td>
                    <td className="dim">{t.hold_days < 1 ? "<1d" : `${t.hold_days.toFixed(t.hold_days < 10 ? 1 : 0)}d`}</td>
                    <td className="dim neg">{gpShort(t.tax)}</td>
                    <td className={t.net >= 0 ? "pos" : "neg"}>{gp(t.net)}</td>
                    <td className={(t.roi ?? 0) >= 0 ? "pos" : "neg"}>{pct(t.roi, 1)}</td>
                  </tr>
                ))}
                {pf.closed_trips.length === 0 && (
                  <tr><td colSpan={9} className="left muted">No closed round-trips yet — log a sell against an item you've bought.</td></tr>
                )}
              </tbody>
            </table>
          </div>

          {pf.realized_by_item.length > 0 && (
            <div className="panel-section">
              <h4>Realized P&L by item</h4>
              <div className="sector-exp">
                {pf.realized_by_item.map((r) => (
                  <div className="se-row clickable" key={r.item_id} onClick={() => onSelect?.(r.item_id)}>
                    <span className="se-label">{r.name}</span>
                    <span className="se-bar">
                      <span style={{ width: `${Math.round((Math.abs(r.net) / maxAbsItem) * 100)}%`,
                        background: r.net >= 0 ? "#1d7a4f" : "#7a2530" }} />
                    </span>
                    <span className={`se-pct ${r.net >= 0 ? "pos" : "neg"}`}>{gp(r.net)}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {pf.sector_exposure && pf.sector_exposure.length > 0 && (
            <div className="panel-section">
              <h4>
                Sector exposure{" "}
                <span className="dim" style={{ fontWeight: 400, textTransform: "none", letterSpacing: 0 }}>
                  · where your open capital sits (watch for over-concentration)
                </span>
              </h4>
              <div className="sector-exp">
                {pf.sector_exposure.map((s) => (
                  <div className="se-row" key={s.sector}>
                    <span className="se-label">{s.label}</span>
                    <span className="se-bar"><span style={{ width: `${Math.round(s.pct * 100)}%` }} /></span>
                    <span className="se-pct">{Math.round(s.pct * 100)}% · {gpShort(s.capital)}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          <div className="panel-section">
            <h4>
              Trade log{" "}
              <span className="dim" style={{ fontWeight: 400, textTransform: "none", letterSpacing: 0 }}>
                · ✎ edit qty/price (bump as an order fills) · ⧉ repeat into the form · ✕ delete
              </span>
            </h4>
            <table className="tbl">
              <thead>
                <tr>
                  <th className="left">When (Central)</th>
                  <th className="left">Item</th>
                  <th className="left">Side</th>
                  <th>Qty</th>
                  <th>Price</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {pf.trades.map((t) => (
                  <tr key={t.id}>
                    <td className="left dim">{fmtTsCentral(t.ts)}</td>
                    <td className="name left">{t.name}</td>
                    <td className={`left ${t.side === "buy" ? "pos" : "neg"}`}>{t.side}</td>
                    {editId === t.id ? (
                      <>
                        <td><input className="edit-in" value={eQty} onChange={(e) => setEQty(e.target.value)}
                          onKeyDown={(e) => e.key === "Enter" && saveEdit()} autoFocus /></td>
                        <td><input className="edit-in" value={ePrice} onChange={(e) => setEPrice(e.target.value)}
                          onKeyDown={(e) => e.key === "Enter" && saveEdit()} /></td>
                        <td className="left" style={{ whiteSpace: "nowrap" }}>
                          <button className="del" style={{ color: "#25d07d" }} onClick={saveEdit} title="Save">✓</button>
                          <button className="del" onClick={() => setEditId(null)} title="Cancel">✕</button>
                        </td>
                      </>
                    ) : (
                      <>
                        <td>{t.qty.toLocaleString()}</td>
                        <td>{gp(t.price)}</td>
                        <td className="left" style={{ whiteSpace: "nowrap" }}>
                          <button className="del" onClick={() => startEdit(t.id, t.qty, t.price)} title="Edit qty / price">✎</button>
                          <button className="del" onClick={() => repeat(t.name, t.side, t.price)} title="Repeat into the form">⧉</button>
                          <button className="del" onClick={() => del(t.id)} title="Delete">✕</button>
                        </td>
                      </>
                    )}
                  </tr>
                ))}
                {pf.trades.length === 0 && (
                  <tr><td colSpan={6} className="left muted">No trades logged yet.</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
