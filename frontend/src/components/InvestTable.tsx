import type { Row } from "../api";
import { gp, gpShort, num, pct } from "../format";
import { SortTh, useSortable } from "./sortable";

const confClass = (c?: number | null) => (c == null ? "" : c >= 70 ? "conf-hi" : c >= 50 ? "conf-md" : "conf-lo");

function Conf({ c }: { c?: number | null }) {
  if (c == null) return <span className="dim">–</span>;
  return <span className={`conf ${confClass(c)}`}>{Math.round(c)}</span>;
}

/** Buy-only value finder + (when you hold items) a "time to sell" section for rich holdings.
 *  Server ranks buys by confidence then upside. Click any row for the deep dive. */
export function InvestTable({
  buys,
  sells,
  selectedId,
  onSelect,
}: {
  buys: Row[];
  sells: Row[];
  selectedId: number | null;
  onSelect: (id: number) => void;
}) {
  const { sorted, sort } = useSortable(buys, "value_confidence");
  return (
    <div className="tbl-scroll">
      {sells.length > 0 && (
        <>
          <div className="sub-head sell">Your holdings · time to sell ({sells.length})</div>
          <table className="tbl">
            <thead>
              <tr>
                <th className="left">Item</th>
                <th>Qty</th>
                <th>Avg cost</th>
                <th>Now</th>
                <th>Fair value</th>
                <th>Unrealized</th>
                <th className="left"> </th>
              </tr>
            </thead>
            <tbody>
              {sells.map((r) => (
                <tr key={`s${r.item_id}`} className={r.item_id === selectedId ? "selected" : ""} onClick={() => onSelect(r.item_id)}>
                  <td className="name left">{r.name}</td>
                  <td>{num(r.qty)}</td>
                  <td>{gp(r.avg_cost)}</td>
                  <td>{gp(r.mid)}</td>
                  <td>{gp(r.value_target)}</td>
                  <td className={(r.unrealized ?? 0) >= 0 ? "pos" : "neg"}>{gpShort(r.unrealized)}</td>
                  <td className="left"><span className="badge badge-SELL">SELL</span></td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="sub-head buy">Value buys ({buys.length})</div>
        </>
      )}
      <table className="tbl">
        <thead>
          <tr>
            <SortTh k="value_confidence" sort={sort}>Conf</SortTh>
            <SortTh k="value_horizon" sort={sort} className="left">Horizon</SortTh>
            <SortTh k="name" sort={sort} className="left">Item</SortTh>
            <SortTh k="sell_price" sort={sort}>Buy at</SortTh>
            <SortTh k="value_target" sort={sort}>Fair value</SortTh>
            <SortTh k="value_exp_roi" sort={sort}>Upside</SortTh>
            <SortTh k="value_discount" sort={sort}>Discount</SortTh>
            <SortTh k="alch_support" sort={sort} title="Buy price vs its high-alch floor (highalch − nature rune). Low / 🛡 = alching caps the downside.">Downside</SortTh>
            <SortTh k="vol_daily_7d" sort={sort}>Vol/day</SortTh>
          </tr>
        </thead>
        <tbody>
          {sorted.map((r) => (
            <tr key={r.item_id} className={r.item_id === selectedId ? "selected" : ""} onClick={() => onSelect(r.item_id)}>
              <td><Conf c={r.value_confidence} /></td>
              <td className="left"><span className="hz-chip">{r.value_horizon}</span></td>
              <td className="name left">{r.name}</td>
              <td>{gp(r.sell_price)}</td>
              <td>{gp(r.value_target)}</td>
              <td className="pos">{pct(r.value_exp_roi, 1)}</td>
              <td className="pos">{pct(r.value_discount, 0)}</td>
              <td
                className={r.alch_support == null ? "dim" : r.alch_support <= 0.15 ? "pos" : "dim"}
                title="distance above the high-alch floor"
              >
                {r.alch_support == null ? "–" : (r.alch_support <= 0.15 ? "🛡 " : "") + pct(r.alch_support, 0)}
              </td>
              <td className="dim">{gpShort(r.vol_daily_7d)}</td>
            </tr>
          ))}
          {buys.length === 0 && (
            <tr>
              <td colSpan={9} className="left muted">
                No value buys clear the filters right now — lower Min confidence / Min discount in the controls, or
                widen the price range.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
