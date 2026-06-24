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
const sign = (v?: number | null) => (v == null ? "" : v > 0 ? "pos" : v < 0 ? "neg" : "");
const recCls = (v?: number | null) => (v == null ? "dim" : v >= 50 ? "pos" : v < 35 ? "neg" : "");
const hrs = (h?: number) => (h == null ? "–" : h < 1 ? `${Math.round(h * 60)}m` : h < 48 ? `${h.toFixed(0)}h` : `${(h / 24).toFixed(1)}d`);

/**
 * The unified 8-slot decision engine. Reads your open positions + live orders + capital and gives
 * one refined plan for all 8 GE slots: SELL/HOLD/CUT each holding (competitive price + recovery
 * read) and competitive BUYS for the free slots, with realistic fill timelines. Recommender only.
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
  const sells = [...plan.slots.filter((s) => s.action !== "BUY"), ...plan.bench];
  const buys = plan.slots.filter((s) => s.action === "BUY");
  const SLOTS = 8;

  const liveDot = (s: PlanSlot) => (s.live ? <span className="pos" title="A matching order is already live on the GE">● </span> : null);

  // at-a-glance 8-slot grid
  const tiles: ReactNode[] = [];
  plan.slots.forEach((s, i) =>
    tiles.push(
      <div key={`s${i}`} className={`slot-tile ${ACT_SLOT[s.action]}`} onClick={() => onSelect(s.item_id)}>
        <div className="slot-no">
          <span className={`badge ${ACT_BADGE[s.action]}`}>{s.action}</span>
          {s.live && <span className="pos" title="live on GE"> ●</span>}
        </div>
        <div className="slot-item" title={s.name}>{s.name}</div>
        <div className="slot-meta">
          {s.action === "BUY"
            ? `${(s.units ?? 0).toLocaleString()} @ ${gp(s.price)}`
            : `${(s.qty ?? 0).toLocaleString()} @ ${gp(s.price)}`}
        </div>
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
        <b>8-slot plan.</b> One refined recommendation for every GE slot from your live positions, open orders, and
        capital: <b>SELL</b> reverted winners, <b>HOLD</b> the ones with upside left, <b>CUT</b> the sinking ships, and
        fill the free slots with competitive <b>BUYS</b>. Prices nudge into the spread to win the queue; quantities +
        profits are throttled to what the market can actually fill in the shown time. <b>Recommender only</b> — you place
        the offers in-game. <span className="pos">●</span> = already live on the GE.
      </div>

      <div className="tiles" style={{ gridTemplateColumns: "repeat(5, 1fr)" }}>
        <Tile k="Slots" v={`${plan.n_sells + plan.n_buys} / 8`} title={`${plan.n_sells} holdings listed + ${plan.n_buys} buys`} />
        <Tile k="Capital to deploy" v={gpShort(plan.capital_in)} cls="pos" title={`Bankroll ${gp(plan.bankroll)} − ${gp(plan.committed_capital)} committed in open buys`} />
        <Tile
          k="Realize from sells"
          v={gp(t.expected_realized)}
          cls={sign(t.expected_realized)}
          title="Net P&L you'd lock in by filling the SELL + CUT offers in this plan"
        />
        <Tile k="Buys gp/day" v={gpShort(t.plan_gp_day)} cls="pos" title="Ongoing modeled gp/day from the BUY slots (competitive margins, realistic fill rate)" />
        <Tile
          k="≈ growth/day"
          v={t.growth_day != null ? pct(t.growth_day, 1) : "–"}
          cls="pos"
          title="Buys gp/day as a % of bankroll — the compounding rate to grow. Optimistic ceiling."
        />
      </div>

      <div className="slot-head" style={{ marginTop: 14 }}>
        Your 8 slots — <b>{plan.n_sells}</b> to sell/hold, <b>{plan.n_buys}</b> to buy{plan.bench.length > 0 ? `, ${plan.bench.length} waiting for a slot` : ""}
      </div>
      <div className="slot-grid">{tiles}</div>

      <div className="slot-head" style={{ marginTop: 16 }}>Holdings — sell / hold / cut</div>
      <table className="tbl">
        <thead>
          <tr>
            <th className="left">Action</th>
            <th className="left">Item</th>
            <th>Qty</th>
            <th>List at</th>
            <th>Avg cost</th>
            <th>Now</th>
            <th>Exp. P&L</th>
            <th title="Recovery score for underwater positions: 0–100, higher = more likely to revert up">Recover</th>
            <th title="Rough time to sell this quantity at the item's real volume">~Sell</th>
            <th className="left">Why</th>
          </tr>
        </thead>
        <tbody>
          {sells.map((s) => (
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
          {sells.length === 0 && (
            <tr><td colSpan={10} className="left muted">No open positions — log buys in Portfolio or run the RuneLite plugin, and your holdings will be managed here.</td></tr>
          )}
        </tbody>
      </table>

      <div className="slot-head" style={{ marginTop: 16 }}>Buy the free slots{plan.free_slots === 0 ? " — none free (all 8 used by holdings)" : ""}</div>
      <table className="tbl">
        <thead>
          <tr>
            <th className="left">Action</th>
            <th className="left">Item</th>
            <th>Units</th>
            <th>Buy at</th>
            <th>Sell target</th>
            <th>Capital</th>
            <th>Margin/ea</th>
            <th>gp/day</th>
            <th title="Realistic time to buy AND sell this quantity at the item's real volume">~Round-trip</th>
            <th className="left">Why</th>
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
          {buys.length === 0 && (
            <tr><td colSpan={10} className="left muted">{plan.free_slots === 0 ? "All 8 slots are taken by holdings above." : "No buys clear the bar (positive competitive margin + your filters)."}</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
