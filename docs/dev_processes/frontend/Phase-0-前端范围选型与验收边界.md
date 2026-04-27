# Phase 0 — 前端范围、选型与验收边界

## 输入

- `docs/prd/frontend/00-前端业务对话台PRD.md`
- 当前后端 API 与 DTO：`src/api/dto.py`、`src/api/routes/*.py`
- 当前后端 capability：`main_agent.respond`、`sql_query.query`

## 目标

1. 冻结 v1 是内部业务用户对话台，不是研发/运维调试台。
2. 确认技术栈：React + TypeScript + Vite + Ant Design + EventSource/SSE。
3. 定义前端工程目录、自动化验收命令和人工验证入口。
4. 明确不得暴露 internal SQLQuery capability、SQL、DAG、schema、审计日志。

## 实施范围

- 新增 `frontend/` 独立 SPA 工程。
- 前端 API 基础路径默认使用相对 `/api`，开发态通过 Vite proxy 转发到 FastAPI。
- 不要求后端新增业务接口；如独立端口存在跨域问题，优先用 Vite proxy 规避。
- 本地持久化仅保存 `conversation_id`、可选最近任务摘要与配置默认值。

## 非目标

- 登录 / RBAC / 服务端会话列表。
- 文件上传、文件管理、研发调试台。
- WebSocket、Next.js、Redux/Zustand、Vercel AI SDK UI。

## 验收标准

- Phase 1~5 文档均存在，并与本 Phase 的边界一致。
- `.omx/plans/prd-20260427-frontend-dialog-console.md` 与 `.omx/plans/test-spec-20260427-frontend-dialog-console.md` 存在，满足 Ralph 规划门禁。
- 后续每个 Phase 都能给出独立验证命令或人工验收点。

## 完成记录

- 2026-04-27：本 Phase 已按 PRD v1 完成首轮实现，并通过前端单测/构建或对应脚本验证纳入最终回归。
