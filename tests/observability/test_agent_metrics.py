from __future__ import annotations

import math
import unittest

from src.orchestration.agent_loop.observability import (
    AGENT_METRIC_SPECS,
    AgentMetricsRecorder,
    InMemoryAgentMetricSink,
    validate_agent_metric,
)


EXPECTED_METRICS = {
    "agent_aborted_calls_total",
    "agent_compaction_duration_seconds",
    "agent_compactions_total",
    "agent_context_compactions_total",
    "agent_context_preflights_total",
    "agent_final_publish_delay_seconds",
    "agent_lease_acquire_total",
    "agent_lease_lost_total",
    "agent_lease_remaining_seconds",
    "agent_lease_renew_total",
    "agent_resume_total",
    "agent_result_projections_total",
    "agent_run_samples",
    "agent_run_tool_calls",
    "agent_runs_active",
    "agent_runs_total",
    "agent_sample_duration_seconds",
    "agent_samples_total",
    "agent_time_to_final_seconds",
    "agent_transient_skill_results_total",
    "agent_tool_call_duration_seconds",
    "agent_tool_calls_total",
    "agent_waiting_total",
}


class AgentMetricsTest(unittest.TestCase):
    def test_every_required_metric_accepts_only_its_closed_labels(self) -> None:
        self.assertEqual(set(AGENT_METRIC_SPECS), EXPECTED_METRICS)
        sink = InMemoryAgentMetricSink()
        recorder = AgentMetricsRecorder(sink)

        for name, spec in AGENT_METRIC_SPECS.items():
            labels = {
                label_name: sorted(allowed)[0]
                for label_name, allowed in spec.label_values.items()
            }
            with self.subTest(metric=name):
                self.assertTrue(recorder.record(name, 1, **labels))

        self.assertEqual({sample.name for sample in sink.samples}, EXPECTED_METRICS)

    def test_result_projection_metric_has_only_closed_outcomes(self) -> None:
        sink = InMemoryAgentMetricSink()
        recorder = AgentMetricsRecorder(sink)
        outcomes = {
            "inline",
            "artifact_backed",
            "invalid",
            "artifact_persist_failed",
            "projection_too_large",
            "reused",
            "transient_staged",
            "transient_stage_failed",
        }

        for outcome in outcomes:
            self.assertTrue(
                recorder.record(
                    "agent_result_projections_total",
                    projection_mode=outcome,
                )
            )
        with self.assertRaisesRegex(ValueError, "label_invalid"):
            recorder.record(
                "agent_result_projections_total",
                projection_mode="skill.secret",
            )

    def test_context_and_transient_metrics_have_only_closed_outcomes(self) -> None:
        recorder = AgentMetricsRecorder(InMemoryAgentMetricSink())
        closed_values = {
            "agent_context_preflights_total": (
                "decision",
                {"fits", "compaction_required", "required_too_large"},
            ),
            "agent_context_compactions_total": (
                "outcome",
                {"completed", "failed", "no_progress", "required_too_large"},
            ),
            "agent_transient_skill_results_total": (
                "outcome",
                {"staged", "injected", "covered", "cleaned", "failed"},
            ),
        }

        for metric_name, (label_name, values) in closed_values.items():
            for value in values:
                with self.subTest(metric=metric_name, value=value):
                    self.assertTrue(
                        recorder.record(metric_name, **{label_name: value})
                    )
            with self.subTest(metric=metric_name, value="task-1"):
                with self.assertRaisesRegex(ValueError, "label_invalid"):
                    recorder.record(metric_name, **{label_name: "task-1"})

    def test_high_cardinality_unknown_and_secret_labels_are_rejected(self) -> None:
        for labels in (
            {"outcome": "completed", "task_id": "task-1"},
            {"outcome": "completed", "conversation_id": "conv-1"},
            {"outcome": "completed", "call_id": "call-1"},
            {"outcome": "completed", "capability_id": "skill.secret"},
            {"outcome": "completed", "username": "alice"},
            {"outcome": "user supplied text"},
        ):
            with self.subTest(labels=labels):
                with self.assertRaisesRegex(ValueError, "contract_invalid|label_invalid"):
                    validate_agent_metric("agent_runs_total", 1, labels)

    def test_invalid_values_fail_closed(self) -> None:
        for value in (-1, math.inf, math.nan, True):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "value_invalid"):
                    validate_agent_metric(
                        "agent_final_publish_delay_seconds",
                        value,
                        {},
                    )

    def test_backend_failure_is_observability_only_and_reported(self) -> None:
        faults = []

        class FailingSink:
            def record(self, _sample) -> None:
                raise RuntimeError("metrics unavailable")

        recorder = AgentMetricsRecorder(FailingSink(), fault_sink=faults.append)

        self.assertFalse(recorder.record("agent_runs_total", outcome="completed"))
        self.assertEqual(faults, ["agent_metric_backend_failed"])
