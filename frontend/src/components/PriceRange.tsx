import { gpShort } from "../format";

// Dual-handle price filter on a LOG scale (prices span 1gp .. ~2.1B, so linear is useless).
const MAXP = 2_100_000_000;
const NO_CAP = 2_147_483_647; // backend "no ceiling" sentinel
const L = Math.log10(MAXP);
const RES = 1000;

const toPos = (price: number) => (price <= 1 ? 0 : Math.max(0, Math.min(RES, Math.round((RES * Math.log10(price)) / L))));
const toPrice = (pos: number) => {
  if (pos <= 0) return 0;
  if (pos >= RES) return NO_CAP;
  const raw = Math.pow(10, (pos / RES) * L);
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  return Math.round((raw / mag) * 10) / 10 * mag; // 2 significant figures
};

/** Min/max price range slider. Sends minPrice (0 = no floor) and maxPrice (NO_CAP = no ceiling). */
export function PriceRange({
  minPrice,
  maxPrice,
  onChange,
}: {
  minPrice: number;
  maxPrice: number;
  onChange: (min: number, max: number) => void;
}) {
  const lo = toPos(minPrice);
  const hi = maxPrice >= MAXP ? RES : toPos(maxPrice);
  const setLo = (p: number) => onChange(toPrice(Math.min(p, hi)), maxPrice);
  const setHi = (p: number) => {
    const np = Math.max(p, lo);
    onChange(minPrice, np >= RES ? NO_CAP : toPrice(np));
  };
  const loLabel = minPrice <= 0 ? "0" : gpShort(minPrice);
  const hiLabel = maxPrice >= MAXP ? "max" : gpShort(maxPrice);

  return (
    <div className="ctrl price-range">
      <label>Price range · {loLabel} – {hiLabel}</label>
      <div className="rng">
        <div className="rng-track" />
        <div className="rng-fill" style={{ left: `${(lo / RES) * 100}%`, right: `${100 - (hi / RES) * 100}%` }} />
        <input type="range" min={0} max={RES} value={lo} onChange={(e) => setLo(Number(e.target.value))} className="rng-a" aria-label="Min price" />
        <input type="range" min={0} max={RES} value={hi} onChange={(e) => setHi(Number(e.target.value))} className="rng-b" aria-label="Max price" />
      </div>
    </div>
  );
}
