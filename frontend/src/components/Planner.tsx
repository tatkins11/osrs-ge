import { useEffect, useState, type ReactNode } from "react";
import { getPlan, type Filters, type PlanResponse, type PlanSlot } from "../api";
import { gp, gpShort, pct } from "../format";

function Tile({ k, v, cls = "", title }: { k: string; v: ReactNode; cls?: string; title?: string }) {
  return (
    <div className="tile" title={title}>
      <div className="k">{k}</div>
      <div className={`v ${cls}`}>{v}</div>
    </div>
  );
}

const ACT_BADGE: Record<string, string> = { CUT: "badge-STRONG_SELL", SELL: "badge-SELL", HOLD: "badge-HOLD", BUY: "badge-BUY" };
const ACT_SLOT: Record<string, string> = { CUT: "sell", SELL: "sell", HOLD: "hold", BUY: "buy" };
const REC_BADGE: Record<string, string> = { keep: "badge-BUY", reprice: "badge-ILLIQUID", cancel: "badge-STRONG_SELL" };
const sign = (v?: number | null) => (v == null ? "" : v > 0 ? "pos" : v < 0 ? "neg" : "");
const recCls = (v?: number | null) => (v == null ? "dim" : v >= 50 ? "pos" : v < 35 ? "neg" : "");
const hrs = (h?: number) => (h == null ? "–" : h < 1 ? `${Math.round(h * 60)}m` : h < 48 ? `${h.toFixed(0)}h` : `${(h / 24).toFixed(1)}d`);

/**
 * The unified 8-slot decision engine. Reads your open positions + live orders + capital and gives
 * one refined plan: which holdings to SELL/CUT in a slot now, which to HOLD off-market for a better
 * price, competitive BUYS for the free slots, and what to do with each order already on the GE.
 */
export function Planner({
  filters,
  refreshNonce,
  selectedId,
  onSelect,
}: {
  filters: Filters;
  refreshNonce: number;
  selectedId: number | null;
  onSelect: (id: number) => void;
}) {
  const [plan, setPlan] = useState<PlanResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setErr(null);
    getPlan(filters)
      .then((p) => !cancelled && setPlan(p))
      .catch((e) => !cancelled && setErr(String(e)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [filters, refreshNonce]);

  if (err) return <div className="empty">Plan error: {err}</div>;
  if (!plan) return <div className="empty">{loading ? "Building your 8-slot plan…" : "No plan."}</div>;

  const t = plan.totals;
  const activeSells = plan.slots.filter((s) => s.action !== "BUY");
  const buys = plan.slots.filter((s) => s.action === "BUY");
  const SLOTS = 8;
  const liveDot = (s: { live?: boolean }) => (s.live ? <span className="pos" title="Already live on the GE">● </span> : null);

  // at-a-glance 8-slot grid (active config only — held-off-market items don't take a slot)
  const tiles: ReactNode[] = [];
  plan.slots.forEach((s, i) =>
    tiles.push(
      <div key={`s${i}`} className={`slot-tile ${ACT_SLOT[s.action]}`} onClick={() => onSelect(s.item_id)}>
        <div className="slot-no">
          <span className={`badge ${ACT_BADGE[s.action]}`}>{s.action}</span>
          {s.live && <span className="pos" title="live on GE"> ●</span>}
        </div>
        <div className="slot-item" title={s.name}>{s.name}</div>
        <div className="slot-meta">{((s.units ?? s.qty) ?? 0).toLocaleString()} @ {gp(s.price)}</div>
      </div>
    )
  );
  while (tiles.length < SLOTS) {
    const i = tiles.length;
    tiles.push(
      <div key={`f${i}`} className="slot-tile free">
        <div className="slot-no">Slot {i + 1}</div>
        <div className="slot-free">free</div>
      </div>
    );
  }

  return (
    <div className="tbl-scroll">
      <div className="exp-banner">
        <b>8-slot plan.</b> Your live positions, open orders, and capital → one recommendation per slot:{" "}
        <b>SELL</b>/<b>CUT</b> holdings worth listing now, competitive <b>BUYS</b> for the free slots, and the rest of
        your stock <b>held off-market</b> (no slot wasted) until its price comes in. Prices nudge into the spread to win
        the queue; quantities + profits are throttled to what the market actually fills in the shown time.{" "}
        <b>Recommender only.</b> <span className="pos">●</span> = already live on the GE.
      </div>

      <div className="tiles" style={{ gridTemplateColumns: "repeat(5, 1fr)" }}>
        <Tile k="Slots in use" v={`${plan.slots_used} / 8`} title={`${plan.n_active_sells} sells/cuts + ${plan.n_buys} buys · ${plan.free_slots} free · ${plan.n_holding} held off-market`} />
        <Tile k="Capital to deploy" v={gpShort(plan.capital_in)} cls="pos" title={`Bankroll ${gp(plan.bankroll)} − ${gp(plan.committed_capital)} committed in open buys`} />
        <Tile k="Realize from sells" v={gp(t.expected_realized)} cls={sign(t.expected_realized)} title="Net P&L you'd lock in by filling the SELL + CUT offers in this plan" />
        <Tile k="Buys gp/day" v={gpShort(t.plan_gp_day)} cls="pos" title="Ongoing modeled gp/day from the BUY slots (competitive margins, realistic fill rate)" />
        <Tile k="≈ growth/day" v={t.growth_day != null ? pct(t.growth_day, 1) : "–"} cls="pos" title="Buys gp/day as a % of bankroll — the compounding rate to grow. Optimistic ceiling." />
      </div>

      <div className="slot-head" style={{ marginTop: 14 }}>
        Your 8 slots — <b>{plan.n_active_sells}</b> to sell/cut, <b>{plan.n_buys}</b> to buy, <b>{plan.free_slots}</b> free
      </div>
      <div className="slot-grid">{tiles}</div>

      <div className="slot-head" style={{ marginTop: 16 }}>Sell / cut now (active slots)</div>
      <table className="tbl">
        <thead>
          <tr>
            <th className="left">Action</th><th className="left">Item</th><th>Qty</th><th>List at</th>
            <th>Avg cost</th><th>Now</th><th>Exp. P&L</th>
            <th title="Recovery score 0–100 (underwater positions): higher = more likely to revert up">Recover</th>
            <th title="Rough time to sell this quantity at the item's real volume">~Sell</th><th className="left">Why</th>
          </tr>
        </thead>
        <tbody>
          {activeSells.map((s) => (
            <tr key={s.item_id} className={s.item_id === selectedId ? "selected" : ""} onClick={() => onSelect(s.item_id)}>
              <td className="left"><span className={`badge ${ACT_BADGE[s.action]}`}>{liveDot(s)}{s.action}</span></td>
              <td className="name left">{s.name}</td>
              <td>{(s.qty ?? 0).toLocaleString()}</td>
              <td>{gp(s.price)}</td>
              <td className="dim">{gp(s.avg_cost)}</td>
              <td className="dim">{gp(s.cur_price)}</td>
              <td className={sign(s.expected_net)}>{s.expected_net == null ? "–" : gpShort(s.expected_net)}</td>
              <td className={recCls(s.recovery_score)}>{s.recovery_score ?? "–"}</td>
              <td className="dim">{hrs(s.sell_h)}</td>
              <td className="left dim" title={s.reason}>{s.reason}</td>
            </tr>
          ))}
          {activeSells.length === 0 && <tr><td colSpan={10} className="left muted">Nothing to sell or cut right now — your holdings are all worth holding (see below).</td></tr>}
        </tbody>
      </table>

      <div className="slot-head" style={{ marginTop: 16 }}>Buy the free slots{plan.free_slots === 0 ? " — none free" : ""}</div>
      <table className="tbl">
        <thead>
          <tr>
            <th className="left">Action</th><th className="left">Item</th><th>Units</th><th>Buy at</th><th>Sell target</th>
            <th>Capital</th><th>Margin/ea</th><th>gp/day</th>
            <th title="Realistic time to buy AND sell this quantity at the item's real volume">~Round-trip</th><th className="left">Why</th>
          </tr>
        </thead>
        <tbody>
          {buys.map((b) => (
            <tr key={b.item_id} className={b.item_id === selectedId ? "selected" : ""} onClick={() => onSelect(b.item_id)}>
              <td className="left"><span className={`badge ${ACT_BADGE[b.action]}`}>{liveDot(b)}{b.action}</span></td>
              <td className="name left">{b.name}</td>
              <td>{(b.units ?? 0).toLocaleString()}</td>
              <td>{gp(b.price)}</td>
              <td>{gp(b.sell_target)}</td>
              <td className="dim">{gpShort(b.capital)}</td>
              <td className="pos">{gp(b.margin)}</td>
              <td className="pos">{gpShort(b.gp_day)}</td>
              <td className="dim">{hrs(b.roundtrip_h)}</td>
              <td className="left dim" title={b.reason}>{b.reason}</td>
            </tr>
          ))}
          {buys.length === 0 && <tr><td colSpan={10} className="left muted">{plan.free_slots === 0 ? "All 8 slots are taken." : "No buys clear the bar (positive competitive margin + your filters)."}</td></tr>}
        </tbody>
      </table>

      {plan.holding.length > 0 && (
        <>
          <div className="slot-head" style={{ marginTop: 16 }}>
            Hold off-market — no slot used, waiting for a better price <span className="dim">({plan.holding.length})</span>
          </div>
          <table className="tbl">
            <thead>
              <tr>
                <th className="left">Item</th><th>Qty</th><th>Avg cost</th><th>Now</th>
                <th title="Place a sell here (fair value) when you're ready, or wait for the price to come to it">Sell when ≥</th>
                <th>Unrealized</th><th title="Recovery score 0–100: higher = more likely to revert up">Recover</th><th className="left">Why hold</th>
              </tr>
            </thead>
            <tbody>
              {plan.holding.map((h) => (
                <tr key={h.item_id} className={h.item_id === selectedId ? "selected" : ""} onClick={() => onSelect(h.item_id)}>
                  <td className="name left">{h.name}</td>
                  <td>{(h.qty ?? 0).toLocaleString()}</td>
                  <td className="dim">{gp(h.avg_cost)}</td>
                  <td className="dim">{gp(h.cur_price)}</td>
                  <td>{gp(h.target ?? h.price)}</td>
                  <td className={sign(h.unrealized)}>{h.unrealized == null ? "–" : gpShort(h.unrealized)}</td>
                  <td className={recCls(h.recovery_score)}>{h.recovery_score ?? "–"}</td>
                  <td className="left dim" title={h.reason}>{h.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {plan.reconcile.length > 0 && (
        <>
          <div className="slot-head" style={{ marginTop: 16 }}>
            Your live orders — keep / reprice / cancel <span className="dim">({plan.reconcile.length})</span>
          </div>
          <table className="tbl">
            <thead>
              <tr>
                <th className="left">Do</th><th className="left">Side</th><th className="left">Item</th>
                <th>Your price</th><th className="left">Filled</th><th className="left">Note</th>
              </tr>
            </thead>
            <tbody>
              {plan.reconcile.map((r, i) => (
                <tr key={`${r.order_id ?? r.item_id}-${i}`} className={r.item_id === selectedId ? "selected" : ""} onClick={() => onSelect(r.item_id)}>
                  <td className="left"><span className={`badge ${REC_BADGE[r.status]}`}>{r.status}</span></td>
                  <td className={`left ${r.side === "buy" ? "pos" : "neg"}`}>{r.side}</td>
                  <td className="name left">{r.name}</td>
                  <td>{gp(r.price)}</td>
                  <td className="left dim">{r.progress}</td>
                  <td className="left dim" title={r.note}>{r.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}
