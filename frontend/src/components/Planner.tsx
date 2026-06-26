import { useEffect, useState, type ReactNode } from "react";
import { getPlan, type ClockHour, type Filters, type PlanResponse, type PlanSlot } from "../api";
import { centralHourNow, centralTimeNow, gp, gpShort, hour12, utcHourToCentral, utcOffsetFromCentral } from "../format";

function Tile({ k, v, cls = "", title }: { k: string; v: ReactNode; cls?: string; title?: string }) {
  return (
    <div className="tile" title={title}>
      <div className="k">{k}</div>
      <div className={`v ${cls}`}>{v}</div>
    </div>
  );
}

const ACT_BADGE: Record<string, string> = { CUT: "badge-STRONG_SELL", SELL: "badge-SELL", HOLD: "badge-HOLD", BUY: "badge-BUY", LIST: "badge-ILLIQUID" };
const ACT_SLOT: Record<string, string> = { CUT: "sell", SELL: "sell", HOLD: "hold", BUY: "buy", LIST: "hold" };
const REC_BADGE: Record<string, string> = { keep: "badge-BUY", reprice: "badge-ILLIQUID", cancel: "badge-STRONG_SELL" };
const sign = (v?: number | null) => (v == null ? "" : v > 0 ? "pos" : v < 0 ? "neg" : "");
const recCls = (v?: number | null) => (v == null ? "dim" : v >= 50 ? "pos" : v < 35 ? "neg" : "");
const hrs = (h?: number) => (h == null ? "–" : h < 1 ? `${Math.round(h * 60)}m` : h < 48 ? `${h.toFixed(0)}h` : `${(h / 24).toFixed(1)}d`);
// fill-frequency = fraction of 5-min windows the item actually trades on the relevant side. Green
// >=30% (fills readily), red <15% (the gate floor — it'll just sit), amber between.
const fillCls = (f?: number) => (f == null ? "dim" : f >= 0.3 ? "pos" : f < 0.15 ? "neg" : "");
// item's busiest UTC hours -> Central, 12-hour: e.g. [1,4,23] -> "6 PM, 7 PM, 11 PM CT"
const centralHrs = (utcHours: number[]) =>
  utcHours.map(utcHourToCentral).sort((a, b) => a - b).map(hour12).join(", ") + " CT";
const fillTitle = (s: PlanSlot) =>
  s.fill_freq == null
    ? ""
    : `Trades in ~${(s.fill_freq * 100).toFixed(0)}% of 5-min windows on this side` +
      (s.best_hours?.length ? ` · busiest ${centralHrs(s.best_hours)}` : "");
const fillCell = (s: PlanSlot) => (
  <td className={fillCls(s.fill_freq)} title={fillTitle(s)}>
    {s.fill_freq == null ? "–" : `${(s.fill_freq * 100).toFixed(0)}%`}
  </td>
);

/** Market-wide trade volume by hour, shown in Central time — a "when do orders fill" clock. */
function LiquidityClock({ clock }: { clock: ClockHour[] }) {
  if (!clock?.length) return null;
  const byUtc = new Map(clock.map((c) => [c.hour, c]));
  const peakUtc = new Set([...clock].sort((a, b) => b.rel - a.rel).slice(0, 6).map((c) => c.hour));
  const nowCt = centralHourNow();
  const off = utcOffsetFromCentral();
  // lay the bars out in CENTRAL order (left→right = midnight→11 PM CT), reading each from its UTC bucket
  const bars = Array.from({ length: 24 }, (_, ch) => {
    const utc = (ch + off) % 24;
    return { ch, rel: byUtc.get(utc)?.rel ?? 0, isPeak: peakUtc.has(utc) };
  });
  return (
    <>
      <div className="slot-head" style={{ marginTop: 16 }}>
        When orders fill — market liquidity by hour (Central){" "}
        <span className="dim">· now {centralTimeNow()} · taller = more trading · green = peak</span>
      </div>
      <div style={{ display: "flex", alignItems: "flex-end", gap: 2, height: 52, marginTop: 4 }}>
        {bars.map((b) => {
          const isNow = b.ch === nowCt;
          const col = isNow ? "var(--accent)" : b.isPeak ? "var(--green)" : "var(--grid)";
          return (
            <div
              key={b.ch}
              title={`${hour12(b.ch)} CT — ${(b.rel * 100).toFixed(0)}% of peak liquidity${isNow ? " · NOW" : ""}`}
              style={{ flex: 1, height: "100%", display: "flex", alignItems: "flex-end" }}
            >
              <div style={{ width: "100%", height: `${Math.max(6, b.rel * 100)}%`, background: col, opacity: isNow || b.isPeak ? 1 : 0.6, borderRadius: 1 }} />
            </div>
          );
        })}
      </div>
      <div style={{ display: "flex", gap: 2 }}>
        {bars.map((b) => (
          <div key={b.ch} className="dim" style={{ flex: 1, textAlign: "center", fontSize: 9, fontFamily: "var(--mono)" }}>
            {b.ch % 6 === 0 ? hour12(b.ch).replace(" ", "") : ""}
          </div>
        ))}
      </div>
    </>
  );
}

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
  onAddOrder,
}: {
  filters: Filters;
  refreshNonce: number;
  selectedId: number | null;
  onSelect: (id: number) => void;
  onAddOrder: (o: { item_id: number; side: "buy" | "sell"; price: number; qty: number }) => void | Promise<void>;
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
  const addBtn = (o: { item_id: number; side: "buy" | "sell"; price?: number | null; qty?: number | null; live?: boolean }) =>
    o.live ? (
      <span className="dim" title="already on the GE">live</span>
    ) : (
      <button
        className="ord-act"
        title="Add this as an open order to track it (and adjust your free gp)"
        onClick={(e) => {
          e.stopPropagation();
          if (o.price && o.qty) onAddOrder({ item_id: o.item_id, side: o.side, price: o.price, qty: o.qty });
        }}
      >
        ＋ order
      </button>
    );

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
        <b>SELL</b>/<b>CUT</b> holdings worth listing now, competitive <b>BUYS</b> for the free slots, and — rather than
        leave slots empty — <b>LIST</b> the best holds at fair value to catch a lucky spike (free to wait). The rest of
        your stock is <b>held off-market</b>. Prices nudge into the spread to win the queue; quantities + profits are
        throttled to what the market actually fills in the shown time.{" "}
        <b>Recommender only.</b> <span className="pos">●</span> = already live on the GE.
        {plan.mirage_skipped > 0 && (
          <> · <span className="dim">filtered <b>{plan.mirage_skipped}</b> stale/illiquid outliers (ghost spreads — wide bid-ask or a margin that doesn't hold)</span></>
        )}
        {plan.slow_skipped > 0 && (
          <> · <span className="dim">skipped <b>{plan.slow_skipped}</b> too-slow-to-fill (high margin but volume too low — they'd sit unfilled)</span></>
        )}
        {plan.thin_skipped > 0 && (
          <> · <span className="dim">skipped <b>{plan.thin_skipped}</b> too rarely traded (a seller shows up &lt;{Math.round(0.15 * 100)}% of the time — they just sit, like the 3rd age range top)</span></>
        )}
      </div>

      <div className="tiles" style={{ gridTemplateColumns: "repeat(5, 1fr)" }}>
        <Tile k="Slots in use" v={`${plan.slots_used} / 8`} title={`${plan.n_active_sells} sells/cuts + ${plan.n_buys} buys + ${plan.n_listed} listed-at-target · ${plan.free_slots} free · ${plan.n_holding} held off-market`} />
        <Tile k="Free gp" v={gpShort(plan.free_gp)} cls="pos" title="Deployable cash right now (your Free gp filter). New buys draw from this — auto-decrements when you ＋add a buy." />
        <Tile k="Net worth" v={gpShort(plan.net_worth)} title={`Free ${gp(plan.free_gp)} + open buys ${gp(plan.committed_capital)} + holdings ${gp(plan.holdings_value)}`} />
        <Tile k="Realize from sells" v={gp(t.expected_realized)} cls={sign(t.expected_realized)} title="Net P&L you'd lock in by filling the SELL + CUT offers in this plan" />
        <Tile k="Buys gp/day" v={gpShort(t.plan_gp_day)} cls="pos" title="Ongoing modeled gp/day from the BUY slots (competitive margins, realistic fill rate)" />
      </div>

      <div className="slot-head" style={{ marginTop: 14 }}>
        Your 8 slots — <b>{plan.n_active_sells}</b> to sell/cut, <b>{plan.n_buys}</b> to buy, <b>{plan.n_listed}</b> listed at target, <b>{plan.free_slots}</b> free
      </div>
      <div className="slot-grid">{tiles}</div>

      <LiquidityClock clock={plan.liquidity_clock} />

      <div className="slot-head" style={{ marginTop: 16 }}>Sell / cut / list (active slots)</div>
      <table className="tbl">
        <thead>
          <tr>
            <th className="left">Action</th><th className="left">Item</th><th>Qty</th><th>List at</th>
            <th>Avg cost</th><th>Now</th><th>Exp. P&L</th>
            <th title="Recovery score 0–100 (underwater positions): higher = more likely to revert up">Recover</th>
            <th title="How often a BUYER is present so this can actually sell — % of 5-min windows traded (last 7d)">Fill</th>
            <th title="Rough time to sell this quantity at the item's real volume">~Sell</th><th className="left">Why</th>
            <th className="left">Add</th>
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
              {fillCell(s)}
              <td className="dim">{hrs(s.sell_h)}</td>
              <td className="left dim" title={s.reason}>{s.reason}</td>
              <td className="left ord-actions" onClick={(e) => e.stopPropagation()}>{addBtn({ item_id: s.item_id, side: "sell", price: s.price, qty: s.qty, live: s.live })}</td>
            </tr>
          ))}
          {activeSells.length === 0 && <tr><td colSpan={12} className="left muted">Nothing to sell or cut right now — your holdings are all worth holding (see below).</td></tr>}
        </tbody>
      </table>

      <div className="slot-head" style={{ marginTop: 16 }}>Buy the free slots{plan.free_slots === 0 ? " — none free" : ""}</div>
      <table className="tbl">
        <thead>
          <tr>
            <th className="left">Action</th><th className="left">Item</th><th>Units</th><th>Buy at</th><th>Sell target</th>
            <th>Capital</th><th>Margin/ea</th><th>gp/day</th>
            <th title="How often a SELLER is present so your buy can fill — % of 5-min windows traded (last 7d). Below 15% it just sits.">Fill</th>
            <th title="Realistic time to buy AND sell this quantity at the item's real volume">~Round-trip</th><th className="left">Why</th>
            <th className="left">Add</th>
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
              {fillCell(b)}
              <td className="dim">{hrs(b.roundtrip_h)}</td>
              <td className="left dim" title={b.reason}>{b.reason}</td>
              <td className="left ord-actions" onClick={(e) => e.stopPropagation()}>{addBtn({ item_id: b.item_id, side: "buy", price: b.price, qty: b.units, live: b.live })}</td>
            </tr>
          ))}
          {buys.length === 0 && <tr><td colSpan={12} className="left muted">{plan.free_slots === 0 ? "All 8 slots are taken." : "No buys clear the bar (your Min profit/round-trip + filters). Lower it on the toolbar to surface smaller flips."}</td></tr>}
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
                <th>Unrealized</th><th title="Recovery score 0–100: higher = more likely to revert up">Recover</th>
                <th title="How often a BUYER is present so this can sell — % of 5-min windows traded (last 7d)">Fill</th><th className="left">Why hold</th>
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
                  {fillCell(h)}
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
