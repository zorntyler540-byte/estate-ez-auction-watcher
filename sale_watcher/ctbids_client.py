"""CTBids live sale helpers: list items, current bids, and (when signed in) bidder nicknames."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import requests

SALE_API = "https://sale.ctbids.com/services/api/v1"
BUYER_API = "https://api.ctbids.com/services/api/v1"
SITE = "https://ctbids.com"

_SALE_URL_RE = re.compile(r"/estate-sale/(\d+)", re.I)


@dataclass
class LiveItem:
    item_id: int
    title: str
    sku: str = ""
    category: str = ""
    close_time: str = ""
    seo_url: str = ""
    thumbnail: str = ""
    item_status: str = "Ready"  # Ready | Sold | Not Sold | …
    reserve_price: float = 0.0
    bid_price: float = 0.0
    bid_count: int = 0
    high_bidder: str = ""
    buyer_id: int = 0
    membership_completed: bool = False
    suspended: bool = False
    unpaid_invoice_count: int | None = None  # None = not checked (needs Seller login)
    paid_invoice_count: int | None = None
    void_invoice_count: int | None = None
    items_bid_count: int | None = None  # CT Seller "Items Currently Bidding"
    suspended_here: bool = False  # active suspend at THIS Seller login's locations
    link: str = ""

    @property
    def is_open(self) -> bool:
        return (self.item_status or "Ready").strip().lower() == "ready"

    @property
    def is_sold(self) -> bool:
        return (self.item_status or "").strip().lower() == "sold"

    @property
    def is_closed(self) -> bool:
        return not self.is_open

    @property
    def has_bids(self) -> bool:
        return self.bid_price > 0 or self.bid_count > 0

    @property
    def has_unpaid(self) -> bool:
        return bool(self.unpaid_invoice_count and self.unpaid_invoice_count > 0)

    @property
    def has_voids(self) -> bool:
        return bool(self.void_invoice_count and self.void_invoice_count > 0)

    def _paid_suffix(self) -> str:
        if self.paid_invoice_count is None:
            return ""
        return f" · Paid ({int(self.paid_invoice_count)})"

    @property
    def account_status(self) -> str:
        """Short label for the high bidder's account health."""
        if self.is_sold:
            return "Sold"
        if not self.high_bidder:
            return ""
        paid = self._paid_suffix()
        if self.suspended:
            if self.unpaid_invoice_count is None:
                return "Suspended"
            if self.unpaid_invoice_count > 0:
                n = self.unpaid_invoice_count
                return f"Suspended · Unpaid ({n}){paid}"
            return f"Suspended · Clear{paid}"
        if self.has_unpaid:
            n = int(self.unpaid_invoice_count or 0)
            return f"Unpaid ({n}){paid}"
        if self.paid_invoice_count is not None:
            if self.membership_completed:
                return f"Member{paid}"
            if self.paid_invoice_count > 0:
                return f"Paid ({int(self.paid_invoice_count)})"
            return "Clean"
        if self.membership_completed:
            return "Member"
        return "Clean"

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "title": self.title,
            "sku": self.sku,
            "category": self.category,
            "close_time": self.close_time,
            "seo_url": self.seo_url,
            "thumbnail": self.thumbnail,
            "item_status": self.item_status,
            "reserve_price": self.reserve_price,
            "bid_price": self.bid_price,
            "bid_count": self.bid_count,
            "high_bidder": self.high_bidder,
            "buyer_id": self.buyer_id,
            "membership_completed": self.membership_completed,
            "suspended": self.suspended,
            "suspended_here": self.suspended_here,
            "unpaid_invoice_count": self.unpaid_invoice_count,
            "paid_invoice_count": self.paid_invoice_count,
            "void_invoice_count": self.void_invoice_count,
            "items_bid_count": self.items_bid_count,
            "account_status": self.account_status,
            "link": self.link,
        }


@dataclass
class HighBidderInfo:
    nickname: str = ""
    buyer_id: int = 0
    membership_completed: bool = False


@dataclass
class BidChange:
    item: LiveItem
    kind: str  # new_bid | price_up | bidder_change | first_bid | sold
    old_price: float = 0.0
    old_bidder: str = ""
    message: str = ""


@dataclass
class Snapshot:
    sale_id: int
    items: dict[int, LiveItem] = field(default_factory=dict)
    hammer_total: float = 0.0
    bidding_count: int = 0
    open_count: int = 0
    sold_count: int = 0
    unique_bidders: int = 0


def parse_sale_id(sale_url_or_id: str) -> int:
    text = (sale_url_or_id or "").strip()
    if text.isdigit():
        return int(text)
    match = _SALE_URL_RE.search(text)
    if match:
        return int(match.group(1))
    # combo labels look like "The Vault  (#47921) · live"
    hash_id = re.search(r"#(\d+)\b", text)
    if hash_id:
        return int(hash_id.group(1))
    # bare path /estate-sale/123
    parsed = urlparse(text if "://" in text else f"https://ctbids.com/{text.lstrip('/')}")
    match = _SALE_URL_RE.search(parsed.path)
    if match:
        return int(match.group(1))
    raise ValueError(
        "Could not find a sale ID. Paste a link like:\n"
        "https://ctbids.com/estate-sale/46500"
    )


def buyer_sale_url(sale_id: int) -> str:
    return f"{SITE}/estate-sale/{int(sale_id)}"


def item_link(sale_id: int, item: LiveItem) -> str:
    if item.seo_url:
        return f"{SITE}/estate-sale/{sale_id}/item/{item.item_id}/{item.seo_url}"
    return f"{SITE}/estate-sale/{sale_id}/item/{item.item_id}"


def _row_money(row: dict[str, Any], *keys: str) -> float:
    for key in keys:
        raw = row.get(key)
        if raw is None or str(raw).strip() in ("", "None", "nan"):
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return 0.0


class CtbidsClient:
    def __init__(self, access_token: str | None = None, timeout: float = 30.0) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json", "Accept": "application/json"})
        self.set_token(access_token)

    def set_token(self, access_token: str | None) -> None:
        self.access_token = (access_token or "").strip() or None
        if self.access_token:
            self.session.headers["Authorization"] = f"Bearer {self.access_token}"
        else:
            self.session.headers.pop("Authorization", None)

    @property
    def signed_in(self) -> bool:
        return bool(self.access_token)

    def fetch_sale_items(self, sale_id: int, page_size: int = 100) -> list[LiveItem]:
        """Paginate sale items — includes Ready (live) and Sold (final price)."""
        items: list[LiveItem] = []
        next_key: str | None = None
        guard = 0
        while guard < 40:
            guard += 1
            page: dict[str, Any] = {"size": page_size}
            if next_key:
                page["next"] = next_key
            body = {
                "sort": [{"field": "itemclosetime", "direction": "asc"}],
                "page": page,
                "field": [
                    "id",
                    "title",
                    "SKU",
                    "category",
                    "saleid",
                    "itemclosetime",
                    "thumbnailurl",
                    "itemseourl",
                    "itemstatus",
                    "itemtypeid",
                    "reserveprice",
                ],
                # Saleid only — works while live (Started) and after close (Closed).
                # Filtering salestatus=Started returns nothing once the sale ends.
                "filter": [
                    {"field": "saleid", "value": sale_id, "op": "=", "join": "AND"},
                ],
            }
            resp = self.session.post(f"{SALE_API}/search/item/new/list", json=body, timeout=self.timeout)
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("status") != "success":
                raise RuntimeError(payload.get("message") or "Item list request failed")
            batch = payload.get("data") or []
            if not batch:
                break
            for row in batch:
                status = str(row.get("itemstatus") or "Ready").strip()
                # Skip photo-pending drafts; keep Ready / Sold / Not Sold
                if status.lower() == "pending":
                    continue
                item_id = int(row["id"])
                live = LiveItem(
                    item_id=item_id,
                    title=str(row.get("title") or "").strip(),
                    sku=str(row.get("SKU") or row.get("sku") or "").strip(),
                    category=str(row.get("category") or "").strip(),
                    close_time=str(row.get("itemclosetime") or "").strip(),
                    seo_url=str(row.get("itemseourl") or "").strip(),
                    thumbnail=str(row.get("thumbnailurl") or "").strip(),
                    item_status=status or "Ready",
                    reserve_price=_row_money(row, "reserveprice", "reservePrice"),
                )
                live.link = item_link(sale_id, live)
                items.append(live)
            total = int((payload.get("page") or {}).get("total") or 0)
            next_key = ((payload.get("page") or {}).get("keyset") or {}).get("next")
            if not next_key or len(items) >= total:
                break
            if len(batch) < page_size:
                break
        seen: dict[int, LiveItem] = {}
        for it in items:
            seen.setdefault(it.item_id, it)
        return list(seen.values())

    def fetch_current_bids(self, item_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Public current-bid endpoint (price + count, no usernames)."""
        out: dict[int, dict[str, Any]] = {}
        chunk = 80
        for i in range(0, len(item_ids), chunk):
            ids = item_ids[i : i + chunk]
            if not ids:
                continue
            resp = self.session.post(
                f"{BUYER_API}/buyer/auction/item/current/bid",
                json={"data": {"itemIds": ids}},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("status") != "success":
                raise RuntimeError(payload.get("message") or "Current bid request failed")
            for row in payload.get("data") or []:
                item_id = int(row["itemid"])
                out[item_id] = {
                    "bid_price": float(row.get("bidprice") or 0),
                    "bid_count": int(row.get("bidcount") or 0),
                    "close_time": str(row.get("itemclosetime") or ""),
                    "bid_status": row.get("bidstatus"),
                }
        return out

    def fetch_high_bidder_info(self, item_id: int) -> HighBidderInfo | None:
        """Leading bidder nickname + buyer id / membership — requires signed-in token."""
        if not self.access_token:
            return None
        resp = self.session.get(
            f"{BUYER_API}/buyer/auction/bid/history/{item_id}",
            timeout=self.timeout,
        )
        if resp.status_code in (401, 403):
            raise PermissionError("CTBids login expired or missing. Sign in again.")
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("status") != "success":
            return None
        data = payload.get("data") or {}
        history = data.get("bidhistory") or data.get("bidHistory") or []
        if not history:
            return None

        def _nick(row: dict[str, Any]) -> str:
            for key in ("nickname", "username", "userName", "bidder", "biddername"):
                val = row.get(key)
                if val and str(val).strip() and str(val).strip() != "-":
                    return str(val).strip()
            return ""

        def _amount(row: dict[str, Any]) -> float:
            for key in ("bidprice", "bidPrice", "bidamount", "bidAmount", "amount"):
                if row.get(key) is None:
                    continue
                try:
                    return float(row.get(key) or 0)
                except (TypeError, ValueError):
                    continue
            return 0.0

        best: dict[str, Any] | None = None
        best_amt = -1.0
        for row in history:
            if not isinstance(row, dict):
                continue
            nick = _nick(row)
            if not nick:
                continue
            amt = _amount(row)
            if amt > best_amt:
                best_amt = amt
                best = row
        if best is None and isinstance(history[0], dict):
            best = history[0]
        if not best:
            return None

        nick = _nick(best)
        if not nick:
            return None
        try:
            buyer_id = int(best.get("buyerid") or best.get("buyerId") or 0)
        except (TypeError, ValueError):
            buyer_id = 0
        member_raw = best.get("membershipCompleted")
        member = member_raw in (1, "1", True, "true", "True")
        return HighBidderInfo(nickname=nick, buyer_id=buyer_id, membership_completed=member)

    def fetch_high_bidder(self, item_id: int) -> str:
        info = self.fetch_high_bidder_info(item_id)
        return info.nickname if info else ""

    def fetch_buyer_suspended(self, buyer_id: int) -> bool:
        """True if CTBids has an active suspension record for this buyer id."""
        if not self.access_token or not buyer_id:
            return False
        resp = self.session.post(
            f"{BUYER_API}/buyer/user/suspend/list",
            json={
                "filter": [{"field": "buyerid", "value": int(buyer_id), "op": "=", "join": "AND"}],
                "page": {"size": 5},
            },
            timeout=self.timeout,
        )
        if resp.status_code in (401, 403):
            raise PermissionError("CTBids login expired or missing. Sign in again.")
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("status") != "success":
            return False
        rows = payload.get("data") or []
        # Active suspension = row present with no revoke date
        for row in rows:
            if not isinstance(row, dict):
                continue
            if int(row.get("buyerid") or 0) != int(buyer_id):
                continue
            if not row.get("revokedate"):
                return True
        return False

    def token_status(self) -> str:
        """'ok', 'unauthorized', or 'unknown' (network/timeout — do not log out)."""
        if not self.access_token:
            return "unauthorized"
        # Bid history is auth-gated; item 1 may 404 but still proves the token.
        try:
            resp = self.session.get(
                f"{BUYER_API}/buyer/auction/bid/history/1",
                timeout=min(8.0, self.timeout),
            )
            if resp.status_code in (401, 403):
                return "unauthorized"
            return "ok"
        except Exception:
            return "unknown"

    def token_works(self) -> bool:
        """True only if the token can read bid history (utc endpoint is public)."""
        return self.token_status() == "ok"


def merge_bids(items: list[LiveItem], bids: dict[int, dict[str, Any]]) -> list[LiveItem]:
    for item in items:
        info = bids.get(item.item_id)
        if not info:
            continue
        item.bid_price = float(info.get("bid_price") or 0)
        item.bid_count = int(info.get("bid_count") or 0)
        if info.get("close_time"):
            item.close_time = str(info["close_time"])
    return items


def build_snapshot(sale_id: int, items: list[LiveItem]) -> Snapshot:
    by_id = {it.item_id: it for it in items}
    with_bids = [it for it in items if it.has_bids]
    live_bidding = [it for it in with_bids if it.is_open]
    sold = [it for it in items if it.is_sold]
    open_lots = [it for it in items if it.is_open]
    hammer = sum(it.bid_price for it in with_bids)
    bidders = {it.high_bidder for it in with_bids if it.high_bidder and it.high_bidder != "-"}
    return Snapshot(
        sale_id=sale_id,
        items=by_id,
        hammer_total=hammer,
        bidding_count=len(live_bidding),
        open_count=len(open_lots),
        sold_count=len(sold),
        unique_bidders=len(bidders),
    )


def diff_snapshots(old: Snapshot | None, new: Snapshot) -> list[BidChange]:
    if old is None:
        return []
    changes: list[BidChange] = []
    for item_id, item in new.items.items():
        prev = old.items.get(item_id)
        if prev is None:
            if item.bid_price > 0 and item.is_open:
                changes.append(
                    BidChange(
                        item=item,
                        kind="first_bid",
                        message=f"NEW item bidding: {item.title[:60]} @ ${item.bid_price:,.2f}"
                        + (f" ({item.high_bidder})" if item.high_bidder else ""),
                    )
                )
            elif item.is_sold and item.has_bids:
                changes.append(
                    BidChange(
                        item=item,
                        kind="sold",
                        old_price=item.bid_price,
                        message=(
                            f"SOLD: {item.sku or item.title[:50]} → ${item.bid_price:,.2f}"
                            + (f" · {item.high_bidder}" if item.high_bidder else "")
                        ),
                    )
                )
            continue

        became_sold = item.is_sold and not prev.is_sold
        price_up = item.is_open and item.bid_price > prev.bid_price + 0.001
        first = item.is_open and prev.bid_price <= 0 < item.bid_price
        bidder_changed = (
            item.is_open
            and bool(item.high_bidder)
            and bool(prev.high_bidder)
            and item.high_bidder != prev.high_bidder
        )

        if became_sold:
            changes.append(
                BidChange(
                    item=item,
                    kind="sold",
                    old_price=prev.bid_price,
                    old_bidder=prev.high_bidder,
                    message=(
                        f"SOLD: {item.sku or item.title[:50]} → ${item.bid_price:,.2f}"
                        + (f" · {item.high_bidder}" if item.high_bidder else "")
                    ),
                )
            )
        elif first:
            changes.append(
                BidChange(
                    item=item,
                    kind="first_bid",
                    old_price=prev.bid_price,
                    old_bidder=prev.high_bidder,
                    message=(
                        f"FIRST BID: {item.title[:55]} → ${item.bid_price:,.2f}"
                        + (f" by {item.high_bidder}" if item.high_bidder else "")
                    ),
                )
            )
        elif price_up:
            changes.append(
                BidChange(
                    item=item,
                    kind="price_up",
                    old_price=prev.bid_price,
                    old_bidder=prev.high_bidder,
                    message=(
                        f"BID UP: {item.title[:50]} ${prev.bid_price:,.2f} → ${item.bid_price:,.2f}"
                        + (f" ({item.high_bidder})" if item.high_bidder else "")
                    ),
                )
            )
        elif bidder_changed and not price_up:
            changes.append(
                BidChange(
                    item=item,
                    kind="bidder_change",
                    old_price=prev.bid_price,
                    old_bidder=prev.high_bidder,
                    message=f"HIGH BIDDER: {item.title[:50]} now {item.high_bidder} (was {prev.high_bidder})",
                )
            )
    changes.sort(key=lambda c: (-c.item.bid_price, c.item.title.lower()))
    return changes
