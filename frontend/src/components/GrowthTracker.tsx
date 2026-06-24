import { useEffect, useState, type ReactNode } from "react";
import { getGrowth, type Filters, type GrowthResponse } from "../api";
import { gp, gpShort, pct } from "../format";

function Tile({ k, v, cls = "", title, sub }: { k: string; v: ReactNode; cls?: string; title?: string; sub?: string }) {
  return (
    <div className="tile" title={title}>
      <div className="k">{k}</div>
      <div className={`v ${cls}`}>{v}</div>
      {sub && <div className="k" style={{ marginTop: 2, textTransform: "none", letterSpacing: 0 }}>{sub}</div>}
    </div>
  );
}

const DAY = 86400000;
const fmtDate = (days: number | null) => {
  if (days == null) return "never";
  if (days <= 0) return "reached ✓";
  return new Date(Date.now() + days * DAY).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
};
const fmtDays = (days: number | null) =>
  days == null || days <= 0 ? "" : days < 60 ? `${Math.round(days)}d` : days < 730 ? `${(days / 30.4).toFixed(1)}mo` : `${(days / 365).toFixed(1)}yr`;

/** Growth chart: log-scale net worth over time (solid) + compounding projection to the next target (dashed). */
function GrowthChart({ g }: { g: GrowthResponse }) {
  const W = 760, H = 240, PAD = 46;
  const nw = g.net_worth;
  const headline = g.targets.find((t) => t.value > nw) ?? g.targets[g.targets.length - 1];
  const hist = g.history.filter((h) => h.value > 0);

  const proj: { t: number; value: number }[] = [];
  if (g.daily_pct > 0) {
    const etaD = headline?.days_realized ?? null;
    const horizon = etaD && etaD > 0 ? Math.min(etaD * 1.05, 365) : 180;
    for (let i = 0; i <= 40; i++) {
      const d = (horizon * i) / 40;
      proj.push({ t: Date.now() + d * DAY, value: nw * Math.pow(1 + g.daily_pct, d) });
    }
  }
  const histT = hist.map((h) => new Date(h.ts).getTime());
  const tMin = histT.length ? Math.min(...histT) : Date.now();
  const tMax = proj.length ? proj[proj.length - 1].t : histT.length ? Math.max(...histT) : Date.now() + DAY;
  const allV = [...hist.map((h) => h.value), ...proj.map((p) => p.value), nw];
  const vMin = Math.max(1, Math.min(...allV) * 0.85);
  const vMax = Math.max(headline?.value ?? 0, ...allV) * 1.12;
  const X = (t: number) => PAD + ((t - tMin) / (tMax - tMin || 1)) * (W - 2 * PAD);
  const lmin = Math.log10(vMin), lmax = Math.log10(vMax);
  const Y = (v: number) => H - PAD - ((Math.log10(Math.max(v, 1)) - lmin) / (lmax - lmin || 1)) * (H - 2 * PAD);
  const poly = (pts: { t: number; value: number }[]) => pts.map((p) => `${X(p.t).toFixed(1)},${Y(p.value).toFixed(1)}`).join(" ");

  const lines = g.targets.filter((t) => t.value >= vMin && t.value <= vMax);
  const nowX = X(Date.now());

  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: "auto", display: "block" }}>
      {/* target gridlines */}
      {lines.map((t) => (
        <g key={t.label}>
          <line x1={PAD} x2={W - PAD} y1={Y(t.value)} y2={Y(t.value)} stroke="var(--border)" strokeDasharray="3 4" />
          <text x={W - PAD + 2} y={Y(t.value) + 3} fontSize="10" fill="var(--muted)">{t.label}</text>
        </g>
      ))}
      {/* now marker */}
      {proj.length > 0 && <line x1={nowX} x2={nowX} y1={PAD - 8} y2={H - PAD} stroke="var(--border)" strokeDasharray="2 3" />}
      {proj.length > 0 && <text x={nowX} y={PAD - 12} fontSize="9" fill="var(--muted)" textAnchor="middle">now</text>}
      {/* projection (dashed) */}
      {proj.length > 1 && <polyline points={poly(proj)} fill="none" stroke="var(--accent)" strokeWidth="1.6" strokeDasharray="5 4" />}
      {/* history (solid) */}
      {hist.length > 1 && <polyline points={poly(hist.map((h) => ({ t: new Date(h.ts).getTime(), value: h.value })))} fill="none" stroke="var(--green)" strokeWidth="2" />}
      {/* current point */}
      <circle cx={nowX} cy={Y(nw)} r="3.5" fill="var(--green)" />
      {/* y range labels */}
      <text x={PAD - 4} y={Y(nw) + 3} fontSize="10" fill="var(--fg)" textAnchor="end">{gpShort(nw)}</text>
      {/* x labels */}
      {histT.length > 0 && <text x={PAD} y={H - PAD + 14} fontSize="9" fill="var(--muted)">{new Date(tMin).toLocaleDateString(undefined, { month: "short", day: "numeric" })}</text>}
      {headline?.days_realized != null && headline.days_realized > 0 && (
        <text x={W - PAD} y={H - PAD + 14} fontSize="9" fill="var(--muted)" textAnchor="end">{fmtDate(headline.days_realized)}</text>
      )}
    </svg>
  );
}

/** Bankroll growth tracker — net worth, the real %/day, and the projected road to billions. */
export function GrowthTracker({ filters, refreshNonce }: { filters: Filters; refreshNonce: number }) {
  const [g, setG] = useState<GrowthResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setErr(null);
    getGrowth(filters)
      .then((d) => !cancelled && setG(d))
      .catch((e) => !cancelled && setErr(String(e)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [filters, refreshNonce]);

  if (err) return <div className="empty">Growth error: {err}</div>;
  if (!g) return <div className="empty">{loading ? "Tracking your bankroll…" : "No data."}</div>;

  const headline = g.targets.find((t) => t.value > g.net_worth) ?? g.targets[g.targets.length - 1];
  const idleHigh = g.idle_frac >= 0.4;

  return (
    <div className="tbl-scroll">
      <div className="exp-banner">
        <b>Growth tracker.</b> Net worth = your cash (bankroll) + holdings at live value. The solid line is your real
        net-worth path from logged trades; the dashed line projects it forward, compounding at your{" "}
        <b>realized {pct(g.daily_pct, 1)}/day</b>. Keep that rate up and deploy idle gp — that's the whole game. Set the{" "}
        <b>bankroll</b> filter to your liquid cash for an accurate read.
      </div>

      <div className="tiles" style={{ gridTemplateColumns: "repeat(5, 1fr)" }}>
        <Tile k="Net worth" v={gpShort(g.net_worth)} title={`Free ${gp(g.bankroll)} + open buys ${gp(g.committed)} + holdings ${gp(g.holdings_value)}`} sub={`${gpShort(g.bankroll)} free · ${gpShort(g.committed)} orders · ${gpShort(g.holdings_value)} held`} />
        <Tile k="Realized P&L" v={gp(g.realized_total)} cls={g.realized_total >= 0 ? "pos" : "neg"} title={`Over ${g.days_active} days of logged trades`} sub={g.win_rate != null ? `${pct(g.win_rate, 0)} win · ${g.n_closed} trips` : undefined} />
        <Tile k="Growth / day" v={pct(g.daily_pct, 1)} cls={g.daily_pct >= 0 ? "pos" : "neg"} title={`Realized ${gp(g.recent_gp_day)}/day over the last ${g.recent_days} days, vs net worth`} sub={`${gpShort(g.recent_gp_day)}/day realized`} />
        <Tile k={`${headline?.label ?? "1B"} ETA`} v={fmtDate(headline?.days_realized ?? null)} cls="pos" title="At your realized growth rate, compounding" sub={fmtDays(headline?.days_realized ?? null)} />
        <Tile k="Idle capital" v={pct(g.idle_frac, 0)} cls={idleHigh ? "neg" : ""} title={`${gp(g.capital_in)} undeployed — put it to work in the 8-Slot Plan`} sub={`${gpShort(g.capital_in)} undeployed`} />
      </div>

      {idleHigh && (
        <div className="crash-banner">
          ⚑ <b>{pct(g.idle_frac, 0)}</b> of your bankroll (<b>{gpShort(g.capital_in)}</b>) is sitting idle — undeployed gp
          doesn't compound. Fill your free slots on the <b>8-Slot Plan</b> tab to lift your growth rate.
        </div>
      )}

      <div className="slot-head" style={{ marginTop: 14 }}>
        Net worth → {headline?.label ?? "billions"} (log scale){" "}
        <span className="dim" style={{ fontWeight: 400, textTransform: "none", letterSpacing: 0 }}>
          {g.history_source === "snapshots"
            ? `· real daily net worth (${g.n_snapshots} snapshots)`
            : "· reconstructed from realized P&L — real daily snapshots start now"}
        </span>
      </div>
      <div style={{ background: "var(--panel)", border: "1px solid var(--border)", borderRadius: 8, padding: "10px 6px 4px" }}>
        <GrowthChart g={g} />
      </div>

      <div className="slot-head" style={{ marginTop: 16 }}>Road to billions — projected dates</div>
      <table className="tbl">
        <thead>
          <tr>
            <th className="left">Target</th>
            <th className="left" title="At your realized growth rate (actual trade history)">At realized {pct(g.daily_pct, 1)}/day</th>
            <th className="left" title="At the 8-Slot Plan's modeled rate if you deploy fully (optimistic ceiling)">At modeled {pct(g.modeled_pct, 1)}/day</th>
          </tr>
        </thead>
        <tbody>
          {g.targets.filter((t) => t.value > g.net_worth).map((t) => (
            <tr key={t.label}>
              <td className="name left">{t.label} <span className="dim">({gpShort(t.value)})</span></td>
              <td className="left">{fmtDate(t.days_realized)} <span className="dim">{fmtDays(t.days_realized)}</span></td>
              <td className="left pos">{fmtDate(t.days_modeled)} <span className="dim">{fmtDays(t.days_modeled)}</span></td>
            </tr>
          ))}
          {g.targets.every((t) => t.value <= g.net_worth) && (
            <tr><td colSpan={3} className="left pos">You're past every target on the board — add bigger ones. 🎉</td></tr>
          )}
          {g.daily_pct <= 0 && (
            <tr><td colSpan={3} className="left muted">No positive realized growth in the last {g.recent_days} days — log more winning round-trips (or deploy idle capital) to set a rate.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
