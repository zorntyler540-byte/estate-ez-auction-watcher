"""Per-sale Estate-Ez push status: one sale's result never overwrites another's, and errors stay visible."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import requests

from watcher_test_support import TempHomeTestCase, make_response

from ctbids_client import LiveItem, Snapshot  # noqa: E402
import engagement_sync  # noqa: E402
import push_status  # noqa: E402
import watcher as watcher_mod  # noqa: E402
from push_status import FAILED, PARTIAL, SENT, SKIPPED, PushResult, SalePushStatus  # noqa: E402

T0 = datetime(2026, 11, 4, 18, 0, 0, tzinfo=timezone.utc)


def _at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


class SalePushStatusTests(unittest.TestCase):
    def test_starts_idle(self) -> None:
        status = SalePushStatus(1)
        self.assertEqual(status.level, "idle")
        self.assertEqual(status.summary(), "Estate-Ez push: not yet")

    def test_results_for_another_sale_are_ignored(self) -> None:
        a, b = SalePushStatus(1), SalePushStatus(2)
        sent_b = PushResult(2, SENT, "sent b", at=_at(0), item_count=9)
        self.assertFalse(a.record(sent_b))
        self.assertTrue(b.record(sent_b))
        self.assertIsNone(a.last_sent_at)
        self.assertEqual(b.last_sent_items, 9)

    def test_older_result_does_not_clobber_newer(self) -> None:
        status = SalePushStatus(1)
        status.record(PushResult(1, SENT, "new", at=_at(60), item_count=42))
        self.assertFalse(status.record(PushResult(1, FAILED, "old failure", at=_at(10), error="boom")))
        self.assertEqual(status.last_message, "new")
        self.assertEqual(status.last_error, "")
        self.assertEqual(status.level, "ok")

    def test_failure_keeps_last_success_and_shows_error(self) -> None:
        status = SalePushStatus(1)
        status.record(PushResult(1, SENT, "Engagement snapshot sent.", at=_at(0), item_count=42))
        status.record(PushResult(1, FAILED, "Engagement snapshot not sent: down", at=_at(45), error="down"))
        self.assertEqual(status.level, "error")
        self.assertTrue(status.has_current_error)
        self.assertEqual(status.last_sent_at, _at(0))
        self.assertEqual(status.last_sent_items, 42)
        self.assertEqual(status.last_error, "down")
        self.assertEqual(status.consecutive_failures, 1)
        summary = status.summary()
        self.assertIn("failed", summary)
        self.assertIn("last sent", summary)
        labels = dict(status.details())
        self.assertIn("down", labels["Last error"])
        self.assertEqual(labels["Failures in a row"], "1")

    def test_success_after_failure_clears_the_current_error(self) -> None:
        status = SalePushStatus(1)
        status.record(PushResult(1, FAILED, "x", at=_at(0), error="down"))
        status.record(PushResult(1, FAILED, "x", at=_at(45), error="down"))
        self.assertEqual(status.consecutive_failures, 2)
        status.record(PushResult(1, SENT, "ok", at=_at(90), item_count=3))
        self.assertFalse(status.has_current_error)
        self.assertEqual(status.consecutive_failures, 0)
        self.assertEqual(status.level, "ok")
        # The last error stays in the details for support.
        self.assertIn("Last error", dict(status.details()))

    def test_skip_does_not_hide_the_last_result_or_error(self) -> None:
        status = SalePushStatus(1)
        status.record(PushResult(1, SENT, "sent", at=_at(0), item_count=5))
        status.record(PushResult(1, FAILED, "failed", at=_at(30), error="down"))
        status.record(PushResult(1, SKIPPED, "Sign in to Estate-Ez to send the engagement snapshot.", at=_at(60)))
        self.assertEqual(status.last_outcome, SKIPPED)
        self.assertEqual(status.last_sent_at, _at(0))
        self.assertTrue(status.has_current_error)
        self.assertEqual(status.level, "error")

    def test_partial_is_a_warning(self) -> None:
        status = SalePushStatus(1)
        status.record(PushResult(1, PARTIAL, "partial", at=_at(0), item_count=40))
        self.assertEqual(status.level, "warn")
        self.assertIn("partial", status.summary())

    def test_clock_formats_local_time(self) -> None:
        self.assertEqual(push_status.clock(None), "—")
        self.assertRegex(push_status.clock(T0), r"^\d{1,2}:\d{2}:\d{2} (AM|PM)$")


def _snap(sale_id: int = 48212) -> Snapshot:
    item = LiveItem(item_id=1, title="Lamp", sku="MS-1", bid_price=10.0, bid_count=1)
    return Snapshot(sale_id=sale_id, items={1: item}, captured_at=datetime.now(timezone.utc))


class PushResultTests(TempHomeTestCase):
    def test_failure_becomes_a_failed_result_with_a_friendly_message(self) -> None:
        boom = requests.ConnectionError("HTTPSConnectionPool(host='api'): Max retries exceeded")
        with mock.patch.object(engagement_sync, "_push", side_effect=boom):
            result = engagement_sync.push_sale_snapshot_result(48212, _snap(), seller=None)
        self.assertEqual(result.outcome, FAILED)
        self.assertEqual(result.sale_id, 48212)
        self.assertNotIn("HTTPSConnectionPool", result.message)
        self.assertIn("Estate-Ez", result.message)
        self.assertTrue(result.error)

    def test_legacy_string_api_still_raises(self) -> None:
        with mock.patch.object(engagement_sync, "_push", side_effect=RuntimeError("down")):
            with self.assertRaises(RuntimeError):
                engagement_sync.push_sale_snapshot(48212, _snap(), seller=None)

    def test_skip_reasons_are_skipped_results(self) -> None:
        with mock.patch.object(engagement_sync, "_estate_ez") as ee:
            ee.return_value.ensure_estate_ez_token.return_value = None
            result = engagement_sync.push_sale_snapshot_result(48212, _snap(), seller=None)
        self.assertEqual(result.outcome, SKIPPED)
        self.assertIn("Sign in to Estate-Ez", result.message)


class SnapshotPostRetryTests(TempHomeTestCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch("time.sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_snapshot_post_retries_503(self) -> None:
        post = mock.Mock(side_effect=[make_response(503), make_response(200, {"stored": True})])
        with mock.patch.object(engagement_sync.requests, "post", post):
            self.assertEqual(engagement_sync.post_snapshot_body("tok", {"sale_id": 1}), {"stored": True})
        self.assertEqual(post.call_count, 2)

    def test_snapshot_post_does_not_retry_401(self) -> None:
        post = mock.Mock(return_value=make_response(401, {"detail": "expired"}))
        with mock.patch.object(engagement_sync.requests, "post", post):
            with self.assertRaises(Exception):
                engagement_sync.post_snapshot_body("tok", {"sale_id": 1})
        self.assertEqual(post.call_count, 1)


class WatcherCallbackTests(TempHomeTestCase):
    def test_push_result_goes_to_on_push_for_that_sale(self) -> None:
        pushed: list[PushResult] = []
        said: list[str] = []
        sw = watcher_mod.SaleWatcher(
            "48212", auto_save_on_end=False, on_push=pushed.append, on_status=said.append,
        )
        result = PushResult(48212, SENT, "Engagement snapshot sent.", item_count=1)
        with mock.patch.object(engagement_sync, "push_sale_snapshot_result", return_value=result):
            sw._push_engagement(_snap())
        self.assertEqual(pushed, [result])
        self.assertIn("Engagement snapshot sent.", said)

    def test_a_broken_on_push_callback_does_not_stop_polling(self) -> None:
        sw = watcher_mod.SaleWatcher("48212", auto_save_on_end=False, on_push=mock.Mock(side_effect=ValueError))
        result = PushResult(48212, FAILED, "x", error="x")
        with mock.patch.object(engagement_sync, "push_sale_snapshot_result", return_value=result):
            sw._push_engagement(_snap())

    def test_run_reports_sync_start_ok_and_friendly_errors(self) -> None:
        events: list[tuple[str, str]] = []
        sw = watcher_mod.SaleWatcher(
            "48212", auto_save_on_end=False, on_sync=lambda state, msg: events.append((state, msg)),
        )
        calls = {"n": 0}

        def poll():
            calls["n"] += 1
            if calls["n"] == 1:
                return _snap(), []
            sw._stop.set()
            raise requests.ConnectionError("HTTPSConnectionPool(host='ctbids.com'): Max retries exceeded")

        sw.poll_seconds = 0
        with mock.patch.object(sw, "poll_once", side_effect=poll):
            sw._run()
        states = [state for state, _ in events]
        self.assertEqual(states, ["start", "ok", "start", "error"])
        self.assertNotIn("HTTPSConnectionPool", events[-1][1])
        self.assertIn("CTBids", events[-1][1])


if __name__ == "__main__":
    unittest.main()
