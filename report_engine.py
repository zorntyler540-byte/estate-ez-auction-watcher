"""Single entry point that turns a CTBids sale CSV into internal + client reports.

Used by both the desktop app (app.py) and the command line. Reads a config
file (config.json) that lives next to the program, so a new franchise only has
to edit prefixes/labels there -- no code changes.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

import pandas as pd

from build_active_sale_report import REQUIRED_COLUMNS as ACTIVE_REQUIRED
from build_active_sale_report import build as build_active_sale
from build_end_of_sale_report import build
from extract_sku_prefixes import discover_sku_prefixes, extract_by_prefix

CONFIG_NAME = "config.json"

DEFAULT_CONFIG: dict = {
    "business_name": "My Estate Sales",
    "prefixes": {
        "NK": "Coins & Collectibles",
        "SA": "Estate & Household",
        "Z": "General Merchandise",
    },
}

REQUIRED_CSV_COLUMNS = ["SKU", "Item Name", "Sale Price", "Buyers Premium", "Tax", "Status"]
# Active-sale exports uniquely include bidder/activity fields
ACTIVE_HINT_COLUMNS = {"Winner First Name", "Number of Bids", "Number of Views", "Reserve Price"}
# End-of-sale / sale-history exports include settlement fields
END_HINT_COLUMNS = {"Invoice Number", "Invoice Status", "Tax Buyers Premium", "Add On Invoice Number"}

ProgressFn = Callable[[str], None]


def app_dir() -> Path:
    """Folder the program runs from (next to the .exe when frozen)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def config_path() -> Path:
    return app_dir() / CONFIG_NAME


def load_config() -> dict:
    """Load config.json next to the program; create a default one if missing."""
    path = config_path()
    if not path.exists():
        path.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        return dict(DEFAULT_CONFIG)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"Could not read {CONFIG_NAME}: {exc}") from exc

    prefixes = data.get("prefixes", {})
    if prefixes is None:
        prefixes = {}
    if not isinstance(prefixes, dict):
        raise ValueError(
            f"{CONFIG_NAME} \"prefixes\" must be an object mapping each "
            "SKU prefix to a client name (or empty to auto-detect)."
        )
    data["prefixes"] = {str(k).upper(): str(v) for k, v in prefixes.items()}
    data.setdefault("business_name", DEFAULT_CONFIG["business_name"])
    return data


def find_browser() -> Path | None:
    """Locate an installed Chromium browser (Edge or Chrome) for PDF export."""
    override = os.environ.get("SALE_REPORT_BROWSER")
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
    """Convert one HTML file to PDF using headless Chromium (Edge/Chrome).

    Edge/Chrome may hand the work to a detached background process and return
    immediately, so we poll for the output file instead of trusting exit code.
    """
    file_url = Path(html_path).resolve().as_uri()
    pdf_path = Path(pdf_path).resolve()
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    for headless in ("--headless=new", "--headless"):
        user_data = Path(tempfile.mkdtemp(prefix="sr_pdf_"))
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
                    # launcher exited; give the detached worker a moment to write
                    time.sleep(1.0)
                    if pdf_path.exists() and pdf_path.stat().st_size > 0:
                        break
                time.sleep(0.25)
        except Exception:  # noqa: BLE001 - try the next headless flavor
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
            # small grace so the worker releases the temp profile before cleanup
            time.sleep(0.3)
            shutil.rmtree(user_data, ignore_errors=True)
        if pdf_path.exists() and pdf_path.stat().st_size > 0:
            return True
    return False


def save_config(config: dict) -> Path:
    """Write the config back to config.json next to the program.

    An empty prefixes map is allowed — generate_reports will auto-detect
    SKU groups from the CSV when none are configured.
    """
    prefixes = config.get("prefixes", {})
    if not isinstance(prefixes, dict):
        raise ValueError("prefixes must be a mapping of SKU prefix to client name.")
    config = dict(config)
    config["prefixes"] = {str(k).upper(): str(v) for k, v in prefixes.items()}
    path = config_path()
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


def detect_csv_kind(csv_path: Path) -> str:
    """Return 'active_sale' or 'end_of_sale' based on CSV headers."""
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"File not found:\n{csv_path}")
    try:
        header = pd.read_csv(csv_path, nrows=0)
    except Exception as exc:  # noqa: BLE001 - surface any parse issue plainly
        raise ValueError(f"This file could not be read as a CSV:\n{csv_path}\n\n{exc}") from exc

    # Normalize header names (BOM / stray spaces)
    cols = {str(c).replace("\ufeff", "").strip() for c in header.columns}
    end_ready = all(c in cols for c in REQUIRED_CSV_COLUMNS)
    end_hits = len(cols & END_HINT_COLUMNS)
    active_hits = len(cols & ACTIVE_HINT_COLUMNS)
    name = csv_path.name.lower()

    # CTBids "sale-history-report" downloads are always end-of-sale / settlement
    if "sale-history" in name or "sale_history" in name:
        if end_ready or end_hits >= 1:
            return "end_of_sale"

    # Sale-history / end-of-sale CSVs often also have winner + bid columns.
    # Prefer end-of-sale when settlement columns are present.
    if end_ready and end_hits >= 1:
        return "end_of_sale"
    if end_ready and "Status" in cols and "Buyers Premium" in cols and "Tax" in cols:
        # Settlement fields without Invoice* still mean end-of-sale
        return "end_of_sale"
    if active_hits >= 2 and all(c in cols for c in ("SKU", "Item Name", "Sale Price")):
        return "active_sale"
    if end_ready:
        return "end_of_sale"
    raise ValueError(
        "This CSV isn't recognized as a CTBids sale-history or active-sale export.\n\n"
        "Use a sale-history report (end of sale) or an active-sale report (mid-sale summary)."
    )


_UNSAFE_FOLDER = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sale_title_from_csv(csv_path: Path) -> str:
    """Best-effort sale title from the first data row (CTBids exports include Sale Title)."""
    try:
        sample = pd.read_csv(csv_path, nrows=1)
        if "Sale Title" not in sample.columns or sample.empty:
            return ""
        title = str(sample.iloc[0]["Sale Title"]).strip()
        if not title or title.lower() in {"nan", "none", "nat"}:
            return ""
        return title
    except Exception:  # noqa: BLE001 - folder naming is optional
        return ""


def _safe_folder_name(name: str, max_len: int = 60) -> str:
    cleaned = _UNSAFE_FOLDER.sub(" ", name)
    cleaned = " ".join(cleaned.split()).strip(" .")
    if not cleaned:
        return "Sale"
    return cleaned[:max_len].rstrip(" .")


def make_run_output_dir(base_dir: Path, csv_path: Path, kind: str) -> Path:
    """Create a unique subfolder so each Generate keeps its own copies of the reports."""
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    title = _sale_title_from_csv(csv_path)
    if title:
        prefix = _safe_folder_name(title)
    elif kind == "active_sale":
        prefix = "Active_Sale"
    else:
        prefix = "End_of_Sale"
    folder = base_dir / f"{prefix}_{stamp}"
    n = 2
    while folder.exists():
        folder = base_dir / f"{prefix}_{stamp}_{n}"
        n += 1
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def validate_csv(csv_path: Path) -> None:
    """Raise a friendly error if the file isn't a usable CTBids end-of-sale export."""
    if not csv_path.exists():
        raise FileNotFoundError(f"File not found:\n{csv_path}")
    try:
        header = pd.read_csv(csv_path, nrows=0)
    except Exception as exc:  # noqa: BLE001 - surface any parse issue plainly
        raise ValueError(f"This file could not be read as a CSV:\n{csv_path}\n\n{exc}") from exc

    missing = [c for c in REQUIRED_CSV_COLUMNS if c not in header.columns]
    if missing:
        raise ValueError(
            "This doesn't look like a CTBids sale-history export.\n\n"
            "Missing columns: " + ", ".join(missing)
        )


def _generate_active_sale(
    csv_path: Path,
    output_dir: Path,
    progress: ProgressFn | None,
    make_pdf: bool,
) -> dict:
    def say(msg: str) -> None:
        if progress:
            progress(msg)

    say("Detected an active-sale CSV - building boss summary...")
    # Light validation against active required set
    header = pd.read_csv(csv_path, nrows=0)
    missing = [c for c in ACTIVE_REQUIRED if c not in header.columns]
    if missing:
        raise ValueError(
            "This doesn't look like a CTBids active-sale export.\n\n"
            "Missing columns: " + ", ".join(missing)
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    outputs = build_active_sale(csv_path, output_dir)

    internal = next((p for p in outputs if p.suffix == ".html"), None)
    workbook = next((p for p in outputs if p.suffix == ".xlsx"), None)

    pdfs: list[Path] = []
    if make_pdf and internal is not None:
        browser = find_browser()
        if browser is None:
            say("Could not find Edge or Chrome, so PDFs were skipped. The HTML report is ready.")
        else:
            say(f"Creating PDF with {browser.name}...")
            pdf_path = internal.with_suffix(".pdf")
            if html_to_pdf(browser, internal, pdf_path):
                pdfs.append(pdf_path)
                say(f"   PDF ready: {pdf_path.name}")
            else:
                say(f"   Could not create a PDF for {internal.name} (HTML version is fine).")

    say("Done. Active sale summary is ready.")
    return {
        "kind": "active_sale",
        "internal": internal,
        "workbook": workbook,
        "clients": [],
        "pdfs": pdfs,
        "output_dir": output_dir,
    }


def generate_reports(
    csv_path: Path,
    output_dir: Path,
    config: dict | None = None,
    progress: ProgressFn | None = None,
    make_pdf: bool = False,
) -> dict:
    """Build reports from a CTBids CSV (end-of-sale or active-sale).

    Auto-detects the CSV type. Writes into a new timestamped subfolder of
    ``output_dir`` so repeated Generate clicks do not overwrite prior files.
    Returns a dict with keys: kind, internal, workbook, clients, pdfs, output_dir
    (``output_dir`` is the unique run folder).
    """
    def say(msg: str) -> None:
        if progress:
            progress(msg)

    csv_path = Path(csv_path)
    base_dir = Path(output_dir)

    say("Checking the sale file...")
    kind = detect_csv_kind(csv_path)
    output_dir = make_run_output_dir(base_dir, csv_path, kind)
    say(f"Saving this run to: {output_dir.name}")
    if kind == "active_sale":
        return _generate_active_sale(csv_path, output_dir, progress, make_pdf)

    cfg = config or load_config()
    configured = cfg.get("prefixes") or {}
    labels: dict[str, str] = {str(k).upper(): str(v) for k, v in configured.items()}

    validate_csv(csv_path)

    if not labels:
        say("No clients selected - finding SKU groups in the sale file...")
        found = discover_sku_prefixes(csv_path)
        if not found:
            raise ValueError(
                "No SKU letter prefixes were found in this sale file.\n"
                "Add clients under Manage Clients, or check that SKUs start with letters."
            )
        # Use the prefix itself as the client label when auto-detecting
        labels = {code: code for code in found}
        say(f"Found {len(labels)} SKU group(s): {', '.join(labels)}")

    output_dir.mkdir(parents=True, exist_ok=True)

    work_dir = Path(tempfile.mkdtemp(prefix="sale_reports_"))
    try:
        say(f"Sorting items into {len(labels)} client group(s)...")
        extract_by_prefix(csv_path, work_dir, tuple(labels.keys()))

        say("Building the full internal report and client reports...")
        outputs = build(work_dir, csv_path, prefix_labels=labels, out_dir=output_dir)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    internal = next((p for p in outputs if p.suffix == ".html" and p.parent == output_dir), None)
    workbook = next((p for p in outputs if p.suffix == ".xlsx"), None)
    clients = [p for p in outputs if p.parent.name == "client_reports"]

    pdfs: list[Path] = []
    if make_pdf:
        browser = find_browser()
        if browser is None:
            say("Could not find Edge or Chrome, so PDFs were skipped. The HTML reports are ready.")
        else:
            html_reports = [p for p in ([internal] + clients) if p is not None]
            say(f"Creating {len(html_reports)} PDF version(s) with {browser.name}...")
            for html in html_reports:
                pdf_path = html.with_suffix(".pdf")
                if html_to_pdf(browser, html, pdf_path):
                    pdfs.append(pdf_path)
                    say(f"   PDF ready: {pdf_path.name}")
                else:
                    say(f"   Could not create a PDF for {html.name} (HTML version is fine).")

    say(f"Done. Created {len(clients)} client report(s).")
    return {
        "kind": "end_of_sale",
        "internal": internal,
        "workbook": workbook,
        "clients": clients,
        "pdfs": pdfs,
        "output_dir": output_dir,
    }


def _seller_client_and_token():
    try:
        from seller_client import SellerClient
        from auth import resolve_seller_access_token
    except ImportError:
        from sale_watcher.seller_client import SellerClient
        from sale_watcher.auth import resolve_seller_access_token

    token = resolve_seller_access_token(validate=True, allow_browser=False)
    if not token:
        raise PermissionError(
            "Sign in to Estate-Ez on the Live Watch tab (or Seller sign in), then try again."
        )
    return SellerClient(token)


def list_seller_sales_for_reports() -> list[dict]:
    """Sales on this Seller login that have lots — for the Reports sale picker."""
    client = _seller_client_and_token()
    sales = [s for s in client.list_sales() if s.item_count > 0]
    sales.sort(key=lambda s: (0 if s.is_closed else 1, 0 if s.is_live else 1, -s.sale_id))
    return [
        {
            "sale_id": s.sale_id,
            "title": s.title,
            "status": s.status,
            "item_count": s.item_count,
            "is_live": s.is_live,
            "is_closed": s.is_closed,
        }
        for s in sales
    ]


def generate_sku_prefix_from_seller(
    prefix: str | list[str] = "",
    output_dir: Path | None = None,
    *,
    prefixes: list[str] | None = None,
    all_prefixes: bool = False,
    sale_ids: list[int] | None = None,
    business_name: str = "",
    make_pdf: bool = True,
    progress: ProgressFn | None = None,
) -> dict:
    """Pull chosen CT Seller sales and write sold/unsold reports per SKU prefix."""
    from build_sku_rollup_report import (
        build_html,
        build_index_html,
        discover_prefixes_from_rows,
        filter_rows_by_prefix,
        parse_sku_prefixes,
        write_csv,
    )

    def say(msg: str) -> None:
        if progress:
            progress(msg)

    if output_dir is None:
        raise ValueError("Choose an output folder.")

    wanted = parse_sku_prefixes(prefixes if prefixes is not None else prefix)
    if not all_prefixes and not wanted:
        raise ValueError("Enter one or more SKU prefixes (for example MS, NK) or choose every prefix.")

        say("Using CT Seller session from Estate-Ez / Live Watch...")
    client = _seller_client_and_token()
    if sale_ids:
        say(f"Loading {len(sale_ids)} selected sale(s) from CT Seller...")
    else:
        say("Loading sales from this CT Seller login...")
    rows, sales = client.fetch_all_sale_history_items(sale_ids=sale_ids, progress=say)
    if not rows:
        raise ValueError("Those sales have no lots in CT Seller.")
    say(f"Loaded {len(rows)} lot(s) from {len(sales)} sale(s).")

    if all_prefixes:
        wanted = discover_prefixes_from_rows(rows)
        if not wanted:
            raise ValueError("No letter SKU prefixes were found on the selected sales.")
        say(f"Found SKU prefixes: {', '.join(wanted)}")

    by_prefix: dict[str, list[dict]] = {}
    empty: list[str] = []
    for px in wanted:
        matched = filter_rows_by_prefix(rows, px)
        if matched:
            by_prefix[px] = matched
        else:
            empty.append(px)
    if empty:
        say(f"No lots for: {', '.join(empty)}")
    if not by_prefix:
        raise ValueError(
            "No lots matching those SKU prefixes were found on the selected sales."
        )

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    folder_name = "SKU_Seller_" + stamp
    folder = Path(output_dir) / folder_name
    n = 2
    while folder.exists():
        folder = Path(output_dir) / f"{folder_name}_{n}"
        n += 1
    folder.mkdir(parents=True, exist_ok=True)

    sale_titles = [(s.title or f"Sale {s.sale_id}").strip() for s in sales]
    html_paths: list[Path] = []
    csv_paths: list[Path] = []
    pdfs: list[Path] = []
    browser = find_browser() if make_pdf else None
    if make_pdf and browser is None:
        say("Could not find Edge or Chrome, so PDFs were skipped. HTML is ready.")

    for px, matched in by_prefix.items():
        sold_n = sum(
            1 for r in matched if str(r.get("itemstatus") or "").strip().lower() == "sold"
        )
        say(f"{px}: {len(matched)} lots ({sold_n} sold). Writing report...")
        html_path = folder / f"SKU_{px}_sold_unsold.html"
        csv_path = folder / f"SKU_{px}_sold_unsold.csv"
        html_path.write_text(
            build_html(
                matched,
                prefix=px,
                business_name=business_name,
                sale_count=len(sales),
            ),
            encoding="utf-8",
        )
        write_csv(matched, csv_path)
        html_paths.append(html_path)
        csv_paths.append(csv_path)
        if browser is not None:
            pdf_path = html_path.with_suffix(".pdf")
            say(f"Creating PDF for {px}...")
            if html_to_pdf(browser, html_path, pdf_path):
                pdfs.append(pdf_path)
                say(f"PDF ready: {pdf_path.name}")
            else:
                say(f"Could not create a PDF for {px} (HTML version is fine).")

    index_path = folder / "index.html"
    index_path.write_text(
        build_index_html(
            prefixes=list(by_prefix.keys()),
            business_name=business_name,
            sale_count=len(sales),
            sale_titles=sale_titles,
        ),
        encoding="utf-8",
    )
    say(f"Saved to {folder}")
    return {
        "kind": "sku_rollup",
        "internal": index_path,
        "workbook": csv_paths[0] if csv_paths else None,
        "clients": html_paths,
        "pdfs": pdfs,
        "csvs": csv_paths,
        "output_dir": folder,
    }


def generate_one_sale_from_seller(
    sale_id: int,
    output_dir: Path,
    config: dict | None = None,
    *,
    business_name: str = "",
    make_pdf: bool = True,
    progress: ProgressFn | None = None,
) -> dict:
    """One CT Seller sale → owner master (premium/tax) + per-prefix clients + all-SKU list."""
    from build_sku_rollup_report import build_html, write_csv, write_sale_history_csv

    def say(msg: str) -> None:
        if progress:
            progress(msg)

    try:
        sid = int(sale_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("Pick one sale.") from exc

        say("Using CT Seller session from Estate-Ez / Live Watch...")
    client = _seller_client_and_token()
    say(f"Loading sale #{sid} from CT Seller...")
    rows, sales = client.fetch_all_sale_history_items(sale_ids=[sid], progress=say)
    if not rows:
        raise ValueError("That sale has no lots in CT Seller.")
    sale_title = ""
    if sales:
        sale_title = (sales[0].title or "").strip()
    if not sale_title:
        sale_title = str(rows[0].get("saletitle") or f"Sale {sid}").strip()
    say(f"Loaded {len(rows)} lot(s) from {sale_title}. Building owner and client reports...")

    work = Path(tempfile.mkdtemp(prefix="seller_sale_"))
    try:
        csv_path = work / "sale-history.csv"
        write_sale_history_csv(rows, csv_path)
        result = generate_reports(
            csv_path,
            Path(output_dir),
            config,
            progress=progress,
            make_pdf=make_pdf,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)

    folder = Path(result["output_dir"])
    all_html = folder / "SKU_ALL_sold_unsold.html"
    all_csv = folder / "SKU_ALL_sold_unsold.csv"
    all_html.write_text(
        build_html(
            rows,
            prefix="ALL",
            business_name=business_name or str((config or {}).get("business_name") or ""),
            sale_count=1,
            heading=f"{sale_title} — all SKUs",
        ),
        encoding="utf-8",
    )
    write_csv(rows, all_csv)
    clients = list(result.get("clients") or [])
    clients.insert(0, all_html)
    pdfs = list(result.get("pdfs") or [])
    if make_pdf:
        browser = find_browser()
        if browser is not None:
            pdf_path = all_html.with_suffix(".pdf")
            say("Creating all-SKU PDF...")
            if html_to_pdf(browser, all_html, pdf_path):
                pdfs.insert(0, pdf_path)
    say(f"Saved to {folder}")
    result["kind"] = "end_of_sale"
    result["clients"] = clients
    result["pdfs"] = pdfs
    result["csvs"] = [all_csv]
    return result
