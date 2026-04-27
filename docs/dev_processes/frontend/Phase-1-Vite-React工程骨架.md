# Phase 1 — Vite React 工程骨架

## 目标

建立最小但完整的前端工程，使后续业务代码可以通过 TypeScript、单元测试和生产构建验证。

## 主要输出

- `frontend/package.json`：scripts 至少包含 `dev`、`build`、`test`、`preview`。
- `frontend/vite.config.ts`：React 插件、Vitest jsdom、开发态 `/api` proxy。
- `frontend/tsconfig*.json`：严格 TypeScript 配置。
- `frontend/src/main.tsx`、`frontend/src/App.tsx`、基础样式。
- `frontend/src/test/setup.ts`：React Testing Library / jest-dom 测试环境。

## 依赖选择

- 运行依赖：`react`、`react-dom`、`antd`。
- 开发依赖：`@vitejs/plugin-react`、`vite`、`typescript`、`vitest`、`jsdom`、`@testing-library/react`、`@testing-library/jest-dom`。
- 不引入全局状态库和 request 库，API 使用 typed `fetch` 封装。

## 验收标准

- `cd frontend && npm test -- --run` 可执行。
- `cd frontend && npm run build` 可执行。
- Vite dev server 能把 `/api/*` 转发到 `VITE_API_PROXY_TARGET` 或默认 `http://127.0.0.1:8000`。

## 完成记录

- 2026-04-27：本 Phase 已按 PRD v1 完成首轮实现，并通过前端单测/构建或对应脚本验证纳入最终回归。
