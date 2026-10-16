"""CTBids login session: persist access+refresh and keep JWTs alive.

CTBids access tokens last ~30 minutes. The websites store a longer-lived
``refresh`` token in localStorage and GET a new access token with:

  Authorization: Bearer <refresh>

Buyer:  GET https://api.ctbids.com/services/api/v1/buyer/token/refresh
Seller: GET https://seller.ctbids.com/services/api/v1/token/refresh
Response: ``{ "data": { "newToken": "<access jwt>" } }``
"""
from __future__ import annotations

import base64
import json
import threading
import time
from pathlib import Path
from typing import Callable

import requests

from ctbids_client import CtbidsClient
from seller_client import SellerClient

BUYER_REFRESH_URL = "https://api.ctbids.com/services/api/v1/buyer/token/refresh"
SELLER_REFRESH_URL = "https://seller.ctbids.com/services/api/v1/token/refresh"
BUYER_LOGIN_URL = "https://api.ctbids.com/services/api/v1/buyer/auth/token"
SELLER_LOGIN_URL = "https://seller.ctbids.com/services/api/v1/auth/token"
BUYER_MFA_SEND_URL = "https://api.ctbids.com/services/api/v1/buyer/user/mfa/otp/send"
BUYER_MFA_VERIFY_URL = "https://api.ctbids.com/services/api/v1/buyer/user/mfa/verify"
BUYER_MFA_RESEND_URL = "https://api.ctbids.com/services/api/v1/buyer/user/mfa/otp/resend"
REFRESH_SKEW_SECONDS = 8 * 60  # refresh when fewer than 8 minutes remain

_JS_READ_TOKENS = """() => ({
  access: window.localStorage.getItem('access') || '',
  refresh: window.localStorage.getItem('refresh') || '',
})"""

_buyer_profile_lock = threading.Lock()
_seller_profile_lock = threading.Lock()
_password_login_blocked_until: dict[str, float] = {}


def watcher_data_dir() -> Path:
    base = Path.home() / "AppData" / "Local" / "SaleWatcher"
    base.mkdir(parents=True, exist_ok=True)
    return base


def profile_dir() -> Path:
    path = watcher_data_dir() / "browser_profile"
    path.mkdir(parents=True, exist_ok=True)
    return path


def seller_profile_dir() -> Path:
    path = watcher_data_dir() / "seller_browser_profile"
    path.mkdir(parents=True, exist_ok=True)
    return path


def token_path() -> Path:
    return watcher_data_dir() / "access_token.txt"


def refresh_token_path() -> Path:
    return watcher_data_dir() / "refresh_token.txt"


def seller_token_path() -> Path:
    return watcher_data_dir() / "seller_access_token.txt"


def seller_refresh_token_path() -> Path:
    return watcher_data_dir() / "seller_refresh_token.txt"


def _write_secret(path: Path, value: str | None) -> None:
    if value:
        path.write_text(value.strip(), encoding="utf-8")
    elif path.exists():
        path.unlink()


def _read_secret(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def save_access_token(token: str | None) -> None:
    _write_secret(token_path(), token)


def load_saved_access_token() -> str | None:
    return _read_secret(token_path())


def save_refresh_token(token: str | None) -> None:
    _write_secret(refresh_token_path(), token)


def load_saved_refresh_token() -> str | None:
    return _read_secret(refresh_token_path())


def save_seller_access_token(token: str | None) -> None:
    _write_secret(seller_token_path(), token)


def load_saved_seller_access_token() -> str | None:
    return _read_secret(seller_token_path())


def save_seller_refresh_token(token: str | None) -> None:
    _write_secret(seller_refresh_token_path(), token)


def load_saved_seller_refresh_token() -> str | None:
    return _read_secret(seller_refresh_token_path())


def jwt_exp(token: str | None) -> int | None:
    if not token or token.count(".") != 2:
        return None
    try:
        payload = token.split(".")[1]
        payload += "=" * ((4 - len(payload) % 4) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        exp = data.get("exp")
        return int(exp) if exp is not None else None
    except Exception:
        return None


def jwt_seconds_left(token: str | None) -> float | None:
    exp = jwt_exp(token)
    if exp is None:
        return None
    return exp - time.time()


def jwt_needs_refresh(token: str | None, *, skew: int = REFRESH_SKEW_SECONDS) -> bool:
    """True when missing, unreadable, expired, or close to expiry."""
    left = jwt_seconds_left(token)
    if left is None:
        return True
    return left <= skew


def _parse_new_access(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return None
    for key in ("newToken", "access", "accessToken", "token"):
        val = data.get(key)
        if isinstance(val, str) and val.count(".") == 2:
            return val.strip()
    return None


def refresh_buyer_access(refresh: str) -> str | None:
    refresh = (refresh or "").strip()
    if not refresh:
        return None
    try:
        resp = requests.get(
            BUYER_REFRESH_URL,
            headers={
                "Authorization": f"Bearer {refresh}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=20,
        )
    except Exception:
        return None
    if resp.status_code in (401, 403):
        return None
    try:
        return _parse_new_access(resp.json())
    except Exception:
        return None


def refresh_seller_access(refresh: str) -> str | None:
    refresh = (refresh or "").strip()
    if not refresh:
        return None
    try:
        resp = requests.get(
            SELLER_REFRESH_URL,
            headers={
                "Authorization": f"Bearer {refresh}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=20,
        )
    except Exception:
        return None
    if resp.status_code in (401, 403):
        return None
    try:
        return _parse_new_access(resp.json())
    except Exception:
        return None


def usernames_path() -> Path:
    return watcher_data_dir() / "login_usernames.json"


def _password_path(kind: str) -> Path:
    return watcher_data_dir() / f"{kind}_password.dpapi"


def load_saved_username(kind: str) -> str:
    path = usernames_path()
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(data.get(kind) or "").strip()


def _save_username(kind: str, username: str) -> None:
    path = usernames_path()
    data: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            data = {}
    username = (username or "").strip()
    if username:
        data[kind] = username
    else:
        data.pop(kind, None)
    if data:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    elif path.exists():
        path.unlink()


def _dpapi_protect(plain: str) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    raw = plain.encode("utf-8")
    buf = ctypes.create_string_buffer(raw, len(raw))
    blob_in = DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    blob_out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise OSError("Could not protect password on this PC.")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _dpapi_unprotect(blob: bytes) -> str:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    buf = ctypes.create_string_buffer(blob, len(blob))
    blob_in = DATA_BLOB(len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    blob_out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise OSError("Could not read saved password.")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def load_saved_password(kind: str) -> str:
    path = _password_path(kind)
    if not path.exists():
        return ""
    try:
        return _dpapi_unprotect(path.read_bytes()).strip()
    except Exception:
        return ""


def save_login_credentials(kind: str, username: str, password: str | None) -> None:
    """Save username always; password only when provided (DPAPI, this Windows user)."""
    _save_username(kind, username)
    path = _password_path(kind)
    if password:
        path.write_bytes(_dpapi_protect(password))
    elif path.exists():
        path.unlink()


def clear_saved_password(kind: str) -> None:
    path = _password_path(kind)
    if path.exists():
        path.unlink()


class BuyerMfaRequired(RuntimeError):
    """Password was accepted; CTBids wants an email/SMS one-time code on this PC."""

    def __init__(self, mfatoken: str, email: str = "", mobile: str = "") -> None:
        super().__init__("CTBids needs a one-time code on this PC.")
        self.mfatoken = (mfatoken or "").strip()
        self.email = (email or "").strip()
        self.mobile = (mobile or "").strip()


def _btoa(password: str) -> str:
    """Match CTBids website btoa() for the login payload."""
    return base64.b64encode(password.encode("latin-1", errors="replace")).decode("ascii")


def _tokens_from_login_body(payload: object) -> tuple[str | None, str | None]:
    if not isinstance(payload, dict):
        return None, None
    blobs: list[dict] = [payload]
    data = payload.get("data")
    if isinstance(data, dict):
        blobs.append(data)
    access = refresh = None
    for blob in blobs:
        if access is None:
            for key in ("accessToken", "access_token", "access", "token", "newToken"):
                val = blob.get(key)
                if isinstance(val, str) and val.count(".") == 2:
                    access = val.strip()
                    break
        if refresh is None:
            for key in ("refreshToken", "refresh_token", "refresh"):
                val = blob.get(key)
                if isinstance(val, str) and val.count(".") == 2:
                    refresh = val.strip()
                    break
    return access, refresh


def _mfa_from_login_body(payload: object) -> BuyerMfaRequired | None:
    if not isinstance(payload, dict):
        return None
    blobs: list[dict] = [payload]
    data = payload.get("data")
    if isinstance(data, dict):
        blobs.append(data)
    for blob in blobs:
        token = str(blob.get("mfatoken") or blob.get("mfaToken") or "").strip()
        kind = str(blob.get("type") or "").lower()
        if token and (kind == "mfa" or kind == "otp" or blob.get("mobile") or blob.get("email")):
            return BuyerMfaRequired(
                token,
                str(blob.get("email") or ""),
                str(blob.get("mobile") or ""),
            )
    return None


def _post_ctbids(url: str, payload: dict) -> tuple[int, object, str]:
    try:
        resp = requests.post(
            url,
            data=json.dumps(payload),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0",
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Could not reach CTBids: {exc}") from exc
    text = resp.text or ""
    try:
        body: object = resp.json() if resp.content else {}
    except Exception:
        body = {}
    return resp.status_code, body, text


def _mfa_channel(channel: str) -> str:
    raw = (channel or "").strip().lower()
    if raw in ("email", "mail"):
        return "email"
    return "sms"


def send_buyer_mfa_otp(mfatoken: str, channel: str, *, resend: bool = False) -> None:
    token = (mfatoken or "").strip()
    if not token:
        raise RuntimeError("Sign in again — the one-time-code session expired.")
    url = BUYER_MFA_RESEND_URL if resend else BUYER_MFA_SEND_URL
    kind = _mfa_channel(channel)
    tried = [kind]
    if kind == "sms":
        tried.append("mobile")
    last_err = "Could not send the code."
    for type_val in tried:
        status, body, _text = _post_ctbids(url, {"data": {"mfatoken": token, "type": type_val}})
        if status < 400 and isinstance(body, dict) and str(body.get("status") or "").lower() != "failed":
            return
        last_err = _login_error_message_from_body(body, status)
        if "invalid" in last_err.lower() and "type" in last_err.lower():
            continue
        if status < 400:
            continue
        # CBMFA001 / other hard failures — don't keep hammering
        if "CBMFA" in str(body):
            break
    raise RuntimeError(last_err)


def verify_buyer_mfa(mfatoken: str, otp: str, *, trusted: bool = True) -> str:
    token = (mfatoken or "").strip()
    code = "".join(ch for ch in (otp or "") if ch.isalnum())
    if not token:
        raise RuntimeError("Sign in again — the one-time-code session expired.")
    if len(code) < 4:
        raise RuntimeError("Enter the code CTBids sent.")
    payloads = [
        {"data": {"mfatoken": token, "otp": code, "isTrustedMode": bool(trusted)}},
        {"data": {"mfatoken": token, "otp": code}},
        {"data": {"mfatoken": token, "code": code, "isTrustedMode": bool(trusted)}},
    ]
    last_err = "That code was not accepted."
    for payload in payloads:
        status, body, _text = _post_ctbids(BUYER_MFA_VERIFY_URL, payload)
        if status >= 400:
            last_err = _login_error_message_from_body(body, status)
            continue
        access, refresh = _tokens_from_login_body(body)
        if access:
            _save_pair("buyer", access, refresh)
            _password_login_blocked_until.pop("buyer", None)
            return access
        mfa = _mfa_from_login_body(body)
        if mfa:
            last_err = "CTBids still wants a code. Request a new one."
            continue
        last_err = _login_error_message_from_body(body, status)
    raise RuntimeError(last_err)


def _login_error_message_from_body(body: object, status: int) -> str:
    msg = body.get("message") if isinstance(body, dict) else None
    if isinstance(msg, str) and msg.strip():
        text = msg.strip()
        if " : " in text:
            text = text.split(" : ", 1)[1]
        return text
    if isinstance(msg, dict):
        for key in ("CBMSW", "CBMSM"):
            inner = msg.get(key)
            if isinstance(inner, dict) and inner.get("message"):
                return str(inner["message"]).strip()
        for val in msg.values():
            if isinstance(val, dict) and val.get("message"):
                return str(val["message"]).strip()
            if isinstance(val, str) and val.strip():
                return val.strip()
    if status:
        return f"Sign in failed ({status})."
    return "Email or password was not accepted."


def _login_error_message(resp: requests.Response) -> str:
    try:
        body = resp.json()
    except Exception:
        return f"Sign in failed ({resp.status_code})."
    return _login_error_message_from_body(body, resp.status_code)


def login_with_password(kind: str, username: str, password: str) -> str:
    """Sign in via CTBids API. Returns access token. Raises RuntimeError on failure.

    Buyer logins on a new PC often raise ``BuyerMfaRequired`` instead of returning
    a JWT — CTBids wants an email/SMS code first.
    """
    username = (username or "").strip()
    password = password or ""
    if not username or not password:
        raise RuntimeError("Enter both email and password.")
    url = SELLER_LOGIN_URL if kind == "seller" else BUYER_LOGIN_URL
    payload = {
        "data": {
            "username": username,
            "password": _btoa(password),
            "keep-user-logged-in": True,
        }
    }
    try:
        resp = requests.post(
            url,
            data=json.dumps(payload),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0",
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Could not reach CTBids: {exc}") from exc
    if resp.status_code >= 400:
        raise RuntimeError(_login_error_message(resp))
    try:
        body = resp.json() if resp.content else {}
    except Exception as exc:
        raise RuntimeError("CTBids sent an unexpected sign-in response.") from exc
    mfa = _mfa_from_login_body(body)
    if mfa:
        raise mfa
    access, refresh = _tokens_from_login_body(body)
    if not access:
        raise RuntimeError("CTBids signed in but did not return a session token.")
    _save_pair(kind, access, refresh)
    _password_login_blocked_until.pop(kind, None)
    return access


def login_from_saved(kind: str) -> str | None:
    if time.time() < _password_login_blocked_until.get(kind, 0):
        return None
    username = load_saved_username(kind)
    password = load_saved_password(kind)
    if not username or not password:
        return None
    try:
        return login_with_password(kind, username, password)
    except BuyerMfaRequired:
        return None
    except Exception:
        _password_login_blocked_until[kind] = time.time() + 15 * 60
        return None


def _playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed. Run:\n"
            "  pip install playwright\n"
            "  playwright install chromium"
        ) from exc
    return sync_playwright


def _clear_stale_profile_locks(user_data_dir: Path) -> None:
    """Drop Chromium singleton files if no process still holds the profile."""
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        path = user_data_dir / name
        if not path.exists():
            continue
        try:
            path.unlink()
        except OSError:
            pass


def _read_tokens_from_pages(context) -> tuple[str | None, str | None]:
    access = refresh = None
    for page in list(context.pages):
        try:
            pair = page.evaluate(_JS_READ_TOKENS)
        except Exception:
            continue
        if not isinstance(pair, dict):
            continue
        got_a = str(pair.get("access") or "").strip()
        got_r = str(pair.get("refresh") or "").strip()
        if got_a:
            access = got_a
        if got_r:
            refresh = got_r
    return access, refresh


def _launch_persistent(user_data_dir: Path, *, headless: bool):
    sync_playwright = _playwright()
    _clear_stale_profile_locks(user_data_dir)
    pw = sync_playwright().start()
    try:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
    except Exception:
        _clear_stale_profile_locks(user_data_dir)
        try:
            context = pw.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                headless=headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception:
            pw.stop()
            raise
    return pw, context


def _read_tokens_from_profile(
    user_data_dir: Path,
    url: str,
    lock: threading.Lock,
) -> tuple[str | None, str | None]:
    if not lock.acquire(timeout=45):
        return None, None
    try:
        pw, context = _launch_persistent(user_data_dir, headless=True)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_timeout(1200)
            except Exception:
                time.sleep(1.2)
            return _read_tokens_from_pages(context)
        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                pw.stop()
            except Exception:
                pass
    except Exception:
        return None, None
    finally:
        lock.release()


def read_buyer_tokens_from_profile() -> tuple[str | None, str | None]:
    return _read_tokens_from_profile(profile_dir(), "https://ctbids.com/", _buyer_profile_lock)


def read_seller_tokens_from_profile() -> tuple[str | None, str | None]:
    return _read_tokens_from_profile(
        seller_profile_dir(),
        "https://seller.ctbids.com/",
        _seller_profile_lock,
    )


def read_access_token_from_profile() -> str | None:
    access, refresh = read_buyer_tokens_from_profile()
    if access:
        save_access_token(access)
    if refresh:
        save_refresh_token(refresh)
    return access


def read_seller_token_from_profile() -> str | None:
    access, refresh = read_seller_tokens_from_profile()
    if access:
        save_seller_access_token(access)
    if refresh:
        save_seller_refresh_token(refresh)
    return access


def _status_ok(kind: str, access: str | None) -> str:
    """Return 'ok', 'unauthorized', or 'unknown'."""
    if not access:
        return "unauthorized"
    if kind == "seller":
        return SellerClient(access).token_status()
    return CtbidsClient(access).token_status()


def _http_refresh(kind: str, refresh: str | None) -> str | None:
    if not refresh:
        return None
    if kind == "seller":
        return refresh_seller_access(refresh)
    return refresh_buyer_access(refresh)


def _save_pair(kind: str, access: str | None, refresh: str | None) -> None:
    if kind == "seller":
        if access:
            save_seller_access_token(access)
        if refresh:
            save_seller_refresh_token(refresh)
    else:
        if access:
            save_access_token(access)
        if refresh:
            save_refresh_token(refresh)


def _clear_access_only(kind: str) -> None:
    if kind == "seller":
        save_seller_access_token(None)
    else:
        save_access_token(None)


def _load_saved_pair(kind: str) -> tuple[str | None, str | None]:
    if kind == "seller":
        return load_saved_seller_access_token(), load_saved_seller_refresh_token()
    return load_saved_access_token(), load_saved_refresh_token()


def _ensure_kind(
    kind: str,
    *,
    validate: bool = True,
    allow_browser: bool = True,
) -> str | None:
    access, refresh = _load_saved_pair(kind)

    if access and not jwt_needs_refresh(access):
        if not validate:
            return access
        status = _status_ok(kind, access)
        if status == "ok":
            return access
        if status == "unknown":
            return access  # keep green through a network blip
        # unauthorized — fall through to refresh

    if refresh:
        fresh = _http_refresh(kind, refresh)
        if fresh:
            _save_pair(kind, fresh, refresh)
            if not validate:
                return fresh
            status = _status_ok(kind, fresh)
            if status in ("ok", "unknown"):
                return fresh

    saved_login = login_from_saved(kind)
    if saved_login:
        if not validate:
            return saved_login
        status = _status_ok(kind, saved_login)
        if status in ("ok", "unknown"):
            return saved_login

    if kind == "seller":
        try:
            from estate_ez import pull_seller_from_estate_ez
        except ImportError:
            from sale_watcher.estate_ez import pull_seller_from_estate_ez
        try:
            ee_access = pull_seller_from_estate_ez()
        except Exception:
            ee_access = None
        if ee_access:
            if not validate:
                return ee_access
            status = _status_ok(kind, ee_access)
            if status in ("ok", "unknown"):
                return ee_access

    if allow_browser:
        try:
            if kind == "seller":
                prof_access, prof_refresh = read_seller_tokens_from_profile()
            else:
                prof_access, prof_refresh = read_buyer_tokens_from_profile()
        except Exception:
            prof_access, prof_refresh = None, None
        if prof_refresh:
            refresh = prof_refresh
            _save_pair(kind, None, refresh)
        if prof_access:
            access = prof_access
            _save_pair(kind, access, refresh)
        if refresh:
            fresh = _http_refresh(kind, refresh)
            if fresh:
                access = fresh
                _save_pair(kind, access, refresh)
        if access:
            if not validate:
                return access
            status = _status_ok(kind, access)
            if status in ("ok", "unknown"):
                return access

    # Access is dead; keep the refresh cookie/file so the next attempt can recover.
    if access:
        status = _status_ok(kind, access)
        if status == "unauthorized":
            _clear_access_only(kind)
    return None


def resolve_access_token(*, validate: bool = True, allow_browser: bool = True) -> str | None:
    return _ensure_kind("buyer", validate=validate, allow_browser=allow_browser)


def resolve_seller_access_token(*, validate: bool = True, allow_browser: bool = True) -> str | None:
    return _ensure_kind("seller", validate=validate, allow_browser=allow_browser)


def keep_sessions_fresh() -> tuple[str | None, str | None]:
    """HTTP-only refresh of buyer + seller access tokens. No browser."""
    buyer = resolve_access_token(validate=False, allow_browser=False)
    seller = resolve_seller_access_token(validate=False, allow_browser=False)
    return buyer, seller


def _wait_for_close_and_capture(context, *, on_tokens: Callable[[str], None] | None) -> tuple[str | None, str | None]:
    access = refresh = None
    announced = False
    deadline = time.time() + 15 * 60
    while time.time() < deadline:
        pages = list(context.pages)
        if not pages:
            break
        got_a, got_r = _read_tokens_from_pages(context)
        if got_a:
            access = got_a
        if got_r:
            refresh = got_r
        if access and on_tokens and not announced:
            announced = True
            try:
                on_tokens(access)
            except Exception:
                pass
        try:
            pages[0].wait_for_timeout(800)
        except Exception:
            time.sleep(0.8)
    # One last pass in case the last navigation wrote tokens as the window closed
    got_a, got_r = _read_tokens_from_pages(context)
    if got_a:
        access = got_a
    if got_r:
        refresh = got_r
    return access, refresh


def open_login_window(
    sale_url: str = "https://ctbids.com/",
    on_tokens: Callable[[str], None] | None = None,
) -> str | None:
    """Open a visible Chromium window so staff can sign in as buyer."""
    target = sale_url.strip() or "https://ctbids.com/"
    if not _buyer_profile_lock.acquire(timeout=90):
        raise RuntimeError("Buyer browser profile is busy. Close the other CTBids window and try again.")
    try:
        pw, context = _launch_persistent(profile_dir(), headless=False)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(target, wait_until="domcontentloaded", timeout=90000)
            access, refresh = _wait_for_close_and_capture(context, on_tokens=on_tokens)
        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                pw.stop()
            except Exception:
                pass
    finally:
        _buyer_profile_lock.release()

    if refresh:
        save_refresh_token(refresh)
    if not access and refresh:
        access = refresh_buyer_access(refresh)
    if access:
        save_access_token(access)
        status = CtbidsClient(access).token_status()
        if status == "unauthorized" and refresh:
            access = refresh_buyer_access(refresh)
            if access:
                save_access_token(access)
                status = CtbidsClient(access).token_status()
        if status != "unauthorized":
            return access
        save_access_token(None)
        return None
    return None


def open_seller_login_window(on_tokens: Callable[[str], None] | None = None) -> str | None:
    """Open seller.ctbids.com so staff can sign in."""
    if not _seller_profile_lock.acquire(timeout=90):
        raise RuntimeError("Seller browser profile is busy. Close the other CT Seller window and try again.")
    try:
        pw, context = _launch_persistent(seller_profile_dir(), headless=False)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://seller.ctbids.com/", wait_until="domcontentloaded", timeout=90000)
            access, refresh = _wait_for_close_and_capture(context, on_tokens=on_tokens)
        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                pw.stop()
            except Exception:
                pass
    finally:
        _seller_profile_lock.release()

    if refresh:
        save_seller_refresh_token(refresh)
    if not access and refresh:
        access = refresh_seller_access(refresh)
    if access:
        save_seller_access_token(access)
        status = SellerClient(access).token_status()
        if status == "unauthorized" and refresh:
            access = refresh_seller_access(refresh)
            if access:
                save_seller_access_token(access)
                status = SellerClient(access).token_status()
        if status != "unauthorized":
            return access
        save_seller_access_token(None)
        return None
    return None
