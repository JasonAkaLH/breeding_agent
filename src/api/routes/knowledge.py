from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile

from src.capabilities.knowledge.service import KnowledgeError

from ..auth import require_authenticated_user
from ..knowledge_dto import (
    KnowledgeBatchConfirm,
    KnowledgeConfirm,
    KnowledgeGrantCreate,
    KnowledgeLibraryCreate,
    KnowledgeLibraryPatch,
    KnowledgeMetadataPatch,
    KnowledgeSelectionSave,
)
from ..runtime_access import runtime_from_request
from ..upload_store import DEFAULT_MAX_UPLOAD_FILE_BYTES


router = APIRouter(prefix="/api/v1", tags=["knowledge"])


def _service(request: Request):
    service = getattr(runtime_from_request(request), "knowledge_service", None)
    if service is None:
        raise HTTPException(503, detail={"code": "knowledge_unavailable"})
    return service


async def _call(operation, *args, **kwargs):
    try:
        return await operation(*args, **kwargs)
    except KnowledgeError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code}) from exc


@router.get("/knowledge/libraries")
async def list_libraries(request: Request):
    actor = (await require_authenticated_user(request)).username
    return {"libraries": await _call(_service(request).list_libraries, actor)}


@router.post("/knowledge/libraries", status_code=201)
async def create_library(payload: KnowledgeLibraryCreate, request: Request):
    actor = (await require_authenticated_user(request)).username
    return await _call(_service(request).create_library, actor, payload.name, payload.description)


@router.get("/knowledge/libraries/{library_id}")
async def get_library(library_id: str, request: Request):
    actor = (await require_authenticated_user(request)).username
    return await _call(_service(request).get_library, actor, library_id)


@router.patch("/knowledge/libraries/{library_id}")
async def update_library(library_id: str, payload: KnowledgeLibraryPatch, request: Request):
    actor = (await require_authenticated_user(request)).username
    return await _call(
        _service(request).update_library, actor, library_id,
        name=payload.name, description=payload.description, expected_revision=payload.revision,
    )


@router.delete("/knowledge/libraries/{library_id}")
async def delete_library(library_id: str, request: Request, revision: int | None = None):
    actor = (await require_authenticated_user(request)).username
    return await _call(_service(request).delete_library, actor, library_id, expected_revision=revision)


@router.get("/knowledge/libraries/{library_id}/index")
async def read_index(library_id: str, request: Request):
    actor = (await require_authenticated_user(request)).username
    return await _call(_service(request).read_index, actor, library_id)


@router.post("/knowledge/libraries/{library_id}/imports", status_code=202)
async def upload_imports(
    library_id: str, request: Request, files: list[UploadFile] = File(...),
    replacement_document_id: str | None = Form(None),
):
    actor = (await require_authenticated_user(request)).username
    service = _service(request)
    # Check permission before reading multipart file bodies into memory.
    library = await _call(service.get_library, actor, library_id)
    if library.get("role") != "owner":
        raise HTTPException(403, detail={"code": "knowledge_read_only"})
    results = []
    try:
        for file in files:
            content = bytearray()
            while chunk := await file.read(1024 * 1024):
                content.extend(chunk)
                if len(content) > DEFAULT_MAX_UPLOAD_FILE_BYTES:
                    results.append({
                        "filename": file.filename or "document.md", "status": "failed",
                        "error": "knowledge_file_too_large",
                    })
                    break
            else:
                results.extend(await _call(
                    service.upload_imports, actor, library_id,
                    [(file.filename or "document.md", bytes(content))],
                    replacement_document_id=replacement_document_id,
                ))
        return {"imports": results}
    finally:
        for file in files:
            await file.close()


@router.get("/knowledge/libraries/{library_id}/imports")
async def list_imports(library_id: str, request: Request):
    actor = (await require_authenticated_user(request)).username
    return {"imports": await _call(_service(request).list_imports, actor, library_id)}


@router.post("/knowledge/libraries/{library_id}/imports/confirm")
async def batch_confirm(library_id: str, payload: KnowledgeBatchConfirm, request: Request):
    actor = (await require_authenticated_user(request)).username
    return {"results": await _call(
        _service(request).batch_confirm, actor, library_id,
        [item.model_dump() for item in payload.imports],
    )}


@router.get("/knowledge/libraries/{library_id}/imports/{import_id}")
async def get_import(library_id: str, import_id: str, request: Request):
    actor = (await require_authenticated_user(request)).username
    return await _call(_service(request).get_import, actor, library_id, import_id)


@router.get("/knowledge/libraries/{library_id}/imports/{import_id}/content")
async def preview_import(library_id: str, import_id: str, request: Request):
    actor = (await require_authenticated_user(request)).username
    result = await _call(_service(request).preview_import, actor, library_id, import_id)
    return Response(result["content"], media_type="text/markdown", headers={"Cache-Control": "no-store", "X-Content-SHA256": result["sha256"]})


def _file_response(result, *, download: bool = True):
    headers = {"Cache-Control": "no-store", "X-Content-SHA256": result["sha256"]}
    if download:
        filename = quote(result["filename"], safe="")
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{filename}"
    return Response(result["content"], media_type=result["media_type"], headers=headers)


@router.get("/knowledge/libraries/{library_id}/imports/{import_id}/source")
async def download_import_source(library_id: str, import_id: str, request: Request):
    actor = (await require_authenticated_user(request)).username
    return _file_response(await _call(_service(request).download_import_source, actor, library_id, import_id))


@router.patch("/knowledge/libraries/{library_id}/imports/{import_id}")
async def edit_import(library_id: str, import_id: str, payload: KnowledgeMetadataPatch, request: Request):
    actor = (await require_authenticated_user(request)).username
    return await _call(
        _service(request).edit_import, actor, library_id, import_id,
        payload.metadata.model_dump(), expected_revision=payload.revision,
    )


@router.post("/knowledge/libraries/{library_id}/imports/{import_id}/confirm")
async def confirm_import(library_id: str, import_id: str, payload: KnowledgeConfirm, request: Request):
    actor = (await require_authenticated_user(request)).username
    return await _call(
        _service(request).confirm_import, actor, library_id, import_id,
        expected_revision=payload.revision,
    )


@router.post("/knowledge/libraries/{library_id}/imports/{import_id}/retry")
async def retry_import(library_id: str, import_id: str, request: Request):
    actor = (await require_authenticated_user(request)).username
    return await _call(_service(request).retry_import, actor, library_id, import_id)


@router.delete("/knowledge/libraries/{library_id}/imports/{import_id}")
async def discard_import(library_id: str, import_id: str, request: Request, revision: int | None = None):
    actor = (await require_authenticated_user(request)).username
    return await _call(
        _service(request).discard_import, actor, library_id, import_id, expected_revision=revision,
    )


@router.get("/knowledge/libraries/{library_id}/documents")
async def list_documents(library_id: str, request: Request):
    actor = (await require_authenticated_user(request)).username
    return {"documents": await _call(_service(request).list_documents, actor, library_id)}


@router.patch("/knowledge/libraries/{library_id}/documents/{document_id}")
async def update_document(
    library_id: str, document_id: str, payload: KnowledgeMetadataPatch, request: Request,
):
    actor = (await require_authenticated_user(request)).username
    return await _call(
        _service(request).update_document, actor, library_id, document_id,
        payload.metadata.model_dump(), expected_revision=payload.revision,
    )


@router.delete("/knowledge/libraries/{library_id}/documents/{document_id}")
async def delete_document(library_id: str, document_id: str, request: Request, revision: int | None = None):
    actor = (await require_authenticated_user(request)).username
    return await _call(
        _service(request).delete_document, actor, library_id, document_id, expected_revision=revision,
    )


@router.get("/knowledge/libraries/{library_id}/documents/{document_id}/content")
async def read_document(library_id: str, document_id: str, request: Request, download: bool = False):
    actor = (await require_authenticated_user(request)).username
    result = await _call(_service(request).download_document_text, actor, library_id, document_id)
    return _file_response(result, download=download)


@router.get("/knowledge/libraries/{library_id}/documents/{document_id}/source")
async def download_document_source(library_id: str, document_id: str, request: Request):
    actor = (await require_authenticated_user(request)).username
    return _file_response(await _call(_service(request).download_document, actor, library_id, document_id))


@router.get("/knowledge/libraries/{library_id}/grants")
async def list_grants(library_id: str, request: Request):
    actor = (await require_authenticated_user(request)).username
    return {"grants": await _call(_service(request).list_grants, actor, library_id)}


@router.post("/knowledge/libraries/{library_id}/grants", status_code=201)
async def add_grant(library_id: str, payload: KnowledgeGrantCreate, request: Request):
    actor = (await require_authenticated_user(request)).username
    return await _call(
        _service(request).add_grant, actor, library_id, payload.subject_id,
        subject_type=payload.subject_type, role=payload.role,
    )


@router.delete("/knowledge/libraries/{library_id}/grants/{grant_id}", status_code=204)
async def revoke_grant(library_id: str, grant_id: str, request: Request):
    actor = (await require_authenticated_user(request)).username
    await _call(_service(request).revoke_grant, actor, library_id, grant_id)
    return Response(status_code=204)


@router.get("/conversations/{conversation_id}/knowledge-selection")
async def get_selection(conversation_id: str, request: Request):
    actor = (await require_authenticated_user(request)).username
    runtime = runtime_from_request(request)
    return await _call(runtime.get_knowledge_selection, actor, conversation_id)


@router.put("/conversations/{conversation_id}/knowledge-selection")
async def save_selection(conversation_id: str, payload: KnowledgeSelectionSave, request: Request):
    actor = (await require_authenticated_user(request)).username
    runtime = runtime_from_request(request)
    return await _call(
        runtime.save_knowledge_selection, actor, conversation_id, payload.mode,
        payload.library_ids, expected_revision=payload.revision,
    )
