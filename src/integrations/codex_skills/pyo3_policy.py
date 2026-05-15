from __future__ import annotations

import importlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

from .rust_contract import load_skill_runtime_contract

_DEFAULT_MODULE_NAME = "maf_skill_runtime_pyo3"
_REQUIRED_FEATURES = frozenset({"policy_kernel", "pyo3_policy_facade"})


class SkillRuntimePyo3PolicyClient:
    """Thin Python facade for the prebuilt Rust/PyO3 Skill policy kernel."""

    def __init__(self, *, module_name: str = _DEFAULT_MODULE_NAME, module: Any | None = None) -> None:
        self._module = module if module is not None else importlib.import_module(module_name)
        self._validate_contract()

    def validate_policy(
        self,
        *,
        skill_name: str,
        capability_id: str,
        execution_mode: str,
        trust_scope: str,
        handler: str,
        manifest_services: Iterable[str],
        runtime_allowlist_services: Iterable[str],
        requested_services: Iterable[str],
        runtime_allowlist_handlers: Iterable[str],
        x_runtime_rust: Mapping[str, str] | None = None,
        timeout_seconds: float = 5,  # noqa: ARG002 - kept for sidecar-client protocol parity.
    ) -> dict[str, Any]:
        request = {
            "skill_name": str(skill_name),
            "capability_id": str(capability_id),
            "execution_mode": str(execution_mode),
            "trust_scope": str(trust_scope),
            "handler": str(handler),
            "manifest_services": [str(item) for item in manifest_services],
            "runtime_allowlist_services": [str(item) for item in runtime_allowlist_services],
            "requested_services": [str(item) for item in requested_services],
            "runtime_allowlist_handlers": [str(item) for item in runtime_allowlist_handlers],
            "x_runtime_rust": {str(key): str(value) for key, value in dict(x_runtime_rust or {}).items()},
        }
        raw_response = self._module.validate_policy_json(
            json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
        response = _parse_json_mapping(raw_response, "Rust PyO3 policy response")
        if not isinstance(response.get("allowed"), bool) or not isinstance(response.get("bundle_fingerprint"), str):
            raise RuntimeError("skill_runtime_contract_mismatch: Rust PyO3 policy returned invalid policy response")
        error = response.get("error")
        if error is not None and not isinstance(error, Mapping):
            raise RuntimeError("skill_runtime_contract_mismatch: Rust PyO3 policy returned invalid policy response")
        return {
            "allowed": bool(response["allowed"]),
            "bundle_fingerprint": str(response["bundle_fingerprint"]),
            "error": dict(error) if isinstance(error, Mapping) else None,
        }

    def _validate_contract(self) -> None:
        contract_json = getattr(self._module, "contract_json", None)
        validate_policy_json = getattr(self._module, "validate_policy_json", None)
        if not callable(contract_json) or not callable(validate_policy_json):
            raise RuntimeError("skill_runtime_contract_mismatch: Rust PyO3 policy module is missing required entrypoints")
        actual = _parse_json_mapping(contract_json(), "Rust PyO3 policy contract")
        expected = load_skill_runtime_contract()
        for key in ("component", "contract_version", "schema_hash", "error_code_table_hash"):
            if actual.get(key) != expected.get(key):
                raise RuntimeError("skill_runtime_contract_mismatch: Rust PyO3 policy contract mismatch")
        actual_features = {str(feature) for feature in actual.get("supported_features", ())}
        required_features = {str(feature) for feature in expected.get("supported_features", ())} | _REQUIRED_FEATURES
        if not required_features.issubset(actual_features):
            raise RuntimeError("skill_runtime_contract_mismatch: Rust PyO3 policy contract mismatch")


def try_load_skill_runtime_pyo3_policy_client(*, module_name: str = _DEFAULT_MODULE_NAME) -> SkillRuntimePyo3PolicyClient | None:
    try:
        return SkillRuntimePyo3PolicyClient(module_name=module_name)
    except ModuleNotFoundError:
        return None


def _parse_json_mapping(raw: Any, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"skill_runtime_contract_mismatch: {label} is not valid JSON") from exc
    if not isinstance(parsed, Mapping):
        raise RuntimeError(f"skill_runtime_contract_mismatch: {label} is not a JSON object")
    return dict(parsed)


__all__ = ["SkillRuntimePyo3PolicyClient", "try_load_skill_runtime_pyo3_policy_client"]
