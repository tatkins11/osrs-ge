import { useEffect, useRef, useState, type ReactNode } from "react";
import { ColorType, createChart, type UTCTimestamp } from "lightweight-charts";
import { getSectorDetail, type Filters, type SectorDetail, type SectorIndexPoint } from "../api";
import { gp, gpShort, pctp, spct } from "../format";

const cls = (x: number | null | undefined) => (x == null ? "" : x > 0 ? "pos" : x < 0 ? "neg" : "");

function Tile({ k, v, cls = "" }: { k: string; v: ReactNode; cls?: string }) {
  return (
    <div className="tile">
      <div className="k">{k}</div>
      <div className={`v ${cls}`}>{v}</div>
    </div>
  );
}

/** Sector index as a baseline area chart: green above the 0% start, red below. */
function SectorChart({ series }: { series: SectorIndexPoint[] }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current || !series?.length) return;
    const chart = createChart(ref.current, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "#9fb0c3",
        fontFamily: "ui-monospace, monospace",
        fontSize: 11,
      },
      grid: { vertLines: { color: "#15202d" }, horzLines: { color: "#15202d" } },
      rightPriceScale: { borderColor: "#1e2a39" },
      timeScale: { borderColor: "#1e2a39", timeVisible: true, secondsVisible: false },
      crosshair: { mode: 0 },
    });
    const base = chart.addBaselineSeries({
      baseValue: { type: "price", price: 0 },
      topLineColor: "#25d07d",
      topFillColor1: "rgba(37,208,125,.28)",
      topFillColor2: "rgba(37,208,125,.02)",
      bottomLineColor: "#ff5b6e",
      bottomFillColor1: "rgba(255,91,110,.02)",
      bottomFillColor2: "rgba(255,91,110,.28)",
      lineWidth: 2,
      priceLineVisible: false,
      priceFormat: { type: "custom", formatter: (v: number) => `${v >= 0 ? "+" : ""}${v.toFixed(1)}%` },
    });
    base.setData(series.map((p) => ({ time: p.time as UTCTimestamp, value: p.index })));
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [series]);
  return <div className="chart-box" ref={ref} />;
}

/** Deep-dive for one sector: index chart + ranked constituents (click through to item). */
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

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    getSectorDetail(sectorKey, filters)
      .then((r) => !cancelled && setD(r))
      .catch(() => !cancelled && setD(null))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [sectorKey, filters, refreshNonce]);

  if (loading && !d) return <div className="placeholder">Loading…</div>;
  if (!d) return <div className="placeholder">No data for this sector.</div>;

  return (
    <div>
      <div className="panel-head">
        <span className="close" onClick={onClose}>×</span>
        <div className="title">{d.label}</div>
        <div className="sub">{d.blurb} · cap-weighted index, last ~2 weeks</div>
      </div>

      <div className="panel-section">
        <h4>Index move</h4>
        <div className="tiles">
          <Tile k="1h" v={pctp(d.ret_1h)} cls={cls(d.ret_1h)} />
          <Tile k="6h" v={pctp(d.ret_6h)} cls={cls(d.ret_6h)} />
          <Tile k="24h" v={pctp(d.ret_24h)} cls={cls(d.ret_24h)} />
          <Tile k="7d" v={pctp(d.ret_7d)} cls={cls(d.ret_7d)} />
        </div>
      </div>

      <div className="panel-section">
        <h4>Sector index · % move vs 2 weeks ago</h4>
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
    </div>
  );
}
