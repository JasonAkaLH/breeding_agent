from __future__ import annotations

import re

from src.core.models import (
    MCPLegacyMigrationRecord,
    UserMCPCredentialRecord,
    UserMCPServer,
)


def _validate_mcp_legacy_migration_record(
    record: MCPLegacyMigrationRecord,
) -> None:
    if record.event_type != "mcp.legacy.config_migrated":
        raise ValueError("legacy MCP migration event type is invalid")
    if record.disposition != "migrate_owner":
        raise ValueError("legacy MCP migration disposition is invalid")
    if not record.source_server_id.strip() or not record.target_server_id.strip():
        raise ValueError("legacy MCP migration server identity is invalid")
    sha_values = (
        record.migration_id,
        record.plan_fingerprint,
        record.source_fingerprint,
        record.target_consumer_set_digest,
        record.capability_obligations_fingerprint,
        record.catalog_fingerprint,
        record.capability_fingerprint,
        record.validator_provenance_fingerprint,
    )
    if any(re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None for value in sha_values):
        raise ValueError("legacy MCP migration fingerprint is invalid")
    hmac_values = (record.owner_consumer_ref, record.credential_digest)
    if any(
        re.fullmatch(r"hmac-sha256:[0-9a-f]{64}", value) is None
        for value in hmac_values
    ):
        raise ValueError("legacy MCP migration safe reference is invalid")
    if record.occurred_at >= record.evidence_expires_at:
        raise ValueError("legacy MCP migration evidence window is invalid")


def _mcp_legacy_migration_record_values(
    record: MCPLegacyMigrationRecord,
) -> dict[str, object]:
    return {
        "migration_id": record.migration_id,
        "event_type": record.event_type,
        "plan_fingerprint": record.plan_fingerprint,
        "source_server_id": record.source_server_id,
        "source_fingerprint": record.source_fingerprint,
        "owner_consumer_ref": record.owner_consumer_ref,
        "target_server_id": record.target_server_id,
        "target_consumer_set_digest": record.target_consumer_set_digest,
        "capability_obligations_fingerprint": (
            record.capability_obligations_fingerprint
        ),
        "catalog_fingerprint": record.catalog_fingerprint,
        "capability_fingerprint": record.capability_fingerprint,
        "validator_provenance_fingerprint": (
            record.validator_provenance_fingerprint
        ),
        "credential_digest": record.credential_digest,
        "disposition": record.disposition,
        "occurred_at": record.occurred_at,
        "evidence_expires_at": record.evidence_expires_at,
    }


def _user_mcp_server_insert_values(
    server: UserMCPServer,
    credential: UserMCPCredentialRecord | None,
) -> dict[str, object | None]:
    return {
        "server_id": server.server_id,
        "owner_user_id": server.owner_user_id,
        "display_name": server.display_name,
        "routing_description": server.routing_description,
        "endpoint_url": server.endpoint_url,
        "transport": str(server.transport),
        "protocol_preference": str(server.protocol_preference),
        "auth_type": str(server.auth_type),
        "auth_metadata": dict(server.auth_metadata),
        "enabled": server.enabled,
        "health_status": str(server.health_status),
        "config_version": max(1, int(server.config_version)),
        "security_version": max(1, int(server.security_version)),
        "credential_ciphertext": (
            None if credential is None else credential.credential_ciphertext
        ),
        "credential_nonce": None if credential is None else credential.credential_nonce,
        "encryption_version": (
            None if credential is None else credential.encryption_version
        ),
        "credential_updated_at": (
            None if credential is None else credential.credential_updated_at
        ),
        "last_tested_at": server.last_tested_at,
        "last_test_error_code": server.last_test_error_code,
        "deletion_pending": False,
        "deleted_at": None,
        "created_at": server.created_at,
        "updated_at": server.updated_at,
    }
