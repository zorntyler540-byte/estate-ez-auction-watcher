"""Sign-in opens the website page instead of the in-app one-time-code form."""

from __future__ import annotations

import ast
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sale_watcher"))

from auth import browser_channel_order
from estate_ez import web_base

APP_PATH = ROOT / "sale_watcher" / "app.py"


def _method_source(name: str) -> str:
    tree = ast.parse(APP_PATH.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "WatcherApp":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == name:
                    return ast.get_source_segment(APP_PATH.read_text(encoding="utf-8"), item) or ""
    raise AssertionError(f"WatcherApp.{name} is missing")


class BrowserSignInTests(unittest.TestCase):
    def test_sign_in_buttons_open_the_website_not_an_app_form(self) -> None:
        src = APP_PATH.read_text(encoding="utf-8")
        self.assertNotIn("Paste the 6-digit code", src)
        self.assertNotIn("Use browser instead", src)
        self.assertNotIn("_open_sign_in_dialog", src)
        self.assertNotIn("_open_estate_ez_dialog", src)
        self.assertIn('self._browser_sign_in("buyer")', _method_source("_on_login"))
        self.assertIn('self._browser_sign_in("seller")', _method_source("_on_seller_login"))
        self.assertIn('self._browser_sign_in("estateez")', _method_source("_on_estate_ez_login"))
        browser = _method_source("_browser_sign_in")
        self.assertIn("open_login_window", browser)
        self.assertIn("open_seller_login_window", browser)
        self.assertIn("open_estate_ez_login_window", browser)

    def test_visible_sign_in_prefers_the_installed_browser(self) -> None:
        self.assertEqual(
            browser_channel_order(headless=False, saved=None),
            ["chrome", "msedge", None],
        )

    def test_quiet_refresh_does_not_switch_browsers(self) -> None:
        self.assertEqual(browser_channel_order(headless=True, saved=None), [None])
        self.assertEqual(browser_channel_order(headless=True, saved="chrome"), ["chrome", None])
        self.assertEqual(browser_channel_order(headless=False, saved="msedge"), ["msedge", None])
        self.assertEqual(browser_channel_order(headless=False, saved="chromium"), [None])

    def test_estate_ez_page_defaults_to_the_live_site(self) -> None:
        previous = os.environ.pop("ESTATE_EZ_WEB_BASE", None)
        try:
            self.assertEqual(web_base(), "https://sublime-delight-production.up.railway.app")
            os.environ["ESTATE_EZ_WEB_BASE"] = "https://example.test/estate"
            self.assertEqual(web_base(), "https://example.test/estate")
        finally:
            if previous is None:
                os.environ.pop("ESTATE_EZ_WEB_BASE", None)
            else:
                os.environ["ESTATE_EZ_WEB_BASE"] = previous


if __name__ == "__main__":
    unittest.main()
