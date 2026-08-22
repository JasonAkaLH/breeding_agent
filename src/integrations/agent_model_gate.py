from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from src.orchestration.agent_loop.models import AgentProtocolRetryPolicy

from .model_editions import ModelEditionOption, default_model_edition, model_edition_options


@dataclass(frozen=True, slots=True)
class AgentModelGateReport:
    default_model_edition: str | None
    ready_editions: tuple[str, ...]
    rejected_editions: Mapping[str, tuple[str, ...]]


def evaluate_agent_model_gate(config: Mapping[str, Any] | None = None) -> AgentModelGateReport:
    config = config or {}
    ready: list[str] = []
    rejected: dict[str, tuple[str, ...]] = {}
    for option in model_edition_options(config):
        profile = option.agent_capabilities
        missing = ("agent_capabilities",) if profile is None else profile.missing_requirements()
        if missing:
            rejected[option.value] = missing
        else:
            ready.append(option.value)
    return AgentModelGateReport(
        default_model_edition=default_model_edition(config),
        ready_editions=tuple(ready),
        rejected_editions=rejected,
    )


def validate_agent_model_gate(config: Mapping[str, Any] | None = None) -> AgentModelGateReport:
    AgentProtocolRetryPolicy.from_config(config)
    report = evaluate_agent_model_gate(config)
    default = report.default_model_edition
    if default is not None and default not in report.ready_editions:
        reasons = ", ".join(report.rejected_editions.get(default, ("not_configured",)))
        raise ValueError(f"Default model edition is not Agent-ready: {default}: {reasons}")
    return report


def agent_ready_model_edition_options(config: Mapping[str, Any] | None = None) -> tuple[ModelEditionOption, ...]:
    ready = set(evaluate_agent_model_gate(config).ready_editions)
    return tuple(option for option in model_edition_options(config) if option.value in ready)


def validate_agent_model_edition(value: str | None, *, config: Mapping[str, Any] | None = None) -> str | None:
    if value is None or not value.strip():
        return None
    candidate = value.strip()
    ready = set(evaluate_agent_model_gate(config).ready_editions)
    if candidate not in ready:
        raise ValueError(f"Unsupported or non-Agent-ready model_edition: {candidate}")
    return candidate
