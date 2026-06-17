export const DASH = "–";

export const gp = (n: number | null | undefined): string =>
  n == null || Number.isNaN(n) ? DASH : Math.round(n).toLocaleString("en-US");

export function gpShort(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return DASH;
  const a = Math.abs(n);
  if (a >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (a >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (a >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return Math.round(n).toString();
}

export const pct = (x: number | null | undefined, dp = 1): string =>
  x == null || Number.isNaN(x) ? DASH : (x * 100).toFixed(dp) + "%";

export const fixed = (x: number | null | undefined, dp = 2): string =>
  x == null || Number.isNaN(x) ? DASH : x.toFixed(dp);

export const num = (n: number | null | undefined): string =>
  n == null || Number.isNaN(n) ? DASH : Math.round(n).toLocaleString("en-US");

export const age = (mins: number | null | undefined): string => {
  if (mins == null || Number.isNaN(mins)) return DASH;
  if (mins < 60) return `${Math.round(mins)}m`;
  if (mins < 1440) return `${(mins / 60).toFixed(1)}h`;
  return `${(mins / 1440).toFixed(1)}d`;
};
