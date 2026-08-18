from __future__ import annotations

import asyncio
import pickle
import unittest

from src.integrations.mcp.credentials import (
    CredentialSecurityError,
    MCPAuditReferenceSigner,
    MCPCredentialCipher,
    MCPRecoveryCipher,
    MasterKeySentinelCipher,
)
from src.integrations.master_key import (
    MasterKeyDeriver,
    MasterKeyDomain,
    MasterKeyError,
)
from src.integrations.mcp.pending_action_payloads import (
    MCPPendingActionPayloadCipher,
)


class _SentinelStorage:
    def __init__(self) -> None:
        self.record = None

    async def get_maf_master_key_validation(self):
        return self.record

    async def create_or_get_maf_master_key_validation(self, record):
        if self.record is None:
            self.record = record
        return self.record


class UserMCPCredentialTests(unittest.IsolatedAsyncioTestCase):
    def test_domain_crypto_objects_reject_serialization(self) -> None:
        deriver = MasterKeyDeriver.from_bytes(b"m" * 32)
        domain_objects = (
            MCPCredentialCipher(
                deriver.derive(MasterKeyDomain.MCP_CREDENTIAL)
            ),
            MCPRecoveryCipher(
                deriver.derive(MasterKeyDomain.MCP_RECOVERY)
            ),
            MCPPendingActionPayloadCipher(
                deriver.derive(MasterKeyDomain.MCP_RECOVERY)
            ),
            MCPAuditReferenceSigner(
                deriver.derive(MasterKeyDomain.MCP_AUDIT_REFERENCE)
            ),
            MasterKeySentinelCipher(
                deriver.derive(MasterKeyDomain.KEY_VALIDATION)
            ),
        )

        for domain_object in domain_objects:
            with self.subTest(domain_object=type(domain_object).__name__):
                with self.assertRaises(TypeError):
                    pickle.dumps(domain_object)

    def test_domain_types_reject_miswired_derived_keys(self) -> None:
        constructors = (
            (MCPCredentialCipher, MasterKeyDomain.MCP_RECOVERY),
            (MCPRecoveryCipher, MasterKeyDomain.MCP_CREDENTIAL),
            (MCPPendingActionPayloadCipher, MasterKeyDomain.MCP_CREDENTIAL),
            (MCPAuditReferenceSigner, MasterKeyDomain.AUTH_TOKEN),
            (MasterKeySentinelCipher, MasterKeyDomain.MCP_AUDIT_REFERENCE),
        )
        deriver = MasterKeyDeriver.from_bytes(b"m" * 32)

        for constructor, wrong_domain in constructors:
            with self.subTest(constructor=constructor.__name__):
                with self.assertRaises(MasterKeyError) as raised:
                    constructor(deriver.derive(wrong_domain))
                self.assertEqual(raised.exception.code, "maf_key_domain_invalid")

        for constructor in (
            MCPCredentialCipher,
            MCPRecoveryCipher,
            MCPPendingActionPayloadCipher,
            MCPAuditReferenceSigner,
            MasterKeySentinelCipher,
        ):
            with self.subTest(constructor=constructor.__name__, raw_key=True):
                with self.assertRaises(MasterKeyError) as raised:
                    constructor(b"m" * 32)  # type: ignore[arg-type]
                self.assertEqual(raised.exception.code, "maf_key_domain_invalid")

    def test_domain_credential_round_trip_and_nonce_uniqueness(self) -> None:
        deriver = MasterKeyDeriver.from_bytes(b"m" * 32)
        cipher = MCPCredentialCipher(
            deriver.derive(MasterKeyDomain.MCP_CREDENTIAL)
        )

        first = cipher.encrypt(
            owner_user_id="alice",
            server_id="server-1",
            auth_type="bearer",
            values={"token": "canary"},
        )
        second = cipher.encrypt(
            owner_user_id="alice",
            server_id="server-1",
            auth_type="bearer",
            values={"token": "canary"},
        )

        self.assertNotEqual(first.nonce, second.nonce)
        self.assertEqual(
            cipher.decrypt(
                first,
                owner_user_id="alice",
                server_id="server-1",
                auth_type="bearer",
            ),
            {"token": "canary"},
        )
        self.assertNotIn("canary", repr(first))

    def test_credential_and_recovery_domains_cannot_decrypt_each_other(self) -> None:
        deriver = MasterKeyDeriver.from_bytes(b"m" * 32)
        credential = MCPCredentialCipher(
            deriver.derive(MasterKeyDomain.MCP_CREDENTIAL)
        )
        recovery = MCPRecoveryCipher(
            deriver.derive(MasterKeyDomain.MCP_RECOVERY)
        )
        encrypted_credential = credential.encrypt(
            owner_user_id="alice",
            server_id="server-1",
            auth_type="bearer",
            values={"token": "canary"},
        )
        recovery_arguments = {
            "owner_user_id": "alice",
            "task_id": "task-1",
            "node_id": "node-1",
            "call_ref": "call-1",
            "state_kind": "request_state",
            "server_id": "server-1",
            "protocol_version": "2026-07-28",
        }
        encrypted_recovery = recovery.seal_task_private_payload(
            **recovery_arguments,
            payload={"request_state": "opaque"},
        )

        with self.assertRaisesRegex(
            CredentialSecurityError,
            "^mcp_task_private_decryption_failed$",
        ):
            recovery.unseal_task_private_payload(
                encrypted_credential,
                **recovery_arguments,
            )
        with self.assertRaisesRegex(
            CredentialSecurityError,
            "^mcp_credential_decryption_failed$",
        ):
            credential.decrypt(
                encrypted_recovery,
                owner_user_id="alice",
                server_id="server-1",
                auth_type="bearer",
            )

    def test_audit_reference_is_stable_and_context_bound(self) -> None:
        deriver = MasterKeyDeriver.from_bytes(b"m" * 32)
        signer = MCPAuditReferenceSigner(
            deriver.derive(MasterKeyDomain.MCP_AUDIT_REFERENCE)
        )

        reference = signer.safe_reference("alice", context="config-v1")

        self.assertTrue(
            signer.verify_reference("alice", reference, context="config-v1")
        )
        self.assertFalse(
            signer.verify_reference("alice", reference, context="config-v2")
        )
        self.assertFalse(
            signer.verify_reference("bob", reference, context="config-v1")
        )
        self.assertNotIn("alice", reference)

    def test_master_key_sentinel_round_trip_and_wrong_master_key(self) -> None:
        first_deriver = MasterKeyDeriver.from_bytes(b"m" * 32)
        second_deriver = MasterKeyDeriver.from_bytes(b"n" * 32)
        first = MasterKeySentinelCipher(
            first_deriver.derive(MasterKeyDomain.KEY_VALIDATION)
        )
        second = MasterKeySentinelCipher(
            second_deriver.derive(MasterKeyDomain.KEY_VALIDATION)
        )

        sealed = first.seal()

        first.verify(sealed)
        self.assertEqual(sealed.derivation_version, 1)
        self.assertEqual(len(sealed.validation_nonce), 12)
        self.assertNotIn(sealed.validation_ciphertext.hex(), repr(sealed))
        with self.assertRaisesRegex(
            CredentialSecurityError,
            "^maf_master_key_mismatch$",
        ):
            second.verify(sealed)

    def test_rollout_owner_reference_is_keyed_stable_and_context_bound(self) -> None:
        first = MCPAuditReferenceSigner(
            MasterKeyDeriver.from_bytes(b"a" * 32).derive(
                MasterKeyDomain.MCP_AUDIT_REFERENCE
            )
        )
        second = MCPAuditReferenceSigner(
            MasterKeyDeriver.from_bytes(b"b" * 32).derive(
                MasterKeyDomain.MCP_AUDIT_REFERENCE
            )
        )

        reference = first.safe_owner_reference("alice", context="config-v1")

        self.assertEqual(
            reference,
            first.safe_owner_reference("alice", context="config-v1"),
        )
        self.assertNotEqual(
            reference,
            first.safe_owner_reference("alice", context="config-v2"),
        )
        self.assertNotEqual(
            reference,
            first.safe_owner_reference("bob", context="config-v1"),
        )
        self.assertNotEqual(
            reference,
            second.safe_owner_reference("alice", context="config-v1"),
        )
        self.assertNotIn("alice", reference)

    def test_round_trip_nonce_uniqueness_and_redacted_repr(self) -> None:
        cipher = self._credential_cipher(b"a" * 32)
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
        encrypted = self._credential_cipher(b"a" * 32).encrypt(
            owner_user_id="alice", server_id="server-1", auth_type="api_key_header", values={"value": "canary"}
        )
        for cipher, owner, server in (
            (self._credential_cipher(b"a" * 32), "bob", "server-1"),
            (self._credential_cipher(b"a" * 32), "alice", "server-2"),
            (self._credential_cipher(b"b" * 32), "alice", "server-1"),
        ):
            with self.assertRaisesRegex(CredentialSecurityError, "^mcp_credential_decryption_failed$") as raised:
                cipher.decrypt(encrypted, owner_user_id=owner, server_id=server, auth_type="api_key_header")
            self.assertNotIn("canary", str(raised.exception))

    def test_payload_structure_is_allowlisted_on_both_sides(self) -> None:
        cipher = self._credential_cipher(b"a" * 32)
        with self.assertRaisesRegex(CredentialSecurityError, "payload_invalid"):
            cipher.encrypt(owner_user_id="alice", server_id="s", auth_type="bearer", values={"token": "x", "extra": "y"})
        encrypted = cipher.encrypt(owner_user_id="alice", server_id="s", auth_type="bearer", values={"token": "x"})
        with self.assertRaisesRegex(CredentialSecurityError, "decryption_failed"):
            cipher.decrypt(encrypted, owner_user_id="alice", server_id="s", auth_type="static_headers")

    async def test_sentinel_create_verify_and_wrong_key(self) -> None:
        storage = _SentinelStorage()
        first = self._sentinel_cipher(b"a" * 32)
        await first.create_or_verify_sentinel(storage)
        await first.create_or_verify_sentinel(storage)
        with self.assertRaisesRegex(CredentialSecurityError, "maf_master_key_mismatch"):
            await self._sentinel_cipher(b"b" * 32).create_or_verify_sentinel(storage)

    async def test_concurrent_first_start_converges_on_one_sentinel(self) -> None:
        class RacingStorage(_SentinelStorage):
            def __init__(self) -> None:
                super().__init__()
                self.lock = asyncio.Lock()

            async def create_or_get_maf_master_key_validation(self, record):
                await asyncio.sleep(0)
                async with self.lock:
                    if self.record is None:
                        self.record = record
                    return self.record

        storage = RacingStorage()
        cipher = self._sentinel_cipher(b"a" * 32)

        await asyncio.gather(
            *(cipher.create_or_verify_sentinel(storage) for _ in range(8))
        )

        self.assertIsNotNone(storage.record)
        await cipher.create_or_verify_sentinel(storage)

    @staticmethod
    def _credential_cipher(root_key: bytes) -> MCPCredentialCipher:
        return MCPCredentialCipher(
            MasterKeyDeriver.from_bytes(root_key).derive(
                MasterKeyDomain.MCP_CREDENTIAL
            )
        )

    @staticmethod
    def _sentinel_cipher(root_key: bytes) -> MasterKeySentinelCipher:
        return MasterKeySentinelCipher(
            MasterKeyDeriver.from_bytes(root_key).derive(
                MasterKeyDomain.KEY_VALIDATION
            )
        )


if __name__ == "__main__":
    unittest.main()
