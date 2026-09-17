-- PostgreSQL least-privilege contract for the user-MCP phase-3 rollout ledger.
-- Apply after the runtime schema. Role membership is intentionally managed
-- by the deployment platform, outside this credential-free template.

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'maf_rollout_app_writer') THEN
        CREATE ROLE maf_rollout_app_writer NOLOGIN;
    END IF;
END; $$;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'maf_rollout_snapshot_producer') THEN
        CREATE ROLE maf_rollout_snapshot_producer NOLOGIN;
    END IF;
END; $$;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'maf_rollout_ci_evidence_writer') THEN
        CREATE ROLE maf_rollout_ci_evidence_writer NOLOGIN;
    END IF;
END; $$;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'maf_rollout_gate_evaluator') THEN
        CREATE ROLE maf_rollout_gate_evaluator NOLOGIN;
    END IF;
END; $$;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'maf_rollout_operator') THEN
        CREATE ROLE maf_rollout_operator NOLOGIN;
    END IF;
END; $$;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'maf_rollout_validator') THEN
        CREATE ROLE maf_rollout_validator NOLOGIN;
    END IF;
END; $$;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'maf_rollout_drill_recorder') THEN
        CREATE ROLE maf_rollout_drill_recorder NOLOGIN;
    END IF;
END; $$;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'maf_rollout_api_owner') THEN
        CREATE ROLE maf_rollout_api_owner NOLOGIN;
    END IF;
END; $$;

ALTER ROLE maf_rollout_app_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE maf_rollout_snapshot_producer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE maf_rollout_ci_evidence_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE maf_rollout_gate_evaluator NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE maf_rollout_operator NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE maf_rollout_validator NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE maf_rollout_drill_recorder NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE maf_rollout_api_owner NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

DO $$ BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member = (
            SELECT oid FROM pg_catalog.pg_roles
            WHERE rolname = 'maf_rollout_api_owner'
        )
           OR membership.roleid = (
            SELECT oid FROM pg_catalog.pg_roles
            WHERE rolname = 'maf_rollout_api_owner'
        )
    ) THEN
        RAISE EXCEPTION 'rollout API owner must not inherit any role';
    END IF;
END; $$;

CREATE SCHEMA IF NOT EXISTS mcp_rollout_api;
REVOKE ALL ON SCHEMA mcp_rollout_api FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA mcp_rollout_api REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
GRANT USAGE ON SCHEMA mcp_rollout_api TO
    maf_rollout_app_writer,
    maf_rollout_snapshot_producer,
    maf_rollout_ci_evidence_writer,
    maf_rollout_gate_evaluator,
    maf_rollout_operator,
    maf_rollout_drill_recorder;

CREATE TABLE IF NOT EXISTS public.mcp_rollout_drill_observation (
    drill_observation_id pg_catalog.text PRIMARY KEY,
    environment_id pg_catalog.text NOT NULL,
    rollout_program pg_catalog.text NOT NULL,
    deployment_id pg_catalog.text NOT NULL,
    stage pg_catalog.text NOT NULL,
    config_fingerprint pg_catalog.text NOT NULL,
    drill pg_catalog.text NOT NULL,
    outcome pg_catalog.text NOT NULL,
    observed_at pg_catalog.timestamptz NOT NULL,
    recorded_at pg_catalog.timestamptz NOT NULL,
    expires_at pg_catalog.timestamptz NOT NULL,
    payload_digest pg_catalog.text NOT NULL,
    CONSTRAINT mcp_rollout_drill_program
        CHECK (rollout_program = 'user_mcp_phase3'),
    CONSTRAINT mcp_rollout_drill_stage CHECK (stage = 'internal_enforce'),
    CONSTRAINT mcp_rollout_drill_name CHECK (drill IN (
        'cancellation', 'long_call_120_seconds', 'disconnect_five_minutes',
        'restart_unknown', 'mrtr_recovery', 'tasks_recovery',
        'fair_queueing', 'flag_rollback'
    )),
    CONSTRAINT mcp_rollout_drill_outcome CHECK (outcome IN ('passed', 'failed')),
    CONSTRAINT mcp_rollout_drill_time
        CHECK (recorded_at >= observed_at AND expires_at > recorded_at),
    CONSTRAINT uq_mcp_rollout_drill_scope_observed UNIQUE (
        environment_id, deployment_id, stage, config_fingerprint,
        drill, observed_at
    )
);
CREATE INDEX IF NOT EXISTS idx_mcp_rollout_drill_scope_window
ON public.mcp_rollout_drill_observation (
    environment_id, deployment_id, stage, observed_at
);

REVOKE ALL PRIVILEGES ON TABLE
    public.mcp_rollout_gate_scope,
    public.mcp_rollout_metric_bucket,
    public.mcp_shadow_audit_sample,
    public.mcp_rollout_evidence_snapshot,
    public.mcp_rollout_stage_approval,
    public.mcp_rollout_deployment_activation,
    public.mcp_rollout_promotion_block,
    public.mcp_rollout_block_resolution,
    public.mcp_rollout_instance_config,
    public.mcp_rollout_drill_observation
FROM PUBLIC;

REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE
    public.mcp_rollout_gate_scope,
    public.mcp_rollout_metric_bucket,
    public.mcp_shadow_audit_sample,
    public.mcp_rollout_evidence_snapshot,
    public.mcp_rollout_stage_approval,
    public.mcp_rollout_deployment_activation,
    public.mcp_rollout_promotion_block,
    public.mcp_rollout_block_resolution,
    public.mcp_rollout_instance_config,
    public.mcp_rollout_drill_observation
FROM
    maf_rollout_app_writer,
    maf_rollout_snapshot_producer,
    maf_rollout_ci_evidence_writer,
    maf_rollout_gate_evaluator,
    maf_rollout_operator,
    maf_rollout_validator,
    maf_rollout_drill_recorder;

GRANT SELECT ON TABLE
    public.mcp_rollout_gate_scope,
    public.mcp_rollout_metric_bucket,
    public.mcp_shadow_audit_sample,
    public.mcp_rollout_evidence_snapshot,
    public.mcp_rollout_stage_approval,
    public.mcp_rollout_deployment_activation,
    public.mcp_rollout_promotion_block,
    public.mcp_rollout_block_resolution,
    public.mcp_rollout_instance_config,
    public.mcp_rollout_drill_observation
TO maf_rollout_validator;

-- The snapshot producer receives only de-identified durable samples and metric
-- buckets. It can append signed production evidence through the function below,
-- but has no base-table DML. Application and CI roles cannot author production
-- evidence.
GRANT SELECT ON TABLE
    public.mcp_shadow_audit_sample,
    public.mcp_rollout_metric_bucket,
    public.mcp_rollout_drill_observation
TO maf_rollout_snapshot_producer;

GRANT SELECT ON TABLE
    public.mcp_rollout_gate_scope,
    public.mcp_rollout_metric_bucket,
    public.mcp_rollout_evidence_snapshot,
    public.mcp_rollout_stage_approval,
    public.mcp_rollout_deployment_activation,
    public.mcp_rollout_promotion_block,
    public.mcp_rollout_block_resolution,
    public.mcp_rollout_instance_config
TO maf_rollout_gate_evaluator, maf_rollout_operator;

GRANT SELECT ON TABLE
    public.mcp_rollout_gate_scope,
    public.mcp_rollout_metric_bucket,
    public.mcp_rollout_deployment_activation,
    public.mcp_rollout_instance_config
TO maf_rollout_app_writer;

CREATE OR REPLACE FUNCTION mcp_rollout_api.canonical_jsonb_text(
    p_value pg_catalog.jsonb
) RETURNS pg_catalog.text
LANGUAGE sql
IMMUTABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
    SELECT CASE pg_catalog.jsonb_typeof(p_value)
        WHEN 'object' THEN '{' || COALESCE((
            SELECT pg_catalog.string_agg(
                pg_catalog.to_jsonb(item.key)::pg_catalog.text || ':' ||
                mcp_rollout_api.canonical_jsonb_text(item.value),
                ',' ORDER BY item.key
            )
            FROM pg_catalog.jsonb_each(p_value) AS item(key, value)
        ), '') || '}'
        WHEN 'array' THEN '[' || COALESCE((
            SELECT pg_catalog.string_agg(
                mcp_rollout_api.canonical_jsonb_text(item.value),
                ',' ORDER BY item.ordinality
            )
            FROM pg_catalog.jsonb_array_elements(p_value)
                WITH ORDINALITY AS item(value, ordinality)
        ), '') || ']'
        ELSE p_value::pg_catalog.text
    END;
$function$;

CREATE OR REPLACE FUNCTION mcp_rollout_api.canonical_timestamp(
    p_value pg_catalog.timestamptz
) RETURNS pg_catalog.text
LANGUAGE sql
IMMUTABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
    SELECT pg_catalog.to_char(
        p_value AT TIME ZONE 'UTC',
        'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
    );
$function$;

CREATE OR REPLACE FUNCTION mcp_rollout_api.append_drill_observation(
    p_drill_observation_id pg_catalog.text,
    p_environment_id pg_catalog.text,
    p_deployment_id pg_catalog.text,
    p_config_fingerprint pg_catalog.text,
    p_drill pg_catalog.text,
    p_outcome pg_catalog.text,
    p_observed_at pg_catalog.timestamptz,
    p_recorded_at pg_catalog.timestamptz,
    p_expires_at pg_catalog.timestamptz,
    p_payload_digest pg_catalog.text
) RETURNS public.mcp_rollout_drill_observation
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    existing public.mcp_rollout_drill_observation;
    saved public.mcp_rollout_drill_observation;
    expected_digest pg_catalog.text;
BEGIN
    IF p_drill_observation_id IS NULL
       OR pg_catalog.btrim(p_drill_observation_id) = ''
       OR p_environment_id IS NULL OR pg_catalog.btrim(p_environment_id) = ''
       OR p_deployment_id IS NULL OR pg_catalog.btrim(p_deployment_id) = ''
       OR p_config_fingerprint IS NULL
       OR p_config_fingerprint !~ '^[0-9a-f]{64}$'
       OR p_drill IS NULL OR p_drill NOT IN (
           'cancellation', 'long_call_120_seconds', 'disconnect_five_minutes',
           'restart_unknown', 'mrtr_recovery', 'tasks_recovery',
           'fair_queueing', 'flag_rollback'
       )
       OR p_outcome IS NULL OR p_outcome NOT IN ('passed', 'failed')
       OR p_observed_at IS NULL OR p_recorded_at IS NULL OR p_expires_at IS NULL
       OR p_recorded_at < p_observed_at OR p_expires_at <= p_recorded_at
       OR p_payload_digest IS NULL OR p_payload_digest !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'rollout drill observation is invalid';
    END IF;
    expected_digest := pg_catalog.encode(
        pg_catalog.sha256(
            pg_catalog.convert_to(
                mcp_rollout_api.canonical_jsonb_text(
                    pg_catalog.jsonb_build_object(
                        'drill_observation_id', p_drill_observation_id,
                        'environment_id', p_environment_id,
                        'rollout_program', 'user_mcp_phase3',
                        'deployment_id', p_deployment_id,
                        'stage', 'internal_enforce',
                        'config_fingerprint', p_config_fingerprint,
                        'drill', p_drill,
                        'outcome', p_outcome,
                        'observed_at', mcp_rollout_api.canonical_timestamp(p_observed_at),
                        'recorded_at', mcp_rollout_api.canonical_timestamp(p_recorded_at),
                        'expires_at', mcp_rollout_api.canonical_timestamp(p_expires_at)
                    )
                ),
                'UTF8'
            )
        ),
        'hex'
    );
    IF p_payload_digest IS DISTINCT FROM expected_digest THEN
        RAISE EXCEPTION 'rollout drill observation digest is invalid';
    END IF;

    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(p_drill_observation_id, 0)
    );
    SELECT * INTO existing
    FROM public.mcp_rollout_drill_observation
    WHERE drill_observation_id = p_drill_observation_id;
    IF existing.drill_observation_id IS NOT NULL THEN
        IF ROW(
            existing.environment_id, existing.rollout_program,
            existing.deployment_id, existing.stage,
            existing.config_fingerprint, existing.drill, existing.outcome,
            existing.observed_at, existing.recorded_at, existing.expires_at,
            existing.payload_digest
        ) IS DISTINCT FROM ROW(
            p_environment_id, 'user_mcp_phase3', p_deployment_id,
            'internal_enforce', p_config_fingerprint, p_drill, p_outcome,
            p_observed_at, p_recorded_at, p_expires_at, p_payload_digest
        ) THEN
            RAISE EXCEPTION 'rollout drill observation ID payload conflict';
        END IF;
        RETURN existing;
    END IF;

    INSERT INTO public.mcp_rollout_drill_observation (
        drill_observation_id, environment_id, rollout_program, deployment_id,
        stage, config_fingerprint, drill, outcome, observed_at, recorded_at,
        expires_at, payload_digest
    ) VALUES (
        p_drill_observation_id, p_environment_id, 'user_mcp_phase3',
        p_deployment_id, 'internal_enforce', p_config_fingerprint, p_drill,
        p_outcome, p_observed_at, p_recorded_at, p_expires_at, p_payload_digest
    ) RETURNING * INTO saved;
    RETURN saved;
END;
$function$;

CREATE OR REPLACE FUNCTION mcp_rollout_api.lock_gate_scope(
    p_environment_id pg_catalog.text,
    p_created_at pg_catalog.timestamptz
) RETURNS pg_catalog.void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
BEGIN
    INSERT INTO public.mcp_rollout_gate_scope (environment_id, rollout_program, created_at)
    VALUES (p_environment_id, 'user_mcp_phase3', p_created_at)
    ON CONFLICT (environment_id, rollout_program) DO NOTHING;

    PERFORM 1
    FROM public.mcp_rollout_gate_scope
    WHERE environment_id = p_environment_id
      AND rollout_program = 'user_mcp_phase3'
    FOR UPDATE;
END;
$function$;

CREATE OR REPLACE FUNCTION mcp_rollout_api.upsert_metric_bucket(
    p_metric_bucket_id pg_catalog.text,
    p_environment_id pg_catalog.text,
    p_deployment_id pg_catalog.text,
    p_stage pg_catalog.text,
    p_config_fingerprint pg_catalog.text,
    p_metric_name pg_catalog.text,
    p_bucket_started_at pg_catalog.timestamptz,
    p_bucket_ended_at pg_catalog.timestamptz,
    p_execution_path pg_catalog.text,
    p_routing_mode pg_catalog.text,
    p_transport pg_catalog.text,
    p_protocol_version pg_catalog.text,
    p_adapter pg_catalog.text,
    p_result_category pg_catalog.text,
    p_error_category pg_catalog.text,
    p_call_kind pg_catalog.text,
    p_red_line pg_catalog.text,
    p_latency_bucket pg_catalog.text,
    p_value pg_catalog.int8,
    p_recorded_at pg_catalog.timestamptz
) RETURNS public.mcp_rollout_metric_bucket
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    saved public.mcp_rollout_metric_bucket;
    activation public.mcp_rollout_deployment_activation;
    v_block_id pg_catalog.text;
BEGIN
    IF p_bucket_started_at IS NULL
       OR p_bucket_ended_at IS NULL
       OR pg_catalog.date_trunc(
            'minute', p_bucket_started_at AT TIME ZONE 'UTC'
       ) <> p_bucket_started_at AT TIME ZONE 'UTC'
       OR p_bucket_ended_at <> p_bucket_started_at + INTERVAL '1 minute' THEN
        RAISE EXCEPTION 'metric bucket must be one complete UTC-aligned minute';
    END IF;
    IF p_value IS NULL OR p_value < 0 THEN
        RAISE EXCEPTION 'additive metric value must be non-negative';
    END IF;
    IF p_metric_name = 'mcp_safety_red_line_total' AND p_value > 0 THEN
        IF p_red_line IS NULL OR p_red_line = 'not_applicable' THEN
            RAISE EXCEPTION 'positive safety red-line metric requires a red-line label';
        END IF;
        PERFORM mcp_rollout_api.lock_gate_scope(p_environment_id, p_recorded_at);
        SELECT * INTO activation
        FROM public.mcp_rollout_deployment_activation AS candidate
        WHERE candidate.environment_id = p_environment_id
          AND candidate.rollout_program = 'user_mcp_phase3'
          AND candidate.deployment_id = p_deployment_id
          AND candidate.stage = p_stage
          AND candidate.config_fingerprint = p_config_fingerprint
        ORDER BY candidate.created_at DESC, candidate.activation_id DESC
        LIMIT 1;
        IF activation.activation_id IS NULL THEN
            RAISE EXCEPTION 'positive safety red-line metric has no exact activation';
        END IF;
        v_block_id := 'redline-' || pg_catalog.substring(
            pg_catalog.encode(
                pg_catalog.sha256(
                    pg_catalog.convert_to(
                        p_environment_id || ':' || p_deployment_id || ':' ||
                        p_stage || ':' || p_config_fingerprint || ':' ||
                        activation.evidence_id || ':safety_red_line_nonzero',
                        'UTF8'
                    )
                ),
                'hex'
            ), 1, 56
        );
        INSERT INTO public.mcp_rollout_promotion_block (
            block_id, environment_id, rollout_program, deployment_id, stage,
            config_fingerprint, evidence_id, reason_code, created_at
        ) VALUES (
            v_block_id, p_environment_id, 'user_mcp_phase3', p_deployment_id,
            p_stage, p_config_fingerprint, activation.evidence_id,
            'safety_red_line_nonzero', p_recorded_at
        ) ON CONFLICT (block_id) DO NOTHING;
    END IF;
    INSERT INTO public.mcp_rollout_metric_bucket (
        metric_bucket_id, environment_id, rollout_program, deployment_id, stage,
        config_fingerprint, metric_name, bucket_started_at, bucket_ended_at,
        execution_path, routing_mode, transport, protocol_version, adapter,
        result_category, error_category, call_kind, red_line, latency_bucket,
        value, created_at, updated_at
    ) VALUES (
        p_metric_bucket_id, p_environment_id, 'user_mcp_phase3', p_deployment_id, p_stage,
        p_config_fingerprint, p_metric_name, p_bucket_started_at, p_bucket_ended_at,
        p_execution_path, p_routing_mode, p_transport, p_protocol_version, p_adapter,
        p_result_category, p_error_category, p_call_kind, p_red_line, p_latency_bucket,
        p_value, p_recorded_at, p_recorded_at
    )
    ON CONFLICT ON CONSTRAINT uq_mcp_rollout_metric_series_bucket DO UPDATE
    SET value = public.mcp_rollout_metric_bucket.value + EXCLUDED.value,
        updated_at = EXCLUDED.updated_at
    RETURNING * INTO saved;
    RETURN saved;
END;
$function$;

CREATE OR REPLACE FUNCTION mcp_rollout_api.set_metric_bucket(
    p_metric_bucket_id pg_catalog.text,
    p_environment_id pg_catalog.text,
    p_deployment_id pg_catalog.text,
    p_stage pg_catalog.text,
    p_config_fingerprint pg_catalog.text,
    p_metric_name pg_catalog.text,
    p_bucket_started_at pg_catalog.timestamptz,
    p_bucket_ended_at pg_catalog.timestamptz,
    p_execution_path pg_catalog.text,
    p_routing_mode pg_catalog.text,
    p_transport pg_catalog.text,
    p_protocol_version pg_catalog.text,
    p_adapter pg_catalog.text,
    p_result_category pg_catalog.text,
    p_error_category pg_catalog.text,
    p_call_kind pg_catalog.text,
    p_red_line pg_catalog.text,
    p_latency_bucket pg_catalog.text,
    p_value pg_catalog.int8,
    p_recorded_at pg_catalog.timestamptz
) RETURNS public.mcp_rollout_metric_bucket
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    saved public.mcp_rollout_metric_bucket;
BEGIN
    IF p_bucket_started_at IS NULL
       OR p_bucket_ended_at IS NULL
       OR pg_catalog.date_trunc(
            'minute', p_bucket_started_at AT TIME ZONE 'UTC'
       ) <> p_bucket_started_at AT TIME ZONE 'UTC'
       OR p_bucket_ended_at <> p_bucket_started_at + INTERVAL '1 minute' THEN
        RAISE EXCEPTION 'metric bucket must be one complete UTC-aligned minute';
    END IF;
    IF p_metric_name = 'mcp_safety_red_line_total' THEN
        RAISE EXCEPTION 'safety red-line metric is additive-counter-only';
    END IF;
    IF p_value IS NULL OR p_value < 0 THEN
        RAISE EXCEPTION 'gauge metric value must be non-negative';
    END IF;
    INSERT INTO public.mcp_rollout_metric_bucket (
        metric_bucket_id, environment_id, rollout_program, deployment_id, stage,
        config_fingerprint, metric_name, bucket_started_at, bucket_ended_at,
        execution_path, routing_mode, transport, protocol_version, adapter,
        result_category, error_category, call_kind, red_line, latency_bucket,
        value, created_at, updated_at
    ) VALUES (
        p_metric_bucket_id, p_environment_id, 'user_mcp_phase3', p_deployment_id, p_stage,
        p_config_fingerprint, p_metric_name, p_bucket_started_at, p_bucket_ended_at,
        p_execution_path, p_routing_mode, p_transport, p_protocol_version, p_adapter,
        p_result_category, p_error_category, p_call_kind, p_red_line, p_latency_bucket,
        p_value, p_recorded_at, p_recorded_at
    )
    ON CONFLICT ON CONSTRAINT uq_mcp_rollout_metric_series_bucket DO UPDATE
    SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
    RETURNING * INTO saved;
    RETURN saved;
END;
$function$;

CREATE OR REPLACE FUNCTION mcp_rollout_api.append_shadow_audit_sample(
    p_sample_id pg_catalog.text,
    p_environment_id pg_catalog.text,
    p_deployment_id pg_catalog.text,
    p_config_fingerprint pg_catalog.text,
    p_manifest_fingerprint pg_catalog.text,
    p_fixture_fingerprint pg_catalog.text,
    p_mapping_fingerprint pg_catalog.text,
    p_scenario pg_catalog.text,
    p_nonce pg_catalog.text,
    p_safe_owner_ref pg_catalog.text,
    p_safe_task_ref pg_catalog.text,
    p_safe_call_ref pg_catalog.text,
    p_legacy_outcome pg_catalog.text,
    p_shadow_outcome pg_catalog.text,
    p_transport pg_catalog.text,
    p_endpoint_policy pg_catalog.text,
    p_comparison pg_catalog.text,
    p_blockers pg_catalog.jsonb,
    p_payload_digest pg_catalog.text,
    p_observed_at pg_catalog.timestamptz,
    p_recorded_at pg_catalog.timestamptz,
    p_expires_at pg_catalog.timestamptz
) RETURNS public.mcp_shadow_audit_sample
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    existing public.mcp_shadow_audit_sample;
    saved public.mcp_shadow_audit_sample;
    expected_digest pg_catalog.text;
BEGIN
    SELECT * INTO existing
    FROM public.mcp_shadow_audit_sample
    WHERE sample_id = p_sample_id;
    IF existing.sample_id IS NOT NULL THEN
        IF ROW(
            existing.environment_id, existing.rollout_program, existing.deployment_id,
            existing.stage, existing.config_fingerprint, existing.manifest_fingerprint,
            existing.fixture_fingerprint, existing.mapping_fingerprint, existing.scenario,
            existing.nonce, existing.safe_owner_ref, existing.safe_task_ref,
            existing.safe_call_ref, existing.legacy_outcome, existing.shadow_outcome,
            existing.transport, existing.endpoint_policy, existing.comparison,
            existing.blockers, existing.payload_digest, existing.observed_at,
            existing.recorded_at, existing.expires_at
        ) IS DISTINCT FROM ROW(
            p_environment_id, 'user_mcp_phase3', p_deployment_id, 'internal_shadow',
            p_config_fingerprint, p_manifest_fingerprint, p_fixture_fingerprint,
            p_mapping_fingerprint, p_scenario, p_nonce, p_safe_owner_ref,
            p_safe_task_ref, p_safe_call_ref, p_legacy_outcome, p_shadow_outcome,
            p_transport, p_endpoint_policy, p_comparison, p_blockers,
            p_payload_digest, p_observed_at, p_recorded_at, p_expires_at
        ) THEN
            RAISE EXCEPTION 'MCP shadow audit sample ID payload conflict';
        END IF;
        RETURN existing;
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.mcp_shadow_audit_sample
        WHERE environment_id = p_environment_id
          AND deployment_id = p_deployment_id
          AND stage = 'internal_shadow'
          AND config_fingerprint = p_config_fingerprint
          AND nonce = p_nonce
    ) THEN
        RAISE EXCEPTION 'MCP shadow audit sample nonce replay';
    END IF;
    expected_digest := pg_catalog.encode(
        pg_catalog.sha256(
            pg_catalog.convert_to(
                mcp_rollout_api.canonical_jsonb_text(pg_catalog.jsonb_build_object(
                    'sample_id', p_sample_id,
                    'environment_id', p_environment_id,
                    'deployment_id', p_deployment_id,
                    'stage', 'internal_shadow',
                    'config_fingerprint', p_config_fingerprint,
                    'manifest_fingerprint', p_manifest_fingerprint,
                    'fixture_fingerprint', p_fixture_fingerprint,
                    'mapping_fingerprint', p_mapping_fingerprint,
                    'scenario', p_scenario,
                    'nonce', p_nonce,
                    'legacy_outcome', p_legacy_outcome,
                    'shadow_outcome', p_shadow_outcome,
                    'transport', p_transport,
                    'endpoint_policy', p_endpoint_policy,
                    'comparison', p_comparison,
                    'blockers', p_blockers,
                    'observed_at', mcp_rollout_api.canonical_timestamp(p_observed_at),
                    'recorded_at', mcp_rollout_api.canonical_timestamp(p_recorded_at),
                    'expires_at', mcp_rollout_api.canonical_timestamp(p_expires_at),
                    'safe_owner_ref', p_safe_owner_ref,
                    'safe_task_ref', p_safe_task_ref,
                    'safe_call_ref', p_safe_call_ref,
                    'rollout_program', 'user_mcp_phase3'
                )),
                'UTF8'
            )
        ),
        'hex'
    );
    IF p_sample_id IS NULL OR pg_catalog.btrim(p_sample_id) = ''
       OR p_environment_id IS NULL OR pg_catalog.btrim(p_environment_id) = ''
       OR p_deployment_id IS NULL OR pg_catalog.btrim(p_deployment_id) = ''
       OR p_config_fingerprint IS NULL
       OR p_manifest_fingerprint IS NULL
       OR p_fixture_fingerprint IS NULL
       OR p_mapping_fingerprint IS NULL
       OR p_scenario IS NULL OR pg_catalog.btrim(p_scenario) = ''
       OR p_nonce IS NULL OR pg_catalog.btrim(p_nonce) = ''
       OR p_legacy_outcome IS NULL OR pg_catalog.btrim(p_legacy_outcome) = ''
       OR p_shadow_outcome IS NULL OR pg_catalog.btrim(p_shadow_outcome) = ''
       OR p_transport IS NULL OR pg_catalog.btrim(p_transport) = ''
       OR p_endpoint_policy IS NULL OR pg_catalog.btrim(p_endpoint_policy) = ''
       OR p_comparison IS NULL OR pg_catalog.btrim(p_comparison) = ''
       OR p_blockers IS NULL
       OR p_payload_digest IS NULL
       OR p_observed_at IS NULL
       OR p_recorded_at IS NULL
       OR p_expires_at IS NULL
       OR p_payload_digest IS DISTINCT FROM expected_digest
       OR p_config_fingerprint !~ '^[0-9a-f]{64}$'
       OR p_manifest_fingerprint !~ '^[0-9a-f]{64}$'
       OR p_fixture_fingerprint !~ '^[0-9a-f]{64}$'
       OR p_mapping_fingerprint !~ '^[0-9a-f]{64}$'
       OR (p_safe_owner_ref IS NOT NULL AND p_safe_owner_ref !~ '^hmac-sha256:[0-9a-f]{64}$')
       OR (p_safe_task_ref IS NOT NULL AND p_safe_task_ref !~ '^hmac-sha256:[0-9a-f]{64}$')
       OR (p_safe_call_ref IS NOT NULL AND p_safe_call_ref !~ '^hmac-sha256:[0-9a-f]{64}$')
       OR p_scenario NOT IN (
           'https_streamable_success', 'https_legacy_sse_success',
           'public_http_legacy_sse_success',
           'allowlisted_http_legacy_sse_success', 'authentication_failure',
           'timeout', 'permission_denial', 'large_output'
       )
       OR p_legacy_outcome NOT IN (
           'tool_call_succeeded', 'tool_call_succeeded_large_result',
           'control_plane_ready', 'authentication_failed', 'timeout',
           'permission_denied_suppressed', 'observer_failed', 'cleanup_failed'
       )
       OR p_shadow_outcome NOT IN (
           'tool_call_succeeded', 'tool_call_succeeded_large_result',
           'control_plane_ready', 'authentication_failed', 'timeout',
           'permission_denied_suppressed', 'observer_failed', 'cleanup_failed'
       )
       OR p_transport NOT IN ('streamable_http', 'legacy_http_sse')
       OR p_endpoint_policy NOT IN (
           'allowed', 'allowed_by_enterprise_allowlist', 'runtime_enforced'
       )
       OR p_comparison NOT IN ('matched', 'mismatched', 'not_comparable', 'excluded')
       OR (
           p_comparison = 'matched'
           AND NOT EXISTS (
               SELECT 1
               FROM (VALUES
                   (
                       'https_streamable_success', 'tool_call_succeeded',
                       'control_plane_ready', 'streamable_http', 'runtime_enforced'
                   ),
                   (
                       'https_legacy_sse_success', 'tool_call_succeeded',
                       'control_plane_ready', 'legacy_http_sse', 'runtime_enforced'
                   ),
                   (
                       'public_http_legacy_sse_success', 'tool_call_succeeded',
                       'control_plane_ready', 'legacy_http_sse',
                       'runtime_enforced'
                   ),
                   (
                       'allowlisted_http_legacy_sse_success', 'tool_call_succeeded',
                       'control_plane_ready', 'legacy_http_sse',
                       'allowed_by_enterprise_allowlist'
                   ),
                   (
                       'authentication_failure', 'authentication_failed',
                       'authentication_failed', 'streamable_http', 'runtime_enforced'
                   ),
                   (
                       'timeout', 'timeout', 'timeout',
                       'streamable_http', 'runtime_enforced'
                   ),
                   (
                       'permission_denial', 'tool_call_succeeded',
                       'permission_denied_suppressed',
                       'streamable_http', 'runtime_enforced'
                   ),
                   (
                       'large_output', 'tool_call_succeeded_large_result',
                       'control_plane_ready', 'streamable_http', 'runtime_enforced'
                   )
               ) AS expected(
                   scenario, legacy_outcome, shadow_outcome,
                   transport, endpoint_policy
               )
               WHERE ROW(
                   expected.scenario, expected.legacy_outcome,
                   expected.shadow_outcome, expected.transport,
                   expected.endpoint_policy
               ) = ROW(
                   p_scenario, p_legacy_outcome, p_shadow_outcome,
                   p_transport, p_endpoint_policy
               )
           )
       )
       OR p_recorded_at < p_observed_at
       OR p_expires_at <> p_recorded_at + INTERVAL '30 days'
       OR pg_catalog.jsonb_typeof(p_blockers) <> 'array'
       OR EXISTS (
           SELECT 1
           FROM pg_catalog.jsonb_array_elements(p_blockers) AS blocker(value)
           WHERE pg_catalog.jsonb_typeof(blocker.value) <> 'string'
              OR blocker.value #>> '{}' NOT IN (
                  'approved_verified_retire', 'audit_incomplete',
                  'catalog_count_mismatch', 'catalog_names_hmac_mismatch',
                  'cleanup_incomplete', 'config_fingerprint_mismatch',
                  'digest_invalid', 'endpoint_policy_allowed_mismatch',
                  'endpoint_policy_mismatch', 'fixture_fingerprint_mismatch',
                  'grant_check_mismatch', 'legacy_cleanup_incomplete',
                  'legacy_outcome_mismatch', 'legacy_route_mapping_mismatch',
                  'manifest_fingerprint_mismatch',
                  'mapping_config_fingerprint_mismatch',
                  'mapping_set_fingerprint_mismatch',
                  'ownership_verified_mismatch', 'sample_nonce_missing',
                  'sample_not_terminal', 'sample_outside_window',
                  'schema_fingerprints_mismatch', 'schema_valid_mismatch',
                  'selected_tool_hmac_mismatch', 'shadow_outcome_mismatch',
                  'shadow_outcome_not_ready', 'shadow_route_mapping_mismatch',
                  'timeout_checkpoint_mismatch', 'transport_mismatch',
                  'verified_mapping_ambiguous',
                  'verified_mapping_input_invalid', 'verified_mapping_invalid',
                  'verified_mapping_missing',
                  'verified_mapping_not_in_approved_set'
              )
       )
       OR (
           SELECT pg_catalog.count(*)
           FROM pg_catalog.jsonb_array_elements_text(p_blockers) AS blocker(value)
       ) <> (
           SELECT pg_catalog.count(DISTINCT blocker.value)
           FROM pg_catalog.jsonb_array_elements_text(p_blockers) AS blocker(value)
       )
       OR NOT (CASE p_comparison
           WHEN 'matched' THEN p_blockers = '[]'::pg_catalog.jsonb
           WHEN 'mismatched' THEN
               pg_catalog.jsonb_array_length(p_blockers) > 0
               AND NOT (p_blockers ?| ARRAY[
                   'approved_verified_retire',
                   'sample_not_terminal',
                   'sample_outside_window'
               ]::pg_catalog.text[])
               AND p_blockers <> '["verified_mapping_missing"]'::pg_catalog.jsonb
           WHEN 'not_comparable' THEN p_blockers IN (
               '["approved_verified_retire"]'::pg_catalog.jsonb,
               '["verified_mapping_missing"]'::pg_catalog.jsonb
           )
           WHEN 'excluded' THEN
               pg_catalog.jsonb_array_length(p_blockers) > 0
               AND NOT EXISTS (
                   SELECT 1
                   FROM pg_catalog.jsonb_array_elements_text(p_blockers) AS blocker(value)
                   WHERE blocker.value NOT IN (
                       'sample_not_terminal', 'sample_outside_window'
                   )
               )
           ELSE FALSE
       END) THEN
        RAISE EXCEPTION 'MCP shadow audit sample is invalid';
    END IF;
    INSERT INTO public.mcp_shadow_audit_sample (
        sample_id, environment_id, rollout_program, deployment_id, stage,
        config_fingerprint, manifest_fingerprint, fixture_fingerprint,
        mapping_fingerprint, scenario, nonce, safe_owner_ref, safe_task_ref,
        safe_call_ref, legacy_outcome, shadow_outcome, transport, endpoint_policy,
        comparison, blockers, payload_digest, observed_at, recorded_at, expires_at
    ) VALUES (
        p_sample_id, p_environment_id, 'user_mcp_phase3', p_deployment_id,
        'internal_shadow', p_config_fingerprint, p_manifest_fingerprint,
        p_fixture_fingerprint, p_mapping_fingerprint, p_scenario, p_nonce,
        p_safe_owner_ref, p_safe_task_ref, p_safe_call_ref, p_legacy_outcome,
        p_shadow_outcome, p_transport, p_endpoint_policy, p_comparison,
        p_blockers, p_payload_digest, p_observed_at, p_recorded_at, p_expires_at
    ) RETURNING * INTO saved;
    RETURN saved;
END;
$function$;

CREATE OR REPLACE FUNCTION mcp_rollout_api.delete_expired_shadow_audit_samples(
    p_now pg_catalog.timestamptz,
    p_limit pg_catalog.int4
) RETURNS pg_catalog.int4
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    deleted_count pg_catalog.int4;
BEGIN
    IF p_limit <= 0 THEN
        RAISE EXCEPTION 'MCP shadow audit cleanup limit must be positive';
    END IF;
    WITH candidates AS (
        SELECT sample_id
        FROM public.mcp_shadow_audit_sample
        WHERE expires_at <= p_now
        ORDER BY expires_at, sample_id
        LIMIT p_limit
        FOR UPDATE SKIP LOCKED
    ), deleted AS (
        DELETE FROM public.mcp_shadow_audit_sample AS sample
        USING candidates
        WHERE sample.sample_id = candidates.sample_id
        RETURNING 1
    )
    SELECT pg_catalog.count(*)::pg_catalog.int4 INTO deleted_count FROM deleted;
    RETURN deleted_count;
END;
$function$;

CREATE OR REPLACE FUNCTION mcp_rollout_api.upsert_instance_config_lease(
    p_instance_config_id pg_catalog.text,
    p_environment_id pg_catalog.text,
    p_deployment_id pg_catalog.text,
    p_instance_id pg_catalog.text,
    p_stage pg_catalog.text,
    p_config_fingerprint pg_catalog.text,
    p_activation_id pg_catalog.text,
    p_lease_expires_at pg_catalog.timestamptz,
    p_recorded_at pg_catalog.timestamptz
) RETURNS public.mcp_rollout_instance_config
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    saved public.mcp_rollout_instance_config;
BEGIN
    PERFORM mcp_rollout_api.lock_gate_scope(p_environment_id, p_recorded_at);

    IF EXISTS (
        SELECT 1
        FROM public.mcp_rollout_instance_config
        WHERE environment_id = p_environment_id
          AND rollout_program = 'user_mcp_phase3'
          AND deployment_id = p_deployment_id
          AND (stage, config_fingerprint, activation_id)
              IS DISTINCT FROM (p_stage, p_config_fingerprint, p_activation_id)
    ) THEN
        RAISE EXCEPTION 'rollout deployment config fingerprint mismatch';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM public.mcp_rollout_deployment_activation
        WHERE activation_id = p_activation_id
          AND environment_id = p_environment_id
          AND rollout_program = 'user_mcp_phase3'
          AND deployment_id = p_deployment_id
          AND stage = p_stage
          AND config_fingerprint = p_config_fingerprint
    ) THEN
        RAISE EXCEPTION 'rollout instance config activation does not match';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.mcp_rollout_deployment_activation AS activation
        WHERE activation.activation_id = p_activation_id
          AND NOT activation.is_rollback
    ) AND EXISTS (
        SELECT 1
        FROM public.mcp_rollout_promotion_block AS block
        WHERE block.environment_id = p_environment_id
          AND block.rollout_program = 'user_mcp_phase3'
          AND NOT EXISTS (
              SELECT 1 FROM public.mcp_rollout_block_resolution AS resolution
              WHERE resolution.block_id = block.block_id
          )
    ) THEN
        RAISE EXCEPTION 'rollout instance admission is blocked';
    END IF;

    INSERT INTO public.mcp_rollout_instance_config (
        instance_config_id, environment_id, rollout_program, deployment_id,
        instance_id, stage, config_fingerprint, activation_id, lease_expires_at,
        created_at, updated_at
    ) VALUES (
        p_instance_config_id, p_environment_id, 'user_mcp_phase3', p_deployment_id,
        p_instance_id, p_stage, p_config_fingerprint, p_activation_id, p_lease_expires_at,
        p_recorded_at, p_recorded_at
    )
    ON CONFLICT ON CONSTRAINT uq_mcp_rollout_instance_deployment DO UPDATE
    SET instance_config_id = EXCLUDED.instance_config_id,
        stage = EXCLUDED.stage,
        config_fingerprint = EXCLUDED.config_fingerprint,
        activation_id = EXCLUDED.activation_id,
        lease_expires_at = EXCLUDED.lease_expires_at,
        updated_at = EXCLUDED.updated_at
    RETURNING * INTO saved;
    RETURN saved;
END;
$function$;

CREATE OR REPLACE FUNCTION mcp_rollout_api.append_production_evidence_snapshot(
    p_evidence_id pg_catalog.text,
    p_environment_id pg_catalog.text,
    p_git_sha pg_catalog.text,
    p_deployment_id pg_catalog.text,
    p_stage pg_catalog.text,
    p_config_fingerprint pg_catalog.text,
    p_window_started_at pg_catalog.timestamptz,
    p_window_ended_at pg_catalog.timestamptz,
    p_recorded_at pg_catalog.timestamptz,
    p_snapshot_id pg_catalog.int8,
    p_nonce pg_catalog.text,
    p_evidence_kind pg_catalog.text,
    p_payload pg_catalog.jsonb,
    p_payload_digest pg_catalog.text,
    p_attestation_key_id pg_catalog.text,
    p_attestation_signature pg_catalog.text
) RETURNS public.mcp_rollout_evidence_snapshot
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
BEGIN
    RAISE EXCEPTION 'caller-authored production evidence is disabled';
END;
$function$;

CREATE OR REPLACE FUNCTION mcp_rollout_api.derive_production_evidence_snapshot(
    p_environment_id pg_catalog.text,
    p_deployment_id pg_catalog.text,
    p_git_sha pg_catalog.text,
    p_window_started_at pg_catalog.timestamptz,
    p_window_ended_at pg_catalog.timestamptz
) RETURNS TABLE (
    evidence_id pg_catalog.text,
    environment_id pg_catalog.text,
    rollout_program pg_catalog.text,
    git_sha pg_catalog.text,
    deployment_id pg_catalog.text,
    stage pg_catalog.text,
    config_fingerprint pg_catalog.text,
    window_started_at pg_catalog.timestamptz,
    window_ended_at pg_catalog.timestamptz,
    recorded_at pg_catalog.timestamptz,
    producer pg_catalog.text,
    source pg_catalog.text,
    snapshot_id pg_catalog.int8,
    nonce pg_catalog.text,
    evidence_kind pg_catalog.text,
    payload pg_catalog.jsonb,
    payload_digest pg_catalog.text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    v_recorded_at pg_catalog.timestamptz := p_window_ended_at;
    v_activation public.mcp_rollout_deployment_activation;
    v_stage pg_catalog.text;
    v_snapshot_id pg_catalog.int8;
    v_nonce pg_catalog.text;
    v_evidence_id pg_catalog.text;
    v_config_fingerprint pg_catalog.text;
    v_manifest_fingerprint pg_catalog.text;
    v_fixture_fingerprint pg_catalog.text;
    v_mapping_fingerprint pg_catalog.text;
    v_sample_count pg_catalog.int8;
    v_metric_count pg_catalog.int8;
    v_missing_bucket_count pg_catalog.int8;
    v_metrics pg_catalog.jsonb;
    v_call_kinds pg_catalog.jsonb;
    v_scenarios pg_catalog.jsonb;
    v_completed_drills pg_catalog.jsonb;
    v_red_lines pg_catalog.jsonb;
    v_payload pg_catalog.jsonb;
    v_content pg_catalog.jsonb;
    v_payload_digest pg_catalog.text;
    v_previous public.mcp_rollout_evidence_snapshot;
BEGIN
    IF p_environment_id IS NULL OR pg_catalog.btrim(p_environment_id) = ''
       OR p_deployment_id IS NULL OR pg_catalog.btrim(p_deployment_id) = ''
       OR p_window_started_at IS NULL OR p_window_ended_at IS NULL
       OR p_window_ended_at <= p_window_started_at THEN
        RAISE EXCEPTION 'production evidence window is invalid';
    END IF;
    IF p_git_sha IS NULL
       OR p_git_sha !~ '^[0-9a-fA-F]{40}([0-9a-fA-F]{24})?$' THEN
        RAISE EXCEPTION 'production evidence git SHA is invalid';
    END IF;
    PERFORM mcp_rollout_api.lock_gate_scope(p_environment_id, v_recorded_at);

    SELECT * INTO v_activation
    FROM public.mcp_rollout_deployment_activation AS activation
    WHERE activation.environment_id = p_environment_id
      AND activation.rollout_program = 'user_mcp_phase3'
      AND activation.deployment_id = p_deployment_id
    ORDER BY activation.created_at DESC, activation.activation_id DESC
    LIMIT 1;
    IF v_activation.activation_id IS NULL
       OR v_activation.stage NOT IN (
           'internal_shadow', 'internal_enforce', 'cohort_enforce',
           'full_enforce', 'legacy_assembly_off'
       ) THEN
        RAISE EXCEPTION 'production evidence has no exact active deployment';
    END IF;
    v_stage := v_activation.stage;
    v_config_fingerprint := v_activation.config_fingerprint;
    IF v_config_fingerprint !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'production evidence activation fingerprint is invalid';
    END IF;

    IF v_stage = 'internal_shadow' THEN
        SELECT
            pg_catalog.count(*),
            pg_catalog.min(sample.manifest_fingerprint),
            pg_catalog.min(sample.fixture_fingerprint),
            pg_catalog.min(sample.mapping_fingerprint)
        INTO
            v_sample_count,
            v_manifest_fingerprint,
            v_fixture_fingerprint,
            v_mapping_fingerprint
        FROM public.mcp_shadow_audit_sample AS sample
        WHERE sample.environment_id = p_environment_id
          AND sample.rollout_program = 'user_mcp_phase3'
          AND sample.deployment_id = p_deployment_id
          AND sample.stage = 'internal_shadow'
          AND sample.config_fingerprint = v_config_fingerprint
          AND sample.observed_at >= p_window_started_at
          AND sample.observed_at < p_window_ended_at
          AND sample.expires_at > p_window_ended_at;
        IF v_sample_count = 0 OR EXISTS (
            SELECT 1
            FROM public.mcp_shadow_audit_sample AS sample
            WHERE sample.environment_id = p_environment_id
              AND sample.rollout_program = 'user_mcp_phase3'
              AND sample.deployment_id = p_deployment_id
              AND sample.stage = 'internal_shadow'
              AND sample.observed_at >= p_window_started_at
              AND sample.observed_at < p_window_ended_at
              AND sample.expires_at > p_window_ended_at
              AND (
                  sample.config_fingerprint <> v_config_fingerprint
                  OR sample.manifest_fingerprint <> v_manifest_fingerprint
                  OR sample.fixture_fingerprint <> v_fixture_fingerprint
                  OR sample.mapping_fingerprint <> v_mapping_fingerprint
              )
        ) THEN
            RAISE EXCEPTION 'production evidence sample scope is incomplete or inconsistent';
        END IF;
        IF v_manifest_fingerprint !~ '^[0-9a-f]{64}$'
           OR v_fixture_fingerprint !~ '^[0-9a-f]{64}$'
           OR v_mapping_fingerprint !~ '^[0-9a-f]{64}$' THEN
            RAISE EXCEPTION 'production evidence fingerprint is invalid';
        END IF;
    ELSE
        v_sample_count := 0;
        v_manifest_fingerprint := NULL;
        v_fixture_fingerprint := NULL;
        v_mapping_fingerprint := NULL;
    END IF;

    IF EXISTS (
        SELECT 1 FROM public.mcp_rollout_metric_bucket AS metric
        WHERE metric.environment_id = p_environment_id
          AND metric.rollout_program = 'user_mcp_phase3'
          AND metric.deployment_id = p_deployment_id
          AND metric.stage = v_stage
          AND metric.bucket_started_at >= p_window_started_at
          AND metric.bucket_ended_at <= p_window_ended_at
          AND metric.config_fingerprint <> v_config_fingerprint
    ) THEN
        RAISE EXCEPTION 'production evidence metric config fingerprint is inconsistent';
    END IF;

    SELECT pg_catalog.count(*), COALESCE(
        pg_catalog.jsonb_agg(
            pg_catalog.jsonb_build_object(
                'metric_name', metric.metric_name,
                'bucket_started_at', mcp_rollout_api.canonical_timestamp(metric.bucket_started_at),
                'bucket_ended_at', mcp_rollout_api.canonical_timestamp(metric.bucket_ended_at),
                'labels', pg_catalog.jsonb_build_object(
                    'execution_path', metric.execution_path,
                    'routing_mode', metric.routing_mode,
                    'transport', metric.transport,
                    'protocol_version', metric.protocol_version,
                    'adapter', metric.adapter,
                    'result_category', metric.result_category,
                    'error_category', metric.error_category,
                    'call_kind', CASE WHEN metric.call_kind = 'not_applicable' THEN NULL ELSE metric.call_kind END,
                    'red_line', CASE WHEN metric.red_line = 'not_applicable' THEN NULL ELSE metric.red_line END
                ),
                'value', metric.value,
                'latency_bucket', metric.latency_bucket
            ) ORDER BY metric.bucket_started_at, metric.metric_name, metric.metric_bucket_id
        ),
        '[]'::pg_catalog.jsonb
    )
    INTO v_metric_count, v_metrics
    FROM public.mcp_rollout_metric_bucket AS metric
    WHERE metric.environment_id = p_environment_id
      AND metric.rollout_program = 'user_mcp_phase3'
      AND metric.deployment_id = p_deployment_id
      AND metric.stage = v_stage
      AND metric.config_fingerprint = v_config_fingerprint
      AND metric.bucket_started_at >= p_window_started_at
      AND metric.bucket_ended_at <= p_window_ended_at;

    WITH required(kind, key1, key2) AS (
        SELECT 'red_line', red_line, NULL::pg_catalog.text
        FROM (VALUES
            ('cross_user_access'), ('secret_exposure'), ('dual_tool_call'),
            ('unauthorized_tool_call'), ('endpoint_policy_bypass'),
            ('unknown_result_replay'), ('shadow_tool_call'),
            ('persistent_resource_leak')
        ) AS red_lines(red_line)
        UNION ALL
        SELECT 'terminal', call_kind, result_category
        FROM (
            SELECT call_kind
            FROM (VALUES ('ordinary'), ('remote_task')) AS fixed(call_kind)
            WHERE v_stage IN (
                'cohort_enforce', 'full_enforce', 'legacy_assembly_off'
            )
            UNION
            SELECT DISTINCT metric.call_kind
            FROM public.mcp_rollout_metric_bucket AS metric
            WHERE v_stage = 'internal_enforce'
              AND metric.environment_id = p_environment_id
              AND metric.rollout_program = 'user_mcp_phase3'
              AND metric.deployment_id = p_deployment_id
              AND metric.stage = v_stage
              AND metric.config_fingerprint = v_config_fingerprint
              AND metric.metric_name = 'mcp_tool_calls_total'
              AND metric.execution_path = 'user_scoped'
              AND metric.call_kind IN ('ordinary', 'remote_task')
              AND metric.bucket_started_at >= p_window_started_at
              AND metric.bucket_ended_at <= p_window_ended_at
        ) AS kinds
        CROSS JOIN (VALUES
            ('succeeded'), ('failed'), ('unknown'), ('cancelled')
        ) AS results(result_category)
    ), coverage AS (
        SELECT required.*,
            COALESCE((
                SELECT pg_catalog.range_agg(
                    pg_catalog.tstzrange(
                        metric.bucket_started_at,
                        metric.bucket_ended_at,
                        '[)'
                    )
                ) @> pg_catalog.tstzrange(
                    p_window_started_at, p_window_ended_at, '[)'
                )
                FROM public.mcp_rollout_metric_bucket AS metric
                WHERE metric.environment_id = p_environment_id
                  AND metric.rollout_program = 'user_mcp_phase3'
                  AND metric.deployment_id = p_deployment_id
                  AND metric.stage = v_stage
                  AND metric.config_fingerprint = v_config_fingerprint
                  AND metric.bucket_started_at >= p_window_started_at
                  AND metric.bucket_ended_at <= p_window_ended_at
                  AND (
                      (required.kind = 'red_line'
                       AND metric.metric_name = 'mcp_safety_red_line_total'
                       AND metric.red_line = required.key1)
                      OR
                      (required.kind = 'terminal'
                       AND metric.metric_name = 'mcp_tool_calls_total'
                       AND metric.execution_path = 'user_scoped'
                       AND metric.call_kind = required.key1
                       AND metric.result_category = required.key2)
                  )
            ), false) AS covered
        FROM required
    )
    SELECT pg_catalog.count(*) FILTER (WHERE NOT covered)
    INTO v_missing_bucket_count
    FROM coverage;

    WITH kind_list(call_kind) AS (
        SELECT call_kind
        FROM (VALUES ('ordinary'), ('remote_task')) AS fixed(call_kind)
        WHERE v_stage IN (
            'cohort_enforce', 'full_enforce', 'legacy_assembly_off'
        )
        UNION
        SELECT DISTINCT metric.call_kind
        FROM public.mcp_rollout_metric_bucket AS metric
        WHERE v_stage = 'internal_enforce'
          AND metric.environment_id = p_environment_id
          AND metric.rollout_program = 'user_mcp_phase3'
          AND metric.deployment_id = p_deployment_id
          AND metric.stage = v_stage
          AND metric.config_fingerprint = v_config_fingerprint
          AND metric.metric_name = 'mcp_tool_calls_total'
          AND metric.execution_path = 'user_scoped'
          AND metric.call_kind IN ('ordinary', 'remote_task')
          AND metric.bucket_started_at >= p_window_started_at
          AND metric.bucket_ended_at <= p_window_ended_at
    )
    SELECT COALESCE(pg_catalog.jsonb_agg(
        pg_catalog.jsonb_build_object(
            'call_kind', kind_list.call_kind,
            'terminal_success_count', COALESCE((
                SELECT pg_catalog.sum(metric.value)
                FROM public.mcp_rollout_metric_bucket AS metric
                WHERE metric.environment_id = p_environment_id
                  AND metric.rollout_program = 'user_mcp_phase3'
                  AND metric.deployment_id = p_deployment_id
                  AND metric.stage = v_stage
                  AND metric.config_fingerprint = v_config_fingerprint
                  AND metric.metric_name = 'mcp_tool_calls_total'
                  AND metric.execution_path = 'user_scoped'
                  AND metric.call_kind = kind_list.call_kind
                  AND metric.result_category = 'succeeded'
                  AND metric.bucket_started_at >= p_window_started_at
                  AND metric.bucket_ended_at <= p_window_ended_at
            ), 0),
            'terminal_error_count', COALESCE((
                SELECT pg_catalog.sum(metric.value)
                FROM public.mcp_rollout_metric_bucket AS metric
                WHERE metric.environment_id = p_environment_id
                  AND metric.rollout_program = 'user_mcp_phase3'
                  AND metric.deployment_id = p_deployment_id
                  AND metric.stage = v_stage
                  AND metric.config_fingerprint = v_config_fingerprint
                  AND metric.metric_name = 'mcp_tool_calls_total'
                  AND metric.execution_path = 'user_scoped'
                  AND metric.call_kind = kind_list.call_kind
                  AND metric.result_category IN ('failed', 'unknown')
                  AND metric.bucket_started_at >= p_window_started_at
                  AND metric.bucket_ended_at <= p_window_ended_at
            ), 0),
            'cancellation_count', COALESCE((
                SELECT pg_catalog.sum(metric.value)
                FROM public.mcp_rollout_metric_bucket AS metric
                WHERE metric.environment_id = p_environment_id
                  AND metric.rollout_program = 'user_mcp_phase3'
                  AND metric.deployment_id = p_deployment_id
                  AND metric.stage = v_stage
                  AND metric.config_fingerprint = v_config_fingerprint
                  AND metric.metric_name = 'mcp_tool_calls_total'
                  AND metric.execution_path = 'user_scoped'
                  AND metric.call_kind = kind_list.call_kind
                  AND metric.result_category = 'cancelled'
                  AND metric.bucket_started_at >= p_window_started_at
                  AND metric.bucket_ended_at <= p_window_ended_at
            ), 0),
            'p95_latency_ms', (
                SELECT percentile.milliseconds
                FROM (
                    SELECT latency.ordinality, latency.milliseconds,
                        pg_catalog.sum(metric.value) AS bucket_count,
                        pg_catalog.sum(pg_catalog.sum(metric.value)) OVER (
                            ORDER BY latency.ordinality
                        ) AS cumulative_count,
                        pg_catalog.sum(pg_catalog.sum(metric.value)) OVER () AS total_count
                    FROM public.mcp_rollout_metric_bucket AS metric
                    JOIN (VALUES
                        ('le_100_ms', 1, 100), ('le_500_ms', 2, 500),
                        ('le_1_s', 3, 1000), ('le_5_s', 4, 5000),
                        ('le_30_s', 5, 30000), ('le_120_s', 6, 120000),
                        ('gt_120_s', 7, NULL::pg_catalog.int4)
                    ) AS latency(name, ordinality, milliseconds)
                      ON latency.name = metric.latency_bucket
                    WHERE metric.environment_id = p_environment_id
                      AND metric.rollout_program = 'user_mcp_phase3'
                      AND metric.deployment_id = p_deployment_id
                      AND metric.stage = v_stage
                      AND metric.config_fingerprint = v_config_fingerprint
                      AND metric.metric_name = 'mcp_tool_call_duration_seconds'
                      AND metric.execution_path = 'user_scoped'
                      AND metric.call_kind = kind_list.call_kind
                      AND metric.bucket_started_at >= p_window_started_at
                      AND metric.bucket_ended_at <= p_window_ended_at
                    GROUP BY latency.ordinality, latency.milliseconds
                ) AS percentile
                WHERE percentile.total_count > 0
                  AND percentile.cumulative_count * 20 >= percentile.total_count * 19
                ORDER BY percentile.ordinality
                LIMIT 1
            ),
            'baseline_success_count', COALESCE((
                SELECT pg_catalog.sum(metric.value)
                FROM public.mcp_rollout_metric_bucket AS metric
                WHERE metric.environment_id = p_environment_id
                  AND metric.rollout_program = 'user_mcp_phase3'
                  AND metric.deployment_id = p_deployment_id
                  AND metric.stage = v_stage
                  AND metric.config_fingerprint = v_config_fingerprint
                  AND metric.metric_name = 'mcp_tool_calls_total'
                  AND metric.execution_path = 'legacy'
                  AND metric.call_kind = kind_list.call_kind
                  AND metric.result_category = 'succeeded'
                  AND metric.bucket_started_at >= p_window_started_at
                  AND metric.bucket_ended_at <= p_window_ended_at
            ), 0),
            'baseline_error_count', COALESCE((
                SELECT pg_catalog.sum(metric.value)
                FROM public.mcp_rollout_metric_bucket AS metric
                WHERE metric.environment_id = p_environment_id
                  AND metric.rollout_program = 'user_mcp_phase3'
                  AND metric.deployment_id = p_deployment_id
                  AND metric.stage = v_stage
                  AND metric.config_fingerprint = v_config_fingerprint
                  AND metric.metric_name = 'mcp_tool_calls_total'
                  AND metric.execution_path = 'legacy'
                  AND metric.call_kind = kind_list.call_kind
                  AND metric.result_category IN ('failed', 'unknown')
                  AND metric.bucket_started_at >= p_window_started_at
                  AND metric.bucket_ended_at <= p_window_ended_at
            ), 0),
            'baseline_p95_latency_ms', (
                SELECT percentile.milliseconds
                FROM (
                    SELECT latency.ordinality, latency.milliseconds,
                        pg_catalog.sum(pg_catalog.sum(metric.value)) OVER (
                            ORDER BY latency.ordinality
                        ) AS cumulative_count,
                        pg_catalog.sum(pg_catalog.sum(metric.value)) OVER () AS total_count
                    FROM public.mcp_rollout_metric_bucket AS metric
                    JOIN (VALUES
                        ('le_100_ms', 1, 100), ('le_500_ms', 2, 500),
                        ('le_1_s', 3, 1000), ('le_5_s', 4, 5000),
                        ('le_30_s', 5, 30000), ('le_120_s', 6, 120000),
                        ('gt_120_s', 7, NULL::pg_catalog.int4)
                    ) AS latency(name, ordinality, milliseconds)
                      ON latency.name = metric.latency_bucket
                    WHERE metric.environment_id = p_environment_id
                      AND metric.rollout_program = 'user_mcp_phase3'
                      AND metric.deployment_id = p_deployment_id
                      AND metric.stage = v_stage
                      AND metric.config_fingerprint = v_config_fingerprint
                      AND metric.metric_name = 'mcp_tool_call_duration_seconds'
                      AND metric.execution_path = 'legacy'
                      AND metric.call_kind = kind_list.call_kind
                      AND metric.bucket_started_at >= p_window_started_at
                      AND metric.bucket_ended_at <= p_window_ended_at
                    GROUP BY latency.ordinality, latency.milliseconds
                ) AS percentile
                WHERE percentile.total_count > 0
                  AND percentile.cumulative_count * 20 >= percentile.total_count * 19
                ORDER BY percentile.ordinality
                LIMIT 1
            )
        ) ORDER BY pg_catalog.array_position(
            ARRAY['ordinary', 'remote_task'], kind_list.call_kind
        )
    ), '[]'::pg_catalog.jsonb)
    INTO v_call_kinds
    FROM kind_list;

    WITH latest AS (
        SELECT DISTINCT ON (observation.drill)
            observation.drill, observation.outcome
        FROM public.mcp_rollout_drill_observation AS observation
        WHERE v_stage = 'internal_enforce'
          AND observation.environment_id = p_environment_id
          AND observation.rollout_program = 'user_mcp_phase3'
          AND observation.deployment_id = p_deployment_id
          AND observation.stage = v_stage
          AND observation.config_fingerprint = v_config_fingerprint
          AND observation.observed_at >= p_window_started_at
          AND observation.observed_at < p_window_ended_at
          AND observation.expires_at > p_window_ended_at
        ORDER BY observation.drill, observation.observed_at DESC,
            observation.recorded_at DESC, observation.drill_observation_id DESC
    )
    SELECT COALESCE(pg_catalog.jsonb_agg(
        latest.drill ORDER BY latest.drill
    ) FILTER (WHERE latest.outcome = 'passed'), '[]'::pg_catalog.jsonb)
    INTO v_completed_drills
    FROM latest;

    WITH scenario_list(ordinality, scenario) AS (VALUES
        (1, 'https_streamable_success'),
        (2, 'https_legacy_sse_success'),
        (3, 'public_http_legacy_sse_success'),
        (4, 'authentication_failure'),
        (5, 'timeout'),
        (6, 'permission_denial'),
        (7, 'large_output')
    )
    SELECT pg_catalog.jsonb_agg(
        pg_catalog.jsonb_build_object(
            'scenario', listed.scenario,
            'matched_count', (SELECT pg_catalog.count(*) FROM public.mcp_shadow_audit_sample AS sample WHERE v_stage = 'internal_shadow' AND sample.environment_id = p_environment_id AND sample.deployment_id = p_deployment_id AND sample.stage = v_stage AND sample.config_fingerprint = v_config_fingerprint AND sample.observed_at >= p_window_started_at AND sample.observed_at < p_window_ended_at AND sample.expires_at > p_window_ended_at AND sample.scenario = listed.scenario AND sample.comparison = 'matched'),
            'mismatched_count', (SELECT pg_catalog.count(*) FROM public.mcp_shadow_audit_sample AS sample WHERE v_stage = 'internal_shadow' AND sample.environment_id = p_environment_id AND sample.deployment_id = p_deployment_id AND sample.stage = v_stage AND sample.config_fingerprint = v_config_fingerprint AND sample.observed_at >= p_window_started_at AND sample.observed_at < p_window_ended_at AND sample.expires_at > p_window_ended_at AND sample.scenario = listed.scenario AND sample.comparison = 'mismatched'),
            'invalid_count', 0,
            'not_comparable_count', (SELECT pg_catalog.count(*) FROM public.mcp_shadow_audit_sample AS sample WHERE v_stage = 'internal_shadow' AND sample.environment_id = p_environment_id AND sample.deployment_id = p_deployment_id AND sample.stage = v_stage AND sample.config_fingerprint = v_config_fingerprint AND sample.observed_at >= p_window_started_at AND sample.observed_at < p_window_ended_at AND sample.expires_at > p_window_ended_at AND sample.scenario = listed.scenario AND sample.comparison = 'not_comparable'),
            'excluded_count', (SELECT pg_catalog.count(*) FROM public.mcp_shadow_audit_sample AS sample WHERE v_stage = 'internal_shadow' AND sample.environment_id = p_environment_id AND sample.deployment_id = p_deployment_id AND sample.stage = v_stage AND sample.config_fingerprint = v_config_fingerprint AND sample.observed_at >= p_window_started_at AND sample.observed_at < p_window_ended_at AND sample.expires_at > p_window_ended_at AND sample.scenario = listed.scenario AND sample.comparison = 'excluded')
        ) ORDER BY listed.ordinality
    ) INTO v_scenarios FROM scenario_list AS listed;

    WITH red_line_list(ordinality, red_line) AS (VALUES
        (1, 'cross_user_access'), (2, 'secret_exposure'),
        (3, 'dual_tool_call'), (4, 'unauthorized_tool_call'),
        (5, 'endpoint_policy_bypass'), (6, 'unknown_result_replay'),
        (7, 'shadow_tool_call'), (8, 'persistent_resource_leak')
    )
    SELECT pg_catalog.jsonb_agg(
        pg_catalog.jsonb_build_object(
            'red_line', listed.red_line,
            'count', COALESCE((
                SELECT pg_catalog.sum(metric.value)
                FROM public.mcp_rollout_metric_bucket AS metric
                WHERE metric.environment_id = p_environment_id
                  AND metric.deployment_id = p_deployment_id
                  AND metric.stage = v_stage
                  AND metric.config_fingerprint = v_config_fingerprint
                  AND metric.bucket_started_at >= p_window_started_at
                  AND metric.bucket_ended_at <= p_window_ended_at
                  AND metric.metric_name = 'mcp_safety_red_line_total'
                  AND metric.red_line = listed.red_line
            ), 0)
        ) ORDER BY listed.ordinality
    ) INTO v_red_lines FROM red_line_list AS listed;

    v_payload := pg_catalog.jsonb_build_object(
        'kind', v_stage,
        'metric_buckets', v_metrics,
        'call_kinds', v_call_kinds,
        'shadow_scenarios', CASE WHEN v_stage = 'internal_shadow' THEN v_scenarios ELSE '[]'::pg_catalog.jsonb END,
        'completed_drills', v_completed_drills,
        'red_line_counts', v_red_lines,
        'continuous_window', v_missing_bucket_count = 0,
        'missing_bucket_count', v_missing_bucket_count,
        'invalid_evidence_count', 0,
        'unresolved_mismatch_count', (SELECT pg_catalog.count(*) FROM public.mcp_shadow_audit_sample AS sample WHERE v_stage = 'internal_shadow' AND sample.environment_id = p_environment_id AND sample.deployment_id = p_deployment_id AND sample.stage = v_stage AND sample.config_fingerprint = v_config_fingerprint AND sample.observed_at >= p_window_started_at AND sample.observed_at < p_window_ended_at AND sample.expires_at > p_window_ended_at AND sample.comparison = 'mismatched'),
        'unapproved_not_comparable_count', (SELECT pg_catalog.count(*) FROM public.mcp_shadow_audit_sample AS sample WHERE v_stage = 'internal_shadow' AND sample.environment_id = p_environment_id AND sample.deployment_id = p_deployment_id AND sample.stage = v_stage AND sample.config_fingerprint = v_config_fingerprint AND sample.observed_at >= p_window_started_at AND sample.observed_at < p_window_ended_at AND sample.expires_at > p_window_ended_at AND sample.comparison = 'not_comparable' AND sample.blockers <> '["approved_verified_retire"]'::pg_catalog.jsonb),
        'shadow_observation_count', v_sample_count,
        'pre_dispatch_excluded_count', (SELECT pg_catalog.count(*) FROM public.mcp_shadow_audit_sample AS sample WHERE v_stage = 'internal_shadow' AND sample.environment_id = p_environment_id AND sample.deployment_id = p_deployment_id AND sample.stage = v_stage AND sample.config_fingerprint = v_config_fingerprint AND sample.observed_at >= p_window_started_at AND sample.observed_at < p_window_ended_at AND sample.expires_at > p_window_ended_at AND sample.comparison = 'excluded'),
        'ci_conformance_passed', false,
        'manifest_fingerprint', v_manifest_fingerprint,
        'fixture_fingerprint', v_fixture_fingerprint,
        'mapping_fingerprint', v_mapping_fingerprint
    );

    SELECT * INTO v_previous
    FROM public.mcp_rollout_evidence_snapshot AS evidence
    WHERE evidence.environment_id = p_environment_id
      AND evidence.rollout_program = 'user_mcp_phase3'
      AND evidence.deployment_id = p_deployment_id
      AND evidence.stage = v_stage
    ORDER BY evidence.snapshot_id DESC, evidence.recorded_at DESC
    LIMIT 1;
    IF v_previous.evidence_id IS NOT NULL THEN
        IF p_window_ended_at <= v_previous.window_ended_at
           OR p_window_started_at > v_previous.window_ended_at
           OR v_config_fingerprint <> v_previous.config_fingerprint THEN
            RAISE EXCEPTION 'production evidence window or config is non-monotonic';
        END IF;
        v_snapshot_id := v_previous.snapshot_id + 1;
    ELSE
        v_snapshot_id := 1;
    END IF;

    v_nonce := pg_catalog.encode(
        pg_catalog.sha256(
            pg_catalog.convert_to(
                mcp_rollout_api.canonical_jsonb_text(pg_catalog.jsonb_build_object(
                    'environment_id', p_environment_id,
                    'deployment_id', p_deployment_id,
                    'stage', v_stage,
                    'window_started_at', mcp_rollout_api.canonical_timestamp(p_window_started_at),
                    'window_ended_at', mcp_rollout_api.canonical_timestamp(p_window_ended_at),
                    'recorded_at', mcp_rollout_api.canonical_timestamp(v_recorded_at),
                    'snapshot_id', v_snapshot_id,
                    'config_fingerprint', v_config_fingerprint
                )),
                'UTF8'
            )
        ),
        'hex'
    );
    v_evidence_id := 'prod-' || pg_catalog.substring(v_nonce, 1, 59);
    v_content := pg_catalog.jsonb_build_object(
        'evidence_id', v_evidence_id,
        'environment_id', p_environment_id,
        'git_sha', p_git_sha,
        'deployment_id', p_deployment_id,
        'stage', v_stage,
        'config_fingerprint', v_config_fingerprint,
        'window_started_at', mcp_rollout_api.canonical_timestamp(p_window_started_at),
        'window_ended_at', mcp_rollout_api.canonical_timestamp(p_window_ended_at),
        'recorded_at', mcp_rollout_api.canonical_timestamp(v_recorded_at),
        'producer', 'production_snapshot_producer',
        'source', 'production',
        'snapshot_id', v_snapshot_id,
        'nonce', v_nonce,
        'payload', v_payload
    );
    v_payload_digest := pg_catalog.encode(
        pg_catalog.sha256(
            pg_catalog.convert_to(
                mcp_rollout_api.canonical_jsonb_text(v_content), 'UTF8'
            )
        ),
        'hex'
    );
    RETURN QUERY SELECT
        v_evidence_id, p_environment_id, 'user_mcp_phase3', p_git_sha,
        p_deployment_id, v_stage, v_config_fingerprint,
        p_window_started_at, p_window_ended_at, v_recorded_at,
        'production_snapshot_producer', 'production', v_snapshot_id, v_nonce,
        v_stage, v_payload, v_payload_digest;
END;
$function$;

CREATE OR REPLACE FUNCTION mcp_rollout_api.prepare_production_evidence_snapshot(
    p_environment_id pg_catalog.text,
    p_deployment_id pg_catalog.text,
    p_git_sha pg_catalog.text,
    p_window_started_at pg_catalog.timestamptz,
    p_window_ended_at pg_catalog.timestamptz
) RETURNS TABLE (
    evidence_id pg_catalog.text,
    environment_id pg_catalog.text,
    rollout_program pg_catalog.text,
    git_sha pg_catalog.text,
    deployment_id pg_catalog.text,
    stage pg_catalog.text,
    config_fingerprint pg_catalog.text,
    window_started_at pg_catalog.timestamptz,
    window_ended_at pg_catalog.timestamptz,
    recorded_at pg_catalog.timestamptz,
    producer pg_catalog.text,
    source pg_catalog.text,
    snapshot_id pg_catalog.int8,
    nonce pg_catalog.text,
    evidence_kind pg_catalog.text,
    payload pg_catalog.jsonb,
    payload_digest pg_catalog.text
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
    SELECT * FROM mcp_rollout_api.derive_production_evidence_snapshot(
        p_environment_id, p_deployment_id, p_git_sha,
        p_window_started_at, p_window_ended_at
    );
$function$;

CREATE OR REPLACE FUNCTION mcp_rollout_api.finalize_production_evidence_snapshot(
    p_environment_id pg_catalog.text,
    p_deployment_id pg_catalog.text,
    p_git_sha pg_catalog.text,
    p_window_started_at pg_catalog.timestamptz,
    p_window_ended_at pg_catalog.timestamptz,
    p_expected_evidence_id pg_catalog.text,
    p_expected_payload_digest pg_catalog.text,
    p_attestation_key_id pg_catalog.text,
    p_attestation_signature pg_catalog.text
) RETURNS public.mcp_rollout_evidence_snapshot
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    materialized record;
    saved public.mcp_rollout_evidence_snapshot;
BEGIN
    SELECT * INTO materialized
    FROM mcp_rollout_api.derive_production_evidence_snapshot(
        p_environment_id, p_deployment_id, p_git_sha,
        p_window_started_at, p_window_ended_at
    );
    IF materialized.evidence_id IS DISTINCT FROM p_expected_evidence_id
       OR materialized.payload_digest IS DISTINCT FROM p_expected_payload_digest THEN
        RAISE EXCEPTION 'production evidence materialization changed';
    END IF;
    IF p_attestation_key_id !~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$'
       OR p_attestation_signature !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'production evidence attestation is invalid';
    END IF;
    INSERT INTO public.mcp_rollout_evidence_snapshot (
        evidence_id, environment_id, rollout_program, git_sha, deployment_id,
        stage, config_fingerprint, window_started_at, window_ended_at, recorded_at,
        producer, source, snapshot_id, nonce, evidence_kind, payload, payload_digest,
        attestation_key_id, attestation_signature
    ) VALUES (
        materialized.evidence_id, materialized.environment_id,
        materialized.rollout_program, materialized.git_sha,
        materialized.deployment_id, materialized.stage,
        materialized.config_fingerprint, materialized.window_started_at,
        materialized.window_ended_at, materialized.recorded_at,
        materialized.producer, materialized.source, materialized.snapshot_id,
        materialized.nonce, materialized.evidence_kind, materialized.payload,
        materialized.payload_digest, p_attestation_key_id,
        p_attestation_signature
    ) RETURNING * INTO saved;
    RETURN saved;
END;
$function$;

CREATE OR REPLACE FUNCTION mcp_rollout_api.append_ci_evidence_snapshot(
    p_evidence_id pg_catalog.text,
    p_environment_id pg_catalog.text,
    p_git_sha pg_catalog.text,
    p_deployment_id pg_catalog.text,
    p_stage pg_catalog.text,
    p_config_fingerprint pg_catalog.text,
    p_window_started_at pg_catalog.timestamptz,
    p_window_ended_at pg_catalog.timestamptz,
    p_recorded_at pg_catalog.timestamptz,
    p_snapshot_id pg_catalog.int8,
    p_nonce pg_catalog.text,
    p_evidence_kind pg_catalog.text,
    p_payload pg_catalog.jsonb,
    p_payload_digest pg_catalog.text
) RETURNS public.mcp_rollout_evidence_snapshot
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    expected_digest pg_catalog.text;
    previous public.mcp_rollout_evidence_snapshot;
    saved public.mcp_rollout_evidence_snapshot;
BEGIN
    PERFORM mcp_rollout_api.lock_gate_scope(p_environment_id, p_recorded_at);
    expected_digest := pg_catalog.encode(
        pg_catalog.sha256(
            pg_catalog.convert_to(
                mcp_rollout_api.canonical_jsonb_text(pg_catalog.jsonb_build_object(
                    'evidence_id', p_evidence_id,
                    'environment_id', p_environment_id,
                    'git_sha', p_git_sha,
                    'deployment_id', p_deployment_id,
                    'stage', p_stage,
                    'config_fingerprint', p_config_fingerprint,
                    'window_started_at', mcp_rollout_api.canonical_timestamp(p_window_started_at),
                    'window_ended_at', mcp_rollout_api.canonical_timestamp(p_window_ended_at),
                    'recorded_at', mcp_rollout_api.canonical_timestamp(p_recorded_at),
                    'producer', 'ci_pipeline',
                    'source', 'ci',
                    'snapshot_id', p_snapshot_id,
                    'nonce', p_nonce,
                    'payload', p_payload
                )),
                'UTF8'
            )
        ),
        'hex'
    );
    IF p_payload_digest IS DISTINCT FROM expected_digest
       OR p_evidence_kind IS DISTINCT FROM p_payload ->> 'kind'
       OR p_window_ended_at <= p_window_started_at
       OR p_recorded_at < p_window_ended_at THEN
        RAISE EXCEPTION 'CI rollout evidence is invalid';
    END IF;
    SELECT * INTO previous
    FROM public.mcp_rollout_evidence_snapshot AS evidence
    WHERE evidence.environment_id = p_environment_id
      AND evidence.rollout_program = 'user_mcp_phase3'
      AND evidence.deployment_id = p_deployment_id
      AND evidence.stage = p_stage
    ORDER BY evidence.snapshot_id DESC, evidence.recorded_at DESC
    LIMIT 1;
    IF previous.evidence_id IS NOT NULL AND (
        p_snapshot_id <= previous.snapshot_id
        OR p_recorded_at <= previous.recorded_at
        OR p_window_ended_at <= previous.window_ended_at
        OR p_window_started_at > previous.window_ended_at
        OR p_config_fingerprint <> previous.config_fingerprint
    ) THEN
        RAISE EXCEPTION 'CI rollout evidence is non-monotonic';
    END IF;
    INSERT INTO public.mcp_rollout_evidence_snapshot (
        evidence_id, environment_id, rollout_program, git_sha, deployment_id,
        stage, config_fingerprint, window_started_at, window_ended_at, recorded_at,
        producer, source, snapshot_id, nonce, evidence_kind, payload, payload_digest
    ) VALUES (
        p_evidence_id, p_environment_id, 'user_mcp_phase3', p_git_sha, p_deployment_id,
        p_stage, p_config_fingerprint, p_window_started_at, p_window_ended_at, p_recorded_at,
        'ci_pipeline', 'ci', p_snapshot_id, p_nonce, p_evidence_kind, p_payload, p_payload_digest
    ) RETURNING * INTO saved;
    RETURN saved;
END;
$function$;

CREATE OR REPLACE FUNCTION mcp_rollout_api.ensure_gate_scope(
    p_environment_id pg_catalog.text,
    p_created_at pg_catalog.timestamptz
) RETURNS public.mcp_rollout_gate_scope
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
    INSERT INTO public.mcp_rollout_gate_scope (environment_id, rollout_program, created_at)
    VALUES (p_environment_id, 'user_mcp_phase3', p_created_at)
    ON CONFLICT (environment_id, rollout_program) DO UPDATE
    SET rollout_program = EXCLUDED.rollout_program
    RETURNING *;
$function$;

CREATE OR REPLACE FUNCTION mcp_rollout_api.append_stage_approval(
    p_approval_id pg_catalog.text,
    p_environment_id pg_catalog.text,
    p_deployment_id pg_catalog.text,
    p_stage pg_catalog.text,
    p_config_fingerprint pg_catalog.text,
    p_evidence_id pg_catalog.text,
    p_reason pg_catalog.text,
    p_approver pg_catalog.text,
    p_created_at pg_catalog.timestamptz
) RETURNS public.mcp_rollout_stage_approval
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    saved public.mcp_rollout_stage_approval;
    evidence public.mcp_rollout_evidence_snapshot;
    evidence_rank pg_catalog.int4;
    target_rank pg_catalog.int4;
BEGIN
    PERFORM mcp_rollout_api.lock_gate_scope(p_environment_id, p_created_at);

    SELECT * INTO evidence
    FROM public.mcp_rollout_evidence_snapshot AS stored
    WHERE stored.evidence_id = p_evidence_id
      AND stored.environment_id = p_environment_id
      AND stored.rollout_program = 'user_mcp_phase3';
    IF evidence.evidence_id IS NULL THEN
        RAISE EXCEPTION 'rollout approval evidence scope does not match';
    END IF;
    IF p_created_at < evidence.recorded_at
       OR p_deployment_id = '' OR p_config_fingerprint = ''
       OR p_reason = '' OR p_approver = ''
       OR evidence.evidence_kind <> (evidence.payload ->> 'kind') THEN
        RAISE EXCEPTION 'rollout approval fields are invalid';
    END IF;
    evidence_rank := pg_catalog.array_position(
        ARRAY['off', 'internal_shadow', 'internal_enforce', 'cohort_enforce', 'full_enforce', 'legacy_assembly_off'],
        evidence.stage
    );
    target_rank := pg_catalog.array_position(
        ARRAY['off', 'internal_shadow', 'internal_enforce', 'cohort_enforce', 'full_enforce', 'legacy_assembly_off'],
        p_stage
    );
    IF evidence_rank IS NULL OR target_rank IS NULL THEN
        RAISE EXCEPTION 'rollout approval stage is invalid';
    END IF;
    IF evidence.stage = 'off' AND p_stage = 'internal_shadow' THEN
        IF evidence.source <> 'ci' OR evidence.producer <> 'ci_pipeline'
           OR evidence.evidence_kind <> 'ci_conformance' THEN
            RAISE EXCEPTION 'off-to-shadow approval requires CI conformance evidence';
        END IF;
    ELSIF evidence.source <> 'production'
       OR evidence.producer <> 'production_snapshot_producer'
       OR evidence.evidence_kind <> evidence.stage
       OR NOT (
           (evidence.stage = 'internal_shadow' AND p_stage = 'internal_enforce')
           OR (evidence.stage = 'internal_enforce' AND p_stage = 'cohort_enforce')
           OR (evidence.stage = 'cohort_enforce' AND p_stage IN ('cohort_enforce', 'full_enforce'))
           OR (evidence.stage = 'full_enforce' AND p_stage = 'legacy_assembly_off')
           OR target_rank <= evidence_rank
       ) THEN
        RAISE EXCEPTION 'rollout approval evidence transition does not match';
    END IF;

    INSERT INTO public.mcp_rollout_stage_approval (
        approval_id, environment_id, rollout_program, deployment_id, stage,
        config_fingerprint, evidence_id, reason, approver, created_at
    ) VALUES (
        p_approval_id, p_environment_id, 'user_mcp_phase3', p_deployment_id, p_stage,
        p_config_fingerprint, p_evidence_id, p_reason, p_approver, p_created_at
    ) RETURNING * INTO saved;
    RETURN saved;
END;
$function$;

CREATE OR REPLACE FUNCTION mcp_rollout_api.append_promotion_block(
    p_block_id pg_catalog.text,
    p_environment_id pg_catalog.text,
    p_deployment_id pg_catalog.text,
    p_stage pg_catalog.text,
    p_config_fingerprint pg_catalog.text,
    p_evidence_id pg_catalog.text,
    p_reason_code pg_catalog.text,
    p_created_at pg_catalog.timestamptz
) RETURNS public.mcp_rollout_promotion_block
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    saved public.mcp_rollout_promotion_block;
BEGIN
    PERFORM mcp_rollout_api.lock_gate_scope(p_environment_id, p_created_at);

    SELECT * INTO saved
    FROM public.mcp_rollout_promotion_block
    WHERE block_id = p_block_id;
    IF saved.block_id IS NOT NULL THEN
        IF ROW(
            saved.environment_id, saved.rollout_program, saved.deployment_id,
            saved.stage, saved.config_fingerprint, saved.evidence_id,
            saved.reason_code, saved.created_at
        ) IS DISTINCT FROM ROW(
            p_environment_id, 'user_mcp_phase3', p_deployment_id, p_stage,
            p_config_fingerprint, p_evidence_id, p_reason_code, p_created_at
        ) THEN
            RAISE EXCEPTION 'rollout promotion block ID payload conflict';
        END IF;
        RETURN saved;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM public.mcp_rollout_evidence_snapshot
        WHERE evidence_id = p_evidence_id
          AND environment_id = p_environment_id
          AND rollout_program = 'user_mcp_phase3'
          AND deployment_id = p_deployment_id
          AND stage = p_stage
          AND config_fingerprint = p_config_fingerprint
    ) THEN
        RAISE EXCEPTION 'rollout promotion block evidence scope does not match';
    END IF;

    INSERT INTO public.mcp_rollout_promotion_block (
        block_id, environment_id, rollout_program, deployment_id, stage,
        config_fingerprint, evidence_id, reason_code, created_at
    ) VALUES (
        p_block_id, p_environment_id, 'user_mcp_phase3', p_deployment_id, p_stage,
        p_config_fingerprint, p_evidence_id, p_reason_code, p_created_at
    ) RETURNING * INTO saved;
    RETURN saved;
END;
$function$;

CREATE OR REPLACE FUNCTION mcp_rollout_api.append_deployment_activation(
    p_activation_id pg_catalog.text,
    p_environment_id pg_catalog.text,
    p_deployment_id pg_catalog.text,
    p_stage pg_catalog.text,
    p_config_fingerprint pg_catalog.text,
    p_approval_id pg_catalog.text,
    p_evidence_id pg_catalog.text,
    p_previous_activation_id pg_catalog.text,
    p_operator_reason pg_catalog.text,
    p_is_rollback pg_catalog.bool,
    p_created_at pg_catalog.timestamptz
) RETURNS public.mcp_rollout_deployment_activation
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    saved public.mcp_rollout_deployment_activation;
    evidence public.mcp_rollout_evidence_snapshot;
    previous_activation public.mcp_rollout_deployment_activation;
    latest_activation_id pg_catalog.text;
    evidence_rank pg_catalog.int4;
    previous_rank pg_catalog.int4;
    candidate_rank pg_catalog.int4;
BEGIN
    PERFORM mcp_rollout_api.lock_gate_scope(p_environment_id, p_created_at);

    IF NOT EXISTS (
        SELECT 1
        FROM public.mcp_rollout_stage_approval
        WHERE approval_id = p_approval_id
          AND environment_id = p_environment_id
          AND rollout_program = 'user_mcp_phase3'
          AND deployment_id = p_deployment_id
          AND stage = p_stage
          AND config_fingerprint = p_config_fingerprint
          AND evidence_id = p_evidence_id
    ) THEN
        RAISE EXCEPTION 'rollout activation approval does not match target';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.mcp_rollout_block_resolution
        WHERE approval_id = p_approval_id
    ) THEN
        RAISE EXCEPTION 'rollout approval was consumed by a block resolution';
    END IF;

    SELECT * INTO evidence
    FROM public.mcp_rollout_evidence_snapshot AS stored
    WHERE stored.evidence_id = p_evidence_id
      AND stored.environment_id = p_environment_id
      AND stored.rollout_program = 'user_mcp_phase3';
    IF evidence.evidence_id IS NULL
       OR evidence.evidence_kind <> (evidence.payload ->> 'kind')
       OR p_created_at < evidence.recorded_at
       OR p_operator_reason = '' THEN
        RAISE EXCEPTION 'rollout activation evidence is invalid';
    END IF;

    SELECT activation.activation_id INTO latest_activation_id
    FROM public.mcp_rollout_deployment_activation AS activation
    WHERE activation.environment_id = p_environment_id
      AND activation.rollout_program = 'user_mcp_phase3'
    ORDER BY activation.created_at DESC, activation.activation_id DESC
    LIMIT 1;
    IF latest_activation_id IS NOT NULL
       AND p_previous_activation_id IS DISTINCT FROM latest_activation_id THEN
        RAISE EXCEPTION 'rollout activation chain fork is not allowed';
    END IF;

    IF p_previous_activation_id IS NOT NULL THEN
        SELECT * INTO previous_activation
        FROM public.mcp_rollout_deployment_activation AS activation
        WHERE activation.activation_id = p_previous_activation_id
          AND activation.environment_id = p_environment_id
          AND activation.rollout_program = 'user_mcp_phase3';
        IF previous_activation.activation_id IS NULL THEN
            RAISE EXCEPTION 'rollout previous activation scope does not match';
        END IF;
    END IF;

    evidence_rank := pg_catalog.array_position(
        ARRAY['off', 'internal_shadow', 'internal_enforce', 'cohort_enforce', 'full_enforce', 'legacy_assembly_off'],
        evidence.stage
    );
    previous_rank := pg_catalog.array_position(
        ARRAY['off', 'internal_shadow', 'internal_enforce', 'cohort_enforce', 'full_enforce', 'legacy_assembly_off'],
        previous_activation.stage
    );
    candidate_rank := pg_catalog.array_position(
        ARRAY['off', 'internal_shadow', 'internal_enforce', 'cohort_enforce', 'full_enforce', 'legacy_assembly_off'],
        p_stage
    );
    IF evidence_rank IS NULL OR candidate_rank IS NULL THEN
        RAISE EXCEPTION 'rollout activation stage is invalid';
    END IF;
    IF p_previous_activation_id IS NOT NULL AND (
        evidence.deployment_id <> previous_activation.deployment_id
        OR evidence.stage <> previous_activation.stage
        OR evidence.config_fingerprint <> previous_activation.config_fingerprint
    ) THEN
        RAISE EXCEPTION 'rollout evidence is not bound to the previous activation';
    END IF;

    IF p_is_rollback THEN
        IF previous_activation.activation_id IS NULL
           OR previous_rank IS NULL
           OR candidate_rank >= previous_rank
           OR evidence.source <> 'production'
           OR evidence.producer <> 'production_snapshot_producer'
           OR evidence.evidence_kind <> evidence.stage THEN
            RAISE EXCEPTION 'rollout rollback evidence or target is invalid';
        END IF;
    ELSE
        IF NOT (
            (evidence.stage = 'off' AND p_stage = 'internal_shadow'
             AND evidence.source = 'ci' AND evidence.producer = 'ci_pipeline'
             AND evidence.evidence_kind = 'ci_conformance')
            OR (evidence.stage = 'internal_shadow' AND p_stage = 'internal_enforce'
                AND evidence.source = 'production' AND evidence.evidence_kind = 'internal_shadow')
            OR (evidence.stage = 'internal_enforce' AND p_stage = 'cohort_enforce'
                AND evidence.source = 'production' AND evidence.evidence_kind = 'internal_enforce')
            OR (evidence.stage = 'cohort_enforce' AND p_stage IN ('cohort_enforce', 'full_enforce')
                AND evidence.source = 'production' AND evidence.evidence_kind = 'cohort_enforce')
            OR (evidence.stage = 'full_enforce' AND p_stage = 'legacy_assembly_off'
                AND evidence.source = 'production' AND evidence.evidence_kind = 'full_enforce')
        ) THEN
            RAISE EXCEPTION 'rollout activation transition does not match evidence';
        END IF;
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.mcp_rollout_promotion_block AS block
        WHERE block.environment_id = p_environment_id
          AND block.rollout_program = 'user_mcp_phase3'
          AND NOT EXISTS (
              SELECT 1 FROM public.mcp_rollout_block_resolution AS resolution
              WHERE resolution.block_id = block.block_id
          )
    ) THEN
        IF NOT p_is_rollback OR previous_activation.activation_id IS NULL THEN
            RAISE EXCEPTION 'rollout activation is blocked by an active promotion block';
        END IF;
        IF previous_rank IS NULL OR candidate_rank IS NULL OR candidate_rank >= previous_rank THEN
            RAISE EXCEPTION 'rollout rollback must strictly decrease exposure';
        END IF;
    END IF;

    INSERT INTO public.mcp_rollout_deployment_activation (
        activation_id, environment_id, rollout_program, deployment_id, stage,
        config_fingerprint, approval_id, evidence_id, previous_activation_id,
        operator_reason, is_rollback, created_at
    ) VALUES (
        p_activation_id, p_environment_id, 'user_mcp_phase3', p_deployment_id, p_stage,
        p_config_fingerprint, p_approval_id, p_evidence_id, p_previous_activation_id,
        p_operator_reason, p_is_rollback, p_created_at
    ) RETURNING * INTO saved;
    RETURN saved;
END;
$function$;

CREATE OR REPLACE FUNCTION mcp_rollout_api.append_block_resolution(
    p_resolution_id pg_catalog.text,
    p_block_id pg_catalog.text,
    p_approval_id pg_catalog.text,
    p_evidence_id pg_catalog.text,
    p_reason pg_catalog.text,
    p_approver pg_catalog.text,
    p_created_at pg_catalog.timestamptz
) RETURNS public.mcp_rollout_block_resolution
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    saved public.mcp_rollout_block_resolution;
    block_environment_id pg_catalog.text;
BEGIN
    SELECT environment_id INTO block_environment_id
    FROM public.mcp_rollout_promotion_block
    WHERE block_id = p_block_id;
    IF block_environment_id IS NULL THEN
        RAISE EXCEPTION 'rollout promotion block does not exist';
    END IF;

    PERFORM mcp_rollout_api.lock_gate_scope(block_environment_id, p_created_at);

    IF NOT EXISTS (
        SELECT 1
        FROM public.mcp_rollout_stage_approval AS approval
        JOIN public.mcp_rollout_evidence_snapshot AS evidence
          ON evidence.evidence_id = p_evidence_id
        WHERE approval.approval_id = p_approval_id
          AND approval.evidence_id = p_evidence_id
          AND approval.environment_id = block_environment_id
          AND evidence.environment_id = block_environment_id
    ) THEN
        RAISE EXCEPTION 'rollout block resolution scope does not match';
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.mcp_rollout_deployment_activation
        WHERE approval_id = p_approval_id
    ) THEN
        RAISE EXCEPTION 'rollout approval was consumed by an activation';
    END IF;

    INSERT INTO public.mcp_rollout_block_resolution (
        resolution_id, block_id, approval_id, evidence_id, reason, approver, created_at
    ) VALUES (
        p_resolution_id, p_block_id, p_approval_id, p_evidence_id,
        p_reason, p_approver, p_created_at
    ) RETURNING * INTO saved;
    RETURN saved;
END;
$function$;

CREATE OR REPLACE FUNCTION mcp_rollout_api.reject_history_mutation()
RETURNS pg_catalog.trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $function$
BEGIN
    RAISE EXCEPTION 'rollout history is append-only';
END;
$function$;

DO $triggers$
DECLARE
    ledger_table pg_catalog.text;
BEGIN
    FOREACH ledger_table IN ARRAY ARRAY[
        'mcp_rollout_drill_observation',
        'mcp_rollout_evidence_snapshot',
        'mcp_rollout_stage_approval',
        'mcp_rollout_deployment_activation',
        'mcp_rollout_promotion_block',
        'mcp_rollout_block_resolution'
    ] LOOP
        IF NOT EXISTS (
            SELECT 1
            FROM pg_catalog.pg_trigger
            WHERE tgrelid = pg_catalog.to_regclass('public.' || ledger_table)
              AND tgname = 'mcp_rollout_history_append_only'
              AND NOT tgisinternal
        ) THEN
            EXECUTE pg_catalog.format(
                'CREATE TRIGGER mcp_rollout_history_append_only '
                'BEFORE UPDATE OR DELETE ON public.%I '
                'FOR EACH ROW EXECUTE FUNCTION mcp_rollout_api.reject_history_mutation()',
                ledger_table
            );
        END IF;
    END LOOP;
END;
$triggers$;

-- SECURITY DEFINER code is owned by a dedicated, non-login role with only the
-- exact table authority required by these static functions. Runtime roles own
-- neither the API schema nor any function or base table.
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public
FROM maf_rollout_api_owner;
REVOKE ALL ON SCHEMA public, mcp_rollout_api
FROM maf_rollout_api_owner;
GRANT USAGE ON SCHEMA public, mcp_rollout_api
TO maf_rollout_api_owner;
GRANT SELECT, INSERT, UPDATE ON TABLE
    public.mcp_rollout_gate_scope,
    public.mcp_rollout_metric_bucket,
    public.mcp_rollout_instance_config
TO maf_rollout_api_owner;
GRANT SELECT, INSERT, DELETE ON TABLE
    public.mcp_shadow_audit_sample
TO maf_rollout_api_owner;
GRANT SELECT, INSERT ON TABLE
    public.mcp_rollout_drill_observation,
    public.mcp_rollout_evidence_snapshot,
    public.mcp_rollout_stage_approval,
    public.mcp_rollout_deployment_activation,
    public.mcp_rollout_promotion_block,
    public.mcp_rollout_block_resolution
TO maf_rollout_api_owner;

GRANT CREATE ON SCHEMA mcp_rollout_api TO maf_rollout_api_owner;
DO $owners$
DECLARE
    api_function pg_catalog.regprocedure;
BEGIN
    FOR api_function IN
        SELECT procedure.oid::pg_catalog.regprocedure
        FROM pg_catalog.pg_proc AS procedure
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = procedure.pronamespace
        WHERE namespace.nspname = 'mcp_rollout_api'
    LOOP
        EXECUTE pg_catalog.format(
            'ALTER FUNCTION %s OWNER TO maf_rollout_api_owner',
            api_function
        );
    END LOOP;
END;
$owners$;
REVOKE CREATE ON SCHEMA mcp_rollout_api FROM maf_rollout_api_owner;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA mcp_rollout_api FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA mcp_rollout_api FROM
    maf_rollout_app_writer,
    maf_rollout_snapshot_producer,
    maf_rollout_ci_evidence_writer,
    maf_rollout_gate_evaluator,
    maf_rollout_operator,
    maf_rollout_validator,
    maf_rollout_drill_recorder;

GRANT EXECUTE ON FUNCTION mcp_rollout_api.append_drill_observation(
    pg_catalog.text, pg_catalog.text, pg_catalog.text, pg_catalog.text,
    pg_catalog.text, pg_catalog.text, pg_catalog.timestamptz,
    pg_catalog.timestamptz, pg_catalog.timestamptz, pg_catalog.text
) TO maf_rollout_drill_recorder;

GRANT EXECUTE ON FUNCTION mcp_rollout_api.upsert_metric_bucket(
    pg_catalog.text, pg_catalog.text, pg_catalog.text, pg_catalog.text,
    pg_catalog.text, pg_catalog.text, pg_catalog.timestamptz, pg_catalog.timestamptz,
    pg_catalog.text, pg_catalog.text, pg_catalog.text, pg_catalog.text,
    pg_catalog.text, pg_catalog.text, pg_catalog.text, pg_catalog.text,
    pg_catalog.text, pg_catalog.text, pg_catalog.int8, pg_catalog.timestamptz
) TO maf_rollout_app_writer;
GRANT EXECUTE ON FUNCTION mcp_rollout_api.set_metric_bucket(
    pg_catalog.text, pg_catalog.text, pg_catalog.text, pg_catalog.text,
    pg_catalog.text, pg_catalog.text, pg_catalog.timestamptz, pg_catalog.timestamptz,
    pg_catalog.text, pg_catalog.text, pg_catalog.text, pg_catalog.text,
    pg_catalog.text, pg_catalog.text, pg_catalog.text, pg_catalog.text,
    pg_catalog.text, pg_catalog.text, pg_catalog.int8, pg_catalog.timestamptz
) TO maf_rollout_app_writer;
GRANT EXECUTE ON FUNCTION mcp_rollout_api.append_shadow_audit_sample(
    pg_catalog.text, pg_catalog.text, pg_catalog.text, pg_catalog.text,
    pg_catalog.text, pg_catalog.text, pg_catalog.text, pg_catalog.text,
    pg_catalog.text, pg_catalog.text, pg_catalog.text, pg_catalog.text,
    pg_catalog.text, pg_catalog.text, pg_catalog.text, pg_catalog.text,
    pg_catalog.text, pg_catalog.jsonb, pg_catalog.text, pg_catalog.timestamptz,
    pg_catalog.timestamptz, pg_catalog.timestamptz
) TO maf_rollout_app_writer;
GRANT EXECUTE ON FUNCTION mcp_rollout_api.delete_expired_shadow_audit_samples(
    pg_catalog.timestamptz, pg_catalog.int4
) TO maf_rollout_app_writer;
GRANT EXECUTE ON FUNCTION mcp_rollout_api.upsert_instance_config_lease(
    pg_catalog.text, pg_catalog.text, pg_catalog.text, pg_catalog.text,
    pg_catalog.text, pg_catalog.text, pg_catalog.text, pg_catalog.timestamptz,
    pg_catalog.timestamptz
) TO maf_rollout_app_writer;
GRANT EXECUTE ON FUNCTION mcp_rollout_api.prepare_production_evidence_snapshot(
    pg_catalog.text, pg_catalog.text, pg_catalog.text,
    pg_catalog.timestamptz, pg_catalog.timestamptz
) TO maf_rollout_snapshot_producer;
GRANT EXECUTE ON FUNCTION mcp_rollout_api.finalize_production_evidence_snapshot(
    pg_catalog.text, pg_catalog.text, pg_catalog.text,
    pg_catalog.timestamptz, pg_catalog.timestamptz,
    pg_catalog.text, pg_catalog.text, pg_catalog.text, pg_catalog.text
) TO maf_rollout_snapshot_producer;
GRANT EXECUTE ON FUNCTION mcp_rollout_api.append_ci_evidence_snapshot(
    pg_catalog.text, pg_catalog.text, pg_catalog.text, pg_catalog.text,
    pg_catalog.text, pg_catalog.text, pg_catalog.timestamptz, pg_catalog.timestamptz,
    pg_catalog.timestamptz, pg_catalog.int8, pg_catalog.text, pg_catalog.text,
    pg_catalog.jsonb, pg_catalog.text
) TO maf_rollout_ci_evidence_writer;
GRANT EXECUTE ON FUNCTION mcp_rollout_api.ensure_gate_scope(
    pg_catalog.text, pg_catalog.timestamptz
) TO maf_rollout_gate_evaluator, maf_rollout_operator;
GRANT EXECUTE ON FUNCTION mcp_rollout_api.append_stage_approval(
    pg_catalog.text, pg_catalog.text, pg_catalog.text, pg_catalog.text,
    pg_catalog.text, pg_catalog.text, pg_catalog.text, pg_catalog.text,
    pg_catalog.timestamptz
) TO maf_rollout_operator;
GRANT EXECUTE ON FUNCTION mcp_rollout_api.append_promotion_block(
    pg_catalog.text, pg_catalog.text, pg_catalog.text, pg_catalog.text,
    pg_catalog.text, pg_catalog.text, pg_catalog.text, pg_catalog.timestamptz
) TO maf_rollout_gate_evaluator;
GRANT EXECUTE ON FUNCTION mcp_rollout_api.append_deployment_activation(
    pg_catalog.text, pg_catalog.text, pg_catalog.text, pg_catalog.text,
    pg_catalog.text, pg_catalog.text, pg_catalog.text, pg_catalog.text,
    pg_catalog.text, pg_catalog.bool, pg_catalog.timestamptz
) TO maf_rollout_operator;
GRANT EXECUTE ON FUNCTION mcp_rollout_api.append_block_resolution(
    pg_catalog.text, pg_catalog.text, pg_catalog.text, pg_catalog.text,
    pg_catalog.text, pg_catalog.text, pg_catalog.timestamptz
) TO maf_rollout_operator;

-- CI can append only source='ci' evidence through its dedicated function. It has
-- no base-table DML and no EXECUTE privilege on the production evidence function.
DO $$ BEGIN
    IF pg_catalog.to_regprocedure(
        'mcp_rollout_api.append_production_evidence_snapshot(text,text,text,text,text,text,timestamptz,timestamptz,timestamptz,int8,text,text,jsonb,text)'
    ) IS NOT NULL THEN
        REVOKE EXECUTE ON FUNCTION mcp_rollout_api.append_production_evidence_snapshot(
            pg_catalog.text, pg_catalog.text, pg_catalog.text, pg_catalog.text,
            pg_catalog.text, pg_catalog.text, pg_catalog.timestamptz, pg_catalog.timestamptz,
            pg_catalog.timestamptz, pg_catalog.int8, pg_catalog.text, pg_catalog.text,
            pg_catalog.jsonb, pg_catalog.text
        ) FROM
            maf_rollout_app_writer,
            maf_rollout_snapshot_producer,
            maf_rollout_ci_evidence_writer,
            maf_rollout_gate_evaluator,
            maf_rollout_operator,
            maf_rollout_validator,
            maf_rollout_drill_recorder;
    END IF;
END; $$;
REVOKE EXECUTE ON FUNCTION mcp_rollout_api.append_production_evidence_snapshot(
    pg_catalog.text, pg_catalog.text, pg_catalog.text, pg_catalog.text,
    pg_catalog.text, pg_catalog.text, pg_catalog.timestamptz, pg_catalog.timestamptz,
    pg_catalog.timestamptz, pg_catalog.int8, pg_catalog.text, pg_catalog.text,
    pg_catalog.jsonb, pg_catalog.text, pg_catalog.text, pg_catalog.text
) FROM
    maf_rollout_app_writer,
    maf_rollout_snapshot_producer,
    maf_rollout_ci_evidence_writer,
    maf_rollout_gate_evaluator,
    maf_rollout_operator,
    maf_rollout_validator,
    maf_rollout_drill_recorder;
