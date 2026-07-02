import { useEffect, useState } from "react";
import { getGrowth, getPlan, type Filters, type GrowthResponse, type PlanResponse, type PlanSlot } from "../api";
import { centralHourNow, centralTimeNow, gp, gpShort, pct } from "../format";
import { GrowthChart } from "./GrowthTracker";
import { LiquidityClock } from "./Planner";

const ACT_BADGE: Record<string, string> = { CUT: "badge-STRONG_SELL", SELL: "badge-SELL", HOLD: "badge-HOLD", BUY: "badge-BUY", LIST: "badge-ILLIQUID" };
const ACT_SLOT: Record<string, string> = { CUT: "sell", SELL: "sell", HOLD: "hold", BUY: "buy", LIST: "hold" };
const DAY = 86400000;
const fmtEta = (days: number | null | undefined) => {
  if (days == null) return "—";
  if (days <= 0) return "reached ✓";
  const date = new Date(Date.now() + days * DAY).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  return days < 60 ? `~${Math.round(days)}d · ${date}` : `~${(days / 30.4).toFixed(1)}mo · ${date}`;
};

/** Today — the "what do I do right now" home screen: session guidance, the money picture,
 *  the next actions from the 8-Slot Plan, and the road to the next target. */
export function Dashboard({
  filters,
  refreshNonce,
  onSelect,
  goTo,
}: {
  filters: Filters;
  refreshNonce: number;
  onSelect: (id: number) => void;
  goTo: (tab: string) => void;
}) {
  const [plan, setPlan] = useState<PlanResponse | null>(null);
  const [g, setG] = useState<GrowthResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setErr(null);
    const t = setTimeout(() => {   // same debounce rationale as the Planner (bankroll auto-sync bursts)
      const mode = localStorage.getItem("ge.plan.mode") ?? "2touch";
      Promise.all([getPlan(filters, mode), getGrowth(filters)])
        .then(([p, gr]) => { if (!cancelled) { setPlan(p); setG(gr); } })
        .catch((e) => !cancelled && setErr(String(e)));
    }, 600);
    return () => { cancelled = true; clearTimeout(t); };
  }, [filters, refreshNonce]);

  if (err) return <div className="empty">Dashboard error: {err}</div>;
  if (!plan || !g) return <div className="empty"><span className="spinner" />Sizing up your day…</div>;

  const ch = centralHourNow();
  const evening = ch >= 17 || ch < 2;
  const morning = ch >= 5 && ch < 12;
  const session = evening
    ? { icon: "🌙", title: "Evening session", text: <>Collect today's fills → list your sells → place the overnight buys → log off. The market's busiest window is now.</> }
    : morning
      ? { icon: "☀️", title: "Morning session", text: <>Collect overnight fills → list them at their sell targets. 15 minutes and you're done — sells work all day without you.</> }
      : { icon: "🕑", title: "Midday", text: <>Nothing required. Optionally refresh stale offers. Tonight's buy list firms up as the evening approaches.</> };

  // next actions, in execution order: free slots first (CUT/SELL), then place buys, then hopeful lists
  const order: Record<string, number> = { CUT: 0, SELL: 1, BUY: 2, LIST: 3, HOLD: 4 };
  const actions = [...plan.slots]
    .filter((s) => s.action !== "HOLD")
    .sort((a, b) => (order[a.action] ?? 9) - (order[b.action] ?? 9))
    .slice(0, 8);

  // Δ today only means something vs an ACTUAL yesterday snapshot — comparing against an older
  // gap (or a corrupted-era row) printed a -163M "loss" that never happened. Gaps show "—".
  const hist = g.history.filter((h) => h.value > 0);
  const dayOf = (ts: string) => Math.floor(new Date(ts).getTime() / 86400000);
  const last = hist[hist.length - 1];
  const prevRow = hist.length > 1 ? hist[hist.length - 2] : null;
  const prevIsYesterday = last && prevRow && dayOf(last.ts) - dayOf(prevRow.ts) === 1;
  const dToday = prevIsYesterday && prevRow ? g.net_worth - prevRow.value : null;
  const headline = g.targets.find((t) => t.value > g.net_worth) ?? g.targets[g.targets.length - 1];
  const deployed = 1 - g.idle_frac;

  const slotQP = (s: PlanSlot) => `${((s.units ?? s.qty) ?? 0).toLocaleString()} @ ${gp(s.price)}`;

  return (
    <div className="dash">
      <div className="dash-session">
        <span className="when">{session.icon}</span>
        <div>
          <b>{session.title}</b> · {centralTimeNow()} Central — {session.text}
        </div>
      </div>

      {plan.cash_clamped && (
        <div className="exp-banner" style={{ margin: "0 0 12px" }}>
          ⚠ Free gp ({gpShort(plan.free_gp)}) looks inflated vs recent history — the plan sized off {gpShort(plan.sizing_cash ?? 0)}.
          If your cash really is that high, re-set Free gp on the Portfolio page.
        </div>
      )}

      <div className="tiles" style={{ gridTemplateColumns: "repeat(5, 1fr)", marginBottom: 12 }}>
        <div className="tile" title={`Free ${gp(plan.free_gp)} + open buys ${gp(plan.committed_capital)} + holdings ${gp(plan.holdings_value)}`}>
          <div className="k">Net worth</div>
          <div className="v">{gpShort(plan.net_worth)}</div>
          <div className="k" style={{ marginTop: 2, textTransform: "none", letterSpacing: 0 }}>{gpShort(plan.free_gp)} free · {gpShort(plan.holdings_value)} held</div>
        </div>
        <div className="tile" title="Change vs yesterday's net-worth snapshot">
          <div className="k">Δ today</div>
          <div className={`v ${dToday == null ? "" : dToday >= 0 ? "pos" : "neg"}`}>{dToday == null ? "—" : `${dToday >= 0 ? "+" : ""}${gpShort(dToday)}`}</div>
        </div>
        <div className="tile" title={`Realized ${gp(g.recent_gp_day)}/day over the last ${g.recent_days} days, vs net worth`}>
          <div className="k">Growth / day</div>
          <div className={`v ${g.daily_pct >= 0 ? "pos" : "neg"}`}>{pct(g.daily_pct, 1)}</div>
          <div className="k" style={{ marginTop: 2, textTransform: "none", letterSpacing: 0 }}>{gpShort(g.recent_gp_day)}/day realized</div>
        </div>
        <div className="tile" title="At your realized growth rate, compounding">
          <div className="k">{headline?.label ?? "1B"} ETA</div>
          <div className="v pos">{fmtEta(headline?.days_realized)}</div>
        </div>
        <div className="tile" title={`${gp(g.capital_in)} undeployed — idle gp doesn't compound`}>
          <div className="k">Deployed</div>
          <div className={`v ${deployed < 0.6 ? "neg" : "pos"}`}>{pct(deployed, 0)}</div>
          <div className="k" style={{ marginTop: 2, textTransform: "none", letterSpacing: 0 }}>{gpShort(g.capital_in)} idle</div>
        </div>
      </div>

      <div className="dash-grid">
        <div className="dash-card">
          <h3>
            Do this now — {actions.length ? `${actions.length} action${actions.length > 1 ? "s" : ""}` : "all clear"}
            <button className="linklike" onClick={() => goTo("allocate")}>open the 8-Slot Plan →</button>
          </h3>
          {actions.map((s) => (
            <div key={`${s.action}-${s.item_id}`} className="act-row" onClick={() => onSelect(s.item_id)}>
              <span className={`badge ${ACT_BADGE[s.action]}`}>{s.tag === "range" ? "📐 " : s.tag === "crash" ? "🔪 " : s.overnight ? "🌙 " : ""}{s.action}</span>
              <span className="nm">{s.name}</span>
              <span className="qp">{slotQP(s)}</span>
              <span className="why">{s.reason}</span>
            </div>
          ))}
          {actions.length === 0 && (
            <div className="muted" style={{ padding: "10px 4px" }}>
              Nothing needs your hands right now — offers are working. Check back at the next session.
            </div>
          )}

          <h3 style={{ marginTop: 16 }}>Your 8 GE slots</h3>
          <div className="slot-grid">
            {plan.slots.map((s, i) => (
              <div key={`s${i}`} className={`slot-tile ${ACT_SLOT[s.action]}`} onClick={() => onSelect(s.item_id)}>
                <div className="slot-no">
                  <span className={`badge ${ACT_BADGE[s.action]}`}>{s.action}</span>
                  {s.live && <span className="pos" title="live on GE"> ●</span>}
                </div>
                <div className="slot-item" title={s.name}>{s.name}</div>
                <div className="slot-meta">{slotQP(s)}</div>
              </div>
            ))}
            {Array.from({ length: Math.max(0, 8 - plan.slots.length) }, (_, i) => (
              <div key={`f${i}`} className="slot-tile free">
                <div className="slot-no">Slot {plan.slots.length + i + 1}</div>
                <div className="slot-free">free</div>
              </div>
            ))}
          </div>
        </div>

        <div className="dash-card">
          <h3>
            Road to {headline?.label ?? "1B"} <span className="dim" style={{ fontWeight: 400, textTransform: "none", letterSpacing: 0 }}>compounding {pct(g.daily_pct, 1)}/day</span>
            <button className="linklike" onClick={() => goTo("growth")}>growth detail →</button>
          </h3>
          <GrowthChart g={g} />
          <LiquidityClock clock={plan.liquidity_clock} />
        </div>
      </div>
    </div>
  );
}
