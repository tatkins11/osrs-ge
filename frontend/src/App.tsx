import { useEffect, useMemo, useRef, useState } from "react";
import { getCrashes, getFlips, getInvest, getItems, getMeta, getOvernight, getSectors, getVolume, type Filters, type InvestResponse, type Meta, type Row, type SectorsResponse } from "./api";
import { gpShort } from "./format";
import { Controls } from "./components/Controls";
import { CrashTable } from "./components/CrashTable";
import { InvestTable } from "./components/InvestTable";
import { ItemPanel } from "./components/ItemPanel";
import { MarketTable } from "./components/MarketTable";
import { OvernightTable } from "./components/OvernightTable";
import { Portfolio } from "./components/Portfolio";
import { SectorGrid } from "./components/SectorGrid";
import { SectorPanel } from "./components/SectorPanel";
import { VolumeTable } from "./components/VolumeTable";

type Tab = "flips" | "invest" | "crashes" | "movers" | "overnight" | "sectors" | "all" | "portfolio";

const DEFAULT_FILTERS: Filters = {
  bankroll: 250_000_000,
  minVolume: 100,
  minMargin: 1,
  minRoi: 0.004,
  minProfit: 500_000,
  minPrice: 1_000,
  maxPrice: 2_147_483_647,
  minConfidence: 40,
  minDiscount: 0.08,
  zBuy: -1.5,
  zSell: 1.5,
};

const TABS: { id: Tab; label: string }[] = [
  { id: "flips", label: "Flips" },
  { id: "invest", label: "Invest" },
  { id: "crashes", label: "Crashes" },
  { id: "movers", label: "Movers" },
  { id: "overnight", label: "Overnight" },
  { id: "sectors", label: "Sectors" },
  { id: "all", label: "All items" },
  { id: "portfolio", label: "Portfolio" },
];

const REFRESH_MS = 60_000;

export default function App() {
  const [meta, setMeta] = useState<Meta | null>(null);
  const [tab, setTab] = useState<Tab>("flips");
  const [filters, setFilters] = useState<Filters>(DEFAULT_FILTERS);
  const [rows, setRows] = useState<Row[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [search, setSearch] = useState("");
  const [nonce, setNonce] = useState(0);
  const [auto, setAuto] = useState(true);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const [nowTick, setNowTick] = useState(() => Date.now());
  const [sectorsData, setSectorsData] = useState<SectorsResponse | null>(null);
  const [investData, setInvestData] = useState<InvestResponse | null>(null);
  const [selectedSector, setSelectedSector] = useState<string | null>(null);

  useEffect(() => {
    getMeta().then(setMeta).catch(() => {});
  }, [nonce]);

  // clear stale rows + error on tab switch so one tab's data never renders under another's columns
  useEffect(() => {
    setRows([]);
    setErr(null);
  }, [tab]);

  const deb = useRef<number | undefined>(undefined);
  useEffect(() => {
    if (tab === "portfolio") return;
    window.clearTimeout(deb.current);
    let cancelled = false; // ignore a response that arrives after the tab/filters changed (race guard)
    deb.current = window.setTimeout(() => {
      setLoading(true);
      setErr(null);
      const ok = () => !cancelled;
      const done = () => ok() && setLoading(false);
      const fail = (e: unknown) => ok() && setErr(String(e));
      if (tab === "sectors") {
        getSectors(filters)
          .then((d) => ok() && (setSectorsData(d), setUpdatedAt(Date.now())))
          .catch(fail)
          .finally(done);
        return;
      }
      if (tab === "invest") {
        getInvest(filters)
          .then((d) => ok() && (setInvestData(d), setUpdatedAt(Date.now())))
          .catch(fail)
          .finally(done);
        return;
      }
      const req =
        tab === "flips" ? getFlips(filters)
        : tab === "crashes" ? getCrashes(filters)
        : tab === "movers" ? getVolume(filters)
        : tab === "overnight" ? getOvernight(filters)
        : getItems(filters);
      req
        .then((r) => ok() && (setRows(r), setUpdatedAt(Date.now())))
        .catch(fail)
        .finally(done);
    }, 250);
    return () => {
      cancelled = true;
      window.clearTimeout(deb.current);
    };
  }, [tab, filters, nonce]);

  useEffect(() => {
    if (!auto) return;
    const id = window.setInterval(() => setNonce((n) => n + 1), REFRESH_MS);
    return () => window.clearInterval(id);
  }, [auto]);

  useEffect(() => {
    const id = window.setInterval(() => setNowTick(Date.now()), 10_000); // keep "updated ago" fresh
    return () => window.clearInterval(id);
  }, []);

  const shown = useMemo(() => {
    const q = search.trim().toLowerCase();
    return q ? rows.filter((r) => r.name?.toLowerCase().includes(q)) : rows;
  }, [rows, search]);

  const defaultSort = tab === "all" ? [{ id: "profit_per_cycle", desc: true }] : [];
  const ago = (t: number) => {
    const s = Math.max(0, Math.round((nowTick - t) / 1000));
    return s < 60 ? `${s}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : `${Math.round(s / 3600)}h ago`;
  };

  return (
    <div className="app">
      <header className="header">
        <div className="brand">
          <span className="mark">◆</span> GE TERMINAL <span className="ver">v0.1</span>
        </div>
        {meta && <span className={`pill ${meta.data_mode}`}>{meta.data_mode === "demo" ? "DEMO DATA" : "LIVE"}</span>}
        <div className="spacer" />
        {meta && (
          <>
            <div className="hstat">
              <span className="k">Items</span>
              <span className="v">{meta.coverage.items.toLocaleString()}</span>
            </div>
            <div className="hstat">
              <span className="k">Tax</span>
              <span className="v">{(meta.tax.rate * 100).toFixed(0)}% · max {gpShort(meta.tax.cap)}/item</span>
            </div>
            <div className="hstat">
              <span className="k">History rows</span>
              <span className="v">{meta.coverage.history_rows.toLocaleString()}</span>
            </div>
          </>
        )}
      </header>

      <div className="toolbar">
        <div className="tabs">
          {TABS.map((t) => (
            <button key={t.id} className={`tab ${tab === t.id ? "active" : ""}`} onClick={() => setTab(t.id)}>
              {t.label}
            </button>
          ))}
        </div>
        <Controls filters={filters} setFilters={setFilters} />
        <div className="ctrl search">
          <label>Search</label>
          <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="item name…" />
        </div>
        <div className="spacer" />
        <span className="note">
          {loading ? "loading…" : tab === "sectors" ? `${sectorsData?.sectors.length ?? 0} sectors` : tab === "invest" ? `${investData?.buys.length ?? 0} buys` : `${shown.length} rows`}
          {updatedAt ? ` · updated ${ago(updatedAt)}` : ""}
          {err ? ` · error: ${err}` : ""}
        </span>
        <label className="autobox" title={`auto-refresh every ${REFRESH_MS / 1000}s`}>
          <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /> auto
        </label>
        <button className="refresh" onClick={() => setNonce((n) => n + 1)} title="Refresh now">↻</button>
      </div>

      <div className="main">
        {tab === "portfolio" ? (
          <Portfolio refreshNonce={nonce} />
        ) : tab === "sectors" ? (
          <>
            <div className="table-wrap">
              <SectorGrid
                data={sectorsData}
                selectedKey={selectedSector}
                onSelect={(k) => {
                  setSelectedSector(k);
                  setSelected(null);
                }}
              />
            </div>
            <div className={`panel-wrap ${selected != null || selectedSector ? "open" : ""}`}>
              {selected != null ? (
                <ItemPanel itemId={selected} filters={filters} refreshNonce={nonce} onClose={() => setSelected(null)} />
              ) : selectedSector ? (
                <SectorPanel
                  sectorKey={selectedSector}
                  filters={filters}
                  refreshNonce={nonce}
                  onSelectItem={setSelected}
                  onClose={() => setSelectedSector(null)}
                />
              ) : null}
            </div>
          </>
        ) : tab === "invest" ? (
          <>
            <div className="table-wrap">
              <div className="invest-banner">
                Value buys — undervalued vs each item's established fair value, ranked by a 0–100 confidence (discount ·
                how-unusual · cheapness · liquidity · level-health). Higher confidence + shorter horizon = stronger edge;
                long holds are more speculative. Buy near "Buy at", sell near "Fair value". Log trades in <b>Portfolio</b>
                to get sell signals on what you hold.
              </div>
              <InvestTable
                buys={investData?.buys ?? []}
                sells={investData?.sells ?? []}
                selectedId={selected}
                onSelect={setSelected}
              />
            </div>
            <div className={`panel-wrap ${selected != null ? "open" : ""}`}>
              <ItemPanel itemId={selected} filters={filters} refreshNonce={nonce} onClose={() => setSelected(null)} />
            </div>
          </>
        ) : (
          <>
            <div className="table-wrap">
              {tab === "crashes" && (
                <div className="crash-banner">
                  Crash-&-recover: items ≥18% below their 7-day established level. Backtested ~59% win / profit factor ~2
                  even paying the full spread — modest but real, and it'll firm up as more data accrues. Buy near
                  "Buy now", place a sell offer near "Target".
                </div>
              )}
              {tab === "movers" && (
                <div className="exp-banner">
                  Unusual volume — items trading well above their normal daily volume (news, a meta shift, or
                  manipulation). A watchlist of what's <b>in play, not a buy signal</b>: backtested, a volume spike
                  alone doesn't predict a tradeable move (~0% forward, negative after spread + 2% tax). Use it to spot
                  activity, then open the item; act via <b>Crashes</b> / <b>Invest</b>.
                </div>
              )}
              {tab === "overnight" && (
                <div className="crash-banner">
                  Overnight lowball offers — place these buy offers (at "Buy offer") before you log off; each fills only
                  if the price dumps overnight, then you sell next day toward "Sell target". Now ranked by real{" "}
                  <b>Fill chance</b> (how often it's actually filled by morning over the last ~2 weeks) and{" "}
                  <b>Win rate</b> when filled. Fills are inherently infrequent — place several, and even a 50%
                  fill-chance item only fills about half the nights. A filled order can sit underwater until it reverts.
                </div>
              )}
              {tab === "crashes" ? (
                <CrashTable rows={shown} selectedId={selected} onSelect={setSelected} />
              ) : tab === "movers" ? (
                <VolumeTable rows={shown} selectedId={selected} onSelect={setSelected} />
              ) : tab === "overnight" ? (
                <OvernightTable rows={shown} selectedId={selected} onSelect={setSelected} />
              ) : (
                <MarketTable key={tab} rows={shown} selectedId={selected} onSelect={setSelected} defaultSort={defaultSort} />
              )}
            </div>
            <div className={`panel-wrap ${selected != null ? "open" : ""}`}>
              <ItemPanel itemId={selected} filters={filters} refreshNonce={nonce} onClose={() => setSelected(null)} />
            </div>
          </>
        )}
      </div>
    </div>
  );
}
