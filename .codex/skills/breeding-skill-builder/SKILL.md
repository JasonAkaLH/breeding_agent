---
name: breeding-skill-builder
description: >-
  Create or update breeding_agent backend v2 Skill bundles. Use when working on files under project skill bundle directories, writing or reviewing SKILL.md, skill.contract.yaml, schemas/*.input.yaml, references/*.md, platform_service contracts, python_subprocess entrypoints, SkillResourceService policies, slot-collection behavior, or migrating old v1 Skill frontmatter fields to the v2 contract layout.
---

# Breeding Skill Builder

Use this skill to create, migrate, review, or repair project-level backend Skill bundles for `breeding_agent`.

A valid project Skill is a v2-only bundle:

```text
skill/<skill-name>/
  SKILL.md                  # agent-facing runbook; frontmatter only name/description
  skill.contract.yaml        # platform capability/runtime/entrypoint/output/resource contract
  schemas/*.input.yaml       # machine-readable selected-schema input contracts
  references/*.md            # prompt-facing help/resources loaded on demand
  scripts/ or runtime/       # implementation internals referenced only by contract
```

## Golden rules

1. Keep `SKILL.md` useful, not empty: it is the agent-facing runbook loaded after the skill triggers.
2. Keep platform facts out of `SKILL.md`: no capability ids, entrypoints, handler keys, runtime/service config, old parameter manifests, or autorun semantics.
3. Put all public registration and execution facts in `skill.contract.yaml`.
4. Put executable input contracts in `schemas/*.input.yaml`; required fields are scoped to the selected schema.
5. Put detailed user-visible explanations in `references/*.md`, and list when to read them from `SKILL.md`.
6. Use `SkillResourceService` boundaries: prompt-facing resources must not expose scripts, runtime, schemas, native code, config, secrets, tokens, credentials, `.env`, `.git`, or absolute/local paths.
7. External callers never submit `capability_id=skill.*` directly. Slash/API skill selection goes through `main_agent.respond + metadata.soft_skill_binding`.

## Workflow

### 1. Classify the request

- **New skill**: create the full v2 bundle.
- **Migration**: remove v1 platform fields from `SKILL.md`, create contract/schema/resources, and preserve useful old runbook content.
- **Review/fix**: compare files against the v2 rules and repair the smallest broken surface.
- **Docs/template work**: update `Skill构建指南.md` or reusable examples without changing runtime behavior unless asked.

### 2. Inspect existing local patterns first

Before editing, inspect related files:

- Current project skills: `skill/*/SKILL.md`, `skill/*/skill.contract.yaml`, `skill/*/schemas/*.input.yaml`, `skill/*/references/*.md`.
- Core implementation: `src/integrations/agent_skills/`, `src/capabilities/main_agent/`, `src/capabilities/skill_tool/`.
- Canonical guide: `Skill构建指南.md`.
- PRD source of truth when needed: `docs/prd/backend/skill-contract-progressive-disclosure/`.

### 3. Write or update `SKILL.md`

`SKILL.md` frontmatter must contain only:

```yaml
name: <hyphen-case-skill-name>
description: >-
  What the skill does and specific situations/user phrasing that should trigger it.
```

The body should be a compact agent runbook, usually 50-200 lines:

- Overview and supported workflows.
- Welcome/start protocol if the skill is user-facing.
- How to select sub-workflows or modes.
- User-visible input and missing-info strategy.
- Resource navigation: list each relevant `references/*.md` and when to read it.
- Output strategy and follow-up behavior.
- Boundaries: no internal paths/config/secrets/runtime details in user-facing answers.

Do not place old v1 platform fields in `SKILL.md`: `capability_id`, `display_name`, `triggers`, `public_usage`, top-level `parameters`, `input_parameters`, `scripts`, `outputs` as execution contract, `execution`, `auto_run`, or `run_by_default`.

### 4. Write or update `skill.contract.yaml`

Use the contract for platform facts:

- `contract_version: '2'`
- `capability.id`, `display_name`, `description`, `version`
- `routing.triggers`, `intent_aliases`, `examples`
- `runtime.mode`, `answer_mode`, trust/service config when platform service
- `entrypoints`
- `input_schemas`
- `schema_selector` when more than one schema can match
- `outputs` / `output_contracts`
- `resources` and `resource_policy`

For templates, read `references/templates.md`.

### 5. Write or update `schemas/*.input.yaml`

Use one schema per business mode when required fields differ. Examples:

- RCBD and Interval should be separate schemas if CK parameters are only required for Interval.
- OCR file input and SQL query input can each be a single schema.

Each field should describe type, title/question, sources, required/required_when, aliases/patterns, and validation. Artifact fields must come from upload/artifact sources, not LLM-generated file paths.

### 6. Write or update `references/*.md`

References are prompt-facing help resources. Put detailed but user-safe content there:

- Field definitions and accepted formats.
- Business examples.
- Report structure and interpretation rules.
- Query boundaries and user-facing safety notes.

Do not put secrets, connection strings, handler names, raw script internals, deployment endpoints, or config values in prompt-facing references.

### 7. Verify

Run the smallest relevant checks:

```bash
python /Users/yinpeihai/.codex/skills/.system/skill-creator/scripts/quick_validate.py .codex/skills/breeding-skill-builder
python -m pytest tests/integrations/agent_skills/test_project_skill_manifest_contract.py
```

For runtime changes, add targeted integration/API tests and run relevant subsets from `references/checklist.md`.

## References

- `references/templates.md`: v2 file templates for `SKILL.md`, `skill.contract.yaml`, input schema, and references.
- `references/checklist.md`: review and verification checklist.
- `references/migration.md`: how to migrate old v1 `SKILL.md` content into v2 files.
