from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from src.core.enums import EventVisibility
from src.core.models import EventRecord

from .models import AgentItem, AgentRun


@dataclass(frozen=True, slots=True)
class AgentMetricSpec:
    kind: str
    label_values: dict[str, frozenset[str]]


@dataclass(frozen=True, slots=True)
class AgentMetricSample:
    name: str
    kind: str
    value: float
    labels: tuple[tuple[str, str], ...]


_OUTCOMES = frozenset(
    {
        "aborted",
        "acquired",
        "cancelled",
        "completed",
        "duplicate",
        "failed",
        "lease_conflict",
        "lease_lost",
        "rejected",
        "renewed",
        "resumed",
        "waiting",
    }
)
_CAPABILITY_KINDS = frozenset({"internal", "mcp", "skill", "unknown"})
_REASON_KINDS = frozenset(
    {
        "cancel",
        "mcp_approval",
        "mcp_elicitation",
        "mcp_remote_task",
        "protocol",
        "skill_input",
        "storage",
        "unknown_side_effect",
    }
)
_PHASES = frozenset(
    {"capability_wave", "compaction", "final_publish", "model_sample", "recovery"}
)
_RESULT_PROJECTION_MODES = frozenset(
    {
        "artifact_backed",
        "artifact_persist_failed",
        "inline",
        "invalid",
        "projection_too_large",
        "transient_staged",
        "transient_stage_failed",
    }
)
_RESULT_PROJECTION_ERRORS = {
    "artifact_backed": None,
    "artifact_persist_failed": "agent_result_artifact_persist_failed",
    "inline": None,
    "invalid": "agent_result_invalid",
    "projection_too_large": "agent_result_projection_too_large",
    "transient_staged": None,
    "transient_stage_failed": "agent_transient_skill_result_stage_failed",
}
_CONTEXT_PREFLIGHT_DECISIONS = frozenset(
    {"fits", "compaction_required", "required_too_large"}
)
_CONTEXT_COMPACTION_OUTCOMES = frozenset(
    {"completed", "failed", "no_progress", "required_too_large"}
)
_TRANSIENT_RESULT_OUTCOMES = frozenset(
    {"staged", "injected", "covered", "cleaned", "failed"}
)


AGENT_METRIC_SPECS = {
    "agent_runs_active": AgentMetricSpec("gauge", {}),
    "agent_runs_total": AgentMetricSpec("counter", {"outcome": _OUTCOMES}),
    "agent_time_to_final_seconds": AgentMetricSpec("histogram", {"outcome": _OUTCOMES}),
    "agent_samples_total": AgentMetricSpec("counter", {"outcome": _OUTCOMES}),
    "agent_sample_duration_seconds": AgentMetricSpec("histogram", {}),
    "agent_tool_calls_total": AgentMetricSpec(
        "counter",
        {"capability_kind": _CAPABILITY_KINDS, "outcome": _OUTCOMES},
    ),
    "agent_tool_call_duration_seconds": AgentMetricSpec(
        "histogram", {"capability_kind": _CAPABILITY_KINDS}
    ),
    "agent_run_tool_calls": AgentMetricSpec("histogram", {}),
    "agent_run_samples": AgentMetricSpec("histogram", {}),
    "agent_waiting_total": AgentMetricSpec(
        "counter", {"reason_kind": _REASON_KINDS}
    ),
    "agent_resume_total": AgentMetricSpec("counter", {"outcome": _OUTCOMES}),
    "agent_result_projections_total": AgentMetricSpec(
        "counter", {"projection_mode": _RESULT_PROJECTION_MODES}
    ),
    "agent_context_preflights_total": AgentMetricSpec(
        "counter", {"decision": _CONTEXT_PREFLIGHT_DECISIONS}
    ),
    "agent_context_compactions_total": AgentMetricSpec(
        "counter", {"outcome": _CONTEXT_COMPACTION_OUTCOMES}
    ),
    "agent_transient_skill_results_total": AgentMetricSpec(
        "counter", {"outcome": _TRANSIENT_RESULT_OUTCOMES}
    ),
    "agent_lease_acquire_total": AgentMetricSpec(
        "counter", {"outcome": _OUTCOMES}
    ),
    "agent_lease_renew_total": AgentMetricSpec("counter", {"outcome": _OUTCOMES}),
    "agent_lease_lost_total": AgentMetricSpec("counter", {"phase": _PHASES}),
    "agent_lease_remaining_seconds": AgentMetricSpec("histogram", {}),
    "agent_compactions_total": AgentMetricSpec("counter", {"outcome": _OUTCOMES}),
    "agent_compaction_duration_seconds": AgentMetricSpec("histogram", {}),
    "agent_aborted_calls_total": AgentMetricSpec(
        "counter", {"reason_kind": _REASON_KINDS}
    ),
    "agent_final_publish_delay_seconds": AgentMetricSpec("histogram", {}),
}


@dataclass(frozen=True, slots=True)
class AgentResultProjectionObservation:
    capability_id: str
    projection_mode: str
    original_size_bytes: int
    projected_size_bytes: int
    raw_sha256: str | None
    artifact_count: int
    error_code: str | None

    def __post_init__(self) -> None:
        if not self.capability_id.strip():
            raise ValueError("agent_result_projection_capability_invalid")
        if (
            self.projection_mode not in _RESULT_PROJECTION_MODES
            or self.error_code != _RESULT_PROJECTION_ERRORS[self.projection_mode]
        ):
            raise ValueError("agent_result_projection_outcome_invalid")
        for value in (
            self.original_size_bytes,
            self.projected_size_bytes,
            self.artifact_count,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("agent_result_projection_size_invalid")
        if self.raw_sha256 is not None and (
            len(self.raw_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.raw_sha256)
        ):
            raise ValueError("agent_result_projection_digest_invalid")

    def to_payload(self) -> dict[str, object]:
        return {
            "capability_id": self.capability_id,
            "projection_mode": self.projection_mode,
            "original_size_bytes": self.original_size_bytes,
            "projected_size_bytes": self.projected_size_bytes,
            "raw_sha256": self.raw_sha256,
            "artifact_count": self.artifact_count,
            "error_code": self.error_code,
        }


def build_agent_result_projected_event(
    *,
    run: AgentRun,
    call_item: AgentItem,
    observation: AgentResultProjectionObservation,
) -> EventRecord:
    payload = observation.to_payload()
    payload_bytes = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    identity = hashlib.sha256(
        b"maf.agent.result_projected.v1\0"
        + call_item.item_id.encode("utf-8")
        + b"\0"
        + payload_bytes
    ).hexdigest()
    call_payload = json.loads(call_item.payload_json)
    return EventRecord(
        event_id=f"agent-result-projected:v1:{identity}",
        conversation_id=run.conversation_id,
        task_id=run.task_id,
        node_id=str(call_payload.get("node_id") or "") or None,
        event_type="agent.result_projected",
        payload=payload,
        visibility=EventVisibility.AUDIT_ONLY,
    )


class AgentMetricSink(Protocol):
    def record(self, sample: AgentMetricSample) -> None: ...


class InMemoryAgentMetricSink:
    def __init__(self) -> None:
        self.samples: list[AgentMetricSample] = []

    def record(self, sample: AgentMetricSample) -> None:
        self.samples.append(sample)


class AgentMetricsRecorder:
    def __init__(
        self,
        sink: AgentMetricSink,
        *,
        fault_sink: Callable[[str], None] | None = None,
    ) -> None:
        self._sink = sink
        self._fault_sink = fault_sink

    def record(
        self,
        name: str,
        value: int | float = 1,
        **labels: str,
    ) -> bool:
        sample = validate_agent_metric(name, value, labels)
        try:
            self._sink.record(sample)
        except Exception:
            if self._fault_sink is not None:
                try:
                    self._fault_sink("agent_metric_backend_failed")
                except Exception:
                    pass
            return False
        return True


def validate_agent_metric(
    name: str,
    value: int | float,
    labels: dict[str, str],
) -> AgentMetricSample:
    spec = AGENT_METRIC_SPECS.get(name)
    if spec is None or set(labels) != set(spec.label_values):
        raise ValueError("agent_metric_contract_invalid")
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError("agent_metric_value_invalid")
    for label_name, allowed in spec.label_values.items():
        if labels[label_name] not in allowed:
            raise ValueError("agent_metric_label_invalid")
    return AgentMetricSample(
        name=name,
        kind=spec.kind,
        value=float(value),
        labels=tuple(sorted(labels.items())),
    )


__all__ = [
    "AGENT_METRIC_SPECS",
    "AgentMetricSample",
    "AgentMetricsRecorder",
    "AgentResultProjectionObservation",
    "InMemoryAgentMetricSink",
    "build_agent_result_projected_event",
    "validate_agent_metric",
]
