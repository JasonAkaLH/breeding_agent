from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Mapping


@dataclass(frozen=True, slots=True)
class AuthGenerationSnapshot:
    username: str
    auth_generation: int
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AuthGenerationCheck:
    known: bool
    current: bool
    current_generation: int | None


class AuthGenerationCache:
    """Process-local username -> auth generation cache.

    The cache intentionally stores only username and generation metadata. It must
    never contain raw bearer tokens, token hashes, or Authorization headers.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._generations: dict[str, AuthGenerationSnapshot] = {}

    def apply(self, username: str, auth_generation: int, *, updated_at: datetime | None = None) -> AuthGenerationSnapshot:
        normalized = _normalize_username(username)
        generation = _normalize_generation(auth_generation)
        with self._lock:
            current = self._generations.get(normalized)
            if current is not None and current.auth_generation > generation:
                return current
            snapshot = AuthGenerationSnapshot(normalized, generation, updated_at)
            self._generations[normalized] = snapshot
            return snapshot

    def reconcile(self, snapshots: Mapping[str, int] | list[AuthGenerationSnapshot] | tuple[AuthGenerationSnapshot, ...]) -> None:
        replacement: dict[str, AuthGenerationSnapshot] = {}
        if isinstance(snapshots, Mapping):
            for username, generation in snapshots.items():
                normalized = _normalize_username(username)
                replacement[normalized] = AuthGenerationSnapshot(normalized, _normalize_generation(generation))
        else:
            for snapshot in snapshots:
                normalized = _normalize_username(snapshot.username)
                replacement[normalized] = AuthGenerationSnapshot(
                    normalized,
                    _normalize_generation(snapshot.auth_generation),
                    snapshot.updated_at,
                )
        with self._lock:
            self._generations = replacement

    def get(self, username: str) -> AuthGenerationSnapshot | None:
        with self._lock:
            return self._generations.get(_normalize_username(username))

    def is_current(self, username: str, auth_generation: int) -> AuthGenerationCheck:
        expected = _normalize_generation(auth_generation)
        snapshot = self.get(username)
        if snapshot is None:
            return AuthGenerationCheck(known=False, current=False, current_generation=None)
        return AuthGenerationCheck(
            known=True,
            current=snapshot.auth_generation == expected,
            current_generation=snapshot.auth_generation,
        )

    def public_snapshot(self) -> dict[str, int]:
        with self._lock:
            return {username: snapshot.auth_generation for username, snapshot in sorted(self._generations.items())}

    def __repr__(self) -> str:
        return f"AuthGenerationCache(users={len(self.public_snapshot())})"


def _normalize_username(username: str) -> str:
    normalized = str(username or "").strip()
    if not normalized:
        raise ValueError("username is required")
    return normalized


def _normalize_generation(auth_generation: int) -> int:
    generation = int(auth_generation)
    if generation < 0:
        raise ValueError("auth_generation must be non-negative")
    return generation
