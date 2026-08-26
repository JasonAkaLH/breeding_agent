from __future__ import annotations

import ast
import inspect
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

import src.api as api
import src.api.runtime as runtime_module
from src.api.app import create_app
from src.api.runtime import ApiRuntime, build_api_runtime
from src.storage.postgres import PostgreSQLAgentRepository
from src.storage.runtime_sidecar_agent_repository import RuntimeSidecarAgentRepository
from src.storage.sqlite import SQLiteAgentRepository


EXPECTED_SIGNATURES = {
    "init": {
        "parameters": [
            ("self", "POSITIONAL_OR_KEYWORD", None, "__required__"),
            ("engine", "KEYWORD_ONLY", "Engine", "__required__"),
            ("storage", "KEYWORD_ONLY", "StoragePort", "__required__"),
            ("capability_registry", "KEYWORD_ONLY", "CapabilityRegistry", "__required__"),
            ("instance_registry", "KEYWORD_ONLY", "InstanceRegistry", "__required__"),
            ("event_broker", "KEYWORD_ONLY", "InMemoryEventBroker", "__required__"),
            ("cancellation_service", "KEYWORD_ONLY", "CancellationService", "__required__"),
            ("interrupt_service", "KEYWORD_ONLY", "InterruptService", "__required__"),
            ("agent_loop_orchestrator", "KEYWORD_ONLY", "AgentLoopOrchestrator", "__required__"),
            ("agent_run_repository", "KEYWORD_ONLY", "AgentRunRepository", "__required__"),
            ("agent_task_projection", "KEYWORD_ONLY", "AgentTaskProjectionService", "__required__"),
            ("agent_capability_invoker", "KEYWORD_ONLY", "AgentCapabilityInvoker", "__required__"),
            ("agent_invocation_contexts", "KEYWORD_ONLY", "AgentInvocationContextStore", "__required__"),
            ("agent_run_recovery", "KEYWORD_ONLY", "AgentRunRecoveryCoordinator", "__required__"),
            ("main_agent_llm_runtime", "KEYWORD_ONLY", "SharedLLMRuntime", "__required__"),
            ("mysql_adapter", "KEYWORD_ONLY", "MySQLReadonlyAdapter | None", "None"),
            ("username_token_service", "KEYWORD_ONLY", "UsernameTokenService | None", "None"),
            ("master_key_sentinel_cipher", "KEYWORD_ONLY", "MasterKeySentinelCipher", "__required__"),
            ("mcp_audit_reference_signer", "KEYWORD_ONLY", "MCPAuditReferenceSigner", "__required__"),
            ("conversation_title_generator", "KEYWORD_ONLY", "ConversationTitleGenerator | None", "None"),
            ("upload_store", "KEYWORD_ONLY", "InMemoryUploadStore | None", "None"),
            ("conversation_memory_builder", "KEYWORD_ONLY", "ConversationMemoryBuilder | None", "None"),
            ("artifact_file_store", "KEYWORD_ONLY", "LocalArtifactFileStore | None", "None"),
            ("conversation_file_store", "KEYWORD_ONLY", "LocalConversationFileStore | None", "None"),
            ("audit_sink", "KEYWORD_ONLY", "JsonlAuditSink | None", "None"),
            ("skill_runtime_state", "KEYWORD_ONLY", "SkillRuntimeState | None", "None"),
            ("skill_input_text_generator", "KEYWORD_ONLY", "SkillInputTextGenerator | None", "None"),
            ("mcp_runtime_state", "KEYWORD_ONLY", "MCPRuntimeState | None", "None"),
            ("runtime_sidecar_client", "KEYWORD_ONLY", "Any | None", "None"),
            ("model_edition_config", "KEYWORD_ONLY", "Mapping[str, Any] | None", "None"),
            ("local_cancelled_task_ids", "KEYWORD_ONLY", "set[str] | None", "None"),
            ("auth_generation_cache", "KEYWORD_ONLY", "AuthGenerationCache | None", "None"),
            ("auth_invalidation_bus", "KEYWORD_ONLY", "InMemoryAuthInvalidationBus | None", "None"),
            ("postgres_auth_invalidation_bus", "KEYWORD_ONLY", "PostgresAuthInvalidationBus | None", "None"),
            ("user_mcp_config_service", "KEYWORD_ONLY", "UserMCPConfigService | None", "None"),
            ("user_mcp_health_runner", "KEYWORD_ONLY", "MCPHealthRunner | None", "None"),
            ("user_mcp_gateway", "KEYWORD_ONLY", "MCPGateway | None", "None"),
            ("mcp_credential_cipher", "KEYWORD_ONLY", "MCPCredentialCipher | None", "None"),
            ("mcp_remote_task_recovery_worker", "KEYWORD_ONLY", "MCPRemoteTaskRecoveryWorker | None", "None"),
            ("mcp_invalidation_bus", "KEYWORD_ONLY", "InMemoryMCPInvalidationBus | None", "None"),
            ("postgres_mcp_invalidation_bus", "KEYWORD_ONLY", "PostgresMCPInvalidationBus | None", "None"),
            ("user_mcp_result_store", "KEYWORD_ONLY", "MCPTemporaryResultStore | None", "None"),
            ("mcp_pending_action_payload_store", "KEYWORD_ONLY", "MCPPendingActionPayloadStore | None", "None"),
            ("mcp_terminal_candidate_snapshot_authority", "KEYWORD_ONLY", "MCPTerminalCandidateSnapshotAuthority | None", "None"),
            ("mcp_durable_result_snapshot_authority", "KEYWORD_ONLY", "MCPDurableResultSnapshotAuthority | None", "None"),
            ("mcp_terminal_candidate_lifecycle_manager", "KEYWORD_ONLY", "MCPTerminalCandidateLifecycleManager | None", "None"),
            ("mcp_durable_result_lifecycle_manager", "KEYWORD_ONLY", "MCPDurableResultLifecycleManager | None", "None"),
            ("mcp_result_artifact_projector", "KEYWORD_ONLY", "MCPResultArtifactProjector | None", "None"),
            ("mcp_projection_store", "KEYWORD_ONLY", "MCPProjectionStore | None", "None"),
            ("user_mcp_result_janitor", "KEYWORD_ONLY", "MCPTemporaryResultJanitor | None", "None"),
            ("user_mcp_presence_service", "KEYWORD_ONLY", "MCPTaskPresenceService | None", "None"),
            ("user_mcp_audit_service", "KEYWORD_ONLY", "MCPAuditService | None", "None"),
            ("mcp_shadow_observer", "KEYWORD_ONLY", "MCPShadowRuntimeObserver | None", "None"),
            ("mcp_shadow_manifest", "KEYWORD_ONLY", "VerifiedShadowScenarioManifest | None", "None"),
            ("mcp_shadow_scenario_bindings", "KEYWORD_ONLY", "Mapping[str, ShadowScenario] | None", "None"),
            ("mcp_shadow_manifest_gap_reason", "KEYWORD_ONLY", "str | None", "None"),
            ("user_mcp_routing_enabled", "KEYWORD_ONLY", "bool", "False"),
            ("mcp_rollout_config", "KEYWORD_ONLY", "MCPRolloutConfig | None", "None"),
            ("mcp_rollout_instance_admission", "KEYWORD_ONLY", "_MCPRolloutInstanceAdmission | None", "None"),
            ("mcp_rollout_metric_recorder", "KEYWORD_ONLY", "MCPRolloutMetricRecorder | None", "None"),
            ("mcp_rollout_engine", "KEYWORD_ONLY", "Engine | None", "None"),
            ("mcp_terminal_result_root", "KEYWORD_ONLY", "Path | None", "None"),
            ("mcp_legacy_retirement_binding", "KEYWORD_ONLY", "tuple[str, str] | None", "None"),
            ("mcp_cp7_safety_facade", "KEYWORD_ONLY", "CP7LocalSafetyFacade | None", "None"),
            ("mcp_cp7_open_boundary", "KEYWORD_ONLY", "CP7BoundaryEvidence | None", "None"),
            ("mcp_cp7_boundary_provider", "KEYWORD_ONLY", "Callable[[], Any] | None", "None"),
            ("mcp_cp7_fatal_exit", "KEYWORD_ONLY", "Callable[[int], None]", "os._exit"),
            ("mcp_cp7_safety_probes", "KEYWORD_ONLY", "tuple[Callable[[datetime, datetime], None], ...]", "()"),
            ("mcp_cp7_predecessor_close", "KEYWORD_ONLY", "CP7PredecessorClose | None", "None"),
            ("mcp_cp7_verifier_authorized", "KEYWORD_ONLY", "bool", "False"),
            ("mcp_cp7_maintenance_authorization", "KEYWORD_ONLY", "object | None", "None"),
            ("mcp_cp7_maintenance_authorizer", "KEYWORD_ONLY", "Callable[[object], bool] | None", "None"),
            ("submission_admission_coordinator", "KEYWORD_ONLY", "SubmissionAdmissionCoordinator | None", "None"),
            ("prepared_agent_recovery_loader", "KEYWORD_ONLY", "PreparedAgentRecoveryLoader | None", "None"),
        ],
        "return": "None",
    },
    "build": {
        "parameters": [
            ("database_path", "KEYWORD_ONLY", "str | Path", "__required__"),
            ("audit_log_path", "KEYWORD_ONLY", "str | Path", "__required__"),
            ("mysql_adapter", "KEYWORD_ONLY", "MySQLReadonlyAdapter | None", "None"),
            ("platform_llm_text_generator", "KEYWORD_ONLY", None, "None"),
            ("platform_llm_config", "KEYWORD_ONLY", "Mapping[str, Any] | None", "None"),
            ("platform_llm_config_path", "KEYWORD_ONLY", "str | Path | None", "None"),
            ("platform_llm_client_factory", "KEYWORD_ONLY", "Callable[..., Any] | None", "None"),
            ("enable_platform_llm", "KEYWORD_ONLY", "bool", "True"),
            ("main_agent_stream_generator", "KEYWORD_ONLY", "StreamGenerator | None", "None"),
            ("main_agent_llm_config", "KEYWORD_ONLY", "Mapping[str, Any] | None", "None"),
            ("main_agent_llm_config_path", "KEYWORD_ONLY", "str | Path | None", "None"),
            ("main_agent_llm_client_factory", "KEYWORD_ONLY", "Callable[..., Any] | None", "None"),
            ("main_agent_reasoning_effort", "KEYWORD_ONLY", "ReasoningEffort", "'minimal'"),
            ("skill_input_text_generator", "KEYWORD_ONLY", "SkillInputTextGenerator | None", "None"),
            ("enable_skill_input_llm", "KEYWORD_ONLY", "bool", "True"),
            ("skill_platform_handlers", "KEYWORD_ONLY", "Mapping[str, Callable[..., Any]] | None", "None"),
            ("trusted_skill_handlers", "KEYWORD_ONLY", "Mapping[str, str] | None", "None"),
            ("trusted_skill_services", "KEYWORD_ONLY", "Mapping[str, tuple[str, ...] | list[str] | set[str]] | None", "None"),
            ("skill_services", "KEYWORD_ONLY", "Mapping[str, Any] | None", "None"),
            ("conversation_title_generator", "KEYWORD_ONLY", "ConversationTitleGenerator | None", "None"),
            ("enable_conversation_title_llm", "KEYWORD_ONLY", "bool", "True"),
            ("skill_roots", "KEYWORD_ONLY", "Iterable[str | Path] | None", "None"),
            ("public_skill_roots", "KEYWORD_ONLY", "Iterable[str | Path] | None", "None"),
            ("skill_catalog", "KEYWORD_ONLY", "SkillCatalog | None", "None"),
            ("project_skill_bundle_digest", "KEYWORD_ONLY", "str | None", "None"),
            ("mcp_config", "KEYWORD_ONLY", "Mapping[str, Any] | None", "None"),
            ("mcp_client_factory", "KEYWORD_ONLY", "Callable[..., Any] | None", "None"),
            ("mcp_sidecar_client", "KEYWORD_ONLY", "Any | None", "None"),
            ("mcp_runtime_state", "KEYWORD_ONLY", "MCPRuntimeState | None", "None"),
            ("master_key_file", "KEYWORD_ONLY", "str | Path | None", "None"),
            ("master_key_bytes", "KEYWORD_ONLY", "bytes | None", "None"),
            ("upload_store", "KEYWORD_ONLY", "InMemoryUploadStore | None", "None"),
            ("conversation_memory_builder", "KEYWORD_ONLY", "ConversationMemoryBuilder | None", "None"),
            ("enable_conversation_memory", "KEYWORD_ONLY", "bool", "True"),
            ("conversation_memory_resolution_generator", "KEYWORD_ONLY", "ResolutionGenerator | None", "None"),
            ("enable_conversation_memory_resolution_llm", "KEYWORD_ONLY", "bool", "True"),
            ("artifact_store_path", "KEYWORD_ONLY", "str | Path | None", "None"),
            ("conversation_file_store_path", "KEYWORD_ONLY", "str | Path | None", "None"),
            ("runtime_sidecar_client", "KEYWORD_ONLY", "Any | None", "None"),
            ("skill_sandbox_client", "KEYWORD_ONLY", "Any | None", "None"),
            ("enable_user_mcp", "KEYWORD_ONLY", "bool | None", "None"),
            ("enable_user_mcp_routing", "KEYWORD_ONLY", "bool | None", "None"),
            ("user_mcp_terminal_result_store_path", "KEYWORD_ONLY", "str | Path | None", "None"),
            ("mcp_legacy_retirement_inventory_id", "KEYWORD_ONLY", "str | None", "None"),
            ("mcp_legacy_retirement_inventory_sha256", "KEYWORD_ONLY", "str | None", "None"),
            ("mcp_cp7_runtime_identity", "KEYWORD_ONLY", "CP7RuntimeIdentity | None", "None"),
            ("mcp_cp7_open_boundary", "KEYWORD_ONLY", "CP7BoundaryEvidence | None", "None"),
            ("mcp_cp7_boundary_provider", "KEYWORD_ONLY", "Callable[[], Any] | None", "None"),
            ("mcp_cp7_fatal_exit", "KEYWORD_ONLY", "Callable[[int], None]", "os._exit"),
            ("mcp_cp7_predecessor_close", "KEYWORD_ONLY", "CP7PredecessorClose | None", "None"),
            ("mcp_cp7_verifier_authorized", "KEYWORD_ONLY", "bool", "False"),
            ("mcp_cp7_maintenance_authorization", "KEYWORD_ONLY", "object | None", "None"),
            ("mcp_cp7_maintenance_authorizer", "KEYWORD_ONLY", "Callable[[object], bool] | None", "None"),
        ],
        "return": "ApiRuntime",
    },
}

EXPECTED_ROUTE_RUNTIME_ATTRIBUTES = {
    "_assistant_history_fallback_metadata",
    "_mcp_projection_store",
    "_utcnow_naive",
    "artifact_file_store",
    "auth_generation_cache",
    "cancel_task",
    "capability_registry",
    "delete_conversation",
    "delete_upload",
    "ensure_upload_allowed",
    "iter_frontend_events",
    "list_interrupts",
    "list_uploads",
    "mcp_result_artifact_projections_for_task",
    "mcp_terminal_projection_for_task",
    "postgres_auth_invalidation_bus",
    "refresh_skills_for_capabilities_list",
    "rename_conversation",
    "save_upload",
    "storage",
    "submit_chat_message",
    "sync_assistant_history_messages",
    "try_sync_assistant_history_message_for_task",
    "upload_store",
    "user_mcp_config_service",
    "user_mcp_gateway",
    "user_mcp_presence_service",
}

EXPECTED_IMPORT_TIME_PROJECT_ENV_KEYS = {
    "MAF_CORE_LIFECYCLE_PYO3_MODULE",
    "MAF_RUST_CORE_MODE",
}


def _annotation_token(value: object) -> str | None:
    if value is inspect.Signature.empty:
        return None
    if isinstance(value, str):
        return value
    return inspect.formatannotation(value)


def _default_token(value: object) -> str:
    if value is inspect.Signature.empty:
        return "__required__"
    if value is os._exit:
        return "os._exit"
    return repr(value)


def _signature_shape(function: object) -> dict[str, object]:
    signature = inspect.signature(function)
    return {
        "parameters": [
            (
                parameter.name,
                parameter.kind.name,
                _annotation_token(parameter.annotation),
                _default_token(parameter.default),
            )
            for parameter in signature.parameters.values()
        ],
        "return": _annotation_token(signature.return_annotation),
    }


def _route_runtime_attribute_names(root: Path) -> set[str]:
    names: set[str] = set()
    for source_path in sorted((root / "src" / "api" / "routes").glob("*.py")):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "runtime"
            ):
                names.add(node.attr)
    return names


def _agent_repository_assignments(root: Path) -> tuple[str, ...]:
    tree = ast.parse((root / "src" / "api" / "runtime.py").read_text(encoding="utf-8"))
    build = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "build_api_runtime"
    )
    assignments: list[tuple[int, str]] = []
    for node in ast.walk(build):
        if not isinstance(node, ast.Assign) or not any(
            isinstance(target, ast.Name) and target.id == "agent_repository"
            for target in node.targets
        ):
            continue
        if not isinstance(node.value, ast.Call) or not isinstance(node.value.func, ast.Name):
            raise AssertionError("agent_repository must be assigned by a direct constructor call")
        assignments.append((node.lineno, node.value.func.id))
    return tuple(name for _line, name in sorted(assignments))


def _api_runtime_init_assignments(root: Path) -> set[str]:
    source_path = root / "src" / "api" / "runtime.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    runtime_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ApiRuntime"
    )
    initializer = next(
        node
        for node in runtime_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "__init__"
    )
    return {
        node.attr
        for node in ast.walk(initializer)
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and isinstance(node.ctx, ast.Store)
        )
    }


class RuntimePublicContractTest(unittest.TestCase):
    def test_api_exports_and_defining_object_identity_are_exact(self) -> None:
        self.assertEqual(api.__all__, ["ApiRuntime", "build_api_runtime", "create_app"])
        self.assertIs(api.ApiRuntime, runtime_module.ApiRuntime)
        self.assertIs(api.build_api_runtime, runtime_module.build_api_runtime)
        self.assertIs(api.create_app, create_app)

    def test_runtime_constructor_and_factory_signatures_are_exact(self) -> None:
        self.assertEqual(_signature_shape(ApiRuntime.__init__), EXPECTED_SIGNATURES["init"])
        self.assertEqual(_signature_shape(build_api_runtime), EXPECTED_SIGNATURES["build"])

    def test_routes_consume_the_exact_runtime_surface(self) -> None:
        root = Path(__file__).resolve().parents[2]
        route_attributes = _route_runtime_attribute_names(root)
        self.assertEqual(route_attributes, EXPECTED_ROUTE_RUNTIME_ATTRIBUTES)

        init_assignments = _api_runtime_init_assignments(root)
        unresolved = sorted(
            name
            for name in route_attributes
            if not hasattr(ApiRuntime, name) and name not in init_assignments
        )
        self.assertEqual(unresolved, [])

    def test_repository_constructor_patch_seams_keep_object_identity(self) -> None:
        self.assertIs(runtime_module.SQLiteAgentRepository, SQLiteAgentRepository)
        self.assertIs(runtime_module.PostgreSQLAgentRepository, PostgreSQLAgentRepository)
        self.assertIs(
            runtime_module.RuntimeSidecarAgentRepository,
            RuntimeSidecarAgentRepository,
        )

    def test_composition_root_is_the_only_agent_repository_selector(self) -> None:
        root = Path(__file__).resolve().parents[2]
        self.assertEqual(
            _agent_repository_assignments(root),
            (
                "PostgreSQLAgentRepository",
                "SQLiteAgentRepository",
                "RuntimeSidecarAgentRepository",
            ),
        )
        concrete_names = {
            "PostgreSQLAgentRepository",
            "RuntimeSidecarAgentRepository",
            "SQLiteAgentRepository",
        }
        import_owners = set()
        for path in (root / "src" / "api").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            if any(
                isinstance(node, ast.ImportFrom)
                and any(alias.name in concrete_names for alias in node.names)
                for node in ast.walk(tree)
            ):
                import_owners.add(path.relative_to(root).as_posix())
        self.assertEqual(import_owners, {"src/api/runtime.py"})

    def test_fresh_api_import_only_reads_existing_core_contract_mode_keys(self) -> None:
        root = Path(__file__).resolve().parents[2]
        script = r"""
import json
import os
from unittest.mock import patch

environment_type = type(os.environ)
original_get = environment_type.get
original_contains = environment_type.__contains__
seen = []
project_prefixes = ("APP_", "MAF_", "MCP_")

def guarded_get(self, key, default=None):
    if str(key).startswith(project_prefixes):
        seen.append(str(key))
    return original_get(self, key, default)

def guarded_contains(self, key):
    if str(key).startswith(project_prefixes):
        seen.append(str(key))
    return original_contains(self, key)

with (
    patch.object(environment_type, "get", guarded_get),
    patch.object(environment_type, "__contains__", guarded_contains),
):
    import src.api

print(json.dumps(sorted(set(seen))))
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            env=dict(os.environ),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            set(json.loads(completed.stdout)),
            EXPECTED_IMPORT_TIME_PROJECT_ENV_KEYS,
        )


if __name__ == "__main__":
    unittest.main()
