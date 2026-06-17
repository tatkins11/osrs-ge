import { useEffect, useMemo, useRef, useState } from "react";
import { getCrashes, getFlips, getItems, getMeta, getSectors, getSignals, type Filters, type Meta, type Row, type SectorsResponse } from "./api";
import { gpShort } from "./format";
import { Controls } from "./components/Controls";
import { CrashTable } from "./components/CrashTable";
import { ItemPanel } from "./components/ItemPanel";
import { MarketTable } from "./components/MarketTable";
import { Portfolio } from "./components/Portfolio";
import { SectorGrid } from "./components/SectorGrid";
import { SectorPanel } from "./components/SectorPanel";

type Tab = "flips" | "signals" | "crashes" | "sectors" | "all" | "portfolio";

const DEFAULT_FILTERS: Filters = {
  bankroll: 250_000_000,
  minVolume: 100,
  minMargin: 1,
  minRoi: 0.004,
  minProfit: 500_000,
  minPrice: 1_000,
  maxPrice: 2_147_483_647,
  zBuy: -1.5,
  zSell: 1.5,
};

const TABS: { id: Tab; label: string }[] = [
  { id: "flips", label: "Flips" },
  { id: "signals", label: "Signals" },
  { id: "crashes", label: "Crashes" },
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
  const [sectorsData, setSectorsData] = useState<SectorsResponse | null>(null);
  const [selectedSector, setSelectedSector] = useState<string | null>(null);

  useEffect(() => {
    getMeta().then(setMeta).catch(() => {});
  }, [nonce]);

  const deb = useRef<number | undefined>(undefined);
  useEffect(() => {
    if (tab === "portfolio") return;
    window.clearTimeout(deb.current);
    deb.current = window.setTimeout(() => {
      setLoading(true);
      setErr(null);
      if (tab === "sectors") {
        getSectors(filters)
          .then((d) => {
            setSectorsData(d);
            setUpdatedAt(Date.now());
          })
          .catch((e) => setErr(String(e)))
          .finally(() => setLoading(false));
        return;
      }
      const req =
        tab === "flips" ? getFlips(filters)
        : tab === "signals" ? getSignals(filters)
        : tab === "crashes" ? getCrashes(filters)
        : getItems(filters);
      req
        .then((r) => {
          setRows(r);
          setUpdatedAt(Date.now());
        })
        .catch((e) => setErr(String(e)))
        .finally(() => setLoading(false));
    }, 250);
    return () => window.clearTimeout(deb.current);
  }, [tab, filters, nonce]);

  useEffect(() => {
    if (!auto) return;
    const id = window.setInterval(() => setNonce((n) => n + 1), REFRESH_MS);
    return () => window.clearInterval(id);
  }, [auto]);

  const shown = useMemo(() => {
    const q = search.trim().toLowerCase();
    return q ? rows.filter((r) => r.name?.toLowerCase().includes(q)) : rows;
  }, [rows, search]);

  const defaultSort = tab === "all" ? [{ id: "profit_per_cycle", desc: true }] : [];

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
          {loading ? "loading…" : tab === "sectors" ? `${sectorsData?.sectors.length ?? 0} sectors` : `${shown.length} rows`}
          {updatedAt ? ` · ${new Date(updatedAt).toLocaleTimeString()}` : ""}
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
        ) : (
          <>
            <div className="table-wrap">
              {tab === "signals" && (
                <div className="exp-banner">
                  ⚠ Experimental — mean-reversion signals are not yet validated (backtests are negative on current data).
                  Informational only; use the <b>Flips</b> tab for trades.
                </div>
              )}
              {tab === "crashes" && (
                <div className="crash-banner">
                  Crash-&-recover: items ≥18% below their 7-day established level. Backtested ~59% win / profit factor ~2
                  even paying the full spread — modest but real, and it'll firm up as more data accrues. Buy near
                  "Buy now", place a sell offer near "Target".
                </div>
              )}
              {tab === "crashes" ? (
                <CrashTable rows={shown} selectedId={selected} onSelect={setSelected} />
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
