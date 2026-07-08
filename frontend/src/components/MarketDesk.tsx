import { Fragment, useEffect, useMemo, useState, type ReactNode } from "react";
import { getMarketDesk, type DeskIssue, type DeskItemRow, type MarketDeskResponse, type Prediction } from "../api";
import { gp, gpShort } from "../format";

type Period = "daily" | "weekly" | "monthly";
const PERIODS: { id: Period; label: string }[] = [
  { id: "daily", label: "Daily" },
  { id: "weekly", label: "Weekly" },
  { id: "monthly", label: "Monthly" },
];

const sigPct = (x: number | null | undefined, dp = 1): ReactNode => {
  if (x == null) return <span className="muted">—</span>;
  const v = x * 100;
  const cls = v > 0.05 ? "pos" : v < -0.05 ? "neg" : "muted";
  return <span className={cls}>{v > 0 ? "+" : ""}{v.toFixed(dp)}%</span>;
};

/** Inline markdown: **bold** only (safe — builds React nodes, no dangerouslySetInnerHTML). */
function inline(text: string): ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*)/g).filter(Boolean).map((seg, i) =>
    seg.startsWith("**") && seg.endsWith("**") ? <strong key={i}>{seg.slice(2, -2)}</strong> : <Fragment key={i}>{seg}</Fragment>
  );
}

/** Markdown-lite: #/## headings, - lists, blank-line paragraphs. Enough for the analyst column. */
function Markdown({ text }: { text: string }) {
  const blocks: ReactNode[] = [];
  const lines = text.replace(/\r/g, "").split("\n");
  let para: string[] = [];
  let list: string[] = [];
  const flushPara = () => { if (para.length) { blocks.push(<p key={`p${blocks.length}`}>{inline(para.join(" "))}</p>); para = []; } };
  const flushList = () => {
    if (list.length) {
      blocks.push(<ul key={`u${blocks.length}`} style={{ margin: "6px 0 10px", paddingLeft: 20 }}>
        {list.map((li, i) => <li key={i} style={{ margin: "3px 0" }}>{inline(li)}</li>)}
      </ul>);
      list = [];
    }
  };
  for (const raw of lines) {
    const l = raw.trimEnd();
    if (!l.trim()) { flushPara(); flushList(); continue; }
    if (l.startsWith("## ")) { flushPara(); flushList(); blocks.push(<h3 key={`h${blocks.length}`} className="desk-h">{inline(l.slice(3))}</h3>); }
    else if (l.startsWith("# ")) { flushPara(); flushList(); blocks.push(<h2 key={`h${blocks.length}`} className="desk-title">{inline(l.slice(2))}</h2>); }
    else if (/^[-*]\s+/.test(l)) { flushPara(); list.push(l.replace(/^[-*]\s+/, "")); }
    else { flushList(); para.push(l); }
  }
  flushPara(); flushList();
  return <div className="desk-prose">{blocks}</div>;
}

function Tile({ k, v, cls = "", title }: { k: string; v: ReactNode; cls?: string; title?: string }) {
  return <div className="tile" title={title}><div className="k">{k}</div><div className={`v ${cls}`}>{v}</div></div>;
}

function MoverTable({ title, rows, field }: { title: string; rows: DeskItemRow[]; field: "chg" | "vol_ratio" }) {
  if (!rows?.length) return null;
  return (
    <div style={{ minWidth: 240, flex: 1 }}>
      <div className="k" style={{ marginBottom: 4 }}>{title}</div>
      <table className="mini"><tbody>
        {rows.slice(0, 6).map((r) => (
          <tr key={r.item_id}>
            <td style={{ textAlign: "left" }}>{r.name}</td>
            <td style={{ textAlign: "right" }} className="muted">{gpShort(r.mid ?? 0)}</td>
            <td style={{ textAlign: "right" }}>
              {field === "chg" ? sigPct(r.chg) : <span className="muted">{(r.vol_ratio ?? 0).toFixed(1)}×</span>}
            </td>
          </tr>
        ))}
      </tbody></table>
    </div>
  );
}

/** Fallback view when no analyst prose is stored yet (e.g. before the narrative key is set). */
function FactsView({ issue }: { issue: DeskIssue }) {
  const p = issue.packet;
  if (!p) return <div className="muted">No issue yet — the desk publishes nightly.</div>;
  const i = p.internals, rg = p.regime;
  return (
    <div>
      <div className="hint" style={{ marginBottom: 10 }}>
        Facts view (analyst prose not generated yet — set <code>ANTHROPIC_API_KEY</code> to enable the written column).
      </div>
      <div className="tiles" style={{ marginBottom: 12 }}>
        <Tile k="Regime" v={rg?.label ?? "—"} cls={rg?.label === "risk-on" ? "pos" : rg?.label === "risk-off" ? "neg" : ""} />
        <Tile k="Breadth" v={i ? `${i.advancers}▲ / ${i.decliners}▼` : "—"} />
        <Tile k="% Positive" v={i?.pct_positive != null ? `${i.pct_positive}%` : "—"} />
        <Tile k="Median move" v={sigPct((i?.median_move_pct ?? 0) / 100)} />
        <Tile k="Leading sector" v={rg?.leading_sector ?? "—"} />
        <Tile k="Volatility" v={rg?.volatility ?? "—"} />
      </div>
      <div style={{ display: "flex", gap: 20, flexWrap: "wrap", marginBottom: 12 }}>
        <MoverTable title="Top gainers" rows={p.movers?.gainers ?? []} field="chg" />
        <MoverTable title="Top losers" rows={p.movers?.losers ?? []} field="chg" />
        <MoverTable title="Accumulation (vol×)" rows={p.volume?.accumulation ?? []} field="vol_ratio" />
      </div>
      {!!p.events?.length && (
        <div>
          <div className="k" style={{ marginBottom: 4 }}>Event radar</div>
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {p.events.slice(0, 5).map((e, n) => <li key={n} className="muted" style={{ margin: "2px 0" }}>{e.title}</li>)}
          </ul>
        </div>
      )}
    </div>
  );
}

function outcomeBadge(o: string): ReactNode {
  const map: Record<string, { t: string; c: string }> = {
    hit: { t: "HIT", c: "pos" }, dir: { t: "RIGHT WAY", c: "pos" },
    miss: { t: "MISS", c: "neg" }, pending: { t: "OPEN", c: "muted" },
  };
  const m = map[o] ?? map.pending;
  return <span className={`badge ${m.c}`}>{m.t}</span>;
}

function PredRow({ p }: { p: Prediction }) {
  const arrow = p.direction > 0 ? "▲" : "▼";
  return (
    <tr>
      <td style={{ textAlign: "left" }}>{p.name}</td>
      <td className={p.direction > 0 ? "pos" : "neg"}>{arrow}</td>
      <td style={{ textAlign: "right" }} className="muted">{gp(p.ref_price)}</td>
      <td style={{ textAlign: "right" }}>{gp(p.target_price)}</td>
      <td style={{ textAlign: "center" }} className="muted">{p.horizon_days}d</td>
      <td style={{ textAlign: "center" }}>{Math.round(p.confidence * 100)}%</td>
      <td style={{ textAlign: "center" }}>{outcomeBadge(p.outcome)}</td>
      <td style={{ textAlign: "right" }}>{p.fwd_pct != null ? sigPct(p.fwd_pct) : ""}</td>
    </tr>
  );
}

export function MarketDesk({ refreshNonce }: { refreshNonce: number }) {
  const [data, setData] = useState<MarketDeskResponse | null>(null);
  const [period, setPeriod] = useState<Period>(() => (localStorage.getItem("ge.desk.period") as Period) || "daily");
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    getMarketDesk().then((d) => { if (alive) { setData(d); setErr(null); } })
      .catch((e) => { if (alive) setErr(String(e)); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [refreshNonce]);

  useEffect(() => { localStorage.setItem("ge.desk.period", period); }, [period]);

  const sc = data?.scorecard;
  const issue = data?.latest?.[period];
  const openCalls = useMemo(() => (data?.open_predictions ?? []).filter((p) => p.period === period), [data, period]);
  const resolved = data?.resolved_predictions ?? [];

  return (
    <div className="desk">
      <div className="desk-head" style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap", marginBottom: 12 }}>
        <h2 style={{ margin: 0 }}>📰 Market Desk</h2>
        <div className="tabs">
          {PERIODS.map((p) => (
            <button key={p.id} className={`tab ${period === p.id ? "active" : ""}`} onClick={() => setPeriod(p.id)}>{p.label}</button>
          ))}
        </div>
        {issue && <span className="muted" style={{ fontSize: 12 }}>issue #{issue.id} · {new Date(issue.ts).toLocaleString()}</span>}
      </div>

      {/* standing track record — the point of the whole thing */}
      {sc && sc.resolved > 0 ? (
        <div className="tiles" style={{ marginBottom: 14 }}>
          <Tile k="Calls resolved" v={sc.resolved} title="Matured, auto-graded predictions" />
          <Tile k="Direction acc." v={`${sc.dir_accuracy ?? 0}%`} cls={(sc.dir_accuracy ?? 0) >= 55 ? "pos" : ""} />
          <Tile k="Target hit" v={`${sc.target_hit_rate ?? 0}%`} />
          <Tile k="Edge (median)" v={sigPct((sc.edge_median_pct ?? 0) / 100)} title="Realized move in the predicted direction" />
          <Tile k="Brier" v={(sc.brier ?? 0).toFixed(3)} title="Calibration error (lower is better)" />
          <Tile k="Open calls" v={sc.open ?? 0} />
        </div>
      ) : (
        <div className="hint" style={{ marginBottom: 14 }}>
          Track record builds as calls mature (7-day horizon). {sc?.open ? `${sc.open} open call(s) pending.` : ""}
        </div>
      )}

      {loading && !data ? <div className="muted">Loading the desk…</div>
        : err ? <div className="neg">Failed to load: {err}</div>
        : !issue ? <div className="muted">No {period} issue published yet — the desk runs nightly.</div>
        : (
          <div className="desk-body" style={{ display: "grid", gridTemplateColumns: "minmax(0,1.6fr) minmax(0,1fr)", gap: 18 }}>
            <div className="card" style={{ padding: "4px 18px 14px" }}>
              {issue.prose ? <Markdown text={issue.prose} /> : <FactsView issue={issue} />}
            </div>
            <div>
              <div className="card" style={{ padding: 14, marginBottom: 14 }}>
                <div className="k" style={{ marginBottom: 6 }}>The desk's calls — {period}</div>
                {openCalls.length ? (
                  <table className="mini"><thead><tr>
                    <th style={{ textAlign: "left" }}>Item</th><th></th><th style={{ textAlign: "right" }}>Now</th>
                    <th style={{ textAlign: "right" }}>Target</th><th>Hz</th><th>Conv</th><th>Status</th><th></th>
                  </tr></thead><tbody>{openCalls.map((p) => <PredRow key={p.id} p={p} />)}</tbody></table>
                ) : <div className="muted">No open calls this period.</div>}
              </div>
              {!!resolved.length && (
                <div className="card" style={{ padding: 14 }}>
                  <div className="k" style={{ marginBottom: 6 }}>Recently graded</div>
                  <table className="mini"><thead><tr>
                    <th style={{ textAlign: "left" }}>Item</th><th></th><th style={{ textAlign: "right" }}>Ref</th>
                    <th style={{ textAlign: "right" }}>Target</th><th>Hz</th><th>Conv</th><th>Result</th><th style={{ textAlign: "right" }}>Move</th>
                  </tr></thead><tbody>{resolved.slice(0, 12).map((p) => <PredRow key={p.id} p={p} />)}</tbody></table>
                </div>
              )}
            </div>
          </div>
        )}
    </div>
  );
}
