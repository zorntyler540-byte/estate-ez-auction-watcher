"""Retry transient HTTP failures with exponential backoff and jitter.

Only network errors, timeouts, 429 and 5xx are retried. 401 / 403 and every
other 4xx go straight back to the caller, which owns auth handling (token
clearing, login blocks, mint cooldowns).
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable
from urllib.parse import urlsplit

import requests

log = logging.getLogger("sale_watcher.http")


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 3  # total tries, including the first
    base_delay: float = 0.5
    max_delay: float = 8.0
    # A server asking for a longer wait than this is handed back to the caller
    # (e.g. the Estate-Ez mint cooldown) instead of blocking a worker thread.
    max_retry_after: float = 30.0
    retry_429: bool = True
    retry_5xx: bool = True
    # "all": any connection error or timeout.
    # "connect": only failures where the request never reached the server.
    network: str = "all"


DEFAULT = RetryPolicy()
# Token checks run while the UI waits; keep them short.
QUICK = RetryPolicy(attempts=2, max_delay=2.0)
# Writes (suspend, unsuspend, retract): a read timeout or 5xx may mean the
# server already acted, so only retry when it provably never saw the request.
WRITE = RetryPolicy(retry_5xx=False, network="connect")
# Estate-Ez CT Seller mint: its own cooldown owns 429 / 5xx.
CONNECT_ONLY = RetryPolicy(retry_429=False, retry_5xx=False, network="connect")
NO_RETRY = RetryPolicy(attempts=1)


def parse_retry_after(
    resp: requests.Response | None,
    *,
    now: float | None = None,
    cap: float | None = None,
) -> float | None:
    """Seconds from a Retry-After header (delta-seconds or HTTP-date). None if absent or unreadable."""
    if resp is None:
        return None
    raw = str((resp.headers or {}).get("Retry-After") or "").strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError, IndexError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        current = datetime.fromtimestamp(time.time() if now is None else now, tz=timezone.utc)
        seconds = (when - current).total_seconds()
    seconds = max(0.0, seconds)
    if cap is not None:
        seconds = min(seconds, float(cap))
    return seconds


def backoff_delay(retry_index: int, policy: RetryPolicy, rng: random.Random | None = None) -> float:
    """Equal-jitter exponential backoff: half the step is fixed, half is random."""
    step = min(policy.max_delay, policy.base_delay * (2 ** retry_index))
    rand = (rng or random).uniform(0.0, step / 2.0)
    return step / 2.0 + rand


def describe_url(method: str, url: str) -> str:
    """``POST host/path`` — never the query string, which can carry ids or keys."""
    parts = urlsplit(url)
    return f"{method.upper()} {parts.netloc}{parts.path}"


def _exception_chain(exc: BaseException):
    seen: set[int] = set()
    stack: list[object] = [exc]
    while stack:
        cur = stack.pop()
        if cur is None or id(cur) in seen or len(seen) > 12:
            continue
        seen.add(id(cur))
        yield cur
        if isinstance(cur, BaseException):
            stack.extend([cur.__cause__, cur.__context__, *cur.args])
        reason = getattr(cur, "reason", None)
        if reason is not None:
            stack.append(reason)


def request_never_sent(exc: BaseException) -> bool:
    """True when the connection itself failed, so the server cannot have acted on the request."""
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return True
    if not isinstance(exc, requests.exceptions.ConnectionError) or isinstance(exc, requests.exceptions.SSLError):
        return False
    try:
        from urllib3.exceptions import ConnectTimeoutError, NewConnectionError
    except ImportError:  # pragma: no cover - urllib3 ships with requests
        return False
    return any(isinstance(link, (NewConnectionError, ConnectTimeoutError)) for link in _exception_chain(exc))


def _retryable_exception(exc: requests.RequestException, policy: RetryPolicy) -> bool:
    if policy.network == "connect":
        return request_never_sent(exc)
    if isinstance(exc, requests.exceptions.SSLError):
        return False
    return isinstance(
        exc,
        (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ),
    )


def _retryable_status(status: int, policy: RetryPolicy) -> bool:
    if status == 429:
        return policy.retry_429
    if 500 <= status <= 599:
        return policy.retry_5xx
    return False


def _release(resp: requests.Response) -> None:
    try:
        resp.close()
    except Exception:  # noqa: BLE001 - best effort; a test double may have no socket
        pass


def request_with_retry(
    send: Callable[[], requests.Response],
    *,
    policy: RetryPolicy = DEFAULT,
    label: str = "HTTP request",
    sleep: Callable[[float], None] | None = None,
    rng: random.Random | None = None,
) -> requests.Response:
    """Call ``send()`` until it succeeds, fails for good, or attempts run out.

    Returns the last response (any status) so callers keep their own status
    handling. Re-raises the last network error when every attempt failed.
    """
    attempts = max(1, int(policy.attempts))
    pause = sleep or time.sleep
    for attempt in range(attempts):
        last = attempt == attempts - 1
        try:
            resp = send()
        except requests.RequestException as exc:
            if last or not _retryable_exception(exc, policy):
                if attempt:
                    log.warning("%s failed after %d attempts: %s", label, attempt + 1, type(exc).__name__)
                raise
            delay = backoff_delay(attempt, policy, rng)
            log.warning(
                "%s: %s; retry %d of %d in %.1fs",
                label, type(exc).__name__, attempt + 1, attempts - 1, delay,
            )
            pause(delay)
            continue

        status = int(getattr(resp, "status_code", 0) or 0)
        if last or not _retryable_status(status, policy):
            if attempt and status >= 400:
                log.warning("%s still HTTP %s after %d attempts", label, status, attempt + 1)
            return resp
        server_wait = parse_retry_after(resp)
        if server_wait is not None and server_wait > policy.max_retry_after:
            log.warning("%s: HTTP %s with Retry-After %.0fs; not waiting here", label, status, server_wait)
            return resp
        delay = max(server_wait or 0.0, backoff_delay(attempt, policy, rng))
        log.warning(
            "%s: HTTP %s; retry %d of %d in %.1fs",
            label, status, attempt + 1, attempts - 1, delay,
        )
        _release(resp)
        pause(delay)
    raise AssertionError("unreachable")  # pragma: no cover
