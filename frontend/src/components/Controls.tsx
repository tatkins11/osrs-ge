import type { ChangeEvent } from "react";
import type { Filters } from "../api";
import { PriceRange } from "./PriceRange";

export function Controls({ filters, setFilters }: { filters: Filters; setFilters: (f: Filters) => void }) {
  const upd = (k: keyof Filters) => (e: ChangeEvent<HTMLInputElement>) =>
    setFilters({ ...filters, [k]: Number(e.target.value) || 0 });

  return (
    <>
      <div className="ctrl">
        <label>Bankroll (gp)</label>
        <input value={filters.bankroll} onChange={upd("bankroll")} />
      </div>
      <div className="ctrl">
        <label>Min profit (gp)</label>
        <input value={filters.minProfit} onChange={upd("minProfit")} />
      </div>
      <PriceRange
        minPrice={filters.minPrice}
        maxPrice={filters.maxPrice}
        onChange={(min, max) => setFilters({ ...filters, minPrice: min, maxPrice: max })}
      />
      <div className="ctrl small">
        <label>Min vol</label>
        <input value={filters.minVolume} onChange={upd("minVolume")} />
      </div>
      <div className="ctrl small">
        <label>Min net</label>
        <input value={filters.minMargin} onChange={upd("minMargin")} />
      </div>
      <div className="ctrl small">
        <label>Min ROI %</label>
        <input
          value={+(filters.minRoi * 100).toFixed(2)}
          onChange={(e) => setFilters({ ...filters, minRoi: (Number(e.target.value) || 0) / 100 })}
        />
      </div>
      <div className="ctrl small">
        <label>Z buy ≤</label>
        <input value={filters.zBuy} onChange={upd("zBuy")} />
      </div>
      <div className="ctrl small">
        <label>Z sell ≥</label>
        <input value={filters.zSell} onChange={upd("zSell")} />
      </div>
    </>
  );
}
