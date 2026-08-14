from __future__ import annotations

from src.auth import AuthTokenHasher
from src.integrations.master_key import MasterKeyDeriver, MasterKeyDomain
from src.integrations.mcp.credentials import (
    MCPAuditReferenceSigner,
    MCPCredentialCipher,
    MCPRecoveryCipher,
    MasterKeySentinelCipher,
)


def credential_cipher(root_key: bytes) -> MCPCredentialCipher:
    return MCPCredentialCipher(
        MasterKeyDeriver.from_bytes(root_key).derive(MasterKeyDomain.MCP_CREDENTIAL)
    )


def recovery_cipher(root_key: bytes) -> MCPRecoveryCipher:
    return MCPRecoveryCipher(
        MasterKeyDeriver.from_bytes(root_key).derive(MasterKeyDomain.MCP_RECOVERY)
    )


def audit_reference_signer(root_key: bytes) -> MCPAuditReferenceSigner:
    return MCPAuditReferenceSigner(
        MasterKeyDeriver.from_bytes(root_key).derive(
            MasterKeyDomain.MCP_AUDIT_REFERENCE
        )
    )


def sentinel_cipher(root_key: bytes) -> MasterKeySentinelCipher:
    return MasterKeySentinelCipher(
        MasterKeyDeriver.from_bytes(root_key).derive(MasterKeyDomain.KEY_VALIDATION)
    )


def auth_token_hasher(root_key: bytes) -> AuthTokenHasher:
    return AuthTokenHasher(
        MasterKeyDeriver.from_bytes(root_key).derive(MasterKeyDomain.AUTH_TOKEN)
    )
