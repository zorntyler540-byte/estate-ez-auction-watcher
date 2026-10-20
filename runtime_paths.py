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


def _load_dotenv() -> None:
    """Load key=value pairs from a local .env without overriding real env vars."""
    seen: set[Path] = set()
    for path in (app_base_dir() / ".env", Path.cwd() / ".env"):
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen or not path.is_file():
            continue
        seen.add(resolved)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.lower().startswith("export "):
                line = line[7:].strip()
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'").strip('"')
            if key and key not in os.environ:
                os.environ[key] = value


def prepare_runtime() -> None:
    """Load local .env, then point Playwright at bundled Chromium when present."""
    _load_dotenv()
    bundled = app_base_dir() / "ms-playwright"
    if bundled.is_dir():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(bundled))
