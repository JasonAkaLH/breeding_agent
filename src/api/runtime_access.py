from __future__ import annotations

from fastapi import Request

from .runtime import ApiRuntime


def runtime_from_request(request: Request) -> ApiRuntime:
    return request.app.state.runtime
