from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse
from sse_starlette.sse import EventSourceResponse

from src.core.enums import ArtifactType, EventVisibility, NodeStatus, TaskStatus
from src.core.models import Artifact, EventRecord
from src.lifecycle.mcp_presence import MCPPresenceConnection
from src.integrations.mcp.gateway_models import MCPCancelStatus, MCPContinueStatus
from src.storage.artifact_files import (
    is_active_skill_output_file,
    parse_file_storage_ref,
)

from ..artifact_responses import artifact_response, should_return_task_artifact
from ..auth import get_optional_owned_conversation, require_authenticated_user, require_task_owner
from ..dto import (
    CancelTaskRequest,
    CancelTaskResponse,
    InterruptResponse,
    TaskArtifactsResponse,
    TaskGraphResponse,
    TaskInterruptsResponse,
    MCPCallControlResponse,
    TaskListResponse,
    TaskNodeResponse,
    TaskSummaryResponse,
)
from ..runtime import ApiRuntime
from ..runtime_access import runtime_from_request as _runtime
from ..sse import encode_sse_event

router = APIRouter()
SSE_AUTH_REVALIDATION_INTERVAL_SECONDS = 15.0


@dataclass(frozen=True, slots=True)
class SseConnectionContext:
    username: str
    conversation_id: str
    task_id: str
    auth_generation_at_connect: int
    connected_at: datetime
    connection_id: str


UNFINISHED_TASK_STATUSES = {
    TaskStatus.ACCEPTED,
    TaskStatus.PLANNING,
    TaskStatus.RUNNING,
    TaskStatus.CANCELLING,
}


def _count_active_nodes(nodes) -> int:
    active_statuses = {
        NodeStatus.PENDING,
        NodeStatus.READY,
        NodeStatus.RUNNING,
        NodeStatus.WAITING_FOR_DEPENDENCY,
        NodeStatus.WAITING_FOR_INPUT,
        NodeStatus.READY_TO_RESUME,
        NodeStatus.RESUMING,
        NodeStatus.CANCELLING,
    }
    return sum(node.status in active_statuses for node in nodes)


def _count_failed_nodes(nodes) -> int:
    failed_statuses = {
        NodeStatus.FAILED,
        NodeStatus.CANCELLED,
        NodeStatus.BLOCKED_BY_CANCELLATION,
        NodeStatus.ORPHANED,
    }
    return sum(node.status in failed_statuses for node in nodes)


async def _build_task_summary(runtime: ApiRuntime, task) -> TaskSummaryResponse:
    agent_projection = getattr(runtime, "agent_task_projection", None)
    if agent_projection is not None:
        await agent_projection.get_agent_run(task.task_id)
    if task.status == TaskStatus.COMPLETED:
        await runtime.try_sync_assistant_history_message_for_task(task.task_id, task.conversation_id)
    nodes = await runtime.storage.list_task_nodes_for_task(task.task_id)
    return TaskSummaryResponse(
        task_id=task.task_id,
        conversation_id=task.conversation_id,
        status=str(task.status),
        root_node_id=None,
        summary=task.summary,
        requested_capability_id=task.requested_capability_id,
        active_node_count=_count_active_nodes(nodes),
        completed_node_count=sum(node.status == NodeStatus.COMPLETED for node in nodes),
        failed_node_count=_count_failed_nodes(nodes),
        cancel_requested=task.cancel_requested_at is not None or task.status in {TaskStatus.CANCELLING, TaskStatus.CANCELLED},
        created_at=task.created_at,
        updated_at=task.updated_at,
        mcp_terminal_projection=await runtime.mcp_terminal_projection_for_task(task),
        mcp_result_artifact_projections=(
            await runtime.mcp_result_artifact_projections_for_task(task.task_id)
        ),
    )


@router.get("/api/v1/conversations/{conversation_id}/tasks", response_model=TaskListResponse)
async def list_conversation_tasks(conversation_id: str, request: Request, scope: str = "unfinished") -> TaskListResponse:
    if scope not in {"unfinished", "all"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unsupported task list scope: {scope}")
    runtime = _runtime(request)
    user = await require_authenticated_user(request)
    conversation = await get_optional_owned_conversation(runtime, conversation_id, user)
    if conversation is None:
        return TaskListResponse(conversation_id=conversation_id, tasks=[])
    tasks = await runtime.storage.list_tasks_for_conversation(
        conversation_id,
        statuses=UNFINISHED_TASK_STATUSES if scope == "unfinished" else None,
    )
    return TaskListResponse(
        conversation_id=conversation_id,
        tasks=[await _build_task_summary(runtime, task) for task in tasks],
    )


@router.get("/api/v1/tasks/{task_id}", response_model=TaskSummaryResponse)
async def get_task(task_id: str, request: Request) -> TaskSummaryResponse:
    runtime = _runtime(request)
    user = await require_authenticated_user(request)
    task = await require_task_owner(runtime, task_id, user)
    return await _build_task_summary(runtime, task)


@router.get("/api/v1/tasks/{task_id}/events")
async def stream_task_events(task_id: str, request: Request) -> EventSourceResponse:
    runtime = _runtime(request)
    user = await require_authenticated_user(request)
    task = await require_task_owner(runtime, task_id, user)
    runtime.auth_generation_cache.apply(user.username, user.auth_generation, updated_at=runtime._utcnow_naive())
    context = SseConnectionContext(
        username=user.username,
        conversation_id=task.conversation_id,
        task_id=task_id,
        auth_generation_at_connect=user.auth_generation,
        connected_at=runtime._utcnow_naive(),
        connection_id=f"sse-{uuid4().hex[:12]}",
    )

    async def _event_stream():
        async for event in _iter_authorized_frontend_events(runtime, context, request):
            yield encode_sse_event(event)

    return EventSourceResponse(_event_stream())


async def _iter_authorized_frontend_events(
    runtime: ApiRuntime,
    context: SseConnectionContext | str,
    request: Request | None = None,
    username: str | None = None,
    *,
    revalidation_interval_seconds: float = SSE_AUTH_REVALIDATION_INTERVAL_SECONDS,
):
    # Backward-compatible call shape for older unit tests: (runtime, task_id, request, username).
    if isinstance(context, str):
        if username is None:
            raise ValueError("username is required when context is a task_id")
        cached_generation = runtime.auth_generation_cache.get(username)
        context = SseConnectionContext(
            username=username,
            conversation_id="",
            task_id=context,
            auth_generation_at_connect=cached_generation.auth_generation if cached_generation is not None else 0,
            connected_at=runtime._utcnow_naive(),
            connection_id=f"sse-{uuid4().hex[:12]}",
        )
    event_iterator = runtime.iter_frontend_events(context.task_id).__aiter__()
    pending_next = asyncio.create_task(event_iterator.__anext__())
    presence_service = runtime.user_mcp_presence_service
    if presence_service is not None:
        await presence_service.connect(
            MCPPresenceConnection(
                connection_id=context.connection_id,
                task_id=context.task_id,
                owner_user_id=context.username,
                auth_generation=context.auth_generation_at_connect,
            )
        )
    try:
        while True:
            done, _pending = await asyncio.wait(
                {pending_next},
                timeout=revalidation_interval_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            postgres_auth_bus = runtime.postgres_auth_invalidation_bus
            if postgres_auth_bus is not None and not postgres_auth_bus.health.ready:
                if presence_service is not None:
                    await presence_service.invalidate_owner(
                        context.username,
                        auth_generation=context.auth_generation_at_connect,
                        reason="auth_generation_unavailable",
                    )
                yield _auth_invalidated_event(
                    runtime,
                    context,
                    reason="auth_generation_unavailable",
                    current_auth_generation=None,
                )
                return
            auth_check = runtime.auth_generation_cache.is_current(context.username, context.auth_generation_at_connect)
            if not auth_check.current:
                if presence_service is not None:
                    await presence_service.invalidate_owner(
                        context.username,
                        auth_generation=context.auth_generation_at_connect,
                        reason=(
                            "auth_generation_unknown"
                            if not auth_check.known
                            else "auth_generation_mismatch"
                        ),
                    )
                yield _auth_invalidated_event(
                    runtime,
                    context,
                    reason="auth_generation_unknown" if not auth_check.known else "auth_generation_mismatch",
                    current_auth_generation=auth_check.current_generation,
                )
                return
            if presence_service is not None:
                await presence_service.heartbeat(context.connection_id)
            if not done:
                continue
            try:
                event = pending_next.result()
            except StopAsyncIteration:
                return
            yield event
            pending_next = asyncio.create_task(event_iterator.__anext__())
    finally:
        if presence_service is not None:
            await presence_service.disconnect(context.connection_id)
        if not pending_next.done():
            pending_next.cancel()
            with suppress(asyncio.CancelledError):
                await pending_next
        aclose = getattr(event_iterator, "aclose", None)
        if callable(aclose):
            with suppress(RuntimeError):
                await aclose()


def _auth_invalidated_event(
    runtime: ApiRuntime,
    context: SseConnectionContext,
    *,
    reason: str,
    current_auth_generation: int | None,
) -> EventRecord:
    return EventRecord(
        event_id=f"evt-{uuid4().hex[:12]}",
        conversation_id=context.conversation_id,
        task_id=context.task_id,
        event_type="auth.invalidated",
        payload={
            "reason": reason,
            "connection_id": context.connection_id,
            "auth_generation_at_connect": context.auth_generation_at_connect,
            "current_auth_generation": current_auth_generation,
            "occurred_at": runtime._utcnow_naive().isoformat(),
        },
        visibility=EventVisibility.FRONTEND,
        created_at=runtime._utcnow_naive(),
    )


@router.post("/api/v1/tasks/cancel", response_model=CancelTaskResponse, status_code=status.HTTP_202_ACCEPTED)
async def cancel_task(body: CancelTaskRequest, request: Request) -> CancelTaskResponse:
    runtime = _runtime(request)
    user = await require_authenticated_user(request)
    task_id = body.task_id
    await require_task_owner(runtime, task_id, user)
    try:
        task = await runtime.cancel_task(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return CancelTaskResponse(task_id=task.task_id, status=str(task.status), accepted=True)


@router.post(
    "/api/v1/tasks/{task_id}/mcp-calls/{call_ref}/continue",
    response_model=MCPCallControlResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def continue_mcp_call(
    task_id: str,
    call_ref: str,
    request: Request,
) -> MCPCallControlResponse:
    runtime = _runtime(request)
    user = await require_authenticated_user(request)
    await require_task_owner(runtime, task_id, user)
    if runtime.user_mcp_gateway is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "mcp_feature_unavailable"},
        )
    outcome = await runtime.user_mcp_gateway.continue_call_for_task(task_id, call_ref)
    if str(outcome.status) == str(MCPContinueStatus.UNKNOWN_CALL):
        raise HTTPException(status_code=404, detail={"code": "mcp_call_not_found"})
    if str(outcome.status) == str(MCPContinueStatus.ALREADY_TERMINAL):
        raise HTTPException(status_code=409, detail={"code": "mcp_call_already_terminal"})
    return MCPCallControlResponse(
        task_id=task_id,
        call_ref=call_ref,
        status=str(outcome.status),
    )


@router.post(
    "/api/v1/tasks/{task_id}/mcp-calls/{call_ref}/cancel",
    response_model=MCPCallControlResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def cancel_mcp_call(
    task_id: str,
    call_ref: str,
    request: Request,
) -> MCPCallControlResponse:
    runtime = _runtime(request)
    user = await require_authenticated_user(request)
    await require_task_owner(runtime, task_id, user)
    if runtime.user_mcp_gateway is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "mcp_feature_unavailable"},
        )
    outcome = await runtime.user_mcp_gateway.cancel_call_for_task(
        task_id,
        call_ref,
        "user_cancelled",
    )
    if str(outcome.status) == str(MCPCancelStatus.UNKNOWN_CALL):
        raise HTTPException(status_code=404, detail={"code": "mcp_call_not_found"})
    if str(outcome.status) == str(MCPCancelStatus.ALREADY_TERMINAL):
        raise HTTPException(status_code=409, detail={"code": "mcp_call_already_terminal"})
    return MCPCallControlResponse(
        task_id=task_id,
        call_ref=call_ref,
        status=str(outcome.status),
    )


@router.get("/api/v1/tasks/{task_id}/interrupts", response_model=TaskInterruptsResponse)
async def list_task_interrupts(task_id: str, request: Request) -> TaskInterruptsResponse:
    runtime = _runtime(request)
    user = await require_authenticated_user(request)
    await require_task_owner(runtime, task_id, user)
    interrupts = await runtime.list_interrupts(task_id)
    return TaskInterruptsResponse(
        task_id=task_id,
        interrupts=[InterruptResponse(**interrupt) for interrupt in interrupts],
    )


@router.get("/api/v1/tasks/{task_id}/graph", response_model=TaskGraphResponse)
async def get_task_graph(task_id: str, request: Request) -> TaskGraphResponse:
    runtime = _runtime(request)
    user = await require_authenticated_user(request)
    await require_task_owner(runtime, task_id, user)
    agent_projection = getattr(runtime, "agent_task_projection", None)
    if agent_projection is not None:
        projected = await agent_projection.project_graph(task_id)
        if projected is not None:
            return projected
    nodes = await runtime.storage.list_task_nodes_for_task(task_id)
    return TaskGraphResponse(
        task_id=task_id,
        nodes=[
            TaskNodeResponse(
                node_id=node.node_id,
                capability_id=node.capability_id,
                status=str(node.status),
                criticality="required",
                dependency_type="hard",
                assigned_instance_id=node.assigned_instance_id,
                started_at=node.started_at,
                finished_at=node.finished_at,
            )
            for node in nodes
        ],
        edges=[],
    )


@router.get("/api/v1/tasks/{task_id}/artifacts", response_model=TaskArtifactsResponse)
async def get_task_artifacts(task_id: str, request: Request) -> TaskArtifactsResponse:
    runtime = _runtime(request)
    user = await require_authenticated_user(request)
    await require_task_owner(runtime, task_id, user)
    artifacts = await runtime.storage.list_artifacts_for_task(task_id)
    responses = []
    for artifact in artifacts:
        if should_return_task_artifact(artifact):
            responses.append(
                await artifact_response(
                    artifact,
                    artifact_file_store=runtime.artifact_file_store,
                    projection_store=runtime._mcp_projection_store,
                )
            )
    return TaskArtifactsResponse(task_id=task_id, artifacts=responses)


@router.get("/api/v1/artifacts/{artifact_id}/download")
async def download_artifact(artifact_id: str, request: Request) -> FileResponse:
    runtime = _runtime(request)
    user = await require_authenticated_user(request)
    artifact = await runtime.storage.get_artifact(artifact_id)
    if artifact is None or artifact.artifact_type != ArtifactType.FILE:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown artifact: {artifact_id}")
    await require_task_owner(runtime, artifact.task_id, user)
    metadata = parse_file_storage_ref(artifact.storage_ref)
    if not is_active_skill_output_file(metadata):
        await runtime.storage.append_event(
            _artifact_event(
                artifact=artifact,
                event_type="artifact.download_gone",
                payload={"artifact_id": artifact.artifact_id},
            )
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown artifact: {artifact_id}")
    storage_key = str(metadata.get("storage_key"))
    try:
        file_path = runtime.artifact_file_store.open_path(storage_key)
    except ValueError as exc:
        await runtime.storage.append_event(
            _artifact_event(
                artifact=artifact,
                event_type="artifact.download_denied",
                payload={"artifact_id": artifact.artifact_id, "reason": "invalid_storage_key"},
            )
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown artifact: {artifact_id}") from exc
    if not file_path.exists() or not file_path.is_file():
        await runtime.storage.append_event(
            _artifact_event(
                artifact=artifact,
                event_type="artifact.download_denied",
                payload={"artifact_id": artifact.artifact_id, "reason": "file_missing"},
            )
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown artifact: {artifact_id}")
    await runtime.storage.append_event(
        _artifact_event(
            artifact=artifact,
            event_type="artifact.downloaded",
            payload={
                "artifact_id": artifact.artifact_id,
                "filename": metadata.get("filename"),
                "mime_type": metadata.get("mime_type"),
            },
        )
    )
    return FileResponse(
        file_path,
        media_type=str(metadata.get("mime_type") or "application/octet-stream"),
        filename=str(metadata.get("filename") or file_path.name),
        headers={"X-Content-Type-Options": "nosniff"},
        content_disposition_type="attachment",
    )


def _artifact_event(*, artifact: Artifact, event_type: str, payload: dict) -> EventRecord:
    return EventRecord(
        event_id=f"{artifact.artifact_id}:{event_type}:{uuid4().hex[:12]}",
        conversation_id=str((parse_file_storage_ref(artifact.storage_ref) or {}).get("conversation_id") or ""),
        task_id=artifact.task_id,
        node_id=artifact.producer_node_id,
        event_type=event_type,
        payload=payload,
        visibility=EventVisibility.AUDIT_ONLY,
    )
