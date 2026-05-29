from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter(tags=["developer-docs"])

_API_DOCS_DIR = Path(__file__).resolve().parents[3] / "docs" / "api"
_API_DOC_PATH = _API_DOCS_DIR / "api-doc.html"
_API_CHANGELOG_PATH = _API_DOCS_DIR / "API更新日志.md"


@router.get("/api-doc", include_in_schema=False)
async def api_doc() -> FileResponse:
    """Serve the internal REST API documentation page."""
    if not _API_DOC_PATH.is_file():
        raise HTTPException(status_code=404, detail="API documentation page is not available.")
    return FileResponse(_API_DOC_PATH, media_type="text/html; charset=utf-8")


@router.get("/api-doc/API更新日志.md", include_in_schema=False)
@router.get("/api-doc/api-changelog.md", include_in_schema=False)
async def api_doc_changelog() -> FileResponse:
    """Serve the API changelog Markdown used by the documentation page."""
    if not _API_CHANGELOG_PATH.is_file():
        raise HTTPException(status_code=404, detail="API changelog is not available.")
    return FileResponse(_API_CHANGELOG_PATH, media_type="text/markdown; charset=utf-8")
