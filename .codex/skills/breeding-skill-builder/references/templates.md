# v2 Skill bundle templates

## SKILL.md template

```markdown
---
name: my-skill
description: >-
  Do X for Y. Use when the user asks for A, B, C, or wants help with D/E/F.
---

# My Skill

Use this Skill to ...

Platform execution facts live in `skill.contract.yaml` and selected `schemas/*.input.yaml`; this file is the agent-facing runbook.

## Start protocol

When the user invokes the skill without enough information, ask only for the minimum missing information.

## Workflow selection

- Choose mode A when ...
- Choose mode B when ...

## Inputs and slot filling

Explain only user-visible fields. Missing executable fields are determined by selected schema.

## Resources

- `references/usage.md`: overall usage and examples.
- `references/data-format.md`: input columns or accepted payloads.

## Output strategy

Describe the final user answer: summary, preview table, artifact links, caveats, follow-up suggestions.

## Boundaries

Do not expose scripts, handler keys, services, config, secrets, tokens, database URLs, local absolute paths, or internal runtime directories.
```

## skill.contract.yaml template

```yaml
contract_version: '2'
capability:
  id: skill.my_skill
  display_name: My Skill
  description: One user-facing sentence describing what this skill does.
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
outputs:
  default_output:
    required:
      - answer
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
title: Default input
description: What this schema executes.
activation:
  aliases:
    - user-facing mode name
fields:
  input_file:
    type: artifact
    required: true
    title: Input file
    question: Please upload the input file.
    sources:
      - upload
  mode:
    type: string
    required: true
    title: Mode
    question: Which mode should I use?
    sources:
      - user_text
      - metadata
    validation:
      enum:
        - mode_a
        - mode_b
  count:
    type: integer
    required: false
    title: Count
    question: Please provide the count.
    sources:
      - user_text
    validation:
      min: 1
      max: 100
```

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

State user-visible limits without exposing internals.
```
