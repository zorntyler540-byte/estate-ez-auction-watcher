"""Single entry point that turns a CTBids sale CSV into internal + client reports.

Used by both the desktop app (app.py) and the command line. Reads a config
file (config.json) that lives next to the program, so a new franchise only has
to edit prefixes/labels there -- no code changes.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
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
    if not csv_path.exists():
        raise FileNotFoundError(f"File not found:\n{csv_path}")
    try:
        header = pd.read_csv(csv_path, nrows=0)
    except Exception as exc:  # noqa: BLE001 - surface any parse issue plainly
        raise ValueError(f"This file could not be read as a CSV:\n{csv_path}\n\n{exc}") from exc

    cols = set(header.columns)
    active_hits = len(cols & ACTIVE_HINT_COLUMNS)
    if active_hits >= 2 and all(c in cols for c in ("SKU", "Item Name", "Sale Price")):
        return "active_sale"
    if all(c in cols for c in REQUIRED_CSV_COLUMNS):
        return "end_of_sale"
    raise ValueError(
        "This CSV isn't recognized as a CTBids sale-history or active-sale export.\n\n"
        "Use a sale-history report (end of sale) or an active-sale report (mid-sale summary)."
    )


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

    Auto-detects the CSV type. Returns a dict with keys:
    kind, internal, workbook, clients, pdfs, output_dir.
    """
    def say(msg: str) -> None:
        if progress:
            progress(msg)

    csv_path = Path(csv_path)
    output_dir = Path(output_dir)

    say("Checking the sale file...")
    kind = detect_csv_kind(csv_path)
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
