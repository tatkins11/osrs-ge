import { useEffect, useMemo, useRef, useState } from "react";
import { getFlips, getItems, getMeta, getSignals, type Filters, type Meta, type Row } from "./api";
import { gpShort } from "./format";
import { Controls } from "./components/Controls";
import { ItemPanel } from "./components/ItemPanel";
import { MarketTable } from "./components/MarketTable";

type Tab = "flips" | "signals" | "all";

const DEFAULT_FILTERS: Filters = {
  bankroll: 250_000_000,
  minVolume: 100,
  minMargin: 1,
  minRoi: 0.004,
  zBuy: -1.5,
  zSell: 1.5,
};

const TABS: { id: Tab; label: string }[] = [
  { id: "flips", label: "Flips" },
  { id: "signals", label: "Signals" },
  { id: "all", label: "All items" },
];

export default function App() {
  const [meta, setMeta] = useState<Meta | null>(null);
  const [tab, setTab] = useState<Tab>("flips");
  const [filters, setFilters] = useState<Filters>(DEFAULT_FILTERS);
  const [rows, setRows] = useState<Row[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [search, setSearch] = useState("");

  useEffect(() => {
    getMeta().then(setMeta).catch(() => {});
  }, []);

  const deb = useRef<number | undefined>(undefined);
  useEffect(() => {
    window.clearTimeout(deb.current);
    deb.current = window.setTimeout(() => {
      setLoading(true);
      setErr(null);
      const req = tab === "flips" ? getFlips(filters) : tab === "signals" ? getSignals(filters) : getItems(filters);
      req
        .then(setRows)
        .catch((e) => setErr(String(e)))
        .finally(() => setLoading(false));
    }, 250);
    return () => window.clearTimeout(deb.current);
  }, [tab, filters]);

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
              <span className="v">{(meta.tax.rate * 100).toFixed(0)}% · cap {gpShort(meta.tax.cap)}</span>
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
          {loading ? "loading…" : `${shown.length} rows`}
          {err ? ` · error: ${err}` : ""}
        </span>
      </div>

      <div className="main">
        <div className="table-wrap">
          <MarketTable key={tab} rows={shown} selectedId={selected} onSelect={setSelected} defaultSort={defaultSort} />
        </div>
        <div className="panel-wrap">
          <ItemPanel itemId={selected} filters={filters} onClose={() => setSelected(null)} />
        </div>
      </div>
    </div>
  );
}
