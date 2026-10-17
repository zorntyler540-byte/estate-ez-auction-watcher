"""Export a Sale Watcher snapshot to HTML + PDF (Edge/Chrome headless print)."""
from __future__ import annotations

import csv
import html
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from brand import APP_NAME, COPYRIGHT_LINE
from ctbids_client import LiveItem, Snapshot

_LEADING_LETTERS = re.compile(r"^([A-Za-z]+)")

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
    .status-badge { display: inline-block; font-size: 0.75rem; padding: 2px 8px; border-radius: 4px; }
    .sold-badge { background: #d1e7dd; color: #0f5132; }
    .unsold-badge { background: #f8d7da; color: #842029; }
    .open-badge { background: #cfe2ff; color: #084298; }
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


def find_browser() -> Path | None:
    override = os.environ.get("SALE_REPORT_BROWSER") or os.environ.get("SALE_WATCHER_BROWSER")
    if override and Path(override).exists():
        return Path(override)

    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        Path(pf86) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(pf) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(pf) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(pf86) / "Google" / "Chrome" / "Application" / "chrome.exe",
    ]
    if local:
        candidates.append(Path(local) / "Google" / "Chrome" / "Application" / "chrome.exe")
        candidates.append(Path(local) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    for path in candidates:
        if path.exists():
            return path
    for name in ("msedge", "chrome", "chromium", "chrome.exe", "msedge.exe"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def html_to_pdf(browser: Path, html_path: Path, pdf_path: Path, timeout: int = 90) -> bool:
    file_url = Path(html_path).resolve().as_uri()
    pdf_path = Path(pdf_path).resolve()
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    for headless in ("--headless=new", "--headless"):
        user_data = Path(tempfile.mkdtemp(prefix="sw_pdf_"))
        args = [
            str(browser),
            headless,
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            f"--user-data-dir={user_data}",
            f"--print-to-pdf={pdf_path}",
            file_url,
        ]
        proc = None
        try:
            if pdf_path.exists():
                pdf_path.unlink()
            proc = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=no_window,
            )
            deadline = time.time() + timeout
            while time.time() < deadline:
                if pdf_path.exists() and pdf_path.stat().st_size > 0:
                    break
                if proc.poll() is not None:
                    time.sleep(1.0)
                    if pdf_path.exists() and pdf_path.stat().st_size > 0:
                        break
                time.sleep(0.25)
        except Exception:  # noqa: BLE001
            pass
        finally:
            if proc is not None and proc.poll() is None:
                for stop in (proc.terminate, proc.kill):
                    try:
                        stop()
                        proc.wait(timeout=5)
                        break
                    except Exception:  # noqa: BLE001
                        pass
            time.sleep(0.3)
            shutil.rmtree(user_data, ignore_errors=True)
        if pdf_path.exists() and pdf_path.stat().st_size > 0:
            return True
    return False


def _sku_prefix(sku: object) -> str:
    match = _LEADING_LETTERS.match(str(sku or "").strip())
    return match.group(1).upper() if match else "?"


def _below_reserve(item: LiveItem) -> bool:
    return item.reserve_price > 0 and item.bid_price < item.reserve_price


def _item_bucket(item: LiveItem) -> str:
    if item.is_sold:
        return "sold"
    if item.is_open:
        return "open"
    return "unsold"


def _item_rows(items: list[LiveItem], *, bucket: str) -> str:
    rows = []
    for it in items:
        missed = bucket != "sold" and _below_reserve(it)
        cls = "sold" if bucket == "sold" else ("missed" if missed else "unsold")
        if bucket == "sold" and it.bought_now:
            status, badge_cls = "Bought now", "sold-badge"
        elif bucket == "sold":
            status, badge_cls = "Sold", "sold-badge"
        elif bucket == "open":
            status, badge_cls = "Open", "open-badge"
        else:
            status, badge_cls = "Not Sold", "unsold-badge"
        met = _met_reserve(it)
        if met == "Yes":
            met_html = '<span class="met-yes">Yes</span>'
        elif met == "No":
            met_html = '<span class="met-no">No</span>'
        else:
            met_html = '<span class="muted">—</span>'
        reserve_cell = _money(it.reserve_price) if it.reserve_price > 0 else "—"
        bid_cell = _money(it.bid_price) if it.bid_price > 0 else "—"
        buy_cell = _money(it.buy_now_price) if it.buy_now_price > 0 else "—"
        rows.append(
            f'<tr class="{cls}">'
            f"<td>{_esc(it.sku) or '<span class=\"muted\">—</span>'}</td>"
            f"<td>{_esc(it.title)}</td>"
            f'<td><span class="status-badge {badge_cls}">{status}</span></td>'
            f'<td class="num">{bid_cell}</td>'
            f'<td class="num">{buy_cell}</td>'
            f'<td class="num">{reserve_cell}</td>'
            f"<td>{met_html}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def _section_table(items: list[LiveItem], *, bucket: str) -> str:
    if not items:
        return ""
    if bucket == "sold":
        label = "Sold"
    elif bucket == "open":
        label = "Still open"
    else:
        label = "Not sold"
    bid_sum = sum(it.bid_price for it in items)
    buy_sum = sum(it.buy_now_price for it in items if it.buy_now_price > 0)
    reserve_sum = sum(it.reserve_price for it in items if it.reserve_price > 0)
    foot_bid = _money(bid_sum) if bid_sum else "—"
    foot_buy = _money(buy_sum) if buy_sum else "—"
    foot_res = _money(reserve_sum) if reserve_sum else "—"
    return f"""
  <h3>{label} ({len(items)})</h3>
  <table>
    <thead>
      <tr>
        <th>SKU</th>
        <th>Item</th>
        <th>Status</th>
        <th class="num">High bid</th>
        <th class="num">Buy now</th>
        <th class="num">Reserve</th>
        <th>Met reserve</th>
      </tr>
    </thead>
    <tbody>
      {_item_rows(items, bucket=bucket)}
    </tbody>
    <tfoot>
      <tr>
        <td colspan="3">{label} subtotal — {len(items)} items</td>
        <td class="num">{foot_bid}</td>
        <td class="num">{foot_buy}</td>
        <td class="num">{foot_res}</td>
        <td></td>
      </tr>
    </tfoot>
  </table>
"""


def build_html(
    snap: Snapshot,
    *,
    sale_url: str = "",
    generated_at: datetime | None = None,
) -> str:
    when = generated_at or datetime.now()
    stamp = when.strftime("%B %d, %Y · %I:%M %p").lstrip("0").replace(" 0", " ")
    items = list(snap.items.values())
    sale_link = sale_url.strip() or f"https://ctbids.com/estate-sale/{snap.sale_id}"

    sold = [it for it in items if _item_bucket(it) == "sold"]
    not_sold = [it for it in items if _item_bucket(it) == "unsold"]
    still_open = [it for it in items if _item_bucket(it) == "open"]
    missed = [it for it in items if not it.is_sold and _below_reserve(it)]
    missed_with_bid = [it for it in missed if it.bid_price > 0]
    closed = len(sold) + len(not_sold)
    sell_rate = f"{100 * len(sold) / closed:.0f}%" if closed else "0%"
    sold_hammer = sum(it.bid_price for it in sold)
    missed_bids = sum(it.bid_price for it in missed_with_bid)
    missed_reserves = sum(it.reserve_price for it in missed)

    by_prefix: dict[str, list[LiveItem]] = {}
    for it in items:
        by_prefix.setdefault(_sku_prefix(it.sku), []).append(it)
    prefix_order = sorted(by_prefix, key=lambda code: (code == "?", code))

    prefix_rows = []
    sections = []
    for code in prefix_order:
        group = by_prefix[code]
        g_sold = sorted(
            [it for it in group if _item_bucket(it) == "sold"],
            key=lambda it: (-it.bid_price, it.sku or "", it.title),
        )
        g_ns = sorted(
            [it for it in group if _item_bucket(it) == "unsold"],
            key=lambda it: (-it.reserve_price, it.sku or "", it.title),
        )
        g_open = sorted(
            [it for it in group if _item_bucket(it) == "open"],
            key=lambda it: (-it.bid_price, it.sku or "", it.title),
        )
        g_miss = [it for it in group if not it.is_sold and _below_reserve(it)]
        open_cell = f'<td class="num">{len(g_open)}</td>' if still_open else ""
        prefix_rows.append(
            "<tr>"
            f"<td><strong>{_esc(code)}</strong></td>"
            f'<td class="num">{len(group)}</td>'
            f'<td class="num">{len(g_sold)}</td>'
            f'<td class="num">{len(g_ns)}</td>'
            f"{open_cell}"
            f'<td class="num">{len(g_miss)}</td>'
            f'<td class="num">{_money(sum(it.bid_price for it in g_sold))}</td>'
            f'<td class="num">{_money(sum(it.bid_price for it in g_miss if it.bid_price > 0))}</td>'
            "</tr>"
        )
        item_word = "item" if len(group) == 1 else "items"
        pills = (
            f'<span class="pill sold"><strong>{len(g_sold)}</strong> sold</span>'
            f'<span class="pill unsold"><strong>{len(g_ns)}</strong> not sold</span>'
        )
        if g_open:
            pills += f'<span class="pill"><strong>{len(g_open)}</strong> still open</span>'
        pills += f'<span class="pill"><strong>{len(g_miss)}</strong> below reserve</span>'
        sections.append(
            f"<h2>SKU {_esc(code)} — {len(group)} {item_word}</h2>"
            f'<div class="tally-bar">{pills}</div>'
            + _section_table(g_sold, bucket="sold")
            + _section_table(g_ns, bucket="unsold")
            + _section_table(g_open, bucket="open")
        )

    open_header = "<th class=\"num\">Open</th>" if still_open else ""
    open_pill = (
        f'<span class="pill"><strong>{len(still_open)}</strong> still open</span>'
        if still_open
        else ""
    )
    badge = f"Live · {APP_NAME}" if still_open else f"End of sale · {APP_NAME}"
    subtitle = (
        "High bid vs reserve, grouped by SKU prefix"
        if not still_open
        else "Live high bid vs reserve, grouped by SKU prefix"
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>{APP_NAME} — Sale {snap.sale_id}</title>
  <style>{STYLES}</style>
</head>
<body>
  <div class="badge">{badge}</div>
  <h1>Sale {snap.sale_id}</h1>
  <p class="subtitle">{subtitle}</p>
  <p class="meta">
    Generated {_esc(stamp)}<br />
    <a href="{_esc(sale_link)}">{_esc(sale_link)}</a>
  </p>

  <div class="tally-bar">
    <span class="pill sold"><strong>{len(sold)}</strong> sold</span>
    <span class="pill unsold"><strong>{len(not_sold)}</strong> not sold</span>
    {open_pill}
    <span class="pill"><strong>{len(items)}</strong> total</span>
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
    Those bids are kept here so they are not lost after CTBids drops them.
  </div>

  <h2>By SKU prefix</h2>
  <table>
    <thead>
      <tr>
        <th>Prefix</th>
        <th class="num">Lots</th>
        <th class="num">Sold</th>
        <th class="num">Not sold</th>
        {open_header}
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
    High bid is the live hammer if sold, or the last bid if not sold.
    Reserve comes from the same lot list. This is an internal snapshot, not an invoice.
    Premium, tax, and invoice totals are in the Sale Report Generator from the official CTBids CSV.
    <br />{_esc(COPYRIGHT_LINE)}
  </p>
</body>
</html>
"""


def default_snapshot_dir() -> Path:
    """Where auto-saved closing snapshots go (same parent as Sale Suite reports)."""
    docs = Path.home() / "Documents"
    base = docs if docs.exists() else Path.home()
    path = base / "Sale Reports" / "Live Snapshots"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _met_reserve(item: LiveItem) -> str:
    if item.reserve_price <= 0:
        return ""
    if item.bid_price >= item.reserve_price:
        return "Yes"
    return "No"


def live_snapshot_path(sale_id: int, out_dir: Path | None = None) -> Path:
    folder = Path(out_dir) if out_dir else default_snapshot_dir()
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"sale-{sale_id}-LIVE.csv"


def write_closing_csv(snap: Snapshot, csv_path: Path) -> Path:
    """Save high bid + reserve while CTBids still has live bid amounts."""
    csv_path = Path(csv_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    items = sorted(
        snap.items.values(),
        key=lambda it: (-it.bid_price, it.sku or "", it.title),
    )
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "SKU",
                "Item Name",
                "Item ID",
                "Status",
                "High Bid",
                "Buy Now Price",
                "Reserve Price",
                "Met Reserve",
                "Number of Bids",
                "High Bidder",
                "Close Time",
                "Item Link",
            ]
        )
        for it in items:
            writer.writerow(
                [
                    it.sku,
                    it.title,
                    it.item_id,
                    it.item_status,
                    f"{it.bid_price:.2f}",
                    f"{it.buy_now_price:.2f}",
                    f"{it.reserve_price:.2f}",
                    _met_reserve(it),
                    it.bid_count,
                    it.high_bidder,
                    it.close_time,
                    it.link,
                ]
            )
    return csv_path


def save_closing_snapshot(
    snap: Snapshot,
    *,
    sale_url: str = "",
    out_dir: Path | None = None,
) -> list[Path]:
    """Write CSV + HTML + PDF when the sale has no lots left open."""
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    folder = Path(out_dir) if out_dir else default_snapshot_dir()
    folder.mkdir(parents=True, exist_ok=True)
    stem = folder / f"sale-{snap.sale_id}-closing-{stamp}"
    csv_path = write_closing_csv(snap, stem.with_suffix(".csv"))
    html_path = stem.with_suffix(".html")
    html_path.write_text(build_html(snap, sale_url=sale_url), encoding="utf-8")
    paths: list[Path] = [csv_path, html_path]
    pdf_path = stem.with_suffix(".pdf")
    browser = find_browser()
    if browser and html_to_pdf(browser, html_path, pdf_path):
        paths.append(pdf_path)
    return paths


def export_snapshot(
    snap: Snapshot,
    pdf_path: Path,
    *,
    sale_url: str = "",
    also_html: bool = True,
) -> tuple[Path, Path | None]:
    """Write HTML (+ PDF via Edge/Chrome). Returns (pdf_or_html_path, html_path)."""
    pdf_path = Path(pdf_path)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    html_path = pdf_path.with_suffix(".html")
    html_path.write_text(
        build_html(snap, sale_url=sale_url),
        encoding="utf-8",
    )

    browser = find_browser()
    if not browser:
        if also_html:
            return html_path, html_path
        raise RuntimeError(
            "Could not find Microsoft Edge or Google Chrome to create a PDF.\n"
            "The HTML report was saved instead:\n" + str(html_path)
        )

    ok = html_to_pdf(browser, html_path, pdf_path)
    if not ok:
        raise RuntimeError(
            "PDF conversion failed. The HTML report is available at:\n" + str(html_path)
        )
    if not also_html:
        try:
            html_path.unlink()
            html_path = None  # type: ignore[assignment]
        except OSError:
            pass
    return pdf_path, html_path
