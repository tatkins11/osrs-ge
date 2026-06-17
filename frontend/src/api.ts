export interface Row {
  item_id: number;
  name: string;
  members?: boolean;
  exempt?: boolean;
  buy_limit?: number | null;
  buy_price?: number | null;
  sell_price?: number | null;
  mid?: number | null;
  tax?: number | null;
  gross_margin?: number | null;
  net_margin?: number | null;
  roi?: number | null;
  profit_per_cycle?: number | null;
  sugg_units?: number | null;
  sugg_capital?: number | null;
  sugg_profit?: number | null;
  affordable?: boolean;
  high_vol?: number | null;
  low_vol?: number | null;
  vol_side?: number | null;
  vol_daily_7d?: number | null;
  price_age_min?: number | null;
  mean_7d?: number | null;
  sd_7d?: number | null;
  z_7d?: number | null;
  pct_30d?: number | null;
  volatility_7d?: number | null;
  min_30d?: number | null;
  max_30d?: number | null;
  signal?: string;
  flip_ok?: boolean;
  tradeable?: boolean;
  mr_entry?: number | null;
  mr_target?: number | null;
  mr_exp_margin?: number | null;
  mr_exp_roi?: number | null;
  confidence?: number | null;
  margin_uptime?: number | null;
  margin_median_7d?: number | null;
  reasons?: string[];
  [k: string]: unknown;
}

export interface SeriesPoint {
  time: number;
  avg_high: number | null;
  avg_low: number | null;
  mid: number | null;
  ma: number | null;
  upper: number | null;
  lower: number | null;
  z: number | null;
  rsi: number | null;
  high_vol: number | null;
  low_vol: number | null;
}

export interface ProfilePoint {
  hour?: number;
  dow?: number;
  avg_dev: number | null;
  count: number;
}

export interface ItemDetail {
  item: { item_id: number; name: string; members: boolean | null; buy_limit: number | null; exempt: boolean; high_alch: number | null };
  current: Record<string, number | null>;
  stats: Record<string, number | null>;
  series: SeriesPoint[];
  hour_profile: ProfilePoint[];
  dow_profile: ProfilePoint[];
  signal_row?: Row;
}

export interface Meta {
  data_mode: "demo" | "live";
  coverage: { items: number; snapshot_rows: number; history_rows: number; snapshot_first: string | null; snapshot_last: string | null };
  tax: { rate: number; cap: number; min_price: number; exempt_count: number };
  defaults: { bankroll: number; min_volume: number; min_margin: number };
}

export interface Filters {
  bankroll: number;
  minVolume: number;
  minMargin: number;
  minRoi: number;
  minProfit: number;
  minPrice: number;
  zBuy: number;
  zSell: number;
}

function qs(f: Filters): string {
  return new URLSearchParams({
    bankroll: String(f.bankroll),
    min_volume: String(f.minVolume),
    min_margin: String(f.minMargin),
    min_roi: String(f.minRoi),
    min_profit: String(f.minProfit),
    min_price: String(f.minPrice),
    z_buy: String(f.zBuy),
    z_sell: String(f.zSell),
  }).toString();
}

async function get<T>(path: string): Promise<T> {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return (await r.json()) as T;
}

export const getMeta = () => get<Meta>("/api/meta");
export const getFlips = (f: Filters, limit = 250) => get<Row[]>(`/api/flips?${qs(f)}&limit=${limit}`);
export const getSignals = (f: Filters, limit = 250) => get<Row[]>(`/api/signals?${qs(f)}&limit=${limit}`);
export const getItems = (f: Filters) => get<Row[]>(`/api/items?${qs(f)}`);
export const getItem = (id: number, f: Filters) => get<ItemDetail>(`/api/item/${id}?${qs(f)}`);
export const getItemSeries = (id: number, timestep: string) =>
  get<{ timestep: string; series: SeriesPoint[] }>(`/api/item/${id}/series?timestep=${encodeURIComponent(timestep)}`);

// --- portfolio / trade tracker ---------------------------------------------
export interface OpenPosition {
  item_id: number;
  name: string;
  qty: number;
  avg_cost: number;
  cur_price: number | null;
  cur_net: number | null;
  cost_basis: number;
  market_value: number | null;
  unrealized: number | null;
  unrealized_pct: number | null;
}
export interface Trade {
  id: number;
  ts: string;
  item_id: number;
  name: string;
  side: string;
  qty: number;
  price: number;
  note: string;
}
export interface Portfolio {
  open_positions: OpenPosition[];
  trades: Trade[];
  realized_total: number;
  unrealized_total: number;
  invested: number;
  n_trades: number;
  n_open: number;
}
export interface ItemName {
  item_id: number;
  name: string;
}

export const getItemNames = () => get<ItemName[]>("/api/itemnames");
export const getPortfolio = () => get<Portfolio>("/api/portfolio");
export const addTrade = (t: { item_id: number; side: string; qty: number; price: number; note?: string }) =>
  fetch("/api/trades", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(t) }).then(
    (r) => {
      if (!r.ok) throw new Error(`add trade -> ${r.status}`);
      return r.json();
    }
  );
export const deleteTrade = (id: number) =>
  fetch(`/api/trades/${id}`, { method: "DELETE" }).then((r) => {
    if (!r.ok) throw new Error(`delete -> ${r.status}`);
    return r.json();
  });
