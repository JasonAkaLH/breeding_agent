#!/usr/bin/env python3
"""Produce a redacted reasoning-effort compatibility matrix for configured models."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml
from openai import APIStatusError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.integrations.llm_client import LLMClient  # noqa: E402
from src.integrations.model_editions import (  # noqa: E402
    model_edition_options,
    model_reasoning_effort_configs,
    validate_model_reasoning_effort_configs,
)


INVALID_COMBINATION_SIGNATURE = "Invalid combination of reasoning_effort and thinking type"
EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_INCONCLUSIVE = 3
EXIT_MISMATCH = 4
_PROMPT = "Reply with exactly OK."


@dataclass(frozen=True, slots=True)
class ProbeCase:
    model: str
    state: str
    effort: str
    expected_supported: bool


def build_probe_cases(config: Mapping[str, Any]) -> tuple[ProbeCase, ...]:
    validate_model_reasoning_effort_configs(config)
    registry = model_reasoning_effort_configs(config)
    cases: list[ProbeCase] = []
    for model_option in model_edition_options(config):
        reasoning = registry[model_option.value]
        for state, thinking_enabled in (("enabled", True), ("disabled", False)):
            supported = set(reasoning.supported_values(thinking_enabled))
            cases.extend(
                ProbeCase(
                    model=model_option.value,
                    state=state,
                    effort=effort.value,
                    expected_supported=effort.value in supported,
                )
                for effort in reasoning.options
            )
    return tuple(cases)


def classify_provider_error(*, status: int | None, code: str | None, message: str) -> str:
    if status == 400 and code == "InvalidParameter" and INVALID_COMBINATION_SIGNATURE in message:
        return "capability_rejection"
    return "inconclusive"


def _provider_error_details(exc: APIStatusError) -> tuple[int | None, str | None, str]:
    status = int(exc.status_code) if exc.status_code is not None else None
    code_value = getattr(exc, "code", None)
    message = str(exc)
    body = getattr(exc, "body", None)
    if isinstance(body, Mapping):
        error = body.get("error")
        if isinstance(error, Mapping):
            if code_value in (None, ""):
                code_value = error.get("code")
            body_message = error.get("message")
            if isinstance(body_message, str):
                message = body_message
    code = str(code_value) if code_value not in (None, "") else None
    return status, code, message


def _planned_row(case: ProbeCase) -> dict[str, Any]:
    return {
        **asdict(case),
        "observed": "planned",
        "match": None,
        "http_status": None,
        "provider_code": None,
    }


def build_plan_report(config: Mapping[str, Any]) -> dict[str, Any]:
    rows = [_planned_row(case) for case in build_probe_cases(config)]
    return {
        "mode": "plan",
        "summary": {
            "total": len(rows),
            "matched": 0,
            "mismatch": 0,
            "inconclusive": 0,
        },
        "cases": rows,
    }


async def _probe_case(client: LLMClient, case: ProbeCase) -> dict[str, Any]:
    try:
        await client.generate_text(
            _PROMPT,
            thinking=case.state == "enabled",
            reasoning_effort=case.effort,
        )
    except APIStatusError as exc:
        status, code, message = _provider_error_details(exc)
        classification = classify_provider_error(status=status, code=code, message=message)
        if classification == "capability_rejection":
            matched = not case.expected_supported
            return {
                **asdict(case),
                "observed": classification,
                "match": matched,
                "http_status": status,
                "provider_code": code,
            }
        return {
            **asdict(case),
            "observed": "inconclusive",
            "match": None,
            "http_status": status,
            "provider_code": code,
        }
    except Exception:  # noqa: BLE001 - output intentionally discards exception details.
        return {
            **asdict(case),
            "observed": "inconclusive",
            "match": None,
            "http_status": None,
            "provider_code": None,
        }
    return {
        **asdict(case),
        "observed": "accepted",
        "match": case.expected_supported,
        "http_status": None,
        "provider_code": None,
    }


async def run_live_matrix(
    config: Mapping[str, Any],
    *,
    timeout_seconds: float,
    client_factory: Callable[..., LLMClient] = LLMClient,
) -> dict[str, Any]:
    cases = build_probe_cases(config)
    cases_by_model: dict[str, list[ProbeCase]] = {}
    for case in cases:
        cases_by_model.setdefault(case.model, []).append(case)

    async def probe_model(model: str, model_cases: list[ProbeCase]) -> list[dict[str, Any]]:
        try:
            client = client_factory(
                config=config,
                model=model,
                timeout=timeout_seconds,
                max_retries=0,
            )
        except Exception:  # noqa: BLE001 - config/client details must not escape.
            return [
                {
                    **asdict(case),
                    "observed": "inconclusive",
                    "match": None,
                    "http_status": None,
                    "provider_code": None,
                }
                for case in model_cases
            ]
        try:
            return [await _probe_case(client, case) for case in model_cases]
        finally:
            await client.aclose()

    grouped = await asyncio.gather(
        *(probe_model(model, model_cases) for model, model_cases in cases_by_model.items())
    )
    rows = [row for model_rows in grouped for row in model_rows]
    return {
        "mode": "live",
        "summary": {
            "total": len(rows),
            "matched": sum(row["match"] is True for row in rows),
            "mismatch": sum(row["match"] is False for row in rows),
            "inconclusive": sum(row["observed"] == "inconclusive" for row in rows),
            "accepted": sum(row["observed"] == "accepted" for row in rows),
            "capability_rejected": sum(row["observed"] == "capability_rejection" for row in rows),
        },
        "cases": rows,
    }


def exit_code_for_report(report: Mapping[str, Any]) -> int:
    summary = report.get("summary") if isinstance(report, Mapping) else None
    values = summary if isinstance(summary, Mapping) else {}
    if int(values.get("inconclusive") or 0) > 0:
        return EXIT_INCONCLUSIVE
    if int(values.get("mismatch") or 0) > 0:
        return EXIT_MISMATCH
    return EXIT_OK


def _load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = yaml.safe_load(handle)
    if not isinstance(value, dict):
        raise ValueError("config_not_mapping")
    return value


def _emit(report: Mapping[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return
    summary = report["summary"]
    print(
        "reasoning matrix "
        f"mode={report['mode']} total={summary['total']} "
        f"matched={summary['matched']} mismatch={summary['mismatch']} "
        f"inconclusive={summary['inconclusive']}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe configured model reasoning efforts with redacted output.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--live", action="store_true", help="Send real provider requests. Omission is plan-only.")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    args = parser.parse_args()

    try:
        config = _load_config(Path(args.config))
        report = (
            asyncio.run(run_live_matrix(config, timeout_seconds=args.timeout_seconds))
            if args.live
            else build_plan_report(config)
        )
    except Exception:  # noqa: BLE001 - config and credential details must not escape.
        _emit(
            {
                "mode": "live" if args.live else "plan",
                "summary": {"total": 0, "matched": 0, "mismatch": 0, "inconclusive": 0},
                "error_code": "config_or_usage_error",
                "cases": [],
            },
            json_output=args.json,
        )
        return EXIT_CONFIG_ERROR
    _emit(report, json_output=args.json)
    return exit_code_for_report(report)


if __name__ == "__main__":
    raise SystemExit(main())
