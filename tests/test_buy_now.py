"""Buy It Now price is tracked, and a completed purchase shows as sold."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sale_watcher"))

from sale_watcher.ctbids_client import (
    LiveItem,
    Snapshot,
    apply_buy_now,
    diff_snapshots,
)
from sale_watcher.engagement_sync import build_snapshot_body
from sale_watcher.export_report import write_closing_csv
from sale_watcher.seller_client import SellerClient
from sale_watcher.watcher import SaleWatcher


def _lot(**kwargs) -> LiveItem:
    base = dict(item_id=10, title="Quilt", sku="BN100", item_status="Ready", reserve_price=40)
    base.update(kwargs)
    return LiveItem(**base)


class BuyNowTests(unittest.TestCase):
    def test_active_price_does_not_count_as_a_bid(self) -> None:
        item = _lot()
        apply_buy_now(
            [item],
            [{
                "id": 10,
                "itemtypeid": 1,
                "itemtypename": "Direct buy",
                "itemstatus": "Ready",
                "buynowprice": 25,
                "email": "buyer@example.com",
                "firstname": "Pat",
            }],
        )
        self.assertEqual(item.buy_now_price, 25)
        self.assertEqual(item.bid_price, 0)
        self.assertEqual(item.bid_count, 0)
        self.assertEqual(item.item_status, "Ready")
        self.assertFalse(item.bought_now)
        self.assertTrue(item.is_direct_buy)
        blob = json.dumps(item.to_dict()).lower()
        self.assertNotIn("buyer@example.com", blob)
        self.assertNotIn("pat", blob)

        body = build_snapshot_body(48212, [item], [])
        self.assertEqual(body["items"][0]["price"], 0)
        self.assertEqual(body["items"][0]["bids"], 0)
        self.assertNotIn("buy_now_price", body["items"][0])

    def test_sold_direct_buy_fills_the_price_and_stays_sold(self) -> None:
        item = _lot(bid_price=0, bid_count=0)
        apply_buy_now(
            [item],
            [{
                "id": "10",
                "itemtypename": "Direct buy",
                "itemstatus": "Sold",
                "buynowprice": 25,
                "soldprice": 0,
                "itemsoldprice": 25,
            }],
        )
        watcher = SaleWatcher("48212")
        watcher._freeze_closed_lots([item])
        self.assertTrue(item.bought_now)
        self.assertEqual(item.item_status, "Sold")
        self.assertEqual(item.bid_price, 25)
        self.assertEqual(item.buy_now_price, 25)

    def test_missed_reserve_auction_is_still_not_sold(self) -> None:
        item = _lot(item_status="Sold", bid_price=10, bid_count=2, item_type_id=2, item_type_name="Auction")
        watcher = SaleWatcher("48212")
        watcher._freeze_closed_lots([item])
        self.assertEqual(item.item_status, "Not Sold")
        self.assertEqual(item.bid_price, 10)

    def test_auction_buy_now_with_no_bids_uses_the_purchase_amount(self) -> None:
        item = _lot(item_type_id=2, item_type_name="Auction")
        apply_buy_now(
            [item],
            [{
                "itemid": 10,
                "itemtypeid": 2,
                "itemtypename": "Auction",
                "itemstatus": "Sold",
                "buynowprice": 55,
                "soldprice": 55,
            }],
        )
        self.assertTrue(item.bought_now)
        self.assertEqual(item.item_status, "Sold")
        self.assertEqual(item.bid_price, 55)
        self.assertEqual(item.buy_now_price, 55)

    def test_auction_that_sells_on_bids_keeps_the_hammer(self) -> None:
        item = _lot(
            item_status="Ready",
            item_type_id=2,
            item_type_name="Auction",
            bid_price=18,
            bid_count=3,
        )
        apply_buy_now(
            [item],
            [{
                "id": 10,
                "itemtypeid": 2,
                "itemtypename": "Auction",
                "itemstatus": "Sold",
                "buynowprice": 80,
                "soldprice": 18,
            }],
        )
        self.assertFalse(item.bought_now)
        self.assertEqual(item.bid_price, 18)
        self.assertEqual(item.buy_now_price, 80)
        self.assertEqual(item.item_status, "Sold")

    def test_purchase_announces_bought_now_once_watching(self) -> None:
        before = _lot()
        after = _lot(item_status="Sold", bid_price=25, buy_now_price=25, bought_now=True, item_type_id=1)
        first = diff_snapshots(None, Snapshot(sale_id=1, items={10: after}))
        self.assertEqual(first, [])
        changes = diff_snapshots(
            Snapshot(sale_id=1, items={10: before}),
            Snapshot(sale_id=1, items={10: after}),
        )
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].kind, "buy_now")
        self.assertIn("BOUGHT NOW", changes[0].message)
        self.assertIn("$25.00", changes[0].message)
        self.assertNotIn("@", changes[0].message)

    def test_later_refresh_keeps_a_purchase_that_had_no_auction_bid(self) -> None:
        watcher = SaleWatcher("48212")
        first = _lot()
        apply_buy_now(
            [first],
            [{"id": 10, "itemtypeid": 2, "itemtypename": "Auction", "itemstatus": "Sold", "buynowprice": 55, "soldprice": 55}],
        )
        watcher._remember_buy_now([first], {10})
        self.assertTrue(first.bought_now)

        # Next poll: bid feed is still empty, so the last hammer is restored first
        # and the seller row no longer looks like a zero-bid purchase.
        again = _lot(bid_price=55, reserve_price=90, item_type_id=2, item_type_name="Auction")
        apply_buy_now(
            [again],
            [{"id": 10, "itemtypeid": 2, "itemtypename": "Auction", "itemstatus": "Sold", "buynowprice": 55, "soldprice": 55}],
        )
        self.assertFalse(again.bought_now)
        watcher._remember_buy_now([again], {10})
        watcher._freeze_closed_lots([again])
        self.assertTrue(again.bought_now)
        self.assertEqual(again.item_status, "Sold")
        self.assertEqual(again.bid_price, 55)
        self.assertEqual(again.buy_now_price, 55)

    def test_missing_seller_login_keeps_the_last_price(self) -> None:
        notes: list[str] = []
        watcher = SaleWatcher("48212", on_status=notes.append)
        item = _lot()
        watcher._last_buy_now[10] = 30.0
        watcher._apply_seller_buy_now([item])
        watcher._apply_seller_buy_now([item])
        self.assertEqual(item.buy_now_price, 30)
        self.assertEqual(notes.count("Sign in to CT Seller to load Buy now prices."), 1)

    def test_seller_fields_have_no_bidder_identity(self) -> None:
        banned = {"email", "firstname", "lastname", "nickname", "phone", "buyerid", "highbidder"}
        self.assertTrue(banned.isdisjoint(name.lower() for name in SellerClient.BUY_NOW_FIELDS))
        self.assertIn("buynowprice", SellerClient.BUY_NOW_FIELDS)

    def test_csv_includes_buy_now_price(self) -> None:
        item = _lot(item_status="Sold", bid_price=25, buy_now_price=25, bought_now=True)
        snap = Snapshot(sale_id=48212, items={item.item_id: item})
        with tempfile.TemporaryDirectory() as folder:
            path = write_closing_csv(snap, Path(folder) / "live.csv")
            text = path.read_text(encoding="utf-8-sig")
        self.assertIn("Buy Now Price", text.splitlines()[0])
        self.assertIn("25.00", text)


if __name__ == "__main__":
    unittest.main()
