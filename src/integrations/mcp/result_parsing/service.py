from __future__ import annotations

import asyncio
import hashlib
import multiprocessing
import inspect
from time import monotonic
from collections import Counter, deque
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from multiprocessing.connection import Connection
from typing import Any, AsyncIterator

from src.integrations.token_counter import (
    TOOL_RESULT_BUSINESS_MAX_TOKENS,
    TokenBoundedText,
    truncate_text_to_token_budget_async,
)

from .models import MCPParsedToolResult, MCPRawResultDescriptor, MCPResultDecodeRequest
from .projection_store import (
    MCPProjectionBinding,
    MCPProjectionStagingHandle,
    MCPPublishedProjection,
    MCPProjectionStore,
    MCPProjectionStoreError,
)
from .worker import MCPValidatedResultCheckpoint, PARSER_REVISION, worker_entry
from .json_values import canonical_json_bytes
from .projections import (
    build_agent_projection,
    build_business_text,
    build_user_view,
    validate_safe_result_candidate,
)


MAX_MAPPING_INPUT_BYTES = 64 * 1024
MAX_ACTIVE_WORKERS = 1
MAX_QUEUED_JOBS = 8
MAX_OWNER_JOBS = 2
MAX_QUEUE_WAIT_SECONDS = 30.0
MAX_WORKER_WALL_SECONDS = 10.0
MAX_RAW_RESULT_BYTES = 64 * 1024 * 1024


class MCPResultWorkerError(RuntimeError):
    def __init__(self, code: str, *, worker_category: str | None = None) -> None:
        self.code = code
        self.worker_category = worker_category
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class MCPResultProjectionCandidate:
    result: MCPParsedToolResult
    binding: MCPProjectionBinding
    parsed_model_sha256: str


@dataclass(frozen=True, slots=True)
class MCPResultServiceOutcome:
    raw_sha256: str
    checkpoint: MCPValidatedResultCheckpoint
    projection_candidate: MCPResultProjectionCandidate | None
    projection_error: str | None = None

    @property
    def projection_staging_handle(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class MCPResultParserObservation:
    protocol_version: str
    source: str
    outcome: str
    reason: str
    primary_kind: str
    metadata_kinds: tuple[str, ...]
    structured_present: bool
    projection_sha256: str
    truncated: bool
    duration_seconds: float


@dataclass(slots=True)
class _GateTicket:
    owner_user_id: str
    future: asyncio.Future[None]


class MCPResultWorkerGate:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._queue: deque[_GateTicket] = deque()
        self._owner_counts: Counter[str] = Counter()
        self._active = False

    @property
    def active(self) -> int:
        return int(self._active)

    @property
    def queued(self) -> int:
        return len(self._queue)

    @asynccontextmanager
    async def acquire(self, owner_user_id: str) -> AsyncIterator[None]:
        loop = asyncio.get_running_loop()
        ticket = _GateTicket(str(owner_user_id), loop.create_future())
        async with self._lock:
            if self._owner_counts[ticket.owner_user_id] >= MAX_OWNER_JOBS:
                raise MCPResultWorkerError("mcp_result_parser_owner_capacity")
            if len(self._queue) >= MAX_QUEUED_JOBS:
                raise MCPResultWorkerError("mcp_result_parser_queue_capacity")
            self._owner_counts[ticket.owner_user_id] += 1
            self._queue.append(ticket)
            self._admit_next_locked()
        try:
            await asyncio.wait_for(ticket.future, timeout=MAX_QUEUE_WAIT_SECONDS)
        except asyncio.TimeoutError as exc:
            async with self._lock:
                if ticket in self._queue:
                    self._queue.remove(ticket)
                    self._owner_counts[ticket.owner_user_id] -= 1
            raise MCPResultWorkerError("mcp_result_parser_queue_timeout") from exc
        except BaseException:
            async with self._lock:
                if ticket in self._queue:
                    self._queue.remove(ticket)
                    self._owner_counts[ticket.owner_user_id] -= 1
                    self._admit_next_locked()
                elif self._active:
                    self._active = False
                    self._owner_counts[ticket.owner_user_id] -= 1
                    self._admit_next_locked()
            raise
        try:
            yield
        finally:
            async with self._lock:
                self._active = False
                self._owner_counts[ticket.owner_user_id] -= 1
                self._admit_next_locked()

    def _admit_next_locked(self) -> None:
        if self._active or not self._queue:
            return
        ticket = self._queue.popleft()
        self._active = True
        if not ticket.future.done():
            ticket.future.set_result(None)


class MCPIsolatedResultService:
    def __init__(
        self,
        *,
        projection_store: MCPProjectionStore,
        gate: MCPResultWorkerGate | None = None,
        worker_timeout_seconds: float = MAX_WORKER_WALL_SECONDS,
        observer: Callable[
            [MCPResultParserObservation], None | Awaitable[None]
        ]
        | None = None,
        tokenization_config: Mapping[str, Any] | None = None,
        token_budgeter: Callable[..., Awaitable[TokenBoundedText]] = (
            truncate_text_to_token_budget_async
        ),
    ) -> None:
        self._projection_store = projection_store
        self._gate = gate or MCPResultWorkerGate()
        self._worker_timeout_seconds = worker_timeout_seconds
        self._context = multiprocessing.get_context("spawn")
        self._observer = observer
        self._tokenization_config = dict(tokenization_config or {})
        self._token_budgeter = token_budgeter

    @property
    def gate(self) -> MCPResultWorkerGate:
        return self._gate

    def configure_observer(
        self,
        observer: Callable[
            [MCPResultParserObservation], None | Awaitable[None]
        ],
    ) -> None:
        if not callable(observer):
            raise ValueError("mcp_result_parser_observer_invalid")
        self._observer = observer

    def publish_projection(
        self, handle: MCPProjectionStagingHandle
    ) -> MCPPublishedProjection:
        return self._projection_store.publish(handle)

    def discard_projection(self, handle: MCPProjectionStagingHandle) -> None:
        self._projection_store.discard(handle)

    def consume_projection(
        self, handle: MCPProjectionStagingHandle
    ) -> Mapping[str, Any]:
        return self._projection_store.consume_staged(handle)

    async def stage_projection(
        self,
        candidate: MCPResultProjectionCandidate,
        *,
        model_edition: str,
    ) -> MCPProjectionStagingHandle:
        result = validate_safe_result_candidate(candidate.result)
        business_text = build_business_text(result)
        bounded = await self._token_budgeter(
            business_text,
            max_tokens=TOOL_RESULT_BUSINESS_MAX_TOKENS,
            model_edition=model_edition,
            config=self._tokenization_config,
        )
        agent_projection = build_agent_projection(
            result,
            business_text=bounded.text,
            truncated=bounded.truncated,
        )
        envelope = {
            "schema": "maf.mcp.parsed_result_projection.v2",
            "parsed_model_sha256": candidate.parsed_model_sha256,
            "user_view": build_user_view(
                result,
                business_text=bounded.text,
                truncated=bounded.truncated,
            ),
            "agent_projection": agent_projection.content,
            "agent_projection_truncated": agent_projection.truncated,
            "workflow_control": None,
        }
        return self._projection_store.stage(
            canonical_json_bytes(envelope),
            binding=candidate.binding,
        )

    async def parse(
        self,
        *,
        owner_user_id: str,
        task_id: str,
        node_id: str,
        call_ref: str,
        request: MCPResultDecodeRequest,
        measured_mapping_bytes: int | None = None,
    ) -> MCPResultServiceOutcome:
        started_at = monotonic()
        if not call_ref:
            raise ValueError("MCP result parser call_ref is required")
        if isinstance(request.payload, Mapping):
            if (
                isinstance(measured_mapping_bytes, bool)
                or not isinstance(measured_mapping_bytes, int)
                or measured_mapping_bytes < 0
                or measured_mapping_bytes > MAX_MAPPING_INPUT_BYTES
            ):
                raise MCPResultWorkerError("mcp_result_mapping_length_evidence_invalid")
        elif not isinstance(request.payload, MCPRawResultDescriptor):
            raise MCPResultWorkerError("mcp_result_live_payload_descriptor_required")
        elif (
            request.payload.size_bytes < 0
            or request.payload.size_bytes > MAX_RAW_RESULT_BYTES
            or not _is_sha256(
                "sha256:" + request.payload.sha256.removeprefix("sha256:")
            )
            or request.payload.device < 0
            or request.payload.inode <= 0
            or request.payload.owner_uid < 0
        ):
            raise MCPResultWorkerError("mcp_result_raw_descriptor_size_invalid")
        job = {
            "request": {
                "protocol_version": request.protocol_version,
                "source": str(request.source),
                "payload": request.payload,
                "output_schema": None
                if request.output_schema is None
                else dict(request.output_schema),
                "output_schema_sha256": request.output_schema_sha256,
                "historical_compatibility": request.historical_compatibility,
                "call_ref": call_ref,
            }
        }
        async with self._gate.acquire(owner_user_id):
            response = await self._run_worker(job)
        checkpoint = _validate_worker_checkpoint(response.get("checkpoint"), job)
        candidate_value = response.get("candidate")
        candidate = None
        projection_error = response.get("projection_error")
        if checkpoint.outcome == "succeeded" and candidate_value is not None:
            try:
                safe_result = validate_safe_result_candidate(candidate_value)
                if checkpoint.parsed_model_sha256 is None:
                    raise MCPResultWorkerError(
                        "mcp_result_parser_checkpoint_invalid"
                    )
                candidate = MCPResultProjectionCandidate(
                    result=safe_result,
                    parsed_model_sha256=checkpoint.parsed_model_sha256,
                    binding=MCPProjectionBinding(
                        owner_user_id=owner_user_id,
                        task_id=task_id,
                        node_id=node_id,
                        call_ref=call_ref,
                        raw_sha256=checkpoint.raw_sha256,
                        output_schema_sha256=request.output_schema_sha256,
                        source=str(request.source),
                        parser_revision=PARSER_REVISION,
                    ),
                )
            except (MCPResultWorkerError, MCPProjectionStoreError, OSError, ValueError):
                candidate = None
                projection_error = "projection_failed"
        elif checkpoint.outcome == "succeeded":
            projection_error = "projection_failed"
        outcome = MCPResultServiceOutcome(
            raw_sha256=checkpoint.raw_sha256,
            checkpoint=checkpoint,
            projection_candidate=candidate,
            projection_error=(
                "projection_failed" if projection_error is not None else None
            ),
        )
        await self._observe(
            _build_observation(
                request=request,
                checkpoint=checkpoint,
                candidate=candidate,
                projection_error=outcome.projection_error,
                duration_seconds=max(0.0, monotonic() - started_at),
            )
        )
        return outcome

    async def _observe(self, observation: MCPResultParserObservation) -> None:
        if self._observer is None:
            return
        try:
            pending = self._observer(observation)
            if inspect.isawaitable(pending):
                await pending
        except Exception:
            return

    async def _run_worker(self, job: Mapping[str, Any]) -> Mapping[str, Any]:
        receive, send = self._context.Pipe(duplex=False)
        process = self._context.Process(
            target=worker_entry, args=(send, job), daemon=True
        )
        process.start()
        send.close()
        response: Any = None
        failure: BaseException | None = None
        started_at = monotonic()
        try:
            first = await asyncio.wait_for(
                asyncio.to_thread(_receive_worker_response, receive),
                timeout=self._worker_timeout_seconds,
            )
            if not isinstance(first, Mapping) or first.get("worker_error"):
                raise MCPResultWorkerError(
                    "mcp_result_parser_worker_failed",
                    worker_category=(
                        str(first.get("worker_error"))
                        if isinstance(first, Mapping)
                        else "invalid_ipc"
                    ),
                )
            if "checkpoint" not in first:
                raise MCPResultWorkerError("mcp_result_parser_checkpoint_invalid")
            remaining = max(
                0.001, self._worker_timeout_seconds - (monotonic() - started_at)
            )
            try:
                second = await asyncio.wait_for(
                    asyncio.to_thread(_receive_worker_response, receive),
                    timeout=remaining,
                )
            except (asyncio.TimeoutError, MCPResultWorkerError):
                if process.is_alive():
                    process.terminate()
                    await asyncio.to_thread(process.join, 2.0)
                second = {"projection_error": "projection_failed"}
            response = dict(first)
            if isinstance(second, Mapping) and not second.get("worker_error"):
                response.update(second)
            else:
                response["projection_error"] = "projection_failed"
        except asyncio.TimeoutError as exc:
            process.terminate()
            await asyncio.to_thread(process.join, 2.0)
            if process.is_alive():
                process.kill()
                await asyncio.to_thread(process.join, 2.0)
            raise MCPResultWorkerError("mcp_result_parser_worker_timeout") from exc
        except BaseException as exc:
            failure = exc
            raise
        finally:
            receive.close()
            if failure is not None and process.is_alive():
                process.terminate()
                await asyncio.to_thread(process.join, 2.0)
        await asyncio.to_thread(process.join, 2.0)
        if process.is_alive():
            process.terminate()
            await asyncio.to_thread(process.join, 2.0)
        if not isinstance(response, Mapping) or response.get("worker_error"):
            raise MCPResultWorkerError("mcp_result_parser_worker_failed")
        return response


def _receive_worker_response(connection: Connection) -> Any:
    try:
        return connection.recv()
    except EOFError as exc:
        raise MCPResultWorkerError("mcp_result_parser_worker_failed") from exc


def _validate_worker_checkpoint(
    value: object, job: Mapping[str, Any]
) -> MCPValidatedResultCheckpoint:
    if not isinstance(value, Mapping):
        raise MCPResultWorkerError("mcp_result_parser_checkpoint_invalid")
    expected_keys = {
        "parser_revision",
        "protocol_version",
        "source",
        "call_ref",
        "raw_sha256",
        "output_schema_sha256",
        "outcome",
        "parsed_model_sha256",
        "diagnostics",
        "reason",
        "checkpoint_sha256",
    }
    if set(value) != expected_keys:
        raise MCPResultWorkerError("mcp_result_parser_checkpoint_invalid")
    request = job["request"]
    if (
        value["parser_revision"] != PARSER_REVISION
        or value["protocol_version"] != request["protocol_version"]
        or value["source"] != request["source"]
        or value["call_ref"] != request["call_ref"]
        or value["output_schema_sha256"] != request["output_schema_sha256"]
        or value["outcome"] not in {"succeeded", "tool_error", "malformed"}
        or not isinstance(value["diagnostics"], (list, tuple))
        or not _is_sha256(value["raw_sha256"])
        or not _is_sha256(value["checkpoint_sha256"])
        or any(
            diagnostic
            not in {
                "legacy_output_schema_unavailable",
                "legacy_missing_result_type",
                "structured_text_duplicate",
                "user_projection_truncated",
                "agent_projection_truncated",
            }
            for diagnostic in value["diagnostics"]
        )
    ):
        raise MCPResultWorkerError("mcp_result_parser_checkpoint_invalid")
    payload = {
        "schema": "maf.mcp.validated_result_checkpoint.v1",
        "parser_revision": value["parser_revision"],
        "protocol_version": value["protocol_version"],
        "source": value["source"],
        "call_ref": value["call_ref"],
        "raw_sha256": value["raw_sha256"],
        "output_schema_sha256": value["output_schema_sha256"],
        "outcome": value["outcome"],
        "parsed_model_sha256": value["parsed_model_sha256"],
        "diagnostics": list(value["diagnostics"]),
        "reason": value["reason"],
    }
    expected_checkpoint_sha = "sha256:" + hashlib.sha256(
        canonical_json_bytes(payload)
    ).hexdigest()
    if value["checkpoint_sha256"] != expected_checkpoint_sha:
        raise MCPResultWorkerError("mcp_result_parser_checkpoint_invalid")
    if value["outcome"] == "malformed":
        if (
            value["parsed_model_sha256"] is not None
            or value["reason"]
            not in {
                "unsupported_protocol_version",
                "unsupported_result_source",
                "malformed_json",
                "result_shape_invalid",
                "content_block_invalid",
                "output_schema_invalid",
                "output_schema_validation_failed",
            }
        ):
            raise MCPResultWorkerError("mcp_result_parser_checkpoint_invalid")
    elif not _is_sha256(value["parsed_model_sha256"]) or value["reason"] is not None:
        raise MCPResultWorkerError("mcp_result_parser_checkpoint_invalid")
    descriptor = request["payload"]
    if isinstance(descriptor, MCPRawResultDescriptor) and (
        value["raw_sha256"]
        != "sha256:" + descriptor.sha256.removeprefix("sha256:")
    ):
        raise MCPResultWorkerError("mcp_result_parser_checkpoint_invalid")
    return MCPValidatedResultCheckpoint(
        parser_revision=value["parser_revision"],
        protocol_version=value["protocol_version"],
        source=value["source"],
        call_ref=value["call_ref"],
        raw_sha256=value["raw_sha256"],
        output_schema_sha256=value["output_schema_sha256"],
        outcome=value["outcome"],
        parsed_model_sha256=value["parsed_model_sha256"],
        diagnostics=tuple(value["diagnostics"]),
        reason=value["reason"],
        checkpoint_sha256=value["checkpoint_sha256"],
    )


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _build_observation(
    *,
    request: MCPResultDecodeRequest,
    checkpoint: MCPValidatedResultCheckpoint,
    candidate: MCPResultProjectionCandidate | None,
    projection_error: str | None,
    duration_seconds: float,
) -> MCPResultParserObservation:
    primary_kind = "none"
    metadata_kinds: tuple[str, ...] = ()
    structured_present = False
    truncated = False
    projection_sha256 = "none"
    if candidate is not None:
        result = candidate.result
        structured_present = result.structured_content.present
        primary_kind = "structured" if structured_present else "text"
        metadata_kinds = tuple(
            sorted(
                {
                    block.kind
                    for block in result.content_blocks
                    if block.kind
                    in {
                        "image",
                        "audio",
                        "embedded_blob_resource",
                        "resource_link",
                        "embedded_text_resource",
                    }
                }
            )
        )
    return MCPResultParserObservation(
        protocol_version=request.protocol_version,
        source=str(request.source),
        outcome=checkpoint.outcome,
        reason=checkpoint.reason or projection_error or "none",
        primary_kind=primary_kind,
        metadata_kinds=metadata_kinds,
        structured_present=structured_present,
        projection_sha256=projection_sha256,
        truncated=truncated,
        duration_seconds=duration_seconds,
    )
