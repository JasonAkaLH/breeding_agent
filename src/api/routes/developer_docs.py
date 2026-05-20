from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter(tags=["developer-docs"])

_API_DOC_PATH = Path(__file__).resolve().parents[3] / "docs" / "api" / "api-doc.html"


@router.get("/api-doc", include_in_schema=False)
async def api_doc() -> FileResponse:
    """Serve the internal REST API documentation page."""
    if not _API_DOC_PATH.is_file():
        raise HTTPException(status_code=404, detail="API documentation page is not available.")
    return FileResponse(_API_DOC_PATH, media_type="text/html; charset=utf-8")
