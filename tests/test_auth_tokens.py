"""Token files are written atomically, cleared only when they still hold the failed token,
and only one refresh / mint per kind runs at once."""

from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

from watcher_test_support import TempHomeTestCase, make_jwt

import auth  # noqa: E402  (path set by watcher_test_support)


class AtomicWriteTests(TempHomeTestCase):
    def test_write_goes_through_temp_file_and_os_replace(self) -> None:
        path = auth.seller_token_path()
        with mock.patch.object(auth.os, "replace", wraps=auth.os.replace) as replace:
            auth.save_seller_access_token("  abc.def.ghi  ")
        replace.assert_called_once()
        src, dst = replace.call_args.args
        self.assertEqual(dst, path)
        self.assertEqual(str(src).rsplit("/", 1)[0], str(path.parent))
        self.assertEqual(auth.read_secret(path), "abc.def.ghi")
        self.assertEqual([p.name for p in path.parent.iterdir() if p.name.endswith(".tmp")], [])

    def test_failed_replace_keeps_the_old_file_and_cleans_temp(self) -> None:
        auth.save_access_token("old.token.value")
        with mock.patch.object(auth.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                auth.save_access_token("new.token.value")
        self.assertEqual(auth.load_saved_access_token(), "old.token.value")
        self.assertEqual([p.name for p in self.data_dir.iterdir() if p.name.endswith(".tmp")], [])

    def test_save_none_removes_file_and_tolerates_missing(self) -> None:
        auth.save_access_token("a.b.c")
        auth.save_access_token(None)
        self.assertIsNone(auth.load_saved_access_token())
        auth.save_access_token(None)


class ClearIfMatchesTests(TempHomeTestCase):
    def test_clears_when_file_still_holds_failed_token(self) -> None:
        auth.save_seller_access_token("failed.jwt.x")
        self.assertTrue(auth.clear_seller_access_token_if("failed.jwt.x"))
        self.assertIsNone(auth.load_saved_seller_access_token())

    def test_keeps_newer_token(self) -> None:
        auth.save_seller_access_token("newer.jwt.y")
        self.assertFalse(auth.clear_seller_access_token_if("failed.jwt.x"))
        self.assertEqual(auth.load_saved_seller_access_token(), "newer.jwt.y")

    def test_none_or_missing_is_a_no_op(self) -> None:
        self.assertFalse(auth.clear_access_token_if(None))
        self.assertFalse(auth.clear_access_token_if("a.b.c"))


class EnsureKindLockTests(TempHomeTestCase):
    def test_one_refresh_per_kind_at_a_time(self) -> None:
        active = {"buyer": 0, "seller": 0}
        peak = {"buyer": 0, "seller": 0}
        guard = threading.Lock()

        def slow(kind, *, validate, allow_browser):
            with guard:
                active[kind] += 1
                peak[kind] = max(peak[kind], active[kind])
            time.sleep(0.04)
            with guard:
                active[kind] -= 1
            return None

        with mock.patch.object(auth, "_ensure_kind_locked", side_effect=slow):
            threads = [
                threading.Thread(target=auth._ensure_kind, args=(kind,))
                for kind in ("buyer", "seller") * 3
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(peak, {"buyer": 1, "seller": 1})

    def test_waiter_reuses_token_saved_by_first_refresh(self) -> None:
        fresh = make_jwt(30 * 60)
        calls = []

        def refresh(kind, token):
            calls.append(token)
            time.sleep(0.05)
            return fresh

        auth.save_seller_access_token(make_jwt(60))
        auth.save_seller_refresh_token("refresh.jwt.z")
        results: list[str | None] = []
        with mock.patch.object(auth, "_http_refresh", side_effect=refresh):
            threads = [
                threading.Thread(
                    target=lambda: results.append(
                        auth._ensure_kind("seller", validate=False, allow_browser=False)
                    )
                )
                for _ in range(3)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(len(calls), 1)
        self.assertEqual(results, [fresh, fresh, fresh])


class SellerMintHandOffTests(TempHomeTestCase):
    def test_rejected_seller_token_is_passed_to_mint_and_cleared_on_failure(self) -> None:
        import estate_ez

        revoked = make_jwt(20 * 60)
        auth.save_seller_access_token(revoked)
        with (
            mock.patch.object(auth, "_status_ok", return_value="unauthorized"),
            mock.patch.object(auth, "login_from_saved", return_value=None),
            mock.patch.object(estate_ez, "pull_seller_from_estate_ez", return_value=None) as pull,
        ):
            self.assertIsNone(auth._ensure_kind("seller", validate=True, allow_browser=False))
        self.assertIn(revoked, pull.call_args.kwargs["rejected"])
        self.assertIsNone(auth.load_saved_seller_access_token())

    def test_minted_token_is_used(self) -> None:
        import estate_ez

        minted = make_jwt(30 * 60)
        with (
            mock.patch.object(auth, "login_from_saved", return_value=None),
            mock.patch.object(estate_ez, "pull_seller_from_estate_ez", return_value=minted),
        ):
            self.assertEqual(auth._ensure_kind("seller", validate=False, allow_browser=False), minted)


if __name__ == "__main__":
    unittest.main()
