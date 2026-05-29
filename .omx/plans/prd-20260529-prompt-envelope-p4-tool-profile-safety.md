# Prompt Envelope P4 — 工具信息分层与能力公开档案安全实施计划

- 日期：2026-05-29
- 模式：`$ralph` + `$plan`（直接计划，非交互；实施由 Ralph 单 owner 完成，Team 仅在测试/回归无法串行收口时启用）
- 目标 PRD：`docs/prd/backend/prompt-envelope/05-阶段四-工具信息分层与能力公开档案安全PRD.md`
- Ralph context snapshot：`.omx/context/prompt-envelope-p4-tool-profile-safety-20260529T032540Z.md`

## 1. Requirements Summary

1. 主代理匹配到 Skill 时，prompt 必须使用 public Skill profile，禁止注入 `match.manifest.body`。当前 legacy prompt 仍在 Skill match block 拼接 `match.manifest.body`（`src/capabilities/main_agent/prompt_builder.py:63-73`），string envelope 的 tool result segment 也仍拼入 `match.manifest.body`（`src/capabilities/main_agent/prompt_envelope_builder.py:411-418`）。
2. PromptEnvelope 要形成四层工具信息：`stable_tool_rules`、`selected_public_tool_profiles`、`tool_input_schema`、`required_tool_results_and_artifacts`。核心 renderer 已支持 `tool_profile` / `tool_schema` security role（`src/orchestration/prompt_envelope.py:18-28`），P4 只需在主代理 envelope 装配层接入。
3. 必须复用 `build_public_skill_profile` 的 sanitizer，不复制不一致 allowlist。该函数已明确只从 frontmatter / `public_usage` allowlist 生成 profile，不读取 `manifest.body` 或 runtime/script 字段（`src/integrations/codex_skills/public_profile.py:90-114`）。
4. 公开档案需足够回答用户“需要什么数据、格式、字段、示例如何填写”，因此需要补强 inputs/outputs schema 投影，并保留参数名、类型、必填、aliases、patterns/enum/default、public_usage examples 等用户可见信息（现有参数投影见 `src/integrations/codex_skills/public_profile.py:117-138`）。
5. string 模式 P2 guard 要解除：当前 `MAF_PROMPT_ENVELOPE_MODE=string` 且存在 `skill_matches` 时会 fallback 到 legacy 并记录 `skill_match_requires_p4_public_profile`（`src/capabilities/main_agent/prompt_envelope_builder.py:253-263`）；P4 完成后应发送 string envelope 且仍不泄漏内部信息。
6. tool result segment 必须保留 download_url、missing/error/diagnostics 等事实，同时去除 script path、entrypoint、handler、runtime、local path、raw artifact content 等面向内部的字段。现有 dependency/artifact sanitizer 已保留平台 `/api/v1/artifacts/.../download` 并丢弃 raw path/content（`src/capabilities/main_agent/prompt_builder.py:169-204`），但 script results 仍直接 JSON dump（`src/capabilities/main_agent/prompt_builder.py:73-74`）。
7. 现有测试有意锁定旧风险，需要在 P4 反转：`test_phase_zero_documents_current_skill_manifest_body_exposure_risk` 断言 manifest body 暴露（`tests/capabilities/main_agent/test_conversation_memory_prompt.py:87-99`），string+skill guard 测试断言 fallback（`tests/capabilities/main_agent/test_main_agent_workflow_and_executor.py:585-634`），matched skill body 测试断言 body 注入（`tests/capabilities/main_agent/test_main_agent_workflow_and_executor.py:747-783`）。

## 2. Acceptance Criteria（可测试）

- AC1：legacy/off 主代理 prompt 和 string PromptEnvelope 在 Skill match 下都包含 `capability_id`、`display_name`、`description`、`public_usage`、用户可见参数/输入/输出信息；不包含 `manifest.body` 中的 `runtime`、`handler`、`scripts/...`、`config`、DSN、token、secret。
- AC2：string PromptEnvelope 在 Skill match 下不再 fallback 到 off；`main_agent.prompt_envelope_rendered` audit payload 的 `effective_mode == "string"`，且 audit payload 不包含用户原文或内部 Skill body。
- AC3：PromptEnvelope segment 顺序与安全角色为：稳定系统契约 → stable tool rules → selected public tool profiles → tool input schema → bulk conversation history → required tool results/artifacts → active continuity notes → current user request → recency guard。
- AC4：tool input schema segment 只含用户可见契约：参数名、类型、必填、sources/aliases/patterns/enum/default、inputs/outputs 公开 schema、missing input 标准；不含 entrypoint/handler/internal path/runtime/script。
- AC5：tool result segment 继续保留平台下载 URL `/api/v1/artifacts/{id}/download`、missing/error/diagnostics；不把 artifact raw content、storage_ref、local path 或 script entrypoint 暴露给 LLM prompt。
- AC6：`/skill` 软绑定答疑继续使用 public profile，现有 prompt safety test 仍通过。
- AC7：无依赖/许可变更，License Requirement 记录为未触发 cargo-deny 风险。

## 3. Implementation Steps

### CP-0 — TDD 反转旧风险测试

- 修改/新增 `tests/capabilities/main_agent/test_conversation_memory_prompt.py`：
  - 将 P0 “body exposure risk” 测试反转为 P4 public profile 安全测试。
  - 新增 string rendered seam + skill match 测试，断言 `selected_public_tool_profiles` 与 `tool_input_schema` segment 存在且 marker 顺序正确。
  - 新增 artifact/script result 安全测试，覆盖 raw content/path/storage_ref/entrypoint 不入 prompt，download_url/missing/error/diagnostics 保留。
- 修改 `tests/capabilities/main_agent/test_main_agent_workflow_and_executor.py`：
  - 将 string+skill guard 测试改为 string prompt 正常发送。
  - 将 `test_prompt_includes_matched_skill_body` 改为 “matched skill prompt uses public profile”。
- 扩展 `tests/integrations/codex_skills/test_public_skill_profile.py`：覆盖 inputs/outputs schema 的公开投影和敏感字段拒绝。

### CP-1 — 扩展 public profile 一处 sanitizer

- 在 `src/integrations/codex_skills/public_profile.py` 内补强 `_io_contract_payload`：读取 `SkillIOContract.schema` 的公开字段（例如 required/files/schema/properties/columns/fields/formats/examples），继续复用 `_sanitize_public_value` 与 forbidden key/text denylist。
- 如需补充 tool schema 投影 helper，应放在 public_profile 同模块，保证主代理与软绑定共用同一安全口径。

### CP-2 — legacy prompt 使用 public profile 与安全 tool result 投影

- 在 `src/capabilities/main_agent/prompt_builder.py` 引入 `build_public_skill_profile`，新增小型 helper：
  - `build_selected_public_skill_profiles(skill_matches)`：按 matched skill 生成 public profile，并加入 match score/reason（仅安全文本）。
  - `build_tool_input_schemas_from_profiles(profiles)`：生成用户可见 tool schema。
  - `sanitize_script_results_for_prompt(script_results)`：保留 `skill_name`、safe output、missing/error/diagnostics/output_files download facts；丢弃 `entrypoint`、handler/runtime/path/raw content/storage_ref。
- 替换 legacy `# 已匹配 Skill 指令` 内容为 public profile JSON/文本；保留旧 marker 以降低 prompt 顺序兼容风险，但内容不再含 body。
- `# Skill 脚本输出` 使用 sanitized script results。

### CP-3 — PromptEnvelope segment 分层与 string guard 下线

- 在 `src/capabilities/main_agent/prompt_envelope_builder.py`：
  - 增加 `selected_public_tool_profiles` segment（security_role=`tool_profile`）。
  - 增加 `tool_input_schema` segment（security_role=`tool_schema`）。
  - 从 `_format_required_tool_results_and_artifacts` 移除 skill match body，仅保留 artifact/dependency/sanitized script results。
  - 解除 string+skill fallback guard，让 P4 string mode 正常渲染/发送 envelope。
  - 更新 template version（例如 `p4-tool-profile-v1`），便于 audit 区分。

### CP-4 — 回归、architect verification、deslop 与提交

- 运行目标测试与相关分层回归。
- 由 architect 复核 prompt/audit 安全、segment ordering、下载事实保留。
- 对本轮变更文件执行 ai-slop-cleaner 范围内的简化/一致性检查，随后重新运行回归。
- 写入 Ralph completion audit、更新 Codex goal complete、按 Lore protocol 提交 git。

## 4. RALPLAN-DR Summary

### Principles

1. **公开优先**：LLM 只接收用户可见能力说明，不接收内部执行结构。
2. **单一 sanitizer**：所有 public Skill profile / tool schema 复用 `public_profile.py` 的 allowlist + denylist。
3. **事实保留但不泄密**：download/missing/error/diagnostics 是最终回答必要事实，路径/token/runtime 不是。
4. **默认 fail closed**：预算、required segment、敏感字段扫描失败均视为不可放量。

### Decision Drivers

1. P4 安全验收要求 prompt/audit 无脚本路径、handler、runtime、DSN、token、secret。
2. P2/P3 已接入 final input preflight，P4 不应绕开 renderer/audit。
3. `/skill` 软绑定已建立 public profile 基线，应复用而非另建并行 schema。

### Viable Options

- 方案 A（选定）：在 public_profile 模块补强公开 schema 投影，主代理 legacy/string 都消费 public profile helper。
  - 优点：最少重复；安全口径集中；兼容现有 `/skill`。
  - 缺点：public_profile 模块职责扩大，需要测试约束避免塞入 runtime 字段。
- 方案 B：只在 main_agent prompt_builder 内做局部脱敏。
  - 优点：改动表面更小。
  - 缺点：与 `/skill` sanitizer 产生双轨，后续易漂移；不满足 P4-FR-2。
- 方案 C：P4 仅保留 string guard，等 P5 一起做工具 schema。
  - 优点：风险低。
  - 缺点：不满足 P4 解除 `manifest.body` 与四层 segment 的交付目标。

### ADR

- Decision：采用方案 A，public_profile 是唯一 public Skill/profile/schema 安全投影来源；main_agent legacy/string prompt 只消费该投影与 sanitized tool result。
- Drivers：P4-FR-1/2/3/4/5；现有 `build_public_skill_profile` 已具备禁止 body/runtime/script 的设计；PromptEnvelope core 已支持 tool_profile/tool_schema role。
- Alternatives considered：方案 B 因 sanitizer 双轨被拒绝；方案 C 因范围不足被拒绝。
- Why chosen：能同时关闭旧泄漏、解除 string+skill guard、保留用户可见用法信息，并维持小而可测的 diff。
- Consequences：旧测试需反转；audit template version 会变化；执行 output_payload 中的内部 entrypoint 可保持 contract，但 LLM prompt 投影不得暴露。
- Follow-ups：P5 可在此 public profile/tool schema 之上扩展 Soft Skill/Planner/Resolver 多调用场景档案。

## 5. Risks and Mitigations

- 风险：public profile 过度脱敏导致用户无法理解字段。
  - 缓解：测试覆盖 `public_usage.input_formats/parameters/examples`、parameters aliases/patterns/enum/default、inputs/outputs schema。
- 风险：script result sanitization 误删 finalizer 必要下载事实。
  - 缓解：测试同时断言 `/api/v1/artifacts/.../download` 保留且本地路径/raw content 删除。
- 风险：string mode 放量后 required public profile segment 过大导致预算失败。
  - 缓解：profile 只限 selected Skill 且 public_usage/schema 经过 sanitizer；renderer final preflight 仍 fail closed。
- 风险：audit payload 泄漏 prompt/raw content。
  - 缓解：沿用 segment hash/stats audit，不记录 content；新增敏感词扫描测试。

## 6. Verification Steps

```bash
conda run -n multi_agent python -m unittest tests.integrations.codex_skills.test_public_skill_profile
conda run -n multi_agent python -m unittest tests.capabilities.main_agent.test_conversation_memory_prompt
conda run -n multi_agent python -m unittest tests.capabilities.main_agent.test_main_agent_workflow_and_executor
conda run -n multi_agent python -m unittest discover -s tests/capabilities/main_agent -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/integrations/codex_skills -p 'test_*.py'
python -m compileall src/capabilities/main_agent src/integrations/codex_skills
```

License Requirement：本计划不新增/升级依赖，不触发 `native/`、`Cargo.lock`、`native/deny.toml` 或供应链策略变更；最终报告记录“无依赖/许可变更，未触发 cargo-deny 风险”。

## 7. Team / Goal Follow-up Guidance

- 当前推荐 Ralph 单 owner 实施：改动集中在 prompt/profile/test，串行 TDD + 回归更易控制共享文件冲突。
- 如回归大面积失败或需要并行审计，可启用 `$team`：
  - Lane A（executor）：实现 public_profile/prompt builder。
  - Lane B（test-engineer）：维护 main_agent 与 integrations 测试。
  - Lane C（verifier/architect）：安全扫描、audit 与 finalizer 下载事实复核。
- Goal stop condition：上述 AC1-AC7 全部有测试或文件证据，architect verification 与 post-deslop regression 均通过，Ralph completion audit readback 成功并完成 git commit。
