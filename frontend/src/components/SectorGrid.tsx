import type { SectorsResponse } from "../api";
import { gpShort, spct } from "../format";
import { C } from "../theme";

const cls = (x: number | null | undefined) => (x == null ? "flat" : x > 0 ? "pos" : x < 0 ? "neg" : "flat");

/** Tiny inline SVG sparkline of the sector index (% anchored at 0). */
function Sparkline({ data, up }: { data: number[]; up: boolean }) {
  if (!data || data.length < 2) return <div className="spark spark-empty" />;
  const w = 200, h = 38, pad = 3;
  const min = Math.min(...data, 0), max = Math.max(...data, 0);
  const span = max - min || 1;
  const x = (i: number) => pad + (i / (data.length - 1)) * (w - 2 * pad);
  const y = (v: number) => pad + (1 - (v - min) / span) * (h - 2 * pad);
  const pts = data.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  return (
    <svg className="spark" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none">
      <line x1={0} x2={w} y1={y(0)} y2={y(0)} className="spark-zero" />
      <polyline points={pts} fill="none" stroke={up ? C.green : C.red} strokeWidth={1.6} />
    </svg>
  );
}

/** Grid of sector "ETF" cards (Whole Market pinned first). Click a card for the deep-dive. */
export function SectorGrid({
  data,
  selectedKey,
  onSelect,
}: {
  data: SectorsResponse | null;
  selectedKey: string | null;
  onSelect: (key: string) => void;
}) {
  const sectors = data?.sectors ?? [];
  if (!sectors.length) {
    return (
      <div className="sector-wrap">
        <div className="muted pad">
          No sectors yet — needs live history to build the indices. If this is fresh, give the collector a little
          time, or loosen Min volume.
        </div>
      </div>
    );
  }
  return (
    <div className="sector-wrap">
      <div className="sector-note">
        {data?.coverage.classified ?? 0} of {data?.coverage.liquid ?? 0} liquid items classified · cap-weighted by gp
        traded/day · headline = 1-day move, sorted by 1d.
      </div>
      <div className="sector-grid">
        {sectors.map((s) => {
          const d1 = s.changes["1d"];
          return (
            <div
              key={s.key}
              className={`sector-card ${s.key === "market" ? "market" : ""} ${s.key === selectedKey ? "selected" : ""}`}
              onClick={() => onSelect(s.key)}
            >
              <div className="sc-head">
                <span className="sc-label">{s.label}</span>
                <span className={`sc-move ${cls(d1)}`}>{spct(d1)}</span>
              </div>
              <div className="sc-blurb">{s.blurb}</div>
              <Sparkline data={s.spark} up={(d1 ?? 0) >= 0} />
              <div className="sc-rets">
                <span>2w <b className={cls(s.changes["2w"])}>{spct(s.changes["2w"])}</b></span>
                <span>3mo <b className={cls(s.changes["3mo"])}>{spct(s.changes["3mo"])}</b></span>
                <span>1y <b className={cls(s.changes["1y"])}>{spct(s.changes["1y"])}</b></span>
              </div>
              <div className="sc-foot">
                <span>{s.n_items} items</span>
                <span>{gpShort(s.gp_vol)}/day</span>
                <span title="whole sector price vs its 7-day level — negative = sector is cheap">
                  vs 7d <b className={cls(s.dev)}>{spct(s.dev)}</b>
                </span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
