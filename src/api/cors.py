from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

_ALLOWED_HEADERS = ("Authorization", "Content-Type", "Accept")
_ALLOWED_METHODS = ("GET", "POST", "PATCH", "DELETE", "OPTIONS")


def parse_cors_allowed_origins(raw: str | None = None) -> tuple[str, ...]:
    value = os.getenv("MAF_API_CORS_ALLOWED_ORIGINS", "") if raw is None else raw
    origins = tuple(dict.fromkeys(item.strip().rstrip("/") for item in value.split(",") if item.strip()))
    if "*" in origins:
        raise ValueError("MAF_API_CORS_ALLOWED_ORIGINS must not contain '*' for authenticated API access.")
    return origins


def configure_cors(app: FastAPI, *, allowed_origins: tuple[str, ...] | None = None) -> None:
    origins = parse_cors_allowed_origins() if allowed_origins is None else allowed_origins
    if not origins:
        return
    if "*" in origins:
        raise ValueError("CORS wildcard origins are not allowed for authenticated API access.")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(origins),
        allow_credentials=False,
        allow_methods=list(_ALLOWED_METHODS),
        allow_headers=list(_ALLOWED_HEADERS),
    )
