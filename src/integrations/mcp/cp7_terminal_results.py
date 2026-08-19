from __future__ import annotations

import hashlib
import hmac
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import AbstractSet, Mapping

from src.core.models import (
    MCPTerminalCandidateSnapshot,
    MCPValidatedTerminalResultCandidate,
    MCPTerminalState,
)
from src.integrations.mcp.cp7_artifacts import (
    CP7ArtifactConflictError,
    CP7ArtifactValidationError,
    CanonicalEnvelopeArtifact,
    canonical_envelope_bytes,
    canonical_sha256,
    mcp_terminal_candidate_id,
    parse_canonical_envelope_bytes,
    parse_canonical_json_bytes,
    publish_or_compare_immutable,
    secure_read,
    secure_read_canonical_envelope,
)
from src.integrations.mcp.temporary_results import MAX_DURABLE_MCP_RESULT_BYTES


TERMINAL_CANDIDATE_SCHEMA_V1 = "maf.user_mcp.cp7.terminal_result_candidate.v1"
TERMINAL_CANDIDATE_SCHEMA_V2 = "maf.user_mcp.cp7.terminal_result_candidate.v2"
TERMINAL_CANDIDATE_SCHEMA = TERMINAL_CANDIDATE_SCHEMA_V2
TERMINAL_TASK_INDEX_SCHEMA = "maf.user_mcp.cp7.terminal_result_task_index.v1"
TERMINAL_CALL_INDEX_SCHEMA = "maf.user_mcp.cp7.terminal_result_call_index.v1"
DEFAULT_MAXIMUM_TERMINAL_CANDIDATES = 10_000
TERMINAL_CANDIDATE_WARNING_THRESHOLD = 8_000
DEFAULT_MAXIMUM_TERMINAL_ARTIFACTS = 3 * DEFAULT_MAXIMUM_TERMINAL_CANDIDATES
_ARTIFACT_SIZE_LIMIT = 64 * 1024
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_UTC_SECOND_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class CP7TerminalResultCorruptionError(CP7ArtifactValidationError):
    """A sealed terminal-result store violates its closed relationship graph."""


class CP7TerminalResultLimitError(CP7TerminalResultCorruptionError):
    """A terminal-result inventory exceeds its configured startup bound."""


@dataclass(frozen=True, slots=True)
class SealedTerminalResultCandidate:
    candidate: MCPValidatedTerminalResultCandidate
    candidate_schema: str
    candidate_file_sha256: str
    candidate_payload_sha256: str
    task_index_file_sha256: str
    call_index_file_sha256: str


@dataclass(frozen=True, slots=True)
class TerminalResultSealOutcome:
    sealed: SealedTerminalResultCandidate
    candidate_created: bool
    task_index_created: bool
    call_index_created: bool


def seal_terminal_result_candidate(
    artifact_root: str | os.PathLike[str],
    candidate: MCPValidatedTerminalResultCandidate,
) -> TerminalResultSealOutcome:
    """Durably seal a candidate before any authority-database transaction."""

    root = _validated_root(artifact_root)
    names = _bounded_names(
        root,
        maximum_entries=DEFAULT_MAXIMUM_TERMINAL_ARTIFACTS,
    )
    candidate_path = terminal_candidate_path(root, candidate.candidate_id)
    if (
        not candidate_path.exists()
        and sum(name.startswith("candidate-") for name in names)
        >= DEFAULT_MAXIMUM_TERMINAL_CANDIDATES
    ):
        raise CP7TerminalResultLimitError(
            "terminal-result candidate inventory exceeds its active bound"
        )
    payload = _candidate_payload(candidate)
    candidate_publication = publish_or_compare_immutable(
        terminal_candidate_path(root, candidate.candidate_id),
        canonical_envelope_bytes(TERMINAL_CANDIDATE_SCHEMA, payload),
        maximum_size=_ARTIFACT_SIZE_LIMIT,
    )
    candidate_payload_sha256 = canonical_sha256(payload)
    index_payload = _index_payload(
        candidate,
        candidate_file_sha256=candidate_publication.artifact.file_sha256,
        candidate_payload_sha256=candidate_payload_sha256,
    )
    task_publication = publish_or_compare_immutable(
        terminal_task_index_path(root, candidate.task_id, candidate.candidate_id),
        canonical_envelope_bytes(TERMINAL_TASK_INDEX_SCHEMA, index_payload),
        maximum_size=_ARTIFACT_SIZE_LIMIT,
    )
    call_publication = publish_or_compare_immutable(
        terminal_call_index_path(root, candidate.call_id),
        canonical_envelope_bytes(TERMINAL_CALL_INDEX_SCHEMA, index_payload),
        maximum_size=_ARTIFACT_SIZE_LIMIT,
    )
    sealed = secure_read_terminal_result_candidate(root, candidate.candidate_id)
    if sealed.candidate != candidate:
        raise CP7TerminalResultCorruptionError(
            "sealed terminal-result candidate differs from the requested candidate"
        )
    return TerminalResultSealOutcome(
        sealed=sealed,
        candidate_created=candidate_publication.created,
        task_index_created=task_publication.created,
        call_index_created=call_publication.created,
    )


def secure_read_terminal_result_candidate(
    artifact_root: str | os.PathLike[str],
    candidate_id: str,
    *,
    maximum_entries: int = DEFAULT_MAXIMUM_TERMINAL_ARTIFACTS,
) -> SealedTerminalResultCandidate:
    sealed_candidates = enumerate_unconsumed_terminal_result_candidates(
        artifact_root,
        maximum_entries=maximum_entries,
    )
    matches = [
        sealed
        for sealed in sealed_candidates
        if _identities_equal(sealed.candidate.candidate_id, candidate_id)
    ]
    if len(matches) != 1:
        raise CP7TerminalResultCorruptionError(
            "terminal-result candidate lookup is missing or non-unique"
        )
    return matches[0]


def compare_terminal_result_candidate(
    artifact_root: str | os.PathLike[str],
    expected: MCPValidatedTerminalResultCandidate,
) -> SealedTerminalResultCandidate:
    sealed = secure_read_terminal_result_candidate(artifact_root, expected.candidate_id)
    if sealed.candidate != expected:
        raise CP7TerminalResultCorruptionError(
            "sealed terminal-result candidate binding does not match"
        )
    return sealed


def secure_read_terminal_result_candidate_active_or_archive(
    artifact_root: str | os.PathLike[str],
    candidate_id: str,
    *,
    archive_root: str | os.PathLike[str] | None = None,
) -> SealedTerminalResultCandidate:
    root = _validated_root(artifact_root)
    candidate_path = terminal_candidate_path(root, candidate_id)
    if candidate_path.exists() or candidate_path.is_symlink():
        return secure_read_terminal_result_candidate(root, candidate_id)
    resolved_archive = _validated_root(
        Path(archive_root)
        if archive_root is not None
        else root.with_name(root.name + "-archive")
    )
    return _read_and_validate_candidate(resolved_archive, candidate_id)


def enumerate_unconsumed_terminal_result_candidates(
    artifact_root: str | os.PathLike[str],
    *,
    consumed_candidate_ids: AbstractSet[str] = frozenset(),
    maximum_entries: int = DEFAULT_MAXIMUM_TERMINAL_ARTIFACTS,
    maximum_candidates: int = DEFAULT_MAXIMUM_TERMINAL_CANDIDATES,
) -> tuple[SealedTerminalResultCandidate, ...]:
    """Validate the whole store and return unconsumed candidates deterministically."""

    if isinstance(maximum_entries, bool) or maximum_entries <= 0:
        raise CP7TerminalResultLimitError(
            "terminal-result enumeration bound is invalid"
        )
    if isinstance(maximum_candidates, bool) or maximum_candidates <= 0:
        raise CP7TerminalResultLimitError(
            "terminal-result candidate enumeration bound is invalid"
        )
    root = _validated_root(artifact_root)
    names = _bounded_names(root, maximum_entries=maximum_entries)
    candidate_names = [name for name in names if name.startswith("candidate-")]
    task_index_names = [name for name in names if name.startswith("task-index-")]
    call_index_names = [name for name in names if name.startswith("call-index-")]
    if len(candidate_names) > maximum_candidates:
        raise CP7TerminalResultLimitError(
            "terminal-result candidate inventory exceeds its active bound"
        )
    if len(candidate_names) + len(task_index_names) + len(call_index_names) != len(
        names
    ):
        raise CP7TerminalResultCorruptionError(
            "terminal-result store contains an unexpected artifact"
        )

    candidates: list[SealedTerminalResultCandidate] = []
    candidate_ids: set[str] = set()
    expected_task_names: set[str] = set()
    expected_call_names: set[str] = set()
    call_candidates: dict[str, str] = {}
    for name in candidate_names:
        envelope = _read_candidate_envelope(root / name)
        candidate = _candidate_from_payload(envelope.payload, schema=envelope.schema)
        expected_name = terminal_candidate_path(root, candidate.candidate_id).name
        if name != expected_name or candidate.candidate_id in candidate_ids:
            raise CP7TerminalResultCorruptionError(
                "terminal-result candidate filename or identity is forked"
            )
        previous = call_candidates.setdefault(candidate.call_id, candidate.candidate_id)
        if previous != candidate.candidate_id:
            raise CP7TerminalResultCorruptionError(
                "terminal-result call has multiple sealed candidates"
            )
        sealed = _read_and_validate_candidate(root, candidate.candidate_id)
        candidates.append(sealed)
        candidate_ids.add(candidate.candidate_id)
        expected_task_names.add(
            terminal_task_index_path(
                root, candidate.task_id, candidate.candidate_id
            ).name
        )
        expected_call_names.add(terminal_call_index_path(root, candidate.call_id).name)

    if set(task_index_names) != expected_task_names:
        raise CP7TerminalResultCorruptionError(
            "terminal-result task index set is missing or forked"
        )
    if set(call_index_names) != expected_call_names:
        raise CP7TerminalResultCorruptionError(
            "terminal-result call index set is missing or forked"
        )
    return tuple(
        sealed
        for sealed in sorted(
            candidates, key=lambda item: item.candidate.candidate_id.encode()
        )
        if sealed.candidate.candidate_id not in consumed_candidate_ids
    )


def terminal_candidate_path(root: Path, candidate_id: str) -> Path:
    return root / f"candidate-{_path_key(candidate_id)}.json"


def terminal_task_index_path(root: Path, task_id: str, candidate_id: str) -> Path:
    return root / f"task-index-{_path_key(task_id + chr(0) + candidate_id)}.json"


def terminal_call_index_path(root: Path, call_id: str) -> Path:
    return root / f"call-index-{_path_key(call_id)}.json"


def _read_and_validate_candidate(
    root: Path, candidate_id: str
) -> SealedTerminalResultCandidate:
    candidate_envelope = _read_candidate_envelope(
        terminal_candidate_path(root, candidate_id)
    )
    candidate = _candidate_from_payload(
        candidate_envelope.payload, schema=candidate_envelope.schema
    )
    if not _identities_equal(candidate.candidate_id, candidate_id):
        raise CP7TerminalResultCorruptionError(
            "terminal-result candidate identity does not match its lookup key"
        )
    index_payload = _index_payload(
        candidate,
        candidate_file_sha256=candidate_envelope.artifact.file_sha256,
        candidate_payload_sha256=candidate_envelope.payload_sha256,
    )
    task_envelope = _read_envelope(
        terminal_task_index_path(root, candidate.task_id, candidate.candidate_id),
        TERMINAL_TASK_INDEX_SCHEMA,
    )
    call_envelope = _read_envelope(
        terminal_call_index_path(root, candidate.call_id), TERMINAL_CALL_INDEX_SCHEMA
    )
    if task_envelope.payload != index_payload or call_envelope.payload != index_payload:
        raise CP7TerminalResultCorruptionError(
            "terminal-result seal and index binding differ"
        )
    return SealedTerminalResultCandidate(
        candidate=candidate,
        candidate_schema=candidate_envelope.schema,
        candidate_file_sha256=candidate_envelope.artifact.file_sha256,
        candidate_payload_sha256=candidate_envelope.payload_sha256,
        task_index_file_sha256=task_envelope.artifact.file_sha256,
        call_index_file_sha256=call_envelope.artifact.file_sha256,
    )


class MCPTerminalCandidateSnapshotAuthority:
    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        archive_root: str | os.PathLike[str] | None = None,
    ) -> None:
        self._root = _validated_root(root)
        self._archive_root = Path(
            archive_root
            if archive_root is not None
            else self._root.with_name(self._root.name + "-archive")
        )

    def snapshot(
        self, sealed: SealedTerminalResultCandidate
    ) -> MCPTerminalCandidateSnapshot:
        candidate = sealed.candidate
        return MCPTerminalCandidateSnapshot(
            candidate=candidate,
            candidate_schema=sealed.candidate_schema,
            active_candidate_filename=terminal_candidate_path(
                self._root, candidate.candidate_id
            ).name,
            active_task_index_filename=terminal_task_index_path(
                self._root, candidate.task_id, candidate.candidate_id
            ).name,
            active_call_index_filename=terminal_call_index_path(
                self._root, candidate.call_id
            ).name,
            candidate_file_sha256=sealed.candidate_file_sha256,
            task_index_file_sha256=sealed.task_index_file_sha256,
            call_index_file_sha256=sealed.call_index_file_sha256,
        )

    def revalidate(
        self, snapshot: MCPTerminalCandidateSnapshot
    ) -> MCPTerminalCandidateSnapshot:
        candidate = snapshot.candidate
        active_paths = (
            terminal_candidate_path(self._root, candidate.candidate_id),
            terminal_task_index_path(
                self._root, candidate.task_id, candidate.candidate_id
            ),
            terminal_call_index_path(self._root, candidate.call_id),
        )
        if not active_paths[0].exists() and any(
            path.exists() or path.is_symlink() for path in active_paths[1:]
        ):
            raise CP7TerminalResultCorruptionError(
                "terminal-result active candidate triple is partial"
            )
        sealed = secure_read_terminal_result_candidate_active_or_archive(
            self._root,
            candidate.candidate_id,
            archive_root=self._archive_root,
        )
        current = MCPTerminalCandidateSnapshot(
            candidate=sealed.candidate,
            candidate_schema=sealed.candidate_schema,
            active_candidate_filename=snapshot.active_candidate_filename,
            active_task_index_filename=snapshot.active_task_index_filename,
            active_call_index_filename=snapshot.active_call_index_filename,
            candidate_file_sha256=sealed.candidate_file_sha256,
            task_index_file_sha256=sealed.task_index_file_sha256,
            call_index_file_sha256=sealed.call_index_file_sha256,
        )
        if current != snapshot:
            raise CP7TerminalResultCorruptionError(
                "terminal-result candidate snapshot changed"
            )
        return snapshot


def _candidate_payload(
    candidate: MCPValidatedTerminalResultCandidate,
) -> dict[str, object]:
    expected_id = mcp_terminal_candidate_id(
        candidate.call_id, candidate.result_payload_sha256
    )
    if not _identities_equal(candidate.candidate_id, expected_id):
        raise CP7TerminalResultCorruptionError(
            "terminal-result candidate ID does not match its call and payload"
        )
    for name in (
        "owner_user_id",
        "conversation_id",
        "task_id",
        "node_id",
        "intent_id",
        "call_id",
        "server_id",
    ):
        _require_closed_string(getattr(candidate, name), name)
    for name in ("server_config_version", "server_security_version"):
        value = getattr(candidate, name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise CP7TerminalResultCorruptionError(
                f"terminal-result {name} must be a positive integer"
            )
    if _SHA256_RE.fullmatch(candidate.result_payload_sha256) is None:
        raise CP7TerminalResultCorruptionError(
            "terminal-result payload digest is invalid"
        )
    try:
        terminal_state = MCPTerminalState(candidate.terminal_state)
    except ValueError as exc:
        raise CP7TerminalResultCorruptionError(
            "terminal-result terminal state is invalid"
        ) from exc
    if terminal_state is MCPTerminalState.COMPLETED:
        _require_closed_string(candidate.safe_result_ref, "safe_result_ref")
        if candidate.safe_result_ref_sha256 != canonical_sha256(
            candidate.safe_result_ref
        ):
            raise CP7TerminalResultCorruptionError(
                "terminal-result safe result reference digest does not match"
            )
        if candidate.safe_error_code is not None:
            raise CP7TerminalResultCorruptionError(
                "completed terminal result cannot contain an error code"
            )
        if _SHA256_RE.fullmatch(str(candidate.safe_result_content_sha256)) is None:
            raise CP7TerminalResultCorruptionError(
                "completed terminal result content digest is invalid"
            )
        if (
            isinstance(candidate.safe_result_size_bytes, bool)
            or not isinstance(candidate.safe_result_size_bytes, int)
            or candidate.safe_result_size_bytes < 0
            or candidate.safe_result_size_bytes > MAX_DURABLE_MCP_RESULT_BYTES
        ):
            raise CP7TerminalResultCorruptionError(
                "completed terminal result size is invalid"
            )
        if candidate.safe_result_store_kind != "durable_content_addressed":
            raise CP7TerminalResultCorruptionError(
                "completed terminal result store kind is invalid"
            )
    else:
        if (
            candidate.safe_result_ref is not None
            or candidate.safe_result_ref_sha256 is not None
        ):
            raise CP7TerminalResultCorruptionError(
                "failed or cancelled terminal result cannot contain a result reference"
            )
        _require_closed_string(candidate.safe_error_code, "safe_error_code")
        if (
            candidate.safe_result_content_sha256 is not None
            or candidate.safe_result_size_bytes is not None
            or candidate.safe_result_store_kind is not None
        ):
            raise CP7TerminalResultCorruptionError(
                "failed or cancelled terminal result cannot contain durable result metadata"
            )
    return {
        "candidate_id": candidate.candidate_id,
        "owner_user_id": candidate.owner_user_id,
        "conversation_id": candidate.conversation_id,
        "task_id": candidate.task_id,
        "node_id": candidate.node_id,
        "intent_id": candidate.intent_id,
        "call_id": candidate.call_id,
        "server_id": candidate.server_id,
        "server_config_version": candidate.server_config_version,
        "server_security_version": candidate.server_security_version,
        "terminal_state": terminal_state.value,
        "result_payload_sha256": candidate.result_payload_sha256,
        "safe_result_ref": candidate.safe_result_ref,
        "safe_result_ref_sha256": candidate.safe_result_ref_sha256,
        "safe_error_code": candidate.safe_error_code,
        "safe_result_content_sha256": candidate.safe_result_content_sha256,
        "safe_result_size_bytes": candidate.safe_result_size_bytes,
        "safe_result_store_kind": candidate.safe_result_store_kind,
        "sealed_at": _format_utc_second(candidate.sealed_at),
    }


def _candidate_from_payload(
    payload: Mapping[str, object],
    *,
    schema: str,
) -> MCPValidatedTerminalResultCandidate:
    expected_fields = {
        "candidate_id",
        "owner_user_id",
        "conversation_id",
        "task_id",
        "node_id",
        "intent_id",
        "call_id",
        "server_id",
        "server_config_version",
        "server_security_version",
        "terminal_state",
        "result_payload_sha256",
        "safe_result_ref",
        "safe_result_ref_sha256",
        "safe_error_code",
        "sealed_at",
    }
    if schema == TERMINAL_CANDIDATE_SCHEMA_V2:
        expected_fields.update(
            {
                "safe_result_content_sha256",
                "safe_result_size_bytes",
                "safe_result_store_kind",
            }
        )
    elif schema != TERMINAL_CANDIDATE_SCHEMA_V1:
        raise CP7TerminalResultCorruptionError(
            "terminal-result candidate schema is unsupported"
        )
    if set(payload) != expected_fields:
        raise CP7TerminalResultCorruptionError(
            "terminal-result candidate payload fields are not closed"
        )
    sealed_at = payload["sealed_at"]
    if not isinstance(sealed_at, str) or _UTC_SECOND_RE.fullmatch(sealed_at) is None:
        raise CP7TerminalResultCorruptionError(
            "terminal-result sealed time is not UTC RFC3339 seconds"
        )
    try:
        parsed_at = datetime.strptime(sealed_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        candidate = MCPValidatedTerminalResultCandidate(
            candidate_id=payload["candidate_id"],
            owner_user_id=payload["owner_user_id"],
            conversation_id=payload["conversation_id"],
            task_id=payload["task_id"],
            node_id=payload["node_id"],
            intent_id=payload["intent_id"],
            call_id=payload["call_id"],
            server_id=payload["server_id"],
            server_config_version=payload["server_config_version"],
            server_security_version=payload["server_security_version"],
            terminal_state=MCPTerminalState(payload["terminal_state"]),
            result_payload_sha256=payload["result_payload_sha256"],
            safe_result_ref=payload["safe_result_ref"],
            safe_result_ref_sha256=payload["safe_result_ref_sha256"],
            safe_error_code=payload["safe_error_code"],
            sealed_at=parsed_at,
            safe_result_content_sha256=payload.get("safe_result_content_sha256"),
            safe_result_size_bytes=payload.get("safe_result_size_bytes"),
            safe_result_store_kind=payload.get("safe_result_store_kind"),
        )
    except (TypeError, ValueError) as exc:
        raise CP7TerminalResultCorruptionError(
            "terminal-result candidate payload types are invalid"
        ) from exc
    if schema == TERMINAL_CANDIDATE_SCHEMA_V2:
        _candidate_payload(candidate)
    else:
        _candidate_payload_v1(candidate)
    return candidate


def _candidate_payload_v1(
    candidate: MCPValidatedTerminalResultCandidate,
) -> dict[str, object]:
    legacy = MCPValidatedTerminalResultCandidate(
        candidate_id=candidate.candidate_id,
        owner_user_id=candidate.owner_user_id,
        conversation_id=candidate.conversation_id,
        task_id=candidate.task_id,
        node_id=candidate.node_id,
        intent_id=candidate.intent_id,
        call_id=candidate.call_id,
        server_id=candidate.server_id,
        server_config_version=candidate.server_config_version,
        server_security_version=candidate.server_security_version,
        terminal_state=candidate.terminal_state,
        result_payload_sha256=candidate.result_payload_sha256,
        safe_result_ref=candidate.safe_result_ref,
        safe_result_ref_sha256=candidate.safe_result_ref_sha256,
        safe_error_code=candidate.safe_error_code,
        sealed_at=candidate.sealed_at,
        safe_result_content_sha256=(
            "sha256:" + "0" * 64
            if candidate.terminal_state is MCPTerminalState.COMPLETED
            else None
        ),
        safe_result_size_bytes=(
            0 if candidate.terminal_state is MCPTerminalState.COMPLETED else None
        ),
        safe_result_store_kind=(
            "durable_content_addressed"
            if candidate.terminal_state is MCPTerminalState.COMPLETED
            else None
        ),
    )
    payload = _candidate_payload(legacy)
    for field in (
        "safe_result_content_sha256",
        "safe_result_size_bytes",
        "safe_result_store_kind",
    ):
        payload.pop(field)
    return payload


def _index_payload(
    candidate: MCPValidatedTerminalResultCandidate,
    *,
    candidate_file_sha256: str,
    candidate_payload_sha256: str,
) -> dict[str, str]:
    return {
        "candidate_id": candidate.candidate_id,
        "owner_user_id": candidate.owner_user_id,
        "task_id": candidate.task_id,
        "call_id": candidate.call_id,
        "candidate_file_sha256": candidate_file_sha256,
        "candidate_payload_sha256": candidate_payload_sha256,
    }


def _read_envelope(path: Path, schema: str) -> CanonicalEnvelopeArtifact:
    try:
        return secure_read_canonical_envelope(
            path, expected_schema=schema, maximum_size=_ARTIFACT_SIZE_LIMIT
        )
    except (CP7ArtifactConflictError, CP7ArtifactValidationError) as exc:
        raise CP7TerminalResultCorruptionError(
            "terminal-result artifact is missing, unsafe, or corrupt"
        ) from exc


def _read_candidate_envelope(path: Path) -> CanonicalEnvelopeArtifact:
    try:
        artifact = secure_read(path, expected_mode=0o600, maximum_size=_ARTIFACT_SIZE_LIMIT)
        envelope = parse_canonical_json_bytes(artifact.content)
        if not isinstance(envelope, Mapping):
            raise CP7ArtifactValidationError("terminal-result envelope is invalid")
        schema = envelope.get("schema")
        if schema not in {TERMINAL_CANDIDATE_SCHEMA_V1, TERMINAL_CANDIDATE_SCHEMA_V2}:
            raise CP7ArtifactValidationError("terminal-result candidate schema is unsupported")
        payload = parse_canonical_envelope_bytes(
            artifact.content, expected_schema=str(schema)
        )
        return CanonicalEnvelopeArtifact(
            artifact=artifact,
            envelope=envelope,
            schema=str(schema),
            payload=payload,
            payload_sha256=str(envelope["payload_sha256"]),
        )
    except (CP7ArtifactConflictError, CP7ArtifactValidationError) as exc:
        raise CP7TerminalResultCorruptionError(
            "terminal-result artifact is missing, unsafe, or corrupt"
        ) from exc


def _validated_root(value: str | os.PathLike[str]) -> Path:
    root = Path(value)
    try:
        metadata = root.stat(follow_symlinks=False)
    except (FileNotFoundError, NotADirectoryError, OSError) as exc:
        raise CP7TerminalResultCorruptionError(
            "terminal-result artifact root is unsafe or missing"
        ) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise CP7TerminalResultCorruptionError(
            "terminal-result artifact root metadata is invalid"
        )
    try:
        resolved = root.resolve(strict=True)
        resolved_metadata = resolved.stat(follow_symlinks=False)
    except OSError as exc:
        raise CP7TerminalResultCorruptionError(
            "terminal-result artifact root cannot be resolved safely"
        ) from exc
    if (metadata.st_dev, metadata.st_ino) != (
        resolved_metadata.st_dev,
        resolved_metadata.st_ino,
    ):
        raise CP7TerminalResultCorruptionError(
            "terminal-result artifact root identity changed during validation"
        )
    return resolved


def _bounded_names(root: Path, *, maximum_entries: int) -> tuple[str, ...]:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise CP7TerminalResultCorruptionError(
            "O_NOFOLLOW is required for terminal-result enumeration"
        )
    descriptor = -1
    try:
        descriptor = os.open(root, flags | nofollow)
        before = os.fstat(descriptor)
        names = os.listdir(descriptor)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise CP7TerminalResultCorruptionError(
            "terminal-result artifact root cannot be enumerated safely"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (before.st_dev, before.st_ino, before.st_mode, before.st_uid) != (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_uid,
    ):
        raise CP7TerminalResultCorruptionError(
            "terminal-result artifact root changed during enumeration"
        )
    if len(names) > maximum_entries:
        raise CP7TerminalResultLimitError(
            "terminal-result artifact inventory exceeds its startup bound"
        )
    return tuple(sorted(names, key=lambda name: os.fsencode(name)))


def _format_utc_second(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None or value.microsecond != 0:
        raise CP7TerminalResultCorruptionError(
            "terminal-result sealed time must be timezone-aware at whole-second precision"
        )
    normalized = value.astimezone(timezone.utc)
    return normalized.strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_terminal_utc_second(value: datetime) -> datetime:
    """Return an aware UTC whole-second terminal timestamp.

    SQL lifecycle clocks remain independent and may use the repository's
    existing UTC-naive convention. Terminal candidate evidence must never
    inherit that convention because its canonical contract requires an
    explicit UTC offset.
    """

    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CP7TerminalResultCorruptionError(
            "terminal-result clock must be timezone-aware"
        )
    return value.astimezone(timezone.utc).replace(microsecond=0)


def terminal_now_utc_second() -> datetime:
    """Return the shared default clock for canonical terminal evidence."""

    return datetime.now(timezone.utc).replace(microsecond=0)


def _require_closed_string(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or any(ord(character) < 0x20 for character in value)
    ):
        raise CP7TerminalResultCorruptionError(f"terminal-result {name} is invalid")
    return value


def _path_key(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise CP7TerminalResultCorruptionError(
            "terminal-result path identity is invalid"
        )
    return hashlib.sha256(value.encode("utf-8", errors="strict")).hexdigest()


def _identities_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(
        left.encode("utf-8", errors="strict"),
        right.encode("utf-8", errors="strict"),
    )


__all__ = [
    "CP7TerminalResultCorruptionError",
    "CP7TerminalResultLimitError",
    "DEFAULT_MAXIMUM_TERMINAL_ARTIFACTS",
    "DEFAULT_MAXIMUM_TERMINAL_CANDIDATES",
    "MCPTerminalCandidateSnapshotAuthority",
    "SealedTerminalResultCandidate",
    "TERMINAL_CALL_INDEX_SCHEMA",
    "TERMINAL_CANDIDATE_WARNING_THRESHOLD",
    "TERMINAL_CANDIDATE_SCHEMA",
    "TERMINAL_CANDIDATE_SCHEMA_V1",
    "TERMINAL_CANDIDATE_SCHEMA_V2",
    "TERMINAL_TASK_INDEX_SCHEMA",
    "TerminalResultSealOutcome",
    "compare_terminal_result_candidate",
    "enumerate_unconsumed_terminal_result_candidates",
    "normalize_terminal_utc_second",
    "terminal_now_utc_second",
    "seal_terminal_result_candidate",
    "secure_read_terminal_result_candidate",
    "secure_read_terminal_result_candidate_active_or_archive",
    "terminal_call_index_path",
    "terminal_candidate_path",
    "terminal_task_index_path",
]
