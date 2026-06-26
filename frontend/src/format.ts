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

// signed percent for values ALREADY in percentage-points (e.g. sector index moves)
export const pctp = (x: number | null | undefined, dp = 1): string =>
  x == null || Number.isNaN(x) ? DASH : (x >= 0 ? "+" : "") + x.toFixed(dp) + "%";

// signed percent for a FRACTION (e.g. -0.21 -> "-21.0%")
export const spct = (x: number | null | undefined, dp = 1): string =>
  x == null || Number.isNaN(x) ? DASH : (x >= 0 ? "+" : "") + (x * 100).toFixed(dp) + "%";

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

// --- time: render everything in US Central time, 12-hour (AM/PM) -----------------------------
// Central, not the browser's timezone, so it reads the same wherever the page is opened. Auto
// handles CST/CDT via the IANA zone. Backend hour-of-day data is UTC; convert before display.
const CENTRAL_TZ = "America/Chicago";

// Current hour-of-day (0–23) in Central — used to mark "now" on the liquidity clock.
export const centralHourNow = (): number =>
  Number(new Intl.DateTimeFormat("en-US", { timeZone: CENTRAL_TZ, hour: "numeric", hour12: false }).format(new Date())) % 24;

// Hours UTC is currently ahead of Central (5 during CDT, 6 during CST).
export const utcOffsetFromCentral = (): number =>
  ((new Date().getUTCHours() - centralHourNow()) % 24 + 24) % 24;

// A UTC hour-of-day (0–23) → the Central hour-of-day.
export const utcHourToCentral = (utcHour: number): number =>
  ((utcHour - utcOffsetFromCentral()) % 24 + 24) % 24;

// A bare hour in 12-hour form: 0→"12 AM", 13→"1 PM", 23→"11 PM".
export const hour12 = (h24: number): string => `${(h24 % 12) || 12} ${h24 < 12 ? "AM" : "PM"}`;

// Current Central wall-clock like "1:49 PM".
export const centralTimeNow = (): string =>
  new Intl.DateTimeFormat("en-US", { timeZone: CENTRAL_TZ, hour: "numeric", minute: "2-digit", hour12: true }).format(new Date());

// A backend UTC timestamp (ISO, possibly without a 'Z') → "Jun 24, 1:49 PM" in Central.
export const fmtTsCentral = (iso: string | null | undefined): string => {
  if (!iso) return DASH;
  const s = /[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : iso + "Z";  // backend ts are UTC wall-clock
  const d = new Date(s);
  if (Number.isNaN(d.getTime())) return iso;
  return new Intl.DateTimeFormat("en-US", { timeZone: CENTRAL_TZ, month: "short", day: "numeric", hour: "numeric", minute: "2-digit", hour12: true }).format(d);
};
