"""Estate-Ez Auction Watcher — Live Watch + Reports in one window."""
from __future__ import annotations

import sys
from pathlib import Path

import customtkinter as ctk

from runtime_paths import prepare_runtime

prepare_runtime()

ROOT = Path(__file__).resolve().parent
WATCHER_DIR = ROOT / "sale_watcher"
# Root first so `import app` is the Report Generator, not sale_watcher/app.py
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(WATCHER_DIR) not in sys.path:
    sys.path.append(str(WATCHER_DIR))

from app import ReportApp, _DND_AVAILABLE  # noqa: E402

if _DND_AVAILABLE:
    from app import _DnDCTk  # noqa: E402

from sale_watcher.app import THEMES, WatcherApp, apply_sizable_main_window, save_main_window_layout  # noqa: E402
from brand import APP_NAME  # noqa: E402

SUITE_TITLE = APP_NAME
WINDOW_BG = THEMES["dark"]["window"]
ACCENT = THEMES["dark"]["green"]
PANEL = THEMES["dark"]["panel"]


class SaleSuiteApp:
    def __init__(self, root: ctk.CTk, *, dnd: bool = False) -> None:
        self.root = root
        self.dnd = dnd
        root.title(SUITE_TITLE)
        root.configure(fg_color=WINDOW_BG)

        shell = ctk.CTkFrame(root, fg_color="transparent")
        shell.pack(fill="both", expand=True, padx=4, pady=4)

        self.tabs = ctk.CTkTabview(
            shell,
            fg_color=PANEL,
            segmented_button_fg_color=THEMES["dark"]["card_bg"],
            segmented_button_selected_color=ACCENT,
            segmented_button_selected_hover_color=THEMES["dark"]["green_hover"],
            segmented_button_unselected_color=THEMES["dark"]["card_bg"],
            segmented_button_unselected_hover_color=THEMES["dark"]["row_alt"],
            text_color=THEMES["dark"]["on_accent"],
            corner_radius=12,
        )
        self.tabs.pack(fill="both", expand=True)
        watch_tab = self.tabs.add("Live Watch")
        risk_tab = self.tabs.add("At risk")
        reports_tab = self.tabs.add("Reports")

        # Fill each tab edge-to-edge
        for tab in (watch_tab, risk_tab, reports_tab):
            tab.grid_columnconfigure(0, weight=1)
            tab.grid_rowconfigure(0, weight=1)

        watch_host = ctk.CTkFrame(watch_tab, fg_color=WINDOW_BG, corner_radius=0)
        watch_host.grid(row=0, column=0, sticky="nsew")
        risk_host = ctk.CTkFrame(risk_tab, fg_color=WINDOW_BG, corner_radius=0)
        risk_host.grid(row=0, column=0, sticky="nsew")
        reports_host = ctk.CTkFrame(reports_tab, fg_color=WINDOW_BG, corner_radius=0)
        reports_host.grid(row=0, column=0, sticky="nsew")

        self.watcher = WatcherApp(watch_host, manage_window=False)
        self.watcher.mount_risk_panel(risk_host)
        self.reports = ReportApp(
            reports_host, dnd=dnd, manage_window=False, appearance="dark",
        )
        apply_sizable_main_window(root, self.watcher.cfg)
        try:
            root.protocol("WM_DELETE_WINDOW", self._on_close)
        except Exception:
            pass

    def _on_close(self) -> None:
        save_main_window_layout(self.root, self.watcher.cfg)
        try:
            self.root.destroy()
        except Exception:
            pass


def main() -> None:
    prepare_runtime()
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("dark-blue")

    root = None
    dnd = False
    if _DND_AVAILABLE:
        try:
            root = _DnDCTk()
            dnd = True
        except Exception:  # noqa: BLE001
            root = None
    if root is None:
        root = ctk.CTk()

    SaleSuiteApp(root, dnd=dnd)
    root.mainloop()


if __name__ == "__main__":
    main()
