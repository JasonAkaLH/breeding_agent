from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

from .models import (
    AgentCallOutcomeStatus,
    AgentItem,
    AgentItemKind,
    AgentModelBinding,
    AgentRun,
)


class AgentResumeKind(StrEnum):
    SKILL_INPUT = "skill_input"
    MCP_APPROVAL = "mcp_approval"
    MCP_ELICITATION = "mcp_elicitation"
    MCP_REMOTE_TASK = "mcp_remote_task"

    @property
    def waiting_status(self) -> AgentCallOutcomeStatus:
        if self is AgentResumeKind.MCP_REMOTE_TASK:
            return AgentCallOutcomeStatus.WAITING_FOR_DEPENDENCY
        return AgentCallOutcomeStatus.WAITING_FOR_INPUT


@dataclass(frozen=True, slots=True)
class AgentContinuationLocator:
    run_id: str
    sample_item_id: str
    call_item_id: str
    provider_call_id: str
    capability_id: str
    task_id: str
    node_id: str
    owner_scope: str
    conversation_id: str
    resume_kind: AgentResumeKind
    authority_digest: str
    pinned_bundle_revision: str | None
    model_binding: AgentModelBinding

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "authority_digest": self.authority_digest,
            "call_item_id": self.call_item_id,
            "capability_id": self.capability_id,
            "conversation_id": self.conversation_id,
            "model_binding": self.model_binding.to_safe_dict(),
            "node_id": self.node_id,
            "owner_scope": self.owner_scope,
            "pinned_bundle_revision": self.pinned_bundle_revision,
            "provider_call_id": self.provider_call_id,
            "resume_kind": self.resume_kind.value,
            "run_id": self.run_id,
            "sample_item_id": self.sample_item_id,
            "task_id": self.task_id,
        }

    @property
    def digest(self) -> str:
        encoded = json.dumps(
            self.to_safe_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


class AgentContinuationLocatorService:
    def build(
        self,
        *,
        run: AgentRun,
        call_item: AgentItem,
        owner_scope: str,
        resume_kind: AgentResumeKind,
        authority_digest: str,
        pinned_bundle_revision: str | None,
    ) -> AgentContinuationLocator:
        if (
            call_item.kind is not AgentItemKind.TOOL_CALL
            or call_item.run_id != run.run_id
            or call_item.task_id != run.task_id
            or not call_item.parent_item_id
            or call_item.parent_item_id != run.active_sample_item_id
            or not owner_scope.strip()
            or not _is_digest(authority_digest)
            or (
                pinned_bundle_revision is not None
                and not pinned_bundle_revision.strip()
            )
        ):
            raise ValueError("agent_continuation_locator_identity_invalid")
        payload = _payload(call_item)
        node_id = str(payload.get("node_id") or "")
        provider_call_id = str(payload.get("call_id") or "")
        capability_id = str(payload.get("capability_id") or "")
        if not node_id or not provider_call_id or not capability_id:
            raise ValueError("agent_continuation_locator_node_missing")
        return AgentContinuationLocator(
            run_id=run.run_id,
            sample_item_id=call_item.parent_item_id,
            call_item_id=call_item.item_id,
            provider_call_id=provider_call_id,
            capability_id=capability_id,
            task_id=run.task_id,
            node_id=node_id,
            owner_scope=owner_scope,
            conversation_id=run.conversation_id,
            resume_kind=resume_kind,
            authority_digest=authority_digest,
            pinned_bundle_revision=pinned_bundle_revision,
            model_binding=run.binding,
        )

    def resolve_unique(
        self,
        locators: tuple[AgentContinuationLocator, ...],
        *,
        owner_scope: str,
        conversation_id: str,
        task_id: str,
        call_item_id: str | None = None,
    ) -> AgentContinuationLocator:
        matches = tuple(
            locator
            for locator in locators
            if locator.owner_scope == owner_scope
            and locator.conversation_id == conversation_id
            and locator.task_id == task_id
            and (call_item_id is None or locator.call_item_id == call_item_id)
        )
        if len(matches) != 1:
            raise ValueError("agent_continuation_locator_ambiguous")
        return matches[0]

    @staticmethod
    def remaining_waiting_calls(
        run: AgentRun,
        *,
        completed_call_item_id: str,
    ) -> tuple[str, ...]:
        if completed_call_item_id not in run.waiting_call_item_ids:
            raise ValueError("agent_continuation_call_not_waiting")
        return tuple(
            call_id
            for call_id in run.waiting_call_item_ids
            if call_id != completed_call_item_id
        )


def _payload(item: AgentItem) -> Mapping[str, Any]:
    value = json.loads(item.payload_json)
    if not isinstance(value, Mapping):
        raise ValueError("agent_continuation_call_payload_invalid")
    return value


def _is_digest(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
