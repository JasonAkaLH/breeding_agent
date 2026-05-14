from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class MCPShadowMetrics:
    consecutive_shadow_days: int
    effective_samples: int
    contract_mismatch_count: int = 0
    panic_or_crash_count: int = 0
    raw_leak_count: int = 0
    rust_p95_latency_ms: float = 0
    legacy_p95_latency_ms: float = 0
    rust_error_rate: float = 0
    legacy_error_rate: float = 0
    recovery_drill_passed: bool = False
    rollback_drill_passed: bool = False
    ops_ready: bool = False
    conformance_passed: bool = False


@dataclass(slots=True, frozen=True)
class MCPPromotionDecision:
    allowed: bool
    blockers: tuple[str, ...]


class MCPEnforcePromotionGate:
    def __init__(self, *, min_shadow_days: int = 7, min_samples: int = 1000, max_latency_ratio: float = 1.10) -> None:
        self._min_shadow_days = min_shadow_days
        self._min_samples = min_samples
        self._max_latency_ratio = max_latency_ratio

    def evaluate(self, metrics: MCPShadowMetrics) -> MCPPromotionDecision:
        blockers: list[str] = []
        if metrics.consecutive_shadow_days < self._min_shadow_days:
            blockers.append("shadow_duration_below_threshold")
        if metrics.effective_samples < self._min_samples:
            blockers.append("shadow_samples_below_threshold")
        if metrics.contract_mismatch_count != 0:
            blockers.append("contract_mismatch_nonzero")
        if metrics.panic_or_crash_count != 0:
            blockers.append("panic_or_crash_nonzero")
        if metrics.raw_leak_count != 0:
            blockers.append("raw_leak_nonzero")
        if metrics.legacy_p95_latency_ms > 0 and metrics.rust_p95_latency_ms > metrics.legacy_p95_latency_ms * self._max_latency_ratio:
            blockers.append("p95_latency_regressed")
        if metrics.rust_error_rate > metrics.legacy_error_rate:
            blockers.append("error_rate_regressed")
        if not metrics.recovery_drill_passed:
            blockers.append("recovery_drill_missing")
        if not metrics.rollback_drill_passed:
            blockers.append("rollback_drill_missing")
        if not metrics.ops_ready:
            blockers.append("ops_readiness_missing")
        if not metrics.conformance_passed:
            blockers.append("mcp_conformance_missing")
        return MCPPromotionDecision(allowed=not blockers, blockers=tuple(blockers))


def can_shadow_replay_tool(*, risk_level: str, idempotent: bool = False, dry_run: bool = False) -> bool:
    normalized = str(risk_level or "").strip().lower()
    if normalized == "read_only":
        return True
    return bool(idempotent or dry_run)
