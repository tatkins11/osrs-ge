import { useEffect, useState, type ReactNode } from "react";
import { getAllocator, getOrders, type AllocatorPlan, type Filters, type Order } from "../api";
import { gp, gpShort, pct } from "../format";

function Tile({ k, v, cls = "", title }: { k: string; v: ReactNode; cls?: string; title?: string }) {
  return (
    <div className="tile" title={title}>
      <div className="k">{k}</div>
      <div className={`v ${cls}`}>{v}</div>
    </div>
  );
}

/**
 * 8-slot GE capital allocator. Reads your live open orders (used slots + gp committed in open
 * buy offers), then recommends the flips to fill your FREE slots to maximize total gp/day within
 * remaining capital + per-item buy limits. Recommender only — you place the offers in-game.
 */
export function Allocator({
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
  const [plan, setPlan] = useState<AllocatorPlan | null>(null);
  const [orders, setOrders] = useState<Order[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setErr(null);
    Promise.all([getAllocator(filters), getOrders().catch(() => [] as Order[])])
      .then(([p, o]) => {
        if (!cancelled) {
          setPlan(p);
          setOrders(o);
        }
      })
      .catch((e) => !cancelled && setErr(String(e)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [filters, refreshNonce]);

  if (err) return <div className="empty">Allocator error: {err}</div>;
  if (!plan) return <div className="empty">{loading ? "Optimizing your 8 slots…" : "No plan."}</div>;

  const recs = plan.recommendations;
  const openOrders = orders.filter((o) => o.open);
  const SLOTS = 8;
  const growth = plan.bankroll > 0 ? plan.total_gp_day / plan.bankroll : null;

  // 8-slot capacity view: current open orders first, then the plan's picks for the free slots
  const tiles: ReactNode[] = [];
  openOrders.slice(0, SLOTS).forEach((o, i) =>
    tiles.push(
      <div key={`o${i}`} className={`slot-tile ${o.side === "buy" ? "buy" : "sell"}`} onClick={() => onSelect(o.item_id)}>
        <div className="slot-no">In use · {o.side}</div>
        <div className="slot-item" title={o.name}>{o.name}</div>
        <div className="slot-meta">{o.filled_qty.toLocaleString()}/{o.total_qty.toLocaleString()} filled</div>
      </div>
    )
  );
  recs.forEach((r, i) => {
    if (tiles.length >= SLOTS) return;
    tiles.push(
      <div key={`r${i}`} className="slot-tile buy alloc-plan" onClick={() => onSelect(r.item_id)}>
        <div className="slot-no">Deploy →</div>
        <div className="slot-item" title={r.name}>{r.name}</div>
        <div className="slot-meta">{r.units.toLocaleString()} @ {gp(r.buy)} · {gpShort(r.capital)}</div>
      </div>
    );
  });
  while (tiles.length < SLOTS) {
    const i = tiles.length;
    tiles.push(
      <div key={`f${i}`} className="slot-tile free">
        <div className="slot-no">Slot {i + 1}</div>
        <div className="slot-free">{plan.skipped_no_capital > 0 ? "out of capital" : "free"}</div>
      </div>
    );
  }

  return (
    <div className="tbl-scroll">
      <div className="exp-banner">
        <b>8-slot capital allocator.</b> Reads your live open orders, then fills your free GE slots with the flips that
        maximize total <b>gp/day</b> within your remaining capital and each item's 4-hour buy limit — greedy by capital
        velocity, after-slippage margins only. <b>Recommender only</b> — place the offers yourself in-game. Tune the size
        of the plan with the <b>bankroll</b> and <b>min profit/margin</b> filters above.
      </div>

      <div className="tiles" style={{ gridTemplateColumns: "repeat(5, 1fr)" }}>
        <Tile k="Free slots" v={`${plan.free_slots} / 8`} title={`${plan.used_slots} in use right now`} />
        <Tile
          k="Capital to deploy"
          v={gpShort(plan.capital_in)}
          cls="pos"
          title={`Bankroll ${gp(plan.bankroll)} − ${gp(plan.committed_capital)} committed in open buy offers`}
        />
        <Tile k="Plan deploys" v={gpShort(plan.total_capital)} title={`${pct(plan.utilization, 0)} of available capital put to work`} />
        <Tile
          k="Plan gp/day"
          v={gpShort(plan.total_gp_day)}
          cls="pos"
          title="Sum of modeled gp/day across the recommended slots (after-slippage, volume-throttled). Relative ranking — absolute is optimistic."
        />
        <Tile
          k="≈ growth/day"
          v={growth != null ? pct(growth, 1) : "–"}
          cls="pos"
          title="Plan gp/day as a % of bankroll — your modeled daily compounding rate. This is the number to grow."
        />
      </div>

      <div className="slot-head" style={{ marginTop: 14 }}>
        Your 8 slots — <b>{openOrders.length}</b> in use, <b>{recs.length}</b> to deploy
      </div>
      <div className="slot-grid">{tiles}</div>

      <table className="tbl">
        <thead>
          <tr>
            <th className="left">#</th>
            <th className="left">Item</th>
            <th>Buy at</th>
            <th>Units</th>
            <th>Capital</th>
            <th>Sell target</th>
            <th>Real/ea</th>
            <th>Cycle</th>
            <th>Est gp/day</th>
          </tr>
        </thead>
        <tbody>
          {recs.map((r, i) => (
            <tr key={r.item_id} className={r.item_id === selectedId ? "selected" : ""} onClick={() => onSelect(r.item_id)}>
              <td className="left dim">{i + 1}</td>
              <td className="name left">{r.name}</td>
              <td>{gp(r.buy)}</td>
              <td>{r.units.toLocaleString()}</td>
              <td className="dim">{gpShort(r.capital)}</td>
              <td>{gp(r.sell_target)}</td>
              <td className="pos">{gp(r.slip_margin)}</td>
              <td className="dim">{r.cycle_h}h</td>
              <td className="pos">{gpShort(r.gp_day)}</td>
            </tr>
          ))}
          {recs.length === 0 && (
            <tr>
              <td colSpan={9} className="left muted">
                {plan.free_slots === 0
                  ? "All 8 slots are in use — nothing to deploy. Free a slot to get a recommendation."
                  : "No flips clear the bar right now (positive after-slippage margin + your filters). Loosen Min profit / Min margin, or check back."}
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
