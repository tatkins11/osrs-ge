import { useEffect, useRef } from "react";
import { ColorType, createChart, type UTCTimestamp } from "lightweight-charts";
import type { SeriesPoint } from "../api";

/** TradingView-style price history: mid price, 7d moving average, Bollinger
 *  bands (dashed) and a traded-volume histogram. */
export function PriceChart({ series }: { series: SeriesPoint[] }) {
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
    const band = (color: string) =>
      chart.addLineSeries({ color, lineWidth: 1, lineStyle: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });

    const upper = band("rgba(123,211,252,.35)");
    const lower = band("rgba(123,211,252,.35)");
    const ma = chart.addLineSeries({ color: "#f5b53d", lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
    const mid = chart.addLineSeries({ color: "#4ea1ff", lineWidth: 2, priceLineVisible: false });
    const vol = chart.addHistogramSeries({ priceScaleId: "vol", color: "rgba(78,161,255,.30)" });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.84, bottom: 0 } });

    const sel = (key: keyof SeriesPoint) =>
      series.filter((p) => p[key] != null).map((p) => ({ time: t(p), value: p[key] as number }));

    mid.setData(sel("mid"));
    ma.setData(sel("ma"));
    upper.setData(sel("upper"));
    lower.setData(sel("lower"));
    vol.setData(
      series
        .filter((p) => p.high_vol != null || p.low_vol != null)
        .map((p) => ({ time: t(p), value: (p.high_vol ?? 0) + (p.low_vol ?? 0) }))
    );
    chart.timeScale().fitContent();

    return () => chart.remove();
  }, [series]);

  return <div className="chart-box" ref={ref} />;
}
