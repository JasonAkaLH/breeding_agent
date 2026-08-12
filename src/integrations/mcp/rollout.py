from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any


MCP_ROLLOUT_COHORT_SCHEMA_VERSION = "maf.mcp.rollout_cohorts.v1"
MCP_ROLLOUT_HASH_ALGORITHM_VERSION = "hmac-sha256-percent-v1"

_GATEWAY_ENABLED_ENV = "MCP_USER_SCOPED_GATEWAY_ENABLED"
_ROUTING_MODE_ENV = "MCP_ROUTING_MODE"
_LEGACY_ENABLED_ENV = "MCP_LEGACY_GLOBAL_RUNTIME_ENABLED"
_ENFORCE_COHORTS_ENV = "MCP_ENFORCE_COHORTS"
_ENFORCE_PERCENT_ENV = "MCP_ENFORCE_PERCENT"
_ENFORCE_HASH_SALT_ENV = "MCP_ENFORCE_HASH_SALT"
_COHORT_CONFIG_FILE_ENV = "MCP_ENFORCE_COHORT_CONFIG_FILE"
MCP_ROLLOUT_ENV_KEYS = frozenset(
    {
        _GATEWAY_ENABLED_ENV,
        _ROUTING_MODE_ENV,
        _LEGACY_ENABLED_ENV,
        _ENFORCE_COHORTS_ENV,
        _ENFORCE_PERCENT_ENV,
        _ENFORCE_HASH_SALT_ENV,
        _COHORT_CONFIG_FILE_ENV,
    }
)
_COHORT_FILE_KEYS = frozenset({"schema_version", "config_version", "user_cohorts"})
_MAX_COHORT_FILE_BYTES = 4 * 1024 * 1024


class MCPRolloutConfigError(ValueError):
    """The process cannot safely start with the supplied rollout configuration."""


class MCPRoutingMode(StrEnum):
    OFF = "off"
    SHADOW = "shadow"
    ENFORCE = "enforce"


class MCPExecutionPath(StrEnum):
    LEGACY = "legacy"
    USER_SCOPED = "user_scoped"
    UNAVAILABLE = "unavailable"


class MCPRouteReason(StrEnum):
    ROUTING_OFF = "routing_off"
    SHADOW_ENABLED = "shadow_enabled"
    ENFORCE_SELECTED = "enforce_selected"
    COHORT_NOT_SELECTED = "cohort_not_selected"
    PERCENT_NOT_SELECTED = "percent_not_selected"
    EXPLICIT_LEGACY_CAPABILITY = "explicit_legacy_capability"
    USER_SERVER_ROLLOUT_UNAVAILABLE = "user_server_rollout_unavailable"
    NO_EXECUTION_PATH = "no_execution_path"


class MCPExposureChange(StrEnum):
    DECREASE = "decrease"
    UNCHANGED = "unchanged"
    INCREASE = "increase"


@dataclass(frozen=True, slots=True)
class MCPTaskRouteAssignment:
    routing_mode: MCPRoutingMode
    real_path: MCPExecutionPath
    shadow_enabled: bool
    config_version: str
    reason_code: MCPRouteReason


@dataclass(frozen=True, slots=True)
class MCPRolloutConfig:
    gateway_enabled: bool = False
    routing_mode: MCPRoutingMode = MCPRoutingMode.OFF
    legacy_enabled: bool = True
    enforce_cohorts: frozenset[str] = frozenset()
    enforce_percent: int = 0
    enforce_hash_salt: str = field(default="", repr=False)
    cohort_config_version: str | None = None
    cohort_file_digest: str = ""
    _user_cohorts: Mapping[str, frozenset[str]] = field(
        default_factory=lambda: MappingProxyType({}),
        repr=False,
    )
    fingerprint: str = field(init=False)
    config_version: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "routing_mode", _coerce_enum(self.routing_mode, MCPRoutingMode, _ROUTING_MODE_ENV))
        object.__setattr__(self, "enforce_cohorts", frozenset(self.enforce_cohorts))
        frozen_mapping = MappingProxyType(
            {user_id: frozenset(cohort_ids) for user_id, cohort_ids in self._user_cohorts.items()}
        )
        object.__setattr__(self, "_user_cohorts", frozen_mapping)
        self._validate()
        fingerprint = _config_fingerprint(self)
        object.__setattr__(self, "fingerprint", fingerprint)
        object.__setattr__(self, "config_version", f"mcp-rollout-v1:{fingerprint}")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> MCPRolloutConfig:
        source = os.environ if env is None else env
        gateway_enabled = _parse_bool(source.get(_GATEWAY_ENABLED_ENV), default=False, name=_GATEWAY_ENABLED_ENV)
        routing_mode = _parse_routing_mode(source.get(_ROUTING_MODE_ENV))
        legacy_enabled = _parse_bool(source.get(_LEGACY_ENABLED_ENV), default=True, name=_LEGACY_ENABLED_ENV)
        enforce_cohorts = _parse_cohort_filter(source.get(_ENFORCE_COHORTS_ENV, ""))
        enforce_percent = _parse_percent(source.get(_ENFORCE_PERCENT_ENV))
        enforce_hash_salt = source.get(_ENFORCE_HASH_SALT_ENV, "")
        if enforce_hash_salt != enforce_hash_salt.strip():
            raise MCPRolloutConfigError(f"{_ENFORCE_HASH_SALT_ENV} must not contain surrounding whitespace")

        cohort_path_raw = source.get(_COHORT_CONFIG_FILE_ENV, "")
        cohort_config_version: str | None = None
        cohort_file_digest = ""
        user_cohorts: Mapping[str, frozenset[str]] = MappingProxyType({})
        if cohort_path_raw:
            if cohort_path_raw != cohort_path_raw.strip():
                raise MCPRolloutConfigError(f"{_COHORT_CONFIG_FILE_ENV} must not contain surrounding whitespace")
            cohort_snapshot = _load_cohort_file(Path(cohort_path_raw))
            cohort_config_version = cohort_snapshot.config_version
            cohort_file_digest = cohort_snapshot.digest
            user_cohorts = cohort_snapshot.user_cohorts

        return cls(
            gateway_enabled=gateway_enabled,
            routing_mode=routing_mode,
            legacy_enabled=legacy_enabled,
            enforce_cohorts=enforce_cohorts,
            enforce_percent=enforce_percent,
            enforce_hash_salt=enforce_hash_salt,
            cohort_config_version=cohort_config_version,
            cohort_file_digest=cohort_file_digest,
            _user_cohorts=user_cohorts,
        )

    @property
    def salt(self) -> str:
        return self.enforce_hash_salt

    @property
    def hash_salt(self) -> str:
        return self.enforce_hash_salt

    def cohorts_for_user(self, authenticated_user_id: str) -> frozenset[str]:
        _validate_authenticated_user_id(authenticated_user_id)
        return self._user_cohorts.get(authenticated_user_id, frozenset())

    def stable_bucket(self, authenticated_user_id: str) -> int:
        return stable_user_bucket(authenticated_user_id=authenticated_user_id, salt=self.enforce_hash_salt)

    def assign_authenticated_user(
        self,
        authenticated_user_id: str,
        *,
        has_user_scoped_server: bool = False,
        explicit_legacy_capability: bool = False,
    ) -> MCPTaskRouteAssignment:
        _validate_authenticated_user_id(authenticated_user_id)
        if self.routing_mode is MCPRoutingMode.OFF:
            if has_user_scoped_server and not explicit_legacy_capability:
                return self._user_server_unavailable_assignment()
            return self._assignment(
                MCPExecutionPath.LEGACY if self.legacy_enabled else MCPExecutionPath.UNAVAILABLE,
                shadow_enabled=False,
                reason=MCPRouteReason.ROUTING_OFF if self.legacy_enabled else MCPRouteReason.NO_EXECUTION_PATH,
            )
        if self.routing_mode is MCPRoutingMode.SHADOW:
            return self._assignment(
                MCPExecutionPath.LEGACY,
                shadow_enabled=True,
                reason=MCPRouteReason.SHADOW_ENABLED,
            )

        if explicit_legacy_capability:
            return self._assignment(
                MCPExecutionPath.LEGACY if self.legacy_enabled else MCPExecutionPath.UNAVAILABLE,
                shadow_enabled=False,
                reason=(
                    MCPRouteReason.EXPLICIT_LEGACY_CAPABILITY
                    if self.legacy_enabled
                    else MCPRouteReason.NO_EXECUTION_PATH
                ),
            )
        if self.enforce_cohorts and not (self.cohorts_for_user(authenticated_user_id) & self.enforce_cohorts):
            if has_user_scoped_server:
                return self._user_server_unavailable_assignment()
            return self._fallback_assignment(MCPRouteReason.COHORT_NOT_SELECTED)
        if self.stable_bucket(authenticated_user_id) >= self.enforce_percent:
            if has_user_scoped_server:
                return self._user_server_unavailable_assignment()
            return self._fallback_assignment(MCPRouteReason.PERCENT_NOT_SELECTED)
        return self._assignment(
            MCPExecutionPath.USER_SCOPED,
            shadow_enabled=False,
            reason=MCPRouteReason.ENFORCE_SELECTED,
        )

    def assignment_for_user(self, authenticated_user_id: str) -> MCPTaskRouteAssignment:
        return self.assign_authenticated_user(authenticated_user_id)

    def _fallback_assignment(self, reason: MCPRouteReason) -> MCPTaskRouteAssignment:
        return self._assignment(
            MCPExecutionPath.LEGACY if self.legacy_enabled else MCPExecutionPath.UNAVAILABLE,
            shadow_enabled=False,
            reason=reason,
        )

    def _user_server_unavailable_assignment(self) -> MCPTaskRouteAssignment:
        return self._assignment(
            MCPExecutionPath.UNAVAILABLE,
            shadow_enabled=False,
            reason=MCPRouteReason.USER_SERVER_ROLLOUT_UNAVAILABLE,
        )

    def _assignment(
        self,
        real_path: MCPExecutionPath,
        *,
        shadow_enabled: bool,
        reason: MCPRouteReason,
    ) -> MCPTaskRouteAssignment:
        return MCPTaskRouteAssignment(
            routing_mode=self.routing_mode,
            real_path=real_path,
            shadow_enabled=shadow_enabled,
            config_version=self.config_version,
            reason_code=reason,
        )

    def _validate(self) -> None:
        if not isinstance(self.gateway_enabled, bool) or not isinstance(self.legacy_enabled, bool):
            raise MCPRolloutConfigError("MCP rollout enabled fields must be boolean")
        if isinstance(self.enforce_percent, bool) or not isinstance(self.enforce_percent, int):
            raise MCPRolloutConfigError(f"{_ENFORCE_PERCENT_ENV} must be an integer from 0 to 100")
        if not 0 <= self.enforce_percent <= 100:
            raise MCPRolloutConfigError(f"{_ENFORCE_PERCENT_ENV} must be an integer from 0 to 100")
        _validate_cohort_ids(self.enforce_cohorts, source=_ENFORCE_COHORTS_ENV)

        if self.routing_mode is MCPRoutingMode.OFF:
            if self.gateway_enabled:
                raise MCPRolloutConfigError("MCP_ROUTING_MODE=off requires the user-scoped gateway to be disabled")
        elif self.routing_mode is MCPRoutingMode.SHADOW:
            if not self.gateway_enabled or not self.legacy_enabled:
                raise MCPRolloutConfigError("MCP_ROUTING_MODE=shadow requires both gateway and legacy runtimes")
        else:
            if not self.gateway_enabled:
                raise MCPRolloutConfigError("MCP_ROUTING_MODE=enforce requires the user-scoped gateway")
            if not self.enforce_hash_salt:
                raise MCPRolloutConfigError(f"MCP_ROUTING_MODE=enforce requires {_ENFORCE_HASH_SALT_ENV}")
            if not self.legacy_enabled and (self.enforce_percent != 100 or self.enforce_cohorts):
                raise MCPRolloutConfigError(
                    "disabling the legacy runtime requires unfiltered 100 percent enforce routing"
                )

        if self.enforce_cohorts:
            if not self.cohort_file_digest:
                raise MCPRolloutConfigError(
                    f"nonempty {_ENFORCE_COHORTS_ENV} requires {_COHORT_CONFIG_FILE_ENV}"
                )
            mapped_cohorts = frozenset(
                cohort_id for cohort_ids in self._user_cohorts.values() for cohort_id in cohort_ids
            )
            missing = self.enforce_cohorts - mapped_cohorts
            if missing:
                raise MCPRolloutConfigError("configured enforce cohorts have no mapped users")


@dataclass(frozen=True, slots=True)
class _CohortFileSnapshot:
    config_version: str
    digest: str
    user_cohorts: Mapping[str, frozenset[str]] = field(repr=False)


def stable_user_bucket(*, authenticated_user_id: str, salt: str) -> int:
    _validate_authenticated_user_id(authenticated_user_id)
    if not isinstance(salt, str) or not salt:
        raise MCPRolloutConfigError("MCP rollout hash salt must be nonempty")
    digest = hmac.new(
        salt.encode("utf-8"),
        authenticated_user_id.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return int.from_bytes(digest, byteorder="big") % 100


def mcp_rollout_env_is_configured(env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return any(name in source for name in MCP_ROLLOUT_ENV_KEYS)


def compare_mcp_rollout_exposure(
    current: MCPRolloutConfig,
    candidate: MCPRolloutConfig,
) -> MCPExposureChange:
    if current.fingerprint == candidate.fingerprint:
        return MCPExposureChange.UNCHANGED

    rank = {
        MCPRoutingMode.OFF: 0,
        MCPRoutingMode.SHADOW: 1,
        MCPRoutingMode.ENFORCE: 2,
    }
    current_rank = rank[current.routing_mode]
    candidate_rank = rank[candidate.routing_mode]
    if candidate_rank < current_rank:
        return MCPExposureChange.DECREASE
    if candidate_rank > current_rank:
        return MCPExposureChange.INCREASE

    if current.enforce_hash_salt != candidate.enforce_hash_salt:
        return MCPExposureChange.INCREASE
    if current._user_cohorts != candidate._user_cohorts:
        return MCPExposureChange.INCREASE
    if current.routing_mode is not MCPRoutingMode.ENFORCE:
        return MCPExposureChange.INCREASE

    if not _cohort_filter_is_subset(candidate.enforce_cohorts, current.enforce_cohorts):
        return MCPExposureChange.INCREASE
    if candidate.enforce_percent > current.enforce_percent:
        return MCPExposureChange.INCREASE
    if (
        candidate.enforce_percent < current.enforce_percent
        or candidate.enforce_cohorts != current.enforce_cohorts
    ):
        return MCPExposureChange.DECREASE
    return MCPExposureChange.UNCHANGED


def is_strict_mcp_exposure_decrease(current: MCPRolloutConfig, candidate: MCPRolloutConfig) -> bool:
    return compare_mcp_rollout_exposure(current, candidate) is MCPExposureChange.DECREASE


def _parse_bool(raw: str | None, *, default: bool, name: str) -> bool:
    if raw is None or raw == "":
        return default
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise MCPRolloutConfigError(f"{name} must be exactly 'true' or 'false'")


def _parse_routing_mode(raw: str | None) -> MCPRoutingMode:
    if raw is None or raw == "":
        return MCPRoutingMode.OFF
    return _coerce_enum(raw, MCPRoutingMode, _ROUTING_MODE_ENV)


def _coerce_enum(value: Any, enum_type: type[MCPRoutingMode], name: str) -> MCPRoutingMode:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise MCPRolloutConfigError(f"{name} must be one of: {allowed}") from exc


def _parse_percent(raw: str | None) -> int:
    if raw is None or raw == "":
        return 0
    if not raw.isascii() or not raw.isdecimal():
        raise MCPRolloutConfigError(f"{_ENFORCE_PERCENT_ENV} must be an integer from 0 to 100")
    value = int(raw)
    if not 0 <= value <= 100:
        raise MCPRolloutConfigError(f"{_ENFORCE_PERCENT_ENV} must be an integer from 0 to 100")
    return value


def _parse_cohort_filter(raw: str) -> frozenset[str]:
    if not raw:
        return frozenset()
    values = raw.split(",")
    if any(value != value.strip() or not value for value in values):
        raise MCPRolloutConfigError(f"{_ENFORCE_COHORTS_ENV} must be a canonical comma-separated list")
    if len(values) != len(set(values)):
        raise MCPRolloutConfigError(f"{_ENFORCE_COHORTS_ENV} must not contain duplicate cohort IDs")
    result = frozenset(values)
    _validate_cohort_ids(result, source=_ENFORCE_COHORTS_ENV)
    return result


def _validate_cohort_ids(values: frozenset[str], *, source: str) -> None:
    for value in values:
        if not isinstance(value, str) or not value or len(value) > 128:
            raise MCPRolloutConfigError(f"{source} contains an invalid cohort ID")
        if any(character.isspace() or character == "," for character in value):
            raise MCPRolloutConfigError(f"{source} contains an invalid cohort ID")


def _validate_authenticated_user_id(authenticated_user_id: str) -> None:
    if not isinstance(authenticated_user_id, str) or not authenticated_user_id:
        raise MCPRolloutConfigError("authenticated user ID must be a nonempty string")


def _load_cohort_file(path: Path) -> _CohortFileSnapshot:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise MCPRolloutConfigError(f"{_COHORT_CONFIG_FILE_ENV} is not a readable regular file") from exc

    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise MCPRolloutConfigError(f"{_COHORT_CONFIG_FILE_ENV} must be a regular file")
        permissions = stat.S_IMODE(before.st_mode)
        if permissions & ~0o440:
            raise MCPRolloutConfigError(
                f"{_COHORT_CONFIG_FILE_ENV} permission must be no broader than 0440"
            )
        if before.st_size > _MAX_COHORT_FILE_BYTES:
            raise MCPRolloutConfigError(f"{_COHORT_CONFIG_FILE_ENV} exceeds the size limit")

        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > _MAX_COHORT_FILE_BYTES:
                raise MCPRolloutConfigError(f"{_COHORT_CONFIG_FILE_ENV} exceeds the size limit")
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)

    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    if identity_before != identity_after:
        raise MCPRolloutConfigError(f"{_COHORT_CONFIG_FILE_ENV} changed during startup read")

    raw = b"".join(chunks)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MCPRolloutConfigError(f"{_COHORT_CONFIG_FILE_ENV} must contain valid UTF-8 JSON") from exc
    config_version, user_cohorts = _validate_cohort_payload(payload)
    return _CohortFileSnapshot(
        config_version=config_version,
        digest=hashlib.sha256(raw).hexdigest(),
        user_cohorts=MappingProxyType(user_cohorts),
    )


def _validate_cohort_payload(payload: Any) -> tuple[str, dict[str, frozenset[str]]]:
    if not isinstance(payload, dict) or frozenset(payload) != _COHORT_FILE_KEYS:
        raise MCPRolloutConfigError(f"{_COHORT_CONFIG_FILE_ENV} uses an open or incomplete schema")
    if payload["schema_version"] != MCP_ROLLOUT_COHORT_SCHEMA_VERSION:
        raise MCPRolloutConfigError(f"{_COHORT_CONFIG_FILE_ENV} schema_version is unsupported")
    config_version = payload["config_version"]
    if not isinstance(config_version, str) or not config_version or len(config_version) > 128:
        raise MCPRolloutConfigError(f"{_COHORT_CONFIG_FILE_ENV} config_version is invalid")
    raw_mapping = payload["user_cohorts"]
    if not isinstance(raw_mapping, dict):
        raise MCPRolloutConfigError(f"{_COHORT_CONFIG_FILE_ENV} user_cohorts must be an object")

    user_cohorts: dict[str, frozenset[str]] = {}
    for user_id, raw_cohorts in raw_mapping.items():
        if not isinstance(user_id, str) or not user_id:
            raise MCPRolloutConfigError(f"{_COHORT_CONFIG_FILE_ENV} contains an invalid user mapping")
        if not isinstance(raw_cohorts, list) or any(not isinstance(value, str) for value in raw_cohorts):
            raise MCPRolloutConfigError(f"{_COHORT_CONFIG_FILE_ENV} contains an invalid cohort list")
        if len(raw_cohorts) != len(set(raw_cohorts)):
            raise MCPRolloutConfigError(f"{_COHORT_CONFIG_FILE_ENV} contains duplicate cohort IDs")
        cohort_ids = frozenset(raw_cohorts)
        _validate_cohort_ids(cohort_ids, source=_COHORT_CONFIG_FILE_ENV)
        user_cohorts[user_id] = cohort_ids
    return config_version, user_cohorts


def _config_fingerprint(config: MCPRolloutConfig) -> str:
    payload = {
        "algorithm_version": MCP_ROLLOUT_HASH_ALGORITHM_VERSION,
        "cohort_config_version": config.cohort_config_version,
        "cohort_file_digest": config.cohort_file_digest,
        "enforce_cohorts": sorted(config.enforce_cohorts),
        "enforce_hash_salt_sha256": hashlib.sha256(config.enforce_hash_salt.encode("utf-8")).hexdigest(),
        "enforce_percent": config.enforce_percent,
        "gateway_enabled": config.gateway_enabled,
        "legacy_enabled": config.legacy_enabled,
        "routing_mode": config.routing_mode.value,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _cohort_filter_is_subset(candidate: frozenset[str], current: frozenset[str]) -> bool:
    if not current:
        return True
    if not candidate:
        return False
    return candidate <= current


MCPTaskExecutionPath = MCPExecutionPath
MCPRouteReasonCode = MCPRouteReason
compare_rollout_exposure = compare_mcp_rollout_exposure
is_strict_exposure_decrease = is_strict_mcp_exposure_decrease


__all__ = [
    "MCPExecutionPath",
    "MCPExposureChange",
    "MCPRouteReason",
    "MCPRouteReasonCode",
    "MCPRolloutConfig",
    "MCPRolloutConfigError",
    "MCPRoutingMode",
    "MCPTaskExecutionPath",
    "MCPTaskRouteAssignment",
    "MCP_ROLLOUT_ENV_KEYS",
    "MCP_ROLLOUT_COHORT_SCHEMA_VERSION",
    "MCP_ROLLOUT_HASH_ALGORITHM_VERSION",
    "compare_mcp_rollout_exposure",
    "compare_rollout_exposure",
    "is_strict_exposure_decrease",
    "is_strict_mcp_exposure_decrease",
    "stable_user_bucket",
    "mcp_rollout_env_is_configured",
]
