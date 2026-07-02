import { useEffect, useState, type ReactNode } from "react";
import { getPlan, getSetArb, type ClockHour, type Filters, type PlanResponse, type PlanSlot, type SetArbRow } from "../api";
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
const days = (d?: number | null) => (d == null ? "–" : d < 1 ? `${Math.round(d * 24)}h` : `${d.toFixed(d < 10 ? 1 : 0)}d`);
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
export function LiquidityClock({ clock }: { clock: ClockHour[] }) {
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
  onAddOrder: (o: { item_id: number; side: "buy" | "sell"; price: number; qty: number; tag?: string }) => void | Promise<void>;
}) {
  const [plan, setPlan] = useState<PlanResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // 2touch = overnight-first (the OOS-proven edge; two sessions/day) — the default for a
  // couple-hours-a-day schedule. active = presence-required fast flips for keyboard sessions.
  const [mode, setMode] = useState<string>(() => localStorage.getItem("ge.plan.mode") ?? "2touch");
  const [setArb, setSetArb] = useState<SetArbRow[]>([]);
  const pickMode = (m: string) => {
    setMode(m);
    localStorage.setItem("ge.plan.mode", m);
  };

  // set<->components conversion arb (slow-moving; refresh with the plan)
  useEffect(() => {
    if (mode !== "2touch") return;
    getSetArb().then((d) => setSetArb(d.rows)).catch(() => setSetArb([]));
  }, [refreshNonce, mode]);

  useEffect(() => {
    let cancelled = false;
    setErr(null);
    // Debounce: the bankroll filter auto-syncs from live GE fills, so a burst of fills would
    // otherwise fire a burst of full /api/plan scans and hammer the 1-vCPU box (this froze the
    // site). Coalesce rapid changes into one fetch after things settle.
    const t = setTimeout(() => {
      setLoading(true);
      getPlan(filters, mode)
        .then((p) => !cancelled && setPlan(p))
        .catch((e) => !cancelled && setErr(String(e)))
        .finally(() => !cancelled && setLoading(false));
    }, 700);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [filters, refreshNonce, mode]);

  if (err) return <div className="empty">Plan error: {err}</div>;
  if (!plan) return <div className="empty">{loading ? "Building your 8-slot plan…" : "No plan."}</div>;

  const t = plan.totals;
  const activeSells = plan.slots.filter((s) => s.action !== "BUY");
  const buys = plan.slots.filter((s) => s.action === "BUY");
  const SLOTS = 8;
  const liveDot = (s: { live?: boolean }) => (s.live ? <span className="pos" title="Already live on the GE">● </span> : null);
  const addBtn = (o: { item_id: number; side: "buy" | "sell"; price?: number | null; qty?: number | null; live?: boolean; tag?: string }) =>
    o.live ? (
      <span className="dim" title="already on the GE">live</span>
    ) : (
      <button
        className="ord-act"
        title="Add this as an open order to track it (and adjust your free gp)"
        onClick={(e) => {
          e.stopPropagation();
          if (o.price && o.qty) onAddOrder({ item_id: o.item_id, side: o.side, price: o.price, qty: o.qty, tag: o.tag });
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

  // 2-touch session guidance by Central time: evening = place overnight buys; morning = collect+list.
  const ch = centralHourNow();
  const session =
    ch >= 17 || ch < 2
      ? "🌙 Evening session — collect today's fills, list your sells, place the overnight buys below, log off."
      : ch >= 5 && ch < 12
        ? "☀️ Morning session — collect overnight fills and list them at their sell targets. That's all it needs."
        : "Midday — optional touch: refresh stale offers. The buy list below is built for tonight.";

  return (
    <div className="tbl-scroll">
      <div className="slot-head" style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8, flexWrap: "wrap" }}>
        <div className="seg">
          <button className={`seg-btn ${mode === "2touch" ? "on" : ""}`}
                  title="Overnight-first: the OOS-proven edge (78% win, +7%/night). Two short sessions a day — place in the evening, collect + list in the morning."
                  onClick={() => pickMode("2touch")}>🌙 2-Touch</button>
          <button className={`seg-btn ${mode === "active" ? "on" : ""}`}
                  title="Fast flips for when you're AT the keyboard. Don't leave these working unattended."
                  onClick={() => pickMode("active")}>⚡ Active</button>
        </div>
        {mode === "2touch" && <span className="dim">{session}</span>}
        {mode === "active" && <span className="dim">fast flips — only while you're at the keyboard; switch to 🌙 before logging off</span>}
      </div>
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
        {plan.n_stale > 0 && (
          <> · <span className="neg">♻ recycling <b>{plan.n_stale}</b> stale hold{plan.n_stale > 1 ? "s" : ""} ({gpShort(plan.stale_capital)}) — parked too long with no progress, cut to redeploy</span></>
        )}
        {(plan.spike_skipped ?? 0) > 0 && (
          <> · <span className="dim">skipped <b>{plan.spike_skipped}</b> blowoff top{(plan.spike_skipped ?? 0) > 1 ? "s" : ""} (+2σ or +25%/24h pumps — they mean-revert on you)</span></>
        )}
        {(plan.knife_skipped ?? 0) > 0 && (
          <> · <span className="dim">skipped <b>{plan.knife_skipped}</b> falling knife{(plan.knife_skipped ?? 0) > 1 ? "s" : ""} (down &gt;5%/24h, no alch-floor support)</span></>
        )}
        {(plan.small_skipped ?? 0) > 0 && (
          <> · <span className="dim">skipped <b>{plan.small_skipped}</b> too-small overnight setup{(plan.small_skipped ?? 0) > 1 ? "s" : ""} (a filled night wouldn't pay your Min profit/RT — an empty slot beats a scrap)</span></>
        )}
        {plan.cash_clamped && (
          <> · <span className="neg">⚠ Free gp ({gpShort(plan.free_gp)}) looks inflated vs recent history — buys are sized off {gpShort(plan.sizing_cash ?? 0)} instead. If your real cash IS this high, re-set Free gp on the Portfolio page to re-baseline.</span></>
        )}
        {plan.cash_drift_pct != null && (
          <> · <span className="neg">⚠ accounting drifted {gpShort(Math.abs(plan.cash_drift ?? 0))} on {plan.cash_drift_day} ({(Math.abs(plan.cash_drift_pct) * 100).toFixed(1)}% of net worth) — the day's net-worth change doesn't reconcile with P&L. Re-set Free gp on Portfolio to re-baseline.</span></>
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

      <div className="slot-head" style={{ marginTop: 16 }}>
        {plan.mode === "2touch" ? <>🌙 Overnight buys — place tonight, sell tomorrow <span className="dim">· lowballs at the proven discount; gp/day = per-night EV with fill + win odds priced in</span></> : <>Buy the free slots</>}
        {plan.free_slots === 0 ? " — none free" : ""}
      </div>
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
              <td className="left"><span className={`badge ${ACT_BADGE[b.action]}`}>{liveDot(b)}{b.tag === "range" ? "📐 " : b.tag === "crash" ? "🔪 " : b.overnight ? "🌙 " : ""}{b.action}</span></td>
              <td className="name left">{b.name}</td>
              <td title={b.exp_units != null && b.exp_units < (b.units ?? 0) ? `Place ${b.units?.toLocaleString()}; the printed-depth model expects ~${b.exp_units.toLocaleString()} to actually fill on a dip night` : undefined}>
                {(b.units ?? 0).toLocaleString()}
                {b.exp_units != null && b.exp_units < (b.units ?? 0) && <span className="dim"> ≈{b.exp_units.toLocaleString()}</span>}
              </td>
              <td>{gp(b.price)}</td>
              <td>{gp(b.sell_target)}</td>
              <td className="dim">{gpShort(b.capital)}</td>
              <td className="pos">{gp(b.margin)}</td>
              <td className="pos">{gpShort(b.gp_day)}</td>
              {fillCell(b)}
              <td className="dim">{hrs(b.roundtrip_h)}</td>
              <td className="left dim" title={b.reason}>{b.reason}</td>
              <td className="left ord-actions" onClick={(e) => e.stopPropagation()}>{addBtn({ item_id: b.item_id, side: "buy", price: b.price, qty: b.units, live: b.live, tag: b.tag ?? (b.overnight ? "overnight" : "flip") })}</td>
            </tr>
          ))}
          {buys.length === 0 && <tr><td colSpan={12} className="left muted">{plan.free_slots === 0 ? "All 8 slots are taken." : plan.mode === "2touch" ? "No overnight setups qualify tonight (fill odds ≥30% + win ≥55% + exitable next day + a filled night pays your Min profit/RT). Some nights are thin — don't force it; lower Min profit/RT to surface smaller setups." : "No buys clear the bar (your Min profit/round-trip + filters). Lower it on the toolbar to surface smaller flips."}</td></tr>}
        </tbody>
      </table>

      {plan.overnight.length > 0 && (
        <>
          <div className="slot-head" style={{ marginTop: 16 }}>
            🌙 Overnight picks — place in the evening, sell next morning{" "}
            <span className="dim">· the only OOS-proven signal (+7.5% median/night, 78% win) · separate from your 8 slots</span>
          </div>
          <table className="tbl">
            <thead>
              <tr>
                <th className="left">Item</th>
                <th title="This item's own EV-optimal lowball depth below the bid (fitted per item, not one global setting)">Disc</th>
                <th>Buy ≤</th><th>Sell target</th><th>Margin/ea</th>
                <th title="Historical odds this lowball fills overnight (calibrated to realized fills)">Fill odds</th>
                <th title="Historical win rate on the nights it fills">Win</th>
                <th title="Expected gp/night = margin × fill odds × win rate">EV/night</th>
                <th className="left">Add</th>
              </tr>
            </thead>
            <tbody>
              {plan.overnight.map((o) => (
                <tr key={o.item_id} className={o.item_id === selectedId ? "selected" : ""} onClick={() => onSelect(o.item_id)}>
                  <td className="name left">{o.name}</td>
                  <td className="dim">{o.disc != null ? `−${Math.round(o.disc * 100)}%` : "–"}</td>
                  <td>{gp(o.buy)}</td>
                  <td>{gp(o.target)}</td>
                  <td className="pos">{gp(o.margin)}</td>
                  <td className="dim">{o.fill_prob == null ? "–" : `${Math.round(o.fill_prob * 100)}%`}</td>
                  <td className={(o.win_rate ?? 0) >= 0.6 ? "pos" : "dim"}>{o.win_rate == null ? "–" : `${Math.round(o.win_rate * 100)}%`}</td>
                  <td className="pos">{gpShort(o.ev)}</td>
                  <td className="left ord-actions" onClick={(e) => e.stopPropagation()}>{addBtn({ item_id: o.item_id, side: "buy", price: o.buy, qty: o.units, tag: "overnight" })}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {setArb.length > 0 && (
        <>
          <div className="slot-head" style={{ marginTop: 16 }}>
            🧩 Conversion arbitrage — sets (GE clerk) &amp; potion decants (Bob Barter), both free{" "}
            <span className="dim">· sets carry a +4-10% premium over their pieces; second-tier decant routes pay +3-10% (365d validated) · pieces/forms fill overnight, convert + list in the morning</span>
          </div>
          {setArb.filter((r) => r.roi > 0.02).length === 0 && (
            <div className="muted" style={{ padding: "6px 2px 10px" }}>No conversion route clears +2% right now — the basis breathes; check back at your evening session.</div>
          )}
          {setArb.filter((r) => r.roi > 0.02).length > 0 && (
          <table className="tbl">
            <thead>
              <tr>
                <th className="left">Set</th>
                <th title="Sum of the piece bids — patient lowball fills land about here">Pieces cost</th>
                <th title="List the combined set at the ask">Set sell</th>
                <th title="After the 2% sell tax">Net/set</th>
                <th>ROI</th>
                <th title="Crossing the full spread both ways RIGHT NOW — if positive, it pays even without patience">Instant</th>
                <th title="How often a set BUYER is present (5-min windows, last 7d)">Sell uptime</th>
                <th title="Sets traded per day — the capacity constraint">Sets/day</th>
              </tr>
            </thead>
            <tbody>
              {setArb.filter((r) => r.roi > 0.02).map((r) => (
                <tr key={r.set_id} className={r.set_id === selectedId ? "selected" : ""} onClick={() => onSelect(r.set_id)}>
                  <td className="name left" title={`Pieces: ${r.pieces.join(", ")}`}>
                    {r.name}
                    {!r.verified && <span className="neg" title="Clerk exchange NOT yet confirmed for this set — check the GE clerk's Sets tab in-game before trading it"> ⚠ verify</span>}
                  </td>
                  <td>{gp(r.pieces_cost)}</td>
                  <td>{gp(r.set_sell)}</td>
                  <td className="pos">{gp(r.net_per_set)}</td>
                  <td className="pos">{(r.roi * 100).toFixed(1)}%</td>
                  <td className={r.instant_roi != null && r.instant_roi > 0 ? "pos" : "dim"}>{r.instant_roi == null ? "–" : `${(r.instant_roi * 100).toFixed(1)}%`}</td>
                  <td className={fillCls(r.set_sell_uptime)}>{Math.round(r.set_sell_uptime * 100)}%</td>
                  <td className="dim">{r.sets_per_day.toFixed(0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          )}
        </>
      )}

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
                <th title="How long this capital has been parked — long + flat holds get recycled into faster flips">Held</th>
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
                  <td className="dim">{days(h.held_days)}</td>
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
