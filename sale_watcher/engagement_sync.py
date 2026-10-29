"""Send a PII-free live snapshot to Estate-EZ after each Watcher refresh.

Bidder names, buyer ids, emails, phones, addresses, and invoices stay on the PC.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import requests

try:
    from ctbids_client import LiveItem, Snapshot, buyer_sale_url
except ImportError:  # noqa: BLE001 - package vs script import
    from sale_watcher.ctbids_client import LiveItem, Snapshot, buyer_sale_url


def _estate_ez():
    try:
        from estate_ez import api_base, ensure_estate_ez_token, save_estate_ez_token
    except ImportError:  # noqa: BLE001 - package vs script import
        from sale_watcher.estate_ez import api_base, ensure_estate_ez_token, save_estate_ez_token
    return api_base, ensure_estate_ez_token, save_estate_ez_token

TIMEOUT = 45

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


def _int(value: Any) -> int:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def _first_count(row: dict[str, Any], keys: tuple[str, ...]) -> int:
    for key in keys:
        if row.get(key) not in (None, ""):
            return _int(row.get(key))
    return 0


def _attention(row: dict[str, Any]) -> tuple[int, int]:
    # CT Seller's active-sale report names these viewcount and wishlistcount
    # ("Number of Views" / "Number of Watchers" on the sheet).
    views = _first_count(row, ("viewcount", "views"))
    watchers = _first_count(row, ("wishlistcount", "watchercount", "watcher_count", "watchcount"))
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


def public_item(item: LiveItem, *, views: int, watchers: int) -> dict[str, Any]:
    payload = {
        "ctseller_item_id": int(item.item_id),
        "sku": (item.sku or "").strip(),
        "title": (item.title or "").strip(),
        "views": int(views),
        "watchers": int(watchers),
        "bids": int(item.bid_count or 0),
        "price": float(item.bid_price or 0),
        "reserve": float(item.reserve_price or 0),
        "item_url": (item.link or "").strip(),
        "item_status": (item.item_status or "").strip(),
    }
    extra = set(payload) - ALLOWED_ITEM_KEYS
    leaked = set(payload) & FORBIDDEN_ITEM_KEYS
    if extra or leaked:
        raise RuntimeError("Engagement item included a field that is not allowed.")
    return payload


def build_snapshot_body(
    sale_id: int,
    items: list[LiveItem],
    history_rows: list[dict[str, Any]] | None,
    *,
    sale_title: str = "",
) -> dict[str, Any]:
    attention: dict[int, tuple[int, int]] = {}
    for row in history_rows or []:
        if not isinstance(row, dict):
            continue
        try:
            item_id = int(row.get("itemid") or row.get("item_id") or row.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if not item_id:
            continue
        views, watchers = _attention(row)
        prev = attention.get(item_id)
        if prev is None:
            attention[item_id] = (views, watchers)
        else:
            attention[item_id] = (max(prev[0], views), max(prev[1], watchers))
    public_items = []
    for item in items:
        views, watchers = attention.get(int(item.item_id), (0, 0))
        public_items.append(public_item(item, views=views, watchers=watchers))
    return {
        "ctseller_sale_id": int(sale_id),
        "sale_title": (sale_title or f"CT Bids sale {int(sale_id)}").strip(),
        "auction_url": buyer_sale_url(sale_id),
        "sale_ends_at": _close_iso(items),
        "items": public_items,
    }


def post_snapshot_body(token: str, body: dict[str, Any]) -> dict[str, Any]:
    try:
        api_base, _ensure, _save = _estate_ez()
        resp = requests.post(
            f"{api_base()}/integrations/engagement/snapshots",
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
        _save(None)
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


def push_sale_snapshot(sale_id: int, snap: Snapshot, seller: Any) -> str:
    """Pull views/watchers, drop bidder fields, POST. Returns a status line."""
    items = list(snap.items.values()) if snap and snap.items else []
    if not items:
        return ""
    _api_base, ensure_estate_ez_token, _save = _estate_ez()
    token = ensure_estate_ez_token()
    if not token:
        return "Sign in to Estate-Ez to send the engagement snapshot."
    if seller is None or not getattr(seller, "signed_in", False):
        return "CT Seller is not signed in, so views and watchers were not sent."
    try:
        history = seller.fetch_active_sale_items(int(sale_id))
    except PermissionError:
        raise
    except Exception:
        history = seller.fetch_sale_history_items(int(sale_id))
    body = build_snapshot_body(int(sale_id), items, history if isinstance(history, list) else [])
    if not body["items"]:
        return ""
    summary = post_snapshot_body(token, body)
    push = summary.get("push")
    matched = summary.get("matched_lots")
    return (
        f"Sent {summary.get('item_count', len(body['items']))} lots to Estate-Ez"
        f" ({push} Push, {matched} matched to catalog lots)."
    )
