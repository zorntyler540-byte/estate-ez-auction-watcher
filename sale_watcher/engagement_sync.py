"""Send a PII-free live snapshot to Estate-EZ after each Watcher refresh.

Bidder names, buyer ids, emails, phones, addresses, and invoices stay on the PC.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import requests

try:
    from ctbids_client import FetchReport, LiveItem, Snapshot, buyer_sale_url
except ImportError:  # noqa: BLE001 - package vs script import
    from sale_watcher.ctbids_client import FetchReport, LiveItem, Snapshot, buyer_sale_url


def _estate_ez():
    try:
        import estate_ez
    except ImportError:  # noqa: BLE001 - package vs script import
        from sale_watcher import estate_ez
    return estate_ez

TIMEOUT = 45

# Server builds before the `complete` field reject unknown keys (extra=forbid),
# so it is only sent when one of these is turned on.
SEND_COMPLETE_ENV = "ESTATE_EZ_SEND_COMPLETE"
SEND_COMPLETE_CONFIG_KEY = "engagement_send_complete"

# Keys that must never appear on a snapshot item, even if a seller row has them.
FORBIDDEN_ITEM_KEYS = frozenset(
    {
        "high_bidder",
        "buyer_id",
        "email",
        "firstname",
        "lastname",
        "nickname",
        "phone",
        "address",
        "winner",
        "invoicenumber",
        "invoicestatus",
        "membership_completed",
        "suspended",
        "unpaid_invoice_count",
        "paid_invoice_count",
        "void_invoice_count",
    }
)

ALLOWED_ITEM_KEYS = frozenset(
    {
        "ctseller_item_id",
        "sku",
        "title",
        "views",
        "watchers",
        "bids",
        "price",
        "reserve",
        "item_url",
        "item_status",
    }
)

_sent_lock = threading.Lock()
_sale_locks: dict[int, threading.Lock] = {}
_last_sent_at: dict[int, datetime] = {}


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def send_complete_enabled() -> bool:
    """Off unless ESTATE_EZ_SEND_COMPLETE or watcher_config.json turns it on."""
    env = os.environ.get(SEND_COMPLETE_ENV)
    if env is not None and env.strip():
        return _truthy(env)
    try:
        cfg = _estate_ez().watcher_config()
    except Exception:  # noqa: BLE001 - a bad config file must not stop Live Watch
        return False
    return _truthy(cfg.get(SEND_COMPLETE_CONFIG_KEY))


def _count(value: Any) -> int | None:
    """Non-negative count, or None when CT Seller left it blank."""
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in ("", "none", "null", "nan"):
        return None
    try:
        return max(0, int(float(text)))
    except (TypeError, ValueError):
        return None


def _attention(row: dict[str, Any]) -> tuple[int | None, int | None]:
    views = _count(row.get("viewcount"))
    watchers = None
    for key in ("watchercount", "watcher_count", "watchcount"):
        watchers = _count(row.get(key))
        if watchers is not None:
            break
    return views, watchers


def _close_iso(items: list[LiveItem]) -> str | None:
    best: datetime | None = None
    for item in items:
        raw = (item.close_time or "").strip()
        if not raw:
            continue
        text = raw.replace("Z", "+00:00")
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            continue
        if best is None or moment > best:
            best = moment
    return best.isoformat() if best else None


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def public_item(
    item: LiveItem,
    *,
    views: int | None,
    watchers: int | None,
) -> dict[str, Any]:
    """One lot for Estate-Ez. Views / watchers are left off when CT Seller didn't report them."""
    payload: dict[str, Any] = {
        "ctseller_item_id": int(item.item_id),
        "sku": (item.sku or "").strip(),
        "title": (item.title or "").strip(),
        "bids": int(item.bid_count or 0),
        "price": float(item.bid_price or 0),
        "reserve": float(item.reserve_price or 0),
        "item_url": (item.link or "").strip(),
        "item_status": (item.item_status or "").strip(),
    }
    if views is not None:
        payload["views"] = int(views)
    if watchers is not None:
        payload["watchers"] = int(watchers)
    extra = set(payload) - ALLOWED_ITEM_KEYS
    leaked = set(payload) & FORBIDDEN_ITEM_KEYS
    if extra or leaked:
        raise RuntimeError("Engagement item included a field that is not allowed.")
    return payload


@dataclass
class Completeness:
    complete: bool
    reasons: list[str] = field(default_factory=list)
    missing_attention: int = 0


def _report_reasons(label: str, report: FetchReport | None) -> list[str]:
    if report is None:
        return [f"{label} not checked"]
    if report.truncated:
        return [f"{label} cut off ({report.describe()})"]
    if report.expected is None:
        return [f"{label} total unknown"]
    if report.fetched < report.expected:
        return [f"{label} short ({report.describe()})"]
    return []


def assess_completeness(
    *,
    item_report: FetchReport | None,
    history_report: FetchReport | None,
    missing_attention: int,
) -> Completeness:
    """complete=True only for full, untruncated item + history fetches where every lot has views and watchers."""
    reasons = _report_reasons("item list", item_report) + _report_reasons("sale history", history_report)
    if missing_attention:
        reasons.append(f"{missing_attention} lot(s) missing views or watchers")
    return Completeness(
        complete=not reasons,
        reasons=reasons,
        missing_attention=missing_attention,
    )


def _attention_by_item(history_rows: list[dict[str, Any]] | None) -> dict[int, tuple[int | None, int | None]]:
    attention: dict[int, tuple[int | None, int | None]] = {}
    for row in history_rows or []:
        if not isinstance(row, dict):
            continue
        try:
            item_id = int(row.get("itemid") or row.get("item_id") or 0)
        except (TypeError, ValueError):
            continue
        if not item_id:
            continue
        views, watchers = _attention(row)
        prev_views, prev_watchers = attention.get(item_id, (None, None))
        attention[item_id] = (
            prev_views if prev_views is not None else views,
            prev_watchers if prev_watchers is not None else watchers,
        )
    return attention


def build_snapshot_payload(
    sale_id: int,
    items: list[LiveItem],
    history_rows: list[dict[str, Any]] | None,
    *,
    sale_title: str = "",
    captured_at: datetime | None = None,
    item_report: FetchReport | None = None,
    history_report: FetchReport | None = None,
    include_complete: bool = False,
) -> tuple[dict[str, Any], Completeness]:
    attention = _attention_by_item(history_rows)
    public_items = []
    missing = 0
    for item in items:
        views, watchers = attention.get(int(item.item_id), (None, None))
        if views is None or watchers is None:
            missing += 1
        public_items.append(public_item(item, views=views, watchers=watchers))
    completeness = assess_completeness(
        item_report=item_report,
        history_report=history_report,
        missing_attention=missing,
    )
    live = [it for it in items if not it.is_pending]
    body: dict[str, Any] = {
        "ctseller_sale_id": int(sale_id),
        "sale_title": (sale_title or f"CT Bids sale {int(sale_id)}").strip(),
        "auction_url": buyer_sale_url(sale_id),
        "sale_ends_at": _close_iso(live or items),
        "items": public_items,
    }
    if captured_at is not None:
        body["captured_at"] = _as_utc(captured_at).isoformat()
    if include_complete:
        body["complete"] = completeness.complete
    return body, completeness


def build_snapshot_body(
    sale_id: int,
    items: list[LiveItem],
    history_rows: list[dict[str, Any]] | None,
    *,
    sale_title: str = "",
    captured_at: datetime | None = None,
    item_report: FetchReport | None = None,
    history_report: FetchReport | None = None,
    include_complete: bool = False,
) -> dict[str, Any]:
    body, _completeness = build_snapshot_payload(
        sale_id,
        items,
        history_rows,
        sale_title=sale_title,
        captured_at=captured_at,
        item_report=item_report,
        history_report=history_report,
        include_complete=include_complete,
    )
    return body


def post_snapshot_body(token: str, body: dict[str, Any]) -> dict[str, Any]:
    ee = _estate_ez()
    try:
        resp = requests.post(
            f"{ee.api_base()}/integrations/engagement/snapshots",
            json=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Could not reach Estate-Ez ({exc}).") from exc
    if resp.status_code == 401:
        ee.clear_estate_ez_token_if(token)
        raise RuntimeError("Estate-Ez session expired. Sign in again.")
    if resp.status_code >= 400:
        detail = ""
        try:
            payload = resp.json()
            detail = str(payload.get("detail") or "")
        except ValueError:
            detail = ""
        raise RuntimeError(detail or f"Estate-Ez rejected the snapshot ({resp.status_code}).")
    try:
        return resp.json()
    except ValueError as exc:
        raise RuntimeError("Estate-Ez accepted the snapshot but returned no summary.") from exc


def _sale_lock(sale_id: int) -> threading.Lock:
    with _sent_lock:
        return _sale_locks.setdefault(int(sale_id), threading.Lock())


def last_sent_at(sale_id: int) -> datetime | None:
    with _sent_lock:
        return _last_sent_at.get(int(sale_id))


def _is_stale(sale_id: int, captured_at: datetime) -> bool:
    last = last_sent_at(sale_id)
    return last is not None and captured_at <= last


def push_sale_snapshot(sale_id: int, snap: Snapshot, seller: Any) -> str:
    """Pull views/watchers, drop bidder fields, POST. Returns a status line."""
    sale_id = int(sale_id)
    live_by_id = snap.items if snap and snap.items else {}
    pending_by_id = getattr(snap, "pending_items", None) or {}
    items = list(live_by_id.values()) + [
        it for item_id, it in pending_by_id.items() if item_id not in live_by_id
    ]
    if not items:
        return ""
    captured_at = _as_utc(getattr(snap, "captured_at", None) or datetime.now(timezone.utc))
    stale_note = f"Skipped an engagement snapshot for sale {sale_id} that is older than the last one sent."
    if _is_stale(sale_id, captured_at):
        return stale_note
    token = _estate_ez().ensure_estate_ez_token()
    if not token:
        return "Sign in to Estate-Ez to send the engagement snapshot."
    if seller is None or not getattr(seller, "signed_in", False):
        return "CT Seller is not signed in, so views and watchers were not sent."
    if not seller.sale_is_listed(sale_id):
        return f"Sale {sale_id} is not on this CT Seller login's sales, so no engagement snapshot was sent."
    history, history_report = seller.fetch_sale_history_report(sale_id)
    body, completeness = build_snapshot_payload(
        sale_id,
        items,
        history if isinstance(history, list) else [],
        captured_at=captured_at,
        item_report=getattr(snap, "item_report", None),
        history_report=history_report,
        include_complete=send_complete_enabled(),
    )
    if not body["items"]:
        return ""
    with _sale_lock(sale_id):
        if _is_stale(sale_id, captured_at):
            return stale_note
        summary = post_snapshot_body(token, body)
        with _sent_lock:
            _last_sent_at[sale_id] = captured_at
    push = summary.get("push")
    matched = summary.get("matched_lots")
    note = (
        f"Sent {summary.get('item_count', len(body['items']))} lots to Estate-Ez"
        f" ({push} Push, {matched} matched to catalog lots)."
    )
    if not completeness.complete:
        note += " Partial: " + "; ".join(completeness.reasons) + "."
    return note
