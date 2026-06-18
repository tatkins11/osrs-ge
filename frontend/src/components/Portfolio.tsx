import { useEffect, useMemo, useState, type ReactNode } from "react";
import { addTrade, deleteTrade, getItemNames, getPortfolio, type ItemName, type Portfolio as Pf } from "../api";
import { gp, gpShort, pct } from "../format";
import { SortTh, useSortable } from "./sortable";

function Tile({ k, v, cls = "" }: { k: string; v: ReactNode; cls?: string }) {
  return (
    <div className="tile">
      <div className="k">{k}</div>
      <div className={`v ${cls}`}>{v}</div>
    </div>
  );
}

export function Portfolio({ refreshNonce = 0 }: { refreshNonce?: number }) {
  const [pf, setPf] = useState<Pf | null>(null);
  const [names, setNames] = useState<ItemName[]>([]);
  const [itemText, setItemText] = useState("");
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [qty, setQty] = useState("");
  const [price, setPrice] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const load = () => getPortfolio().then(setPf).catch(() => {});
  useEffect(() => {
    load();
  }, [refreshNonce]);
  useEffect(() => {
    getItemNames().then(setNames).catch(() => {});
  }, []);

  const nameToId = useMemo(() => {
    const m = new Map<string, number>();
    for (const n of names) m.set(n.name.toLowerCase(), n.item_id);
    return m;
  }, [names]);

  const submit = async () => {
    const id = nameToId.get(itemText.trim().toLowerCase());
    if (id == null) {
      setErr("Pick an item from the list.");
      return;
    }
    const q = Number(qty);
    const p = Number(price);
    if (!(q > 0) || !(p >= 0)) {
      setErr("Enter a valid quantity and price.");
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      await addTrade({ item_id: id, side, qty: q, price: p });
      setItemText("");
      setQty("");
      setPrice("");
      await load();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  };

  const del = async (id: number) => {
    await deleteTrade(id).catch(() => {});
    load();
  };

  const { sorted: sortedPos, sort: posSort } = useSortable(pf?.open_positions ?? [], "unrealized");

  return (
    <div className="portfolio">
      <div className="panel-section">
        <h4>Log a trade</h4>
        <div className="trade-form">
          <input list="itemlist" placeholder="item name…" value={itemText} onChange={(e) => setItemText(e.target.value)} />
          <datalist id="itemlist">
            {names.map((n) => (
              <option key={n.item_id} value={n.name} />
            ))}
          </datalist>
          <select value={side} onChange={(e) => setSide(e.target.value as "buy" | "sell")}>
            <option value="buy">Buy</option>
            <option value="sell">Sell</option>
          </select>
          <input placeholder="qty" value={qty} onChange={(e) => setQty(e.target.value)} />
          <input placeholder="price / ea" value={price} onChange={(e) => setPrice(e.target.value)} />
          <button className="refresh" disabled={busy} onClick={submit}>
            Add trade
          </button>
          {err && <span className="neg" style={{ fontSize: 11 }}>{err}</span>}
        </div>
        <div className="note" style={{ marginTop: 8 }}>
          Log what you actually bought/sold in-game. Sells are taxed 2% automatically; cost basis is moving-average.
        </div>
      </div>

      {pf && (
        <>
          <div className="panel-section">
            <div className="tiles" style={{ gridTemplateColumns: "repeat(4, 1fr)" }}>
              <Tile k="Realized P&L" v={gp(pf.realized_total)} cls={pf.realized_total >= 0 ? "pos" : "neg"} />
              <Tile k="Unrealized P&L" v={gp(pf.unrealized_total)} cls={pf.unrealized_total >= 0 ? "pos" : "neg"} />
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
                · value &amp; P&amp;L are net of the 2% sell tax; <b>Rec. sell</b> = where to place a sell offer now
                (green once it clears breakeven)
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
                  <SortTh k="cur_net" sort={posSort}>Cur (net)</SortTh>
                  <SortTh k="market_value" sort={posSort}>Market value</SortTh>
                  <SortTh k="unrealized" sort={posSort}>Unrealized</SortTh>
                  <SortTh k="unrealized_pct" sort={posSort}>%</SortTh>
                  <SortTh k="status" sort={posSort} className="left">Status</SortTh>
                </tr>
              </thead>
              <tbody>
                {sortedPos.map((p) => (
                  <tr key={p.item_id}>
                    <td className="name left">{p.name}</td>
                    <td>{p.qty.toLocaleString()}</td>
                    <td>{gp(p.avg_cost)}</td>
                    <td className="dim">{gp(p.breakeven)}</td>
                    <td className={(p.cur_price ?? 0) >= (p.breakeven ?? 0) ? "pos" : "neg"}>{gp(p.cur_price)}</td>
                    <td className="dim">{gp(p.target)}</td>
                    <td>{gp(p.cur_net)}</td>
                    <td>{gp(p.market_value)}</td>
                    <td className={(p.unrealized ?? 0) >= 0 ? "pos" : "neg"}>{gp(p.unrealized)}</td>
                    <td className={(p.unrealized_pct ?? 0) >= 0 ? "pos" : "neg"}>{pct(p.unrealized_pct, 1)}</td>
                    <td className="left">
                      {p.status === "sell" ? (
                        <span className="badge badge-SELL">SELL</span>
                      ) : p.status === "underwater" ? (
                        <span className="neg">underwater</span>
                      ) : (
                        <span className="dim">hold</span>
                      )}
                    </td>
                  </tr>
                ))}
                {pf.open_positions.length === 0 && (
                  <tr>
                    <td colSpan={11} className="left muted">No open positions — log a buy above.</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

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
            <h4>Trade log</h4>
            <table className="tbl">
              <thead>
                <tr>
                  <th className="left">When (UTC)</th>
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
                    <td className="left dim">{t.ts.slice(0, 16).replace("T", " ")}</td>
                    <td className="name left">{t.name}</td>
                    <td className={`left ${t.side === "buy" ? "pos" : "neg"}`}>{t.side}</td>
                    <td>{t.qty.toLocaleString()}</td>
                    <td>{gp(t.price)}</td>
                    <td>
                      <button className="del" onClick={() => del(t.id)} title="Delete">
                        ✕
                      </button>
                    </td>
                  </tr>
                ))}
                {pf.trades.length === 0 && (
                  <tr>
                    <td colSpan={6} className="left muted">No trades logged yet.</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
