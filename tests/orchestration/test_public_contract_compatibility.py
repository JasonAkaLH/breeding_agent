from __future__ import annotations

import pickle
import unittest

import src.capabilities.main_agent as main_agent
from src.capabilities.main_agent import helpers as main_agent_helpers
from src.capabilities.main_agent import prompt_envelope_builder, skill_output_artifacts
import src.orchestration.agent_loop as agent_loop
from src.core.models import TaskNode
from src.orchestration.agent_loop import continuation, invocation, models


EXPECTED_MAIN_AGENT_EXPORTS = [
    "LiveEventRecorder",
    "SkillOutputArtifactManager",
    "StreamGenerator",
    "build_main_agent_rendered_prompt",
    "resolve_main_agent_prompt_envelope_mode",
]

EXPECTED_AGENT_LOOP_EXPORTS = [
    "AgentCancellationToken",
    "AgentFinishMetadata",
    "AgentFinalOutputCommit",
    "AgentFinalOutputResult",
    "AgentItem",
    "AgentItemKind",
    "AgentItemState",
    "AgentLeaseLost",
    "AgentMessage",
    "AgentModelBinding",
    "AgentModelPort",
    "AgentModelRequest",
    "AgentProtocolErrorCode",
    "AgentProtocolFailure",
    "AgentProtocolRetryPolicy",
    "AgentRun",
    "AgentRunStatus",
    "AgentTaskLease",
    "AgentSample",
    "AgentSamplingCancelled",
    "AgentToolCall",
    "AgentToolChoice",
    "AgentToolDescriptor",
    "AgentUsage",
    "AgentCallOutcomeCommit",
    "AgentCallOutcomeStatus",
    "AgentCompactionCommit",
    "AgentCompactionResult",
    "AgentSampleCommit",
    "AgentSampleCommitResult",
    "AgentUserMessageCommit",
    "AgentUserMessageCommitResult",
    "AgentStagedArtifact",
    "AgentStorageConflict",
    "CapabilityInvocationService",
    "InvocationCommitPort",
    "InvocationRequest",
    "InvocationResult",
    "AgentCatalogPreflight",
    "AgentToolCatalog",
    "AgentToolCatalogBuilder",
    "CapabilityInvocationPolicy",
    "CapabilityVisibilityContext",
    "CatalogPreflightDecision",
    "CatalogPreflightResult",
    "default_agent_invocation_policy",
    "DelegatedSkillActivation",
    "DelegatedSkillActivationService",
    "SkillActivationCommitPort",
    "CanonicalSkillActivation",
    "build_canonical_skill_activation",
    "build_delegated_skill_instruction_result",
    "build_skill_activation_item",
    "RunBoundMCPTextGenerator",
    "AgentCallResultProjection",
    "AgentCallResultProjector",
    "build_model_result_envelope",
    "skill_result_artifact_id",
    "AgentSkillResultArtifactStager",
    "AgentSkillResultArtifactJanitor",
    "AGENT_TRANSIENT_SKILL_RESULT_MANIFEST_SCHEMA",
    "AGENT_TRANSIENT_SKILL_RESULT_PROJECTION_REVISION",
    "AGENT_TRANSIENT_SKILL_RESULT_SOURCE_KIND",
    "AgentTransientSkillResultStage",
    "AgentTransientSkillResultResolver",
    "AgentTransientSkillResultStore",
    "transient_skill_result_stage_ref",
    "build_agent_terminal_event",
    "AgentContextBuilder",
    "AgentContextRules",
    "AgentContextBudget",
    "AGENT_CONTEXT_BUDGET_POLICY_REVISION",
    "AGENT_CONTEXT_COMPACT_THRESHOLD_PERCENT",
    "AgentContextCandidate",
    "AgentContextCandidateBuilder",
    "AgentContextPreflightDecision",
    "AgentContextPreflightResult",
    "AgentCallExecution",
    "AgentCallInvoker",
    "AgentLoopRunResult",
    "AgentLoopRunner",
    "AgentCompactionOutcome",
    "AgentCompactionService",
    "AgentFinalOutputPublisher",
    "AgentContinuationLocator",
    "AgentContinuationLocatorService",
    "AgentResumeKind",
    "AgentExecutionRequest",
    "AgentLoopOrchestrator",
    "AgentOrchestrationResult",
    "initial_required_tool_name",
]

EXPECTED_PUBLIC_TYPE_MODULES = {
    "AgentRun": "src.orchestration.agent_loop.models",
    "AgentItem": "src.orchestration.agent_loop.models",
    "AgentModelBinding": "src.orchestration.agent_loop.models",
    "AgentCallOutcomeCommit": "src.orchestration.agent_loop.models",
    "AgentContinuationLocator": "src.orchestration.agent_loop.continuation",
    "AgentResumeKind": "src.orchestration.agent_loop.continuation",
    "InvocationRequest": "src.orchestration.agent_loop.invocation",
    "InvocationResult": "src.orchestration.agent_loop.invocation",
}


class PublicContractCompatibilityTest(unittest.TestCase):
    def test_main_agent_exports_are_exact_and_identity_preserving(self) -> None:
        self.assertEqual(main_agent.__all__, EXPECTED_MAIN_AGENT_EXPORTS)
        defining_objects = {
            "LiveEventRecorder": main_agent_helpers.LiveEventRecorder,
            "SkillOutputArtifactManager": skill_output_artifacts.SkillOutputArtifactManager,
            "StreamGenerator": main_agent_helpers.StreamGenerator,
            "build_main_agent_rendered_prompt": prompt_envelope_builder.build_main_agent_rendered_prompt,
            "resolve_main_agent_prompt_envelope_mode": (
                prompt_envelope_builder.resolve_main_agent_prompt_envelope_mode
            ),
        }
        expected_modules = {
            "LiveEventRecorder": "collections.abc",
            "SkillOutputArtifactManager": "src.capabilities.main_agent.skill_output_artifacts",
            "StreamGenerator": "collections.abc",
            "build_main_agent_rendered_prompt": "src.capabilities.main_agent.prompt_envelope_builder",
            "resolve_main_agent_prompt_envelope_mode": "src.capabilities.main_agent.prompt_envelope_builder",
        }
        for name, expected in defining_objects.items():
            self.assertIs(getattr(main_agent, name), expected)
            self.assertEqual(expected.__module__, expected_modules[name])

    def test_agent_loop_exports_and_defining_object_identity_are_exact(self) -> None:
        self.assertEqual(agent_loop.__all__, EXPECTED_AGENT_LOOP_EXPORTS)
        defining_objects = {
            "AgentRun": models.AgentRun,
            "AgentItem": models.AgentItem,
            "AgentModelBinding": models.AgentModelBinding,
            "AgentCallOutcomeCommit": models.AgentCallOutcomeCommit,
            "AgentContinuationLocator": continuation.AgentContinuationLocator,
            "AgentResumeKind": continuation.AgentResumeKind,
            "InvocationRequest": invocation.InvocationRequest,
            "InvocationResult": invocation.InvocationResult,
        }
        for name, expected in defining_objects.items():
            self.assertIs(getattr(agent_loop, name), expected)
            self.assertEqual(expected.__module__, EXPECTED_PUBLIC_TYPE_MODULES[name])

    def test_current_picklable_public_records_round_trip_with_class_identity(self) -> None:
        samples = (
            agent_loop.AgentItem(
                item_id="item-1",
                run_id="run-1",
                task_id="task-1",
                sequence=1,
                kind=agent_loop.AgentItemKind.USER_MESSAGE,
                state=agent_loop.AgentItemState.COMMITTED,
                payload_json="{}",
                payload_sha256="sha",
            ),
            agent_loop.AgentCallOutcomeCommit(
                run_id="run-1",
                expected_revision=0,
                expected_claim_token=None,
                call_item_id="call-1",
                safe_result_payload={},
                status=agent_loop.AgentCallOutcomeStatus.COMPLETED,
            ),
            agent_loop.InvocationRequest(
                capability_id="main_agent.respond",
                conversation_id="conv-1",
                task_id="task-1",
                node_id="node-1",
            ),
            agent_loop.InvocationResult(
                node=TaskNode(
                    node_id="node-1",
                    task_id="task-1",
                    capability_id="main_agent.respond",
                ),
                output_payload={},
            ),
            agent_loop.AgentResumeKind.SKILL_INPUT,
        )
        for sample in samples:
            restored = pickle.loads(pickle.dumps(sample))
            self.assertIs(type(restored), type(sample))
            self.assertEqual(restored, sample)

    def test_mapping_proxy_records_keep_current_non_picklable_boundary(self) -> None:
        binding = agent_loop.AgentModelBinding("model-edition")
        run = agent_loop.AgentRun(
            "run-1",
            "task-1",
            "conv-1",
            agent_loop.AgentRunStatus.RUNNING,
            binding,
        )
        locator = agent_loop.AgentContinuationLocator(
            "run-1",
            "sample-1",
            "call-1",
            "provider-call-1",
            "skill.example",
            "task-1",
            "node-1",
            "owner-1",
            "conv-1",
            agent_loop.AgentResumeKind.SKILL_INPUT,
            "authority-sha",
            None,
            binding,
        )
        for sample in (binding, run, locator):
            with self.assertRaisesRegex(TypeError, "mappingproxy"):
                pickle.dumps(sample)

    def test_public_agent_errors_keep_class_code_and_message_contracts(self) -> None:
        failure = agent_loop.AgentProtocolFailure(
            agent_loop.AgentProtocolErrorCode.MISSING_CALL_ID,
            attempts=2,
        )
        self.assertIs(type(failure), models.AgentProtocolFailure)
        self.assertEqual(failure.code, agent_loop.AgentProtocolErrorCode.MISSING_CALL_ID)
        self.assertEqual(failure.attempts, 2)
        self.assertEqual(
            str(failure),
            "Agent model protocol failed after 2 attempt(s): missing_call_id",
        )

        lease_lost = agent_loop.AgentLeaseLost("agent_lease_lost")
        storage_conflict = agent_loop.AgentStorageConflict("agent_storage_conflict")
        self.assertIs(type(lease_lost), models.AgentLeaseLost)
        self.assertIs(type(storage_conflict), models.AgentStorageConflict)
        self.assertEqual(str(lease_lost), "agent_lease_lost")
        self.assertEqual(str(storage_conflict), "agent_storage_conflict")


if __name__ == "__main__":
    unittest.main()
