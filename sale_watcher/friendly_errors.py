"""Turn exceptions into short messages staff can act on. Raw details go to the log, not the screen."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

import requests

LOG_HINT = "Details were saved to the Watcher log (Logs, bottom right)."

_RAW_LOOKING = re.compile(
    r"HTTPS?ConnectionPool|Max retries exceeded|Traceback|urllib3|NewConnectionError|"
    r"<[A-Za-z_.]+ object at 0x|\[Errno|\[WinError|Expecting value: line",
)


def service_name(url: str | None) -> str:
    host = urlsplit(url or "").netloc.lower()
    if not host:
        return "the server"
    if host.startswith("seller."):
        return "CT Seller"
    if host == "ctbids.com" or host.endswith(".ctbids.com"):
        return "CTBids"
    return "Estate-Ez"


def _service_for(exc: BaseException, default: str) -> str:
    request = getattr(exc, "request", None)
    url = getattr(request, "url", None) if request is not None else None
    if not url:
        response = getattr(exc, "response", None)
        url = getattr(response, "url", None) if response is not None else None
    return service_name(url) if url else default


def _status_message(service: str, status: int) -> str:
    if status in (401, 403):
        return f"{service} sign-in expired or was refused. Sign in again."
    if status == 404:
        return f"{service} could not find that (error 404)."
    if status == 429:
        return f"{service} asked the Watcher to slow down. It will try again shortly."
    if 500 <= status <= 599:
        return f"{service} is having trouble right now (error {status}). Try again in a few minutes."
    return f"{service} returned an error ({status})."


def looks_friendly(text: str) -> bool:
    text = (text or "").strip()
    return bool(text) and len(text) <= 300 and not _RAW_LOOKING.search(text)


def friendly_error(exc: BaseException | str | None, *, service: str = "the server") -> str:
    """One or two plain sentences for a status line or message box."""
    if exc is None:
        return "Something went wrong. " + LOG_HINT
    if isinstance(exc, str):
        return exc.strip() if looks_friendly(exc) else "Something went wrong. " + LOG_HINT

    name = _service_for(exc, service)
    if isinstance(exc, requests.exceptions.SSLError):
        return f"A secure connection to {name} failed. Check this PC's date, time and network."
    if isinstance(exc, (requests.exceptions.ConnectTimeout, requests.exceptions.ConnectionError)):
        return f"Could not reach {name}. Check this PC's internet connection and try again."
    if isinstance(exc, requests.exceptions.Timeout):
        return f"{name} took too long to answer. The Watcher will try again on the next refresh."
    if isinstance(exc, requests.exceptions.HTTPError):
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status:
            return _status_message(name, int(status))
    if isinstance(exc, requests.exceptions.JSONDecodeError):
        return f"{name} sent an unexpected response. Try again in a minute."
    if isinstance(exc, requests.RequestException):
        return f"Could not talk to {name}. Try again in a minute."

    text = str(exc).strip()
    if isinstance(exc, (PermissionError, RuntimeError, ValueError)) and looks_friendly(text):
        return text
    if isinstance(exc, OSError) and getattr(exc, "strerror", None):
        where = f" ({exc.filename})" if getattr(exc, "filename", None) else ""
        return f"{exc.strerror}{where}."
    return f"Something went wrong ({type(exc).__name__}). {LOG_HINT}"
