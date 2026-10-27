from __future__ import annotations

import base64
import hashlib
import inspect
import json
from collections.abc import Mapping
from typing import Any

from .models import AgentItemKind


KNOWLEDGE_RESULT_SCHEMA = "maf.agent.knowledge_reference.v1"
KNOWLEDGE_MODEL_SCHEMA = "maf.agent.knowledge_content.v1"
KNOWLEDGE_CAPABILITY_IDS = frozenset(
    {"knowledge.list_libraries", "knowledge.read_index", "knowledge.read_document"}
)
_REFERENCE_KEYS = frozenset(
    {
        "actor", "conversation_id", "task_id", "run_id", "root_message_id",
        "capability_id", "library_id", "document_id", "version", "start", "end",
        "total_characters", "next_cursor", "metadata",
    }
)
_STATUSES = frozenset(
    {"available", "unavailable", "forbidden", "changed", "requires_reselection", "budget_exceeded"}
)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def is_knowledge_result(value: object) -> bool:
    return isinstance(value, Mapping) and value.get("schema") == KNOWLEDGE_RESULT_SCHEMA


def validate_knowledge_result(value: object) -> dict[str, Any]:
    if (
        not isinstance(value, Mapping)
        or set(value) != {"schema", "status", "reference"}
        or value.get("schema") != KNOWLEDGE_RESULT_SCHEMA
        or value.get("status") not in _STATUSES
    ):
        raise ValueError("agent_knowledge_reference_invalid")
    result = dict(value)
    ref = result["reference"]
    if result["status"] != "available":
        if ref is not None:
            raise ValueError("agent_knowledge_reference_invalid")
        return result
    if not isinstance(ref, Mapping) or set(ref) != _REFERENCE_KEYS:
        raise ValueError("agent_knowledge_reference_invalid")
    for key in ("actor", "conversation_id", "task_id", "run_id", "root_message_id", "version"):
        if not isinstance(ref[key], str) or not ref[key].strip():
            raise ValueError("agent_knowledge_reference_invalid")
    if ref["capability_id"] not in KNOWLEDGE_CAPABILITY_IDS:
        raise ValueError("agent_knowledge_reference_invalid")
    for key in ("library_id", "document_id"):
        if ref[key] is not None and (not isinstance(ref[key], str) or not ref[key]):
            raise ValueError("agent_knowledge_reference_invalid")
    if ref["capability_id"] != "knowledge.list_libraries" and ref["library_id"] is None:
        raise ValueError("agent_knowledge_reference_invalid")
    if ref["capability_id"] == "knowledge.read_document" and ref["document_id"] is None:
        raise ValueError("agent_knowledge_reference_invalid")
    if ref["capability_id"] in {"knowledge.list_libraries", "knowledge.read_document"} and (
        len(ref["version"]) != 64 or any(char not in "0123456789abcdef" for char in ref["version"])
    ):
        raise ValueError("agent_knowledge_reference_invalid")
    for key in ("start", "end", "total_characters"):
        if isinstance(ref[key], bool) or not isinstance(ref[key], int) or ref[key] < 0:
            raise ValueError("agent_knowledge_reference_invalid")
    if not 0 <= ref["start"] <= ref["end"] <= ref["total_characters"]:
        raise ValueError("agent_knowledge_reference_invalid")
    if ref["next_cursor"] is not None and not isinstance(ref["next_cursor"], str):
        raise ValueError("agent_knowledge_reference_invalid")
    if ref["next_cursor"] != (encode_knowledge_cursor(ref, ref["end"]) if ref["end"] < ref["total_characters"] else None):
        raise ValueError("agent_knowledge_reference_invalid")
    metadata = ref["metadata"]
    if not isinstance(metadata, Mapping) or set(metadata) - {"title", "authors", "year", "doi"}:
        raise ValueError("agent_knowledge_reference_invalid")
    if len(canonical_json(metadata).encode("utf-8")) > 16_384:
        raise ValueError("agent_knowledge_reference_invalid")
    if ("title" in metadata and not isinstance(metadata["title"], str)) or (
        "authors" in metadata and (not isinstance(metadata["authors"], list) or any(not isinstance(author, str) for author in metadata["authors"]))
    ) or (metadata.get("year") is not None and (isinstance(metadata["year"], bool) or not isinstance(metadata["year"], int))) or (
        metadata.get("doi") is not None and not isinstance(metadata["doi"], str)
    ):
        raise ValueError("agent_knowledge_reference_invalid")
    return result


def trusted_knowledge_scope(metadata: Mapping[str, Any], *, conversation_id: str) -> Mapping[str, Any]:
    scope = metadata.get("knowledge_scope")
    if not isinstance(scope, Mapping):
        raise ValueError("agent_knowledge_scope_unavailable")
    actor = scope.get("actor")
    owner = metadata.get("agent_owner_scope") or metadata.get("authenticated_owner_scope")
    if (
        not isinstance(actor, str) or not actor
        or owner != hashlib.sha256(f"agent-owner-scope-v1\0{actor}".encode()).hexdigest()
        or scope.get("conversation_id") != conversation_id
        or scope.get("mode") not in {"auto", "selected"}
        or not isinstance(scope.get("root_message_id"), str)
        or not scope["root_message_id"]
        or not isinstance(scope.get("candidate_library_ids"), (list, tuple))
        or any(not isinstance(item, str) or not item for item in scope["candidate_library_ids"])
    ):
        raise ValueError("agent_knowledge_scope_invalid")
    return scope


def encode_knowledge_cursor(ref: Mapping[str, Any], offset: int) -> str:
    payload = {
        "capability_id": ref["capability_id"], "library_id": ref["library_id"],
        "document_id": ref["document_id"], "version": ref["version"], "offset": offset,
    }
    return base64.urlsafe_b64encode(canonical_json(payload).encode()).decode().rstrip("=")


def decode_knowledge_cursor(value: str) -> dict[str, Any]:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError("agent_knowledge_cursor_invalid")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        payload = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("agent_knowledge_cursor_invalid") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"capability_id", "library_id", "document_id", "version", "offset"}
        or isinstance(payload.get("offset"), bool)
        or not isinstance(payload.get("offset"), int)
        or payload["offset"] < 0
    ):
        raise ValueError("agent_knowledge_cursor_invalid")
    return payload


async def load_knowledge_resource(service: Any, scope: Mapping[str, Any], *, capability_id: str,
                                  library_id: str | None, document_id: str | None) -> dict[str, Any]:
    actor = scope["actor"]
    if capability_id == "knowledge.list_libraries":
        result = await service.list_for_scope(actor, scope)
        text = canonical_json(result)
        return {"text": text, "version": hashlib.sha256(text.encode()).hexdigest(), "metadata": {}}
    if library_id not in scope["candidate_library_ids"]:
        raise ValueError("agent_knowledge_out_of_scope")
    if capability_id == "knowledge.read_index":
        result = await service.read_index(actor, library_id)
        return {"text": result["text"], "version": str(result["index_revision"]), "metadata": {}}
    result = await service.read_document(actor, library_id, document_id)
    metadata = {key: value for key, value in dict(result.get("metadata") or {}).items()
                if key in {"title", "authors", "year", "doi"}}
    return {"text": result["text"], "version": result["version"], "metadata": metadata}


def knowledge_model_view(result: Mapping[str, Any], text: str | None = None,
                         metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
    view = {"schema": KNOWLEDGE_MODEL_SCHEMA, "status": result["status"], "reference": result["reference"]}
    if text is not None:
        view.update({"text": text, "metadata": dict(metadata or {}),
                     "untrusted_material": True,
                     "complete": result["reference"]["end"] == result["reference"]["total_characters"]})
    return view


def expected_knowledge_failure(exc: Exception) -> str | None:
    code = str(getattr(exc, "code", "") or str(exc))
    if code in {"agent_knowledge_scope_unavailable", "agent_knowledge_out_of_scope"}:
        return "unavailable"
    status = getattr(exc, "status_code", None)
    if status == 403 or code in {"knowledge_forbidden", "forbidden"}:
        return "forbidden"
    if status in {404, 409, 410, 503} or code in {"not_found", "knowledge_not_found"}:
        return "unavailable"
    return None


class KnowledgeResultResolver:
    """Rehydrate current, authorized ranges only; never keeps a full-text cache."""

    def __init__(self, *, service: Any, scope_loader: Any) -> None:
        self._service = service
        self._load_scope = scope_loader

    async def answer_sources(self, *, run: Any, items: tuple) -> list[dict[str, Any]]:
        calls = {item.item_id: item for item in items if item.kind is AgentItemKind.TOOL_CALL}
        documents: dict[tuple[str, str], list[tuple]] = {}
        for item in sorted(items, key=lambda item: item.sequence):
            if item.kind is not AgentItemKind.TOOL_RESULT or item.state.value != "committed":
                continue
            payload = json.loads(item.payload_json)
            result = payload.get("safe_result")
            if not is_knowledge_result(result):
                continue
            ref = validate_knowledge_result(result)["reference"]
            if ref is not None and ref["capability_id"] == "knowledge.read_document":
                key = (ref["library_id"], ref["document_id"])
                documents.setdefault(key, []).append((item, payload, ref))
        sources = []
        for number, records in enumerate(documents.values(), 1):
            item, payload, ref = records[-1]
            call = calls.get(str(item.source_call_item_id))
            if call is None:
                raise ValueError("agent_knowledge_reference_identity_invalid")
            view = (await self.resolve_tool_result(
                run=run, call_item=call, result_item=item, durable_payload=payload,
            ))["safe_result"]
            source = {"citation": number, "library_id": ref["library_id"], "document_id": ref["document_id"],
                      "status": view["status"], "complete": False, "metadata": {}}
            if view["status"] == "available":
                ranges = sorted((value[2]["start"], value[2]["end"]) for value in records
                                if value[2]["version"] == ref["version"])
                contiguous = 0
                for start, end in ranges:
                    if start > contiguous:
                        break
                    contiguous = max(contiguous, end)
                source.update({"version": ref["version"], "metadata": view["metadata"],
                               "read_characters": contiguous, "total_characters": ref["total_characters"],
                               "complete": contiguous == ref["total_characters"],
                               "next_cursor": encode_knowledge_cursor(ref, contiguous) if contiguous < ref["total_characters"] else None})
            sources.append(source)
        return sources

    async def resolve_tool_result(self, *, run: Any, call_item: Any, result_item: Any,
                                  durable_payload: Mapping[str, Any]) -> dict[str, Any]:
        result = validate_knowledge_result(durable_payload.get("safe_result"))
        if (call_item.run_id != run.run_id or result_item.run_id != run.run_id
                or call_item.task_id != run.task_id or result_item.task_id != run.task_id
                or result_item.source_call_item_id != call_item.item_id
                or call_item.kind is not AgentItemKind.TOOL_CALL
                or result_item.kind is not AgentItemKind.TOOL_RESULT):
            raise ValueError("agent_knowledge_reference_identity_invalid")
        ref = result["reference"]
        if ref is not None and (ref["run_id"] != run.run_id or ref["task_id"] != run.task_id
                                or ref["conversation_id"] != run.conversation_id
                                or json.loads(call_item.payload_json).get("capability_id") != ref["capability_id"]):
            raise ValueError("agent_knowledge_reference_identity_invalid")
        if ref is not None:
            arguments = json.loads(json.loads(call_item.payload_json)["arguments_json"])
            if not isinstance(arguments, dict) or any(arguments.get(key) != ref[key] for key in ("library_id", "document_id")):
                raise ValueError("agent_knowledge_reference_identity_invalid")
            cursor = arguments.get("cursor")
            if cursor is not None:
                decoded = decode_knowledge_cursor(cursor)
                if any(decoded[key] != ref[key] for key in ("capability_id", "library_id", "document_id", "version")) or decoded["offset"] != ref["start"]:
                    raise ValueError("agent_knowledge_reference_identity_invalid")
            elif ref["start"] != 0:
                raise ValueError("agent_knowledge_reference_identity_invalid")
            loaded = self._load_scope(run)
            if inspect.isawaitable(loaded):
                loaded = await loaded
            # Runtime supplies trusted metadata; accepting a bare scope keeps the seam narrow.
            metadata = loaded if isinstance(loaded, Mapping) and "knowledge_scope" in loaded else {
                "knowledge_scope": loaded,
                "agent_owner_scope": hashlib.sha256(f"agent-owner-scope-v1\0{loaded.get('actor')}".encode()).hexdigest()
                if isinstance(loaded, Mapping) else None,
            }
            try:
                scope = trusted_knowledge_scope(metadata, conversation_id=run.conversation_id)
                if ref["actor"] != scope["actor"] or ref["root_message_id"] != scope["root_message_id"]:
                    raise ValueError("agent_knowledge_reference_identity_invalid")
                resource = await load_knowledge_resource(
                    self._service, scope, capability_id=ref["capability_id"],
                    library_id=ref["library_id"], document_id=ref["document_id"],
                )
            except Exception as exc:
                status = expected_knowledge_failure(exc)
                if status is None:
                    raise
                result = {"schema": KNOWLEDGE_RESULT_SCHEMA, "status": status, "reference": None}
            else:
                if resource["version"] != ref["version"] or len(resource["text"]) != ref["total_characters"]:
                    result = {"schema": KNOWLEDGE_RESULT_SCHEMA, "status": "changed", "reference": None}
                else:
                    return self._envelope(durable_payload, knowledge_model_view(
                        result, resource["text"][ref["start"]:ref["end"]], resource["metadata"]))
        return self._envelope(durable_payload, knowledge_model_view(result))

    @staticmethod
    def _envelope(payload: Mapping[str, Any], view: Mapping[str, Any]) -> dict[str, Any]:
        return {"outcome": payload.get("outcome"), "safe_error_code": payload.get("safe_error_code"),
                "safe_result": dict(view), "artifact_refs": []}
