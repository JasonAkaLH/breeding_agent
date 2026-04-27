# 前端开发流程总览

本目录用于把 `docs/prd/frontend/00-前端业务对话台PRD.md` 拆解成可执行的前端 Phase 文档。v1 目标是业务用户对话台，而不是研发调试台。

## Phase 总表

| Phase | 主题 | 主要输出 | 状态 |
|---|---|---|---|
| Phase 0 | 冻结 v1 范围、选型与验收边界 | 前端边界、目录约定、验收命令 | 已完成 |
| Phase 1 | 建立 Vite React 工程骨架 | `frontend/` 工程、测试/构建脚本、Vite proxy | 已完成 |
| Phase 2 | API/SSE/状态模型契约层 | TypeScript DTO、API client、SSE event reducer、artifact parser | 已完成 |
| Phase 3 | 业务对话台 UI 主路径 | 页面布局、自动规划模式展示、streaming 气泡、状态条、取消 | 已完成 |
| Phase 4 | SQLQuery 结果卡片与降级 | 主代理回答、简表预览、artifact 降级、隐藏技术细节 | 已完成 |
| Phase 5 | 全栈联调与人工验证脚本 | `scripts/run_fullstack_dev.py`、验收说明、回归证据 | 已完成 |

## 执行顺序

必须按顺序推进：

1. `Phase-0-前端范围选型与验收边界.md`
2. `Phase-1-Vite-React工程骨架.md`
3. `Phase-2-API-SSE状态模型契约层.md`
4. `Phase-3-业务对话台UI主路径.md`
5. `Phase-4-SQLQuery结果卡片与降级.md`
6. `Phase-5-全栈联调与人工验证脚本.md`

## 使用约束

- Phase 文档是开发过程文档，不替代 PRD。
- v1 必须基于当前后端 API/SSE/artifacts 完成，不要求后端新增业务接口。
- 前端默认不展示 DAG、SQL、schema、SQL Guard、LLM fallback、审计日志。
- 如后续扩大范围，应先更新本目录对应 Phase 文档。

## 当前验证命令

```bash
cd frontend && npm test -- --run
cd frontend && npm run build
python -m py_compile scripts/run_fullstack_dev.py
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
```

人工验证入口：`python scripts/run_fullstack_dev.py`。默认拉起仓库真实 FastAPI runtime；如需不依赖真实 LLM/MySQL provider、只做 UI-only 验证，可加 `--fake-backend`。
