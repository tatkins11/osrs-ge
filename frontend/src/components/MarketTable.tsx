import { useState } from "react";
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
  type SortingState,
} from "@tanstack/react-table";
import type { Row } from "../api";
import { age, fixed, gp, gpShort, num, pct } from "../format";
import { SignalBadge } from "./SignalBadge";

const colh = createColumnHelper<Row>();
const sign = (v?: number | null) => (v == null ? "" : v > 0 ? "pos" : v < 0 ? "neg" : "");

const columns = [
  colh.accessor("name", { header: "Item", cell: (c) => <span className="name">{c.getValue() as string}</span>, meta: { left: true } }),
  colh.accessor("signal", { header: "Signal", cell: (c) => <SignalBadge signal={c.getValue() as string} />, meta: { left: true } }),
  colh.accessor("buy_price", { header: "Buy", cell: (c) => gp(c.getValue() as number) }),
  colh.accessor("sell_price", { header: "Sell", cell: (c) => gp(c.getValue() as number) }),
  colh.accessor("net_margin", { header: "Net/ea", cell: (c) => <span className={sign(c.getValue() as number)}>{gp(c.getValue() as number)}</span> }),
  colh.accessor("roi", { header: "ROI", cell: (c) => <span className={sign(c.getValue() as number)}>{pct(c.getValue() as number, 2)}</span> }),
  colh.accessor("buy_limit", { header: "Limit", cell: (c) => num(c.getValue() as number) }),
  colh.accessor("profit_per_cycle", { header: "Profit/4h", cell: (c) => <span className="dim">{gpShort(c.getValue() as number)}</span> }),
  colh.accessor("vol_daily_7d", { header: "Vol/day", cell: (c) => <span className="dim">{gpShort(c.getValue() as number)}</span> }),
  colh.accessor("z_7d", {
    header: "Z",
    cell: (c) => {
      const v = c.getValue() as number | null;
      return <span className={v == null ? "" : v < 0 ? "pos" : "neg"}>{fixed(v, 2)}</span>;
    },
  }),
  colh.accessor("pct_30d", { header: "30d %", cell: (c) => pct(c.getValue() as number, 0) }),
  colh.accessor("price_age_min", { header: "Age", cell: (c) => <span className="dim">{age(c.getValue() as number)}</span> }),
];

export function MarketTable({
  rows,
  selectedId,
  onSelect,
  defaultSort = [],
}: {
  rows: Row[];
  selectedId: number | null;
  onSelect: (id: number) => void;
  defaultSort?: { id: string; desc: boolean }[];
}) {
  const [sorting, setSorting] = useState<SortingState>(defaultSort);
  const table = useReactTable({
    data: rows,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  });

  return (
    <div className="tbl-scroll">
      <table className="tbl">
        <thead>
          {table.getHeaderGroups().map((hg) => (
            <tr key={hg.id}>
              {hg.headers.map((h) => {
                const left = (h.column.columnDef.meta as { left?: boolean })?.left;
                const sorted = h.column.getIsSorted();
                return (
                  <th key={h.id} className={left ? "left" : ""} onClick={h.column.getToggleSortingHandler()}>
                    {flexRender(h.column.columnDef.header, h.getContext())}
                    {sorted ? <span className="arrow">{sorted === "asc" ? "▲" : "▼"}</span> : null}
                  </th>
                );
              })}
            </tr>
          ))}
        </thead>
        <tbody>
          {table.getRowModel().rows.map((r) => {
            const id = r.original.item_id;
            return (
              <tr key={id} className={id === selectedId ? "selected" : ""} onClick={() => onSelect(id)}>
                {r.getVisibleCells().map((cell) => {
                  const left = (cell.column.columnDef.meta as { left?: boolean })?.left;
                  return (
                    <td key={cell.id} className={left ? "left" : ""}>
                      {flexRender(cell.column.columnDef.cell, cell.getContext())}
                    </td>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
      {rows.length === 0 && <div className="empty">No rows match the current filters.</div>}
    </div>
  );
}
