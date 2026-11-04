"""Windows DPAPI (CurrentUser scope) for token files and saved passwords.

Encrypted token files start with ``MAGIC`` so a legacy plaintext token can be
recognised and re-written encrypted on first read. Off Windows (tests, dev)
DPAPI does not exist: tokens fall back to plaintext with a one-time warning,
and saving a password raises instead of writing it in the clear.
"""
from __future__ import annotations

import logging
import sys
import threading

log = logging.getLogger("sale_watcher.secure_store")

MAGIC = b"SWDPAPI1\n"
TOKEN_ENTROPY = b"Estate-Ez Auction Watcher token v1"
CRYPTPROTECT_UI_FORBIDDEN = 0x1

_warn_lock = threading.Lock()
_warned_plaintext = False


class SecretStoreError(OSError):
    """DPAPI is missing or refused to protect / unprotect the data."""


def dpapi_available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.crypt32 and ctypes.windll.kernel32)
    except (AttributeError, OSError, ImportError):
        return False


def _crypt(op: str, data: bytes, entropy: bytes | None) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    def _blob(raw: bytes) -> tuple[DATA_BLOB, object]:
        buf = ctypes.create_string_buffer(raw, len(raw))
        return DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte))), buf

    blob_in, _keep_in = _blob(data)
    entropy_ref = None
    _keep_entropy = None
    if entropy:
        entropy_blob, _keep_entropy = _blob(entropy)
        entropy_ref = ctypes.byref(entropy_blob)
    blob_out = DATA_BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if op == "protect" else ctypes.windll.crypt32.CryptUnprotectData
    ok = fn(
        ctypes.byref(blob_in), None, entropy_ref, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out),
    )
    if not ok:
        raise SecretStoreError(f"Windows could not {op} the saved data on this PC.")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def protect(data: bytes, *, entropy: bytes | None = None) -> bytes:
    if not dpapi_available():
        raise SecretStoreError("Windows DPAPI is not available, so this cannot be saved securely.")
    return _crypt("protect", data, entropy)


def unprotect(blob: bytes, *, entropy: bytes | None = None) -> bytes:
    if not dpapi_available():
        raise SecretStoreError("Windows DPAPI is not available, so this cannot be read.")
    return _crypt("unprotect", blob, entropy)


def _warn_plaintext_once() -> None:
    global _warned_plaintext
    with _warn_lock:
        if _warned_plaintext:
            return
        _warned_plaintext = True
    log.warning(
        "Windows DPAPI is not available on this platform; session token files are stored "
        "UNENCRYPTED. This is only expected in tests or development, never on a franchise PC."
    )


def encode_token(value: str) -> bytes:
    """Bytes to write for a token file: DPAPI-encrypted on Windows, plaintext elsewhere."""
    raw = value.encode("utf-8")
    if not dpapi_available():
        _warn_plaintext_once()
        return raw
    return MAGIC + protect(raw, entropy=TOKEN_ENTROPY)


def decode_token(raw: bytes) -> tuple[str | None, bool]:
    """``(token, needs_migration)``. A token that can't be decrypted reads as missing."""
    if raw.startswith(MAGIC):
        try:
            text = unprotect(raw[len(MAGIC):], entropy=TOKEN_ENTROPY).decode("utf-8")
        except (SecretStoreError, UnicodeDecodeError) as exc:
            log.warning("A saved session token could not be decrypted (%s); sign in again.", type(exc).__name__)
            return None, False
        return text.strip() or None, False
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        log.warning("A saved session token file was unreadable; sign in again.")
        return None, False
    value = text.strip() or None
    return value, bool(value) and dpapi_available()
