import { pct } from "../format";

export interface ProfileRow {
  label: string;
  dev: number | null;
}

/** Diverging horizontal bars: green to the left = historically cheap (good to
 *  buy), red to the right = expensive. Centered at the period mean. */
export function ProfileBars({ rows }: { rows: ProfileRow[] }) {
  const maxAbs = Math.max(0.0001, ...rows.map((r) => Math.abs(r.dev ?? 0)));
  return (
    <div className="profile">
      {rows.map((r) => {
        const dev = r.dev ?? 0;
        const w = Math.min(48, (Math.abs(dev) / maxAbs) * 48);
        const up = dev >= 0;
        const style: React.CSSProperties = up
          ? { left: "50%", width: `${w}%` }
          : { left: `${50 - w}%`, width: `${w}%` };
        return (
          <div className="prow" key={r.label}>
            <span className="lbl">{r.label}</span>
            <span className="barwrap">
              <span className="mid" />
              <span className={`bar ${up ? "up" : "down"}`} style={style} />
            </span>
            <span className={`val ${up ? "neg" : "pos"}`}>
              {dev > 0 ? "+" : ""}
              {pct(dev, 1)}
            </span>
          </div>
        );
      })}
    </div>
  );
}
