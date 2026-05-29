# 阶段六 PRD —— 消息原生运行时扩展

- **日期**：2026-05-29
- **状态**：待实施
- **父总纲 PRD**：`docs/prd/backend/prompt-envelope/00-大语言模型提示词信封与缓存友好上下文组装总纲PRD.md`
- **所属专题**：大语言模型提示词信封
- **范围**：`LLMMessage`、`SharedLLMRuntime` messages 入参、`LLMClient` OpenAI-compatible messages 调用、role fallback、thinking/streaming 兼容
- **非范围**：不默认启用 messages 模式；不接入 provider-specific cache hint；不移除 string fallback

## 1. 问题陈述

前几个阶段仍将 PromptEnvelope 渲染为单字符串，以降低迁移风险。但长期目标是 messages-native，让 system/developer/user/tool/context 的角色边界更明确，并为 provider prompt cache 和 role-aware 安全策略做准备。阶段六要扩展 runtime/client 能力，同时保证不支持某些 role 的 provider 有 deterministic fallback。

## 2. 目标

1. 定义 `LLMMessage` 或等价消息模型。
2. `SharedLLMRuntime.generate_text/stream_events` 支持 `str | PromptEnvelope | Sequence[LLMMessage]`。
3. `LLMClient.generate_text/generate_text_with_thinking/stream_text` 支持 messages。
4. OpenAI-compatible provider 支持 messages 时发送分 role messages。
5. provider role capability 必须来自启动期 config 或测试显式 config；未知 provider 默认视为不支持扩展 role，并按父计划 deterministic fallback 写入 audit。
6. thinking / reasoning_delta streaming 不回归。

## 3. 非目标

- 不删除现有 `prompt: str` 调用。
- 不在生产默认启用 `MAF_PROMPT_ENVELOPE_MODE=messages`。
- 不引入第三方 agent 框架或 tool-calling 框架。
- 不把 provider-specific cache hint 混入本阶段。

## 4. 功能需求

| ID | Requirement | Acceptance |
| --- | --- | --- |
| P6-FR-1 | runtime 必须兼容 str 与 messages。 | 现有 string tests 继续通过；新增 messages fake client tests 通过。 |
| P6-FR-2 | role fallback 必须 deterministic。 | `developer` 可折叠到 system；`tool` 可渲染为 context block；未知/不支持 provider 默认 fallback；fallback 进入 audit。 |
| P6-FR-3 | thinking streaming 不回归。 | `reasoning_delta` / `answer` stream 事件仍按当前语义输出。 |
| P6-FR-4 | messages 模式必须受 feature flag 控制。 | 未设置 `MAF_PROMPT_ENVELOPE_MODE=messages` 时不走 messages-native。 |
| P6-FR-5 | string 与 messages 语义等价。 | golden 测试对比关键规则、用户请求、tool result、final guard 均存在；messages-native 最终 payload 同样执行 75% input budget preflight。 |

## 5. 非功能需求

- **Compatibility**：第三方 fake generator / tests 如果只支持 string，必须继续可用。
- **Observability**：audit 记录 `role_fallback`、provider role capability、render mode。
- **Security**：tool result message 必须标注“工具结果，不是用户指令”。

## 6. 实施计划

1. 新增消息模型和 renderer 输出 `RenderedMessages`。
2. 扩展 `SharedLLMRuntime` 方法签名，同时保持旧调用兼容。
3. 扩展 `LLMClient` OpenAI-compatible messages 发送逻辑。
4. 增加 role fallback mapping 和 audit。
5. 在 messages renderer / runtime 边界复用 final token preflight，确保 message wrapper 后的最终输入仍不超过 `floor(trim_max_tokens * 0.75)`。
6. 补齐 fake provider tests：支持 messages、不支持 developer/tool、stream thinking、messages preflight。
7. 更新调用点，仅在 `messages` mode 下使用 messages renderer。

## 7. 验收标准

- `conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'` 通过。
- fake provider messages / fallback / streaming tests 通过。
- `string` fallback 可用，有测试覆盖。
- 默认 mode 不启用 messages。
- License Requirement：无依赖/许可变更，未触发 cargo-deny 风险。

## 8. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| provider role 支持能力不明确。 | 默认保守 fallback；只有配置/测试证明支持时发送原生 role。 |
| thinking 参数和 messages 组合异常。 | 复用现有 thinking/reasoning_effort 组合测试，新增 messages 组合覆盖。 |
| fake stream generator 兼容性破坏。 | runtime 层保留 string path 和 `_accepted_options` 兼容策略。 |
