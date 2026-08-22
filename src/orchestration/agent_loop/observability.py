from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


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
    "InMemoryAgentMetricSink",
    "validate_agent_metric",
]
