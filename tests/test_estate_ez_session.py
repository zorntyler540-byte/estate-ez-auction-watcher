"""Estate-Ez login / CT Seller mint: 401 vs 403, login block, Retry-After cooldown, mint lock."""

from __future__ import annotations

import threading
import time
import unittest
from email.utils import formatdate
from unittest import mock

from watcher_test_support import TempHomeTestCase, make_jwt, make_response

import estate_ez  # noqa: E402  (path set by watcher_test_support)


def _seller_session(access: str = "", refresh: str = "") -> dict:
    return {
        "access_token": access or make_jwt(30 * 60, role="seller"),
        "refresh_token": refresh or make_jwt(24 * 3600, role="seller-refresh"),
        "username": "franchise@example.com",
    }


class MintStatusTests(TempHomeTestCase):
    def test_401_clears_the_estate_ez_token(self) -> None:
        token = make_jwt(3600)
        estate_ez.save_estate_ez_token(token)
        with mock.patch.object(estate_ez.requests, "post", return_value=make_response(401, {"detail": "expired"})):
            with self.assertRaises(estate_ez.EstateEzAuthError):
                estate_ez.fetch_ctseller_session(token)
        self.assertIsNone(estate_ez.load_estate_ez_token())

    def test_401_keeps_a_newer_token_saved_by_another_thread(self) -> None:
        old = make_jwt(3600, n=1)
        newer = make_jwt(3600, n=2)
        estate_ez.save_estate_ez_token(newer)
        with mock.patch.object(estate_ez.requests, "post", return_value=make_response(401)):
            with self.assertRaises(estate_ez.EstateEzAuthError):
                estate_ez.fetch_ctseller_session(old)
        self.assertEqual(estate_ez.load_estate_ez_token(), newer)

    def test_403_keeps_token_and_stops_minting_for_the_run(self) -> None:
        token = make_jwt(3600)
        estate_ez.save_estate_ez_token(token)
        post = mock.Mock(return_value=make_response(403, {"detail": "Seller mint is not enabled"}))
        with mock.patch.object(estate_ez.requests, "post", post):
            with self.assertRaises(estate_ez.EstateEzMintUnavailable):
                estate_ez.fetch_ctseller_session(token)
            self.assertEqual(estate_ez.load_estate_ez_token(), token)
            self.assertTrue(estate_ez.minting_unavailable())

            # Quiet recovery no longer calls the mint endpoint.
            self.assertIsNone(estate_ez.pull_seller_from_estate_ez())
            with self.assertRaises(estate_ez.EstateEzMintUnavailable):
                estate_ez.fetch_ctseller_session(token)
        self.assertEqual(post.call_count, 1)

    def test_sign_in_returns_token_with_no_seller_on_403(self) -> None:
        token = make_jwt(3600)
        responses = [
            make_response(200, {"access_token": token}),
            make_response(403, {"detail": "Estate-Ez error (403)"}),
        ]
        with mock.patch.object(estate_ez.requests, "post", side_effect=responses):
            result = estate_ez.sign_in_estate_ez("staff@example.com", "pw", remember=False)
        self.assertEqual(result["estate_ez_token"], token)
        self.assertIsNone(result["seller"])
        self.assertEqual(result["message"], estate_ez.MINT_UNAVAILABLE_MESSAGE)
        self.assertEqual(estate_ez.load_estate_ez_token(), token)

    def test_sign_in_shows_server_detail_with_the_403_message(self) -> None:
        responses = [
            make_response(200, {"access_token": make_jwt(3600)}),
            make_response(403, {"detail": "Only owners can load CT Seller"}),
        ]
        with mock.patch.object(estate_ez.requests, "post", side_effect=responses):
            result = estate_ez.sign_in_estate_ez("staff@example.com", "pw", remember=False)
        self.assertIn(estate_ez.MINT_UNAVAILABLE_MESSAGE, result["message"])
        self.assertIn("Only owners can load CT Seller", result["message"])

    def test_same_account_sign_in_after_403_does_not_mint_again(self) -> None:
        post = mock.Mock(
            side_effect=[
                make_response(200, {"access_token": make_jwt(3600)}),
                make_response(403),
                make_response(200, {"access_token": make_jwt(3600)}),
            ]
        )
        with mock.patch.object(estate_ez.requests, "post", post):
            estate_ez.sign_in_estate_ez("staff@example.com", "pw", remember=False)
            result = estate_ez.sign_in_estate_ez("Staff@example.com", "pw", remember=False)
        self.assertIsNone(result["seller"])
        self.assertEqual(post.call_count, 3)

    def test_different_account_sign_in_after_403_tries_minting(self) -> None:
        seller = _seller_session()
        with mock.patch.object(
            estate_ez.requests,
            "post",
            side_effect=[
                make_response(200, {"access_token": make_jwt(3600)}),
                make_response(403),
                make_response(200, {"access_token": make_jwt(3600)}),
                make_response(200, seller),
            ],
        ):
            estate_ez.sign_in_estate_ez("staff@example.com", "pw", remember=False)
            result = estate_ez.sign_in_estate_ez("owner@example.com", "pw", remember=False)
        self.assertEqual(result["seller"]["access_token"], seller["access_token"])
        self.assertFalse(estate_ez.minting_unavailable())

    def test_success_saves_seller_tokens(self) -> None:
        import auth

        seller = _seller_session()
        with mock.patch.object(estate_ez.requests, "post", return_value=make_response(200, seller)):
            got = estate_ez.fetch_ctseller_session(make_jwt(3600))
        self.assertEqual(got["access_token"], seller["access_token"])
        self.assertEqual(auth.load_saved_seller_access_token(), seller["access_token"])
        self.assertEqual(auth.load_saved_seller_refresh_token(), seller["refresh_token"])


class LoginBlockTests(TempHomeTestCase):
    def test_failed_saved_login_blocks_for_15_minutes(self) -> None:
        post = mock.Mock(return_value=make_response(401))
        now = [1_000_000.0]
        with (
            mock.patch.object(estate_ez, "load_saved_username", return_value="staff@example.com"),
            mock.patch.object(estate_ez, "load_saved_password", return_value="wrong"),
            mock.patch.object(estate_ez.requests, "post", post),
            mock.patch.object(estate_ez.time, "time", side_effect=lambda: now[0]),
        ):
            self.assertIsNone(estate_ez.login_estate_ez_from_saved())
            self.assertIsNone(estate_ez.login_estate_ez_from_saved())
            self.assertEqual(post.call_count, 1)
            self.assertAlmostEqual(estate_ez.login_blocked_seconds_left(), 15 * 60)

            now[0] += 15 * 60 - 1
            self.assertIsNone(estate_ez.login_estate_ez_from_saved())
            self.assertEqual(post.call_count, 1)

            now[0] += 2
            self.assertIsNone(estate_ez.login_estate_ez_from_saved())
            self.assertEqual(post.call_count, 2)

    def test_interactive_login_clears_the_block(self) -> None:
        estate_ez._set_login_block(15 * 60)
        with mock.patch.object(
            estate_ez.requests, "post", return_value=make_response(200, {"access_token": make_jwt(3600)})
        ):
            estate_ez.login_estate_ez("staff@example.com", "pw")
        self.assertEqual(estate_ez.login_blocked_seconds_left(), 0)


class MintCooldownTests(TempHomeTestCase):
    def test_retry_after_seconds_and_http_date(self) -> None:
        self.assertEqual(estate_ez.retry_after_seconds(make_response(429, headers={"Retry-After": "120"})), 120)
        self.assertIsNone(estate_ez.retry_after_seconds(make_response(429)))
        self.assertIsNone(estate_ez.retry_after_seconds(make_response(429, headers={"Retry-After": "soon"})))
        now = 1_700_000_000.0
        date = formatdate(now + 900, usegmt=True)
        self.assertAlmostEqual(
            estate_ez.retry_after_seconds(make_response(503, headers={"Retry-After": date}), now=now),
            900,
            delta=1,
        )
        self.assertEqual(
            estate_ez.retry_after_seconds(make_response(429, headers={"Retry-After": "999999"})),
            estate_ez.MAX_RETRY_AFTER_SECONDS,
        )

    def test_429_retry_after_blocks_quiet_and_interactive_mint(self) -> None:
        token = make_jwt(3600)
        estate_ez.save_estate_ez_token(token)
        post = mock.Mock(return_value=make_response(429, headers={"Retry-After": "120"}))
        with mock.patch.object(estate_ez.requests, "post", post):
            with self.assertRaises(estate_ez.EstateEzMintCoolingDown) as caught:
                estate_ez.fetch_ctseller_session(token)
            self.assertAlmostEqual(caught.exception.seconds_left, 120, delta=1)
            self.assertIsNone(estate_ez.pull_seller_from_estate_ez())
            with self.assertRaises(estate_ez.EstateEzMintCoolingDown):
                estate_ez.fetch_ctseller_session(token, interactive=True)
        self.assertEqual(post.call_count, 1)
        self.assertAlmostEqual(estate_ez.mint_cooldown_left(), 120, delta=1)

    def test_sign_in_during_server_cooldown_returns_message(self) -> None:
        estate_ez._start_mint_cooldown(make_response(429, headers={"Retry-After": "600"}))
        with mock.patch.object(
            estate_ez.requests, "post", return_value=make_response(200, {"access_token": make_jwt(3600)})
        ) as post:
            result = estate_ez.sign_in_estate_ez("staff@example.com", "pw", remember=False)
        self.assertEqual(post.call_count, 1)  # login only
        self.assertIsNone(result["seller"])
        self.assertIn("wait about 10 min", result["message"])

    def test_default_cooldown_without_retry_after_only_gates_quiet_mint(self) -> None:
        token = make_jwt(3600)
        estate_ez.save_estate_ez_token(token)
        with mock.patch.object(estate_ez.requests, "post", return_value=make_response(500)) as post:
            with self.assertRaises(estate_ez.EstateEzAuthError):
                estate_ez.fetch_ctseller_session(token)
            self.assertAlmostEqual(estate_ez.mint_cooldown_left(), estate_ez.MINT_COOLDOWN_SECONDS, delta=1)
            self.assertIsNone(estate_ez.pull_seller_from_estate_ez())
            self.assertEqual(post.call_count, 1)
        seller = _seller_session()
        with mock.patch.object(estate_ez.requests, "post", return_value=make_response(200, seller)):
            got = estate_ez.fetch_ctseller_session(token, interactive=True)
        self.assertEqual(got["access_token"], seller["access_token"])
        self.assertEqual(estate_ez.mint_cooldown_left(), 0)


class PullSellerTests(TempHomeTestCase):
    def test_skips_mint_while_seller_token_has_more_than_8_minutes(self) -> None:
        import auth

        seller = make_jwt(20 * 60)
        auth.save_seller_access_token(seller)
        with mock.patch.object(estate_ez.requests, "post") as post:
            self.assertEqual(estate_ez.pull_seller_from_estate_ez(), seller)
        post.assert_not_called()

    def test_mints_when_seller_token_is_close_to_expiry(self) -> None:
        import auth

        auth.save_seller_access_token(make_jwt(5 * 60))
        estate_ez.save_estate_ez_token(make_jwt(3600))
        fresh = _seller_session()
        with mock.patch.object(estate_ez.requests, "post", return_value=make_response(200, fresh)) as post:
            self.assertEqual(estate_ez.pull_seller_from_estate_ez(), fresh["access_token"])
        self.assertEqual(post.call_count, 1)

    def test_mints_when_the_long_lived_token_was_rejected(self) -> None:
        import auth

        revoked = make_jwt(20 * 60)
        auth.save_seller_access_token(revoked)
        estate_ez.save_estate_ez_token(make_jwt(3600))
        fresh = _seller_session()
        with mock.patch.object(estate_ez.requests, "post", return_value=make_response(200, fresh)):
            got = estate_ez.pull_seller_from_estate_ez(rejected=frozenset({revoked}))
        self.assertEqual(got, fresh["access_token"])


class MintLockTests(TempHomeTestCase):
    def test_only_one_mint_runs_at_a_time(self) -> None:
        active = 0
        peak = 0
        guard = threading.Lock()

        def slow_post(*_args, **_kwargs):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with guard:
                active -= 1
            return make_response(200, _seller_session())

        token = make_jwt(3600)
        with mock.patch.object(estate_ez.requests, "post", side_effect=slow_post):
            threads = [
                threading.Thread(target=estate_ez.fetch_ctseller_session, args=(token,))
                for _ in range(4)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(peak, 1)


if __name__ == "__main__":
    unittest.main()
