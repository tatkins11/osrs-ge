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
  realistic_profit?: number | null;
  units_per_4h?: number | null;
  sugg_units?: number | null;
  sugg_capital?: number | null;
  sugg_profit?: number | null;
  affordable?: boolean;
  high_vol?: number | null;
  low_vol?: number | null;
  vol_side?: number | null;
  vol_daily_7d?: number | null;
  vol_24h?: number | null;
  vol_ratio?: number | null;
  chg_24h?: number | null;
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
  established?: number | null;
  drawdown?: number | null;
  crash_target?: number | null;
  crash_exp_margin?: number | null;
  crash_exp_roi?: number | null;
  crash_exp_profit?: number | null;
  is_crash?: boolean;
  value_discount?: number | null;
  level_health?: number | null;
  value_target?: number | null;
  value_exp_margin?: number | null;
  value_exp_roi?: number | null;
  value_exp_profit?: number | null;
  value_confidence?: number | null;
  value_horizon?: string;
  is_value_buy?: boolean;
  post_update_drop?: boolean | null; // recent drop landed within ~2d of a game update (value-trap risk)
  post_update_title?: string | null; // title of the nearby update, for the tooltip
  alch_floor?: number | null;
  alch_support?: number | null; // buy price vs high-alch floor (fraction above; low = downside-protected)
  qty?: number | null;
  avg_cost?: number | null;
  unrealized?: number | null;
  unrealized_pct?: number | null;
  sell_ok?: boolean;
  on_buy?: number | null;
  on_target?: number | null;
  on_margin?: number | null;
  on_roi?: number | null;
  on_fill_prob?: number | null;
  on_win_rate?: number | null;
  on_exp_margin?: number | null;
  on_nights?: number | null;
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
  changes?: Changes;
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
  maxPrice: number;
  minConfidence: number;
  minDiscount: number;
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
    max_price: String(f.maxPrice),
    value_min_confidence: String(f.minConfidence),
    value_min_discount: String(f.minDiscount),
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
export const getCrashes = (f: Filters, limit = 200) => get<Row[]>(`/api/crashes?${qs(f)}&limit=${limit}`);
export const getVolume = (f: Filters, limit = 200) => get<Row[]>(`/api/volume?${qs(f)}&limit=${limit}`);
export const getOvernight = (f: Filters, limit = 150) => get<Row[]>(`/api/overnight?${qs(f)}&limit=${limit}`);

export interface InvestResponse {
  buys: Row[];
  sells: Row[];
}
export const getInvest = (f: Filters, limit = 150) => get<InvestResponse>(`/api/invest?${qs(f)}&limit=${limit}`);
export const getItem = (id: number, f: Filters) => get<ItemDetail>(`/api/item/${id}?${qs(f)}`);
export const getItemSeries = (id: number, timestep: string) =>
  get<{ timestep: string; series: SeriesPoint[] }>(`/api/item/${id}/series?timestep=${encodeURIComponent(timestep)}`);

// --- multi-horizon % changes (fractions, e.g. -0.05 = -5%) -----------------
export interface Changes {
  "1d": number | null;
  "1w": number | null;
  "2w": number | null;
  "1mo": number | null;
  "3mo": number | null;
  "1y": number | null;
}
export const HORIZON_KEYS: (keyof Changes)[] = ["1d", "1w", "2w", "1mo", "3mo", "1y"];

// --- sectors / ETF tracker -------------------------------------------------
export interface SectorMover {
  item_id: number;
  name: string;
  dev: number | null; // fraction vs 7d baseline
}
export interface SectorCard {
  key: string;
  label: string;
  blurb: string;
  n_items: number;
  gp_vol: number;
  dev: number | null; // weighted fraction vs 7d baseline (cheap/expensive)
  changes: Changes;
  spark: number[];
  top_up: SectorMover[];
  top_down: SectorMover[];
}
export interface SectorsResponse {
  sectors: SectorCard[];
  coverage: { classified: number; liquid: number };
}
export interface SectorConstituent {
  item_id: number;
  name: string;
  mid: number | null;
  established: number | null;
  dev: number | null;
  gp_vol: number;
  weight_pct: number | null;
}
export interface SectorIndexPoint {
  time: number;
  index: number; // percentage-points, anchored at 0 at window start
}
export interface SectorDetail {
  key: string;
  label: string;
  blurb: string;
  timeframe: string;
  series: SectorIndexPoint[];
  changes: Changes;
  constituents: SectorConstituent[];
}

export const getSectors = (f: Filters) => get<SectorsResponse>(`/api/sectors?${qs(f)}`);
export const getSectorDetail = (key: string, f: Filters, timeframe = "2wk") =>
  get<SectorDetail>(`/api/sector/${encodeURIComponent(key)}?${qs(f)}&timeframe=${encodeURIComponent(timeframe)}`);

// --- portfolio / trade tracker ---------------------------------------------
export interface OpenPosition {
  item_id: number;
  name: string;
  qty: number;
  avg_cost: number;
  breakeven: number | null;
  cur_price: number | null;
  cur_net: number | null;
  cost_basis: number;
  market_value: number | null;
  unrealized: number | null;
  unrealized_pct: number | null;
  target: number | null;      // 7d established fair-value sell target
  target_net: number | null;
  to_target: number | null;   // upside from current price to fair value (fraction)
  alch_floor: number | null;
  sector: string | null;
  status: string;             // sell | hold | underwater | no price
}
export interface SectorExposure {
  sector: string;
  label: string;
  capital: number;
  pct: number;
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
export interface TradePrefill {
  item_id: number;
  name: string;
  side: "buy" | "sell";
  price: number;
}
export interface ClosedTrip {
  item_id: number;
  name: string;
  qty: number;
  buy_avg: number;
  sell_price: number;
  gross: number;
  tax: number;
  net: number;
  roi: number | null;
  buy_ts: string;
  sell_ts: string;
  hold_days: number;
  sector: string | null;
}
export interface PortfolioStats {
  n_closed: number;
  win_rate: number | null;
  avg_win: number | null;
  avg_loss: number | null;
  best: number | null;
  worst: number | null;
  total_tax: number;
  avg_hold_days: number | null;
  realized_total: number;
}
export interface Portfolio {
  open_positions: OpenPosition[];
  trades: Trade[];
  closed_trips: ClosedTrip[];
  stats: PortfolioStats;
  realized_by_item: { item_id: number; name: string; net: number }[];
  equity_curve: { ts: string; cum: number }[];
  realized_total: number;
  unrealized_total: number;
  invested: number;
  n_trades: number;
  n_open: number;
  sector_exposure: SectorExposure[];
  n_alerts: number;
}
export interface ItemName {
  item_id: number;
  name: string;
}

export interface GameUpdate {
  ts: string;
  title: string;
  url: string;
}
export const getUpdates = () => get<GameUpdate[]>("/api/updates");

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
export const updateTrade = (id: number, patch: { qty?: number; price?: number; note?: string; side?: string }) =>
  fetch(`/api/trades/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch) }).then(
    (r) => {
      if (!r.ok) throw new Error(`update -> ${r.status}`);
      return r.json();
    }
  );
