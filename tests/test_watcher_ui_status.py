"""Live Watch UI keeps push / sync status per sale (needs a display; skipped when Tk can't open one)."""

from __future__ import annotations

import tkinter as tk
import unittest
from datetime import datetime, timedelta, timezone

from watcher_test_support import TempHomeTestCase

from push_status import FAILED, SENT, PushResult  # noqa: E402

A, B = 47921, 48010


def _tk_available() -> bool:
    try:
        root = tk.Tk()
    except tk.TclError:
        return False
    root.destroy()
    return True


@unittest.skipUnless(_tk_available(), "needs a display for Tk")
class WatcherUiStatusTests(TempHomeTestCase):
    def setUp(self) -> None:
        super().setUp()
        import customtkinter as ctk

        import app as watcher_app  # sale_watcher/app.py (path set by watcher_test_support)

        self.mod = watcher_app
        self.root = ctk.CTk()
        self.addCleanup(self._destroy)
        self.ui = watcher_app.WatcherApp(self.root, manage_window=True)
        self.ui._ensure_session(A, "", "The Vault")
        self.ui._ensure_session(B, "", "Maple Ridge")
        self.ui._select_session(A, title="The Vault")

    def _destroy(self) -> None:
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def chip(self, chip) -> str:
        return chip.label.cget("text")

    def test_title_shows_version(self) -> None:
        self.assertIn(f"v{self.mod.APP_VERSION}", self.root.title())

    def test_other_sales_push_does_not_overwrite_the_visible_status(self) -> None:
        now = datetime.now(timezone.utc)
        self.ui._on_push_result(A, PushResult(A, SENT, "sent A", at=now, item_count=12))
        shown = self.chip(self.ui.push_chip)
        self.assertIn("12 lots", shown)

        self.ui._on_push_result(B, PushResult(B, FAILED, "B failed", at=now + timedelta(seconds=5), error="down"))
        self.assertEqual(self.chip(self.ui.push_chip), shown)
        self.assertEqual(self.ui._sessions[A].push_status.last_outcome, SENT)
        self.assertEqual(self.ui._sessions[B].push_status.last_error, "down")

        self.ui._select_session(B, title="Maple Ridge")
        self.assertIn("failed", self.chip(self.ui.push_chip))
        self.ui._select_session(A, title="The Vault")
        self.assertEqual(self.chip(self.ui.push_chip), shown)

    def test_status_line_and_sync_state_are_restored_per_sale(self) -> None:
        self.ui._on_session_status(A, "OK — A is fine")
        self.ui._on_session_status(B, "Refresh failed: B is down")
        self.assertEqual(self.ui.status_var.get(), "OK — A is fine")

        self.ui._on_sync_state(A, "ok")
        self.ui._on_sync_state(B, "error", "B is down")
        self.assertIn("Last sync", self.chip(self.ui.sync_chip))

        self.ui._select_session(B, title="Maple Ridge")
        self.assertEqual(self.ui.status_var.get(), "Refresh failed: B is down")
        self.assertIn("Refresh failed", self.chip(self.ui.sync_chip))

        self.ui._on_sync_state(A, "start")
        self.assertIn("Refresh failed", self.chip(self.ui.sync_chip))

    def test_push_window_lists_every_sale(self) -> None:
        now = datetime.now(timezone.utc)
        self.ui._on_push_result(A, PushResult(A, SENT, "sent A", at=now, item_count=12))
        self.ui._on_push_result(B, PushResult(B, FAILED, "B failed", at=now, error="down"))
        self.ui._show_push_status()
        texts: list[str] = []

        def walk(widget) -> None:
            try:
                texts.append(str(widget.cget("text")))
            except (tk.TclError, ValueError, AttributeError):
                pass
            for child in widget.winfo_children():
                walk(child)

        walk(self.ui._push_window)
        joined = "\n".join(texts)
        self.assertIn("The Vault", joined)
        self.assertIn("Maple Ridge", joined)
        self.assertIn("down", joined)

    def test_sign_in_chips_follow_tokens(self) -> None:
        self.ui.estate_ez_token = "x"
        self.ui.token = None
        self.ui.seller_token = "y"
        self.ui._refresh_login_badge()
        self.assertEqual(self.chip(self.ui.ee_chip), "Estate-Ez: signed in")
        self.assertEqual(self.chip(self.ui.buyer_chip), "Buyer: signed out")
        self.assertEqual(self.chip(self.ui.seller_chip), "Seller: signed in")

    def test_theme_toggle_restyles_without_errors(self) -> None:
        self.ui._toggle_theme()
        self.ui._toggle_theme()


if __name__ == "__main__":
    unittest.main()
