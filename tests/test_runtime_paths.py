"""Local .env is loaded without overriding real environment variables."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from runtime_paths import _load_dotenv


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


if __name__ == "__main__":
    unittest.main()
