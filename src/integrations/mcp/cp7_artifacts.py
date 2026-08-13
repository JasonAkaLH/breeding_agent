from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


_SHA256_PREFIX = "sha256:"
_RESTORE_SERVICES = ("backend", "frontend", "runtime-sidecar")
_RESTORE_ID_DOMAIN = "maf.user_mcp.cp7.isolated-restore-id.v1"
_RESTORE_RELEASE_PHASES = {
    ("B_L", "bl_prefreeze"),
    ("B_L", "frozen_baseline"),
    ("C_A", "rehearsal"),
    ("C_A", "authoritative_candidate"),
    ("C_B", "retirement"),
    ("R_A", "rollback_to_ca"),
    ("R_L", "rollback_to_bl"),
}
_LOWER_HEX_40_RE = re.compile(r"^[0-9a-f]{40}$")
_APPROVAL_REQUEST_RE = re.compile(r"^cp7a-[0-9a-f]{32}$")
_DIGEST_REFERENCE_RE = re.compile(r"^\S+@sha256:[0-9a-f]{64}$")


class CP7ArtifactError(ValueError):
    """Base error for fail-closed CP7 artifact handling."""


class CP7ArtifactValidationError(CP7ArtifactError):
    """Artifact bytes or filesystem metadata violate the closed contract."""


class CP7ArtifactConflictError(CP7ArtifactError):
    """An immutable target exists with conflicting bytes or identity."""


@dataclass(frozen=True, slots=True)
class SecureArtifact:
    path: Path
    content: bytes
    file_sha256: str
    device: int
    inode: int
    uid: int
    mode: int
    nlink: int
    size: int


@dataclass(frozen=True, slots=True)
class ImmutablePublishResult:
    artifact: SecureArtifact
    created: bool


@dataclass(frozen=True, slots=True)
class CanonicalEnvelopeArtifact:
    artifact: SecureArtifact
    envelope: Mapping[str, Any]
    schema: str
    payload: Mapping[str, Any]
    payload_sha256: str


def canonical_json_bytes(value: Any) -> bytes:
    """Return the sole CP7 JSON representation, including one trailing LF."""

    _validate_json_value(value)
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return rendered.encode("utf-8", errors="strict") + b"\n"
    except (UnicodeEncodeError, TypeError, ValueError) as exc:
        raise CP7ArtifactValidationError("cp7 canonical JSON value is invalid") from exc


def parse_canonical_json_bytes(content: bytes) -> Any:
    """Parse JSON only when the input is byte-for-byte canonical CP7 JSON."""

    if not isinstance(content, bytes):
        raise CP7ArtifactValidationError("cp7 canonical JSON input must be bytes")
    if not content.endswith(b"\n") or content.endswith(b"\n\n"):
        raise CP7ArtifactValidationError("cp7 canonical JSON must end with one LF")
    try:
        text = content[:-1].decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_closed_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, CP7ArtifactValidationError) as exc:
        if isinstance(exc, CP7ArtifactValidationError):
            raise
        raise CP7ArtifactValidationError("cp7 canonical JSON cannot be parsed") from exc
    if canonical_json_bytes(value) != content:
        raise CP7ArtifactValidationError("cp7 JSON input is not canonical")
    return value


def canonical_sha256(value: Any) -> str:
    return _SHA256_PREFIX + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def canonical_envelope(schema: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(schema, str) or not schema:
        raise CP7ArtifactValidationError("cp7 envelope schema must be non-empty")
    if not isinstance(payload, Mapping):
        raise CP7ArtifactValidationError("cp7 envelope payload must be an object")
    closed_payload = dict(payload)
    return {
        "schema": schema,
        "payload": closed_payload,
        "payload_sha256": canonical_sha256(closed_payload),
    }


def canonical_envelope_bytes(schema: str, payload: Mapping[str, Any]) -> bytes:
    return canonical_json_bytes(canonical_envelope(schema, payload))


def parse_canonical_envelope_bytes(
    content: bytes,
    *,
    expected_schema: str,
) -> Mapping[str, Any]:
    envelope = parse_canonical_json_bytes(content)
    if not isinstance(envelope, Mapping) or set(envelope) != {
        "schema",
        "payload",
        "payload_sha256",
    }:
        raise CP7ArtifactValidationError("cp7 envelope fields are not closed")
    if envelope["schema"] != expected_schema:
        raise CP7ArtifactValidationError("cp7 envelope schema does not match")
    payload = envelope["payload"]
    payload_sha256 = envelope["payload_sha256"]
    if not isinstance(payload, Mapping) or not _is_sha256(payload_sha256):
        raise CP7ArtifactValidationError("cp7 envelope payload or digest is invalid")
    expected_digest = canonical_sha256(payload)
    if not hmac.compare_digest(payload_sha256, expected_digest):
        raise CP7ArtifactValidationError("cp7 envelope payload digest does not match")
    return dict(payload)


def deterministic_id(*, prefix: str, domain: str, subject: Mapping[str, Any]) -> str:
    if not prefix or not domain:
        raise CP7ArtifactValidationError("cp7 deterministic ID prefix/domain is required")
    try:
        domain_bytes = domain.encode("ascii", errors="strict")
    except UnicodeEncodeError as exc:
        raise CP7ArtifactValidationError("cp7 deterministic ID domain must be ASCII") from exc
    material = domain_bytes + b"\0" + canonical_json_bytes(dict(subject))
    return prefix + hashlib.sha256(material).hexdigest()


def cp7_restore_id(
    *,
    approval_request_id: str,
    release: str,
    phase: str,
    commit: str,
    tree: str,
    daemon_reference: str,
    exports: Sequence[Mapping[str, str]],
) -> str:
    if _APPROVAL_REQUEST_RE.fullmatch(approval_request_id) is None:
        raise CP7ArtifactValidationError("cp7 restore approval request ID is invalid")
    if (release, phase) not in _RESTORE_RELEASE_PHASES:
        raise CP7ArtifactValidationError("cp7 restore release/phase tuple is invalid")
    if _LOWER_HEX_40_RE.fullmatch(commit) is None or _LOWER_HEX_40_RE.fullmatch(tree) is None:
        raise CP7ArtifactValidationError("cp7 restore commit/tree identity is invalid")
    if _DIGEST_REFERENCE_RE.fullmatch(daemon_reference) is None:
        raise CP7ArtifactValidationError("cp7 restore daemon reference is not immutable")
    if len(exports) != len(_RESTORE_SERVICES):
        raise CP7ArtifactValidationError("cp7 restore exports must contain three services")
    closed_exports: list[dict[str, str]] = []
    for expected_service, item in zip(_RESTORE_SERVICES, exports, strict=True):
        if set(item) != {"service", "sha256"} or item.get("service") != expected_service:
            raise CP7ArtifactValidationError("cp7 restore exports use a fixed service order")
        digest = item.get("sha256")
        if not _is_sha256(digest):
            raise CP7ArtifactValidationError("cp7 restore export digest is invalid")
        closed_exports.append({"service": expected_service, "sha256": digest})
    subject = {
        "approval_request_id": approval_request_id,
        "release": release,
        "phase": phase,
        "commit": commit,
        "tree": tree,
        "daemon_reference": daemon_reference,
        "exports": closed_exports,
    }
    return deterministic_id(
        prefix="cp7-restore-v1-",
        domain=_RESTORE_ID_DOMAIN,
        subject=subject,
    )


def mcp_no_server_intent_id(task_id: str, *, node_id: str | None = None) -> str:
    _require_identifier_component(task_id, "task_id")
    suffix = "initial" if node_id is None else _require_identifier_component(node_id, "node_id")
    return f"mcp-no-server-intent:v1:{task_id}:{suffix}"


def mcp_dispatch_resume_outbox_id(intent_id: str) -> str:
    return "mcp-dispatch-resume:v1:" + _require_identifier_component(
        intent_id, "intent_id"
    )


def mcp_terminal_candidate_id(call_id: str, result_payload_sha256: str) -> str:
    _require_identifier_component(call_id, "call_id")
    _require_sha256(result_payload_sha256, "result_payload_sha256")
    return f"mcp-terminal-candidate:v1:{call_id}:{result_payload_sha256}"


def mcp_terminal_receipt_id(call_id: str, result_payload_sha256: str) -> str:
    _require_identifier_component(call_id, "call_id")
    _require_sha256(result_payload_sha256, "result_payload_sha256")
    return f"mcp-terminal-result:v1:{call_id}:{result_payload_sha256}"


def mcp_terminal_projection_id(call_id: str) -> str:
    return "mcp-terminal-projection:v1:" + _require_identifier_component(
        call_id, "call_id"
    )


def publish_immutable(
    path: str | os.PathLike[str],
    content: bytes,
    *,
    mode: int = 0o600,
    maximum_size: int = 64 * 1024 * 1024,
) -> SecureArtifact:
    """Publish bytes once through a durable temp-link-unlink sequence."""

    if not isinstance(content, bytes):
        raise CP7ArtifactValidationError("cp7 artifact content must be bytes")
    if len(content) > maximum_size:
        raise CP7ArtifactValidationError("cp7 artifact exceeds its size limit")
    if mode != 0o600:
        raise CP7ArtifactValidationError("cp7 immutable artifacts require mode 0600")
    artifact_path = Path(path)
    parent_fd, basename = _open_parent(artifact_path)
    temp_name = f".{basename}.tmp-{secrets.token_hex(16)}"
    descriptor = -1
    try:
        _validate_parent_directory_fd(parent_fd)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _o_cloexec() | _o_nofollow()
        descriptor = os.open(temp_name, flags, mode, dir_fd=parent_fd)
        os.fchmod(descriptor, mode)
        _write_all(descriptor, content)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        try:
            _validate_parent_directory_fd(parent_fd)
            os.link(
                temp_name,
                basename,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise CP7ArtifactConflictError("cp7 immutable artifact already exists") from exc
        os.fsync(parent_fd)
        os.unlink(temp_name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temp_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        else:
            os.fsync(parent_fd)
        os.close(parent_fd)
    return secure_read(
        artifact_path,
        expected_mode=mode,
        maximum_size=maximum_size,
    )


def publish_or_compare_immutable(
    path: str | os.PathLike[str],
    content: bytes,
    *,
    mode: int = 0o600,
    maximum_size: int = 64 * 1024 * 1024,
) -> ImmutablePublishResult:
    try:
        artifact = publish_immutable(
            path,
            content,
            mode=mode,
            maximum_size=maximum_size,
        )
        return ImmutablePublishResult(artifact=artifact, created=True)
    except CP7ArtifactConflictError:
        artifact = secure_read(path, expected_mode=mode, maximum_size=maximum_size)
        if artifact.content != content:
            raise CP7ArtifactConflictError(
                "cp7 immutable artifact exists with different bytes"
            )
        return ImmutablePublishResult(artifact=artifact, created=False)


def secure_read(
    path: str | os.PathLike[str],
    *,
    expected_uid: int | None = None,
    expected_mode: int = 0o600,
    maximum_size: int = 64 * 1024 * 1024,
) -> SecureArtifact:
    artifact_path = Path(path)
    parent_fd, basename = _open_parent(artifact_path)
    descriptor = -1
    try:
        _validate_parent_directory_fd(parent_fd)
        before_path = os.stat(basename, dir_fd=parent_fd, follow_symlinks=False)
        descriptor = os.open(
            basename,
            os.O_RDONLY | _o_cloexec() | _o_nofollow(),
            dir_fd=parent_fd,
        )
        before = os.fstat(descriptor)
        _validate_file_metadata(
            before,
            expected_uid=os.getuid() if expected_uid is None else expected_uid,
            expected_mode=expected_mode,
            maximum_size=maximum_size,
        )
        if not _same_identity(before_path, before):
            raise CP7ArtifactValidationError("cp7 artifact path identity changed before read")
        content = _read_exact(descriptor, before.st_size)
        after = os.fstat(descriptor)
        after_path = os.stat(basename, dir_fd=parent_fd, follow_symlinks=False)
        _validate_parent_directory_fd(parent_fd)
        if not _same_identity(before, after) or not _same_identity(after, after_path):
            raise CP7ArtifactValidationError("cp7 artifact identity changed during read")
        if after.st_size != len(content):
            raise CP7ArtifactValidationError("cp7 artifact size changed during read")
        return SecureArtifact(
            path=artifact_path,
            content=content,
            file_sha256=_SHA256_PREFIX + hashlib.sha256(content).hexdigest(),
            device=after.st_dev,
            inode=after.st_ino,
            uid=after.st_uid,
            mode=stat.S_IMODE(after.st_mode),
            nlink=after.st_nlink,
            size=after.st_size,
        )
    except (FileNotFoundError, NotADirectoryError, OSError) as exc:
        if isinstance(exc, CP7ArtifactValidationError):
            raise
        raise CP7ArtifactValidationError("cp7 artifact path is unsafe or missing") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)


def secure_read_canonical_envelope(
    path: str | os.PathLike[str],
    *,
    expected_schema: str,
    expected_uid: int | None = None,
    expected_mode: int = 0o600,
    maximum_size: int = 64 * 1024 * 1024,
) -> CanonicalEnvelopeArtifact:
    artifact = secure_read(
        path,
        expected_uid=expected_uid,
        expected_mode=expected_mode,
        maximum_size=maximum_size,
    )
    payload = parse_canonical_envelope_bytes(
        artifact.content,
        expected_schema=expected_schema,
    )
    envelope = parse_canonical_json_bytes(artifact.content)
    return CanonicalEnvelopeArtifact(
        artifact=artifact,
        envelope=envelope,
        schema=expected_schema,
        payload=payload,
        payload_sha256=str(envelope["payload_sha256"]),
    )


def _validate_json_value(value: Any) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str):
            try:
                value.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise CP7ArtifactValidationError("cp7 JSON contains a surrogate") from exc
        return
    if isinstance(value, float):
        raise CP7ArtifactValidationError("cp7 canonical JSON does not accept floats")
    if isinstance(value, list) or isinstance(value, tuple):
        for item in value:
            _validate_json_value(item)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CP7ArtifactValidationError("cp7 JSON object keys must be strings")
            _validate_json_value(key)
            _validate_json_value(item)
        return
    raise CP7ArtifactValidationError("cp7 JSON value has an unsupported type")


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CP7ArtifactValidationError("cp7 JSON object contains a duplicate key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise CP7ArtifactValidationError(f"cp7 JSON constant is invalid: {value}")


def _open_parent(path: Path) -> tuple[int, str]:
    if not path.name or path.name in {".", ".."}:
        raise CP7ArtifactValidationError("cp7 artifact basename is invalid")
    parent = path.parent
    parts = parent.parts
    if parent.is_absolute():
        descriptor = os.open("/", os.O_RDONLY | _o_directory() | _o_cloexec())
        parts = parts[1:]
    else:
        descriptor = os.open(".", os.O_RDONLY | _o_directory() | _o_cloexec())
    try:
        for part in parts:
            if part in {"", "."}:
                continue
            if part == "..":
                raise CP7ArtifactValidationError("cp7 artifact paths cannot traverse parents")
            next_descriptor = os.open(
                part,
                os.O_RDONLY | _o_directory() | _o_cloexec() | _o_nofollow(),
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        _validate_parent_directory_fd(descriptor)
        return descriptor, path.name
    except OSError as exc:
        os.close(descriptor)
        raise CP7ArtifactValidationError(
            "cp7 artifact parent path is unsafe or missing"
        ) from exc
    except BaseException:
        os.close(descriptor)
        raise


def _validate_file_metadata(
    metadata: os.stat_result,
    *,
    expected_uid: int,
    expected_mode: int,
    maximum_size: int,
) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise CP7ArtifactValidationError("cp7 artifact is not a regular file")
    if metadata.st_uid != expected_uid:
        raise CP7ArtifactValidationError("cp7 artifact owner is invalid")
    if stat.S_IMODE(metadata.st_mode) != expected_mode:
        raise CP7ArtifactValidationError("cp7 artifact mode is invalid")
    if metadata.st_nlink != 1:
        raise CP7ArtifactValidationError("cp7 artifact link count is invalid")
    if metadata.st_size < 0 or metadata.st_size > maximum_size:
        raise CP7ArtifactValidationError("cp7 artifact size is invalid")


def _validate_parent_directory_fd(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        raise CP7ArtifactValidationError("cp7 artifact parent is not a directory")
    if metadata.st_uid != os.getuid():
        raise CP7ArtifactValidationError("cp7 artifact parent owner is invalid")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise CP7ArtifactValidationError("cp7 artifact parent mode is too broad")


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_uid,
        left.st_nlink,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_uid,
        right.st_nlink,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise CP7ArtifactValidationError("cp7 artifact write did not progress")
        view = view[written:]


def _read_exact(descriptor: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(descriptor, min(remaining, 1024 * 1024))
        if not chunk:
            raise CP7ArtifactValidationError("cp7 artifact was truncated during read")
        chunks.append(chunk)
        remaining -= len(chunk)
    if os.read(descriptor, 1):
        raise CP7ArtifactValidationError("cp7 artifact grew during read")
    return b"".join(chunks)


def _is_sha256(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith(_SHA256_PREFIX):
        return False
    suffix = value.removeprefix(_SHA256_PREFIX)
    return len(suffix) == 64 and all(character in "0123456789abcdef" for character in suffix)


def _require_identifier_component(value: str, name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise CP7ArtifactValidationError(f"cp7 {name} is invalid")
    return value


def _require_sha256(value: str, name: str) -> str:
    if not _is_sha256(value):
        raise CP7ArtifactValidationError(f"cp7 {name} is invalid")
    return value


def _o_nofollow() -> int:
    value = getattr(os, "O_NOFOLLOW", None)
    if value is None:
        raise CP7ArtifactValidationError("O_NOFOLLOW is required for CP7 artifacts")
    return int(value)


def _o_directory() -> int:
    value = getattr(os, "O_DIRECTORY", None)
    if value is None:
        raise CP7ArtifactValidationError("O_DIRECTORY is required for CP7 artifacts")
    return int(value)


def _o_cloexec() -> int:
    return int(getattr(os, "O_CLOEXEC", 0))


__all__ = [
    "CP7ArtifactConflictError",
    "CP7ArtifactError",
    "CP7ArtifactValidationError",
    "CanonicalEnvelopeArtifact",
    "ImmutablePublishResult",
    "SecureArtifact",
    "canonical_envelope",
    "canonical_envelope_bytes",
    "canonical_json_bytes",
    "canonical_sha256",
    "cp7_restore_id",
    "deterministic_id",
    "mcp_dispatch_resume_outbox_id",
    "mcp_no_server_intent_id",
    "mcp_terminal_candidate_id",
    "mcp_terminal_projection_id",
    "mcp_terminal_receipt_id",
    "parse_canonical_envelope_bytes",
    "parse_canonical_json_bytes",
    "publish_immutable",
    "publish_or_compare_immutable",
    "secure_read",
    "secure_read_canonical_envelope",
]
