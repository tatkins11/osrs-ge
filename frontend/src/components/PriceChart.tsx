import { useEffect, useRef } from "react";
import { ColorType, createChart, type UTCTimestamp } from "lightweight-charts";
import type { SeriesPoint } from "../api";

// Render axis + crosshair in the viewer's LOCAL timezone (lightweight-charts is UTC by default).
const fmtFull = (t: number) =>
  new Date(t * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
const tickFmt = (time: unknown, type: number) => {
  const d = new Date((time as number) * 1000);
  if (type >= 3) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (type === 2) return d.toLocaleDateString([], { month: "short", day: "numeric" });
  if (type === 1) return d.toLocaleDateString([], { month: "short", year: "numeric" });
  return String(d.getFullYear());
};
const gpf = (n: number | null | undefined) => (n == null ? "–" : Math.round(n).toLocaleString());

/** Price history with a 7d/short moving average, Bollinger bands and volume.
 *  type="line": mid-price line. type="candle": OHLC candles (avg_high/avg_low wicks, mid body). */
export function PriceChart({
  series,
  type = "line",
  className = "",
  levels = [],
  markers = [],
  events = [],
  fairValue,
}: {
  series: SeriesPoint[];
  type?: "line" | "candle";
  className?: string;
  levels?: { price: number; color: string; title: string; dashed?: boolean }[];
  markers?: { time: number; side: string }[];
  events?: { time: number; title: string }[];
  fairValue?: number;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ref.current || !series?.length) return;
    const el = ref.current;
    const chart = createChart(el, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "#9fb0c3",
        fontFamily: "ui-monospace, monospace",
        fontSize: 11,
      },
      grid: { vertLines: { color: "#15202d" }, horzLines: { color: "#15202d" } },
      rightPriceScale: { borderColor: "#1e2a39", scaleMargins: { top: 0.06, bottom: 0.30 } },
      timeScale: { borderColor: "#1e2a39", timeVisible: true, secondsVisible: false, tickMarkFormatter: tickFmt },
      localization: { timeFormatter: fmtFull },
      crosshair: { mode: 0 },
    });

    const t = (p: SeriesPoint) => p.time as UTCTimestamp;
    const sel = (key: keyof SeriesPoint) =>
      series.filter((p) => p[key] != null).map((p) => ({ time: t(p), value: p[key] as number }));

    const band = (color: string) =>
      chart.addLineSeries({ color, lineWidth: 1, lineStyle: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    band("rgba(123,211,252,.35)").setData(sel("upper"));
    band("rgba(123,211,252,.35)").setData(sel("lower"));
    chart.addLineSeries({ color: "#f5b53d", lineWidth: 1, priceLineVisible: false, lastValueVisible: false }).setData(sel("ma"));

    const vol = chart.addHistogramSeries({ priceScaleId: "vol", color: "rgba(78,161,255,.30)", lastValueVisible: false, priceLineVisible: false });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.88, bottom: 0 } });
    vol.setData(
      series
        .filter((p) => p.high_vol != null || p.low_vol != null)
        .map((p) => ({ time: t(p), value: (p.high_vol ?? 0) + (p.low_vol ?? 0) }))
    );

    // z-score oscillator sub-pane: how stretched vs the 7d mean, with buy/sell guides
    const osc = chart.addLineSeries({ priceScaleId: "osc", color: "#9b8cff", lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    chart.priceScale("osc").applyOptions({ scaleMargins: { top: 0.72, bottom: 0.14 } });
    osc.setData(sel("z"));
    osc.createPriceLine({ price: 1.5, color: "rgba(255,91,110,.45)", lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: "z +1.5" });
    osc.createPriceLine({ price: 0, color: "rgba(120,138,160,.4)", lineWidth: 1, lineStyle: 2, axisLabelVisible: false });
    osc.createPriceLine({ price: -1.5, color: "rgba(37,208,125,.45)", lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: "z -1.5" });

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    let main: any;
    if (type === "candle") {
      main = chart.addCandlestickSeries({
        upColor: "#25d07d", downColor: "#ff5b6e",
        borderUpColor: "#25d07d", borderDownColor: "#ff5b6e",
        wickUpColor: "#25d07d", wickDownColor: "#ff5b6e",
      });
      const data: { time: UTCTimestamp; open: number; high: number; low: number; close: number }[] = [];
      let prev: number | null = null;
      for (const p of series) {
        if (p.mid == null) continue;
        const open = prev ?? p.mid;
        const close = p.mid;
        const high = Math.max(p.avg_high ?? close, open, close);
        const low = Math.min(p.avg_low ?? close, open, close);
        data.push({ time: t(p), open, high, low, close });
        prev = close;
      }
      main.setData(data);
    } else {
      main = chart.addLineSeries({ color: "#4ea1ff", lineWidth: 2, priceLineVisible: false });
      main.setData(sel("mid"));
    }

    // decision levels (fair value / target / alch floor / your cost+breakeven) + your trades
    for (const lv of levels) {
      if (lv.price > 0)
        main.createPriceLine({ price: lv.price, color: lv.color, lineWidth: 1, lineStyle: lv.dashed ? 2 : 0, axisLabelVisible: true, title: lv.title });
    }
    // trade (B/S) + update (📰) markers, plus a bar -> update-title map for the tooltip
    const eventByBar = new Map<number, string>();
    if (series.length) {
      const lo = series[0].time as number;
      const hi = series[series.length - 1].time as number;
      const times = series.map((p) => p.time as number);
      const nearest = (e: number) => times.reduce((best, tt) => (Math.abs(tt - e) < Math.abs(best - e) ? tt : best), times[0]);
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const all: any[] = [];
      for (const m of markers) {
        if (m.time < lo || m.time > hi) continue;
        all.push({ time: m.time as UTCTimestamp, position: m.side === "buy" ? "belowBar" : "aboveBar",
          color: m.side === "buy" ? "#25d07d" : "#ff5b6e", shape: m.side === "buy" ? "arrowUp" : "arrowDown", text: m.side === "buy" ? "B" : "S" });
      }
      for (const ev of events) {
        if (ev.time < lo - 86400 || ev.time > hi + 86400) continue;
        const bt = nearest(ev.time);
        all.push({ time: bt as UTCTimestamp, position: "aboveBar", color: "#f5b53d", shape: "square", text: "📰" });
        eventByBar.set(bt, eventByBar.has(bt) ? `${eventByBar.get(bt)} · ${ev.title}` : ev.title);
      }
      if (all.length) {
        all.sort((a, b) => (a.time as number) - (b.time as number));
        main.setMarkers(all);
      }
    }

    // hover info box
    const tip = document.createElement("div");
    tip.className = "chart-tip";
    tip.style.display = "none";
    el.appendChild(tip);
    const byTime = new Map(series.map((p) => [p.time as number, p]));
    chart.subscribeCrosshairMove((param) => {
      const pt = param.point;
      if (!param.time || !pt || pt.x < 0 || pt.y < 0 || pt.x > el.clientWidth || pt.y > el.clientHeight) {
        tip.style.display = "none";
        return;
      }
      const md = param.seriesData.get(main) as unknown as
        | { open?: number; high?: number; low?: number; close?: number; value?: number }
        | undefined;
      if (!md) {
        tip.style.display = "none";
        return;
      }
      const vd = param.seriesData.get(vol) as { value?: number } | undefined;
      const body =
        type === "candle"
          ? `O ${gpf(md.open)} · H ${gpf(md.high)} · L ${gpf(md.low)} · C ${gpf(md.close)}`
          : `Price ${gpf(md.value)}`;
      const volTxt = vd?.value != null ? ` · Vol ${gpf(vd.value)}` : "";
      const px = type === "candle" ? md.close : md.value;
      const vf =
        fairValue && fairValue > 0 && px != null
          ? `<div class="tip-v ${px >= fairValue ? "neg" : "pos"}">${px >= fairValue ? "+" : ""}${(((px / fairValue) - 1) * 100).toFixed(1)}% vs fair value</div>`
          : "";
      const sp = byTime.get(param.time as number);
      const zr =
        sp && sp.z != null
          ? `<div class="tip-v dim">z ${sp.z.toFixed(1)}${sp.rsi != null ? ` · RSI ${Math.round(sp.rsi)}` : ""}</div>`
          : "";
      const evt = eventByBar.get(param.time as number);
      const evTxt = evt ? `<div class="tip-v" style="color:#f5b53d">📰 ${evt}</div>` : "";
      tip.innerHTML = `<div class="tip-t">${fmtFull(param.time as number)}</div><div class="tip-v">${body}${volTxt}</div>${vf}${zr}${evTxt}`;
      tip.style.display = "block";
      tip.style.left = Math.min(pt.x + 14, el.clientWidth - 190) + "px";
      tip.style.top = Math.max(6, pt.y - 12) + "px";
    });

    chart.timeScale().fitContent();
    return () => {
      chart.remove();
      tip.remove();
    };
  }, [series, type, levels, markers, events, fairValue]);

  return <div className={`chart-box ${className}`} ref={ref} />;
}
