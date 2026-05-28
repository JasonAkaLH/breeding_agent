# 大语言模型提示词信封分步 PRD 索引

- **日期**：2026-05-29
- **状态**：待实施
- **父实施计划**：`docs/orchestration/大语言模型提示词信封与缓存友好上下文组装实施计划.md`
- **设计来源**：`docs/superpowers/specs/2026-05-28-llm-prompt-envelope-cache-aware-design.md`
- **总目标**：把主代理、Planner、Runtime Replanner、Soft Skill、Skill input resolver、conversation memory 与 LLM runtime 的 prompt 组装升级为结构化、可审计、缓存友好、可灰度迁移的提示词信封子系统。

## 拆分原则

1. 父实施计划保留总体架构、运行模式和跨阶段验收矩阵；本目录按实施步骤拆成可独立开发、验收、回滚的阶段 PRD。
2. 每个阶段都必须先补测试，再实现；跨阶段共享的不变量以父计划为准。
3. 阶段零至阶段五默认不改变 LLM provider 调用形态，除明确启用 `MAF_PROMPT_ENVELOPE_MODE=string` 的路径外，必须保留 `off` / `shadow` 回滚能力。
4. 阶段六才允许启用 messages-native runtime；阶段七才允许 provider-specific cache hint。
5. 本专题默认不做数据库 schema 迁移；prompt audit 通过现有 audit-only event payload 扩展承载，且不得记录 raw prompt、raw artifact、secret、DSN 或内部路径。

## 阶段文件

| 阶段 | PRD | 目标 |
| --- | --- | --- |
| 阶段零 | `01-阶段零-测试基线与旧行为锁定PRD.md` | 锁定现有 prompt 顺序、静态历史预算、Skill 内部结构暴露风险和旧路径回归。 |
| 阶段一 | `02-阶段一-提示词信封核心模型与渲染器PRD.md` | 新增 PromptEnvelope / PromptSegment / RenderAudit 核心模型、排序、动态预算、裁剪和审计。 |
| 阶段二 | `03-阶段二-主代理信封字符串迁移PRD.md` | 主代理接入 envelope-to-string，支持 `off|shadow|string`，保持 LLM runtime 字符串兼容。 |
| 阶段三 | `04-阶段三-对话记忆候选上下文化PRD.md` | conversation memory 从最终预算决策者变为候选上下文提供者，由 PromptAssembler 反算历史预算。 |
| 阶段四 | `05-阶段四-工具信息分层与能力公开档案安全PRD.md` | 拆分工具规则/profile/schema/result，复用 public Skill profile，禁止内部结构进入 prompt。 |
| 阶段五 | `06-阶段五-多调用场景档案迁移PRD.md` | 迁移 Soft Skill decision/answer、Planner/repair、Runtime Replanner、Skill resolver、memory resolver/summary。 |
| 阶段六 | `07-阶段六-消息原生运行时扩展PRD.md` | 扩展 SharedLLMRuntime / LLMClient 支持 messages-native 与 role fallback audit。 |
| 阶段七 | `08-阶段七-供应商缓存与观测增强PRD.md` | 稳定 cacheable prefix hash、动态污染检测、provider cache hint 与 prompt audit 指标。 |

## 跨阶段不变量

- `MAF_PROMPT_ENVELOPE_MODE=off` 必须始终可回滚到旧 prompt 路径。
- `shadow` 模式只允许增加 audit，不得改变实际发送给 LLM 的 prompt。
- 稳定 system / tool rules 是 cacheable prefix；task_id、conversation_id、username、当前用户问题、history、artifact、dependency result 不得进入 stable prefix。
- 历史预算必须按 `trim_max_tokens - required_non_history_tokens - safety_margin` 反算，不得继续把 `trim_max_tokens * 0.75` 当最终历史预算。
- 必保 segment 超限必须 fail closed，不得截断系统规则、工具规则、当前用户请求或最终安全 guard。
- Skill public profile 只允许使用用户可见信息；不得暴露脚本路径、handler、runtime、sidecar、内部目录、配置、DSN、token 或 secret。
- Prompt audit 只记录 hash、token、segment name、trim reason、fallback reason 和 role fallback；不得记录 raw prompt 或 raw artifact content。
- 前端 SSE / completion 语义保持兼容：`main_agent.output_delta`、`main_agent.output_final` 不因 audit 扩展而改变。
