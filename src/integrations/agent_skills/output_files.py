from __future__ import annotations

import mimetypes
import posixpath
import re
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from src.storage.artifact_files import sanitize_download_filename

from .manifest import SkillManifest
from .value_utils import string_tuple

_ALLOWED_SOURCE_EXTENSIONS = {
    ".txt",
    ".md",
    ".json",
    ".csv",
    ".tsv",
    ".html",
    ".pdf",
    ".xlsx",
    ".png",
    ".jpg",
    ".jpeg",
}
_DEFAULT_MIME_BY_EXTENSION = {
    ".md": "text/markdown",
    ".tsv": "text/tab-separated-values",
}
_ALLOWED_MIME_BY_EXTENSION = {
    ".txt": {"text/plain"},
    ".md": {"text/markdown", "text/plain"},
    ".json": {"application/json", "text/json"},
    ".csv": {"text/csv"},
    ".tsv": {"text/tab-separated-values", "text/tsv"},
    ".html": {"text/html"},
    ".pdf": {"application/pdf"},
    ".xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    ".png": {"image/png"},
    ".jpg": {"image/jpeg"},
    ".jpeg": {"image/jpeg"},
}


@dataclass(slots=True, frozen=True)
class SkillOutputFileRejection:
    path: str
    reason: str
    message: str


@dataclass(slots=True, frozen=True)
class CollectedSkillOutputFile:
    source_path: Path
    relative_path: str
    archive_name: str
    filename: str
    mime_type: str
    size_bytes: int
    label: str | None = None
    summary: str | None = None


@dataclass(slots=True, frozen=True)
class SkillOutputFileCollection:
    files: tuple[CollectedSkillOutputFile, ...]
    rejections: tuple[SkillOutputFileRejection, ...]


@dataclass(slots=True, frozen=True)
class CreatedSkillOutputZip:
    path: Path
    filename: str
    mime_type: str
    source_file_count: int


def collect_skill_output_files(
    output: Mapping[str, Any],
    outputs_dir: str | Path,
    *,
    manifest: SkillManifest | None = None,
) -> SkillOutputFileCollection:
    declared = output.get("output_files")
    if declared is None:
        return SkillOutputFileCollection(files=(), rejections=())
    if not isinstance(declared, list | tuple):
        return SkillOutputFileCollection(
            files=(),
            rejections=(SkillOutputFileRejection(path="", reason="invalid_output_files", message="output_files must be a list"),),
        )

    root = Path(outputs_dir).resolve()
    manifest_extensions, manifest_mime_types, manifest_mimes_by_extension = _manifest_file_constraints(manifest)
    files: list[CollectedSkillOutputFile] = []
    rejections: list[SkillOutputFileRejection] = []
    seen_archive_names: set[str] = set()

    for index, item in enumerate(declared):
        if not isinstance(item, Mapping):
            rejections.append(SkillOutputFileRejection(path="", reason="invalid_output_file", message="output file entry must be an object"))
            continue
        raw_path = str(item.get("path") or "").strip()
        if not raw_path:
            rejections.append(SkillOutputFileRejection(path="", reason="missing_path", message="output file path is required"))
            continue
        normalized = _normalize_declared_output_path(raw_path)
        if normalized is None:
            rejections.append(SkillOutputFileRejection(path=raw_path, reason="unsafe_path", message="output file path is unsafe"))
            continue
        source = (root / normalized).resolve()
        if not source.is_relative_to(root):
            rejections.append(SkillOutputFileRejection(path=raw_path, reason="path_escape", message="output file escapes output dir"))
            continue
        if _has_symlink_component(root, normalized):
            rejections.append(SkillOutputFileRejection(path=raw_path, reason="symlink_not_allowed", message="output file path uses symlink"))
            continue
        if not source.exists() or not source.is_file():
            rejections.append(SkillOutputFileRejection(path=raw_path, reason="file_not_found", message="output file does not exist"))
            continue
        if source.stat().st_nlink > 1:
            rejections.append(SkillOutputFileRejection(path=raw_path, reason="hardlink_not_allowed", message="output file hardlink is not allowed"))
            continue
        base_extension = _matching_extension(source.name, _ALLOWED_SOURCE_EXTENSIONS)
        manifest_extension = _matching_extension(source.name, manifest_extensions) if manifest_extensions is not None else None
        extension = base_extension or manifest_extension
        if extension is None:
            rejections.append(SkillOutputFileRejection(path=raw_path, reason="extension_not_allowed", message="output file extension is not allowed"))
            continue
        if manifest_extensions is not None and manifest_extension is None:
            rejections.append(SkillOutputFileRejection(path=raw_path, reason="manifest_extension_not_allowed", message="output file extension is not allowed by skill manifest"))
            continue
        declared_mime = _optional_string(item.get("mime_type"))
        declared_mime = declared_mime.lower() if declared_mime is not None else None
        allowed_mimes = _allowed_mimes_for_extension(extension, manifest_mimes_by_extension)
        if declared_mime is not None:
            if declared_mime not in allowed_mimes:
                rejections.append(SkillOutputFileRejection(path=raw_path, reason="mime_mismatch", message="output file MIME does not match extension"))
                continue
            effective_mime = declared_mime
        else:
            guessed_mime = _guess_mime_type(source.name)
            if guessed_mime not in allowed_mimes:
                rejections.append(SkillOutputFileRejection(path=raw_path, reason="mime_not_allowed", message="output file MIME is not allowed"))
                continue
            effective_mime = guessed_mime
        if manifest_mime_types is not None and effective_mime not in manifest_mime_types:
            rejections.append(SkillOutputFileRejection(path=raw_path, reason="manifest_mime_not_allowed", message="output file MIME is not allowed by skill manifest"))
            continue
        archive_name = _safe_archive_name(normalized)
        if archive_name in seen_archive_names:
            rejections.append(SkillOutputFileRejection(path=raw_path, reason="duplicate_archive_entry", message="zip entry name would be duplicated"))
            continue
        seen_archive_names.add(archive_name)
        filename = sanitize_download_filename(_optional_string(item.get("filename")) or source.name)
        files.append(
            CollectedSkillOutputFile(
                source_path=source,
                relative_path=f"outputs/{normalized.as_posix()}",
                archive_name=archive_name,
                filename=filename,
                mime_type=effective_mime,
                size_bytes=source.stat().st_size,
                label=_optional_string(item.get("label")),
                summary=_optional_string(item.get("summary")),
            )
        )
    return SkillOutputFileCollection(files=tuple(files), rejections=tuple(rejections))


def create_zip_from_collected_files(files: tuple[CollectedSkillOutputFile, ...] | list[CollectedSkillOutputFile], zip_path: str | Path) -> CreatedSkillOutputZip:
    path = Path(zip_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file in files:
            archive.write(file.source_path, arcname=file.archive_name)
    return CreatedSkillOutputZip(path=path, filename=path.name, mime_type="application/zip", source_file_count=len(files))


def _normalize_declared_output_path(raw_path: str) -> PurePosixPath | None:
    if "\x00" in raw_path or re.match(r"^[A-Za-z]:", raw_path) or raw_path.startswith(("/", "\\", "//", "\\\\")):
        return None
    if "\\" in raw_path:
        return None
    pure = PurePosixPath(raw_path)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        return None
    if not pure.parts or pure.parts[0] != "outputs" or len(pure.parts) == 1:
        return None
    return PurePosixPath(*pure.parts[1:])


def _safe_archive_name(relative_under_outputs: PurePosixPath) -> str:
    normalized = posixpath.normpath(relative_under_outputs.as_posix())
    if normalized in {"", "."} or normalized.startswith("../") or normalized == ".." or normalized.startswith("/"):
        raise ValueError("Unsafe archive entry name")
    return normalized


def _has_symlink_component(root: Path, relative_under_outputs: PurePosixPath) -> bool:
    current = root
    for part in relative_under_outputs.parts:
        current = current / part
        if current.exists() and current.is_symlink():
            return True
    return False


def _guess_mime_type(filename: str) -> str:
    extension = _matching_extension(filename, _ALLOWED_MIME_BY_EXTENSION.keys())
    if extension in _DEFAULT_MIME_BY_EXTENSION:
        return _DEFAULT_MIME_BY_EXTENSION[extension]
    allowed_mimes = _ALLOWED_MIME_BY_EXTENSION.get(extension or "", set())
    if len(allowed_mimes) == 1:
        return next(iter(sorted(allowed_mimes)))
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def _matching_extension(filename: str, allowed_extensions: Iterable[str] | None) -> str | None:
    if not allowed_extensions:
        return None
    lower = filename.lower()
    matches = [extension for extension in allowed_extensions if lower.endswith(extension)]
    if not matches:
        return None
    return max(matches, key=len)


def _allowed_mimes_for_extension(extension: str, manifest_mimes_by_extension: Mapping[str, set[str]]) -> set[str]:
    base_mimes = _ALLOWED_MIME_BY_EXTENSION.get(extension)
    if base_mimes:
        return set(base_mimes)
    return set(manifest_mimes_by_extension.get(extension, set()))


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _manifest_file_constraints(manifest: SkillManifest | None) -> tuple[set[str] | None, set[str] | None, dict[str, set[str]]]:
    if manifest is None:
        return None, None, {}
    extensions: set[str] = set()
    mime_types: set[str] = set()
    mimes_by_extension: dict[str, set[str]] = {}

    legacy_files = manifest.outputs.schema.get("files")
    if isinstance(legacy_files, list | tuple):
        _collect_file_constraints(legacy_files, extensions=extensions, mime_types=mime_types, mimes_by_extension=mimes_by_extension)

    contract = getattr(manifest, "contract", None)
    outputs = getattr(contract, "outputs", {}) if contract is not None else {}
    if isinstance(outputs, Mapping):
        for output in outputs.values():
            artifacts = getattr(output, "artifacts", ())
            if isinstance(artifacts, list | tuple):
                _collect_file_constraints(artifacts, extensions=extensions, mime_types=mime_types, mimes_by_extension=mimes_by_extension)

    return (extensions or None), (mime_types or None), mimes_by_extension


def _collect_file_constraints(
    items: list | tuple,
    *,
    extensions: set[str],
    mime_types: set[str],
    mimes_by_extension: dict[str, set[str]],
) -> None:
    for item in items:
        if not isinstance(item, Mapping):
            continue
        item_extensions = {_normalize_extension(ext) for ext in string_tuple(item.get("extensions") or item.get("extension"))}
        item_mime_types = {mime.lower() for mime in string_tuple(item.get("mime_types") or item.get("mime_type"))}
        extensions.update(item_extensions)
        mime_types.update(item_mime_types)
        for extension in item_extensions:
            if item_mime_types:
                mimes_by_extension.setdefault(extension, set()).update(item_mime_types)


def _normalize_extension(value: str) -> str:
    normalized = value.lower()
    return normalized if normalized.startswith(".") else f".{normalized}"
