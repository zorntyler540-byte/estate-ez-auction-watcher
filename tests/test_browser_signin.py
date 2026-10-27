"""Sign-in opens the website page instead of the in-app one-time-code form."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sale_watcher"))

from sale_watcher.auth import browser_channel_order
from sale_watcher.estate_ez import web_base


class BrowserSignInTests(unittest.TestCase):
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
