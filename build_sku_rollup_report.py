"""SKU-prefix sold / unsold rollup across CT Seller sales. Hammer only — no premium."""
from __future__ import annotations

import csv
import re
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

from brand import COPYRIGHT_LINE

STYLES = """
    * { box-sizing: border-box; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
    body {
      font-family: "Segoe UI", Calibri, Arial, sans-serif;
      color: #1a1a1a;
      max-width: 980px;
      margin: 36px auto;
      padding: 0 28px 56px;
      line-height: 1.45;
    }
    h1 { font-size: 1.7rem; color: #1b4332; margin-bottom: 0.2rem; }
    h2 { font-size: 1.12rem; color: #2d6a4f; margin-top: 32px; border-bottom: 2px solid #d8f3dc; padding-bottom: 6px; }
    .subtitle { color: #555; margin: 0 0 8px; }
    .meta { color: #666; font-size: 0.9rem; margin-bottom: 22px; }
    .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 22px; }
    .card { background: #f1faee; border: 1px solid #b7e4c7; border-radius: 8px; padding: 12px 14px; }
    .card .label { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.05em; color: #52796f; }
    .card .value { font-size: 1.28rem; font-weight: 700; color: #1b4332; margin-top: 4px; }
    .tally-bar { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 18px; }
    .pill { background: #e8f5e9; color: #1b4332; padding: 4px 10px; border-radius: 999px; font-size: 0.85rem; }
    .pill.sold { background: #d1e7dd; color: #0f5132; }
    .pill.unsold { background: #f8d7da; color: #842029; }
    .pill.refunded { background: #fff3cd; color: #664d03; }
    table { width: 100%; border-collapse: collapse; font-size: 0.86rem; margin-bottom: 8px; }
    th { background: #1b4332; color: #fff; text-align: left; padding: 8px 10px; }
    th.num, td.num { text-align: right; }
    td { padding: 7px 10px; border-bottom: 1px solid #e6eee8; vertical-align: top; }
    tr.sold td { background: #f4faf5; }
    tr.refunded td { background: #fffbeb; }
    tr.unsold td { color: #555; }
    .muted { color: #888; }
    .footer { margin-top: 28px; color: #777; font-size: 0.82rem; }
"""


def _esc(value: object) -> str:
    return escape(str(value or ""), quote=True)


def _money(amount: float | None) -> str:
    if amount is None:
        return "—"
    try:
        n = float(amount)
    except (TypeError, ValueError):
        return "—"
    if n <= 0:
        return "—"
    return f"${n:,.2f}"


def _num(row: dict[str, Any], *keys: str) -> float:
    for key in keys:
        raw = row.get(key)
        if raw is None or str(raw).strip() in ("", "None", "nan"):
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return 0.0


_REFUNDED_STATUSES = frozenset(
    {
        "refunded",
        "returned",
        "returned & refunded",
        "returned and refunded",
        "r+rn",
        "r+rn refunded",
    }
)


def _is_refunded(status: object) -> bool:
    return str(status or "").strip().lower() in _REFUNDED_STATUSES


def _is_sold(status: object) -> bool:
    text = str(status or "").strip().lower()
    return text == "sold" and text not in _REFUNDED_STATUSES


def _row_kind(row: dict[str, Any]) -> str:
    status = row.get("itemstatus") or row.get("Status")
    if _is_refunded(status):
        return "refunded"
    if _is_sold(status):
        return "sold"
    return "unsold"


def mark_rows_refunded(rows: list[dict[str, Any]], skus: list[str] | set[str]) -> int:
    """Move matching sold lots to Returned & refunded. Leaves unsold lots alone."""
    want = {str(s).strip().upper() for s in skus if str(s).strip()}
    marked = 0
    for row in rows:
        sku = str(row.get("sku") or row.get("SKU") or "").strip().upper()
        if sku not in want or _row_kind(row) != "sold":
            continue
        row["itemstatus"] = "Returned & refunded"
        marked += 1
    return marked


def _sale_closed_text(row: dict[str, Any]) -> str:
    """Sale end/close date from CT Seller (endtime) or a sale-history CSV."""
    raw = ""
    for key in ("endtime", "saleenddate", "Sale End Date", "Sale Closed", "closetime"):
        val = row.get(key)
        if val is None:
            continue
        text = str(val).strip()
        if text and text.lower() not in ("none", "nan", "nat"):
            raw = text
            break
    if not raw:
        return ""
    if re.fullmatch(r"[A-Za-z]{3} \d{1,2}, \d{4}", raw):
        return raw
    cleaned = raw.replace("T", " ").replace(" PDT", "").replace(" PST", "").strip()
    attempts = (
        (cleaned[:19], "%Y-%m-%d %H:%M:%S"),
        (cleaned[:10], "%Y-%m-%d"),
        (cleaned, "%m/%d/%Y %I:%M %p"),
        (cleaned[:10], "%m/%d/%Y"),
    )
    for snippet, fmt in attempts:
        try:
            dt = datetime.strptime(snippet.strip(), fmt)
            return dt.strftime("%b %d, %Y").replace(" 0", " ")
        except ValueError:
            continue
    return cleaned[:10]


_LEADING_LETTERS = re.compile(r"^([A-Za-z]+)")
_PREFIX_TOKEN = re.compile(r"^[A-Z0-9]{1,12}$")


def parse_sku_prefixes(text: str | list[str] | None) -> list[str]:
    """Split typed prefixes: 'MS, NK SA' → ['MS', 'NK', 'SA']."""
    if isinstance(text, (list, tuple)):
        raw = " ".join(str(p) for p in text)
    else:
        raw = str(text or "")
    seen: set[str] = set()
    out: list[str] = []
    for part in re.split(r"[\s,;]+", raw.upper()):
        token = part.strip()
        if not token or not _PREFIX_TOKEN.fullmatch(token) or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def sku_letter_prefix(sku: object) -> str:
    match = _LEADING_LETTERS.match(str(sku or "").strip())
    return match.group(1).upper() if match else ""


def discover_prefixes_from_rows(rows: list[dict[str, Any]]) -> list[str]:
    counts: dict[str, int] = {}
    for row in rows:
        prefix = sku_letter_prefix(row.get("sku") or row.get("SKU"))
        if prefix:
            counts[prefix] = counts.get(prefix, 0) + 1
    return sorted(counts, key=lambda p: (-counts[p], p))


def filter_rows_by_prefix(rows: list[dict[str, Any]], prefix: str) -> list[dict[str, Any]]:
    want = (prefix or "").strip().upper()
    if not want:
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        sku = str(row.get("sku") or row.get("SKU") or "").strip()
        if sku.upper().startswith(want):
            out.append(row)
    return out


def _item_rows(rows: list[dict[str, Any]], *, kind: str) -> str:
    bits: list[str] = []
    show_price = kind in ("sold", "refunded")
    for row in rows:
        sku = str(row.get("sku") or "").strip() or "—"
        title = str(row.get("itemtitle") or row.get("title") or "").strip()
        sale = str(row.get("saletitle") or row.get("Sale Title") or "").strip() or f"Sale {row.get('saleid') or ''}"
        closed = _sale_closed_text(row)
        if kind == "refunded":
            status = str(row.get("itemstatus") or "").strip() or "Returned & refunded"
        elif kind == "sold":
            status = str(row.get("itemstatus") or "").strip() or "Sold"
        else:
            status = str(row.get("itemstatus") or "").strip() or "Not Sold"
        price = _num(row, "saleprice", "soldprice", "itemsoldprice") if show_price else None
        bits.append(
            "<tr class=\"" + kind + "\">"
            f"<td>{_esc(sku)}</td>"
            f"<td>{_esc(title)}</td>"
            f"<td>{_esc(sale)}</td>"
            f"<td>{_esc(closed) if closed else '<span class=\"muted\">—</span>'}</td>"
            f"<td>{_esc(status)}</td>"
            f"<td class=\"num\">{_money(price) if show_price else '<span class=\"muted\">—</span>'}</td>"
            "</tr>"
        )
    if not bits:
        bits.append(
            '<tr><td colspan="6" class="muted">None</td></tr>'
        )
    return "\n".join(bits)


def build_html(
    rows: list[dict[str, Any]],
    *,
    prefix: str,
    business_name: str = "",
    sale_count: int = 0,
    heading: str | None = None,
) -> str:
    prefix = (prefix or "").strip().upper()
    title_text = (heading or f"SKU {prefix} — sold and unsold").strip()
    lot_label = "lots" if prefix == "ALL" else f"{prefix} lots"
    sold = [r for r in rows if _row_kind(r) == "sold"]
    refunded = [r for r in rows if _row_kind(r) == "refunded"]
    unsold = [r for r in rows if _row_kind(r) == "unsold"]
    sold.sort(
        key=lambda r: (-_num(r, "saleprice", "soldprice"), str(r.get("sku") or "")),
    )
    refunded.sort(
        key=lambda r: (-_num(r, "saleprice", "soldprice"), str(r.get("sku") or "")),
    )
    unsold.sort(key=lambda r: str(r.get("sku") or "").upper())
    hammer = sum(_num(r, "saleprice", "soldprice") for r in sold)
    refunded_amt = sum(_num(r, "saleprice", "soldprice") for r in refunded)
    sell_rate = f"{100 * len(sold) / len(rows):.0f}%" if rows else "0%"
    stamp = datetime.now().strftime("%B %d, %Y · %I:%M %p").lstrip("0").replace(" 0", " ")
    biz = (business_name or "").strip()
    refund_meta = ""
    refund_pills = ""
    refund_cards = ""
    refund_table = ""
    if refunded:
        refund_meta = (
            f"<br />Returned &amp; refunded lots are removed from sold hammer "
            f"({len(refunded)} lot(s), {_esc(_money(refunded_amt))})."
        )
        refund_pills = (
            f'<span class="pill refunded"><strong>{len(refunded)}</strong> returned &amp; refunded</span>'
        )
        refund_cards = f"""
    <div class="card"><div class="label">Returned &amp; refunded</div><div class="value">{_money(refunded_amt)}</div></div>
    <div class="card"><div class="label">Refunded lots</div><div class="value">{len(refunded)}</div></div>"""
        refund_table = f"""
  <h2>Returned &amp; refunded</h2>
  <table>
    <thead><tr><th>SKU</th><th>Item</th><th>Sale</th><th>Closed</th><th>Status</th><th class="num">Sale price</th></tr></thead>
    <tbody>
      {_item_rows(refunded, kind="refunded")}
    </tbody>
  </table>"""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>{_esc(title_text)}</title>
  <style>{STYLES}</style>
</head>
<body>
  <h1>{_esc(title_text)}</h1>
  <p class="subtitle">{_esc(biz) if biz else "CT Seller sales on this login"}</p>
  <p class="meta">
    {sale_count} sale(s) pulled from CT Seller · Report date: {_esc(stamp)}<br />
    Sale price is the hammer (winning bid) only — buyer&rsquo;s premium and tax are omitted.
    Unsold lots show no price.{refund_meta}
  </p>
  <div class="tally-bar">
    <span class="pill sold"><strong>{len(sold)}</strong> sold</span>
    {refund_pills}
    <span class="pill unsold"><strong>{len(unsold)}</strong> not sold</span>
    <span class="pill"><strong>{len(rows)}</strong> {_esc(lot_label)}</span>
    <span class="pill">Sell-through <strong>{sell_rate}</strong></span>
  </div>
  <div class="cards">
    <div class="card"><div class="label">Sold hammer</div><div class="value">{_money(hammer)}</div></div>
    <div class="card"><div class="label">Sold lots</div><div class="value">{len(sold)}</div></div>
    {refund_cards}
    <div class="card"><div class="label">Not sold</div><div class="value">{len(unsold)}</div></div>
  </div>
  <h2>Sold</h2>
  <table>
    <thead><tr><th>SKU</th><th>Item</th><th>Sale</th><th>Closed</th><th>Status</th><th class="num">Sale price</th></tr></thead>
    <tbody>
      {_item_rows(sold, kind="sold")}
    </tbody>
  </table>
  {refund_table}
  <h2>Not sold</h2>
  <table>
    <thead><tr><th>SKU</th><th>Item</th><th>Sale</th><th>Closed</th><th>Status</th><th class="num">Sale price</th></tr></thead>
    <tbody>
      {_item_rows(unsold, kind="unsold")}
    </tbody>
  </table>
  <p class="footer">
    Source: CT Seller sale-history report for this login&rsquo;s sales. No buyer&rsquo;s premium.
    <br />{_esc(COPYRIGHT_LINE)}
  </p>
</body>
</html>
"""


def write_csv(rows: list[dict[str, Any]], path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["SKU", "Item Name", "Status", "Sale Price", "Sale Title", "Sale Closed", "Sale ID"])
        for row in rows:
            kind = _row_kind(row)
            show_price = kind in ("sold", "refunded")
            price = _num(row, "saleprice", "soldprice") if show_price else 0.0
            writer.writerow(
                [
                    str(row.get("sku") or "").strip(),
                    str(row.get("itemtitle") or "").strip(),
                    str(row.get("itemstatus") or "").strip(),
                    f"{price:.2f}" if show_price and price > 0 else "",
                    str(row.get("saletitle") or "").strip(),
                    _sale_closed_text(row),
                    row.get("saleid") or "",
                ]
            )
    return path


def _csv_num(row: dict[str, Any], *keys: str) -> float:
    return _num(row, *keys)


def write_sale_history_csv(rows: list[dict[str, Any]], path: Path) -> Path:
    """Write CTBids-shaped sale-history CSV so the owner master report can run."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "Item Name",
                "Type",
                "SKU",
                "Category Name",
                "Invoice Number",
                "Invoice Status",
                "Item ID",
                "Winner First Name",
                "Winner Last Name",
                "Winner Username",
                "Winner Email",
                "Buyers Premium",
                "Tax Buyers Premium",
                "Status",
                "Sale Title",
                "Sale Start Date",
                "Sale End Date",
                "Item Close Time",
                "Reserve Price",
                "Number of Views",
                "Sale Price",
                "Tax",
                "Number of Bids",
                "Taxexempt",
                "Item Link",
                "Item Shippable",
            ]
        )
        for row in rows:
            sold = _is_sold(row.get("itemstatus"))
            sale_id = row.get("saleid") or ""
            item_id = row.get("itemid") or ""
            link = ""
            if sale_id and item_id:
                link = f"https://ctbids.com/estate-sale/{sale_id}/item/{item_id}"
            hammer = _csv_num(row, "saleprice") if sold else 0.0
            premium = _csv_num(row, "buyerspremium", "updatedBuyersPermium") if sold else 0.0
            tax = _csv_num(row, "itemtax", "updatedtax", "tax") if sold else 0.0
            tax_bp = _csv_num(row, "taxonbpamount") if sold else 0.0
            reserve = _csv_num(row, "reserveprice")
            writer.writerow(
                [
                    str(row.get("itemtitle") or "").strip(),
                    str(row.get("itemtype") or "Auction").strip() or "Auction",
                    str(row.get("sku") or "").strip(),
                    str(row.get("category") or "").strip(),
                    str(row.get("invoicenumber") or "").strip(),
                    str(row.get("invoicestatus") or "").strip(),
                    item_id,
                    str(row.get("firstname") or "").strip(),
                    str(row.get("lastname") or "").strip(),
                    str(row.get("nickname") or "").strip(),
                    str(row.get("email") or "").strip(),
                    f"{premium:.2f}" if sold else "0",
                    f"{tax_bp:.2f}" if sold else "0",
                    str(row.get("itemstatus") or "").strip(),
                    str(row.get("saletitle") or "").strip(),
                    str(row.get("starttime") or "").strip(),
                    str(row.get("endtime") or "").strip(),
                    str(row.get("closetime") or "").strip(),
                    f"{reserve:.2f}" if reserve else "0",
                    row.get("viewcount") or "",
                    f"{hammer:.2f}" if sold else "0",
                    f"{tax:.2f}" if sold else "0",
                    row.get("bidcount") or "",
                    "Yes" if row.get("taxexempt") in (True, 1, "1", "Yes", "yes") else "No",
                    link,
                    "Yes" if row.get("isshippable") in (True, 1, "1", "Yes", "yes") else "No",
                ]
            )
    return path


def build_index_html(
    *,
    prefixes: list[str],
    business_name: str = "",
    sale_count: int = 0,
    sale_titles: list[str] | None = None,
) -> str:
    biz = (business_name or "").strip()
    stamp = datetime.now().strftime("%B %d, %Y · %I:%M %p").lstrip("0").replace(" 0", " ")
    links = "\n".join(
        f'<li><a href="SKU_{_esc(p)}_sold_unsold.html">SKU {_esc(p)}</a>'
        f' &nbsp;<span class="muted">PDF / CSV in this folder</span></li>'
        for p in prefixes
    )
    sales_bit = ""
    titles = [t for t in (sale_titles or []) if str(t).strip()]
    if titles:
        shown = titles[:12]
        extra = f" and {len(titles) - 12} more" if len(titles) > 12 else ""
        sales_bit = "<br />Sales: " + _esc(", ".join(shown) + extra)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>SKU reports from CT Seller</title>
  <style>{STYLES}</style>
</head>
<body>
  <h1>SKU reports from CT Seller</h1>
  <p class="subtitle">{_esc(biz) if biz else "CT Seller sales on this login"}</p>
  <p class="meta">
    {sale_count} sale(s) · {len(prefixes)} SKU prefix report(s) · {_esc(stamp)}
    {sales_bit}<br />
    Sale price is hammer only — no buyer&rsquo;s premium.
  </p>
  <h2>Client reports</h2>
  <ul>
    {links or '<li class="muted">None</li>'}
  </ul>
  <p class="footer">{_esc(COPYRIGHT_LINE)}</p>
</body>
</html>
"""
