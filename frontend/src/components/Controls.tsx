import { useEffect, useRef, useState } from "react";
import type { Filters } from "../api";
import { PriceRange } from "./PriceRange";

/** Numeric input that keeps its own text state so you can type "-", "", "1.", etc.
 *  without it snapping to 0 mid-edit (the old `Number(x)||0` broke negative Z values
 *  and made fields impossible to clear). Commits a parsed number only when valid.
 *  `factor` scales display<->value (e.g. 100 shows a 0.004 ratio as "0.4"). */
function NumInput({
  value,
  onCommit,
  factor = 1,
  className = "",
  decimals = 6,
}: {
  value: number;
  onCommit: (n: number) => void;
  factor?: number;
  className?: string;
  decimals?: number;
}) {
  const disp = (v: number) => String(+(v * factor).toFixed(decimals));
  const [s, setS] = useState(() => disp(value));
  const last = useRef(value);
  useEffect(() => {
    if (value !== last.current) {
      setS(disp(value));
      last.current = value;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);
  return (
    <input
      className={className}
      inputMode="decimal"
      value={s}
      onChange={(e) => {
        const v = e.target.value;
        setS(v);
        const t = v.trim();
        if (t === "" || t === "-" || t === "." || t === "-.") return; // intermediate — don't commit yet
        const n = Number(t);
        if (Number.isFinite(n)) {
          const real = n / factor;
          last.current = real;
          onCommit(real);
        }
      }}
    />
  );
}

export function Controls({ filters, setFilters, onBankrollCommit }: { filters: Filters; setFilters: (f: Filters) => void; onBankrollCommit?: (n: number) => void }) {
  const set = (k: keyof Filters) => (n: number) => setFilters({ ...filters, [k]: n });

  return (
    <>
      <div className="ctrl">
        <label title="Your FREE gp — cash available to deploy now (server-tracked: auto-adjusts as orders are placed/filled/cancelled). The 8-Slot Plan + Growth add open orders + holdings on top to get net worth.">Free gp</label>
        <NumInput value={filters.bankroll} onCommit={onBankrollCommit ?? set("bankroll")} />
      </div>
      <div className="ctrl">
        <label>Min profit (gp)</label>
        <NumInput value={filters.minProfit} onCommit={set("minProfit")} />
      </div>
      <div className="ctrl">
        <label title="8-Slot Plan only: skip any buy whose profit per round-trip is below this.">Min profit/RT</label>
        <NumInput value={filters.minRtProfit} onCommit={set("minRtProfit")} />
      </div>
      <PriceRange
        minPrice={filters.minPrice}
        maxPrice={filters.maxPrice}
        onChange={(min, max) => setFilters({ ...filters, minPrice: min, maxPrice: max })}
      />
      <div className="ctrl small">
        <label>Min vol</label>
        <NumInput value={filters.minVolume} onCommit={set("minVolume")} />
      </div>
      <div className="ctrl small">
        <label>Min net</label>
        <NumInput value={filters.minMargin} onCommit={set("minMargin")} />
      </div>
      <div className="ctrl small">
        <label>Min ROI %</label>
        <NumInput value={filters.minRoi} factor={100} decimals={2} onCommit={set("minRoi")} />
      </div>
      <div className="ctrl small" title="Invest tab: minimum 0-100 value-buy confidence">
        <label>Min conf</label>
        <NumInput value={filters.minConfidence} onCommit={set("minConfidence")} />
      </div>
      <div className="ctrl small" title="Invest tab: minimum % below the established fair-value level">
        <label>Min disc %</label>
        <NumInput value={filters.minDiscount} factor={100} decimals={0} onCommit={set("minDiscount")} />
      </div>
      <div className="ctrl small">
        <label>Z buy ≤</label>
        <NumInput value={filters.zBuy} onCommit={set("zBuy")} />
      </div>
      <div className="ctrl small">
        <label>Z sell ≥</label>
        <NumInput value={filters.zSell} onCommit={set("zSell")} />
      </div>
    </>
  );
}
