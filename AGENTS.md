# Repository Guidelines

## 项目结构与模块组织
当前仓库已进入前后端联调阶段，不再是空白/最小占位仓库。一期主代理内核、SQLQuery MVP、API/SSE、状态存储、后续 LLM/Skill 增强以及前端 v1 业务对话台均已有实际代码与测试。

当前主要目录职责如下：
- `src/api/`：FastAPI app、DTO、SSE、runtime 装配与 API routes。
- `src/core/`：跨模块共享 contract、模型、枚举与基础错误；不得放 capability 专属业务语义。
- `src/storage/`：状态存储抽象与 SQLite 实现；后续 PostgreSQL 应保持逻辑同构迁移。
- `src/lifecycle/`：task / node / mailbox / interrupt / cancel / conversation guard 生命周期规则。
- `src/orchestration/`：capability registry、scheduler、workflow plan、LLM planner、router、validator、expander 与编排服务。
- `src/capabilities/main_agent/`：`main_agent.respond` 主代理 capability、prompt 构造与 streaming 输出。
- `src/capabilities/sql_query/`：SQLQuery public macro 与内部六节点只读查询 workflow；尾节点通过 LLM / 降级路径筛选 LIKE 召回的候选表格，同时保留原始表格 preview。
- `src/integrations/`：LLM client、MySQL readonly adapter、audit logger、Codex Skill 兼容层、LLM 上下文 token 计数等外部适配 / 运行时辅助能力。
- `src/sql_query/`：SQLQuery schema context builder 与领域模型；作为 capability 层复用资产保留。
- `configs/sql_query/`：SQLQuery routing rules、schema metadata、SQL Guard rules。
- `frontend/`：React + TypeScript + Vite + Ant Design 前端业务对话台；包含 API/SSE client、状态 reducer、SQLQuery 结果卡片与 Vitest 测试。
- `tests/`：后端按 `core`、`storage`、`lifecycle`、`orchestration`、`integrations`、`capabilities`、`api`、`e2e`、`observability` 分层组织回归测试。
- `docs/prd/`：PRD 总目录；后端 PRD 在 `docs/prd/backend/`，前端 PRD 在 `docs/prd/frontend/`。
- `docs/dev_processes/`：开发流程文档总目录；后端 Phase 文档在 `docs/dev_processes/backend/`，前端 Phase 文档在 `docs/dev_processes/frontend/`。
- `scripts/`：显式手工 smoke / 维护脚本；真实 provider smoke 不属于默认自动化回归；`run_fullstack_dev.py` 可拉起前后端用于人工验证。

仍需遵守：不要提交空目录、空测试或占位实现；新增 `native/`、`cpp/` 或其他大型目录前必须有明确设计/评审依据。

## 构建、测试与开发命令
仓库当前仍采用分层 `unittest` 作为正式回归入口，暂未引入统一的全项目 pytest / lint / build 命令；不要自行假定超出当前阶段的标准命令。

当前已落地的最小测试命令：

```bash
conda run -n multi_agent python -m unittest discover -s tests/core -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/lifecycle -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/main_agent -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/sql_query -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/e2e -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/observability -p 'test_*.py'
```

显式手工 smoke（会访问本地 `config.yaml` 配置的真实 LLM provider，不属于默认回归）：

```bash
conda run -n multi_agent python scripts/smoke_main_agent_llm.py --config config.yaml
```

前端 v1 最小验证命令：

```bash
cd frontend
npm test -- --run
npm run build
```

全栈人工验证脚本（默认拉起仓库真实 FastAPI runtime，会在启动期使用本地 `config.yaml` bootstrap 出环境变量来装配主代理、LLM Planner 与 SQLQuery 内部 LLM；需要 UI-only 验证时可加 `--fake-backend`）：

```bash
python scripts/run_fullstack_dev.py
```

运行时配置约定：`config.yaml` 只在 API runtime 启动 / 手工 smoke 初始化时读取一次，并写入 `MAF_CONFIG_*` 进程环境变量；后续 `LLMClient`、Planner、主代理、SQLQuery 与 `trim_max_tokens` 均从环境读取。测试或上层 runtime 可用显式 `config` dict 注入覆盖，不要在业务节点执行阶段重复读取 `config.yaml`。
同一个 runtime 中的 `*_config_path` 必须指向同一个启动配置文件；如确需为不同组件使用不同 provider，请使用显式 `config` dict 或 client factory 注入，避免多个 YAML 文件竞争同一环境变量命名空间。

如果某次变更引入了新工具，请在同一个 PR 中同步更新 `README.md` 与本文件。未来可能出现的命令示例：

```bash
python -m pytest
ruff check .
cmake -S . -B build
cmake --build build
```

除上述最小测试命令外，其余仍仅为示例，不代表当前仓库标准。

## 开发环境
本项目统一在 Conda 环境 `multi_agent` 中开发，当前确认的 Python 版本为 `3.13.13`。

常用命令示例：

```bash
conda activate multi_agent
python --version
```

如需补充依赖、脚本或工具链，请默认以该环境为基准，不要混用其他本地 Python 环境。

根目录中的 `requirements.txt` 视为当前环境依赖快照。写代码前应先查看 `requirements.txt`，确认当前可用依赖包；实现功能时应尽量基于现有依赖完成。如果确实需要新增依赖，应在 `multi_agent` 环境中安装，并同步更新 `requirements.txt`。

按当前项目约定，推送到远端仓库时**不要求主动抹去敏感信息**。如 MySQL 数据库用户名、地址、LLM 的 API Key 等信息，默认可按仓库当前内容保留；只有在用户明确要求脱敏、替换或迁移到配置管理时，才执行相关处理。

根目录 `CHANGELOG.md` 是仓库级开发记录入口。每天开发工作结束时，应把当天工作内容写入 `CHANGELOG.md`。
开始任何分析、设计、编码或文档修改工作前，也应先阅读 `CHANGELOG.md` 的最近相关条目，了解此前已经完成的工作与当前上下文，避免重复判断或偏离既有结论。

## 异步开发约束
本项目的业务逻辑默认采用 **async / await** 方式实现，整体运行模型以异步为基础。

- 新增业务流程、服务编排、I/O 调用时，优先设计为异步接口与异步执行链路。
- 除非存在明确且充分的理由，不要把核心业务逻辑写成同步阻塞模型。
- 如果必须引入同步代码，需要说明原因，并评估是否会阻塞事件循环或破坏并发能力。

## 执行边界与改动前置要求
- 聚焦用户当前直接请求，不做无关范围扩张；但实现标准必须面向长期交付，不接受“临时可跑、最小糊上、后面再说”的实现。
- 功能、修复与重构都应形成逻辑闭环：入口、状态流转、错误处理、权限/并发边界、持久化影响、前后端契约和验证证据需要相互一致。
- 优先交付稳健、可维护、可演进的代码：复用既有边界与工具，避免重复逻辑、散落特判、隐式状态、吞异常、未验证 fallback 和只服务当前样例的硬编码。
- 控制改动范围与长期质量不是冲突关系：不要无证据过度设计或引入大框架，但当前范围内的抽象、命名、测试与文档应达到后续维护者可接手的标准。
- 在提出修改建议或实际动手改代码前，先阅读与任务直接相关的代码、配置和文档，确保判断基于当前仓库事实，而不是凭印象操作。

## 编码风格与命名规范
- Python 使用 4 个空格缩进；文件、函数、变量用 `snake_case`；类名用 `PascalCase`。
- C++ 模块需明确区分头文件与实现文件，风格以后续批准的首个原生模块规范为准。
- Markdown 文档文件可使用中文命名。
- 除 Markdown 文档外，其他文件与目录应使用英文命名；代码、配置、脚本、YAML/JSON 等文件不要使用中文文件名。
- 优先边界清晰、职责单一、长期可维护的模块；避免无证据的提前泛化，也避免在已有稳定复用需求时继续复制粘贴。
- 未经明确同意，不要引入 LangChain、LangGraph、AutoGen 等现成 Agent 框架。

## 测试规范
不要添加空测试或仅作占位的测试目录。只有在行为、接口或约束明确后再补测试。

项目开发应**严格遵循 TDD（测试驱动开发）**：
- 先写或先明确失败测试，再写实现代码。
- 功能改动、缺陷修复、规则变更都应优先补测试或先落验收测试。
- 未有测试依据时，不应直接进入大规模实现；至少先补足够支撑长期维护的可验证测试面或测试计划。

推荐命名：
- Python：`tests/test_<feature>.py`
- C++：按模块名对应测试文件

任何引入运行时行为的 PR，都应附带验证步骤或测试说明。

## 提交与 Pull Request 规范
现有提交历史采用**简短、祈使句、意图优先**的标题，例如：
- `Keep the repository blank until the architecture is decided`
- `Establish the initial service-first repository baseline`

非 trivial 提交建议补充正文，说明约束、备选方案、风险范围和验证结果。

PR 至少应包含：
- 变更目的
- 对设计或目录结构的影响
- 关联 issue / 决策记录（如有）
- 已完成的验证

## 架构约束
本仓库服务于自研、多 Agent、面向内部业务的框架建设。当前后端主代理框架、SQLQuery、状态存储、API/SSE、主代理 Skill / LLM runtime 已形成实现基线；新增重大运行时、部署方式、编排模型或跨模块边界变更前，应先更新对应 PRD / dev_processes 设计与测试计划。
