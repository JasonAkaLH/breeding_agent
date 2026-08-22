from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from src.integrations.agent_skills.public_profile import PublicSkillProfile
from src.storage.agent_payload import canonicalize_agent_payload

from .models import AgentItem, AgentItemKind, AgentItemState, AgentRun


class SkillActivationCommitPort(Protocol):
    async def commit_skill_activation(self, item: AgentItem) -> AgentItem: ...


@dataclass(frozen=True, slots=True)
class DelegatedSkillActivation:
    item: AgentItem
    profile_digest: str


class DelegatedSkillActivationService:
    """Persist a delegated Skill's public profile without loading executable content."""

    def __init__(
        self,
        commit_port: SkillActivationCommitPort,
        *,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._commit_port = commit_port
        self._now = now_fn or (lambda: datetime.now(timezone.utc))

    async def activate(
        self,
        *,
        run: AgentRun,
        profile: PublicSkillProfile,
        sequence: int,
        pinned_bundle_revision: str,
        resolved_bundle_revision: str,
    ) -> DelegatedSkillActivation:
        if (
            not pinned_bundle_revision.strip()
            or pinned_bundle_revision != resolved_bundle_revision
        ):
            raise ValueError("agent_skill_pinned_revision_mismatch")
        if sequence != run.next_item_sequence:
            raise ValueError("agent_skill_activation_sequence_mismatch")
        safe_profile = _safe_profile(profile)
        profile_payload = canonicalize_agent_payload(safe_profile)
        payload = canonicalize_agent_payload(
            {
                "pinned_bundle_revision": pinned_bundle_revision,
                "profile": safe_profile,
                "profile_digest": profile_payload.sha256,
            }
        )
        identity = hashlib.sha256(
            f"{run.run_id}\0{profile.capability_id}\0{pinned_bundle_revision}".encode()
        ).hexdigest()[:24]
        now = self._now()
        item = AgentItem(
            item_id=f"agent-item:{run.run_id}:skill-activation:{identity}",
            run_id=run.run_id,
            task_id=run.task_id,
            sequence=sequence,
            kind=AgentItemKind.SKILL_ACTIVATION,
            state=AgentItemState.COMMITTED,
            payload_json=payload.json_text,
            payload_sha256=payload.sha256,
            created_at=now,
            committed_at=now,
        )
        stored = await self._commit_port.commit_skill_activation(item)
        if stored != item:
            raise RuntimeError("agent_skill_activation_commit_drift")
        return DelegatedSkillActivation(item=stored, profile_digest=profile_payload.sha256)


def _safe_profile(profile: PublicSkillProfile) -> dict[str, Any]:
    value = profile.to_dict()
    value["resource_index"] = [
        {
            key: resource[key]
            for key in ("resource_id", "title", "description", "audience")
            if key in resource
        }
        for resource in value.get("resource_index", ())
        if isinstance(resource, dict)
    ]
    return value
