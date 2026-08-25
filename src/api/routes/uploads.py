from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile, status

from ..auth import require_authenticated_user
from ..dto import (
    DeleteUploadRequest,
    DeleteUploadResponse,
    UploadFileResponse,
    UploadListResponse,
    UploadPreviewResponse,
    is_reserved_identity_key,
)
from ..runtime_access import runtime_from_request as _runtime
from ..upload_store import UploadValidationError

router = APIRouter()
UPLOAD_READ_CHUNK_BYTES = 64 * 1024
_UPLOAD_FORM_FIELDS = frozenset({"conversation_id", "file"})


def _upload_response(record) -> UploadFileResponse:
    return UploadFileResponse(
        upload_id=record.upload_id,
        conversation_id=record.conversation_id,
        filename=record.filename,
        content_type=record.content_type,
        file_type=record.file_type,
        size_bytes=record.size_bytes,
        sha256=record.sha256,
        expires_at=record.expires_at,
        status=getattr(record, "status", None),
        description_status=getattr(record, "description_status", None),
        preview=UploadPreviewResponse(**record.preview),
    )


async def _read_upload_content_with_limit(
    file: UploadFile,
    *,
    max_bytes: int,
    chunk_size: int = UPLOAD_READ_CHUNK_BYTES,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise UploadValidationError(f"Uploaded file exceeds {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


async def _reject_unexpected_upload_form_fields(request: Request) -> None:
    form = await request.form()
    unexpected_fields = sorted(str(key) for key in form.keys() if str(key) not in _UPLOAD_FORM_FIELDS)
    if not unexpected_fields:
        return
    reserved_fields = [field for field in unexpected_fields if is_reserved_identity_key(field)]
    reason = "reserved identity fields" if reserved_fields else "unsupported fields"
    raise HTTPException(
        status_code=422,
        detail=f"Upload form contains {reason}: {', '.join(unexpected_fields)}",
    )


@router.get("/api/v1/conversations/{conversation_id}/uploads", response_model=UploadListResponse, response_model_exclude_none=True)
async def list_conversation_uploads(
    conversation_id: str,
    request: Request,
    limit: int | None = Query(default=None, ge=1, le=500),
    cursor: str | None = Query(default=None),
    include_deleted: bool = Query(default=False),
) -> UploadListResponse:
    runtime = _runtime(request)
    user = await require_authenticated_user(request)
    try:
        records = await runtime.list_uploads(
            conversation_id,
            user.username,
            include_deleted=include_deleted,
            limit=limit,
            cursor=cursor,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown conversation: {conversation_id}") from exc
    next_cursor = records[-1].upload_id if limit is not None and len(records) == limit else None
    return UploadListResponse(
        conversation_id=conversation_id,
        uploads=[_upload_response(record) for record in records],
        next_cursor=next_cursor,
    )


@router.post(
    "/api/v1/conversations/uploads",
    response_model=UploadFileResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_conversation_file(
    request: Request,
    conversation_id: str = Form(...),
    file: UploadFile = File(...),
) -> UploadFileResponse:
    runtime = _runtime(request)
    user = await require_authenticated_user(request)
    await _reject_unexpected_upload_form_fields(request)
    try:
        await runtime.ensure_upload_allowed(conversation_id, user.username)
        content = await _read_upload_content_with_limit(
            file,
            max_bytes=runtime.upload_store.max_file_bytes,
        )
        record = await runtime.save_upload(
            conversation_id=conversation_id,
            username=user.username,
            filename=file.filename or "upload",
            content_type=file.content_type,
            content=content,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown conversation: {conversation_id}") from exc
    except UploadValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return _upload_response(record)


@router.delete("/api/v1/conversations/uploads", response_model=DeleteUploadResponse)
async def delete_conversation_upload(body: DeleteUploadRequest, request: Request) -> DeleteUploadResponse:
    runtime = _runtime(request)
    user = await require_authenticated_user(request)
    conversation_id = body.conversation_id
    upload_id = body.upload_id
    try:
        deleted = await runtime.delete_upload(conversation_id, user.username, upload_id)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown upload: {upload_id}") from exc
    except UploadValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return DeleteUploadResponse(upload_id=upload_id, deleted=deleted)
