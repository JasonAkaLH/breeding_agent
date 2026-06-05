# v2 Skill review checklist

## Structure

- [ ] Bundle lives under `skill/<skill-name>/`.
- [ ] `SKILL.md` exists and has only `name` and `description` frontmatter.
- [ ] `skill.contract.yaml` exists with `contract_version: '2'`.
- [ ] Executable skills have `schemas/*.input.yaml` unless they are truly delegated/instruction-only.
- [ ] Prompt-facing help lives in `references/*.md`.

## SKILL.md

- [ ] Description explains what the skill does and when to use it.
- [ ] Body is an agent-facing runbook, not an empty stub.
- [ ] Body includes workflow, missing-info strategy, output strategy, resource navigation, and boundaries.
- [ ] Body does not expose script paths, handler keys, service allowlists, config, secrets, local absolute paths, or internal runtime directories.
- [ ] Body stays under 500 lines; long details move to references.
- [ ] No v1 platform fields: `capability_id`, `display_name`, `triggers`, `public_usage`, top-level `parameters`, `input_parameters`, `scripts`, `execution`, `auto_run`, `run_by_default`.

## Contract

- [ ] `capability.id` is a stable `skill.*` id.
- [ ] Display name, description, and version are present.
- [ ] Routing triggers/examples are user phrases, not internal module names only.
- [ ] Runtime mode is one of supported v2 modes.
- [ ] Entrypoint paths are bundle-relative and do not escape with `..` or absolute paths.
- [ ] Output contracts declare required keys that the executor/handler actually returns.
- [ ] Resource policy has safe size limits and prompt-facing audiences only for safe references.

## Input schemas

- [ ] Separate schemas exist for modes with different required fields.
- [ ] Required fields are scoped to selected schema.
- [ ] Artifact fields come from upload/artifact sources, not generated local paths.
- [ ] Validation rules reject invalid enum/range/pattern values.
- [ ] Questions are user-facing and do not expose internals.

## Resource safety

- [ ] Prompt-facing resources are under `references/` or otherwise explicitly safe.
- [ ] Prompt-facing resources do not include scripts/runtime/schema/config/native internals.
- [ ] No secrets, tokens, credentials, DSNs, full auth headers, `.env`, `.git`, or local absolute paths.

## Tests

Recommended targeted checks:

```bash
python -m pytest tests/integrations/agent_skills/test_project_skill_manifest_contract.py
python -m pytest tests/integrations/agent_skills/test_skill_contract_parser.py tests/integrations/agent_skills/test_skill_capabilities.py
python -m pytest tests/integrations/agent_skills/test_input_schema_parser.py tests/integrations/agent_skills/test_input_schema_validation.py
python -m pytest tests/integrations/agent_skills/test_skill_resource_service.py tests/integrations/agent_skills/test_public_skill_profile.py
```

For project skills, add/update the skill-specific integration test and run it.
