from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

from .mcp_runtime_gates import (
    load_mcp_runtime_artifact_trust,
    validate_mcp_runtime_artifact_provenance,
)
from .protocol import MCP_PROTOCOL_VERSION, SUPPORTED_MCP_PROTOCOL_VERSIONS

MCP_SIDECAR_COMPONENT = "maf_mcp_runtime_sidecar"
MCP_SIDECAR_PROTOCOL_VERSION = "maf.mcp.sidecar.v1"
MCP_SIDECAR_CLIENT_VERSION = "0.1.0"

_DEFAULT_SUPPORTED_FEATURES = frozenset(
    {
        "health",
        "readiness",
        "version",
        "compatibility_handshake",
    }
)


class MCPSidecarMode(StrEnum):
    OFF = "off"
    SHADOW = "shadow"
    ENFORCE = "enforce"


@dataclass(slots=True, frozen=True)
class MCPRustRuntimeSettings:
    mode: MCPSidecarMode = MCPSidecarMode.OFF
    endpoint: str = ""
    expected_schema_hash: str = ""
    expected_error_code_table_hash: str = ""
    required_features: frozenset[str] = frozenset()
    artifact_manifest_path: str = ""
    artifact_allowlist_path: str = ""
    connect_timeout_seconds: float = 2.0
    request_timeout_seconds: float = 30.0

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "MCPRustRuntimeSettings":
        env = os.environ if environ is None else environ
        return cls.from_mapping(
            {
                "mode": env.get("MAF_RUST_MCP_RUNTIME_MODE", "off"),
                "endpoint": env.get("MAF_RUST_MCP_RUNTIME_ENDPOINT", ""),
                "expected_schema_hash": env.get("MAF_RUST_MCP_RUNTIME_SCHEMA_HASH", ""),
                "expected_error_code_table_hash": env.get("MAF_RUST_MCP_RUNTIME_ERROR_CODE_TABLE_HASH", ""),
                "required_features": _split_csv(env.get("MAF_RUST_MCP_RUNTIME_REQUIRED_FEATURES", "")),
                "artifact_manifest_path": env.get("MAF_RUST_MCP_RUNTIME_ARTIFACT_MANIFEST_PATH", ""),
                "artifact_allowlist_path": env.get("MAF_RUST_MCP_RUNTIME_ARTIFACT_ALLOWLIST_PATH", ""),
                "connect_timeout_seconds": env.get("MAF_RUST_MCP_RUNTIME_CONNECT_TIMEOUT_SECONDS", "2"),
                "request_timeout_seconds": env.get("MAF_RUST_MCP_RUNTIME_REQUEST_TIMEOUT_SECONDS", "30"),
            }
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "MCPRustRuntimeSettings":
        raw = dict(payload or {})
        mode = _parse_mode(raw.get("mode", "off"))
        return cls(
            mode=mode,
            endpoint=str(raw.get("endpoint") or "").strip(),
            expected_schema_hash=str(raw.get("expected_schema_hash") or raw.get("schema_hash") or "").strip(),
            expected_error_code_table_hash=str(
                raw.get("expected_error_code_table_hash") or raw.get("error_code_table_hash") or ""
            ).strip(),
            required_features=frozenset(
                str(item).strip() for item in raw.get("required_features") or () if str(item).strip()
            ),
            artifact_manifest_path=str(raw.get("artifact_manifest_path") or "").strip(),
            artifact_allowlist_path=str(raw.get("artifact_allowlist_path") or "").strip(),
            connect_timeout_seconds=_positive_float(raw.get("connect_timeout_seconds"), 2.0),
            request_timeout_seconds=_positive_float(raw.get("request_timeout_seconds"), 30.0),
        )

    def validation_error(self) -> str:
        if self.mode == MCPSidecarMode.OFF:
            return ""
        if not self.endpoint:
            return "MAF_RUST_MCP_RUNTIME_ENDPOINT is required when MCP Rust runtime mode is shadow or enforce."
        if not _is_allowed_internal_endpoint(self.endpoint):
            return "MCP Rust sidecar endpoint must be an internal unix:// or loopback endpoint."
        artifact_error = self.artifact_trust_error()
        if artifact_error:
            return artifact_error
        return ""

    def artifact_trust_error(self) -> str:
        if not self.artifact_manifest_path and not self.artifact_allowlist_path:
            if self.mode == MCPSidecarMode.ENFORCE:
                return (
                    "mcp_runtime_artifact_untrusted: MCP Rust sidecar enforce mode requires an artifact manifest "
                    "and allowlist."
                )
            return ""
        if not self.artifact_manifest_path or not self.artifact_allowlist_path:
            return (
                "mcp_runtime_artifact_untrusted: MCP Rust sidecar artifact trust requires both manifest "
                "and allowlist paths."
            )
        try:
            load_mcp_runtime_artifact_trust(
                manifest_path=self.artifact_manifest_path,
                allowlist_path=self.artifact_allowlist_path,
            )
        except RuntimeError as exc:
            return str(exc)
        return ""


@dataclass(slots=True, frozen=True)
class MCPSidecarVersionInfo:
    component: str
    build_version: str
    protocol_version: str
    schema_hash: str
    error_code_table_hash: str
    supported_features: frozenset[str]
    min_client_version: str
    max_client_version: str
    external_mcp_protocol_version: str = MCP_PROTOCOL_VERSION
    external_mcp_protocol_versions: tuple[str, ...] = (MCP_PROTOCOL_VERSION,)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "MCPSidecarVersionInfo":
        external_version = str(payload.get("external_mcp_protocol_version") or MCP_PROTOCOL_VERSION)
        return cls(
            component=str(payload.get("component") or ""),
            build_version=str(payload.get("build_version") or ""),
            protocol_version=str(payload.get("protocol_version") or ""),
            schema_hash=str(payload.get("schema_hash") or ""),
            error_code_table_hash=str(payload.get("error_code_table_hash") or ""),
            supported_features=frozenset(str(item) for item in payload.get("supported_features") or ()),
            min_client_version=str(payload.get("min_client_version") or ""),
            max_client_version=str(payload.get("max_client_version") or ""),
            external_mcp_protocol_version=external_version,
            external_mcp_protocol_versions=_external_protocol_versions(
                payload.get("external_mcp_protocol_versions"),
                fallback=external_version,
            ),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "build_version": self.build_version,
            "protocol_version": self.protocol_version,
            "schema_hash": self.schema_hash,
            "error_code_table_hash": self.error_code_table_hash,
            "supported_features": tuple(sorted(self.supported_features)),
            "min_client_version": self.min_client_version,
            "max_client_version": self.max_client_version,
            "external_mcp_protocol_version": self.external_mcp_protocol_version,
            "external_mcp_protocol_versions": self.external_mcp_protocol_versions,
        }

    def with_updates(self, **updates: Any) -> "MCPSidecarVersionInfo":
        return replace(self, **updates)


class MCPSidecarError(RuntimeError):
    def __init__(self, message: str, *, code: str = "mcp_runtime_sidecar_error", metadata: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.mcp_error_code = code
        self.metadata = dict(metadata or {})


class MCPSidecarCompatibilityError(MCPSidecarError):
    def __init__(self, message: str, *, metadata: Mapping[str, Any] | None = None) -> None:
        super().__init__(message, code="mcp_runtime_sidecar_incompatible", metadata=metadata)


class MCPFeatureUnsupportedError(MCPSidecarError):
    def __init__(self, feature: str) -> None:
        super().__init__(
            f"MCP Rust sidecar feature is not supported: {feature}",
            code="mcp_runtime_sidecar_feature_unsupported",
            metadata={"feature": feature},
        )


@runtime_checkable
class MCPSidecarTransport(Protocol):
    async def health(self) -> Mapping[str, Any]: ...

    async def readiness(self) -> Mapping[str, Any]: ...

    async def version(self) -> Mapping[str, Any] | MCPSidecarVersionInfo: ...

    async def close(self) -> None: ...


class InMemoryMCPSidecarTransport:
    """Deterministic test/dev transport for the Python facade; production uses gRPC."""

    def __init__(self, *, version: MCPSidecarVersionInfo | None = None, healthy: bool = True) -> None:
        self._version = version or MCPSidecarVersionInfo(
            component=MCP_SIDECAR_COMPONENT,
            build_version="dev",
            protocol_version=MCP_SIDECAR_PROTOCOL_VERSION,
            schema_hash="dev-schema",
            error_code_table_hash="dev-errors",
            supported_features=_DEFAULT_SUPPORTED_FEATURES,
            min_client_version=MCP_SIDECAR_CLIENT_VERSION,
            max_client_version=MCP_SIDECAR_CLIENT_VERSION,
            external_mcp_protocol_version=MCP_PROTOCOL_VERSION,
        )
        self._healthy = healthy
        self._ready = False
        self.closed = False

    async def health(self) -> Mapping[str, Any]:
        return {"healthy": self._healthy, "component": self._version.component}

    async def readiness(self) -> Mapping[str, Any]:
        return {"ready": self._ready, "component": self._version.component}

    async def version(self) -> MCPSidecarVersionInfo:
        return self._version

    def mark_ready(self) -> None:
        self._ready = True

    async def close(self) -> None:
        self.closed = True


class MCPSidecarClient:
    def __init__(
        self,
        *,
        transport: MCPSidecarTransport,
        client_version: str = MCP_SIDECAR_CLIENT_VERSION,
        expected_schema_hash: str = "",
        expected_error_code_table_hash: str = "",
        artifact_provenance: Mapping[str, Any] | None = None,
        allowed_artifact_checksums: tuple[str, ...] = (),
        allowed_cargo_lock_digests: tuple[str, ...] = (),
    ) -> None:
        if artifact_provenance is not None:
            validate_mcp_runtime_artifact_provenance(
                artifact_provenance,
                allowed_checksums=set(allowed_artifact_checksums),
                allowed_cargo_lock_digests=set(allowed_cargo_lock_digests),
            )
        self._transport = transport
        self._client_version = client_version
        self._expected_schema_hash = expected_schema_hash
        self._expected_error_code_table_hash = expected_error_code_table_hash
        self._version: MCPSidecarVersionInfo | None = None
        self._ready = False

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def version_info(self) -> MCPSidecarVersionInfo | None:
        return self._version

    async def health(self) -> Mapping[str, Any]:
        return dict(await self._transport.health())

    async def readiness(self) -> Mapping[str, Any]:
        if not self._ready:
            return {"ready": False, "component": MCP_SIDECAR_COMPONENT}
        payload = dict(await self._transport.readiness())
        return {**payload, "ready": True, "component": payload.get("component") or MCP_SIDECAR_COMPONENT}

    async def handshake(self, *, required_features: Iterable[str] = ()) -> MCPSidecarVersionInfo:
        raw = await self._transport.version()
        version = raw if isinstance(raw, MCPSidecarVersionInfo) else MCPSidecarVersionInfo.from_mapping(raw)
        self._validate_compatibility(version, required_features=frozenset(required_features))
        self._version = version
        self._ready = True
        mark_ready = getattr(self._transport, "mark_ready", None)
        if callable(mark_ready):
            mark_ready()
        return version

    def require_feature(self, feature: str) -> None:
        if self._version is None or feature not in self._version.supported_features:
            raise MCPFeatureUnsupportedError(feature)

    async def close(self) -> None:
        await self._transport.close()

    def _validate_compatibility(self, version: MCPSidecarVersionInfo, *, required_features: frozenset[str]) -> None:
        if version.component != MCP_SIDECAR_COMPONENT:
            raise MCPSidecarCompatibilityError("MCP sidecar component mismatch.", metadata={"component": version.component})
        if version.protocol_version != MCP_SIDECAR_PROTOCOL_VERSION:
            raise MCPSidecarCompatibilityError(
                "MCP sidecar protocol_version mismatch.",
                metadata={"protocol_version": version.protocol_version},
            )
        if version.external_mcp_protocol_version != MCP_PROTOCOL_VERSION:
            raise MCPSidecarCompatibilityError(
                "MCP sidecar external MCP protocol version mismatch.",
                metadata={"external_mcp_protocol_version": version.external_mcp_protocol_version},
            )
        if (
            not version.external_mcp_protocol_versions
            or MCP_PROTOCOL_VERSION not in version.external_mcp_protocol_versions
            or any(item not in SUPPORTED_MCP_PROTOCOL_VERSIONS for item in version.external_mcp_protocol_versions)
        ):
            raise MCPSidecarCompatibilityError(
                "MCP sidecar external MCP protocol versions mismatch.",
                metadata={"external_mcp_protocol_versions": version.external_mcp_protocol_versions},
            )
        if (
            len(set(version.external_mcp_protocol_versions)) > 1
            and "multi_version_transport" not in version.supported_features
        ):
            raise MCPSidecarCompatibilityError(
                "MCP sidecar cannot claim multi-version external transport without multi_version_transport feature.",
                metadata={"external_mcp_protocol_versions": version.external_mcp_protocol_versions},
            )
        if self._expected_schema_hash and version.schema_hash != self._expected_schema_hash:
            raise MCPSidecarCompatibilityError("MCP sidecar schema_hash mismatch.", metadata={"schema_hash": version.schema_hash})
        if self._expected_error_code_table_hash and version.error_code_table_hash != self._expected_error_code_table_hash:
            raise MCPSidecarCompatibilityError(
                "MCP sidecar error_code_table_hash mismatch.",
                metadata={"error_code_table_hash": version.error_code_table_hash},
            )
        if self._client_version < version.min_client_version or self._client_version > version.max_client_version:
            raise MCPSidecarCompatibilityError(
                "MCP sidecar client version is outside the supported range.",
                metadata={
                    "client_version": self._client_version,
                    "min_client_version": version.min_client_version,
                    "max_client_version": version.max_client_version,
                },
            )
        missing = sorted(required_features - version.supported_features)
        if missing:
            raise MCPSidecarCompatibilityError("MCP sidecar missing required features.", metadata={"missing_features": tuple(missing)})


def _parse_mode(value: Any) -> MCPSidecarMode:
    raw = str(value or "off").strip().lower()
    try:
        return MCPSidecarMode(raw)
    except ValueError as exc:
        raise ValueError("MAF_RUST_MCP_RUNTIME_MODE must be one of: off, shadow, enforce") from exc


def _positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _external_protocol_versions(value: Any, *, fallback: str) -> tuple[str, ...]:
    if value is None:
        return (fallback,)
    if isinstance(value, str):
        items = (value,)
    else:
        try:
            items = tuple(str(item) for item in value)
        except TypeError:
            return ()
    parsed = tuple(item.strip() for item in items if item.strip())
    return parsed


def _is_allowed_internal_endpoint(endpoint: str) -> bool:
    try:
        parsed = urlparse(endpoint)
        hostname = parsed.hostname
    except ValueError:
        return False
    if parsed.scheme == "unix":
        return bool(parsed.path or parsed.netloc)
    if parsed.scheme != "http":
        return False
    if parsed.username or parsed.password:
        return False
    return hostname in {"localhost", "127.0.0.1", "::1"}
