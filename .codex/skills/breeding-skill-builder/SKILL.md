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
8. Write descriptive and explanatory prose in Chinese by default. Keep only necessary parameter names, filenames, paths, code identifiers, API terms, enum values, and domain-standard terms in their original language.
9. Hardcode every platform-read field that the runtime will not infer: capability id, routing triggers, entrypoint names/paths, input schema refs, schema aliases, selected-schema constants, source policies, enum/default/range/pattern rules, output artifact extensions/MIME types, resources, and platform-service handlers/services.

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

The body should be a compact agent runbook, usually 50-200 lines. Keep only
the decisions and interaction rules an agent needs immediately after trigger:

- Overview and supported workflows.
- Welcome/start protocol if the skill is user-facing.
- How to select sub-workflows or modes.
- User-visible input and missing-info strategy.
- Resource navigation: list each relevant `references/*.md` and when to read it.
- Output strategy and follow-up behavior.
- Boundaries: no internal paths/config/secrets/runtime details in user-facing answers.
- Language: descriptive/explanatory prose should be Chinese except necessary parameter names, filenames, paths, identifiers, API terms, enum values, and domain-standard terms.

Do not copy long field tables, full examples, report templates, service boundary
details, or implementation notes into `SKILL.md`. Put those in `references/*.md`
when they are user-safe and prompt-facing, or keep them only in contract/runtime
files when they are platform/internal.

Do not place old v1 platform fields in `SKILL.md`: `capability_id`, `display_name`, `triggers`, `public_usage`, top-level `parameters`, `input_parameters`, `scripts`, `outputs` as execution contract, `execution`, `auto_run`, or `run_by_default`.

### 4. Write or update `skill.contract.yaml`

Use the contract for platform facts:

- `contract_version: '2'`
- `capability.id`, `display_name`, `description`, `version`
- `routing.triggers`, `intent_aliases`, `examples`
- `runtime.mode`, `answer_mode`, trust/service config when platform service
- `entrypoints`
- `input_schemas` with bundle-relative paths and schema-level `aliases` for every mode phrase the selector must recognize
- `schema_selector` when more than one schema can match; remember current selector behavior is driven by schema refs/aliases/titles/descriptions, not free-form prose
- `outputs` / `output_contracts`; file outputs must be declared with `artifacts`, not `files`
- `resources` and `resource_policy`

Hardcode contract internals deliberately. The runtime does not infer script paths, entrypoint ids, selected input schema ids, output contracts, artifact allowlists, platform handlers, or service allowlists from `SKILL.md` prose.

For templates, read `references/templates.md`.

### 5. Write or update `schemas/*.input.yaml`

Use one schema per business mode when required fields differ. Examples:

- RCBD and Interval should be separate schemas if CK parameters are only required for Interval.
- OCR file input and SQL query input can each be a single schema.

Each schema file must use the current backend shape: top-level `schema_id` (or
`id`) and `inputs`, with each field using `source.allowed` for accepted sources.
Each field should describe type, title/question or clarification,
required/required_when, aliases/patterns, and validation. Artifact fields must
come from trusted upload/artifact sources, not LLM-generated file paths. Do not
use unsupported `fields:` or `sources:` keys in new project skills.

Hardcode schema internals the resolver depends on:

- For multi-schema skills, add `input_schemas.<schema_id>.aliases` in `skill.contract.yaml`; do not rely on schema-local `activation.aliases` alone for schema selection.
- If selecting a schema implies a fixed field value, put `const` on the field, for example `design.const: rcbd`; do not require the LLM or user to restate it.
- Put `enum` / `choices` at the field top level when validation depends on it; do not hide executable enums only under `validation.enum`.
- For natural-language slot extraction, include resolver-readable sources such as `query`, `current_user_message`, `resolved_user_message`, `recent_user_message`, or `text`; do not use only `user_text` unless the runtime has explicit support for it.
- For important scalar slots, hardcode `patterns` that match expected Chinese and English phrasing, especially numeric phrases like `3次重复`, `列数10`, `K=3`, or `maf=0.05`.
- Keep field names aligned with script/handler payload keys (`material_data`, `blocks`, `ncols`, `analysis`, etc.); changing field names requires changing runtime code too.
- Hardcode defaults such as `planter: serpentine`, `randomize: true`, `ck_ratio: A`, and range limits on the field, not only in explanatory text.

### 6. Write or update `references/*.md`

References are prompt-facing help resources loaded on demand. Put detailed but
user-safe content there, not in `SKILL.md`:

- Field definitions and accepted formats.
- Business examples and longer input/output examples.
- Report structure, interpretation rules, and final-answer style.
- Query/data boundaries and user-facing safety notes.

References must still be safe for the main agent to read. Do not put secrets,
connection strings, handler names, raw script internals, runtime/service
selection rules, deployment endpoints, config values, local absolute paths, or
schema/runtime implementation details in prompt-facing references. If a note is
needed only by a wrapper or service operator, keep it out of `references/` or do
not expose it with `audience: [main_agent]`.

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
