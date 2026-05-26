from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .contracts import StateHealthSnapshot
from .errors import redact_value


def build_readiness_payload(snapshot: StateHealthSnapshot) -> dict[str, Any]:
    return snapshot.public_dict()


@dataclass(frozen=True, slots=True)
class StatePlatformTelemetry:
    operation: str
    status: str
    duration_ms: float
    error_code: str | None = None
    partition_category: str | None = None
    attempt_count: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def public_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "error_code": self.error_code,
            "partition_category": self.partition_category,
            "attempt_count": self.attempt_count,
            "metadata": _redact_metadata(self.metadata),
        }


def _redact_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    public: dict[str, Any] = {}
    for key, value in metadata.items():
        lowered = str(key).lower()
        if lowered == "payload" or "raw" in lowered:
            public["payload_redacted"] = True
        elif any(marker in lowered for marker in ("dsn", "token", "password", "secret")):
            public[str(key)] = "<redacted>"
        else:
            public[str(key)] = redact_value(value)
    return public
