# 阶段七 PRD —— 供应商缓存与观测增强

- **日期**：2026-05-29
- **状态**：已实施（2026-05-29）
- **父总纲 PRD**：`docs/prd/backend/prompt-envelope/00-大语言模型提示词信封与缓存友好上下文组装总纲PRD.md`
- **所属专题**：大语言模型提示词信封
- **范围**：cacheable prefix hash、prefix 动态污染检测、provider cache hint 配置、prompt render metrics、audit-only 观测
- **非范围**：不依赖单一 provider 专有能力；不记录 raw prompt；不改变业务回答语义

## 1. 问题陈述

PromptEnvelope 的结构化 segment 能稳定 system/tool prefix，但只有持续观测 prefix hash、prefix token 和动态污染，才能验证 KV Cache 命中是否稳定。不同 provider 的 prompt cache 能力差异很大，因此阶段七要先把 provider cache 作为可配置增强，而不是把实现绑死到某个 vendor。

## 2. 目标

1. audit 记录 `cacheable_prefix_hash`、`cacheable_prefix_tokens`、`first_dynamic_segment`。
2. 增加 prefix 动态污染检测，禁止 task_id、conversation_id、username、current user、artifact、dependency result 进入 stable prefix。
3. provider 支持 prompt cache hint 时，通过配置启用；不支持时只保留 hash 观测。
4. 增加 prompt render metrics / audit-only event，不记录 raw prompt。
5. 为生产灰度提供对比字段：mode、template_version、prefix hash、final input budget、final input tokens、history budget、history compression retry、trim reason、role fallback。

## 3. 非目标

- 不承诺所有 provider 都有真实 KV Cache 命中。
- 不为了 cache 命中牺牲安全排序或当前用户 recency。
- 不把 cache metadata 写入前端可见事件。
- 不引入新的数据库 schema，除非后续单独 PRD 批准。

## 4. 功能需求

| ID | Requirement | Acceptance |
| --- | --- | --- |
| P7-FR-1 | prefix hash 必须稳定。 | 同模板同 stable segments，用户请求/history/tool result 变化不改变 prefix hash。 |
| P7-FR-2 | 动态污染必须被检测。 | stable prefix segment 中出现 task_id/conversation_id/current user/artifact 等字段时测试 fail closed。 |
| P7-FR-3 | provider cache hint 必须配置控制。 | 默认关闭；启用时只对支持 provider 生效，不支持 provider 记录 no-op。 |
| P7-FR-4 | metrics/audit 不得含 raw prompt。 | audit-only payload 扫描不含 raw content、secret、DSN、token。 |
| P7-FR-5 | 灰度观测字段完整。 | audit 可回答 prefix hash 是否变化、最终输入是否在 75% 预算内、是否触发唯一一次 history compression retry、history 裁剪多少、role fallback 是否发生。 |

## 5. 非功能需求

- **Security**：cache key / hash 不可反推出 prompt 原文。
- **Operability**：运维可按 template_version、mode、provider、prefix hash 聚合观察。
- **Compatibility**：不支持 cache hint 的 provider 不受影响。

## 6. 实施计划

1. 扩展 `PromptRenderAudit` 字段和序列化测试。
2. 增加 prefix pollution detector，并在 P1/P2/P5 profiles 中接入。
3. 在 LLM runtime/client 配置中增加 provider cache capability 与 hint 开关。
4. 为支持/不支持 provider 分别加 fake tests。
5. 增加 audit-only event 或 metrics payload，不改变前端 SSE。
6. 编写 rollout 建议：shadow 观察 -> string 小流量 -> messages 小流量 -> provider cache hint 小流量。

## 7. 验收标准

- prefix hash determinism tests 通过。
- prompt render metrics 包含 `final_input_token_budget`、`final_input_tokens`、`preflight_retry_count` 和 `history_compression_retry`。
- 当前用户、history、tool result 变化不影响 stable prefix hash。
- 动态字段污染 stable prefix 时 fail closed。
- audit payload 不含 raw prompt / secret / DSN / token。
- License Requirement：无依赖/许可变更，未触发 cargo-deny 风险。

## 8. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| provider cache hint 行为不透明。 | 不把 cache 命中作为 correctness 依赖，只做观测优化。 |
| hash 粒度过粗无法排查。 | 保留 segment-level content_hash 和 prefix hash，但不记录 raw content。 |
| 为追求 prefix 稳定把动态信息提前。 | pollution detector 和 segment order tests 防止动态信息进入 stable prefix。 |

## 9. 灰度与回滚建议

1. **Shadow 观察**：先保持 `MAF_PROMPT_ENVELOPE_MODE=shadow`，只读取 audit-only 的 `prompt_render_metrics`、`cacheable_prefix_hash`、`final_input_token_budget`、`final_input_tokens`、`history_compression_retry` 与 `trim_reasons`；前端 SSE 不展示这些事件。
2. **String 小流量**：在 prefix hash 稳定且 `final_input_tokens <= final_input_token_budget` 持续成立后，将少量主代理流量切到 `string`；若出现 `stable_prefix_dynamic_pollution` 或 `final_input_over_budget`，立即回滚到 `shadow/off`。
3. **Messages 小流量**：provider role fallback 观测稳定后再切 `messages`，重点观察 `role_fallbacks` / `role_fallback_count` 是否符合 provider capabilities。
4. **Provider cache hint 小流量**：仅当配置声明 `supports_prompt_cache=true` 且显式开启 `prompt_cache_hint_enabled=true` 时启用；unsupported/default disabled provider 必须 no-op，仅保留 hash/metrics 观测。
5. **观测聚合口径**：按 `mode`、`effective_mode`、`template_version`、provider/model、`cacheable_prefix_hash`、`first_dynamic_segment` 聚合，对比 prefix hash 波动、history retry 比例、trim reason 分布与 role fallback 发生率。
6. **安全回滚口径**：任何 audit payload 扫描发现 raw prompt、secret、DSN、token、provider `base_url` / `api_key`，或 stable prefix 污染检测失败未 fail-closed，均停止 provider cache hint 放量并回滚到上一阶段。
