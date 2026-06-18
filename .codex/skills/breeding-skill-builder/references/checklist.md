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
- [ ] Descriptive/explanatory prose is written in Chinese, except necessary parameter names, filenames, paths, identifiers, API terms, enum values, and domain-standard terms.
- [ ] Body does not expose script paths, handler keys, service allowlists, config, secrets, local absolute paths, or internal runtime directories.
- [ ] Body stays under 500 lines; long field tables, examples, report templates, and user-facing explanations move to references.
- [ ] No v1 platform fields: `capability_id`, `display_name`, `triggers`, `public_usage`, top-level `parameters`, `input_parameters`, `scripts`, `execution`, `auto_run`, `run_by_default`.

## Contract

- [ ] `capability.id` is a stable `skill.*` id.
- [ ] Display name, description, and version are present.
- [ ] Routing triggers/examples are user phrases, not internal module names only.
- [ ] Multi-schema skills hardcode schema refs with `input_schemas.<schema_id>.aliases`; schema selection does not rely only on `activation.aliases`, prose, or unsupported selector patterns.
- [ ] Runtime mode is one of supported v2 modes.
- [ ] Entrypoint paths are bundle-relative and do not escape with `..` or absolute paths.
- [ ] Output contracts declare required keys that the executor/handler actually returns.
- [ ] File outputs are declared under `artifacts`, not unsupported `files`.
- [ ] Output artifact extensions and MIME types are hardcoded for every emitted file type.
- [ ] Resource policy has safe size limits and prompt-facing audiences only for safe references.
- [ ] Skill-wide file needs, when present, are declared with `file_intent` and not only described in prose.

## Input schemas

- [ ] Separate schemas exist for modes with different required fields.
- [ ] Required fields are scoped to selected schema.
- [ ] Schemas use top-level `inputs`, not unsupported `fields`.
- [ ] Field source policies use `source.allowed`, not unsupported `sources`.
- [ ] Natural-language scalar fields include resolver-readable sources (`query`, `current_user_message`, `resolved_user_message`, `recent_user_message`, or `text`) when LLM/regex extraction is expected; they do not rely only on `user_text`.
- [ ] Artifact/file/data fields come from trusted upload/artifact sources, not generated local paths.
- [ ] File-like inputs that need chat-session file selection declare `file_selection` with supported file types, multi-file policy, expected content, helpful columns, or disambiguation hints as needed.
- [ ] File requirements can be normalized into a `FileRequirementProfile` from `file_selection`, `file_intent`, or legacy `type: file/artifact/data` signals; they are not only in `SKILL.md` or references prose.
- [ ] New python_subprocess file-processing Skills read `payload["resource_manifest_path"]` and `manifest["files"][].mount_path` as the primary file input contract.
- [ ] `uploaded_artifacts[].content` / `content_base64` are used only as explicit legacy compatibility fallback.
- [ ] Prompt-facing docs and references do not expose persistent storage paths, `storage_key`, or runtime-internal workspace assumptions.
- [ ] Selected-schema fixed values use field-level `const` when a mode implies the value.
- [ ] Enums/choices are field-level `enum` or `choices`, not only `validation.enum`.
- [ ] Important scalar fields hardcode `aliases` and `patterns` for common Chinese/English phrasing.
- [ ] Defaults and range limits are hardcoded on fields, not only described in prose.
- [ ] Validation rules reject invalid enum/range/pattern values.
- [ ] Questions are user-facing, written in Chinese unless preserving necessary terms, and do not expose internals.
- [ ] Schema field names match the script/handler payload keys; renaming fields is paired with runtime code changes.

## Resource safety

- [ ] Prompt-facing resources are under `references/` or otherwise explicitly safe.
- [ ] Prompt-facing resources do not include scripts/runtime/schema/config/native internals, service selection rules, deployment endpoints, handler names, or implementation-only notes.
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
