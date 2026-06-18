import type { Row } from "../api";
import { fixed, gp, gpShort, spct } from "../format";
import { SortTh, useSortable } from "./sortable";

const sign = (x?: number | null) => (x == null ? "" : x > 0 ? "pos" : x < 0 ? "neg" : "");

/** Items "in play": last-24h volume well above their normal daily volume.
 *  Click a header to sort; click a row for the deep dive. */
export function VolumeTable({
  rows,
  selectedId,
  onSelect,
}: {
  rows: Row[];
  selectedId: number | null;
  onSelect: (id: number) => void;
}) {
  const { sorted, sort } = useSortable(rows, "vol_ratio");
  return (
    <div className="tbl-scroll">
      <table className="tbl">
        <thead>
          <tr>
            <SortTh k="name" sort={sort} className="left">Item</SortTh>
            <SortTh k="vol_ratio" sort={sort}>Vol ×normal</SortTh>
            <SortTh k="vol_24h" sort={sort}>Vol 24h</SortTh>
            <SortTh k="chg_24h" sort={sort}>24h chg</SortTh>
            <SortTh k="mid" sort={sort}>Price</SortTh>
            <SortTh k="vol_daily_7d" sort={sort}>Avg vol/day</SortTh>
            <SortTh k="z_7d" sort={sort}>Z 7d</SortTh>
          </tr>
        </thead>
        <tbody>
          {sorted.map((r) => (
            <tr key={r.item_id} className={r.item_id === selectedId ? "selected" : ""} onClick={() => onSelect(r.item_id)}>
              <td className="name left">{r.name}</td>
              <td className="pos">{r.vol_ratio == null ? "–" : fixed(r.vol_ratio, 1) + "×"}</td>
              <td className="dim">{gpShort(r.vol_24h)}</td>
              <td className={sign(r.chg_24h)}>{spct(r.chg_24h)}</td>
              <td>{gp(r.mid)}</td>
              <td className="dim">{gpShort(r.vol_daily_7d)}</td>
              <td className={r.z_7d == null ? "" : r.z_7d < 0 ? "pos" : "neg"}>{fixed(r.z_7d, 2)}</td>
            </tr>
          ))}
          {sorted.length === 0 && (
            <tr>
              <td colSpan={7} className="left muted">
                Nothing unusually active right now — lower the spike multiple, or check back later.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
