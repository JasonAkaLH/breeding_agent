# breeding_agent

本仓库当前已进入前后端联调阶段：一期“主代理最小内核 + 可移除数据查询 Skill bundle + FastAPI/SSE/cancel/query API”已完成，后续补齐了主代理 Skill 兼容层、主代理真实 LLM runtime 绑定、默认主代理 LLM 编排、运行时受控重排与 smoke 验证；前端 v1 业务对话台已基于现有 API/SSE/artifacts 落地。

## 当前目录结构

| 路径 | 当前用途 |
|---|---|
| `AGENTS.md` | 仓库级 AI Agent 协作、编码、测试与文档约束。 |
| `CHANGELOG.md` | 仓库级变更日志；开始任何分析、设计、编码或文档修改前应先阅读最近条目。 |
| `Skill构建指南.md` | 项目级 Skill 构建、manifest、脚本执行与产物约束。 |
| `requirements.txt` | `multi_agent` Conda 环境依赖快照。 |
| `docs/prd/` | PRD 总目录；后端 PRD 在 `docs/prd/backend/`，前端 PRD 在 `docs/prd/frontend/`。 |
| `docs/` 其他文件 | Capability 接入指南、Agent 基础设施优化建议、Skill prompt 模板、架构图与状态流转图；历史阶段文档已收口到 `docs/prd/` 与 `CHANGELOG.md`。 |
| `skill/` | 项目级 Skill 目录；后端默认扫描 `skill/**/SKILL.md`，具体构建约束见 `Skill构建指南.md`。 |
| `src/api/` | FastAPI app、DTO、SSE、runtime 装配与 API routes。 |
| `src/core/` | 跨模块共享 contract、模型、枚举与基础错误。 |
| `src/storage/` | 状态存储抽象与 SQLite 实现。 |
| `src/lifecycle/` | task / node / mailbox / interrupt / cancel / conversation guard 生命周期规则。 |
| `src/orchestration/` | capability registry、scheduler、workflow plan、主代理 LLM 高层规划、router、validator、expander、运行时受控重编排与编排服务。 |
| `src/capabilities/main_agent/` | `main_agent.respond` 主代理 capability、prompt 构造与 streaming 输出。 |
| `src/integrations/` | LLM client、MySQL readonly adapter、audit logger、Skill 兼容层、MCP Python facade、LLM 上下文 token 计数等外部适配/运行时辅助能力。 |
| `native/` | Rust workspace；当前包含 Core/Lifecycle、Runtime Store/Event/Dispatcher、RuntimeSidecar service kernel + tonic/prost gRPC binding + `maf-runtime-sidecar` 二进制入口、RuntimeSidecar SQLite durable adapter、Skill Runtime policy / SkillSandboxService + tonic gRPC binding + `maf-skill-sandbox` 二进制入口与受限进程执行基线（client version / handler allowlist、相对 argv、sandbox root、timeout、stdin 上限、stdout/stderr 并发有界 drain、`env_clear` 最小环境、process-group cleanup）、MCP Runtime sidecar contract/kernel + `maf-mcp-runtime-sidecar` 二进制入口、Artifact/Auth/DataAccess/Audit 等 Rust contract/kernel crates 与 sidecar proto。部分 Python facade 已消费 Rust contract resource limits；Core/Lifecycle、Skill Runtime policy 与 Artifact/Auth/DataAccess/Audit safety kernels 已具备预构建 PyO3 module 加载 facade、Rust JSON bridge、`maturin` wheel build/import smoke 路径；RuntimeSidecar Python h2c / mTLS gRPC client 与 SkillSandbox Python h2c gRPC client 已可连接外部 Rust sidecar binary 做 runtime store/event/dispatcher RPC 与 Skill policy/sandbox RPC；MCP Runtime 目前仍是 Phase 0/1 sidecar contract/facade + evidence gate，MCP tool 真实执行仍走 Python legacy client，Phase 2-5 canonical runtime operations 待完成；RuntimeSidecar、Skill Runtime 与 MCP Runtime 均具备 artifact provenance / benchmark / promotion / ops / decommission gate + Python fail-closed validator；真实 production shadow / benchmark / ops drill / allowlist promotion evidence 仍按 PRD phase 门禁推进。 |
| `skill/<domain-query>/` | 可移除 数据查询 Skill bundle，包含 manifest、领域 runtime、配置与 Skill 专属测试；移除该目录后系统只保留 generic Skill loader。 |
| `scripts/` | 显式手工 smoke / 维护脚本，包含主代理真实 LLM smoke 与全栈开发启动脚本。 |
| `tests/` | 后端分层 unittest 回归，包括 core、storage、lifecycle、orchestration、integrations、capabilities、api、e2e、observability。 |
| `frontend/` | React + TypeScript + Vite + Ant Design 前端业务对话台，含 API/SSE client、状态 reducer、通用 data-query / file artifact 渲染与 Vitest 测试。 |

## 当前最小开发基线

- 当前默认开发环境：`conda activate multi_agent`
- 目标部署/运行环境：Ubuntu 22.04.5 LTS（`GNU/Linux 6.8.0-49-generic x86_64`）、CUDA 12.6、NVIDIA V100 GPU。当前 Rust/PyO3 policy wheel 与 sidecar 基线未直接链接 CUDA；CUDA/V100 仅约束后续如引入 GPU/native 推理依赖时的 build/runtime 兼容性。macOS arm64 只作为本地开发 smoke，不能替代 Ubuntu 22.04 x86_64 生产 wheel / sidecar 证据。
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

- Rust runtime contract/kernel workspace 当前验证命令：

```bash
conda run -n multi_agent python scripts/run_rust_quality_gates.py --run --only cargo_fmt --only cargo_test --only rust_coverage_thresholds --skip-unavailable
cd native
cargo fmt --check
cargo test --workspace --all-features
cargo check --workspace --all-targets --all-features
```

- Skill Runtime PyO3 wheel 本地 smoke（非默认回归；产物仍需 CI / 部署 provenance 后才能进入生产 allowlist；生产目标 wheel 必须在 Ubuntu 22.04 x86_64 / Python 3.13 上生成 `manylinux_2_35` 产物，本机 macOS wheel 仅限开发验证）：

```bash
CARGO_BUILD_JOBS=1 conda run -n multi_agent python -m maturin build --release --locked --manifest-path native/crates/maf_skill_runtime_pyo3/Cargo.toml --interpreter /opt/miniconda3/envs/multi_agent/bin/python --compatibility manylinux_2_35 --auditwheel check --out native/target/wheels
conda run -n multi_agent python -m pip install --force-reinstall --no-deps native/target/wheels/maf_skill_runtime_pyo3-*.whl
conda run -n multi_agent python -m unittest tests.integrations.codex_skills.test_pyo3_wheel_build_contract.SkillRuntimePyo3WheelBuildContractTest.test_installed_pyo3_module_matches_rust_contract_when_available
conda run -n multi_agent python scripts/rust_artifact_provenance.py self-test
```

- Core/Lifecycle PyO3 wheel 本地 smoke（非默认回归；与 Skill Runtime 使用同一 Ubuntu 22.04 x86_64 / Python 3.13 / `manylinux_2_35` CI wheel 目标，本机 macOS wheel 仅限开发验证）：

```bash
CARGO_BUILD_JOBS=1 conda run -n multi_agent python -m maturin build --release --locked --manifest-path native/crates/maf_core_lifecycle_pyo3/Cargo.toml --interpreter /opt/miniconda3/envs/multi_agent/bin/python --compatibility manylinux_2_35 --auditwheel check --out native/target/wheels
conda run -n multi_agent python -m pip install --force-reinstall --no-deps native/target/wheels/maf_core_lifecycle_pyo3-*.whl
conda run -n multi_agent python -m unittest tests.integrations.test_core_lifecycle_pyo3_wheel_contract.CoreLifecyclePyo3WheelContractTest.test_installed_pyo3_module_matches_core_lifecycle_contract_when_available
```

- Safety Kernels PyO3 wheel 本地 smoke（PRD06 Artifact/Auth/DataAccess/Audit；非默认回归；与上述 PyO3 wheel 使用同一 Ubuntu 22.04 x86_64 / Python 3.13 / `manylinux_2_35` CI wheel 目标，本机 macOS wheel 仅限开发验证）：

```bash
CARGO_BUILD_JOBS=1 conda run -n multi_agent python -m maturin build --release --locked --manifest-path native/crates/maf_safety_kernels_pyo3/Cargo.toml --interpreter /opt/miniconda3/envs/multi_agent/bin/python --compatibility manylinux_2_35 --auditwheel check --out native/target/wheels
conda run -n multi_agent python -m pip install --force-reinstall --no-deps native/target/wheels/maf_safety_kernels_pyo3-*.whl
conda run -n multi_agent python -m unittest tests.integrations.test_safety_kernels_pyo3_wheel_contract.SafetyKernelsPyo3WheelContractTest.test_installed_pyo3_module_matches_safety_contract_when_available
python scripts/validate_prd06_safety_kernel_evidence.py --allow-pending --json
# PRD07 orchestration/hotspot 条件候选 guard（当前应返回 guarded；不启动 Rust 化）
python scripts/validate_prd07_orchestration_hotspot_evidence.py --json
```

CI 中的 Ubuntu 22.04 x86_64 / Python 3.13 wheel job 还会用 `cargo metadata --locked --format-version 1 --manifest-path native/Cargo.toml` 生成依赖元数据，并通过 `scripts/rust_artifact_provenance.py write-sbom`、`write-provenance` 与 `generate` 产出脱敏 SBOM、provenance 与 manifest 后上传 artifact；Rust quality workflow 也会构建 `maf-runtime-sidecar`、`maf-skill-sandbox` 与 `maf-mcp-runtime-sidecar` Linux x86_64 release binary 并上传对应 SBOM / provenance / manifest。`scripts/validate_prd03_runtime_sidecar_evidence.py --allow-pending`、`scripts/validate_prd04_skill_runtime_evidence.py --allow-pending`、`scripts/validate_prd05_mcp_runtime_evidence.py --allow-pending` 与 `scripts/validate_prd06_safety_kernel_evidence.py --allow-pending` 用于确认 PRD03 / PRD04 / PRD05 / PRD06 evidence ledger 仍 fail-closed 且未伪造生产证据；PRD03-PRD06 不带 `--allow-pending` 时只有真实 artifact allowlist、benchmark、7 天 shadow、ops/migration/recovery drill 与 decommission evidence 全部满足才应通过。`scripts/validate_prd07_orchestration_hotspot_evidence.py --json` 用于确认 PRD07 条件候选仍为 guarded，不启动 `maf_orchestration_kernel` / WASM。`cargo deny check` 使用 `native/deny.toml` 中冻结的 license allowlist 与 private workspace crate 规则。当前远端 Rust quality gates 已在 `rust_branch` push run `25995183561` 验证 wheel / binary artifact 生成上传链路可跑通；但真实生产 artifact allowlist、deployment promotion、7 天 shadow、benchmark、ops/migration/recovery drill 与 decommission evidence 仍需后续生产流水线 / 运维证据补齐。


- 全栈人工验证脚本（默认拉起仓库真实 FastAPI runtime）：

```bash
python scripts/run_fullstack_dev.py
```

真实 runtime 会在启动期使用本地 `config.yaml` bootstrap 出环境变量，并创建共享的主代理 `SharedLLMRuntime`；默认自动模式下，主代理高层规划、运行时观察/重排与最终回答共享这个主代理 runtime。可移除 Skill bundle 可通过 runtime allowlisted service 复用主代理 `SharedLLMRuntime` 的受控非流式调用；数据查询 Skill 的只读 MySQL 连接与领域配置随 `skill/<domain-query>/` bundle 管理；如需不依赖真实 LLM/MySQL provider、只验证前端交互，可增加 `--fake-backend` 使用 deterministic fake provider/数据库适配器。

- Docker Compose 打包 / 启动（会把本地 git-ignored `config.yaml` 复制进 backend 镜像；该文件包含 provider / 数据库等敏感配置时只应在受控环境构建和分发镜像）：

```bash
docker compose build
docker compose up
```

Compose 会构建两个 `linux/amd64` 本地镜像：`breeding-agent-backend:local`（Ubuntu 22.04 + Conda Python 3.13.13，启动 `python -m uvicorn src.api.app:create_app --factory --host 0.0.0.0 --port 8000`）与 `breeding-agent-frontend:local`（Ubuntu 22.04 + nginx，服务 Vite build 产物并代理 `/api/`、`/api-doc` 到 backend）。默认宿主机端口：前端 `http://127.0.0.1:51999`，后端直连 `http://127.0.0.1:51888`；运行时 SQLite / audit / artifact 数据通过 named volume `breeding-agent-runtime` 挂载到 `/app/runtime`。

`.dockerignore` 会把 `tests/`、根目录 Markdown 文档、`docs/` 中除 `docs/api/` 外的文档、node_modules、构建缓存与本地 runtime 数据排除出 Docker context / 镜像；`docs/api/api-doc.html` 会保留，因为后端 `/api-doc` 路由在运行时读取它。

若通过远端域名对浏览器开放该 Compose 栈，应在反向代理 / LB 层提供 HTTPS；当前认证只通过 `Authorization: Bearer <access-token>` 传递，前端将登录返回的 token 保存在浏览器 localStorage 中并为 REST、multipart upload 与 SSE fetch-stream 注入 Authorization。

运行时配置约定：`config.yaml` 只在 API runtime 启动 / 手工 smoke 初始化时读取一次，并写入 `MAF_CONFIG_*` 进程环境变量；后续 `LLMClient`、Planner、主代理、Skill runtime 与 `trim_max_tokens` 均从环境读取。测试或上层 runtime 仍可通过显式 `config` dict 注入覆盖，不应在业务节点执行阶段重复读取 `config.yaml`。
MySQL 只读连接配置也放在本地 `config.yaml` 的 `mysql_readonly.url`（或部署环境变量 `MAF_MYSQL_READONLY_URL`）中；`config.yaml` 已被 `.gitignore` 忽略，禁止把真实数据库地址、账号或密码写入 tracked 文件。
认证相关部署配置不得写入 tracked 文件：跨站 REST API 只通过 `MAF_API_CORS_ALLOWED_ORIGINS` 配置逗号分隔的显式 origin allowlist，不允许 `*`；token hash pepper 使用 `MAF_AUTH_TOKEN_HASH_SECRET`，当 `MAF_API_ENV` / `MAF_ENV` / `APP_ENV` 为 `production` / `prod` 或显式设置 `MAF_AUTH_TOKEN_HASH_SECRET_REQUIRED=1` 时，缺失 secret 必须 fail closed；未配置 secret 的开发/测试进程只使用进程内随机 pepper，重启后既有 token 自动失效。不要把 token 放入 URL query 或业务请求体。
敏感信息（数据库连接、账号密码、API key、token、provider `base_url`、secret 等）不得写入、提交或推送到 tracked 文件；开发 / 手工 smoke 统一放入本地 `config.yaml` 或部署环境变量，并由启动 bootstrap 写入 `MAF_CONFIG_*` / 专用环境变量供 runtime 消费。若发现待提交内容包含敏感信息，应先迁移到配置或环境变量并从本次提交中移除；历史提交清理只在明确要求时执行。
同一个 API runtime 中的 `*_config_path` 必须指向同一个启动配置文件；默认生产路径使用一个主代理 LLM runtime；Skill 内部 LLM service 只能通过受控 allowlisted adapter 复用该 runtime。显式组件级 `config` dict、client factory 或 fake generator 仍作为测试/定制 seam 保留。
RuntimeSidecar 连接配置当前通过部署环境变量 `MAF_RUNTIME_SIDECAR_ENDPOINT` 注入，支持 `http://127.0.0.1:<port>` loopback h2c、`https://host:<port>` mTLS gRPC 与 `unix:///absolute/path` Unix domain socket 内部连接；可选 `MAF_RUNTIME_SIDECAR_ALLOWED_HOSTS`、`MAF_RUNTIME_SIDECAR_MTLS_ENABLED`、`MAF_RUNTIME_SIDECAR_TLS_CA_PATH`、`MAF_RUNTIME_SIDECAR_TLS_CERT_PATH`、`MAF_RUNTIME_SIDECAR_TLS_KEY_PATH` 与 `MAF_RUNTIME_SIDECAR_TLS_SERVER_NAME` 用于 endpoint allowlist / client-side mTLS 身份门禁。`MAF_RUNTIME_SIDECAR_ARTIFACT_MANIFEST_PATH` 与 `MAF_RUNTIME_SIDECAR_ARTIFACT_ALLOWLIST_PATH` 可提供部署流水线生成的 sidecar artifact manifest / allowlist；任何 RuntimeSidecar component 进入 `enforce` 且配置 endpoint 时缺少这两个文件会 fail closed。`maf-runtime-sidecar --serve <addr> --tls-cert <cert> --tls-key <key> --client-ca <ca>` 或 server-side `MAF_RUNTIME_SIDECAR_TLS_CERT_PATH` / `MAF_RUNTIME_SIDECAR_TLS_KEY_PATH` / `MAF_RUNTIME_SIDECAR_TLS_CLIENT_CA_PATH` 可启用 mTLS；跨主机访问缺少 mTLS 仍 fail-closed，artifact provenance allowlist 仍按 Rust PRD 门禁推进。`MAF_RUST_RUNTIME_STORE_MODE=shadow` / `MAF_RUST_EVENT_LOG_MODE=shadow` / `MAF_RUST_TASK_DISPATCHER_MODE=shadow` 下，task submit、node transition、cancellation token write、event append 与 Skill/MCP bundle revision pin/release 仍以 Python legacy 写入作为用户可见结果，并旁路调用已配置 RuntimeSidecar client，将脱敏 fingerprint / error code / duration 写入 `runtime.sidecar_shadow_diff` 审计事件。
Core/Lifecycle PyO3 配置当前通过 `MAF_RUST_CORE_MODE` / `MAF_RUST_LIFECYCLE_MODE` 控制 `off|shadow|enforce`，预构建 module 名可用 `MAF_CORE_LIFECYCLE_PYO3_MODULE` 覆盖，默认 `maf_core_lifecycle_pyo3`。`enforce` 下缺少预构建 PyO3 module、contract/schema/error/transition hash 或 supported_features 不匹配会 fail closed；`shadow` 下仍以 Python facade / checked-in Rust contract artifact 结果为用户可见结果。runtime 启动和请求路径不得调用 `maturin` / Cargo。
SkillSandbox 连接配置当前通过部署环境变量 `MAF_SKILL_SANDBOX_ENDPOINT` 与 `MAF_SKILL_SANDBOX_ROOT` 注入；`MAF_SKILL_SANDBOX_ARTIFACT_MANIFEST_PATH` 与 `MAF_SKILL_SANDBOX_ARTIFACT_ALLOWLIST_PATH` 可提供部署流水线生成的 sandbox sidecar artifact manifest / allowlist，`MAF_RUST_SKILL_RUNTIME_MODE=enforce` 且配置 endpoint 时缺少这两个文件会 fail closed。Python `SkillSandboxGrpcClient` 会校验 h2c gRPC payload 精确长度、拒绝缺失/短头/多余/截断消息，并按 Rust contract 校验 server 返回的 client version range 与可选 artifact provenance allowlist。Skill policy trust gate 会优先尝试加载 `MAF_SKILL_POLICY_PYO3_MODULE`（默认 `maf_skill_runtime_pyo3`）指向的预构建 PyO3 policy module，校验 contract 后再调用 Rust policy；未安装该 module 时继续使用已配置 SkillSandbox policy client。`native/crates/maf_skill_runtime_pyo3` 只提供 CI / 部署预构建用 wheel source 与本地 smoke 路径，runtime 启动和请求路径不得调用 `maturin` / Cargo。Skill Runtime 生产 promotion 证据现在可通过 Rust contract 驱动的 Python validator 校验 artifact provenance、benchmark、shadow/enforce threshold、ops readiness 与 legacy decommission readiness；`scripts/validate_prd04_skill_runtime_evidence.py` 只作为进入 `enforce` / 下线 legacy 的 fail-closed 门禁，不代表当前仓库已经具备真实 production provenance 或生产观测证据。`MAF_RUST_SKILL_RUNTIME_MODE=enforce` 时平台服务 trust gate 与脚本型 Skill 都必须配置 Rust policy / SkillSandbox client，否则 fail closed，不回退 Python trust gate / subprocess legacy。`shadow` 模式下平台服务仍以 Python legacy 结果为准，并记录不含用户输入正文、secret 或真实 payload 的 Rust policy diff 审计事件。

Safety Kernels PyO3 配置当前通过 `MAF_RUST_ARTIFACT_STORE_MODE` / `MAF_RUST_AUTH_CORE_MODE` / `MAF_RUST_DATA_ACCESS_MODE` / `MAF_RUST_AUDIT_SANITIZER_MODE` 控制 `off|shadow|enforce`，预构建 module 名可用 `MAF_SAFETY_KERNELS_PYO3_MODULE` 覆盖，默认 `maf_safety_kernels_pyo3`。`enforce` 下缺少预构建 PyO3 module、contract/schema/error-table hash 或 supported_features 不匹配会 fail closed；`shadow` 下仍以 Python facade / checked-in Rust safety contract artifact 作为用户可见结果，并在配置 audit sink 时写入只含 fingerprint / error code / duration 的 `safety.kernel_shadow_diff`。当前 Python upload/artifact/auth/readonly DB/audit sink 已消费 safety facade 的 path/hash/auth compare/HMAC/readonly SQL/shape/deadline/audit redaction contract；生产 allowlist promotion、7 天 shadow、真实 benchmark、ops drill 与 Python legacy 下线仍由 `scripts/validate_prd06_safety_kernel_evidence.py` fail-closed 管理。runtime 启动和请求路径不得调用 `maturin` / Cargo。

Orchestration / hotspot Rust 化当前保持 PRD07 条件候选，不属于必做 Rust 化目标集；`scripts/validate_prd07_orchestration_hotspot_evidence.py --json` 必须返回 `guarded`，未来只有在另开 implementation PRD、补齐性能/可靠性证据、baseline/shadow compare、供应链、benchmark/SLO、migration/DR、ops 与 legacy decommission gate 后才允许启动 `maf_orchestration_kernel` 或 WASM hotspot。

MCP Runtime sidecar 当前通过 `MAF_RUST_MCP_RUNTIME_MODE=off|shadow|enforce` 与 `MAF_RUST_MCP_RUNTIME_ENDPOINT` 控制，endpoint 必须为 Unix socket 或 loopback 内部地址。`MAF_RUST_MCP_RUNTIME_ARTIFACT_MANIFEST_PATH` 与 `MAF_RUST_MCP_RUNTIME_ARTIFACT_ALLOWLIST_PATH` 可提供部署流水线生成的 MCP sidecar artifact manifest / allowlist，`enforce` 且配置 endpoint 时缺少这两个文件会 fail closed。当前仓库只能宣称 MCP Phase 0/1 sidecar contract/facade、compatibility handshake、endpoint allowlist、artifact provenance gate 与 PRD05 evidence ledger 已落地；MCP tool canonical execution、Streamable HTTP/SSE kernel、durable task registry、API/SSE sidecar event bridge、production shadow/enforce 与 Python legacy 下线仍需按 `docs/prd/MCP/` Phase 2-5 推进。
