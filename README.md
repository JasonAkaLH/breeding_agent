# multi_agent_framework

本仓库当前已进入前后端联调阶段：一期“主代理最小内核 + 可移除数据查询 Skill bundle + FastAPI/SSE/cancel/query API”已完成，后续补齐了主代理 Skill 兼容层、主代理真实 LLM runtime 绑定、默认主代理 LLM 编排、运行时受控重排与 smoke 验证；前端 v1 业务对话台已基于现有 API/SSE/artifacts 落地。

## 当前目录结构

| 路径 | 当前用途 |
|---|---|
| `AGENTS.md` | 仓库级 AI Agent 协作、编码、测试与文档约束。 |
| `CHANGELOG.md` | 仓库级变更日志；开始任何分析、设计、编码或文档修改前应先阅读最近条目。 |
| `requirements.txt` | `multi_agent` Conda 环境依赖快照。 |
| `docs/prd/` | PRD 总目录；后端 PRD 在 `docs/prd/backend/`，前端 PRD 在 `docs/prd/frontend/`。 |
| `docs/` 其他文件 | Capability 接入指南、Codex Skill 构建指南、Agent 基础设施优化建议、Skill prompt 模板、架构图与状态流转图；历史阶段文档已收口到 `docs/prd/` 与 `CHANGELOG.md`。 |
| `skill/` | 项目级 Codex Skill 目录；后端默认扫描 `skill/**/SKILL.md`，具体构建约束见 `Codex-Skill构建指南.md`。 |
| `src/api/` | FastAPI app、DTO、SSE、runtime 装配与 API routes。 |
| `src/core/` | 跨模块共享 contract、模型、枚举与基础错误。 |
| `src/storage/` | 状态存储抽象与 SQLite 实现。 |
| `src/lifecycle/` | task / node / mailbox / interrupt / cancel / conversation guard 生命周期规则。 |
| `src/orchestration/` | capability registry、scheduler、workflow plan、主代理 LLM 高层规划、router、validator、expander、运行时受控重编排与编排服务。 |
| `src/capabilities/main_agent/` | `main_agent.respond` 主代理 capability、prompt 构造与 streaming 输出。 |
| `src/integrations/` | LLM client、MySQL readonly adapter、audit logger、Codex Skill 兼容层、MCP Python facade、LLM 上下文 token 计数等外部适配/运行时辅助能力。 |
| `native/` | Rust workspace；当前包含 MCP Runtime sidecar/proto Phase1 骨架，生产 Rust sidecar 后续仍按 PRD phase 门禁推进。 |
| `skill/<domain-query>/` | 可移除 数据查询 Skill bundle，包含 manifest、领域 runtime、配置与 Skill 专属测试；移除该目录后系统只保留 generic Skill loader。 |
| `scripts/` | 显式手工 smoke / 维护脚本，包含主代理真实 LLM smoke 与全栈开发启动脚本。 |
| `tests/` | 后端分层 unittest 回归，包括 core、storage、lifecycle、orchestration、integrations、capabilities、api、e2e、observability。 |
| `frontend/` | React + TypeScript + Vite + Ant Design 前端业务对话台，含 API/SSE client、状态 reducer、通用 data-query / file artifact 渲染与 Vitest 测试。 |

## 当前最小开发基线

- 当前默认开发环境：`conda activate multi_agent`
- 当前已落地的最小测试命令：

```bash
conda run -n multi_agent python -m unittest discover -s tests/core -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/lifecycle -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/main_agent -p 'test_*.py'
# 项目级可移除 Skill 自测：在对应 Skill bundle 目录下运行其 tests/ 目录
(cd skill/<skill-name> && conda run -n multi_agent python -m unittest discover -s tests -p 'test_*.py')
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/e2e -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/observability -p 'test_*.py'
```

- 显式手工 smoke（会访问本地 `config.yaml` 配置的真实 LLM provider，不属于默认回归）：

```bash
conda run -n multi_agent python scripts/smoke_main_agent_llm.py --config config.yaml
```

- 前端 v1 最小验证命令：

```bash
cd frontend
npm test -- --run
npm run build
```

- Rust MCP sidecar/proto skeleton 当前验证命令：

```bash
cd native
cargo fmt --check
cargo test --workspace --all-features
cargo check --workspace --all-targets --all-features
```


- 全栈人工验证脚本（默认拉起仓库真实 FastAPI runtime）：

```bash
python scripts/run_fullstack_dev.py
```

真实 runtime 会在启动期使用本地 `config.yaml` bootstrap 出环境变量，并创建共享的主代理 `SharedLLMRuntime`；默认自动模式下，主代理高层规划、运行时观察/重排与最终回答共享这个主代理 runtime。可移除 Skill bundle 可通过 runtime allowlisted service 复用主代理 `SharedLLMRuntime` 的受控非流式调用；数据查询 Skill 的只读 MySQL 连接与领域配置随 `skill/<domain-query>/` bundle 管理；如需不依赖真实 LLM/MySQL provider、只验证前端交互，可增加 `--fake-backend` 使用 deterministic fake provider/数据库适配器。

运行时配置约定：`config.yaml` 只在 API runtime 启动 / 手工 smoke 初始化时读取一次，并写入 `MAF_CONFIG_*` 进程环境变量；后续 `LLMClient`、Planner、主代理、Skill runtime 与 `trim_max_tokens` 均从环境读取。测试或上层 runtime 仍可通过显式 `config` dict 注入覆盖，不应在业务节点执行阶段重复读取 `config.yaml`。
MySQL 只读连接配置也放在本地 `config.yaml` 的 `mysql_readonly.url`（或部署环境变量 `MAF_MYSQL_READONLY_URL`）中；`config.yaml` 已被 `.gitignore` 忽略，禁止把真实数据库地址、账号或密码写入 tracked 文件。
同一个 API runtime 中的 `*_config_path` 必须指向同一个启动配置文件；默认生产路径使用一个主代理 LLM runtime；Skill 内部 LLM service 只能通过受控 allowlisted adapter 复用该 runtime。显式组件级 `config` dict、client factory 或 fake generator 仍作为测试/定制 seam 保留。
