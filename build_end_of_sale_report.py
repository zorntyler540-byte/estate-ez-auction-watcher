"""Build client-ready end-of-sale report from SKU prefix CSV files."""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

import pandas as pd

from brand import COPYRIGHT_LINE

DEFAULT_DIR = Path(__file__).resolve().parent
PREFIX_FILES = {
    "NK": "Coins & Collectibles",
    "SA": "Estate & Household",
    "Z": "General Merchandise",
    "NL": "NL Items",
}

OUTPUT_COLUMNS = [
    "SKU", "Item Name", "Category Name", "Sale Price",
    "Buyers Premium", "Tax", "Total Collected", "Status", "Invoice Number",
]

STYLES = """
    * {
      box-sizing: border-box;
      -webkit-print-color-adjust: exact;
      print-color-adjust: exact;
    }
    body {
      font-family: "Segoe UI", Calibri, Arial, sans-serif;
      color: #1a1a1a;
      max-width: 960px;
      margin: 36px auto;
      padding: 0 28px 56px;
      line-height: 1.5;
      -webkit-print-color-adjust: exact;
      print-color-adjust: exact;
    }
    h1 { font-size: 1.85rem; color: #1b4332; margin-bottom: 0.2rem; }
    h2 { font-size: 1.15rem; color: #2d6a4f; margin-top: 36px; border-bottom: 2px solid #d8f3dc; padding-bottom: 6px; }
    .client-tag {
      display: inline-block;
      background: #1b4332;
      color: #fff;
      font-size: 0.8rem;
      font-weight: 600;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      padding: 4px 12px;
      border-radius: 4px;
      margin-bottom: 12px;
    }
    .subtitle { color: #555; margin: 0 0 8px; font-size: 1.05rem; }
    .meta { color: #666; font-size: 0.9rem; margin-bottom: 28px; }
    .cards {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 14px;
      margin-bottom: 28px;
    }
    .card {
      background: #f1faee;
      border: 1px solid #b7e4c7;
      border-radius: 8px;
      padding: 14px 16px;
    }
    .card .label { font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em; color: #52796f; }
    .card .value { font-size: 1.35rem; font-weight: 700; color: #1b4332; margin-top: 4px; }
    .highlight {
      background: #fff3cd;
      border-left: 4px solid #ffc107;
      padding: 14px 18px;
      margin-bottom: 28px;
      border-radius: 0 8px 8px 0;
    }
    table { width: 100%; border-collapse: collapse; font-size: 0.9rem; margin-bottom: 8px; }
    th { background: #1b4332; color: #fff; text-align: left; padding: 9px 11px; }
    th.num, td.num { text-align: right; }
    td { padding: 8px 11px; border-bottom: 1px solid #e9ecef; vertical-align: top; }
    tr:nth-child(even):not(.subtotal) td { background: #fafafa; }
    tr.subtotal td { background: #d8f3dc; border-top: 2px solid #1b4332; }
    tr.unsold td { color: #666; }
    tr.unsold .status-badge {
      display: inline-block;
      background: #f8d7da;
      color: #842029;
      font-size: 0.75rem;
      padding: 2px 8px;
      border-radius: 4px;
    }
    tr.sold .status-badge {
      display: inline-block;
      background: #d1e7dd;
      color: #0f5132;
      font-size: 0.75rem;
      padding: 2px 8px;
      border-radius: 4px;
    }
    .met-yes { color: #0f5132; font-weight: 600; }
    .met-no { color: #842029; font-weight: 600; }
    .muted { color: #888; }
    .tally-bar {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      margin-bottom: 28px;
    }
    .tally-bar .pill {
      background: #e9ecef;
      border-radius: 20px;
      padding: 8px 16px;
      font-size: 0.9rem;
    }
    .tally-bar .pill.sold { background: #d1e7dd; color: #0f5132; }
    .tally-bar .pill.unsold { background: #f8d7da; color: #842029; }
    .tally-bar .pill.total { background: #cfe2ff; color: #084298; }
    tfoot td { font-weight: 700; background: #b7e4c7; border-top: 2px solid #1b4332; }
    tr.grand-total td {
      font-weight: 700;
      background: #1b4332;
      color: #fff;
      border-top: 3px solid #0d2818;
      font-size: 1rem;
    }
    .totals-banner {
      display: flex;
      flex-wrap: wrap;
      gap: 24px;
      background: #1b4332;
      color: #fff;
      border-radius: 8px;
      padding: 16px 24px;
      margin-bottom: 28px;
      align-items: center;
    }
    .totals-banner .tb-item { flex: 1; min-width: 140px; }
    .totals-banner .tb-label {
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      opacity: 0.85;
    }
    .totals-banner .tb-value { font-size: 1.4rem; font-weight: 700; margin-top: 2px; }
    .totals-banner .tb-item.grand .tb-value { font-size: 1.75rem; }
    .footer { margin-top: 40px; font-size: 0.82rem; color: #888; border-top: 1px solid #ddd; padding-top: 16px; }
    @media print {
      html, body {
        -webkit-print-color-adjust: exact;
        print-color-adjust: exact;
      }
      body { margin: 16px; max-width: none; }
      table { page-break-inside: auto; }
      tr { page-break-inside: avoid; }
      thead { display: table-header-group; }
    }
"""


def _money(val: float) -> str:
    return f"${val:,.2f}"


def _load_prefix(path: Path) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, int]]:
    """Return (sold items, not sold items, tally counts)."""
    if not path.exists():
        return pd.DataFrame(), pd.DataFrame(), {"sold": 0, "not_sold": 0, "total": 0}

    df = pd.read_csv(path)
    summary_mask = df["SKU"].astype(str).str.startswith("[TALLY]")
    items = df[~summary_mask].copy()

    sold = items[items["Status"].astype(str).str.strip().str.lower() == "sold"].copy()
    not_sold = items[items["Status"].astype(str).str.strip().str.lower() != "sold"].copy()

    counts = {"sold": len(sold), "not_sold": len(not_sold), "total": len(items)}
    return sold, not_sold, counts


def _totals(df: pd.DataFrame) -> dict[str, float]:
    if df.empty:
        return {"items": 0, "sale": 0.0, "premium": 0.0, "tax": 0.0, "total": 0.0}
    return {
        "items": len(df),
        "sale": float(df["Sale Price"].sum()),
        "premium": float(df["Buyers Premium"].sum()),
        "tax": float(df["Tax"].sum()),
        "total": float(df["Total Collected"].sum()),
    }


def _sale_meta(source_csv: Path) -> dict[str, str]:
    meta = {
        "title": "CTBids Online Auction",
        "start": "",
        "end": "",
        "total_listed": "",
        "total_sold": "",
        "total_not_sold": "",
    }
    if not source_csv.exists():
        return meta
    raw = pd.read_csv(source_csv)
    if not raw.empty:
        meta["title"] = str(raw.iloc[0].get("Sale Title", meta["title"]))
        meta["start"] = str(raw.iloc[0].get("Sale Start Date", ""))
        meta["end"] = str(raw.iloc[0].get("Sale End Date", ""))
    status = raw["Status"].astype(str).str.strip().str.lower()
    meta["total_listed"] = str(len(raw))
    meta["total_sold"] = str((status == "sold").sum())
    meta["total_not_sold"] = str((status != "sold").sum())
    return meta


def _reserve_cell(row) -> str:
    try:
        val = float(row.get("Reserve Price") or 0)
    except (TypeError, ValueError):
        val = 0.0
    return _money(val) if val > 0 else "—"


def _met_cell(row) -> str:
    met = str(row.get("Met Reserve") or "").strip()
    if met == "Yes":
        return '<span class="met-yes">Yes</span>'
    if met == "No":
        return '<span class="met-no">No</span>'
    return '<span class="muted">—</span>'


def _item_rows_sold_client(df: pd.DataFrame) -> list[str]:
    rows = []
    for _, row in df.iterrows():
        sale = float(row["Sale Price"])
        rows.append(
            f"""<tr class="sold">
              <td>{row['SKU']}</td>
              <td>{row['Item Name']}</td>
              <td>{row.get('Category Name', '')}</td>
              <td><span class="status-badge">Sold</span></td>
              <td class="num">{_money(sale)}</td>
            </tr>"""
        )
    return rows


def _item_rows_unsold_client(df: pd.DataFrame) -> list[str]:
    rows = []
    for _, row in df.iterrows():
        status = str(row.get("Status", "Not Sold"))
        rows.append(
            f"""<tr class="unsold">
              <td>{row['SKU']}</td>
              <td>{row['Item Name']}</td>
              <td>{row.get('Category Name', '')}</td>
              <td><span class="status-badge">{status}</span></td>
              <td class="num">—</td>
            </tr>"""
        )
    return rows


def _item_rows_sold(df: pd.DataFrame) -> list[str]:
    rows = []
    for _, row in df.iterrows():
        rows.append(
            f"""<tr class="sold">
              <td>{row['SKU']}</td>
              <td>{row['Item Name']}</td>
              <td>{row.get('Category Name', '')}</td>
              <td><span class="status-badge">Sold</span></td>
              <td class="num">{_money(float(row['Sale Price']))}</td>
              <td class="num">{_reserve_cell(row)}</td>
              <td>{_met_cell(row)}</td>
              <td class="num">{_money(float(row['Buyers Premium']))}</td>
              <td class="num">{_money(float(row['Tax']))}</td>
              <td class="num">{_money(float(row['Total Collected']))}</td>
            </tr>"""
        )
    return rows


def _item_rows_unsold(df: pd.DataFrame) -> list[str]:
    rows = []
    for _, row in df.iterrows():
        status = str(row.get("Status", "Not Sold"))
        rows.append(
            f"""<tr class="unsold">
              <td>{row['SKU']}</td>
              <td>{row['Item Name']}</td>
              <td>{row.get('Category Name', '')}</td>
              <td><span class="status-badge">{status}</span></td>
              <td class="num">—</td>
              <td class="num">{_reserve_cell(row)}</td>
              <td>{_met_cell(row)}</td>
              <td class="num">—</td>
              <td class="num">—</td>
              <td class="num">—</td>
            </tr>"""
        )
    return rows


def build_client_html(
    code: str,
    label: str,
    sold_df: pd.DataFrame,
    not_sold_df: pd.DataFrame,
    counts: dict[str, int],
    t: dict[str, float],
    meta: dict[str, str],
    out_path: Path,
) -> None:
    """Single-client end-of-sale report for one SKU prefix (sale price only)."""
    sold_rows = _item_rows_sold_client(sold_df)
    unsold_rows = _item_rows_unsold_client(not_sold_df)
    sell_rate = f"{100 * counts['sold'] / counts['total']:.0f}%" if counts["total"] else "0%"

    not_sold_footer = ""
    if unsold_rows:
        not_sold_footer = (
            f'<tr class="subtotal"><td colspan="4">'
            f"<strong>Not sold — {counts['not_sold']} items</strong></td>"
            f'<td class="num">—</td></tr>'
        )

    grand_total_row = f"""<tr class="grand-total">
        <td colspan="4"><strong>Grand total — {counts['sold']} sold items</strong></td>
        <td class="num"><strong>{_money(t['sale'])}</strong></td>
      </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>End of Sale Report — {code} Client</title>
  <style>{STYLES}</style>
</head>
<body>
  <div class="client-tag">Client report — SKU {code}</div>
  <h1>End of Sale Report</h1>
  <p class="subtitle">{label}</p>
  <p class="subtitle">{meta['title']}</p>
  <p class="meta">
    Sale period: {meta['start']} — {meta['end']}<br>
    Report date: {datetime.now().strftime("%B %d, %Y")}
  </p>

  <div class="tally-bar">
    <span class="pill sold"><strong>{counts['sold']}</strong> sold</span>
    <span class="pill unsold"><strong>{counts['not_sold']}</strong> not sold</span>
    <span class="pill total"><strong>{counts['total']}</strong> total items</span>
    <span class="pill total">Sell-through: <strong>{sell_rate}</strong></span>
  </div>

  <div class="cards">
    <div class="card"><div class="label">Items sold</div><div class="value">{counts['sold']}</div></div>
    <div class="card"><div class="label">Items not sold</div><div class="value">{counts['not_sold']}</div></div>
    <div class="card"><div class="label">Total sale price</div><div class="value">{_money(t['sale'])}</div></div>
  </div>

  <div class="totals-banner">
    <div class="tb-item grand">
      <div class="tb-label">Total sale price</div>
      <div class="tb-value">{_money(t['sale'])}</div>
    </div>
  </div>

  <div class="highlight">
    Your consignment ({code} items): <strong>{counts['sold']} sold</strong>,
    <strong>{counts['not_sold']} did not sell</strong>, out of <strong>{counts['total']}</strong> total items.
    Sold items totaled <strong>{_money(t['sale'])}</strong> in sale price.
  </div>

  <h2>All items ({counts['total']}) — {counts['sold']} sold, {counts['not_sold']} not sold</h2>
  <table>
    <thead>
      <tr>
        <th>SKU</th>
        <th>Item</th>
        <th>Category</th>
        <th>Status</th>
        <th class="num">Sale price</th>
      </tr>
    </thead>
    <tbody>
      {''.join(sold_rows) if sold_rows else '<tr><td colspan="5">No items sold.</td></tr>'}
      <tr class="subtotal">
        <td colspan="4"><strong>Sold subtotal — {counts['sold']} items</strong></td>
        <td class="num"><strong>{_money(t['sale'])}</strong></td>
      </tr>
      {''.join(unsold_rows)}
      {not_sold_footer}
      {grand_total_row}
    </tbody>
  </table>

  <p class="footer">
    This report covers all items with SKU prefix <strong>{code}</strong>.
    &ldquo;Not sold&rdquo; includes unsold and pending items.
    Sale price is the winning bid (hammer price).
    <br />{COPYRIGHT_LINE}
  </p>
</body>
</html>"""
    out_path.write_text(html, encoding="utf-8")


def build_html(
    sections: list[tuple[str, str, pd.DataFrame, pd.DataFrame, dict, dict]],
    meta: dict[str, str],
    grand: dict[str, float],
    grand_counts: dict[str, int],
    out_path: Path,
) -> None:
    summary_rows = []
    for code, label, _, _, counts, t in sections:
        if counts["total"] == 0:
            continue
        share = f"{100 * t['sale'] / grand['sale']:.1f}%" if grand["sale"] else "0%"
        summary_rows.append(
            f"""<tr>
              <td><strong>{code}</strong></td>
              <td>{label}</td>
              <td class="num">{counts['sold']}</td>
              <td class="num">{counts['not_sold']}</td>
              <td class="num">{counts['total']}</td>
              <td class="num">{_money(t['sale'])}</td>
              <td class="num">{_money(t['premium'])}</td>
              <td class="num">{_money(t['tax'])}</td>
              <td class="num">{share}</td>
              <td class="num">{_money(t['total'])}</td>
            </tr>"""
        )

    detail_blocks = []
    for code, label, sold_df, not_sold_df, counts, t in sections:
        if counts["total"] == 0:
            continue
        sold_rows = _item_rows_sold(sold_df)
        unsold_rows = _item_rows_unsold(not_sold_df)
        detail_blocks.append(
            f"""
            <h2>{code} — {label}</h2>
            <p><span class="pill sold">{counts['sold']} sold</span>
               <span class="pill unsold">{counts['not_sold']} not sold</span>
               <span class="pill total">{counts['total']} total</span></p>
            <h3>Items sold</h3>
            <table>
              <thead>
                <tr>
                  <th>SKU</th><th>Item</th><th>Category</th><th>Status</th>
                  <th class="num">Sale price</th>
                  <th class="num">Reserve</th>
                  <th>Met</th>
                  <th class="num">Buyer's premium</th>
                  <th class="num">Tax</th>
                  <th class="num">Total collected</th>
                </tr>
              </thead>
              <tbody>
                {''.join(sold_rows) if sold_rows else '<tr><td colspan="10">None</td></tr>'}
                <tr class="subtotal">
                  <td colspan="4"><strong>Sold subtotal</strong></td>
                  <td class="num"><strong>{_money(t['sale'])}</strong></td>
                  <td></td>
                  <td></td>
                  <td class="num"><strong>{_money(t['premium'])}</strong></td>
                  <td class="num"><strong>{_money(t['tax'])}</strong></td>
                  <td class="num"><strong>{_money(t['total'])}</strong></td>
                </tr>
              </tbody>
            </table>
            {'<h3>Items not sold</h3><table><thead><tr><th>SKU</th><th>Item</th><th>Category</th><th>Status</th><th class="num">Sale price</th><th class="num">Reserve</th><th>Met</th><th class="num">Premium</th><th class="num">Tax</th><th class="num">Total</th></tr></thead><tbody>' + ''.join(unsold_rows) + '</tbody></table>' if unsold_rows else ''}"""
        )

    top = max((s for s in sections if s[4]["total"] > 0), key=lambda s: s[5]["sale"], default=None)
    highlight = ""
    if top:
        highlight = f"""
        <div class="highlight">
          <strong>Top SKU group:</strong> {top[0]} ({top[1]}) —
          {top[4]['sold']} sold / {top[4]['not_sold']} not sold —
          {_money(top[5]['sale'])} in sale price.
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>End of Sale Report</title>
  <style>{STYLES}</style>
</head>
<body>
  <h1>End of Sale Report</h1>
  <p class="subtitle">{meta['title']}</p>
  <p class="meta">
    Sale period: {meta['start']} — {meta['end']}<br>
    Report date: {datetime.now().strftime("%B %d, %Y")}<br>
    Items listed: {meta['total_listed']} &nbsp;|&nbsp;
    Sold: {meta['total_sold']} &nbsp;|&nbsp;
    Unsold / pending: {meta['total_not_sold']}
  </p>

  <div class="cards">
    <div class="card"><div class="label">Items sold</div><div class="value">{grand_counts['sold']}</div></div>
    <div class="card"><div class="label">Items not sold</div><div class="value">{grand_counts['not_sold']}</div></div>
    <div class="card"><div class="label">Total sale price</div><div class="value">{_money(grand['sale'])}</div></div>
    <div class="card"><div class="label">Buyer's premium</div><div class="value">{_money(grand['premium'])}</div></div>
    <div class="card"><div class="label">Tax</div><div class="value">{_money(grand['tax'])}</div></div>
    <div class="card"><div class="label">Total collected</div><div class="value">{_money(grand['total'])}</div></div>
  </div>

  {highlight}

  <h2>Summary by SKU group</h2>
  <table>
    <thead>
      <tr>
        <th>Code</th>
        <th>Description</th>
        <th class="num">Sold</th>
        <th class="num">Not sold</th>
        <th class="num">Total</th>
        <th class="num">Sale price</th>
        <th class="num">Buyer's premium</th>
        <th class="num">Tax</th>
        <th class="num">% of total</th>
        <th class="num">Total collected</th>
      </tr>
    </thead>
    <tbody>{''.join(summary_rows)}</tbody>
    <tfoot>
      <tr>
        <td colspan="2">Grand total</td>
        <td class="num">{grand_counts['sold']}</td>
        <td class="num">{grand_counts['not_sold']}</td>
        <td class="num">{grand_counts['total']}</td>
        <td class="num">{_money(grand['sale'])}</td>
        <td class="num">{_money(grand['premium'])}</td>
        <td class="num">{_money(grand['tax'])}</td>
        <td class="num">100%</td>
        <td class="num">{_money(grand['total'])}</td>
      </tr>
    </tfoot>
  </table>

  <h2>Item detail by SKU group</h2>
  {''.join(detail_blocks)}

  <p class="footer">
    Prepared from CTBids sale history export. Sale price is the winning bid (hammer price).
    Total collected includes sale price, buyer&rsquo;s premium, and applicable tax.
    SKU groups: NK = coins/collectibles, SA = estate &amp; household, Z = general merchandise.
    <br />{COPYRIGHT_LINE}
  </p>
</body>
</html>"""
    out_path.write_text(html, encoding="utf-8")


def build_excel(
    sections: list[tuple[str, str, pd.DataFrame, pd.DataFrame, dict, dict]],
    meta: dict[str, str],
    grand: dict[str, float],
    grand_counts: dict[str, int],
    out_path: Path,
) -> None:
    summary_rows = []
    for code, label, _, _, counts, t in sections:
        if counts["total"] == 0:
            continue
        summary_rows.append(
            {
                "SKU Group": code,
                "Description": label,
                "Items Sold": counts["sold"],
                "Items Not Sold": counts["not_sold"],
                "Total Items": counts["total"],
                "Sale Price": t["sale"],
                "Buyer's Premium": t["premium"],
                "Tax": t["tax"],
                "Total Collected": t["total"],
            }
        )
    summary_df = pd.DataFrame(summary_rows)
    summary_df.loc[len(summary_df)] = {
        "SKU Group": "TOTAL",
        "Description": "",
        "Items Sold": grand_counts["sold"],
        "Items Not Sold": grand_counts["not_sold"],
        "Total Items": grand_counts["total"],
        "Sale Price": grand["sale"],
        "Buyer's Premium": grand["premium"],
        "Tax": grand["tax"],
        "Total Collected": grand["total"],
    }

    cover = pd.DataFrame(
        [
            ["End of Sale Report", ""],
            ["Sale", meta["title"]],
            ["Sale start", meta["start"]],
            ["Sale end", meta["end"]],
            ["Report date", datetime.now().strftime("%B %d, %Y")],
            ["Items sold (SKU groups)", str(grand_counts["sold"])],
            ["Items not sold (SKU groups)", str(grand_counts["not_sold"])],
            ["Total items (SKU groups)", str(grand_counts["total"])],
            ["Total sale price", grand["sale"]],
            ["Buyer's premium", grand["premium"]],
            ["Tax", grand["tax"]],
            ["Total collected", grand["total"]],
        ],
        columns=["Field", "Value"],
    )

    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        cover.to_excel(writer, sheet_name="Report Info", index=False)
        summary_df.to_excel(writer, sheet_name="Summary", index=False)
        for code, label, sold_df, not_sold_df, counts, _ in sections:
            if counts["total"] == 0:
                continue
            sheet = code[:31]
            combined = pd.concat([sold_df, not_sold_df], ignore_index=True)
            export_cols = [
                "SKU", "Item Name", "Category Name", "Sale Price",
                "Reserve Price", "Met Reserve",
                "Buyers Premium", "Tax", "Total Collected", "Status",
            ]
            export = combined[[c for c in export_cols if c in combined.columns]].copy()
            export.to_excel(writer, sheet_name=sheet, index=False)


def build(
    data_dir: Path,
    source_csv: Path | None = None,
    prefix_labels: dict[str, str] | None = None,
    out_dir: Path | None = None,
) -> list[Path]:
    source = source_csv or data_dir / "sale-history-report (1).csv"
    labels = prefix_labels or PREFIX_FILES
    out_dir = out_dir or data_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = _sale_meta(source)

    sections: list[tuple[str, str, pd.DataFrame, pd.DataFrame, dict, dict]] = []
    grand = {"sale": 0.0, "premium": 0.0, "tax": 0.0, "total": 0.0}
    grand_counts = {"sold": 0, "not_sold": 0, "total": 0}

    for code, label in labels.items():
        sold_df, not_sold_df, counts = _load_prefix(data_dir / f"SKU_{code}.csv")
        t = _totals(sold_df)
        sections.append((code, label, sold_df, not_sold_df, counts, t))
        for k in grand:
            grand[k] += t[k]
        for k in grand_counts:
            grand_counts[k] += counts[k]

    outputs: list[Path] = []
    html_path = out_dir / "End_of_Sale_Report.html"
    build_html(sections, meta, grand, grand_counts, html_path)
    outputs.append(html_path)

    client_dir = out_dir / "client_reports"
    client_dir.mkdir(exist_ok=True)
    for code, label, sold_df, not_sold_df, counts, t in sections:
        if counts["total"] == 0:
            continue
        client_path = client_dir / f"End_of_Sale_Report_{code}.html"
        build_client_html(code, label, sold_df, not_sold_df, counts, t, meta, client_path)
        outputs.append(client_path)

    xlsx_path = out_dir / "End_of_Sale_Report.xlsx"
    try:
        build_excel(sections, meta, grand, grand_counts, xlsx_path)
        outputs.append(xlsx_path)
    except ImportError:
        pass

    return outputs


def main() -> None:
    parser = argparse.ArgumentParser(description="Build end-of-sale client report")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--source", type=Path, default=None)
    args = parser.parse_args()

    paths = build(args.data_dir, args.source)
    print("Reports created:")
    for p in paths:
        print(f"  {p}")
    client_dir = args.data_dir / "client_reports"
    if client_dir.exists():
        print(f"\nPer-client HTML files in: {client_dir}")


if __name__ == "__main__":
    main()
