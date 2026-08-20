from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from src.capabilities.mcp_dispatch.models import (
    MCPAttachmentSummary,
    MCPBindingMode,
    MCPSelectorContext,
    MCPToolProfile,
    build_mcp_selector_context,
)
from src.core.contracts import StoragePort
from src.core.enums import UserMCPHealthStatus
from src.core.models import MCPCallRecord, MCPTerminalResultReceipt, TaskInputAttachment
from src.integrations.mcp.cp7_artifacts import (
    mcp_durable_result_artifact_id,
    mcp_dispatch_resume_outbox_id,
    mcp_no_server_intent_id,
)
from src.integrations.mcp.result_parsing.projection_store import (
    MCPProjectionBinding,
    MCPProjectionStore,
)
from src.storage.artifact_files import parse_file_storage_ref
from src.integrations.mcp.resume_envelope import (
    MCPDispatchResumeEnvelopeError,
    mcp_dispatch_resume_envelope_version,
    project_mcp_dependency_artifacts,
    validate_mcp_dispatch_resume_envelope_v2,
)
from src.orchestration.models import UserMCPServerProfile


MAX_MCP_SELECTOR_USER_REQUEST_CHARS = 8000
MAX_MCP_SELECTOR_ATTACHMENTS = 20
MAX_MCP_SELECTOR_RESULT_CODE_POINTS = 20_000
MAX_MCP_SELECTOR_RESULT_BYTES = 80_000


class MCPSelectorContextAuthorityError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@runtime_checkable
class MCPCompletedResultProjectionAuthority(Protocol):
    """Load only an identity-bound, published agent projection."""

    async def load_agent_projection(
        self,
        *,
        call: MCPCallRecord,
        receipt: MCPTerminalResultReceipt,
    ) -> str: ...


@dataclass(slots=True)
class MCPPublishedAgentProjectionAuthority:
    storage: StoragePort
    projection_store: MCPProjectionStore

    async def load_agent_projection(
        self,
        *,
        call: MCPCallRecord,
        receipt: MCPTerminalResultReceipt,
    ) -> str:
        if receipt.safe_result_ref is None:
            raise MCPSelectorContextAuthorityError(
                "mcp_selector_context_projection_authority_invalid"
            )
        artifact = await self.storage.get_artifact(
            mcp_durable_result_artifact_id(receipt.safe_result_ref)
        )
        metadata = (
            {} if artifact is None else parse_file_storage_ref(artifact.storage_ref) or {}
        )
        projection_ref = metadata.get("projection_ref")
        projection_sha256 = metadata.get("projection_sha256")
        if (
            artifact is None
            or artifact.task_id != call.task_id
            or artifact.producer_node_id != call.node_id
            or metadata.get("source_kind") != "mcp_result"
            or metadata.get("visibility") != "internal_raw"
            or metadata.get("protocol_version") != call.protocol_version
            or metadata.get("terminal_result_source") != call.terminal_result_source
            or metadata.get("output_schema_sha256") != call.output_schema_sha256
            or metadata.get("parser_revision") != receipt.result_parser_revision
            or metadata.get("call_ref") != call.call_ref
            or metadata.get("owner_user_id") != call.owner_user_id
            or str(metadata.get("sha256") or "")
            != str(receipt.safe_result_content_sha256 or "").removeprefix("sha256:")
            or not isinstance(projection_ref, str)
            or not isinstance(projection_sha256, str)
        ):
            raise MCPSelectorContextAuthorityError(
                "mcp_selector_context_projection_authority_invalid"
            )
        envelope = self.projection_store.load(
            projection_ref,
            binding=MCPProjectionBinding(
                owner_user_id=call.owner_user_id,
                task_id=call.task_id,
                node_id=call.node_id,
                call_ref=call.call_ref,
                raw_sha256=str(receipt.safe_result_content_sha256),
                output_schema_sha256=call.output_schema_sha256,
                source=str(call.terminal_result_source),
                parser_revision=str(receipt.result_parser_revision),
            ),
            expected_projection_sha256=projection_sha256,
        )
        projection = envelope.get("agent_projection")
        if not isinstance(projection, str):
            raise MCPSelectorContextAuthorityError(
                "mcp_selector_context_projection_authority_invalid"
            )
        return projection


@runtime_checkable
class MCPSelectorContextBuilderPort(Protocol):
    async def build(
        self,
        *,
        owner_user_id: str,
        task_id: str,
        node_id: str,
        branch_id: str,
        expected_server_id: str,
        tools: tuple[MCPToolProfile, ...],
    ) -> MCPSelectorContext: ...


@dataclass(slots=True)
class MCPDurableSelectorContextBuilder:
    storage: StoragePort
    projection_authority: MCPCompletedResultProjectionAuthority

    async def build(
        self,
        *,
        owner_user_id: str,
        task_id: str,
        node_id: str,
        branch_id: str,
        expected_server_id: str,
        tools: tuple[MCPToolProfile, ...],
    ) -> MCPSelectorContext:
        task = await self.storage.get_task(task_id)
        root_message = (
            None if task is None else await self.storage.get_message(task.root_message_id)
        )
        branch = await self.storage.get_mcp_branch_record(
            owner_user_id, task_id, branch_id
        )
        intent = await self.storage.get_mcp_no_server_intent(
            mcp_no_server_intent_id(task_id, node_id=node_id)
        )
        if (
            task is None
            or root_message is None
            or branch is None
            or intent is None
            or task.root_message_id != root_message.message_id
            or task.conversation_id != root_message.conversation_id
            or branch.owner_user_id != owner_user_id
            or branch.task_id != task_id
            or branch.node_id != node_id
            or intent.owner_user_id != owner_user_id
            or intent.task_id != task_id
            or intent.node_id != node_id
        ):
            raise MCPSelectorContextAuthorityError(
                "mcp_selector_context_identity_conflict"
            )
        envelope = dict(intent.resume_envelope_json or {})
        try:
            if mcp_dispatch_resume_envelope_version(envelope) != "v2":
                raise MCPSelectorContextAuthorityError(
                    "mcp_selector_context_v2_authority_required"
                )
            validate_mcp_dispatch_resume_envelope_v2(envelope)
        except MCPDispatchResumeEnvelopeError as exc:
            raise MCPSelectorContextAuthorityError(exc.code) from exc
        if (
            envelope["task_id"] != task_id
            or envelope["node_id"] != node_id
            or envelope["conversation_id"] != task.conversation_id
            or envelope["root_message_id"] != task.root_message_id
        ):
            raise MCPSelectorContextAuthorityError(
                "mcp_selector_context_envelope_identity_conflict"
            )
        if (
            branch.initial_server_id is None
            or envelope["server_id"] != branch.initial_server_id
            or intent.requested_server_id != branch.initial_server_id
        ):
            raise MCPSelectorContextAuthorityError(
                "mcp_selector_context_initial_server_conflict"
            )

        binding_mode, bound_server_id, bound_config, bound_security = (
            _binding_from_root_message(root_message.metadata)
        )
        attachments = await self.storage.list_task_input_attachments_for_task(task_id)
        attachment_summaries = _attachment_summaries(
            attachments,
            task_id=task_id,
            conversation_id=task.conversation_id,
            root_message_id=task.root_message_id,
            expected_attachment_ids=envelope["input_attachment_ids"],
        )
        upstream_facts = await self._dependency_facts(
            task_id=task_id,
            node_id=node_id,
            dependency_output_refs=envelope["dependency_output_refs"],
        )

        calls = sorted(
            await self.storage.list_mcp_call_records(
                owner_user_id, task_id, branch_id=branch_id
            ),
            key=lambda call: (call.call_sequence, call.call_ref),
        )
        _validate_calls(calls, owner_user_id, task_id, node_id, branch_id)
        approved_action = await self.storage.get_latest_approved_mcp_tool_action(
            owner_user_id, task_id, node_id
        )
        current_server_id = (
            approved_action.server_id
            if approved_action is not None
            else calls[-1].server_id
            if calls
            else branch.initial_server_id
        )
        if (
            current_server_id is None
            or current_server_id != expected_server_id
            or (bound_server_id is not None and bound_server_id != current_server_id)
        ):
            raise MCPSelectorContextAuthorityError(
                "mcp_selector_context_server_conflict"
            )
        server = await self.storage.get_user_mcp_server(
            owner_user_id, current_server_id
        )
        if (
            server is None
            or not server.enabled
            or server.deletion_pending
            or server.deleted_at is not None
            or server.health_status != UserMCPHealthStatus.AVAILABLE
        ):
            raise MCPSelectorContextAuthorityError(
                "mcp_selector_context_server_unavailable"
            )
        if approved_action is not None and (
            approved_action.server_config_version != server.config_version
            or approved_action.server_security_version != server.security_version
        ):
            raise MCPSelectorContextAuthorityError(
                "mcp_selector_context_action_server_drift"
            )
        if calls and (
            calls[-1].server_config_version != server.config_version
            or calls[-1].server_security_version != server.security_version
        ):
            raise MCPSelectorContextAuthorityError(
                "mcp_selector_context_call_server_drift"
            )
        if bound_server_id is not None and (
            bound_config != server.config_version
            or bound_security != server.security_version
        ):
            raise MCPSelectorContextAuthorityError(
                "mcp_selector_context_binding_server_drift"
            )

        outbox = await self.storage.get_mcp_dispatch_resume_outbox(
            mcp_dispatch_resume_outbox_id(intent.intent_id)
        )
        if (
            outbox is None
            or outbox.intent_id != intent.intent_id
            or outbox.owner_user_id != owner_user_id
            or outbox.task_id != task_id
            or outbox.node_id != node_id
            or outbox.server_id != branch.initial_server_id
            or str(outbox.status) not in {"claimed", "active"}
        ):
            raise MCPSelectorContextAuthorityError(
                "mcp_selector_context_outbox_missing"
            )
        completed_projections, last_receipt_id = await self._completed_result_projections(
            calls, intent.intent_id
        )
        if completed_projections and (
            str(outbox.status) != "active"
            or str(outbox.resume_reason)
            not in {"ordinary_terminal", "remote_terminal"}
            or outbox.resume_receipt_id != last_receipt_id
            or outbox.result_receipt_id != last_receipt_id
        ):
            raise MCPSelectorContextAuthorityError(
                "mcp_selector_context_resume_cursor_conflict"
            )
        if branch.tool_call_count != len(calls):
            raise MCPSelectorContextAuthorityError(
                "mcp_selector_context_call_budget_conflict"
            )
        if (
            branch.active_call_ref is not None
            or branch.max_tool_calls < branch.tool_call_count
        ):
            raise MCPSelectorContextAuthorityError(
                "mcp_selector_context_branch_not_selectable"
            )
        ordered_tools = tuple(sorted(tools, key=lambda tool: tool.name))
        if len({tool.name for tool in ordered_tools}) != len(ordered_tools):
            raise MCPSelectorContextAuthorityError(
                "mcp_selector_context_tool_catalog_conflict"
            )

        return build_mcp_selector_context(
            user_request=_user_request(root_message.content, bool(attachment_summaries)),
            server=UserMCPServerProfile(
                server_id=server.server_id,
                display_name=server.display_name,
                routing_description=server.routing_description,
                transport=str(server.transport),
            ),
            tools=ordered_tools,
            binding_mode=binding_mode,
            attachments=attachment_summaries,
            upstream_facts=upstream_facts,
            completed_result_projections=completed_projections,
            failed_call_fingerprints=frozenset(
                call.arguments_sha256 for call in calls if call.status == "failed"
            ),
            rejected_call_fingerprints=frozenset(
                call.arguments_sha256 for call in calls if call.status == "rejected"
            ),
            remaining_call_budget=max(
                0, branch.max_tool_calls - branch.tool_call_count
            ),
            selector_step_total=outbox.selector_step_total,
            approval_round_total=outbox.approval_round_total,
        )

    async def _completed_result_projections(
        self, calls: Sequence[MCPCallRecord], intent_id: str
    ) -> tuple[tuple[str, ...], str | None]:
        projections: list[tuple[int, str]] = []
        last_receipt_id: str | None = None
        for call in calls:
            if call.status != "completed":
                continue
            receipt = await self.storage.get_mcp_terminal_result_receipt_for_call(
                call.call_ref
            )
            if (
                receipt is None
                or str(receipt.terminal_state) != "completed"
                or str(receipt.completion_mode) != "normal_terminal_projection"
                or receipt.intent_id != intent_id
                or receipt.owner_user_id != call.owner_user_id
                or receipt.task_id != call.task_id
                or receipt.node_id != call.node_id
                or receipt.call_id != call.call_ref
                or receipt.server_id != call.server_id
                or receipt.server_config_version != call.server_config_version
                or receipt.server_security_version != call.server_security_version
                or receipt.safe_result_ref is None
                or receipt.safe_result_ref != call.result_ref
                or receipt.safe_result_content_sha256 is None
                or receipt.safe_result_size_bytes is None
                or receipt.safe_result_store_kind != "durable_content_addressed"
                or receipt.result_parser_revision is None
                or receipt.validated_checkpoint_sha256 is None
                or receipt.parsed_model_sha256 is None
                or call.terminal_result_source is None
            ):
                raise MCPSelectorContextAuthorityError(
                    "mcp_selector_context_receipt_conflict"
                )
            try:
                projection = await self.projection_authority.load_agent_projection(
                    call=call, receipt=receipt
                )
            except Exception as exc:
                raise MCPSelectorContextAuthorityError(
                    "mcp_selector_context_result_authority_conflict"
                ) from exc
            projections.append((call.call_sequence, projection))
            last_receipt_id = receipt.result_receipt_id
        return _budget_agent_projections(projections), last_receipt_id

    async def _dependency_facts(
        self,
        *,
        task_id: str,
        node_id: str,
        dependency_output_refs: Sequence[Mapping[str, object]],
    ) -> tuple[str, ...]:
        edges = await self.storage.list_task_edges(task_id)
        expected_dependencies = sorted(
            {
                edge.from_node_id
                for edge in edges
                if edge.to_node_id == node_id
            }
        )
        refs_by_node = {
            str(item["node_id"]): tuple(str(value) for value in item["artifact_ids"])
            for item in dependency_output_refs
        }
        if sorted(refs_by_node) != expected_dependencies:
            raise MCPSelectorContextAuthorityError(
                "mcp_selector_context_dependency_conflict"
            )
        facts: list[str] = []
        for dependency_node_id in expected_dependencies:
            artifact_ids = refs_by_node[dependency_node_id]
            artifacts = {}
            for artifact_id in artifact_ids:
                artifact = await self.storage.get_artifact(artifact_id)
                if artifact is not None:
                    artifacts[artifact_id] = artifact
            try:
                projection = project_mcp_dependency_artifacts(
                    task_id=task_id,
                    node_id=dependency_node_id,
                    artifact_ids=artifact_ids,
                    artifacts_by_id=artifacts,
                )
            except MCPDispatchResumeEnvelopeError as exc:
                raise MCPSelectorContextAuthorityError(exc.code) from exc
            facts.append(
                f"{dependency_node_id}:safe_summary={projection['safe_summary']}"
            )
        return tuple(facts)


def _binding_from_root_message(
    metadata: Mapping[str, object],
) -> tuple[MCPBindingMode, str | None, int | None, int | None]:
    value = metadata.get("mcp_server_binding_context")
    if value is None:
        if "mcp_server_badge" in metadata:
            raise MCPSelectorContextAuthorityError(
                "mcp_selector_context_binding_missing"
            )
        return MCPBindingMode.AUTOMATIC, None, None, None
    if not isinstance(value, Mapping) or set(value) != {
        "server_id",
        "server_config_version",
        "server_security_version",
        "binding_mode",
    }:
        raise MCPSelectorContextAuthorityError(
            "mcp_selector_context_binding_invalid"
        )
    server_id = value.get("server_id")
    config_version = value.get("server_config_version")
    security_version = value.get("server_security_version")
    if (
        value.get("binding_mode") != "explicit_command"
        or not isinstance(server_id, str)
        or not server_id
        or isinstance(config_version, bool)
        or not isinstance(config_version, int)
        or config_version < 1
        or isinstance(security_version, bool)
        or not isinstance(security_version, int)
        or security_version < 1
    ):
        raise MCPSelectorContextAuthorityError(
            "mcp_selector_context_binding_invalid"
        )
    return (
        MCPBindingMode.EXPLICIT_COMMAND,
        server_id,
        config_version,
        security_version,
    )


def _attachment_summaries(
    attachments: Sequence[TaskInputAttachment],
    *,
    task_id: str,
    conversation_id: str,
    root_message_id: str,
    expected_attachment_ids: Sequence[str],
) -> tuple[MCPAttachmentSummary, ...]:
    ordered = sorted(attachments, key=lambda item: item.attachment_id)
    if (
        [item.attachment_id for item in ordered] != list(expected_attachment_ids)
        or len(attachments) > MAX_MCP_SELECTOR_ATTACHMENTS
        or any(
            item.task_id != task_id or item.conversation_id != conversation_id
            for item in attachments
        )
    ):
        raise MCPSelectorContextAuthorityError(
            "mcp_selector_context_attachment_conflict"
        )
    selected = [
        item
        for item in ordered
        if item.source_kind == "message_upload"
        and item.source_message_id == root_message_id
    ]
    return tuple(
        MCPAttachmentSummary(
            basename=_safe_attachment_basename(item.filename),
            content_type=_safe_attachment_content_type(item.content_type),
            size_bytes=max(0, int(item.size_bytes or 0)),
        )
        for item in selected
    )


def _validate_calls(
    calls: Sequence[MCPCallRecord],
    owner_user_id: str,
    task_id: str,
    node_id: str,
    branch_id: str,
) -> None:
    sequences = [call.call_sequence for call in calls]
    if (
        sequences != list(range(1, len(calls) + 1))
        or any(
            call.owner_user_id != owner_user_id
            or call.task_id != task_id
            or call.node_id != node_id
            or call.branch_id != branch_id
            for call in calls
        )
    ):
        raise MCPSelectorContextAuthorityError(
            "mcp_selector_context_call_identity_conflict"
        )


def _user_request(content: str, has_attachments: bool) -> str:
    normalized = str(content or "").strip()
    if normalized:
        return normalized[:MAX_MCP_SELECTOR_USER_REQUEST_CHARS]
    if has_attachments:
        return "处理本消息附带的文件"
    return "Complete the user's request using the selected MCP server."


def _safe_attachment_basename(value: object) -> str:
    normalized = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    normalized = "".join(
        char
        for char in normalized
        if not (ord(char) < 32 or 127 <= ord(char) <= 159)
    ).strip()
    return _truncate_utf8(normalized or "attachment", 255)


def _safe_attachment_content_type(value: object) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized
        or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in normalized)
        or len(normalized.encode("utf-8")) > 255
    ):
        return "application/octet-stream"
    return normalized


def _truncate_utf8(value: str, limit: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode("utf-8", errors="ignore")


def _budget_agent_projections(
    projections: Sequence[tuple[int, str]],
) -> tuple[str, ...]:
    remaining_code_points = MAX_MCP_SELECTOR_RESULT_CODE_POINTS
    remaining_bytes = MAX_MCP_SELECTOR_RESULT_BYTES
    selected: list[tuple[int, str]] = []
    for sequence, projection in reversed(projections):
        if remaining_code_points <= 0 or remaining_bytes <= 0:
            break
        bounded = projection[:remaining_code_points]
        bounded = _truncate_utf8(bounded, remaining_bytes)
        if not bounded:
            continue
        selected.append((sequence, bounded))
        remaining_code_points -= len(bounded)
        remaining_bytes -= len(bounded.encode("utf-8"))
    return tuple(value for _, value in sorted(selected))


__all__ = [
    "MCPCompletedResultProjectionAuthority",
    "MCPDurableSelectorContextBuilder",
    "MCPPublishedAgentProjectionAuthority",
    "MCPSelectorContextAuthorityError",
    "MCPSelectorContextBuilderPort",
]
