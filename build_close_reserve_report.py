"""Internal PDF: high bid vs reserve, grouped by SKU prefix (sold / not sold).

Reads a closing-snapshot CSV (from Live Watch or CT Seller) and writes HTML + PDF
in the same folder, using the same Edge/Chrome print path as other Sale Suite reports.
"""
from __future__ import annotations

import argparse
import html
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd

from report_engine import find_browser, html_to_pdf
from brand import COPYRIGHT_LINE

_LEADING_LETTERS = re.compile(r"^([A-Za-z]+)")
SALE_ID_RE = re.compile(r"/estate-sale/(\d+)", re.I)

STYLES = """
    * { box-sizing: border-box; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
    body {
      font-family: "Segoe UI", Calibri, Arial, sans-serif;
      color: #1a1a1a;
      max-width: 980px;
      margin: 36px auto;
      padding: 0 28px 56px;
      line-height: 1.5;
    }
    h1 { font-size: 1.85rem; color: #1b4332; margin-bottom: 0.2rem; }
    h2 {
      font-size: 1.15rem; color: #2d6a4f; margin-top: 36px;
      border-bottom: 2px solid #d8f3dc; padding-bottom: 6px;
    }
    h3 { font-size: 1rem; color: #1b4332; margin: 18px 0 8px; }
    .badge {
      display: inline-block; background: #1b4332; color: #fff;
      font-size: 0.78rem; font-weight: 600; letter-spacing: 0.05em;
      text-transform: uppercase; padding: 4px 12px; border-radius: 4px; margin-bottom: 12px;
    }
    .subtitle { color: #555; margin: 0 0 8px; font-size: 1.05rem; }
    .meta { color: #666; font-size: 0.9rem; margin-bottom: 28px; }
    .cards {
      display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 12px; margin-bottom: 22px;
    }
    .card { background: #f1faee; border: 1px solid #b7e4c7; border-radius: 8px; padding: 12px 14px; }
    .card.warn { background: #fff3cd; border-color: #ffc107; }
    .card .label { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.05em; color: #52796f; }
    .card.warn .label { color: #856404; }
    .card .value { font-size: 1.25rem; font-weight: 700; color: #1b4332; margin-top: 4px; }
    .highlight {
      background: #fff3cd; border-left: 4px solid #ffc107; padding: 14px 18px;
      margin-bottom: 28px; border-radius: 0 8px 8px 0;
    }
    .tally-bar { display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 18px; }
    .tally-bar .pill { background: #e9ecef; border-radius: 20px; padding: 8px 16px; font-size: 0.9rem; }
    .tally-bar .pill.sold { background: #d1e7dd; color: #0f5132; }
    .tally-bar .pill.unsold { background: #f8d7da; color: #842029; }
    table { width: 100%; border-collapse: collapse; font-size: 0.88rem; margin-bottom: 10px; }
    th { background: #1b4332; color: #fff; text-align: left; padding: 8px 10px; }
    th.num, td.num { text-align: right; }
    td { padding: 7px 10px; border-bottom: 1px solid #e9ecef; vertical-align: top; }
    tr:nth-child(even) td { background: #fafafa; }
    tr.unsold td { color: #555; }
    tr.missed td { background: #fff8e1; }
    .status-badge {
      display: inline-block; font-size: 0.75rem; padding: 2px 8px; border-radius: 4px;
    }
    .sold-badge { background: #d1e7dd; color: #0f5132; }
    .unsold-badge { background: #f8d7da; color: #842029; }
    .met-yes { color: #0f5132; font-weight: 600; }
    .met-no { color: #842029; font-weight: 600; }
    .muted { color: #888; }
    tfoot td { font-weight: 700; background: #d8f3dc; border-top: 2px solid #1b4332; }
    .footer { margin-top: 40px; font-size: 0.82rem; color: #888; border-top: 1px solid #ddd; padding-top: 16px; }
    @media print {
      html, body { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
      body { margin: 16px; max-width: none; }
      tr { page-break-inside: avoid; }
      thead { display: table-header-group; }
      h2 { page-break-after: avoid; }
    }
"""


def _money(val: float) -> str:
    return f"${val:,.2f}"


def _esc(text: object) -> str:
    return html.escape(str(text or ""), quote=True)


def _prefix(sku: object) -> str:
    match = _LEADING_LETTERS.match(str(sku or "").strip())
    return match.group(1).upper() if match else "?"


def _is_sold(status: object) -> bool:
    return str(status or "").strip().lower() == "sold"


def _sale_id_from_links(df: pd.DataFrame) -> str:
    if "Item Link" not in df.columns:
        return ""
    for raw in df["Item Link"].astype(str):
        match = SALE_ID_RE.search(raw)
        if match:
            return match.group(1)
    return ""


def _num(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").fillna(0.0)


def _item_rows(df: pd.DataFrame, *, sold: bool) -> str:
    rows = []
    for _, row in df.iterrows():
        bid = float(row["High Bid"])
        reserve = float(row["Reserve Price"])
        met = str(row.get("Met Reserve") or "").strip()
        missed = (not sold) and reserve > 0 and bid < reserve
        cls = "sold" if sold else ("missed" if missed else "unsold")
        badge_cls = "sold-badge" if sold else "unsold-badge"
        status = "Sold" if sold else "Not Sold"
        if met == "Yes":
            met_html = '<span class="met-yes">Yes</span>'
        elif met == "No":
            met_html = '<span class="met-no">No</span>'
        else:
            met_html = '<span class="muted">—</span>'
        reserve_cell = _money(reserve) if reserve > 0 else "—"
        bid_cell = _money(bid) if bid > 0 else "—"
        rows.append(
            f'<tr class="{cls}">'
            f"<td>{_esc(row['SKU'])}</td>"
            f"<td>{_esc(row['Item Name'])}</td>"
            f'<td><span class="status-badge {badge_cls}">{status}</span></td>'
            f'<td class="num">{bid_cell}</td>'
            f'<td class="num">{reserve_cell}</td>'
            f"<td>{met_html}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def _section_table(df: pd.DataFrame, *, sold: bool, bid_sum: float, reserve_sum: float) -> str:
    if df.empty:
        return ""
    label = "Sold" if sold else "Not sold"
    foot_bid = _money(bid_sum) if bid_sum else "—"
    foot_res = _money(reserve_sum) if reserve_sum else "—"
    return f"""
  <h3>{label} ({len(df)})</h3>
  <table>
    <thead>
      <tr>
        <th>SKU</th>
        <th>Item</th>
        <th>Status</th>
        <th class="num">High bid</th>
        <th class="num">Reserve</th>
        <th>Met reserve</th>
      </tr>
    </thead>
    <tbody>
      {_item_rows(df, sold=sold)}
    </tbody>
    <tfoot>
      <tr>
        <td colspan="3">{label} subtotal — {len(df)} items</td>
        <td class="num">{foot_bid}</td>
        <td class="num">{foot_res}</td>
        <td></td>
      </tr>
    </tfoot>
  </table>
"""


def build_html(df: pd.DataFrame, *, title: str, sale_id: str) -> str:
    work = df.copy()
    work["High Bid"] = _num(work["High Bid"])
    work["Reserve Price"] = _num(work["Reserve Price"])
    work["_prefix"] = work["SKU"].map(_prefix)
    work["_sold"] = work["Status"].map(_is_sold)

    sold = work[work["_sold"]]
    not_sold = work[~work["_sold"]]
    missed = not_sold[(not_sold["Reserve Price"] > 0) & (not_sold["High Bid"] < not_sold["Reserve Price"])]
    missed_with_bid = missed[missed["High Bid"] > 0]
    sell_rate = f"{100 * len(sold) / len(work):.0f}%" if len(work) else "0%"

    sold_hammer = float(sold["High Bid"].sum())
    missed_bids = float(missed_with_bid["High Bid"].sum())
    missed_reserves = float(missed["Reserve Price"].sum())

    prefix_order = sorted(work["_prefix"].unique(), key=lambda c: (c == "?", c))
    prefix_rows = []
    sections = []
    for code in prefix_order:
        group = work[work["_prefix"] == code]
        g_sold = group[group["_sold"]].sort_values(["High Bid", "SKU"], ascending=[False, True])
        g_ns = group[~group["_sold"]].sort_values(["Reserve Price", "SKU"], ascending=[False, True])
        g_miss = g_ns[(g_ns["Reserve Price"] > 0) & (g_ns["High Bid"] < g_ns["Reserve Price"])]
        prefix_rows.append(
            "<tr>"
            f"<td><strong>{_esc(code)}</strong></td>"
            f'<td class="num">{len(group)}</td>'
            f'<td class="num">{len(g_sold)}</td>'
            f'<td class="num">{len(g_ns)}</td>'
            f'<td class="num">{len(g_miss)}</td>'
            f'<td class="num">{_money(float(g_sold["High Bid"].sum()))}</td>'
            f'<td class="num">{_money(float(g_miss[g_miss["High Bid"] > 0]["High Bid"].sum()))}</td>'
            "</tr>"
        )
        item_word = "item" if len(group) == 1 else "items"
        sections.append(
            f"<h2>SKU {_esc(code)} — {len(group)} {item_word}</h2>"
            '<div class="tally-bar">'
            f'<span class="pill sold"><strong>{len(g_sold)}</strong> sold</span>'
            f'<span class="pill unsold"><strong>{len(g_ns)}</strong> not sold</span>'
            f'<span class="pill"><strong>{len(g_miss)}</strong> below reserve</span>'
            "</div>"
            + _section_table(
                g_sold,
                sold=True,
                bid_sum=float(g_sold["High Bid"].sum()),
                reserve_sum=float(g_sold["Reserve Price"].sum()),
            )
            + _section_table(
                g_ns,
                sold=False,
                bid_sum=float(g_ns["High Bid"].sum()),
                reserve_sum=float(g_ns["Reserve Price"].sum()),
            )
        )

    stamp = datetime.now().strftime("%B %d, %Y · %I:%M %p").lstrip("0").replace(" 0", " ")
    sale_link = f"https://ctbids.com/estate-sale/{sale_id}" if sale_id else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>High bid vs reserve — { _esc(title) }</title>
  <style>{STYLES}</style>
</head>
<body>
  <div class="badge">Internal · CT Seller close snapshot</div>
  <h1>High bid vs reserve</h1>
  <p class="subtitle">{_esc(title)}</p>
  <p class="meta">
    Report date: {_esc(stamp)}
    {f'<br /><a href="{_esc(sale_link)}">{_esc(sale_link)}</a>' if sale_link else ""}
  </p>

  <div class="tally-bar">
    <span class="pill sold"><strong>{len(sold)}</strong> sold</span>
    <span class="pill unsold"><strong>{len(not_sold)}</strong> not sold</span>
    <span class="pill"><strong>{len(work)}</strong> total</span>
    <span class="pill">Sell-through <strong>{sell_rate}</strong></span>
  </div>

  <div class="cards">
    <div class="card"><div class="label">Sold hammer</div><div class="value">{_money(sold_hammer)}</div></div>
    <div class="card"><div class="label">Sold lots</div><div class="value">{len(sold)}</div></div>
    <div class="card warn"><div class="label">Below reserve</div><div class="value">{len(missed)}</div></div>
    <div class="card warn"><div class="label">Bids that missed reserve</div><div class="value">{_money(missed_bids)}</div></div>
  </div>

  <div class="highlight">
    <strong>{len(missed_with_bid)}</strong> lots had a high bid under reserve
    (high bids totaling <strong>{_money(missed_bids)}</strong> against reserves of
    <strong>{_money(missed_reserves)}</strong>).
    Those bids are from CT Seller <em>sold price</em> on Not Sold lots — they do not appear
    on the sale-history CSV.
  </div>

  <h2>By SKU prefix</h2>
  <table>
    <thead>
      <tr>
        <th>Prefix</th>
        <th class="num">Lots</th>
        <th class="num">Sold</th>
        <th class="num">Not sold</th>
        <th class="num">Below reserve</th>
        <th class="num">Sold hammer</th>
        <th class="num">Missed bids</th>
      </tr>
    </thead>
    <tbody>
      {''.join(prefix_rows)}
    </tbody>
  </table>

  {''.join(sections)}

  <p class="footer">
    High bid is the CT Seller sold price (final hammer if sold, last bid if not sold).
    Reserve comes from the same Seller item list. This is an internal snapshot, not an invoice.
    <br />{COPYRIGHT_LINE}
  </p>
</body>
</html>
"""


def build(csv_path: Path, *, title: str = "", out_dir: Path | None = None) -> list[Path]:
    csv_path = Path(csv_path)
    df = pd.read_csv(csv_path)
    if "SKU" not in df.columns or "High Bid" not in df.columns:
        raise ValueError(f"Not a closing-snapshot CSV: {csv_path}")
    sale_id = _sale_id_from_links(df)
    heading = title.strip() or (
        f"Nancy's Estate — sale {sale_id}" if sale_id else csv_path.stem
    )
    folder = Path(out_dir) if out_dir else csv_path.parent
    folder.mkdir(parents=True, exist_ok=True)
    stem = folder / f"{csv_path.stem}-by-prefix"
    html_path = stem.with_suffix(".html")
    html_path.write_text(build_html(df, title=heading, sale_id=sale_id), encoding="utf-8")
    paths = [html_path]
    pdf_path = stem.with_suffix(".pdf")
    browser = find_browser()
    if browser and html_to_pdf(browser, html_path, pdf_path):
        paths.append(pdf_path)
    elif browser:
        raise RuntimeError(f"Could not create PDF with {browser.name}. HTML is at {html_path}")
    else:
        raise RuntimeError(f"Edge/Chrome not found. HTML is ready at {html_path}")
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description="Build prefix sold/not-sold bid vs reserve PDF")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--title", type=str, default="")
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args()
    for path in build(args.input, title=args.title, out_dir=args.output_dir):
        print(path)


if __name__ == "__main__":
    main()
