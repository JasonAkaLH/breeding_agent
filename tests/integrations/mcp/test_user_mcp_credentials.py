from __future__ import annotations

import base64
import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from src.integrations.mcp.credentials import (
    CredentialCipher,
    CredentialSecurityError,
    load_credential_key,
)


class _SentinelStorage:
    def __init__(self) -> None:
        self.record = None

    async def get_mcp_credential_key_validation(self):
        return self.record

    async def create_or_get_mcp_credential_key_validation(self, record):
        if self.record is None:
            self.record = record
        return self.record


class UserMCPCredentialTests(unittest.IsolatedAsyncioTestCase):
    def test_round_trip_nonce_uniqueness_and_redacted_repr(self) -> None:
        cipher = CredentialCipher(b"a" * 32)
        first = cipher.encrypt(owner_user_id="alice", server_id="server-1", auth_type="bearer", values={"token": "canary"})
        second = cipher.encrypt(owner_user_id="alice", server_id="server-1", auth_type="bearer", values={"token": "canary"})

        self.assertNotEqual(first.nonce, second.nonce)
        self.assertEqual(
            cipher.decrypt(first, owner_user_id="alice", server_id="server-1", auth_type="bearer"),
            {"token": "canary"},
        )
        self.assertNotIn("canary", repr(first))
        self.assertNotIn(first.ciphertext.hex(), repr(first))

    def test_aad_owner_server_and_wrong_key_fail_closed(self) -> None:
        encrypted = CredentialCipher(b"a" * 32).encrypt(
            owner_user_id="alice", server_id="server-1", auth_type="api_key_header", values={"value": "canary"}
        )
        for cipher, owner, server in (
            (CredentialCipher(b"a" * 32), "bob", "server-1"),
            (CredentialCipher(b"a" * 32), "alice", "server-2"),
            (CredentialCipher(b"b" * 32), "alice", "server-1"),
        ):
            with self.assertRaisesRegex(CredentialSecurityError, "^mcp_credential_decryption_failed$") as raised:
                cipher.decrypt(encrypted, owner_user_id=owner, server_id=server, auth_type="api_key_header")
            self.assertNotIn("canary", str(raised.exception))

    def test_payload_structure_is_allowlisted_on_both_sides(self) -> None:
        cipher = CredentialCipher(b"a" * 32)
        with self.assertRaisesRegex(CredentialSecurityError, "payload_invalid"):
            cipher.encrypt(owner_user_id="alice", server_id="s", auth_type="bearer", values={"token": "x", "extra": "y"})
        encrypted = cipher.encrypt(owner_user_id="alice", server_id="s", auth_type="bearer", values={"token": "x"})
        with self.assertRaisesRegex(CredentialSecurityError, "decryption_failed"):
            cipher.decrypt(encrypted, owner_user_id="alice", server_id="s", auth_type="static_headers")

    def test_key_file_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "key")
            path.write_bytes(base64.b64encode(b"k" * 32) + b"\n")
            path.chmod(0o600)
            self.assertEqual(load_credential_key(path), b"k" * 32)

            path.chmod(0o644)
            with self.assertRaisesRegex(CredentialSecurityError, "invalid_permissions"):
                load_credential_key(path)
            path.chmod(0o600)
            with self.assertRaisesRegex(CredentialSecurityError, "invalid_permissions"):
                load_credential_key(path, require_read_only=True)
            path.chmod(0o400)
            self.assertEqual(
                load_credential_key(path, require_read_only=True), b"k" * 32
            )
            path.chmod(0o600)
            path.write_bytes(base64.b64encode(b"k" * 32) + b"\n\n")
            with self.assertRaisesRegex(CredentialSecurityError, "invalid_format"):
                load_credential_key(path)

            path.write_bytes(base64.b64encode(b"short"))
            with self.assertRaisesRegex(CredentialSecurityError, "invalid_length"):
                load_credential_key(path)

            link = Path(directory, "link")
            path.write_bytes(base64.b64encode(b"k" * 32))
            os.symlink(path, link)
            with self.assertRaisesRegex(CredentialSecurityError, "invalid_type"):
                load_credential_key(link)

    async def test_sentinel_create_verify_and_wrong_key(self) -> None:
        storage = _SentinelStorage()
        await CredentialCipher(b"a" * 32).create_or_verify_sentinel(storage)
        await CredentialCipher(b"a" * 32).create_or_verify_sentinel(storage)
        with self.assertRaisesRegex(CredentialSecurityError, "sentinel_mismatch"):
            await CredentialCipher(b"b" * 32).create_or_verify_sentinel(storage)

    async def test_concurrent_first_start_converges_on_one_sentinel(self) -> None:
        class RacingStorage(_SentinelStorage):
            def __init__(self) -> None:
                super().__init__()
                self.lock = asyncio.Lock()

            async def create_or_get_mcp_credential_key_validation(self, record):
                await asyncio.sleep(0)
                async with self.lock:
                    if self.record is None:
                        self.record = record
                    return self.record

        storage = RacingStorage()
        cipher = CredentialCipher(b"a" * 32)

        await asyncio.gather(
            *(cipher.create_or_verify_sentinel(storage) for _ in range(8))
        )

        self.assertIsNotNone(storage.record)
        await cipher.create_or_verify_sentinel(storage)


if __name__ == "__main__":
    unittest.main()
