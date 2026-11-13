"""Local .env is loaded without overriding real environment variables."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from runtime_paths import _load_dotenv, mac_browser_candidates, macos_distribution_dir

SALE_WATCHER_DIR = Path(__file__).resolve().parents[1] / "sale_watcher"
if str(SALE_WATCHER_DIR) not in sys.path:
    sys.path.insert(0, str(SALE_WATCHER_DIR))
import paths  # noqa: E402


class LoadDotenvTests(unittest.TestCase):
    def test_loads_file_but_does_not_override_existing_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "ESTATE_EZ_API_BASE=https://example.test\n"
                'SALE_REPORT_BROWSER="/usr/bin/chromium"\n'
                "# comment\n"
                "export SALE_WATCHER_BROWSER=from-dotenv\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {"SALE_WATCHER_BROWSER": "already-set"},
                clear=False,
            ):
                os.environ.pop("ESTATE_EZ_API_BASE", None)
                os.environ.pop("SALE_REPORT_BROWSER", None)
                with mock.patch("runtime_paths.app_base_dir", return_value=Path(tmp)):
                    with mock.patch("runtime_paths.Path.cwd", return_value=Path(tmp)):
                        _load_dotenv()
                self.assertEqual(os.environ["ESTATE_EZ_API_BASE"], "https://example.test")
                self.assertEqual(os.environ["SALE_REPORT_BROWSER"], "/usr/bin/chromium")
                self.assertEqual(os.environ["SALE_WATCHER_BROWSER"], "already-set")
                os.environ.pop("ESTATE_EZ_API_BASE", None)
                os.environ.pop("SALE_REPORT_BROWSER", None)


class PlatformPathTests(unittest.TestCase):
    def test_mac_support_dir_uses_application_support(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(paths.sys, "platform", "darwin"):
                with mock.patch.dict(os.environ, {"HOME": tmp}, clear=False):
                    os.environ.pop("LOCALAPPDATA", None)
                    self.assertEqual(
                        paths.support_dir(),
                        Path(tmp) / "Library" / "Application Support" / "SaleWatcher",
                    )

    def test_localappdata_still_wins_on_mac(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(paths.sys, "platform", "darwin"):
                with mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}, clear=False):
                    self.assertEqual(paths.support_dir(), Path(tmp) / "SaleWatcher")

    def test_mac_browsers_empty_off_mac(self) -> None:
        with mock.patch("runtime_paths.sys.platform", "linux"):
            self.assertEqual(mac_browser_candidates(), [])

    def test_distribution_dir_only_for_frozen_mac_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "Estate-Ez Auction Watcher.app" / "Contents" / "MacOS" / "Estate-Ez Auction Watcher"
            exe.parent.mkdir(parents=True)
            exe.write_text("", encoding="utf-8")
            with mock.patch("runtime_paths.sys.platform", "darwin"):
                with mock.patch("runtime_paths.sys.frozen", True, create=True):
                    with mock.patch("runtime_paths.sys.executable", str(exe)):
                        self.assertEqual(macos_distribution_dir(), Path(tmp))


if __name__ == "__main__":
    unittest.main()
