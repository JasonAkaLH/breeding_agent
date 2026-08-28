from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from src.integrations.agent_skills.public_profile import PublicSkillProfile
from src.storage.agent_payload import CanonicalAgentPayload, canonicalize_agent_payload

from .models import AgentItem, AgentItemKind, AgentItemState, AgentRun


DELEGATED_SKILL_INSTRUCTION_MAX_CODE_POINTS = 20_000
AGENT_MODEL_RESULT_MAX_BYTES = 80_000


class SkillActivationCommitPort(Protocol):
    async def commit_skill_activation(self, item: AgentItem) -> AgentItem: ...


@dataclass(frozen=True, slots=True)
class DelegatedSkillActivation:
    item: AgentItem
    profile_digest: str


@dataclass(frozen=True, slots=True)
class CanonicalSkillActivation:
    binding_mode: str
    capability_id: str
    pinned_bundle_revision: str
    profile_digest: str
    payload_json: str
    payload_sha256: str
    size_bytes: int


def build_canonical_skill_activation(
    *,
    binding_mode: str,
    profile: PublicSkillProfile,
    pinned_bundle_revision: str,
    resolved_bundle_revision: str,
) -> CanonicalSkillActivation:
    mode = str(binding_mode).strip()
    if mode not in {"hint", "delegated"}:
        raise ValueError("agent_skill_activation_binding_mode_invalid")
    revision = str(pinned_bundle_revision).strip()
    if not revision or revision != str(resolved_bundle_revision).strip():
        raise ValueError("agent_skill_pinned_revision_mismatch")
    safe_profile = _safe_profile(profile)
    profile_payload = canonicalize_agent_payload(safe_profile)
    payload = canonicalize_agent_payload(
        {
            "binding_mode": mode,
            "pinned_bundle_revision": revision,
            "profile": safe_profile,
            "profile_digest": profile_payload.sha256,
        }
    )
    return _activation_from_payload(
        binding_mode=mode,
        capability_id=profile.capability_id,
        pinned_bundle_revision=revision,
        profile_digest=profile_payload.sha256,
        payload=payload,
    )


def build_skill_activation_item(
    *,
    run: AgentRun,
    sequence: int,
    activation: CanonicalSkillActivation,
    committed_at: datetime,
) -> AgentItem:
    if sequence != run.next_item_sequence:
        raise ValueError("agent_skill_activation_sequence_mismatch")
    identity = hashlib.sha256(
        (
            f"{run.run_id}\0{activation.capability_id}\0"
            f"{activation.pinned_bundle_revision}"
        ).encode()
    ).hexdigest()[:24]
    return AgentItem(
        item_id=f"agent-item:{run.run_id}:skill-activation:{identity}",
        run_id=run.run_id,
        task_id=run.task_id,
        sequence=sequence,
        kind=AgentItemKind.SKILL_ACTIVATION,
        state=AgentItemState.COMMITTED,
        payload_json=activation.payload_json,
        payload_sha256=activation.payload_sha256,
        created_at=committed_at,
        committed_at=committed_at,
    )


def build_delegated_skill_instruction_result(
    *,
    capability_id: str,
    pinned_bundle_revision: str,
    profile_digest: str,
    instruction_body: str,
) -> dict[str, Any]:
    if not instruction_body.strip() or len(instruction_body) > DELEGATED_SKILL_INSTRUCTION_MAX_CODE_POINTS:
        raise ValueError("delegated_skill_instruction_invalid")
    instruction_sha256 = hashlib.sha256(instruction_body.encode("utf-8")).hexdigest()
    model_view = {
        "schema": "maf.agent.delegated_skill_activation.v1",
        "capability_id": capability_id,
        "pinned_bundle_revision": pinned_bundle_revision,
        "profile_digest": profile_digest,
        "instruction_body": instruction_body,
        "instruction_sha256": instruction_sha256,
    }
    canonical_model_view = canonicalize_agent_payload(model_view)
    result: dict[str, Any] = {
        "schema": "maf.agent.model_result.v1",
        "projection_revision": "delegated-skill-instruction-v1",
        "projection_mode": "inline",
        "model_view": model_view,
        "original_size_bytes": canonical_model_view.size_bytes,
        "projected_size_bytes": 0,
        "raw_sha256": canonical_model_view.sha256,
        "projection_truncated": False,
    }
    for _ in range(3):
        canonical_result = canonicalize_agent_payload(result)
        if result["projected_size_bytes"] == canonical_result.size_bytes:
            break
        result["projected_size_bytes"] = canonical_result.size_bytes
    canonical_result = canonicalize_agent_payload(result)
    if (
        canonical_result.size_bytes > AGENT_MODEL_RESULT_MAX_BYTES
        or result["projected_size_bytes"] != canonical_result.size_bytes
    ):
        raise ValueError("delegated_skill_instruction_invalid")
    return result


def _activation_from_payload(
    *,
    binding_mode: str,
    capability_id: str,
    pinned_bundle_revision: str,
    profile_digest: str,
    payload: CanonicalAgentPayload,
) -> CanonicalSkillActivation:
    return CanonicalSkillActivation(
        binding_mode=binding_mode,
        capability_id=capability_id,
        pinned_bundle_revision=pinned_bundle_revision,
        profile_digest=profile_digest,
        payload_json=payload.json_text,
        payload_sha256=payload.sha256,
        size_bytes=payload.size_bytes,
    )


class DelegatedSkillActivationService:
    """Persist a delegated Skill's public profile without loading executable content."""

    def __init__(
        self,
        commit_port: SkillActivationCommitPort | None,
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
        item, profile_digest = self.build_item(
            run=run,
            profile=profile,
            sequence=sequence,
            pinned_bundle_revision=pinned_bundle_revision,
            resolved_bundle_revision=resolved_bundle_revision,
        )
        if self._commit_port is None:
            raise RuntimeError("agent_skill_activation_commit_port_missing")
        stored = await self._commit_port.commit_skill_activation(item)
        if stored != item:
            raise RuntimeError("agent_skill_activation_commit_drift")
        return DelegatedSkillActivation(item=stored, profile_digest=profile_digest)

    def build_item(
        self,
        *,
        run: AgentRun,
        profile: PublicSkillProfile,
        sequence: int,
        pinned_bundle_revision: str,
        resolved_bundle_revision: str,
    ) -> tuple[AgentItem, str]:
        activation = build_canonical_skill_activation(
            binding_mode="delegated",
            profile=profile,
            pinned_bundle_revision=pinned_bundle_revision,
            resolved_bundle_revision=resolved_bundle_revision,
        )
        item = build_skill_activation_item(
            run=run,
            sequence=sequence,
            activation=activation,
            committed_at=self._now(),
        )
        return item, activation.profile_digest


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
