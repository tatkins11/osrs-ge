import { useEffect, useState, type ReactNode } from "react";
import { getItem, getItemSeries, HORIZON_KEYS, type Filters, type ItemDetail, type Row, type SeriesPoint } from "../api";
import { fixed, gp, gpShort, num, pct, spct } from "../format";
import { ChartModal } from "./ChartModal";
import { PriceChart } from "./PriceChart";
import { ProfileBars } from "./ProfileBars";
import { SignalBadge } from "./SignalBadge";

const DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function Tile({ k, v, cls = "" }: { k: string; v: ReactNode; cls?: string }) {
  return (
    <div className="tile">
      <div className="k">{k}</div>
      <div className={`v ${cls}`}>{v}</div>
    </div>
  );
}

export function ItemPanel({
  itemId,
  filters,
  refreshNonce = 0,
  onClose,
}: {
  itemId: number | null;
  filters: Filters;
  refreshNonce?: number;
  onClose: () => void;
}) {
  const [data, setData] = useState<ItemDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [tf, setTf] = useState("1h");
  const [tfSeries, setTfSeries] = useState<SeriesPoint[] | null>(null);
  const [chartType, setChartType] = useState<"line" | "candle">("line");
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    if (itemId == null) {
      setData(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    getItem(itemId, filters)
      .then((d) => !cancelled && setData(d))
      .catch(() => !cancelled && setData(null))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [itemId, filters, refreshNonce]);

  useEffect(() => {
    setTf("1h");
    setTfSeries(null);
  }, [itemId]);

  useEffect(() => {
    if (itemId == null || tf === "1h") return;
    let cancelled = false;
    getItemSeries(itemId, tf)
      .then((r) => !cancelled && setTfSeries(r.series))
      .catch(() => !cancelled && setTfSeries([]));
    return () => {
      cancelled = true;
    };
  }, [itemId, tf]);

  if (itemId == null)
    return (
      <div className="placeholder">
        Select an item to deep-dive: price history with Bollinger bands, hour-of-day &amp; weekday seasonality, statistics and a position-sized signal.
      </div>
    );
  if (loading && !data) return <div className="placeholder">Loading…</div>;
  if (!data) return <div className="placeholder">No data for this item.</div>;

  const c = data.current;
  const st = data.stats;
  const sr: Partial<Row> = data.signal_row ?? {};

  return (
    <div>
      <div className="panel-head">
        <span className="close" onClick={onClose}>×</span>
        <div className="title">{data.item.name}</div>
        <div className="sub">
          #{data.item.item_id} · {data.item.members ? "Members" : "F2P"} · limit {num(data.item.buy_limit)}/4h
          {data.item.exempt ? " · tax-exempt" : ""}
        </div>
        <div style={{ marginTop: 8 }}>
          <SignalBadge signal={sr.signal as string} />
        </div>
      </div>

      {sr.signal && sr.signal !== "HOLD" && sr.signal !== "ILLIQUID" && (
        <div className="panel-section thesis">
          <h4>Trade thesis</h4>
          {Array.isArray(sr.reasons) && (sr.reasons as string[]).length > 0 && (
            <ul className="reasons">
              {(sr.reasons as string[]).map((x, i) => (
                <li key={i}>{x}</li>
              ))}
            </ul>
          )}
          {sr.signal === "FLIP" ? (
            <div className="tiles">
              <Tile k="Buy at" v={gp(sr.buy_price as number)} />
              <Tile k="Sell at" v={gp(sr.sell_price as number)} />
              <Tile k="Net / ea" v={gp(sr.net_margin as number)} cls="pos" />
              <Tile k="ROI" v={pct(sr.roi as number, 2)} cls="pos" />
              <Tile k="Margin uptime" v={pct(sr.margin_uptime as number, 0)} />
              <Tile k="Profit / 4h" v={gpShort(sr.profit_per_cycle as number)} cls="pos" />
            </div>
          ) : (
            <>
              <div className="tiles">
                <Tile k={String(sr.signal).includes("BUY") ? "Buy near" : "Sell near"} v={gp(sr.mr_entry as number)} />
                <Tile k="Fair value" v={gp(sr.mr_target as number)} />
                <Tile k="Exp. profit/ea" v={gp(sr.mr_exp_margin as number)} cls={((sr.mr_exp_margin as number) ?? 0) > 0 ? "pos" : "neg"} />
                <Tile k="Exp. ROI" v={pct(sr.mr_exp_roi as number, 1)} />
                <Tile k="Confidence" v={sr.confidence != null ? String(sr.confidence) : "–"} />
                <Tile k="Z-score" v={fixed(sr.z_7d as number, 2)} />
              </div>
              <div className="note" style={{ marginTop: 8 }}>
                ⚠ Experimental — mean-reversion isn't validated yet (backtests negative on current data). Informational only.
              </div>
            </>
          )}
        </div>
      )}

      <div className="panel-section">
        <h4>Now · after 2% tax</h4>
        <div className="tiles">
          <Tile k="Insta-buy" v={gp(c.instabuy)} />
          <Tile k="Insta-sell" v={gp(c.instasell)} />
          <Tile k="Spread" v={gp(c.gross_margin)} />
          <Tile k="Net margin" v={gp(c.net_margin)} cls={(c.net_margin ?? 0) > 0 ? "pos" : "neg"} />
          <Tile k="ROI" v={pct(c.roi, 2)} cls={(c.roi ?? 0) > 0 ? "pos" : "neg"} />
          <Tile k="Tax/ea" v={gp(c.tax)} />
        </div>
      </div>

      {data.changes && (
        <div className="panel-section">
          <h4>Price change</h4>
          <div className="tiles changes">
            {HORIZON_KEYS.map((k) => (
              <Tile
                key={k}
                k={k}
                v={spct(data.changes![k])}
                cls={(data.changes![k] ?? 0) > 0 ? "pos" : (data.changes![k] ?? 0) < 0 ? "neg" : ""}
              />
            ))}
          </div>
        </div>
      )}

      <div className="panel-section">
        <div className="tf-row">
          <h4>Price history · MA · Bollinger · volume</h4>
          <div style={{ display: "flex", gap: 8 }}>
            <div className="tf-toggle">
              {([["line", "Line"], ["candle", "Candles"]] as const).map(([v, l]) => (
                <button key={v} className={`tf ${chartType === v ? "active" : ""}`} onClick={() => setChartType(v)}>
                  {l}
                </button>
              ))}
            </div>
            <div className="tf-toggle">
              {([["1h", "2wk"], ["6h", "3mo"], ["24h", "1yr"]] as const).map(([v, l]) => (
                <button key={v} className={`tf ${tf === v ? "active" : ""}`} onClick={() => setTf(v)}>
                  {l}
                </button>
              ))}
            </div>
            <button className="expand" title="Expand chart" onClick={() => setExpanded(true)}>⤢</button>
          </div>
        </div>
        <PriceChart series={tf === "1h" ? data.series : tfSeries ?? []} type={chartType} />
      </div>

      <div className="panel-section">
        <h4>Statistics</h4>
        <div className="tiles">
          <Tile k="Z-score 7d" v={fixed(st.z_7d, 2)} cls={(st.z_7d ?? 0) < 0 ? "pos" : "neg"} />
          <Tile k="RSI 14" v={fixed(st.rsi, 0)} />
          <Tile k="Volatility" v={pct(st.volatility_7d, 1)} />
          <Tile k="7d mean" v={gpShort(st.mean_7d)} />
          <Tile k="30d low" v={gpShort(st.min_30d)} />
          <Tile k="30d high" v={gpShort(st.max_30d)} />
        </div>
      </div>

      <div className="panel-section">
        <h4>Position sizing · bankroll {gpShort(filters.bankroll)}</h4>
        <div className="tiles">
          <Tile k="Units" v={num(sr.sugg_units as number)} />
          <Tile k="Capital" v={gpShort(sr.sugg_capital as number)} />
          <Tile k="Est. profit" v={gpShort(sr.sugg_profit as number)} cls="pos" />
        </div>
        {sr.affordable === false && (
          <div className="note" style={{ marginTop: 8 }}>⚠ One buy-limit cycle exceeds your per-position cap / bankroll.</div>
        )}
      </div>

      <div className="panel-section">
        <h4>Hour-of-day seasonality (UTC) · green = cheap, red = expensive</h4>
        <ProfileBars rows={data.hour_profile.map((p) => ({ label: `${String(p.hour).padStart(2, "0")}:00`, dev: p.avg_dev }))} />
      </div>

      <div className="panel-section">
        <h4>Day-of-week seasonality</h4>
        <ProfileBars rows={data.dow_profile.map((p) => ({ label: DOW[p.dow ?? 0], dev: p.avg_dev }))} />
      </div>

      {expanded && (
        <ChartModal
          title={`${data.item.name} · price (${tf === "1h" ? "2wk" : tf === "6h" ? "3mo" : "1yr"})`}
          onClose={() => setExpanded(false)}
        >
          <PriceChart series={tf === "1h" ? data.series : tfSeries ?? []} type={chartType} className="modal-chart" />
        </ChartModal>
      )}
    </div>
  );
}
