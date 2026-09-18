-- Explicit compatibility upgrade; run as the real database administrator.
-- Does not initialize MCP or change any stored evidence.
BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '30s';
SET LOCAL search_path = pg_catalog, public, pg_temp;
LOCK TABLE public.mcp_rollout_evidence_snapshot IN ACCESS EXCLUSIVE MODE;
ALTER TABLE public.mcp_rollout_evidence_snapshot DROP CONSTRAINT IF EXISTS ck_mcp_rollout_evidence_snapshot_mcp_rollout_evidence_a_48af;
ALTER TABLE public.mcp_rollout_evidence_snapshot ADD CONSTRAINT ck_mcp_rollout_evidence_snapshot_mcp_rollout_evidence_a_48af CHECK ((source IN ('ci', 'deployment') AND attestation_key_id IS NULL AND attestation_signature IS NULL) OR (source = 'production' AND attestation_key_id IS NOT NULL AND attestation_signature IS NOT NULL));
ALTER TABLE public.mcp_rollout_evidence_snapshot DROP CONSTRAINT IF EXISTS ck_mcp_rollout_evidence_snapshot_mcp_rollout_evidence_kind;
ALTER TABLE public.mcp_rollout_evidence_snapshot ADD CONSTRAINT ck_mcp_rollout_evidence_snapshot_mcp_rollout_evidence_kind CHECK (evidence_kind IN ('ci_conformance', 'internal_shadow', 'internal_enforce', 'cohort_enforce', 'full_enforce', 'legacy_assembly_off', 'rollback_drill', 'resource_baseline', 'release_tag', 'first_enablement'));
ALTER TABLE public.mcp_rollout_evidence_snapshot DROP CONSTRAINT IF EXISTS ck_mcp_rollout_evidence_snapshot_mcp_rollout_evidence_producer;
ALTER TABLE public.mcp_rollout_evidence_snapshot ADD CONSTRAINT ck_mcp_rollout_evidence_snapshot_mcp_rollout_evidence_producer CHECK (producer IN ('ci_pipeline', 'production_snapshot_producer', 'deployment_initializer'));
ALTER TABLE public.mcp_rollout_evidence_snapshot DROP CONSTRAINT IF EXISTS ck_mcp_rollout_evidence_snapshot_mcp_rollout_evidence_source;
ALTER TABLE public.mcp_rollout_evidence_snapshot ADD CONSTRAINT ck_mcp_rollout_evidence_snapshot_mcp_rollout_evidence_source CHECK (source IN ('ci', 'production', 'deployment'));
ALTER TABLE public.mcp_rollout_evidence_snapshot DROP CONSTRAINT IF EXISTS ck_mcp_rollout_evidence_snapshot_mcp_rollout_first_enab_5881;
ALTER TABLE public.mcp_rollout_evidence_snapshot ADD CONSTRAINT ck_mcp_rollout_evidence_snapshot_mcp_rollout_first_enab_5881 CHECK ((source = 'deployment' AND producer = 'deployment_initializer' AND evidence_kind = 'first_enablement' AND stage = 'off') OR (source <> 'deployment' AND producer <> 'deployment_initializer' AND evidence_kind <> 'first_enablement'));
COMMIT;
