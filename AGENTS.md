# Repository Guidelines

## 项目结构与模块组织
当前仓库已进入前后端联调阶段，不再是空白/最小占位仓库。一期主代理内核、可移除 数据查询 Skill bundle、API/SSE、状态存储、后续 LLM/Skill 增强以及前端 v1 业务对话台均已有实际代码与测试。

当前主要目录职责如下：
- `src/api/`：FastAPI app、DTO、SSE、runtime 装配与 API routes。
- `src/core/`：跨模块共享 contract、模型、枚举与基础错误；不得放 capability 专属业务语义。
- `src/storage/`：状态存储抽象与 SQLite 实现；后续 PostgreSQL 应保持逻辑同构迁移。
- `src/lifecycle/`：task / node / mailbox / interrupt / cancel / conversation guard 生命周期规则。
- `src/orchestration/`：capability registry、scheduler、workflow plan、LLM planner、router、validator、expander 与编排服务。
- `src/capabilities/main_agent/`：`main_agent.respond` 主代理 capability、prompt 构造与 streaming 输出。
- `src/integrations/`：LLM client、MySQL readonly adapter、audit logger、Codex Skill 兼容层、LLM 上下文 token 计数等外部适配 / 运行时辅助能力。
- `skill/<domain-query>/`：可移除 数据查询 Skill bundle；manifest、领域 runtime、配置与 Skill 专属测试物理归属此目录，runtime 只通过 generic Skill loader / allowlisted platform-service handler 接入。
- `frontend/`：React + TypeScript + Vite + Ant Design 前端业务对话台；包含 API/SSE client、状态 reducer、通用 data-query / file artifact 渲染与 Vitest 测试。
- `native/`：Rust workspace；当前承载 runtime contract/kernel crates、Core/Lifecycle PyO3 facade crate、Artifact/Auth/DataAccess/Audit safety PyO3 facade crate、RuntimeSidecar service kernel + tonic/prost gRPC binding + `maf-runtime-sidecar` 二进制入口、RuntimeSidecar SQLite durable adapter、Skill Runtime policy / SkillSandboxService + `maf-skill-sandbox` 二进制入口、MCP Runtime Phase 0/1 sidecar contract/kernel + `maf-mcp-runtime-sidecar` 二进制入口、loopback/Unix socket/mTLS sidecar transport 与 sidecar proto；production provenance / enforce / Phase 2-5 canonical operations / legacy 下线仍需按 Rust PRD 门禁推进。
- `tests/`：后端按 `core`、`storage`、`lifecycle`、`orchestration`、`integrations`、`capabilities`、`api`、`e2e`、`observability` 分层组织回归测试。
- `docs/prd/`：PRD 总目录；后端 PRD 在 `docs/prd/backend/`，前端 PRD 在 `docs/prd/frontend/`。
- `scripts/`：显式手工 smoke / 维护脚本；真实 provider smoke 不属于默认自动化回归；`run_fullstack_dev.py` 可拉起前后端用于人工验证。
- `skill/`：项目级 Codex Skill 目录；后端默认扫描 `skill/**/SKILL.md`，不要再把项目共享 Skill 放在 `.codex/skills/`。

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
# 项目级可移除 Skill 自测：在对应 Skill bundle 目录下运行其 tests/ 目录
(cd skill/<skill-name> && conda run -n multi_agent python -m unittest discover -s tests -p 'test_*.py')
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

Docker Compose 打包 / 启动命令（会把本地 git-ignored `config.yaml` 复制进 backend 镜像；该文件含敏感配置时只在受控环境构建 / 分发镜像）：

```bash
docker compose build
docker compose up
```

当前 Compose 构建两个 `linux/amd64` 本地镜像：`breeding-agent-backend:local` 使用 Ubuntu 22.04 + Conda Python 3.13.13 运行 FastAPI；`breeding-agent-frontend:local` 使用 Ubuntu 22.04 + nginx 服务 Vite build 产物并反代 `/api/` 与 `/api-doc`。`.dockerignore` 必须继续排除 `tests/`、根目录 Markdown 文档与 `docs/` 中除 `docs/api/` 外的文档。

- Rust runtime contract/kernel workspace 当前验证命令：

```bash
conda run -n multi_agent python scripts/run_rust_quality_gates.py --run --only cargo_fmt --only cargo_test --only rust_coverage_thresholds --skip-unavailable
cd native
cargo fmt --check
cargo test --workspace --all-features
cargo check --workspace --all-targets --all-features
```

Skill Runtime PyO3 wheel 本地 smoke（非默认回归；产物仍需 CI / 部署 provenance 后才能进入生产 allowlist；生产目标 wheel 必须在 Ubuntu 22.04 x86_64 / Python 3.13 上生成 `manylinux_2_35` 产物，本机 macOS wheel 仅限开发验证）：

```bash
CARGO_BUILD_JOBS=1 conda run -n multi_agent python -m maturin build --release --locked --manifest-path native/crates/maf_skill_runtime_pyo3/Cargo.toml --interpreter /opt/miniconda3/envs/multi_agent/bin/python --compatibility manylinux_2_35 --auditwheel check --out native/target/wheels
conda run -n multi_agent python -m pip install --force-reinstall --no-deps native/target/wheels/maf_skill_runtime_pyo3-*.whl
conda run -n multi_agent python -m unittest tests.integrations.codex_skills.test_pyo3_wheel_build_contract.SkillRuntimePyo3WheelBuildContractTest.test_installed_pyo3_module_matches_rust_contract_when_available
conda run -n multi_agent python scripts/rust_artifact_provenance.py self-test
```

Core/Lifecycle PyO3 wheel 本地 smoke（非默认回归；与 Skill Runtime 使用同一 Ubuntu 22.04 x86_64 / Python 3.13 / `manylinux_2_35` CI wheel 目标）：

```bash
CARGO_BUILD_JOBS=1 conda run -n multi_agent python -m maturin build --release --locked --manifest-path native/crates/maf_core_lifecycle_pyo3/Cargo.toml --interpreter /opt/miniconda3/envs/multi_agent/bin/python --compatibility manylinux_2_35 --auditwheel check --out native/target/wheels
conda run -n multi_agent python -m pip install --force-reinstall --no-deps native/target/wheels/maf_core_lifecycle_pyo3-*.whl
conda run -n multi_agent python -m unittest tests.integrations.test_core_lifecycle_pyo3_wheel_contract.CoreLifecyclePyo3WheelContractTest.test_installed_pyo3_module_matches_core_lifecycle_contract_when_available
```

Safety Kernels PyO3 wheel 本地 smoke（PRD06 Artifact/Auth/DataAccess/Audit，非默认回归；与上述 PyO3 wheel 使用同一 Ubuntu 22.04 x86_64 / Python 3.13 / `manylinux_2_35` CI wheel 目标）：

```bash
CARGO_BUILD_JOBS=1 conda run -n multi_agent python -m maturin build --release --locked --manifest-path native/crates/maf_safety_kernels_pyo3/Cargo.toml --interpreter /opt/miniconda3/envs/multi_agent/bin/python --compatibility manylinux_2_35 --auditwheel check --out native/target/wheels
conda run -n multi_agent python -m pip install --force-reinstall --no-deps native/target/wheels/maf_safety_kernels_pyo3-*.whl
conda run -n multi_agent python -m unittest tests.integrations.test_safety_kernels_pyo3_wheel_contract.SafetyKernelsPyo3WheelContractTest.test_installed_pyo3_module_matches_safety_contract_when_available
python scripts/validate_prd06_safety_kernel_evidence.py --allow-pending --json
# PRD07 orchestration/hotspot 条件候选 guard（当前应返回 guarded；不启动 Rust 化）
python scripts/validate_prd07_orchestration_hotspot_evidence.py --json
```

Core/Lifecycle PyO3 runtime 约定：`MAF_RUST_CORE_MODE` / `MAF_RUST_LIFECYCLE_MODE` 控制 `off|shadow|enforce`，默认 `off`；预构建 module 名可用 `MAF_CORE_LIFECYCLE_PYO3_MODULE` 覆盖，默认 `maf_core_lifecycle_pyo3`。`enforce` 下缺 module 或 contract/hash/features 不匹配必须 fail closed；runtime 路径不得调用 `maturin` / Cargo。

Safety Kernels PyO3 runtime 约定：`MAF_RUST_ARTIFACT_STORE_MODE` / `MAF_RUST_AUTH_CORE_MODE` / `MAF_RUST_DATA_ACCESS_MODE` / `MAF_RUST_AUDIT_SANITIZER_MODE` 控制 `off|shadow|enforce`，默认 `off`；预构建 module 名可用 `MAF_SAFETY_KERNELS_PYO3_MODULE` 覆盖，默认 `maf_safety_kernels_pyo3`。`enforce` 下缺 module 或 contract/schema/error-table hash/features 不匹配必须 fail closed；runtime 路径不得调用 `maturin` / Cargo。当前 upload/artifact/auth/readonly SQL/DB deadline/audit sink 已消费 safety facade，生产 allowlist、7 天 shadow、benchmark、ops drill 与 legacy 下线仍由 `scripts/validate_prd06_safety_kernel_evidence.py` fail-closed 管理。PRD07 orchestration / hotspot 条件候选仍由 `scripts/validate_prd07_orchestration_hotspot_evidence.py --json` 保持 guarded，不得在缺少独立实施 PRD 与性能/可靠性证据时创建 `maf_orchestration_kernel`。

CI 中的 Ubuntu 22.04 x86_64 / Python 3.13 wheel job 还会用 `cargo metadata --locked --format-version 1 --manifest-path native/Cargo.toml` 生成依赖元数据，并通过 `scripts/rust_artifact_provenance.py write-sbom`、`write-provenance` 与 `generate` 产出脱敏 SBOM、provenance 与 manifest 后上传 artifact；Rust quality workflow 还会构建 `maf-runtime-sidecar`、`maf-skill-sandbox` 与 `maf-mcp-runtime-sidecar` Linux x86_64 release binary 并上传对应 SBOM / provenance / manifest。`scripts/validate_prd03_runtime_sidecar_evidence.py --allow-pending`、`scripts/validate_prd04_skill_runtime_evidence.py --allow-pending`、`scripts/validate_prd05_mcp_runtime_evidence.py --allow-pending` 与 `scripts/validate_prd06_safety_kernel_evidence.py --allow-pending` 用于确认 PRD03 / PRD04 / PRD05 / PRD06 evidence ledger 仍 fail-closed 且未伪造生产证据；PRD03-PRD06 不带 `--allow-pending` 时只有真实 artifact allowlist、benchmark、7 天 shadow、ops/migration/recovery drill 与 decommission evidence 全部满足才应通过。`scripts/validate_prd07_orchestration_hotspot_evidence.py --json` 用于确认 PRD07 条件候选仍为 guarded，不启动 `maf_orchestration_kernel` / WASM。`cargo deny check` 使用 `native/deny.toml` 中冻结的 license allowlist 与 private workspace crate 规则。当前远端 Rust quality gates 已在 `rust_branch` push run `25995183561` 验证 wheel / binary artifact 生成上传链路可跑通；但真实生产 artifact allowlist、deployment promotion、7 天 shadow、benchmark、ops/migration/recovery drill 与 decommission evidence 仍需后续生产流水线 / 运维证据补齐。


全栈人工验证脚本（默认拉起仓库真实 FastAPI runtime，会在启动期使用本地 `config.yaml` bootstrap 出环境变量来装配主代理、LLM Planner 与 Skill allowlisted LLM service；需要 UI-only 验证时可加 `--fake-backend`）：

```bash
python scripts/run_fullstack_dev.py
```

运行时配置约定：`config.yaml` 只在 API runtime 启动 / 手工 smoke 初始化时读取一次，并写入 `MAF_CONFIG_*` 进程环境变量；后续 `LLMClient`、Planner、主代理、Skill runtime 与 `trim_max_tokens` 均从环境读取。测试或上层 runtime 可用显式 `config` dict 注入覆盖，不要在业务节点执行阶段重复读取 `config.yaml`。
MySQL 只读连接配置放在本地 `config.yaml` 的 `mysql_readonly.url`（或部署环境变量 `MAF_MYSQL_READONLY_URL`）中；`config.yaml` 已被 `.gitignore` 忽略，禁止把真实数据库地址、账号或密码写入 tracked 文件。
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

项目目标运行环境为 Ubuntu 22.04.5 LTS（`GNU/Linux 6.8.0-49-generic x86_64`）、CUDA 12.6 与 NVIDIA V100 GPU。涉及系统依赖、GPU/CUDA、Rust/Python 原生扩展、模型推理、性能或部署行为的实现与验证，应以该目标环境为兼容基线；当前本地环境可作为开发期测试与回归验证环境，但不得替代目标环境兼容性判断。

常用命令示例：

```bash
conda activate multi_agent
python --version
```

如需补充依赖、脚本或工具链，请默认以该环境为基准，不要混用其他本地 Python 环境。

根目录中的 `requirements.txt` 视为当前环境依赖快照。写代码前应先查看 `requirements.txt`，确认当前可用依赖包；实现功能时应尽量基于现有依赖完成。如果确实需要新增依赖，应在 `multi_agent` 环境中安装，并同步更新 `requirements.txt`。

敏感信息（数据库连接、账号密码、API key、token、provider `base_url`、secret 等）不得写入、提交或推送到 tracked 文件。开发 / 手工 smoke 使用本地 `config.yaml`（已被 `.gitignore` 忽略）或部署环境变量保存敏感配置；项目启动 / 手工 smoke bootstrap 负责把 `config.yaml` 写入 `MAF_CONFIG_*` 或专用环境变量（如 `MAF_MYSQL_READONLY_URL`），业务 runtime 只从环境变量或显式测试 seam（`config` dict、client factory、fake generator）消费这些信息。若发现待提交内容包含敏感信息，应先迁移到 `config.yaml` / 部署环境变量并从本次提交中移除；只有在用户明确要求清理历史提交或重写远端历史时，才执行历史脱敏 / 替换操作。

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

每次开发工作结束前，都必须检查本次变更是否满足 License Requirement：
- 涉及 `native/` / Rust 依赖、`Cargo.lock`、`native/deny.toml` 或供应链策略变更时，至少运行 `cd native && cargo deny check` 并读取结果。
- 未涉及依赖或许可策略时，也应在最终验证说明中明确记录“License Requirement：无依赖/许可变更，未触发 cargo-deny 风险”。
- 不得通过关闭 license gate、扩大忽略范围或删除依赖证据来绕过许可要求。

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
本仓库服务于自研、多 Agent、面向内部业务的框架建设。当前后端主代理框架、数据查询 Skill、状态存储、API/SSE、主代理 Skill / LLM runtime 已形成实现基线；新增重大运行时、部署方式、编排模型或跨模块边界变更前，应先更新对应 PRD 设计与测试计划。
