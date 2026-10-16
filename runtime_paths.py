"""Resolve app / browser paths for both source runs and frozen PyInstaller builds."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def app_base_dir() -> Path:
    """Folder that holds the .exe (frozen) or this project (source)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def prepare_runtime() -> None:
    """Point Playwright at a bundled Chromium folder when present next to the app."""
    bundled = app_base_dir() / "ms-playwright"
    if bundled.is_dir():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(bundled))
