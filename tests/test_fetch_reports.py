"""Item / sale-history fetchers report expected vs fetched, truncation, and pending lots."""

from __future__ import annotations

import unittest
from unittest import mock

from watcher_test_support import make_response

from ctbids_client import CtbidsClient, FetchReport  # noqa: E402
from seller_client import SellerClient, SellerSale  # noqa: E402


def _item_row(item_id: int, status: str = "Ready") -> dict:
    return {
        "id": item_id,
        "title": f"Lot {item_id}",
        "SKU": f"SKU{item_id}",
        "itemclosetime": "2026-11-05T18:00:00-08:00",
        "itemstatus": status,
        "reserveprice": 5,
    }


def _item_page(rows: list[dict], *, total: int | None, next_key: str | None) -> dict:
    page: dict = {}
    if total is not None:
        page["total"] = total
    if next_key:
        page["keyset"] = {"next": next_key}
    return {"status": "success", "data": rows, "page": page}


def _client_with_pages(pages: list[dict]) -> CtbidsClient:
    client = CtbidsClient()
    client.session.post = mock.Mock(side_effect=[make_response(200, p) for p in pages])
    return client


class SaleItemsReportTests(unittest.TestCase):
    def test_full_fetch_keeps_pending_lots_and_counts_them(self) -> None:
        client = _client_with_pages(
            [
                _item_page([_item_row(1), _item_row(2, "Pending")], total=3, next_key="k1"),
                _item_page([_item_row(3, "Sold")], total=3, next_key=None),
            ]
        )
        items, report = client.fetch_sale_items_report(48212, page_size=2)
        self.assertEqual([it.item_id for it in items], [1, 2, 3])
        self.assertEqual(report.expected, 3)
        self.assertEqual(report.fetched, 3)
        self.assertEqual(report.pending, 1)
        self.assertFalse(report.truncated)
        self.assertTrue(report.full)
        self.assertTrue(items[1].is_pending)

    def test_live_board_list_still_leaves_pending_out(self) -> None:
        client = _client_with_pages(
            [_item_page([_item_row(1), _item_row(2, "Pending")], total=2, next_key=None)]
        )
        self.assertEqual([it.item_id for it in client.fetch_sale_items(48212)], [1])

    def test_page_cap_marks_truncated(self) -> None:
        client = _client_with_pages(
            [_item_page([_item_row(1), _item_row(2)], total=6, next_key="k1")]
        )
        _items, report = client.fetch_sale_items_report(48212, page_size=2, max_pages=1)
        self.assertTrue(report.truncated)
        self.assertEqual((report.fetched, report.expected), (2, 6))
        self.assertFalse(report.full)

    def test_missing_next_key_before_total_marks_truncated(self) -> None:
        client = _client_with_pages(
            [_item_page([_item_row(1), _item_row(2)], total=5, next_key=None)]
        )
        _items, report = client.fetch_sale_items_report(48212, page_size=2)
        self.assertTrue(report.truncated)

    def test_unknown_total_is_not_full(self) -> None:
        client = _client_with_pages([_item_page([_item_row(1)], total=None, next_key=None)])
        _items, report = client.fetch_sale_items_report(48212)
        self.assertIsNone(report.expected)
        self.assertFalse(report.truncated)
        self.assertFalse(report.full)

    def test_zero_total_with_rows_counts_as_unknown(self) -> None:
        client = _client_with_pages([_item_page([_item_row(1)], total=0, next_key=None)])
        _items, report = client.fetch_sale_items_report(48212)
        self.assertIsNone(report.expected)
        self.assertFalse(report.full)

    def test_duplicate_rows_across_pages_do_not_count_twice(self) -> None:
        client = _client_with_pages(
            [
                _item_page([_item_row(1), _item_row(2)], total=3, next_key="k1"),
                _item_page([_item_row(2)], total=3, next_key=None),
            ]
        )
        items, report = client.fetch_sale_items_report(48212, page_size=2)
        self.assertEqual(len(items), 2)
        self.assertTrue(report.truncated)


def _history_page(rows: list[dict], *, total: int | None, next_key: str | None) -> dict:
    page: dict = {}
    if total is not None:
        page["total"] = total
    if next_key:
        page["keyset"] = {"next": next_key}
    return {"data": rows, "page": page}


def _seller_with_pages(pages: list[dict]) -> SellerClient:
    seller = SellerClient("seller.jwt.token")
    seller.session.post = mock.Mock(side_effect=[make_response(200, p) for p in pages])
    return seller


class SaleHistoryReportTests(unittest.TestCase):
    def test_full_history(self) -> None:
        seller = _seller_with_pages(
            [
                _history_page([{"itemid": 1, "viewcount": 3}], total=2, next_key="n1"),
                _history_page([{"itemid": 2, "itemstatus": "Pending"}], total=2, next_key=None),
            ]
        )
        rows, report = seller.fetch_sale_history_report(48212)
        self.assertEqual(len(rows), 2)
        self.assertTrue(report.full)
        self.assertEqual(report.pending, 1)

    def test_page_cap_marks_truncated(self) -> None:
        seller = _seller_with_pages([_history_page([{"itemid": 1}], total=3, next_key="n1")])
        _rows, report = seller.fetch_sale_history_report(48212, max_pages=1)
        self.assertTrue(report.truncated)
        self.assertEqual(report.describe(), "1 of 3 (cut off)")

    def test_repeated_next_key_stops_and_marks_short(self) -> None:
        seller = _seller_with_pages(
            [
                _history_page([{"itemid": 1}], total=3, next_key="n1"),
                _history_page([{"itemid": 2}], total=3, next_key="n1"),
            ]
        )
        rows, report = seller.fetch_sale_history_report(48212)
        self.assertEqual(len(rows), 2)
        self.assertTrue(report.truncated)

    def test_list_wrapper_returns_rows(self) -> None:
        seller = _seller_with_pages([_history_page([{"itemid": 1}], total=1, next_key=None)])
        self.assertEqual(seller.fetch_sale_history_items(48212), [{"itemid": 1}])


class ListedSaleTests(unittest.TestCase):
    def test_only_sales_from_list_sales_count(self) -> None:
        seller = SellerClient("seller.jwt.token")
        with mock.patch.object(
            seller, "list_sales", return_value=[SellerSale(sale_id=48212, title="Ours")]
        ) as list_sales:
            self.assertTrue(seller.sale_is_listed(48212))
            self.assertTrue(seller.sale_is_listed(48212))
            self.assertEqual(list_sales.call_count, 1)
            self.assertFalse(seller.sale_is_listed(99999))

    def test_new_token_drops_the_cache(self) -> None:
        seller = SellerClient("seller.jwt.token")
        with mock.patch.object(
            seller, "list_sales", return_value=[SellerSale(sale_id=1, title="A")]
        ) as list_sales:
            seller.sale_is_listed(1)
            seller.set_token("other.jwt.token")
            seller.sale_is_listed(1)
        self.assertEqual(list_sales.call_count, 2)


class FetchReportTests(unittest.TestCase):
    def test_describe(self) -> None:
        self.assertEqual(FetchReport(expected=None, fetched=4).describe(), "4 of ?")
        self.assertEqual(FetchReport(expected=5, fetched=5, pending=2).describe(), "5 of 5, 2 pending")


if __name__ == "__main__":
    unittest.main()
