from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse
from sse_starlette.sse import EventSourceResponse

from src.core.enums import ArtifactType, EventVisibility, NodeStatus, TaskStatus
from src.core.models import Artifact, EventRecord
from src.storage.artifact_files import is_active_skill_output_file, parse_file_storage_ref

from ..auth import get_optional_owned_conversation, require_authenticated_user, require_task_owner
from ..dto import (
    AnswerInterruptRequest,
    AnswerInterruptResponse,
    ArtifactResponse,
    CancelTaskResponse,
    InterruptResponse,
    TaskArtifactsResponse,
    TaskEdgeResponse,
    TaskGraphResponse,
    TaskInterruptsResponse,
    TaskListResponse,
    TaskNodeResponse,
    TaskSummaryResponse,
)
from ..runtime import ApiRuntime
from ..sse import encode_sse_event

router = APIRouter()

UNFINISHED_TASK_STATUSES = {
    TaskStatus.ACCEPTED,
    TaskStatus.PLANNING,
    TaskStatus.RUNNING,
    TaskStatus.CANCELLING,
}


def _runtime(request: Request) -> ApiRuntime:
    return request.app.state.runtime


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
    nodes = await runtime.storage.list_task_nodes_for_task(task.task_id)
    return TaskSummaryResponse(
        task_id=task.task_id,
        conversation_id=task.conversation_id,
        status=str(task.status),
        root_node_id=task.root_node_id,
        summary=task.summary,
        requested_capability_id=task.requested_capability_id,
        active_node_count=_count_active_nodes(nodes),
        completed_node_count=sum(node.status == NodeStatus.COMPLETED for node in nodes),
        failed_node_count=_count_failed_nodes(nodes),
        cancel_requested=task.cancel_requested_at is not None or task.status in {TaskStatus.CANCELLING, TaskStatus.CANCELLED},
        created_at=task.created_at,
        updated_at=task.updated_at,
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
    await require_task_owner(runtime, task_id, user)

    async def _event_stream():
        async for event in runtime.iter_frontend_events(task_id):
            yield encode_sse_event(event)

    return EventSourceResponse(_event_stream())


@router.post("/api/v1/tasks/{task_id}/cancel", response_model=CancelTaskResponse, status_code=status.HTTP_202_ACCEPTED)
async def cancel_task(task_id: str, request: Request) -> CancelTaskResponse:
    runtime = _runtime(request)
    user = await require_authenticated_user(request)
    await require_task_owner(runtime, task_id, user)
    try:
        task = await runtime.cancel_task(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return CancelTaskResponse(task_id=task.task_id, status="cancelling", accepted=True)


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


@router.post(
    "/api/v1/tasks/{task_id}/interrupts/{interrupt_id}/answer",
    response_model=AnswerInterruptResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def answer_task_interrupt(
    task_id: str,
    interrupt_id: str,
    body: AnswerInterruptRequest,
    request: Request,
) -> AnswerInterruptResponse:
    runtime = _runtime(request)
    user = await require_authenticated_user(request)
    await require_task_owner(runtime, task_id, user)
    try:
        result = await runtime.answer_interrupt(task_id, interrupt_id, body.answer_payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return AnswerInterruptResponse(**result)


@router.get("/api/v1/tasks/{task_id}/graph", response_model=TaskGraphResponse)
async def get_task_graph(task_id: str, request: Request) -> TaskGraphResponse:
    runtime = _runtime(request)
    user = await require_authenticated_user(request)
    await require_task_owner(runtime, task_id, user)
    nodes = await runtime.storage.list_task_nodes_for_task(task_id)
    edges = await runtime.storage.list_task_edges(task_id)
    return TaskGraphResponse(
        task_id=task_id,
        nodes=[
            TaskNodeResponse(
                node_id=node.node_id,
                capability_id=node.capability_id,
                status=str(node.status),
                criticality=str(node.criticality),
                dependency_type=str(node.dependency_type),
                assigned_instance_id=node.assigned_instance_id,
                started_at=node.started_at,
                finished_at=node.finished_at,
            )
            for node in nodes
        ],
        edges=[
            TaskEdgeResponse(
                from_node_id=edge.from_node_id,
                to_node_id=edge.to_node_id,
                edge_type=str(edge.edge_type),
                condition=edge.condition,
            )
            for edge in edges
        ],
    )


@router.get("/api/v1/tasks/{task_id}/artifacts", response_model=TaskArtifactsResponse)
async def get_task_artifacts(task_id: str, request: Request) -> TaskArtifactsResponse:
    runtime = _runtime(request)
    user = await require_authenticated_user(request)
    await require_task_owner(runtime, task_id, user)
    artifacts = await runtime.storage.list_artifacts_for_task(task_id)
    return TaskArtifactsResponse(
        task_id=task_id,
        artifacts=[
            _artifact_response(artifact)
            for artifact in artifacts
            if _should_return_artifact(artifact)
        ],
    )


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


def _should_return_artifact(artifact: Artifact) -> bool:
    if artifact.artifact_type != ArtifactType.FILE:
        return True
    return is_active_skill_output_file(parse_file_storage_ref(artifact.storage_ref))


def _artifact_response(artifact: Artifact) -> ArtifactResponse:
    if artifact.artifact_type != ArtifactType.FILE:
        return ArtifactResponse(
            artifact_id=artifact.artifact_id,
            producer_node_id=artifact.producer_node_id,
            artifact_type=str(artifact.artifact_type),
            storage_ref=artifact.storage_ref,
            summary=artifact.summary,
            is_complete=artifact.is_complete,
            created_at=artifact.created_at,
        )
    metadata = parse_file_storage_ref(artifact.storage_ref) or {}
    return ArtifactResponse(
        artifact_id=artifact.artifact_id,
        producer_node_id=artifact.producer_node_id,
        artifact_type=str(artifact.artifact_type),
        storage_ref="",
        summary=str(metadata.get("summary") or artifact.summary or ""),
        is_complete=artifact.is_complete,
        created_at=artifact.created_at,
        filename=_optional_string(metadata.get("filename")),
        mime_type=_optional_string(metadata.get("mime_type")),
        size_bytes=_optional_int(metadata.get("size_bytes")),
        sha256=_optional_string(metadata.get("sha256")),
        download_url=f"/api/v1/artifacts/{artifact.artifact_id}/download",
        source_file_count=_optional_int(metadata.get("source_file_count")),
        archive_format=_optional_string(metadata.get("archive_format")),
        retention_status=_optional_string(metadata.get("retention_status")),
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


def _optional_string(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
