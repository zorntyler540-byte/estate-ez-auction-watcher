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

ProgressFn = Callable[[str], None]
SnapshotFn = Callable[[Snapshot, list[BidChange]], None]


def watcher_data_dir() -> Path:
    base = Path.home() / "AppData" / "Local" / "SaleWatcher"
    base.mkdir(parents=True, exist_ok=True)
    return base


def profile_dir() -> Path:
    path = watcher_data_dir() / "browser_profile"
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return watcher_data_dir() / "watcher_config.json"


def load_watcher_config() -> dict:
    path = config_path()
    if not path.exists():
        return {"sale_url": "", "poll_seconds": 45, "theme": "dark"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"sale_url": "", "poll_seconds": 45, "theme": "dark"}
    data.setdefault("theme", "dark")
    return data


def save_watcher_config(cfg: dict) -> None:
    config_path().write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def token_path() -> Path:
    return watcher_data_dir() / "access_token.txt"


def save_access_token(token: str | None) -> None:
    path = token_path()
    if token:
        path.write_text(token.strip(), encoding="utf-8")
    elif path.exists():
        path.unlink()


def load_saved_access_token() -> str | None:
    path = token_path()
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def read_access_token_from_profile() -> str | None:
    """Launch a short Playwright context against the saved profile and read localStorage.access."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed. Run:\n"
            "  pip install playwright\n"
            "  playwright install chromium"
        ) from exc

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir()),
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://ctbids.com/", wait_until="domcontentloaded", timeout=60000)
            token = page.evaluate("() => window.localStorage.getItem('access')")
            token = (token or "").strip() or None
            if token:
                save_access_token(token)
            return token
        finally:
            context.close()


def resolve_access_token(*, validate: bool = True) -> str | None:
    """Prefer a freshly read profile token; fall back to the last saved token.

    When validate=True, only return a token that can actually read bid history.
    """
    candidates: list[str] = []
    try:
        token = read_access_token_from_profile()
        if token:
            candidates.append(token)
    except Exception:
        pass
    saved = load_saved_access_token()
    if saved and saved not in candidates:
        candidates.append(saved)

    if not validate:
        return candidates[0] if candidates else None

    for token in candidates:
        client = CtbidsClient(token)
        if client.token_works():
            save_access_token(token)
            return token

    # Stale tokens — clear so the UI doesn't pretend we're signed in
    if candidates:
        save_access_token(None)
    return None


def open_login_window(sale_url: str = "https://ctbids.com/") -> str | None:
    """Open a visible Chromium window so the boss can sign in; return access token if present."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed. Run:\n"
            "  pip install playwright\n"
            "  playwright install chromium"
        ) from exc

    target = sale_url.strip() or "https://ctbids.com/"
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir()),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(target, wait_until="domcontentloaded", timeout=90000)
        # Wait until user closes the window (or up to 15 minutes)
        deadline = time.time() + 15 * 60
        token: str | None = None
        while time.time() < deadline:
            try:
                if not context.pages:
                    break
                token = page.evaluate("() => window.localStorage.getItem('access')")
                page.wait_for_timeout(1000)
            except Exception:
                break
        try:
            token = page.evaluate("() => window.localStorage.getItem('access')")
        except Exception:
            pass
        try:
            context.close()
        except Exception:
            pass
        token = (token or "").strip() or None
        if token:
            # Only keep it if CTBids accepts it for bid history
            if CtbidsClient(token).token_works():
                save_access_token(token)
                return token
            save_access_token(None)
            return None
        return None


class SaleWatcher:
    """Background poller for one CTBids sale."""

    def __init__(
        self,
        sale_url_or_id: str,
        access_token: str | None = None,
        poll_seconds: float = 45.0,
        on_update: SnapshotFn | None = None,
        on_status: ProgressFn | None = None,
        on_auth_lost: ProgressFn | None = None,
    ) -> None:
        self.sale_id = parse_sale_id(sale_url_or_id)
        self.poll_seconds = max(10.0, float(poll_seconds))
        self.on_update = on_update
        self.on_status = on_status
        self.on_auth_lost = on_auth_lost
        self.client = CtbidsClient(access_token)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.latest: Snapshot | None = None
        self._bidder_cache: dict[int, str] = {}
        self._bidder_info: dict[int, HighBidderInfo] = {}
        self._suspend_cache: dict[int, bool] = {}  # buyer_id -> suspended
        self._last_prices: dict[int, float] = {}
        self._last_counts: dict[int, int] = {}
        self._pending_names: set[int] = set()  # bid moved but history not ready yet
        self._force_name_refresh = bool(access_token)
        self._auth_recovery_attempted = False

    def say(self, msg: str) -> None:
        if self.on_status:
            self.on_status(msg)

    def set_token(self, token: str | None) -> None:
        self.client.set_token(token)
        # Next poll should backfill usernames with the new login
        self._force_name_refresh = bool(token)
        self._auth_recovery_attempted = False
        if not token:
            self._bidder_cache.clear()
            self._bidder_info.clear()
            self._suspend_cache.clear()
            self._pending_names.clear()

    def invalidate_usernames(self) -> None:
        self._bidder_cache.clear()
        self._bidder_info.clear()
        self._suspend_cache.clear()
        self._pending_names.clear()
        self._force_name_refresh = True

    def stop(self) -> None:
        self._stop.set()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _handle_auth_lost(self) -> bool:
        """Try one profile re-read; otherwise clear token and notify UI. Returns True if recovered."""
        if not self._auth_recovery_attempted:
            self._auth_recovery_attempted = True
            self.say("Login expired — trying to refresh from browser profile…")
            try:
                fresh = resolve_access_token(validate=True)
            except Exception:
                fresh = None
            if fresh:
                self.client.set_token(fresh)
                self._force_name_refresh = True
                self.say("Login restored — fetching usernames.")
                return True

        self.client.set_token(None)
        save_access_token(None)
        msg = "CTBids login expired. Click Sign in again to load usernames."
        self.say(msg)
        if self.on_auth_lost:
            self.on_auth_lost(msg)
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
        else:
            item.buyer_id = 0
            item.membership_completed = False
            item.suspended = False
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
        ids = [it.item_id for it in items]
        bids = self.client.fetch_current_bids(ids) if ids else {}
        merge_bids(items, bids)
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

        for it in items:
            self._last_prices[it.item_id] = it.bid_price
            self._last_counts[it.item_id] = it.bid_count
            if it.item_id in self._bidder_cache:
                self._apply_bidder(it)

        snap = build_snapshot(self.sale_id, items)
        with self._lock:
            prev = self.latest
            changes = diff_snapshots(prev, snap)
            self.latest = snap
        return snap, changes

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                snap, changes = self.poll_once()
                if self.on_update:
                    self.on_update(snap, changes)
                named = sum(1 for it in snap.items.values() if it.high_bidder)
                self.say(
                    f"OK — {snap.bidding_count} bidding / {snap.open_count} open · "
                    f"hammer ${snap.hammer_total:,.2f} · {named} username(s)"
                )
            except PermissionError as exc:
                self.say(str(exc))
            except Exception as exc:  # noqa: BLE001
                self.say(f"Refresh error: {exc}")
            self._stop.wait(self.poll_seconds)
