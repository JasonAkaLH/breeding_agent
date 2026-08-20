from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
from time import monotonic
from collections import Counter, deque
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from multiprocessing.connection import Connection
from typing import Any, AsyncIterator

from .models import MCPRawResultDescriptor, MCPResultDecodeRequest
from .projection_store import (
    MAX_PROJECTION_ENVELOPE_BYTES,
    MCPProjectionBinding,
    MCPProjectionStagingHandle,
    MCPPublishedProjection,
    MCPProjectionStore,
    MCPProjectionStoreError,
)
from .worker import MCPValidatedResultCheckpoint, PARSER_REVISION, worker_entry
from .json_values import canonical_json_bytes


MAX_MAPPING_INPUT_BYTES = 64 * 1024
MAX_ACTIVE_WORKERS = 1
MAX_QUEUED_JOBS = 8
MAX_OWNER_JOBS = 2
MAX_QUEUE_WAIT_SECONDS = 30.0
MAX_WORKER_WALL_SECONDS = 10.0
MAX_RAW_RESULT_BYTES = 64 * 1024 * 1024


class MCPResultParserMode(StrEnum):
    SAFE_HIDE = "safe_hide"
    SHADOW = "shadow"
    ENFORCE = "enforce"


def resolve_result_parser_mode(value: object) -> MCPResultParserMode:
    try:
        return MCPResultParserMode(str(value or "").strip().lower())
    except ValueError:
        return MCPResultParserMode.SAFE_HIDE


class MCPResultWorkerError(RuntimeError):
    def __init__(self, code: str, *, worker_category: str | None = None) -> None:
        self.code = code
        self.worker_category = worker_category
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class MCPResultServiceOutcome:
    raw_sha256: str
    checkpoint: MCPValidatedResultCheckpoint
    projection_staging_handle: MCPProjectionStagingHandle | None
    projection_error: str | None = None


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
    ) -> None:
        self._projection_store = projection_store
        self._gate = gate or MCPResultWorkerGate()
        self._worker_timeout_seconds = worker_timeout_seconds
        self._context = multiprocessing.get_context("spawn")

    @property
    def gate(self) -> MCPResultWorkerGate:
        return self._gate

    def publish_projection(
        self, handle: MCPProjectionStagingHandle
    ) -> MCPPublishedProjection:
        return self._projection_store.publish(handle)

    def discard_projection(self, handle: MCPProjectionStagingHandle) -> None:
        self._projection_store.discard(handle)

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
        projection = response.get("projection")
        handle = None
        projection_error = response.get("projection_error")
        if projection is not None:
            try:
                if not isinstance(projection, bytes) or len(projection) > MAX_PROJECTION_ENVELOPE_BYTES:
                    raise MCPResultWorkerError("mcp_result_parser_projection_invalid")
                _validate_projection_envelope(projection, checkpoint)
                handle = self._projection_store.stage(
                    projection,
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
            except (MCPResultWorkerError, MCPProjectionStoreError, OSError):
                handle = None
                projection_error = "projection_failed"
        return MCPResultServiceOutcome(
            raw_sha256=checkpoint.raw_sha256,
            checkpoint=checkpoint,
            projection_staging_handle=handle,
            projection_error=(
                "projection_failed" if projection_error is not None else None
            ),
        )

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


def _validate_projection_envelope(
    projection: bytes, checkpoint: MCPValidatedResultCheckpoint
) -> None:
    try:
        envelope = json.loads(projection)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MCPResultWorkerError("mcp_result_parser_projection_invalid") from exc
    if (
        not isinstance(envelope, dict)
        or set(envelope)
        != {
            "schema",
            "parsed_model_sha256",
            "user_view",
            "agent_projection",
            "workflow_control",
        }
        or envelope["schema"] != "maf.mcp.parsed_result_projection.v1"
        or envelope["parsed_model_sha256"] != checkpoint.parsed_model_sha256
        or not isinstance(envelope["user_view"], dict)
        or not isinstance(envelope["agent_projection"], str)
        or envelope["workflow_control"] is not None
    ):
        raise MCPResultWorkerError("mcp_result_parser_projection_invalid")


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )
