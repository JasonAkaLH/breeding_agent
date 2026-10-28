from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from .cors import configure_cors
from .routes.auth import router as auth_router
from .routes.capabilities import router as capabilities_router
from .routes.config import router as config_router
from .routes.conversations import router as conversations_router
from .routes.developer_docs import router as developer_docs_router
from .routes.tasks import router as tasks_router
from .routes.uploads import router as uploads_router
from .routes.user_mcp import router as user_mcp_router
from .routes.user_mcp_grants import router as user_mcp_grants_router
from .runtime import ApiRuntime, build_api_runtime


def create_app(*, runtime: ApiRuntime | None = None) -> FastAPI:
    resolved_runtime = runtime or build_api_runtime(
        database_path=Path("runtime/dev.sqlite3"),
        audit_log_path=Path("runtime/audit.jsonl"),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await app.state.runtime.start()
        try:
            yield
        finally:
            await app.state.runtime.shutdown()

    app = FastAPI(title="breeding_agent API", version="0.1.0", lifespan=lifespan)
    app.state.runtime = resolved_runtime
    configure_cors(app)
    app.include_router(developer_docs_router)
    app.include_router(auth_router)
    app.include_router(conversations_router)
    app.include_router(tasks_router)
    app.include_router(uploads_router)
    app.include_router(capabilities_router)
    app.include_router(config_router)
    app.include_router(user_mcp_router)
    app.include_router(user_mcp_grants_router)
    return app
