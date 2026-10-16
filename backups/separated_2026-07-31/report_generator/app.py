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

from report_engine import app_dir, generate_reports, load_config, save_config

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    _DND_AVAILABLE = True
except Exception:  # noqa: BLE001 - drag and drop is an optional nicety
    _DND_AVAILABLE = False

APP_TITLE = "CTBids Sale Report Generator"

# Brand palette (matches the reports)
GREEN = "#2d6a4f"
GREEN_DARK = "#1b4332"
GREEN_HOVER = "#245c43"
RED = "#b23a48"
RED_HOVER = "#8c2c37"
MUTED = "#6b7280"

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
    def __init__(self, root: ctk.CTk, dnd: bool = False) -> None:
        self.root = root
        self.dnd = dnd
        try:
            self.config = load_config()
        except Exception as exc:  # noqa: BLE001 - show config problems up front
            messagebox.showerror(APP_TITLE, str(exc))
            self.config = {"business_name": "My Estate Sales", "prefixes": {}}

        root.geometry("760x720")
        root.minsize(680, 660)

        self.csv_var = StringVar()
        self.out_var = StringVar(value=str(_default_output_dir()))
        self.pdf_var = BooleanVar(value=True)
        self.last_output: Path | None = None

        self._build_ui()
        self._apply_business_name()
        self._refresh_clients_label()

    # ----- layout -----------------------------------------------------
    def _card(self, parent, **kwargs):
        return ctk.CTkFrame(parent, corner_radius=12, **kwargs)

    def _build_ui(self) -> None:
        outer = ctk.CTkFrame(self.root, fg_color="transparent")
        outer.pack(fill="both", expand=True, padx=22, pady=18)

        # Header
        header = ctk.CTkFrame(outer, fg_color="transparent")
        header.pack(fill="x")
        titles = ctk.CTkFrame(header, fg_color="transparent")
        titles.pack(side="left", fill="x", expand=True)
        self.business_label = ctk.CTkLabel(
            titles, text="Estate Sales", font=("Segoe UI Semibold", 24), text_color=GREEN_DARK,
            anchor="w",
        )
        self.business_label.pack(anchor="w")
        ctk.CTkLabel(
            titles,
            text="Drop a sale-history CSV (end of sale) or active-sale CSV (boss mid-sale summary).",
            font=("Segoe UI", 13), text_color=MUTED, anchor="w",
        ).pack(anchor="w")
        self.clients_label = ctk.CTkLabel(
            titles, text="", font=("Segoe UI", 12), text_color=GREEN, anchor="w",
            wraplength=560, justify="left",
        )
        self.clients_label.pack(anchor="w", pady=(6, 0))

        ctk.CTkButton(
            header, text="Manage Clients", width=130, height=34,
            fg_color="transparent", text_color=GREEN, border_color=GREEN, border_width=1,
            hover_color="#e8f2ec", command=self._manage_prefixes,
        ).pack(side="right", anchor="n")

        # Step 1 - CSV
        csv_card = self._card(outer)
        csv_card.pack(fill="x", pady=(16, 0))
        ctk.CTkLabel(
            csv_card, text="1.  Sale CSV  (CTBids sale-history or active-sale export)",
            font=("Segoe UI Semibold", 14), anchor="w",
        ).pack(fill="x", padx=16, pady=(14, 6))
        csv_row = ctk.CTkFrame(csv_card, fg_color="transparent")
        csv_row.pack(fill="x", padx=16, pady=(0, 6))
        self.csv_entry = ctk.CTkEntry(
            csv_row, textvariable=self.csv_var, height=38,
            placeholder_text="Choose or drag your sale CSV file here...",
        )
        self.csv_entry.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(
            csv_row, text="Browse", width=90, height=38, fg_color=GREEN,
            hover_color=GREEN_HOVER, command=self._pick_csv,
        ).pack(side="left", padx=(10, 0))
        self.drag_hint = ctk.CTkLabel(
            csv_card, text="", font=("Segoe UI", 12), text_color=GREEN, anchor="w",
        )
        self.drag_hint.pack(fill="x", padx=16, pady=(0, 14))
        if self.dnd:
            self.drag_hint.configure(text="Tip: drag the CSV file from a folder and drop it on the box above.")
            self._enable_drop(csv_card)
            self._enable_drop(self.csv_entry)

        # Step 2 - Output
        out_card = self._card(outer)
        out_card.pack(fill="x", pady=(14, 0))
        ctk.CTkLabel(
            out_card, text="2.  Where should the reports be saved?",
            font=("Segoe UI Semibold", 14), anchor="w",
        ).pack(fill="x", padx=16, pady=(14, 6))
        out_row = ctk.CTkFrame(out_card, fg_color="transparent")
        out_row.pack(fill="x", padx=16, pady=(0, 12))
        ctk.CTkEntry(out_row, textvariable=self.out_var, height=38).pack(
            side="left", fill="x", expand=True
        )
        ctk.CTkButton(
            out_row, text="Browse", width=90, height=38, fg_color=GREEN,
            hover_color=GREEN_HOVER, command=self._pick_out,
        ).pack(side="left", padx=(10, 0))
        ctk.CTkCheckBox(
            out_card, text="Also create PDF versions  (best for emailing to clients)",
            variable=self.pdf_var, onvalue=True, offvalue=False,
            fg_color=GREEN, hover_color=GREEN_HOVER, font=("Segoe UI", 13),
        ).pack(anchor="w", padx=16, pady=(0, 14))

        # Actions
        actions = ctk.CTkFrame(outer, fg_color="transparent")
        actions.pack(fill="x", pady=(16, 4))
        self.generate_btn = ctk.CTkButton(
            actions, text="Generate Reports", height=46, width=200,
            font=("Segoe UI Semibold", 15), fg_color=GREEN, hover_color=GREEN_HOVER,
            command=self._on_generate,
        )
        self.generate_btn.pack(side="left")
        self.open_btn = ctk.CTkButton(
            actions, text="Open Reports Folder", height=46, width=180,
            fg_color="transparent", text_color=GREEN, border_color=GREEN, border_width=1,
            hover_color="#e8f2ec", command=self._open_output, state="disabled",
        )
        self.open_btn.pack(side="left", padx=(12, 0))

        self.progress = ctk.CTkProgressBar(outer, height=8, progress_color=GREEN)
        self.progress.pack(fill="x", pady=(12, 6))
        self.progress.set(0)

        # Status
        status_card = self._card(outer)
        status_card.pack(fill="both", expand=True, pady=(6, 0))
        ctk.CTkLabel(
            status_card, text="Status", font=("Segoe UI Semibold", 13), anchor="w",
        ).pack(fill="x", padx=16, pady=(12, 4))
        self.log = ctk.CTkTextbox(status_card, font=("Consolas", 12), activate_scrollbars=True)
        self.log.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.log.configure(state="disabled")

    # ----- header helpers ---------------------------------------------
    def _apply_business_name(self) -> None:
        business = self.config.get("business_name", "").strip()
        self.business_label.configure(text=business or "Estate Sales")
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
        PrefixManager(self.root, self.config, on_save=self._on_prefixes_saved)

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
        self.generate_btn.configure(state="disabled", text="Working...")
        self.open_btn.configure(state="disabled")
        self.progress.configure(mode="indeterminate")
        self.progress.start()

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

    def _stop_progress(self) -> None:
        self.progress.stop()
        self.progress.configure(mode="determinate")
        self.progress.set(0)

    def _on_error(self, exc: Exception) -> None:
        self._stop_progress()
        self.generate_btn.configure(state="normal", text="Generate Reports")
        self._log(f"Error: {exc}")
        messagebox.showerror(APP_TITLE, str(exc))

    def _on_done(self, result: dict) -> None:
        self._stop_progress()
        self.progress.set(1)
        self.generate_btn.configure(state="normal", text="Generate Reports")
        self.last_output = result["output_dir"]
        self.open_btn.configure(state="normal")

        if result.get("internal"):
            label = "Active sale summary" if result.get("kind") == "active_sale" else "Internal report"
            self._log(f"{label}: {Path(result['internal']).name}")
        if result.get("workbook"):
            self._log(f"Excel workbook: {Path(result['workbook']).name}")
        for c in result.get("clients", []):
            self._log(f"Client report: {Path(c).name}")
        pdfs = result.get("pdfs", [])
        for p in pdfs:
            self._log(f"PDF: {Path(p).name}")

        pdf_note = f"\n{len(pdfs)} PDF version(s) were also created." if pdfs else ""
        kind_note = (
            "Active sale boss summary created."
            if result.get("kind") == "active_sale"
            else "Reports created successfully!"
        )
        messagebox.showinfo(
            APP_TITLE,
            f"{kind_note}\n\n"
            f"Location:\n{result['output_dir']}"
            f"{pdf_note}\n\n"
            "Click \"Open Reports Folder\" to view them.",
        )


class PrefixManager(ctk.CTkToplevel):
    """Dialog to set the business name and add / edit / remove clients."""

    def __init__(self, parent, config: dict, on_save) -> None:
        super().__init__(parent)
        self.on_save = on_save
        self.rows: dict[str, str] = {
            str(k).upper(): str(v) for k, v in config.get("prefixes", {}).items()
        }

        self.title("Manage Clients")
        self.geometry("580x620")
        self.minsize(520, 560)
        self.transient(parent)
        self.after(200, self.grab_set)

        self.business_var = StringVar(value=config.get("business_name", ""))
        self.prefix_var = StringVar()
        self.name_var = StringVar()

        pad = 18
        ctk.CTkLabel(
            self, text="Manage Clients", font=("Segoe UI Semibold", 20), text_color=GREEN_DARK,
        ).pack(anchor="w", padx=pad, pady=(pad, 4))

        # Business name
        biz = ctk.CTkFrame(self, corner_radius=12)
        biz.pack(fill="x", padx=pad, pady=(6, 4))
        ctk.CTkLabel(
            biz, text="Your business name  (shown on the reports)",
            font=("Segoe UI Semibold", 13), anchor="w",
        ).pack(fill="x", padx=14, pady=(12, 4))
        ctk.CTkEntry(biz, textvariable=self.business_var, height=36).pack(
            fill="x", padx=14, pady=(0, 12)
        )

        ctk.CTkLabel(
            self,
            text=(
                "Each SKU prefix becomes one client report. Example: prefix \"NK\" "
                "sends every item whose SKU starts with NK to that client.\n"
                "Leave the list empty to auto-find every letter prefix in the sale CSV."
            ),
            font=("Segoe UI", 12), text_color=MUTED, anchor="w", justify="left", wraplength=520,
        ).pack(fill="x", padx=pad, pady=(6, 6))

        # Client list
        self.list_frame = ctk.CTkScrollableFrame(self, label_text="Clients", corner_radius=12)
        self.list_frame.pack(fill="both", expand=True, padx=pad, pady=(0, 8))

        # Add / update row
        add = ctk.CTkFrame(self, corner_radius=12)
        add.pack(fill="x", padx=pad, pady=(0, 8))
        add_row = ctk.CTkFrame(add, fg_color="transparent")
        add_row.pack(fill="x", padx=12, pady=12)
        ctk.CTkEntry(
            add_row, textvariable=self.prefix_var, width=90, height=36,
            placeholder_text="SKU",
        ).pack(side="left")
        ctk.CTkEntry(
            add_row, textvariable=self.name_var, height=36, placeholder_text="Client name",
        ).pack(side="left", fill="x", expand=True, padx=(8, 8))
        ctk.CTkButton(
            add_row, text="Add / Update", width=110, height=36, fg_color=GREEN,
            hover_color=GREEN_HOVER, command=self._add_update,
        ).pack(side="left")

        # Save / cancel
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.pack(fill="x", padx=pad, pady=(0, pad))
        ctk.CTkButton(
            bottom, text="Save", width=120, height=40, fg_color=GREEN,
            hover_color=GREEN_HOVER, font=("Segoe UI Semibold", 14), command=self._save,
        ).pack(side="right")
        ctk.CTkButton(
            bottom, text="Cancel", width=100, height=40, fg_color="transparent",
            text_color=MUTED, border_color=MUTED, border_width=1, hover_color="#f0f0f0",
            command=self.destroy,
        ).pack(side="right", padx=(0, 10))

        self._render_rows()

    def _render_rows(self) -> None:
        for w in self.list_frame.winfo_children():
            w.destroy()
        if not self.rows:
            ctk.CTkLabel(
                self.list_frame,
                text="No clients selected. Reports will auto-group by SKU prefix.",
                text_color=MUTED,
            ).pack(anchor="w", padx=8, pady=10)
            return
        for prefix, name in self.rows.items():
            row = ctk.CTkFrame(self.list_frame, fg_color="transparent")
            row.pack(fill="x", pady=3)
            ctk.CTkLabel(
                row, text=prefix, width=56, anchor="w", font=("Segoe UI Semibold", 13),
                text_color=GREEN_DARK,
            ).pack(side="left", padx=(4, 8))
            ctk.CTkLabel(row, text=name, anchor="w").pack(side="left", fill="x", expand=True)
            ctk.CTkButton(
                row, text="Remove", width=76, height=28, fg_color=RED, hover_color=RED_HOVER,
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
    ReportApp(root, dnd=dnd)
    root.mainloop()


if __name__ == "__main__":
    main()
