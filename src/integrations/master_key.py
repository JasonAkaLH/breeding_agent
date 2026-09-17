from __future__ import annotations

import base64
import binascii
import errno
import os
import stat
from enum import Enum
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


_APPLICATION_SALT = b"maf/master-key-domain-derivation/v1"
_MASTER_KEY_LENGTH = 32
_ENCODED_KEY_LENGTH = 44
_MAX_FILE_SIZE = 45
_ALLOWED_FILE_MODES = frozenset({0o400, 0o600})


class MasterKeyError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class MasterKeyDomain(Enum):
    MCP_CREDENTIAL = b"maf/mcp-credential-aes-gcm/v1"
    MCP_RECOVERY = b"maf/mcp-recovery-aes-gcm/v1"
    AUTH_TOKEN = b"maf/auth-token-hmac-sha256/v1"
    MCP_AUDIT_REFERENCE = b"maf/mcp-audit-reference-hmac/v1"
    KEY_VALIDATION = b"maf/key-validation-aes-gcm/v1"


class _DerivedDomainKey:
    __slots__ = ("__domain", "__key_bytes")

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("derived domain keys are created only by MasterKeyDeriver")

    @classmethod
    def _create(
        cls,
        domain: MasterKeyDomain,
        key_bytes: bytes,
    ) -> _DerivedDomainKey:
        instance = object.__new__(cls)
        instance.__domain = domain
        instance.__key_bytes = key_bytes
        return instance

    def _consume_for(self, expected_domain: MasterKeyDomain) -> bytes:
        if (
            not isinstance(expected_domain, MasterKeyDomain)
            or self.__domain is not expected_domain
        ):
            raise MasterKeyError("maf_key_domain_invalid")
        return self.__key_bytes

    def __repr__(self) -> str:
        return "<_DerivedDomainKey redacted>"

    def __reduce__(self) -> object:
        raise TypeError("derived domain keys cannot be serialized")

    def __reduce_ex__(self, _protocol: int) -> object:
        raise TypeError("derived domain keys cannot be serialized")


class MasterKeyDeriver:
    __slots__ = ("__master_key",)

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("master key derivers are created by from_file or from_bytes")

    @classmethod
    def from_file(cls, path: str | os.PathLike[str]) -> MasterKeyDeriver:
        return cls._create(_load_master_key(path))

    @classmethod
    def from_bytes(cls, master_key: bytes) -> MasterKeyDeriver:
        if not isinstance(master_key, bytes) or len(master_key) != _MASTER_KEY_LENGTH:
            raise MasterKeyError("maf_master_key_invalid_length")
        return cls._create(master_key)

    @classmethod
    def _create(cls, master_key: bytes) -> MasterKeyDeriver:
        instance = object.__new__(cls)
        instance.__master_key = master_key
        return instance

    def derive(self, domain: MasterKeyDomain) -> _DerivedDomainKey:
        if not isinstance(domain, MasterKeyDomain):
            raise MasterKeyError("maf_key_domain_invalid")
        key_bytes = HKDF(
            algorithm=hashes.SHA256(),
            length=_MASTER_KEY_LENGTH,
            salt=_APPLICATION_SALT,
            info=domain.value,
        ).derive(self.__master_key)
        return _DerivedDomainKey._create(domain, key_bytes)

    def __repr__(self) -> str:
        return "<MasterKeyDeriver redacted>"

    def __reduce__(self) -> object:
        raise TypeError("master key derivers cannot be serialized")

    def __reduce_ex__(self, _protocol: int) -> object:
        raise TypeError("master key derivers cannot be serialized")


def _load_master_key(path: str | os.PathLike[str]) -> bytes:
    try:
        raw_path = os.fspath(path)
    except TypeError as exc:
        raise MasterKeyError("maf_master_key_file_missing") from exc
    if not isinstance(raw_path, str) or not raw_path:
        raise MasterKeyError("maf_master_key_file_missing")

    key_path = Path(os.path.abspath(raw_path))
    path_parts = key_path.parts
    if not path_parts or key_path.name in {"", ".", ".."}:
        raise MasterKeyError("maf_master_key_file_missing")

    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )

    directory_fd = _open_start_directory(key_path, directory_flags)
    try:
        parent_parts = path_parts[1:-1] if key_path.is_absolute() else path_parts[:-1]
        for component in parent_parts:
            if component == ".":
                continue
            next_fd = _open_directory_component(
                directory_fd, component, directory_flags
            )
            os.close(directory_fd)
            directory_fd = next_fd

        basename = path_parts[-1]
        descriptor = _open_final_file(directory_fd, basename, file_flags)
        try:
            payload = _read_trusted_payload(descriptor, directory_fd, basename)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory_fd)

    return _decode_master_key(payload)


def _open_start_directory(path: Path, flags: int) -> int:
    start = os.sep if path.is_absolute() else "."
    try:
        return os.open(start, flags)
    except OSError as exc:
        raise MasterKeyError("maf_master_key_file_unavailable") from exc


def _open_directory_component(directory_fd: int, component: str, flags: int) -> int:
    try:
        return os.open(component, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise MasterKeyError(_path_open_error_code(exc, final=False)) from exc


def _open_final_file(directory_fd: int, basename: str, flags: int) -> int:
    try:
        return os.open(basename, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise MasterKeyError(_path_open_error_code(exc, final=True)) from exc


def _path_open_error_code(exc: OSError, *, final: bool) -> str:
    if exc.errno == errno.ENOENT:
        return "maf_master_key_file_missing"
    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
        return "maf_master_key_file_invalid_type"
    if final and exc.errno in {errno.EACCES, errno.EPERM}:
        return "maf_master_key_file_invalid_permissions"
    if final and exc.errno == errno.ENXIO:
        return "maf_master_key_file_invalid_type"
    return "maf_master_key_file_unavailable"


def _read_trusted_payload(descriptor: int, parent_fd: int, basename: str) -> bytes:
    try:
        before = os.fstat(descriptor)
    except OSError as exc:
        raise MasterKeyError("maf_master_key_file_unavailable") from exc
    _validate_file_stat(before)

    expected_size = before.st_size
    payload_parts: list[bytes] = []
    remaining = expected_size
    try:
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            payload_parts.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        final_path_stat = os.stat(basename, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise MasterKeyError("maf_master_key_file_unavailable") from exc

    payload = b"".join(payload_parts)
    if remaining or _file_identity(before) != _file_identity(after):
        raise MasterKeyError("maf_master_key_file_unavailable")
    _validate_file_stat(after)
    if (final_path_stat.st_dev, final_path_stat.st_ino) != (after.st_dev, after.st_ino):
        raise MasterKeyError("maf_master_key_file_unavailable")
    if not stat.S_ISREG(final_path_stat.st_mode):
        raise MasterKeyError("maf_master_key_file_invalid_type")
    return payload


def _validate_file_stat(file_stat: os.stat_result) -> None:
    if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
        raise MasterKeyError("maf_master_key_file_invalid_type")
    if stat.S_IMODE(file_stat.st_mode) not in _ALLOWED_FILE_MODES:
        raise MasterKeyError("maf_master_key_file_invalid_permissions")
    if file_stat.st_size > _MAX_FILE_SIZE:
        raise MasterKeyError("maf_master_key_file_invalid_format")


def _file_identity(file_stat: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


def _decode_master_key(payload: bytes) -> bytes:
    if len(payload) == _MAX_FILE_SIZE and payload.endswith(b"\n"):
        encoded_key = payload[:-1]
    elif len(payload) == _ENCODED_KEY_LENGTH:
        encoded_key = payload
    else:
        raise MasterKeyError("maf_master_key_file_invalid_format")

    try:
        master_key = base64.b64decode(encoded_key, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MasterKeyError("maf_master_key_file_invalid_format") from exc
    if base64.b64encode(master_key) != encoded_key:
        raise MasterKeyError("maf_master_key_file_invalid_format")
    if len(master_key) != _MASTER_KEY_LENGTH:
        raise MasterKeyError("maf_master_key_invalid_length")
    return master_key


__all__ = ["MasterKeyDeriver", "MasterKeyDomain", "MasterKeyError"]
