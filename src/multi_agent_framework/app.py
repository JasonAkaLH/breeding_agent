from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from multi_agent_framework import __version__
from multi_agent_framework.api.routes import router
from multi_agent_framework.bootstrap import build_registry
from multi_agent_framework.config import get_settings
from multi_agent_framework.core.registry import AgentNotFoundError
from multi_agent_framework.infra.logging import configure_logging
from multi_agent_framework.orchestration.coordinator import Coordinator


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)

    registry = build_registry()
    coordinator = Coordinator(registry=registry)

    app.state.settings = settings
    app.state.registry = registry
    app.state.coordinator = coordinator
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.service_name,
        version=__version__,
        debug=settings.debug,
        lifespan=lifespan,
    )

    @app.exception_handler(AgentNotFoundError)
    async def agent_not_found_handler(_: Request, exc: AgentNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    app.include_router(router, prefix=settings.api_prefix)
    return app


app = create_app()
