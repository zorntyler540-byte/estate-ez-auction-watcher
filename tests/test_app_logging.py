"""Rotating log files: location, rotation, version/level stamps, and that secrets never reach disk."""

from __future__ import annotations

import logging
import os
import tempfile
import unittest
from logging.handlers import RotatingFileHandler
from pathlib import Path
from unittest import mock

from watcher_test_support import make_jwt

import app_logging  # noqa: E402  (path set by watcher_test_support)
from app_logging import REDACTED, RedactionFilter, redact  # noqa: E402

JWT = make_jwt(3600, sub="staff@example.com")
REFRESH = make_jwt(86400, role="seller-refresh")


class RedactTests(unittest.TestCase):
    def assertScrubbed(self, text: str, *secrets: str) -> str:
        out = redact(text)
        for secret in secrets:
            self.assertNotIn(secret, out)
        self.assertIn(REDACTED, out)
        return out

    def test_jwts_anywhere(self) -> None:
        self.assertScrubbed(f"minted {JWT} for seller", JWT)

    def test_bearer_header(self) -> None:
        self.assertScrubbed("Authorization: Bearer abcdef0123456789", "abcdef0123456789")
        self.assertScrubbed("headers={'Authorization': 'Bearer opaque-token-value'}", "opaque-token-value")

    def test_password_and_token_fields(self) -> None:
        self.assertScrubbed('{"email": "a@b.c", "password": "hunter2!"}', "hunter2!")
        self.assertScrubbed("password=hunter2 user=bob", "hunter2")
        self.assertScrubbed(f'{{"accessToken": "{JWT}", "refreshToken": "{REFRESH}"}}', JWT, REFRESH)
        self.assertScrubbed("refresh_token=opaque1234&grant=x", "opaque1234")
        self.assertScrubbed("https://x/y?token=sekrit&id=5", "sekrit")

    def test_mfa_codes(self) -> None:
        self.assertScrubbed('{"otp": "123456"}', "123456")
        self.assertScrubbed("verify code 654321 for buyer", "654321")
        self.assertScrubbed("mfaToken=abc123def456", "abc123def456")

    def test_ordinary_text_is_left_alone(self) -> None:
        for text in (
            "Seller token refreshed for sale 47921",
            "OK — 42 lots, hammer $1,234.00",
            "HTTP 503; retry 1 of 2 in 0.6s",
            "Estate-Ez push sent 2:31:05 PM · 42 lots",
        ):
            self.assertEqual(redact(text), text)

    def test_filter_redacts_args_and_exception_text(self) -> None:
        record = logging.LogRecord(
            "sale_watcher.test", logging.WARNING, __file__, 1,
            "login for %s with password=%s", ("bob", "hunter2"), None,
        )
        try:
            raise RuntimeError(f"refused: Bearer {JWT}")
        except RuntimeError:
            import sys

            record.exc_info = sys.exc_info()
        self.assertTrue(RedactionFilter().filter(record))
        self.assertNotIn("hunter2", record.getMessage())
        self.assertNotIn(JWT, record.exc_text)
        self.assertIn("RuntimeError", record.exc_text)


class LogFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        app_logging.teardown_logging()
        self.addCleanup(app_logging.teardown_logging)
        root = logging.getLogger()
        old_level = root.level
        self.addCleanup(root.setLevel, old_level)

    def _handler(self) -> RotatingFileHandler:
        handlers = [h for h in logging.getLogger().handlers if getattr(h, "_sale_watcher", False)]
        self.assertEqual(len(handlers), 1)
        self.assertIsInstance(handlers[0], RotatingFileHandler)
        return handlers[0]

    def test_default_folder_is_localappdata_salewatcher_logs(self) -> None:
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": self._tmp.name}):
            self.assertEqual(app_logging.log_dir(), Path(self._tmp.name) / "SaleWatcher" / "logs")
            path = app_logging.setup_logging("9.9.9")
        self.assertEqual(path, Path(self._tmp.name) / "SaleWatcher" / "logs" / "watcher.log")

    def test_rotation_is_five_files_of_two_mb(self) -> None:
        app_logging.setup_logging("9.9.9", directory=Path(self._tmp.name))
        handler = self._handler()
        self.assertEqual(handler.maxBytes, 2 * 1024 * 1024)
        self.assertEqual(handler.backupCount, 4)

    def test_setup_is_idempotent(self) -> None:
        first = app_logging.setup_logging("9.9.9", directory=Path(self._tmp.name))
        second = app_logging.setup_logging("9.9.9", directory=Path(self._tmp.name) / "elsewhere")
        self.assertEqual(first, second)
        self._handler()

    def test_file_has_timestamp_level_version_and_no_secrets(self) -> None:
        path = app_logging.setup_logging("1.2.3", directory=Path(self._tmp.name))
        log = logging.getLogger("sale_watcher.test")
        log.info("Estate-Ez minted %s", JWT)
        log.warning("Seller sign in failed for bob password=%s otp=%s", "hunter2", "998877")
        try:
            raise ValueError(f"server said Bearer {REFRESH}")
        except ValueError:
            log.exception("refresh failed")
        self._handler().flush()
        text = path.read_text(encoding="utf-8")
        for secret in (JWT, REFRESH, "hunter2", "998877"):
            self.assertNotIn(secret, text)
        self.assertRegex(text, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} INFO    v1\.2\.3 ")
        self.assertIn("WARNING v1.2.3", text)
        self.assertIn("ERROR   v1.2.3", text)
        self.assertIn("Traceback", text)
        self.assertIn("Starting v1.2.3", text)

    def test_unwritable_folder_falls_back_without_crashing(self) -> None:
        blocker = Path(self._tmp.name) / "not-a-dir"
        blocker.write_text("x", encoding="utf-8")
        self.assertIsNone(app_logging.setup_logging("9.9.9", directory=blocker / "logs"))
        logging.getLogger("sale_watcher.test").info("still fine")


class OpenFolderTests(unittest.TestCase):
    def test_open_log_folder_creates_and_opens_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}):
            with mock.patch.object(app_logging, "open_folder") as opener:
                folder = app_logging.open_log_folder()
        opener.assert_called_once_with(folder)
        self.assertEqual(folder, Path(tmp) / "SaleWatcher" / "logs")

    def test_open_folder_uses_platform_opener(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "logs"
            with mock.patch.object(app_logging.sys, "platform", "linux"), \
                    mock.patch.object(app_logging.subprocess, "Popen") as popen:
                app_logging.open_folder(target)
            self.assertTrue(target.is_dir())
            popen.assert_called_once_with(["xdg-open", str(target)])


if __name__ == "__main__":
    unittest.main()
