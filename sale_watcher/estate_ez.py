"""Estate-Ez website login + CT Seller session for Watcher.

Staff sign in with the same Estate-Ez account used on the website. The site
already stores CT Seller on the account; Watcher asks for a short-lived seller
JWT (never the CT Seller password).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import requests

try:
    from http_retry import CONNECT_ONLY, DEFAULT, parse_retry_after, request_with_retry
except ImportError:  # noqa: BLE001 - package vs script import
    from sale_watcher.http_retry import CONNECT_ONLY, DEFAULT, parse_retry_after, request_with_retry

try:
    from auth import (
        clear_secret_if_matches,
        jwt_needs_refresh,
        load_saved_password,
        load_saved_seller_access_token,
        load_saved_username,
        read_secret,
        save_login_credentials,
        save_seller_access_token,
        save_seller_refresh_token,
        watcher_data_dir,
        write_secret,
    )
except ImportError:  # noqa: BLE001 - package vs script import
    from sale_watcher.auth import (
        clear_secret_if_matches,
        jwt_needs_refresh,
        load_saved_password,
        load_saved_seller_access_token,
        load_saved_username,
        read_secret,
        save_login_credentials,
        save_seller_access_token,
        save_seller_refresh_token,
        watcher_data_dir,
        write_secret,
    )

log = logging.getLogger("sale_watcher.estate_ez")

DEFAULT_API_BASE = "https://caringtransitions-ai-production.up.railway.app"
LOGIN_KIND = "estateez"
TIMEOUT = 30
LOGIN_BLOCK_SECONDS = 15 * 60
MINT_COOLDOWN_SECONDS = 5 * 60
MAX_RETRY_AFTER_SECONDS = 6 * 60 * 60

UNREACHABLE_MESSAGE = (
    "Could not reach Estate-Ez. Check this PC's internet connection and try again."
)

MINT_UNAVAILABLE_MESSAGE = (
    "Estate-Ez signed in, but this Estate-Ez account can't load a CT Seller session. "
    "Use Seller sign in for unpaid checks."
)

_state_lock = threading.Lock()
_login_lock = threading.Lock()
_mint_lock = threading.Lock()
_login_blocked_until = 0.0
_mint_cooldown_until = 0.0
_mint_cooldown_from_server = False
_mint_unavailable_detail: str | None = None
_mint_unavailable_account = ""


class EstateEzAuthError(RuntimeError):
    """Estate-Ez email/password failed or the session expired."""


class EstateEzSellerNotConnected(RuntimeError):
    """Estate-Ez account has no CT Seller credentials on Account."""


class EstateEzMintUnavailable(RuntimeError):
    """Estate-Ez answered 403 to the CT Seller session mint. The Estate-Ez login is still good."""


class EstateEzMintCoolingDown(RuntimeError):
    """Estate-Ez asked Watcher to wait before minting another CT Seller session."""

    def __init__(self, seconds_left: float, message: str = "") -> None:
        self.seconds_left = max(0.0, float(seconds_left))
        minutes = max(1, int(round(self.seconds_left / 60.0)))
        super().__init__(
            message
            or f"Estate-Ez asked Watcher to wait about {minutes} min before loading CT Seller again."
        )


def watcher_config() -> dict[str, Any]:
    cfg = watcher_data_dir() / "watcher_config.json"
    if not cfg.exists():
        return {}
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def api_base() -> str:
    env = (os.environ.get("ESTATE_EZ_API_BASE") or "").strip().rstrip("/")
    if env:
        return env
    custom = str(watcher_config().get("estate_ez_api") or "").strip().rstrip("/")
    if custom:
        return custom
    return DEFAULT_API_BASE


def estate_ez_token_path() -> Path:
    return watcher_data_dir() / "estate_ez_access_token.txt"


def save_estate_ez_token(token: str | None) -> None:
    write_secret(estate_ez_token_path(), token)


def clear_estate_ez_token_if(failed: str | None) -> bool:
    """Drop the saved Estate-Ez JWT only if it is still the one that just failed."""
    return clear_secret_if_matches(estate_ez_token_path(), failed)


def load_estate_ez_token() -> str | None:
    return read_secret(estate_ez_token_path())


def _detail(resp: requests.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
        if isinstance(detail, list) and detail:
            first = detail[0]
            if isinstance(first, dict):
                msg = first.get("msg") or first.get("message")
                if msg:
                    return str(msg)
            return str(first)
    return f"Estate-Ez error ({resp.status_code})"


def retry_after_seconds(resp: requests.Response | None, *, now: float | None = None) -> float | None:
    """Seconds from a Retry-After header (delta-seconds or HTTP-date), clamped. None if absent."""
    return parse_retry_after(resp, now=now, cap=MAX_RETRY_AFTER_SECONDS)


def _start_mint_cooldown(resp: requests.Response | None) -> float:
    global _mint_cooldown_until, _mint_cooldown_from_server
    server = retry_after_seconds(resp)
    seconds = server if server is not None else float(MINT_COOLDOWN_SECONDS)
    with _state_lock:
        _mint_cooldown_until = time.time() + seconds
        _mint_cooldown_from_server = server is not None
    return seconds


def _clear_mint_cooldown() -> None:
    global _mint_cooldown_until, _mint_cooldown_from_server
    with _state_lock:
        _mint_cooldown_until = 0.0
        _mint_cooldown_from_server = False


def mint_cooldown_left(*, server_only: bool = False) -> float:
    with _state_lock:
        if server_only and not _mint_cooldown_from_server:
            return 0.0
        return max(0.0, _mint_cooldown_until - time.time())


def _disable_minting(detail: str) -> None:
    global _mint_unavailable_detail, _mint_unavailable_account
    with _state_lock:
        _mint_unavailable_detail = detail or ""
        _mint_unavailable_account = (load_saved_username(LOGIN_KIND) or "").strip().lower()


def minting_unavailable() -> bool:
    with _state_lock:
        return _mint_unavailable_detail is not None


def _reset_minting_for_account(email: str) -> None:
    """A different Estate-Ez account signed in, so the earlier 403 no longer applies."""
    global _mint_unavailable_detail, _mint_unavailable_account
    with _state_lock:
        if _mint_unavailable_detail is None:
            return
        if (email or "").strip().lower() != _mint_unavailable_account:
            _mint_unavailable_detail = None
            _mint_unavailable_account = ""


def login_blocked_seconds_left() -> float:
    with _state_lock:
        return max(0.0, _login_blocked_until - time.time())


def _set_login_block(seconds: float) -> None:
    global _login_blocked_until
    with _state_lock:
        _login_blocked_until = time.time() + seconds if seconds > 0 else 0.0


def login_estate_ez(email: str, password: str) -> str:
    """POST /auth/login. Returns the Estate-Ez JWT."""
    user = (email or "").strip()
    if not user or not password:
        raise EstateEzAuthError("Enter your Estate-Ez email and password.")
    url = f"{api_base()}/auth/login"
    try:
        resp = request_with_retry(
            lambda: requests.post(
                url,
                json={"email": user, "password": password},
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                timeout=TIMEOUT,
            ),
            policy=DEFAULT,
            label="Estate-Ez POST /auth/login",
        )
    except requests.RequestException as exc:
        log.warning("Estate-Ez login could not connect: %s", type(exc).__name__)
        raise EstateEzAuthError(UNREACHABLE_MESSAGE) from exc
    if resp.status_code >= 400:
        log.warning("Estate-Ez login refused (HTTP %s)", resp.status_code)
    if resp.status_code == 401:
        raise EstateEzAuthError("Invalid Estate-Ez login.")
    if resp.status_code == 403:
        raise EstateEzAuthError(_detail(resp) or "This Estate-Ez account is inactive.")
    if resp.status_code >= 400:
        raise EstateEzAuthError(_detail(resp))
    try:
        body = resp.json()
    except ValueError as exc:
        raise EstateEzAuthError("Estate-Ez signed in but did not return a session.") from exc
    token = str(body.get("access_token") or "").strip()
    if not token:
        raise EstateEzAuthError("Estate-Ez signed in but did not return a session.")
    save_estate_ez_token(token)
    _set_login_block(0)
    log.info("Estate-Ez signed in")
    return token


def _mint_gate(*, interactive: bool) -> None:
    """Raise if minting is off for this run or cooling down."""
    with _state_lock:
        detail = _mint_unavailable_detail
    if detail is not None:
        raise EstateEzMintUnavailable(detail or MINT_UNAVAILABLE_MESSAGE)
    # Staff clicking Sign in skip our own back-off, but never a server Retry-After.
    left = mint_cooldown_left(server_only=interactive)
    if left > 0:
        raise EstateEzMintCoolingDown(left)


def fetch_ctseller_session(ee_token: str, *, interactive: bool = False) -> dict[str, str]:
    """POST /integrations/ctseller/session. Returns seller access + refresh."""
    token = (ee_token or "").strip()
    if not token:
        raise EstateEzAuthError("Sign in to Estate-Ez first.")
    with _mint_lock:
        _mint_gate(interactive=interactive)
        url = f"{api_base()}/integrations/ctseller/session"
        try:
            resp = request_with_retry(
                lambda: requests.post(
                    url,
                    headers={
                        "Accept": "application/json",
                        "Authorization": f"Bearer {token}",
                    },
                    timeout=TIMEOUT,
                ),
                policy=CONNECT_ONLY,
                label="Estate-Ez POST /integrations/ctseller/session",
            )
        except requests.RequestException as exc:
            seconds = _start_mint_cooldown(None)
            log.warning("CT Seller mint could not connect (%s); cooling down %.0fs", type(exc).__name__, seconds)
            raise EstateEzAuthError(UNREACHABLE_MESSAGE) from exc
        if resp.status_code >= 400:
            log.warning("CT Seller mint refused (HTTP %s)", resp.status_code)
        if resp.status_code == 401:
            clear_estate_ez_token_if(token)
            raise EstateEzAuthError(_detail(resp) or "Estate-Ez session expired. Sign in again.")
        if resp.status_code == 403:
            detail = _detail(resp)
            _disable_minting(detail)
            raise EstateEzMintUnavailable(detail or MINT_UNAVAILABLE_MESSAGE)
        if resp.status_code == 400:
            _start_mint_cooldown(resp)
            raise EstateEzSellerNotConnected(
                _detail(resp)
                or "Connect CT Seller on Estate-Ez → Account first."
            )
        if resp.status_code == 429:
            seconds = _start_mint_cooldown(resp)
            raise EstateEzMintCoolingDown(seconds)
        if resp.status_code >= 400:
            _start_mint_cooldown(resp)
            raise EstateEzAuthError(_detail(resp))
        try:
            body = resp.json()
        except ValueError as exc:
            _start_mint_cooldown(None)
            raise EstateEzAuthError("Estate-Ez did not return a CT Seller session.") from exc
        access = str(body.get("access_token") or "").strip()
        refresh = str(body.get("refresh_token") or "").strip()
        if not access:
            _start_mint_cooldown(None)
            raise EstateEzAuthError("Estate-Ez did not return a CT Seller session.")
        _clear_mint_cooldown()
        save_seller_access_token(access)
        if refresh:
            save_seller_refresh_token(refresh)
        log.info("CT Seller session loaded from Estate-Ez")
        return {
            "access_token": access,
            "refresh_token": refresh,
            "username": str(body.get("username") or "").strip(),
        }


def login_estate_ez_from_saved() -> str | None:
    if login_blocked_seconds_left() > 0:
        return None
    email = load_saved_username(LOGIN_KIND)
    password = load_saved_password(LOGIN_KIND)
    if not email or not password:
        return None
    try:
        return login_estate_ez(email, password)
    except EstateEzAuthError:
        _set_login_block(LOGIN_BLOCK_SECONDS)
        log.warning("Saved Estate-Ez login failed; not retrying for %d min", LOGIN_BLOCK_SECONDS // 60)
        return None


def ensure_estate_ez_token() -> str | None:
    token = load_estate_ez_token()
    if token and not jwt_needs_refresh(token):
        return token
    with _login_lock:
        # Another thread may have signed in while this one waited.
        token = load_estate_ez_token()
        if token and not jwt_needs_refresh(token):
            return token
        return login_estate_ez_from_saved()


def pull_seller_from_estate_ez(rejected: frozenset[str] | set[str] = frozenset()) -> str | None:
    """Quiet recovery: use a saved Estate-Ez session to mint CT Seller tokens.

    ``rejected`` holds seller tokens CT Seller just refused, so a revoked token
    with time left on it does not block a mint.
    """
    current = load_saved_seller_access_token()
    if current and current not in rejected and not jwt_needs_refresh(current):
        return current
    if minting_unavailable() or mint_cooldown_left() > 0:
        return None
    token = ensure_estate_ez_token()
    if not token:
        return None
    try:
        session = fetch_ctseller_session(token)
    except (
        EstateEzAuthError,
        EstateEzSellerNotConnected,
        EstateEzMintUnavailable,
        EstateEzMintCoolingDown,
    ) as exc:
        log.info("Quiet CT Seller recovery skipped: %s", type(exc).__name__)
        return None
    return session.get("access_token") or None


def sign_in_estate_ez(
    email: str,
    password: str,
    *,
    remember: bool = True,
) -> dict[str, Any]:
    """Interactive sign-in: save EE JWT, then load CT Seller from the account.

    Returns ``estate_ez_token``, ``seller`` (None when the account can't mint or
    Estate-Ez asked us to wait) and ``message`` for the status line.
    """
    token = login_estate_ez(email, password)
    _reset_minting_for_account(email)
    if remember:
        save_login_credentials(LOGIN_KIND, email, password)
    else:
        save_login_credentials(LOGIN_KIND, email, None)
    try:
        seller = fetch_ctseller_session(token, interactive=True)
    except EstateEzMintUnavailable as exc:
        detail = str(exc).strip()
        message = MINT_UNAVAILABLE_MESSAGE
        if detail and detail != MINT_UNAVAILABLE_MESSAGE and not detail.startswith("Estate-Ez error ("):
            message = f"{MINT_UNAVAILABLE_MESSAGE} ({detail})"
        return {"estate_ez_token": token, "seller": None, "message": message}
    except EstateEzMintCoolingDown as exc:
        return {
            "estate_ez_token": token,
            "seller": None,
            "message": f"Estate-Ez signed in. {exc}",
        }
    return {
        "estate_ez_token": token,
        "seller": seller,
        "message": "Estate-Ez signed in. CT Seller session loaded from your account.",
    }
