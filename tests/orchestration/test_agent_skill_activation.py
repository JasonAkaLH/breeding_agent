from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from src.integrations.agent_skills.public_profile import PublicSkillProfile
from src.orchestration.agent_loop.models import (
    AgentItem,
    AgentItemKind,
    AgentModelBinding,
    AgentRun,
    AgentRunStatus,
)
from src.orchestration.agent_loop.skill_activation import (
    DelegatedSkillActivationService,
    build_canonical_skill_activation,
    build_skill_activation_item,
)
from src.storage.agent_payload import AGENT_PAYLOAD_MAX_BYTES, AgentPayloadError


class _RecordingActivationPort:
    def __init__(self) -> None:
        self.items: list[AgentItem] = []

    async def commit_skill_activation(self, item: AgentItem) -> AgentItem:
        self.items.append(item)
        return item


class DelegatedSkillActivationServiceTest(unittest.IsolatedAsyncioTestCase):
    def test_canonical_builder_has_exact_keys_and_item_reuses_payload_bytes(self) -> None:
        profile = PublicSkillProfile(
            capability_id="skill.report",
            name="report",
            display_name="Report",
            description="safe description",
            triggers=("report",),
        )
        activation = build_canonical_skill_activation(
            binding_mode="hint",
            profile=profile,
            pinned_bundle_revision="revision-1",
            resolved_bundle_revision="revision-1",
        )
        payload = json.loads(activation.payload_json)
        self.assertEqual(
            set(payload),
            {"binding_mode", "pinned_bundle_revision", "profile", "profile_digest"},
        )
        self.assertEqual(payload["binding_mode"], "hint")
        self.assertEqual(payload["profile_digest"], activation.profile_digest)
        self.assertNotIn("SKILL.md raw body", activation.payload_json)

        run = AgentRun(
            "run-1",
            "task-1",
            "conv-1",
            AgentRunStatus.RUNNING,
            AgentModelBinding("edition-a"),
        )
        item = build_skill_activation_item(
            run=run,
            sequence=run.next_item_sequence,
            activation=activation,
            committed_at=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(item.payload_json, activation.payload_json)
        self.assertEqual(item.payload_sha256, activation.payload_sha256)

    def test_complete_activation_payload_enforces_exact_byte_limit(self) -> None:
        def build(description_length: int):
            return build_canonical_skill_activation(
                binding_mode="hint",
                profile=PublicSkillProfile(
                    capability_id="skill.report",
                    name="report",
                    display_name="Report",
                    description="x" * description_length,
                    triggers=(),
                ),
                pinned_bundle_revision="revision-1",
                resolved_bundle_revision="revision-1",
            )

        empty = build(0)
        accepted_131071 = build(AGENT_PAYLOAD_MAX_BYTES - empty.size_bytes - 1)
        accepted_131072 = build(AGENT_PAYLOAD_MAX_BYTES - empty.size_bytes)
        self.assertEqual(accepted_131071.size_bytes, AGENT_PAYLOAD_MAX_BYTES - 1)
        self.assertEqual(accepted_131072.size_bytes, AGENT_PAYLOAD_MAX_BYTES)
        with self.assertRaisesRegex(AgentPayloadError, "agent_payload_too_large"):
            build(AGENT_PAYLOAD_MAX_BYTES - empty.size_bytes + 1)

    async def test_activation_persists_only_public_profile_and_resource_metadata(self) -> None:
        port = _RecordingActivationPort()
        service = DelegatedSkillActivationService(
            port,
            now_fn=lambda: datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc),
        )
        profile = PublicSkillProfile(
            capability_id="skill.report",
            name="report",
            display_name="Report",
            description="safe description",
            triggers=("report",),
            resource_index=(
                {
                    "resource_id": "guide",
                    "title": "Guide",
                    "description": "safe",
                    "audience": ["main_agent"],
                    "path": "/private/guide.md",
                    "body": "must not leak",
                },
            ),
        )
        run = AgentRun(
            "run-1",
            "task-1",
            "conv-1",
            AgentRunStatus.RUNNING,
            AgentModelBinding("edition-a"),
        )

        activation = await service.activate(
            run=run,
            profile=profile,
            sequence=run.next_item_sequence,
            pinned_bundle_revision="revision-1",
            resolved_bundle_revision="revision-1",
        )

        self.assertEqual(len(port.items), 1)
        self.assertEqual(activation.item.kind, AgentItemKind.SKILL_ACTIVATION)
        payload = json.loads(activation.item.payload_json)
        self.assertEqual(payload["profile_digest"], activation.profile_digest)
        self.assertEqual(payload["binding_mode"], "delegated")
        resource = payload["profile"]["resource_index"][0]
        self.assertEqual(set(resource), {"resource_id", "title", "description", "audience"})
        serialized = activation.item.payload_json.lower()
        for forbidden in ("/private", '"body"', "scripts/", "config.yaml", "secret"):
            self.assertNotIn(forbidden, serialized)

    async def test_pinned_revision_mismatch_fails_before_commit(self) -> None:
        port = _RecordingActivationPort()
        service = DelegatedSkillActivationService(port)
        run = AgentRun(
            "run-1",
            "task-1",
            "conv-1",
            AgentRunStatus.RUNNING,
            AgentModelBinding("edition-a"),
        )
        profile = PublicSkillProfile("skill.report", "report", "Report", "safe", ())
        with self.assertRaisesRegex(ValueError, "pinned_revision_mismatch"):
            await service.activate(
                run=run,
                profile=profile,
                sequence=1,
                pinned_bundle_revision="revision-1",
                resolved_bundle_revision="revision-2",
            )
        self.assertEqual(port.items, [])
