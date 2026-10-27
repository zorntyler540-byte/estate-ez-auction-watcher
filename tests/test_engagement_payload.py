"""The Watcher snapshot sent to Estate-EZ has no bidder PII."""

from __future__ import annotations

import json
import unittest

from sale_watcher.ctbids_client import LiveItem
from sale_watcher.engagement_sync import FORBIDDEN_ITEM_KEYS, build_snapshot_body


class EngagementPayloadTests(unittest.TestCase):
    def test_history_pii_and_bidder_fields_stay_off_the_payload(self) -> None:
        item = LiveItem(
            item_id=5667527,
            title="Playing cards",
            sku="TG368AD",
            close_time="2026-10-20T18:00:00-07:00",
            reserve_price=5,
            bid_price=5,
            bid_count=2,
            high_bidder="alice",
            buyer_id=4242,
            item_status="Ready",
        )
        item.link = "https://ctbids.com/estate-sale/48212/item/5667527"
        history = [
            {
                "itemid": 5667527,
                "viewcount": 25,
                "wishlistcount": 8,
                "email": "winner@example.com",
                "firstname": "Pat",
                "lastname": "Lee",
                "nickname": "patlee",
                "phone": "702-555-0100",
            }
        ]
        body = build_snapshot_body(48212, [item], history)
        blob = json.dumps(body).lower()
        self.assertNotIn("alice", blob)
        self.assertNotIn("winner@example.com", blob)
        self.assertNotIn("patlee", blob)
        self.assertNotIn("702-555", blob)
        self.assertNotIn("pat", blob)
        item_keys = set(body["items"][0])
        self.assertTrue(item_keys.isdisjoint(FORBIDDEN_ITEM_KEYS))
        self.assertEqual(body["items"][0]["views"], 25)
        self.assertEqual(body["items"][0]["watchers"], 8)
        self.assertEqual(body["items"][0]["bids"], 2)
        self.assertEqual(body["items"][0]["price"], 5)
        self.assertEqual(body["ctseller_sale_id"], 48212)
        self.assertNotIn("high_bidder", body["items"][0])

    def test_watchercount_alias_still_counts(self) -> None:
        item = LiveItem(item_id=1, title="Quilt", sku="MM500", bid_price=13, bid_count=1)
        body = build_snapshot_body(
            1,
            [item],
            [{"itemid": "1", "viewcount": "9", "watchercount": "3", "email": "hidden@example.com"}],
        )
        self.assertEqual(body["items"][0]["views"], 9)
        self.assertEqual(body["items"][0]["watchers"], 3)
        self.assertNotIn("hidden@example.com", json.dumps(body))


if __name__ == "__main__":
    unittest.main()
