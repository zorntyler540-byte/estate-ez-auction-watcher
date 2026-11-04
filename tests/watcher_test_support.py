"""Shared helpers: import sale_watcher modules the way the app does, with a temp AppData."""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SALE_WATCHER_DIR = Path(__file__).resolve().parents[1] / "sale_watcher"
if str(SALE_WATCHER_DIR) not in sys.path:
    sys.path.insert(0, str(SALE_WATCHER_DIR))

import requests  # noqa: E402


def make_response(status: int, body: object | None = None, headers: dict | None = None) -> requests.Response:
    resp = requests.Response()
    resp.status_code = status
    resp._content = json.dumps(body).encode("utf-8") if body is not None else b""
    resp.headers.update(headers or {})
    return resp


def make_jwt(seconds_left: float, **claims: object) -> str:
    def _b64(data: dict) -> str:
        raw = json.dumps(data).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    payload = {"exp": int(time.time() + seconds_left), **claims}
    return f"{_b64({'alg': 'none'})}.{_b64(payload)}.sig"


def reset_estate_ez_state() -> None:
    import estate_ez

    estate_ez._login_blocked_until = 0.0
    estate_ez._mint_cooldown_until = 0.0
    estate_ez._mint_cooldown_from_server = False
    estate_ez._mint_unavailable_detail = None
    estate_ez._mint_unavailable_account = ""


class TempHomeTestCase(unittest.TestCase):
    """Points Path.home() (and so watcher_data_dir) at a temp folder."""

    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        patcher = mock.patch.dict(
            os.environ,
            {"HOME": str(self.home), "USERPROFILE": str(self.home)},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # patch.dict restores the whole environment on stop, popped keys included.
        os.environ.pop("ESTATE_EZ_API_BASE", None)
        os.environ.pop("ESTATE_EZ_SEND_COMPLETE", None)
        reset_estate_ez_state()
        self.addCleanup(reset_estate_ez_state)

    @property
    def data_dir(self) -> Path:
        import auth

        return auth.watcher_data_dir()
