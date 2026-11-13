"""Resolve app / browser paths for both source runs and frozen PyInstaller builds."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def macos_distribution_dir() -> Path | None:
    """Folder that contains the ``.app`` when this is a frozen Mac build.

    Staff edit ``config.json`` in that folder. Inside the bundle the executable
    lives at ``Name.app/Contents/MacOS/Name``.
    """
    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        return None
    exe = Path(sys.executable).resolve()
    macos_dir = exe.parent
    contents = macos_dir.parent
    bundle = contents.parent
    if macos_dir.name == "MacOS" and contents.name == "Contents" and bundle.suffix == ".app":
        return bundle.parent
    return None


def mac_browser_candidates() -> list[Path]:
    """Chrome, Edge, and a Chromium bundled next to a frozen Mac build."""
    if sys.platform != "darwin":
        return []
    found = [
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        Path.home() / "Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]
    roots = [app_base_dir() / "ms-playwright"]
    outside = macos_distribution_dir()
    if outside is not None:
        roots.append(outside / "ms-playwright")
    for root in roots:
        if not root.is_dir():
            continue
        found.extend(root.glob("chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium"))
    return found


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
