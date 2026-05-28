# 阶段二 PRD —— 主代理信封字符串迁移

- **日期**：2026-05-29
- **状态**：待实施
- **父实施计划**：`docs/orchestration/大语言模型提示词信封与缓存友好上下文组装实施计划.md`
- **所属专题**：大语言模型提示词信封
- **范围**：主代理 prompt envelope builder、`off|shadow|string` 模式、主代理 audit-only event、文件下载硬约束迁移、主代理回归
- **非范围**：不迁移 Planner/Soft Skill/Skill resolver；不改 LLMClient 入参；不启用 messages-native

## 1. 问题陈述

主代理是用户最终回答、Skill 中间回答和全局 finalizer 的核心 LLM 调用点。当前 `build_main_agent_prompt` 把系统规则、memory、artifact、dependency、Skill body、script result 和用户问题拼成单字符串。阶段二要让主代理先接入提示词信封，但对 runtime 仍渲染为字符串，保证迁移可回滚。

## 2. 目标

1. 新增 `src/capabilities/main_agent/prompt_envelope_builder.py` 或等价适配层。
2. 将主代理稳定规则拆成 `stable_system_contract`、`stable_tool_rules`、`final_recency_guard`。
3. 将 memory 放入 `bulk_conversation_history`，dependency/script/artifact 放入 `required_tool_results_and_artifacts`。
4. 将当前用户请求放入尾部 recency 区。
5. 支持 `MAF_PROMPT_ENVELOPE_MODE=off|shadow|string`。
6. `shadow` 模式返回旧 prompt，但生成 prompt audit。
7. `string` 模式发送 envelope-to-string prompt。

## 3. 非目标

- 不把 Skill public profile 安全替换作为本阶段完成条件；阶段二可保留现状或仅为阶段四预留 segment。
- 不改变 `LLMClient` / `SharedLLMRuntime`。
- 不改前端 UI。
- 不在 audit 中记录 raw prompt。

## 4. 功能需求

| ID | Requirement | Acceptance |
| --- | --- | --- |
| P2-FR-1 | 必须提供可测试 rendered seam。 | 有 `build_main_agent_rendered_prompt(...) -> RenderedPrompt` 或等价函数，可直接断言 segments/audit。 |
| P2-FR-2 | `off` 模式必须保持旧行为。 | 旧主代理 prompt 顺序/关键内容测试不变。 |
| P2-FR-3 | `shadow` 模式不得改变发送给 LLM 的 prompt。 | fake stream generator 收到旧 prompt；audit-only event 包含 envelope audit。 |
| P2-FR-4 | `string` 模式必须发送 envelope-to-string prompt。 | fake stream generator 收到新顺序 prompt；current user 与 final guard 接近末尾。 |
| P2-FR-5 | 文件下载硬约束必须保留。 | string 模式 prompt 仍禁止 `sandbox:/mnt/data`、`file://`、`outputs/...`，并只允许平台 download_url。 |
| P2-FR-6 | audit event 不影响 SSE。 | 前端可见 `main_agent.output_delta` / `main_agent.output_final` 语义不变。 |

## 5. 非功能需求

- **Backward compatibility**：默认 mode 不得直接切到 `string`；合并时默认应为 `off` 或 `shadow`。
- **Observability**：`main_agent.llm_call` 或新增 audit-only event 包含 render audit 摘要。
- **Security**：audit 不含 raw prompt / raw artifact；artifact_context 仍使用现有脱敏逻辑。

## 6. 实施计划

1. 在阶段一核心模型基础上新增主代理 builder。
2. 改造 `build_main_agent_prompt` 为兼容包装：`off` 走旧逻辑，`shadow/string` 走 envelope seam。
3. 在 `MainAgentRespondCapability.execute` 收集 rendered audit 并写入 audit-only event 或扩展 `main_agent.llm_call` payload。
4. 更新主代理 prompt 测试，覆盖 `off`、`shadow`、`string`。
5. 跑 API/main_agent targeted tests，确认 streaming completion-only 行为不变。

## 7. 验收标准

- `conda run -n multi_agent python -m unittest tests.capabilities.main_agent.test_conversation_memory_prompt` 通过。
- `conda run -n multi_agent python -m unittest tests.capabilities.main_agent.test_main_agent_workflow_and_executor` 通过。
- `conda run -n multi_agent python -m unittest tests.api.test_main_agent_llm` 通过或等价 API 回归通过。
- shadow 模式不改变实际 prompt，有测试证据。
- License Requirement：无依赖/许可变更，未触发 cargo-deny 风险。

## 8. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| prompt 顺序变化影响模型输出。 | 默认 `off/shadow`，string 需显式启用；先用 fake/provider smoke 比较。 |
| audit 写入污染前端事件。 | 只使用 `EventVisibility.AUDIT_ONLY`；增加 SSE 回归。 |
| builder 与旧函数并存导致分叉。 | 明确旧函数只是兼容包装，新增行为在 rendered seam 测试。 |
