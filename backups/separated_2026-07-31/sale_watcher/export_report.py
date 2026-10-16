"""Export a Sale Watcher snapshot to HTML + PDF (Edge/Chrome headless print)."""
from __future__ import annotations

import html
import os
import shutil
import subprocess
import tempfile
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from ctbids_client import LiveItem, Snapshot

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
    h2 {
      font-size: 1.15rem; color: #2d6a4f; margin-top: 36px;
      border-bottom: 2px solid #d8f3dc; padding-bottom: 6px;
    }
    .badge {
      display: inline-block; background: #2d6a4f; color: #fff;
      font-size: 0.78rem; font-weight: 600; letter-spacing: 0.05em;
      text-transform: uppercase; padding: 4px 12px; border-radius: 4px;
      margin-bottom: 12px;
    }
    .subtitle { color: #555; margin: 0 0 8px; font-size: 1.05rem; }
    .meta { color: #666; font-size: 0.9rem; margin-bottom: 28px; }
    .cards {
      display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 12px; margin-bottom: 24px;
    }
    .card {
      background: #f1faee; border: 1px solid #b7e4c7; border-radius: 8px;
      padding: 12px 14px;
    }
    .card .label {
      font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.05em;
      color: #52796f;
    }
    .card .value { font-size: 1.25rem; font-weight: 700; color: #1b4332; margin-top: 4px; }
    table { width: 100%; border-collapse: collapse; font-size: 0.88rem; margin-bottom: 8px; }
    th { background: #1b4332; color: #fff; text-align: left; padding: 8px 10px; }
    th.num, td.num { text-align: right; }
    td { padding: 7px 10px; border-bottom: 1px solid #e9ecef; vertical-align: top; }
    tr:nth-child(even) td { background: #fafafa; }
    tfoot td { font-weight: 700; background: #b7e4c7; border-top: 2px solid #1b4332; }
    .muted { color: #888; }
    .footer {
      margin-top: 40px; font-size: 0.82rem; color: #888;
      border-top: 1px solid #ddd; padding-top: 16px;
    }
    @media print {
      body { margin: 16px; max-width: none; }
      tr { page-break-inside: avoid; }
      thead { display: table-header-group; }
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


def _bidder_totals(items: list[LiveItem]) -> list[tuple[str, int, float]]:
    totals: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])  # count, spend
    for it in items:
        if it.bid_price <= 0:
            continue
        name = (it.high_bidder or "").strip() or "(unknown)"
        totals[name][0] += 1
        totals[name][1] += it.bid_price
    ranked = [(name, int(v[0]), float(v[1])) for name, v in totals.items()]
    ranked.sort(key=lambda row: (-row[2], -row[1], row[0].lower()))
    return ranked


def build_html(
    snap: Snapshot,
    *,
    sale_url: str = "",
    generated_at: datetime | None = None,
) -> str:
    when = generated_at or datetime.now()
    stamp = when.strftime("%B %d, %Y · %I:%M %p").lstrip("0").replace(" 0", " ")
    items = sorted(snap.items.values(), key=lambda it: (-it.bid_price, -it.bid_count, it.title.lower()))
    with_bids = [it for it in items if it.bid_price > 0 or it.bid_count > 0]
    bidders = _bidder_totals(with_bids)
    sale_link = sale_url.strip() or f"https://ctbids.com/estate-sale/{snap.sale_id}"

    cards = [
        ("Hammer total", _money(snap.hammer_total)),
        ("Items with bids", f"{snap.bidding_count:,}"),
        ("Still open", f"{snap.open_count:,}"),
        ("Unique bidders", f"{snap.unique_bidders:,}"),
        ("Listed items", f"{len(snap.items):,}"),
    ]
    cards_html = "".join(
        f'<div class="card"><div class="label">{_esc(label)}</div>'
        f'<div class="value">{_esc(value)}</div></div>'
        for label, value in cards
    )

    bidder_rows = []
    for i, (name, count, spend) in enumerate(bidders[:40], start=1):
        bidder_rows.append(
            "<tr>"
            f"<td>{i}</td>"
            f"<td>{_esc(name)}</td>"
            f'<td class="num">{count}</td>'
            f'<td class="num">{_money(spend)}</td>'
            "</tr>"
        )
    if not bidder_rows:
        bidder_rows.append('<tr><td colspan="4" class="muted">No bidder names available.</td></tr>')

    item_rows = []
    for it in with_bids:
        item_rows.append(
            "<tr>"
            f"<td>{_esc(it.sku) or '<span class=\"muted\">—</span>'}</td>"
            f'<td class="num">{_money(it.bid_price)}</td>'
            f'<td class="num">{it.bid_count}</td>'
            f"<td>{_esc(it.high_bidder) or '<span class=\"muted\">—</span>'}</td>"
            f"<td>{_esc(it.account_status) or '<span class=\"muted\">—</span>'}</td>"
            f"<td>{_esc(it.title)}</td>"
            f"<td>{_esc(it.category)}</td>"
            "</tr>"
        )
    if not item_rows:
        item_rows.append('<tr><td colspan="7" class="muted">No bids recorded in this snapshot.</td></tr>')

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Sale Watcher — Sale {snap.sale_id}</title>
  <style>{STYLES}</style>
</head>
<body>
  <div class="badge">End of sale · Sale Watcher</div>
  <h1>Sale {snap.sale_id}</h1>
  <p class="subtitle">Live hammer snapshot from CTBids Sale Watcher</p>
  <p class="meta">
    Generated {_esc(stamp)}<br />
    <a href="{_esc(sale_link)}">{_esc(sale_link)}</a>
  </p>

  <div class="cards">{cards_html}</div>

  <h2>Top bidders by hammer</h2>
  <table>
    <thead>
      <tr><th>#</th><th>Bidder</th><th class="num">Items</th><th class="num">Hammer</th></tr>
    </thead>
    <tbody>
      {''.join(bidder_rows)}
    </tbody>
  </table>

  <h2>Items with bids ({len(with_bids):,})</h2>
  <table>
    <thead>
      <tr>
        <th>SKU</th>
        <th class="num">Price</th>
        <th class="num">Bids</th>
        <th>Highest bidder</th>
        <th>Acct</th>
        <th>Item</th>
        <th>Category</th>
      </tr>
    </thead>
    <tbody>
      {''.join(item_rows)}
    </tbody>
    <tfoot>
      <tr>
        <td></td>
        <td class="num">{_money(snap.hammer_total)}</td>
        <td colspan="5">Hammer total</td>
      </tr>
    </tfoot>
  </table>

  <p class="footer">
    Prices are current bid / hammer amounts from the live sale feed.
    Premium, tax, and invoice totals are not included — use the Sale Report Generator
    with the official CTBids CSV for final accounting.
  </p>
</body>
</html>
"""


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
