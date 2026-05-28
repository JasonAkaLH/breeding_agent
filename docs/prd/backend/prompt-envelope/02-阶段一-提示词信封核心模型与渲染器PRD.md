# 阶段一 PRD —— 提示词信封核心模型与渲染器

- **日期**：2026-05-29
- **状态**：待实施
- **父总纲 PRD**：`docs/prd/backend/prompt-envelope/00-大语言模型提示词信封与缓存友好上下文组装总纲PRD.md`
- **所属专题**：大语言模型提示词信封
- **范围**：`PromptEnvelope`、`PromptSegment`、render audit、segment 排序、动态预算、裁剪策略、prefix hash、字符串渲染核心
- **非范围**：不接入主代理生产路径；不改 conversation memory 生成逻辑；不扩展 LLMClient messages-native

## 1. 问题陈述

当前各调用点自由拼接 prompt，缺少统一 segment 模型、预算计算、裁剪策略和审计结构。阶段一需要先实现与业务调用点解耦的核心能力，作为后续主代理、Planner、Skill resolver 和 runtime 迁移的共同底座。

## 2. 目标

1. 新增 `src/orchestration/prompt_envelope.py` 或等价模块。
2. 定义提示词信封、段落、渲染结果和审计数据模型。
3. 按父计划规定的稳定 prefix、半稳定 profile/schema、中段历史、工具结果、recency 区顺序渲染。
4. 按本次实际非历史 token 反算 bulk history budget。
5. 支持 required / compressible / drop_oldest / drop_if_needed 裁剪策略。
6. 生成不含 raw content 的审计信息。

## 3. 非目标

- 不改变 `build_main_agent_prompt` 返回值。
- 不写 API/SSE event。
- 不做 provider tokenization 网络调用；只接入现有 token counter seam 或可注入 estimator。
- 不支持 messages-native role fallback；该能力属于阶段六。

## 4. 功能需求

| ID | Requirement | Acceptance |
| --- | --- | --- |
| P1-FR-1 | 必须定义不可变或低副作用的数据模型。 | `PromptSegment`、`PromptEnvelope`、`PromptSegmentAudit`、`PromptRenderAudit`、`RenderedPrompt` 可被单元测试 import。 |
| P1-FR-2 | 必须实现 deterministic segment order。 | 同一组 segments 输入顺序不同，输出顺序一致。 |
| P1-FR-3 | 必须实现动态历史预算。 | `bulk_history_budget = trim_max_tokens - required_non_history_tokens - safety_margin`；不再固定 75%。 |
| P1-FR-4 | 必须实现 required 超限 fail closed。 | 必保 segment 超预算时抛出明确异常或返回 fail-closed 结果，不截断必保内容。 |
| P1-FR-5 | 必须实现可压缩/可丢弃裁剪。 | flexible history segment 可按策略裁剪，并在 audit 中记录 tokens_before/after、trim_reason。 |
| P1-FR-6 | 必须实现 cacheable prefix hash。 | 只覆盖 `cache_affinity=prefix` 且 `mutability=stable` 的 segment。 |
| P1-FR-7 | audit 不得记录 raw prompt。 | 单元测试递归扫描 audit dict，不包含 segment content / raw prompt / artifact content。 |

## 5. 非功能需求

- **Security**：audit 中不得出现 secret、DSN、token、内部路径或 raw prompt。
- **Performance**：renderer 应线性处理 segments，避免对长历史做重复全量 JSON dump。
- **Maintainability**：核心模块不得依赖 FastAPI、具体 provider、Skill executor 或 storage repository。
- **Determinism**：相同输入必须产生相同 rendered prompt、prefix hash 和 audit。

## 6. 实施计划

1. 新增 `src/orchestration/prompt_envelope.py`。
2. 新增 `tests/orchestration/test_prompt_envelope.py`。
3. 先写 segment order、dynamic budget、fail-closed、audit no-raw、prefix hash red/green 测试。
4. 实现数据模型和 renderer。
5. 为 token counter fallback 增加更大 safety margin 测试，例如 fallback 时 margin 至少为 `max(2048, trim_max_tokens * 0.02)` 或实施时确认的等价规则。
6. 保持所有业务调用点不接入，降低阶段风险。

## 7. 验收标准

- `conda run -n multi_agent python -m unittest tests.orchestration.test_prompt_envelope` 通过。
- 新模块无 FastAPI / storage / provider 依赖。
- audit 对 raw content 的禁止有自动化测试。
- `git diff --check` 通过。
- License Requirement：无依赖/许可变更，未触发 cargo-deny 风险。

## 8. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| token 估算误差导致后续 provider 拒绝。 | 阶段一只定义 estimator seam 和 fallback margin；真实 provider 优化放后续阶段。 |
| 裁剪策略过早复杂化。 | 先实现父计划列出的四种策略；新策略必须有测试和使用场景。 |
| audit hash 无法排查问题。 | audit 保留 segment name、tokens、trim reason、content_hash；禁止 raw content 但允许 hash 对比。 |
