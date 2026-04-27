# Phase 2 — API / SSE / 状态模型契约层

## 目标

把后端 DTO、SSE event 和 artifacts 解析封装在 UI 之外，降低页面组件对后端内部细节的耦合。

## 主要输出

- `frontend/src/api/types.ts`：SubmitMessage、TaskSummary、Capability、Artifact、SSE event 类型。
- `frontend/src/api/client.ts`：capability list、submit message、cancel task、task/artifacts/graph 查询。
- `frontend/src/api/taskEvents.ts`：EventSource 订阅与可测试的解析入口。
- `frontend/src/domain/taskEvents.ts`：前端状态机 / reducer，映射业务化状态文案。
- `frontend/src/domain/artifacts.ts`：SQLQuery result_summary / query_result_preview 解析与降级。

## 关键规则

- 普通对话提交 `capability_id: null`。
- 数据库查询也提交 `capability_id: null`，由后端自动规划是否调用 SQLQuery；显式 `sql_query.query` 仅保留为兼容/调试入口。
- UI 不允许提交 `sql_query.intent_route` 等 internal capability。
- `main_agent.output_delta` 只追加 delta；最终兜底可从 text artifact 的 `storage_ref` 恢复。
- SQLQuery 表格只使用 `columns` / `rows` / `row_count` / `truncated`，不得展示 `sql` 或 `guard_pass_token`。

## 验收标准

- reducer 单测覆盖 accepted、streaming、completed、failed、cancelled、guard blocked。
- artifact parser 单测覆盖表格 JSON、中性完成提示、summary 降级、坏 JSON 降级、隐藏 SQL 字段。

## 完成记录

- 2026-04-27：本 Phase 已按 PRD v1 完成首轮实现，并通过前端单测/构建或对应脚本验证纳入最终回归。
