"""Encrypt token files and saved passwords for the signed-in user.

Windows uses DPAPI (CurrentUser). Mac uses a random key kept in the login
Keychain, then seals the file with that key. Encrypted token files start with
``MAGIC`` so a legacy plaintext token can be recognised and re-written on first
read. With neither store (tests, Linux dev) tokens fall back to plaintext with
a one-time warning, and saving a password raises instead of writing it in the clear.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import shutil
import subprocess
import sys
import threading

log = logging.getLogger("sale_watcher.secure_store")

MAGIC = b"SWDPAPI1\n"
_KEYCHAIN_MAGIC = b"SWKC1\n"
TOKEN_ENTROPY = b"Estate-Ez Auction Watcher token v1"
CRYPTPROTECT_UI_FORBIDDEN = 0x1
KEYCHAIN_SERVICE = "Estate-Ez Auction Watcher"
KEYCHAIN_ACCOUNT = "secret-file-key-v1"

_warn_lock = threading.Lock()
_warned_plaintext = False


class SecretStoreError(OSError):
    """The platform store is missing or refused to protect / unprotect the data."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.strerror = message


def dpapi_available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.crypt32 and ctypes.windll.kernel32)
    except (AttributeError, OSError, ImportError):
        return False


def keychain_available() -> bool:
    """True on a Mac where the ``security`` tool can reach the login Keychain."""
    if sys.platform != "darwin":
        return False
    return shutil.which("security") is not None or _security_binary().is_file()


def secure_store_available() -> bool:
    return dpapi_available() or keychain_available()


def _security_binary() -> "Path":
    from pathlib import Path

    return Path("/usr/bin/security")


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


def _run_security(args: list[str]) -> subprocess.CompletedProcess[str]:
    binary = _security_binary()
    command = [str(binary) if binary.is_file() else "security", *args]
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SecretStoreError("The Mac Keychain did not answer. Unlock this Mac and try again.") from exc


def _read_keychain_secret(service: str, account: str) -> str:
    result = _run_security(["find-generic-password", "-s", service, "-a", account, "-w"])
    if result.returncode != 0:
        err = f"{result.stderr or ''} {result.stdout or ''}".lower()
        if "could not be found" in err or "item not found" in err or "not found" in err:
            return ""
        raise SecretStoreError("The Mac Keychain could not be read. Unlock this Mac and try again.")
    return (result.stdout or "").strip()


def _write_keychain_secret(service: str, account: str, secret: str) -> None:
    result = _run_security(
        [
            "add-generic-password",
            "-s",
            service,
            "-a",
            account,
            "-w",
            secret,
            "-U",
            "-j",
            "Estate-Ez Auction Watcher",
        ]
    )
    if result.returncode != 0:
        raise SecretStoreError("The Mac Keychain refused to save this. Unlock this Mac and try again.")


def _keychain_key() -> bytes:
    existing = _read_keychain_secret(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
    if existing:
        try:
            key = base64.b64decode(existing, validate=True)
        except Exception as exc:
            raise SecretStoreError(
                "The Mac Keychain entry for the Watcher is unreadable. "
                "Remove “Estate-Ez Auction Watcher” in Keychain Access, then sign in again."
            ) from exc
        if len(key) < 16:
            raise SecretStoreError(
                "The Mac Keychain entry for the Watcher is unreadable. "
                "Remove “Estate-Ez Auction Watcher” in Keychain Access, then sign in again."
            )
        return key
    key = secrets.token_bytes(32)
    _write_keychain_secret(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT, base64.b64encode(key).decode("ascii"))
    return key


def _keystream(key: bytes, nonce: bytes, size: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < size:
        out.extend(hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest())
        counter += 1
    return bytes(out[:size])


def _seal(data: bytes, key: bytes, entropy: bytes | None) -> bytes:
    material = entropy or b""
    stream_key = hmac.new(key, b"stream" + material, hashlib.sha256).digest()
    mac_key = hmac.new(key, b"mac" + material, hashlib.sha256).digest()
    nonce = secrets.token_bytes(16)
    stream = _keystream(stream_key, nonce, len(data))
    ciphertext = bytes(left ^ right for left, right in zip(data, stream))
    mac = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
    return _KEYCHAIN_MAGIC + nonce + mac + ciphertext


def _open_sealed(blob: bytes, key: bytes, entropy: bytes | None) -> bytes:
    if not blob.startswith(_KEYCHAIN_MAGIC):
        raise SecretStoreError("The saved data could not be unlocked on this Mac. Sign in again.")
    body = blob[len(_KEYCHAIN_MAGIC) :]
    if len(body) < 16 + 32:
        raise SecretStoreError("The saved data could not be unlocked on this Mac. Sign in again.")
    nonce, mac, ciphertext = body[:16], body[16:48], body[48:]
    material = entropy or b""
    mac_key = hmac.new(key, b"mac" + material, hashlib.sha256).digest()
    expected = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        raise SecretStoreError("The saved data could not be unlocked on this Mac. Sign in again.")
    stream_key = hmac.new(key, b"stream" + material, hashlib.sha256).digest()
    stream = _keystream(stream_key, nonce, len(ciphertext))
    return bytes(left ^ right for left, right in zip(ciphertext, stream))


def _unavailable(action: str) -> SecretStoreError:
    return SecretStoreError(
        f"This computer cannot {action} that securely. "
        "Saved passwords work on Windows and Mac."
    )


def protect(data: bytes, *, entropy: bytes | None = None) -> bytes:
    if dpapi_available():
        return _crypt("protect", data, entropy)
    if keychain_available():
        return _seal(data, _keychain_key(), entropy)
    raise _unavailable("save")


def unprotect(blob: bytes, *, entropy: bytes | None = None) -> bytes:
    if dpapi_available():
        return _crypt("unprotect", blob, entropy)
    if keychain_available():
        return _open_sealed(blob, _keychain_key(), entropy)
    raise _unavailable("read")


def _warn_plaintext_once() -> None:
    global _warned_plaintext
    with _warn_lock:
        if _warned_plaintext:
            return
        _warned_plaintext = True
    log.warning(
        "No secure store is available on this platform; session token files are stored "
        "UNENCRYPTED. This is only expected in tests or development, never on a franchise PC."
    )


def encode_token(value: str) -> bytes:
    """Bytes to write for a token file. Encrypted on Windows and Mac, plaintext elsewhere."""
    raw = value.encode("utf-8")
    if not secure_store_available():
        _warn_plaintext_once()
        return raw
    return MAGIC + protect(raw, entropy=TOKEN_ENTROPY)


def decode_token(raw: bytes) -> tuple[str | None, bool]:
    """``(token, needs_migration)``. A token that can't be decrypted reads as missing."""
    if raw.startswith(MAGIC):
        try:
            text = unprotect(raw[len(MAGIC) :], entropy=TOKEN_ENTROPY).decode("utf-8")
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
    return value, bool(value) and secure_store_available()
