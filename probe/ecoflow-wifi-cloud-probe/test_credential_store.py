"""Offline ABI/ownership tests. Never reads or writes the real macOS Keychain."""
import ctypes
import unittest
from unittest.mock import patch

import credential_store as store


class FakeSecurity:
    def __init__(self, entries=None):
        self.entries = dict(entries or {})
        self.buffers = []
        self.freed = []
        self.released = []
        self.find_calls = []
        self.fail_find = 0
        self.fail_add = 0
        self.fail_modify = 0
        self.duplicate_race = False

    def key(self, service_len, service, account_len, account):
        assert service[:service_len] == b"ecoflow-wifi-cloud-probe"
        return account[:account_len]

    def SecKeychainFindGenericPassword(self, keychain, sl, service, al, account, length, content, item):
        key = self.key(sl, service, al, account)
        self.find_calls.append((length is not None, content is not None, item is not None))
        if self.fail_find:
            return self.fail_find
        if key not in self.entries:
            return -25300
        if item is not None:
            ctypes.cast(item, ctypes.POINTER(ctypes.c_void_p))[0] = 321
        if content is not None:
            value = self.entries[key]
            buffer = ctypes.create_string_buffer(value)
            self.buffers.append(buffer)
            ctypes.cast(content, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(buffer)
            ctypes.cast(length, ctypes.POINTER(ctypes.c_uint32))[0] = len(value)
        return 0

    def SecKeychainAddGenericPassword(self, keychain, sl, service, al, account, length, content, item):
        key = self.key(sl, service, al, account)
        assert item is None
        if self.fail_add:
            return self.fail_add
        if self.duplicate_race:
            self.entries[key] = b"raced entry"
            return -25299
        self.entries[key] = ctypes.string_at(content, length)
        return 0

    def SecKeychainItemModifyAttributesAndData(self, item, attrs, length, content):
        assert item.value == 321 and attrs is None
        if self.fail_modify:
            return self.fail_modify
        self.entries[next(iter(self.entries))] = ctypes.string_at(content, length)
        return 0

    def SecKeychainItemFreeContent(self, attrs, content):
        assert attrs is None
        self.freed.append(content.value)
        return 0

    def CFRelease(self, item):
        self.released.append(item.value)


class CredentialStoreTests(unittest.TestCase):
    account = "13800000000"

    def fake(self, api):
        return patch.object(store, "_load_api", return_value=(api, api))

    def test_missing_returns_none(self):
        api = FakeSecurity()
        with self.fake(api):
            self.assertIsNone(store.get_password(self.account))
        self.assertEqual(api.find_calls, [(True, True, False)])
        self.assertFalse(api.freed)

    def test_add_read_update_preserves_spaces_unicode_and_embedded_nul(self):
        api = FakeSecurity()
        first, second = "  虛構密碼\x00尾  ", "  new fake secret  "
        with self.fake(api):
            store.set_password(self.account, first)
            self.assertEqual(store.get_password(self.account), first)
            store.set_password(self.account, second)
            self.assertEqual(store.get_password(self.account), second)
        self.assertEqual(len(api.freed), 2)
        self.assertEqual(api.released, [321])
        self.assertEqual(api.find_calls, [(False, False, True), (True, True, False),
                                          (False, False, True), (True, True, False)])

    def test_denied_is_not_treated_as_missing_or_leaked(self):
        api = FakeSecurity()
        api.fail_find = -25308
        with self.fake(api), self.assertRaises(store.CredentialStoreError) as caught:
            store.get_password(self.account)
        self.assertEqual(caught.exception.status, -25308)
        self.assertNotIn(self.account, str(caught.exception))

    def test_invalid_utf8_still_frees_security_content(self):
        api = FakeSecurity({self.account.encode(): b"\xffinvalid"})
        with self.fake(api), self.assertRaises(store.CredentialStoreError):
            store.get_password(self.account)
        self.assertEqual(len(api.freed), 1)

    def test_modify_failure_releases_item_and_never_echoes_secret(self):
        api = FakeSecurity({self.account.encode(): b"old"})
        api.fail_modify = -25293
        with self.fake(api), self.assertRaises(store.CredentialStoreError) as caught:
            store.set_password(self.account, "do not expose this fake password")
        self.assertEqual(api.released, [321])
        self.assertEqual(api.entries[self.account.encode()], b"old")
        self.assertNotIn("fake password", str(caught.exception))
        self.assertNotIn(self.account, str(caught.exception))

    def test_add_failure_reports_failure(self):
        api = FakeSecurity()
        api.fail_add = -128
        with self.fake(api), self.assertRaises(store.CredentialStoreError) as caught:
            store.set_password(self.account, "fake")
        self.assertEqual(caught.exception.operation, "保存密碼")
        self.assertFalse(api.entries)

    def test_duplicate_race_updates_without_reading_existing_secret(self):
        api = FakeSecurity()
        api.duplicate_race = True
        with self.fake(api):
            store.set_password(self.account, "fake replacement")
        self.assertEqual(api.entries[self.account.encode()], b"fake replacement")
        self.assertEqual(api.find_calls, [(False, False, True), (False, False, True)])
        self.assertEqual(api.released, [321])

    def test_invalid_input_never_opens_keychain(self):
        with patch.object(store, "_load_api") as load:
            for account in ("", "invalid\x00account", None):
                with self.assertRaises(store.CredentialStoreError):
                    store.get_password(account)
            for password in ("", None):
                with self.assertRaises(store.CredentialStoreError):
                    store.set_password(self.account, password)
            load.assert_not_called()

    def test_non_macos_fails_without_loading_framework(self):
        store._load_api.cache_clear()
        try:
            with patch.object(store.sys, "platform", "linux"), patch.object(store.ctypes, "CDLL") as library:
                with self.assertRaises(store.CredentialStoreError):
                    store.get_password(self.account)
                library.assert_not_called()
        finally:
            store._load_api.cache_clear()


if __name__ == "__main__":
    unittest.main()
