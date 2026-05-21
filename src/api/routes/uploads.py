from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status

from ..auth import require_authenticated_user
from ..dto import DeleteUploadRequest, DeleteUploadResponse, UploadFileResponse, UploadListResponse, UploadPreviewResponse
from ..runtime import ApiRuntime
from ..upload_store import UploadValidationError

router = APIRouter()
UPLOAD_READ_CHUNK_BYTES = 64 * 1024


def _runtime(request: Request) -> ApiRuntime:
    return request.app.state.runtime


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


@router.get("/api/v1/conversations/{conversation_id}/uploads", response_model=UploadListResponse)
async def list_conversation_uploads(conversation_id: str, request: Request) -> UploadListResponse:
    runtime = _runtime(request)
    user = await require_authenticated_user(request, required_scopes=("conversation:read",))
    try:
        records = await runtime.list_uploads(conversation_id, user.username)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown conversation: {conversation_id}") from exc
    return UploadListResponse(
        conversation_id=conversation_id,
        uploads=[_upload_response(record) for record in records],
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
    user = await require_authenticated_user(request, required_scopes=("upload:write",))
    try:
        await runtime.ensure_upload_allowed(conversation_id, user.username)
        content = await _read_upload_content_with_limit(
            file,
            max_bytes=runtime.upload_store.max_file_bytes,
        )
        record = await runtime.save_upload(
            conversation_id=conversation_id,
            account_id=user.username,
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
    user = await require_authenticated_user(request, required_scopes=("upload:write",))
    conversation_id = body.conversation_id
    upload_id = body.upload_id
    try:
        deleted = await runtime.delete_upload(conversation_id, user.username, upload_id)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown upload: {upload_id}") from exc
    return DeleteUploadResponse(upload_id=upload_id, deleted=deleted)
