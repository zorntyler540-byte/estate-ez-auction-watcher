"""Live CTBids sale watcher — boss dashboard for bid activity + usernames."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from tkinter import BooleanVar, StringVar, filedialog, messagebox, ttk
import tkinter as tk
import tkinter.font as tkfont

import customtkinter as ctk

# Allow both `python app.py` (inside sale_watcher) and `from sale_watcher.app import …`
_WATCHER_DIR = Path(__file__).resolve().parent
_ROOT = _WATCHER_DIR.parent
if str(_WATCHER_DIR) not in sys.path:
    sys.path.insert(0, str(_WATCHER_DIR))
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from brand import APP_NAME as APP_TITLE, COPYRIGHT
from runtime_paths import prepare_runtime
from ctbids_client import BidChange, LiveItem, Snapshot, buyer_sale_url, item_link, parse_sale_id
from export_report import export_snapshot
from seller_client import REVOKE_TYPES, SELLER_SITE, SUSPEND_TYPES, SellerClient, SellerSale
from watcher import (
    BuyerMfaRequired,
    EstateEzAuthError,
    EstateEzSellerNotConnected,
    SaleWatcher,
    clear_saved_password,
    keep_sessions_fresh,
    load_estate_ez_token,
    load_saved_password,
    load_saved_username,
    load_watcher_config,
    login_with_password,
    open_login_window,
    open_seller_login_window,
    resolve_access_token,
    resolve_seller_access_token,
    save_login_credentials,
    save_watcher_config,
    send_buyer_mfa_otp,
    sign_in_estate_ez,
    verify_buyer_mfa,
)

prepare_runtime()

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
        "unmet": "#c9a00a",
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
        "okayed": "#1d4ed8",
        "okayed_soft": "#dbeafe",
    },
    "dark": {
        "green": "#3fc1c0",
        "green_dark": "#e8eef2",
        "green_hover": "#2ba8a7",
        "green_soft": "#15353a",
        "muted": "#8b95a5",
        "amber": "#e0a84a",
        "amber_soft": "#3a2e14",
        "unmet": "#f5d020",
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
        "okayed": "#7dd3fc",
        "okayed_soft": "#16344a",
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
UNMET = THEMES["dark"]["unmet"]
RED = THEMES["dark"]["danger"]
CARD_BG = THEMES["dark"]["card_bg"]
ROW_ALT = THEMES["dark"]["row_alt"]
WHITE = THEMES["dark"]["panel"]
ON_ACCENT = THEMES["dark"]["on_accent"]
OKAYED = THEMES["dark"]["okayed"]
OKAYED_SOFT = THEMES["dark"]["okayed_soft"]


def _apply_palette(mode: str) -> dict:
    global GREEN, GREEN_DARK, GREEN_HOVER, GREEN_SOFT, MUTED, AMBER, AMBER_SOFT, UNMET, RED, CARD_BG, ROW_ALT, WHITE, ON_ACCENT, OKAYED, OKAYED_SOFT
    t = THEMES["dark" if mode == "dark" else "light"]
    GREEN = t["green"]
    GREEN_DARK = t["green_dark"]
    GREEN_HOVER = t["green_hover"]
    GREEN_SOFT = t["green_soft"]
    MUTED = t["muted"]
    AMBER = t["amber"]
    AMBER_SOFT = t["amber_soft"]
    UNMET = t["unmet"]
    RED = t["danger"]
    CARD_BG = t["card_bg"]
    ROW_ALT = t["row_alt"]
    WHITE = t["panel"]
    ON_ACCENT = t["on_accent"]
    OKAYED = t["okayed"]
    OKAYED_SOFT = t["okayed_soft"]
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
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Estate-Ez Auction Watcher')"
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


def format_reserve(item: LiveItem) -> str:
    if item.reserve_price <= 0:
        return "—"
    return _money(item.reserve_price)


def format_met(item: LiveItem) -> str:
    if item.reserve_price <= 0:
        return "—"
    if item.bid_price >= item.reserve_price:
        return "Met"
    gap = item.reserve_price - item.bid_price
    return f"{_money(gap)} under"


def _below_reserve(item: LiveItem) -> bool:
    return item.reserve_price > 0 and item.bid_price < item.reserve_price


def _bidding_under(item: LiveItem) -> bool:
    return item.has_bids and not item.is_sold and _below_reserve(item)


def _reserve_help_text(item: LiveItem) -> str:
    if item.reserve_price <= 0:
        return "No reserve on this lot."
    price = _money(item.bid_price) if item.has_bids else "no bids yet"
    reserve = _money(item.reserve_price)
    if item.bid_price >= item.reserve_price:
        return f"Reserve met.\nHigh bid {price}  ·  reserve {reserve}"
    gap = _money(item.reserve_price - item.bid_price)
    return f"Still {gap} under reserve.\nHigh bid {price}  ·  reserve {reserve}"


def _parse_close_time(raw: str) -> datetime | None:
    """Parse CTBids item close time (usually UTC, with or without timezone)."""
    text = (raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        dt = None
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def format_time_left(item: LiveItem) -> str:
    """Human countdown to item close, e.g. 2d 5h · 45m · 12s."""
    if item.is_sold or item.is_closed:
        return "Ended"
    close = _parse_close_time(item.close_time)
    if close is None:
        return "—"
    secs = int((close - datetime.now(timezone.utc)).total_seconds())
    if secs <= 0:
        return "Ending…"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    if days > 0:
        text = f"{days}d {hours}h"
    elif hours > 0:
        text = f"{hours}h {minutes}m"
    elif minutes > 0:
        text = f"{minutes}m {seconds:02d}s"
    else:
        text = f"{seconds}s"
    # Marker only in this cell — don't paint the whole row
    if secs <= 300:
        return f"⚠ {text}"
    return text


ACCOUNT_STATUS_HELP = {
    "Sold": (
        "This lot has closed. The price shown is the final sold / hammer amount.\n"
        "It stays on the list so you can still see what it brought."
    ),
    "Suspended": (
        "CTBids has a suspension on file for this bidder.\n"
        "It may be from another Caring Transitions franchise, not yours.\n"
        "Use Suspend at our locations if you want to block them here."
    ),
    "Suspended · Unpaid": (
        "CTBids shows a suspension (possibly another franchise), and unpaid invoice(s).\n"
        "Treat as high risk — confirm in Seller before awarding / pickup."
    ),
    "Suspended · Clear": (
        "CTBids shows a suspension (possibly another franchise), but no unpaid invoices.\n"
        "That flag is not a ban at your locations unless you suspend them here."
    ),
    "Unpaid": (
        "CT Seller shows unpaid invoice(s) for this bidder "
        "(even if they are not suspended)."
    ),
    "Voids": (
        "CT Seller shows voided invoice(s) on this bidder's invoice tab.\n"
        "Same list as CT Seller → buyer profile → Status = Void."
    ),
    "Paid": (
        "CT Seller shows paid invoice(s) on file for this bidder.\n"
        "No active suspension / unpaid found."
    ),
    "Member": (
        "Bidder has completed CTBids membership / profile setup.\n"
        "Not a guarantee they are trustworthy."
    ),
    "Okayed": (
        "Staff reviewed this bidder and marked them okay.\n"
        "Blue row — they drop off At risk / Risky bidders.\n"
        "Does not change CT Seller. Right-click to undo."
    ),
    "Clean": (
        "No active CTBids suspension found for this bidder (any franchise).\n"
        "Does not mean they are verified or risk-free."
    ),
    "—": "Sign in and wait for usernames to load account status.",
    "": "Sign in and wait for usernames to load account status.",
}

ACCT_COLUMN_HELP = (
    "Acct / lot status:\n"
    "• Red row = Suspended on CTBids (may be another franchise, not yours)\n"
    "• Blue row = Okayed — staff reviewed this bidder and they are not a concern\n"
    "• Unpaid invoices column = yellow marker when count > 0\n"
    "• Voids column = voided invoices on their CT Seller invoice tab\n"
    "• Paid invoices column = green marker when count > 0\n"
    "• Sold — lot closed; price is the final hammer\n"
    "• Member / Clean — account flags (not a full trust check)"
)


def format_acct_label(item: LiveItem, *, okayed: bool = False) -> str:
    """Acct column only — invoice counts live in Unpaid invoices / Paid invoices."""
    if item.is_sold:
        return "Sold"
    if item.is_closed and not item.is_sold:
        return item.item_status or "Closed"
    if not item.high_bidder:
        return "—" if item.has_bids else ""
    if okayed:
        return "Okayed"
    if item.suspended:
        return "Suspended"
    if item.membership_completed:
        return "Member"
    return "Clean"


def _item_is_at_risk(item: LiveItem, okayed: set[int] | None = None) -> bool:
    """Suspended and/or unpaid invoices — the people staff should glance at first."""
    if not (item.high_bidder or "").strip():
        return False
    bid = int(item.buyer_id or 0)
    if okayed and bid and bid in okayed:
        return False
    return bool(item.suspended or item.has_unpaid or item.has_voids)


def _invoice_count_cell(count: int | None, *, hot: str, cold: str) -> str:
    """Keep the digit under the same x as a marked count (marker takes a slot even at 0)."""
    if count is None:
        return f"{cold} —"
    n = int(count)
    return f"{hot if n > 0 else cold} {n}"


def format_unpaid_cell(item: LiveItem) -> str:
    return _invoice_count_cell(item.unpaid_invoice_count, hot="🟡", cold="⚪")


def format_paid_cell(item: LiveItem) -> str:
    return _invoice_count_cell(item.paid_invoice_count, hot="🟢", cold="⚪")


def format_void_cell(item: LiveItem) -> str:
    return _invoice_count_cell(item.void_invoice_count, hot="⬛", cold="⬜")


def _acct_help_text(status: str, item: LiveItem | None = None) -> str:
    status = (status or "").strip()
    if status.startswith("Suspended · Unpaid"):
        base = ACCOUNT_STATUS_HELP["Suspended · Unpaid"]
    elif status.startswith("Suspended · Clear"):
        base = ACCOUNT_STATUS_HELP["Suspended · Clear"]
    elif status.startswith("Unpaid"):
        base = ACCOUNT_STATUS_HELP["Unpaid"]
    elif status.startswith("Paid"):
        base = ACCOUNT_STATUS_HELP["Paid"]
    elif status.startswith("Member"):
        base = ACCOUNT_STATUS_HELP["Member"]
    elif status.startswith("Suspended"):
        base = ACCOUNT_STATUS_HELP["Suspended"]
    elif status in ACCOUNT_STATUS_HELP:
        base = ACCOUNT_STATUS_HELP[status]
    else:
        base = ACCOUNT_STATUS_HELP["Clean"]
    if item is not None and (
        item.paid_invoice_count is not None
        or item.unpaid_invoice_count is not None
        or item.void_invoice_count is not None
    ):
        paid = item.paid_invoice_count if item.paid_invoice_count is not None else "—"
        unpaid = item.unpaid_invoice_count if item.unpaid_invoice_count is not None else "—"
        voids = item.void_invoice_count if item.void_invoice_count is not None else "—"
        base = f"{base}\n\nInvoices (CT Seller): {paid} paid · {unpaid} unpaid · {voids} void"
    return base


def apply_sizable_main_window(root, cfg: dict | None = None) -> None:
    """Let staff drag or maximize the Live Watch window; fill a laptop screen."""
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        root.resizable(True, True)
    except Exception:
        pass
    try:
        root.minsize(860, 540)
    except Exception:
        pass
    saved = str(cfg.get("window_geometry") or "").strip()
    saved_max = bool(cfg.get("window_maximized"))

    def _place() -> None:
        try:
            root.resizable(True, True)
        except Exception:
            pass
        try:
            sw = int(root.winfo_screenwidth() or 1280)
            sh = int(root.winfo_screenheight() or 800)
        except Exception:
            sw, sh = 1280, 800
        geo = saved
        if geo:
            try:
                root.geometry(geo)
            except Exception:
                geo = ""
        if not geo:
            w = max(860, min(sw - 24, 1680))
            h = max(540, min(sh - 80, 1040))
            try:
                root.geometry(f"{w}x{h}+0+0")
            except Exception:
                pass
        laptop = sw <= 1680 or sh <= 1000
        if saved_max or (not saved and laptop):
            try:
                root.state("zoomed")
            except Exception:
                pass

    try:
        root.after(80, _place)
        root.after(400, _place)
        root.after(1200, lambda: root.resizable(True, True))
    except Exception:
        _place()


def save_main_window_layout(root, cfg: dict) -> None:
    if not isinstance(cfg, dict):
        return
    try:
        cfg["window_maximized"] = str(root.state()) == "zoomed"
        cfg["window_geometry"] = root.geometry()
        save_watcher_config(cfg)
    except Exception:
        pass


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
            wraplength=380,
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
    def __init__(self, parent, label: str, command=None, **kwargs) -> None:
        super().__init__(parent, corner_radius=8, fg_color=CARD_BG, **kwargs)
        self._warn = False
        self._command = command
        inner = ctk.CTkFrame(self, fg_color="transparent")
        inner.pack(fill="x", padx=8, pady=3)
        self.label = ctk.CTkLabel(
            inner, text=label.upper(), font=("Segoe UI", 10), text_color=MUTED, anchor="w",
        )
        self.label.pack(side="left")
        self.value = ctk.CTkLabel(
            inner, text="—", font=("Segoe UI Semibold", 13), text_color=GREEN, anchor="e",
        )
        self.value.pack(side="right")
        if command:
            for widget in (self, inner, self.label, self.value):
                widget.bind("<Button-1>", lambda _e: command())
            try:
                self.configure(cursor="hand2")
                self.label.configure(cursor="hand2")
                self.value.configure(cursor="hand2")
            except Exception:
                pass

    def set(self, text: str, *, warn: bool = False) -> None:
        self._warn = warn
        self.value.configure(text=text, text_color=AMBER if warn else GREEN)

    def apply_theme(self) -> None:
        self.configure(fg_color=CARD_BG)
        self.label.configure(text_color=MUTED)
        self.value.configure(text_color=AMBER if self._warn else GREEN)


class _WatchSession:
    """One sale's watcher + UI state so Live Watch can keep several tabs alive."""

    def __init__(self, sale_id: int, sale_url: str, title: str = "") -> None:
        self.sale_id = int(sale_id)
        self.sale_url = sale_url
        self.title = (title or f"Sale {sale_id}").strip()
        self.watcher: SaleWatcher | None = None
        self.snap: Snapshot | None = None
        self.activity: list[tuple[str, str, int]] = []
        self.filter_value = "bidding"
        self.search = ""
        self.sort_col: str | None = None
        self.sort_asc = True

    @property
    def tab_name(self) -> str:
        title = self.title.strip() or f"Sale {self.sale_id}"
        if len(title) > 18:
            title = title[:16].rstrip() + "…"
        return f"{title}  {self.sale_id}"


class _RiskRow:
    """One unique at-risk bidder across watched sales."""

    def __init__(self, item: LiveItem, sale_title: str, sale_id: int = 0) -> None:
        self.item = item
        self.buyer_id = int(item.buyer_id or 0)
        self.name = item.high_bidder
        self.suspended = bool(item.suspended)
        self.unpaid = item.unpaid_invoice_count
        self.paid = item.paid_invoice_count
        self.voids = item.void_invoice_count
        self.items_bidding = item.items_bid_count
        self.items = [item]
        self.sales = [sale_title] if sale_title else []
        self.sale_ids: list[int] = [int(sale_id)] if sale_id else []
        self.item_sales: dict[int, int] = {item.item_id: int(sale_id)} if sale_id else {}

    def add(self, item: LiveItem, sale_title: str, sale_id: int = 0) -> None:
        self.items.append(item)
        if sale_title and sale_title not in self.sales:
            self.sales.append(sale_title)
        self.suspended = self.suspended or bool(item.suspended)
        if item.unpaid_invoice_count is not None:
            cur = int(self.unpaid or 0)
            self.unpaid = max(cur, int(item.unpaid_invoice_count))
        if item.paid_invoice_count is not None:
            cur = int(self.paid or 0)
            self.paid = max(cur, int(item.paid_invoice_count))
        if item.void_invoice_count is not None:
            cur = int(self.voids or 0)
            self.voids = max(cur, int(item.void_invoice_count))
        if item.items_bid_count is not None:
            cur = int(self.items_bidding or 0)
            self.items_bidding = max(cur, int(item.items_bid_count))
        self.item = item
        if sale_id:
            sid = int(sale_id)
            if sid not in self.sale_ids:
                self.sale_ids.append(sid)
            self.item_sales[item.item_id] = sid

    @property
    def why(self) -> str:
        unpaid_n = int(self.unpaid or 0)
        void_n = int(self.voids or 0)
        bits: list[str] = []
        if self.suspended:
            bits.append("Suspended")
        if unpaid_n > 0:
            bits.append(f"Unpaid ({unpaid_n})")
        if void_n > 0:
            bits.append(f"Void ({void_n})")
        return " · ".join(bits) if bits else "At risk"

    @property
    def sort_key(self) -> tuple:
        unpaid_n = int(self.unpaid or 0)
        void_n = int(self.voids or 0)
        return (
            0 if (self.suspended and unpaid_n > 0) else 1 if self.suspended else 2 if void_n > 0 else 3,
            -unpaid_n,
            -void_n,
            self.name.lower(),
        )

    def lots_on_sale(self, sale_id: int | None) -> int:
        if not sale_id:
            return len(self.items)
        sid = int(sale_id)
        return sum(1 for it in self.items if int(self.item_sales.get(it.item_id) or 0) == sid)

    def lots_other_sales(self, sale_id: int | None) -> int:
        return max(0, len(self.items) - self.lots_on_sale(sale_id))

    def open_on_sale(self, sale_id: int | None) -> int:
        if not sale_id:
            return sum(1 for it in self.items if it.is_open)
        sid = int(sale_id)
        return sum(
            1
            for it in self.items
            if it.is_open and int(self.item_sales.get(it.item_id) or 0) == sid
        )

    def displayed_lots(self, sale_id: int | None, *, limit: int = 8) -> list[LiveItem]:
        lots = [
            it for it in self.items
            if not sale_id or int(self.item_sales.get(it.item_id) or 0) == int(sale_id)
        ]
        return lots[:limit]

    def extra_lot_count(self, sale_id: int | None, *, limit: int = 8) -> int:
        lots = [
            it for it in self.items
            if not sale_id or int(self.item_sales.get(it.item_id) or 0) == int(sale_id)
        ]
        return max(0, len(lots) - limit)

    def skus_on_sale(self, sale_id: int | None, *, limit: int = 8) -> str:
        lots = self.displayed_lots(sale_id, limit=limit)
        skus = ", ".join((it.sku or str(it.item_id)) for it in lots)
        extra = self.extra_lot_count(sale_id, limit=limit)
        if extra:
            skus += f" +{extra}"
        return skus


class WatcherApp:
    def __init__(self, parent, *, manage_window: bool = True) -> None:
        self.host = parent
        self.root = parent.winfo_toplevel()
        self.manage_window = manage_window
        self.cfg = load_watcher_config()
        self.token: str | None = None
        self.seller_token: str | None = None
        self.estate_ez_token: str | None = load_estate_ez_token()
        self._browser_signin_busy: set[str] = set()
        self.watcher: SaleWatcher | None = None
        self._sessions: dict[int, _WatchSession] = {}
        self._active_sale_id: int | None = None
        self._seller_sales: list[SellerSale] = []
        self._seller_locations: list[tuple[int, str]] = []
        self._sale_by_label: dict[str, SellerSale] = {}
        self._sale_selecting = False
        self._tabs_visible = False
        self._activity_empty = None
        self._bids_fingerprint = ""
        self._bid_items: dict[str, LiveItem] = {}
        self._search_after_id: str | None = None
        self._latest_snap: Snapshot | None = None
        self._sort_col: str | None = None  # None = default (open + price)
        self._sort_asc = True
        self._col_headings = {
            "sku": "SKU",
            "item": "Item",
            "price": "Price",
            "buynow": "Buy now",
            "reserve": "Reserve",
            "met": "Met",
            "left": "Time left",
            "bids": "Bids",
            "bidder": "Highest bidder",
            "status": "Acct",
            "unpaid": "Unpaid invoices",
            "voids": "Voids",
            "paid": "Paid invoices",
            "category": "Category",
        }
        self._num_cols = ("price", "buynow", "reserve", "met", "left", "bids")
        self._col_defaults = {
            "sku": (100, 56, "w"),
            "item": (260, 80, "w"),
            "price": (76, 56, "e"),
            "buynow": (84, 64, "e"),
            "reserve": (76, 56, "e"),
            "met": (88, 64, "e"),
            "left": (80, 56, "e"),
            "bids": (48, 36, "e"),
            "bidder": (108, 72, "w"),
            "status": (80, 56, "w"),
            "unpaid": (108, 64, "w"),
            "voids": (72, 52, "w"),
            "paid": (96, 60, "w"),
            "category": (88, 56, "w"),
            "view": (48, 40, "center"),
        }
        self._tree_press_region = ""
        self._tree_press_x = 0
        self._ignore_header_sort = False
        self._pause_ticks = False
        self._tree_last_width = 0
        self._tree_cursor = ""
        self._unmet_overlays: list[tk.Label] = []
        self._unmet_paint_after: str | None = None
        self._activity_open = bool(self.cfg.get("activity_open", False))
        self._risk_tree: ttk.Treeview | None = None
        self._risk_rows: dict[str, _RiskRow] = {}
        self._risk_fingerprint = ""
        self._sale_risk_tree: ttk.Treeview | None = None
        self._sale_risk_rows: dict[str, _RiskRow] = {}
        self._sale_risk_fingerprint = ""
        self._bids_view = "lots"
        self._okayed_buyers: set[int] = set()
        for raw in self.cfg.get("okayed_buyers") or []:
            try:
                n = int(raw)
            except (TypeError, ValueError):
                continue
            if n > 0:
                self._okayed_buyers.add(n)
        self.risk_status_var = StringVar(
            value="Start watching to list suspended, unpaid, and voided invoices."
        )

        if manage_window:
            self.root.title(APP_TITLE)
            apply_sizable_main_window(self.root, self.cfg)
            try:
                self.root.protocol("WM_DELETE_WINDOW", self._on_main_close)
            except Exception:
                pass

        self.sale_var = StringVar(value=str(self.cfg.get("sale_url") or ""))
        self.poll_var = StringVar(value=str(self.cfg.get("poll_seconds") or 45))
        self.auto_save_var = BooleanVar(value=bool(self.cfg.get("auto_save_on_end", True)))
        self.filter_var = StringVar(value="bidding")
        self.search_var = StringVar()
        self.status_var = StringVar(value="Idle — sign in (buyer + seller). Live sales load from CT Seller.")
        self.login_var = StringVar(value="Buyer —  ·  Seller —")
        self.updated_var = StringVar(value="")
        self.theme_mode = "light" if str(self.cfg.get("theme", "dark")).lower() == "light" else "dark"
        self._sign_in_windows: dict[str, ctk.CTkToplevel] = {}

        _apply_palette(self.theme_mode)
        if manage_window:
            ctk.set_appearance_mode(self.theme_mode)
            self.root.configure(fg_color=THEMES[self.theme_mode]["window"])

        self._build_ui()
        self.search_var.trace_add("write", lambda *_: self._schedule_bid_render())
        self.root.after(200, self._try_load_token_quiet)
        self.root.after(400, self._try_load_seller_token_quiet)
        self.root.after(1000, self._tick_time_left)
        self.root.after(15000, self._keep_sessions_alive)

    def _build_ui(self) -> None:
        try:
            self.host.configure(fg_color=THEMES[self.theme_mode]["window"])
        except Exception:
            pass
        outer = ctk.CTkFrame(self.host, fg_color="transparent")
        outer.pack(fill="both", expand=True, padx=6, pady=4)

        # ----- Header (standalone only) -----
        header = ctk.CTkFrame(outer, fg_color="transparent")
        titles = ctk.CTkFrame(header, fg_color="transparent")
        self.title_label = ctk.CTkLabel(
            titles, text=APP_TITLE, font=("Segoe UI Semibold", 22), text_color=GREEN_DARK,
        )
        self.subtitle_label = ctk.CTkLabel(
            titles,
            text="Live hammer total, top bids, and bidder usernames — pick a sale from CT Seller, or paste a URL.",
            font=("Segoe UI", 13), text_color=MUTED,
        )
        header_right = ctk.CTkFrame(header, fg_color="transparent")
        if self.manage_window:
            header.pack(fill="x")
            titles.pack(side="left", fill="x", expand=True)
            self.title_label.pack(anchor="w")
            self.subtitle_label.pack(anchor="w")
            header_right.pack(side="right", anchor="n")

        # ----- Controls (one row so Top bids gets the height) -----
        self.controls = ctk.CTkFrame(outer, corner_radius=12, fg_color=WHITE)
        self.controls.pack(fill="x", pady=(2, 0))

        row1 = ctk.CTkFrame(self.controls, fg_color="transparent")
        row1.pack(fill="x", padx=8, pady=4)
        btns = ctk.CTkFrame(row1, fg_color="transparent")
        btns.pack(side="right")
        self.estate_ez_btn = ctk.CTkButton(
            btns, text="Estate-Ez sign in", width=128, height=28,
            fg_color="transparent", text_color=GREEN, border_color=GREEN, border_width=1,
            hover_color=GREEN_SOFT, command=self._on_estate_ez_login,
        )
        self.estate_ez_btn.pack(side="left")
        self.signin_btn = ctk.CTkButton(
            btns, text="Buyer sign in", width=108, height=28,
            fg_color="transparent", text_color=GREEN, border_color=GREEN, border_width=1,
            hover_color=GREEN_SOFT, command=self._on_login,
        )
        self.signin_btn.pack(side="left", padx=(6, 0))
        self.seller_btn = ctk.CTkButton(
            btns, text="Seller sign in", width=108, height=28,
            fg_color="transparent", text_color=GREEN, border_color=GREEN, border_width=1,
            hover_color=GREEN_SOFT, command=self._on_seller_login,
        )
        self.seller_btn.pack(side="left", padx=(6, 0))
        self.start_btn = ctk.CTkButton(
            btns, text="Start", width=72, height=28,
            fg_color=GREEN, hover_color=GREEN_HOVER, text_color=ON_ACCENT,
            font=("Segoe UI Semibold", 13),
            command=self._on_start,
        )
        self.start_btn.pack(side="left", padx=(6, 0))
        self.watch_all_btn = ctk.CTkButton(
            btns, text="Watch all", width=88, height=28,
            fg_color="transparent", text_color=GREEN, border_color=GREEN, border_width=1,
            hover_color=GREEN_SOFT, command=self._on_watch_all,
        )
        self.stop_btn = ctk.CTkButton(
            btns, text="Stop", width=64, height=28,
            fg_color="transparent", text_color=MUTED, border_color=MUTED, border_width=1,
            command=self._on_stop, state="disabled",
        )
        self.stop_btn.pack(side="left", padx=(6, 0))
        self.export_btn = ctk.CTkButton(
            btns, text="Export PDF", width=96, height=28,
            fg_color="transparent", text_color=GREEN, border_color=GREEN, border_width=1,
            hover_color=GREEN_SOFT, command=self._on_export_pdf,
        )
        self.export_btn.pack(side="right", padx=(6, 0))
        self.opensale_btn = ctk.CTkButton(
            btns, text="Open sale", width=88, height=28,
            fg_color="transparent", text_color=GREEN, border_color=GREEN, border_width=1,
            hover_color=GREEN_SOFT, command=self._open_sale,
        )
        self.opensale_btn.pack(side="right")
        badge_host = header_right if self.manage_window else btns
        self.theme_btn = ctk.CTkButton(
            badge_host,
            text="Dark" if self.theme_mode == "light" else "Light",
            width=70, height=28,
            fg_color="transparent", text_color=GREEN, border_color=GREEN, border_width=1,
            hover_color=GREEN_SOFT, command=self._toggle_theme,
        )
        self.login_badge = ctk.CTkLabel(
            badge_host, textvariable=self.login_var, font=("Segoe UI Semibold", 12),
            text_color=GREEN, fg_color=GREEN_SOFT, corner_radius=8, padx=10, pady=4,
        )
        if self.manage_window:
            self.theme_btn.pack(side="left", padx=(0, 8))
            self.login_badge.pack(side="left")
        else:
            self.login_badge.pack(side="right", padx=(8, 0))
            self.theme_btn.pack(side="right", padx=(6, 0))

        self.sale_label = ctk.CTkLabel(row1, text="Sale", width=40, anchor="w", font=("Segoe UI Semibold", 13), text_color=GREEN_DARK)
        self.sale_label.pack(side="left")
        self.sale_combo = ctk.CTkComboBox(
            row1,
            height=28,
            command=self._on_sale_combo,
            font=("Segoe UI", 13),
            dropdown_font=("Segoe UI", 12),
        )
        self.sale_combo.pack(side="left", fill="x", expand=True, padx=(6, 4))
        self.refresh_sales_btn = ctk.CTkButton(
            row1, text="Sales", width=56, height=28,
            fg_color="transparent", text_color=GREEN, border_color=GREEN, border_width=1,
            hover_color=GREEN_SOFT, command=self._on_refresh_sales,
        )
        self.refresh_sales_btn.pack(side="left", padx=(0, 6))
        self.every_label = ctk.CTkLabel(row1, text="every", text_color=MUTED)
        self.every_label.pack(side="left")
        ctk.CTkEntry(row1, textvariable=self.poll_var, width=44, height=28).pack(side="left", padx=(4, 2))
        self.sec_label = ctk.CTkLabel(row1, text="sec", text_color=MUTED)
        self.sec_label.pack(side="left")
        self.auto_save_chk = ctk.CTkCheckBox(
            row1,
            text="Save as lots close",
            variable=self.auto_save_var,
            onvalue=True,
            offvalue=False,
            fg_color=GREEN,
            hover_color=GREEN_HOVER,
            text_color=GREEN_DARK,
            font=("Segoe UI", 12),
        )
        self.auto_save_chk.pack(side="left", padx=(10, 8))

        self.sale_tabs_wrap = ctk.CTkFrame(outer, fg_color="transparent")
        self.sale_tabs_hint = ctk.CTkLabel(
            self.sale_tabs_wrap,
            text="Live sales",
            font=("Segoe UI Semibold", 12),
            text_color=MUTED,
        )
        self.sale_tabs = ctk.CTkSegmentedButton(
            self.sale_tabs_wrap,
            values=["Sale"],
            command=self._on_sale_tab,
            selected_color=GREEN,
            selected_hover_color=GREEN_HOVER,
            unselected_color=GREEN_SOFT,
            unselected_hover_color=ROW_ALT,
            text_color=GREEN_DARK if self.theme_mode == "dark" else ON_ACCENT,
        )
        self.sale_tabs.pack(side="left", fill="x", expand=True)

        # ----- Metrics -----
        metrics = ctk.CTkFrame(outer, fg_color="transparent")
        self.metrics_row = metrics
        metrics.pack(fill="x", pady=(2, 0))
        metrics.grid_columnconfigure((0, 1, 2, 3, 4, 5), weight=1, uniform="m")
        self.metric_hammer = MetricCard(metrics, "Hammer total")
        self.metric_bidding = MetricCard(metrics, "Bidding")
        self.metric_open = MetricCard(metrics, "Still open")
        self.metric_below = MetricCard(metrics, "Below reserve", command=self._on_below_metric)
        self.metric_bidders = MetricCard(metrics, "Bidders")
        self.metric_items = MetricCard(metrics, "Listed")
        metric_cards = (
            self.metric_hammer, self.metric_bidding, self.metric_open,
            self.metric_below, self.metric_bidders, self.metric_items,
        )
        last = len(metric_cards) - 1
        for i, card in enumerate(metric_cards):
            card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 6, 0 if i == last else 6))

        # ----- Body: resizable bids | activity -----
        body = ctk.CTkFrame(outer, fg_color="transparent")
        body.pack(fill="both", expand=True, pady=(2, 0))

        win_bg = THEMES[self.theme_mode]["window"]
        self.paned = tk.PanedWindow(
            body,
            orient=tk.HORIZONTAL,
            sashwidth=8,
            sashrelief=tk.FLAT,
            bg=win_bg,
            bd=0,
            opaqueresize=False,
        )
        self.paned.pack(fill="both", expand=True)

        # Native frames as paned children so the split actually fills the pane.
        # CTkFrame inside tk.PanedWindow keeps its content width and leaves a gap.
        self.bids_host = tk.Frame(self.paned, bg=win_bg, highlightthickness=0)
        self.act_host = tk.Frame(self.paned, bg=win_bg, highlightthickness=0)

        bids_panel = ctk.CTkFrame(self.bids_host, corner_radius=12, fg_color=WHITE)
        bids_panel.pack(fill="both", expand=True)
        self.bids_panel = bids_panel

        bids_head = ctk.CTkFrame(bids_panel, fg_color="transparent")
        bids_head.pack(fill="x", padx=8, pady=(4, 2))
        self.bids_title = ctk.CTkLabel(
            bids_head, text="Top bids", font=("Segoe UI Semibold", 15), text_color=GREEN_DARK,
        )
        self.bids_title.pack(side="left")
        self.bids_view_menu = ctk.CTkSegmentedButton(
            bids_head,
            values=["Lots", "Risky bidders"],
            command=self._on_bids_view,
            selected_color=GREEN,
            selected_hover_color=GREEN_HOVER,
            unselected_color=GREEN_SOFT,
            unselected_hover_color=ROW_ALT,
            text_color=GREEN_DARK if self.theme_mode == "dark" else ON_ACCENT,
            height=28,
        )
        self.bids_view_menu.set("Lots")
        self.bids_view_menu.pack(side="left", padx=(10, 0))
        self.filter_menu = ctk.CTkSegmentedButton(
            bids_head,
            values=["Bidding", "Under", "Open", "Sold", "All"],
            command=self._on_filter,
            selected_color=GREEN,
            selected_hover_color=GREEN_HOVER,
            unselected_color=GREEN_SOFT,
            unselected_hover_color=ROW_ALT,
            text_color=GREEN_DARK if self.theme_mode == "dark" else ON_ACCENT,
            height=28,
        )
        self.filter_menu.set("Bidding")
        self.filter_menu.pack(side="left", padx=(10, 0))
        self.acct_legend = ctk.CTkLabel(
            bids_head,
            text="Yellow reserve = not met  ·  amber row = bidding under  ·  click username for CT Seller",
            font=("Segoe UI", 11),
            text_color=MUTED,
            anchor="w",
        )
        self.act_toggle_btn = ctk.CTkButton(
            bids_head,
            text="Hide activity" if self._activity_open else "Activity",
            width=100, height=28,
            fg_color="transparent", text_color=GREEN, border_color=GREEN, border_width=1,
            hover_color=GREEN_SOFT, command=self._toggle_activity,
        )
        self.act_toggle_btn.pack(side="right")
        ctk.CTkEntry(
            bids_head, textvariable=self.search_var, width=160, height=28,
            placeholder_text="Search SKU, item, or bidder…",
        ).pack(side="right", padx=(0, 6))
        self.view_btn = ctk.CTkButton(
            bids_head, text="View item", width=84, height=28,
            fg_color="transparent", text_color=GREEN, border_color=GREEN, border_width=1,
            hover_color=GREEN_SOFT, command=self._open_selected_bid,
        )
        self.view_btn.pack(side="right", padx=(0, 6))
        self.retract_btn = ctk.CTkButton(
            bids_head, text="Retract bids", width=104, height=28,
            fg_color="transparent", text_color=GREEN, border_color=GREEN, border_width=1,
            hover_color=GREEN_SOFT, command=self._on_retract_selected,
        )
        self.retract_btn.pack(side="right", padx=(0, 6))
        self.okay_btn = ctk.CTkButton(
            bids_head, text="Okay", width=72, height=28,
            fg_color="transparent", text_color=GREEN, border_color=GREEN, border_width=1,
            hover_color=GREEN_SOFT, command=self._on_okay_selected,
        )
        self.okay_btn.pack(side="right", padx=(0, 6))
        self.suspend_btn = ctk.CTkButton(
            bids_head, text="Suspend", width=84, height=28,
            fg_color="transparent", text_color=GREEN, border_color=GREEN, border_width=1,
            hover_color=GREEN_SOFT, command=self._on_suspend_selected,
        )
        self.suspend_btn.pack(side="right", padx=(0, 6))
        ctk.CTkLabel(
            bids_head, textvariable=self.updated_var, font=("Segoe UI", 11), text_color=MUTED,
        ).pack(side="right", padx=(0, 8))

        # Native tree table — much smoother than rebuilding CTk rows every poll
        table_host = tk.Frame(bids_panel, bg=THEMES[self.theme_mode]["panel"], highlightthickness=0)
        table_host.pack(fill="both", expand=True, padx=8, pady=(0, 6))
        self.table_host = table_host
        self.table_wrap = table_host

        cols = (
            "sku", "item", "price", "buynow", "reserve", "met", "left", "bids",
            "bidder", "status", "unpaid", "voids", "paid", "category", "view", "pad",
        )
        self.bids_tree = ttk.Treeview(
            table_host,
            columns=cols,
            show="headings",
            style="Bids.Treeview",
            selectmode="browse",
        )
        for col, label in self._col_headings.items():
            self.bids_tree.heading(
                col,
                text=label,
                anchor="e" if col in self._num_cols else "w",
                command=lambda c=col: self._on_sort_column(c),
            )
        self.bids_tree.heading("view", text="", anchor="center")
        self.bids_tree.heading("pad", text="")
        self._apply_column_layout()

        xscroll = ttk.Scrollbar(table_host, orient="horizontal", command=self.bids_tree.xview)
        yscroll = ttk.Scrollbar(table_host, orient="vertical", command=self.bids_tree.yview)
        self._tree_xscroll = xscroll
        self._tree_yscroll = yscroll
        self.bids_tree.configure(yscrollcommand=self._on_tree_yview, xscrollcommand=self._on_tree_xview)
        xscroll.pack(side="bottom", fill="x")
        yscroll.pack(side="right", fill="y")
        self.bids_tree.pack(side="left", fill="both", expand=True)
        self.bids_tree.bind("<Double-1>", self._on_bid_double_click)
        self.bids_tree.bind("<Return>", self._on_bid_double_click)
        self.bids_tree.bind("<ButtonPress-1>", self._on_tree_press, add="+")
        self.bids_tree.bind("<ButtonRelease-1>", self._on_bid_click)
        self.bids_tree.bind("<Button-3>", self._on_bid_right_click)
        self.bids_tree.bind("<Control-Button-1>", self._on_bid_right_click)
        self._acct_tip = TreeTooltip(self.bids_tree)
        self.bids_tree.bind("<Motion>", self._on_tree_motion)
        self.bids_tree.bind("<Leave>", self._on_tree_leave)
        self.bids_tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.bids_tree.bind("<Configure>", self._on_tree_configure)

        sale_risk_cols = (
            "bidder", "why", "this_sale", "open", "other_sales", "ctbids",
            "unpaid", "voids", "paid", "skus",
        )
        self._sale_risk_tree = ttk.Treeview(
            table_host,
            columns=sale_risk_cols,
            show="headings",
            style="Bids.Treeview",
            selectmode="browse",
        )
        sale_risk_heads = {
            "bidder": ("Bidder", "w", 140),
            "why": ("Why", "w", 170),
            "this_sale": ("This sale", "e", 80),
            "open": ("Still open", "e", 80),
            "other_sales": ("Other sales", "e", 88),
            "ctbids": ("CTBids bidding", "e", 110),
            "unpaid": ("Unpaid", "w", 72),
            "voids": ("Voids", "w", 60),
            "paid": ("Paid", "w", 64),
            "skus": ("SKUs this sale", "w", 280),
        }
        for col, (label, anchor, width) in sale_risk_heads.items():
            self._sale_risk_tree.heading(col, text=label, anchor=anchor)
            self._sale_risk_tree.column(
                col, width=width, minwidth=48, anchor=anchor, stretch=(col in ("skus", "why")),
            )
        self._sale_risk_tree.bind("<Double-1>", self._on_sale_risk_double)
        self._sale_risk_tree.bind("<Return>", self._on_sale_risk_double)
        self._sale_risk_tree.bind("<ButtonRelease-1>", self._on_risk_tree_click)
        self._sale_risk_tree.bind("<Motion>", self._on_risk_tree_motion)
        self._sale_risk_tree.bind("<Leave>", self._on_risk_tree_leave)
        self._sale_risk_tree.bind("<Button-3>", self._on_sale_risk_right_click)
        self._sale_risk_tree.bind("<Control-Button-1>", self._on_sale_risk_right_click)
        self._sale_risk_tip = TreeTooltip(self._sale_risk_tree)

        self._bids_placeholder = None

        # Activity panel
        self.act_panel = ctk.CTkFrame(self.act_host, corner_radius=12, fg_color=WHITE)
        self.act_panel.pack(fill="both", expand=True)
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

        self.paned.add(self.bids_host, minsize=360, stretch="always")
        if self._activity_open:
            self.paned.add(self.act_host, minsize=180, stretch="never")
        self.paned.bind("<ButtonPress-1>", self._on_sash_press)
        self.paned.bind("<ButtonRelease-1>", self._save_sash)
        self._sash_tries = 0
        self.root.after(120, self._restore_sash)

        # Status bar
        status = ctk.CTkFrame(outer, fg_color="transparent")
        status.pack(fill="x", pady=(4, 0))
        self.copyright_label = ctk.CTkLabel(
            status, text=COPYRIGHT, font=("Segoe UI", 11), text_color=MUTED, anchor="e",
        )
        self.copyright_label.pack(side="right")
        self.status_label = ctk.CTkLabel(
            status, textvariable=self.status_var, font=("Segoe UI", 12), text_color=MUTED, anchor="w",
        )
        self.status_label.pack(side="left", fill="x", expand=True)

        self._show_bids_placeholder("Start watching to load live bids.")
        self._style_tree()
        self._seed_session_from_saved_url()

    def _on_main_close(self) -> None:
        save_main_window_layout(self.root, self.cfg)
        try:
            self.root.destroy()
        except Exception:
            pass

    def mount_risk_panel(self, host) -> None:
        """Dedicated At risk tab: unique suspended / unpaid bidders across watched sales."""
        try:
            host.configure(fg_color=THEMES[self.theme_mode]["window"])
        except Exception:
            pass
        outer = ctk.CTkFrame(host, fg_color="transparent")
        outer.pack(fill="both", expand=True, padx=8, pady=8)
        head = ctk.CTkFrame(outer, fg_color="transparent")
        head.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(
            head,
            text="At-risk bidders",
            font=("Segoe UI Semibold", 18),
            text_color=GREEN_DARK,
        ).pack(side="left")
        ctk.CTkLabel(
            head,
            text="Suspended, unpaid, or voided invoices on lots they are high bidder on. This sale vs other watched sales vs CTBids current bids.",
            font=("Segoe UI", 12),
            text_color=MUTED,
        ).pack(side="left", padx=(12, 0), pady=(4, 0))
        self.risk_open_btn = ctk.CTkButton(
            head, text="Open in Seller", width=120, height=28,
            fg_color="transparent", text_color=GREEN, border_color=GREEN, border_width=1,
            hover_color=GREEN_SOFT, command=self._on_risk_open_seller,
        )
        self.risk_open_btn.pack(side="right")
        self.risk_retract_btn = ctk.CTkButton(
            head, text="Retract bids", width=104, height=28,
            fg_color="transparent", text_color=GREEN, border_color=GREEN, border_width=1,
            hover_color=GREEN_SOFT, command=self._on_risk_retract,
        )
        self.risk_retract_btn.pack(side="right", padx=(0, 6))
        self.risk_okay_btn = ctk.CTkButton(
            head, text="Okay", width=72, height=28,
            fg_color="transparent", text_color=GREEN, border_color=GREEN, border_width=1,
            hover_color=GREEN_SOFT, command=self._on_risk_okay,
        )
        self.risk_okay_btn.pack(side="right", padx=(0, 6))
        self.risk_suspend_btn = ctk.CTkButton(
            head, text="Suspend", width=84, height=28,
            fg_color="transparent", text_color=GREEN, border_color=GREEN, border_width=1,
            hover_color=GREEN_SOFT, command=self._on_risk_suspend,
        )
        self.risk_suspend_btn.pack(side="right", padx=(0, 6))

        panel = ctk.CTkFrame(outer, corner_radius=12, fg_color=WHITE)
        panel.pack(fill="both", expand=True)
        table_host = tk.Frame(panel, bg=THEMES[self.theme_mode]["panel"], highlightthickness=0)
        table_host.pack(fill="both", expand=True, padx=8, pady=8)
        cols = ("bidder", "why", "unpaid", "voids", "paid", "lots", "ctbids", "skus", "sale")
        self._risk_tree = ttk.Treeview(
            table_host, columns=cols, show="headings", style="Bids.Treeview", selectmode="browse",
        )
        headings = {
            "bidder": ("Bidder", "w"),
            "why": ("Why", "w"),
            "unpaid": ("Unpaid invoices", "w"),
            "voids": ("Voids", "w"),
            "paid": ("Paid invoices", "w"),
            "lots": ("Watched lots", "e"),
            "ctbids": ("CTBids bidding", "e"),
            "skus": ("SKUs", "w"),
            "sale": ("Sale", "w"),
        }
        widths = {
            "bidder": 140, "why": 170, "unpaid": 120, "voids": 72, "paid": 110,
            "lots": 90, "ctbids": 110, "skus": 240, "sale": 180,
        }
        for col, (label, anchor) in headings.items():
            self._risk_tree.heading(col, text=label, anchor=anchor)
            self._risk_tree.column(col, width=widths[col], minwidth=48, anchor=anchor, stretch=(col in ("skus", "sale", "why")))
        yscroll = ttk.Scrollbar(table_host, orient="vertical", command=self._risk_tree.yview)
        self._risk_tree.configure(yscrollcommand=yscroll.set)
        yscroll.pack(side="right", fill="y")
        self._risk_tree.pack(side="left", fill="both", expand=True)
        self._risk_tree.bind("<Double-1>", self._on_risk_double)
        self._risk_tree.bind("<ButtonRelease-1>", self._on_risk_tree_click)
        self._risk_tree.bind("<Motion>", self._on_risk_tree_motion)
        self._risk_tree.bind("<Leave>", self._on_risk_tree_leave)
        self._risk_tree.bind("<Button-3>", self._on_risk_right_click)
        self._risk_tree.bind("<Control-Button-1>", self._on_risk_right_click)
        self._risk_tab_tip = TreeTooltip(self._risk_tree)
        ctk.CTkLabel(
            outer, textvariable=self.risk_status_var, font=("Segoe UI", 12), text_color=MUTED, anchor="w",
        ).pack(fill="x", pady=(6, 0))
        self._style_tree()
        self._render_risk()

    def _collect_at_risk(self, *, sale_id: int | None = None) -> list[_RiskRow]:
        grouped: dict[str, _RiskRow] = {}
        sources: list[tuple[str, int, Snapshot]] = []
        for sess in self._sessions.values():
            if sess.snap:
                sources.append((sess.title or f"Sale {sess.sale_id}", int(sess.sale_id), sess.snap))
        if not sources and self._latest_snap is not None:
            sources.append((f"Sale {self._latest_snap.sale_id}", int(self._latest_snap.sale_id), self._latest_snap))
        for sale_title, sid, snap in sources:
            for item in snap.items.values():
                if not _item_is_at_risk(item, self._okayed_buyers):
                    continue
                key = str(item.buyer_id) if item.buyer_id else f"n:{(item.high_bidder or '').lower()}"
                row = grouped.get(key)
                if row is None:
                    grouped[key] = _RiskRow(item, sale_title, sid)
                else:
                    row.add(item, sale_title, sid)
        rows = list(grouped.values())
        if sale_id:
            want = int(sale_id)
            rows = [r for r in rows if r.lots_on_sale(want) > 0]
        query = self.search_var.get().strip().lower() if sale_id else ""
        if query:
            rows = [
                r for r in rows
                if query in (r.name or "").lower()
                or query in r.why.lower()
                or any(query in f"{it.sku} {it.title}".lower() for it in r.items)
            ]
        if sale_id:
            rows.sort(key=lambda r: (-r.lots_on_sale(sale_id), -int(r.unpaid or 0), r.name.lower()))
        else:
            rows.sort(key=lambda r: r.sort_key)
        return rows

    def _render_risk(self) -> None:
        tree = self._risk_tree
        if tree is None:
            return
        rows = self._collect_at_risk()
        fingerprint = "|".join(
            f"{r.buyer_id}:{r.name}:{r.why}:{r.unpaid}:{r.voids}:{len(r.items)}:{r.items_bidding}:{','.join(r.sales)}"
            for r in rows
        )
        if fingerprint == self._risk_fingerprint:
            return
        self._risk_fingerprint = fingerprint
        tree.delete(*tree.get_children())
        self._risk_rows.clear()
        if not rows:
            watching = any(s.snap for s in self._sessions.values()) or self._latest_snap is not None
            if not watching:
                self.risk_status_var.set("Start watching a sale — suspended, unpaid, and voided invoices will list here.")
            elif not self.seller_token:
                self.risk_status_var.set("No suspended bidders yet. Seller sign-in is needed to see unpaid and void invoices.")
            else:
                self.risk_status_var.set("No suspended, unpaid, or voided bidders on watched sales right now.")
            return
        for i, row in enumerate(rows):
            iid = str(row.buyer_id or row.name)
            skus = ", ".join((it.sku or str(it.item_id)) for it in row.items[:8])
            if len(row.items) > 8:
                skus += f" +{len(row.items) - 8}"
            unpaid = "—" if row.unpaid is None else str(int(row.unpaid))
            voids = "—" if row.voids is None else str(int(row.voids))
            paid = "—" if row.paid is None else str(int(row.paid))
            ctbids = "—" if row.items_bidding is None else str(int(row.items_bidding))
            tags = ["odd" if i % 2 else "even"]
            if row.suspended:
                tags.append("suspended")
            elif int(row.unpaid or 0) > 0 or int(row.voids or 0) > 0:
                tags.append("under")
            tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    row.name,
                    row.why,
                    unpaid,
                    voids,
                    paid,
                    str(len(row.items)),
                    ctbids,
                    skus,
                    " · ".join(row.sales),
                ),
                tags=tuple(tags),
            )
            self._risk_rows[iid] = row
        self.risk_status_var.set(
            f"{len(rows)} at-risk bidder(s) · click a row, then Suspend / Retract / Open in Seller"
        )

    def _selected_risk_row(self) -> _RiskRow | None:
        tree = self._risk_tree
        if tree is None:
            return None
        sel = tree.selection()
        if not sel:
            return None
        return self._risk_rows.get(sel[0])

    def _popup_risk_menu(self, event, row: _RiskRow | None, *, sale_id: int | None = None) -> None:
        menu = tk.Menu(self.root, tearoff=0)
        if not row:
            menu.add_command(label="No bidder selected", state="disabled")
        else:
            menu.add_command(label=f"Open {row.name} in CT Seller…", command=lambda: self._open_buyer(row.item))
            lots = row.displayed_lots(sale_id, limit=8)
            if lots:
                sku = lots[0].sku or "lot"
                first = lots[0]
                sid = int(row.item_sales.get(first.item_id) or 0) or None
                menu.add_command(
                    label=f"Open {sku} on CTBids",
                    command=lambda it=first, s=sid: self._open_item(it, sale_id=s),
                )
            menu.add_separator()
            self._add_okay_menu(menu, row.item, row.name)
            if self.seller_token:
                menu.add_separator()
                self._add_location_suspend_menu(menu, row.item, row.name)
                owned_lots = [
                    it for it in row.items if self._owns_sale(row.item_sales.get(it.item_id, 0))
                ]
                if owned_lots:
                    menu.add_command(
                        label=f"Retract {row.name}'s bids on their lots…",
                        command=lambda: self._open_retract_dialog(
                            row.item,
                            all_lots=True,
                            lots=owned_lots,
                            sale_id=(row.sale_ids[0] if row.sale_ids else None),
                        ),
                    )
                else:
                    menu.add_command(
                        label="Retract only on this Seller login's sales",
                        state="disabled",
                    )
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _on_risk_open_seller(self) -> None:
        row = self._selected_risk_row()
        if not row:
            messagebox.showinfo(APP_TITLE, "Click a bidder on At risk first.")
            return
        self._open_buyer(row.item)

    def _on_risk_suspend(self) -> None:
        row = self._selected_risk_row()
        if not row:
            messagebox.showinfo(APP_TITLE, "Click a bidder on At risk first.")
            return
        self._open_suspend_dialog(row.item, unsuspend=False)

    def _on_risk_retract(self) -> None:
        row = self._selected_risk_row()
        if not row:
            messagebox.showinfo(APP_TITLE, "Click a bidder on At risk first.")
            return
        self._open_retract_dialog(
            row.item,
            all_lots=True,
            lots=[it for it in row.items if self._owns_sale(row.item_sales.get(it.item_id, 0))],
            sale_id=(row.sale_ids[0] if row.sale_ids else None),
        )

    def _on_risk_double(self, event=None) -> None:
        tree = getattr(self, "_risk_tree", None)
        if tree is not None and event is not None:
            name = self._tree_column_name(tree, tree.identify_column(event.x))
            if name in ("bidder", "skus"):
                return
        row = self._selected_risk_row()
        if row:
            self._open_buyer(row.item)

    def _on_risk_right_click(self, event) -> None:
        tree = self._risk_tree
        if tree is None:
            return
        row_id = tree.identify_row(event.y)
        if row_id:
            tree.selection_set(row_id)
            tree.focus(row_id)
        self._popup_risk_menu(event, self._selected_risk_row())

    def _on_bids_view(self, value: str) -> None:
        want = "risk" if str(value).lower().startswith("risky") else "lots"
        if want == getattr(self, "_bids_view", "lots"):
            return
        self._bids_view = want
        self._apply_bids_view()

    def _apply_bids_view(self) -> None:
        risk = getattr(self, "_bids_view", "lots") == "risk"
        tree = getattr(self, "_sale_risk_tree", None)
        self._hide_unmet_overlays()
        try:
            self.bids_title.configure(text="Risky bidders" if risk else "Top bids")
        except Exception:
            pass
        if risk:
            try:
                self.filter_menu.pack_forget()
            except Exception:
                pass
            self.bids_tree.pack_forget()
            if tree is not None:
                tree.pack(side="left", fill="both", expand=True)
                self._wire_tree_scroll(tree)
            try:
                self.view_btn.configure(text="Open in Seller")
            except Exception:
                pass
            self._sale_risk_fingerprint = ""
            self._render_sale_risk()
        else:
            if tree is not None:
                tree.pack_forget()
            self.bids_tree.pack(side="left", fill="both", expand=True)
            self._wire_tree_scroll(self.bids_tree)
            try:
                if not self.filter_menu.winfo_ismapped():
                    self.filter_menu.pack(side="left", padx=(8, 0), after=self.bids_view_menu)
            except Exception:
                try:
                    self.filter_menu.pack(side="left", padx=(8, 0))
                except Exception:
                    pass
            try:
                self.view_btn.configure(text="View item")
            except Exception:
                pass
            self._bids_fingerprint = ""
            self._render_bids()

    def _wire_tree_scroll(self, tree) -> None:
        try:
            self._tree_xscroll.configure(command=tree.xview)
            self._tree_yscroll.configure(command=tree.yview)
            tree.configure(yscrollcommand=self._on_tree_yview, xscrollcommand=self._on_tree_xview)
        except Exception:
            pass

    def _render_sale_risk(self) -> None:
        tree = getattr(self, "_sale_risk_tree", None)
        if tree is None or getattr(self, "_bids_view", "lots") != "risk":
            return
        sale_id = int(self._active_sale_id or 0)
        if not sale_id:
            tree.delete(*tree.get_children())
            self._sale_risk_rows.clear()
            self._sale_risk_fingerprint = "nosale"
            self.updated_var.set("Pick a sale, then start watching.")
            return
        rows = self._collect_at_risk(sale_id=sale_id)
        fingerprint = "|".join(
            f"{r.buyer_id}:{r.name}:{r.why}:{r.lots_on_sale(sale_id)}:{r.lots_other_sales(sale_id)}:"
            f"{r.items_bidding}:{r.unpaid}:{r.voids}"
            for r in rows
        )
        fingerprint = f"{sale_id}::{self.search_var.get().strip().lower()}::{fingerprint}"
        if fingerprint == self._sale_risk_fingerprint:
            return
        self._sale_risk_fingerprint = fingerprint
        tree.delete(*tree.get_children())
        self._sale_risk_rows.clear()
        if not rows:
            watching = any(s.snap for s in self._sessions.values()) or self._latest_snap is not None
            if not watching:
                self.updated_var.set("Start watching — risky bidders for this sale will list here.")
            elif not self.seller_token:
                self.updated_var.set("No suspended bidders yet. Seller sign-in is needed for unpaid/voids.")
            else:
                self.updated_var.set("No suspended, unpaid, or voided bidders on this sale right now.")
            return
        for i, row in enumerate(rows):
            iid = f"s{sale_id}:{row.buyer_id or row.name}"
            unpaid = "—" if row.unpaid is None else str(int(row.unpaid))
            voids = "—" if row.voids is None else str(int(row.voids))
            paid = "—" if row.paid is None else str(int(row.paid))
            ctbids = "—" if row.items_bidding is None else str(int(row.items_bidding))
            tags = ["odd" if i % 2 else "even"]
            if row.suspended:
                tags.append("suspended")
            elif int(row.unpaid or 0) > 0 or int(row.voids or 0) > 0:
                tags.append("under")
            tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    row.name,
                    row.why,
                    str(row.lots_on_sale(sale_id)),
                    str(row.open_on_sale(sale_id)),
                    str(row.lots_other_sales(sale_id)),
                    ctbids,
                    unpaid,
                    voids,
                    paid,
                    row.skus_on_sale(sale_id),
                ),
                tags=tuple(tags),
            )
            self._sale_risk_rows[iid] = row
        self.updated_var.set(
            f"{len(rows)} at-risk bidder(s) this sale · This sale = lots they are high on here · "
            f"Other sales = other watched sales · CTBids bidding = current bids network-wide"
        )

    def _selected_sale_risk_row(self) -> _RiskRow | None:
        tree = getattr(self, "_sale_risk_tree", None)
        if tree is None:
            return None
        sel = tree.selection()
        if not sel:
            return None
        return self._sale_risk_rows.get(sel[0])

    def _tree_column_name(self, tree, col_id: str) -> str:
        try:
            cols = tuple(tree["columns"])
            return str(cols[int(str(col_id).lstrip("#")) - 1])
        except (TypeError, ValueError, IndexError):
            return ""

    def _risk_row_from_tree(self, tree, row_id: str) -> _RiskRow | None:
        if tree is getattr(self, "_sale_risk_tree", None):
            return self._sale_risk_rows.get(row_id)
        return self._risk_rows.get(row_id)

    def _risk_sale_id_for_tree(self, tree) -> int | None:
        if tree is getattr(self, "_sale_risk_tree", None):
            return int(self._active_sale_id or 0) or None
        return None

    def _risk_tip_for_tree(self, tree) -> TreeTooltip | None:
        if tree is getattr(self, "_sale_risk_tree", None):
            return getattr(self, "_sale_risk_tip", None)
        return getattr(self, "_risk_tab_tip", None)

    def _sku_font(self) -> tkfont.Font:
        fnt = getattr(self, "_sku_font_obj", None)
        if fnt is None:
            fnt = tkfont.Font(family="Segoe UI", size=12)
            self._sku_font_obj = fnt
        return fnt

    def _nav_once(self, key: str) -> bool:
        now = time.monotonic()
        prev = getattr(self, "_nav_once_map", {})
        if now - float(prev.get(key) or 0) < 0.4:
            return False
        prev[key] = now
        self._nav_once_map = prev
        return True

    @staticmethod
    def _sku_tip_text(item: LiveItem) -> str:
        sku = (item.sku or str(item.item_id)).strip()
        title = (item.title or "").strip()
        if title and title != sku:
            return f"{sku}\n{title}"
        return sku or title

    def _lot_at_sku_x(self, tree, event, row: _RiskRow) -> LiveItem | None:
        sale_id = self._risk_sale_id_for_tree(tree)
        lots = row.displayed_lots(sale_id, limit=8)
        if not lots:
            return None
        row_id = tree.identify_row(event.y)
        bbox = tree.bbox(row_id, "skus") if row_id else None
        if not bbox:
            return lots[0]
        fnt = self._sku_font()
        x = event.x - int(bbox[0]) - 6
        cursor = 0
        sep_w = fnt.measure(", ")
        for i, it in enumerate(lots):
            label = it.sku or str(it.item_id)
            width = fnt.measure(label)
            extra = sep_w / 2 if i < len(lots) - 1 else 24
            if x < cursor + width + extra:
                return it
            cursor += width + sep_w
        return lots[-1]

    def _on_risk_tree_click(self, event) -> None:
        tree = event.widget
        if tree.identify_region(event.x, event.y) != "cell":
            return
        row_id = tree.identify_row(event.y)
        if not row_id:
            return
        tree.selection_set(row_id)
        tree.focus(row_id)
        row = self._risk_row_from_tree(tree, row_id)
        if not row:
            return
        name = self._tree_column_name(tree, tree.identify_column(event.x))
        if name == "bidder":
            if self._nav_once(f"seller:{row.buyer_id or row.name}"):
                self._open_buyer(row.item)
            return
        if name != "skus":
            return
        item = self._lot_at_sku_x(tree, event, row)
        if not item:
            return
        sid = int(row.item_sales.get(item.item_id) or 0) or None
        if self._nav_once(f"item:{item.item_id}"):
            self._open_item(item, sale_id=sid)

    def _on_risk_tree_motion(self, event) -> None:
        tree = event.widget
        tip = self._risk_tip_for_tree(tree)
        region = tree.identify_region(event.x, event.y)
        name = ""
        if region == "cell":
            name = self._tree_column_name(tree, tree.identify_column(event.x))
        want = "hand2" if name in ("bidder", "skus") else ""
        if getattr(tree, "_hover_cursor", "") != want:
            tree._hover_cursor = want
            try:
                tree.configure(cursor=want)
            except Exception:
                pass
        if tip is None:
            return
        if name == "skus":
            row_id = tree.identify_row(event.y)
            row = self._risk_row_from_tree(tree, row_id) if row_id else None
            if row:
                item = self._lot_at_sku_x(tree, event, row)
                if item:
                    tip.schedule(self._sku_tip_text(item), event.x_root, event.y_root)
                    return
        elif name == "bidder":
            row_id = tree.identify_row(event.y)
            row = self._risk_row_from_tree(tree, row_id) if row_id else None
            if row and row.name:
                tip.schedule(f"Click to open {row.name} in CT Seller", event.x_root, event.y_root)
                return
        tip.hide()

    def _on_risk_tree_leave(self, event) -> None:
        tree = event.widget
        try:
            tree.configure(cursor="")
            tree._hover_cursor = ""
        except Exception:
            pass
        tip = self._risk_tip_for_tree(tree)
        if tip is not None:
            tip.hide()

    def _on_sale_risk_double(self, event=None) -> None:
        tree = getattr(self, "_sale_risk_tree", None)
        if tree is not None and event is not None:
            name = self._tree_column_name(tree, tree.identify_column(event.x))
            if name in ("bidder", "skus"):
                return
        row = self._selected_sale_risk_row()
        if row:
            self._open_buyer(row.item)

    def _on_sale_risk_right_click(self, event) -> None:
        tree = getattr(self, "_sale_risk_tree", None)
        if tree is None:
            return
        row_id = tree.identify_row(event.y)
        if row_id:
            tree.selection_set(row_id)
            tree.focus(row_id)
        self._popup_risk_menu(
            event, self._selected_sale_risk_row(), sale_id=int(self._active_sale_id or 0) or None
        )

    def _seed_session_from_saved_url(self) -> None:
        try:
            sale_id = parse_sale_id(self.sale_var.get())
        except Exception:
            return
        url = self.sale_var.get().strip() or buyer_sale_url(sale_id)
        self._ensure_session(sale_id, url, f"Sale {sale_id}")
        self._active_sale_id = sale_id
        self.watcher = None
        try:
            self.sale_combo.set(url)
        except Exception:
            pass

    def _current_session(self) -> _WatchSession | None:
        if self._active_sale_id is None:
            return None
        return self._sessions.get(self._active_sale_id)

    def _live_seller_sales(self) -> list[SellerSale]:
        return [s for s in self._seller_sales if s.is_live]

    def _ensure_session(self, sale_id: int, sale_url: str = "", title: str = "") -> _WatchSession:
        sess = self._sessions.get(int(sale_id))
        if sess is None:
            url = sale_url.strip() or buyer_sale_url(sale_id)
            sess = _WatchSession(int(sale_id), url, title or f"Sale {sale_id}")
            self._sessions[int(sale_id)] = sess
            return sess
        if sale_url.strip():
            sess.sale_url = sale_url.strip()
        if title.strip():
            sess.title = title.strip()
        return sess

    def _seller_sale_for(self, sale_id: int) -> SellerSale | None:
        for sale in self._seller_sales:
            if sale.sale_id == sale_id:
                return sale
        return None

    def _owns_sale(self, sale_id: int | None) -> bool:
        """True only if this CT Seller login has that sale on its dashboard."""
        if not self.seller_token or not sale_id:
            return False
        try:
            sid = int(sale_id)
        except (TypeError, ValueError):
            return False
        return self._seller_sale_for(sid) is not None

    def _seller_location_label(self) -> str:
        if self._seller_locations:
            return ", ".join(title for _i, title in self._seller_locations)
        return "this Seller login's location(s)"

    def _match_seller_sale(self, text: str) -> SellerSale | None:
        choice = (text or "").strip()
        if not choice:
            return None
        hit = self._sale_by_label.get(choice)
        if hit:
            return hit
        try:
            sale_id = parse_sale_id(choice)
        except Exception:
            return None
        return self._seller_sale_for(sale_id)

    def _stash_current_session(self) -> None:
        sess = self._current_session()
        if sess is None:
            return
        sess.filter_value = self.filter_var.get() or "bidding"
        sess.search = self.search_var.get()
        sess.sort_col = self._sort_col
        sess.sort_asc = self._sort_asc
        if self._latest_snap and self._latest_snap.sale_id == sess.sale_id:
            sess.snap = self._latest_snap

    def _select_session(self, sale_id: int, *, title: str = "", url: str = "") -> _WatchSession:
        sale = self._seller_sale_for(sale_id)
        self._stash_current_session()
        sess = self._ensure_session(
            sale_id,
            url or (sale.buyer_url if sale else buyer_sale_url(sale_id)),
            title or (sale.title if sale else f"Sale {sale_id}"),
        )
        self._active_sale_id = sale_id
        self.watcher = sess.watcher
        self._sale_selecting = True
        try:
            label = sale.combo_label if sale else sess.sale_url
            self.sale_var.set(sess.sale_url)
            try:
                self.sale_combo.set(label)
            except Exception:
                self.sale_combo.set(sess.sale_url)
        finally:
            self._sale_selecting = False
        self._restore_session_ui(sess)
        self._sync_start_stop()
        self._sync_sale_tabs(select=sess.tab_name)
        return sess

    def _restore_session_ui(self, sess: _WatchSession) -> None:
        self.filter_var.set(sess.filter_value or "bidding")
        filter_label = {
            "bidding": "Bidding",
            "under": "Under",
            "open": "Open",
            "sold": "Sold",
            "all": "All",
        }.get((sess.filter_value or "").lower(), "Bidding")
        try:
            self.filter_menu.set(filter_label)
        except Exception:
            pass
        self._sort_col = sess.sort_col
        self._sort_asc = sess.sort_asc
        self.search_var.set(sess.search)
        self._latest_snap = sess.snap
        self._bids_fingerprint = ""
        if sess.snap and sess.snap.items:
            self._set_metrics(sess.snap)
            self._render_bids()
        else:
            for card in (
                self.metric_hammer, self.metric_bidding, self.metric_open,
                self.metric_below, self.metric_bidders, self.metric_items,
            ):
                card.set("—")
            self._show_bids_placeholder(
                f"Start watching {sess.title} to load live bids."
                if sess.title else "Start watching to load live bids."
            )
        self._rebuild_activity(sess)

    def _rebuild_activity(self, sess: _WatchSession | None) -> None:
        self._clear_rows(self.activity_list)
        self._style_activity_list()
        self._activity_empty = None
        rows = list(sess.activity) if sess else []
        if not rows:
            self._activity_empty = ctk.CTkLabel(
                self.activity_list, text="Bid changes will show up here.", text_color=MUTED,
            )
            self._activity_empty.pack(anchor="w", padx=6, pady=10)
            return
        for entry in rows[-50:]:
            message = entry[0]
            kind = entry[1] if len(entry) > 1 else "info"
            buyer_id = int(entry[2] or 0) if len(entry) > 2 else 0
            self._draw_activity_row(message, kind, buyer_id=buyer_id)

    def _tab_sessions(self) -> list[_WatchSession]:
        ordered: list[_WatchSession] = []
        seen: set[int] = set()
        for sale in self._live_seller_sales():
            sess = self._ensure_session(sale.sale_id, sale.buyer_url, sale.title)
            ordered.append(sess)
            seen.add(sale.sale_id)
        for sale_id, sess in self._sessions.items():
            if sale_id in seen:
                continue
            if sess.watcher or sess.snap:
                ordered.append(sess)
        return ordered

    def _sync_sale_tabs(self, select: str | None = None) -> None:
        sessions = self._tab_sessions()
        names = [sess.tab_name for sess in sessions]
        live_n = len(self._live_seller_sales())
        if live_n > 1 and not self.watch_all_btn.winfo_ismapped():
            self.watch_all_btn.pack(side="left", padx=(8, 0), after=self.start_btn)
        elif live_n <= 1 and self.watch_all_btn.winfo_ismapped():
            self.watch_all_btn.pack_forget()
        if not names:
            if self._tabs_visible:
                self.sale_tabs_wrap.pack_forget()
                self._tabs_visible = False
            return
        if not self._tabs_visible:
            self.sale_tabs_wrap.pack(fill="x", pady=(4, 0), before=self.metrics_row)
            self._tabs_visible = True
        pick = select
        if pick not in names:
            current = self._current_session()
            pick = current.tab_name if current and current.tab_name in names else names[0]
        self._sale_selecting = True
        try:
            self.sale_tabs.configure(values=names)
            self.sale_tabs.set(pick)
        except Exception:
            pass
        finally:
            self._sale_selecting = False

    def _on_sale_tab(self, name: str) -> None:
        if self._sale_selecting:
            return
        for sess in self._sessions.values():
            if sess.tab_name == name:
                if sess.sale_id != self._active_sale_id:
                    self._select_session(sess.sale_id, title=sess.title, url=sess.sale_url)
                return

    def _on_sale_combo(self, choice: str) -> None:
        if self._sale_selecting:
            return
        sale = self._match_seller_sale(choice)
        if sale:
            self._select_session(sale.sale_id, title=sale.title, url=sale.buyer_url)
            return
        try:
            sale_id = parse_sale_id(choice)
        except Exception:
            return
        self._select_session(sale_id, url=buyer_sale_url(sale_id))

    def _on_refresh_sales(self) -> None:
        if not self.seller_token:
            messagebox.showinfo(
                APP_TITLE,
                "Sign in to CT Seller first.\n\nLive Watch loads this franchise's active sales from Seller, then fills the CTBids URL.",
            )
            return
        self.status_var.set("Loading sales from CT Seller…")
        self._refresh_seller_sales(announce=True)

    def _refresh_seller_sales(self, *, announce: bool = False) -> None:
        token = self.seller_token
        if not token:
            return

        def work() -> None:
            try:
                client = SellerClient(token)
                sales = client.list_sales()
                try:
                    locations = client.list_locations()
                except Exception:
                    locations = []
                err = ""
            except Exception as exc:
                sales = []
                locations = []
                err = str(exc) or type(exc).__name__
            self.root.after(0, self._apply_seller_sales, sales, locations, err, announce)

        threading.Thread(target=work, daemon=True).start()

    def _apply_seller_sales(
        self, sales: list[SellerSale], locations: list[tuple[int, str]], err: str, announce: bool
    ) -> None:
        if err:
            if announce:
                messagebox.showerror(APP_TITLE, err)
            self.status_var.set(err)
            return
        self._seller_sales = list(sales)
        self._seller_locations = list(locations or [])
        dropdown = [s for s in sales if s.in_dropdown]
        labels = [s.combo_label for s in dropdown]
        self._sale_by_label = {s.combo_label: s for s in dropdown}
        self._sale_selecting = True
        try:
            self.sale_combo.configure(values=labels or [self.sale_var.get() or ""])
        except Exception:
            pass
        finally:
            self._sale_selecting = False

        live = self._live_seller_sales()
        for sale in live:
            self._ensure_session(sale.sale_id, sale.buyer_url, sale.title)

        keep_id = self._active_sale_id
        keep = self._sessions.get(keep_id) if keep_id else None
        chosen: SellerSale | None = None
        try:
            current_id = parse_sale_id(self.sale_var.get())
            chosen = self._seller_sale_for(current_id)
        except Exception:
            chosen = None
        if chosen is None and live:
            chosen = live[0]

        watching = any(s.watcher for s in self._sessions.values())
        keep_live = bool(keep and self._seller_sale_for(keep.sale_id) and self._seller_sale_for(keep.sale_id).is_live)
        if keep is not None and (watching or keep_live or not live):
            sale = self._seller_sale_for(keep.sale_id)
            if sale:
                self._ensure_session(sale.sale_id, sale.buyer_url, sale.title)
                self._sale_selecting = True
                try:
                    self.sale_var.set(sale.buyer_url)
                    self.sale_combo.set(sale.combo_label)
                finally:
                    self._sale_selecting = False
            self._sync_sale_tabs(select=keep.tab_name)
        elif live:
            pick = chosen if (chosen and chosen.is_live) else live[0]
            self._select_session(pick.sale_id, title=pick.title, url=pick.buyer_url)
        else:
            self._sync_sale_tabs()

        if announce or (live and keep is None):
            if len(live) == 1:
                self.status_var.set(f"Loaded {live[0].title.strip()} from CT Seller — click Start to watch.")
            elif live:
                self.status_var.set(
                    f"Loaded {len(live)} live sales from CT Seller — pick a tab, then Start. Watch all starts every live sale."
                )
            elif announce:
                self.status_var.set("No live sales on this Seller login. Paste a CTBids URL if you still need one.")

    def _iter_watchers(self) -> list[SaleWatcher]:
        out: list[SaleWatcher] = []
        for sess in self._sessions.values():
            if sess.watcher:
                out.append(sess.watcher)
        return out

    def _sync_start_stop(self) -> None:
        running = bool(self.watcher and self.watcher.running)
        try:
            self.start_btn.configure(state="disabled" if running else "normal")
            self.stop_btn.configure(state="normal" if running else "disabled")
        except Exception:
            pass

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

    def _sash_limits(self) -> tuple[int, int, int]:
        """Return (pane width, min sash x, max sash x)."""
        width = max(int(self.paned.winfo_width()), 0)
        left_min = 360
        right_min = 180
        return width, left_min, max(left_min, width - right_min)

    def _on_sash_press(self, _event=None) -> None:
        self._pause_ticks = True
        self._hide_unmet_overlays()

    def _save_sash(self, _event=None) -> None:
        self._pause_ticks = False
        if not self._activity_open:
            return
        try:
            width, lo, hi = self._sash_limits()
            if width < lo + 180:
                return
            pos = int(self.paned.sash_coord(0)[0])
            if pos < lo or pos > hi:
                return
            self.cfg["sash_pos"] = pos
            self.cfg["theme"] = self.theme_mode
            save_watcher_config(self.cfg)
        except Exception:
            pass
        try:
            self.root.after_idle(self._sync_item_fill_from_width)
        except Exception:
            pass

    def _restore_sash(self) -> None:
        """Place the activity split; ignore stale positions from a bigger/maximized window."""
        if not self._activity_open:
            try:
                self.root.after_idle(self._sync_item_fill_from_width)
            except Exception:
                pass
            return
        try:
            self.paned.update_idletasks()
            width, lo, hi = self._sash_limits()
            if width < 200:
                self._sash_tries += 1
                if self._sash_tries < 25:
                    self.root.after(120, self._restore_sash)
                return
            default = max(lo, width - 220)
            raw = self.cfg.get("sash_pos")
            try:
                pos = int(raw)
            except (TypeError, ValueError):
                pos = default
            if pos < lo or pos > hi:
                pos = default
            self.paned.sash_place(0, pos, 0)
            self._tree_last_width = 0
            self.root.after_idle(self._sync_item_fill_from_width)
        except Exception:
            pass

    def _toggle_activity(self) -> None:
        self._activity_open = not self._activity_open
        self.cfg["activity_open"] = self._activity_open
        try:
            save_watcher_config(self.cfg)
        except Exception:
            pass
        self._apply_activity_pane()

    def _apply_activity_pane(self) -> None:
        try:
            self.act_toggle_btn.configure(
                text="Hide activity" if self._activity_open else "Activity"
            )
        except Exception:
            pass
        try:
            panes = [str(p) for p in self.paned.panes()]
        except Exception:
            panes = []
        act_id = str(self.act_host)
        if self._activity_open:
            if act_id not in panes:
                try:
                    self.paned.add(self.act_host, minsize=180, stretch="never")
                except Exception:
                    pass
            self._sash_tries = 0
            self.root.after(80, self._restore_sash)
        else:
            if act_id in panes:
                try:
                    self.paned.forget(self.act_host)
                except Exception:
                    pass
            self._tree_last_width = 0
            try:
                self.root.after_idle(self._sync_item_fill_from_width)
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
            rowheight=32,
            font=("Segoe UI", 12),
            borderwidth=0,
        )
        style.configure(
            "Bids.Treeview.Heading",
            background=THEMES[self.theme_mode]["heading_bg"],
            foreground=THEMES[self.theme_mode]["heading_fg"],
            font=("Segoe UI Semibold", 11),
            relief="flat",
            padding=(6, 4),
        )
        style.map(
            "Bids.Treeview",
            background=[("selected", GREEN_SOFT)],
            foreground=[("selected", GREEN)],
        )
        style.map("Bids.Treeview.Heading", background=[("active", GREEN_HOVER)])
        self.bids_tree.tag_configure("odd", background=ROW_ALT)
        self.bids_tree.tag_configure("even", background=WHITE)
        self.bids_tree.tag_configure("under", background=AMBER_SOFT)
        self.bids_tree.tag_configure("suspended", foreground=RED)
        self.bids_tree.tag_configure("sold", foreground=MUTED)
        self.bids_tree.tag_configure("ending", foreground=AMBER)
        self.bids_tree.tag_configure("okayed", background=OKAYED_SOFT, foreground=OKAYED)
        tree = getattr(self, "_sale_risk_tree", None)
        if tree is not None:
            tree.tag_configure("odd", background=ROW_ALT)
            tree.tag_configure("even", background=WHITE)
            tree.tag_configure("under", background=AMBER_SOFT)
            tree.tag_configure("suspended", foreground=RED)
            tree.tag_configure("okayed", background=OKAYED_SOFT, foreground=OKAYED)
        risk = getattr(self, "_risk_tree", None)
        if risk is not None:
            risk.tag_configure("odd", background=ROW_ALT)
            risk.tag_configure("even", background=WHITE)
            risk.tag_configure("under", background=AMBER_SOFT)
            risk.tag_configure("suspended", foreground=RED)
            risk.tag_configure("okayed", background=OKAYED_SOFT, foreground=OKAYED)
        self._schedule_unmet_paint()

    def _outline_btn(self, btn: ctk.CTkButton, *, muted: bool = False) -> None:
        if muted:
            btn.configure(fg_color="transparent", text_color=MUTED, border_color=MUTED, hover_color=GREEN_SOFT)
        else:
            btn.configure(fg_color="transparent", text_color=GREEN, border_color=GREEN, hover_color=GREEN_SOFT)

    def _restyle_ui(self) -> None:
        _apply_palette(self.theme_mode)
        if self.manage_window:
            ctk.set_appearance_mode(self.theme_mode)
        t = THEMES[self.theme_mode]
        try:
            self.host.configure(fg_color=t["window"])
        except Exception:
            pass
        if self.manage_window:
            self.root.configure(fg_color=t["window"])

        self.title_label.configure(text_color=GREEN_DARK)
        self.subtitle_label.configure(text_color=MUTED)
        self.theme_btn.configure(
            text="Dark" if self.theme_mode == "light" else "Light",
            text_color=GREEN, border_color=GREEN, hover_color=GREEN_SOFT,
        )
        self._refresh_login_badge()

        self.controls.configure(fg_color=WHITE)
        self.sale_label.configure(text_color=GREEN_DARK)
        self.every_label.configure(text_color=MUTED)
        self.sec_label.configure(text_color=MUTED)
        self.auto_save_chk.configure(
            fg_color=GREEN, hover_color=GREEN_HOVER, text_color=GREEN_DARK,
        )
        self._outline_btn(self.signin_btn)
        self._outline_btn(self.seller_btn)
        self._outline_btn(self.refresh_sales_btn)
        self._outline_btn(self.watch_all_btn)
        self.start_btn.configure(fg_color=GREEN, hover_color=GREEN_HOVER, text_color=ON_ACCENT)
        self._outline_btn(self.stop_btn, muted=True)
        self._outline_btn(self.export_btn)
        self._outline_btn(self.opensale_btn)
        try:
            self.sale_combo.configure(
                button_color=GREEN,
                button_hover_color=GREEN_HOVER,
                border_color=GREEN,
                dropdown_hover_color=GREEN_SOFT,
            )
        except Exception:
            pass
        try:
            self.sale_tabs_hint.configure(text_color=MUTED)
            self.sale_tabs.configure(
                selected_color=GREEN,
                selected_hover_color=GREEN_HOVER,
                unselected_color=GREEN_SOFT,
                unselected_hover_color=t["row_alt"],
                text_color=GREEN_DARK if self.theme_mode == "dark" else ON_ACCENT,
            )
        except Exception:
            pass

        for card in (
            self.metric_hammer, self.metric_bidding, self.metric_open,
            self.metric_below, self.metric_bidders, self.metric_items,
        ):
            card.apply_theme()

        self.bids_panel.configure(fg_color=WHITE)
        self.act_panel.configure(fg_color=WHITE)
        try:
            self.table_host.configure(bg=WHITE)
        except Exception:
            pass
        try:
            self.bids_host.configure(bg=t["window"])
            self.act_host.configure(bg=t["window"])
        except Exception:
            pass
        self.bids_title.configure(text_color=GREEN_DARK)
        self.act_title.configure(text_color=GREEN_DARK)
        self._outline_btn(self.view_btn)
        self._outline_btn(self.retract_btn)
        self._outline_btn(self.suspend_btn)
        self._outline_btn(self.okay_btn)
        self._outline_btn(self.act_toggle_btn)
        if getattr(self, "risk_open_btn", None) is not None:
            self._outline_btn(self.risk_open_btn)
            self._outline_btn(self.risk_retract_btn)
            self._outline_btn(self.risk_suspend_btn)
            self._outline_btn(self.risk_okay_btn)
        self.clear_act_btn.configure(text_color=MUTED, hover_color=GREEN_SOFT)
        self.status_label.configure(text_color=MUTED)
        try:
            self.copyright_label.configure(text_color=MUTED)
        except Exception:
            pass
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
            self.bids_view_menu.configure(
                selected_color=GREEN,
                selected_hover_color=GREEN_HOVER,
                unselected_color=GREEN_SOFT,
                unselected_hover_color=t["row_alt"],
                text_color=GREEN_DARK if self.theme_mode == "dark" else ON_ACCENT,
            )
        except Exception:
            pass
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
        self._apply_column_layout()
        self._bids_fingerprint = ""
        self._render_bids()

    # ----- UI helpers -----
    def _on_filter(self, value: str) -> None:
        mapping = {
            "Bidding": "bidding",
            "Under": "under",
            "Open": "open",
            "Sold": "sold",
            "All": "all",
        }
        self.filter_var.set(mapping.get(value, "bidding"))
        sess = self._current_session()
        if sess is not None:
            sess.filter_value = self.filter_var.get()
        self._bids_fingerprint = ""  # force refresh
        self._render_bids()

    def _on_below_metric(self) -> None:
        self.filter_menu.set("Under")
        self._on_filter("Under")

    def _schedule_bid_render(self) -> None:
        if self._search_after_id is not None:
            try:
                self.root.after_cancel(self._search_after_id)
            except Exception:
                pass
        self._bids_fingerprint = ""
        self._sale_risk_fingerprint = ""
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
        try:
            self._sale_risk_fingerprint = ""
            self._render_sale_risk()
        except Exception:
            pass

    def _append_activity(
        self,
        message: str,
        *,
        kind: str = "info",
        session: _WatchSession | None = None,
        draw: bool | None = None,
        buyer_id: int = 0,
    ) -> None:
        bid = int(buyer_id or 0)
        sess = session if session is not None else self._current_session()
        if sess is not None:
            sess.activity.append((message, kind, bid))
            if len(sess.activity) > 50:
                sess.activity = sess.activity[-50:]
        should_draw = bool(draw) if draw is not None else (sess is None or sess is self._current_session())
        if should_draw:
            self._draw_activity_row(message, kind, buyer_id=bid)

    def _bind_open_seller(self, widget, buyer_id: int) -> None:
        if not buyer_id:
            return

        def _open(_event=None, bid: int = buyer_id) -> None:
            self._open_seller_buyer(bid)

        widget.bind("<Button-1>", _open)
        try:
            widget.configure(cursor="hand2")
        except Exception:
            pass
        for child in widget.winfo_children():
            self._bind_open_seller(child, buyer_id)

    def _draw_activity_row(self, message: str, kind: str, buyer_id: int = 0) -> None:
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
            "sold": (GREEN_SOFT, GREEN, "SOLD"),
            "buy_now": (GREEN_SOFT, GREEN, "BOUGHT NOW"),
            "risk": (AMBER_SOFT, RED, "OPEN IN SELLER"),
            "info": (THEMES[self.theme_mode]["info_bg"], MUTED, "INFO"),
        }
        bg, fg, tag = colors.get(kind, colors["info"])
        stamp = datetime.now().strftime("%I:%M:%S %p").lstrip("0")
        bid = int(buyer_id or 0)

        # Append only — no full rebuild (keeps UI smooth)
        row = ctk.CTkFrame(self.activity_list, fg_color=bg, corner_radius=8)
        row.pack(fill="x", pady=3, padx=2)
        top = ctk.CTkFrame(row, fg_color="transparent")
        top.pack(fill="x", padx=10, pady=(8, 0))
        ctk.CTkLabel(
            top, text=tag, font=("Segoe UI Semibold", 11), text_color=fg, anchor="w",
        ).pack(side="left")
        ctk.CTkLabel(top, text=stamp, font=("Segoe UI", 10), text_color=MUTED).pack(side="right")
        show_hint = bool(bid) and kind == "risk"
        msg_pad = (2, 2) if show_hint else (2, 8)
        ctk.CTkLabel(
            row, text=message, font=("Segoe UI", 12), text_color=GREEN_DARK,
            anchor="w", justify="left", wraplength=270,
        ).pack(fill="x", padx=10, pady=msg_pad)
        if show_hint:
            ctk.CTkLabel(
                row,
                text="Click to open in CT Seller",
                font=("Segoe UI", 10),
                text_color=MUTED,
                anchor="w",
            ).pack(fill="x", padx=10, pady=(0, 8))
        if bid:
            self._bind_open_seller(row, bid)

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
        sess = self._current_session()
        if sess is not None:
            sess.activity.clear()
        self._rebuild_activity(sess)

    def _open_item(self, item: LiveItem, sale_id: int | None = None) -> None:
        url = (item.link or "").strip()
        if not url:
            sid = int(sale_id or 0)
            if sid:
                url = item_link(sid, item)
        if url:
            webbrowser.open(url)

    def _open_seller_buyer(self, buyer_id: int) -> None:
        """Open CT Seller buyer profile (suspend, notes, invoices)."""
        if not buyer_id:
            messagebox.showinfo(
                APP_TITLE,
                "Wait until this username finishes loading, then click again.",
            )
            return
        webbrowser.open(f"{SELLER_SITE}/buyer/edit/{int(buyer_id)}")

    def _open_buyer(self, item: LiveItem) -> None:
        self._open_seller_buyer(int(item.buyer_id or 0))

    def _on_bid_double_click(self, _event=None) -> None:
        sel = self.bids_tree.selection()
        if not sel:
            return
        item = self._bid_items.get(sel[0])
        if item:
            self._open_item(item)

    def _selected_live_item(self) -> LiveItem | None:
        if getattr(self, "_bids_view", "lots") == "risk":
            row = self._selected_sale_risk_row()
            return row.item if row else None
        sel = self.bids_tree.selection()
        if not sel:
            return None
        return self._bid_items.get(sel[0])

    def _open_selected_bid(self) -> None:
        if getattr(self, "_bids_view", "lots") == "risk":
            row = self._selected_sale_risk_row()
            if not row:
                messagebox.showinfo(APP_TITLE, "Click a bidder first, then Open in Seller.")
                return
            self._open_buyer(row.item)
            return
        item = self._selected_live_item()
        if not item:
            messagebox.showinfo(APP_TITLE, "Select a row in the table first, then click View item.")
            return
        self._open_item(item)

    def _on_suspend_selected(self) -> None:
        item = self._selected_live_item()
        if not item or not item.high_bidder:
            messagebox.showinfo(APP_TITLE, "Click a bidder row first, then Suspend.")
            return
        self._open_suspend_dialog(item, unsuspend=False)

    def _on_okay_selected(self) -> None:
        if getattr(self, "_bids_view", "lots") == "risk":
            row = self._selected_sale_risk_row()
            if not row or not row.name:
                messagebox.showinfo(APP_TITLE, "Click a bidder first, then Okay.")
                return
            self._toggle_okayed(row.item)
            return
        item = self._selected_live_item()
        if not item or not item.high_bidder:
            messagebox.showinfo(APP_TITLE, "Click a bidder row first, then Okay.")
            return
        self._toggle_okayed(item)

    def _on_risk_okay(self) -> None:
        row = self._selected_risk_row()
        if not row:
            messagebox.showinfo(APP_TITLE, "Click a bidder on At risk first.")
            return
        self._toggle_okayed(row.item)

    def _is_okayed(self, item: LiveItem | None) -> bool:
        if item is None:
            return False
        bid = int(item.buyer_id or 0)
        return bool(bid and bid in self._okayed_buyers)

    def _toggle_okayed(self, item: LiveItem) -> None:
        if not item or not item.buyer_id:
            messagebox.showinfo(APP_TITLE, "Wait until this row has a username, then try again.")
            return
        name = (item.high_bidder or "this bidder").strip() or "this bidder"
        bid = int(item.buyer_id)
        if bid in self._okayed_buyers:
            self._okayed_buyers.discard(bid)
            okayed = False
        else:
            self._okayed_buyers.add(bid)
            okayed = True
        self.cfg["okayed_buyers"] = sorted(self._okayed_buyers)
        try:
            save_watcher_config(self.cfg)
        except Exception:
            pass
        self._bids_fingerprint = ""
        self._risk_fingerprint = ""
        self._sale_risk_fingerprint = ""
        try:
            self._render_bids()
        except Exception:
            pass
        try:
            self._render_risk()
        except Exception:
            pass
        try:
            self._render_sale_risk()
        except Exception:
            pass
        if okayed:
            self.status_var.set(f"{name} marked okay — blue row. Right-click to undo.")
        else:
            self.status_var.set(
                f"Okay removed for {name} — they show as at-risk again if unpaid, void, or suspended."
            )

    def _add_okay_menu(self, menu: tk.Menu, item: LiveItem, name: str) -> None:
        if not item.buyer_id:
            return
        if self._is_okayed(item):
            menu.add_command(
                label=f"Undo okay on {name}",
                command=lambda: self._toggle_okayed(item),
            )
        else:
            menu.add_command(
                label=f"Okay {name} — not a risk",
                command=lambda: self._toggle_okayed(item),
            )

    def _on_retract_selected(self) -> None:
        if getattr(self, "_bids_view", "lots") == "risk":
            row = self._selected_sale_risk_row()
            if not row or not row.name:
                messagebox.showinfo(APP_TITLE, "Click a bidder first, then Retract bids.")
                return
            sale_id = int(self._active_sale_id or 0)
            lots = [
                it for it in row.items
                if int(row.item_sales.get(it.item_id) or 0) == sale_id and self._owns_sale(sale_id)
            ]
            if not lots:
                messagebox.showinfo(
                    APP_TITLE,
                    "Retract only works on sales that belong to this CT Seller login.",
                )
                return
            self._open_retract_dialog(row.item, all_lots=True, lots=lots, sale_id=sale_id or None)
            return
        item = self._selected_live_item()
        if not item or not item.high_bidder:
            messagebox.showinfo(APP_TITLE, "Click a bidder row first, then Retract bids.")
            return
        if not self._owns_sale(self._active_sale_id):
            messagebox.showinfo(
                APP_TITLE,
                "Retract only works on sales that belong to this CT Seller login.",
            )
            return
        self._open_retract_dialog(item, all_lots=False)

    def _tree_col(self, name: str) -> str:
        """Treeview identify_column id (#1, #2, …) for a named column."""
        cols = tuple(self.bids_tree["columns"])
        return f"#{cols.index(name) + 1}"

    def _apply_column_layout(self) -> None:
        saved = self.cfg.get("tree_cols") if isinstance(self.cfg.get("tree_cols"), dict) else {}
        for col, (width, minwidth, anchor) in self._col_defaults.items():
            try:
                user_w = int(saved.get(col) or 0)
            except (TypeError, ValueError):
                user_w = 0
            # Ignore runaway widths from the old Item-fill bug / accidental drags.
            cap = 720 if col == "item" else max(width * 2, minwidth + 48)
            if user_w > cap:
                user_w = 0
            self.bids_tree.column(
                col,
                width=user_w if user_w >= minwidth else width,
                minwidth=minwidth,
                anchor=anchor,
                stretch=False,
            )
        # Leftover pane width lives here so shrinking Item does not snap back.
        self.bids_tree.column("pad", width=8, minwidth=0, stretch=True, anchor="w")
        self._tree_last_width = 0
        try:
            self.root.after_idle(self._sync_item_fill_from_width)
        except Exception:
            pass

    def _sum_col_widths(self, skip: tuple[str, ...] = ()) -> int:
        total = 0
        try:
            cols = tuple(self.bids_tree["columns"])
        except Exception:
            return 0
        for col in cols:
            if col in skip:
                continue
            try:
                total += int(self.bids_tree.column(col, "width"))
            except Exception:
                pass
        return total

    def _item_fill_cap(self) -> int:
        """Widest Item can be without pushing Price / Reserve / Time left off-screen."""
        try:
            w = int(self.bids_tree.winfo_width())
        except Exception:
            w = 0
        others = self._sum_col_widths(skip=("item", "pad"))
        min_w = int(self._col_defaults["item"][1])
        return max(min_w, w - others - 8)

    def _set_item_width(self, width: int) -> None:
        min_w = int(self._col_defaults["item"][1])
        try:
            min_w = int(self.bids_tree.column("item", "minwidth") or min_w)
        except Exception:
            pass
        try:
            self.bids_tree.column("item", width=max(min_w, int(width)), stretch=False)
        except Exception:
            pass

    def _give_extra_to_item(self, extra: int) -> None:
        if extra < 4:
            return
        try:
            item_w = int(self.bids_tree.column("item", "width"))
        except Exception:
            return
        cap = self._item_fill_cap()
        new_w = min(cap, item_w + extra)
        if new_w > item_w:
            self._set_item_width(new_w)

    def _clamp_item_to_viewport(self) -> None:
        try:
            item_w = int(self.bids_tree.column("item", "width"))
        except Exception:
            return
        cap = self._item_fill_cap()
        if item_w > cap:
            self._set_item_width(cap)

    def _fit_tree_to_width(self) -> None:
        """Keep Price / Reserve / Time left on screen; grow Item only with leftover room."""
        if self._pause_ticks:
            return
        try:
            w = int(self.bids_tree.winfo_width())
        except Exception:
            return
        if w < 80:
            return
        self._tree_last_width = w
        try:
            cols = [c for c in self.bids_tree["columns"] if c != "pad"]
        except Exception:
            return
        widths: dict[str, int] = {}
        mins: dict[str, int] = {}
        for col in cols:
            try:
                widths[col] = int(self.bids_tree.column(col, "width"))
                mins[col] = int(self.bids_tree.column(col, "minwidth") or 0)
            except Exception:
                return
        total = sum(widths.values())
        if total > w:
            overflow = total - w
            if "item" in widths:
                take = min(overflow, max(0, widths["item"] - mins.get("item", 0)))
                widths["item"] -= take
                overflow -= take
            if overflow > 0:
                flexible = [c for c in cols if c != "item"]
                slack = sum(max(0, widths[c] - mins[c]) for c in flexible)
                remaining = overflow
                for i, col in enumerate(flexible):
                    room = max(0, widths[col] - mins[col])
                    if room <= 0 or slack <= 0:
                        continue
                    take = remaining if i == len(flexible) - 1 else int(overflow * room / slack)
                    take = min(room, max(0, take))
                    widths[col] -= take
                    remaining -= take
            for col in cols:
                try:
                    self.bids_tree.column(col, width=max(mins[col], widths[col]), stretch=False)
                except Exception:
                    pass
            try:
                self.bids_tree.column("pad", width=4, stretch=True)
            except Exception:
                pass
            return
        extra = w - total
        if extra > 8 and "item" in widths:
            others = total - widths["item"]
            cap = max(mins.get("item", 80), w - others - 8)
            new_w = min(cap, widths["item"] + extra)
            if new_w > widths["item"]:
                self._set_item_width(new_w)

    def _sync_item_fill_from_width(self) -> None:
        """Grow Item into leftover space, but never hide Price / Reserve / etc."""
        self._fit_tree_to_width()

    def _on_tree_configure(self, event) -> None:
        if event.widget is not self.bids_tree:
            return
        if self._pause_ticks:
            return
        self._sync_item_fill_from_width()
        self._schedule_unmet_paint()

    def _on_tree_yview(self, first, last) -> None:
        try:
            self._tree_yscroll.set(first, last)
        except Exception:
            pass
        self._schedule_unmet_paint()

    def _on_tree_xview(self, first, last) -> None:
        try:
            self._tree_xscroll.set(first, last)
        except Exception:
            pass
        self._schedule_unmet_paint()

    def _schedule_unmet_paint(self) -> None:
        if self._unmet_paint_after:
            try:
                self.root.after_cancel(self._unmet_paint_after)
            except Exception:
                pass
        try:
            self._unmet_paint_after = self.root.after(20, self._paint_unmet_reserves)
        except Exception:
            self._unmet_paint_after = None

    def _hide_unmet_overlays(self) -> None:
        for lbl in self._unmet_overlays:
            try:
                lbl.place_forget()
            except Exception:
                pass

    def _paint_unmet_reserves(self) -> None:
        """Yellow overlay on the Reserve number when it is not met (Treeview can't color one cell)."""
        self._unmet_paint_after = None
        if getattr(self, "_bids_view", "lots") == "risk":
            self._hide_unmet_overlays()
            return
        tree = getattr(self, "bids_tree", None)
        if tree is None:
            return
        if self._pause_ticks:
            self._hide_unmet_overlays()
            return
        needed: list[tuple[str, LiveItem, tuple[int, int, int, int]]] = []
        try:
            selected = set(tree.selection())
            for iid in tree.get_children():
                item = self._bid_items.get(iid)
                if item is None or not _below_reserve(item):
                    continue
                bbox = tree.bbox(iid, "reserve")
                if not bbox:
                    continue
                needed.append((iid, item, bbox))
        except Exception:
            return

        while len(self._unmet_overlays) < len(needed):
            lbl = tk.Label(
                tree,
                text="",
                font=("Segoe UI", 12),
                anchor="e",
                padx=8,
                borderwidth=0,
                highlightthickness=0,
            )
            lbl.bind("<MouseWheel>", self._on_unmet_wheel)
            lbl.bind("<Button-4>", self._on_unmet_wheel)
            lbl.bind("<Button-5>", self._on_unmet_wheel)
            self._unmet_overlays.append(lbl)

        for i, (iid, item, bbox) in enumerate(needed):
            lbl = self._unmet_overlays[i]
            x, y, w, h = bbox
            tags = set(tree.item(iid, "tags") or ())
            if iid in selected:
                bg = GREEN_SOFT
            elif "okayed" in tags:
                bg = OKAYED_SOFT
            elif "under" in tags:
                bg = AMBER_SOFT
            elif "odd" in tags:
                bg = ROW_ALT
            else:
                bg = WHITE
            lbl.configure(text=format_reserve(item), fg=UNMET, bg=bg)
            lbl.bind("<Button-1>", lambda e, row=iid: self._on_unmet_click(row))
            lbl.bind("<Double-1>", lambda e, row=iid: self._on_unmet_double(row))
            lbl.bind("<Button-3>", lambda e, row=iid: self._on_unmet_right(e, row))
            try:
                lbl.place(x=x, y=y, width=w, height=h)
            except Exception:
                pass

        for lbl in self._unmet_overlays[len(needed):]:
            try:
                lbl.place_forget()
            except Exception:
                pass

    def _on_unmet_wheel(self, event) -> None:
        try:
            if getattr(event, "delta", 0):
                self.bids_tree.yview_scroll(-1 if event.delta > 0 else 1, "units")
            elif getattr(event, "num", 0) == 4:
                self.bids_tree.yview_scroll(-1, "units")
            elif getattr(event, "num", 0) == 5:
                self.bids_tree.yview_scroll(1, "units")
        except Exception:
            pass
        self._schedule_unmet_paint()

    def _on_unmet_click(self, iid: str) -> None:
        try:
            self.bids_tree.selection_set(iid)
            self.bids_tree.focus(iid)
        except Exception:
            pass
        self._schedule_unmet_paint()

    def _on_unmet_double(self, iid: str) -> None:
        self._on_unmet_click(iid)
        item = self._bid_items.get(iid)
        if item:
            self._open_item(item)

    def _on_unmet_right(self, event, iid: str) -> None:
        self._on_unmet_click(iid)
        try:
            bbox = self.bids_tree.bbox(iid)
            if bbox:
                event.x = int(bbox[0] + 4)
                event.y = int(bbox[1] + bbox[3] // 2)
        except Exception:
            pass
        try:
            self._on_bid_right_click(event)
        except Exception:
            pass

    def _save_column_widths(self) -> None:
        widths: dict[str, int] = {}
        for col in self._col_defaults:
            try:
                widths[col] = int(self.bids_tree.column(col, "width"))
            except Exception:
                continue
        if not widths:
            return
        self.cfg["tree_cols"] = widths
        try:
            save_watcher_config(self.cfg)
        except Exception:
            pass

    def _on_tree_press(self, event) -> None:
        self._tree_press_region = self.bids_tree.identify_region(event.x, event.y)
        self._tree_press_x = event.x
        self._ignore_header_sort = self._tree_press_region == "separator"
        self._pause_ticks = self._tree_press_region == "separator"
        if self._pause_ticks:
            self._hide_unmet_overlays()

    def _on_tree_motion(self, event) -> None:
        if self._pause_ticks or self._tree_press_region == "separator":
            return
        region = self.bids_tree.identify_region(event.x, event.y)
        if region == "separator":
            if self._tree_cursor != "sb_h_double_arrow":
                self._tree_cursor = "sb_h_double_arrow"
                try:
                    self.bids_tree.configure(cursor="sb_h_double_arrow")
                except Exception:
                    pass
            self._acct_tip.hide()
            return
        col = self.bids_tree.identify_column(event.x)
        bidder_col = self._tree_col("bidder")
        view_col = self._tree_col("view")
        status_col = self._tree_col("status")
        clickable = region == "cell" and col in (bidder_col, view_col)
        want = "hand2" if clickable else ""
        if want != self._tree_cursor:
            self._tree_cursor = want
            try:
                self.bids_tree.configure(cursor=want)
            except Exception:
                pass
        if region == "cell" and col == bidder_col:
            row_id = self.bids_tree.identify_row(event.y)
            item = self._bid_items.get(row_id) if row_id else None
            if item and item.high_bidder:
                self._acct_tip.schedule(
                    f"Click to open {item.high_bidder} in CT Seller\n"
                    "(suspension, notes, unpaid invoices).",
                    event.x_root,
                    event.y_root,
                )
                return
        if region == "cell" and col in (self._tree_col("met"), self._tree_col("reserve")):
            row_id = self.bids_tree.identify_row(event.y)
            item = self._bid_items.get(row_id) if row_id else None
            if item:
                self._acct_tip.schedule(
                    _reserve_help_text(item),
                    event.x_root,
                    event.y_root,
                )
                return
        if col != status_col:
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
        status = "Okayed" if self._is_okayed(item) else (item.account_status or "—")
        tip = _acct_help_text(status, item)
        if item.high_bidder:
            tip = f"{item.high_bidder} — {status}\n\n{tip}"
        self._acct_tip.schedule(tip, event.x_root, event.y_root)

    def _on_tree_leave(self, _event=None) -> None:
        self._acct_tip.hide()
        self._tree_cursor = ""
        try:
            self.bids_tree.configure(cursor="")
        except Exception:
            pass

    def _on_tree_select(self, _event=None) -> None:
        self._acct_tip.hide()
        self._schedule_unmet_paint()

    def _on_bid_click(self, event) -> None:
        """Click username → CT Seller buyer page; View cell → lot page."""
        self._pause_ticks = False
        region = self.bids_tree.identify_region(event.x, event.y)
        dragged = abs(event.x - int(getattr(self, "_tree_press_x", event.x) or event.x)) > 4
        if region == "separator" or self._tree_press_region == "separator" or (
            dragged and self._tree_press_region == "heading"
        ):
            self._ignore_header_sort = True
            self._pause_ticks = False
            self._save_column_widths()
            self._schedule_unmet_paint()
            return
        if region != "cell":
            return
        col = self.bids_tree.identify_column(event.x)
        row_id = self.bids_tree.identify_row(event.y)
        if not row_id:
            return
        item = self._bid_items.get(row_id)
        if not item:
            return
        if col == self._tree_col("bidder"):
            self._open_buyer(item)
        elif col == self._tree_col("view"):
            self._open_item(item)

    def _add_location_suspend_menu(self, menu: tk.Menu, item: LiveItem, name: str) -> None:
        """Suspend always; Unsuspend only if this Seller login already banned them."""
        menu.add_command(
            label=f"Suspend {name} at this location…",
            command=lambda: self._open_suspend_dialog(item, unsuspend=False),
        )
        if item.suspended_here:
            menu.add_command(
                label=f"Unsuspend {name} at this location…",
                command=lambda: self._open_suspend_dialog(item, unsuspend=True),
            )

    def _on_bid_right_click(self, event) -> None:
        row_id = self.bids_tree.identify_row(event.y)
        if not row_id:
            return
        self.bids_tree.selection_set(row_id)
        self.bids_tree.focus(row_id)
        item = self._bid_items.get(row_id)
        menu = tk.Menu(self.root, tearoff=0)
        if not item or not item.high_bidder:
            menu.add_command(label="No bidder on this row yet", state="disabled")
        elif not item.buyer_id:
            menu.add_command(label="Wait for username / bidder id to load", state="disabled")
        else:
            name = item.high_bidder
            menu.add_command(
                label=f"Open {name} in CT Seller…",
                command=lambda: self._open_buyer(item),
            )
            menu.add_separator()
            self._add_okay_menu(menu, item, name)
            if not self.seller_token:
                menu.add_separator()
                menu.add_command(
                    label="Seller sign in to suspend / retract",
                    command=self._on_seller_login,
                )
            else:
                menu.add_separator()
                self._add_location_suspend_menu(menu, item, name)
                if self._owns_sale(self._active_sale_id):
                    menu.add_command(
                        label=f"Retract {name}'s bids on this lot…",
                        command=lambda: self._open_retract_dialog(item, all_lots=False),
                    )
                    menu.add_command(
                        label=f"Retract {name}'s bids on all their lots this sale…",
                        command=lambda: self._open_retract_dialog(item, all_lots=True),
                    )
                else:
                    menu.add_command(
                        label="Retract only on this Seller login's sales",
                        state="disabled",
                    )
            menu.add_separator()
            menu.add_command(label="Open lot on CTBids", command=lambda: self._open_item(item))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _open_suspend_dialog(self, item: LiveItem, *, unsuspend: bool) -> None:
        if not self.seller_token:
            messagebox.showinfo(APP_TITLE, "Seller sign in first — this uses CT Seller.")
            return
        if not item.buyer_id:
            messagebox.showinfo(APP_TITLE, "Wait until this row has a username, then try again.")
            return

        title = "Unsuspend bidder" if unsuspend else "Suspend bidder"
        types = list(REVOKE_TYPES if unsuspend else SUSPEND_TYPES)
        default_type = types[-1] if unsuspend else types[0]
        default_reason = (
            "Resolved during live sale."
            if unsuspend
            else f"Suspended from live sale {self.sale_var.get().strip() or 'watch'}."
        )
        other_lots = [
            it for it in self._bid_items.values()
            if it.buyer_id == item.buyer_id and it.item_id != item.item_id
        ]

        win = ctk.CTkToplevel(self.root)
        win.title(title)
        win.geometry("480x560" if not unsuspend else "460x420")
        win.minsize(440, 400 if unsuspend else 520)
        win.transient(self.root)
        try:
            win.configure(fg_color=THEMES[self.theme_mode]["window"])
        except Exception:
            pass
        win.after(200, win.grab_set)

        pad = 18
        ctk.CTkLabel(win, text=title, font=("Segoe UI Semibold", 20), text_color=GREEN_DARK).pack(
            anchor="w", padx=pad, pady=(pad, 4)
        )
        if unsuspend:
            body = (
                f"{item.high_bidder}  ·  buyer #{item.buyer_id}\n"
                f"Lifts their suspension only at {self._seller_location_label()}. "
                "Does not change other Caring Transitions franchises."
            )
        else:
            body = (
                f"{item.high_bidder}  ·  buyer #{item.buyer_id}\n"
                f"Suspends them only at {self._seller_location_label()} "
                "(this CT Seller login). Does not suspend them at other franchises."
            )
        ctk.CTkLabel(
            win,
            text=body,
            font=("Segoe UI", 13),
            text_color=MUTED,
            wraplength=410,
            justify="left",
        ).pack(anchor="w", padx=pad, pady=(0, 10))

        card = ctk.CTkFrame(win, corner_radius=12, fg_color=WHITE)
        card.pack(fill="x", padx=pad)
        ctk.CTkLabel(card, text="Type", font=("Segoe UI Semibold", 13), text_color=GREEN_DARK).pack(
            anchor="w", padx=14, pady=(12, 4)
        )
        type_box = ctk.CTkComboBox(card, values=types, height=36, width=400)
        type_box.set(default_type)
        type_box.pack(fill="x", padx=14)
        ctk.CTkLabel(card, text="Reason", font=("Segoe UI Semibold", 13), text_color=GREEN_DARK).pack(
            anchor="w", padx=14, pady=(10, 4)
        )
        reason_box = ctk.CTkTextbox(card, height=90)
        reason_box.pack(fill="x", padx=14, pady=(0, 8))
        reason_box.insert("1.0", default_reason)
        retract_this = BooleanVar(value=not unsuspend)
        retract_all = BooleanVar(value=False)
        if not unsuspend:
            ctk.CTkCheckBox(
                card,
                text="Also retract their bids on this lot",
                variable=retract_this,
                fg_color=GREEN,
                hover_color=GREEN_HOVER,
                text_color=GREEN_DARK,
                font=("Segoe UI", 12),
            ).pack(anchor="w", padx=14, pady=(0, 4))
            extra = f" ({len(other_lots)} more)" if other_lots else ""
            ctk.CTkCheckBox(
                card,
                text=f"Retract on every lot they are high bidder this sale{extra}",
                variable=retract_all,
                fg_color=GREEN,
                hover_color=GREEN_HOVER,
                text_color=GREEN_DARK,
                font=("Segoe UI", 12),
            ).pack(anchor="w", padx=14, pady=(0, 12))
        else:
            ctk.CTkFrame(card, fg_color="transparent", height=4).pack(pady=(0, 8))

        status_local = StringVar(value="")
        ctk.CTkLabel(win, textvariable=status_local, font=("Segoe UI", 12), text_color=MUTED).pack(
            anchor="w", padx=pad, pady=(8, 0)
        )

        btns = ctk.CTkFrame(win, fg_color="transparent")
        btns.pack(fill="x", padx=pad, pady=(8, pad))
        action_btn = ctk.CTkButton(
            btns,
            text="Unsuspend" if unsuspend else "Suspend",
            width=130,
            height=36,
            fg_color=GREEN if unsuspend else "#c24141",
            hover_color=GREEN_HOVER if unsuspend else "#9f2d2d",
            text_color=ON_ACCENT,
            font=("Segoe UI Semibold", 13),
        )
        action_btn.pack(side="left")
        ctk.CTkButton(
            btns,
            text="Cancel",
            width=90,
            height=36,
            fg_color="transparent",
            text_color=MUTED,
            border_color=MUTED,
            border_width=1,
            hover_color=GREEN_SOFT,
            command=win.destroy,
        ).pack(side="left", padx=(8, 0))

        def submit() -> None:
            reason = reason_box.get("1.0", "end").strip()
            kind = type_box.get().strip()
            if not reason:
                status_local.set("Enter a reason.")
                return
            action_btn.configure(state="disabled")
            status_local.set("Talking to CT Seller…")
            do_retract_this = bool(retract_this.get())
            do_retract_all = bool(retract_all.get())

            def work() -> None:
                retracted: list[int] = []
                retract_errors: list[str] = []
                try:
                    client = SellerClient(self.seller_token)
                    if unsuspend:
                        locations = client.revoke_buyer_suspend(
                            item.buyer_id, reason=reason, revoke_type=kind
                        )
                    else:
                        locations = client.suspend_buyer(
                            item.buyer_id,
                            reason=reason,
                            suspension_type=kind,
                        )
                        lot_ids: list[int] = []
                        if do_retract_all:
                            lot_ids = [
                                it.item_id
                                for it in self._bid_items.values()
                                if it.buyer_id == item.buyer_id
                            ]
                        elif do_retract_this:
                            lot_ids = [item.item_id]
                        if lot_ids and self._owns_sale(self._active_sale_id):
                            try:
                                retracted, retract_errors = client.retract_bidder_on_items(
                                    lot_ids, item.buyer_id, reason=reason
                                )
                            except Exception as exc:
                                retract_errors = [str(exc) or type(exc).__name__]
                        elif lot_ids:
                            retract_errors = [
                                "Retract skipped — this sale is not on this CT Seller login."
                            ]
                except Exception as exc:
                    msg = str(exc) or type(exc).__name__
                    self.root.after(0, lambda m=msg: _fail(m))
                    return
                names = ", ".join(title for _i, title in locations) or "your locations"
                self.root.after(
                    0,
                    lambda n=names, r=list(retracted), e=list(retract_errors): _ok(n, r, e),
                )

            threading.Thread(target=work, daemon=True).start()

        def _fail(msg: str) -> None:
            action_btn.configure(state="normal")
            status_local.set(msg)

        def _ok(loc_names: str, retracted: list[int], retract_errors: list[str]) -> None:
            try:
                win.destroy()
            except Exception:
                pass
            self._apply_local_suspension(item.buyer_id, suspended=not unsuspend)
            if retracted:
                self._apply_retract_locally(retracted)
            verb = "Unsuspended" if unsuspend else "Suspended"
            extra = ""
            if retracted:
                extra = f" Retracted bids on {len(retracted)} lot(s)."
            if retract_errors:
                extra += f" {len(retract_errors)} retract failed."
            self.status_var.set(f"{verb} {item.high_bidder} at {loc_names}.{extra}")
            self._append_activity(
                f"{verb} {item.high_bidder} (#{item.buyer_id}) at {loc_names}.{extra}",
                kind="info",
            )

        action_btn.configure(command=submit)

    def _lots_for_buyer(self, buyer_id: int) -> list[LiveItem]:
        return [it for it in self._bid_items.values() if it.buyer_id == buyer_id]

    def _open_retract_dialog(
        self,
        item: LiveItem,
        *,
        all_lots: bool,
        lots: list[LiveItem] | None = None,
        sale_id: int | None = None,
    ) -> None:
        if not self.seller_token:
            messagebox.showinfo(APP_TITLE, "Seller sign in first — bid retract uses CT Seller.")
            return
        if not item.buyer_id:
            messagebox.showinfo(APP_TITLE, "Wait until this row has a username, then try again.")
            return
        sid = int(sale_id or self._active_sale_id or 0)
        if lots is None:
            lots = [item] if not all_lots else self._lots_for_buyer(item.buyer_id)
        if not self._owns_sale(sid):
            messagebox.showinfo(
                APP_TITLE,
                "Retract only works on sales that belong to this CT Seller login.",
            )
            return
        if not lots:
            messagebox.showinfo(APP_TITLE, "No lots found for this bidder on this Seller login's sales.")
            return
        title = "Retract this bidder on all their lots" if all_lots else "Retract this bidder on this lot"
        default_reason = f"Retracted during live sale ({item.high_bidder})."

        win = ctk.CTkToplevel(self.root)
        win.title(title)
        win.geometry("460x380")
        win.minsize(420, 340)
        win.transient(self.root)
        try:
            win.configure(fg_color=THEMES[self.theme_mode]["window"])
        except Exception:
            pass
        win.after(200, win.grab_set)

        pad = 18
        ctk.CTkLabel(win, text=title, font=("Segoe UI Semibold", 20), text_color=GREEN_DARK).pack(
            anchor="w", padx=pad, pady=(pad, 4)
        )
        lot_bits = ", ".join((it.sku or str(it.item_id)) for it in lots[:8])
        if len(lots) > 8:
            lot_bits += f" +{len(lots) - 8} more"
        if all_lots:
            who = (
                f"Only {item.high_bidder} (buyer #{item.buyer_id}).\n"
                f"Retracts their bids on {len(lots)} lots they are high bidder on: {lot_bits}\n"
                "Does not retract anyone else’s bids, and only on this Seller login's sales."
            )
        else:
            sku = item.sku or str(item.item_id)
            who = (
                f"Only {item.high_bidder} (buyer #{item.buyer_id}) on lot {sku}.\n"
                "Does not retract other bidders on this lot, and does not touch their other lots."
            )
        ctk.CTkLabel(
            win,
            text=who,
            font=("Segoe UI", 13),
            text_color=MUTED,
            wraplength=410,
            justify="left",
        ).pack(anchor="w", padx=pad, pady=(0, 10))

        card = ctk.CTkFrame(win, corner_radius=12, fg_color=WHITE)
        card.pack(fill="x", padx=pad)
        ctk.CTkLabel(card, text="Reason", font=("Segoe UI Semibold", 13), text_color=GREEN_DARK).pack(
            anchor="w", padx=14, pady=(12, 4)
        )
        reason_box = ctk.CTkTextbox(card, height=80)
        reason_box.pack(fill="x", padx=14, pady=(0, 12))
        reason_box.insert("1.0", default_reason)

        status_local = StringVar(value="")
        ctk.CTkLabel(win, textvariable=status_local, font=("Segoe UI", 12), text_color=MUTED).pack(
            anchor="w", padx=pad, pady=(8, 0)
        )
        btns = ctk.CTkFrame(win, fg_color="transparent")
        btns.pack(fill="x", padx=pad, pady=(8, pad))
        action_btn = ctk.CTkButton(
            btns,
            text=f"Retract {item.high_bidder}" if not all_lots else f"Retract {len(lots)} lot(s)",
            width=150,
            height=36,
            fg_color="#c24141",
            hover_color="#9f2d2d",
            text_color=ON_ACCENT,
            font=("Segoe UI Semibold", 13),
        )
        action_btn.pack(side="left")
        ctk.CTkButton(
            btns,
            text="Cancel",
            width=90,
            height=36,
            fg_color="transparent",
            text_color=MUTED,
            border_color=MUTED,
            border_width=1,
            hover_color=GREEN_SOFT,
            command=win.destroy,
        ).pack(side="left", padx=(8, 0))

        lot_ids = [it.item_id for it in lots]

        def submit() -> None:
            reason = reason_box.get("1.0", "end").strip()
            if not reason:
                status_local.set("Enter a reason.")
                return
            action_btn.configure(state="disabled")
            status_local.set("Retracting bids…")

            def work() -> None:
                try:
                    client = SellerClient(self.seller_token)
                    owned_sales = client.owned_sale_ids()
                    if int(sid) not in owned_sales:
                        raise RuntimeError("That sale is not on this CT Seller login.")
                    ok, errors = client.retract_bidder_on_items(
                        lot_ids, item.buyer_id, reason=reason
                    )
                except Exception as exc:
                    msg = str(exc) or type(exc).__name__
                    self.root.after(0, lambda m=msg: _fail(m))
                    return
                self.root.after(0, lambda o=list(ok), e=list(errors): _ok(o, e))

            threading.Thread(target=work, daemon=True).start()

        def _fail(msg: str) -> None:
            action_btn.configure(state="normal")
            status_local.set(msg)

        def _ok(ok_ids: list[int], errors: list[str]) -> None:
            try:
                win.destroy()
            except Exception:
                pass
            self._apply_retract_locally(ok_ids)
            extra = f" {len(errors)} failed." if errors else ""
            self.status_var.set(
                f"Retracted {item.high_bidder} on {len(ok_ids)} lot(s).{extra}"
            )
            self._append_activity(
                f"Retracted {item.high_bidder} (#{item.buyer_id}) on {len(ok_ids)} lot(s).{extra}",
                kind="info",
            )

        action_btn.configure(command=submit)

    def _apply_retract_locally(self, item_ids: list[int]) -> None:
        id_set = {int(i) for i in item_ids}
        if self.watcher:
            self.watcher.forget_bidders_on_items(list(id_set))
        for it in self._bid_items.values():
            if it.item_id in id_set:
                it.high_bidder = ""
                it.buyer_id = 0
                it.membership_completed = False
                it.suspended = False
                it.suspended_here = False
                it.unpaid_invoice_count = None
                it.paid_invoice_count = None
                it.void_invoice_count = None
        self._bids_fingerprint = ""
        self._render_bids()

    def _apply_local_suspension(self, buyer_id: int, *, suspended: bool) -> None:
        for watcher in self._iter_watchers():
            watcher.note_suspension(buyer_id, suspended)
        for it in self._bid_items.values():
            if it.buyer_id == buyer_id:
                it.suspended_here = bool(suspended)
                if suspended:
                    it.suspended = True
        for sess in self._sessions.values():
            if not sess.snap:
                continue
            for it in sess.snap.items.values():
                if it.buyer_id == buyer_id:
                    it.suspended_here = bool(suspended)
                    if suspended:
                        it.suspended = True
        self._bids_fingerprint = ""
        self._risk_fingerprint = ""
        self._sale_risk_fingerprint = ""
        self._render_bids()
        self._render_risk()

    def _on_sort_column(self, col: str) -> None:
        """Toggle sort when a column header is clicked."""
        if col == "pad" or self._ignore_header_sort:
            self._ignore_header_sort = False
            return
        if self._sort_col == col:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_col = col
            # First click: high→low for money/counts; soonest-first for time; A→Z for text
            self._sort_asc = col not in (
                "price", "buynow", "reserve", "met", "bids", "unpaid", "voids", "paid",
            )
        self._refresh_sort_headings()
        self._bids_fingerprint = ""
        self._render_bids()

    def _refresh_sort_headings(self) -> None:
        for col, label in self._col_headings.items():
            if col == self._sort_col:
                mark = " ▲" if self._sort_asc else " ▼"
                text = f"{label}{mark}"
            else:
                text = label
            anchor = "e" if col in self._num_cols else "w"
            self.bids_tree.heading(
                col,
                text=text,
                anchor=anchor,
                command=lambda c=col: self._on_sort_column(c),
            )

    @staticmethod
    def _seconds_left(item: LiveItem) -> float:
        if item.is_sold or item.is_closed:
            return float("inf")
        close = _parse_close_time(item.close_time)
        if close is None:
            return float("inf")
        return (close - datetime.now(timezone.utc)).total_seconds()

    def _sort_key(self, item: LiveItem):
        col = self._sort_col
        if col == "sku":
            return (item.sku or "").lower()
        if col == "price":
            return float(item.bid_price or 0)
        if col == "buynow":
            return float(item.buy_now_price or 0)
        if col == "reserve":
            return float(item.reserve_price or 0)
        if col == "met":
            if item.reserve_price <= 0:
                return -1.0
            return float(item.reserve_price - item.bid_price)
        if col == "left":
            return self._seconds_left(item)
        if col == "bids":
            return int(item.bid_count or 0)
        if col == "bidder":
            return (item.high_bidder or "").lower()
        if col == "status":
            return format_acct_label(item).lower()
        if col == "unpaid":
            return -1 if item.unpaid_invoice_count is None else int(item.unpaid_invoice_count)
        if col == "voids":
            return -1 if item.void_invoice_count is None else int(item.void_invoice_count)
        if col == "paid":
            return -1 if item.paid_invoice_count is None else int(item.paid_invoice_count)
        if col == "item":
            return (item.title or "").lower()
        if col == "category":
            return (item.category or "").lower()
        return 0

    def _filtered_rows(self, snap: Snapshot) -> list[LiveItem]:
        mode = self.filter_var.get()
        query = self.search_var.get().strip().lower()
        rows: list[LiveItem] = []
        for it in snap.items.values():
            if mode == "bidding" and not it.has_bids and it.buy_now_price <= 0:
                continue
            if mode == "open" and (not it.is_open or it.has_bids):
                continue
            if mode == "sold" and not it.is_sold:
                continue
            if mode == "under" and not _bidding_under(it):
                continue
            if query:
                hay = (
                    f"{it.sku} {it.title} {it.high_bidder} {it.account_status} "
                    f"{it.category} {format_acct_label(it)}"
                ).lower()
                if query not in hay:
                    continue
            rows.append(it)

        if self._sort_col:
            rows.sort(key=self._sort_key, reverse=not self._sort_asc)
        elif mode == "under":
            rows.sort(
                key=lambda it: (it.reserve_price - it.bid_price, -it.bid_price, it.sku or ""),
                reverse=True,
            )
        else:
            rows.sort(
                key=lambda it: (
                    0 if it.is_open else 1,
                    -it.bid_price,
                    -it.bid_count,
                    it.title.lower(),
                )
            )
        return rows

    def _row_tags(self, item: LiveItem, row_index: int) -> tuple[str, ...]:
        tag = "odd" if row_index % 2 else "even"
        tags = [tag]
        okayed = self._is_okayed(item)
        if okayed:
            tags.append("okayed")
            if item.is_sold:
                tags.append("sold")
            return tuple(tags)
        if _bidding_under(item):
            tags.append("under")
        if item.is_sold:
            tags.append("sold")
        elif item.suspended:
            tags.append("suspended")  # red — high risk only
        return tuple(tags)

    def _row_values(self, item: LiveItem) -> tuple[str, ...]:
        bidding = item.has_bids
        price = _money(item.bid_price) if bidding else "—"
        buy_now = _money(item.buy_now_price) if item.buy_now_price > 0 else "—"
        left = format_time_left(item)
        bids = str(item.bid_count) if bidding else "—"
        bidder = item.high_bidder if item.high_bidder else ("—" if bidding else "")
        status = format_acct_label(item, okayed=self._is_okayed(item)) or "—"
        return (
            item.sku or "—",
            item.title,
            price,
            buy_now,
            format_reserve(item),
            format_met(item),
            left,
            bids,
            bidder or "—",
            status,
            format_unpaid_cell(item),
            format_void_cell(item),
            format_paid_cell(item),
            item.category or "",
            "View",
            "",
        )

    def _tick_time_left(self) -> None:
        """Refresh countdown cells every second without rebuilding the whole table."""
        if not self._pause_ticks:
            try:
                for i, iid in enumerate(self.bids_tree.get_children()):
                    item = self._bid_items.get(iid)
                    if item is None:
                        continue
                    values = list(self.bids_tree.item(iid, "values"))
                    cols = tuple(self.bids_tree["columns"])
                    if "left" not in cols or len(values) <= cols.index("left"):
                        continue
                    left_i = cols.index("left")
                    left = format_time_left(item)
                    if values[left_i] != left:
                        values[left_i] = left
                        self.bids_tree.item(iid, values=values, tags=self._row_tags(item, i))
            except Exception:
                pass
        try:
            self.root.after(1000, self._tick_time_left)
        except Exception:
            pass

    def _render_bids(self) -> None:
        try:
            self._render_risk()
        except Exception:
            pass
        try:
            self._render_sale_risk()
        except Exception:
            pass
        if getattr(self, "_bids_view", "lots") == "risk":
            return
        snap = self._latest_snap
        if snap is None:
            return

        rows = self._filtered_rows(snap)
        fingerprint = "|".join(
            f"{it.item_id}:{it.bid_price}:{it.bid_count}:{it.high_bidder}:"
            f"{it.account_status}:{it.item_status}:{it.close_time}:{it.reserve_price}:"
            f"{it.buy_now_price}:{int(it.bought_now)}"
            for it in rows
        )
        fingerprint = (
            f"{self.filter_var.get()}::{self.search_var.get().strip().lower()}::"
            f"{self._sort_col}:{self._sort_asc}::{fingerprint}"
        )
        if fingerprint == self._bids_fingerprint:
            return
        self._bids_fingerprint = fingerprint

        new_ids = [str(it.item_id) for it in rows]
        old_ids = list(self.bids_tree.get_children())

        # Fast path: same items in same order — update cell values only (no flicker)
        if new_ids == old_ids:
            for i, it in enumerate(rows):
                iid = str(it.item_id)
                self.bids_tree.item(iid, values=self._row_values(it), tags=self._row_tags(it, i))
                self._bid_items[iid] = it
            self._schedule_unmet_paint()
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
            self._schedule_unmet_paint()
            return

        for i, it in enumerate(rows):
            iid = str(it.item_id)
            self.bids_tree.insert(
                "", "end", iid=iid, values=self._row_values(it), tags=self._row_tags(it, i)
            )
            self._bid_items[iid] = it
            if it.item_id in selected_ids:
                self.bids_tree.selection_add(iid)

        try:
            self.bids_tree.yview_moveto(y_pos)
        except Exception:
            pass
        self._schedule_unmet_paint()

    def _set_metrics(self, snap: Snapshot) -> None:
        missed = sum(1 for it in snap.items.values() if _bidding_under(it))
        self.metric_hammer.set(_money(snap.hammer_total))
        self.metric_bidding.set(str(snap.bidding_count))
        self.metric_open.set(str(snap.open_count), warn=snap.open_count > 0)
        self.metric_below.set(str(missed), warn=missed > 0)
        self.metric_bidders.set(str(snap.unique_bidders))
        self.metric_items.set(f"{len(snap.items)} ({snap.sold_count} sold)")
        self.updated_var.set(
            f"Updated {datetime.now().strftime('%I:%M:%S %p').lstrip('0')}  ·  "
            f"{snap.sold_count} sold kept at final price  ·  click View or double-click"
        )

    # ----- Auth / actions -----
    def _refresh_login_badge(self) -> None:
        ee = "Estate-Ez ✓" if self.estate_ez_token else "Estate-Ez —"
        buyer = "Buyer ✓" if self.token else "Buyer —"
        seller = "Seller ✓" if self.seller_token else "Seller —"
        self.login_var.set(f"{ee}  ·  {buyer}  ·  {seller}")
        ok = bool(self.token and self.seller_token)
        partial = bool(self.token or self.seller_token or self.estate_ez_token)
        if ok:
            self.login_badge.configure(text_color=GREEN, fg_color=GREEN_SOFT)
        elif partial:
            self.login_badge.configure(text_color=AMBER, fg_color=AMBER_SOFT)
        else:
            self.login_badge.configure(text_color=AMBER, fg_color=AMBER_SOFT)

    def _try_load_token_quiet(self) -> None:
        def work() -> None:
            try:
                # HTTP / saved tokens only — do not launch a hidden Chromium here.
                # A headless profile lock is what made Buyer/Seller sign-in never open.
                token = resolve_access_token(validate=True, allow_browser=False)
            except Exception:
                token = None
            self.root.after(0, self._apply_token, token, False)

        threading.Thread(target=work, daemon=True).start()

    def _try_load_seller_token_quiet(self) -> None:
        def work() -> None:
            try:
                token = resolve_seller_access_token(validate=True, allow_browser=False)
            except Exception:
                token = None
            self.root.after(0, self._apply_seller_token, token, False)

        threading.Thread(target=work, daemon=True).start()

    def _keep_sessions_alive(self) -> None:
        """Refresh buyer/seller JWTs before the 30-minute access token dies."""
        def work() -> None:
            try:
                buyer, seller = keep_sessions_fresh()
            except Exception:
                buyer = seller = None
            self.root.after(0, self._apply_kept_sessions, buyer, seller)

        threading.Thread(target=work, daemon=True).start()
        try:
            self.root.after(45000, self._keep_sessions_alive)
        except Exception:
            pass

    def _apply_kept_sessions(self, buyer: str | None, seller: str | None) -> None:
        if buyer:
            self._apply_token(buyer, False)
        if seller:
            self._apply_seller_token(seller, False)

    def _apply_token(self, token: str | None, announce: bool) -> None:
        if token is None and self.token and not announce:
            return
        changed = token != self.token
        self.token = token
        if changed:
            for watcher in self._iter_watchers():
                watcher.set_token(token)
        self._refresh_login_badge()
        if token:
            if announce:
                self.status_var.set("Buyer signed in. Usernames will fill in on the next refresh.")
                self._append_activity("Buyer signed in — fetching usernames on next pass.", kind="info")
        elif announce:
            self.status_var.set("Buyer not signed in — prices still work; Buyer sign in for usernames.")

    def _apply_seller_token(self, token: str | None, announce: bool) -> None:
        if token is None and self.seller_token and not announce:
            return
        changed = token != self.seller_token
        self.seller_token = token
        if changed:
            for watcher in self._iter_watchers():
                watcher.set_seller_token(token)
        self._refresh_login_badge()
        if token:
            if announce or not self._seller_sales:
                self._refresh_seller_sales(announce=announce)
            if announce:
                self.status_var.set("Seller signed in. Loading live sales from CT Seller…")
                self._append_activity(
                    "Seller signed in — unpaid invoices will show on Suspended rows.",
                    kind="info",
                )
        else:
            self._seller_sales = []
            self._seller_locations = []
            if announce:
                self.status_var.set("Seller not signed in — Suspended won't show unpaid status.")

    def _on_auth_lost(self, message: str) -> None:
        self.token = None
        self._refresh_login_badge()
        self.status_var.set(message)
        self._append_activity(message, kind="info")

    def _on_seller_auth_lost(self, message: str) -> None:
        self.seller_token = None
        self._seller_sales = []
        self._seller_locations = []
        self._refresh_login_badge()
        self.status_var.set(message)
        self._append_activity(message, kind="info")

    def _on_login(self) -> None:
        self._browser_sign_in("buyer")

    def _on_seller_login(self) -> None:
        self._browser_sign_in("seller")

    def _on_estate_ez_login(self) -> None:
        self._browser_sign_in("estateez")

    def _open_estate_ez_dialog(self) -> None:
        existing = self._sign_in_windows.get("estateez")
        if existing is not None:
            try:
                existing.lift()
                existing.focus_force()
                return
            except Exception:
                self._sign_in_windows.pop("estateez", None)

        win = ctk.CTkToplevel(self.root)
        self._sign_in_windows["estateez"] = win
        win.title("Estate-Ez sign in")
        win.geometry("440x340")
        win.minsize(420, 320)
        win.transient(self.root)
        try:
            win.configure(fg_color=THEMES[self.theme_mode]["window"])
        except Exception:
            pass
        win.after(200, win.grab_set)

        def _on_close() -> None:
            self._sign_in_windows.pop("estateez", None)
            try:
                win.grab_release()
            except Exception:
                pass
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", _on_close)

        email_var = StringVar(value=load_saved_username("estateez"))
        password_var = StringVar(value=load_saved_password("estateez"))
        remember_var = BooleanVar(value=True)
        status_local = StringVar(value="")
        pad = 18
        ctk.CTkLabel(
            win, text="Estate-Ez sign in", font=("Segoe UI Semibold", 20), text_color=GREEN_DARK,
        ).pack(anchor="w", padx=pad, pady=(pad, 4))
        ctk.CTkLabel(
            win,
            text="Same email and password as the Estate-Ez website. If CT Seller is connected on Account, Watcher loads that session — no second Seller login.",
            font=("Segoe UI", 13), text_color=MUTED, wraplength=390, justify="left",
        ).pack(anchor="w", padx=pad, pady=(0, 10))

        card = ctk.CTkFrame(win, corner_radius=12, fg_color=WHITE)
        card.pack(fill="x", padx=pad)
        ctk.CTkLabel(card, text="Email", font=("Segoe UI Semibold", 13), text_color=GREEN_DARK).pack(
            anchor="w", padx=14, pady=(12, 4)
        )
        email_entry = ctk.CTkEntry(card, textvariable=email_var, height=36)
        email_entry.pack(fill="x", padx=14)
        ctk.CTkLabel(card, text="Password", font=("Segoe UI Semibold", 13), text_color=GREEN_DARK).pack(
            anchor="w", padx=14, pady=(10, 4)
        )
        password_entry = ctk.CTkEntry(card, textvariable=password_var, height=36, show="•")
        password_entry.pack(fill="x", padx=14)
        ctk.CTkCheckBox(
            card,
            text="Remember on this PC",
            variable=remember_var,
            fg_color=GREEN,
            hover_color=GREEN_HOVER,
            text_color=GREEN_DARK,
            font=("Segoe UI", 12),
        ).pack(anchor="w", padx=14, pady=(10, 12))

        ctk.CTkLabel(win, textvariable=status_local, font=("Segoe UI", 12), text_color=MUTED).pack(
            anchor="w", padx=pad, pady=(8, 0)
        )
        btns = ctk.CTkFrame(win, fg_color="transparent")
        btns.pack(fill="x", padx=pad, pady=(8, pad))
        sign_btn = ctk.CTkButton(
            btns,
            text="Sign in",
            width=120,
            height=36,
            fg_color=GREEN,
            hover_color=GREEN_HOVER,
            text_color=ON_ACCENT,
            font=("Segoe UI Semibold", 13),
        )
        sign_btn.pack(side="left")

        def _fail(msg: str) -> None:
            sign_btn.configure(state="normal")
            status_local.set(msg)

        def _ok(ee_token: str, seller_access: str | None, seller_note: str) -> None:
            self.estate_ez_token = ee_token
            self._refresh_login_badge()
            if seller_access:
                self._apply_seller_token(seller_access, True)
            self.status_var.set(seller_note)
            self._append_activity(seller_note, kind="info")
            _on_close()

        def submit(_event=None) -> None:
            email = email_var.get().strip()
            password = password_var.get()
            if not email or not password:
                status_local.set("Enter email and password.")
                return
            sign_btn.configure(state="disabled")
            status_local.set("Signing in…")

            def work() -> None:
                try:
                    result = sign_in_estate_ez(email, password, remember=remember_var.get())
                except EstateEzSellerNotConnected as exc:
                    ee_token = load_estate_ez_token()

                    def _partial() -> None:
                        self.estate_ez_token = ee_token
                        self._refresh_login_badge()
                        note = str(exc) or "Connect CT Seller on Estate-Ez → Account, then sign in again."
                        self.status_var.set(note)
                        self._append_activity(note, kind="info")
                        _on_close()

                    self.root.after(0, _partial)
                    return
                except Exception as exc:
                    msg = str(exc) or type(exc).__name__
                    self.root.after(0, lambda m=msg: _fail(m))
                    return
                seller = result.get("seller") or {}
                access = str(seller.get("access_token") or "") or None
                note = (
                    "Estate-Ez signed in. CT Seller session loaded from your account."
                    if access
                    else "Estate-Ez signed in."
                )
                self.root.after(0, lambda t=result["estate_ez_token"], a=access, n=note: _ok(t, a, n))

            threading.Thread(target=work, daemon=True).start()

        sign_btn.configure(command=submit)
        win.bind("<Return>", submit)
        if email_var.get():
            password_entry.focus_set()
        else:
            email_entry.focus_set()

    def _open_sign_in_dialog(self, kind: str) -> None:
        existing = self._sign_in_windows.get(kind)
        if existing is not None:
            try:
                existing.lift()
                existing.focus_force()
                return
            except Exception:
                self._sign_in_windows.pop(kind, None)

        is_seller = kind == "seller"
        title = "Seller sign in" if is_seller else "Buyer sign in"
        hint = (
            "CT Seller email and password — unpaid / paid counts."
            if is_seller
            else "CTBids buyer email and password — usernames and Suspended."
        )
        win = ctk.CTkToplevel(self.root)
        self._sign_in_windows[kind] = win
        win.title(title)
        win.geometry("420x360")
        win.minsize(400, 340)
        win.transient(self.root)
        try:
            win.configure(fg_color=THEMES[self.theme_mode]["window"])
        except Exception:
            pass
        win.after(200, win.grab_set)

        def _on_close() -> None:
            self._sign_in_windows.pop(kind, None)
            try:
                win.grab_release()
            except Exception:
                pass
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", _on_close)

        email_var = StringVar(value=load_saved_username(kind))
        password_var = StringVar(value=load_saved_password(kind))
        remember_var = BooleanVar(value=True)
        trust_var = BooleanVar(value=True)
        otp_var = StringVar(value="")
        status_local = StringVar(value="")
        mfa_hint = StringVar(value="")
        mfa_state = {"token": "", "channel": ""}

        pad = 18
        ctk.CTkLabel(
            win, text=title, font=("Segoe UI Semibold", 20), text_color=GREEN_DARK,
        ).pack(anchor="w", padx=pad, pady=(pad, 4))
        hint_lbl = ctk.CTkLabel(
            win, text=hint, font=("Segoe UI", 13), text_color=MUTED, wraplength=370, justify="left",
        )
        hint_lbl.pack(anchor="w", padx=pad, pady=(0, 10))

        card = ctk.CTkFrame(win, corner_radius=12, fg_color=WHITE)
        card.pack(fill="x", padx=pad)

        ctk.CTkLabel(card, text="Email", font=("Segoe UI Semibold", 13), text_color=GREEN_DARK).pack(
            anchor="w", padx=14, pady=(12, 4)
        )
        email_entry = ctk.CTkEntry(card, textvariable=email_var, height=36)
        email_entry.pack(fill="x", padx=14)
        pw_label = ctk.CTkLabel(card, text="Password", font=("Segoe UI Semibold", 13), text_color=GREEN_DARK)
        pw_label.pack(anchor="w", padx=14, pady=(10, 4))
        password_entry = ctk.CTkEntry(card, textvariable=password_var, height=36, show="•")
        password_entry.pack(fill="x", padx=14)
        ctk.CTkCheckBox(
            card,
            text="Remember on this PC",
            variable=remember_var,
            fg_color=GREEN,
            hover_color=GREEN_HOVER,
            text_color=GREEN_DARK,
            font=("Segoe UI", 12),
        ).pack(anchor="w", padx=14, pady=(10, 12))

        mfa_frame = ctk.CTkFrame(win, corner_radius=12, fg_color=WHITE)
        ctk.CTkLabel(
            mfa_frame, textvariable=mfa_hint, font=("Segoe UI", 13),
            text_color=GREEN_DARK, wraplength=360, justify="left",
        ).pack(anchor="w", padx=14, pady=(12, 8))
        mfa_choice = ctk.CTkFrame(mfa_frame, fg_color="transparent")
        mfa_choice.pack(fill="x", padx=14)
        email_code_btn = ctk.CTkButton(mfa_choice, text="Email me a code", width=150, height=32)
        email_code_btn.pack(side="left")
        sms_code_btn = ctk.CTkButton(mfa_choice, text="Text me a code", width=150, height=32)
        sms_code_btn.pack(side="left", padx=(8, 0))
        ctk.CTkLabel(
            mfa_frame, text="Paste the 6-digit code here", font=("Segoe UI Semibold", 13), text_color=GREEN_DARK,
        ).pack(anchor="w", padx=14, pady=(10, 4))
        otp_entry = ctk.CTkEntry(mfa_frame, textvariable=otp_var, height=40, placeholder_text="______")
        otp_entry.pack(fill="x", padx=14)
        ctk.CTkCheckBox(
            mfa_frame,
            text="Trust this PC (skip the code next time if CTBids allows it)",
            variable=trust_var,
            fg_color=GREEN,
            hover_color=GREEN_HOVER,
            text_color=GREEN_DARK,
            font=("Segoe UI", 12),
        ).pack(anchor="w", padx=14, pady=(8, 4))
        mfa_actions = ctk.CTkFrame(mfa_frame, fg_color="transparent")
        mfa_actions.pack(fill="x", padx=14, pady=(4, 12))
        verify_btn = ctk.CTkButton(mfa_actions, text="Verify code", width=120, height=32)
        verify_btn.pack(side="left")
        resend_btn = ctk.CTkButton(
            mfa_actions, text="Resend", width=80, height=32,
            fg_color="transparent", text_color=GREEN, border_color=GREEN, border_width=1,
            hover_color=GREEN_SOFT,
        )
        resend_btn.pack(side="left", padx=(8, 0))

        ctk.CTkLabel(win, textvariable=status_local, font=("Segoe UI", 12), text_color=MUTED).pack(
            anchor="w", padx=pad, pady=(8, 0)
        )

        btns = ctk.CTkFrame(win, fg_color="transparent")
        btns.pack(fill="x", padx=pad, pady=(8, pad))
        sign_btn = ctk.CTkButton(
            btns,
            text="Sign in",
            width=120,
            height=36,
            fg_color=GREEN,
            hover_color=GREEN_HOVER,
            text_color=ON_ACCENT,
            font=("Segoe UI Semibold", 13),
        )
        sign_btn.pack(side="left")
        ctk.CTkButton(
            btns,
            text="Use browser instead",
            width=150,
            height=36,
            fg_color="transparent",
            text_color=GREEN,
            border_color=GREEN,
            border_width=1,
            hover_color=GREEN_SOFT,
            command=lambda: self._browser_sign_in(kind, _on_close),
        ).pack(side="left", padx=(8, 0))

        def _save_creds() -> None:
            email = email_var.get().strip()
            password = str(mfa_state.get("password") or password_var.get() or "")
            if remember_var.get():
                save_login_credentials(kind, email, password)
            else:
                save_login_credentials(kind, email, None)
                clear_saved_password(kind)

        def _fail(msg: str) -> None:
            sign_btn.configure(state="normal")
            email_code_btn.configure(state="normal")
            sms_code_btn.configure(state="normal")
            verify_btn.configure(state="normal")
            resend_btn.configure(state="normal")
            status_local.set(msg)

        def _ok(token: str) -> None:
            if is_seller:
                self._apply_seller_token(token, True)
            else:
                self._apply_token(token, True)
            _on_close()

        def _show_mfa(challenge: BuyerMfaRequired) -> None:
            # Keep THIS window. Turn the password box into the code box so it
            # cannot hide below the "check your email" status line.
            mfa_state["token"] = challenge.mfatoken
            mfa_state["channel"] = "email"
            mfa_state["password"] = password_var.get()
            otp_var.set("")
            password_var.set("")
            try:
                pw_label.configure(text="Paste the 6-digit code from your email")
                password_entry.configure(show="")
            except Exception:
                pass
            try:
                hint_lbl.configure(
                    text="Password worked. Check your email for the code, then type it in the box below."
                )
            except Exception:
                pass
            sign_btn.configure(state="normal", text="Verify code")
            status_local.set("Sending a code to your email…")
            _send_mfa("email")
            try:
                password_entry.focus_set()
            except Exception:
                pass

        def _send_mfa(channel: str, *, resend: bool = False) -> None:
            token = mfa_state.get("token") or ""
            if not token:
                status_local.set("Sign in with email and password first.")
                return
            mfa_state["channel"] = channel
            email_code_btn.configure(state="disabled")
            sms_code_btn.configure(state="disabled")
            resend_btn.configure(state="disabled")
            status_local.set("Sending code…")

            def work() -> None:
                try:
                    send_buyer_mfa_otp(token, channel, resend=resend)
                except Exception as exc:
                    msg = str(exc) or type(exc).__name__
                    self.root.after(0, lambda m=msg: _fail(m))
                    return
                where = "email" if channel == "email" else "phone"

                def _sent() -> None:
                    _fail("")
                    status_local.set(f"Check your {where} — type the 6-digit code in the Password box, then Verify code.")
                    password_entry.focus_set()

                self.root.after(0, _sent)

            threading.Thread(target=work, daemon=True).start()

        def _verify_mfa(_event=None) -> None:
            token = mfa_state.get("token") or ""
            if not token:
                return
            code = otp_var.get().strip() or password_var.get().strip()
            if not code:
                status_local.set("Type the 6-digit code in the box under your email.")
                return
            verify_btn.configure(state="disabled")
            status_local.set("Checking code…")

            def work() -> None:
                try:
                    access = verify_buyer_mfa(token, code, trusted=trust_var.get())
                    _save_creds()
                except Exception as exc:
                    msg = str(exc) or type(exc).__name__
                    self.root.after(0, lambda m=msg: _fail(m))
                    return
                self.root.after(0, lambda t=access: _ok(t))

            threading.Thread(target=work, daemon=True).start()

        def submit(_event=None) -> None:
            if mfa_state.get("token"):
                _verify_mfa()
                return
            email = email_var.get().strip()
            password = password_var.get()
            if not email or not password:
                status_local.set("Enter email and password.")
                return
            sign_btn.configure(state="disabled")
            status_local.set("Signing in…")

            def work() -> None:
                try:
                    access = login_with_password(kind, email, password)
                    _save_creds()
                except BuyerMfaRequired as challenge:
                    self.root.after(0, lambda c=challenge: _show_mfa(c))
                    return
                except Exception as exc:
                    msg = str(exc) or type(exc).__name__
                    self.root.after(0, lambda m=msg: _fail(m))
                    return
                self.root.after(0, lambda t=access: _ok(t))

            threading.Thread(target=work, daemon=True).start()

        email_code_btn.configure(command=lambda: _send_mfa("email"))
        sms_code_btn.configure(
            command=lambda: _send_mfa("sms"),
            fg_color="transparent", text_color=GREEN, border_color=GREEN, border_width=1,
            hover_color=GREEN_SOFT,
        )
        verify_btn.configure(command=_verify_mfa)
        resend_btn.configure(
            command=lambda: _send_mfa(mfa_state.get("channel") or "email", resend=True)
        )
        sign_btn.configure(command=submit)
        win.bind("<Return>", submit)
        if email_var.get():
            password_entry.focus_set()
        else:
            email_entry.focus_set()

    def _browser_sign_in(self, kind: str, close_dialog=None) -> None:
        """Open the real sign-in page. The one-time code is entered there, not in this app."""
        if close_dialog:
            close_dialog()
        if kind in self._browser_signin_busy:
            self.status_var.set("That sign-in window is already open.")
            return
        self._browser_signin_busy.add(kind)
        if kind == "seller":
            self.status_var.set(
                "Opening CT Seller in the browser. Sign in on that page, including the one-time code, then close the window."
            )
        elif kind == "estateez":
            self.status_var.set(
                "Opening Estate-Ez in the browser. Sign in on that page, then close the window."
            )
        else:
            self.status_var.set(
                "Opening CTBids in the browser. Sign in on that page, including the one-time code, then close the window."
            )
        self.root.update_idletasks()
        sale = self.sale_var.get().strip() or "https://ctbids.com/"

        def work() -> None:
            try:
                if kind == "seller":
                    def captured(token: str) -> None:
                        self.root.after(0, self._apply_seller_token, token, False)

                    token = open_seller_login_window(on_tokens=captured)
                    self.root.after(0, self._finish_browser_sign_in, "seller", token)
                elif kind == "estateez":
                    try:
                        from auth import open_estate_ez_login_window
                    except ImportError:  # noqa: BLE001 - package vs script import
                        from sale_watcher.auth import open_estate_ez_login_window

                    def captured_ee(token: str) -> None:
                        self.root.after(0, self._finish_browser_sign_in, "estateez", token)

                    token = open_estate_ez_login_window(on_token=captured_ee)
                    self.root.after(0, self._finish_browser_sign_in, "estateez", token)
                else:
                    def captured(token: str) -> None:
                        self.root.after(0, self._apply_token, token, False)

                    token = open_login_window(sale, on_tokens=captured)
                    self.root.after(0, self._finish_browser_sign_in, "buyer", token)
            except Exception as exc:
                msg = str(exc) or type(exc).__name__
                self.root.after(0, lambda m=msg: messagebox.showerror(APP_TITLE, m))
            finally:
                self.root.after(0, self._browser_signin_busy.discard, kind)

        threading.Thread(target=work, daemon=True).start()

    def _finish_browser_sign_in(self, kind: str, token: str | None) -> None:
        if not token:
            if kind == "seller":
                self.status_var.set(
                    "CT Seller window closed before sign-in finished. Sign in on the page, then close the window."
                )
            elif kind == "estateez":
                self.status_var.set(
                    "Estate-Ez window closed before sign-in finished. Sign in on the page, then close the window."
                )
            else:
                self.status_var.set(
                    "CTBids window closed before sign-in finished. Sign in on the page, then close the window."
                )
            return
        if kind == "seller":
            self._apply_seller_token(token, True)
            return
        if kind == "buyer":
            self._apply_token(token, True)
            return
        self._finish_estate_ez_sign_in(token)

    def _finish_estate_ez_sign_in(self, token: str) -> None:
        self.estate_ez_token = token
        self._refresh_login_badge()
        try:
            from estate_ez import fetch_ctseller_session
        except ImportError:  # noqa: BLE001 - package vs script import
            from sale_watcher.estate_ez import fetch_ctseller_session
        try:
            session = fetch_ctseller_session(token)
        except EstateEzSellerNotConnected as exc:
            note = str(exc) or "Estate-Ez signed in. Connect CT Seller on Account, then sign in again."
            self.status_var.set(note)
            self._append_activity(note, kind="info")
            return
        except EstateEzAuthError as exc:
            self.estate_ez_token = load_estate_ez_token()
            self._refresh_login_badge()
            note = str(exc) or "Estate-Ez did not keep that sign-in. Close the window after the page finishes signing in."
            self.status_var.set(note)
            self._append_activity(note, kind="info")
            return
        except Exception as exc:
            note = f"Estate-Ez signed in. Seller session was not loaded ({exc})."
            self.status_var.set(note)
            self._append_activity("Estate-Ez signed in.", kind="info")
            return
        access = str(session.get("access_token") or "") or None
        if access:
            self._apply_seller_token(access, False)
        self.status_var.set("Estate-Ez signed in. CT Seller session loaded from your account.")
        self._append_activity("Estate-Ez signed in. CT Seller session loaded from your account.", kind="info")

    def _open_sale(self) -> None:
        url = self.sale_var.get().strip()
        sess = self._current_session()
        if sess and sess.sale_url:
            url = sess.sale_url
        if not url:
            messagebox.showwarning(APP_TITLE, "Pick a sale from CT Seller, or paste the sale URL first.")
            return
        try:
            sale_id = parse_sale_id(url)
            webbrowser.open(buyer_sale_url(sale_id))
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _on_start(self) -> None:
        try:
            typed = (self.sale_combo.get() or "").strip()
        except Exception:
            typed = ""
        sale = typed or self.sale_var.get().strip()
        sess = self._current_session()
        if not sale and sess:
            sale = sess.sale_url
        if not sale:
            live = self._live_seller_sales()
            if len(live) == 1:
                sale = live[0].buyer_url
            elif live:
                messagebox.showwarning(APP_TITLE, "Pick a live sale tab, then click Start.\n\nOr click Watch all.")
                return
            else:
                messagebox.showwarning(
                    APP_TITLE,
                    "No sale selected.\n\nSign in to CT Seller to load live sales, or paste a CTBids sale URL.",
                )
                return
        try:
            matched = self._match_seller_sale(sale)
            sale_id = matched.sale_id if matched else parse_sale_id(sale)
            poll = float(self.poll_var.get().strip() or "45")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        url = matched.buyer_url if matched else buyer_sale_url(sale_id)
        self._start_sale(sale_id, url, poll)

    def _confirm_logins(self) -> bool:
        self.status_var.set("Checking CTBids logins...")
        self.root.update_idletasks()
        try:
            fresh = resolve_access_token(validate=True, allow_browser=False)
            if fresh:
                self.token = fresh
        except Exception:
            pass
        try:
            seller_fresh = resolve_seller_access_token(validate=True, allow_browser=False)
            if seller_fresh:
                self.seller_token = seller_fresh
        except Exception:
            pass
        self._apply_token(self.token, False)
        self._apply_seller_token(self.seller_token, False)
        if not self.token:
            if not messagebox.askyesno(
                APP_TITLE,
                "No CTBids buyer login found.\n\n"
                "You can still watch prices, but usernames will be blank.\n\n"
                "Start anyway? (Or cancel and click Buyer sign in first.)",
            ):
                self.status_var.set("Start cancelled — sign in first for usernames.")
                return False
        return True

    def _start_sale(self, sale_id: int, sale_url: str, poll: float, *, confirm: bool = True) -> bool:
        if confirm and not self._confirm_logins():
            return False
        sale = self._seller_sale_for(sale_id)
        title = sale.title if sale else f"Sale {sale_id}"
        url = sale.buyer_url if sale else (sale_url.strip() or buyer_sale_url(sale_id))
        sess = self._select_session(sale_id, title=title, url=url)

        self.cfg["sale_url"] = url
        self.cfg["poll_seconds"] = poll
        self.cfg["theme"] = self.theme_mode
        self.cfg["auto_save_on_end"] = bool(self.auto_save_var.get())
        save_watcher_config(self.cfg)

        if self.token and not self.seller_token:
            self._append_activity(
                "Tip: Seller sign in unlocks unpaid invoice checks on high bidders.",
                kind="info",
                session=sess,
            )

        if sess.watcher:
            sess.watcher.stop()
            sess.watcher = None

        watcher = SaleWatcher(
            url,
            access_token=self.token,
            seller_token=self.seller_token,
            poll_seconds=poll,
            on_update=lambda snap, changes, sid=sale_id: self.root.after(
                0, self._on_session_update, sid, snap, changes
            ),
            on_status=lambda msg, sid=sale_id: self.root.after(0, self._on_session_status, sid, msg),
            on_auth_lost=lambda msg: self.root.after(0, self._on_auth_lost, msg),
            on_seller_auth_lost=lambda msg: self.root.after(0, self._on_seller_auth_lost, msg),
            on_end_snapshot=lambda snap, paths: self.root.after(0, self._on_end_snapshot, snap, paths),
            auto_save_on_end=bool(self.auto_save_var.get()),
        )
        sess.watcher = watcher
        self.watcher = watcher
        watcher.start()
        self._sync_start_stop()
        self._sync_sale_tabs(select=sess.tab_name)
        bits = []
        if self.token:
            bits.append("usernames")
        if self.seller_token:
            bits.append("unpaid checks")
        self._append_activity(
            f"Watcher started for {sess.title}" + (f" — {', '.join(bits)}" if bits else " — prices only"),
            kind="info",
            session=sess,
        )
        if sess is self._current_session():
            self.status_var.set(f"Watching {sess.title}… first refresh starting.")
        return True

    def _on_watch_all(self) -> None:
        live = self._live_seller_sales()
        if not live:
            messagebox.showinfo(
                APP_TITLE,
                "No live sales on this CT Seller login yet.\n\nSign in to Seller, or paste a sale URL and click Start.",
            )
            return
        try:
            poll = float(self.poll_var.get().strip() or "45")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        if not self._confirm_logins():
            return
        started = 0
        for sale in live:
            sess = self._sessions.get(sale.sale_id)
            if sess and sess.watcher and sess.watcher.running:
                continue
            if self._start_sale(sale.sale_id, sale.buyer_url, poll, confirm=False):
                started += 1
        if started:
            first = live[0]
            self._select_session(first.sale_id, title=first.title, url=first.buyer_url)
            self.status_var.set(f"Watching {started} live sale{'s' if started != 1 else ''}…")
            self._append_activity(f"Watch all — started {started} sale(s).", kind="info")
        else:
            self.status_var.set("Those live sales are already watching.")

    def _on_stop(self) -> None:
        sess = self._current_session()
        watcher = sess.watcher if sess else self.watcher
        if watcher:
            watcher.stop()
        if sess:
            sess.watcher = None
        if self.watcher is watcher:
            self.watcher = None
        self._sync_start_stop()
        title = sess.title if sess else "sale"
        self.status_var.set(f"Stopped {title}.")
        self._append_activity(f"Watcher stopped for {title}.", kind="info", session=sess)

    def _on_session_status(self, sale_id: int, message: str) -> None:
        if sale_id == self._active_sale_id:
            self.status_var.set(message)

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
        sess = self._current_session()
        sale_url = (sess.sale_url if sess else "") or self.sale_var.get().strip()

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

    def _on_end_snapshot(self, snap: Snapshot, paths: list) -> None:
        sess = self._sessions.get(snap.sale_id)
        if sess is not None and self._active_sale_id != snap.sale_id:
            self._select_session(snap.sale_id, title=sess.title, url=sess.sale_url)
        folder = Path(paths[0]).parent if paths else None
        csv_name = next((Path(p).name for p in paths if str(p).lower().endswith(".csv")), "")
        missed = sum(
            1
            for it in snap.items.values()
            if it.reserve_price > 0 and it.bid_price < it.reserve_price
        )
        self._append_activity(
            f"Sale ended — saved {csv_name or 'closing snapshot'}"
            + (f" · {missed} lot(s) below reserve" if missed else ""),
            kind="info",
        )
        loc = str(folder) if folder else ""
        title = sess.title if sess else f"Sale {snap.sale_id}"
        self.status_var.set(f"{title} ended — snapshot saved.")
        extra = f"\n\n{missed} lot(s) had a bid under reserve." if missed else ""
        messagebox.showinfo(
            APP_TITLE,
            f"{title}: the last lot closed.\n\n"
            "Saved a closing snapshot: CSV, HTML, and PDF (if Edge/Chrome is installed).\n\n"
            f"{loc}{extra}\n\n"
            "The PDF is grouped by SKU prefix (sold / not sold, high bid vs reserve). The CSV is best for Excel.",
        )
        if folder:
            try:
                os.startfile(folder)  # type: ignore[attr-defined]
            except Exception:
                pass

    def _on_session_update(self, sale_id: int, snap: Snapshot, changes: list[BidChange]) -> None:
        sess = self._sessions.get(sale_id)
        if sess is None:
            sess = self._ensure_session(sale_id, buyer_sale_url(sale_id), f"Sale {sale_id}")
        sess.snap = snap
        if sale_id == self._active_sale_id:
            self._on_update(snap, changes, session=sess)
            return
        self._record_bid_changes(sess, snap, changes, draw=False)
        try:
            self._render_risk()
        except Exception:
            pass

    def _on_update(self, snap: Snapshot, changes: list[BidChange], session: _WatchSession | None = None) -> None:
        sess = session or self._current_session()
        self._latest_snap = snap
        if sess is not None:
            sess.snap = snap
        # Bidding tab goes empty when everything closes — flip to Sold so totals stay visible
        if (
            snap.open_count == 0
            and snap.bidding_count == 0
            and snap.sold_count > 0
            and self.filter_var.get() == "bidding"
        ):
            self.filter_var.set("sold")
            if sess is not None:
                sess.filter_value = "sold"
            try:
                self.filter_menu.set("Sold")
            except Exception:
                pass
            self._bids_fingerprint = ""
            self._append_activity(
                "Sale finished — showing Sold lots with final hammer totals.",
                kind="info",
                session=sess,
            )
        self._set_metrics(snap)
        self._render_bids()
        self._record_bid_changes(sess, snap, changes, draw=True)

    def _record_bid_changes(
        self,
        sess: _WatchSession | None,
        _snap: Snapshot,
        changes: list[BidChange],
        *,
        draw: bool,
    ) -> None:
        sale_bit = f"{sess.title[:28]} · " if sess and not draw else ""
        focus_ids: list[int] = []
        for ch in changes:
            sku = f"{ch.item.sku} · " if ch.item.sku else ""
            title = f"{sku}{ch.item.title[:70]}"
            if ch.kind == "first_bid":
                msg = f"{title}\n{_money(ch.item.bid_price)}"
                if ch.item.high_bidder:
                    msg += f" · {ch.item.high_bidder}"
                if ch.item.account_status and ch.item.account_status != "Sold":
                    msg += f" · {ch.item.account_status}"
                focus_ids.append(ch.item.item_id)
            elif ch.kind == "price_up":
                msg = f"{title}\n{_money(ch.old_price)} → {_money(ch.item.bid_price)}"
                if ch.item.high_bidder:
                    msg += f" · {ch.item.high_bidder}"
                if ch.item.account_status and ch.item.account_status != "Sold":
                    msg += f" · {ch.item.account_status}"
            elif ch.kind == "bidder_change":
                msg = f"{title}\n{ch.old_bidder} → {ch.item.high_bidder}"
                if ch.item.account_status and ch.item.account_status != "Sold":
                    msg += f" · {ch.item.account_status}"
            elif ch.kind == "sold":
                msg = f"{title}\nFinal {_money(ch.item.bid_price)}"
                if ch.item.high_bidder:
                    msg += f" · {ch.item.high_bidder}"
                focus_ids.append(ch.item.item_id)
            elif ch.kind == "buy_now":
                paid = ch.item.bid_price or ch.item.buy_now_price
                msg = f"{title}\nBought now {_money(paid)}"
                focus_ids.append(ch.item.item_id)
            else:
                msg = ch.message
            buyer_id = int(ch.item.buyer_id or 0)
            self._append_activity(
                msg, kind=ch.kind, session=sess, draw=draw, buyer_id=buyer_id,
            )
            if (
                ch.item.suspended
                and not self._is_okayed(ch.item)
                and ch.kind in ("first_bid", "price_up", "bidder_change")
            ):
                inv = ""
                if ch.item.has_unpaid:
                    inv = f" · UNPAID×{ch.item.unpaid_invoice_count}"
                elif ch.item.unpaid_invoice_count == 0:
                    inv = " · no unpaid"
                if ch.item.has_voids:
                    inv += f" · VOID×{ch.item.void_invoice_count}"
                if ch.item.paid_invoice_count is not None:
                    inv += f" · paid×{ch.item.paid_invoice_count}"
                self._append_activity(
                    f"SUSPENDED ACCOUNT: {ch.item.high_bidder or '?'} on "
                    f"{ch.item.sku or ch.item.title[:40]}{inv}",
                    kind="risk",
                    session=sess,
                    draw=draw,
                    buyer_id=buyer_id,
                )
                toast_body = f"{sale_bit}{ch.item.high_bidder} · {ch.item.sku or ch.item.title[:40]}{inv}"
                _toast("Suspended · Unpaid" if ch.item.has_unpaid else "Suspended bidder", toast_body)
            elif (
                (ch.item.has_unpaid or ch.item.has_voids)
                and not ch.item.suspended
                and not self._is_okayed(ch.item)
                and ch.kind in ("first_bid", "price_up", "bidder_change")
            ):
                bits = []
                if ch.item.has_unpaid:
                    bits.append(f"UNPAID×{ch.item.unpaid_invoice_count}")
                if ch.item.has_voids:
                    bits.append(f"VOID×{ch.item.void_invoice_count}")
                if ch.item.paid_invoice_count is not None:
                    bits.append(f"paid×{ch.item.paid_invoice_count}")
                inv = " · ".join(bits)
                label = "Unpaid invoices" if ch.item.has_unpaid else "Voided invoices"
                self._append_activity(
                    f"{label.upper()}: {ch.item.high_bidder or '?'} on "
                    f"{ch.item.sku or ch.item.title[:40]} · {inv}",
                    kind="risk",
                    session=sess,
                    draw=draw,
                    buyer_id=buyer_id,
                )
                _toast(
                    label,
                    f"{sale_bit}{ch.item.high_bidder} · {ch.item.sku or ch.item.title[:40]} · {inv}",
                )
            elif ch.kind == "sold":
                _toast(ch.item.sku or "Sold", f"{sale_bit}{ch.message}")
            elif ch.kind == "buy_now":
                _toast("Bought now", f"{sale_bit}{ch.message}")
            elif ch.kind in ("first_bid", "price_up"):
                toast_title = ch.item.sku or (sess.title if sess else "Estate-Ez Auction Watcher")
                _toast(toast_title, f"{sale_bit}{ch.message}" if sale_bit else ch.message)

        if draw and focus_ids:
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
    WatcherApp(root, manage_window=True)
    root.mainloop()


if __name__ == "__main__":
    main()
