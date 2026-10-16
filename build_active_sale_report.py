"""Build a boss-facing summary from a CTBids active-sale CSV export."""
from __future__ import annotations

import argparse
import re
from datetime import datetime
from pathlib import Path

import pandas as pd

from brand import COPYRIGHT_LINE

REQUIRED_COLUMNS = [
    "SKU",
    "Item Name",
    "Sale Price",
    "Status",
    "Sale Title",
    "Reserve Price",
    "Number of Views",
    "Number of Bids",
    "Category Name",
]

_LEADING_LETTERS = re.compile(r"^([A-Za-z]+)")

STYLES = """
    * {
      box-sizing: border-box;
      -webkit-print-color-adjust: exact;
      print-color-adjust: exact;
    }
    body {
      font-family: "Segoe UI", Calibri, Arial, sans-serif;
      color: #1a1a1a;
      max-width: 980px;
      margin: 36px auto;
      padding: 0 28px 56px;
      line-height: 1.5;
    }
    h1 { font-size: 1.85rem; color: #1b4332; margin-bottom: 0.2rem; }
    h2 { font-size: 1.15rem; color: #2d6a4f; margin-top: 36px; border-bottom: 2px solid #d8f3dc; padding-bottom: 6px; }
    .badge {
      display: inline-block;
      background: #0d6efd;
      color: #fff;
      font-size: 0.78rem;
      font-weight: 600;
      letter-spacing: 0.05em;
      text-transform: uppercase;
      padding: 4px 12px;
      border-radius: 4px;
      margin-bottom: 12px;
    }
    .subtitle { color: #555; margin: 0 0 8px; font-size: 1.05rem; }
    .meta { color: #666; font-size: 0.9rem; margin-bottom: 28px; }
    .cards {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 12px;
      margin-bottom: 24px;
    }
    .card {
      background: #f1faee;
      border: 1px solid #b7e4c7;
      border-radius: 8px;
      padding: 12px 14px;
    }
    .card.warn { background: #fff3cd; border-color: #ffc107; }
    .card .label { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.05em; color: #52796f; }
    .card.warn .label { color: #856404; }
    .card .value { font-size: 1.25rem; font-weight: 700; color: #1b4332; margin-top: 4px; }
    .highlight {
      background: #fff3cd;
      border-left: 4px solid #ffc107;
      padding: 14px 18px;
      margin-bottom: 28px;
      border-radius: 0 8px 8px 0;
    }
    table { width: 100%; border-collapse: collapse; font-size: 0.88rem; margin-bottom: 8px; }
    th { background: #1b4332; color: #fff; text-align: left; padding: 8px 10px; }
    th.num, td.num { text-align: right; }
    td { padding: 7px 10px; border-bottom: 1px solid #e9ecef; vertical-align: top; }
    tr:nth-child(even) td { background: #fafafa; }
    tfoot td { font-weight: 700; background: #b7e4c7; border-top: 2px solid #1b4332; }
    .footer { margin-top: 40px; font-size: 0.82rem; color: #888; border-top: 1px solid #ddd; padding-top: 16px; }
    @media print {
      body { margin: 16px; max-width: none; }
      tr { page-break-inside: avoid; }
      thead { display: table-header-group; }
    }
"""


def _money(val: float) -> str:
    return f"${val:,.2f}"


def _met_label(sale: float, reserve: float) -> str:
    if reserve <= 0:
        return "—"
    return "Yes" if sale >= reserve else "No"


def _safe_num(series: object, index: pd.Index | None = None) -> pd.Series:
    """Coerce a column to numeric; never crash on missing/scalar/duplicate columns."""
    if isinstance(series, pd.DataFrame):
        # Duplicate CSV headers can yield a DataFrame — use the first column
        series = series.iloc[:, 0]
    if isinstance(series, pd.Series):
        return pd.to_numeric(series, errors="coerce").fillna(0)
    if index is None:
        # Last-resort scalar — caller should pass index via _col_num
        return pd.Series(dtype="float64")
    try:
        default = float(series or 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        default = 0.0
    return pd.Series(default, index=index, dtype="float64")


def _col_num(work: pd.DataFrame, name: str, default: float = 0) -> pd.Series:
    if name in work.columns:
        return _safe_num(work[name], index=work.index)
    return pd.Series(float(default), index=work.index, dtype="float64")


def _winner_name(row: pd.Series) -> str:
    first = str(row.get("Winner First Name", "") or "").strip()
    last = str(row.get("Winner Last Name", "") or "").strip()
    name = f"{first} {last}".strip()
    if name:
        return name
    user = str(row.get("Winner Username", "") or "").strip()
    return user or "—"


def analyze(csv_path: Path) -> dict:
    """Compute summary stats from an active-sale CSV."""
    df = pd.read_csv(csv_path)
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            "This doesn't look like a CTBids active-sale export.\n\n"
            "Missing columns: " + ", ".join(missing)
        )

    work = df.copy()
    work["Sale Price"] = _col_num(work, "Sale Price")
    work["Reserve Price"] = _col_num(work, "Reserve Price")
    work["Number of Views"] = _col_num(work, "Number of Views").astype(int)
    work["Number of Bids"] = _col_num(work, "Number of Bids").astype(int)
    work["Number of Watchers"] = _col_num(work, "Number of Watchers").astype(int)
    work["Buyers Premium"] = _col_num(work, "Buyers Premium")
    work["Tax"] = _col_num(work, "Tax")

    has_winner = work["Sale Price"] > 0
    if "Winner Username" in work.columns:
        has_winner = has_winner | work["Winner Username"].notna()

    bidding = work[has_winner].copy()
    open_items = work[~has_winner].copy()

    reserve_set = work["Reserve Price"] > 0
    meeting_reserve = has_winner & reserve_set & (work["Sale Price"] >= work["Reserve Price"])
    below_reserve = has_winner & reserve_set & (work["Sale Price"] < work["Reserve Price"])

    suspended = pd.Series(False, index=work.index)
    if "Suspended" in work.columns:
        suspended = work["Suspended"].astype(str).str.strip().str.lower().eq("yes")

    prefixes = (
        work["SKU"].astype(str).str.extract(_LEADING_LETTERS, expand=False).fillna("?").str.upper()
    )
    work["_prefix"] = prefixes

    by_prefix_rows = []
    for code, group in work.groupby("_prefix", sort=False):
        g_bid = group[group["Sale Price"] > 0]
        by_prefix_rows.append(
            {
                "code": code,
                "total": len(group),
                "bidding": len(g_bid),
                "open": len(group) - len(g_bid),
                "sale": float(g_bid["Sale Price"].sum()),
                "bids": int(group["Number of Bids"].sum()),
                "views": int(group["Number of Views"].sum()),
            }
        )
    by_prefix_rows.sort(key=lambda r: (-r["sale"], -r["total"], r["code"]))

    top_items = (
        bidding.sort_values("Sale Price", ascending=False)
        .head(15)[
            [
                "SKU", "Item Name", "Category Name", "Sale Price",
                "Reserve Price", "Number of Bids", "Number of Views",
            ]
        ]
        .copy()
    )

    buyer_rows = []
    if not bidding.empty and "Winner Username" in bidding.columns:
        tmp = bidding.copy()
        tmp["_buyer"] = tmp.apply(_winner_name, axis=1)
        tmp = tmp[tmp["_buyer"] != "—"]
        for buyer, group in tmp.groupby("_buyer"):
            buyer_rows.append(
                {
                    "buyer": buyer,
                    "items": len(group),
                    "sale": float(group["Sale Price"].sum()),
                    "bids": int(group["Number of Bids"].sum()),
                }
            )
        buyer_rows.sort(key=lambda r: (-r["sale"], -r["items"], r["buyer"]))
        buyer_rows = buyer_rows[:15]

    if "Category Name" in work.columns:
        cat_counts = work["Category Name"].fillna("(none)").astype(str).value_counts().head(12)
    else:
        cat_counts = pd.Series(dtype="int64")

    needs_attention = open_items.sort_values(
        ["Number of Views", "Number of Watchers"], ascending=False
    ).head(12)[["SKU", "Item Name", "Category Name", "Reserve Price", "Number of Views", "Number of Watchers"]]

    title = str(work.iloc[0].get("Sale Title", "Active Sale")).strip() or "Active Sale"
    start = str(work.iloc[0].get("Start Date", "") or "")
    end = str(work.iloc[0].get("End Date", "") or "")

    unique_bidders = 0
    if "Winner Username" in bidding.columns:
        unique_bidders = int(bidding["Winner Username"].dropna().nunique())

    return {
        "title": title,
        "start": start,
        "end": end,
        "total_items": len(work),
        "bidding_count": int(has_winner.sum()),
        "open_count": int((~has_winner).sum()),
        "total_sale": float(bidding["Sale Price"].sum()) if not bidding.empty else 0.0,
        "total_premium": float(work["Buyers Premium"].sum()),
        "total_tax": float(work["Tax"].sum()),
        "total_bids": int(work["Number of Bids"].sum()),
        "total_views": int(work["Number of Views"].sum()),
        "unique_bidders": unique_bidders,
        "reserve_set": int(reserve_set.sum()),
        "meeting_reserve": int(meeting_reserve.sum()),
        "below_reserve": int(below_reserve.sum()),
        "suspended": int(suspended.sum()),
        "by_prefix": by_prefix_rows,
        "top_items": top_items,
        "top_buyers": buyer_rows,
        "categories": cat_counts,
        "needs_attention": needs_attention,
        "status_counts": work["Status"].astype(str).value_counts().to_dict(),
    }


def build_html(stats: dict, out_path: Path) -> Path:
    sell_rate = (
        f"{100 * stats['bidding_count'] / stats['total_items']:.0f}%"
        if stats["total_items"]
        else "0%"
    )

    prefix_rows = []
    for r in stats["by_prefix"]:
        prefix_rows.append(
            f"""<tr>
              <td><strong>{r['code']}</strong></td>
              <td class="num">{r['total']}</td>
              <td class="num">{r['bidding']}</td>
              <td class="num">{r['open']}</td>
              <td class="num">{_money(r['sale'])}</td>
              <td class="num">{r['bids']}</td>
              <td class="num">{r['views']}</td>
            </tr>"""
        )

    top_item_rows = []
    for _, row in stats["top_items"].iterrows():
        sale = float(row["Sale Price"])
        reserve = float(row["Reserve Price"])
        reserve_cell = _money(reserve) if reserve > 0 else "—"
        met = _met_label(sale, reserve)
        top_item_rows.append(
            f"""<tr>
              <td>{row['SKU']}</td>
              <td>{row['Item Name']}</td>
              <td>{row['Category Name']}</td>
              <td class="num">{_money(sale)}</td>
              <td class="num">{reserve_cell}</td>
              <td>{met}</td>
              <td class="num">{int(row['Number of Bids'])}</td>
              <td class="num">{int(row['Number of Views'])}</td>
            </tr>"""
        )

    buyer_rows = []
    for r in stats["top_buyers"]:
        buyer_rows.append(
            f"""<tr>
              <td>{r['buyer']}</td>
              <td class="num">{r['items']}</td>
              <td class="num">{_money(r['sale'])}</td>
              <td class="num">{r['bids']}</td>
            </tr>"""
        )

    cat_rows = []
    for name, count in stats["categories"].items():
        cat_rows.append(f"<tr><td>{name}</td><td class='num'>{int(count)}</td></tr>")

    attention_rows = []
    for _, row in stats["needs_attention"].iterrows():
        attention_rows.append(
            f"""<tr>
              <td>{row['SKU']}</td>
              <td>{row['Item Name']}</td>
              <td>{row['Category Name']}</td>
              <td class="num">{_money(float(row['Reserve Price']))}</td>
              <td class="num">{int(row['Number of Views'])}</td>
              <td class="num">{int(row['Number of Watchers'])}</td>
            </tr>"""
        )

    status_bits = ", ".join(f"{k}: {v}" for k, v in stats["status_counts"].items())

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Active Sale Summary — {stats['title']}</title>
  <style>{STYLES}</style>
</head>
<body>
  <div class="badge">Active sale — boss summary</div>
  <h1>Active Sale Summary</h1>
  <p class="subtitle">{stats['title']}</p>
  <p class="meta">
    Sale period: {stats['start']} — {stats['end']}<br>
    Report date: {datetime.now().strftime("%B %d, %Y at %I:%M %p")}<br>
    Status mix: {status_bits}
  </p>

  <div class="cards">
    <div class="card"><div class="label">Items listed</div><div class="value">{stats['total_items']}</div></div>
    <div class="card"><div class="label">With bids / winner</div><div class="value">{stats['bidding_count']}</div></div>
    <div class="card warn"><div class="label">No bids yet</div><div class="value">{stats['open_count']}</div></div>
    <div class="card"><div class="label">Sell-through so far</div><div class="value">{sell_rate}</div></div>
    <div class="card"><div class="label">Current hammer total</div><div class="value">{_money(stats['total_sale'])}</div></div>
    <div class="card"><div class="label">Unique bidders</div><div class="value">{stats['unique_bidders']}</div></div>
    <div class="card"><div class="label">Total bids</div><div class="value">{stats['total_bids']}</div></div>
    <div class="card"><div class="label">Total views</div><div class="value">{stats['total_views']:,}</div></div>
    <div class="card"><div class="label">Suspended</div><div class="value">{stats['suspended']}</div></div>
    <div class="card"><div class="label">Below reserve</div><div class="value">{stats['below_reserve']}</div></div>
  </div>

  <div class="highlight">
    <strong>Snapshot:</strong> {stats['bidding_count']} of {stats['total_items']} items have a current bid
    ({sell_rate}). Hammer total so far is <strong>{_money(stats['total_sale'])}</strong>
    across <strong>{stats['unique_bidders']}</strong> unique bidders.
    {stats['open_count']} item(s) still have no bids.
    {f"{stats['below_reserve']} winning bid(s) are still below reserve." if stats['below_reserve'] else "No winning bids are below reserve."}
  </div>

  <h2>By SKU group</h2>
  <table>
    <thead>
      <tr>
        <th>Prefix</th>
        <th class="num">Listed</th>
        <th class="num">Bidding</th>
        <th class="num">Open</th>
        <th class="num">Hammer total</th>
        <th class="num">Bids</th>
        <th class="num">Views</th>
      </tr>
    </thead>
    <tbody>
      {''.join(prefix_rows) if prefix_rows else '<tr><td colspan="7">No data</td></tr>'}
    </tbody>
    <tfoot>
      <tr>
        <td>Total</td>
        <td class="num">{stats['total_items']}</td>
        <td class="num">{stats['bidding_count']}</td>
        <td class="num">{stats['open_count']}</td>
        <td class="num">{_money(stats['total_sale'])}</td>
        <td class="num">{stats['total_bids']}</td>
        <td class="num">{stats['total_views']:,}</td>
      </tr>
    </tfoot>
  </table>

  <h2>Top items by current sale price</h2>
  <table>
    <thead>
      <tr>
        <th>SKU</th><th>Item</th><th>Category</th>
        <th class="num">Sale price</th><th class="num">Reserve</th><th>Met</th>
        <th class="num">Bids</th><th class="num">Views</th>
      </tr>
    </thead>
    <tbody>
      {''.join(top_item_rows) if top_item_rows else '<tr><td colspan="8">No bids yet.</td></tr>'}
    </tbody>
  </table>

  <h2>Top bidders by spend so far</h2>
  <table>
    <thead>
      <tr>
        <th>Bidder</th>
        <th class="num">Items</th>
        <th class="num">Hammer total</th>
        <th class="num">Bids placed*</th>
      </tr>
    </thead>
    <tbody>
      {''.join(buyer_rows) if buyer_rows else '<tr><td colspan="4">No bidders yet.</td></tr>'}
    </tbody>
  </table>
  <p class="meta">*Bids placed = sum of bid counts on items they currently lead.</p>

  <h2>Top categories (by item count)</h2>
  <table>
    <thead><tr><th>Category</th><th class="num">Items</th></tr></thead>
    <tbody>
      {''.join(cat_rows) if cat_rows else '<tr><td colspan="2">None</td></tr>'}
    </tbody>
  </table>

  <h2>Needs attention — viewed / watched but no bids</h2>
  <table>
    <thead>
      <tr>
        <th>SKU</th><th>Item</th><th>Category</th>
        <th class="num">Reserve</th><th class="num">Views</th><th class="num">Watchers</th>
      </tr>
    </thead>
    <tbody>
      {''.join(attention_rows) if attention_rows else '<tr><td colspan="6">Every item has a bid — nice.</td></tr>'}
    </tbody>
  </table>

  <p class="footer">
    Prepared from a CTBids active-sale export. Figures are a mid-sale snapshot and will change
    until the sale closes. Hammer total is current winning bids only (buyer's premium / tax may
    still be $0 while the sale is open).
    <br />{COPYRIGHT_LINE}
  </p>
</body>
</html>"""
    out_path.write_text(html, encoding="utf-8")
    return out_path


def build_excel(stats: dict, out_path: Path) -> Path | None:
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        return None

    info = pd.DataFrame(
        [
            ["Sale", stats["title"]],
            ["Start", stats["start"]],
            ["End", stats["end"]],
            ["Items listed", stats["total_items"]],
            ["With bids / winner", stats["bidding_count"]],
            ["No bids yet", stats["open_count"]],
            ["Current hammer total", stats["total_sale"]],
            ["Unique bidders", stats["unique_bidders"]],
            ["Total bids", stats["total_bids"]],
            ["Total views", stats["total_views"]],
            ["Suspended", stats["suspended"]],
            ["Below reserve", stats["below_reserve"]],
            ["Report generated", datetime.now().strftime("%Y-%m-%d %H:%M")],
        ],
        columns=["Field", "Value"],
    )
    by_prefix = pd.DataFrame(stats["by_prefix"])
    if not by_prefix.empty:
        by_prefix = by_prefix.rename(
            columns={
                "code": "Prefix",
                "total": "Listed",
                "bidding": "Bidding",
                "open": "Open",
                "sale": "Hammer Total",
                "bids": "Bids",
                "views": "Views",
            }
        )
    top_items = stats["top_items"].rename(
        columns={
            "Sale Price": "Sale Price",
            "Reserve Price": "Reserve",
            "Number of Bids": "Bids",
            "Number of Views": "Views",
        }
    )
    top_buyers = pd.DataFrame(stats["top_buyers"])
    if not top_buyers.empty:
        top_buyers = top_buyers.rename(
            columns={"buyer": "Bidder", "items": "Items", "sale": "Hammer Total", "bids": "Bids"}
        )
    cats = stats["categories"].rename_axis("Category").reset_index(name="Items")
    attention = stats["needs_attention"].rename(
        columns={
            "Reserve Price": "Reserve",
            "Number of Views": "Views",
            "Number of Watchers": "Watchers",
        }
    )

    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        info.to_excel(writer, sheet_name="Summary", index=False)
        if not by_prefix.empty:
            by_prefix.to_excel(writer, sheet_name="By SKU Group", index=False)
        top_items.to_excel(writer, sheet_name="Top Items", index=False)
        if not top_buyers.empty:
            top_buyers.to_excel(writer, sheet_name="Top Bidders", index=False)
        cats.to_excel(writer, sheet_name="Categories", index=False)
        attention.to_excel(writer, sheet_name="Needs Attention", index=False)
    return out_path


def build(csv_path: Path, out_dir: Path | None = None) -> list[Path]:
    csv_path = Path(csv_path)
    out_dir = Path(out_dir) if out_dir else csv_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    stats = analyze(csv_path)
    outputs: list[Path] = []

    html_path = out_dir / "Active_Sale_Summary.html"
    build_html(stats, html_path)
    outputs.append(html_path)

    xlsx_path = out_dir / "Active_Sale_Summary.xlsx"
    if build_excel(stats, xlsx_path):
        outputs.append(xlsx_path)

    return outputs


def main() -> None:
    parser = argparse.ArgumentParser(description="Build active-sale boss summary")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args()
    paths = build(args.input, args.output_dir)
    print("Created:")
    for p in paths:
        print(f"  {p}")


if __name__ == "__main__":
    main()
