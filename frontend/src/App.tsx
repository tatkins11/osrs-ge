import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { addOrder, getAccount, getCrashes, getFlips, getInvest, getItems, getMeta, getOrders, getOvernight, getSectors, getVolume, setFreeGp, type Filters, type InvestResponse, type Meta, type Order, type Row, type SectorsResponse, type TradePrefill } from "./api";
import { gpShort } from "./format";
import { Dashboard } from "./components/Dashboard";
import { Planner } from "./components/Planner";
import { GrowthTracker } from "./components/GrowthTracker";
import { ProvenItems } from "./components/ProvenItems";
import { Controls } from "./components/Controls";
import { CrashTable } from "./components/CrashTable";
import { InvestTable } from "./components/InvestTable";
import { ItemPanel } from "./components/ItemPanel";
import { MarketTable } from "./components/MarketTable";
import { OrdersTable } from "./components/OrdersTable";
import { OvernightTable } from "./components/OvernightTable";
import { Portfolio } from "./components/Portfolio";
import { SectorGrid } from "./components/SectorGrid";
import { SectorPanel } from "./components/SectorPanel";
import { VolumeTable } from "./components/VolumeTable";

type Tab = "today" | "flips" | "allocate" | "growth" | "proven" | "invest" | "crashes" | "movers" | "overnight" | "sectors" | "all" | "orders" | "portfolio";

const DEFAULT_FILTERS: Filters = {
  bankroll: 250_000_000,
  minVolume: 100,
  minMargin: 1,
  minRoi: 0.004,
  minProfit: 500_000,
  minPrice: 1_000,
  maxPrice: 2_147_483_647,
  minConfidence: 75,
  minDiscount: 0.08,
  zBuy: -1.5,
  zSell: 1.5,
  minRtProfit: 350_000,
};

const TABS: { id: Tab; label: string }[] = [
  { id: "today", label: "Today" },
  { id: "flips", label: "Flips" },
  { id: "allocate", label: "8-Slot Plan" },
  { id: "growth", label: "Growth" },
  { id: "proven", label: "Proven" },
  { id: "invest", label: "Invest" },
  { id: "crashes", label: "Crashes" },
  { id: "movers", label: "Movers" },
  { id: "overnight", label: "Overnight" },
  { id: "sectors", label: "Sectors" },
  { id: "all", label: "All items" },
  { id: "orders", label: "Orders" },
  { id: "portfolio", label: "Portfolio" },
];

const REFRESH_MS = 60_000;

export default function App() {
  const [meta, setMeta] = useState<Meta | null>(null);
  const [tab, setTab] = useState<Tab>(() => {
    try {
      const saved = localStorage.getItem("ge.tab") as Tab | null;
      // one-time: land on the new Today dashboard (the old default was the Flips table)
      if (localStorage.getItem("ge.tab.todaymig") !== "1") {
        localStorage.setItem("ge.tab.todaymig", "1");
        if (!saved || saved === "flips") return "today";
      }
      return saved || "today";
    } catch { return "today"; }
  });
  const [filters, setFilters] = useState<Filters>(() => {
    try {
      const s = localStorage.getItem("ge.filters");
      const f: Filters = s ? { ...DEFAULT_FILTERS, ...JSON.parse(s) } : { ...DEFAULT_FILTERS };
      // one-time migration: the min round-trip profit floor was lowered 500K -> 350K (fillable flips
      // mostly profit 150-450K, so 500K left the plan empty). Bump anyone still on the old default once.
      if (localStorage.getItem("ge.filters.rtmig") !== "1") {
        if (f.minRtProfit === 500_000) f.minRtProfit = 350_000;
        localStorage.setItem("ge.filters.rtmig", "1");
      }
      return f;
    } catch { return DEFAULT_FILTERS; }
  });
  const [prefill, setPrefill] = useState<(TradePrefill & { nonce: number }) | null>(null);
  const prefillN = useRef(0);
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
  const [ordersData, setOrdersData] = useState<Order[]>([]);
  const [selectedSector, setSelectedSector] = useState<string | null>(null);
  const [panelCollapsed, setPanelCollapsed] = useState(false);

  // log-a-trade from a signal row: stash a prefill (new nonce each click) and jump to Portfolio
  const onLog = useCallback((p: TradePrefill) => {
    setPrefill({ ...p, nonce: (prefillN.current += 1) });
    setTab("portfolio");
  }, []);

  // free gp is server-persisted now (auto-adjusts as orders fill). Editing the field POSTs it;
  // the value is re-synced from the server on every refresh so plugin/order changes flow in.
  const onBankrollCommit = useCallback((v: number) => {
    setFilters((f) => ({ ...f, bankroll: Math.max(0, Math.round(v)) }));
    setFreeGp(Math.max(0, Math.round(v))).catch(() => {});
  }, []);

  // quick-add an order from the 8-Slot Plan (or Orders form): place it; the server reconciles free gp
  const onAddOrder = useCallback(
    async (o: { item_id: number; side: "buy" | "sell"; price: number; qty: number }) => {
      try {
        await addOrder({ item_id: o.item_id, side: o.side, price: o.price, total_qty: o.qty });
        setNonce((n) => n + 1); // refresh pulls the server-adjusted free gp + the new order
      } catch { /* surfaced by the next refresh */ }
    },
    []
  );

  // persist filters + active tab across reloads
  useEffect(() => { try { localStorage.setItem("ge.tab", tab); } catch { /* ignore */ } }, [tab]);
  useEffect(() => { try { localStorage.setItem("ge.filters", JSON.stringify(filters)); } catch { /* ignore */ } }, [filters]);

  useEffect(() => {
    getMeta().then(setMeta).catch(() => {});
  }, [nonce]);

  // free gp lives server-side now (auto-adjusts as orders are placed/filled/cancelled). Pull it into
  // the filter on every refresh so the plan/market sizing reflect the live value.
  useEffect(() => {
    getAccount()
      .then((a) => { if (a.free_gp != null) { const v = Math.max(0, a.free_gp); setFilters((f) => (f.bankroll === v ? f : { ...f, bankroll: v })); } })
      .catch(() => {});
  }, [nonce]);

  // picking a (new) item re-opens the panel if it was minimized
  useEffect(() => {
    if (selected != null) setPanelCollapsed(false);
  }, [selected]);

  // clear stale rows + error on tab switch so one tab's data never renders under another's columns
  useEffect(() => {
    setRows([]);
    setErr(null);
  }, [tab]);

  const deb = useRef<number | undefined>(undefined);
  useEffect(() => {
    if (tab === "portfolio" || tab === "allocate" || tab === "growth" || tab === "today") return; // these components self-fetch
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
      if (tab === "orders") {
        getOrders()
          .then((d) => ok() && (setOrdersData(d), setUpdatedAt(Date.now())))
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
        <Controls filters={filters} setFilters={setFilters} onBankrollCommit={onBankrollCommit} />
        <div className="ctrl search">
          <label>Search</label>
          <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="item name…" />
        </div>
        <div className="spacer" />
        <span className="note">
          {loading ? <><span className="spinner" />loading…</> : tab === "sectors" ? `${sectorsData?.sectors.length ?? 0} sectors` : tab === "invest" ? `${investData?.buys.length ?? 0} buys` : tab === "orders" ? `${ordersData.length} orders` : tab === "allocate" ? "8-slot plan" : tab === "growth" ? "bankroll growth" : tab === "today" ? "session dashboard" : tab === "portfolio" ? "" : `${shown.length} rows`}
          {updatedAt ? ` · updated ${ago(updatedAt)}` : ""}
          {err ? ` · error: ${err}` : ""}
        </span>
        <label className="autobox" title={`auto-refresh every ${REFRESH_MS / 1000}s`}>
          <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /> auto
        </label>
        {(selected != null || selectedSector) && (
          <button
            className="refresh"
            onClick={() => setPanelCollapsed((c) => !c)}
            title={panelCollapsed ? "Show details panel" : "Minimize details panel (more room for the table)"}
          >
            {panelCollapsed ? "‹ panel" : "panel ›"}
          </button>
        )}
        <button className="refresh" onClick={() => setNonce((n) => n + 1)} title="Refresh now">↻</button>
      </div>

      <div className={`main ${panelCollapsed ? "panel-collapsed" : ""}`}>
        {tab === "today" ? (
          <>
            <div className="table-wrap">
              <Dashboard filters={filters} refreshNonce={nonce} onSelect={setSelected} goTo={(t) => setTab(t as Tab)} />
            </div>
            <div className={`panel-wrap ${selected != null ? "open" : ""}`}>
              <ItemPanel itemId={selected} filters={filters} refreshNonce={nonce} onClose={() => setSelected(null)} />
            </div>
          </>
        ) : tab === "portfolio" ? (
          <>
            <div className="table-wrap">
              <Portfolio refreshNonce={nonce} prefill={prefill} onSelect={setSelected} />
            </div>
            <div className={`panel-wrap ${selected != null ? "open" : ""}`}>
              <ItemPanel itemId={selected} filters={filters} refreshNonce={nonce} onClose={() => setSelected(null)} />
            </div>
          </>
        ) : tab === "allocate" ? (
          <>
            <div className="table-wrap">
              <Planner filters={filters} refreshNonce={nonce} selectedId={selected} onSelect={setSelected} onAddOrder={onAddOrder} />
            </div>
            <div className={`panel-wrap ${selected != null ? "open" : ""}`}>
              <ItemPanel itemId={selected} filters={filters} refreshNonce={nonce} onClose={() => setSelected(null)} />
            </div>
          </>
        ) : tab === "growth" ? (
          <>
            <div className="table-wrap">
              <GrowthTracker filters={filters} refreshNonce={nonce} />
            </div>
            <div className="panel-wrap" />
          </>
        ) : tab === "proven" ? (
          <>
            <div className="table-wrap">
              <ProvenItems selectedId={selected} onSelect={setSelected} refreshNonce={nonce} />
            </div>
            <div className={`panel-wrap ${selected != null ? "open" : ""}`}>
              <ItemPanel itemId={selected} filters={filters} refreshNonce={nonce} onClose={() => setSelected(null)} />
            </div>
          </>
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
                onLog={onLog}
              />
            </div>
            <div className={`panel-wrap ${selected != null ? "open" : ""}`}>
              <ItemPanel itemId={selected} filters={filters} refreshNonce={nonce} onClose={() => setSelected(null)} />
            </div>
          </>
        ) : tab === "orders" ? (
          <>
            <div className="table-wrap">
              <OrdersTable rows={ordersData} selectedId={selected} onSelect={setSelected} reload={() => { getOrders().then(setOrdersData).catch(() => {}); setNonce((n) => n + 1); }} />
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
                  if the price dumps overnight, then you sell next day toward "Sell target". Ranked by{" "}
                  <b>expected profit/night</b> (Profit/fill × fill chance × win rate), so it favours high-value items and
                  mid-value items with big buy limits over tiny-but-frequent fills. Raise <b>Min profit</b> to push for
                  bigger setups. Fills are inherently infrequent — place several; even a 50% fill-chance item only fills
                  about half the nights, and a filled order can sit underwater until it reverts.
                </div>
              )}
              {tab === "crashes" ? (
                <CrashTable rows={shown} selectedId={selected} onSelect={setSelected} onLog={onLog} />
              ) : tab === "movers" ? (
                <VolumeTable rows={shown} selectedId={selected} onSelect={setSelected} />
              ) : tab === "overnight" ? (
                <OvernightTable rows={shown} selectedId={selected} onSelect={setSelected} />
              ) : (
                <MarketTable key={tab} rows={shown} selectedId={selected} onSelect={setSelected} onLog={onLog} defaultSort={defaultSort} />
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
