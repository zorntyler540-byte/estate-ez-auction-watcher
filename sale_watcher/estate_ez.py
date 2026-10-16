"""Estate-Ez website login + CT Seller session for Watcher.

Staff sign in with the same Estate-Ez account used on the website. The site
already stores CT Seller on the account; Watcher asks for a short-lived seller
JWT (never the CT Seller password).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import requests

try:
    from auth import (
        jwt_needs_refresh,
        load_saved_password,
        load_saved_username,
        save_login_credentials,
        save_seller_access_token,
        save_seller_refresh_token,
        watcher_data_dir,
    )
except ImportError:  # noqa: BLE001 - package vs script import
    from sale_watcher.auth import (
        jwt_needs_refresh,
        load_saved_password,
        load_saved_username,
        save_login_credentials,
        save_seller_access_token,
        save_seller_refresh_token,
        watcher_data_dir,
    )

DEFAULT_API_BASE = "https://caringtransitions-ai-production.up.railway.app"
LOGIN_KIND = "estateez"
TIMEOUT = 30


class EstateEzAuthError(RuntimeError):
    """Estate-Ez email/password failed or the session expired."""


class EstateEzSellerNotConnected(RuntimeError):
    """Estate-Ez account has no CT Seller credentials on Account."""


def api_base() -> str:
    env = (os.environ.get("ESTATE_EZ_API_BASE") or "").strip().rstrip("/")
    if env:
        return env
    cfg = watcher_data_dir() / "watcher_config.json"
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            custom = str(data.get("estate_ez_api") or "").strip().rstrip("/")
            if custom:
                return custom
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    return DEFAULT_API_BASE


def estate_ez_token_path() -> Path:
    return watcher_data_dir() / "estate_ez_access_token.txt"


def save_estate_ez_token(token: str | None) -> None:
    path = estate_ez_token_path()
    if token:
        path.write_text(token.strip(), encoding="utf-8")
    elif path.exists():
        path.unlink()


def load_estate_ez_token() -> str | None:
    path = estate_ez_token_path()
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


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


def login_estate_ez(email: str, password: str) -> str:
    """POST /auth/login. Returns the Estate-Ez JWT."""
    user = (email or "").strip()
    if not user or not password:
        raise EstateEzAuthError("Enter your Estate-Ez email and password.")
    try:
        resp = requests.post(
            f"{api_base()}/auth/login",
            json={"email": user, "password": password},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise EstateEzAuthError(f"Could not reach Estate-Ez ({exc}).") from exc
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
    return token


def fetch_ctseller_session(ee_token: str) -> dict[str, str]:
    """POST /integrations/ctseller/session. Returns seller access + refresh."""
    token = (ee_token or "").strip()
    if not token:
        raise EstateEzAuthError("Sign in to Estate-Ez first.")
    try:
        resp = requests.post(
            f"{api_base()}/integrations/ctseller/session",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise EstateEzAuthError(f"Could not reach Estate-Ez ({exc}).") from exc
    if resp.status_code in (401, 403):
        save_estate_ez_token(None)
        raise EstateEzAuthError(_detail(resp) or "Estate-Ez session expired. Sign in again.")
    if resp.status_code == 400:
        raise EstateEzSellerNotConnected(
            _detail(resp)
            or "Connect CT Seller on Estate-Ez → Account first."
        )
    if resp.status_code >= 400:
        raise EstateEzAuthError(_detail(resp))
    try:
        body = resp.json()
    except ValueError as exc:
        raise EstateEzAuthError("Estate-Ez did not return a CT Seller session.") from exc
    access = str(body.get("access_token") or "").strip()
    refresh = str(body.get("refresh_token") or "").strip()
    if not access:
        raise EstateEzAuthError("Estate-Ez did not return a CT Seller session.")
    save_seller_access_token(access)
    if refresh:
        save_seller_refresh_token(refresh)
    return {
        "access_token": access,
        "refresh_token": refresh,
        "username": str(body.get("username") or "").strip(),
    }


def login_estate_ez_from_saved() -> str | None:
    email = load_saved_username(LOGIN_KIND)
    password = load_saved_password(LOGIN_KIND)
    if not email or not password:
        return None
    try:
        return login_estate_ez(email, password)
    except EstateEzAuthError:
        return None


def ensure_estate_ez_token() -> str | None:
    token = load_estate_ez_token()
    if token and not jwt_needs_refresh(token):
        return token
    return login_estate_ez_from_saved()


def pull_seller_from_estate_ez() -> str | None:
    """Quiet recovery: use a saved Estate-Ez session to mint CT Seller tokens."""
    token = ensure_estate_ez_token()
    if not token:
        return None
    try:
        session = fetch_ctseller_session(token)
    except (EstateEzAuthError, EstateEzSellerNotConnected):
        return None
    return session.get("access_token") or None


def sign_in_estate_ez(
    email: str,
    password: str,
    *,
    remember: bool = True,
) -> dict[str, Any]:
    """Interactive sign-in: save EE JWT, then load CT Seller from the account."""
    token = login_estate_ez(email, password)
    if remember:
        save_login_credentials(LOGIN_KIND, email, password)
    else:
        save_login_credentials(LOGIN_KIND, email, None)
    seller = fetch_ctseller_session(token)
    return {"estate_ez_token": token, "seller": seller}
