from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


AGENT_CONTEXT_BUDGET_POLICY_REVISION = "maf.agent.total_context_budget.v1"
AGENT_CONTEXT_COMPACT_THRESHOLD_PERCENT = 90
_CONTEXT_BUDGET_KEYS = frozenset(
    {
        "compact_threshold_percent",
        "model_context_window_tokens",
        "policy_revision",
        "total_context_limit_tokens",
    }
)


@dataclass(frozen=True, slots=True)
class AgentContextBudget:
    model_context_window_tokens: int
    total_context_limit_tokens: int
    compact_threshold_percent: int = AGENT_CONTEXT_COMPACT_THRESHOLD_PERCENT
    policy_revision: str = AGENT_CONTEXT_BUDGET_POLICY_REVISION

    def __post_init__(self) -> None:
        window = self.model_context_window_tokens
        limit = self.total_context_limit_tokens
        if (
            isinstance(window, bool)
            or not isinstance(window, int)
            or window <= 0
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit <= 0
            or self.compact_threshold_percent
            != AGENT_CONTEXT_COMPACT_THRESHOLD_PERCENT
            or self.policy_revision != AGENT_CONTEXT_BUDGET_POLICY_REVISION
            or limit
            != window * AGENT_CONTEXT_COMPACT_THRESHOLD_PERCENT // 100
        ):
            raise ValueError("agent_context_budget_invalid")

    @classmethod
    def from_model_context_window(cls, value: int) -> AgentContextBudget:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("agent_context_budget_invalid")
        return cls(
            model_context_window_tokens=value,
            total_context_limit_tokens=(
                value * AGENT_CONTEXT_COMPACT_THRESHOLD_PERCENT // 100
            ),
        )

    @classmethod
    def from_payload(cls, value: Any) -> AgentContextBudget:
        if not isinstance(value, Mapping) or set(value) != _CONTEXT_BUDGET_KEYS:
            raise ValueError("agent_context_budget_invalid")
        try:
            return cls(
                compact_threshold_percent=value["compact_threshold_percent"],
                model_context_window_tokens=value["model_context_window_tokens"],
                policy_revision=value["policy_revision"],
                total_context_limit_tokens=value["total_context_limit_tokens"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("agent_context_budget_invalid") from exc

    def to_payload(self) -> dict[str, int | str]:
        return {
            "compact_threshold_percent": self.compact_threshold_percent,
            "model_context_window_tokens": self.model_context_window_tokens,
            "policy_revision": self.policy_revision,
            "total_context_limit_tokens": self.total_context_limit_tokens,
        }

    @property
    def digest(self) -> str:
        encoded = json.dumps(
            self.to_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
