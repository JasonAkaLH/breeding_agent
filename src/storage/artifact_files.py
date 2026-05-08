from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


@dataclass(slots=True, frozen=True)
class StoredArtifactFile:
    storage_key: str
    filename: str
    size_bytes: int
    sha256: str


class LocalArtifactFileStore:
    """Local managed file store for downloadable artifact bodies.

    The public database stores only opaque storage keys produced by this class;
    callers must not pass real filesystem paths to API responses or prompts.
    """

    def __init__(self, root_dir: str | Path) -> None:
        self._root_dir = Path(root_dir)
        self._root_dir.mkdir(parents=True, exist_ok=True)

    @property
    def root_dir(self) -> Path:
        return self._root_dir

    def save_file(self, *, artifact_id: str, filename: str, source_path: str | Path) -> StoredArtifactFile:
        source = Path(source_path)
        if not source.exists() or not source.is_file():
            raise FileNotFoundError(f"Artifact source file does not exist: {source}")
        safe_filename = sanitize_download_filename(filename)
        artifact_dir = self._safe_artifact_dir(artifact_id)
        if artifact_dir.exists():
            shutil.rmtree(artifact_dir)
        artifact_dir.mkdir(parents=True, exist_ok=False)
        target = artifact_dir / safe_filename
        digest = hashlib.sha256()
        size = 0
        try:
            with source.open("rb") as src, target.open("xb") as dst:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    size += len(chunk)
                    dst.write(chunk)
        except Exception:
            shutil.rmtree(artifact_dir, ignore_errors=True)
            raise
        return StoredArtifactFile(
            storage_key=f"{artifact_id}/{safe_filename}",
            filename=safe_filename,
            size_bytes=size,
            sha256=digest.hexdigest(),
        )

    def open_path(self, storage_key: str) -> Path:
        return self._resolve_storage_key(storage_key)

    def delete(self, storage_key: str) -> bool:
        path = self._resolve_storage_key(storage_key)
        existed = path.exists()
        if existed:
            path.unlink()
        parent = path.parent
        if parent != self._root_dir and parent.exists():
            try:
                parent.rmdir()
            except OSError:
                pass
        return existed

    def _safe_artifact_dir(self, artifact_id: str) -> Path:
        normalized = sanitize_storage_component(artifact_id)
        path = (self._root_dir / normalized).resolve()
        if not path.is_relative_to(self._root_dir.resolve()):
            raise ValueError("Artifact id escapes artifact store")
        return path

    def _resolve_storage_key(self, storage_key: str) -> Path:
        pure = PurePosixPath(storage_key)
        if pure.is_absolute() or len(pure.parts) != 2 or any(part in {"", ".", ".."} for part in pure.parts):
            raise ValueError("Invalid artifact storage key")
        artifact_id, filename = pure.parts
        path = (self._root_dir / sanitize_storage_component(artifact_id) / sanitize_download_filename(filename)).resolve()
        if not path.is_relative_to(self._root_dir.resolve()):
            raise ValueError("Artifact storage key escapes artifact store")
        return path


def sanitize_storage_component(value: str) -> str:
    text = str(value).strip()
    text = re.sub(r"[^A-Za-z0-9_.:-]+", "_", text)
    text = text.strip("._")
    if not text:
        raise ValueError("Storage component cannot be empty")
    return text[:160]


def sanitize_download_filename(value: str) -> str:
    text = Path(str(value).replace("\\", "/")).name.strip()
    text = re.sub(r"[\x00-\x1f\x7f]+", "_", text)
    text = re.sub(r"[/\\]+", "_", text)
    text = text.strip(" .")
    return (text or "download.bin")[:200]


def build_file_storage_ref(metadata: Mapping[str, Any]) -> str:
    return json.dumps(dict(metadata), ensure_ascii=False, sort_keys=True, default=str)


def parse_file_storage_ref(storage_ref: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(storage_ref)
    except (TypeError, json.JSONDecodeError):
        return None
    return dict(parsed) if isinstance(parsed, Mapping) else None


def is_active_skill_output_file(metadata: Mapping[str, Any] | None) -> bool:
    return bool(
        metadata
        and metadata.get("source_kind") == "skill_output"
        and metadata.get("retention_status") == "active"
        and isinstance(metadata.get("storage_key"), str)
    )
