# Rust 化 Runtime 模块评估 PRD

- **范围**：后端 / Runtime substrate / Rust native kernel / Python-Rust 边界 / Skill-owned native runtime
- **文档状态**：PRD 草案
- **日期**：2026-05-13
- **触发背景**：SQLQuery 已完成 Skill-only 归属迁移，主体框架不再保留 SQLQuery native capability；需要重新评估成熟 Agent 系统中哪些后端模块应 Rust 化。
- **关联文档**：
  - `docs/prd/backend/00-主代理框架PRD.md`
  - `docs/prd/backend/03-协作协议与任务生命周期.md`
  - `docs/prd/backend/04-状态存储与迁移策略.md`
  - `docs/prd/backend/11-Skill输出文件Artifact与下载PRD.md`
  - `docs/prd/backend/12-Skill一等Capability能力池PRD.md`
  - `docs/prd/backend/13-Skill动态加载与热部署PRD.md`
  - `docs/prd/backend/14-MCPRuntime实现需求PRD.md`
  - `docs/prd/backend/15-SkillExecutor实现需求PRD.md`
  - `skill/sql-query/SKILL.md`
- **外部参考**：
  - Rust 安装与 toolchain：<https://www.rust-lang.org/tools/install>
  - PyO3：<https://pyo3.rs/>
  - maturin：<https://www.maturin.rs/>
  - Tokio：<https://tokio.rs/>
  - SQLx：<https://github.com/launchbadge/sqlx>

## 1. 一句话结论

主体框架不应把 `ApiRuntime` 或 FastAPI 应用整体改写为 Rust；应把成熟 Agent 系统中**确定性、并发敏感、安全敏感、可重放、可类型约束**的 runtime substrate 下沉为 Rust native kernel，并保留 Python 作为 API composition root、LLM/provider glue、prompt 产品语义与快速演进层。

SQLQuery 已是 `skill/sql-query/` 的 Skill-owned platform-service 能力；如需 Rust 化，应在 Skill bundle 内把 SQL Guard、schema context、只读执行、结果整形等领域 kernel 改写为 Skill-owned Rust runtime，而不是重新引入主体框架 native capability。

## 2. 背景与当前状态

### 2.1 SQLQuery 归属变化

当前仓库状态已完成以下迁移：

1. `src/capabilities/` 下只保留 `main_agent`、`skill_tool`、`mcp_tool` 等通用能力目录，不再存在 `src/capabilities/sql_query`。
2. `skill/sql-query/SKILL.md` 声明唯一公开入口 `skill.sql_query`，并通过 `platform_service`、`trust_scope: project`、`handler_module: runtime/sql_query_skill/platform_handler.py` 接入通用 Skill Executor。
3. API runtime 只装配通用 `SkillExecutor` 与 `SkillPlatformHandlerRegistry`，通过 `mysql_readonly`、`llm.non_stream`、`artifact_writer`、`progress_events` 等受控 service 向可信 Skill handler 注入能力。
4. SQLQuery 内部六阶段仍存在，但作为 `skill/sql-query/runtime/sql_query_skill/` 内部 domain flow，不再暴露为主体 orchestration native node。

因此，本 PRD 的主体框架 Rust 化范围不包含旧 SQLQuery native capability；SQLQuery 只作为 Skill-owned native runtime 候选项单独评估。

### 2.2 当前 Python runtime 形态

当前后端以 Python async / await 为主，核心 runtime 分散在：

| 当前目录 / 模块 | 当前职责 | Rust 化关注点 |
|---|---|---|
| `src/api/runtime.py` | FastAPI runtime 装配、任务提交、执行调度、SSE 事件、Skill/MCP bundle revision pinning | 只抽 dispatcher / event / revision / cancellation kernel，不整体迁移 |
| `src/core/` | 跨模块 contracts、models、enums、基础错误 | canonical schema 与类型约束 |
| `src/lifecycle/` | task / node / mailbox / interrupt / cancel 状态规则 | 状态机不可非法转移、并发一致性 |
| `src/storage/` | SQLite storage facade、状态持久化、event append/list | durable store、event log、replay、lease、PostgreSQL 同构 |
| `src/integrations/codex_skills/` | Skill catalog、manifest、runtime bundle、script runner、service binding | sandbox、trust gate、fingerprint、handler allowlist |
| `src/capabilities/skill_tool/` | generic Skill Executor | mode / answer_mode / service binding / result normalization |
| `src/integrations/mcp/` + `src/capabilities/mcp_tool/` | MCP client runtime、tool binding、schema 校验、executor | JSON-RPC、transport、schema validation、untrusted output sanitization |
| `src/storage/artifact_files.py` + `src/api/upload_store.py` | artifact 文件、上传、hash、路径安全、quota | 文件安全与大对象管理 |
| `src/auth/` | password hash、captcha、session | 安全原语与 token/session 规则 |
| `src/orchestration/` | registry、scheduler、workflow plan、router、LLM planner、validator | deterministic DAG kernel 与 LLM glue 分离 |
| `skill/sql-query/runtime/sql_query_skill/` | SQLQuery Skill-owned domain flow | Skill-owned SQL guard / readonly query / result shaping |

## 3. 目标

### 3.1 产品目标

1. 在不改变现有用户行为、API 行为、Skill 行为与前端事件契约的前提下，为后端建立 Rust native runtime 演进边界。
2. 提升成熟 Agent 系统的核心属性：状态一致性、任务可重放、并发安全、资源隔离、安全 fail-closed、协议解析稳定性、运行时可观测。
3. 明确主体框架与 Skill-owned native runtime 的责任划分，避免 SQLQuery 或未来业务 Skill 再次侵入框架内核。
4. 让未来 PostgreSQL、分布式 dispatcher、MCP/Skill sandbox、大文件 artifact、只读 DB 访问等高风险模块具备 Rust 下沉路径。

### 3.2 工程目标

1. 将 Rust 化候选拆成可验证 kernel，而不是按 Python 文件整包迁移。
2. 每个 Rust kernel 必须有 Python 兼容 facade，保持现有测试与 API 契约可回归。
3. 优先 Rust 化纯规则、协议、状态、存储、安全、文件、DB 访问等确定性模块。
4. 保持 Python 层负责 FastAPI route、DTO、LLM provider SDK、prompt 语义、产品策略和 orchestration 高层装配。
5. 建立 Rust toolchain、Cargo workspace、PyO3 / sidecar 集成、测试、审计与发布标准。

### 3.3 不以工期作为判断标准

本 PRD 的“应 Rust 化”只表达架构适配度与长期收益，不代表立即实施优先级。实施顺序应另行通过开发计划、PRD 拆分和测试计划确定。

## 4. 非目标

1. 不把 FastAPI app、API routes、DTO 全量迁移为 Rust。
2. 不把 `ApiRuntime` 作为一个整体类迁移为 Rust；只拆其内部 runtime substrate。
3. 不把 LLM provider SDK 调用层整体迁移为 Rust。
4. 不把主代理 prompt 构造、产品话术、LLM planner prompt 变成 Rust 固定逻辑。
5. 不重新创建主体框架 SQLQuery native capability。
6. 不在本 PRD 中引入 LangChain、LangGraph、AutoGen 等现成 Agent 框架。
7. 不绕过现有 TDD、分层 unittest、Skill bundle 测试与前端契约测试。
8. 不因引入 Rust 放宽现有 secret、MySQL readonly、Skill/MCP trust、artifact path 安全约束。

## 5. 用户、维护者与影响面

| 角色 / 系统 | 关注点 | 本 PRD 承诺 |
|---|---|---|
| 业务用户 | 对话、任务进度、结果和 artifact 行为不能变化 | Rust 化必须行为兼容，前端事件与 API response 不破坏 |
| 后端维护者 | 模块边界、测试、部署复杂度 | Rust kernel 有清晰 facade、golden tests、rollback path |
| Skill 作者 | Skill 是否仍可独立发布 / 移除 | 主体 Rust kernel 不绑定业务 Skill；SQLQuery Rust 化归属 Skill bundle |
| 安全 / 运维 | service binding、secret、文件、外部 tool 输出 | Skill/MCP/file/auth/DB 边界 fail-closed，审计不可弱化 |
| 前端 | SSE、artifact、data query card 兼容 | Rust 化不改变 frontend event schema 与 artifact metadata |
| 未来多实例部署 | dispatcher、store、event replay、lease | Rust runtime store / event log 应支持多实例一致性演进 |

## 6. Rust 化判断原则

模块满足以下条件越多，越应该 Rust 化：

1. **确定性强**：主要是状态机、校验器、协议解析、文件路径、hash、schema、序列化。
2. **安全敏感**：涉及 secret、路径穿透、外部输入、SQL guard、auth、sandbox、MCP/Skill trust boundary。
3. **并发敏感**：涉及任务调度、取消、lease、event stream、bundle revision pinning、active task registry。
4. **持久化关键**：涉及 event log、task/node 状态、artifact metadata、PostgreSQL/SQLite 同构迁移。
5. **性能热点潜力**：涉及大 JSON、表格、artifact、DB rows、token accounting、MCP output sanitization。
6. **类型约束收益高**：Python dataclass/Mapping 容易漂移，Rust enum/struct/result 可固化 contract。
7. **业务语义稳定**：不频繁随产品话术、prompt、provider SDK 变化。

不满足以上条件，且主要承担产品语义、HTTP glue、LLM SDK 适配、UI 展示的模块，不应整体 Rust 化。

## 7. Rust 化范围矩阵

### 7.1 P0：应 Rust 化的主体框架 runtime substrate

| 编号 | 当前模块 | 应 Rust 化部分 | 保留 Python 部分 | 关键收益 |
|---|---|---|---|---|
| RUST-P0-001 | `src/core/` | canonical enums、core structs、event/task/artifact JSON schema、contract validation | dataclass/Pydantic facade、typing 兼容层 | 防止跨模块 contract 漂移 |
| RUST-P0-002 | `src/lifecycle/` | task/node/mailbox/interrupt/cancel 状态机、非法转移错误、property-testable transition table | service wrapper、storage 调用 | 状态不可非法、取消/恢复语义可验证 |
| RUST-P0-003 | `src/storage/` | durable store、event append/replay、idempotency、lease、SQLite/PostgreSQL repository kernel | 配置注入、迁移 orchestration | 多实例、重放、可靠任务恢复 |
| RUST-P0-004 | `src/api/runtime.py` 内 dispatcher | task dispatcher、running task registry、cancellation token、bundle revision pin/release、active task lease | `ApiRuntime` composition root、FastAPI dependency | 消除 in-memory fragile runtime，支持生产化 |
| RUST-P0-005 | `src/integrations/codex_skills/` | Skill manifest parser、bundle fingerprint、public root guard、trust gate、handler allowlist、script sandbox policy | Python handler 兼容、Skill 作者接口 | Skill 安全边界可审计 |
| RUST-P0-006 | `src/capabilities/skill_tool/` | execution mode、answer_mode、service binding、result normalization、error mapping | final answer glue、Python handler call bridge | `skill.*` 一等执行器稳定化 |
| RUST-P0-007 | `src/integrations/mcp/` + `src/capabilities/mcp_tool/` | JSON-RPC、transport state、schema validation、tool binding、output truncation/sanitization | runtime config 注入、业务 capability 包装 | 外部 tool 输出不可信边界稳定化 |
| RUST-P0-008 | artifact/upload/file store | storage key、path normalization、hash、quota、retention、zip/archive safety | API download response、auth dependency | 防路径穿透、防大文件资源泄漏 |

### 7.2 P1：应该 Rust 化，但可作为独立专题拆分

| 编号 | 当前模块 | 应 Rust 化部分 | 保留 Python 部分 | 关键收益 |
|---|---|---|---|---|
| RUST-P1-001 | `src/auth/services.py` | password hash verify、HMAC、captcha verify、session token / TTL core | HTTP cookie/session wiring、页面交互 | 安全原语一致性 |
| RUST-P1-002 | `src/orchestration/` deterministic kernel | scheduler、DAG validator、completion policy、backpressure、payload policy | LLM planner、router glue、provider fallback、prompt | DAG 与调度规则可证明 |
| RUST-P1-003 | `src/integrations/mysql_readonly.py`、`src/mysql_engine.py` | async DB pool、readonly enforcement、timeout、row decoding、query result shape | service registry、配置读取 | DB I/O 与只读约束更稳 |
| RUST-P1-004 | `skill/sql-query/runtime/sql_query_skill/` | SQL Guard、schema context、route/guard contract、readonly execute、result shaping | Skill manifest、LLM prompt wording、platform handler facade | SQLQuery 作为 Skill-owned native runtime 高可靠化 |
| RUST-P1-005 | audit/event serialization | audit payload sanitizer、event serializer、privacy filter | audit sink 注入 | 审计字段一致、敏感信息不泄露 |

### 7.3 P2：只建议抽热点，不整体 Rust 化

| 编号 | 当前模块 | 可 Rust 化部分 | 不建议迁移部分 | 判断 |
|---|---|---|---|---|
| RUST-P2-001 | `src/integrations/token_counter.py` | token budget accounting、message trimming、缓存 | provider-specific tokenizer 选择 | 适合小 kernel，收益取决于数据量 |
| RUST-P2-002 | `src/capabilities/main_agent/` | artifact/dependency sanitizer、Skill match index、输出 schema 校验 | prompt 文案、主代理思考流程、final answer 语义 | 产品语义变化快，不整体迁移 |
| RUST-P2-003 | frontend data-heavy logic | 大表格 preview、artifact JSON/CSV 解析 WASM | React UI、Ant Design 组件 | 仅在前端数据量成为热点时采用 |
| RUST-P2-004 | config bootstrap | typed config schema、secret redaction | YAML 读取入口、部署环境整合 | 可选，优先级低于 runtime store |

### 7.4 不建议整体 Rust 化

| 当前模块 | 不建议整体 Rust 化原因 |
|---|---|
| FastAPI routes / DTO | HTTP glue 与 Python 生态绑定强，行为变化风险大，性能瓶颈通常不在 route 函数本身 |
| `ApiRuntime` 整体类 | 当前承担装配、注入、协调职责；应拆 dispatcher/store/event kernel，而非整体搬迁 |
| `src/integrations/llm_client.py` / `llm_runtime.py` | provider SDK、流式协议、模型参数变化快，Rust 化维护成本高于收益 |
| `src/capabilities/main_agent/prompt_builder.py` | prompt 产品语义、Skill 指令拼接、回答策略变化频繁 |
| React / Ant Design UI | UI 不是后端 runtime 热点；除大数据解析外无需 Rust |

## 8. 目标架构

### 8.1 推荐分层

```text
FastAPI / Python API layer
  ├─ DTO / auth dependency / SSE response / route glue
  ├─ ApiRuntime composition root
  ├─ LLM provider SDK adapters
  └─ Prompt / product semantics

Python facade layer
  ├─ core model compatibility facade
  ├─ storage port adapters
  ├─ Skill/MCP executor facade
  └─ orchestration service adapter

Rust native kernel layer
  ├─ maf_core_types
  ├─ maf_lifecycle
  ├─ maf_runtime_store
  ├─ maf_event_log
  ├─ maf_task_dispatcher
  ├─ maf_skill_runtime
  ├─ maf_mcp_runtime
  ├─ maf_artifact_store
  ├─ maf_auth_core
  ├─ maf_orchestration_kernel
  └─ maf_data_access

Skill-owned native layer
  └─ skill/sql-query/native or runtime-native/sql_query_core
```

### 8.2 集成方式

| 场景 | 推荐集成方式 | 说明 |
|---|---|---|
| 纯规则 / 校验 / 状态机 | PyO3 extension | 低延迟、可直接替换 Python 函数，适合 lifecycle / core / sanitizer |
| 持久化 / dispatcher / event log | Rust sidecar service 或 Rust library + PyO3 async bridge | 多实例和 crash recovery 更清晰；生产化可逐步 sidecar 化 |
| MCP / Skill sandbox | Rust service 或 isolated process manager | 需要进程、timeout、I/O 限额、外部输入隔离 |
| Skill-owned SQLQuery | Skill bundle 内 native binary 或 PyO3 wheel | 不进入主体框架；随 Skill bundle 维护与测试 |
| 前端大数据处理 | WASM | 非默认，仅在数据量证明需要时启用 |

### 8.3 Rust workspace 建议

新增大型 `native/`、`rust/` 或同类目录前必须先完成设计评审。若评审通过，推荐 workspace 拆分：

| Crate | 职责 |
|---|---|
| `maf_core_types` | core enum / struct / JSON schema / serde contract |
| `maf_lifecycle` | task/node/mailbox/interrupt/cancel transition table |
| `maf_runtime_store` | SQLite/PostgreSQL repository、transaction、lease、idempotency |
| `maf_event_log` | event append、replay、cursor、SSE snapshot support |
| `maf_task_dispatcher` | task queue、active registry、cancellation token、bundle revision pinning |
| `maf_skill_runtime` | Skill manifest、bundle fingerprint、trust gate、script sandbox policy |
| `maf_mcp_runtime` | MCP protocol、transport state、tool binding、schema validation |
| `maf_artifact_store` | artifact/upload/file path、hash、quota、retention、archive safety |
| `maf_auth_core` | password / HMAC / session / captcha primitives |
| `maf_orchestration_kernel` | DAG validation、scheduler、completion policy、backpressure |
| `maf_data_access` | readonly DB adapter、row shape、timeouts |
| `skill_sql_query_core` | SQLQuery Skill-owned guard/schema/execute/result kernel |

## 9. 功能需求

### 9.1 Core contract Rust kernel

- RUST-FR-001：系统必须有唯一 canonical core type/schema 来源，Python facade 不得定义与 Rust schema 冲突的字段语义。
- RUST-FR-002：所有跨模块 `Task`、`TaskNode`、`EventRecord`、`Artifact`、`CapabilityExecutionResult` 必须可 serde round-trip。
- RUST-FR-003：FFI 边界必须把 Rust error 映射为 Python typed exception，不允许 panic 穿透。

### 9.2 Lifecycle Rust kernel

- RUST-FR-010：task/node/mailbox/interrupt/cancel 转移必须由 Rust transition table 统一判定。
- RUST-FR-011：非法状态转移必须 fail-closed，并返回稳定错误码。
- RUST-FR-012：取消、interrupt answer、resume、late result acceptance 必须有 property tests 与 Python golden tests。

### 9.3 Runtime store / event log / dispatcher

- RUST-FR-020：任务提交、计划生成、节点执行、事件追加必须支持 durable event log。
- RUST-FR-021：dispatcher 必须支持 task lease、cancellation token、active task recovery、bundle revision pin/release。
- RUST-FR-022：SQLite 与 PostgreSQL 必须保持逻辑同构；PostgreSQL 上线不得改变 Python API contract。
- RUST-FR-023：SSE 初始 replay 与 live event 订阅必须基于同一 event cursor 语义。

### 9.4 Skill Runtime / Skill Executor

- RUST-FR-030：Skill manifest parser、execution config、public root guard、bundle fingerprint 必须由 Rust kernel 提供确定性结果。
- RUST-FR-031：`platform_service` service binding 必须继续满足 manifest 声明 + runtime allowlist 双重授权。
- RUST-FR-032：普通 `python_subprocess` Skill 不得获得 MySQL、内部 LLM、secret 或完整环境变量。
- RUST-FR-033：Skill script runner 必须限制路径、symlink、cwd、timeout、stdout/stderr、output files，并产出可审计错误。

### 9.5 MCP Runtime

- RUST-FR-040：MCP JSON-RPC request / response / error 必须由 Rust protocol layer 校验。
- RUST-FR-041：tool input 必须按 planner allowlist 与 JSON Schema fail-closed 校验。
- RUST-FR-042：tool output 必须支持 size limit、schema validation、secret redaction、untrusted external content notice。
- RUST-FR-043：MCP bundle activation 必须原子化：新 bundle 准备成功后再切换，失败保留旧 bundle。

### 9.6 Artifact / upload / file safety

- RUST-FR-050：storage key、artifact id、filename、upload id 必须经过 Rust path normalization 与 escape check。
- RUST-FR-051：所有 managed artifact 必须有 size、sha256、retention metadata。
- RUST-FR-052：zip/archive 生成与清理必须防 zip-slip、路径穿透、symlink 泄漏。
- RUST-FR-053：上传 preview 必须有大小、格式、UTF-8、行列数量限制。

### 9.7 Skill-owned SQLQuery native runtime

- RUST-FR-060：SQLQuery Rust 化不得重新注册主体框架 native capability。
- RUST-FR-061：SQLQuery Skill-owned Rust kernel 必须只通过 `skill.sql_query` platform-service handler 进入系统。
- RUST-FR-062：SQL Guard 必须拒绝写入、删除、更新、DDL、多语句、系统 schema、非 route whitelist 表。
- RUST-FR-063：readonly execute 必须通过受控 DB service，不得从 Skill 读取数据库连接 secret。
- RUST-FR-064：result shaping 必须保留现有 artifact metadata、`domain_kind=sql_query`、progress event 与 finalizer contract。

## 10. 非功能需求

### 10.1 性能

- Rust kernel 应降低大 payload 校验、event replay、artifact path/hash、MCP output sanitization、DB rows shaping 的 CPU 与内存抖动。
- 不要求所有 Rust kernel 在首版都快于 Python；但不得引入明显 FFI 往返热点。
- 对高频函数必须提供基准测试，至少覆盖 Python baseline 与 Rust implementation。

### 10.2 可靠性

- Rust runtime store / dispatcher 必须支持 crash 后恢复到可判定状态。
- event log 必须能重放 task terminal event，不依赖仅内存 `_running_tasks`。
- bundle revision pinning 必须在任务终态后释放，异常路径不得泄漏 retained revision。

### 10.3 安全

- 所有外部输入，包括 Skill script output、MCP tool output、upload 文件、SQL LLM output，必须在 Rust 或 Python facade 边界 fail-closed。
- Rust FFI 不得暴露真实文件路径、数据库连接串、API key、base_url、完整 prompt、完整 rows 到前端或 audit。
- `unsafe` Rust 默认禁止；确需使用必须有局部注释、审计说明和测试覆盖。

### 10.4 可观测性

- Rust kernel 必须输出结构化 tracing span / event，可映射到当前 audit event。
- 错误码必须稳定，供 Python 层、前端、测试与审计消费。
- 性能指标至少包含 duration、input/output size、truncated、retriable、error_type。

### 10.5 兼容性

- Python 3.13 / Conda `multi_agent` 环境必须可构建和运行 Rust extension。
- macOS 本地开发与 Linux CI/部署 wheel 构建必须有明确路径。
- Rust 化不得破坏现有分层 unittest、Skill bundle tests、frontend Vitest/build。

## 11. Rust 开发环境与依赖要求

### 11.1 本机工具链

推荐通过 rustup 安装 stable toolchain：

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
rustup toolchain install stable
rustup component add rustfmt clippy
cargo --version
rustc --version
```

仓库应新增或规划：

```text
rust-toolchain.toml
Cargo.toml workspace
.cargo/config.toml（如需要）
```

### 11.2 Python bridge 依赖

如采用 PyO3：

- Python / build dependency：`maturin`
- Rust crates：
  - `pyo3`
  - `serde`
  - `serde_json`
  - `thiserror`
  - `tracing`

### 11.3 Async / service / storage 依赖

- `tokio`
- `sqlx`，按模块启用 `sqlite` / `postgres` / `mysql` 与 `rustls` 相关 feature
- `uuid`
- `time`
- `sha2`
- `hmac`
- `pbkdf2`
- `regex`
- `jsonschema`
- `schemars`

### 11.4 测试与质量工具

- `proptest`：状态机、path sanitizer、SQL guard property tests
- `insta`：snapshot / golden tests
- `rstest`：参数化测试
- `criterion`：benchmark
- `cargo-fuzz`：SQL guard、manifest parser、MCP parser、path sanitizer fuzz
- `cargo-audit`：依赖漏洞扫描
- `cargo-deny`：license / duplicate / advisory policy
- `cargo-llvm-cov`：coverage
- `cargo-nextest`：Rust test runner

新增这些工具前应同步更新 `README.md`、本文件、`AGENTS.md` 与 CI/本地验证说明。

## 12. 数据、迁移与回滚

1. 首个 Rust kernel 必须以 Python facade 包装，保留旧 Python 实现作为 shadow 或 fallback，直到 golden tests 完成。
2. Storage / event log Rust 化必须先定义迁移兼容层，不得直接改变 SQLite schema 或未来 PostgreSQL schema 语义。
3. 每个 Rust kernel 必须有 feature flag 或 runtime config 支持回退到 Python 实现，直到稳定期结束。
4. SQLQuery Skill-owned Rust runtime 必须能随 Skill bundle 独立启停；移除该 Skill bundle 后主体框架不能残留 SQLQuery Rust dependency。
5. FFI wheel 构建失败时，默认开发环境应给出明确错误；生产部署必须提前构建 wheel，不在 runtime 启动时编译 Rust。

## 13. 验收标准

| 编号 | 验收项 | 证明方式 |
|---|---|---|
| AC-001 | SQLQuery 仍只通过 `skill.sql_query` 暴露，不出现主体 native SQLQuery capability | API capability list / ownership guard / grep 检查 |
| AC-002 | Rust lifecycle kernel 与 Python 旧行为完全一致 | Python golden tests + Rust unit/property tests |
| AC-003 | Rust storage/event kernel 不改变 API/SSE 行为 | `tests/api`、`tests/e2e`、event replay tests |
| AC-004 | Skill Runtime Rust kernel 保持 service binding 双重授权 | `tests/integrations/codex_skills`、`tests/capabilities/skill_tool` |
| AC-005 | MCP Runtime Rust kernel 对输入输出 fail-closed | MCP unit tests、schema tests、redaction tests、fuzz tests |
| AC-006 | Artifact/upload Rust kernel 防路径穿透和 symlink 泄漏 | path sanitizer property tests、artifact API tests |
| AC-007 | SQLQuery Skill-owned Rust kernel 不改变 query result artifact contract | `skill/sql-query/tests` 全量通过 |
| AC-008 | Rust build/test 纳入本地验证说明 | README / AGENTS / PRD 更新，`cargo test` 可运行 |
| AC-009 | FFI panic 不穿透 Python runtime | panic boundary tests |
| AC-010 | Rust 化后现有前后端最小验证仍通过 | 后端分层 unittest、Skill bundle tests、frontend Vitest/build |

## 14. 测试策略

### 14.1 TDD 要求

每个 Rust 化模块必须先有行为锁定测试，再实现 Rust kernel：

1. 先在 Python 测试中固定当前行为和边界错误。
2. 再写 Rust unit/property/fuzz tests。
3. 再接入 Python facade。
4. 最后跑分层回归和跨语言 golden tests。

### 14.2 最小测试面

- `tests/core`
- `tests/lifecycle`
- `tests/storage`
- `tests/api`
- `tests/e2e`
- `tests/integrations`
- `tests/capabilities/skill_tool`
- `tests/capabilities/mcp_tool`
- `skill/sql-query/tests`
- Rust `cargo test`
- Rust `cargo clippy -- -D warnings`
- Rust `cargo fmt --check`
- Rust benchmark / fuzz 按模块引入

### 14.3 性能验证

性能验证不得只看 micro-benchmark。至少包含：

1. 单 kernel benchmark；
2. Python facade FFI 往返 benchmark；
3. API task submit + event replay smoke；
4. Skill/MCP output 大 payload 处理；
5. SQLQuery Skill 典型查询链路。

## 15. Rollout / rollback

1. 每个 Rust kernel 独立 feature flag 发布。
2. 先 shadow compare，不改变生产路径。
3. 再单模块开启，观察错误码、duration、event replay、内存与任务成功率。
4. 出现异常可切回 Python implementation。
5. Rust kernel 稳定后，旧 Python 实现才能删除。
6. 删除前必须更新 PRD、README、CHANGELOG 与测试基线。

## 16. 风险、假设与开放问题

### 16.1 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| FFI 复杂度导致调试困难 | 开发效率下降 | 小 kernel、typed facade、golden tests |
| Rust sidecar 增加部署复杂度 | 运维成本上升 | 仅 dispatcher/store/sandbox 采用 sidecar，纯规则用 PyO3 |
| Python/Rust schema 双写漂移 | contract 不一致 | Rust schema 作为 canonical source，生成或校验 Python facade |
| LLM/prompt 逻辑过早 Rust 化 | 产品迭代变慢 | 明确 LLM/prompt glue 不整体迁移 |
| SQLQuery Rust 化回流主体框架 | 破坏 Skill-only 架构 | Skill-owned crate，主体框架只依赖 generic Skill service contract |

### 16.2 假设

1. 后端仍以 Python/FastAPI 作为对外 API 层。
2. 当前 `skill/sql-query` 作为可移除 Skill bundle 保持独立归属。
3. Rust 工具链可被本地 Conda `multi_agent` 环境和未来 CI 支持。
4. 首批 Rust 化不会直接改变数据库 schema。

### 16.3 开放问题

1. Rust workspace 最终目录命名使用 `native/`、`rust/` 还是其他名称，需要设计评审后确定。
2. dispatcher / store 是采用 PyO3 library 还是 sidecar service，需要结合生产部署目标确认。
3. PostgreSQL 正式化是否与 Rust store kernel 合并推进，需要另行拆分计划。
4. SQLQuery Skill-owned Rust runtime 是随 Skill bundle 构建 wheel，还是发布独立 native binary，需要 Skill 分发策略确认。

## 17. 当前证据锚点

- `CHANGELOG.md` 2026-05-13 条目记录 SQLQuery runtime、配置、领域文档与专项回归已收口到 `skill/sql-query/` bundle。
- `src/api/runtime.py` 当前只装配 `MainAgentExecutor`、`SkillExecutor`、`MCPToolExecutor`，并通过通用 service registry 向 Skill handler 注入 `mysql_readonly`、`llm.non_stream` 等服务。
- `skill/sql-query/SKILL.md` 当前声明 `capability_id: skill.sql_query`、`execution.mode: platform_service`、`trust_scope: project`。
- `src/lifecycle/task_state_machine.py` 当前承载可 Rust 化的状态转移规则。
- `src/storage/sqlite/repositories.py` 当前通过 `asyncio.to_thread` 包装同步 SQLAlchemy session，是后续 durable store / async Rust store 的候选边界。
- `src/integrations/codex_skills/execution.py` 当前承载 Skill trust gate、public root、handler allowlist 与 service binding。
- `src/integrations/mcp/` 与 `src/capabilities/mcp_tool/` 当前承载 MCP Runtime 与 generic MCP executor。
