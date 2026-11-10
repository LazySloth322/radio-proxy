"""Token store tests: hashing, validation, expiry, revocation, merge flush."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from private_radio_proxy import utils
from private_radio_proxy.tokens import TokenStore


class TestTokens(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "tokens.json"
        self.store = TokenStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_stores_hash_not_raw(self):
        raw, entry = self.store.add("friend-1")
        self.assertGreaterEqual(len(raw), 30)
        self.assertNotIn(raw, self.path.read_text())
        data = json.loads(self.path.read_text())
        self.assertEqual(data["tokens"][0]["token_hash"], utils.hash_token(raw))
        self.assertTrue(os.path.exists(str(self.path) + ".tmp") is False)
        # file must be 0600
        mode = os.stat(self.path).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_validate_ok_and_wrong(self):
        raw, _ = self.store.add("owner")
        entry = self.store.validate(raw)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.name, "owner")
        self.assertIsNone(self.store.validate("wrong-token"))
        self.assertIsNone(self.store.validate(None))
        self.assertIsNone(self.store.validate(""))

    def test_expiry(self):
        raw, _ = self.store.add("old", expires_at="2020-01-01T00:00:00Z")
        self.assertIsNone(self.store.validate(raw))
        raw2, _ = self.store.add("future", expires_at="2099-01-01T00:00:00Z")
        self.assertIsNotNone(self.store.validate(raw2))

    def test_revocation(self):
        raw, entry = self.store.add("friend-2")
        self.assertIsNotNone(self.store.validate(raw))
        self.assertTrue(self.store.set_enabled(entry.key_id, False))
        self.assertIsNone(self.store.validate(raw))
        self.assertTrue(self.store.set_enabled(entry.key_id, True))
        self.assertIsNotNone(self.store.validate(raw))
        self.assertFalse(self.store.set_enabled("txxx", False))

    def test_list_no_raw_tokens(self):
        self.store.add("a")
        self.store.add("b")
        listing = self.store.list()
        self.assertEqual(len(listing), 2)
        for entry in listing:
            self.assertNotIn("raw", entry.__dict__)
            self.assertTrue(hasattr(entry, "last_used_at"))

    def test_flush_merges_cli_revocation(self):
        raw, entry = self.store.add("friend-3")
        # simulate server usage -> dirty last_used
        self.assertIsNotNone(self.store.validate(raw))
        # owner revokes via a separate store instance (the CLI)
        cli_store = TokenStore(self.path)
        cli_store.set_enabled(entry.key_id, False)
        # force mtime change even within the same clock tick
        st = self.path.stat()
        os.utime(self.path, (st.st_atime + 5, st.st_mtime + 5))
        # server-side validation must now fail (file reloaded by mtime)
        self.assertIsNone(self.store.validate(raw))
        # flush must not resurrect the revoked token
        import asyncio
        asyncio.run(self.store.flush_if_dirty())
        data = json.loads(self.path.read_text())
        self.assertFalse(data["tokens"][0]["enabled"])
        self.assertIsNotNone(data["tokens"][0]["last_used_at"])

    def test_key_ids_unique(self):
        seen = set()
        for i in range(5):
            _, entry = self.store.add(f"u{i}")
            self.assertNotIn(entry.key_id, seen)
            seen.add(entry.key_id)


if __name__ == "__main__":
    unittest.main()
