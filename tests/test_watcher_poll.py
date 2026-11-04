"""SaleWatcher stamps captured_at when the item list returns and keeps pending lots off the live board."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest import mock

from watcher_test_support import TempHomeTestCase

from ctbids_client import FetchReport, LiveItem  # noqa: E402
import watcher as watcher_mod  # noqa: E402


class PollOnceTests(TempHomeTestCase):
    def test_snapshot_carries_capture_time_pending_lots_and_report(self) -> None:
        sw = watcher_mod.SaleWatcher("48212", auto_save_on_end=False)
        items = [
            LiveItem(item_id=1, title="Ready lot", item_status="Ready"),
            LiveItem(item_id=2, title="Photo pending", item_status="Pending"),
        ]
        report = FetchReport(expected=2, fetched=2, pending=1, pages=1)
        before = datetime.now(timezone.utc)
        with (
            mock.patch.object(sw.client, "fetch_sale_items_report", return_value=(items, report)),
            mock.patch.object(sw.client, "fetch_current_bids", return_value={}) as bids,
            mock.patch.object(sw, "_push_engagement") as push,
        ):
            snap, _changes = sw.poll_once()
        self.assertEqual(list(snap.items), [1])
        self.assertEqual(list(snap.pending_items), [2])
        self.assertIs(snap.item_report, report)
        self.assertIsNotNone(snap.captured_at)
        self.assertGreaterEqual(snap.captured_at, before)
        self.assertEqual(bids.call_args.args[0], [1])
        self.assertEqual(snap.open_count, 1)
        push.assert_called_once_with(snap)


if __name__ == "__main__":
    unittest.main()
