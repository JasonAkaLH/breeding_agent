# 阶段零 PRD —— 测试基线与旧行为锁定

- **日期**：2026-05-29
- **状态**：待实施
- **父总纲 PRD**：`docs/prd/backend/prompt-envelope/00-大语言模型提示词信封与缓存友好上下文组装总纲PRD.md`
- **所属专题**：大语言模型提示词信封
- **范围**：主代理现有 prompt 顺序、conversation memory 静态预算、Skill prompt 安全风险、旧 LLM runtime 字符串路径、关键 API 回归基线
- **非范围**：不新增 PromptEnvelope 核心实现；不改变生产 prompt；不改 LLMClient / SharedLLMRuntime；不修复 Skill resume artifact 继承问题

## 1. 问题陈述

后续阶段会重排 prompt segment、改写历史预算、替换 Skill profile 注入和扩展 runtime 入参。如果阶段零不先把现有行为和风险点写成测试，后续迁移无法区分“有意改变”和“回归”。当前主代理仍在 `build_main_agent_prompt` 中用 `parts` 拼接单字符串；conversation memory 使用固定 25% reserved tokens；主代理 Skill match 仍可能注入 `match.manifest.body`。这些都必须先被测试描述。

## 2. 目标

1. 锁定现有主代理 prompt 顺序和关键安全文案。
2. 锁定 `ConversationMemoryConfig.actual_memory_budget` 的当前静态 75% 行为，作为后续替换基线。
3. 证明现有主代理 Skill match 有暴露 `manifest.body` 的风险，并为阶段四的禁止内部结构断言预留测试。
4. 锁定 `LLMClient` / `SharedLLMRuntime` 当前字符串输入路径，确保阶段一至阶段五不误改 provider 调用形态。
5. 形成阶段性验证命令，供后续每个阶段复跑。

## 3. 非目标

- 不引入新的 prompt 结构化模型。
- 不调整任何 prompt 内容或顺序。
- 不新增数据库、事件 schema 或前端能力。
- 不把红测试伪装为已修复行为；阶段零提交到默认回归套件的测试必须通过。若需要描述未来目标行为，应以 `skip`、明确 TODO 用例或后续阶段测试计划记录，不得让默认 `unittest discover` 留下失败测试。

## 4. 当前证据

| 证据 | 约束 |
| --- | --- |
| `src/capabilities/main_agent/prompt_builder.py:23-75` 用 `parts` 拼接主代理 prompt。 | 需要主代理顺序快照测试。 |
| `src/capabilities/main_agent/prompt_builder.py:49-74` memory 位于 artifact / dependency / current user 之前。 | 后续阶段必须有可比较的顺序变化。 |
| `src/capabilities/main_agent/prompt_builder.py:62-71` 拼接 `match.manifest.body`。 | 后续安全迁移必须有失败用例。 |
| `src/orchestration/conversation_memory.py:63-67` 固定 `max(1024, max_tokens // 4)` reserved tokens。 | 后续动态预算替换必须先锁定现状。 |
| `src/integrations/llm_client.py:153-203` 发送单条 user message。 | 阶段零至阶段五不得误启 messages-native。 |

## 5. 功能需求

| ID | Requirement | Acceptance |
| --- | --- | --- |
| P0-FR-1 | 必须新增主代理 prompt 顺序测试。 | 测试覆盖 system rules、memory、artifact、response role、dependency、Skill match、script output、user question 的相对顺序。 |
| P0-FR-2 | 必须新增 conversation memory 当前预算测试。 | `trim_max_tokens=1024000` 时当前预算断言为 `768000`；测试名说明这是待替换基线。 |
| P0-FR-3 | 必须新增 Skill prompt 安全风险测试。 | 构造包含脚本路径/handler/runtime 字样的 `manifest.body`，证明旧主代理路径会包含它，后续阶段将更新为不得包含。 |
| P0-FR-4 | 必须锁定旧 runtime 字符串调用。 | tests/integrations 覆盖当前 client/runtime 接收 `str` 并转换为单条 user message。 |
| P0-FR-5 | 必须记录验证命令。 | PRD 和后续提交说明包含 targeted unittest 命令与 License Requirement。 |

## 6. 非功能需求

- **Testability**：测试应可在本地 Conda `multi_agent` 中运行，不依赖真实 LLM provider。
- **Safety**：风险测试不得写入真实 secret、DSN 或本地绝对敏感路径。
- **Compatibility**：阶段零不得改变运行时代码行为。

## 7. 实施计划

1. 新增或扩展 `tests/capabilities/main_agent/test_conversation_memory_prompt.py`，覆盖主代理 prompt 顺序。
2. 新增 `tests/orchestration/test_prompt_envelope.py` 或临时 baseline 测试，覆盖静态预算现状。
3. 在 main agent prompt 测试中构造带内部实现片段的 fake Skill manifest，锁定旧风险。
4. 复跑现有 `tests/integrations/test_llm_client.py`、`tests/integrations/test_llm_runtime.py`，确认无 provider 形态变更。
5. 更新阶段零完成记录；不得修改生产 prompt。

## 8. 验收标准

- 新增测试能描述现状，并在后续阶段可被有意更新。
- `git diff --check` 通过。
- targeted tests 通过；默认回归套件不得包含未隔离的红测试，未来行为断言必须以 skip/TODO/后续阶段测试计划呈现。
- 无 runtime 行为变更。
- License Requirement：无依赖/许可变更，未触发 cargo-deny 风险。

## 9. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| 快照测试过度脆弱。 | 只断言关键段落相对顺序和安全文案，不比较整段 prompt。 |
| 未来行为测试被误当作当前失败。 | 默认测试必须绿；未来行为使用 skip/TODO 或后续阶段测试计划，不能混入默认失败套件。 |
| 使用真实内部路径造成泄漏。 | 使用 synthetic `scripts/internal_demo.py` 等假路径。 |
