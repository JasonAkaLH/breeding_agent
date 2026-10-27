from __future__ import annotations

import fcntl
import hashlib
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path


_COMPONENT = re.compile(r"[A-Za-z0-9_-]{1,160}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_REVISION = re.compile(r"^<!-- knowledge_index_revision: (\d+) -->\n")


def _component(value: str) -> str:
    if not isinstance(value, str) or not _COMPONENT.fullmatch(value):
        raise ValueError("invalid_knowledge_identifier")
    return value


def _fsync(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class KnowledgeFileStore:
    """Private source and Markdown files. References never contain host paths."""

    def __init__(self, root_dir: str | Path) -> None:
        self._root_dir = Path(root_dir).absolute()
        self._directory(self._root_dir)

    @property
    def root_dir(self) -> Path:
        return self._root_dir

    @staticmethod
    def build_reference(library_id: str, resource_id: str, sha256: str, extension: str = "md") -> str:
        if not _SHA.fullmatch(sha256) or extension not in {"md", "pdf"}:
            raise ValueError("invalid_knowledge_sha256")
        return f"{_component(library_id)}/{_component(resource_id)}/{sha256}.{extension}"

    body_ref = build_reference

    @staticmethod
    def _directory(path: Path) -> None:
        if path.is_symlink():
            raise ValueError("knowledge_symlink_directory")
        created = not path.exists()
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("knowledge_directory_required")
        os.chmod(path, 0o700, follow_symlinks=False)
        if created:
            _fsync(path)
            _fsync(path.parent.resolve())

    def _body_path(self, body_ref: str, *, create: bool = False) -> Path:
        parts = body_ref.split("/") if isinstance(body_ref, str) else []
        if len(parts) != 3 or not parts[2].endswith((".md", ".pdf")):
            raise ValueError("invalid_knowledge_reference")
        library_id, resource_id, name = parts
        stem, extension = name.rsplit(".", 1)
        expected = self.build_reference(library_id, resource_id, stem, extension)
        if expected != body_ref:
            raise ValueError("invalid_knowledge_reference")
        directory = self._root_dir
        for component in parts[:-1]:
            directory = directory / component
            if create:
                self._directory(directory)
            elif directory.is_symlink() or (
                directory.exists() and not directory.is_dir()
            ):
                raise ValueError("unsafe_knowledge_directory")
        return directory / name

    @staticmethod
    def _read(path: Path) -> bytes:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("knowledge_regular_private_file_required")
            if stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError("knowledge_file_permissions")
            return source.read()

    def _put_body(self, library_id: str, resource_id: str, raw: bytes, extension: str = "md") -> dict:
        # Decode validates UTF-8 without rewriting BOM, newline or Unicode bytes.
        if extension == "md":
            raw.decode("utf-8-sig")
        sha256 = hashlib.sha256(raw).hexdigest()
        body_ref = self.build_reference(library_id, resource_id, sha256, extension)
        path = self._body_path(body_ref, create=True)
        if (path.parent.parent / ".deleted").exists() or (
            path.parent / f".{sha256}.deleted"
        ).exists():
            raise ValueError("knowledge_file_deleted")
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent, prefix=".write-", delete=False
            ) as target:
                temporary = target.name
                os.chmod(temporary, 0o600)
                target.write(raw)
                target.flush()
                os.fsync(target.fileno())
            if path.exists() or path.is_symlink():
                if self._read(path) != raw:
                    raise ValueError("knowledge_existing_file_mismatch")
                os.unlink(temporary)
            else:
                # All managed writes/deletes for this library hold the same
                # filesystem lock; rename is atomic without a hardlink window.
                os.replace(temporary, path)
            temporary = None
            _fsync(path.parent)
        finally:
            if temporary is not None:
                os.unlink(temporary)
        return {"body_ref": body_ref, "sha256": sha256, "size_bytes": len(raw)}

    def put_body(self, library_id: str, resource_id: str, raw: bytes, extension: str = "md") -> dict:
        with self._index_lock(library_id):
            return self._put_body(library_id, resource_id, raw, extension)

    def read_body(self, body_ref: str, sha256: str | None = None) -> bytes:
        path = self._body_path(body_ref)
        if (path.parent.parent / ".deleted").exists() or (
            path.parent / f".{path.stem}.deleted"
        ).exists():
            raise FileNotFoundError("knowledge_file_deleted")
        raw = self._read(path)
        expected = sha256 or path.stem
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("knowledge_body_sha256_mismatch")
        return raw

    @staticmethod
    def _deletion_marker(path: Path) -> None:
        try:
            descriptor = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
        except FileExistsError:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size:
                raise ValueError("unsafe_knowledge_deletion_marker")
        else:
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            _fsync(path.parent)

    def _delete_body(self, body_ref: str, *, marker: bool = True) -> None:
        path = self._body_path(body_ref, create=marker)
        if marker:
            self._deletion_marker(path.parent / f".{path.stem}.deleted")
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("unsafe_knowledge_file")
            path.unlink()
            _fsync(path.parent)
        except FileNotFoundError:
            pass
        # An interrupted upload can leave an unpublished temporary body. The
        # reference marker prevents another writer from recreating this upload.
        for temporary in path.parent.glob(".write-*"):
            info = temporary.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("unsafe_knowledge_temporary_file")
            temporary.unlink()
        _fsync(path.parent)

    def delete_body(self, body_ref: str) -> None:
        self._body_path(body_ref)
        library_id = body_ref.split("/")[0]
        with self._index_lock(library_id) as directory:
            if (directory / ".deleted").exists():
                return
            self._delete_body(body_ref)

    @contextmanager
    def _index_lock(self, library_id: str):
        directory = self._root_dir / _component(library_id)
        self._directory(directory)
        fd = os.open(
            directory / ".index.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink != 1:
                raise ValueError("unsafe_knowledge_lock")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield directory
        finally:
            os.close(fd)

    def publish_index(self, library_id: str, revision: int, text: str) -> bool:
        if revision < 0:
            raise ValueError("invalid_knowledge_revision")
        content = f"<!-- knowledge_index_revision: {revision} -->\n{text}".encode(
            "utf-8"
        )
        with self._index_lock(library_id) as directory:
            if (directory / ".deleted").exists():
                raise ValueError("knowledge_library_files_deleted")
            path = directory / "index.md"
            if path.exists() or path.is_symlink():
                previous = self._read(path).decode("utf-8")
                match = _REVISION.match(previous)
                if match is None:
                    raise ValueError("invalid_knowledge_index")
                if int(match[1]) > revision:
                    return False
                if int(match[1]) == revision:
                    if previous.encode("utf-8") != content:
                        raise ValueError("knowledge_index_revision_conflict")
                    return True
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    dir=directory, prefix=".index-", delete=False
                ) as target:
                    temporary = target.name
                    os.chmod(temporary, 0o600)
                    target.write(content)
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(temporary, path)
                temporary = None
                _fsync(directory)
            finally:
                if temporary is not None:
                    os.unlink(temporary)
        return True

    def read_index(self, library_id: str) -> tuple[int, str]:
        directory = self._root_dir / _component(library_id)
        if directory.is_symlink():
            raise ValueError("unsafe_knowledge_directory")
        if (directory / ".deleted").exists():
            raise FileNotFoundError("knowledge_library_files_deleted")
        text = self._read(directory / "index.md").decode("utf-8")
        match = _REVISION.match(text)
        if match is None:
            raise ValueError("invalid_knowledge_index")
        return int(match[1]), text[match.end() :]

    def delete_library_files(self, library_id: str) -> None:
        """Remove only this library's managed bodies/index; refuse foreign entries."""
        directory = self._root_dir / _component(library_id)
        if not directory.exists():
            return
        with self._index_lock(library_id):
            self._deletion_marker(directory / ".deleted")
            for child in directory.iterdir():
                if child.name in {"index.md", ".index.lock", ".deleted"}:
                    if child.is_symlink() or not child.is_file():
                        raise ValueError("unsafe_knowledge_file")
                    if child.name == "index.md":
                        child.unlink()
                elif (
                    child.is_dir()
                    and not child.is_symlink()
                    and _COMPONENT.fullmatch(child.name)
                ):
                    for body in child.iterdir():
                        if (
                            body.name.startswith((".write-", ".index-"))
                            and body.is_file()
                            and not body.is_symlink()
                        ):
                            body.unlink()
                        elif (
                            re.fullmatch(r"\.[0-9a-f]{64}\.deleted", body.name)
                            and body.is_file()
                            and not body.is_symlink()
                        ):
                            body.unlink()
                        else:
                            self._delete_body(
                                f"{library_id}/{child.name}/{body.name}", marker=False
                            )
                    child.rmdir()
                elif (
                    child.name.startswith(".index-")
                    and child.is_file()
                    and not child.is_symlink()
                ):
                    child.unlink()
                else:
                    raise ValueError("unexpected_knowledge_file")
            _fsync(directory)
