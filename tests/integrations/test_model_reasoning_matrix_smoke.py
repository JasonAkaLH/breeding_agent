from __future__ import annotations

import json
import unittest

import httpx
from openai import APIStatusError

from scripts.smoke_model_reasoning_matrix import (
    EXIT_INCONCLUSIVE,
    EXIT_MISMATCH,
    EXIT_OK,
    INVALID_COMBINATION_SIGNATURE,
    build_plan_report,
    build_probe_cases,
    classify_provider_error,
    exit_code_for_report,
    run_live_matrix,
)


def _reasoning(*, disabled: list[str]) -> dict:
    return {
        "options": [
            {"value": "minimal", "label": "Minimal"},
            {"value": "high", "label": "High"},
        ],
        "thinking": {
            "enabled": {"default": "high", "supported": ["minimal", "high"]},
            "disabled": {"default": "minimal", "supported": disabled},
        },
    }


def _config() -> dict:
    return {
        "api_key": "secret-api-key",
        "base_url": "https://secret.example.test",
        "model_editions": {
            "default": "model-a",
            "options": [
                {"value": "model-a", "reasoning_efforts": _reasoning(disabled=["minimal", "high"])},
                {"value": "model-b", "reasoning_efforts": _reasoning(disabled=["minimal"])},
            ],
        },
    }


def _api_error(status: int, *, code: str, message: str) -> APIStatusError:
    body = {"error": {"code": code, "message": message}}
    request = httpx.Request("POST", "https://provider.example.test/chat/completions")
    response = httpx.Response(status, request=request, json=body)
    return APIStatusError(message, response=response, body=body)


class _FakeClient:
    def __init__(self, *, model: str, outcomes: dict[tuple[str, str, str], object], **_kwargs) -> None:
        self.model = model
        self.outcomes = outcomes
        self.closed = False

    async def generate_text(self, _prompt: str, *, thinking: bool, reasoning_effort: str) -> str:
        state = "enabled" if thinking else "disabled"
        outcome = self.outcomes.get((self.model, state, reasoning_effort), "accepted")
        if isinstance(outcome, BaseException):
            raise outcome
        return "OK"

    async def aclose(self) -> None:
        self.closed = True


class ModelReasoningMatrixSmokeTest(unittest.IsolatedAsyncioTestCase):
    def test_plan_covers_both_states_and_catalog_without_network(self) -> None:
        cases = build_probe_cases(_config())
        report = build_plan_report(_config())

        self.assertEqual(len(cases), 8)
        self.assertEqual(report["summary"], {"total": 8, "matched": 0, "mismatch": 0, "inconclusive": 0})
        self.assertEqual(sum(case.expected_supported for case in cases), 7)
        self.assertTrue(all(row["observed"] == "planned" for row in report["cases"]))
        self.assertEqual(exit_code_for_report(report), EXIT_OK)

    def test_only_exact_invalid_combination_is_capability_rejection(self) -> None:
        self.assertEqual(
            classify_provider_error(
                status=400,
                code="InvalidParameter",
                message=f"{INVALID_COMBINATION_SIGNATURE}: high + disabled",
            ),
            "capability_rejection",
        )
        for status, code, message in (
            (400, "InvalidParameter", "another invalid parameter"),
            (401, "Unauthorized", INVALID_COMBINATION_SIGNATURE),
            (429, "RateLimitExceeded", INVALID_COMBINATION_SIGNATURE),
            (500, "InternalError", INVALID_COMBINATION_SIGNATURE),
        ):
            with self.subTest(status=status, code=code):
                self.assertEqual(
                    classify_provider_error(status=status, code=code, message=message),
                    "inconclusive",
                )

    async def test_live_matrix_matches_acceptance_and_exact_rejection(self) -> None:
        outcomes = {
            ("model-b", "disabled", "high"): _api_error(
                400,
                code="InvalidParameter",
                message=f"{INVALID_COMBINATION_SIGNATURE}: high + disabled secret-request-id",
            )
        }
        clients: list[_FakeClient] = []

        def factory(**kwargs):
            client = _FakeClient(outcomes=outcomes, **kwargs)
            clients.append(client)
            return client

        report = await run_live_matrix(_config(), timeout_seconds=1, client_factory=factory)

        self.assertEqual(
            report["summary"],
            {
                "total": 8,
                "matched": 8,
                "mismatch": 0,
                "inconclusive": 0,
                "accepted": 7,
                "capability_rejected": 1,
            },
        )
        self.assertEqual(exit_code_for_report(report), EXIT_OK)
        self.assertTrue(all(client.closed for client in clients))
        rendered = json.dumps(report)
        self.assertNotIn("secret-api-key", rendered)
        self.assertNotIn("secret.example.test", rendered)
        self.assertNotIn("secret-request-id", rendered)
        self.assertNotIn("Reply with exactly OK", rendered)

    async def test_unexpected_acceptance_is_mismatch(self) -> None:
        report = await run_live_matrix(
            _config(),
            timeout_seconds=1,
            client_factory=lambda **kwargs: _FakeClient(outcomes={}, **kwargs),
        )

        self.assertEqual(report["summary"]["mismatch"], 1)
        self.assertEqual(report["summary"]["inconclusive"], 0)
        self.assertEqual(exit_code_for_report(report), EXIT_MISMATCH)

    async def test_rate_limit_is_inconclusive_not_rejection(self) -> None:
        outcomes = {
            ("model-a", "enabled", "high"): _api_error(
                429,
                code="RateLimitExceeded",
                message="rate limited secret-request-id",
            ),
            ("model-b", "disabled", "high"): _api_error(
                400,
                code="InvalidParameter",
                message=f"{INVALID_COMBINATION_SIGNATURE}: high + disabled",
            ),
        }
        report = await run_live_matrix(
            _config(),
            timeout_seconds=1,
            client_factory=lambda **kwargs: _FakeClient(outcomes=outcomes, **kwargs),
        )

        self.assertEqual(report["summary"]["inconclusive"], 1)
        self.assertEqual(exit_code_for_report(report), EXIT_INCONCLUSIVE)
        self.assertNotIn("secret-request-id", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
