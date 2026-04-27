# Phase 5 — 全栈联调与人工验证脚本

## 目标

提供一个脚本拉起完整前后端，支持人工验证 PRD v1 的主路径。

## 主要输出

- `scripts/run_fullstack_dev.py`
  - 启动 FastAPI/uvicorn 后端。
  - 检查并按需安装前端依赖。
  - 启动 Vite 前端开发服务器。
  - 打印前端 URL、后端 URL、Ctrl+C 退出说明。
  - 优雅终止子进程。

## 默认端口

- 后端：`127.0.0.1:8000`
- 前端：`127.0.0.1:5173`
- 前端 `/api` proxy 默认转发到 `http://127.0.0.1:8000`
- 脚本默认使用仓库真实 FastAPI runtime；`--fake-backend` 可切换到 deterministic fake LLM/MySQL provider 以便 UI-only 验证。

## 验收命令

```bash
python -m py_compile scripts/run_fullstack_dev.py
cd frontend && npm test -- --run
cd frontend && npm run build
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
```

## 人工验收路径

1. `python scripts/run_fullstack_dev.py`
2. 打开 `http://127.0.0.1:5173`
3. 普通对话模式提交问题并观察 streaming。
4. 数据库查询模式提交 SQLQuery 问题并观察摘要 + 简表。
5. 运行中点击取消并观察状态。
6. 确认默认界面不展示 SQL / DAG / schema / audit。

## 完成记录

- 2026-04-27：本 Phase 已按 PRD v1 完成首轮实现，并通过前端单测/构建或对应脚本验证纳入最终回归。
