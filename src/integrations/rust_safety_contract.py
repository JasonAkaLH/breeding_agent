from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import os
import threading
import time
from collections.abc import Callable
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

_CONTRACT_PATH = Path(__file__).with_name("rust_contracts") / "safety_contract.json"
_MODULE_ENV = "MAF_SAFETY_KERNELS_PYO3_MODULE"
_DEFAULT_MODULE_NAME = "maf_safety_kernels_pyo3"
_VALID_MODES = frozenset({"off", "shadow", "enforce"})
_MODE_ENV_BY_COMPONENT = {
    "artifact_store": "MAF_RUST_ARTIFACT_STORE_MODE",
    "auth_core": "MAF_RUST_AUTH_CORE_MODE",
    "data_access": "MAF_RUST_DATA_ACCESS_MODE",
    "audit_sanitizer": "MAF_RUST_AUDIT_SANITIZER_MODE",
}
_REQUIRED_FEATURES = frozenset(
    {
        "artifact_store_kernel",
        "auth_core_kernel",
        "data_access_kernel",
        "audit_sanitizer_kernel",
        "pyo3_safety_facade",
    }
)
_SENSITIVE_KEY_MARKERS = ("secret", "token", "password", "base_url", "dsn")
_SENSITIVE_PROMPT_KEYS = frozenset({"prompt", "full_prompt", "raw_prompt", "system_prompt", "user_prompt", "llm_prompt"})
_SENSITIVE_ROWS_KEYS = frozenset({"rows", "raw_rows", "result_rows", "full_rows", "candidate_rows"})
_SENSITIVE_PATH_KEYS = frozenset(
    {
        "path",
        "real_path",
        "file_path",
        "storage_path",
        "absolute_path",
        "filesystem_path",
    }
)
_SHADOW_SINK: Callable[[Mapping[str, str]], Any] | None = None
_SHADOW_DEPTH = threading.local()


class RustSafetyContractError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "audit_sanitizer_contract_mismatch",
        category: str = "contract",
        retriable: bool = False,
        safe_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.category = category
        self.retriable = retriable
        self.safe_metadata = dict(safe_metadata or {})


class DataAccessContractError(RuntimeError):
    def __init__(self, message: str, *, code: str, retriable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retriable = retriable


def load_safety_contract() -> dict[str, Any]:
    """Load the Rust-owned Artifact/Auth/DataAccess/Audit safety contract.

    Runtime never builds native code. In `off` mode Python uses the checked-in
    Rust contract artifact; in `shadow` mode an incompatible/missing PyO3
    module is ignored for user-visible behavior; in `enforce` mode a compatible
    prebuilt PyO3 module is mandatory and mismatches fail closed.
    """

    return _load_safety_contract_for_mode(_component_modes_fingerprint(), _pyo3_module_name())


@lru_cache(maxsize=16)
def _load_safety_contract_for_mode(modes_fingerprint: tuple[tuple[str, str], ...], module_name: str) -> dict[str, Any]:
    checked_in_contract = _load_checked_in_contract()
    modes = dict(modes_fingerprint)
    if all(mode == "off" for mode in modes.values()):
        return checked_in_contract

    enforce_enabled = any(mode == "enforce" for mode in modes.values())
    try:
        pyo3_contract = _load_pyo3_contract(module_name)
    except ModuleNotFoundError as exc:
        if enforce_enabled:
            raise RustSafetyContractError(
                "Safety kernel enforce mode requires a prebuilt PyO3 module; runtime import/build fallback is forbidden",
                safe_metadata={"module": module_name},
            ) from exc
        return checked_in_contract
    except RustSafetyContractError:
        if enforce_enabled:
            raise
        return checked_in_contract

    try:
        _ensure_contract_compatible(pyo3_contract, checked_in_contract)
    except RustSafetyContractError:
        if enforce_enabled:
            raise
        return checked_in_contract
    if enforce_enabled:
        return pyo3_contract
    return checked_in_contract


def component_mode(component: str) -> str:
    env_name = _MODE_ENV_BY_COMPONENT.get(component)
    if env_name is None:
        raise KeyError(f"Unknown Rust safety component: {component}")
    mode = os.environ.get(env_name, "off").strip().lower()
    if mode not in _VALID_MODES:
        raise RustSafetyContractError(
            f"Invalid {env_name}: {mode}",
            code="audit_sanitizer_contract_mismatch",
            safe_metadata={"component": component, "mode": mode},
        )
    return mode


def resource_limit(name: str) -> int:
    value = load_safety_contract()["resource_limits"].get(name)
    if value is None:
        raise KeyError(f"Unknown Rust safety resource limit: {name}")
    return int(value)


def configure_safety_shadow_sink(sink: Callable[[Mapping[str, str]], Any] | None) -> None:
    global _SHADOW_SINK
    _SHADOW_SINK = sink


def normalize_storage_key(key: str) -> str:
    module = _pyo3_module_for_enforce_call("artifact_store")
    if module is not None:
        response = _call_pyo3_json(
            module,
            "normalize_storage_key_json",
            {"key": str(key)},
            "Rust safety artifact path response",
        )
        error = response.get("error")
        if error is not None:
            raise ValueError(_safe_error_message(error, "artifact path escapes managed root"))
        value = response.get("value")
        if not isinstance(value, str) or not value:
            raise RustSafetyContractError(
                "Rust safety artifact path response failed contract validation",
                code="artifact_structured_output_invalid",
            )
        return value
    value = _normalize_storage_key_py(key)
    _record_safety_shadow(
        component="artifact_store",
        operation_name="normalize_storage_key",
        input_payload={"key": str(key)},
        legacy_output={"value": value},
        rust_call=lambda module: _call_pyo3_json(
            module,
            "normalize_storage_key_json",
            {"key": str(key)},
            "Rust safety artifact path response",
        ),
        rust_output=lambda response: {"value": response.get("value"), "error": response.get("error")},
    )
    return value


def sha256_hex(content: bytes) -> str:
    module = _pyo3_module_for_enforce_call("artifact_store")
    if module is not None:
        function = getattr(module, "sha256_hex_bytes", None)
        if not callable(function):
            raise RustSafetyContractError(
                "Safety PyO3 module is missing sha256_hex_bytes entrypoint",
                code="artifact_contract_mismatch",
            )
        return str(function(bytes(content)))
    value = hashlib.sha256(content).hexdigest()
    _record_safety_shadow(
        component="artifact_store",
        operation_name="sha256_hex",
        input_payload={"bytes_sha256": value, "size_bytes": len(content)},
        legacy_output={"value": value},
        rust_call=lambda module: {"value": str(module.sha256_hex_bytes(bytes(content))), "error": None},
        rust_output=lambda response: {"value": response.get("value"), "error": response.get("error")},
    )
    return value


def verify_auth_token(expected: str | bytes, actual: str | bytes) -> bool:
    module = _pyo3_module_for_enforce_call("auth_core")
    if module is not None:
        response = _call_pyo3_json(
            module,
            "verify_token_json",
            {"expected": _text_payload(expected), "actual": _text_payload(actual)},
            "Rust safety auth verify response",
        )
        valid = response.get("valid")
        if not isinstance(valid, bool):
            raise RustSafetyContractError(
                "Rust safety auth response failed contract validation",
                code="auth_structured_output_invalid",
            )
        return bool(valid)
    value = hmac.compare_digest(_bytes_payload(expected), _bytes_payload(actual))
    _record_safety_shadow(
        component="auth_core",
        operation_name="verify_token",
        input_payload={
            "expected_fingerprint": _fingerprint(_bytes_payload(expected)),
            "actual_fingerprint": _fingerprint(_bytes_payload(actual)),
        },
        legacy_output={"valid": value},
        rust_call=lambda module: _call_pyo3_json(
            module,
            "verify_token_json",
            {"expected": _text_payload(expected), "actual": _text_payload(actual)},
            "Rust safety auth verify response",
        ),
        rust_output=lambda response: {"valid": response.get("valid"), "error": response.get("error")},
    )
    return value


def hmac_sha256_hex(secret: str | bytes, payload: str | bytes) -> str:
    module = _pyo3_module_for_enforce_call("auth_core")
    if module is not None:
        response = _call_pyo3_json(
            module,
            "hmac_sha256_hex_json",
            {"secret": _text_payload(secret), "payload": _text_payload(payload)},
            "Rust safety auth HMAC response",
        )
        error = response.get("error")
        if error is not None:
            raise RustSafetyContractError(
                _safe_error_message(error, "Rust safety auth HMAC failed"),
                code=str(error.get("code") or "auth_secret_missing") if isinstance(error, Mapping) else "auth_secret_missing",
            )
        value = response.get("value")
        if not isinstance(value, str) or len(value) != 64:
            raise RustSafetyContractError(
                "Rust safety auth HMAC response failed contract validation",
                code="auth_structured_output_invalid",
            )
        return value
    secret_bytes = _bytes_payload(secret)
    if not secret_bytes:
        raise RustSafetyContractError("auth secret is missing", code="auth_secret_missing")
    payload_bytes = _bytes_payload(payload)
    value = hmac.new(secret_bytes, payload_bytes, hashlib.sha256).hexdigest()
    _record_safety_shadow(
        component="auth_core",
        operation_name="hmac_sha256_hex",
        input_payload={
            "secret_fingerprint": _fingerprint(secret_bytes),
            "payload_fingerprint": _fingerprint(payload_bytes),
        },
        legacy_output={"value": value},
        rust_call=lambda module: _call_pyo3_json(
            module,
            "hmac_sha256_hex_json",
            {"secret": _text_payload(secret_bytes), "payload": _text_payload(payload_bytes)},
            "Rust safety auth HMAC response",
        ),
        rust_output=lambda response: {"value": response.get("value"), "error": response.get("error")},
    )
    return value


def expires_at_ms(issued_at_ms: int, ttl_ms: int) -> int:
    module = _pyo3_module_for_enforce_call("auth_core")
    if module is not None:
        response = _call_pyo3_json(
            module,
            "expires_at_ms_json",
            {"issued_at_ms": int(issued_at_ms), "ttl_ms": int(ttl_ms)},
            "Rust safety auth TTL response",
        )
        value = response.get("value")
        if not isinstance(value, int):
            raise RustSafetyContractError(
                "Rust safety auth TTL response failed contract validation",
                code="auth_structured_output_invalid",
            )
        return int(value)
    value = _saturating_i64_add(int(issued_at_ms), int(ttl_ms))
    _record_safety_shadow(
        component="auth_core",
        operation_name="expires_at_ms",
        input_payload={"issued_at_ms": int(issued_at_ms), "ttl_ms": int(ttl_ms)},
        legacy_output={"value": value},
        rust_call=lambda module: _call_pyo3_json(
            module,
            "expires_at_ms_json",
            {"issued_at_ms": int(issued_at_ms), "ttl_ms": int(ttl_ms)},
            "Rust safety auth TTL response",
        ),
        rust_output=lambda response: {"value": response.get("value"), "error": response.get("error")},
    )
    return value


def ensure_readonly_sql(sql: str) -> None:
    module = _pyo3_module_for_enforce_call("data_access")
    if module is not None:
        response = _call_pyo3_json(
            module,
            "ensure_readonly_sql_json",
            {"sql": str(sql)},
            "Rust safety readonly SQL response",
        )
        if response.get("error") is not None or response.get("allowed") is not True:
            raise PermissionError("SQL is not readonly.")
        return
    _ensure_readonly_sql_py(sql)
    _record_safety_shadow(
        component="data_access",
        operation_name="ensure_readonly_sql",
        input_payload={"sql_fingerprint": _fingerprint(str(sql).encode("utf-8"))},
        legacy_output={"allowed": True},
        rust_call=lambda module: _call_pyo3_json(
            module,
            "ensure_readonly_sql_json",
            {"sql": str(sql)},
            "Rust safety readonly SQL response",
        ),
        rust_output=lambda response: {"allowed": response.get("allowed"), "error": response.get("error")},
    )


def validate_data_access_shape(*, row_count: int, column_count: int, result_bytes: int) -> None:
    module = _pyo3_module_for_enforce_call("data_access")
    if module is not None:
        response = _call_pyo3_json(
            module,
            "validate_shape_json",
            {
                "row_count": int(row_count),
                "column_count": int(column_count),
                "result_bytes": int(result_bytes),
            },
            "Rust safety readonly shape response",
        )
        error = response.get("error")
        if error is not None or response.get("valid") is not True:
            code = str(error.get("code") or "data_access_structured_output_invalid") if isinstance(error, Mapping) else "data_access_structured_output_invalid"
            raise DataAccessContractError(_data_access_limit_message(code), code=code)
        return
    _validate_data_access_shape_py(row_count=int(row_count), column_count=int(column_count), result_bytes=int(result_bytes))
    _record_safety_shadow(
        component="data_access",
        operation_name="validate_data_access_shape",
        input_payload={"row_count": int(row_count), "column_count": int(column_count), "result_bytes": int(result_bytes)},
        legacy_output={"valid": True},
        rust_call=lambda module: _call_pyo3_json(
            module,
            "validate_shape_json",
            {
                "row_count": int(row_count),
                "column_count": int(column_count),
                "result_bytes": int(result_bytes),
            },
            "Rust safety readonly shape response",
        ),
        rust_output=lambda response: {"valid": response.get("valid"), "error": response.get("error")},
    )


def sanitize_audit_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    module = _pyo3_module_for_enforce_call("audit_sanitizer")
    if module is not None:
        response = _call_pyo3_json(
            module,
            "sanitize_value_json",
            {"value": dict(payload)},
            "Rust safety audit sanitizer response",
        )
        error = response.get("error")
        if error is not None:
            raise RustSafetyContractError(
                _safe_error_message(error, "audit payload sanitizer failed"),
                code=str(error.get("code") or "audit_sanitizer_event_too_large")
                if isinstance(error, Mapping)
                else "audit_sanitizer_event_too_large",
            )
        value = response.get("value")
        if not isinstance(value, Mapping):
            raise RustSafetyContractError(
                "Rust safety audit sanitizer response failed contract validation",
                code="audit_sanitizer_structured_output_invalid",
            )
        return dict(value)
    value = dict(_sanitize_value_py(dict(payload)))
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if len(encoded.encode("utf-8")) > resource_limit("audit_event_bytes"):
        raise RustSafetyContractError(
            "audit payload sanitizer output exceeds event size limit",
            code="audit_sanitizer_event_too_large",
        )
    _record_safety_shadow(
        component="audit_sanitizer",
        operation_name="sanitize_audit_payload",
        input_payload={"payload_fingerprint": _fingerprint_json(payload)},
        legacy_output=value,
        rust_call=lambda module: _call_pyo3_json(
            module,
            "sanitize_value_json",
            {"value": dict(payload)},
            "Rust safety audit sanitizer response",
        ),
        rust_output=lambda response: {"value": response.get("value"), "error": response.get("error")},
    )
    return value


def _load_checked_in_contract() -> dict[str, Any]:
    with _CONTRACT_PATH.open("r", encoding="utf-8") as handle:
        contract = json.load(handle)
    if not isinstance(contract, Mapping):
        raise RustSafetyContractError("safety contract artifact is not a JSON object")
    contract = dict(contract)
    if contract.get("component") != "maf_safety_kernels":
        raise RustSafetyContractError("safety contract artifact component mismatch")
    for key in ("contract_version", "schema_hash", "error_code_table_hash", "supported_features"):
        if key not in contract:
            raise RustSafetyContractError(f"safety contract artifact missing {key}")
    return contract


def _load_pyo3_contract(module_name: str) -> dict[str, Any]:
    module = importlib.import_module(module_name)
    contract_json = getattr(module, "contract_json", None)
    if not callable(contract_json):
        raise RustSafetyContractError("Safety PyO3 module is missing contract_json entrypoint")
    return _parse_json_mapping(contract_json(), "Safety PyO3 contract")


def _ensure_contract_compatible(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    for key in ("component", "contract_version", "schema_hash", "error_code_table_hash"):
        if actual.get(key) != expected.get(key):
            raise RustSafetyContractError(
                "Safety PyO3 contract mismatch",
                safe_metadata={"field": key},
            )
    actual_features = {str(feature) for feature in actual.get("supported_features", ())}
    expected_features = {str(feature) for feature in expected.get("supported_features", ())}
    if not (expected_features | _REQUIRED_FEATURES).issubset(actual_features):
        raise RustSafetyContractError(
            "Safety PyO3 contract mismatch",
            safe_metadata={"field": "supported_features"},
        )


def _component_modes_fingerprint() -> tuple[tuple[str, str], ...]:
    return tuple(sorted((component, component_mode(component)) for component in _MODE_ENV_BY_COMPONENT))


def _pyo3_module_for_enforce_call(component: str) -> Any | None:
    if component_mode(component) != "enforce":
        return None
    load_safety_contract()
    return importlib.import_module(_pyo3_module_name())


def _pyo3_module_for_shadow_call(component: str) -> Any | None:
    if component_mode(component) != "shadow":
        return None
    try:
        load_safety_contract()
        return importlib.import_module(_pyo3_module_name())
    except (ModuleNotFoundError, RustSafetyContractError):
        return None


def _record_safety_shadow(
    *,
    component: str,
    operation_name: str,
    input_payload: Mapping[str, Any],
    legacy_output: Mapping[str, Any],
    rust_call: Callable[[Any], Mapping[str, Any]],
    rust_output: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> None:
    if _SHADOW_SINK is None or getattr(_SHADOW_DEPTH, "value", 0):
        return
    if component_mode(component) != "shadow":
        return

    payload: dict[str, str] = {
        "component": component,
        "operation": operation_name,
        "input_fingerprint": _fingerprint_json(input_payload),
        "legacy_output_fingerprint": _fingerprint_json(legacy_output),
        "legacy_status": "ok",
        "rust_status": "unknown",
    }
    started = time.perf_counter()
    try:
        module = _pyo3_module_for_shadow_call(component)
        if module is None:
            raise RuntimeError("audit_sanitizer_contract_mismatch: Safety PyO3 shadow module unavailable")
        response = rust_call(module)
        payload.update(
            {
                "rust_status": "ok",
                "error_code": "",
                "rust_output_fingerprint": _fingerprint_json(rust_output(response)),
            }
        )
    except Exception as exc:  # noqa: BLE001 - shadow mode must preserve Python-visible results.
        code, separator, _ = str(exc).partition(":")
        payload.update(
            {
                "rust_status": "error",
                "error_code": code if separator else type(exc).__name__,
                "rust_output_fingerprint": "",
            }
        )
    finally:
        payload["duration_ms"] = str(max(0, int((time.perf_counter() - started) * 1000)))
        try:
            _SHADOW_DEPTH.value = getattr(_SHADOW_DEPTH, "value", 0) + 1
            _SHADOW_SINK(payload)
        except Exception:  # noqa: BLE001 - audit sink failure must not affect shadow-mode user results.
            return
        finally:
            _SHADOW_DEPTH.value = max(0, getattr(_SHADOW_DEPTH, "value", 1) - 1)


def _pyo3_module_name() -> str:
    return os.environ.get(_MODULE_ENV, "").strip() or _DEFAULT_MODULE_NAME


def _fingerprint(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _fingerprint_json(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return _fingerprint(encoded)


def _call_pyo3_json(module: Any, function_name: str, payload: Mapping[str, Any], label: str) -> dict[str, Any]:
    function = getattr(module, function_name, None)
    if not callable(function):
        raise RustSafetyContractError(
            f"Safety PyO3 module is missing {function_name} entrypoint",
            code="audit_sanitizer_contract_mismatch",
        )
    raw = function(json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return _parse_json_mapping(raw, label)


def _parse_json_mapping(raw: Any, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError) as exc:
        raise RustSafetyContractError(f"{label} is not valid JSON") from exc
    if not isinstance(parsed, Mapping):
        raise RustSafetyContractError(f"{label} is not a JSON object")
    return dict(parsed)


def _safe_error_message(error: Any, fallback: str) -> str:
    if isinstance(error, Mapping):
        message = error.get("message")
        if isinstance(message, str) and message.strip():
            return message
    return fallback


def _normalize_storage_key_py(key: str) -> str:
    text = str(key)
    if not text or text.startswith("/") or text.startswith("~") or "\\" in text:
        raise ValueError("artifact path escapes managed root")
    parts = []
    for part in text.split("/"):
        if not part or part in {".", ".."} or "\x00" in part:
            raise ValueError("artifact path escapes managed root")
        parts.append(part)
    return "/".join(parts)


def _ensure_readonly_sql_py(sql: str) -> None:
    query = str(sql)
    _ensure_readonly_sql_tokens(_sql_policy_tokens(query, comments_break_tokens=True), require_readonly_statement=True)
    compact_tokens = _sql_policy_tokens(query, comments_break_tokens=False)
    _ensure_readonly_sql_tokens(compact_tokens, require_readonly_statement=False)


def _ensure_readonly_sql_tokens(tokens: list[str], *, require_readonly_statement: bool) -> None:
    if require_readonly_statement and (not tokens or ";" in tokens):
        raise PermissionError("SQL is not readonly.")
    if require_readonly_statement and tokens[0] not in {"select", "with"}:
        raise PermissionError("SQL is not readonly.")
    forbidden_single = {
        "insert",
        "update",
        "delete",
        "drop",
        "alter",
        "truncate",
        "create",
        "replace",
        "grant",
        "revoke",
        "merge",
        "call",
        "__mysql_executable_comment__",
    }
    if any(token in forbidden_single for token in tokens):
        raise PermissionError("SQL is not readonly.")
    for left, right in zip(tokens, tokens[1:], strict=False):
        if (left, right) in {("into", "outfile"), ("into", "dumpfile"), ("for", "update"), ("for", "share")}:
            raise PermissionError("SQL is not readonly.")
        if left in {"load_file", "get_lock", "release_lock"} and right == "(":
            raise PermissionError("SQL is not readonly.")
    for first_token, second_token, third_token in zip(tokens, tokens[1:], tokens[2:], strict=False):
        if (first_token, second_token, third_token) == ("lock", "in", "share"):
            raise PermissionError("SQL is not readonly.")


def _sql_policy_tokens(sql: str, *, comments_break_tokens: bool) -> list[str]:
    tokens: list[str] = []
    current: list[str] = []
    idx = 0
    length = len(sql)
    while idx < length:
        char = sql[idx]
        if char.isspace():
            _append_sql_token(tokens, current)
            idx += 1
            continue
        if sql.startswith("--", idx) and _is_mysql_line_comment(sql, idx):
            if comments_break_tokens:
                _append_sql_token(tokens, current)
            idx = _skip_until_newline(sql, idx + 2)
            continue
        if char == "#":
            if comments_break_tokens:
                _append_sql_token(tokens, current)
            idx = _skip_until_newline(sql, idx + 1)
            continue
        if sql.startswith("/*", idx):
            if sql.startswith("/*!", idx):
                _append_sql_token(tokens, current)
                tokens.append("__mysql_executable_comment__")
            elif comments_break_tokens:
                _append_sql_token(tokens, current)
            end = sql.find("*/", idx + 2)
            idx = length if end < 0 else end + 2
            continue
        if char in {"'", '"', "`"}:
            _append_sql_token(tokens, current)
            idx = _skip_quoted_sql(sql, idx, char)
            continue
        if char.isalnum() or char == "_":
            current.append(char.lower())
            idx += 1
            continue
        _append_sql_token(tokens, current)
        if char in {"(", ";"}:
            tokens.append(char)
        idx += 1
    _append_sql_token(tokens, current)
    if tokens and tokens[-1] == ";":
        tokens.pop()
    return tokens


def _append_sql_token(tokens: list[str], current: list[str]) -> None:
    if current:
        tokens.append("".join(current))
        current.clear()


def _skip_until_newline(text: str, idx: int) -> int:
    while idx < len(text) and text[idx] not in {"\n", "\r"}:
        idx += 1
    if idx >= len(text):
        return len(text)
    if text[idx] == "\r" and idx + 1 < len(text) and text[idx + 1] == "\n":
        return idx + 2
    return idx + 1


def _is_mysql_line_comment(sql: str, idx: int) -> bool:
    next_idx = idx + 2
    return next_idx < len(sql) and _is_mysql_comment_space(sql[next_idx])


def _is_mysql_comment_space(char: str) -> bool:
    return char.isspace() or ord(char) < 32 or ord(char) == 127


def _skip_quoted_sql(sql: str, start: int, quote: str) -> int:
    idx = start + 1
    length = len(sql)
    while idx < length:
        if sql[idx] == "\\":
            idx += 2
            continue
        if sql[idx] == quote:
            if idx + 1 < length and sql[idx + 1] == quote:
                idx += 2
                continue
            return idx + 1
        idx += 1
    return length


def _validate_data_access_shape_py(*, row_count: int, column_count: int, result_bytes: int) -> None:
    if row_count > resource_limit("db_row_limit"):
        raise DataAccessContractError(
            _data_access_limit_message("data_access_row_limit_exceeded"),
            code="data_access_row_limit_exceeded",
        )
    if column_count > resource_limit("db_column_limit"):
        raise DataAccessContractError(
            _data_access_limit_message("data_access_column_limit_exceeded"),
            code="data_access_column_limit_exceeded",
        )
    if result_bytes > resource_limit("db_result_bytes"):
        raise DataAccessContractError(
            _data_access_limit_message("data_access_result_too_large"),
            code="data_access_result_too_large",
        )


def _data_access_limit_message(code: str) -> str:
    if code == "data_access_row_limit_exceeded":
        return "readonly query result exceeds row limit"
    if code == "data_access_column_limit_exceeded":
        return "readonly query result exceeds column limit"
    if code == "data_access_result_too_large":
        return "readonly query result exceeds byte limit"
    return "readonly query result violates Rust safety contract"


def _sanitize_value_py(value: Any) -> Any:
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _is_sensitive_key(key_text):
                sanitized[key_text] = "[REDACTED]"
            else:
                sanitized[key_text] = _sanitize_value_py(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_value_py(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_value_py(item) for item in value]
    if isinstance(value, str):
        return _sanitize_text_py(value)
    return value


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in _SENSITIVE_PATH_KEYS or lowered in _SENSITIVE_PROMPT_KEYS or lowered in _SENSITIVE_ROWS_KEYS:
        return True
    if lowered.endswith("_prompt") or lowered.endswith("_rows"):
        return True
    return any(marker in lowered for marker in _SENSITIVE_KEY_MARKERS)


def _sanitize_text_py(text: str) -> str:
    sanitized = text
    for prefix in ("token=", "secret=", "password=", "base_url=", "prompt=", "dsn=", "path="):
        sanitized = _redact_assignment(sanitized, prefix)
    for marker in ("mysql://", "postgres://", "postgresql://"):
        sanitized = _redact_bare_value(sanitized, marker)
    return sanitized


def _redact_assignment(text: str, prefix: str) -> str:
    lowered = text.lower()
    output: list[str] = []
    cursor = 0
    while True:
        start = lowered.find(prefix, cursor)
        if start < 0:
            output.append(text[cursor:])
            return "".join(output)
        value_start = start + len(prefix)
        output.append(text[cursor:value_start])
        output.append("[REDACTED]")
        cursor = _sensitive_value_end(text, value_start)


def _redact_bare_value(text: str, marker: str) -> str:
    lowered = text.lower()
    output: list[str] = []
    cursor = 0
    while True:
        start = lowered.find(marker, cursor)
        if start < 0:
            output.append(text[cursor:])
            return "".join(output)
        output.append(text[cursor:start])
        output.append("[REDACTED]")
        cursor = _sensitive_value_end(text, start)


def _sensitive_value_end(text: str, start: int) -> int:
    for idx in range(start, len(text)):
        if text[idx].isspace() or text[idx] in ",;\"')}]":
            return idx
    return len(text)


def _bytes_payload(value: str | bytes) -> bytes:
    if isinstance(value, bytes):
        return value
    return str(value).encode("utf-8")


def _text_payload(value: str | bytes) -> str:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RustSafetyContractError(
                "Safety PyO3 JSON bridge requires UTF-8 auth payloads",
                code="auth_structured_output_invalid",
            ) from exc
    return str(value)


def _saturating_i64_add(left: int, right: int) -> int:
    value = left + right
    if value > 2**63 - 1:
        return 2**63 - 1
    if value < -(2**63):
        return -(2**63)
    return value


load_safety_contract.cache_clear = _load_safety_contract_for_mode.cache_clear  # type: ignore[attr-defined]


__all__ = [
    "DataAccessContractError",
    "RustSafetyContractError",
    "component_mode",
    "configure_safety_shadow_sink",
    "ensure_readonly_sql",
    "expires_at_ms",
    "hmac_sha256_hex",
    "load_safety_contract",
    "normalize_storage_key",
    "resource_limit",
    "sanitize_audit_payload",
    "sha256_hex",
    "validate_data_access_shape",
    "verify_auth_token",
]
