"""CT Seller API helpers — unpaid invoices / bidder detail / location suspend."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

import requests

SELLER_API = "https://seller.ctbids.com/services/api/v1"
SELLER_SITE = "https://seller.ctbids.com"
BUYER_API = "https://api.ctbids.com/services/api/v1"

SUSPEND_TYPES = (
    "Non Payment - Item",
    "Non Payment - Shipping",
    "Fraudulent Behavior",
    "Inappropriate Behavior",
)
REVOKE_TYPES = (
    "Customer Paid Unpaid Invoice",
    "Customer Paid Unpaid Shipping Invoice",
    "Resolved Issues with Customer",
    "Other - See Notes",
)


@dataclass
class BuyerPaymentSummary:
    buyer_id: int
    unpaid_count: int = 0
    paid_count: int = 0
    void_count: int = 0
    items_won: int = 0
    items_bidding: int = 0
    suspended_count: int = 0


@dataclass
class SellerSale:
    """One sale from this franchise's CT Seller dashboard."""

    sale_id: int
    title: str
    status: str = ""
    item_count: int = 0
    location_id: int = 0
    location_title: str = ""
    is_active: bool = True

    @property
    def buyer_url(self) -> str:
        return f"https://ctbids.com/estate-sale/{int(self.sale_id)}"

    @property
    def status_key(self) -> str:
        return (self.status or "").strip().lower()

    @property
    def is_closed(self) -> bool:
        return self.status_key in ("closed", "inactive", "deleted")

    @property
    def is_live(self) -> bool:
        """Public lots are up — worth a Live Watch tab."""
        return (not self.is_closed) and self.status_key in ("started", "active") and self.item_count > 0

    @property
    def in_dropdown(self) -> bool:
        return not self.is_closed

    @property
    def combo_label(self) -> str:
        title = (self.title or f"Sale {self.sale_id}").strip()
        if len(title) > 52:
            title = title[:50].rstrip() + "…"
        extra = " · live" if self.is_live else f" · {self.status or 'open'}"
        if self.item_count:
            extra += f" · {self.item_count} lots"
        return f"{title}  (#{self.sale_id}){extra}"

    @property
    def tab_label(self) -> str:
        title = (self.title or f"Sale {self.sale_id}").strip()
        if len(title) > 18:
            title = title[:16].rstrip() + "…"
        return f"{title}  {self.sale_id}"


class SellerClient:
    """Authenticated calls against seller.ctbids.com services."""

    def __init__(self, access_token: str | None = None, timeout: float = 30.0) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {"Content-Type": "application/json", "Accept": "application/json"}
        )
        self.set_token(access_token)
        self._connected_buyer_token: str | None = None
        self._owned_location_ids: set[int] | None = None

    def set_token(self, access_token: str | None) -> None:
        self.access_token = (access_token or "").strip() or None
        if self.access_token:
            self.session.headers["Authorization"] = f"Bearer {self.access_token}"
        else:
            self.session.headers.pop("Authorization", None)
        self._connected_buyer_token = None
        self._owned_location_ids = None

    @property
    def signed_in(self) -> bool:
        return bool(self.access_token)

    def token_status(self) -> str:
        """'ok', 'unauthorized', or 'unknown' (network/timeout — do not log out)."""
        if not self.access_token:
            return "unauthorized"
        try:
            # POST location/list 401s for some roles; GET list is a cheap auth check.
            resp = self.session.get(
                f"{SELLER_API}/location/list",
                timeout=min(8.0, self.timeout),
            )
            if resp.status_code in (401, 403):
                return "unauthorized"
            return "ok"
        except Exception:
            return "unknown"

    def token_works(self) -> bool:
        """True if the seller token can hit a protected seller endpoint."""
        return self.token_status() == "ok"

    def fetch_buyer_payment_summary(self, buyer_id: int) -> BuyerPaymentSummary | None:
        """Invoice paid/unpaid counts for a buyer (same source as CT Seller buyer profile)."""
        if not self.access_token or not buyer_id:
            return None
        resp = self.session.post(
            f"{SELLER_API}/buyer/detail/view/{int(buyer_id)}",
            json={
                "field": [
                    "invoicepaidcount",
                    "invoiceunpaidcount",
                    "itemswoncount",
                    "suspendedcount",
                    "unsuspendedcount",
                    "itemsbidcount",
                ],
                "filter": [],
            },
            timeout=self.timeout,
        )
        if resp.status_code in (401, 403):
            raise PermissionError("CT Seller login expired or missing. Sign in to Seller again.")
        if resp.status_code == 404:
            return BuyerPaymentSummary(buyer_id=int(buyer_id))
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("status") not in (None, "success") and "data" not in payload:
            return None
        data = payload.get("data")
        row: dict[str, Any] | None = None
        if isinstance(data, list) and data:
            row = data[0] if isinstance(data[0], dict) else None
        elif isinstance(data, dict):
            row = data
        if not row:
            return BuyerPaymentSummary(buyer_id=int(buyer_id))

        def _int(key: str, *alts: str) -> int:
            for k in (key, *alts):
                if row.get(k) is None:
                    continue
                try:
                    return int(row.get(k) or 0)
                except (TypeError, ValueError):
                    continue
            return 0

        void_count = 0
        try:
            void_count = self.fetch_buyer_void_count(buyer_id)
        except PermissionError:
            raise
        except Exception:
            void_count = 0
        return BuyerPaymentSummary(
            buyer_id=int(buyer_id),
            unpaid_count=_int("invoiceunpaidcount", "invoiceUnpaidCount"),
            paid_count=_int("invoicepaidcount", "invoicePaidCount"),
            void_count=void_count,
            items_won=_int("itemswoncount", "bidWonCount", "itemsWonCount"),
            items_bidding=_int("itemBidCount", "itemsbidcount", "itemsBidCount"),
            suspended_count=_int("suspendedcount", "suspendedCount"),
        )

    def fetch_buyer_void_count(self, buyer_id: int) -> int:
        """How many Void invoices CT Seller shows on this buyer's invoice tab."""
        if not self.access_token or not buyer_id:
            return 0
        resp = self.session.post(
            f"{SELLER_API}/invoice/list",
            data=json.dumps(
                {
                    "field": [
                        "id",
                        "invoicestatus",
                        "buyerid",
                        "locationtitle",
                        "saletitle",
                        "createddate",
                        "invoiceamount",
                        "type",
                    ],
                    "filter": [
                        {"field": "buyerid", "value": int(buyer_id), "op": "=", "join": "AND"},
                        {"field": "invoicestatus", "value": "Void", "op": "=", "join": "AND"},
                    ],
                    "page": {"size": 1},
                }
            ),
            timeout=self.timeout,
        )
        if resp.status_code in (401, 403):
            raise PermissionError("CT Seller login expired or missing. Sign in to Seller again.")
        if resp.status_code >= 400:
            return 0
        payload = resp.json() if resp.content else {}
        page = payload.get("page") if isinstance(payload, dict) else None
        if isinstance(page, dict) and page.get("total") is not None:
            try:
                return max(0, int(page.get("total") or 0))
            except (TypeError, ValueError):
                return 0
        rows = payload.get("data") or []
        return len(rows) if isinstance(rows, list) else 0

    def list_locations(self) -> list[tuple[int, str]]:
        """Franchise locations this seller login can suspend at."""
        if not self.access_token:
            raise PermissionError("CT Seller login expired or missing. Sign in to Seller again.")
        resp = self.session.post(
            f"{SELLER_API}/location/list",
            data=json.dumps({"field": ["id", "title"]}),
            timeout=self.timeout,
        )
        if resp.status_code in (401, 403):
            raise PermissionError("CT Seller login expired or missing. Sign in to Seller again.")
        if resp.status_code >= 400:
            resp = self.session.get(f"{SELLER_API}/location/list", timeout=self.timeout)
            if resp.status_code in (401, 403):
                raise PermissionError("CT Seller login expired or missing. Sign in to Seller again.")
        resp.raise_for_status()
        payload = resp.json() if resp.content else {}
        rows = payload.get("data") or []
        out: list[tuple[int, str]] = []
        if not isinstance(rows, list):
            return out
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                loc_id = int(row.get("id") or 0)
            except (TypeError, ValueError):
                continue
            if loc_id:
                out.append((loc_id, str(row.get("title") or loc_id)))
        self._owned_location_ids = {lid for lid, _title in out}
        return out

    def owned_location_id_set(self) -> set[int]:
        if self._owned_location_ids is None:
            self.list_locations()
        return set(self._owned_location_ids or ())

    def buyer_suspended_at_our_locations(self, buyer_id: int) -> bool:
        """True only if this Seller login has an active location suspend on the buyer."""
        if not self.access_token or not buyer_id:
            return False
        owned = self.owned_location_id_set()
        if not owned:
            return False
        resp = self.session.post(
            f"{SELLER_API}/buyer/suspend/list",
            data=json.dumps(
                {
                    "filter": [
                        {"field": "buyerid", "value": int(buyer_id), "op": "=", "join": "AND"}
                    ],
                    "page": {"size": 20},
                }
            ),
            timeout=self.timeout,
        )
        if resp.status_code in (401, 403):
            raise PermissionError("CT Seller login expired or missing. Sign in to Seller again.")
        if resp.status_code >= 400:
            return False
        payload = resp.json() if resp.content else {}
        rows = payload.get("data") or []
        if not isinstance(rows, list):
            return False
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                loc = int(row.get("locationid") or row.get("locationId") or 0)
            except (TypeError, ValueError):
                loc = 0
            if loc not in owned:
                continue
            if not row.get("revokedate") and not row.get("revokeDate"):
                return True
        return False

    def list_sales(self) -> list[SellerSale]:
        """Sales on this seller login — same list as the CT Seller dashboard."""
        if not self.access_token:
            raise PermissionError("CT Seller login expired or missing. Sign in to Seller again.")
        body = {
            "field": [
                "id",
                "title",
                "status",
                "isactive",
                "itemcount",
                "locationid",
                "locationtitle",
            ]
        }
        resp = self.session.post(
            f"{SELLER_API}/sale/list",
            data=json.dumps(body),
            timeout=self.timeout,
        )
        if resp.status_code in (401, 403):
            raise PermissionError("CT Seller login expired or missing. Sign in to Seller again.")
        if resp.status_code >= 400:
            resp = self.session.get(f"{SELLER_API}/sale/list", timeout=self.timeout)
            if resp.status_code in (401, 403):
                raise PermissionError("CT Seller login expired or missing. Sign in to Seller again.")
        resp.raise_for_status()
        payload = resp.json() if resp.content else {}
        if payload.get("status") not in (None, "success") and "data" not in payload:
            raise RuntimeError(payload.get("message") or "Could not load sales from CT Seller.")
        rows = payload.get("data") or []
        out: list[SellerSale] = []
        if not isinstance(rows, list):
            return out
        seen: set[int] = set()
        for row in rows:
            sale = _parse_seller_sale(row)
            if sale and sale.sale_id not in seen:
                seen.add(sale.sale_id)
                out.append(sale)
        out.sort(key=lambda s: (0 if s.is_live else 1, -s.item_count, -s.sale_id))
        return out

    SALE_HISTORY_FIELDS = (
        "itemtitle",
        "sku",
        "category",
        "itemstatus",
        "itemtype",
        "saletitle",
        "saleid",
        "saleprice",
        "reserveprice",
        "buyerspremium",
        "itemtax",
        "taxonbpamount",
        "invoicenumber",
        "invoicestatus",
        "starttime",
        "endtime",
        "closetime",
        "bidcount",
        "viewcount",
        "itemid",
        "nickname",
        "firstname",
        "lastname",
        "email",
        "taxexempt",
        "isshippable",
    )

    # Active-sale report (seller UI "sale active report"). Watchers are wishlistcount.
    # Bidder name, email, and phone fields are intentionally absent.
    ACTIVE_SALE_FIELDS = (
        "itemid",
        "sku",
        "itemtitle",
        "itemstatus",
        "saleid",
        "viewcount",
        "wishlistcount",
        "bidcount",
        "saleprice",
        "reserveprice",
    )

    def fetch_sale_history_items(self, sale_id: int) -> list[dict[str, Any]]:
        """Lots on one sale from the same report CT Seller's sale-history CSV uses."""
        return self._paged_sale_report("/report/sale/item/list", self.SALE_HISTORY_FIELDS, sale_id)

    def fetch_active_sale_items(self, sale_id: int) -> list[dict[str, Any]]:
        """Live lots from the report that carries Number of Views and Number of Watchers."""
        return self._paged_sale_report("/report/sale/list", self.ACTIVE_SALE_FIELDS, sale_id)

    def _paged_sale_report(self, path: str, fields: tuple[str, ...] | list[str], sale_id: int) -> list[dict[str, Any]]:
        if not self.access_token:
            raise PermissionError("CT Seller login expired or missing. Sign in to Seller again.")
        rows: list[dict[str, Any]] = []
        next_key = ""
        guard = 0
        total = None
        while guard < 80:
            guard += 1
            page: dict[str, Any] = {"size": 200}
            if next_key:
                page["next"] = next_key
            body = {
                "field": list(fields),
                "filter": [
                    {"field": "saleid", "value": int(sale_id), "op": "=", "join": "AND"},
                ],
                "page": page,
            }
            resp = self.session.post(
                f"{SELLER_API}{path}",
                data=json.dumps(body),
                timeout=90,
            )
            if resp.status_code in (401, 403):
                raise PermissionError("CT Seller login expired or missing. Sign in to Seller again.")
            resp.raise_for_status()
            payload = resp.json() if resp.content else {}
            batch = payload.get("data") or []
            if not isinstance(batch, list) or not batch:
                break
            for row in batch:
                if isinstance(row, dict):
                    rows.append(row)
            page_info = payload.get("page") if isinstance(payload.get("page"), dict) else {}
            try:
                total = int(page_info.get("total") or 0) or total
            except (TypeError, ValueError):
                pass
            if total is not None and len(rows) >= total:
                break
            keyset = page_info.get("keyset") if isinstance(page_info.get("keyset"), dict) else {}
            nxt = str(keyset.get("next") or page_info.get("next") or "").strip()
            if not nxt or nxt == next_key:
                break
            next_key = nxt
        return rows

    def fetch_all_sale_history_items(
        self,
        *,
        sale_ids: list[int] | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> tuple[list[dict[str, Any]], list[SellerSale]]:
        """Lots on this Seller login's sales (same source as sale-history-report)."""
        catalog = self.list_sales()
        by_id = {s.sale_id: s for s in catalog}
        if sale_ids is None:
            sales = [s for s in catalog if s.item_count > 0]
        else:
            sales = []
            seen: set[int] = set()
            for raw in sale_ids:
                try:
                    sid = int(raw)
                except (TypeError, ValueError):
                    continue
                if sid in seen:
                    continue
                seen.add(sid)
                sale = by_id.get(sid)
                if sale is None:
                    sale = SellerSale(sale_id=sid, title=f"Sale {sid}")
                sales.append(sale)
        rows: list[dict[str, Any]] = []
        for i, sale in enumerate(sales, 1):
            if progress:
                progress(
                    f"Sale {i} of {len(sales)}: {sale.title}  (#{sale.sale_id}, {sale.item_count} lots)"
                )
            rows.extend(self.fetch_sale_history_items(sale.sale_id))
        return rows, sales

    def owned_sale_ids(self) -> set[int]:
        """Sale ids that belong to this Seller login — never another franchise."""
        return {sale.sale_id for sale in self.list_sales()}

    def require_owned_locations(self) -> list[tuple[int, str]]:
        """Locations on this Seller token only. Empty list is a hard stop."""
        locations = self.list_locations()
        if not locations:
            raise RuntimeError("This CT Seller login has no locations.")
        return locations

    def suspend_buyer(
        self,
        buyer_id: int,
        *,
        reason: str,
        suspension_type: str,
    ) -> list[tuple[int, str]]:
        """Suspend at this Seller login's locations only — never alllocations / other franchises."""
        if not self.access_token:
            raise PermissionError("CT Seller login expired or missing. Sign in to Seller again.")
        reason = (reason or "").strip()
        if not buyer_id:
            raise RuntimeError("No bidder id on this row yet — wait for the username to load.")
        if not reason:
            raise RuntimeError("Enter a reason (shown in CT Seller).")
        owned = self.require_owned_locations()
        locations = [
            {
                "buyerid": int(buyer_id),
                "locationid": int(lid),
                "reason": reason[:400],
                "suspensiontype": suspension_type,
            }
            for lid, _title in owned
        ]
        resp = self.session.post(
            f"{SELLER_API}/buyer/suspend",
            data=json.dumps({"data": {"locations": locations, "alllocations": False}}),
            timeout=self.timeout,
        )
        self._raise_seller_action(resp, "Could not suspend this bidder.")
        return owned

    def revoke_buyer_suspend(
        self,
        buyer_id: int,
        *,
        reason: str,
        revoke_type: str,
    ) -> list[tuple[int, str]]:
        """Lift a suspension at this Seller login's locations only."""
        if not self.access_token:
            raise PermissionError("CT Seller login expired or missing. Sign in to Seller again.")
        reason = (reason or "").strip()
        if not buyer_id:
            raise RuntimeError("No bidder id on this row yet — wait for the username to load.")
        if not reason:
            raise RuntimeError("Enter a reason (shown in CT Seller).")
        owned = self.require_owned_locations()
        resp = self.session.put(
            f"{SELLER_API}/buyer/suspend/revoke",
            data=json.dumps(
                {
                    "data": {
                        "buyerid": int(buyer_id),
                        "locationid": [int(lid) for lid, _title in owned],
                        "revokereason": reason[:400],
                        "revoketype": revoke_type,
                    }
                }
            ),
            timeout=self.timeout,
        )
        self._raise_seller_action(resp, "Could not unsuspend this bidder.")
        return owned

    def fetch_connected_buyer_token(self) -> str:
        """Seller SPA uses GET /seller/buyer/token, then calls buyer bid-retract with it."""
        if not self.access_token:
            raise PermissionError("CT Seller login expired or missing. Sign in to Seller again.")
        if self._connected_buyer_token:
            return self._connected_buyer_token
        resp = self.session.get(f"{SELLER_API}/seller/buyer/token", timeout=self.timeout)
        if resp.status_code in (401, 403):
            raise PermissionError("CT Seller login expired or missing. Sign in to Seller again.")
        resp.raise_for_status()
        payload = resp.json() if resp.content else {}
        if not isinstance(payload, dict):
            raise RuntimeError("CT Seller did not return a buyer session for bid retract.")
        token = (
            payload.get("accessToken")
            or payload.get("access_token")
            or payload.get("access")
            or ""
        )
        data = payload.get("data")
        if not token and isinstance(data, dict):
            token = data.get("accessToken") or data.get("access_token") or data.get("access") or ""
        token = str(token or "").strip()
        if not token or token.count(".") != 2:
            raise RuntimeError("CT Seller did not return a buyer session for bid retract.")
        self._connected_buyer_token = token
        return token

    def retract_bidder_on_item(self, item_id: int, buyer_id: int, *, reason: str) -> None:
        """Retract this buyer's bids on one lot — same as CT Seller bid history retract."""
        reason = (reason or "").strip()
        if not item_id or not buyer_id:
            raise RuntimeError("Need both a lot and a bidder id to retract bids.")
        if not reason:
            raise RuntimeError("Enter a reason (shown in CT Seller).")
        body = json.dumps(
            {
                "data": {
                    "itemid": int(item_id),
                    "buyerid": int(buyer_id),
                    "revokedreason": reason[:400],
                }
            }
        )
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.fetch_connected_buyer_token()}",
        }
        resp = requests.post(
            f"{BUYER_API}/buyer/auction/bidder/bid/retract",
            data=body,
            headers=headers,
            timeout=self.timeout,
        )
        if resp.status_code in (401, 403) and self._connected_buyer_token:
            self._connected_buyer_token = None
            headers["Authorization"] = f"Bearer {self.fetch_connected_buyer_token()}"
            resp = requests.post(
                f"{BUYER_API}/buyer/auction/bidder/bid/retract",
                data=body,
                headers=headers,
                timeout=self.timeout,
            )
        self._raise_seller_action(resp, "Could not retract this bidder's bids on that lot.")

    def retract_bidder_on_items(
        self, item_ids: list[int], buyer_id: int, *, reason: str
    ) -> tuple[list[int], list[str]]:
        """Retract on each lot. Returns (succeeded item ids, error strings)."""
        ok: list[int] = []
        errors: list[str] = []
        for item_id in item_ids:
            try:
                self.retract_bidder_on_item(item_id, buyer_id, reason=reason)
                ok.append(int(item_id))
            except Exception as exc:
                errors.append(f"{item_id}: {exc}")
        if not ok:
            raise RuntimeError(errors[0] if errors else "Could not retract any bids.")
        return ok, errors

    @staticmethod
    def _raise_seller_action(resp: requests.Response, fallback: str) -> None:
        if resp.status_code in (401, 403):
            raise PermissionError("CT Seller login expired or missing. Sign in to Seller again.")
        try:
            payload = resp.json() if resp.content else {}
        except Exception:
            payload = {}
        status = str(payload.get("status") or "").lower() if isinstance(payload, dict) else ""
        if resp.status_code >= 400 or status in ("failed", "error", "fail"):
            raise RuntimeError(_seller_error_message(payload, resp.status_code) or fallback)
        if status and status != "success":
            raise RuntimeError(_seller_error_message(payload, resp.status_code) or fallback)


def _parse_seller_sale(row: object) -> SellerSale | None:
    if not isinstance(row, dict):
        return None
    try:
        sale_id = int(row.get("id") or 0)
    except (TypeError, ValueError):
        return None
    if not sale_id:
        return None
    try:
        item_count = int(row.get("itemcount") or row.get("itemCount") or 0)
    except (TypeError, ValueError):
        item_count = 0
    try:
        location_id = int(row.get("locationid") or row.get("locationId") or 0)
    except (TypeError, ValueError):
        location_id = 0
    raw_active = row.get("isactive", row.get("isActive", 1))
    is_active = str(raw_active).strip().lower() not in ("0", "false", "no", "")
    status = str(row.get("status") or row.get("salestatus") or "").strip()
    return SellerSale(
        sale_id=sale_id,
        title=str(row.get("title") or "").strip() or f"Sale {sale_id}",
        status=status,
        item_count=item_count,
        location_id=location_id,
        location_title=str(row.get("locationtitle") or row.get("locationTitle") or "").strip(),
        is_active=is_active,
    )


def _seller_error_message(payload: object, status_code: int) -> str:
    if not isinstance(payload, dict):
        return f"CT Seller error ({status_code})."
    msg = payload.get("message")
    if isinstance(msg, str) and msg.strip():
        return msg.strip()
    if isinstance(msg, dict):
        for val in msg.values():
            if isinstance(val, dict) and val.get("message"):
                return str(val["message"]).strip()
            if isinstance(val, str) and val.strip():
                return val.strip()
    data = payload.get("data")
    if isinstance(data, str) and data.strip():
        return data.strip()
    return ""
