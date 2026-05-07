from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, Request, UploadFile, status

from ..auth import require_authenticated_user
from ..dto import DeleteUploadResponse, UploadFileResponse, UploadListResponse, UploadPreviewResponse
from ..runtime import ApiRuntime
from ..upload_store import UploadValidationError

router = APIRouter()


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


@router.get("/api/v1/conversations/{conversation_id}/uploads", response_model=UploadListResponse)
async def list_conversation_uploads(conversation_id: str, request: Request) -> UploadListResponse:
    runtime = _runtime(request)
    user = await require_authenticated_user(request)
    try:
        records = await runtime.list_uploads(conversation_id, user.username)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown conversation: {conversation_id}") from exc
    return UploadListResponse(
        conversation_id=conversation_id,
        uploads=[_upload_response(record) for record in records],
    )


@router.post(
    "/api/v1/conversations/{conversation_id}/uploads",
    response_model=UploadFileResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_conversation_file(
    conversation_id: str,
    request: Request,
    file: UploadFile = File(...),
) -> UploadFileResponse:
    runtime = _runtime(request)
    user = await require_authenticated_user(request)
    content = await file.read()
    try:
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


@router.delete("/api/v1/conversations/{conversation_id}/uploads/{upload_id}", response_model=DeleteUploadResponse)
async def delete_conversation_upload(conversation_id: str, upload_id: str, request: Request) -> DeleteUploadResponse:
    runtime = _runtime(request)
    user = await require_authenticated_user(request)
    try:
        deleted = await runtime.delete_upload(conversation_id, user.username, upload_id)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown upload: {upload_id}") from exc
    return DeleteUploadResponse(upload_id=upload_id, deleted=deleted)
