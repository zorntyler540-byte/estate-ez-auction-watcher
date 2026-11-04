"""Engagement snapshot: completeness, missing attention, captured_at ordering, owned sales, the `complete` flag."""

from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from watcher_test_support import TempHomeTestCase, make_jwt, make_response

import engagement_sync  # noqa: E402  (path set by watcher_test_support)
import estate_ez  # noqa: E402
from ctbids_client import FetchReport, LiveItem, build_snapshot  # noqa: E402

# Fields the current Estate-Ez server accepts on EngagementSnapshotIn (extra="forbid").
SERVER_SNAPSHOT_FIELDS = {
    "ctseller_sale_id",
    "sale_title",
    "auction_url",
    "sale_ends_at",
    "captured_at",
    "items",
}
SERVER_ITEM_FIELDS = engagement_sync.ALLOWED_ITEM_KEYS

T0 = datetime(2026, 11, 4, 21, 0, tzinfo=timezone.utc)


def _item(item_id: int, status: str = "Ready", *, bids: int = 0) -> LiveItem:
    it = LiveItem(
        item_id=item_id,
        title=f"Lot {item_id}",
        sku=f"SKU{item_id}",
        close_time="2026-11-05T18:00:00-08:00",
        item_status=status,
        bid_count=bids,
        bid_price=5.0 if bids else 0.0,
    )
    it.link = f"https://ctbids.com/estate-sale/48212/item/{item_id}"
    return it


def _full(n: int) -> FetchReport:
    return FetchReport(expected=n, fetched=n, pages=1)


def _history(*ids: int, views: object = 10, watchers: object = 2) -> list[dict]:
    return [{"itemid": i, "viewcount": views, "watchercount": watchers} for i in ids]


class CompletenessTests(unittest.TestCase):
    def test_complete_only_with_full_fetches_and_full_attention(self) -> None:
        body, comp = engagement_sync.build_snapshot_payload(
            48212,
            [_item(1), _item(2)],
            _history(1, 2),
            item_report=_full(2),
            history_report=_full(2),
            include_complete=True,
        )
        self.assertTrue(comp.complete)
        self.assertIs(body["complete"], True)

    def test_missing_watchers_are_omitted_not_zeroed(self) -> None:
        history = _history(1) + [{"itemid": 2, "viewcount": 7, "watchercount": ""}]
        body, comp = engagement_sync.build_snapshot_payload(
            48212,
            [_item(1), _item(2), _item(3)],
            history,
            item_report=_full(3),
            history_report=_full(3),
            include_complete=True,
        )
        by_id = {row["ctseller_item_id"]: row for row in body["items"]}
        self.assertEqual((by_id[1]["views"], by_id[1]["watchers"]), (10, 2))
        self.assertEqual(by_id[2]["views"], 7)
        self.assertNotIn("watchers", by_id[2])
        self.assertNotIn("views", by_id[3])
        self.assertNotIn("watchers", by_id[3])
        self.assertFalse(comp.complete)
        self.assertEqual(comp.missing_attention, 2)
        self.assertIs(body["complete"], False)

    def test_real_zero_counts_are_still_sent(self) -> None:
        body, comp = engagement_sync.build_snapshot_payload(
            48212, [_item(1)], _history(1, views=0, watchers="0"),
            item_report=_full(1), history_report=_full(1),
        )
        self.assertEqual((body["items"][0]["views"], body["items"][0]["watchers"]), (0, 0))
        self.assertTrue(comp.complete)

    def test_truncated_or_unknown_fetches_are_incomplete(self) -> None:
        cases = {
            "items cut off": (FetchReport(expected=5, fetched=2, truncated=True), _full(1)),
            "history cut off": (_full(1), FetchReport(expected=3, fetched=1, truncated=True)),
            "item total unknown": (FetchReport(expected=None, fetched=1), _full(1)),
            "no item report": (None, _full(1)),
            "no history report": (_full(1), None),
        }
        for label, (item_report, history_report) in cases.items():
            with self.subTest(label):
                _body, comp = engagement_sync.build_snapshot_payload(
                    48212, [_item(1)], _history(1),
                    item_report=item_report, history_report=history_report,
                )
                self.assertFalse(comp.complete)
                self.assertTrue(comp.reasons)

    def test_pending_lots_are_included(self) -> None:
        body, _comp = engagement_sync.build_snapshot_payload(
            48212, [_item(1), _item(2, "Pending")], _history(1, 2),
            item_report=_full(2), history_report=_full(2),
        )
        statuses = {row["ctseller_item_id"]: row["item_status"] for row in body["items"]}
        self.assertEqual(statuses, {1: "Ready", 2: "Pending"})

    def test_default_body_only_has_fields_the_current_server_accepts(self) -> None:
        body, _comp = engagement_sync.build_snapshot_payload(
            48212, [_item(1), _item(2)], _history(1),
            captured_at=T0, item_report=_full(2), history_report=_full(2),
        )
        self.assertNotIn("complete", body)
        self.assertLessEqual(set(body), SERVER_SNAPSHOT_FIELDS)
        for row in body["items"]:
            self.assertLessEqual(set(row), SERVER_ITEM_FIELDS)
        self.assertEqual(body["captured_at"], "2026-11-04T21:00:00+00:00")

    def test_captured_at_is_sent_in_utc(self) -> None:
        local = datetime(2026, 11, 4, 13, 0, tzinfo=timezone(timedelta(hours=-8)))
        body = engagement_sync.build_snapshot_body(48212, [_item(1)], _history(1), captured_at=local)
        self.assertEqual(body["captured_at"], "2026-11-04T21:00:00+00:00")


class SendCompleteFlagTests(TempHomeTestCase):
    def _write_config(self, data: dict) -> None:
        (self.data_dir / "watcher_config.json").write_text(json.dumps(data), encoding="utf-8")

    def test_off_by_default(self) -> None:
        self.assertFalse(engagement_sync.send_complete_enabled())
        self._write_config({"poll_seconds": 45})
        self.assertFalse(engagement_sync.send_complete_enabled())

    def test_config_flag_turns_it_on(self) -> None:
        self._write_config({"engagement_send_complete": True})
        self.assertTrue(engagement_sync.send_complete_enabled())

    def test_env_var_wins_over_config(self) -> None:
        self._write_config({"engagement_send_complete": True})
        with mock.patch.dict(os.environ, {"ESTATE_EZ_SEND_COMPLETE": "0"}):
            self.assertFalse(engagement_sync.send_complete_enabled())
        with mock.patch.dict(os.environ, {"ESTATE_EZ_SEND_COMPLETE": "1"}):
            self._write_config({})
            self.assertTrue(engagement_sync.send_complete_enabled())

    def test_broken_config_stays_off(self) -> None:
        (self.data_dir / "watcher_config.json").write_text("{not json", encoding="utf-8")
        self.assertFalse(engagement_sync.send_complete_enabled())


class FakeSeller:
    signed_in = True

    def __init__(self, *, listed: set[int], history: list[dict], report: FetchReport) -> None:
        self.listed = listed
        self.history = history
        self.report = report
        self.history_calls = 0

    def sale_is_listed(self, sale_id: int) -> bool:
        return int(sale_id) in self.listed

    def fetch_sale_history_report(self, sale_id: int):
        self.history_calls += 1
        return self.history, self.report


class PushSaleSnapshotTests(TempHomeTestCase):
    def setUp(self) -> None:
        super().setUp()
        engagement_sync._last_sent_at.clear()
        self.addCleanup(engagement_sync._last_sent_at.clear)
        patcher = mock.patch.object(estate_ez, "ensure_estate_ez_token", return_value=make_jwt(3600))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.posted: list[dict] = []

        def fake_post(_token, body):
            self.posted.append(body)
            return {"item_count": len(body["items"]), "push": 1, "matched_lots": 0}

        post_patch = mock.patch.object(engagement_sync, "post_snapshot_body", side_effect=fake_post)
        post_patch.start()
        self.addCleanup(post_patch.stop)

    def _snap(self, captured_at: datetime, *, pending: bool = False):
        live = [_item(1, bids=2)]
        pend = [_item(2, "Pending")] if pending else []
        n = len(live) + len(pend)
        return build_snapshot(
            48212, live, captured_at=captured_at, pending_items=pend, item_report=_full(n)
        )

    def _seller(self, *, listed=(48212,), n: int = 1) -> FakeSeller:
        return FakeSeller(listed=set(listed), history=_history(*range(1, n + 1)), report=_full(n))

    def test_sends_captured_at_and_pending_lots(self) -> None:
        note = engagement_sync.push_sale_snapshot(48212, self._snap(T0, pending=True), self._seller(n=2))
        self.assertIn("Sent 2 lots", note)
        self.assertNotIn("Partial", note)
        body = self.posted[0]
        self.assertEqual(body["captured_at"], T0.isoformat())
        self.assertEqual({row["ctseller_item_id"] for row in body["items"]}, {1, 2})
        self.assertNotIn("complete", body)

    def test_complete_is_sent_when_flag_is_on(self) -> None:
        with mock.patch.dict(os.environ, {"ESTATE_EZ_SEND_COMPLETE": "1"}):
            engagement_sync.push_sale_snapshot(48212, self._snap(T0), self._seller())
        self.assertIs(self.posted[0]["complete"], True)

    def test_partial_note_when_attention_is_missing(self) -> None:
        seller = FakeSeller(listed={48212}, history=[], report=_full(0))
        note = engagement_sync.push_sale_snapshot(48212, self._snap(T0), seller)
        self.assertIn("Partial", note)
        self.assertNotIn("views", self.posted[0]["items"][0])

    def test_never_posts_an_older_snapshot(self) -> None:
        seller = self._seller()
        engagement_sync.push_sale_snapshot(48212, self._snap(T0 + timedelta(minutes=1)), seller)
        note = engagement_sync.push_sale_snapshot(48212, self._snap(T0), seller)
        same = engagement_sync.push_sale_snapshot(48212, self._snap(T0 + timedelta(minutes=1)), seller)
        self.assertEqual(len(self.posted), 1)
        self.assertIn("older than the last one sent", note)
        self.assertIn("older than the last one sent", same)
        self.assertEqual(seller.history_calls, 1)
        engagement_sync.push_sale_snapshot(48212, self._snap(T0 + timedelta(minutes=2)), seller)
        self.assertEqual(len(self.posted), 2)

    def test_ordering_is_per_sale(self) -> None:
        seller = self._seller(listed=(48212, 50000))
        engagement_sync.push_sale_snapshot(48212, self._snap(T0 + timedelta(minutes=5)), seller)
        other = build_snapshot(50000, [_item(1)], captured_at=T0, item_report=_full(1))
        engagement_sync.push_sale_snapshot(50000, other, seller)
        self.assertEqual(len(self.posted), 2)

    def test_failed_post_does_not_advance_last_sent(self) -> None:
        seller = self._seller()
        with mock.patch.object(engagement_sync, "post_snapshot_body", side_effect=RuntimeError("down")):
            with self.assertRaises(RuntimeError):
                engagement_sync.push_sale_snapshot(48212, self._snap(T0), seller)
        self.assertIsNone(engagement_sync.last_sent_at(48212))
        engagement_sync.push_sale_snapshot(48212, self._snap(T0), seller)
        self.assertEqual(len(self.posted), 1)

    def test_sale_not_on_seller_list_is_not_pushed(self) -> None:
        seller = self._seller(listed=(11111,))
        note = engagement_sync.push_sale_snapshot(48212, self._snap(T0), seller)
        self.assertIn("not on this CT Seller login", note)
        self.assertEqual(self.posted, [])
        self.assertEqual(seller.history_calls, 0)


class PostSnapshotTokenTests(TempHomeTestCase):
    def test_401_clears_only_the_token_that_failed(self) -> None:
        failed = make_jwt(3600, n=1)
        newer = make_jwt(3600, n=2)
        estate_ez.save_estate_ez_token(newer)
        with mock.patch.object(engagement_sync.requests, "post", return_value=make_response(401)):
            with self.assertRaises(RuntimeError):
                engagement_sync.post_snapshot_body(failed, {"items": []})
        self.assertEqual(estate_ez.load_estate_ez_token(), newer)

        estate_ez.save_estate_ez_token(failed)
        with mock.patch.object(engagement_sync.requests, "post", return_value=make_response(401)):
            with self.assertRaises(RuntimeError):
                engagement_sync.post_snapshot_body(failed, {"items": []})
        self.assertIsNone(estate_ez.load_estate_ez_token())


if __name__ == "__main__":
    unittest.main()
