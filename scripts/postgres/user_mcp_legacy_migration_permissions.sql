-- Dedicated PostgreSQL authority for the one-time legacy MCP migration.
-- Login roles and credentials are provisioned by the deployment platform.

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'maf_mcp_migration_api_owner'
    ) THEN
        CREATE ROLE maf_mcp_migration_api_owner NOLOGIN;
    END IF;
END; $$;
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'maf_mcp_legacy_migrator'
    ) THEN
        CREATE ROLE maf_mcp_legacy_migrator NOLOGIN;
    END IF;
END; $$;

ALTER ROLE maf_mcp_migration_api_owner
    NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
ALTER ROLE maf_mcp_legacy_migrator
    NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;

DO $$ BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member = (
            SELECT oid FROM pg_catalog.pg_roles
            WHERE rolname = 'maf_mcp_migration_api_owner'
        )
           OR membership.roleid = (
            SELECT oid FROM pg_catalog.pg_roles
            WHERE rolname = 'maf_mcp_migration_api_owner'
        )
    ) THEN
        RAISE EXCEPTION 'legacy migration API owner must not have memberships';
    END IF;
END; $$;

CREATE SCHEMA IF NOT EXISTS mcp_migration_api;
ALTER SCHEMA mcp_migration_api OWNER TO CURRENT_USER;
REVOKE ALL ON SCHEMA mcp_migration_api FROM PUBLIC;
REVOKE ALL ON SCHEMA mcp_migration_api FROM maf_mcp_legacy_migrator;
REVOKE ALL ON SCHEMA mcp_migration_api FROM maf_mcp_migration_api_owner;
GRANT USAGE ON SCHEMA mcp_migration_api TO maf_mcp_migration_api_owner;
GRANT USAGE ON SCHEMA mcp_migration_api TO maf_mcp_legacy_migrator;

REVOKE SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE
    public.user_mcp_server,
    public.mcp_legacy_migration_record
FROM PUBLIC, maf_mcp_legacy_migrator, maf_mcp_migration_api_owner;
GRANT SELECT, INSERT ON TABLE
    public.user_mcp_server,
    public.mcp_legacy_migration_record
TO maf_mcp_migration_api_owner;

CREATE OR REPLACE FUNCTION mcp_migration_api.lock_legacy_migration_batch(
    p_lock_identities pg_catalog.text[]
) RETURNS pg_catalog.void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    lock_identity pg_catalog.text;
BEGIN
    IF p_lock_identities IS NULL
       OR pg_catalog.cardinality(p_lock_identities) = 0
       OR EXISTS (
           SELECT 1
           FROM pg_catalog.unnest(p_lock_identities) AS value(identity)
           WHERE identity IS NULL OR pg_catalog.btrim(identity) = ''
       ) THEN
        RAISE EXCEPTION 'legacy MCP migration batch lock identities are invalid';
    END IF;

    FOR lock_identity IN
        SELECT DISTINCT identity
        FROM pg_catalog.unnest(p_lock_identities) AS value(identity)
        ORDER BY identity
    LOOP
        PERFORM pg_catalog.pg_advisory_xact_lock(
            pg_catalog.hashtextextended(lock_identity, 0)
        );
    END LOOP;
END;
$function$;

CREATE OR REPLACE FUNCTION mcp_migration_api.read_legacy_migration_replay_snapshot(
    p_migration_id pg_catalog.text,
    p_plan_fingerprint pg_catalog.text,
    p_source_server_id pg_catalog.text,
    p_source_fingerprint pg_catalog.text,
    p_owner_consumer_ref pg_catalog.text,
    p_target_server_id pg_catalog.text
) RETURNS pg_catalog.jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    existing_record public.mcp_legacy_migration_record;
    existing_server public.user_mcp_server;
BEGIN
    IF p_migration_id !~ '^sha256:[0-9a-f]{64}$'
       OR p_plan_fingerprint !~ '^sha256:[0-9a-f]{64}$'
       OR p_source_server_id IS NULL OR pg_catalog.btrim(p_source_server_id) = ''
       OR p_source_fingerprint !~ '^sha256:[0-9a-f]{64}$'
       OR p_owner_consumer_ref !~ '^hmac-sha256:[0-9a-f]{64}$'
       OR p_target_server_id IS NULL OR pg_catalog.btrim(p_target_server_id) = '' THEN
        RAISE EXCEPTION 'legacy MCP migration replay identity is invalid';
    END IF;

    SELECT * INTO existing_record
    FROM public.mcp_legacy_migration_record
    WHERE migration_id = p_migration_id
       OR (plan_fingerprint = p_plan_fingerprint
           AND source_server_id = p_source_server_id)
       OR target_server_id = p_target_server_id
    ORDER BY migration_id
    LIMIT 1;

    SELECT * INTO existing_server
    FROM public.user_mcp_server
    WHERE server_id = p_target_server_id;

    IF existing_record.migration_id IS NULL AND existing_server.server_id IS NULL THEN
        RETURN NULL;
    END IF;
    IF existing_record.migration_id IS NULL
       OR existing_server.server_id IS NULL
       OR existing_record.migration_id IS DISTINCT FROM p_migration_id
       OR existing_record.plan_fingerprint IS DISTINCT FROM p_plan_fingerprint
       OR existing_record.source_server_id IS DISTINCT FROM p_source_server_id
       OR existing_record.source_fingerprint IS DISTINCT FROM p_source_fingerprint
       OR existing_record.owner_consumer_ref IS DISTINCT FROM p_owner_consumer_ref
       OR existing_record.target_server_id IS DISTINCT FROM p_target_server_id THEN
        RETURN pg_catalog.jsonb_build_object('status', 'conflict');
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'status', 'exact',
        'server', (pg_catalog.to_jsonb(existing_server)
            - 'credential_ciphertext' - 'credential_nonce')
            || pg_catalog.jsonb_build_object(
                'credential_configured',
                existing_server.credential_ciphertext IS NOT NULL,
                'credential_storage_digest',
                'sha256:' || pg_catalog.encode(
                    pg_catalog.sha256(
                        pg_catalog.convert_to(
                            CASE
                                WHEN existing_server.credential_ciphertext IS NULL
                                THEN 'legacy_mcp_credential_storage.v1:none'
                                ELSE 'legacy_mcp_credential_storage.v1:'
                                    || pg_catalog.encode(
                                        existing_server.credential_ciphertext,
                                        'hex'
                                    )
                                    || ':' || pg_catalog.encode(
                                        existing_server.credential_nonce,
                                        'hex'
                                    )
                                    || ':'
                                    || existing_server.encryption_version::pg_catalog.text
                            END,
                            'UTF8'
                        )
                    ),
                    'hex'
                )
            ),
        'record', pg_catalog.to_jsonb(existing_record)
    );
END;
$function$;

CREATE OR REPLACE FUNCTION mcp_migration_api.apply_legacy_migration_candidate(
    p_server_id pg_catalog.text,
    p_owner_user_id pg_catalog.text,
    p_display_name pg_catalog.text,
    p_routing_description pg_catalog.text,
    p_endpoint_url pg_catalog.text,
    p_transport pg_catalog.text,
    p_protocol_preference pg_catalog.text,
    p_auth_type pg_catalog.text,
    p_auth_metadata pg_catalog.jsonb,
    p_enabled pg_catalog.bool,
    p_health_status pg_catalog.text,
    p_config_version pg_catalog.int8,
    p_security_version pg_catalog.int8,
    p_credential_ciphertext pg_catalog.bytea,
    p_credential_nonce pg_catalog.bytea,
    p_encryption_version pg_catalog.int4,
    p_credential_updated_at pg_catalog.timestamptz,
    p_last_tested_at pg_catalog.timestamptz,
    p_last_test_error_code pg_catalog.text,
    p_created_at pg_catalog.timestamptz,
    p_updated_at pg_catalog.timestamptz,
    p_migration_id pg_catalog.text,
    p_plan_fingerprint pg_catalog.text,
    p_source_server_id pg_catalog.text,
    p_source_fingerprint pg_catalog.text,
    p_owner_consumer_ref pg_catalog.text,
    p_target_server_id pg_catalog.text,
    p_target_consumer_set_digest pg_catalog.text,
    p_capability_obligations_fingerprint pg_catalog.text,
    p_catalog_fingerprint pg_catalog.text,
    p_capability_fingerprint pg_catalog.text,
    p_validator_provenance_fingerprint pg_catalog.text,
    p_credential_digest pg_catalog.text,
    p_occurred_at pg_catalog.timestamptz,
    p_evidence_expires_at pg_catalog.timestamptz
) RETURNS pg_catalog.bool
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    existing_server public.user_mcp_server;
    existing_by_id public.mcp_legacy_migration_record;
    existing_by_plan_source public.mcp_legacy_migration_record;
    existing_by_target public.mcp_legacy_migration_record;
    existing_record public.mcp_legacy_migration_record;
    server_missing pg_catalog.bool;
    record_missing pg_catalog.bool;
    lock_identity pg_catalog.text;
    credential_storage_digest pg_catalog.text;
BEGIN
    credential_storage_digest := 'sha256:' || pg_catalog.encode(
        pg_catalog.sha256(
            pg_catalog.convert_to(
                CASE
                    WHEN p_credential_ciphertext IS NULL
                    THEN 'legacy_mcp_credential_storage.v1:none'
                    ELSE 'legacy_mcp_credential_storage.v1:'
                        || pg_catalog.encode(p_credential_ciphertext, 'hex')
                        || ':' || pg_catalog.encode(p_credential_nonce, 'hex')
                        || ':' || p_encryption_version::pg_catalog.text
                END,
                'UTF8'
            )
        ),
        'hex'
    );
    IF p_server_id IS NULL OR pg_catalog.btrim(p_server_id) = ''
       OR p_target_server_id IS DISTINCT FROM p_server_id
       OR p_owner_user_id IS NULL OR pg_catalog.btrim(p_owner_user_id) = ''
       OR p_display_name IS NULL OR pg_catalog.btrim(p_display_name) = ''
       OR p_routing_description IS NULL OR pg_catalog.btrim(p_routing_description) = ''
       OR p_endpoint_url IS NULL OR p_endpoint_url !~ '^https?://[^[:space:]]+$'
       OR p_transport NOT IN ('streamable_http', 'legacy_http_sse')
       OR p_protocol_preference NOT IN (
           'auto', '2024-11-05', '2025-03-26', '2025-06-18',
           '2025-11-25', '2026-07-28'
       )
       OR p_auth_type NOT IN ('none', 'bearer', 'api_key_header', 'static_headers')
       OR p_auth_metadata IS NULL
       OR pg_catalog.jsonb_typeof(p_auth_metadata) <> 'object'
       OR p_auth_metadata #>> ARRAY[
            'migration_provenance', 'credential_storage_digest'
       ] IS DISTINCT FROM credential_storage_digest
       OR p_enabled IS NULL
       OR p_health_status NOT IN ('available', 'disabled')
       OR (p_enabled AND p_health_status <> 'available')
       OR (NOT p_enabled AND p_health_status <> 'disabled')
       OR p_config_version IS NULL OR p_config_version < 1
       OR p_security_version IS NULL OR p_security_version < 1
       OR p_last_tested_at IS NULL
       OR p_last_test_error_code IS NOT NULL
       OR p_created_at IS NULL
       OR p_updated_at IS NULL
       OR p_updated_at < p_created_at
       OR NOT (
           (p_credential_ciphertext IS NULL
            AND p_credential_nonce IS NULL
            AND p_encryption_version IS NULL
            AND p_credential_updated_at IS NULL)
           OR
           (p_credential_ciphertext IS NOT NULL
            AND pg_catalog.octet_length(p_credential_ciphertext) > 0
            AND p_credential_nonce IS NOT NULL
            AND pg_catalog.octet_length(p_credential_nonce) > 0
            AND p_encryption_version IS NOT NULL
            AND p_encryption_version > 0
            AND p_credential_updated_at IS NOT NULL)
       ) THEN
        RAISE EXCEPTION 'legacy MCP migration server candidate is invalid';
    END IF;

    IF p_migration_id !~ '^sha256:[0-9a-f]{64}$'
       OR p_plan_fingerprint !~ '^sha256:[0-9a-f]{64}$'
       OR p_source_server_id IS NULL OR pg_catalog.btrim(p_source_server_id) = ''
       OR p_source_fingerprint !~ '^sha256:[0-9a-f]{64}$'
       OR p_owner_consumer_ref !~ '^hmac-sha256:[0-9a-f]{64}$'
       OR p_target_consumer_set_digest !~ '^sha256:[0-9a-f]{64}$'
       OR p_capability_obligations_fingerprint !~ '^sha256:[0-9a-f]{64}$'
       OR p_catalog_fingerprint !~ '^sha256:[0-9a-f]{64}$'
       OR p_capability_fingerprint !~ '^sha256:[0-9a-f]{64}$'
       OR p_validator_provenance_fingerprint !~ '^sha256:[0-9a-f]{64}$'
       OR p_credential_digest !~ '^hmac-sha256:[0-9a-f]{64}$'
       OR p_occurred_at IS NULL
       OR p_evidence_expires_at IS NULL
       OR p_occurred_at >= p_evidence_expires_at THEN
        RAISE EXCEPTION 'legacy MCP migration record candidate is invalid';
    END IF;

    -- Direct single-candidate callers use the same global lock ordering as the
    -- batch pre-lock API. The batch API acquires the union before any read/write.
    FOR lock_identity IN
        SELECT identity
        FROM pg_catalog.unnest(ARRAY[
            'migration:' || p_migration_id,
            'plan_source:' || p_plan_fingerprint || ':' || p_source_server_id,
            'target:' || p_target_server_id
        ]) AS value(identity)
        ORDER BY identity
    LOOP
        PERFORM pg_catalog.pg_advisory_xact_lock(
            pg_catalog.hashtextextended(lock_identity, 0)
        );
    END LOOP;

    SELECT * INTO existing_server
    FROM public.user_mcp_server
    WHERE server_id = p_server_id;

    SELECT * INTO existing_by_id
    FROM public.mcp_legacy_migration_record
    WHERE migration_id = p_migration_id;
    SELECT * INTO existing_by_plan_source
    FROM public.mcp_legacy_migration_record
    WHERE plan_fingerprint = p_plan_fingerprint
      AND source_server_id = p_source_server_id;
    SELECT * INTO existing_by_target
    FROM public.mcp_legacy_migration_record
    WHERE target_server_id = p_target_server_id;

    IF existing_by_id.migration_id IS NOT NULL THEN
        existing_record := existing_by_id;
    END IF;
    IF existing_by_plan_source.migration_id IS NOT NULL THEN
        IF existing_record.migration_id IS NOT NULL
           AND existing_record.migration_id IS DISTINCT FROM existing_by_plan_source.migration_id THEN
            RAISE EXCEPTION 'legacy MCP migration identity conflicts';
        END IF;
        existing_record := existing_by_plan_source;
    END IF;
    IF existing_by_target.migration_id IS NOT NULL THEN
        IF existing_record.migration_id IS NOT NULL
           AND existing_record.migration_id IS DISTINCT FROM existing_by_target.migration_id THEN
            RAISE EXCEPTION 'legacy MCP migration identity conflicts';
        END IF;
        existing_record := existing_by_target;
    END IF;

    server_missing := existing_server.server_id IS NULL;
    record_missing := existing_record.migration_id IS NULL;

    IF (server_missing OR record_missing)
       AND pg_catalog.statement_timestamp() > p_evidence_expires_at THEN
        RAISE EXCEPTION 'legacy MCP migration evidence is expired';
    END IF;

    IF NOT server_missing AND ROW(
        existing_server.server_id, existing_server.owner_user_id,
        existing_server.display_name, existing_server.routing_description,
        existing_server.endpoint_url, existing_server.transport,
        existing_server.protocol_preference, existing_server.auth_type,
        existing_server.auth_metadata, existing_server.enabled,
        existing_server.health_status, existing_server.config_version,
        existing_server.security_version, existing_server.credential_ciphertext,
        existing_server.credential_nonce, existing_server.encryption_version,
        existing_server.credential_updated_at, existing_server.last_tested_at,
        existing_server.last_test_error_code, existing_server.deletion_pending,
        existing_server.deleted_at, existing_server.created_at,
        existing_server.updated_at
    ) IS DISTINCT FROM ROW(
        p_server_id, p_owner_user_id, p_display_name, p_routing_description,
        p_endpoint_url, p_transport, p_protocol_preference, p_auth_type,
        p_auth_metadata, p_enabled, p_health_status, p_config_version,
        p_security_version, p_credential_ciphertext, p_credential_nonce,
        p_encryption_version, p_credential_updated_at, p_last_tested_at,
        p_last_test_error_code, FALSE, NULL::pg_catalog.timestamptz,
        p_created_at, p_updated_at
    ) THEN
        RAISE EXCEPTION 'legacy MCP migration server conflicts';
    END IF;

    IF NOT record_missing AND ROW(
        existing_record.migration_id, existing_record.event_type,
        existing_record.plan_fingerprint, existing_record.source_server_id,
        existing_record.source_fingerprint, existing_record.owner_consumer_ref,
        existing_record.target_server_id,
        existing_record.target_consumer_set_digest,
        existing_record.capability_obligations_fingerprint,
        existing_record.catalog_fingerprint,
        existing_record.capability_fingerprint,
        existing_record.validator_provenance_fingerprint,
        existing_record.credential_digest, existing_record.disposition,
        existing_record.occurred_at, existing_record.evidence_expires_at
    ) IS DISTINCT FROM ROW(
        p_migration_id, 'mcp.legacy.config_migrated', p_plan_fingerprint,
        p_source_server_id, p_source_fingerprint, p_owner_consumer_ref,
        p_target_server_id, p_target_consumer_set_digest,
        p_capability_obligations_fingerprint, p_catalog_fingerprint,
        p_capability_fingerprint, p_validator_provenance_fingerprint,
        p_credential_digest, 'migrate_owner', p_occurred_at,
        p_evidence_expires_at
    ) THEN
        RAISE EXCEPTION 'legacy MCP migration record conflicts';
    END IF;

    IF server_missing THEN
        INSERT INTO public.user_mcp_server (
            server_id, owner_user_id, display_name, routing_description,
            endpoint_url, transport, protocol_preference, auth_type,
            auth_metadata, enabled, health_status, config_version,
            security_version, credential_ciphertext, credential_nonce,
            encryption_version, credential_updated_at, last_tested_at,
            last_test_error_code, deletion_pending, deleted_at, created_at,
            updated_at
        ) VALUES (
            p_server_id, p_owner_user_id, p_display_name, p_routing_description,
            p_endpoint_url, p_transport, p_protocol_preference, p_auth_type,
            p_auth_metadata, p_enabled, p_health_status, p_config_version,
            p_security_version, p_credential_ciphertext, p_credential_nonce,
            p_encryption_version, p_credential_updated_at, p_last_tested_at,
            p_last_test_error_code, FALSE, NULL, p_created_at, p_updated_at
        );
    END IF;

    IF record_missing THEN
        INSERT INTO public.mcp_legacy_migration_record (
            migration_id, event_type, plan_fingerprint, source_server_id,
            source_fingerprint, owner_consumer_ref, target_server_id,
            target_consumer_set_digest, capability_obligations_fingerprint,
            catalog_fingerprint, capability_fingerprint,
            validator_provenance_fingerprint, credential_digest, disposition,
            occurred_at, evidence_expires_at
        ) VALUES (
            p_migration_id, 'mcp.legacy.config_migrated', p_plan_fingerprint,
            p_source_server_id, p_source_fingerprint, p_owner_consumer_ref,
            p_target_server_id, p_target_consumer_set_digest,
            p_capability_obligations_fingerprint, p_catalog_fingerprint,
            p_capability_fingerprint, p_validator_provenance_fingerprint,
            p_credential_digest, 'migrate_owner', p_occurred_at,
            p_evidence_expires_at
        );
    END IF;

    RETURN server_missing OR record_missing;
END;
$function$;

CREATE OR REPLACE FUNCTION mcp_migration_api.reject_legacy_migration_mutation()
RETURNS pg_catalog.trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $function$
BEGIN
    RAISE EXCEPTION 'legacy MCP migration record is append-only';
END;
$function$;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_trigger
        WHERE tgrelid = pg_catalog.to_regclass('public.mcp_legacy_migration_record')
          AND tgname = 'mcp_legacy_migration_record_append_only'
          AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER mcp_legacy_migration_record_append_only
        BEFORE UPDATE OR DELETE ON public.mcp_legacy_migration_record
        FOR EACH ROW
        EXECUTE FUNCTION mcp_migration_api.reject_legacy_migration_mutation();
    END IF;
END; $$;

ALTER FUNCTION mcp_migration_api.apply_legacy_migration_candidate(
    pg_catalog.text, pg_catalog.text, pg_catalog.text, pg_catalog.text,
    pg_catalog.text, pg_catalog.text, pg_catalog.text, pg_catalog.text,
    pg_catalog.jsonb, pg_catalog.bool, pg_catalog.text, pg_catalog.int8,
    pg_catalog.int8, pg_catalog.bytea, pg_catalog.bytea, pg_catalog.int4,
    pg_catalog.timestamptz, pg_catalog.timestamptz, pg_catalog.text,
    pg_catalog.timestamptz, pg_catalog.timestamptz, pg_catalog.text,
    pg_catalog.text, pg_catalog.text, pg_catalog.text, pg_catalog.text,
    pg_catalog.text, pg_catalog.text, pg_catalog.text, pg_catalog.text,
    pg_catalog.text, pg_catalog.text, pg_catalog.text, pg_catalog.timestamptz,
    pg_catalog.timestamptz
) OWNER TO maf_mcp_migration_api_owner;
ALTER FUNCTION mcp_migration_api.lock_legacy_migration_batch(pg_catalog.text[])
    OWNER TO maf_mcp_migration_api_owner;
ALTER FUNCTION mcp_migration_api.read_legacy_migration_replay_snapshot(
    pg_catalog.text, pg_catalog.text, pg_catalog.text, pg_catalog.text,
    pg_catalog.text, pg_catalog.text
) OWNER TO maf_mcp_migration_api_owner;
ALTER FUNCTION mcp_migration_api.reject_legacy_migration_mutation()
    OWNER TO maf_mcp_migration_api_owner;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA mcp_migration_api FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA mcp_migration_api
    FROM maf_mcp_legacy_migrator;
GRANT EXECUTE ON FUNCTION mcp_migration_api.lock_legacy_migration_batch(
    pg_catalog.text[]
) TO maf_mcp_legacy_migrator;
GRANT EXECUTE ON FUNCTION mcp_migration_api.read_legacy_migration_replay_snapshot(
    pg_catalog.text, pg_catalog.text, pg_catalog.text, pg_catalog.text,
    pg_catalog.text, pg_catalog.text
) TO maf_mcp_legacy_migrator;
GRANT EXECUTE ON FUNCTION mcp_migration_api.apply_legacy_migration_candidate(
    pg_catalog.text, pg_catalog.text, pg_catalog.text, pg_catalog.text,
    pg_catalog.text, pg_catalog.text, pg_catalog.text, pg_catalog.text,
    pg_catalog.jsonb, pg_catalog.bool, pg_catalog.text, pg_catalog.int8,
    pg_catalog.int8, pg_catalog.bytea, pg_catalog.bytea, pg_catalog.int4,
    pg_catalog.timestamptz, pg_catalog.timestamptz, pg_catalog.text,
    pg_catalog.timestamptz, pg_catalog.timestamptz, pg_catalog.text,
    pg_catalog.text, pg_catalog.text, pg_catalog.text, pg_catalog.text,
    pg_catalog.text, pg_catalog.text, pg_catalog.text, pg_catalog.text,
    pg_catalog.text, pg_catalog.text, pg_catalog.text, pg_catalog.timestamptz,
    pg_catalog.timestamptz
) TO maf_mcp_legacy_migrator;
