from __future__ import annotations

import hashlib
import inspect
import json
import time
from collections.abc import Callable, Mapping
from typing import Any

from src.storage.rust_contract import error_policy, mode_for_component
from src.storage.runtime_sidecar_facade import validate_runtime_sidecar_response

RuntimeSidecarShadowSink = Callable[[Mapping[str, str]], Any]


async def record_runtime_sidecar_shadow_write(
    *,
    component: str,
    operation_name: str,
    runtime_sidecar_client: Any | None,
    shadow_sink: RuntimeSidecarShadowSink | None,
    input_payload: Mapping[str, Any],
    legacy_output: Mapping[str, Any],
    rust_call: Callable[[], Any],
    rust_output: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> None:
    if mode_for_component(component) != "shadow" or runtime_sidecar_client is None or shadow_sink is None:
        return

    payload = _build_shadow_payload(
        component=component,
        operation_name=operation_name,
        input_payload=input_payload,
        legacy_output=legacy_output,
    )
    started = time.perf_counter()
    try:
        response = rust_call()
        response = await response if inspect.isawaitable(response) else response
        _apply_shadow_response(payload, operation_name, response, rust_output)
    except Exception as exc:  # noqa: BLE001 - shadow mode must preserve the Python-visible result.
        _apply_shadow_exception(payload, exc)
    finally:
        payload["duration_ms"] = _duration_ms(started)
        try:
            result = shadow_sink(payload)
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 - audit sink failure must not affect shadow-mode user results.
            return


def record_runtime_sidecar_shadow_write_sync(
    *,
    component: str,
    operation_name: str,
    runtime_sidecar_client: Any | None,
    shadow_sink: RuntimeSidecarShadowSink | None,
    input_payload: Mapping[str, Any],
    legacy_output: Mapping[str, Any],
    rust_call: Callable[[], Any],
    rust_output: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> None:
    if mode_for_component(component) != "shadow" or runtime_sidecar_client is None or shadow_sink is None:
        return

    payload = _build_shadow_payload(
        component=component,
        operation_name=operation_name,
        input_payload=input_payload,
        legacy_output=legacy_output,
    )
    started = time.perf_counter()
    try:
        response = rust_call()
        if inspect.isawaitable(response):
            _close_awaitable(response)
            raise RuntimeError(
                f"{_unavailable_code_for_component(component)}: "
                "async sidecar response is not supported in sync shadow path"
            )
        _apply_shadow_response(payload, operation_name, response, rust_output)
    except Exception as exc:  # noqa: BLE001 - shadow mode must preserve the Python-visible result.
        _apply_shadow_exception(payload, exc)
    finally:
        payload["duration_ms"] = _duration_ms(started)
        try:
            result = shadow_sink(payload)
            if inspect.isawaitable(result):
                _close_awaitable(result)
                raise RuntimeError(
                    "runtime_store_unavailable: async audit sink is not supported in sync shadow path"
                )
        except Exception:  # noqa: BLE001 - audit sink failure must not affect shadow-mode user results.
            return


def runtime_sidecar_fingerprint(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def runtime_sidecar_error_code(error: Exception) -> str:
    message = str(error)
    code, separator, _ = message.partition(":")
    if separator:
        try:
            return str(error_policy(code)["code"])
        except KeyError:
            pass
    return type(error).__name__


def normalize_runtime_sidecar_response(operation_name: str, response: Any) -> Any:
    if not isinstance(response, dict) or response.get("operation") == operation_name:
        return response
    if operation_name == "event_append":
        return {"operation": operation_name, "cursor": response, "error": None}
    if operation_name == "task_edge_save":
        return {"operation": operation_name, "edge": response, "error": None}
    if operation_name == "artifact_save":
        return {"operation": operation_name, "artifact": response, "error": None}
    return response


def _build_shadow_payload(
    *,
    component: str,
    operation_name: str,
    input_payload: Mapping[str, Any],
    legacy_output: Mapping[str, Any],
) -> dict[str, str]:
    return {
        "component": component,
        "operation": operation_name,
        "input_fingerprint": runtime_sidecar_fingerprint(input_payload),
        "legacy_output_fingerprint": runtime_sidecar_fingerprint(legacy_output),
        "legacy_status": "ok",
        "rust_status": "unknown",
    }


def _apply_shadow_response(
    payload: dict[str, str],
    operation_name: str,
    response: Any,
    rust_output: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> None:
    envelope = validate_runtime_sidecar_response(
        operation_name,
        normalize_runtime_sidecar_response(operation_name, response),
    )
    error = envelope.get("error")
    if isinstance(error, Mapping):
        payload.update(
            {
                "rust_status": "error",
                "error_code": str(error["code"]),
                "rust_output_fingerprint": "",
            }
        )
        return
    payload.update(
        {
            "rust_status": "ok",
            "error_code": "",
            "rust_output_fingerprint": runtime_sidecar_fingerprint(rust_output(envelope)),
        }
    )


def _apply_shadow_exception(payload: dict[str, str], exc: Exception) -> None:
    payload.update(
        {
            "rust_status": "error",
            "error_code": runtime_sidecar_error_code(exc),
            "rust_output_fingerprint": "",
        }
    )


def _duration_ms(started: float) -> str:
    return str(max(0, int((time.perf_counter() - started) * 1000)))


def _close_awaitable(value: Any) -> None:
    close = getattr(value, "close", None)
    if callable(close):
        close()


def _unavailable_code_for_component(component: str) -> str:
    if component == "task_dispatcher":
        return "dispatcher_unavailable"
    if component == "event_log":
        return "event_log_unavailable"
    return "runtime_store_unavailable"


__all__ = [
    "RuntimeSidecarShadowSink",
    "normalize_runtime_sidecar_response",
    "record_runtime_sidecar_shadow_write",
    "record_runtime_sidecar_shadow_write_sync",
    "runtime_sidecar_error_code",
    "runtime_sidecar_fingerprint",
]
