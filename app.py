"""Desktop app: turn a CTBids sale CSV into internal + client reports.

Modern CustomTkinter UI. For non-technical users: pick (or drag) the sale CSV,
pick where reports go, click one button. Generation runs on a background thread
so the window never freezes.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from tkinter import BooleanVar, StringVar, filedialog, messagebox

import customtkinter as ctk

from runtime_paths import prepare_runtime
from report_engine import (
    app_dir,
    generate_one_sale_from_seller,
    generate_reports,
    generate_sku_prefix_from_seller,
    list_seller_sales_for_reports,
    load_config,
    save_config,
)
from build_sku_rollup_report import parse_sku_prefixes

prepare_runtime()

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    _DND_AVAILABLE = True
except Exception:  # noqa: BLE001 - drag and drop is an optional nicety
    _DND_AVAILABLE = False

APP_TITLE = "CTBids Sale Report Generator"

# Match Sale Suite / Estate Inventory SaaS look
THEMES = {
    "light": {
        "accent": "#2a9d9c",
        "accent_hover": "#238887",
        "accent_soft": "#e6f6f5",
        "text": "#0f1720",
        "muted": "#6b7280",
        "panel": "#ffffff",
        "card": "#ffffff",
        "window": "#e8eef2",
        "on_accent": "#0b0e14",
        "danger": "#b23a48",
        "danger_hover": "#8c2c37",
        "log_bg": "#f7faf8",
    },
    "dark": {
        "accent": "#3fc1c0",
        "accent_hover": "#2ba8a7",
        "accent_soft": "#15353a",
        "text": "#e8eef2",
        "muted": "#8b95a5",
        "panel": "#1a1f26",
        "card": "#151a21",
        "window": "#0b0e14",
        "on_accent": "#0b0e14",
        "danger": "#ef4444",
        "danger_hover": "#dc2626",
        "log_bg": "#12161c",
    },
}

# Defaults (overwritten per instance)
GREEN = THEMES["light"]["accent"]
GREEN_DARK = THEMES["light"]["text"]
GREEN_HOVER = THEMES["light"]["accent_hover"]
GREEN_SOFT = THEMES["light"]["accent_soft"]
RED = THEMES["light"]["danger"]
RED_HOVER = THEMES["light"]["danger_hover"]
MUTED = THEMES["light"]["muted"]
ON_ACCENT = THEMES["light"]["on_accent"]


def _palette(mode: str) -> dict:
    return THEMES["dark" if mode == "dark" else "light"]


if _DND_AVAILABLE:

    class _DnDCTk(ctk.CTk, TkinterDnD.DnDWrapper):
        """A CustomTkinter root that also supports file drag-and-drop."""

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.TkdndVersion = TkinterDnD._require(self)


def _default_output_dir() -> Path:
    docs = Path.home() / "Documents"
    base = docs if docs.exists() else Path.home()
    return base / "Sale Reports"


def _open_folder(path: Path) -> None:
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except Exception:  # noqa: BLE001 - opening a folder should never crash the app
        pass


class ReportApp:
    def __init__(
        self,
        parent,
        dnd: bool = False,
        *,
        manage_window: bool = True,
        appearance: str = "light",
    ) -> None:
        self.host = parent
        self.root = parent.winfo_toplevel()
        self.dnd = dnd
        self.manage_window = manage_window
        self.appearance = "dark" if str(appearance).lower() == "dark" else "light"
        self.colors = _palette(self.appearance)
        try:
            self.config = load_config()
        except Exception as exc:  # noqa: BLE001 - show config problems up front
            messagebox.showerror(APP_TITLE, str(exc))
            self.config = {"business_name": "My Estate Sales", "prefixes": {}}

        if manage_window:
            self.root.geometry("760x720")
            self.root.minsize(680, 660)
            try:
                self.root.configure(fg_color=self.colors["window"])
            except Exception:
                pass

        self.csv_var = StringVar()
        self.sku_var = StringVar(value="MS")
        self.sku_all_var = BooleanVar(value=False)
        self.sku_one_var = BooleanVar(value=False)
        self.out_var = StringVar(value=str(_default_output_dir()))
        self.pdf_var = BooleanVar(value=True)
        self.last_output: Path | None = None

        self._build_ui()
        self._apply_business_name()
        self._refresh_clients_label()

    # ----- layout -----------------------------------------------------
    def _card(self, parent, **kwargs):
        kwargs.setdefault("fg_color", self.colors["card"])
        return ctk.CTkFrame(parent, corner_radius=12, **kwargs)

    def _build_ui(self) -> None:
        c = self.colors
        canvas = ctk.CTkFrame(self.host, fg_color=c["window"], corner_radius=0)
        canvas.pack(fill="both", expand=True)
        outer = ctk.CTkFrame(canvas, fg_color="transparent")
        outer.pack(fill="both", expand=True, padx=22, pady=18)

        # Header
        header = ctk.CTkFrame(outer, fg_color="transparent")
        header.pack(fill="x")
        titles = ctk.CTkFrame(header, fg_color="transparent")
        titles.pack(side="left", fill="x", expand=True)
        self.business_label = ctk.CTkLabel(
            titles, text="Estate Sales", font=("Segoe UI Semibold", 24), text_color=c["text"],
            anchor="w",
        )
        self.business_label.pack(anchor="w")
        ctk.CTkLabel(
            titles,
            text="Drop a sale-history CSV, or pull SKU reports from CT Seller — no download.",
            font=("Segoe UI", 13), text_color=c["muted"], anchor="w",
        ).pack(anchor="w")
        self.clients_label = ctk.CTkLabel(
            titles, text="", font=("Segoe UI", 12), text_color=c["accent"], anchor="w",
            wraplength=560, justify="left",
        )
        self.clients_label.pack(anchor="w", pady=(6, 0))

        ctk.CTkButton(
            header, text="Manage Clients", width=130, height=34,
            fg_color="transparent", text_color=c["accent"], border_color=c["accent"], border_width=1,
            hover_color=c["accent_soft"], command=self._manage_prefixes,
        ).pack(side="right", anchor="n")

        # Step 1 - CSV
        csv_card = self._card(outer)
        csv_card.pack(fill="x", pady=(16, 0))
        ctk.CTkLabel(
            csv_card, text="1.  Sale CSV  (CTBids sale-history or active-sale export)",
            font=("Segoe UI Semibold", 14), anchor="w", text_color=c["text"],
        ).pack(fill="x", padx=16, pady=(14, 6))
        csv_row = ctk.CTkFrame(csv_card, fg_color="transparent")
        csv_row.pack(fill="x", padx=16, pady=(0, 6))
        self.csv_entry = ctk.CTkEntry(
            csv_row, textvariable=self.csv_var, height=38,
            placeholder_text="Choose or drag your sale CSV file here...",
            fg_color=c["panel"], border_color=c["accent_soft"],
            text_color=c["text"], placeholder_text_color=c["muted"],
        )
        self.csv_entry.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(
            csv_row, text="Browse", width=90, height=38, fg_color=c["accent"],
            hover_color=c["accent_hover"], text_color=c["on_accent"],
            command=self._pick_csv,
        ).pack(side="left", padx=(10, 0))
        self.drag_hint = ctk.CTkLabel(
            csv_card, text="", font=("Segoe UI", 12), text_color=c["accent"], anchor="w",
        )
        self.drag_hint.pack(fill="x", padx=16, pady=(0, 14))
        if self.dnd:
            self.drag_hint.configure(text="Tip: drag the CSV file from a folder and drop it on the box above.")
            self._enable_drop(csv_card)
            self._enable_drop(self.csv_entry)

        # Pull from CT Seller by SKU
        sku_card = self._card(outer)
        sku_card.pack(fill="x", pady=(14, 0))
        ctk.CTkLabel(
            sku_card, text="Or pull from CT Seller (no CSV download)",
            font=("Segoe UI Semibold", 14), anchor="w", text_color=c["text"],
        ).pack(fill="x", padx=16, pady=(14, 4))
        ctk.CTkLabel(
            sku_card,
            text="Uses Estate-Ez or Seller sign-in from Live Watch. Prefix reports across sales, or one sale with every SKU plus the owner master (premium, tax, total).",
            font=("Segoe UI", 12), text_color=c["muted"], anchor="w",
            wraplength=640, justify="left",
        ).pack(fill="x", padx=16, pady=(0, 8))
        sku_row = ctk.CTkFrame(sku_card, fg_color="transparent")
        sku_row.pack(fill="x", padx=16, pady=(0, 6))
        ctk.CTkLabel(
            sku_row, text="SKU prefixes", font=("Segoe UI Semibold", 13), text_color=c["text"],
        ).pack(side="left")
        self.sku_entry = ctk.CTkEntry(
            sku_row, textvariable=self.sku_var, height=38,
            placeholder_text="MS, NK, SA",
            fg_color=c["panel"], border_color=c["accent_soft"], text_color=c["text"],
            placeholder_text_color=c["muted"],
        )
        self.sku_entry.pack(side="left", fill="x", expand=True, padx=(10, 10))
        self.sku_all_chk = ctk.CTkCheckBox(
            sku_row, text="Every prefix", variable=self.sku_all_var,
            onvalue=True, offvalue=False,
            fg_color=c["accent"], hover_color=c["accent_hover"],
            text_color=c["text"], checkmark_color=c["on_accent"],
            font=("Segoe UI", 13), command=self._sync_sku_entry,
        )
        self.sku_all_chk.pack(side="left")
        opt_row = ctk.CTkFrame(sku_card, fg_color="transparent")
        opt_row.pack(fill="x", padx=16, pady=(0, 6))
        self.sku_one_chk = ctk.CTkCheckBox(
            opt_row, text="One sale — all SKUs  (owner master with premium & tax)",
            variable=self.sku_one_var,
            onvalue=True, offvalue=False,
            fg_color=c["accent"], hover_color=c["accent_hover"],
            text_color=c["text"], checkmark_color=c["on_accent"],
            font=("Segoe UI", 13), command=self._sync_sku_entry,
        )
        self.sku_one_chk.pack(side="left")
        sku_btn_row = ctk.CTkFrame(sku_card, fg_color="transparent")
        sku_btn_row.pack(fill="x", padx=16, pady=(0, 14))
        self.sku_btn = ctk.CTkButton(
            sku_btn_row, text="Choose sales & pull from CT Seller", height=38, width=280,
            fg_color=c["accent"], hover_color=c["accent_hover"], text_color=c["on_accent"],
            command=self._on_sku_from_seller,
        )
        self.sku_btn.pack(side="left")

        # Step 2 - Output
        out_card = self._card(outer)
        out_card.pack(fill="x", pady=(14, 0))
        ctk.CTkLabel(
            out_card, text="2.  Where should the reports be saved?",
            font=("Segoe UI Semibold", 14), anchor="w", text_color=c["text"],
        ).pack(fill="x", padx=16, pady=(14, 6))
        out_row = ctk.CTkFrame(out_card, fg_color="transparent")
        out_row.pack(fill="x", padx=16, pady=(0, 4))
        ctk.CTkEntry(
            out_row, textvariable=self.out_var, height=38,
            fg_color=c["panel"], border_color=c["accent_soft"],
            text_color=c["text"],
        ).pack(side="left", fill="x", expand=True)
        ctk.CTkButton(
            out_row, text="Browse", width=90, height=38, fg_color=c["accent"],
            hover_color=c["accent_hover"], text_color=c["on_accent"],
            command=self._pick_out,
        ).pack(side="left", padx=(10, 0))
        ctk.CTkLabel(
            out_card,
            text="Each Generate creates a new folder inside this one, so earlier reports are kept.",
            font=("Segoe UI", 12), text_color=c["muted"], anchor="w",
        ).pack(fill="x", padx=16, pady=(0, 8))
        ctk.CTkCheckBox(
            out_card, text="Also create PDF versions  (best for emailing to clients)",
            variable=self.pdf_var, onvalue=True, offvalue=False,
            fg_color=c["accent"], hover_color=c["accent_hover"],
            text_color=c["text"], checkmark_color=c["on_accent"],
            font=("Segoe UI", 13),
        ).pack(anchor="w", padx=16, pady=(0, 14))

        # Actions
        actions = ctk.CTkFrame(outer, fg_color="transparent")
        actions.pack(fill="x", pady=(16, 4))
        self.generate_btn = ctk.CTkButton(
            actions, text="Generate Reports", height=46, width=200,
            font=("Segoe UI Semibold", 15), fg_color=c["accent"], hover_color=c["accent_hover"],
            text_color=c["on_accent"], command=self._on_generate,
        )
        self.generate_btn.pack(side="left")
        self.open_btn = ctk.CTkButton(
            actions, text="Open Reports Folder", height=46, width=180,
            fg_color="transparent", text_color=c["accent"], border_color=c["accent"], border_width=1,
            hover_color=c["accent_soft"], command=self._open_output, state="disabled",
        )
        self.open_btn.pack(side="left", padx=(12, 0))

        self.progress = ctk.CTkProgressBar(
            outer, height=8, progress_color=c["accent"], fg_color=c["card"],
        )
        self.progress.pack(fill="x", pady=(12, 6))
        self.progress.set(0)

        # Status
        status_card = self._card(outer)
        status_card.pack(fill="both", expand=True, pady=(6, 0))
        ctk.CTkLabel(
            status_card, text="Status", font=("Segoe UI Semibold", 13), anchor="w",
            text_color=c["text"],
        ).pack(fill="x", padx=16, pady=(12, 4))
        self.log = ctk.CTkTextbox(
            status_card, font=("Consolas", 12), activate_scrollbars=True,
            fg_color=c["log_bg"], text_color=c["text"],
        )
        self.log.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.log.configure(state="disabled")

    # ----- header helpers ---------------------------------------------
    def _apply_business_name(self) -> None:
        business = self.config.get("business_name", "").strip()
        self.business_label.configure(text=business or "Estate Sales")
        if self.manage_window:
            self.root.title(f"{APP_TITLE} - {business}" if business else APP_TITLE)

    def _refresh_clients_label(self) -> None:
        prefixes = self.config.get("prefixes", {})
        if prefixes:
            names = ", ".join(f"{k} ({v})" for k, v in prefixes.items())
            self.clients_label.configure(text=f"{len(prefixes)} client group(s): {names}")
        else:
            self.clients_label.configure(
                text="No clients selected - will auto-find SKU groups from the sale CSV."
            )

    def _manage_prefixes(self) -> None:
        PrefixManager(
            self.root, self.config, on_save=self._on_prefixes_saved, colors=self.colors,
        )

    def _on_prefixes_saved(self, business_name: str, new_prefixes: dict) -> None:
        self.config["business_name"] = business_name
        self.config["prefixes"] = new_prefixes
        try:
            save_config(self.config)
        except Exception as exc:  # noqa: BLE001 - surface save problems
            messagebox.showerror(APP_TITLE, f"Could not save changes:\n{exc}")
            return
        self._apply_business_name()
        self._refresh_clients_label()

    # ----- drag and drop ----------------------------------------------
    def _enable_drop(self, widget) -> None:
        try:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self._on_drop)
        except Exception:  # noqa: BLE001 - ignore if this widget can't accept drops
            pass

    def _on_drop(self, event) -> None:
        path = self._parse_dropped_path(getattr(event, "data", ""))
        if path:
            self.csv_var.set(path)

    @staticmethod
    def _parse_dropped_path(data: str) -> str:
        """Pull the first file path out of a drop event (handles braces/spaces)."""
        data = (data or "").strip()
        if data.startswith("{"):
            end = data.find("}")
            if end != -1:
                return data[1:end]
        parts = data.split()
        return parts[0] if parts else ""

    # ----- pickers ----------------------------------------------------
    def _pick_csv(self) -> None:
        path = filedialog.askopenfilename(
            title="Select the sale history CSV",
            initialdir=str(app_dir()),
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if path:
            self.csv_var.set(path)

    def _pick_out(self) -> None:
        path = filedialog.askdirectory(title="Select the output folder")
        if path:
            self.out_var.set(path)

    # ----- status log -------------------------------------------------
    def _log(self, msg: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _open_output(self) -> None:
        if self.last_output and self.last_output.exists():
            _open_folder(self.last_output)

    def _set_busy(self, busy: bool) -> None:
        if busy:
            self.generate_btn.configure(state="disabled", text="Working...")
            self.sku_btn.configure(state="disabled", text="Working...")
            self.open_btn.configure(state="disabled")
            self.progress.configure(mode="indeterminate")
            self.progress.start()
            return
        self.generate_btn.configure(state="normal", text="Generate Reports")
        self.sku_btn.configure(state="normal", text=self._sku_btn_label())
        self.progress.stop()
        self.progress.configure(mode="determinate")
        self.progress.set(0)

    def _sku_btn_label(self) -> str:
        if self.sku_one_var.get():
            return "Choose a sale & pull full reports"
        return "Choose sales & pull from CT Seller"

    def _sync_sku_entry(self) -> None:
        one = bool(self.sku_one_var.get())
        every = bool(self.sku_all_var.get())
        self.sku_entry.configure(state="disabled" if one or every else "normal")
        self.sku_all_chk.configure(state="disabled" if one else "normal")
        self.sku_btn.configure(text=self._sku_btn_label())

    def _on_sku_from_seller(self) -> None:
        one_sale = bool(self.sku_one_var.get())
        all_prefixes = bool(self.sku_all_var.get()) and not one_sale
        prefixes = parse_sku_prefixes(self.sku_var.get())
        if not one_sale and not all_prefixes and not prefixes:
            messagebox.showwarning(
                APP_TITLE,
                "Enter one or more SKU prefixes (for example MS, NK), check Every prefix, or check One sale — all SKUs.",
            )
            return
        out_path = self.out_var.get().strip()
        if not out_path:
            messagebox.showwarning(APP_TITLE, "Please choose an output folder.")
            return
        self._clear_log()
        self._set_busy(True)
        self._log("Loading sales from CT Seller...")
        make_pdf = bool(self.pdf_var.get())
        business = str(self.config.get("business_name") or "")
        thread = threading.Thread(
            target=self._load_sales_for_sku,
            args=(prefixes, all_prefixes, one_sale, Path(out_path), make_pdf, business),
            daemon=True,
        )
        thread.start()

    def _load_sales_for_sku(
        self,
        prefixes: list[str],
        all_prefixes: bool,
        one_sale: bool,
        out_path: Path,
        make_pdf: bool,
        business: str,
    ) -> None:
        try:
            sales = list_seller_sales_for_reports()
        except Exception as exc:  # noqa: BLE001
            self.root.after(0, self._on_error, exc)
            return
        self.root.after(
            0,
            self._show_sale_picker,
            sales,
            prefixes,
            all_prefixes,
            one_sale,
            out_path,
            make_pdf,
            business,
        )

    def _show_sale_picker(
        self,
        sales: list[dict],
        prefixes: list[str],
        all_prefixes: bool,
        one_sale: bool,
        out_path: Path,
        make_pdf: bool,
        business: str,
    ) -> None:
        self._set_busy(False)
        if not sales:
            messagebox.showwarning(APP_TITLE, "This Seller login has no sales with lots.")
            return
        if one_sale:
            sku_note = "one sale, all SKUs — owner master (premium & tax) plus client reports"
        elif all_prefixes:
            sku_note = "every SKU prefix"
        else:
            sku_note = ", ".join(prefixes)
        SalePicker(
            self.root,
            sales,
            sku_note=sku_note,
            single=one_sale,
            colors=self.colors,
            on_confirm=lambda ids: self._start_sku_rollup(
                prefixes, all_prefixes, one_sale, ids, out_path, make_pdf, business
            ),
        )

    def _start_sku_rollup(
        self,
        prefixes: list[str],
        all_prefixes: bool,
        one_sale: bool,
        sale_ids: list[int],
        out_path: Path,
        make_pdf: bool,
        business: str,
    ) -> None:
        if not sale_ids:
            messagebox.showwarning(APP_TITLE, "Select at least one sale.")
            return
        if one_sale and len(sale_ids) != 1:
            messagebox.showwarning(APP_TITLE, "Pick exactly one sale for the all-SKU owner report.")
            return
        self._clear_log()
        self._set_busy(True)
        self._log(f"{len(sale_ids)} sale(s) selected.")
        if one_sale:
            thread = threading.Thread(
                target=self._run_one_sale,
                args=(sale_ids[0], out_path, make_pdf, business),
                daemon=True,
            )
        else:
            thread = threading.Thread(
                target=self._run_sku_rollup,
                args=(prefixes, all_prefixes, sale_ids, out_path, make_pdf, business),
                daemon=True,
            )
        thread.start()

    def _run_one_sale(self, sale_id: int, out_path: Path, make_pdf: bool, business: str) -> None:
        try:
            result = generate_one_sale_from_seller(
                sale_id,
                out_path,
                self.config,
                business_name=business,
                make_pdf=make_pdf,
                progress=lambda m: self.root.after(0, self._log, m),
            )
        except Exception as exc:  # noqa: BLE001
            self.root.after(0, self._on_error, exc)
            return
        self.root.after(0, self._on_done, result)

    def _run_sku_rollup(
        self,
        prefixes: list[str],
        all_prefixes: bool,
        sale_ids: list[int],
        out_path: Path,
        make_pdf: bool,
        business: str,
    ) -> None:
        try:
            result = generate_sku_prefix_from_seller(
                prefixes,
                out_path,
                prefixes=prefixes,
                all_prefixes=all_prefixes,
                sale_ids=sale_ids,
                business_name=business,
                make_pdf=make_pdf,
                progress=lambda m: self.root.after(0, self._log, m),
            )
        except Exception as exc:  # noqa: BLE001
            self.root.after(0, self._on_error, exc)
            return
        self.root.after(0, self._on_done, result)

    # ----- generation -------------------------------------------------
    def _on_generate(self) -> None:
        csv_path = self.csv_var.get().strip()
        out_path = self.out_var.get().strip()
        if not csv_path:
            messagebox.showwarning(APP_TITLE, "Please choose the sale history CSV file first.")
            return
        if not out_path:
            messagebox.showwarning(APP_TITLE, "Please choose an output folder.")
            return

        self._clear_log()
        self._set_busy(True)
        make_pdf = bool(self.pdf_var.get())
        thread = threading.Thread(
            target=self._run, args=(Path(csv_path), Path(out_path), make_pdf), daemon=True
        )
        thread.start()

    def _run(self, csv_path: Path, out_path: Path, make_pdf: bool) -> None:
        try:
            result = generate_reports(
                csv_path,
                out_path,
                self.config,
                progress=lambda m: self.root.after(0, self._log, m),
                make_pdf=make_pdf,
            )
        except Exception as exc:  # noqa: BLE001 - report any failure to the user
            self.root.after(0, self._on_error, exc)
            return
        self.root.after(0, self._on_done, result)

    def _on_error(self, exc: Exception) -> None:
        self._set_busy(False)
        self._log(f"Error: {exc}")
        messagebox.showerror(APP_TITLE, str(exc))

    def _on_done(self, result: dict) -> None:
        self._set_busy(False)
        self.progress.set(1)
        self.last_output = result["output_dir"]
        self.open_btn.configure(state="normal")

        if result.get("internal"):
            label = (
                "Active sale summary"
                if result.get("kind") == "active_sale"
                else "Owner master"
                if result.get("kind") == "end_of_sale"
                else "Report index"
                if result.get("kind") == "sku_rollup"
                else "Internal report"
            )
            self._log(f"{label}: {Path(result['internal']).name}")
        if result.get("workbook") and result.get("kind") != "sku_rollup":
            self._log(f"Excel workbook: {Path(result['workbook']).name}")
        for c in result.get("clients", []):
            label = "SKU report" if result.get("kind") == "sku_rollup" else "Client report"
            self._log(f"{label}: {Path(c).name}")
        for csv_path in result.get("csvs") or []:
            self._log(f"CSV: {Path(csv_path).name}")
        pdfs = result.get("pdfs", [])
        for p in pdfs:
            self._log(f"PDF: {Path(p).name}")

        pdf_note = f"\n{len(pdfs)} PDF version(s) were also created." if pdfs else ""
        kind_note = (
            "Active sale boss summary created."
            if result.get("kind") == "active_sale"
            else "Owner master and client reports created."
            if result.get("kind") == "end_of_sale"
            else "SKU sold / unsold report(s) created."
            if result.get("kind") == "sku_rollup"
            else "Reports created successfully!"
        )
        messagebox.showinfo(
            APP_TITLE,
            f"{kind_note}\n\n"
            f"Location:\n{result['output_dir']}"
            f"{pdf_note}\n\n"
            "Click \"Open Reports Folder\" to view them.",
        )


class SalePicker(ctk.CTkToplevel):
    """Choose which CT Seller sales to include in the SKU pull."""

    def __init__(
        self,
        parent,
        sales: list[dict],
        *,
        sku_note: str,
        on_confirm,
        single: bool = False,
        colors: dict | None = None,
    ) -> None:
        super().__init__(parent)
        self.on_confirm = on_confirm
        self.single = bool(single)
        self.colors = colors or _palette("light")
        c = self.colors
        self._vars: list[tuple[int, BooleanVar]] = []
        self._choice = StringVar(value="")

        self.title("Choose a sale" if self.single else "Choose sales")
        self.geometry("620x560")
        self.minsize(520, 460)
        self.transient(parent)
        try:
            self.configure(fg_color=c["window"])
        except Exception:
            pass
        self.after(200, self.grab_set)

        pad = 18
        ctk.CTkLabel(
            self,
            text="Which sale?" if self.single else "Which sales?",
            font=("Segoe UI Semibold", 20), text_color=c["text"],
        ).pack(anchor="w", padx=pad, pady=(pad, 4))
        hint = (
            f"{sku_note}. Pick one sale."
            if self.single
            else f"Reports for {sku_note}. Uncheck any sale you do not want. All are selected to start."
        )
        ctk.CTkLabel(
            self,
            text=hint,
            font=("Segoe UI", 12), text_color=c["muted"], anchor="w", justify="left", wraplength=560,
        ).pack(fill="x", padx=pad, pady=(0, 8))

        if not self.single:
            tools = ctk.CTkFrame(self, fg_color="transparent")
            tools.pack(fill="x", padx=pad, pady=(0, 6))
            ctk.CTkButton(
                tools, text="Select all", width=100, height=30,
                fg_color="transparent", text_color=c["accent"], border_color=c["accent"],
                border_width=1, hover_color=c["accent_soft"],
                command=lambda: self._set_all(True),
            ).pack(side="left")
            ctk.CTkButton(
                tools, text="Select none", width=100, height=30,
                fg_color="transparent", text_color=c["accent"], border_color=c["accent"],
                border_width=1, hover_color=c["accent_soft"],
                command=lambda: self._set_all(False),
            ).pack(side="left", padx=(8, 0))

        scroll = ctk.CTkScrollableFrame(
            self, corner_radius=12, fg_color=c["card"],
        )
        scroll.pack(fill="both", expand=True, padx=pad, pady=(0, 8))

        for sale in sales:
            sid = int(sale.get("sale_id") or 0)
            if not sid:
                continue
            title = str(sale.get("title") or f"Sale {sid}").strip()
            if len(title) > 56:
                title = title[:54].rstrip() + "…"
            status = "live" if sale.get("is_live") else (
                "closed" if sale.get("is_closed") else str(sale.get("status") or "open")
            )
            lots = int(sale.get("item_count") or 0)
            label = f"{title}  (#{sid}) · {status} · {lots} lots"
            if self.single:
                ctk.CTkRadioButton(
                    scroll, text=label, variable=self._choice, value=str(sid),
                    fg_color=c["accent"], hover_color=c["accent_hover"],
                    text_color=c["text"], font=("Segoe UI", 13),
                ).pack(anchor="w", padx=10, pady=3)
            else:
                var = BooleanVar(value=True)
                self._vars.append((sid, var))
                ctk.CTkCheckBox(
                    scroll, text=label, variable=var, onvalue=True, offvalue=False,
                    fg_color=c["accent"], hover_color=c["accent_hover"],
                    text_color=c["text"], checkmark_color=c["on_accent"],
                    font=("Segoe UI", 13),
                ).pack(anchor="w", padx=10, pady=3)

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(fill="x", padx=pad, pady=(0, pad))
        ctk.CTkButton(
            btns, text="Cancel", width=100, height=36,
            fg_color="transparent", text_color=c["text"], border_color=c["muted"],
            border_width=1, hover_color=c["accent_soft"], command=self.destroy,
        ).pack(side="left")
        ctk.CTkButton(
            btns, text="Pull reports", width=140, height=36,
            fg_color=c["accent"], hover_color=c["accent_hover"], text_color=c["on_accent"],
            command=self._confirm,
        ).pack(side="right")

    def _set_all(self, value: bool) -> None:
        for _sid, var in self._vars:
            var.set(value)

    def _confirm(self) -> None:
        if self.single:
            raw = str(self._choice.get() or "").strip()
            ids = [int(raw)] if raw.isdigit() else []
            if not ids:
                messagebox.showwarning("Choose a sale", "Pick one sale.")
                return
        else:
            ids = [sid for sid, var in self._vars if var.get()]
            if not ids:
                messagebox.showwarning("Choose sales", "Select at least one sale.")
                return
        callback = self.on_confirm
        self.destroy()
        callback(ids)


class PrefixManager(ctk.CTkToplevel):
    """Dialog to set the business name and add / edit / remove clients."""

    def __init__(self, parent, config: dict, on_save, colors: dict | None = None) -> None:
        super().__init__(parent)
        self.on_save = on_save
        self.colors = colors or _palette("light")
        c = self.colors
        self.rows: dict[str, str] = {
            str(k).upper(): str(v) for k, v in config.get("prefixes", {}).items()
        }

        self.title("Manage Clients")
        self.geometry("580x620")
        self.minsize(520, 560)
        self.transient(parent)
        try:
            self.configure(fg_color=c["window"])
        except Exception:
            pass
        self.after(200, self.grab_set)

        self.business_var = StringVar(value=config.get("business_name", ""))
        self.prefix_var = StringVar()
        self.name_var = StringVar()

        pad = 18
        ctk.CTkLabel(
            self, text="Manage Clients", font=("Segoe UI Semibold", 20), text_color=c["text"],
        ).pack(anchor="w", padx=pad, pady=(pad, 4))

        # Business name
        biz = ctk.CTkFrame(self, corner_radius=12, fg_color=c["card"])
        biz.pack(fill="x", padx=pad, pady=(6, 4))
        ctk.CTkLabel(
            biz, text="Your business name  (shown on the reports)",
            font=("Segoe UI Semibold", 13), anchor="w", text_color=c["text"],
        ).pack(fill="x", padx=14, pady=(12, 4))
        ctk.CTkEntry(
            biz, textvariable=self.business_var, height=36,
            fg_color=c["panel"], border_color=c["accent_soft"], text_color=c["text"],
        ).pack(fill="x", padx=14, pady=(0, 12))

        ctk.CTkLabel(
            self,
            text=(
                "Each SKU prefix becomes one client report. Example: prefix \"NK\" "
                "sends every item whose SKU starts with NK to that client.\n"
                "Leave the list empty to auto-find every letter prefix in the sale CSV."
            ),
            font=("Segoe UI", 12), text_color=c["muted"], anchor="w", justify="left", wraplength=520,
        ).pack(fill="x", padx=pad, pady=(6, 6))

        # Client list
        self.list_frame = ctk.CTkScrollableFrame(
            self, label_text="Clients", corner_radius=12,
            fg_color=c["card"], label_fg_color=c["card"], label_text_color=c["text"],
        )
        self.list_frame.pack(fill="both", expand=True, padx=pad, pady=(0, 8))

        # Add / update row
        add = ctk.CTkFrame(self, corner_radius=12, fg_color=c["card"])
        add.pack(fill="x", padx=pad, pady=(0, 8))
        add_row = ctk.CTkFrame(add, fg_color="transparent")
        add_row.pack(fill="x", padx=12, pady=12)
        ctk.CTkEntry(
            add_row, textvariable=self.prefix_var, width=90, height=36,
            placeholder_text="SKU",
            fg_color=c["panel"], border_color=c["accent_soft"], text_color=c["text"],
            placeholder_text_color=c["muted"],
        ).pack(side="left")
        ctk.CTkEntry(
            add_row, textvariable=self.name_var, height=36, placeholder_text="Client name",
            fg_color=c["panel"], border_color=c["accent_soft"], text_color=c["text"],
            placeholder_text_color=c["muted"],
        ).pack(side="left", fill="x", expand=True, padx=(8, 8))
        ctk.CTkButton(
            add_row, text="Add / Update", width=110, height=36, fg_color=c["accent"],
            hover_color=c["accent_hover"], text_color=c["on_accent"], command=self._add_update,
        ).pack(side="left")

        # Save / cancel
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.pack(fill="x", padx=pad, pady=(0, pad))
        ctk.CTkButton(
            bottom, text="Save", width=120, height=40, fg_color=c["accent"],
            hover_color=c["accent_hover"], text_color=c["on_accent"],
            font=("Segoe UI Semibold", 14), command=self._save,
        ).pack(side="right")
        ctk.CTkButton(
            bottom, text="Cancel", width=100, height=40, fg_color="transparent",
            text_color=c["muted"], border_color=c["muted"], border_width=1,
            hover_color=c["accent_soft"],
            command=self.destroy,
        ).pack(side="right", padx=(0, 10))

        self._render_rows()

    def _render_rows(self) -> None:
        c = self.colors
        for w in self.list_frame.winfo_children():
            w.destroy()
        if not self.rows:
            ctk.CTkLabel(
                self.list_frame,
                text="No clients selected. Reports will auto-group by SKU prefix.",
                text_color=c["muted"],
            ).pack(anchor="w", padx=8, pady=10)
            return
        for prefix, name in self.rows.items():
            row = ctk.CTkFrame(self.list_frame, fg_color="transparent")
            row.pack(fill="x", pady=3)
            ctk.CTkLabel(
                row, text=prefix, width=56, anchor="w", font=("Segoe UI Semibold", 13),
                text_color=c["accent"],
            ).pack(side="left", padx=(4, 8))
            ctk.CTkLabel(row, text=name, anchor="w", text_color=c["text"]).pack(
                side="left", fill="x", expand=True,
            )
            ctk.CTkButton(
                row, text="Remove", width=76, height=28,
                fg_color=c["danger"], hover_color=c["danger_hover"],
                command=lambda p=prefix: self._remove(p),
            ).pack(side="right", padx=4)

    def _add_update(self) -> None:
        prefix = self.prefix_var.get().strip().upper()
        name = self.name_var.get().strip()
        if not prefix:
            messagebox.showwarning("Manage Clients", "Enter a SKU prefix (for example NK).")
            return
        if not name:
            messagebox.showwarning("Manage Clients", "Enter a client name.")
            return
        self.rows[prefix] = name
        self.prefix_var.set("")
        self.name_var.set("")
        self._render_rows()

    def _remove(self, prefix: str) -> None:
        self.rows.pop(prefix, None)
        self._render_rows()

    def _save(self) -> None:
        self.on_save(self.business_var.get().strip(), dict(self.rows))
        self.destroy()


def main() -> None:
    ctk.set_appearance_mode("light")
    ctk.set_default_color_theme("green")

    root = None
    dnd = False
    if _DND_AVAILABLE:
        try:
            root = _DnDCTk()
            dnd = True
        except Exception:  # noqa: BLE001 - fall back to a normal window
            root = None
    if root is None:
        root = ctk.CTk()
    ReportApp(root, dnd=dnd, manage_window=True)
    root.mainloop()


if __name__ == "__main__":
    main()
