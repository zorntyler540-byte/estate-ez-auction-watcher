"""Shared HTTP retry helper: what is retried, how long it waits, and the call sites that use it."""

from __future__ import annotations

import random
import unittest
from email.utils import formatdate
from unittest import mock

import requests
from urllib3.exceptions import NewConnectionError

from watcher_test_support import TempHomeTestCase, make_jwt, make_response

import http_retry  # noqa: E402  (path set by watcher_test_support)
from http_retry import (  # noqa: E402
    CONNECT_ONLY,
    DEFAULT,
    NO_RETRY,
    WRITE,
    RetryPolicy,
    backoff_delay,
    parse_retry_after,
    request_never_sent,
    request_with_retry,
)


def _sequence(*outcomes):
    """A send() that returns / raises each outcome in turn."""
    items = list(outcomes)
    send = mock.Mock()

    def _next():
        item = items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    send.side_effect = _next
    return send


def _never_connected() -> requests.ConnectionError:
    reason = NewConnectionError(None, "Failed to establish a new connection: [Errno 111] refused")
    return requests.ConnectionError(reason)


class BackoffTests(unittest.TestCase):
    def test_delay_grows_exponentially_with_jitter_inside_bounds(self) -> None:
        policy = RetryPolicy(base_delay=0.5, max_delay=8.0)
        rng = random.Random(7)
        for retry_index, step in enumerate((0.5, 1.0, 2.0, 4.0, 8.0, 8.0)):
            for _ in range(50):
                delay = backoff_delay(retry_index, policy, rng)
                self.assertGreaterEqual(delay, step / 2)
                self.assertLessEqual(delay, step)

    def test_jitter_actually_varies(self) -> None:
        rng = random.Random(1)
        delays = {round(backoff_delay(2, DEFAULT, rng), 6) for _ in range(20)}
        self.assertGreater(len(delays), 5)


class RetryAfterTests(unittest.TestCase):
    def test_delta_seconds(self) -> None:
        self.assertEqual(parse_retry_after(make_response(429, headers={"Retry-After": "7"})), 7.0)

    def test_http_date(self) -> None:
        now = 1_700_000_000.0
        resp = make_response(503, headers={"Retry-After": formatdate(now + 90, usegmt=True)})
        self.assertAlmostEqual(parse_retry_after(resp, now=now), 90.0, delta=1.0)

    def test_cap_and_garbage(self) -> None:
        self.assertEqual(parse_retry_after(make_response(429, headers={"Retry-After": "600"}), cap=60), 60.0)
        self.assertIsNone(parse_retry_after(make_response(429, headers={"Retry-After": "soon"})))
        self.assertIsNone(parse_retry_after(make_response(429)))


class RequestWithRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sleeps: list[float] = []
        self.rng = random.Random(3)

    def _run(self, send, policy=DEFAULT):
        return request_with_retry(send, policy=policy, label="test", sleep=self.sleeps.append, rng=self.rng)

    def test_retries_5xx_then_returns_success(self) -> None:
        send = _sequence(make_response(502), make_response(503), make_response(200, {"ok": True}))
        resp = self._run(send)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(send.call_count, 3)
        self.assertEqual(len(self.sleeps), 2)
        self.assertLess(self.sleeps[0], self.sleeps[1] + 1.0)

    def test_retries_429_and_honors_retry_after(self) -> None:
        send = _sequence(make_response(429, headers={"Retry-After": "4"}), make_response(200))
        self._run(send)
        self.assertEqual(send.call_count, 2)
        self.assertGreaterEqual(self.sleeps[0], 4.0)

    def test_long_retry_after_goes_back_to_the_caller(self) -> None:
        send = _sequence(make_response(429, headers={"Retry-After": "120"}))
        resp = self._run(send)
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.sleeps, [])

    def test_never_retries_auth_failures_or_other_4xx(self) -> None:
        for status in (400, 401, 403, 404, 409, 422):
            with self.subTest(status=status):
                send = _sequence(make_response(status))
                self.assertEqual(self._run(send).status_code, status)
                self.assertEqual(send.call_count, 1)
        self.assertEqual(self.sleeps, [])

    def test_retries_timeouts_and_connection_errors(self) -> None:
        send = _sequence(requests.ReadTimeout("slow"), requests.ConnectionError("reset"), make_response(200))
        self.assertEqual(self._run(send).status_code, 200)
        self.assertEqual(send.call_count, 3)

    def test_reraises_last_network_error_when_attempts_run_out(self) -> None:
        send = _sequence(requests.ConnectTimeout("a"), requests.ConnectTimeout("b"), requests.ConnectTimeout("c"))
        with self.assertRaises(requests.ConnectTimeout) as caught:
            self._run(send)
        self.assertEqual(str(caught.exception), "c")
        self.assertEqual(send.call_count, DEFAULT.attempts)

    def test_returns_last_5xx_when_attempts_run_out(self) -> None:
        send = _sequence(make_response(500), make_response(500), make_response(500))
        self.assertEqual(self._run(send).status_code, 500)
        self.assertEqual(send.call_count, 3)
        self.assertEqual(len(self.sleeps), 2)

    def test_ssl_errors_are_not_retried(self) -> None:
        send = _sequence(requests.exceptions.SSLError("bad cert"))
        with self.assertRaises(requests.exceptions.SSLError):
            self._run(send)
        self.assertEqual(send.call_count, 1)

    def test_non_request_exceptions_propagate_untouched(self) -> None:
        send = _sequence(ValueError("bug"))
        with self.assertRaises(ValueError):
            self._run(send)
        self.assertEqual(send.call_count, 1)

    def test_no_retry_policy_makes_one_attempt(self) -> None:
        send = _sequence(make_response(503))
        self.assertEqual(self._run(send, NO_RETRY).status_code, 503)
        self.assertEqual(send.call_count, 1)

    def test_write_policy_does_not_retry_read_timeout_or_5xx(self) -> None:
        send = _sequence(requests.ReadTimeout("server may have acted"))
        with self.assertRaises(requests.ReadTimeout):
            self._run(send, WRITE)
        self.assertEqual(send.call_count, 1)

        send = _sequence(make_response(500))
        self.assertEqual(self._run(send, WRITE).status_code, 500)
        self.assertEqual(send.call_count, 1)

    def test_write_policy_retries_when_the_request_never_left(self) -> None:
        send = _sequence(_never_connected(), make_response(200))
        self.assertEqual(self._run(send, WRITE).status_code, 200)
        self.assertEqual(send.call_count, 2)

        send = _sequence(make_response(429, headers={"Retry-After": "1"}), make_response(200))
        self.assertEqual(self._run(send, WRITE).status_code, 200)

    def test_connect_only_policy_leaves_429_and_5xx_to_the_caller(self) -> None:
        for status in (429, 500, 503):
            send = _sequence(make_response(status))
            self.assertEqual(self._run(send, CONNECT_ONLY).status_code, status)
            self.assertEqual(send.call_count, 1)

    def test_request_never_sent_classification(self) -> None:
        self.assertTrue(request_never_sent(requests.ConnectTimeout("x")))
        self.assertTrue(request_never_sent(_never_connected()))
        self.assertFalse(request_never_sent(requests.ConnectionError("Connection reset by peer")))
        self.assertFalse(request_never_sent(requests.ReadTimeout("x")))

    def test_retry_logs_do_not_include_the_url_query(self) -> None:
        self.assertEqual(
            http_retry.describe_url("get", "https://seller.ctbids.com/api/x?token=abc&id=5"),
            "GET seller.ctbids.com/api/x",
        )


class CallSiteTests(TempHomeTestCase):
    """The helper is wired into Estate-Ez and CT Seller without changing PR #7 auth semantics."""

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch("time.sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def test_estate_ez_login_retries_502(self) -> None:
        import estate_ez

        token = make_jwt(3600)
        post = mock.Mock(side_effect=[make_response(502), make_response(200, {"access_token": token})])
        with mock.patch.object(estate_ez.requests, "post", post):
            self.assertEqual(estate_ez.login_estate_ez("a@b.c", "pw"), token)
        self.assertEqual(post.call_count, 2)

    def test_estate_ez_login_never_retries_401(self) -> None:
        import estate_ez

        post = mock.Mock(return_value=make_response(401, {"detail": "bad password"}))
        with mock.patch.object(estate_ez.requests, "post", post):
            with self.assertRaises(estate_ez.EstateEzAuthError):
                estate_ez.login_estate_ez("a@b.c", "pw")
        self.assertEqual(post.call_count, 1)

    def test_estate_ez_login_network_failure_is_friendly(self) -> None:
        import estate_ez

        post = mock.Mock(side_effect=requests.ConnectionError("HTTPSConnectionPool(host=...) Max retries exceeded"))
        with mock.patch.object(estate_ez.requests, "post", post):
            with self.assertRaises(estate_ez.EstateEzAuthError) as caught:
                estate_ez.login_estate_ez("a@b.c", "pw")
        self.assertEqual(str(caught.exception), estate_ez.UNREACHABLE_MESSAGE)
        self.assertEqual(post.call_count, DEFAULT.attempts)

    def test_mint_500_still_starts_cooldown_after_one_call(self) -> None:
        import estate_ez

        token = make_jwt(3600)
        post = mock.Mock(return_value=make_response(500))
        with mock.patch.object(estate_ez.requests, "post", post):
            with self.assertRaises(Exception):
                estate_ez.fetch_ctseller_session(token)
        self.assertEqual(post.call_count, 1)
        self.assertGreater(estate_ez._mint_cooldown_until, 0)

    def test_mint_retries_a_connection_that_never_opened(self) -> None:
        import estate_ez

        token = make_jwt(3600)
        session = {
            "access_token": make_jwt(1800, role="seller"),
            "refresh_token": make_jwt(86400, role="seller-refresh"),
            "username": "franchise@example.com",
        }
        post = mock.Mock(side_effect=[_never_connected(), make_response(200, session)])
        with mock.patch.object(estate_ez.requests, "post", post):
            result = estate_ez.fetch_ctseller_session(token)
        self.assertEqual(result["access_token"], session["access_token"])
        self.assertEqual(post.call_count, 2)

    def test_seller_reads_retry_503(self) -> None:
        from seller_client import SellerClient

        client = SellerClient(make_jwt(1800))
        client.session.get = mock.Mock(side_effect=[make_response(503), make_response(200, {"ok": 1})])
        resp = client._call("get", "https://seller.ctbids.com/services/api/v1/x")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(client.session.get.call_count, 2)

    def test_seller_writes_do_not_retry_5xx(self) -> None:
        from seller_client import SellerClient

        client = SellerClient(make_jwt(1800))
        client.session.post = mock.Mock(return_value=make_response(502))
        resp = client._call("post", "https://seller.ctbids.com/services/api/v1/x", policy=WRITE)
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(client.session.post.call_count, 1)

    def test_seller_401_is_not_retried(self) -> None:
        from seller_client import SellerClient

        client = SellerClient(make_jwt(1800))
        client.session.get = mock.Mock(return_value=make_response(401))
        self.assertEqual(client._call("get", "https://seller.ctbids.com/x").status_code, 401)
        self.assertEqual(client.session.get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
