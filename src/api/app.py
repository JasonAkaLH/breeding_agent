from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from .routes.auth import router as auth_router
from .routes.capabilities import router as capabilities_router
from .routes.conversations import router as conversations_router
from .routes.tasks import router as tasks_router
from .routes.uploads import router as uploads_router
from .runtime import ApiRuntime, build_api_runtime


def create_app(*, runtime: ApiRuntime | None = None) -> FastAPI:
    app = FastAPI(title="multi_agent_framework API", version="0.1.0")
    app.state.runtime = runtime or build_api_runtime(
        database_path=Path("runtime/dev.sqlite3"),
        audit_log_path=Path("runtime/audit.jsonl"),
    )
    app.include_router(auth_router)
    app.include_router(conversations_router)
    app.include_router(tasks_router)
    app.include_router(uploads_router)
    app.include_router(capabilities_router)
    return app
