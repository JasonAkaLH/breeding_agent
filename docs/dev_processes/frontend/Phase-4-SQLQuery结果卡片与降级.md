# Phase 4 — SQLQuery 结果卡片与降级

## 目标

让数据库查询模式在任务完成后展示面向业务用户的“答案 + 结果预览”，同时隐藏调试细节。

## 展示内容

- 摘要：优先使用 `result_summary` artifact 的 `summary` 或 `storage_ref.summary`。
- 结果规模：使用 `row_count`。
- 简表预览：使用 `query_result_preview` artifact 的 `columns` 与 `rows`。
- 截断提示：使用 `truncated` 或 preview 数量推断，不强依赖字段存在。

## 隐藏内容

- `sql`、`guard_pass_token`、schema、SQL Guard report、DAG/node id、审计事件。
- 任何内部 capability id 不作为用户可选项或默认展示文案。

## 降级策略

- 无摘要：显示“查询已完成，但摘要不可用”。
- 无表格：仅显示自然语言摘要。
- JSON 解析失败：展示 artifact `summary`，并提示结果预览暂不可用。
- artifacts API 失败：任务完成状态保留，显示“结果加载失败，可重试”。

## 验收标准

- parser 测试验证 SQL 与 guard token 不进入展示模型。
- UI 测试验证 SQLQuery 完成后可渲染摘要和表格。

## 完成记录

- 2026-04-27：本 Phase 已按 PRD v1 完成首轮实现，并通过前端单测/构建或对应脚本验证纳入最终回归。
