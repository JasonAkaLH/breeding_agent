# v2 Skill bundle templates

## SKILL.md template

```markdown
---
name: my-skill
description: >-
  Do X for Y. Use when the user asks for A, B, C, or wants help with D/E/F.
---

# My Skill

用中文说明此 Skill 的用途。除必要的参数名、文件名、路径、代码标识符、API 术语、枚举值和领域标准术语外，描述性和解释性文字默认使用中文。

平台执行事实源由 `skill.contract.yaml` 和 selected `schemas/*.input.yaml` 决定；本文只提供 agent-facing runbook。

## Start protocol

When the user invokes the skill without enough information, ask only for the minimum missing information.

## Workflow selection

- Choose mode A when ...
- Choose mode B when ...

## Inputs and slot filling

Explain only user-visible fields and the missing-info strategy. Missing executable fields are determined by the selected schema. Move long field tables and examples to references.

## Resources

- `references/usage.md`: overall usage and examples; read when the user asks how to use this skill.
- `references/data-format.md`: input columns or accepted payloads; read before explaining accepted data formats.
- `references/report-style.md`: report sections and interpretation style; read before writing or explaining a long report.

## Output strategy

Describe the final user answer: summary, preview table, artifact links, caveats, follow-up suggestions.

## Boundaries

Do not expose scripts, handler keys, service selection rules, deployment endpoints, config, secrets, tokens, database URLs, local absolute paths, schemas, or internal runtime directories.
```

## skill.contract.yaml template

```yaml
contract_version: '2'
capability:
  id: skill.my_skill
  display_name: My Skill
  description: 一句中文用户可见描述，说明此 Skill 能做什么。
  version: 1.0.0
routing:
  triggers:
    - user phrase one
    - user phrase two
  intent_aliases:
    - /my-skill
  examples:
    - /my-skill run this on my uploaded CSV
runtime:
  mode: python_subprocess
  answer_mode: requires_finalizer
file_intent:
  requires_file: true
  default_allow_multiple: false
  supported_file_types:
    - csv
    - spreadsheet
  description: 需要用户上传或会话内已有的输入文件。
entrypoints:
  run:
    path: scripts/run_my_skill.py
    input_schema: default
    output: default_output
    timeout_seconds: 300
input_schemas:
  default:
    path: schemas/default.input.yaml
    title: Default input
    description: User-visible description of the executable input.
    aliases:
      - user-facing mode name
      - 中文模式别名
outputs:
  default_output:
    required:
      - answer
    artifacts:
      - extensions:
          - .csv
        mime_types:
          - text/csv
resources:
  usage:
    path: references/usage.md
    title: Usage
    description: User-visible usage notes and examples.
    audience:
      - main_agent
resource_policy:
  default_audience:
    - main_agent
  max_bytes: 12000
```

## platform_service contract shape

```yaml
contract_version: '2'
capability:
  id: skill.sql_query
  display_name: SQL Query
  description: Safe readonly database question answering.
runtime:
  mode: platform_service
  trust_scope: project
  handler: skill.sql_query.platform_handler
  handler_module: runtime/sql_query_skill/platform_handler.py
  handler_factory: build_handler
  answer_mode: requires_finalizer
  services:
    - mysql_readonly
    - llm.non_stream
    - artifact_writer
    - progress_events
entrypoints:
  run:
    input_schema: readonly_query
    output: query_output
input_schemas:
  readonly_query:
    path: schemas/readonly-query.input.yaml
outputs:
  query_output:
    required:
      - summary
      - filtered_query_result
resources:
  usage:
    path: references/usage.md
    audience: [main_agent]
```

## input schema template

```yaml
id: default
version: 1.0.0
title: 默认输入
description: 中文说明这个 schema 执行什么。
activation:
  aliases:
    - user-facing mode name
inputs:
  input_file:
    type: artifact
    required: true
    title: 输入文件
    question: 请上传输入文件。
    source:
      allowed:
        - artifact
        - task_attachment
        - upload_ledger
    file_selection:
      required: true
      allow_multiple: false
      supported_file_types:
        - csv
        - spreadsheet
      expected_content:
        - 输入数据表
      helpful_columns:
        - id
        - value
      disambiguation_hint: 优先选择用户最近上传或最近用于本 Skill 的输入数据表。
  mode:
    type: string
    required: true
    title: 模式
    question: 请说明要使用哪种模式。
    source:
      allowed:
        - query
        - current_user_message
        - resolved_user_message
        - recent_user_message
        - text
        - metadata
    const: mode_a
    enum:
      - mode_a
      - mode_b
  count:
    type: integer
    required: false
    title: 数量
    question: 请提供数量。
    source:
      allowed:
        - query
        - current_user_message
        - resolved_user_message
        - recent_user_message
        - text
    aliases:
      - 数量
      - count
    patterns:
      - '(?:数量|count)\s*[:：=]?\s*(\d+)'
      - '(\d+)\s*(?:个|次)?(?:数量|count)'
    validation:
      min: 1
      max: 100
```


## Python subprocess file input template

New file-processing Skills should prefer mounted files from the runtime manifest. `uploaded_artifacts[].content` and `content_base64` remain compatibility fields for old Skills only.

```python
import json
import sys
from pathlib import Path


def load_payload() -> dict:
    return json.load(sys.stdin)


def iter_mounted_files(payload: dict):
    manifest_path = payload.get("resource_manifest_path")
    if not manifest_path:
        return []
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    return manifest.get("files", [])


def main() -> None:
    payload = load_payload()
    files = iter_mounted_files(payload)
    if not files:
        raise SystemExit("No mounted input files were provided")
    first = files[0]
    input_path = Path(first["mount_path"])
    data = input_path.read_bytes()
    print(json.dumps({"answer": f"Read {len(data)} bytes from {first.get('filename')}"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
```

Rules:

- Use `payload["resource_manifest_path"]` and `manifest["files"][].mount_path` as the primary file contract.
- `mount_path` points to the per-run temporary workspace, not persistent storage.
- Do not expose or document persistent storage keys/paths in prompt-facing references.
- Keep fallback reads from `uploaded_artifacts[].content` / `content_base64` only when maintaining old Skills.

## Hardcoded field guidance

当前后端只读取固定字段，不会从说明文字推断平台契约。创建或修复 Skill 时必须显式硬编码：

- `skill.contract.yaml`: `capability.id`, `routing.triggers`, `entrypoints.*.path`, `entrypoints.*.input_schema`, `entrypoints.*.output`, `input_schemas.*.path`, `input_schemas.*.aliases`, `outputs.*.required`, `outputs.*.artifacts`, `resources.*.path`。
- `platform_service`: `runtime.handler`, `runtime.handler_module`, `runtime.handler_factory`, `runtime.services`, `runtime.trust_scope`。
- `schemas/*.input.yaml`: payload field names, `source.allowed`, field-level `const`, field-level `enum` / `choices`, `default`, `aliases`, `patterns`, range/length validation, and `required_when` / `constraints`。
- Script/handler code: read the same payload keys declared in schema and return exactly the keys/files declared in output contract.

Do not rely only on `SKILL.md` prose, schema `activation.aliases`, or `validation.enum` for executable behavior.

## references/usage.md template

```markdown
# Usage

## What this Skill does

Explain user-visible capability.

## Inputs

List accepted file types, columns, and parameter meanings.

## Examples

- /my-skill example request
- Natural language example

## Outputs

Explain user-visible output files and answer structure.

## Boundaries

State user-visible limits without exposing internals, deployment endpoints, service selection rules, handler names, config, schemas, scripts, or local paths.
```
