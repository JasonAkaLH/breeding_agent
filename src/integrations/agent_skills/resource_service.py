from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contract import SkillContract


@dataclass(slots=True, frozen=True)
class SkillResourceReadResult:
    ok: bool
    capability_id: str
    skill_name: str
    audience: str
    resource_id: str = ""
    path: str = ""
    content: str = ""
    content_bytes: int = 0
    truncated: bool = False
    redaction_count: int = 0
    denied_reason: str = ""

    @property
    def status(self) -> str:
        return "ok" if self.ok else self.denied_reason or "denied"

    def audit_payload(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "skill_name": self.skill_name,
            "audience": self.audience,
            "resource_id": self.resource_id,
            "path": self.path,
            "ok": self.ok,
            "denied_reason": self.denied_reason,
            "truncated": self.truncated,
            "redaction_count": self.redaction_count,
            "content_bytes": self.content_bytes,
        }


class SkillResourceService:
    HARD_DENY_DIRS = {".git", "scripts", "runtime", "schemas", "native", "__pycache__"}
    HARD_DENY_NAMES = {"config.yaml", "config.yml", ".env", ".env.local"}
    SECRET_NAME_RE = re.compile(r"(?i)(secret|token|credential|password|api[_-]?key|authorization)")
    PROMPT_AUDIENCES = {"main_agent", "slot_question", "schema_selector"}
    TEXT_SUFFIXES = {".md", ".txt", ".csv", ".tsv", ".json", ".yaml", ".yml", ".html", ".xml", ".r", ".py"}

    def __init__(self, *, audit_sink: Any | None = None, default_max_bytes: int = 65536) -> None:
        self._audit_sink = audit_sink
        self._default_max_bytes = default_max_bytes

    def read(
        self,
        contract: SkillContract,
        *,
        skill_name: str,
        audience: str,
        resource_id: str | None = None,
        path: str | None = None,
        max_bytes: int | None = None,
    ) -> SkillResourceReadResult:
        if resource_id:
            ref = contract.resources.get(resource_id)
            if ref is None:
                result = self._denied(contract, skill_name=skill_name, audience=audience, resource_id=resource_id, path=path or "", reason="not_found")
                self._record(result)
                return result
            if audience not in ref.audience and audience not in contract.resource_policy.default_audience:
                result = self._denied(contract, skill_name=skill_name, audience=audience, resource_id=resource_id, path=ref.path, reason="audience_denied")
                self._record(result)
                return result
            target_path = ref.path
        elif path:
            target_path = path
        else:
            result = self._denied(contract, skill_name=skill_name, audience=audience, reason="not_found")
            self._record(result)
            return result

        safe = self._resolve_safe_path(contract.root_dir, target_path)
        if isinstance(safe, str):
            result = self._denied(contract, skill_name=skill_name, audience=audience, resource_id=resource_id or "", path=target_path, reason=safe)
            self._record(result)
            return result
        denied = self._deny_reason(safe, contract.root_dir, audience=audience)
        if denied:
            result = self._denied(contract, skill_name=skill_name, audience=audience, resource_id=resource_id or "", path=target_path, reason=denied)
            self._record(result)
            return result
        if not safe.exists() or not safe.is_file():
            result = self._denied(contract, skill_name=skill_name, audience=audience, resource_id=resource_id or "", path=target_path, reason="not_found")
            self._record(result)
            return result
        if self._looks_binary(safe):
            result = self._denied(contract, skill_name=skill_name, audience=audience, resource_id=resource_id or "", path=target_path, reason="binary_unsupported")
            self._record(result)
            return result
        limit = max(1, int(max_bytes or contract.resource_policy.max_bytes or self._default_max_bytes))
        data = safe.read_bytes()
        truncated = len(data) > limit
        raw = data[:limit]
        text = raw.decode("utf-8", errors="replace")
        text, redactions = self._redact(text)
        rel = safe.relative_to(contract.root_dir.resolve()).as_posix()
        result = SkillResourceReadResult(
            ok=True,
            capability_id=contract.capability.id,
            skill_name=skill_name,
            audience=audience,
            resource_id=resource_id or "",
            path=rel,
            content=text,
            content_bytes=len(data),
            truncated=truncated,
            redaction_count=redactions,
        )
        self._record(result)
        return result

    def _resolve_safe_path(self, root: Path, relative_path: str) -> Path | str:
        raw = Path(relative_path)
        if not str(relative_path).strip() or raw.is_absolute() or any(part == ".." for part in raw.parts):
            return "path_denied"
        try:
            resolved_root = root.resolve()
            resolved = (resolved_root / raw).resolve(strict=False)
            resolved.relative_to(resolved_root)
        except (OSError, ValueError):
            return "path_denied"
        return resolved

    def _deny_reason(self, path: Path, root: Path, *, audience: str) -> str:
        try:
            rel = path.resolve(strict=False).relative_to(root.resolve())
        except (OSError, ValueError):
            return "path_denied"
        parts = tuple(part.lower() for part in rel.parts)
        name = path.name.lower()
        if any(part in self.HARD_DENY_DIRS for part in parts) or name in self.HARD_DENY_NAMES:
            if audience == "runtime" and not any(part in {".git", "__pycache__"} for part in parts) and not self.SECRET_NAME_RE.search(name) and name not in {".env", ".env.local"}:
                # Runtime can read implementation files, but never hard secrets or git internals.
                return ""
            return "internal_path_denied"
        if self.SECRET_NAME_RE.search("/".join(parts)):
            return "secret_path_denied"
        if audience in self.PROMPT_AUDIENCES and path.suffix.lower() not in self.TEXT_SUFFIXES:
            return "binary_unsupported"
        return ""

    def _looks_binary(self, path: Path) -> bool:
        try:
            chunk = path.read_bytes()[:4096]
        except OSError:
            return False
        if b"\x00" in chunk:
            return True
        if path.suffix.lower() not in self.TEXT_SUFFIXES:
            return True
        return False

    def _redact(self, text: str) -> tuple[str, int]:
        patterns = [
            r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+",
            r"(?i)(api[_-]?key\s*[:=]\s*)[^\s,;]+",
            r"(?i)(token\s*[:=]\s*)[^\s,;]+",
            r"(?i)(password\s*[:=]\s*)[^\s,;]+",
            r"(?i)(base_url\s*[:=]\s*)https?://[^\s,;]+",
        ]
        redactions = 0
        redacted = text
        for pattern in patterns:
            redacted, count = re.subn(pattern, r"\1[REDACTED]", redacted)
            redactions += count
        return redacted, redactions

    def _denied(self, contract: SkillContract, *, skill_name: str, audience: str, reason: str, resource_id: str = "", path: str = "") -> SkillResourceReadResult:
        return SkillResourceReadResult(
            ok=False,
            capability_id=contract.capability.id,
            skill_name=skill_name,
            audience=audience,
            resource_id=resource_id,
            path=path,
            denied_reason=reason,
        )

    def _record(self, result: SkillResourceReadResult) -> None:
        sink = self._audit_sink
        if sink is None:
            return
        payload = result.audit_payload()
        try:
            if hasattr(sink, "record_sync"):
                sink.record_sync("skill.resource_read", payload)
                return
            maybe = sink.record("skill.resource_read", payload)
            if asyncio.iscoroutine(maybe):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    asyncio.run(maybe)
                else:
                    loop.create_task(maybe)
        except Exception:
            return
