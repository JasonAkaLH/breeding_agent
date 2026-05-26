from __future__ import annotations

import unittest
from datetime import datetime

from src.auth.generation_cache import AuthGenerationCache, AuthGenerationSnapshot
from src.auth.invalidation_bus import validate_auth_generation_changed


class AuthGenerationCacheTest(unittest.TestCase):
    def test_apply_newer_generation_updates_cache_and_ignores_older(self) -> None:
        cache = AuthGenerationCache()
        cache.apply("alice", 41)
        cache.apply("alice", 42)
        cache.apply("alice", 41)
        self.assertEqual(cache.get("alice").auth_generation, 42)
        self.assertEqual(cache.is_current("alice", 42).current_generation, 42)
        self.assertTrue(cache.is_current("alice", 42).current)
        self.assertFalse(cache.is_current("alice", 41).current)

    def test_reconcile_snapshot_replaces_cache(self) -> None:
        cache = AuthGenerationCache()
        cache.apply("alice", 42)
        cache.reconcile([AuthGenerationSnapshot("bob", 7, datetime(2026, 5, 26))])
        self.assertIsNone(cache.get("alice"))
        self.assertEqual(cache.get("bob").auth_generation, 7)

    def test_cache_miss_is_not_current_and_repr_has_no_token_material(self) -> None:
        cache = AuthGenerationCache()
        check = cache.is_current("alice", 1)
        self.assertFalse(check.known)
        self.assertFalse(check.current)
        self.assertNotIn("maf_tok_SECRET", repr(cache))

    def test_payload_validation_rejects_bad_payloads(self) -> None:
        with self.assertRaises(ValueError):
            validate_auth_generation_changed({"username": "alice", "auth_generation": -1, "changed_at": "2026-05-26T00:00:00", "reason": "refresh"})
        with self.assertRaises(ValueError):
            validate_auth_generation_changed({"username": "alice", "auth_generation": 1, "changed_at": "2026-05-26T00:00:00", "reason": "bad"})


if __name__ == "__main__":
    unittest.main()
