# Migrating old v1 SKILL.md to v2

## What to move where

| Old v1 content | v2 destination |
| --- | --- |
| `name` | Keep in `SKILL.md` frontmatter. |
| `description` | Keep/improve in `SKILL.md` frontmatter; include trigger contexts. |
| `display_name` | `skill.contract.yaml capability.display_name`. |
| `capability_id` | `skill.contract.yaml capability.id`. |
| `triggers` | `skill.contract.yaml routing.triggers`; also summarize important phrases in `description`. |
| `public_usage` | Split into `references/*.md` plus concise `SKILL.md` resource navigation. |
| top-level `parameters` / `input_parameters` | `schemas/*.input.yaml` fields. |
| top-level `outputs` execution contract | `skill.contract.yaml outputs` / `output_contracts`. |
| `scripts` | `skill.contract.yaml entrypoints`. |
| `execution` | `skill.contract.yaml runtime` and platform service fields. |
| useful body workflow | Keep in `SKILL.md` body after removing internal paths/config and platform execution details. |


## File input migration

Old Skills may keep reading `uploaded_artifacts[].content` or `content_base64` during the compatibility window. New and migrated file-processing Skills should switch to:

1. read `payload["resource_manifest_path"]`;
2. parse `resource_manifest.json`;
3. iterate `manifest["files"]`;
4. open each file through `files[].mount_path`.

Do not migrate by asking the LLM to produce a local path. `mount_path` is supplied by the platform and points to a temporary per-run workspace copy. Persistent storage paths and `storage_key` are internal and must not appear in prompt-facing docs.

## Migration steps

1. Preserve a copy of the old file for reference via `git show HEAD:skill/<name>/SKILL.md` when needed.
2. Rewrite `SKILL.md` frontmatter to only `name` and a rich `description`.
3. Convert old body into an agent-facing runbook, and write descriptive/explanatory prose in Chinese except necessary parameter names, filenames, paths, identifiers, API terms, enum values, and domain-standard terms:
   - keep welcome text, workflow selection, input meanings, output strategy, interpretation rules, and boundaries;
   - remove executable command examples, script paths, handler names, internal config, service names, and file-system details unless they are user-visible artifacts.
4. Create `skill.contract.yaml` from old capability/execution/scripts/output fields.
5. Create one or more `schemas/*.input.yaml` from old parameters, splitting modes with different required fields.
6. Move long public usage/detail sections into `references/*.md` and add resource navigation to `SKILL.md`. Keep platform/runtime/service-boundary details out of prompt-facing references unless they are genuinely user-safe and needed by `main_agent`.
7. Run grep gate against project `SKILL.md` files:

```bash
rg -n "capability_id:|public_usage:|^scripts:|^parameters:|^execution:|auto_run|run_by_default" skill/*/SKILL.md || true
```

8. Run targeted tests.

## Common mistakes

- Making `SKILL.md` too small: keep enough runbook detail for another agent to use it.
- Copying machine schemas into `SKILL.md`: explain user-visible fields only.
- Letting references hide everything: `SKILL.md` must say when to read each reference.
- Restoring v1 fields because old tests expected them: update tests to v2 contract expectations.
- Exposing `scripts/`, `runtime/`, `schemas/`, `native/`, `config.yaml`, handler names, service selection rules, deployment endpoints, tokens, DSNs, or local paths to prompt-facing resources.
- Using old template keys such as `fields:` or `sources:` instead of current backend `inputs:` plus `source.allowed`.
- Declaring file outputs with `files:` instead of current backend `artifacts:`.
