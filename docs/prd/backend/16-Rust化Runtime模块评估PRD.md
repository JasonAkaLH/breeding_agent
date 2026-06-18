# Rust 化 Runtime 模块评估 PRD

- **范围**：后端 / Runtime substrate / Rust native kernel / Python-Rust 边界 / Skill-owned native runtime
- **文档状态**：决策基线已冻结；MCP Runtime 已进入联合 Phase 实施，MCP 细节以 `docs/prd/MCP/` 与 `docs/prd/rust/05-MCPRuntimeRustSidecarPRD.md` 为准
- **日期**：2026-05-14
- **触发背景**：业务能力已收口为可移除 Skill / MCP 形态，主体框架不应为具体业务能力保留 native capability；需要重新评估成熟 Agent 系统中哪些后端模块应 Rust 化。
- **关联文档**：
  - `docs/prd/backend/00-主代理框架PRD.md`
  - `docs/prd/backend/03-协作协议与任务生命周期.md`
  - `docs/prd/backend/04-状态存储与迁移策略.md`
  - `docs/prd/backend/11-Skill输出文件Artifact与下载PRD.md`
  - `docs/prd/backend/12-Skill一等Capability能力池PRD.md`
  - `docs/prd/backend/13-Skill动态加载与热部署PRD.md`
  - `docs/prd/backend/14-MCPRuntime实现需求PRD.md`
  - `docs/prd/backend/17-MCP长任务流式SSEPRD.md`
  - `docs/prd/backend/15-SkillExecutor实现需求PRD.md`
  - `docs/prd/MCP/README.md`
  - `docs/prd/rust/05-MCPRuntimeRustSidecarPRD.md`
- **外部参考**：
  - Rust 安装与 toolchain：<https://www.rust-lang.org/tools/install>
  - PyO3：<https://pyo3.rs/>
  - maturin：<https://www.maturin.rs/>
  - Tokio：<https://tokio.rs/>
  - SQLx：<https://github.com/launchbadge/sqlx>

## 1. 一句话结论

主体框架不应把 `ApiRuntime` 或 FastAPI 应用整体改写为 Rust；应把成熟 Agent 系统中**确定性、并发敏感、安全敏感、可重放、可类型约束**的 runtime substrate 下沉为 Rust native kernel，并保留 Python 作为 API composition root、LLM/provider glue、prompt 产品语义与快速演进层。

业务 Skill 如需 Rust 化，应在各自 Skill bundle 内实现 Skill-owned Rust runtime，并通过框架允许的 Skill contract、platform-service handler、service allowlist、artifact/event/audit contract 接入；不得要求主体框架为某个具体 Skill 增加 native capability、路由特判或专属运行时分支。

## 2. 背景与当前状态

### 2.1 业务 Skill 与主体框架归属边界

当前仓库的主体框架归属基线如下：

1. `src/capabilities/` 下保留 `main_agent`、`skill_tool`、`mcp_tool` 等通用能力目录；具体业务能力不得回流为主体框架 native capability。
2. 业务 Skill 的公开入口必须是 `skill.*` capability，并通过通用 Skill Executor / platform-service handler / service allowlist 接入。
3. API runtime 只装配通用 `SkillExecutor` 与 `SkillPlatformHandlerRegistry`；受控服务由 runtime allowlist 注入，不为单个业务 Skill 增加专属装配分支。
4. Skill 内部可以有领域 runtime、Rust core、PyO3 adapter、native binary 或 sidecar，但这些都属于 Skill bundle 自身实现细节，不暴露为主体 orchestration native node。

因此，本 PRD 的主体框架 Rust 化范围只覆盖通用 runtime substrate；Skill Rust runtime 只定义可被框架接受的接入形式和约束，不针对任何单个业务 Skill 做专项开发或优化。

### 2.2 当前 Python runtime 形态

当前后端以 Python async / await 为主，核心 runtime 分散在：

| 当前目录 / 模块 | 当前职责 | Rust 化关注点 |
|---|---|---|
| `src/api/runtime.py` | FastAPI runtime 装配、任务提交、执行调度、SSE 事件、Skill/MCP bundle revision pinning | 只抽 dispatcher / event / revision / cancellation kernel，不整体迁移 |
| `src/core/` | 跨模块 contracts、models、enums、基础错误 | canonical schema 与类型约束 |
| `src/lifecycle/` | task / node / mailbox / interrupt / cancel 状态规则 | 状态机不可非法转移、并发一致性 |
| `src/storage/` | SQLite storage facade、状态持久化、event append/list | durable store、event log、replay、lease、PostgreSQL 同构 |
| `src/integrations/agent_skills/` | Skill catalog、manifest、runtime bundle、script runner、service binding | sandbox、trust gate、fingerprint、handler allowlist |
| `src/capabilities/skill_tool/` | generic Skill Executor | mode / answer_mode / service binding / result normalization |
| `src/integrations/mcp/` + `src/capabilities/mcp_tool/` | MCP client runtime、tool binding、schema 校验、executor | JSON-RPC、transport、schema validation、untrusted output sanitization |
| `src/storage/artifact_files.py` + `src/api/upload_store.py` | artifact 文件、上传、hash、路径安全、quota | 文件安全与大对象管理 |
| `src/auth/` | password hash、captcha、session | 安全原语与 token/session 规则 |
| `src/orchestration/` | registry、scheduler、workflow plan、router、LLM planner、validator | deterministic DAG kernel 与 LLM glue 分离 |
| `skill/<skill-name>/runtime/` | Skill-owned domain flow | Skill-owned Rust core / adapter / domain validation / result shaping |

### 2.3 MCP Runtime 专项状态口径（2026-05-15）

MCP Runtime 已不再只是本总评估 PRD 中的抽象 Rust 化候选项，而是已经进入 `docs/prd/MCP/` 定义的联合 Phase 实施范围。当前口径如下：

1. `docs/prd/backend/16-Rust化Runtime模块评估PRD.md` 继续作为全局 Rust 化边界、非目标、安全原则、sidecar 策略与最终交付门禁的决策基线。
2. MCP 的具体功能范围、Phase 顺序、退出门禁、长任务 / Streamable HTTP / SSE / Tasks / API 事件桥接 / shadow enforce / legacy 下线要求，以 `docs/prd/MCP/` 为实施权威。
3. `docs/prd/rust/05-MCPRuntimeRustSidecarPRD.md` 继续作为 MCP Rust sidecar 专题 PRD，但其状态必须反映 Phase 0 / Phase 1 基线已落地、Phase 2-5 与最终 `enforce` canonical path 尚未完成。
4. 当前已具备 Rust workspace、`maf_mcp_runtime` sidecar/proto 骨架、health/readiness/version/compatibility handshake、Python sidecar facade、mode gate 与 shadow/enforce fail-closed gate；不得据此宣称完整 Rust MCP Runtime、完整 MCP 长任务流式 SSE 或 production enforce 已完成。
5. 完整生产口径只有在 `docs/prd/MCP/06-Phase5-ShadowEnforce生产门禁与Legacy下线PRD.md` 通过后才能宣称：Rust sidecar 承载 MCP protocol / transport / long-task durable registry / sanitizer，Python 只保留 facade / API / event bridge / capability wrapper。

## 3. 目标

### 3.1 产品目标

1. 在不改变现有用户行为、API 行为、Skill 行为与前端事件契约的前提下，为后端建立 Rust native runtime 演进边界。
2. 提升成熟 Agent 系统的核心属性：状态一致性、任务可重放、并发安全、资源隔离、安全 fail-closed、协议解析稳定性、运行时可观测。
3. 明确主体框架与 Skill-owned native runtime 的责任划分，避免任何具体业务 Skill 侵入框架内核。
4. 让未来 PostgreSQL、分布式 dispatcher、MCP/Skill sandbox、大文件 artifact、只读 DB 访问等高风险模块具备 Rust 下沉路径。

### 3.2 工程目标

1. 将 Rust 化候选拆成可验证 kernel，而不是按 Python 文件整包迁移。
2. 每个 Rust kernel 必须有 Python 兼容 facade，保持现有测试与 API 契约可回归。
3. 优先 Rust 化纯规则、协议、状态、存储、安全、文件、DB 访问等确定性模块。
4. 保持 Python 层负责 FastAPI route、DTO、LLM provider SDK、prompt 语义、产品策略和 orchestration 高层装配。
5. 建立 Rust toolchain、Cargo workspace、sidecar service、PyO3 小 kernel、测试、审计、发布与运维标准。

### 3.3 不以工期作为判断标准

本 PRD 的“应 Rust 化”只表达架构适配度与长期收益，不代表立即实施优先级。实施顺序应另行通过开发计划、PRD 拆分和测试计划确定。

### 3.4 长期交付级技术栈原则

本项目按长期交付级 Agent 系统建设，不按一次性 demo、临时 PoC 或“当前能跑即可”的标准设计 Rust 化边界。Rust 化评估必须优先考虑生产级完整技术栈：独立 runtime service、清晰进程边界、typed protocol、健康检查、可观测性、灰度开关、版本兼容、故障回退、CI 构建、跨平台 wheel / binary 发布、存储迁移与安全审计。

当“短期接入成本更低”和“长期 runtime 边界更稳”冲突时，PRD 默认选择长期交付级边界；实施排期可以分阶段，但目标架构不能因短期省工而退化。

## 4. 非目标

1. 不把 FastAPI app、API routes、DTO 全量迁移为 Rust。
2. 不把 `ApiRuntime` 作为一个整体类迁移为 Rust；只拆其内部 runtime substrate。
3. 不把 LLM provider SDK 调用层整体迁移为 Rust。
4. 不把主代理 prompt 构造、产品话术、LLM planner prompt 变成 Rust 固定逻辑。
5. 不为任何具体业务 Skill 创建主体框架 native capability。
6. 不在本 PRD 中引入 LangChain、LangGraph、AutoGen 等现成 Agent 框架。
7. 不绕过现有 TDD、分层 unittest、Skill bundle 测试与前端契约测试。
8. 不因引入 Rust 放宽现有 secret、受控 DB readonly service、Skill/MCP trust、artifact path 安全约束。

## 5. 用户、维护者与影响面

| 角色 / 系统 | 关注点 | 本 PRD 承诺 |
|---|---|---|
| 业务用户 | 对话、任务进度、结果和 artifact 行为不能变化 | Rust 化必须行为兼容，前端事件与 API response 不破坏 |
| 后端维护者 | 模块边界、测试、部署复杂度 | Rust kernel 有清晰 facade、golden tests、rollback path |
| Skill 作者 | Skill 是否仍可独立发布 / 移除 | 主体 Rust kernel 不绑定业务 Skill；Skill Rust runtime 必须适配框架 contract |
| 安全 / 运维 | service binding、secret、文件、外部 tool 输出 | Skill/MCP/file/auth/DB 边界 fail-closed，审计不可弱化 |
| 前端 | SSE、artifact 展示与通用结果卡片兼容 | Rust 化不改变 frontend event schema、artifact metadata 与既有展示契约 |
| 未来多实例部署 | dispatcher、store、event replay、lease | Rust runtime store / event log 应支持多实例一致性演进 |

## 6. Rust 化判断原则

模块满足以下条件越多，越应该 Rust 化：

1. **确定性强**：主要是状态机、校验器、协议解析、文件路径、hash、schema、序列化。
2. **安全敏感**：涉及 secret、路径穿透、外部输入、query / policy guard、auth、sandbox、MCP/Skill trust boundary。
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
| RUST-P0-005 | `src/integrations/agent_skills/` | Skill manifest parser、bundle fingerprint、public root guard、trust gate、handler allowlist、script sandbox policy | Python handler 兼容、Skill 作者接口 | Skill 安全边界可审计 |
| RUST-P0-006 | `src/capabilities/skill_tool/` | execution mode、answer_mode、service binding、result normalization、error mapping | final answer glue、Python handler call bridge | `skill.*` 一等执行器稳定化 |
| RUST-P0-007 | `src/integrations/mcp/` + `src/capabilities/mcp_tool/` | JSON-RPC、transport state、schema validation、tool binding、output truncation/sanitization | runtime config 注入、业务 capability 包装 | 已进入 `docs/prd/MCP/` 联合 Phase 实施；最终以 Rust sidecar 稳定外部 tool 不可信边界 |
| RUST-P0-008 | artifact/upload/file store | storage key、path normalization、hash、quota、retention、zip/archive safety | API download response、auth dependency | 防路径穿透、防大文件资源泄漏 |

### 7.2 P1：独立专题与条件候选

| 编号 | 当前模块 | 应 Rust 化部分 | 保留 Python 部分 | 关键收益 |
|---|---|---|---|---|
| RUST-P1-001 | `src/auth/services.py` | password hash verify、HMAC、captcha verify、session token / TTL core | HTTP cookie/session wiring、页面交互 | 安全原语一致性 |
| RUST-P1-002 | `src/orchestration/` deterministic kernel | scheduler、DAG validator、completion policy、backpressure、payload policy | LLM planner、router glue、provider fallback、prompt | 条件候选；不进入必做 Rust 化目标集 |
| RUST-P1-003 | `src/integrations/mysql_readonly.py`、`src/mysql_engine.py` | async DB pool、readonly enforcement、timeout、row decoding、query result shape | service registry、配置读取 | DB I/O 与只读约束更稳 |
| RUST-P1-004 | `skill/<skill-name>/runtime/` + `skill/<skill-name>/native/` | shared Rust core、adapter contract、domain validation、controlled service client、result shaping | Skill manifest、LLM prompt wording、platform handler facade | Skill-owned Rust runtime 可按框架允许形式稳健接入 |
| RUST-P1-005 | audit/event serialization | audit payload sanitizer、event serializer、privacy filter | audit sink 注入 | 审计字段一致、敏感信息不泄露 |

冻结修订：RUST-P1-002 仅保留为条件候选编号，不代表必做 Rust 化范围。未来如需启动 `maf_orchestration_kernel`，必须另开实施 PRD，证明 deterministic DAG / scheduler / payload policy 存在性能或可靠性瓶颈，并通过 Python baseline shadow compare；LLM planner、router glue、provider fallback、prompt 和产品策略不得迁入 Rust。

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
  ├─ maf_data_access
  ├─ maf_audit_sanitizer
  └─ maf_orchestration_kernel

Skill-owned native layer
  └─ skill/<skill-name>/native/
```

### 8.2 集成方式

| 场景 | 推荐集成方式 | 说明 |
|---|---|---|
| 纯规则 / 校验 / 状态机 | PyO3 extension | Core/Lifecycle 主路径冻结为 PyO3 extension；低延迟、可直接替换 Python 函数，适合 lifecycle / core / sanitizer |
| 持久化 / dispatcher / event log | Rust sidecar service | 作为长期交付级生产边界冻结；承担 lease、durable queue、event replay、crash recovery 与多实例协调 |
| MCP / Skill sandbox | Rust sidecar service 或 isolated process manager | MCP Runtime 最终接入方式冻结为独立 Rust sidecar；通用 Skill Runtime 最终方案冻结为 PyO3 policy kernel + Rust Skill Sandbox sidecar；需要进程、timeout、I/O 限额、外部输入隔离，并与 runtime sidecar 共享运维与可观测标准 |
| Skill-owned Rust runtime | shared Rust core + PyO3 wheel + native binary / sidecar adapters | 同一 Rust core 支持多种适配器；按隔离、并发、部署需要选择，不进入主体框架 |
| 前端大数据处理 | WASM | 非默认，仅在数据量证明需要时启用 |

#### 8.2.1 Dispatcher / store sidecar 冻结决策

dispatcher / store / event log 的目标集成方式已冻结为 **Rust sidecar service**，不是 PyO3 library。PyO3 仍可用于纯规则、小校验器和过渡期 compatibility facade，但不得把 task dispatcher、durable queue、lease、active task recovery、event replay hub 长期塞回 Python 进程内。

sidecar 必须作为长期生产 runtime 设计；进程管理最终方案冻结为生产环境由外部进程管理器 / 容器编排管理 sidecar，Python runtime 不负责生产 sidecar 生命周期，只负责 client connect、health/readiness/version check、shutdown drain 协调、protocol compatibility check 与受限 fallback / fail-closed。dev/test 可提供 launcher，但不作为生产运行方式。

sidecar 至少具备：

1. 明确的 typed protocol；正式生产协议冻结为 gRPC / tonic + protobuf。protobuf schema 归属与版本策略冻结：所有生产 sidecar proto 统一位于 `native/proto/maf/<domain>/v1/`，初始 domain 为 `common`、`runtime`、`skill`、`mcp`；`v1` / `v2` 是协议 schema 主版本 namespace，breaking change 必须新建 `v2` package；Rust server 与 Python client 必须从同一 proto source 生成或校验。HTTP JSON 只允许作为本地开发或极早期 spike，不得作为生产协议，进入正式实现前必须迁移到 gRPC / tonic。
2. health / readiness / liveness 检查。
3. API version 与 protocol compatibility policy。
4. durable queue、task lease、event append/replay、cursor、幂等键。

协议兼容与滚动升级策略已冻结：Python sidecar client / PyO3 facade 必须在启动、connect、首次调用、reconnect 和 sidecar version 变化时校验 component、protocol / contract version、schema hash、error code table hash、build version、supported feature flags 与 client version range。兼容 minor 变更允许滚动升级；breaking change 必须进入新的 `v2` proto package / contract major version，或显式实现 dual-stack client / server。`enforce` 下协议 / contract 不兼容必须 fail closed；`shadow` 下可回退 Python legacy path 并写 `rust.protocol_incompatible` audit event。

网络暴露与服务发现策略已冻结：Rust sidecar 不得对公网、前端、用户、普通 Skill 或外部系统直接暴露；只允许 Python runtime / 受控内部组件通过 Unix domain socket、loopback、同 Pod / 同主机内部网络、私有服务发现或 mTLS 内网访问。sidecar endpoint 必须来自部署配置 / runtime allowlist，不能由用户输入、Skill manifest、LLM 输出或外部 tool output 指定。health / readiness / metrics / debug endpoint 只能内部访问；`enforce` 下公网绑定、未授权 client、未配置 mTLS 的跨主机访问或非 allowlist service discovery 必须 fail closed。

资源限制、backpressure、deadline 与 cancellation 策略已冻结：所有 Rust sidecar / kernel 请求必须有 deadline；禁止无界队列、无界 stream、无界 stdout/stderr 与无界 payload。每个 sidecar 必须声明 max in-flight、queue size、queue wait、request / response size、deadline、retry、cancel、shutdown drain 与 overload typed error。默认 retry max attempts 为 3 次总尝试（含 initial attempt），100ms 起指数退避，最大 1s，±20% jitter；health deadline 为 1s，readiness / version deadline 为 2s，shutdown drain 为 30s，audit / metrics event 单条上限为 64KB，默认 request size 为 1MB、response size 为 4MB。非幂等请求禁止自动重试。

配置、secret 与 identity 管理策略已冻结：Rust sidecar / PyO3 kernel 配置只允许来自部署配置、环境变量、secret manager、只读配置文件或 runtime allowlist；不得来自用户输入、Skill manifest、LLM 输出或外部 tool output。secret、token、mTLS key、数据库连接串、provider key、session / HMAC key 不得写入 tracked 文件、audit、metrics、structured logs、typed error message 或 `safe_metadata`。跨主机 sidecar 访问必须使用 mTLS 或等价服务身份校验；sidecar 必须支持 secret rotation，经受控 reload 或滚动重启生效；`enforce` 下配置缺失、secret 缺失、identity mismatch、证书过期、证书不受信、client identity 未授权或 secret reload 失败必须 fail closed。

`enforce` 故障策略已冻结：Dispatcher / Store / Event sidecar 进入 `enforce` 后，task submit / create、node state transition、event append、lease acquire / renew / release、cancellation token 写入、bundle revision pin / release 等状态写入类操作失败必须 fail closed，不允许自动 fallback 到 Python legacy store。只允许 health/status、metrics、已证明无副作用 read-only snapshot 等极少数只读降级；sidecar unavailable、protocol version 不兼容或写入失败时必须返回 `runtime_store_unavailable` / `dispatcher_unavailable` 等稳定 typed error，由 API 层暴露为可重试失败。
5. shutdown drain、crash recovery、重复启动保护。
6. tracing / metrics / structured logs。
7. Python `StoragePort` / runtime facade 只作为 client adapter，不再拥有最终 dispatcher 状态。
8. 本地开发必须提供一键启动或测试 fixture，避免 sidecar 变成手工运维负担；该 launcher 不作为生产运行方式。

#### 8.2.2 PostgreSQL 正式化延期决策

PostgreSQL 正式化当前不纳入本 PRD 的立即落地范围，不与 Rust runtime sidecar 主线强行合并推进。Dispatcher / Store / Event sidecar 本专题最终交付边界是 SQLite adapter 与 PostgreSQL-compatible contract，不实现 PostgreSQL production adapter；当前 Rust sidecar 设计必须预留 PostgreSQL production adapter、schema ownership 与 migration policy，但 PostgreSQL 生产化作为独立升级项单独决策、单独 PRD / 测试计划推进。

在后续升级前，SQLite 仍作为本地开发、测试 fixture 与过渡存储后端；不得因为 PostgreSQL 延期而削弱 sidecar 的长期生产边界设计。

#### 8.2.3 通用 Skill Runtime 最终方案

通用 Skill Runtime 最终方案冻结为 **Rust policy kernel + Rust sandbox sidecar 双层架构**。`maf_skill_runtime` 通过 PyO3 提供 manifest parsing、bundle fingerprint、public root guard、service binding、handler allowlist 与 `x_runtime.rust` contract 校验；不可信或进程型执行边界，包括 `python_subprocess`、native binary adapter、外部脚本执行、资源限制、timeout、stdout/stderr 限额与执行审计，最终必须进入 Rust Skill Sandbox sidecar / isolated process manager。

`platform_service` trusted handler 仍由 Python runtime 装配和调用，但调用前必须经过 Rust policy kernel 的 allowlist / trust gate 校验。Skill-owned Rust runtime 仍只允许 Skill 自己在 `skill/<skill-name>/native/` 内按指南提供 PyO3 wheel、native binary 或 sidecar adapter；框架不为具体 Skill 做专属适配。

#### 8.2.4 Skill-owned Rust runtime 多适配器决策

Skill-owned Rust runtime 的发布形态已冻结为 **shared Rust core + 多适配器并存**。同一个 Skill-owned Rust core crate 可以同时提供 PyO3 wheel、CLI binary、长期运行的 sidecar service adapter；运行时按隔离、并发、部署与依赖需求选择具体适配器。

该决策的核心原则是：**框架定义可接受的 Skill Rust contract，Skill 必须适配框架；框架不为某个 Skill 反向适配专属 Rust 形态。**

该决策的约束是：

1. 业务逻辑只能写在 shared Rust core crate 中，不允许 PyO3 与 binary / sidecar 各自复制一套领域校验、受控服务调用或 result shaping 规则。
2. PyO3 wheel 适合低延迟库型调用、进程内 pure kernel、与现有 Python Skill handler 平滑兼容。
3. native binary 适合一次性离线执行、独立 CLI 调试、受控 subprocess 与无常驻服务场景。
4. sidecar service 适合长连接池、高并发、强隔离、独立健康检查、资源限额与崩溃隔离场景。
5. Skill manifest / platform handler / runtime config 必须能声明或选择 adapter mode；默认选择不得绕过 service allowlist、secret 管理和 artifact/event 审计。
6. 所有 adapter 必须共享 golden tests 和 contract tests，确保同一输入在可比场景下输出一致。
7. 移除 Skill bundle 时，主体框架不得残留该 Skill 的 PyO3 module、binary、sidecar 配置或 capability 注册。
8. 普通用户级 Skill 不得要求框架在运行时 `cargo build`、下载依赖、执行任意 native binary 或开放任意本机端口；项目级 trusted Skill 也必须经 manifest 声明、runtime allowlist 与构建产物审计。

### 8.3 Rust workspace 建议

Rust workspace 目录命名已冻结：主体框架 Rust native workspace 使用 `native/`；Skill 自有 Rust runtime 使用 `skill/<skill-name>/native/`。首批主体 crate 命名也已冻结；当前 PRD 只冻结目录与命名边界，不要求立即创建目录或空 crate。

新增大型 `native/` 目录前仍必须先完成对应实现 PRD / 测试计划 / 评审。若评审通过，推荐 workspace 拆分。冻结修订：`maf_orchestration_kernel` 仅作为条件候选 crate 名保留，不属于必做 Rust 化目标集，不得创建空 crate 或占位测试。

| Crate | 职责 | 波次 / 状态 |
|---|---|---|
| `maf_core_types` | Core enum / struct / JSON schema / serde contract | Wave 1；首批冻结 |
| `maf_lifecycle` | Task / node / mailbox / interrupt / cancel transition table | Wave 1；首批冻结 |
| `maf_runtime_store` | SQLite repository、future PostgreSQL contract、transaction、lease、idempotency | Wave 2；首批冻结 |
| `maf_event_log` | Event append、replay、cursor、SSE snapshot support | Wave 2；首批冻结 |
| `maf_task_dispatcher` | Task queue、active registry、cancellation token、bundle revision pinning | Wave 2；首批冻结 |
| `maf_skill_runtime` | Skill manifest、bundle fingerprint、trust gate、PyO3 policy kernel、Skill Sandbox sidecar binary target | Wave 3；首批冻结 |
| `maf_mcp_runtime` | MCP protocol、transport state、tool binding、schema validation | Wave 3；首批冻结 |
| `maf_artifact_store` | Artifact/upload/file path、hash、quota、retention、archive safety | Wave 4；首批冻结 |
| `maf_auth_core` | Password / HMAC / session / captcha primitives | Wave 4；首批冻结 |
| `maf_data_access` | Readonly DB adapter、row shape、timeouts | Wave 4；首批冻结 |
| `maf_audit_sanitizer` | Audit payload sanitizer、event serializer privacy filter、redaction rule | Wave 4；首批冻结 |
| `maf_orchestration_kernel` | DAG validation、scheduler、completion policy、backpressure | 条件候选；不进入必做 Rust 化目标集创建 |

## 9. 功能需求

### 9.1 Core contract Rust kernel

- RUST-FR-001：系统必须有唯一 canonical core type/schema 来源；`maf_core_types` 负责 core enums / structs、stable error code、JSON schema / serde contract。
- RUST-FR-002：所有跨模块 `Task`、`TaskNode`、`EventRecord`、`Artifact`、`CapabilityExecutionResult` 必须可 serde round-trip。
- RUST-FR-003：FFI 边界必须把 Rust error 映射为 Python typed exception，不允许 panic 穿透；Rust typed error 必须包含 `code`、`message`、`retriable`、`category`、`safe_metadata`。
- RUST-FR-004：Python `src/core` 只保留 facade / adapter；不得独立定义与 Rust 冲突的 enum 值、默认值、字段语义或 error code 语义。
- RUST-FR-005：Python facade 生成策略采用“生成 contract artifact + 手写薄 facade”；Rust 必须生成或导出 JSON schema、error code table、enum/value snapshot 与 golden fixtures，CI 校验 Python facade 与这些 artifact 一致。
- RUST-FR-006：所有 Rust kernel / sidecar 的结构化 response、typed error、audit event、metrics event、shadow diff、retry event 与 correction event 必须按 schema / protobuf / contract artifact 校验；校验失败必须进入 typed error 与 retry / fail-closed 策略。
- RUST-FR-007：Python sidecar client / PyO3 facade 必须执行 protocol / contract compatibility handshake；`enforce` 下 component、version、schema hash、error code table hash、feature flags 不兼容必须 fail closed。
- RUST-FR-008：Rust sidecar endpoint 必须来自部署配置 / runtime allowlist；sidecar 不得被公网、前端、用户、普通 Skill 或外部系统直连。
- RUST-FR-009：Rust sidecar / kernel 必须执行 resource limit / backpressure / deadline / cancellation 基线；过载、排队超时、deadline exceeded、payload too large、stream idle timeout 与 cancelled 必须映射为稳定 typed error。
- RUST-FR-010：Rust sidecar / kernel config、secret 与 identity 只允许来自部署配置 / 环境变量 / secret manager / 只读配置 / runtime allowlist；`enforce` 下缺失、过期、不匹配或泄露风险必须 fail closed。

### 9.2 Lifecycle Rust kernel

- RUST-FR-010：task/node/mailbox/interrupt/cancel 转移必须由 `maf_lifecycle` Rust transition table 统一判定。
- RUST-FR-011：非法状态转移必须 fail-closed，并返回稳定错误码。
- RUST-FR-012：取消、interrupt answer、resume、late result acceptance 必须有 property tests 与 Python golden tests。
- RUST-FR-013：Python `src/lifecycle` 只保留 facade / adapter；不得独立定义与 Rust 冲突的状态转移规则、默认值或 error code 语义；`enforce` 后 Rust 判定为准。
- RUST-FR-014：`maf_lifecycle` 必须生成或导出 transition table snapshot，作为 Python facade / golden tests / CI 漂移检查的输入。

### 9.3 Runtime store / event log / dispatcher

- RUST-FR-020：任务提交、计划生成、节点执行、事件追加必须支持 durable event log。
- RUST-FR-021：dispatcher 必须支持 task lease、cancellation token、active task recovery、bundle revision pin/release。
- RUST-FR-022：SQLite 与 PostgreSQL 必须保持逻辑同构；PostgreSQL 上线不得改变 Python API contract。
- RUST-FR-023：SSE 初始 replay 与 live event 订阅必须基于同一 event cursor 语义。

### 9.4 通用 Skill Runtime / Skill Executor

- RUST-FR-030：Skill manifest parser、execution config、public root guard、bundle fingerprint 必须由 Rust kernel 提供确定性结果。
- RUST-FR-031：`platform_service` service binding 必须继续满足 manifest 声明 + runtime allowlist 双重授权。
- RUST-FR-032：普通 `python_subprocess` Skill 不得获得受控 DB service、内部 LLM、secret 或完整环境变量。
- RUST-FR-033：Skill script runner 必须限制路径、symlink、cwd、timeout、stdout/stderr、output files，并产出可审计错误。

### 9.5 MCP Runtime

本节只保留 MCP Runtime Rust 化的全局功能约束。MCP 的详细实施范围、Phase 依赖、验收与退出门禁以 `docs/prd/MCP/` 为准；`docs/prd/rust/05-MCPRuntimeRustSidecarPRD.md` 负责承接 Rust sidecar 专题状态。

当前状态：Phase 0 / Phase 1 基线已落地，包含 contract fixture / proto 草案、`native/` workspace、`maf_mcp_runtime` sidecar 骨架、Python facade、compatibility handshake、mode gate 与 fail-closed / shadow fallback 逻辑。Phase 2-5 仍是未完成生产范围，包括 Rust sidecar 内 Streamable HTTP / 多事件 SSE、MCP Tasks durable registry、executor/API/SSE 完整桥接、shadow promotion、production enforce 与 Python legacy 下线。

- RUST-FR-040：MCP JSON-RPC request / response / error 必须由 Rust protocol layer 校验。
- RUST-FR-041：tool input 必须按 planner allowlist 与 JSON Schema fail-closed 校验。
- RUST-FR-042：tool output 必须支持 size limit、schema validation、secret redaction、untrusted external content notice。
- RUST-FR-043：MCP bundle activation 必须原子化：新 bundle 准备成功后再切换，失败保留旧 bundle。

### 9.6 Artifact / upload / file safety

- RUST-FR-050：storage key、artifact id、filename、upload id 必须经过 Rust path normalization 与 escape check。
- RUST-FR-051：所有 managed artifact 必须有 size、sha256、retention metadata。
- RUST-FR-052：zip/archive 生成与清理必须防 zip-slip、路径穿透、symlink 泄漏。
- RUST-FR-053：上传 preview 必须有大小、格式、UTF-8、行列数量限制。

### 9.7 Skill-owned Rust runtime 接入要求

- RUST-FR-060：Skill Rust 化不得重新注册主体框架 native capability；公开入口仍必须是 `skill.*` capability。
- RUST-FR-061：Skill-owned Rust runtime 必须只通过框架允许的 Skill execution mode、platform-service handler、service allowlist 或受控 adapter 进入系统。
- RUST-FR-062：Rust core 必须是 Skill bundle 内部实现细节，不得要求 orchestration、API route、前端或 capability registry 为该 Skill 写专属分支。
- RUST-FR-063：Rust adapter 不得读取未授权 secret、完整环境变量、任意本地路径或未 allowlist 的平台服务。
- RUST-FR-064：adapter 输出必须回到标准 Skill output / artifact / event / audit contract；不得自定义下载接口、事件通道或前端专属协议。
- RUST-FR-065：PyO3、native binary、sidecar adapter 必须共享同一 Rust core 与 contract tests，不得复制业务逻辑。
- RUST-FR-066：Skill manifest 中的 Rust metadata 必须遵循 `.codex/skills/breeding-skill-builder/references/Skill构建指南.md` 的 `x_runtime.rust` 约定；adapter、contract_version 或构建产物未被 runtime allowlist 支持时必须 fail closed。
- RUST-FR-067：Skill 请求执行路径不得触发 `cargo build`、`cargo run`、依赖下载、动态链接任意本地路径或启动未注册 sidecar。


### 9.8 跨模块最终交付门禁

- RUST-FR-070：所有 PyO3 wheel、sidecar binary / image、native binary 与 Skill-owned Rust artifact 必须由 CI / 部署流水线预构建，具备 checksum、SBOM、Cargo.lock digest、toolchain / target / feature / build profile 元数据、contract / proto hash 与 provenance；runtime 只能加载 allowlist 且校验通过的 artifact，请求路径不得编译、下载或替换 Rust 产物。
- RUST-FR-071：每个 Rust kernel / sidecar 必须建立 Python baseline、Rust implementation、PyO3 FFI 或 sidecar RPC overhead、P50/P95/P99 latency、throughput、CPU、memory 与 payload size 性能基线；进入 `enforce` 不得突破模块 SLO，默认 P95 不高于 Python legacy 110%，性能回归必须阻断发布。
- RUST-FR-072：任何 Rust-owned 持久状态、sidecar schema、event log、artifact metadata、bundle/runtime registry 或 contract artifact 版本状态变更，必须具备 schema version、migration lock、preflight、dry-run、backup、restore、replay 校验与 rollback / roll-forward runbook；破坏性迁移无备份一律禁止。
- RUST-FR-073：Rust canonical 稳定后，最终交付版必须下线重复 Python legacy 语义；Python 只保留 facade / sidecar client / DTO adapter，不再保留重复状态机、store 写路径、安全策略、MCP sanitizer、Skill sandbox policy 或 audit sanitizer 语义。
- RUST-FR-074：任一 Rust sidecar / PyO3 kernel 进入 `enforce` 前必须具备 dashboard、alert、SLO、health/readiness/version 诊断、drain / restart / rollback / restore runbook、on-call 分级与演练记录；无 runbook、无告警或无回滚 / 恢复演练不得进入生产最终路径。

## 10. 非功能需求

### 10.1 性能

- Rust kernel 应降低大 payload 校验、event replay、artifact path/hash、MCP output sanitization、DB rows shaping 的 CPU 与内存抖动。
- 不要求所有 Rust kernel 在首版都快于 Python；但不得引入明显 FFI 往返热点。
- 对高频函数必须提供基准测试，至少覆盖 Python baseline、Rust implementation、PyO3 FFI / sidecar RPC overhead、P50/P95/P99、throughput、CPU、memory 与 payload size。
- 性能门禁冻结：进入 `enforce` 不得突破模块 SLO；默认 P95 latency 不高于 Python legacy 110%，error rate 不高于 legacy，CPU / memory 不得出现无解释劣化，性能回归必须阻断发布。

### 10.2 可靠性

- Rust runtime store / dispatcher sidecar 必须支持 crash 后恢复到可判定状态。
- event log 必须能重放 task terminal event，不依赖仅内存 `_running_tasks`。
- bundle revision pinning 必须在任务终态后释放，异常路径不得泄漏 retained revision。
- Rust-owned 持久状态必须可迁移、可备份、可恢复、可 replay 校验；migration 失败或 restore drill 未通过时不得进入 `enforce`。

### 10.3 安全

- 所有外部输入，包括 Skill script output、MCP tool output、upload 文件、LLM-generated tool / query output，必须在 Rust 或 Python facade 边界 fail-closed。
- Rust FFI 不得暴露真实文件路径、数据库连接串、API key、base_url、完整 prompt、完整 rows 到前端或 audit。
- `unsafe` Rust 默认禁止；确需使用必须有局部注释、审计说明和测试覆盖。
- Rust sidecar network exposure 策略已冻结：只允许内部通道访问，不允许公网暴露；health / readiness / metrics / debug endpoint 也只能内网访问。`enforce` 下公网绑定、未授权 client、非 allowlist discovery 或未配置 mTLS 的跨主机访问必须 fail closed。
- Rust resource limit 策略已冻结：禁止无界队列 / stream / stdout-stderr / payload；所有请求必须有 deadline；queue full、queue wait timeout、payload too large、deadline exceeded、stream idle timeout 与 shutdown draining 必须 fail closed 或按模块策略返回可重试 typed error。
- Rust config / secret / identity 策略已冻结：secret、token、mTLS key、DB DSN、provider key、session / HMAC key 不得进入 tracked 文件、audit、metrics、logs、error message 或 safe metadata；跨主机 sidecar 必须 mTLS 或等价身份校验；secret rotation 只记录 version / fingerprint。
- Rust artifact provenance / SBOM / supply-chain 策略已冻结：所有 Rust wheel / binary / image / Skill artifact 必须由 CI / 部署流水线预构建并携带 checksum、SBOM、Cargo.lock digest、contract / proto hash 与 provenance；runtime 只加载 allowlist 校验通过的 artifact。

### 10.4 可观测性

- Rust kernel 必须输出结构化 tracing span / event，可映射到当前 audit event。
- 错误码必须稳定，供 Python 层、前端、测试与审计消费。
- 性能指标至少包含 duration、input/output size、truncated、retriable、error_type。
- typed error、retry、auto-correction 事件必须输出 structured audit / metrics，且不得包含 raw provider error、secret、路径、连接串、完整 prompt 或完整 rows。
- Rust structured output validation 策略已冻结：gRPC response / error response、PyO3 return object、audit event、metrics event、shadow diff event、retry event、correction event、health/readiness/version response 必须先通过 schema / protobuf / contract artifact 校验，再进入 Python facade、API、前端、audit 或 metrics sink。
- 结构化输出校验失败默认映射为 `contract` 类 typed error 并 fail closed；只有 transient / incomplete transport response、幂等或无副作用操作、retry policy 和脱敏 retry audit 全部满足时，才允许自动重试。
- Python 必须向 Rust sidecar / PyO3 kernel 透传 trace context；Rust event 至少包含适用的 trace_id、conversation_id、task_id、node_id、component、mode、sidecar_version、protocol_version、duration_ms、error_code、category、retriable、attempt、input/output fingerprint 与 redaction_applied。

### 10.5 兼容性

- Python 3.13 / Conda `multi_agent` 环境必须可构建和运行 Rust sidecar client / PyO3 extension。
- 当前仓库已因 MCP Phase 1 引入 `native/` Rust workspace、`rust-toolchain.toml` 与 `maf_mcp_runtime` skeleton；任何后续 Rust crate、PyO3 wheel、sidecar binary、Python Rust build dependency 或 CI 门禁扩展，仍必须同步更新 `README.md`、`AGENTS.md`、`requirements.txt` 或独立 Rust build 文档。
- Rust sidecar health / readiness / version response 必须包含 component、build_version、protocol_version、schema_hash、error_code_table_hash、supported_features、min_client_version、max_client_version；readiness 只有在 compatibility handshake 通过后才为 ready。
- PyO3 kernel / Python facade 必须在 import / 初始化 / 首次调用前校验 contract artifact；校验字段至少包含 component、contract_version、schema_hash、error_code_table_hash 与 supported_features。
- 兼容 minor 变更允许滚动升级；breaking change 必须进入 `v2` proto package / contract major version，或显式实现 dual-stack client / server 并提供兼容矩阵测试。
- `shadow` 阶段不兼容可回退 Python legacy path 并写 audit；`enforce` 阶段不兼容默认 fail closed。
- sidecar 服务发现必须来自部署配置、环境变量或 runtime allowlist；不得接受用户输入、Skill manifest、LLM 输出或外部 tool output 指定任意 sidecar 地址、端口、socket path 或 binary path。
- sidecar resource limits v1 默认生产基线：Dispatcher / Store / Event 为 max in-flight `min(64, cpu * 4)`、queue 1024、写 deadline 1-3s、event payload 256KB、replay page 1000 events 或 1MB；Skill Sandbox 为 max concurrent `min(8, cpu)` 默认 4、per-skill 2、queue 64、默认 timeout 60s、hard timeout 300s、stdout/stderr 各 1MB；MCP Runtime 普通短 tool call 为并发 16、per server 4、queue 128、call_tool 默认 60s / hard cap 300s、raw output 8MB、sanitized output 4MB、stream idle 30s；MCP 长任务 / 完整流式 SSE 的持续时间、idle timeout、reconnect、task status 与 cancellation 以 `docs/prd/backend/17-MCP长任务流式SSEPRD.md` 为准；DataAccess readonly 为默认 10s / hard cap 30s、500 rows、100 columns、10MB result；upload preview 10MB、archive hard cap 60s。
- sidecar / PyO3 kernel 配置只允许来自部署配置、环境变量、secret manager、只读配置文件或 runtime allowlist；secret rotation 必须通过受控 reload 或滚动重启完成，rotation 后重新执行 readiness、compatibility handshake 与身份校验。
- Rust artifact allowlist 必须校验 component、artifact id、version、checksum、schema / proto / contract hash、target triple 与 provenance；artifact 缺失、checksum mismatch、provenance 缺失、SBOM 缺失或 allowlist 未授权在 `enforce` 下必须 fail closed。
- CI / 发布产物矩阵已冻结：任一 Rust 代码进入 `native/` 或 `skill/<skill-name>/native/` 后，必须启用 `cargo fmt`、`cargo clippy`、`cargo test`、`cargo nextest`、`cargo audit`、`cargo deny` 必跑门禁；PyO3 wheel 用 `maturin` 构建，sidecar binary 用 Cargo 构建。
- 平台基线已冻结：macOS arm64 用于本地开发与调试，Linux x86_64 是生产部署基线；生产 sidecar 交付形态以 Linux container image / binary 为主；Windows 暂不作为必需发布目标。
- Rust 化不得破坏现有分层 unittest、Skill bundle tests、frontend Vitest/build。
- 所有 Rust kernel / sidecar / 子模块统一使用 `MAF_RUST_<COMPONENT>_MODE=off|shadow|enforce` runtime config 命名规则；默认 `off`，生产 `enforce` 前必须经过 `shadow`。
- shadow compare 差异处理策略冻结：`shadow` 模式下 Python legacy path 始终是用户可见结果来源；Rust 结果只用于旁路对比，差异写入脱敏 structured audit / metrics，不得记录完整 prompt、完整 rows、secret、真实文件路径或敏感 payload，不得影响用户结果。
- 进入 `enforce` 前必须满足全局最低 promotion threshold：至少连续 7 天且不少于 1000 次有效 shadow 样本，并同时满足 contract mismatch rate = 0、panic/crash = 0、P95 latency ≤ Python legacy 110%、error rate 不高于 legacy、audit/redaction/secret leak 测试 100% 通过、rollback 演练通过、对应 regression/cargo/clippy/fmt 全部通过；各专题可以更严格，不得更宽松。
- `enforce` 失败处理策略冻结：Rust kernel / sidecar 失败默认 fail closed；只有对应 PRD 显式声明可 fallback，且 fallback 不会放宽安全、权限、数据一致性、路径、secret、外部输入校验或审计约束时，才允许回退 Python legacy path；fallback 事件必须写 structured audit。Dispatcher / Store / Event sidecar 写路径例外收紧：进入 `enforce` 后，状态写入类操作失败不得自动 fallback 到 Python legacy store。

## 11. Rust 开发环境与依赖要求

### 11.1 本机工具链

推荐通过 rustup 安装 Rust，但仓库 toolchain 策略已冻结为固定具体 stable 版本，而不是裸 `stable` channel：

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
# 当前 native/rust-toolchain.toml 固定 Rust 1.95.0；后续升级必须单独 PR。
rustup toolchain install 1.95.0
rustup component add --toolchain 1.95.0 rustfmt clippy
cargo --version
rustc --version
```

仓库当前已具备或仍需按专题补齐：

```text
native/rust-toolchain.toml（已存在，固定 Rust 1.95.0；不得使用裸 stable channel）
native/Cargo.toml workspace（已存在，默认 Rust edition 2024；MSRV 等于 rust-toolchain.toml 固定版本）
.cargo/config.toml（如后续专题需要）
```

Toolchain 升级必须单独 PR，更新 `CHANGELOG.md` 与相关 Rust PRD，并跑完整 Rust / Python 回归。本文以当前已落地的 `native/rust-toolchain.toml` 为准：Rust `1.95.0`、edition 2024、MSRV 1.95。

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

- `proptest`：状态机、path sanitizer、query / policy guard property tests
- `insta`：snapshot / golden tests
- `rstest`：参数化测试
- `criterion`：benchmark
- `cargo-fuzz`：Skill manifest / allowlist / sandbox policy、MCP JSON-RPC / schema / output sanitizer、artifact path / archive / filename sanitizer、audit redaction / secret masking、DB readonly policy / row shaping fuzz
- `cargo-audit`：依赖漏洞扫描
- `cargo-deny`：license / duplicate / advisory policy
- `cargo-llvm-cov`：coverage
- `cargo-nextest`：Rust test runner

新增这些工具前应同步更新 `README.md`、本文件、`AGENTS.md` 与 CI/本地验证说明。

### 11.5 Rust toolchain / edition / MSRV 策略冻结

Rust toolchain 策略冻结为：`native/rust-toolchain.toml` 固定具体 stable 版本，不使用裸 `stable` channel；Cargo workspace 默认使用 Rust edition 2024；MSRV 等于 `rust-toolchain.toml` 固定版本。当前已落地版本为 Rust `1.95.0` / MSRV 1.95。toolchain 升级必须单独 PR，更新 `CHANGELOG.md` 与相关 Rust PRD，并跑完整 Rust / Python 回归。

### 11.6 Rust dependency 技术栈冻结

Rust dependency 技术栈冻结为 `tokio`、`tonic`、`prost`、`serde`、`serde_json`、`schemars`、`thiserror`、`tracing`、`tracing-subscriber`、`pyo3`、`maturin`、`sqlx`、`uuid`、`time`、`sha2`、`hmac`、`pbkdf2`、`regex`、`jsonschema`、`proptest`、`insta`、`rstest`、`criterion`、`cargo-audit`、`cargo-deny`、`cargo-llvm-cov`、`cargo-nextest`。新增替代依赖必须更新 PRD 并说明替代原因。`axum` 仅可选用于本地 health/debug/admin endpoint，不得替代 gRPC / tonic 生产协议；`cargo-fuzz` 对不可信输入边界强制启用。

## 12. 数据、迁移与回滚

1. 首个 Rust kernel 必须以 Python facade 包装，保留旧 Python 实现作为 `off` / `shadow` legacy path，直到 golden tests 完成；Core / Lifecycle 进入 `enforce` 后以 Rust schema、error code 与 transition table 判定为准。Python facade 采用手写薄层，不要求首版完整 codegen，但必须由 Rust 生成 / 导出的 contract artifact 校验。
2. Storage / event log Rust 化必须以 sidecar contract 先定义迁移兼容层，不得直接改变 SQLite schema 或未来 PostgreSQL schema 语义；PostgreSQL production adapter 当前延期为后续升级项。
3. 每个 Rust kernel / sidecar capability 必须有 feature flag 或 runtime config 支持回退到 Python implementation 或旧 runtime path，直到稳定期结束。
4. Skill-owned Rust runtime 必须能随 Skill bundle 独立启停；PyO3 wheel、native binary 与 sidecar adapter 必须共享同一 Rust core；移除该 Skill bundle 后主体框架不能残留该 Skill 的 Rust dependency、进程、端口或 capability 注册。
5. FFI wheel 构建失败时，默认开发环境应给出明确错误；生产部署必须提前构建 wheel，不在 runtime 启动时编译 Rust。
6. 所有 Rust artifact 必须预构建并通过 checksum / SBOM / provenance / allowlist 校验；请求路径不得编译、下载或替换 wheel / binary / image。
7. Rust-owned 状态变更必须有 migration lock、preflight、dry-run、backup、restore、replay 校验与 rollback / roll-forward runbook；破坏性迁移无备份不得执行。
8. Rust canonical 稳定后必须执行 Python legacy path decommission；最终生产回滚依赖 artifact / deployment rollback 与状态 restore，不依赖隐式 Python 语义 fallback。

## 13. 验收标准

| 编号 | 验收项 | 证明方式 |
|---|---|---|
| AC-001 | 具体业务 Skill 仍只通过 `skill.*` 暴露，不出现主体 native business capability | API capability list / ownership guard / grep 检查 |
| AC-002 | Rust lifecycle kernel 与 Python 旧行为完全一致 | Python golden tests + Rust unit/property tests |
| AC-003 | Rust storage/event kernel 不改变 API/SSE 行为 | `tests/api`、`tests/e2e`、event replay tests |
| AC-004 | 通用 Skill Runtime Rust kernel 保持 service binding 双重授权 | `tests/integrations/agent_skills`、`tests/capabilities/skill_tool` |
| AC-005 | MCP Runtime Rust kernel 对输入输出 fail-closed | MCP unit tests、schema tests、redaction tests、fuzz tests |
| AC-006 | Artifact/upload Rust kernel 防路径穿透和 symlink 泄漏 | path sanitizer property tests、artifact API tests |
| AC-007 | Skill-owned Rust adapter 不改变该 Skill 的 output / artifact / event contract | `skill/<skill-name>/tests` 与 adapter contract tests 全量通过 |
| AC-008 | Rust build/test 纳入本地验证说明 | README / AGENTS / PRD 更新，fmt / clippy / test / nextest / audit / deny / llvm-cov 可运行 |
| AC-009 | FFI panic 不穿透 Python runtime | panic boundary tests |
| AC-010 | Rust 化后现有前后端最小验证仍通过 | 后端分层 unittest、Skill bundle tests、frontend Vitest/build |
| AC-011 | Rust 结构化输出、audit、metrics、shadow diff、retry/correction event 均通过 schema 校验，出错后按 retry / fail-closed 策略处理 | schema validation tests + retry/failure injection + redaction snapshot |
| AC-012 | Python client / PyO3 facade 与 Rust sidecar / kernel 的 compatibility handshake、rolling upgrade 与不兼容 fail-closed 可验证 | compatibility matrix + readiness tests + audit evidence |
| AC-013 | Rust sidecar 仅内部可访问，endpoint 来自部署配置 / runtime allowlist；公网绑定和未授权 client 在 `enforce` 下 fail closed | endpoint allowlist tests + security/failure injection |
| AC-014 | Rust sidecar / kernel 并发、队列、deadline、payload、retry、cancel、shutdown drain 与 overload typed error 均有模块默认值和故障注入测试 | resource/backpressure/deadline/cancel tests + metrics evidence |
| AC-015 | Rust sidecar / kernel config、secret、identity 只来自允许来源；secret 不泄露；rotation、reload、identity mismatch 在 `enforce` 下 fail closed | config source tests + redaction snapshot + mTLS/identity failure injection |
| AC-016 | Rust artifact checksum、SBOM、Cargo.lock digest、contract / proto hash、provenance 与 runtime allowlist 校验可验证；请求路径不编译 / 下载 / 替换产物 | release artifact review + load/connect failure injection + code review |
| AC-017 | 每个 Rust 模块有 Python baseline、Rust baseline、PyO3 / sidecar overhead 与 P50/P95/P99、CPU、memory、throughput 性能门禁 | benchmark report + CI / release regression gate |
| AC-018 | Rust-owned 状态迁移具备 schema version、migration lock、preflight、dry-run、backup、restore、replay 校验与 DR runbook | migration tests + restore drill + replay evidence |
| AC-019 | Rust canonical 稳定后重复 Python legacy 语义下线，只保留 facade / sidecar client / DTO adapter | decommission PR + grep / architecture guard + regression tests |
| AC-020 | `enforce` 前具备 dashboard、alert、SLO、runbook 与 rollback / restore / overload / identity failure 演练证据 | ops checklist + drill records + alert smoke |

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
- `skill/<skill-name>/tests`（存在 Skill-owned Rust runtime 时）
- Rust `cargo fmt --check`
- Rust `cargo clippy --workspace --all-targets --all-features -- -D warnings`
- Rust `cargo test --workspace --all-features`
- Rust `cargo nextest run --workspace --all-features`
- Rust `cargo audit`
- Rust `cargo deny check`
- Rust `cargo llvm-cov --workspace --all-features --summary-only`
- Rust benchmark / fuzz 按模块引入；不可信输入边界必须有 bounded fuzz smoke 与 nightly / release 长跑
- Rust artifact checksum / SBOM / provenance / allowlist load failure tests
- Rust-owned state migration / backup / restore / replay / rollback drills
- Rust ops dashboard / alert / runbook / overload / identity failure drill smoke
- Python legacy decommission guard tests（Rust canonical 稳定后）

### 14.3 性能验证

性能验证不得只看 micro-benchmark。至少包含：

1. 单 kernel benchmark；
2. Python facade FFI 往返 benchmark；
3. API task submit + event replay smoke；
4. Skill/MCP output 大 payload 处理；
5. 含 Rust runtime 的典型 Skill 执行链路。

## 15. Rollout / rollback

1. 每个 Rust kernel / sidecar capability 独立 feature flag 发布。
2. 先 shadow compare；Python legacy path 始终作为用户可见结果来源，Rust 差异只进入脱敏 audit / metrics，不改变生产路径；满足全局最低 promotion threshold 后才允许进入 `enforce`。
3. 再单模块开启，观察 sidecar health、错误码、duration、event replay、内存与任务成功率。
4. `enforce` 出现异常默认 fail closed；只有对应 PRD 显式声明且不放宽安全 / 权限 / 数据一致性约束时，才允许切回 Python implementation，并必须记录 structured audit。Dispatcher / Store / Event sidecar 的状态写入类操作失败不得自动 fallback 到 Python legacy store。
5. Rust kernel 稳定后，旧 Python 实现才能删除。
6. 删除前必须更新 PRD、README、CHANGELOG 与测试基线。
7. 任何 Rust artifact 进入生产前必须通过 checksum、SBOM、provenance 与 runtime allowlist 校验；校验失败不得启动或接流量。
8. 任何 Rust-owned 状态迁移进入生产前必须完成 backup、restore、replay 与 rollback / roll-forward 演练；migration 失败必须 fail closed。
9. `enforce` 前必须具备 dashboard、alert、SLO、runbook 与 rollback / restore / overload / identity failure 演练证据。

## 16. 风险、假设与开放问题

### 16.1 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| FFI 复杂度导致调试困难 | 开发效率下降 | 小 kernel、typed facade、golden tests |
| Rust sidecar 增加部署复杂度 | 运维成本上升 | 生产由外部进程管理器 / 容器编排管理；必须补本地 launcher、health check、CI binary 构建、灰度开关与回退路径 |
| Python/Rust schema 双写漂移 | contract 不一致 | Rust schema 作为 canonical source，生成 contract artifact 校验 Python 手写薄 facade，并用 golden fixtures 阻断漂移 |
| LLM/prompt 逻辑过早 Rust 化 | 产品迭代变慢 | 明确 LLM/prompt glue 不整体迁移 |
| 业务 Skill Rust 化回流主体框架 | 破坏 Skill-only 架构 | Skill-owned crate，主体框架只依赖 generic Skill service contract |
| Rust artifact 供应链不可追溯 | 产物被替换、版本漂移或无法回滚 | checksum、SBOM、Cargo.lock digest、provenance、allowlist 与 release gate |
| 性能收益不可量化 | Rust 化后反而增加延迟或资源抖动 | Python baseline、Rust benchmark、SLO 与性能回归门禁 |
| Rust-owned 状态迁移失败 | 数据不可用或 event replay 不一致 | migration lock、backup、restore、replay 校验、DR runbook 与演练 |
| Python legacy path 长期残留 | 双语义漂移、fallback 放宽安全或一致性 | Rust canonical 稳定后执行 decommission，Python 只保留 facade / client / adapter |
| 缺少运维 runbook | sidecar 故障无法定位和恢复 | `enforce` 前必须有 dashboard、alert、runbook 与 rollback / restore 演练 |

### 16.2 假设

1. 后端仍以 Python/FastAPI 作为对外 API 层。
2. 具体业务 Skill 作为可移除 Skill bundle 保持独立归属。
3. Rust 工具链可被本地 Conda `multi_agent` 环境和未来 CI 支持。
4. 当前 Rust sidecar 主线不会直接改变数据库 schema；PostgreSQL 正式化不在当前 Rust sidecar 主线中落地。

### 16.3 已冻结决策

1. Rust workspace 目录命名：主体框架 Rust native workspace 使用 `native/`；Skill 自有 Rust runtime 使用 `skill/<skill-name>/native/`。
2. 当前只冻结目录边界，不立即创建 `native/` 目录；实际新增前仍需对应实现 PRD、测试计划与评审。
3. dispatcher / store / event log 的目标集成方式冻结为 Rust sidecar service；PyO3 仅用于纯规则、小校验器或 Python compatibility facade，不作为长期 dispatcher/store 生产边界。
4. 本项目按长期交付级 Agent 系统建设；Rust 化目标架构必须考虑生产级完整技术栈，不因短期省工退化为临时进程内实现。
5. Skill-owned Rust runtime 采用 shared Rust core + 多适配器并存策略；PyO3 wheel、native binary 与 sidecar service 可按场景选择，但必须共享同一 core crate、同一 contract tests，不得复制业务逻辑。
6. Skill Rust runtime 的适配方向是 Skill 适配框架 contract；框架不为具体 Skill 增加专属 Rust runtime 分支、API route、前端协议或 capability kind。
7. CI / 发布产物矩阵已冻结：fmt / clippy / test / nextest / audit / deny 必跑，PyO3 wheel 走 `maturin`，sidecar binary 走 Cargo，macOS arm64 作为本地开发基线，Linux x86_64 作为生产部署基线，Windows 暂不作为必需发布目标。
8. Coverage / fuzz 门禁已冻结：`cargo-llvm-cov` 对所有 Rust crate 必跑；普通 crate line coverage ≥80%，安全敏感 crate ≥90%；`cargo-fuzz` 对 Skill manifest / sandbox policy、MCP JSON-RPC / sanitizer、artifact path / archive / filename、audit redaction / secret masking、DB readonly policy / row shaping 等不可信输入边界强制启用；fuzz 可用独立 pinned nightly toolchain，但不得改变生产 stable toolchain。
9. Dispatcher / Store / Event sidecar `enforce` 故障策略已冻结：状态写入类操作失败必须 fail closed，不允许自动 fallback 到 Python legacy store；只允许 health/status、metrics、无副作用 read-only snapshot 等受限只读降级，sidecar unavailable 必须返回稳定 typed error。
10. Core / Lifecycle canonical source 策略已冻结：`maf_core_types` 与 `maf_lifecycle` 是唯一 canonical source；Python `src/core` / `src/lifecycle` 只保留 facade / adapter，不得独立定义冲突 enum、默认值、状态转移规则或 error code 语义；`enforce` 后 Rust 判定为准，稳定后删除重复 Python transition table。
11. Python facade 生成策略已冻结：采用“生成 contract artifact + 手写薄 facade”的混合策略；Rust 生成 / 导出 JSON schema、error code table、enum/value snapshot、transition table snapshot 与 golden fixtures，Python facade 负责 import path、dataclass / Pydantic / API DTO 适配、typed error 映射与最小格式转换，CI 校验一致。
12. Rust typed error / retry-correction policy 已冻结：error code 继续使用 lowercase snake_case string；Rust error 必须包含 `code`、`message`、`retriable`、`category`、`safe_metadata`；自动重试只允许在 `retriable=true`、幂等、retry policy 与 audit 均满足时执行，自动修正只允许系统生成结构化内容，安全 / 权限 / 一致性 error 必须 fail closed。
13. Rust observability / audit / metrics / structured output validation 策略已冻结：所有 Rust response、audit event、metrics event、shadow diff、retry/correction event 必须先通过 schema / proto / contract artifact 校验；校验失败进入 typed error 与 retry / fail-closed 策略，只有 transient 且幂等场景允许自动重试；所有 tracing / metrics 必须脱敏并透传 Python trace context。
14. Rust sidecar / Python client 协议兼容与滚动升级策略已冻结：启动、connect、首次调用、reconnect 和 sidecar version 变化时必须校验 component、protocol / contract version、schema hash、error code table hash、build version、supported features 与 client version range；兼容 minor 变更允许滚动升级，breaking change 必须进入 v2 / contract major version 或 dual-stack；`enforce` 下不兼容 fail closed，`shadow` 下可回退 Python legacy path 并写 audit。
15. Rust sidecar network exposure / service discovery / security boundary 已冻结：sidecar 不对公网、前端、用户、普通 Skill 或外部系统直连暴露；只允许 Python runtime / 受控内部组件经 Unix domain socket、loopback、同 Pod / 内部网络、私有服务发现或 mTLS 内网访问；endpoint 必须来自部署配置 / runtime allowlist，`enforce` 下不安全暴露或未授权访问 fail closed。
16. Rust resource limit / backpressure / deadline / cancellation 策略已冻结：所有请求必须有 deadline；禁止无界队列 / stream / stdout-stderr / payload；模块必须声明 max in-flight、queue、queue wait、payload size、retry、cancel、shutdown drain 与 overload typed error；v1 默认生产基线可按模块收紧，突破 hard cap 必须单独 PRD。
17. Rust sidecar config / secrets / identity 管理策略已冻结：配置只允许来自部署配置、环境变量、secret manager、只读配置文件或 runtime allowlist；secret / token / mTLS key / 连接串不得进入 tracked 文件、audit、metrics、logs、error 或 safe metadata；跨主机访问必须 mTLS 或等价身份校验；secret rotation 通过受控 reload 或滚动重启；`enforce` 下缺失、过期、不匹配或泄露风险 fail closed。
18. Rust build artifact provenance / SBOM / supply-chain 策略已冻结：所有 Rust wheel / binary / image / Skill artifact 由 CI / 部署流水线预构建，携带 checksum、SBOM、Cargo.lock digest、contract / proto hash 与 provenance；runtime 只加载 allowlist 校验通过的 artifact，请求路径不得编译、下载或替换产物。
19. Rust benchmark / performance regression / SLO 策略已冻结：每个 Rust 模块必须有 Python baseline、Rust baseline、PyO3 / sidecar overhead 与 P50/P95/P99、CPU、memory、throughput 指标；默认 P95 不高于 Python legacy 110%，性能回归阻断发布。
20. Rust state migration / backup / restore / DR 策略已冻结：任何 Rust-owned 状态或 schema 变更必须有 schema version、migration lock、preflight、dry-run、backup、restore、replay 校验与 rollback / roll-forward runbook；破坏性迁移无备份一律禁止。
21. Python legacy path decommission 策略已冻结：最终交付版不得长期保留双写或双语义实现；Rust canonical 稳定后 Python 只保留 facade / client / DTO adapter，重复状态机、写路径、安全策略、sanitizer 语义必须下线。
22. Rust ops runbook / incident / rollback drill 策略已冻结：任一 Rust sidecar / PyO3 kernel 进入 `enforce` 前必须具备 dashboard、alert、SLO、诊断、drain / restart / rollback / restore runbook、on-call 分级与演练记录。

### 16.4 延期 / 后续升级项

1. PostgreSQL 正式化当前暂不纳入 Rust sidecar 主线落地范围；Dispatcher / Store / Event sidecar 本专题最终交付边界是 SQLite adapter 与 PostgreSQL-compatible contract，不实现 PostgreSQL production adapter；后续由独立升级 PRD 决策 production adapter、schema ownership、migration policy 与测试计划。

### 16.5 后续实现待决项（非当前 PRD 开放问题）

当前本文档的架构方向、归属边界与接入原则已收口；实施顺序、crate 命名、feature flag 命名、sidecar proto 归属、protocol compatibility / rolling upgrade、sidecar network exposure / service discovery、resource limit / backpressure / deadline / cancellation、config / secrets / identity、artifact provenance / SBOM / supply-chain、performance regression / SLO、state migration / backup / restore / DR、Python legacy decommission、ops runbook / incident / rollback drill、promotion threshold、failure handling、typed error / retry-correction policy、observability / structured output validation、toolchain 策略、dependency 栈、Core/Lifecycle canonical source、Python facade 生成策略与 Orchestration 归属已冻结。以下事项属于后续实现专题的工程细节输入，不是架构开放问题，不阻塞本文档作为决策基线：

1. 各 crate 的具体 owner、内部 module 拆分、contract artifact 导出命令、Python facade 文件布局与 packaging layout。
2. CI 构建矩阵的精确 job 命名、缓存策略、产物上传位置、SBOM / provenance 归档位置与 sidecar image 发布流水线细节；必跑门禁、coverage / fuzz 门禁、平台基线、产物类型和供应链校验规则已冻结。
3. dispatcher / store / event / Skill sandbox / MCP sidecar 的具体 RPC method、错误码表与兼容窗口；proto 归属和主版本策略已冻结为 `native/proto/maf/<domain>/v1/`。
4. 创建 `native/` 时采用的具体 Rust stable 版本；策略已冻结为 `rust-toolchain.toml` 固定具体版本且 MSRV 等于该版本。
5. `cargo-fuzz` job 的具体语料库、运行时长、崩溃 artifact 保留策略与 nightly / release 调度细节；强制边界与 toolchain 隔离策略已冻结。
6. 各模块 SLO 的最终数值、benchmark workload、dashboard panel、alert threshold 与 runbook 命令细节；必须遵守本文冻结的性能门禁、运维门禁与演练要求。
7. 各状态模块的具体 migration 文件名、备份介质、restore 命令、RPO / RTO 数值与 replay 校验脚本；必须遵守本文冻结的 migration / backup / restore / DR 原则。

## 17. 当前证据锚点

- `src/api/runtime.py` 当前只装配 `MainAgentExecutor`、`SkillExecutor`、`MCPToolExecutor`，并通过通用 service registry 向 Skill handler 注入受控平台服务。
- `src/lifecycle/task_state_machine.py` 当前承载可 Rust 化的状态转移规则。
- `src/storage/sqlite/repositories.py` 当前通过 `asyncio.to_thread` 包装同步 SQLAlchemy session，是后续 durable store / async Rust store 的候选边界。
- `src/integrations/agent_skills/execution.py` 当前承载 Skill trust gate、public root、handler allowlist 与 service binding。
- `src/integrations/mcp/` 与 `src/capabilities/mcp_tool/` 当前承载 MCP Python facade / generic MCP executor；MCP Rust sidecar 的 Phase 0 / Phase 1 骨架已进入 `native/crates/maf_mcp_runtime/`，完整 canonical MCP runtime 仍以 `docs/prd/MCP/` Phase 2-5 为后续实施范围。
- `docs/prd/MCP/README.md` 与 `docs/prd/MCP/00-MCPRuntime联合改造总览PRD.md` 当前承载 MCP 长任务流式 SSE 与 Rust sidecar 的联合实施门禁。


## 18. 拆分后的实施专题 PRD

本文件保留为 Rust 化总体决策基线；后续实施、评审与验收应进入 `docs/prd/rust/` 下的拆分 PRD：

| 专题 | 文档 | 关系 |
|---|---|---|
| 总览与拆分索引 | `docs/prd/rust/00-Rust化总览与拆分索引PRD.md` | 继承本文档的架构方向与实施波次 |
| 工具链 / 构建 / 发布 / 质量门禁 | `docs/prd/rust/01-Rust工具链构建发布与质量门禁PRD.md` | 所有 Rust 实现前置 |
| Core 与 Lifecycle kernel | `docs/prd/rust/02-Core与LifecycleKernelPRD.md` | 对应 RUST-P0-001 / RUST-P0-002 |
| Dispatcher / Store / Event sidecar | `docs/prd/rust/03-DispatcherStoreEventSidecarPRD.md` | 对应 RUST-P0-003 / RUST-P0-004 |
| Skill Runtime 与 Skill-owned Rust 接入 | `docs/prd/rust/04-SkillRuntime与SkillOwnedRust接入PRD.md` | 对应 RUST-P0-005 / RUST-P0-006 / RUST-P1-004 |
| MCP Runtime Rust sidecar | `docs/prd/rust/05-MCPRuntimeRustSidecarPRD.md` + `docs/prd/MCP/` | 对应 RUST-P0-007；Phase 0 / Phase 1 基线已落地，完整生产能力以 MCP Phase 2-5 与 Phase 5 enforce 门禁为准 |
| Artifact / Upload / Auth / DataAccess / Audit kernel | `docs/prd/rust/06-ArtifactUploadAuthDataAccessKernelPRD.md` | 聚合 PRD，对应 RUST-P0-008 / RUST-P1-001 / RUST-P1-003 / RUST-P1-005 |
| Orchestration deterministic kernel 与热点优化 | `docs/prd/rust/07-OrchestrationDeterministicKernel与热点优化PRD.md` | 条件候选；不属于必做 Rust 化目标集，对应 RUST-P1-002 与 P2 热点 |

约束：`docs/prd/rust/` 只承接实施专题拆分，不替代本文档的总体边界；如果两者发生冲突，以本文档的冻结决策和后续明确更新过的专题 PRD 为准，并必须同步更新 `CHANGELOG.md`。
