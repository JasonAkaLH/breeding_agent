# V2 Skill Slot State Machine Design

- **Status**: Draft approved for planning review
- **Date**: 2026-06-08
- **Target modules**: Agent Skill runtime, v2 Skill contracts, input schema selection, slot collection, interrupt/resume lifecycle, storage repositories, frontend waiting-input UX
- **Primary outcome**: Replace mixed v1/v2 Skill parameter resolution with a v2-only, event-driven, durable Slot State Machine that lets LLMs extract user-provided parameters from schema-aware context while backend validation remains the execution authority.

## 1. Problem statement

The current Agent Skill parameter path mixes several responsibilities and persistence strategies:

1. v2 Skill contracts exist, but v2 input resolution in `SkillScriptExecutionService._resolve_v2_inputs()` is deterministic and does not use the selected input schema as full LLM extraction context.
2. Existing `_slot_collection` data is embedded in `Interrupt.required_fields`, which makes the interrupt JSON a temporary carrier rather than a durable state authority.
3. User answers are currently converted into field-shaped payloads such as `{"design":"对角线增广"}` and displayed as `design=对角线增广`, leaking internal parameter keys into chat history.
4. For v2 Skills, `skill.contract.yaml + selected input_schema` should be the only parameter source of truth, but current code still has old manifest parameter surfaces and fallback behavior in the Agent execution path.
5. The system cannot reliably audit, recover, deduplicate, or replay multi-round parameter collection because slot state is not a first-class domain object.

The desired runtime must support a production-grade loop:

```text
Start v2 Skill
  -> select input schema or ask for schema selector field
  -> build durable SlotCollection from selected schema
  -> generate natural-language question
  -> wait for user answer
  -> use LLM normal extraction or history recall extraction
  -> validate against schema
  -> update SlotCollection and SlotEvent audit trail
  -> repeat until ready, cancelled, or failed
  -> execute contract-bound Skill entrypoint with canonical payload
```

## 2. Goals and non-goals

### Goals

- **G1 v2-only Agent Skill runtime**: Project Skill execution must use `skill.contract.yaml` and selected `input_schema` as the only parameter fact source.
- **G2 Durable slot state**: `SlotCollection` must be a first-class persisted domain object, not long-term state embedded in `Interrupt.required_fields`.
- **G3 Event-driven auditability**: Every meaningful slot transition must append a `SlotEvent`, with selected events mirrored to existing runtime events.
- **G4 Schema-aware LLM extraction**: LLM extraction must receive selected schema context, current slot state, and user answer, and must return raw/canonical candidate values.
- **G5 History recall separation**: Normal extraction and history recall extraction must use separate modes/prompts so each LLM call has focused attention.
- **G6 Backend authority**: LLM output is never trusted directly. Backend schema validation, artifact ledger checks, and state-machine rules are the final execution gates.
- **G7 Frontend simplification**: Frontend must display backend questions and submit raw user answers/uploads; it must not parse business parameters or display internal key-value payloads.
- **G8 Recovery and idempotency**: Repeated answers, network retries, process restarts, and duplicate scheduling must not corrupt slot state or double-run scripts.
- **G9 Field-design correctness**: Field-design v2 schemas must encode required business parameters such as `ncols`; runtime must not infer required semantics from `SKILL.md` prose.

### Non-goals

- Do not rewrite task/node/message/upload/artifact/checkpoint storage from scratch.
- Do not keep v1 Skill parameter compatibility in the Agent Skill execution path.
- Do not let LLMs invent files, upload IDs, local paths, artifacts, schema fields, or internal execution details.
- Do not store raw artifact content, credentials, provider config, database URLs, cookies, or secrets in slot state.
- Do not use full assistant history as default extraction context.
- Do not use `Interrupt.required_fields._slot_collection` as the long-term state authority.

## 3. Superseded decisions and relationship to existing docs

This design supersedes the persistence decision in:

- `docs/superpowers/specs/2026-06-04-skill-slot-dialogue-design.md`

Specifically, this design rejects that document's Phase 1 decision to avoid new tables and persist slot state primarily inside `Interrupt.required_fields._slot_collection`.

This design aligns with and strengthens the v2-only direction in:

- `docs/prd/backend/skill-contract-progressive-disclosure/05-SkillExecutorV2与SlotCollectionV2PRD.md`

Updated position:

- Existing interrupt/resume UX remains useful.
- Existing `_slot_collection` envelope may remain as a frontend-compatible reference/summary.
- Durable slot state must live in first-class slot storage.
- v1 manifest execution/scripts/parameters must be removed from Agent Skill execution as a supported path.

## 4. Users, stakeholders, and affected systems

| Category | Actor/system | Concern |
| --- | --- | --- |
| End user | Chat user running Skills | Natural clarification, no internal keys, no repeated lost context, recoverable multi-round parameter collection. |
| Skill author | Maintainers of `skill/*/skill.contract.yaml` and schemas | Business requirements must be declared in v2 schemas; `SKILL.md` prose is guidance, not execution authority. |
| Backend runtime | `src/integrations/agent_skills`, `src/capabilities/skill_tool`, `src/api/runtime.py`, orchestration | Clear v2-only execution path, durable state, idempotent resume, schema validation, artifact safety. |
| Frontend | `frontend/src/App.tsx`, API client/types | Display questions, submit raw answers/uploads, restore waiting state, avoid parameter parsing. |
| Storage | SQLite/PostgreSQL repositories and `StoragePort` | New slot models, migrations, indexes, CAS semantics, audit events. |
| Operations/testing | Logs, events, tests | Debuggable event trail, deterministic acceptance criteria, rollback guard. |

## 5. Current-state evidence

| Evidence | Current behavior / constraint |
| --- | --- |
| `src/integrations/agent_skills/execution.py` | v2 contract Skills branch into `_resolve_v2_inputs()`; non-contract Skills use `resolve_skill_inputs_with_llm()`. |
| `src/integrations/agent_skills/execution.py` | `_resolve_v2_inputs()` selects schema and validates payload deterministically; it does not pass full selected schema to an LLM extractor. |
| `src/integrations/agent_skills/missing_input_interrupt.py` | `_slot_collection` is built from `manifest.parameters`, which is empty for v2-only `field-design`; v2 schema constraints such as `const`, `min`, and `max` are not fully represented. |
| `src/api/runtime.py` | `answer_interrupt()` merges answer payloads into metadata, formats user messages as `key=value`, and reschedules execution with the combined message. |
| `src/core/models.py` and `src/storage/sqlite/models.py` | Interrupt and InterruptAnswer can persist JSON, but there is no first-class slot collection model or slot event model. |
| `skill/field-design/skill.contract.yaml` | Field-design is a v2 contract Skill with multiple input schemas and deterministic_then_llm selector intent. |
| `skill/field-design/schemas/*.input.yaml` | `design` uses schema `const`; `ncols` currently appears optional in diagonal and interval schemas despite business guidance saying those flows need `ncols`. |

## 6. Proposed architecture

### 6.1 High-level components

```text
V2SkillExecutionCoordinator
  -> V2SchemaSelector
  -> SlotStateMachine
  -> SlotPromptGenerator
  -> SlotExtractor
       -> NormalExtraction
       -> HistoryRecallExtraction
       -> HistoryReferenceClassifier fallback
  -> SlotSchemaValidator
  -> SkillEntrypointScheduler
  -> ScriptRunner / PlatformHandler
```

### 6.2 Component responsibilities

| Component | Responsibility | Must not do |
| --- | --- | --- |
| `V2SkillExecutionCoordinator` | Own v2-only Skill execution orchestration and ask SlotStateMachine whether execution is ready. | Parse user natural language directly. |
| `V2SchemaSelector` | Select or ask for input schema. It may use deterministic aliases first and LLM fallback when configured. | Execute entrypoints before schema is selected. |
| `SlotStateMachine` | Create, update, validate, transition, persist, and audit SlotCollection. | Trust LLM output without validator. |
| `SlotPromptGenerator` | Generate user-facing questions from current SlotCollection. | Modify schema constraints. |
| `SlotExtractor` | Produce candidate raw/value patches for missing/invalid fields. | Claim artifacts exist or emit undeclared fields. |
| `SlotSchemaValidator` | Enforce selected schema and source policies. | Infer business requiredness from `SKILL.md`. |
| Frontend | Display backend question and submit raw text/uploads/controls. | Construct `{field:value}` payloads for business parameters. |

## 7. SlotCollection lifecycle

### 7.1 Collection kinds

`SlotCollection.kind` distinguishes schema selection from selected-schema parameter collection.

#### `schema_selection`

Used when multiple input schemas are possible and no schema has been selected.

```json
{
  "kind": "schema_selection",
  "status": "waiting_for_user",
  "selector_field": "design",
  "allowed_schemas": [
    {"schema_id": "rcbd", "title": "随机区组设计", "aliases": ["rcbd", "随机区组"]},
    {"schema_id": "diagonal", "title": "对角线增广设计", "aliases": ["diagonal", "对角线", "对角线增广"]},
    {"schema_id": "interval", "title": "间比法设计", "aliases": ["interval", "间比法"]}
  ],
  "missing": ["design"]
}
```

Rules:

- No Skill entrypoint may execute while collection kind is `schema_selection`.
- LLM/heuristics may only choose an allowed schema ID, not populate business parameters.
- Once selected, append `slot.schema_selected` and transition to `input_collection`.

#### `input_collection`

Used after selected schema is known.

```json
{
  "kind": "input_collection",
  "selected_schema_id": "diagonal",
  "selected_entrypoint": "run",
  "schema_snapshot": {"schema_id": "diagonal", "inputs": {}},
  "slots": {},
  "missing": []
}
```

Rules:

- `schema_snapshot` is a complete public snapshot of selected schema exposed inputs.
- Extraction may only resolve fields in current `missing` or `invalid` sets.
- Resume must use the persisted snapshot and must not reselect schema because the bundle changed.

### 7.2 Collection statuses

| Status | Meaning | Allowed next statuses |
| --- | --- | --- |
| `collecting` | State exists and is being prepared/updated. | `waiting_for_user`, `extracting`, `validating`, `ready`, `failed`, `cancelled` |
| `waiting_for_user` | Question generated and interrupt open. | `extracting`, `cancelled`, `failed` |
| `extracting` | Processing a user answer with normal or history recall extraction. | `validating`, `waiting_for_user`, `failed`, `cancelled` |
| `validating` | Backend validator is applying schema/source rules. | `waiting_for_user`, `ready`, `failed`, `cancelled` |
| `ready` | Required parameters complete and entrypoint can be scheduled. | `script_scheduled`, `failed`, `cancelled` |
| `script_scheduled` | Entry point has been scheduled exactly once. | `completed`, `failed`, `cancelled` |
| `completed` | Skill execution completed. | terminal |
| `cancelled` | User or runtime cancellation. | terminal |
| `failed` | System-level failure, not user missing input. | terminal |

## 8. Storage model

### 8.1 `SlotCollection`

Add a first-class model and repository table.

```text
collection_id PK
task_id
node_id
conversation_id
capability_id
skill_name
kind
status
round
revision
selected_schema_id
selected_entrypoint
skill_bundle_revision
contract_revision
schema_digest
schema_snapshot_json
slots_json
resolved_json
missing_json
invalid_json
last_question
created_at
updated_at
completed_at
cancelled_at
failed_at
```

Required indexes:

```text
(task_id, node_id, status)
(task_id, status)
(conversation_id, status, updated_at)
```

### 8.2 `SlotEvent`

Add an append-only event model.

```text
slot_event_id PK
collection_id
task_id
node_id
conversation_id
event_type
round
revision
idempotency_key
payload_json
created_at
```

Required indexes/constraints:

```text
index(collection_id, created_at)
index(task_id, created_at)
unique(collection_id, idempotency_key) when idempotency_key is not null
```

### 8.3 StoragePort methods

Add async methods to the existing storage abstraction rather than creating a new storage stack:

```python
save_slot_collection(collection)
get_slot_collection(collection_id)
get_active_slot_collection_for_node(task_id, node_id)
list_slot_collections_for_task(task_id)
compare_and_swap_slot_collection(collection_id, expected_revision, next_collection)
append_slot_event(event)
list_slot_events(collection_id)
get_slot_event_by_idempotency_key(collection_id, key)
```

SQLite and PostgreSQL repositories must implement equivalent semantics.

## 9. Concurrency, idempotency, and recovery

### 9.1 Revision / CAS

`SlotCollection.revision` is an optimistic concurrency token. Every state mutation must update with expected revision.

```text
update slot_collection
set ..., revision = revision + 1
where collection_id = ? and revision = expected_revision
```

If CAS fails:

1. Reload collection.
2. If desired result already exists, return idempotent success.
3. Otherwise retry a bounded number of times or return conflict.

### 9.2 Answer idempotency

Each user answer must have an idempotency key.

Preferred client payload:

```json
{
  "task_id": "...",
  "interrupt_id": "...",
  "client_request_id": "uuid",
  "answer": {"text": "对角线增广", "upload_ids": []}
}
```

Derived event idempotency key:

```text
answer:{interrupt_id}:{client_request_id}
```

If the frontend cannot provide a client request ID during migration, the backend may derive a weaker key from the saved interrupt answer ID. Delivery-grade implementation should add frontend `client_request_id`.

### 9.3 Single script scheduling gate

Transition from `ready` to `script_scheduled` must be atomic and idempotent. Repeated resume, SSE reconnect, or duplicate answers must not schedule the same Skill entrypoint twice.

Required event gate:

```text
slot.collection_ready
slot.script_scheduled
```

If `slot.script_scheduled` already exists for the collection, later scheduling attempts must return success without executing again.

### 9.4 Recovery

On process restart or task refresh:

- Runtime must query active SlotCollection for task/node.
- Open interrupt can be restored from existing interrupt tables and linked by `_slot_collection_ref`.
- If an interrupt exists but SlotCollection is missing, fail closed and record recovery error; do not reconstruct authoritative state from interrupt JSON.
- If SlotCollection is `ready` but no `script_scheduled` event exists, scheduler may resume scheduling once through the atomic gate.

## 10. Event model

### 10.1 SlotEvent types

Required event types:

```text
slot.collection_started
slot.schema_selection_started
slot.schema_selected
slot.prompt_generated
slot.user_answer_received
slot.extraction_attempted
slot.extraction_succeeded
slot.extraction_failed
slot.validation_failed
slot.collection_updated
slot.collection_ready
slot.script_scheduled
slot.collection_completed
slot.collection_cancelled
slot.collection_failed
slot.runtime_rollback
```

### 10.2 SlotEvent vs EventRecord

`SlotEvent` is the internal domain event. `EventRecord` remains the existing runtime/SSE/audit event surface.

| SlotEvent | Mirror to EventRecord | Visibility |
| --- | --- | --- |
| `slot.collection_started` | Yes | AUDIT_ONLY |
| `slot.schema_selection_started` | Yes | AUDIT_ONLY |
| `slot.schema_selected` | Yes | AUDIT_ONLY |
| `slot.prompt_generated` | Yes | FRONTEND and AUDIT_ONLY summary |
| `slot.user_answer_received` | Yes | AUDIT_ONLY, redacted |
| `slot.extraction_attempted` | Yes | AUDIT_ONLY |
| `slot.extraction_succeeded` | Yes | AUDIT_ONLY, field names only |
| `slot.extraction_failed` | Yes | AUDIT_ONLY, reason code only |
| `slot.validation_failed` | Yes | FRONTEND/AUDIT, field/error code only |
| `slot.collection_updated` | Yes | AUDIT_ONLY |
| `slot.collection_ready` | Yes | AUDIT_ONLY |
| `slot.script_scheduled` | Yes | AUDIT_ONLY |
| `slot.collection_completed` | Yes | AUDIT_ONLY |
| `slot.collection_cancelled` | Yes | FRONTEND/AUDIT |
| `slot.collection_failed` | Yes | FRONTEND/AUDIT if user-visible |
| `slot.runtime_rollback` | Yes | AUDIT_ONLY |

Frontend must continue to rely on task/interrupt events for UX. It must not consume internal SlotEvent directly unless a future API exposes a sanitized slot timeline.

## 11. LLM context design

### 11.1 Shared constraints

All slot LLM calls must:

- Receive only prompt-safe context.
- Use strict JSON output.
- Be parsed and validated as untrusted output.
- Only operate on declared schema fields.
- Never see raw artifact contents, credentials, provider secrets, internal paths, handler names, or database URLs.

### 11.2 Normal extraction

Triggered when a user answers a current slot question directly.

Prompt input:

```json
{
  "mode": "normal_extraction",
  "current_user_answer": "对角线增广",
  "slot_collection": {
    "collection_id": "...",
    "kind": "input_collection",
    "selected_schema_id": "diagonal",
    "round": 1,
    "missing": ["design"],
    "resolved": {},
    "schema_snapshot": {},
    "slots": {}
  }
}
```

Required output schema:

```json
{
  "resolved": {
    "design": {"raw": "对角线增广", "value": "diagonal"}
  },
  "missing": [],
  "invalid": []
}
```

Rules:

- Output fields must be a subset of current missing/invalid fields.
- Values must be canonical where possible.
- LLM may preserve raw user text in `raw`.
- Backend may canonicalize `value` further using schema `const`, `enum`, and aliases.

### 11.3 History recall extraction

Triggered only by history-reference handling.

Prompt input:

```json
{
  "mode": "history_recall_extraction",
  "current_user_answer": "我之前不是告诉过你了吗",
  "slot_collection": {},
  "history_window": {
    "original_user_request": "...",
    "recent_user_messages": [],
    "accepted_interrupt_answers": []
  }
}
```

Rules:

- Only extract current missing/invalid fields.
- Only use bounded user-origin history and accepted interrupt answers.
- Do not use full assistant long replies by default.
- If no clear value exists, return missing and do not guess.

Default history bounds:

- `recent_user_messages`: latest 5 user-origin messages for the same conversation/task context.
- `accepted_interrupt_answers`: all accepted answers for the current task, redacted and ordered.
- `original_user_request`: root user message, redacted.

### 11.4 History recall trigger

History Recall uses a two-level mechanism.

1. Backend deterministic rules trigger directly for stable phrases such as:
   - “之前说过”
   - “上面说了”
   - “刚才那个”
   - “按前面的”
   - “你自己看”
   - “不是告诉过你了吗”

2. If rules do not match, run Normal Extraction first. Only if Normal Extraction resolves no valid fields, pass an ambiguity gate before a lightweight LLM classifier.

Ambiguity gate conditions:

- No valid field extracted, or all candidate fields failed validation.
- Answer does not look like direct data: no clear number, enum, schema alias, file action, or `key=value`.
- Answer has low-information or deictic/context-dependent markers.
- There is bounded history available.
- Answer is not cancellation, refusal, or “I don't know”.

Classifier output:

```json
{"is_history_reference": true, "reason": "..."}
```

If classifier returns true, run History Recall. Otherwise generate the next question.

### 11.5 Slot prompt generation

Triggered when missing/invalid fields remain.

Prompt input:

```json
{
  "mode": "slot_prompt_generation",
  "slot_collection": {
    "missing": ["ncols"],
    "slots": {
      "ncols": {
        "type": "integer",
        "validation": {"min": 1, "max": 1000},
        "validation_error": null
      }
    },
    "schema_snapshot": {}
  }
}
```

Output:

```json
{
  "question": "还需要田块列数，请回复一个 1 到 1000 之间的整数，例如：12。",
  "ask_fields": ["ncols"],
  "answer_hint": "例如：田块列数 12"
}
```

Rules:

- LLM can only generate user-facing text.
- It cannot change schema, slot status, missing fields, constraints, or values.
- If validation error exists, the question must explain the actionable correction.

## 12. Validation and deterministic fallback

### 12.1 Backend validation

Backend validator must enforce:

- Field exists in selected schema.
- Field is in current missing/invalid set.
- Type coercion succeeds.
- `const` matches.
- `enum` is in allowed set.
- `validation.min/max` passes.
- regex, min_length, max_length pass.
- `required_when` and constraints pass.
- `source.allowed` passes.
- Artifact/file/data slots refer to actual upload/artifact ledger entries.
- Sensitive raw values are redacted or rejected.

### 12.2 Deterministic fallback layering

LLM is preferred for natural-language scalar extraction. Deterministic fallback may run after LLM failure or invalid JSON.

Allowed deterministic fallback:

1. Schema `const` and safe canonical guard.
2. Schema `default` for optional fields only, unless schema explicitly marks a required default as user-intent-free.
3. Artifact/upload ledger checks.
4. Clear `key=value` or obvious integer phrases such as `ncols=12`, `12列`, `三个重复`.
5. Alias/enum/const canonicalization when mapping is exact or unambiguous.

Disallowed fallback:

- Inferring required business values from `SKILL.md` prose.
- Creating artifact values from text.
- Filling optional business decisions not represented as schema default.
- Overwriting a resolved value unless the current slot is missing/invalid or the user explicitly corrects it.

### 12.3 Validation failures

Invalid candidate values return the field to missing/invalid state with a structured error.

```json
{
  "status": "missing",
  "raw_value": "零列",
  "value": null,
  "validation_error": {
    "code": "min_value",
    "message": "田块列数必须大于等于 1"
  }
}
```

The next question must address the specific error.

## 13. API contract and frontend behavior

### 13.1 Answer API shape

For v2 slot interrupts, frontend must submit raw answer payloads.

```json
{
  "task_id": "task-1",
  "interrupt_id": "interrupt-1",
  "client_request_id": "uuid",
  "answer": {
    "text": "对角线增广",
    "upload_ids": [],
    "sheet_selections": {}
  }
}
```

Implementation may either revise the existing endpoint DTO or introduce a v2-compatible answer DTO behind the same endpoint. The externally visible contract must reject business field-shaped payloads for v2 slot interrupts.

Rejected v2 payload example:

```json
{"design":"对角线增广"}
```

Response:

```json
{"detail":"v2 slot interrupts require answer.text/upload_ids payload"}
```

### 13.2 Interrupt envelope

`Interrupt.required_fields` should only carry frontend-safe reference/summary data.

```json
{
  "_slot_collection_ref": {
    "collection_id": "slot-...",
    "kind": "input_collection",
    "round": 2,
    "revision": 5,
    "missing": ["ncols"],
    "question": "请提供田块列数，例如 12。"
  }
}
```

Frontend uses `Interrupt.question` for primary display.

### 13.3 Chat display

Supplemental user messages must preserve user-visible raw content:

- Text answer: `对角线增广`
- Upload-only answer: `已上传文件：materials.csv`
- Text plus upload:

```text
对角线增广
已上传文件：materials.csv
```

They must not display `design=对角线增广` or other internal parameter keys.

### 13.4 Frontend responsibilities

Frontend must:

- Display backend question.
- Display optional missing tags using user-facing labels only.
- Submit raw text, upload IDs, sheet selections, and client request ID.
- Restore current waiting state from open interrupt and slot collection ref.

Frontend must not:

- Construct `{field:value}` payloads for business parameters.
- Canonicalize design names, integers, aliases, enums, or defaults.
- Decide which Skill parameter is being answered.
- Display internal field names as the primary UX.

### 13.5 Uploads

Upload flow remains through the existing upload API. Slot answers reference upload IDs only.

Backend must validate:

- Upload belongs to current user and conversation.
- Upload is still available.
- Upload type matches selected schema/source rules.
- Spreadsheet normalization and sheet selection are complete before artifact field is resolved.

### 13.6 Sheet selection

Sheet selection should become a first-class slot/control under v2 slot flow.

Answer payload:

```json
{
  "answer": {
    "text": "",
    "sheet_selections": {"upl-123": "Sheet1"}
  }
}
```

Backend validates sheet existence and updates artifact slot metadata. Frontend may render a select control, but it must not infer business parameters from sheet names.

## 14. Field-design contract cleanup

Because v2 schema is the only execution fact source, field-design schemas must be corrected as part of implementation.

Required cleanup:

- `diagonal.input.yaml`: mark `ncols` required if diagonal design requires it.
- `interval.input.yaml`: mark `ncols` required if interval design requires it.
- Add complete aliases where necessary, e.g. `对角线增广` as a design alias if expected from users.
- Add descriptions/hints for user-facing prompt generation.
- Add validation bounds where business rules require them.
- Keep `design.const` canonical values: `rcbd`, `diagonal`, `interval`.

Runtime must not use `SKILL.md` prose to infer missing required fields.

## 15. Migration, rollout, and rollback

### 15.1 Migration strategy

This is a v2-only cutover, not long-term dual compatibility.

Phases:

1. Add slot models, migrations, repositories, and contract tests.
2. Implement Slot State Machine and V2SkillExecutionCoordinator behind guard.
3. Switch v2 Skill execution to the Slot State Machine.
4. Change frontend answer submission to raw answer payload.
5. Clean field-design schemas.
6. Remove or fail-close v1 project Skill execution paths.

### 15.2 Runtime guard

Use a guard for controlled rollout:

```text
MAF_V2_SLOT_RUNTIME=disabled|shadow|enabled
```

- `disabled`: do not create new slot collections; emergency rollback only.
- `shadow`: compute slot projection/events without changing authoritative execution.
- `enabled`: Slot State Machine is authoritative for v2 Skills.

`shadow` is for rollout validation, not permanent compatibility.

### 15.3 Rollback behavior

If enabled mode fails severely:

- Set `MAF_V2_SLOT_RUNTIME=disabled`.
- Stop creating new SlotCollections.
- Mark open slot collections `failed` or `cancelled_by_runtime_rollback` according to product decision.
- Do not auto-delete already produced artifacts.
- Append `slot.runtime_rollback` and mirror audit event.

## 16. Security, privacy, and redaction

- Slot state may store safe scalar `raw_value` and canonical `value`.
- Secret-like raw values must be redacted or rejected using existing sensitive marker patterns plus slot-specific guards.
- Artifact slots may store upload ID, filename, content type, hash, selected sheet, and summary, but not raw content or base64.
- LLM prompts must not contain provider secrets, DB URLs, cookies, tokens, raw file bytes, local absolute execution paths, handler names, or internal service config.
- Audit/EventRecord payloads should include field names, source type, validation codes, and safe summaries, not full raw history.
- History Recall must only include bounded user-origin history and accepted interrupt answers after redaction.

## 17. Functional requirements

| ID | Requirement | Acceptance signal |
| --- | --- | --- |
| FR-001 | v2 project Skills must execute only through contract + input schema. | No v1 manifest parameters/scripts path executes project Skill without contract. |
| FR-002 | Schema selection ambiguity must open a schema_selection SlotCollection. | Ambiguous field-design request asks for design type and does not execute. |
| FR-003 | Selected schema snapshot must be persisted in SlotCollection. | Resume uses persisted schema digest/revision and does not reselect after bundle change. |
| FR-004 | Normal extraction must receive full selected schema snapshot and current slot state. | Prompt construction test shows `const`, `enum`, aliases, source, and validation bounds. |
| FR-005 | History Recall must be separate from Normal Extraction. | Separate prompt/mode tests and trigger tests pass. |
| FR-006 | LLM output must use raw/value candidate format. | “对角线增广” stores raw and canonical `diagonal`. |
| FR-007 | Backend validator must be the final execution gate. | Invalid LLM value returns field to missing/invalid with validation_error. |
| FR-008 | Frontend must submit raw answer text for v2 slot interrupts. | API/client tests reject `{design:"..."}` and accept `{answer:{text:"..."}}`. |
| FR-009 | User chat history must display raw user answer. | No `design=对角线增广` appears after answer submission or reload. |
| FR-010 | SlotEvent must record every major transition. | Integration tests assert event sequence for multi-round field-design run. |
| FR-011 | Duplicate answers must be idempotent. | Replaying same `client_request_id` does not duplicate extraction or script scheduling. |
| FR-012 | Ready-to-script scheduling must be exactly once. | Concurrent ready/resume attempts produce one script run. |
| FR-013 | Upload/artifact slots must be ledger-backed. | LLM/text cannot create artifact availability without upload record. |
| FR-014 | Sheet selection must integrate with v2 slot flow. | Spreadsheet upload requiring sheet opens sheet selection slot/control and validates selected sheet. |
| FR-015 | Field-design schemas must encode business-required parameters. | Diagonal/Interval missing `ncols` opens a prompt before script execution. |

## 18. Non-functional requirements

| Category | Requirement |
| --- | --- |
| Reliability | SlotCollection and SlotEvent must survive process restart; active waiting collections must restore. |
| Consistency | Slot updates must use revision/CAS semantics or equivalent transaction isolation. |
| Security | LLM and frontend inputs are untrusted; schema/source/artifact validation must fail closed. |
| Privacy | Slot state and events must redact sensitive values and never store raw artifact content. |
| Observability | SlotEvent plus mirrored EventRecord must enable tracing schema selection, extraction, validation, prompt, and scheduling. |
| Performance | Normal successful answer should require at most one extraction LLM call and one prompt LLM call only if still missing. History classifier only runs after ambiguity gate. |
| Accessibility | Waiting state remains screen-reader visible via existing status patterns; question text appears in normal chat flow. |
| Compatibility | Existing task/node/interrupt/upload/artifact/checkpoint infrastructure remains; project Skill v1 path is not compatible by design. |
| Testability | Unit, repository, integration, frontend, and fake-LLM tests must cover every state transition and major failure mode. |

## 19. Edge cases and failure modes

| Case | Expected behavior |
| --- | --- |
| User says “我之前不是告诉过你了吗” | Rule triggers History Recall; if history has clear value, resolve; otherwise ask again with acknowledgement. |
| User gives invalid integer “零列” | Store raw_value, reject value, set validation_error, ask for valid range. |
| LLM returns invalid JSON | Append extraction_failed, deterministic fallback if safe, otherwise continue prompting. |
| LLM returns undeclared field | Reject field and audit diagnostic. |
| LLM claims file exists | Reject unless upload/artifact ledger confirms. |
| User cancels | Mark SlotCollection cancelled, close/cancel interrupt, do not execute Skill. |
| Duplicate answer request | Return idempotent result without duplicate SlotEvent/script run. |
| Bundle updates while waiting | Resume uses persisted schema_snapshot/schema_digest. |
| Collection exists but interrupt missing | Recreate frontend interrupt from collection if safe; otherwise fail closed and audit recovery error. |
| Interrupt exists but collection missing | Fail closed; do not reconstruct authority from interrupt JSON. |
| Runtime rollback | Mark affected open collections failed/cancelled by rollback and audit. |

## 20. Test plan

### 20.1 Unit tests

- Schema snapshot builder includes exposed inputs, `const`, `enum`, aliases, defaults, source policy, validation min/max, regex, required_when, constraints, resources.
- SlotCollection initialization for schema_selection and input_collection.
- SlotStateMachine transitions and forbidden transitions.
- SlotSchemaValidator type/const/enum/min/max/source/artifact checks.
- Normal extraction prompt construction.
- History recall trigger rules and ambiguity gate.
- History recall prompt construction.
- Prompt generator handles validation errors.
- Sensitive raw_value redaction/rejection.

### 20.2 Repository tests

- SQLite insert/update/list SlotCollection.
- SQLite append/list SlotEvent.
- SQLite CAS success/failure.
- SQLite idempotency unique behavior.
- PostgreSQL equivalent repository or shared contract tests.
- Migration/bootstrap tests for both backends.

### 20.3 API/runtime integration tests

- Ambiguous schema opens schema_selection collection.
- Field-design diagonal flow:
  1. upload material file;
  2. answer `对角线增广`;
  3. answer `12列`;
  4. script payload contains `design=diagonal`, `ncols=12`.
- Interval missing `ck_spec` continues loop.
- Duplicate answer does not duplicate scheduling.
- Restart/reload restores waiting collection.
- Validation failure loops back with new question.
- Cancellation path does not execute Skill.
- v2 slot interrupt rejects field-shaped old payload.

### 20.4 Frontend tests

- Interrupt question displayed from backend.
- Answer submit uses raw text/upload IDs/client request ID.
- User message displays raw answer, not `key=value`.
- Missing/validation tags use labels, not internal keys.
- Upload-only answer display remains user-friendly.
- Sheet selection submits `sheet_selections` without business parsing.
- Refresh restores current waiting prompt.

### 20.5 Fake LLM tests

- Normal extraction: `对角线增广` -> raw `对角线增广`, value `diagonal`.
- Normal extraction: `12列` -> value `12`.
- History recall: direct reference with recent history resolves.
- History recall: no history returns missing.
- Invalid LLM canonical value is corrected by exact schema guard or rejected.

## 21. Acceptance criteria

Delivery is complete only when all of the following are true:

1. v2 Skill execution no longer depends on `manifest.parameters` as parameter authority.
2. SlotCollection/SlotEvent models exist for SQLite and PostgreSQL storage paths.
3. `Interrupt.required_fields` contains only frontend-safe slot refs/summaries, not authoritative full slot state.
4. LLM normal extraction receives complete selected schema context.
5. LLM history recall is a distinct mode with deterministic trigger plus ambiguity-gated classifier fallback.
6. Frontend no longer parses business parameters for v2 slot interrupts.
7. User answers display as raw user text in history.
8. `design=对角线增广` no longer appears in normal chat display.
9. Field-design diagonal/interval schemas encode required `ncols` if required by business flow.
10. Field-design answer `对角线增广` ultimately executes with canonical `design="diagonal"`.
11. Multi-round slot flow survives restart and can be audited through SlotEvent.
12. Duplicate answer/schedule paths are idempotent.
13. All required tests in Section 20 pass.

## 22. Rollout risks, assumptions, and open questions

### Risks

| Risk | Mitigation |
| --- | --- |
| Scope is larger than a bug fix. | Implement in phases: storage/contracts, state machine, API/frontend, field-design cleanup, v1 removal. |
| Migration touches SQLite and PostgreSQL. | Add shared repository contract tests and migration verification before runtime cutover. |
| LLM prompt/output instability. | Strict JSON parser, fake LLM tests, fallback, validation gate, prompt profile tests. |
| v1 removal may break unknown project Skills. | Audit project Skill inventory and fail closed with clear error for no-contract Skills. |
| Frontend/API payload change can break existing flows. | Gate by v2 slot interrupt type; update client tests; preserve non-slot specialized flows only where explicitly still needed. |

### Assumptions

- Project Skills should be migrated to v2 contracts rather than supported through v1 compatibility.
- It is acceptable to add database tables and migrations for delivery-grade slot persistence.
- Existing task/node/interrupt/upload/artifact/checkpoint infrastructure is stable enough to reuse.
- Field-design business rules should be expressed in input schema rather than `SKILL.md` prose.

### Open questions for implementation planning

- Whether the answer API should add a new endpoint or evolve the existing endpoint DTO with discriminated payloads.
- Whether `slot_event` should be mirrored synchronously inside the same transaction as SlotCollection update or through a best-effort event sink wrapper.
- Exact PostgreSQL migration naming and rollback convention for the slot tables.
- Whether to expose a sanitized slot timeline API in a later frontend debugging feature.

These questions do not block the product design, but they must be resolved in implementation planning.
