from __future__ import annotations

from dataclasses import dataclass

from .catalog import SkillCatalog
from .manifest import SkillManifest


@dataclass(slots=True, frozen=True)
class SkillMatch:
    manifest: SkillManifest
    score: int
    reason: str


def match_skills(query: str, catalog: SkillCatalog, *, max_matches: int = 1) -> list[SkillMatch]:
    text = query.lower()
    matches: list[SkillMatch] = []
    for skill in catalog.skills:
        score = 0
        reasons: list[str] = []
        triggers = list(skill.triggers)
        if skill.contract is not None:
            triggers.extend(skill.contract.routing.triggers)
            triggers.extend(skill.contract.routing.intent_aliases)
            triggers.extend(skill.contract.routing.examples)
            for ref in skill.contract.input_schemas.values():
                triggers.extend(ref.aliases)
                triggers.extend([ref.title, ref.description])
        for trigger in triggers:
            normalized = trigger.lower().strip()
            if normalized and normalized in text:
                score += 100
                reasons.append(f"trigger:{trigger}")
        if skill.name.lower() in text:
            score += 80
            reasons.append("name")
        for token in _description_tokens(skill.description):
            if token in text:
                score += 10
                reasons.append(f"description:{token}")
        if score > 0:
            matches.append(SkillMatch(manifest=skill, score=score, reason=", ".join(reasons)))
    matches.sort(key=lambda item: (-item.score, item.manifest.name))
    return matches[:max_matches]


def _description_tokens(description: str) -> tuple[str, ...]:
    return tuple(token for token in description.lower().replace("，", " ").replace(",", " ").split() if len(token) >= 2)
