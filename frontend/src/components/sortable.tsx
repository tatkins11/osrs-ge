import { useMemo, useState, type ReactNode } from "react";

/** Lightweight client-side sorting for the hand-rolled tables (Crashes, Movers,
 *  Overnight, ...). Nulls/NaN always sort last. Click a header to sort; click again
 *  to flip direction. */
export type SortState = { key: string | null; dir: "asc" | "desc"; onSort: (k: string) => void };

export function useSortable<T extends Record<string, unknown>>(
  rows: T[],
  initialKey: string | null = null,
  initialDir: "asc" | "desc" = "desc"
) {
  const [key, setKey] = useState<string | null>(initialKey);
  const [dir, setDir] = useState<"asc" | "desc">(initialDir);
  const sorted = useMemo(() => {
    if (!key) return rows;
    const m = dir === "asc" ? 1 : -1;
    const isNull = (v: unknown) => v == null || (typeof v === "number" && Number.isNaN(v));
    return [...rows].sort((a, b) => {
      const av = a[key] as unknown;
      const bv = b[key] as unknown;
      if (isNull(av) && isNull(bv)) return 0;
      if (isNull(av)) return 1; // nulls last regardless of direction
      if (isNull(bv)) return -1;
      if (typeof av === "number" && typeof bv === "number") return (av - bv) * m;
      return String(av).localeCompare(String(bv)) * m;
    });
  }, [rows, key, dir]);
  const onSort = (k: string) =>
    key === k ? setDir((d) => (d === "asc" ? "desc" : "asc")) : (setKey(k), setDir("desc"));
  return { sorted, sort: { key, dir, onSort } as SortState };
}

/** A sortable <th>. Use in place of <th> in tables wired with useSortable. */
export function SortTh({
  k,
  sort,
  className = "",
  title,
  children,
}: {
  k: string;
  sort: SortState;
  className?: string;
  title?: string;
  children: ReactNode;
}) {
  const active = sort.key === k;
  return (
    <th className={`sortable ${className}`} title={title} onClick={() => sort.onSort(k)}>
      {children}
      {active && <span className="arrow">{sort.dir === "asc" ? " ▲" : " ▼"}</span>}
    </th>
  );
}
