from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from src.core.enums import NodeStatus
from src.core.models import Task, TaskNode
from src.orchestration.agent_loop.invocation import (
    CapabilityInvocationService,
    InvocationRequest,
)
from src.orchestration.agent_loop.continuation import AgentContinuationLocator
from src.orchestration.agent_loop.lease import AgentLeaseHandle

from .models import (
    AgentCallOutcomeStatus,
    AgentItemKind,
    AgentItemState,
    AgentRun,
    AgentStagedArtifact,
    AgentStorageConflict,
    AgentToolCall,
)
from .repository import AgentRunRepository
from .runner import AgentCallExecution


@dataclass(slots=True)
class AgentInvocationContextStore:
    _metadata: dict[str, dict[str, Any]] = field(default_factory=dict)
    _user_input: dict[str, str] = field(default_factory=dict)

    def register(
        self,
        run_id: str,
        *,
        metadata: Mapping[str, Any],
        current_user_input: str,
    ) -> None:
        self._metadata[run_id] = dict(metadata)
        self._user_input[run_id] = current_user_input

    def request_metadata(self, run: AgentRun) -> Mapping[str, Any]:
        return dict(self._metadata.get(run.run_id, {}))

    def current_user_input(self, run: AgentRun) -> str:
        return self._user_input.get(run.run_id, "")

    def release(self, run_id: str) -> None:
        self._metadata.pop(run_id, None)
        self._user_input.pop(run_id, None)

    def merge(
        self,
        run_id: str,
        *,
        metadata: Mapping[str, Any] | None = None,
        current_user_input: str | None = None,
    ) -> None:
        self._metadata.setdefault(run_id, {}).update(dict(metadata or {}))
        if current_user_input is not None:
            self._user_input[run_id] = current_user_input


class AgentCapabilityInvoker:
    """Adapt one durable Agent call reservation to the shared invocation kernel."""

    def __init__(
        self,
        *,
        invocation_service: CapabilityInvocationService,
        runs: AgentRunRepository,
        task_loader: Callable[[str], Any],
        node_loader: Callable[[str], Any],
        request_metadata_loader: Callable[[AgentRun], Mapping[str, Any]],
        current_user_input_loader: Callable[[AgentRun], Any],
        continuation_loader: Callable[[str], Mapping[str, Any] | None] | None = None,
        delegated_skill_activator: Callable[
            [AgentRun, str, Mapping[str, Any]], Any
        ]
        | None = None,
        invocation_hook: Callable[..., Any] | None = None,
    ) -> None:
        self._invocation = invocation_service
        self._runs = runs
        self._load_task = task_loader
        self._load_node = node_loader
        self._load_metadata = request_metadata_loader
        self._load_user_input = current_user_input_loader
        self._load_continuation = continuation_loader
        self._activate_delegated_skill = delegated_skill_activator
        self._invocation_hook = invocation_hook

    async def resume(
        self,
        locator: AgentContinuationLocator,
        *,
        cancellation=None,
    ) -> AgentCallExecution:
        run = await self._runs.get_run(locator.run_id)
        if run is None:
            raise AgentStorageConflict("agent_continuation_run_missing")
        items = await self._runs.list_items(run.run_id)
        call_item = next(
            (
                item
                for item in items
                if item.item_id == locator.call_item_id
                and item.kind is AgentItemKind.TOOL_CALL
            ),
            None,
        )
        reservation = next(
            (
                item
                for item in items
                if item.source_call_item_id == locator.call_item_id
                and item.kind is AgentItemKind.TOOL_RESULT
                and item.state is AgentItemState.RESERVED
            ),
            None,
        )
        if call_item is None or reservation is None:
            raise AgentStorageConflict("agent_continuation_call_result_missing")
        import json

        payload = json.loads(call_item.payload_json)
        arguments_json = str(payload.get("arguments_json") or "{}")
        arguments = json.loads(arguments_json)
        if not isinstance(arguments, Mapping):
            raise AgentStorageConflict("agent_continuation_arguments_invalid")
        capability_id = str(payload.get("capability_id") or "")
        metadata = dict(self._load_metadata(run))
        effective_payload = dict(arguments)
        if capability_id == "mcp.dispatch":
            server_id = str(metadata.get("mcp_dispatch_server_id") or "").strip()
            if server_id:
                effective_payload = {"server_id": server_id}
        call = AgentToolCall(
            call_id=str(payload.get("call_id") or locator.provider_call_id),
            provider_safe_name=str(payload.get("provider_safe_name") or ""),
            arguments_json=arguments_json,
            ordinal=call_item.call_ordinal or 0,
        )
        return await self.invoke(
            run=run,
            call=call,
            call_item=call_item,
            result_reservation=reservation,
            capability_id=capability_id,
            effective_payload=effective_payload,
            cancellation=cancellation,
            lease_handle=None,
        )

    async def invoke(
        self,
        *,
        run: AgentRun,
        call,
        call_item,
        result_reservation,
        capability_id: str,
        effective_payload: Mapping[str, Any],
        cancellation,
        lease_handle: AgentLeaseHandle | None = None,
    ) -> AgentCallExecution:
        del call, result_reservation
        task = await self._load_task(run.task_id)
        node = await self._load_node(_node_id(call_item))
        if not isinstance(task, Task) or not isinstance(node, TaskNode):
            return AgentCallExecution(
                AgentCallOutcomeStatus.ABORTED,
                safe_error_code="agent_invocation_projection_missing",
            )
        metadata = dict(self._load_metadata(run))
        metadata["agent_run_id"] = run.run_id
        if self._activate_delegated_skill is not None and capability_id.startswith(
            "skill."
        ):
            activated = self._activate_delegated_skill(run, capability_id, metadata)
            if hasattr(activated, "__await__"):
                activated = await activated
            if activated is not None:
                return activated
        user_input = self._load_user_input(run)
        if hasattr(user_input, "__await__"):
            user_input = await user_input
        user_text = str(user_input or "").strip()
        node_metadata = {
            "user_message": user_text,
            **({"skill_bundle_revision": metadata["skill_bundle_revision"]}
               if metadata.get("skill_bundle_revision") else {}),
        }
        input_payload = dict(effective_payload)
        if capability_id.startswith("skill.") and user_text:
            input_payload.setdefault("query", user_text)
        hook_handle = None
        if self._invocation_hook is not None:
            try:
                hook_handle = self._invocation_hook(
                    phase="begin",
                    run=run,
                    capability_id=capability_id,
                    task=task,
                    node=node,
                    metadata=metadata,
                    effective_payload=effective_payload,
                )
                if hasattr(hook_handle, "__await__"):
                    hook_handle = await hook_handle
            except Exception:
                hook_handle = None
        invocation_request = InvocationRequest(
            capability_id=capability_id,
            conversation_id=run.conversation_id,
            task_id=run.task_id,
            node_id=node.node_id,
            run_id=run.run_id,
            call_item_id=call_item.item_id,
            expected_revision=run.revision,
            expected_claim_token=run.claim_token,
            model_binding=run.binding,
            cancellation=cancellation,
            input_payload=input_payload,
            request_metadata=metadata,
            node_metadata=node_metadata,
            available_server_ids=frozenset(
                str(value)
                for value in metadata.get("available_mcp_server_ids", ())
            ),
            pinned_server_id_present=(capability_id == "mcp.dispatch"),
            pinned_server_id=effective_payload.get("server_id"),
        )

        async def ownership_boundary(request, operation):
            assert lease_handle is not None

            async def run_with_current(lease):
                return await operation(
                    replace(
                        request,
                        expected_revision=lease.revision,
                        expected_claim_token=lease.token,
                    )
                )

            return await lease_handle.run_ownership_bound(run_with_current)

        result = await self._invocation.invoke(
            invocation_request,
            node,
            ownership_boundary=(
                ownership_boundary if lease_handle is not None else None
            ),
        )
        if self._invocation_hook is not None and hook_handle is not None:
            try:
                finished = self._invocation_hook(
                    phase="finish",
                    handle=hook_handle,
                    result=result,
                )
                if hasattr(finished, "__await__"):
                    await finished
            except Exception:
                pass
        execution = result.execution_result
        if execution is None:
            return AgentCallExecution(
                AgentCallOutcomeStatus.FAILED,
                safe_error_code="agent_route_rejected",
            )
        artifacts = tuple(
            AgentStagedArtifact(
                artifact_id=artifact.artifact_id,
                artifact_type=str(artifact.artifact_type),
                storage_ref=artifact.storage_ref,
                summary=artifact.summary,
            )
            for artifact in execution.artifacts
        )
        if result.node.status is NodeStatus.COMPLETED:
            status = AgentCallOutcomeStatus.COMPLETED
        elif result.node.status is NodeStatus.WAITING_FOR_INPUT:
            status = AgentCallOutcomeStatus.WAITING_FOR_INPUT
        elif result.node.status is NodeStatus.WAITING_FOR_DEPENDENCY:
            status = AgentCallOutcomeStatus.WAITING_FOR_DEPENDENCY
        elif result.node.status in {
            NodeStatus.CANCELLED,
            NodeStatus.BLOCKED_BY_CANCELLATION,
        }:
            status = AgentCallOutcomeStatus.ABORTED
        else:
            status = AgentCallOutcomeStatus.FAILED
        safe_result_payload = dict(result.output_payload)
        if status in {
            AgentCallOutcomeStatus.WAITING_FOR_INPUT,
            AgentCallOutcomeStatus.WAITING_FOR_DEPENDENCY,
        } and self._load_continuation is not None:
            locator = self._load_continuation(call_item.item_id)
            if locator is None:
                return AgentCallExecution(
                    AgentCallOutcomeStatus.ABORTED,
                    safe_error_code="agent_continuation_locator_missing",
                )
            safe_result_payload["continuation_locator"] = dict(locator)
        return AgentCallExecution(
            status,
            safe_result_payload=safe_result_payload,
            staged_artifacts=artifacts,
            safe_error_code=(
                execution.error.code if execution.error is not None else None
            ),
        )


def _node_id(call_item) -> str:
    import json

    payload = json.loads(call_item.payload_json)
    return str(payload.get("node_id") or "")
