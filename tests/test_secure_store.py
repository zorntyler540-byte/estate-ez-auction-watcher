"""DPAPI token files: encrypted writes, plaintext migration, atomic replace, and the non-Windows fallback.

The crypto layer is mocked with a reversible fake, so these run anywhere. Real
CryptProtectData / CryptUnprotectData only exist on Windows.
"""

from __future__ import annotations

import logging
import threading
import unittest
from unittest import mock

from watcher_test_support import TempHomeTestCase, make_jwt

import auth  # noqa: E402  (path set by watcher_test_support)
import estate_ez  # noqa: E402
import secure_store  # noqa: E402

TOKEN = make_jwt(3600, role="seller")


def _fake_protect(data: bytes, *, entropy: bytes | None = None) -> bytes:
    return b"ENC[" + (entropy or b"") + b"]" + bytes(reversed(data))


def _fake_unprotect(blob: bytes, *, entropy: bytes | None = None) -> bytes:
    head = b"ENC[" + (entropy or b"") + b"]"
    if not blob.startswith(head):
        raise secure_store.SecretStoreError("wrong user or entropy")
    return bytes(reversed(blob[len(head):]))


class FakeDpapiTestCase(TempHomeTestCase):
    def setUp(self) -> None:
        super().setUp()
        for name, value in (
            ("dpapi_available", mock.Mock(return_value=True)),
            ("protect", mock.Mock(side_effect=_fake_protect)),
            ("unprotect", mock.Mock(side_effect=_fake_unprotect)),
        ):
            patcher = mock.patch.object(secure_store, name, value)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)


class EncryptedTokenTests(FakeDpapiTestCase):
    def test_written_file_is_encrypted_with_header_and_entropy(self) -> None:
        auth.save_seller_access_token(TOKEN)
        raw = auth.seller_token_path().read_bytes()
        self.assertTrue(raw.startswith(secure_store.MAGIC))
        self.assertNotIn(TOKEN.encode(), raw)
        self.protect.assert_called_once_with(TOKEN.encode(), entropy=secure_store.TOKEN_ENTROPY)

    def test_round_trip(self) -> None:
        auth.save_seller_access_token(f"  {TOKEN}  ")
        self.assertEqual(auth.load_saved_seller_access_token(), TOKEN)

    def test_every_token_file_is_encrypted(self) -> None:
        auth.save_access_token("buyer.a.b")
        auth.save_refresh_token("buyer.r.t")
        auth.save_seller_access_token("seller.a.b")
        auth.save_seller_refresh_token("seller.r.t")
        estate_ez.save_estate_ez_token("ee.a.b")
        for path in (
            auth.token_path(), auth.refresh_token_path(), auth.seller_token_path(),
            auth.seller_refresh_token_path(), estate_ez.estate_ez_token_path(),
        ):
            with self.subTest(path=path.name):
                self.assertTrue(path.read_bytes().startswith(secure_store.MAGIC))

    def test_plaintext_file_is_migrated_on_first_read(self) -> None:
        path = auth.seller_token_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(TOKEN + "\n", encoding="utf-8")

        with mock.patch.object(auth.os, "replace", wraps=auth.os.replace) as replace:
            self.assertEqual(auth.load_saved_seller_access_token(), TOKEN)
        replace.assert_called_once()
        raw = path.read_bytes()
        self.assertTrue(raw.startswith(secure_store.MAGIC))
        self.assertNotIn(TOKEN.encode(), raw)

        # Second read decrypts and does not rewrite.
        with mock.patch.object(auth.os, "replace") as replace:
            self.assertEqual(auth.load_saved_seller_access_token(), TOKEN)
        replace.assert_not_called()

    def test_migration_failure_still_returns_the_token(self) -> None:
        path = auth.seller_token_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(TOKEN, encoding="utf-8")
        self.protect.side_effect = secure_store.SecretStoreError("DPAPI refused")
        self.assertEqual(auth.load_saved_seller_access_token(), TOKEN)
        self.assertEqual(path.read_text(encoding="utf-8"), TOKEN)

    def test_undecryptable_file_reads_as_signed_out(self) -> None:
        path = auth.seller_token_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(secure_store.MAGIC + b"written-by-another-windows-user")
        with self.assertLogs("sale_watcher.secure_store", logging.WARNING) as logs:
            self.assertIsNone(auth.load_saved_seller_access_token())
        self.assertNotIn("written-by-another", "\n".join(logs.output))

    def test_writes_stay_atomic(self) -> None:
        path = auth.seller_token_path()
        with mock.patch.object(auth.os, "replace", wraps=auth.os.replace) as replace:
            auth.save_seller_access_token(TOKEN)
        replace.assert_called_once()
        src, dst = replace.call_args.args
        self.assertEqual(str(dst), str(path))
        self.assertTrue(str(src).endswith(".tmp"))
        self.assertEqual([p.name for p in path.parent.iterdir() if p.name.endswith(".tmp")], [])

    def test_failed_encryption_leaves_the_old_file(self) -> None:
        auth.save_seller_access_token("old.token.value")
        self.protect.side_effect = secure_store.SecretStoreError("DPAPI refused")
        with self.assertRaises(secure_store.SecretStoreError):
            auth.save_seller_access_token("new.token.value")
        self.protect.side_effect = _fake_protect
        self.assertEqual(auth.load_saved_seller_access_token(), "old.token.value")

    def test_clear_if_matches_works_on_encrypted_and_plaintext_files(self) -> None:
        auth.save_seller_access_token("failed.jwt.x")
        self.assertTrue(auth.clear_secret_if_matches(auth.seller_token_path(), "failed.jwt.x"))
        self.assertFalse(auth.seller_token_path().exists())

        # Legacy plaintext: the read inside the lock migrates it, then the clear proceeds (RLock).
        path = auth.seller_token_path()
        path.write_text("failed.jwt.y", encoding="utf-8")
        done = threading.Event()

        def run() -> None:
            self.assertTrue(auth.clear_secret_if_matches(path, "failed.jwt.y"))
            done.set()

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(timeout=5)
        self.assertTrue(done.is_set(), "clear_secret_if_matches deadlocked")
        self.assertFalse(path.exists())

    def test_saved_password_uses_dpapi_without_token_entropy(self) -> None:
        auth.save_login_credentials("seller", "franchise@example.com", "hunter2")
        self.protect.assert_called_with(b"hunter2")
        self.assertEqual(auth.load_saved_password("seller"), "hunter2")


class NonWindowsFallbackTests(TempHomeTestCase):
    def setUp(self) -> None:
        super().setUp()
        for name in ("dpapi_available", "keychain_available"):
            patcher = mock.patch.object(secure_store, name, return_value=False)
            patcher.start()
            self.addCleanup(patcher.stop)
        warned = mock.patch.object(secure_store, "_warned_plaintext", False)
        warned.start()
        self.addCleanup(warned.stop)

    def test_tokens_are_plaintext_with_one_clear_warning(self) -> None:
        with self.assertLogs("sale_watcher.secure_store", logging.WARNING) as logs:
            auth.save_seller_access_token(TOKEN)
            auth.save_access_token("buyer.a.b")
        self.assertEqual(len(logs.output), 1)
        self.assertIn("UNENCRYPTED", logs.output[0])
        self.assertNotIn(TOKEN, logs.output[0])
        self.assertEqual(auth.seller_token_path().read_text(encoding="utf-8"), TOKEN)
        self.assertEqual(auth.load_saved_seller_access_token(), TOKEN)

    def test_plaintext_is_not_rewritten_when_dpapi_is_missing(self) -> None:
        path = auth.seller_token_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(TOKEN, encoding="utf-8")
        with mock.patch.object(auth.os, "replace") as replace:
            self.assertEqual(auth.load_saved_seller_access_token(), TOKEN)
        replace.assert_not_called()

    def test_saving_a_password_refuses_instead_of_writing_plaintext(self) -> None:
        with self.assertRaises(secure_store.SecretStoreError):
            auth.save_login_credentials("seller", "franchise@example.com", "hunter2")
        self.assertFalse(auth._password_path("seller").exists())
        self.assertEqual(auth.load_saved_password("seller"), "")


class DpapiAvailabilityTests(unittest.TestCase):
    def test_not_available_off_windows(self) -> None:
        with mock.patch.object(secure_store.sys, "platform", "linux"):
            self.assertFalse(secure_store.dpapi_available())
            self.assertFalse(secure_store.keychain_available())
            with self.assertRaises(secure_store.SecretStoreError):
                secure_store.protect(b"x")
            with self.assertRaises(secure_store.SecretStoreError):
                secure_store.unprotect(b"x")


class MacKeychainTests(TempHomeTestCase):
    """Keychain sealing without calling the real ``security`` tool."""

    def setUp(self) -> None:
        super().setUp()
        self.key = b"k" * 32
        for name, value in (
            ("dpapi_available", mock.Mock(return_value=False)),
            ("keychain_available", mock.Mock(return_value=True)),
            ("_keychain_key", mock.Mock(return_value=self.key)),
        ):
            patcher = mock.patch.object(secure_store, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_password_round_trip_does_not_store_plaintext(self) -> None:
        auth.save_login_credentials("seller", "franchise@example.com", "hunter2")
        raw = auth._password_path("seller").read_bytes()
        self.assertNotIn(b"hunter2", raw)
        self.assertTrue(raw.startswith(b"SWKC1\n"))
        self.assertEqual(auth.load_saved_password("seller"), "hunter2")

    def test_token_file_uses_the_same_header_as_windows(self) -> None:
        auth.save_seller_access_token(TOKEN)
        raw = auth.seller_token_path().read_bytes()
        self.assertTrue(raw.startswith(secure_store.MAGIC))
        self.assertNotIn(TOKEN.encode(), raw)
        self.assertEqual(auth.load_saved_seller_access_token(), TOKEN)

    def test_wrong_key_reads_as_signed_out(self) -> None:
        auth.save_seller_access_token(TOKEN)
        secure_store._keychain_key.return_value = b"z" * 32
        with self.assertLogs("sale_watcher.secure_store", logging.WARNING):
            self.assertIsNone(auth.load_saved_seller_access_token())


if __name__ == "__main__":
    unittest.main()
