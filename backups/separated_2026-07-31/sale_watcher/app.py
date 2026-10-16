"""Live CTBids sale watcher — boss dashboard for bid activity + usernames."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import StringVar, filedialog, messagebox, ttk
import tkinter as tk

import customtkinter as ctk

from ctbids_client import BidChange, LiveItem, Snapshot, parse_sale_id
from export_report import export_snapshot
from watcher import (
    SaleWatcher,
    load_watcher_config,
    open_login_window,
    resolve_access_token,
    save_watcher_config,
)

APP_TITLE = "CTBids Sale Watcher"

THEMES = {
    # Matches Estate Inventory SaaS: charcoal surfaces + teal accent
    "light": {
        "green": "#2a9d9c",
        "green_dark": "#0f1720",
        "green_hover": "#238887",
        "green_soft": "#e6f6f5",
        "muted": "#6b7280",
        "amber": "#b58105",
        "amber_soft": "#fff6e0",
        "card_bg": "#f3f6f8",
        "row_alt": "#eef2f5",
        "panel": "#ffffff",
        "text": "#0f1720",
        "on_accent": "#0b0e14",
        "tree_bg": "#ffffff",
        "tree_fg": "#0f1720",
        "heading_bg": "#1a1f26",
        "heading_fg": "#e8eef2",
        "info_bg": "#e8eef2",
        "window": "#e8eef2",
        "success": "#22c55e",
        "danger": "#ef4444",
    },
    "dark": {
        "green": "#3fc1c0",
        "green_dark": "#e8eef2",
        "green_hover": "#2ba8a7",
        "green_soft": "#15353a",
        "muted": "#8b95a5",
        "amber": "#e0a84a",
        "amber_soft": "#3a2e14",
        "card_bg": "#151a21",
        "row_alt": "#1f252e",
        "panel": "#1a1f26",
        "text": "#e8eef2",
        "on_accent": "#0b0e14",
        "tree_bg": "#1a1f26",
        "tree_fg": "#e8eef2",
        "heading_bg": "#12161c",
        "heading_fg": "#c5ced8",
        "info_bg": "#222831",
        "window": "#0b0e14",
        "success": "#22c55e",
        "danger": "#ef4444",
    },
}

# Mutable palette used by widgets (updated when theme switches)
GREEN = THEMES["dark"]["green"]
GREEN_DARK = THEMES["dark"]["green_dark"]
GREEN_HOVER = THEMES["dark"]["green_hover"]
GREEN_SOFT = THEMES["dark"]["green_soft"]
MUTED = THEMES["dark"]["muted"]
AMBER = THEMES["dark"]["amber"]
AMBER_SOFT = THEMES["dark"]["amber_soft"]
CARD_BG = THEMES["dark"]["card_bg"]
ROW_ALT = THEMES["dark"]["row_alt"]
WHITE = THEMES["dark"]["panel"]
ON_ACCENT = THEMES["dark"]["on_accent"]


def _apply_palette(mode: str) -> dict:
    global GREEN, GREEN_DARK, GREEN_HOVER, GREEN_SOFT, MUTED, AMBER, AMBER_SOFT, CARD_BG, ROW_ALT, WHITE, ON_ACCENT
    t = THEMES["dark" if mode == "dark" else "light"]
    GREEN = t["green"]
    GREEN_DARK = t["green_dark"]
    GREEN_HOVER = t["green_hover"]
    GREEN_SOFT = t["green_soft"]
    MUTED = t["muted"]
    AMBER = t["amber"]
    AMBER_SOFT = t["amber_soft"]
    CARD_BG = t["card_bg"]
    ROW_ALT = t["row_alt"]
    WHITE = t["panel"]
    ON_ACCENT = t["on_accent"]
    return t


def _toast(title: str, body: str) -> None:
    """Best-effort Windows toast via PowerShell (no extra dependency)."""
    if not sys.platform.startswith("win"):
        return
    safe_title = title.replace("'", "")
    safe_body = body.replace("'", "")[:180]
    script = (
        f"[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        f"ContentType = WindowsRuntime] > $null; "
        f"$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        f"[Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
        f"$text = $template.GetElementsByTagName('text'); "
        f"$text.Item(0).AppendChild($template.CreateTextNode('{safe_title}')) | Out-Null; "
        f"$text.Item(1).AppendChild($template.CreateTextNode('{safe_body}')) | Out-Null; "
        f"$toast = [Windows.UI.Notifications.ToastNotification]::new($template); "
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Sale Watcher')"
        f".Show($toast)"
    )
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass


def _money(val: float) -> str:
    return f"${val:,.2f}"


ACCOUNT_STATUS_HELP = {
    "Suspended": (
        "CTBids suspension on file for this bidder.\n"
        "Reason is often unknown — check seller tools / CSV if needed."
    ),
    "Member": (
        "Bidder has completed CTBids membership / profile setup.\n"
        "Not a guarantee they are trustworthy."
    ),
    "Clean": (
        "No active CTBids suspension found for this bidder.\n"
        "Does not mean they are verified or risk-free."
    ),
    "—": "Sign in and wait for usernames to load account status.",
    "": "Sign in and wait for usernames to load account status.",
}

ACCT_COLUMN_HELP = (
    "Acct status from CTBids (buyer login):\n"
    "• Suspended — suspension on file (reason often unknown)\n"
    "• Member — membership/profile completed\n"
    "• Clean — no suspension found (not a full trust check)"
)


class TreeTooltip:
    """Simple hover tip for ttk.Treeview cells / headings."""

    def __init__(self, widget: tk.Widget) -> None:
        self.widget = widget
        self._tip: tk.Toplevel | None = None
        self._after_id: str | None = None
        self._text = ""

    def schedule(self, text: str, x: int, y: int) -> None:
        text = (text or "").strip()
        if not text:
            self.hide()
            return
        if text == self._text and self._tip is not None:
            return
        self._text = text
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
        self._after_id = self.widget.after(350, lambda: self._show(text, x, y))

    def _show(self, text: str, x: int, y: int) -> None:
        self.hide(clear_text=False)
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        try:
            tip.attributes("-topmost", True)
        except Exception:
            pass
        frame = tk.Frame(tip, background=MUTED, padx=1, pady=1)
        frame.pack(fill="both", expand=True)
        tk.Label(
            frame,
            text=text,
            justify="left",
            background=CARD_BG,
            foreground=GREEN_DARK,
            font=("Segoe UI", 10),
            padx=10,
            pady=8,
            wraplength=320,
        ).pack()
        tip.update_idletasks()
        tip.geometry(f"+{x + 16}+{y + 16}")
        self._tip = tip

    def hide(self, clear_text: bool = True) -> None:
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None
        if clear_text:
            self._text = ""


class MetricCard(ctk.CTkFrame):
    def __init__(self, parent, label: str, **kwargs) -> None:
        super().__init__(parent, corner_radius=12, fg_color=CARD_BG, **kwargs)
        self._warn = False
        self.label = ctk.CTkLabel(
            self, text=label.upper(), font=("Segoe UI", 11), text_color=MUTED, anchor="w",
        )
        self.label.pack(fill="x", padx=14, pady=(12, 0))
        self.value = ctk.CTkLabel(
            self, text="—", font=("Segoe UI Semibold", 22), text_color=GREEN, anchor="w",
        )
        self.value.pack(fill="x", padx=14, pady=(2, 12))

    def set(self, text: str, *, warn: bool = False) -> None:
        self._warn = warn
        self.value.configure(text=text, text_color=AMBER if warn else GREEN)

    def apply_theme(self) -> None:
        self.configure(fg_color=CARD_BG)
        self.label.configure(text_color=MUTED)
        self.value.configure(text_color=AMBER if self._warn else GREEN)


class WatcherApp:
    def __init__(self, root: ctk.CTk) -> None:
        self.root = root
        self.cfg = load_watcher_config()
        self.token: str | None = None
        self.watcher: SaleWatcher | None = None
        self._activity_empty = None
        self._bids_fingerprint = ""
        self._bid_items: dict[str, LiveItem] = {}
        self._search_after_id: str | None = None
        self._latest_snap: Snapshot | None = None

        root.title(APP_TITLE)
        root.geometry("1100x760")
        root.minsize(960, 680)

        self.sale_var = StringVar(value=str(self.cfg.get("sale_url") or ""))
        self.poll_var = StringVar(value=str(self.cfg.get("poll_seconds") or 45))
        self.filter_var = StringVar(value="bidding")
        self.search_var = StringVar()
        self.status_var = StringVar(value="Idle — sign in, then start watching.")
        self.login_var = StringVar(value="Not signed in")
        self.updated_var = StringVar(value="")
        self.theme_mode = "light" if str(self.cfg.get("theme", "dark")).lower() == "light" else "dark"

        _apply_palette(self.theme_mode)
        ctk.set_appearance_mode(self.theme_mode)
        self.root.configure(fg_color=THEMES[self.theme_mode]["window"])

        self._build_ui()
        self.search_var.trace_add("write", lambda *_: self._schedule_bid_render())
        self.root.after(200, self._try_load_token_quiet)

    def _build_ui(self) -> None:
        outer = ctk.CTkFrame(self.root, fg_color="transparent")
        outer.pack(fill="both", expand=True, padx=18, pady=14)

        # ----- Header -----
        header = ctk.CTkFrame(outer, fg_color="transparent")
        header.pack(fill="x")
        titles = ctk.CTkFrame(header, fg_color="transparent")
        titles.pack(side="left", fill="x", expand=True)
        self.title_label = ctk.CTkLabel(
            titles, text="Sale Watcher", font=("Segoe UI Semibold", 24), text_color=GREEN_DARK,
        )
        self.title_label.pack(anchor="w")
        self.subtitle_label = ctk.CTkLabel(
            titles,
            text="Live hammer total, top bids, and bidder usernames for your CTBids sale.",
            font=("Segoe UI", 13), text_color=MUTED,
        )
        self.subtitle_label.pack(anchor="w")

        header_right = ctk.CTkFrame(header, fg_color="transparent")
        header_right.pack(side="right", anchor="n", pady=(6, 0))
        self.theme_btn = ctk.CTkButton(
            header_right,
            text="Dark" if self.theme_mode == "light" else "Light",
            width=70, height=32,
            fg_color="transparent", text_color=GREEN, border_color=GREEN, border_width=1,
            hover_color=GREEN_SOFT, command=self._toggle_theme,
        )
        self.theme_btn.pack(side="left", padx=(0, 8))
        self.login_badge = ctk.CTkLabel(
            header_right, textvariable=self.login_var, font=("Segoe UI Semibold", 12),
            text_color=GREEN, fg_color=GREEN_SOFT, corner_radius=8, padx=12, pady=6,
        )
        self.login_badge.pack(side="left")

        # ----- Controls -----
        self.controls = ctk.CTkFrame(outer, corner_radius=12, fg_color=WHITE)
        self.controls.pack(fill="x", pady=(12, 0))

        row1 = ctk.CTkFrame(self.controls, fg_color="transparent")
        row1.pack(fill="x", padx=14, pady=(12, 8))
        self.sale_label = ctk.CTkLabel(row1, text="Sale", width=48, anchor="w", font=("Segoe UI Semibold", 13), text_color=GREEN_DARK)
        self.sale_label.pack(side="left")
        ctk.CTkEntry(
            row1, textvariable=self.sale_var, height=36,
            placeholder_text="https://ctbids.com/estate-sale/46500",
        ).pack(side="left", fill="x", expand=True, padx=(8, 8))
        self.every_label = ctk.CTkLabel(row1, text="every", text_color=MUTED)
        self.every_label.pack(side="left")
        ctk.CTkEntry(row1, textvariable=self.poll_var, width=56, height=36).pack(side="left", padx=(6, 4))
        self.sec_label = ctk.CTkLabel(row1, text="sec", text_color=MUTED)
        self.sec_label.pack(side="left")

        btns = ctk.CTkFrame(self.controls, fg_color="transparent")
        btns.pack(fill="x", padx=14, pady=(0, 12))
        self.signin_btn = ctk.CTkButton(
            btns, text="Sign in", width=100, height=36,
            fg_color="transparent", text_color=GREEN, border_color=GREEN, border_width=1,
            hover_color=GREEN_SOFT, command=self._on_login,
        )
        self.signin_btn.pack(side="left")
        self.start_btn = ctk.CTkButton(
            btns, text="Start", width=100, height=36,
            fg_color=GREEN, hover_color=GREEN_HOVER, text_color=ON_ACCENT,
            font=("Segoe UI Semibold", 13),
            command=self._on_start,
        )
        self.start_btn.pack(side="left", padx=(8, 0))
        self.stop_btn = ctk.CTkButton(
            btns, text="Stop", width=80, height=36,
            fg_color="transparent", text_color=MUTED, border_color=MUTED, border_width=1,
            command=self._on_stop, state="disabled",
        )
        self.stop_btn.pack(side="left", padx=(8, 0))
        self.opensale_btn = ctk.CTkButton(
            btns, text="Open sale", width=100, height=36,
            fg_color="transparent", text_color=GREEN, border_color=GREEN, border_width=1,
            hover_color=GREEN_SOFT, command=self._open_sale,
        )
        self.opensale_btn.pack(side="right")
        self.export_btn = ctk.CTkButton(
            btns, text="Export PDF", width=110, height=36,
            fg_color="transparent", text_color=GREEN, border_color=GREEN, border_width=1,
            hover_color=GREEN_SOFT, command=self._on_export_pdf,
        )
        self.export_btn.pack(side="right", padx=(0, 8))

        # ----- Metrics -----
        metrics = ctk.CTkFrame(outer, fg_color="transparent")
        metrics.pack(fill="x", pady=(12, 0))
        metrics.grid_columnconfigure((0, 1, 2, 3, 4), weight=1, uniform="m")
        self.metric_hammer = MetricCard(metrics, "Hammer total")
        self.metric_bidding = MetricCard(metrics, "Bidding")
        self.metric_open = MetricCard(metrics, "Still open")
        self.metric_bidders = MetricCard(metrics, "Bidders")
        self.metric_items = MetricCard(metrics, "Listed")
        for i, card in enumerate(
            (self.metric_hammer, self.metric_bidding, self.metric_open, self.metric_bidders, self.metric_items)
        ):
            card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 6, 0 if i == 4 else 6))

        # ----- Body: resizable bids | activity -----
        body = ctk.CTkFrame(outer, fg_color="transparent")
        body.pack(fill="both", expand=True, pady=(12, 0))

        self.paned = tk.PanedWindow(
            body,
            orient=tk.HORIZONTAL,
            sashwidth=8,
            sashrelief=tk.FLAT,
            bg=THEMES[self.theme_mode]["window"],
            bd=0,
            opaqueresize=True,
        )
        self.paned.pack(fill="both", expand=True)

        bids_panel = ctk.CTkFrame(self.paned, corner_radius=12, fg_color=WHITE)
        self.bids_panel = bids_panel

        bids_head = ctk.CTkFrame(bids_panel, fg_color="transparent")
        bids_head.pack(fill="x", padx=12, pady=(10, 6))
        self.bids_title = ctk.CTkLabel(
            bids_head, text="Top bids", font=("Segoe UI Semibold", 16), text_color=GREEN_DARK,
        )
        self.bids_title.pack(side="left")
        ctk.CTkEntry(
            bids_head, textvariable=self.search_var, width=180, height=32,
            placeholder_text="Search SKU, item, or bidder…",
        ).pack(side="right")
        self.view_btn = ctk.CTkButton(
            bids_head, text="View item", width=90, height=32,
            fg_color="transparent", text_color=GREEN, border_color=GREEN, border_width=1,
            hover_color=GREEN_SOFT, command=self._open_selected_bid,
        )
        self.view_btn.pack(side="right", padx=(0, 8))

        filters = ctk.CTkFrame(bids_panel, fg_color="transparent")
        filters.pack(fill="x", padx=12, pady=(0, 6))
        self.filter_menu = ctk.CTkSegmentedButton(
            filters,
            values=["Bidding", "Open", "All"],
            command=self._on_filter,
            selected_color=GREEN,
            selected_hover_color=GREEN_HOVER,
            unselected_color=GREEN_SOFT,
            unselected_hover_color=ROW_ALT,
            text_color=GREEN_DARK if self.theme_mode == "dark" else ON_ACCENT,
        )
        self.filter_menu.set("Bidding")
        self.filter_menu.pack(side="left")
        self.acct_legend = ctk.CTkLabel(
            filters,
            text="Hover Acct for meaning",
            font=("Segoe UI", 11),
            text_color=MUTED,
            anchor="w",
        )
        self.acct_legend.pack(side="left", padx=(12, 8))
        ctk.CTkLabel(
            filters, textvariable=self.updated_var, font=("Segoe UI", 11), text_color=MUTED,
        ).pack(side="right")

        # Native tree table — much smoother than rebuilding CTk rows every poll
        self.table_wrap = ctk.CTkFrame(bids_panel, fg_color=WHITE, corner_radius=0)
        self.table_wrap.pack(fill="both", expand=True, padx=12, pady=(0, 10))

        cols = ("sku", "price", "bids", "bidder", "status", "item", "category", "view")
        self.bids_tree = ttk.Treeview(
            self.table_wrap,
            columns=cols,
            show="headings",
            style="Bids.Treeview",
            selectmode="browse",
        )
        self.bids_tree.heading("sku", text="SKU", anchor="w")
        self.bids_tree.heading("price", text="Price", anchor="e")
        self.bids_tree.heading("bids", text="Bids", anchor="e")
        self.bids_tree.heading("bidder", text="Highest bidder", anchor="w")
        self.bids_tree.heading("status", text="Acct", anchor="w")
        self.bids_tree.heading("item", text="Item", anchor="w")
        self.bids_tree.heading("category", text="Category", anchor="w")
        self.bids_tree.heading("view", text="", anchor="center")
        self.bids_tree.column("sku", width=80, minwidth=60, anchor="w", stretch=False)
        self.bids_tree.column("price", width=85, minwidth=70, anchor="e", stretch=False)
        self.bids_tree.column("bids", width=50, minwidth=40, anchor="e", stretch=False)
        self.bids_tree.column("bidder", width=130, minwidth=90, anchor="w", stretch=False)
        self.bids_tree.column("status", width=85, minwidth=70, anchor="w", stretch=False)
        self.bids_tree.column("item", width=280, minwidth=140, anchor="w", stretch=True)
        self.bids_tree.column("category", width=110, minwidth=80, anchor="w", stretch=False)
        self.bids_tree.column("view", width=55, minwidth=50, anchor="center", stretch=False)

        scroll = ttk.Scrollbar(self.table_wrap, orient="vertical", command=self.bids_tree.yview)
        self.bids_tree.configure(yscrollcommand=scroll.set)
        self.bids_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.bids_tree.bind("<Double-1>", self._on_bid_double_click)
        self.bids_tree.bind("<Return>", self._on_bid_double_click)
        self.bids_tree.bind("<ButtonRelease-1>", self._on_bid_click)
        self._acct_tip = TreeTooltip(self.bids_tree)
        self.bids_tree.bind("<Motion>", self._on_tree_motion)
        self.bids_tree.bind("<Leave>", lambda _e: self._acct_tip.hide())
        self.bids_tree.bind("<<TreeviewSelect>>", lambda _e: self._acct_tip.hide())

        self._bids_placeholder = None

        # Activity panel
        self.act_panel = ctk.CTkFrame(self.paned, corner_radius=12, fg_color=WHITE)
        act_head = ctk.CTkFrame(self.act_panel, fg_color="transparent")
        act_head.pack(fill="x", padx=12, pady=(10, 6))
        self.act_title = ctk.CTkLabel(
            act_head, text="Activity", font=("Segoe UI Semibold", 16), text_color=GREEN_DARK,
        )
        self.act_title.pack(side="left")
        self.clear_act_btn = ctk.CTkButton(
            act_head, text="Clear", width=60, height=28,
            fg_color="transparent", text_color=MUTED, border_width=0,
            hover_color=GREEN_SOFT, command=self._clear_activity,
        )
        self.clear_act_btn.pack(side="right")

        self.activity_list = ctk.CTkScrollableFrame(self.act_panel, fg_color=CARD_BG, corner_radius=8)
        self.activity_list.pack(fill="both", expand=True, padx=8, pady=(0, 10))
        self._activity_empty = ctk.CTkLabel(
            self.activity_list, text="Bid changes will show up here.", text_color=MUTED,
        )
        self._activity_empty.pack(anchor="w", padx=6, pady=10)
        self._style_activity_list()

        self.paned.add(self.bids_panel, minsize=420, stretch="always")
        self.paned.add(self.act_panel, minsize=220, stretch="never")
        self.paned.bind("<ButtonRelease-1>", self._save_sash)
        self.root.after(100, self._restore_sash)

        # Status bar
        status = ctk.CTkFrame(outer, fg_color="transparent")
        status.pack(fill="x", pady=(8, 0))
        self.status_label = ctk.CTkLabel(
            status, textvariable=self.status_var, font=("Segoe UI", 12), text_color=MUTED, anchor="w",
        )
        self.status_label.pack(fill="x")

        self._show_bids_placeholder("Start watching to load live bids.")
        self._style_tree()

    def _style_activity_list(self) -> None:
        """Force the activity scroller to match the current theme (CTk leaves canvas light otherwise)."""
        bg = CARD_BG
        try:
            self.activity_list.configure(fg_color=bg)
        except Exception:
            pass
        try:
            canvas = self.activity_list._parent_canvas  # type: ignore[attr-defined]
            canvas.configure(bg=bg, highlightthickness=0, bd=0)
        except Exception:
            pass
        try:
            # Inner frame that holds the rows
            self.activity_list._scrollable_frame.configure(fg_color=bg)  # type: ignore[attr-defined]
        except Exception:
            pass

    def _save_sash(self, _event=None) -> None:
        try:
            pos = self.paned.sash_coord(0)[0]
            if pos > 100:
                self.cfg["sash_pos"] = int(pos)
                self.cfg["theme"] = self.theme_mode
                save_watcher_config(self.cfg)
        except Exception:
            pass

    def _restore_sash(self) -> None:
        pos = self.cfg.get("sash_pos")
        try:
            if pos:
                self.paned.sash_place(0, int(pos), 0)
            else:
                # Default: activity ~300px on the right
                width = max(self.paned.winfo_width(), 900)
                self.paned.sash_place(0, width - 320, 0)
        except Exception:
            pass

    def _toggle_theme(self) -> None:
        self.theme_mode = "dark" if self.theme_mode == "light" else "light"
        self.cfg["theme"] = self.theme_mode
        save_watcher_config(self.cfg)
        self._restyle_ui()

    def _style_tree(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(
            "Bids.Treeview",
            background=WHITE,
            foreground=GREEN_DARK,
            fieldbackground=WHITE,
            rowheight=34,
            font=("Segoe UI", 11),
            borderwidth=0,
        )
        style.configure(
            "Bids.Treeview.Heading",
            background=THEMES[self.theme_mode]["heading_bg"],
            foreground=THEMES[self.theme_mode]["heading_fg"],
            font=("Segoe UI Semibold", 11),
            relief="flat",
        )
        style.map(
            "Bids.Treeview",
            background=[("selected", GREEN_SOFT)],
            foreground=[("selected", GREEN)],
        )
        style.map("Bids.Treeview.Heading", background=[("active", GREEN_HOVER)])
        self.bids_tree.tag_configure("odd", background=ROW_ALT)
        self.bids_tree.tag_configure("even", background=WHITE)
        self.bids_tree.tag_configure("suspended", foreground=AMBER)
        self.bids_tree.tag_configure("member", foreground=GREEN)

    def _outline_btn(self, btn: ctk.CTkButton, *, muted: bool = False) -> None:
        if muted:
            btn.configure(fg_color="transparent", text_color=MUTED, border_color=MUTED, hover_color=GREEN_SOFT)
        else:
            btn.configure(fg_color="transparent", text_color=GREEN, border_color=GREEN, hover_color=GREEN_SOFT)

    def _restyle_ui(self) -> None:
        _apply_palette(self.theme_mode)
        ctk.set_appearance_mode(self.theme_mode)
        t = THEMES[self.theme_mode]
        self.root.configure(fg_color=t["window"])

        self.title_label.configure(text_color=GREEN_DARK)
        self.subtitle_label.configure(text_color=MUTED)
        self.theme_btn.configure(
            text="Dark" if self.theme_mode == "light" else "Light",
            text_color=GREEN, border_color=GREEN, hover_color=GREEN_SOFT,
        )
        signed_in = bool(self.token)
        self.login_badge.configure(
            text_color=GREEN if signed_in else AMBER,
            fg_color=GREEN_SOFT if signed_in else AMBER_SOFT,
        )

        self.controls.configure(fg_color=WHITE)
        self.sale_label.configure(text_color=GREEN_DARK)
        self.every_label.configure(text_color=MUTED)
        self.sec_label.configure(text_color=MUTED)
        self._outline_btn(self.signin_btn)
        self.start_btn.configure(fg_color=GREEN, hover_color=GREEN_HOVER, text_color=ON_ACCENT)
        self._outline_btn(self.stop_btn, muted=True)
        self._outline_btn(self.export_btn)
        self._outline_btn(self.opensale_btn)

        for card in (
            self.metric_hammer, self.metric_bidding, self.metric_open,
            self.metric_bidders, self.metric_items,
        ):
            card.apply_theme()

        self.bids_panel.configure(fg_color=WHITE)
        self.act_panel.configure(fg_color=WHITE)
        self.table_wrap.configure(fg_color=WHITE)
        self.bids_title.configure(text_color=GREEN_DARK)
        self.act_title.configure(text_color=GREEN_DARK)
        self._outline_btn(self.view_btn)
        self.clear_act_btn.configure(text_color=MUTED, hover_color=GREEN_SOFT)
        self.status_label.configure(text_color=MUTED)
        if getattr(self, "acct_legend", None) is not None:
            self.acct_legend.configure(text_color=MUTED)
        self.filter_menu.configure(
            selected_color=GREEN,
            selected_hover_color=GREEN_HOVER,
            unselected_color=GREEN_SOFT,
            unselected_hover_color=t["row_alt"],
            text_color=GREEN_DARK if self.theme_mode == "dark" else ON_ACCENT,
        )
        try:
            self.paned.configure(bg=t["window"])
        except Exception:
            pass
        self._style_activity_list()
        self.root.after(50, self._style_activity_list)
        if getattr(self, "_activity_empty", None) is not None:
            try:
                self._activity_empty.configure(text_color=MUTED)
            except Exception:
                pass

        self._style_tree()
        self._bids_fingerprint = ""
        self._render_bids()

    # ----- UI helpers -----
    def _on_filter(self, value: str) -> None:
        mapping = {"Bidding": "bidding", "Open": "open", "All": "all"}
        self.filter_var.set(mapping.get(value, "bidding"))
        self._bids_fingerprint = ""  # force refresh
        self._render_bids()

    def _schedule_bid_render(self) -> None:
        if self._search_after_id is not None:
            try:
                self.root.after_cancel(self._search_after_id)
            except Exception:
                pass
        self._bids_fingerprint = ""
        self._search_after_id = self.root.after(150, self._render_bids)

    def _clear_rows(self, frame) -> None:
        for child in frame.winfo_children():
            child.destroy()

    def _show_bids_placeholder(self, text: str) -> None:
        self.bids_tree.delete(*self.bids_tree.get_children())
        self._bid_items.clear()
        self._bids_fingerprint = f"placeholder:{text}"
        # Treeview can't show a nice empty state row easily — status line covers it
        self.updated_var.set(text)

    def _append_activity(self, message: str, *, kind: str = "info") -> None:
        if getattr(self, "_activity_empty", None) is not None:
            try:
                if self._activity_empty.winfo_exists():
                    self._activity_empty.destroy()
            except Exception:
                pass
            self._activity_empty = None

        colors = {
            "first_bid": (AMBER_SOFT, AMBER, "FIRST BID"),
            "price_up": (GREEN_SOFT, GREEN, "BID UP"),
            "bidder_change": (GREEN_SOFT, GREEN_DARK, "BIDDER"),
            "info": (THEMES[self.theme_mode]["info_bg"], MUTED, "INFO"),
        }
        bg, fg, tag = colors.get(kind, colors["info"])
        stamp = datetime.now().strftime("%I:%M:%S %p").lstrip("0")

        # Append only — no full rebuild (keeps UI smooth)
        row = ctk.CTkFrame(self.activity_list, fg_color=bg, corner_radius=8)
        row.pack(fill="x", pady=3, padx=2)
        top = ctk.CTkFrame(row, fg_color="transparent")
        top.pack(fill="x", padx=10, pady=(8, 0))
        ctk.CTkLabel(
            top, text=tag, font=("Segoe UI Semibold", 11), text_color=fg, anchor="w",
        ).pack(side="left")
        ctk.CTkLabel(top, text=stamp, font=("Segoe UI", 10), text_color=MUTED).pack(side="right")
        ctk.CTkLabel(
            row, text=message, font=("Segoe UI", 12), text_color=GREEN_DARK,
            anchor="w", justify="left", wraplength=270,
        ).pack(fill="x", padx=10, pady=(2, 8))

        kids = self.activity_list.winfo_children()
        while len(kids) > 50:
            kids[0].destroy()
            kids = self.activity_list.winfo_children()

        # Keep newest visible
        try:
            self.activity_list._parent_canvas.yview_moveto(1.0)  # type: ignore[attr-defined]
        except Exception:
            pass

    def _clear_activity(self) -> None:
        self._clear_rows(self.activity_list)
        self._style_activity_list()
        self._activity_empty = ctk.CTkLabel(
            self.activity_list, text="Bid changes will show up here.", text_color=MUTED,
        )
        self._activity_empty.pack(anchor="w", padx=6, pady=10)

    def _open_item(self, item: LiveItem) -> None:
        if item.link:
            webbrowser.open(item.link)

    def _on_bid_double_click(self, _event=None) -> None:
        sel = self.bids_tree.selection()
        if not sel:
            return
        item = self._bid_items.get(sel[0])
        if item:
            self._open_item(item)

    def _open_selected_bid(self) -> None:
        sel = self.bids_tree.selection()
        if not sel:
            messagebox.showinfo(APP_TITLE, "Select a row in the table first, then click View item.")
            return
        item = self._bid_items.get(sel[0])
        if item:
            self._open_item(item)

    def _on_tree_motion(self, event) -> None:
        region = self.bids_tree.identify_region(event.x, event.y)
        col = self.bids_tree.identify_column(event.x)
        # status is column #5 (1=sku … 5=status)
        if col != "#5":
            self._acct_tip.hide()
            return
        if region == "heading":
            self._acct_tip.schedule(
                ACCT_COLUMN_HELP,
                event.x_root,
                event.y_root,
            )
            return
        if region != "cell":
            self._acct_tip.hide()
            return
        row_id = self.bids_tree.identify_row(event.y)
        item = self._bid_items.get(row_id) if row_id else None
        if not item:
            self._acct_tip.hide()
            return
        status = item.account_status or "—"
        tip = ACCOUNT_STATUS_HELP.get(status, ACCOUNT_STATUS_HELP["Clean"])
        if item.high_bidder:
            tip = f"{item.high_bidder} — {status}\n\n{tip}"
        self._acct_tip.schedule(tip, event.x_root, event.y_root)

    def _on_bid_click(self, event) -> None:
        """Single-click the View cell to open the item page."""
        if self.bids_tree.identify_region(event.x, event.y) != "cell":
            return
        if self.bids_tree.identify_column(event.x) != "#8":  # view column
            return
        row_id = self.bids_tree.identify_row(event.y)
        if not row_id:
            return
        item = self._bid_items.get(row_id)
        if item:
            self._open_item(item)

    def _filtered_rows(self, snap: Snapshot) -> list[LiveItem]:
        mode = self.filter_var.get()
        query = self.search_var.get().strip().lower()
        ranked = sorted(
            snap.items.values(),
            key=lambda it: (-it.bid_price, -it.bid_count, it.title.lower()),
        )
        rows: list[LiveItem] = []
        for it in ranked:
            bidding = it.bid_price > 0 or it.bid_count > 0
            if mode == "bidding" and not bidding:
                continue
            if mode == "open" and bidding:
                continue
            if query:
                hay = f"{it.sku} {it.title} {it.high_bidder} {it.account_status} {it.category}".lower()
                if query not in hay:
                    continue
            rows.append(it)
            # Show every matching item — low $1 first bids were getting dropped by a top-100 cut
        return rows

    @staticmethod
    def _row_values(item: LiveItem) -> tuple[str, str, str, str, str, str, str, str]:
        bidding = item.bid_price > 0 or item.bid_count > 0
        price = _money(item.bid_price) if bidding else "—"
        bids = str(item.bid_count) if bidding else "—"
        bidder = item.high_bidder if item.high_bidder else ("—" if bidding else "")
        status = item.account_status if item.high_bidder else ("—" if bidding else "")
        return (
            item.sku or "—",
            price,
            bids,
            bidder or "—",
            status or "—",
            item.title,
            item.category or "",
            "View",
        )

    def _render_bids(self) -> None:
        snap = self._latest_snap
        if snap is None:
            return

        rows = self._filtered_rows(snap)
        fingerprint = "|".join(
            f"{it.item_id}:{it.bid_price}:{it.bid_count}:{it.high_bidder}:{it.account_status}"
            for it in rows
        )
        fingerprint = f"{self.filter_var.get()}::{self.search_var.get().strip().lower()}::{fingerprint}"
        if fingerprint == self._bids_fingerprint:
            return
        self._bids_fingerprint = fingerprint

        new_ids = [str(it.item_id) for it in rows]
        old_ids = list(self.bids_tree.get_children())

        # Fast path: same items in same order — update cell values only (no flicker)
        if new_ids == old_ids:
            for i, it in enumerate(rows):
                iid = str(it.item_id)
                tag = "odd" if i % 2 else "even"
                tags = [tag]
                if it.suspended:
                    tags.append("suspended")
                elif it.membership_completed:
                    tags.append("member")
                self.bids_tree.item(iid, values=self._row_values(it), tags=tuple(tags))
                self._bid_items[iid] = it
            return

        selected_ids = {
            self._bid_items[i].item_id
            for i in self.bids_tree.selection()
            if i in self._bid_items
        }
        try:
            y_pos = self.bids_tree.yview()[0]
        except Exception:
            y_pos = 0.0

        self.bids_tree.delete(*old_ids)
        self._bid_items.clear()

        if not rows:
            self.updated_var.set("No items match this filter.")
            return

        for i, it in enumerate(rows):
            iid = str(it.item_id)
            tag = "odd" if i % 2 else "even"
            tags = [tag]
            if it.suspended:
                tags.append("suspended")
            elif it.membership_completed:
                tags.append("member")
            self.bids_tree.insert("", "end", iid=iid, values=self._row_values(it), tags=tuple(tags))
            self._bid_items[iid] = it
            if it.item_id in selected_ids:
                self.bids_tree.selection_add(iid)

        try:
            self.bids_tree.yview_moveto(y_pos)
        except Exception:
            pass

    def _set_metrics(self, snap: Snapshot) -> None:
        self.metric_hammer.set(_money(snap.hammer_total))
        self.metric_bidding.set(str(snap.bidding_count))
        self.metric_open.set(str(snap.open_count), warn=snap.open_count > 0)
        self.metric_bidders.set(str(snap.unique_bidders))
        self.metric_items.set(str(len(snap.items)))
        self.updated_var.set(
            f"Updated {datetime.now().strftime('%I:%M:%S %p').lstrip('0')}  ·  "
            f"click View or double-click a row"
        )

    # ----- Auth / actions -----
    def _try_load_token_quiet(self) -> None:
        def work() -> None:
            try:
                token = resolve_access_token()
            except Exception:
                token = None
            self.root.after(0, self._apply_token, token, False)

        threading.Thread(target=work, daemon=True).start()

    def _apply_token(self, token: str | None, announce: bool) -> None:
        self.token = token
        if self.watcher:
            self.watcher.set_token(token)
        if token:
            self.login_var.set("Signed in")
            self.login_badge.configure(text_color=GREEN, fg_color=GREEN_SOFT)
            if announce:
                self.status_var.set("Signed in. Usernames will fill in on the next refresh.")
                self._append_activity("Signed in — fetching usernames on next pass.", kind="info")
        else:
            self.login_var.set("Not signed in")
            self.login_badge.configure(text_color=AMBER, fg_color=AMBER_SOFT)
            if announce:
                self.status_var.set("Not signed in — prices still work; Sign in for usernames.")

    def _on_auth_lost(self, message: str) -> None:
        self.token = None
        self.login_var.set("Not signed in")
        self.login_badge.configure(text_color=AMBER, fg_color=AMBER_SOFT)
        self.status_var.set(message)
        self._append_activity(message, kind="info")

    def _on_login(self) -> None:
        sale = self.sale_var.get().strip() or "https://ctbids.com/"
        self.status_var.set("Opening CTBids — sign in, then close the browser window.")
        self.root.update_idletasks()

        def work() -> None:
            try:
                token = open_login_window(sale)
            except Exception as exc:
                self.root.after(0, lambda: messagebox.showerror(APP_TITLE, str(exc)))
                return
            self.root.after(0, self._apply_token, token, True)

        threading.Thread(target=work, daemon=True).start()

    def _open_sale(self) -> None:
        url = self.sale_var.get().strip()
        if not url:
            messagebox.showwarning(APP_TITLE, "Paste the sale URL first.")
            return
        try:
            sale_id = parse_sale_id(url)
            webbrowser.open(f"https://ctbids.com/estate-sale/{sale_id}")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _on_start(self) -> None:
        sale = self.sale_var.get().strip()
        if not sale:
            messagebox.showwarning(APP_TITLE, "Paste your CTBids sale URL first.")
            return
        try:
            parse_sale_id(sale)
            poll = float(self.poll_var.get().strip() or "45")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return

        self.cfg["sale_url"] = sale
        self.cfg["poll_seconds"] = poll
        self.cfg["theme"] = self.theme_mode
        save_watcher_config(self.cfg)

        self.status_var.set("Checking CTBids login...")
        self.root.update_idletasks()
        try:
            fresh = resolve_access_token()
            if fresh:
                self.token = fresh
        except Exception:
            pass
        self._apply_token(self.token, False)
        if not self.token:
            if not messagebox.askyesno(
                APP_TITLE,
                "No CTBids login found.\n\n"
                "You can still watch prices, but usernames will be blank.\n\n"
                "Start anyway? (Or cancel and click Sign in first.)",
            ):
                self.status_var.set("Start cancelled — sign in first for usernames.")
                return

        if self.watcher:
            self.watcher.stop()

        self.watcher = SaleWatcher(
            sale,
            access_token=self.token,
            poll_seconds=poll,
            on_update=lambda snap, changes: self.root.after(0, self._on_update, snap, changes),
            on_status=lambda msg: self.root.after(0, self.status_var.set, msg),
            on_auth_lost=lambda msg: self.root.after(0, self._on_auth_lost, msg),
        )
        self.watcher.start()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_var.set("Watching… first refresh starting.")
        self._append_activity(
            "Watcher started" + (" — loading usernames" if self.token else " — prices only"),
            kind="info",
        )

    def _on_stop(self) -> None:
        if self.watcher:
            self.watcher.stop()
            self.watcher = None
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.status_var.set("Stopped.")
        self._append_activity("Watcher stopped.", kind="info")

    def _on_export_pdf(self) -> None:
        snap = self._latest_snap
        if self.watcher and self.watcher.latest:
            snap = self.watcher.latest
            self._latest_snap = snap
        if not snap or not snap.items:
            messagebox.showwarning(
                APP_TITLE,
                "No sale data to export yet.\nStart watching (or wait for the first refresh), then try again.",
            )
            return

        default_name = f"sale-{snap.sale_id}-watcher-{datetime.now().strftime('%Y%m%d-%H%M')}.pdf"
        path = filedialog.asksaveasfilename(
            title="Export sale PDF",
            defaultextension=".pdf",
            initialfile=default_name,
            filetypes=[("PDF", "*.pdf"), ("All files", "*.*")],
        )
        if not path:
            return

        self.status_var.set("Exporting PDF…")
        self.export_btn.configure(state="disabled")
        sale_url = self.sale_var.get().strip()

        def work() -> None:
            try:
                pdf_path, html_path = export_snapshot(
                    snap, Path(path), sale_url=sale_url, also_html=True,
                )
                msg = f"Saved:\n{pdf_path}"
                if html_path and html_path != pdf_path:
                    msg += f"\n\nHTML copy:\n{html_path}"
                self.root.after(0, lambda: self._export_done(True, msg, str(pdf_path)))
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                self.root.after(0, lambda: self._export_done(False, err, ""))

        threading.Thread(target=work, daemon=True).start()

    def _export_done(self, ok: bool, message: str, pdf_path: str) -> None:
        self.export_btn.configure(state="normal")
        if ok:
            self.status_var.set(f"Exported PDF — {Path(pdf_path).name}")
            self._append_activity(f"Exported PDF: {Path(pdf_path).name}", kind="info")
            messagebox.showinfo(APP_TITLE, message)
            try:
                if pdf_path and Path(pdf_path).suffix.lower() == ".pdf":
                    os.startfile(pdf_path)  # type: ignore[attr-defined]
                elif pdf_path:
                    webbrowser.open(Path(pdf_path).resolve().as_uri())
            except Exception:
                pass
        else:
            self.status_var.set("Export failed.")
            messagebox.showerror(APP_TITLE, message)

    def _on_update(self, snap: Snapshot, changes: list[BidChange]) -> None:
        self._latest_snap = snap
        self._set_metrics(snap)
        self._render_bids()

        focus_ids: list[int] = []
        for ch in changes:
            # Prefer a cleaner activity line
            sku = f"{ch.item.sku} · " if ch.item.sku else ""
            title = f"{sku}{ch.item.title[:70]}"
            if ch.kind == "first_bid":
                msg = f"{title}\n{_money(ch.item.bid_price)}"
                if ch.item.high_bidder:
                    msg += f" · {ch.item.high_bidder}"
                if ch.item.account_status:
                    msg += f" · {ch.item.account_status}"
                focus_ids.append(ch.item.item_id)
            elif ch.kind == "price_up":
                msg = f"{title}\n{_money(ch.old_price)} → {_money(ch.item.bid_price)}"
                if ch.item.high_bidder:
                    msg += f" · {ch.item.high_bidder}"
                if ch.item.account_status:
                    msg += f" · {ch.item.account_status}"
            elif ch.kind == "bidder_change":
                msg = f"{title}\n{ch.old_bidder} → {ch.item.high_bidder}"
                if ch.item.account_status:
                    msg += f" · {ch.item.account_status}"
            else:
                msg = ch.message
            self._append_activity(msg, kind=ch.kind)
            if ch.item.suspended and ch.kind in ("first_bid", "price_up", "bidder_change"):
                self._append_activity(
                    f"SUSPENDED ACCOUNT: {ch.item.high_bidder or '?'} on {ch.item.sku or ch.item.title[:40]}",
                    kind="first_bid",
                )
                _toast("Suspended bidder", f"{ch.item.high_bidder} · {ch.item.sku or ch.item.title[:40]}")
            elif ch.kind in ("first_bid", "price_up"):
                toast_title = ch.item.sku or "Sale Watcher"
                _toast(toast_title, ch.message)

        if focus_ids:
            self._focus_bid_row(focus_ids[0])

    def _focus_bid_row(self, item_id: int) -> None:
        iid = str(item_id)
        if not self.bids_tree.exists(iid):
            return
        try:
            self.bids_tree.selection_set(iid)
            self.bids_tree.see(iid)
            self.bids_tree.focus(iid)
        except Exception:
            pass


def main() -> None:
    ctk.set_default_color_theme("dark-blue")
    root = ctk.CTk()
    root.configure(fg_color=THEMES["dark"]["window"])
    WatcherApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
