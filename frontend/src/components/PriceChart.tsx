import { useEffect, useRef } from "react";
import { ColorType, createChart, type UTCTimestamp } from "lightweight-charts";
import type { SeriesPoint } from "../api";

/** Price history with a 7d/short moving average, Bollinger bands and volume.
 *  type="line": mid-price line. type="candle": OHLC candles where each period's
 *  insta-buy (avg_high) and insta-sell (avg_low) form the wick range and the mid
 *  open->close forms the body. */
export function PriceChart({
  series,
  type = "line",
  className = "",
}: {
  series: SeriesPoint[];
  type?: "line" | "candle";
  className?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ref.current || !series?.length) return;
    const chart = createChart(ref.current, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "#9fb0c3",
        fontFamily: "ui-monospace, monospace",
        fontSize: 11,
      },
      grid: { vertLines: { color: "#15202d" }, horzLines: { color: "#15202d" } },
      rightPriceScale: { borderColor: "#1e2a39" },
      timeScale: { borderColor: "#1e2a39", timeVisible: true, secondsVisible: false },
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

    const vol = chart.addHistogramSeries({ priceScaleId: "vol", color: "rgba(78,161,255,.30)" });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.84, bottom: 0 } });
    vol.setData(
      series
        .filter((p) => p.high_vol != null || p.low_vol != null)
        .map((p) => ({ time: t(p), value: (p.high_vol ?? 0) + (p.low_vol ?? 0) }))
    );

    if (type === "candle") {
      const candle = chart.addCandlestickSeries({
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
      candle.setData(data);
    } else {
      chart.addLineSeries({ color: "#4ea1ff", lineWidth: 2, priceLineVisible: false }).setData(sel("mid"));
    }

    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [series, type]);

  return <div className={`chart-box ${className}`} ref={ref} />;
}
