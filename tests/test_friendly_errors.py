"""Friendly messages replace raw exceptions on screen."""

from __future__ import annotations

import unittest

import requests

import watcher_test_support  # noqa: F401  (puts sale_watcher on sys.path)
from friendly_errors import LOG_HINT, friendly_error, looks_friendly, service_name  # noqa: E402


def _http_error(status: int, url: str = "https://seller.ctbids.com/services/api/v1/x") -> requests.HTTPError:
    resp = requests.Response()
    resp.status_code = status
    resp.url = url
    return requests.HTTPError(f"{status} Server Error: for url: {url}", response=resp)


class FriendlyErrorTests(unittest.TestCase):
    def test_connection_errors_never_show_urllib3_text(self) -> None:
        raw = requests.ConnectionError(
            "HTTPSConnectionPool(host='api.ctbids.com', port=443): Max retries exceeded with url: /x "
            "(Caused by NewConnectionError('<urllib3.connection.HTTPSConnection object at 0x7f>'))"
        )
        msg = friendly_error(raw, service="CTBids")
        self.assertEqual(msg, "Could not reach CTBids. Check this PC's internet connection and try again.")

    def test_timeouts(self) -> None:
        self.assertIn("took too long", friendly_error(requests.ReadTimeout("x"), service="Estate-Ez"))
        self.assertIn("Could not reach", friendly_error(requests.ConnectTimeout("x"), service="Estate-Ez"))

    def test_ssl(self) -> None:
        self.assertIn("secure connection", friendly_error(requests.exceptions.SSLError("cert"), service="CTBids"))

    def test_http_status_uses_the_service_from_the_url(self) -> None:
        self.assertEqual(
            friendly_error(_http_error(503)),
            "CT Seller is having trouble right now (error 503). Try again in a few minutes.",
        )
        self.assertIn("Sign in again", friendly_error(_http_error(401, "https://api.ctbids.com/x")))
        self.assertIn("slow down", friendly_error(_http_error(429)))
        self.assertIn("404", friendly_error(_http_error(404)))

    def test_bad_json(self) -> None:
        try:
            requests.Response.json(_bad_json_response())
        except requests.exceptions.JSONDecodeError as exc:
            self.assertIn("unexpected response", friendly_error(exc, service="Estate-Ez"))
        else:  # pragma: no cover
            self.fail("expected JSONDecodeError")

    def test_app_messages_pass_through(self) -> None:
        self.assertEqual(friendly_error(RuntimeError("Sign in to Estate-Ez first.")), "Sign in to Estate-Ez first.")
        self.assertEqual(friendly_error(PermissionError("Buyer login expired.")), "Buyer login expired.")

    def test_raw_looking_app_messages_are_replaced(self) -> None:
        msg = friendly_error(RuntimeError("Max retries exceeded with url: /x"))
        self.assertTrue(msg.startswith("Something went wrong"))
        self.assertIn(LOG_HINT, msg)

    def test_unknown_exceptions_name_the_type_and_point_at_the_log(self) -> None:
        msg = friendly_error(KeyError("lot_id"))
        self.assertEqual(msg, f"Something went wrong (KeyError). {LOG_HINT}")

    def test_os_errors(self) -> None:
        msg = friendly_error(PermissionError(13, "Permission denied", "C:\\Reports\\sale.pdf"))
        self.assertEqual(msg, "Permission denied (C:\\Reports\\sale.pdf).")

    def test_strings_and_none(self) -> None:
        self.assertEqual(friendly_error("  Already friendly.  "), "Already friendly.")
        self.assertTrue(friendly_error("Traceback (most recent call last): ...").startswith("Something went wrong"))
        self.assertTrue(friendly_error(None).startswith("Something went wrong"))

    def test_service_name(self) -> None:
        self.assertEqual(service_name("https://seller.ctbids.com/a"), "CT Seller")
        self.assertEqual(service_name("https://api.ctbids.com/a"), "CTBids")
        self.assertEqual(service_name("https://api.estate-ez.com/a"), "Estate-Ez")
        self.assertEqual(service_name(""), "the server")

    def test_looks_friendly(self) -> None:
        self.assertTrue(looks_friendly("Could not find a sale ID."))
        self.assertFalse(looks_friendly("[Errno 111] Connection refused"))
        self.assertFalse(looks_friendly("x" * 400))


def _bad_json_response() -> requests.Response:
    resp = requests.Response()
    resp.status_code = 200
    resp._content = b"<html>gateway</html>"
    resp.encoding = "utf-8"
    return resp


if __name__ == "__main__":
    unittest.main()
