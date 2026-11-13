"""Rotating log files under the per-user SaleWatcher folder, with secrets redacted.

Windows: ``%LOCALAPPDATA%\\SaleWatcher\\logs``. Mac: ``~/Library/Application Support/SaleWatcher/logs``.

Every handler gets ``RedactionFilter`` so JWTs, bearer tokens, passwords and
one-time codes never reach disk, even inside exception text.
"""
from __future__ import annotations

import logging
import os
import platform
import re
import subprocess
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FILE_NAME = "watcher.log"
MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 4  # watcher.log + 4 rotated copies = 5 files
REDACTED = "[REDACTED]"

_SECRET_KEYS = (
    r"password|passwd|pwd|pass|secret|"
    r"token|access_?token|refresh_?token|accessToken|refreshToken|newToken|id_?token|"
    r"access|refresh|mfa_?token|mfaToken|otp|mfa_?code|code|"
    r"authorization|cookie|api_?key|apikey|x-api-key"
)

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # JWTs (header.payload.signature), wherever they appear.
    (re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*"), REDACTED),
    # Authorization schemes followed by a credential.
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + REDACTED),
    # key=value, key: value, "key": "value", ?key=value&...  A key that ends a URL path
    # ("/auth/token: HTTP 503") is not a field, and an already-masked value is left alone.
    (
        re.compile(
            r"(?i)(?<![/\w.-])([\"']?\b(?:" + _SECRET_KEYS + r")\b[\"']?\s*[:=]\s*)([\"']?)"
            r"(?!" + re.escape(REDACTED) + r")([^\"'\s,;&}\]]+)"
        ),
        r"\1\2" + REDACTED,
    ),
    # "code 123456", "OTP is 123456"
    (
        re.compile(r"(?i)\b(otp|code|mfa|passcode)(\s+(?:is|was))?\s+(\d{4,8})\b"),
        r"\1\2 " + REDACTED,
    ),
)


def redact(text: str) -> str:
    """Mask tokens, passwords and one-time codes in free text."""
    if not text:
        return text
    out = str(text)
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out


class RedactionFilter(logging.Filter):
    """Redact the rendered message, exception text and stack before any handler formats it."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad format string must not drop the record
            message = f"{record.msg!r} {record.args!r}"
        record.msg = redact(message)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


class VersionFilter(logging.Filter):
    def __init__(self, version: str) -> None:
        super().__init__()
        self.version = version

    def filter(self, record: logging.LogRecord) -> bool:
        record.app_version = self.version
        return True


LOG_FORMAT = (
    "%(asctime)s.%(msecs)03d %(levelname)-7s v%(app_version)s "
    "[%(threadName)s] %(name)s: %(message)s"
)
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def log_dir() -> Path:
    from paths import support_dir

    return support_dir() / "logs"


def log_file() -> Path:
    return log_dir() / LOG_FILE_NAME


_setup_lock = threading.Lock()
_handler: logging.Handler | None = None
_hooks_installed = False


def _make_handler(path: Path, version: str) -> logging.Handler:
    handler = RotatingFileHandler(
        path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8", delay=True,
    )
    handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
    handler.addFilter(VersionFilter(version))
    handler.addFilter(RedactionFilter())
    return handler


def _install_exception_hooks() -> None:
    global _hooks_installed
    if _hooks_installed:
        return
    _hooks_installed = True
    log = logging.getLogger("sale_watcher.crash")
    previous_sys = sys.excepthook
    previous_thread = threading.excepthook

    def _sys_hook(exc_type, exc, tb) -> None:
        if not issubclass(exc_type, KeyboardInterrupt):
            log.critical("Unhandled error", exc_info=(exc_type, exc, tb))
        previous_sys(exc_type, exc, tb)

    def _thread_hook(args) -> None:
        if args.exc_type is not SystemExit:
            name = args.thread.name if args.thread else "?"
            log.error("Unhandled error in thread %s", name, exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        previous_thread(args)

    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook


def setup_logging(version: str, *, directory: Path | None = None, level: int = logging.INFO) -> Path | None:
    """Attach the rotating file handler to the root logger once. Returns the log file, or None if unwritable."""
    global _handler
    with _setup_lock:
        root = logging.getLogger()
        if _handler is None:
            # This module can be imported twice (``app_logging`` and ``sale_watcher.app_logging``).
            _handler = next((h for h in root.handlers if getattr(h, "_sale_watcher", False)), None)
        if _handler is not None:
            existing = getattr(_handler, "baseFilename", "")
            return Path(existing) if existing else None
        folder = directory or log_dir()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            handler = _make_handler(folder / LOG_FILE_NAME, version)
        except OSError:
            # The windowed .exe has no console: sys.stderr is None there.
            handler = logging.StreamHandler(sys.stderr) if sys.stderr else logging.NullHandler()
            handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
            handler.addFilter(VersionFilter(version))
            handler.addFilter(RedactionFilter())
            path = None
        else:
            path = folder / LOG_FILE_NAME
        handler._sale_watcher = True  # type: ignore[attr-defined]
        root.addHandler(handler)
        if root.level == logging.NOTSET or root.level > level:
            root.setLevel(level)
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        _handler = handler
        _install_exception_hooks()
    logging.getLogger("sale_watcher").info(
        "Starting v%s (Python %s, %s, frozen=%s)",
        version, platform.python_version(), platform.platform(), bool(getattr(sys, "frozen", False)),
    )
    return path


def teardown_logging() -> None:
    """Detach and close the handler added by ``setup_logging``."""
    global _handler
    with _setup_lock:
        root = logging.getLogger()
        for handler in [h for h in root.handlers if getattr(h, "_sale_watcher", False)]:
            root.removeHandler(handler)
            handler.close()
        _handler = None


def open_folder(path: Path) -> None:
    """Show a folder in Explorer (Finder / the desktop file manager off Windows)."""
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def open_log_folder() -> Path:
    folder = log_dir()
    open_folder(folder)
    return folder
