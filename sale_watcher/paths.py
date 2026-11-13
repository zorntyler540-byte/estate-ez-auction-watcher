"""Per-user Watcher folder on Windows and Mac.

``LOCALAPPDATA`` wins when it is set, so tests and a Windows install keep the
historical path. A Mac without that variable uses Application Support.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def support_dir() -> Path:
    override = (os.environ.get("LOCALAPPDATA") or "").strip()
    if override:
        return Path(override) / "SaleWatcher"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "SaleWatcher"
    return Path.home() / "AppData" / "Local" / "SaleWatcher"
