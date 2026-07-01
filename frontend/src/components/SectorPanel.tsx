import { useEffect, useRef, useState } from "react";
import { ColorType, createChart, type UTCTimestamp } from "lightweight-charts";
import { getSectorDetail, HORIZON_KEYS, type Filters, type SectorDetail, type SectorIndexPoint } from "../api";
import { gp, gpShort, spct } from "../format";
import { ChartModal } from "./ChartModal";
import { C } from "../theme";

const cls = (x: number | null | undefined) => (x == null ? "" : x > 0 ? "pos" : x < 0 ? "neg" : "");
const TFS: [string, string][] = [["2wk", "2wk"], ["3mo", "3mo"], ["1yr", "1yr"]];

// local-timezone axis/crosshair formatting (lightweight-charts is UTC by default)
const fmtFull = (t: number) =>
  new Date(t * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
const tickFmt = (time: unknown, kind: number) => {
  const d = new Date((time as number) * 1000);
  if (kind >= 3) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (kind === 2) return d.toLocaleDateString([], { month: "short", day: "numeric" });
  if (kind === 1) return d.toLocaleDateString([], { month: "short", year: "numeric" });
  return String(d.getFullYear());
};

/** Sector index as a baseline area chart: green above the 0% start, red below. */
function SectorChart({ series, className = "" }: { series: SectorIndexPoint[]; className?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current || !series?.length) return;
    const el = ref.current;
    const chart = createChart(el, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "#9fb0c3",
        fontFamily: "ui-monospace, monospace",
        fontSize: 11,
      },
      grid: { vertLines: { color: "#15202d" }, horzLines: { color: "#15202d" } },
      rightPriceScale: { borderColor: "#1e2a39" },
      timeScale: { borderColor: "#1e2a39", timeVisible: true, secondsVisible: false, tickMarkFormatter: tickFmt },
      localization: { timeFormatter: fmtFull },
      crosshair: { mode: 0 },
    });
    const base = chart.addBaselineSeries({
      baseValue: { type: "price", price: 0 },
      topLineColor: C.green,
      topFillColor1: "rgba(37,208,125,.28)",
      topFillColor2: "rgba(37,208,125,.02)",
      bottomLineColor: C.red,
      bottomFillColor1: "rgba(255,91,110,.02)",
      bottomFillColor2: "rgba(255,91,110,.28)",
      lineWidth: 2,
      priceLineVisible: false,
      priceFormat: { type: "custom", formatter: (v: number) => `${v >= 0 ? "+" : ""}${v.toFixed(1)}%` },
    });
    base.setData(series.map((p) => ({ time: p.time as UTCTimestamp, value: p.index })));

    const tip = document.createElement("div");
    tip.className = "chart-tip";
    tip.style.display = "none";
    el.appendChild(tip);
    chart.subscribeCrosshairMove((param) => {
      const pt = param.point;
      if (!param.time || !pt || pt.x < 0 || pt.y < 0 || pt.x > el.clientWidth || pt.y > el.clientHeight) {
        tip.style.display = "none";
        return;
      }
      const md = param.seriesData.get(base) as { value?: number } | undefined;
      if (md?.value == null) {
        tip.style.display = "none";
        return;
      }
      const v = md.value;
      tip.innerHTML = `<div class="tip-t">${fmtFull(param.time as number)}</div><div class="tip-v ${v >= 0 ? "pos" : "neg"}">${v >= 0 ? "+" : ""}${v.toFixed(2)}%</div>`;
      tip.style.display = "block";
      tip.style.left = Math.min(pt.x + 14, el.clientWidth - 130) + "px";
      tip.style.top = Math.max(6, pt.y - 12) + "px";
    });

    chart.timeScale().fitContent();
    return () => {
      chart.remove();
      tip.remove();
    };
  }, [series]);
  return <div className={`chart-box ${className}`} ref={ref} />;
}

/** Deep-dive for one sector: multi-horizon moves + index chart (timeframe toggle) + constituents. */
export function SectorPanel({
  sectorKey,
  filters,
  refreshNonce,
  onSelectItem,
  onClose,
}: {
  sectorKey: string;
  filters: Filters;
  refreshNonce: number;
  onSelectItem: (id: number) => void;
  onClose: () => void;
}) {
  const [d, setD] = useState<SectorDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [tf, setTf] = useState("2wk");
  const [expanded, setExpanded] = useState(false);

  useEffect(() => setTf("2wk"), [sectorKey]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    getSectorDetail(sectorKey, filters, tf)
      .then((r) => !cancelled && setD(r))
      .catch(() => !cancelled && setD(null))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [sectorKey, filters, refreshNonce, tf]);

  if (loading && !d) return <div className="placeholder">Loading…</div>;
  if (!d) return <div className="placeholder">No data for this sector.</div>;

  return (
    <div>
      <div className="panel-head">
        <span className="close" onClick={onClose}>×</span>
        <div className="title">{d.label}</div>
        <div className="sub">{d.blurb} · cap-weighted index</div>
      </div>

      <div className="panel-section">
        <h4>Price change</h4>
        <div className="tiles changes">
          {HORIZON_KEYS.map((k) => (
            <div className="tile" key={k}>
              <div className="k">{k}</div>
              <div className={`v ${cls(d.changes[k])}`}>{spct(d.changes[k])}</div>
            </div>
          ))}
        </div>
      </div>

      <div className="panel-section">
        <div className="tf-row">
          <h4>Sector index · % move</h4>
          <div style={{ display: "flex", gap: 8 }}>
            <div className="tf-toggle">
              {TFS.map(([v, l]) => (
                <button key={v} className={`tf ${tf === v ? "active" : ""}`} onClick={() => setTf(v)}>
                  {l}
                </button>
              ))}
            </div>
            <button className="expand" title="Expand chart" onClick={() => setExpanded(true)}>⤢</button>
          </div>
        </div>
        <SectorChart series={d.series} />
      </div>

      <div className="panel-section">
        <h4>Constituents <span className="dim">({d.constituents.length}) · click to open the item</span></h4>
        <div className="tbl-scroll">
          <table className="tbl">
            <thead>
              <tr>
                <th className="left">Item</th>
                <th>Price</th>
                <th>vs 7d</th>
                <th>gp vol/day</th>
                <th>Weight</th>
              </tr>
            </thead>
            <tbody>
              {d.constituents.map((c) => (
                <tr key={c.item_id} onClick={() => onSelectItem(c.item_id)}>
                  <td className="name left">{c.name}</td>
                  <td>{gp(c.mid)}</td>
                  <td className={cls(c.dev)}>{spct(c.dev)}</td>
                  <td className="dim">{gpShort(c.gp_vol)}</td>
                  <td className="dim">{c.weight_pct == null ? "–" : c.weight_pct.toFixed(1) + "%"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {expanded && (
        <ChartModal title={`${d.label} · index (${tf})`} onClose={() => setExpanded(false)}>
          <SectorChart series={d.series} className="modal-chart" />
        </ChartModal>
      )}
    </div>
  );
}
