"""Login helper + polling loop for the live sale watcher."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable

from ctbids_client import (
    BidChange,
    CtbidsClient,
    HighBidderInfo,
    Snapshot,
    build_snapshot,
    diff_snapshots,
    merge_bids,
    parse_sale_id,
)
from seller_client import SellerClient

try:
    from auth import (
        BuyerMfaRequired,
        clear_saved_password,
        keep_sessions_fresh,
        load_saved_password,
        load_saved_username,
        login_with_password,
        open_login_window,
        open_seller_login_window,
        resolve_access_token,
        resolve_seller_access_token,
        save_access_token,
        save_login_credentials,
        save_seller_access_token,
        send_buyer_mfa_otp,
        verify_buyer_mfa,
        watcher_data_dir,
    )
    from estate_ez import (
        EstateEzAuthError,
        EstateEzSellerNotConnected,
        load_estate_ez_token,
        sign_in_estate_ez,
    )
    from export_report import live_snapshot_path, save_closing_snapshot, write_closing_csv
except ImportError:  # noqa: BLE001 - package vs script import
    from sale_watcher.auth import (
        BuyerMfaRequired,
        clear_saved_password,
        keep_sessions_fresh,
        load_saved_password,
        load_saved_username,
        login_with_password,
        open_login_window,
        open_seller_login_window,
        resolve_access_token,
        resolve_seller_access_token,
        save_access_token,
        save_login_credentials,
        save_seller_access_token,
        send_buyer_mfa_otp,
        verify_buyer_mfa,
        watcher_data_dir,
    )
    from sale_watcher.estate_ez import (
        EstateEzAuthError,
        EstateEzSellerNotConnected,
        load_estate_ez_token,
        sign_in_estate_ez,
    )
    from sale_watcher.export_report import live_snapshot_path, save_closing_snapshot, write_closing_csv

ProgressFn = Callable[[str], None]
SnapshotFn = Callable[[Snapshot, list[BidChange]], None]
EndSnapshotFn = Callable[[Snapshot, list[Path]], None]


def config_path() -> Path:
    return watcher_data_dir() / "watcher_config.json"


def load_watcher_config() -> dict:
    path = config_path()
    if not path.exists():
        return {"sale_url": "", "poll_seconds": 45, "theme": "dark", "auto_save_on_end": True}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"sale_url": "", "poll_seconds": 45, "theme": "dark", "auto_save_on_end": True}
    data.setdefault("theme", "dark")
    data.setdefault("auto_save_on_end", True)
    return data


def save_watcher_config(cfg: dict) -> None:
    config_path().write_text(json.dumps(cfg, indent=2), encoding="utf-8")


class SaleWatcher:
    """Background poller for one CTBids sale."""

    def __init__(
        self,
        sale_url_or_id: str,
        access_token: str | None = None,
        seller_token: str | None = None,
        poll_seconds: float = 45.0,
        on_update: SnapshotFn | None = None,
        on_status: ProgressFn | None = None,
        on_auth_lost: ProgressFn | None = None,
        on_seller_auth_lost: ProgressFn | None = None,
        on_end_snapshot: EndSnapshotFn | None = None,
        auto_save_on_end: bool = True,
    ) -> None:
        self.sale_id = parse_sale_id(sale_url_or_id)
        self.poll_seconds = max(10.0, float(poll_seconds))
        self.on_update = on_update
        self.on_status = on_status
        self.on_auth_lost = on_auth_lost
        self.on_seller_auth_lost = on_seller_auth_lost
        self.on_end_snapshot = on_end_snapshot
        self.auto_save_on_end = bool(auto_save_on_end)
        self.client = CtbidsClient(access_token)
        self.seller = SellerClient(seller_token)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.latest: Snapshot | None = None
        self._bidder_cache: dict[int, str] = {}
        self._bidder_info: dict[int, HighBidderInfo] = {}
        self._suspend_cache: dict[int, bool] = {}  # buyer_id -> network CTBids flag
        self._suspend_here_cache: dict[int, bool] = {}  # buyer_id -> this Seller login
        self._payment_cache: dict[int, tuple[int, ...]] = {}  # unpaid, paid, voids, bidding
        self._last_prices: dict[int, float] = {}
        self._last_counts: dict[int, int] = {}
        self._pending_names: set[int] = set()  # bid moved but history not ready yet
        self._force_name_refresh = bool(access_token)
        self._auth_recovery_attempted = False
        self._seller_auth_recovery_attempted = False
        self._end_snapshot_saved = False
        self._last_reserve: dict[int, float] = {}
        self._frozen: dict[int, dict] = {}
        self._live_csv_announced = False

    def say(self, msg: str) -> None:
        if self.on_status:
            self.on_status(msg)

    def set_token(self, token: str | None) -> None:
        was = self.client.access_token
        self.client.set_token(token)
        self._auth_recovery_attempted = False
        if not token:
            self._bidder_cache.clear()
            self._bidder_info.clear()
            self._suspend_cache.clear()
            self._suspend_here_cache.clear()
            self._pending_names.clear()
            self._force_name_refresh = False
        elif not was:
            # Newly signed in — backfill usernames. JWT rotation keeps the cache.
            self._force_name_refresh = True

    def set_seller_token(self, token: str | None) -> None:
        was = self.seller.access_token
        self.seller.set_token(token)
        self._seller_auth_recovery_attempted = False
        if not token:
            self._payment_cache.clear()
            self._suspend_here_cache.clear()
        elif not was:
            self._force_name_refresh = True

    def invalidate_usernames(self) -> None:
        self._bidder_cache.clear()
        self._bidder_info.clear()
        self._suspend_cache.clear()
        self._suspend_here_cache.clear()
        self._payment_cache.clear()
        self._pending_names.clear()
        self._force_name_refresh = True

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _handle_auth_lost(self) -> bool:
        """Refresh the buyer JWT (HTTP first, then one profile re-read). Returns True if recovered."""
        self.say("Buyer login expired — refreshing session…")
        try:
            fresh = resolve_access_token(validate=True, allow_browser=False)
        except Exception:
            fresh = None
        if not fresh and not self._auth_recovery_attempted:
            self._auth_recovery_attempted = True
            self.say("Buyer login expired — trying saved browser profile…")
            try:
                fresh = resolve_access_token(validate=True, allow_browser=True)
            except Exception:
                fresh = None
        if fresh:
            self.client.set_token(fresh)
            self._force_name_refresh = True
            self._auth_recovery_attempted = False
            self.say("Buyer login restored — fetching usernames.")
            return True

        self.client.set_token(None)
        save_access_token(None)
        msg = "CTBids buyer login expired. Click Buyer sign in if usernames stop updating."
        self.say(msg)
        if self.on_auth_lost:
            self.on_auth_lost(msg)
        return False

    def _handle_seller_auth_lost(self) -> bool:
        """Refresh the seller JWT (HTTP first, then one profile re-read). Returns True if recovered."""
        self.say("Seller login expired — refreshing session…")
        try:
            fresh = resolve_seller_access_token(validate=True, allow_browser=False)
        except Exception:
            fresh = None
        if not fresh and not self._seller_auth_recovery_attempted:
            self._seller_auth_recovery_attempted = True
            self.say("Seller login expired — trying saved browser profile…")
            try:
                fresh = resolve_seller_access_token(validate=True, allow_browser=True)
            except Exception:
                fresh = None
        if fresh:
            self.seller.set_token(fresh)
            self._seller_auth_recovery_attempted = False
            self.say("Seller login restored — checking unpaid invoices.")
            return True

        self.seller.set_token(None)
        save_seller_access_token(None)
        msg = "CT Seller login expired. Click Seller sign in again for unpaid checks."
        self.say(msg)
        if self.on_seller_auth_lost:
            self.on_seller_auth_lost(msg)
        return False

    def _apply_bidder(self, item) -> None:
        info = self._bidder_info.get(item.item_id)
        nick = self._bidder_cache.get(item.item_id, "")
        if info:
            nick = info.nickname or nick
            item.buyer_id = info.buyer_id
            item.membership_completed = info.membership_completed
            if info.buyer_id and info.buyer_id in self._suspend_cache:
                item.suspended = self._suspend_cache[info.buyer_id]
            else:
                item.suspended = False
            item.suspended_here = bool(
                info.buyer_id and self._suspend_here_cache.get(info.buyer_id, False)
            )
            if info.buyer_id and info.buyer_id in self._payment_cache:
                unpaid, paid, voids, bidding = self._unpack_payment(self._payment_cache[info.buyer_id])
                item.unpaid_invoice_count = unpaid
                item.paid_invoice_count = paid
                item.void_invoice_count = voids
                item.items_bid_count = bidding
            else:
                item.unpaid_invoice_count = None
                item.paid_invoice_count = None
                item.void_invoice_count = None
                item.items_bid_count = None
        else:
            item.buyer_id = 0
            item.membership_completed = False
            item.suspended = False
            item.suspended_here = False
            item.unpaid_invoice_count = None
            item.paid_invoice_count = None
            item.void_invoice_count = None
            item.items_bid_count = None
        item.high_bidder = nick

    def _ensure_suspend(self, buyer_id: int) -> bool:
        if not buyer_id:
            return False
        if buyer_id in self._suspend_cache:
            return self._suspend_cache[buyer_id]
        try:
            suspended = self.client.fetch_buyer_suspended(buyer_id)
        except PermissionError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.say(f"Could not check suspension for buyer {buyer_id}: {exc}")
            suspended = False
        self._suspend_cache[buyer_id] = suspended
        time.sleep(0.04)
        return suspended

    def _ensure_suspend_here(self, buyer_id: int) -> bool:
        """True if THIS Seller login has an active location suspend on the buyer."""
        if not buyer_id or not self.seller.signed_in:
            return False
        if buyer_id in self._suspend_here_cache:
            return self._suspend_here_cache[buyer_id]
        try:
            here = self.seller.buyer_suspended_at_our_locations(buyer_id)
        except PermissionError:
            if self._handle_seller_auth_lost():
                return self._ensure_suspend_here(buyer_id)
            return False
        except Exception as exc:  # noqa: BLE001
            self.say(f"Could not check our location suspend for buyer {buyer_id}: {exc}")
            here = False
        self._suspend_here_cache[buyer_id] = here
        time.sleep(0.04)
        return here

    def note_suspension(self, buyer_id: int, suspended: bool) -> None:
        """Update caches after a Seller suspend / unsuspend from the UI."""
        if not buyer_id:
            return
        self._suspend_here_cache[int(buyer_id)] = bool(suspended)
        if suspended:
            self._suspend_cache[int(buyer_id)] = True

    def forget_bidders_on_items(self, item_ids: list[int]) -> None:
        """Drop cached usernames so the next poll picks up the new high bidder after a retract."""
        for item_id in item_ids:
            iid = int(item_id)
            self._bidder_cache.pop(iid, None)
            self._bidder_info.pop(iid, None)
            self._pending_names.add(iid)

    @staticmethod
    def _unpack_payment(entry: tuple[int, ...]) -> tuple[int, int, int, int]:
        unpaid = int(entry[0]) if entry else 0
        paid = int(entry[1]) if len(entry) > 1 else 0
        voids = int(entry[2]) if len(entry) > 2 else 0
        bidding = int(entry[3]) if len(entry) > 3 else 0
        return unpaid, paid, voids, bidding

    def _ensure_payment(self, buyer_id: int) -> tuple[int, int, int, int] | None:
        """Look up unpaid/paid/void invoice counts via Seller API. Cached per buyer id."""
        if not buyer_id or not self.seller.signed_in:
            return None
        if buyer_id in self._payment_cache:
            return self._unpack_payment(self._payment_cache[buyer_id])
        try:
            summary = self.seller.fetch_buyer_payment_summary(buyer_id)
        except PermissionError:
            if self._handle_seller_auth_lost():
                return self._ensure_payment(buyer_id)
            return None
        except Exception as exc:  # noqa: BLE001
            self.say(f"Could not check invoices for buyer {buyer_id}: {exc}")
            return None
        unpaid = int(summary.unpaid_count) if summary else 0
        paid = int(summary.paid_count) if summary else 0
        voids = int(summary.void_count) if summary else 0
        bidding = int(summary.items_bidding) if summary else 0
        self._payment_cache[buyer_id] = (unpaid, paid, voids, bidding)
        time.sleep(0.05)
        return unpaid, paid, voids, bidding

    def _freeze_closed_lots(self, items: list) -> int:
        """Lock in high bid / reserve the moment a lot is no longer open.

        CTBids can zero Sale Price on missed-reserve lots as each item closes,
        not only at the end of the sale.
        """
        newly = 0
        for it in items:
            if it.is_open:
                self._frozen.pop(it.item_id, None)
                continue
            prev = self._frozen.get(it.item_id)
            if prev is None:
                newly += 1
            best_price = max(it.bid_price, float(prev["bid_price"]) if prev else 0.0)
            best_count = max(it.bid_count, int(prev["bid_count"]) if prev else 0)
            best_reserve = max(it.reserve_price, float(prev["reserve_price"]) if prev else 0.0)
            bidder = it.high_bidder or (prev or {}).get("high_bidder") or ""
            status = it.item_status
            if best_reserve > 0 and best_price < best_reserve and str(status).lower() == "sold":
                status = "Not Sold"
            it.bid_price = best_price
            it.bid_count = best_count
            it.reserve_price = best_reserve
            if bidder and not it.high_bidder:
                it.high_bidder = str(bidder)
            it.item_status = status
            self._frozen[it.item_id] = {
                "bid_price": best_price,
                "bid_count": best_count,
                "reserve_price": best_reserve,
                "high_bidder": it.high_bidder,
                "item_status": status,
            }
        return newly

    def _write_live_csv(self, snap: Snapshot, newly_closed: int) -> None:
        if not self.auto_save_on_end or not snap.items:
            return
        try:
            path = write_closing_csv(snap, live_snapshot_path(self.sale_id))
            if not self._live_csv_announced:
                self._live_csv_announced = True
                self.say(f"Live bid file: {path}")
            elif newly_closed:
                self.say(f"Saved {newly_closed} newly closed lot(s) to {path.name}")
        except Exception as exc:  # noqa: BLE001
            self.say(f"Could not update live bid file: {exc}")

    def _maybe_save_closing_snapshot(self, prev: Snapshot | None, snap: Snapshot) -> None:
        """When the last lot closes, dump high bids before CTBids zeros them."""
        if not self.auto_save_on_end or self._end_snapshot_saved:
            return
        if not snap.items or snap.open_count > 0:
            return
        # First poll of an already-closed sale, or we just watched the last lot close.
        saw_it_close = prev is not None and prev.open_count > 0
        started_already_over = prev is None
        if not saw_it_close and not started_already_over:
            return
        try:
            self.say("Sale closed — saving high-bid snapshot before CTBids drops it...")
            paths = save_closing_snapshot(
                snap,
                sale_url=f"https://ctbids.com/estate-sale/{self.sale_id}",
            )
            self._end_snapshot_saved = True
            names = ", ".join(p.name for p in paths)
            self.say(f"Saved closing snapshot: {names}")
            if self.on_end_snapshot:
                self.on_end_snapshot(snap, paths)
        except Exception as exc:  # noqa: BLE001
            self.say(f"Could not auto-save closing snapshot: {exc}")

    def _lookup_names(self, item_ids: list[int], items_by_id: dict[int, object]) -> int:
        """Fetch high-bidder nicknames + account flags for the given item ids."""
        got = 0
        i = 0
        while i < len(item_ids):
            if self._stop.is_set():
                break
            item_id = item_ids[i]
            try:
                info = self.client.fetch_high_bidder_info(item_id)
            except PermissionError:
                if self._handle_auth_lost():
                    continue
                break
            except Exception as exc:  # noqa: BLE001
                self.say(f"Could not read bidder for item {item_id}: {exc}")
                self._pending_names.add(item_id)
                i += 1
                continue

            if info and info.nickname and info.nickname != "-":
                self._bidder_cache[item_id] = info.nickname
                self._bidder_info[item_id] = info
                self._pending_names.discard(item_id)
                got += 1
                try:
                    if info.buyer_id:
                        self._ensure_suspend(info.buyer_id)
                        if self.seller.signed_in:
                            self._ensure_payment(info.buyer_id)
                            self._ensure_suspend_here(info.buyer_id)
                except PermissionError:
                    if self._handle_auth_lost():
                        continue
                    break
            else:
                # Bid API often updates before bid history — retry next poll
                self._pending_names.add(item_id)

            item = items_by_id.get(item_id)
            if item is not None:
                self._apply_bidder(item)
            time.sleep(0.06)
            i += 1
        return got

    def poll_once(self) -> tuple[Snapshot, list[BidChange]]:
        self.say(f"Refreshing sale {self.sale_id}...")
        items = self.client.fetch_sale_items(self.sale_id)
        # If CTBids briefly returns nothing (or sale just flipped status), keep last snapshot
        if not items and self.latest and self.latest.items:
            self.say("Sale list empty this pass — keeping last totals / sold lots.")
            return self.latest, []

        ids = [it.item_id for it in items]
        bids = self.client.fetch_current_bids(ids) if ids else {}
        merge_bids(items, bids)
        # If a sold/closed lot briefly returns $0, keep the last known hammer + bid count
        for it in items:
            if it.bid_price <= 0:
                prev = self._last_prices.get(it.item_id, 0.0)
                if prev > 0:
                    it.bid_price = prev
            if it.bid_count <= 0:
                prev_c = self._last_counts.get(it.item_id, 0)
                if prev_c > 0:
                    it.bid_count = prev_c
            if it.reserve_price <= 0:
                prev_res = self._last_reserve.get(it.item_id, 0.0)
                if prev_res > 0:
                    it.reserve_price = prev_res
            bid_info = bids.get(it.item_id) or {}
            if (
                it.is_open
                and str(bid_info.get("bid_status") or "").lower() == "closed"
            ):
                if it.reserve_price > 0 and it.bid_price < it.reserve_price:
                    it.item_status = "Not Sold"
                elif it.bid_price > 0:
                    it.item_status = "Sold"
        newly_closed = self._freeze_closed_lots(items)
        items_by_id = {it.item_id: it for it in items}

        # Split: first bids / price changes / empty history retries are urgent
        urgent: list[int] = []
        backfill: list[int] = []
        for it in items:
            if it.bid_price <= 0 and it.bid_count <= 0:
                self._bidder_cache.pop(it.item_id, None)
                self._bidder_info.pop(it.item_id, None)
                self._pending_names.discard(it.item_id)
                it.high_bidder = ""
                it.buyer_id = 0
                it.membership_completed = False
                it.suspended = False
                it.suspended_here = False
                it.unpaid_invoice_count = None
                it.paid_invoice_count = None
                it.void_invoice_count = None
                continue

            prev_price = self._last_prices.get(it.item_id)
            prev_count = self._last_counts.get(it.item_id)
            cached = self._bidder_cache.get(it.item_id, "")
            price_changed = (
                prev_price is not None and abs(prev_price - it.bid_price) > 0.001
            )
            count_changed = prev_count is not None and it.bid_count != prev_count
            became_bidding = (
                prev_price is not None
                and prev_price <= 0
                and (it.bid_price > 0 or it.bid_count > 0)
            )
            new_item_bidding = (
                prev_price is None
                and self.latest is not None
                and (it.bid_price > 0 or it.bid_count > 0)
            )
            needs_name = not cached or it.item_id in self._pending_names

            if (
                cached
                and not price_changed
                and not count_changed
                and not became_bidding
                and not new_item_bidding
                and not self._force_name_refresh
            ):
                self._apply_bidder(it)
                self._pending_names.discard(it.item_id)
                continue

            if not self.client.signed_in:
                if cached:
                    self._apply_bidder(it)
                continue

            if (
                became_bidding
                or new_item_bidding
                or price_changed
                or count_changed
                or it.item_id in self._pending_names
            ):
                urgent.append(it.item_id)
            elif needs_name or self._force_name_refresh:
                backfill.append(it.item_id)
            elif cached:
                self._apply_bidder(it)

        def _rank(item_id: int) -> tuple[int, float, int]:
            it = items_by_id[item_id]
            missing = 0 if item_id not in self._bidder_cache else 1
            return (missing, -it.bid_price, -it.bid_count)

        urgent.sort(key=_rank)
        backfill.sort(key=_rank)

        if not self.client.signed_in and (urgent or backfill):
            self.say("Signed out - prices still update, but usernames need a CTBids login.")

        if self.client.signed_in and urgent:
            self.say(f"Fetching usernames for {len(urgent)} new/changed bid(s)...")
            got = self._lookup_names(urgent, items_by_id)
            self.say(f"Got {got}/{len(urgent)} changed-bid username(s).")

        per_poll = 100
        batch = backfill[:per_poll]
        if self.client.signed_in and batch:
            self.say(
                f"Backfilling {len(batch)} username(s)"
                + (f" ({len(backfill) - len(batch)} more next pass)..." if len(backfill) > len(batch) else "...")
            )
            got = self._lookup_names(batch, items_by_id)
            self.say(f"Cached {got} more username(s).")

        if self.client.signed_in and not any(
            (it.bid_price > 0 or it.bid_count > 0) and it.item_id not in self._bidder_cache
            for it in items
        ) and not self._pending_names:
            self._force_name_refresh = False

        # Seller login: fill paid/unpaid counts for every known high bidder
        if self.seller.signed_in:
            known_buyers = {
                info.buyer_id
                for info in self._bidder_info.values()
                if info and info.buyer_id
            }
            for buyer_id in known_buyers:
                if self._stop.is_set():
                    break
                if buyer_id not in self._payment_cache:
                    self._ensure_payment(buyer_id)
                if buyer_id not in self._suspend_here_cache:
                    self._ensure_suspend_here(buyer_id)

        for it in items:
            self._last_prices[it.item_id] = it.bid_price
            self._last_counts[it.item_id] = it.bid_count
            if it.reserve_price > 0:
                self._last_reserve[it.item_id] = it.reserve_price
            if it.item_id in self._bidder_cache:
                self._apply_bidder(it)
            if it.item_id in self._frozen:
                frozen = self._frozen[it.item_id]
                frozen["high_bidder"] = it.high_bidder or frozen.get("high_bidder") or ""
                frozen["bid_price"] = max(it.bid_price, float(frozen.get("bid_price") or 0))
                frozen["bid_count"] = max(it.bid_count, int(frozen.get("bid_count") or 0))
                frozen["reserve_price"] = max(it.reserve_price, float(frozen.get("reserve_price") or 0))
                frozen["item_status"] = it.item_status

        snap = build_snapshot(self.sale_id, items)
        with self._lock:
            prev = self.latest
            changes = diff_snapshots(prev, snap)
            self.latest = snap
        self._maybe_save_closing_snapshot(prev, snap)
        self._write_live_csv(snap, newly_closed)
        return snap, changes

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                snap, changes = self.poll_once()
                if self.on_update:
                    self.on_update(snap, changes)
                named = sum(1 for it in snap.items.values() if it.high_bidder)
                self.say(
                    f"OK — {snap.bidding_count} bidding / {snap.open_count} open / "
                    f"{snap.sold_count} sold · hammer ${snap.hammer_total:,.2f} · {named} username(s)"
                )
            except PermissionError as exc:
                self.say(str(exc))
            except Exception as exc:  # noqa: BLE001
                self.say(f"Refresh error: {exc}")
            self._stop.wait(self.poll_seconds)
