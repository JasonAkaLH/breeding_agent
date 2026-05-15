# Rust 工具链、构建发布与质量门禁 PRD

- **状态**：部分落地（`native/` workspace、Rust 1.95.0 toolchain、`maf_mcp_runtime` Phase 1 骨架已落地；CI / 发布产物矩阵、coverage / fuzz、SBOM / provenance、ops 门禁仍待实现）
- **日期**：2026-05-14
- **来源基线**：`docs/prd/backend/16-Rust化Runtime模块评估PRD.md` 第 8、10、11、12、14、15 节
- **影响范围**：仓库根目录、现有 `native/` workspace、CI、本地开发环境、Python/Rust bridge

## 1. 问题陈述

当前仓库已因 MCP Runtime Phase 1 引入 `native/` Rust workspace、`rust-toolchain.toml`、`maf_mcp_runtime` crate / sidecar binary 骨架与基础 Cargo 构建能力；但完整 CI、发布产物、coverage、fuzz、audit / deny、SBOM、provenance、跨平台 build、runbook 与 production gate 尚未全部落地。后续 Rust 专题如果继续按单功能 PR 自行扩展工具链，会造成构建路径、依赖来源、质量门禁和回滚策略漂移。

## 2. 目标

1. 定义 Rust 工具链、workspace、依赖、构建产物、CI 与本地验证的统一标准。
2. 支持三类 Rust 集成：PyO3 extension、native binary、sidecar service。
3. 禁止在业务请求路径中编译 Rust、下载依赖或生成 native artifact。
4. 为所有后续 Rust 专题提供一致质量门禁。

## 3. 非目标

1. 不在本 PRD 中选择新的业务功能模块；既有 MCP Phase 1 skeleton 的范围由 `docs/prd/MCP/` 与 `05-MCPRuntimeRustSidecarPRD.md` 承接。
2. 不创建新的具体 crate 代码；本文只记录工具链与质量门禁要求。
3. 不强制所有 Rust 模块都用 PyO3；dispatcher/store/event 长期目标是 sidecar。
4. 不替代 Conda `multi_agent` Python 环境约定。

## 4. 功能需求

- RUST-TOOL-FR-001：仓库必须通过 `rust-toolchain.toml` 固定具体 stable 版本，不得使用裸 `stable` channel；Cargo workspace 默认使用 Rust edition 2024，MSRV 等于 `rust-toolchain.toml` 固定版本。
- RUST-TOOL-FR-002：主体框架 Rust workspace 必须位于仓库根目录 `native/`；新增或扩展 crate 前必须经过对应实现 PRD 和评审，既有 `maf_mcp_runtime` Phase 1 骨架不得被外推为其他 crate 已获准创建。
- RUST-TOOL-FR-003：Skill-owned Rust source 必须位于 `skill/<skill-name>/native/`，不得混入主体 workspace 的业务 crate。
- RUST-TOOL-FR-004：PyO3 wheel 必须由 CI 或部署流程预构建；runtime 启动和请求处理不得触发 `cargo build`。
- RUST-TOOL-FR-005：native binary 必须来自固定构建产物路径和 allowlist，不得由 Skill 或用户输入指定任意路径。
- RUST-TOOL-FR-006：sidecar binary / image 必须具备 version、health/readiness、shutdown drain 与 structured logging。
- RUST-TOOL-FR-007：所有 Rust crate 必须使用统一 error mapping 约定，panic 不得跨 FFI 或 protocol 边界外泄。
- RUST-TOOL-FR-008：引入 Rust build dependency 时，必须同步更新 `README.md`、`AGENTS.md`、`requirements.txt` 或独立 Rust build 文档。
- RUST-TOOL-FR-009：正式生产 sidecar protocol 必须使用 gRPC / tonic + protobuf；HTTP JSON 只允许本地开发或极早期 spike，不得作为生产协议。
- RUST-TOOL-FR-010：主体 Rust workspace 目录与首批 crate 命名必须使用本文档冻结名称；未进入实现的 crate 不得提交空目录、空 crate 或占位测试。
- RUST-TOOL-FR-011：所有 Rust kernel / sidecar / 子模块必须使用 `MAF_RUST_<COMPONENT>_MODE=off|shadow|enforce` 命名规则，默认值为 `off`，生产 `enforce` 前必须经过 `shadow`。
- RUST-TOOL-FR-012：sidecar 进程管理最终方案为生产环境外部进程管理器 / 容器编排管理；Python runtime 不负责生产 sidecar 生命周期。
- RUST-TOOL-FR-013：`shadow` 模式下 Python legacy path 必须始终作为用户可见结果来源；Rust 结果只用于旁路对比，差异只能进入 structured audit / metrics，不得影响用户结果。
- RUST-TOOL-FR-014：任一 Rust kernel / sidecar / 子模块从 `shadow` 进入 `enforce` 前，必须满足本文档冻结的全局最低 promotion threshold；各专题可以更严格，不得更宽松。
- RUST-TOOL-FR-015：`enforce` 模式下 Rust kernel / sidecar 失败默认 fail closed；仅当对应 PRD 显式声明且 fallback 不放宽安全 / 权限 / 数据一致性约束时，才允许回退 Python legacy path。
- RUST-TOOL-FR-016：所有生产 sidecar 的 protobuf schema 必须统一归属 `native/proto/maf/<domain>/v1/`；breaking change 必须新建 `v2` package，Rust server 与 Python client 必须从同一 proto source 生成或校验。
- RUST-TOOL-FR-017：Rust 核心依赖技术栈冻结为本文档列出的长期技术栈；新增替代依赖必须更新 PRD 并说明替代原因。
- RUST-TOOL-FR-018：Rust toolchain 升级必须单独 PR，更新 `CHANGELOG.md`，并跑完整 Rust / Python 回归。
- RUST-TOOL-FR-019：任一 Rust 代码进入 `native/` 或 `skill/<skill-name>/native/` 时，CI 必须启用冻结的 Rust 必跑门禁：`cargo fmt`、`cargo clippy`、`cargo test`、`cargo nextest`、`cargo audit`、`cargo deny`。
- RUST-TOOL-FR-020：PyO3 wheel 必须通过 `maturin` 构建；sidecar binary 必须通过 Cargo 构建；生产 sidecar 交付形态以 Linux container image / binary 为主。
- RUST-TOOL-FR-021：Rust CI / 发布平台基线冻结为 macOS arm64 支持本地开发与调试、Linux x86_64 支持生产部署；Windows 暂不作为必需发布目标。
- RUST-TOOL-FR-022：Python 版本基线跟随当前 Conda `multi_agent` 环境，即 Python 3.13 系列；PyO3 wheel 与 Python client smoke 必须覆盖该版本。
- RUST-TOOL-FR-023：`cargo-llvm-cov` 必须对所有 Rust crate 生成覆盖率报告并执行阈值门禁；普通 Rust crate 最低 line coverage 为 80%，安全敏感 crate 最低 line coverage 为 90%。
- RUST-TOOL-FR-024：`cargo-fuzz` 必须对不可信输入边界强制启用；PR 级别运行 bounded fuzz smoke，nightly / release gate 运行更长 fuzz job。
- RUST-TOOL-FR-025：fuzz 可使用单独 pinned nightly toolchain，但仅限 fuzz job，不得改变生产 Rust stable toolchain、MSRV 或 `rust-toolchain.toml` 策略。
- RUST-TOOL-FR-026：Rust typed error / error code / retry-correction policy 必须使用本文档冻结规范；error code 使用 lowercase snake_case string，不使用 dotted code。
- RUST-TOOL-FR-027：Rust error 必须包含 `code`、`message`、`retriable`、`category`、`safe_metadata`；`message` 与 `safe_metadata` 必须可安全进入 API / audit。
- RUST-TOOL-FR-028：自动重试只允许由 `retriable=true`、幂等条件和 retry policy 同时驱动；自动修正只允许处理系统生成的结构化内容，不得修正用户真实意图或放宽安全规则。
- RUST-TOOL-FR-029：所有 Rust kernel / sidecar 的结构化响应、结构化 audit event、metrics event、shadow compare event、retry event 与 correction event 必须按冻结 schema 校验；校验失败不得静默吞掉或降级为非结构化字符串。
- RUST-TOOL-FR-030：结构化输出校验失败必须映射为 typed error 并进入 retry / fail-closed policy；只有 `retriable=true`、幂等、同等安全 retry policy 与 audit 全部满足时才允许自动重试。
- RUST-TOOL-FR-031：所有 Rust kernel / sidecar 必须通过 `tracing` 输出结构化 span / event，并支持 Python trace context 透传；禁止记录 raw prompt、完整 rows、secret、真实路径、连接串、base_url 或 raw provider / OS error。
- RUST-TOOL-FR-032：Python sidecar client / PyO3 facade 必须在启动、connect 和首次调用前校验 component、protocol / contract version、schema hash、error code table hash、build version 与 supported feature flags。
- RUST-TOOL-FR-033：sidecar health / readiness / version response 必须包含 component、build_version、protocol_version、schema_hash、error_code_table_hash、supported_features、min_client_version、max_client_version。
- RUST-TOOL-FR-034：兼容 minor 变更允许滚动升级；breaking change 必须进入新的 `v2` proto package 或新的 contract major version，除非显式实现 dual-stack client / server。
- RUST-TOOL-FR-035：`enforce` 下协议 / contract 不兼容必须 fail closed；`shadow` 下可回退 Python legacy path 并写 `rust.protocol_incompatible` audit event。
- RUST-TOOL-FR-036：Rust sidecar 不得对公网、前端、用户、普通 Skill 或外部系统直接暴露；只允许 Python runtime / 受控内部组件通过受控内部通道访问。
- RUST-TOOL-FR-037：sidecar 允许的内部通道仅限 Unix domain socket、127.0.0.1 / loopback、同 Pod / 同主机内部网络、私有服务发现或启用 mTLS 的内网跨主机通信。
- RUST-TOOL-FR-038：sidecar address / socket path / service name 必须来自部署配置或 runtime allowlist；不得接受用户输入、Skill manifest、LLM 输出或外部 tool output 指定任意 sidecar 地址。
- RUST-TOOL-FR-039：`enforce` 下检测到公网绑定、未授权 client、未配置 mTLS 的跨主机访问、debug/metrics endpoint 外网可达或 service discovery 不在 allowlist 时必须 fail closed。
- RUST-TOOL-FR-040：所有 Rust sidecar / kernel 请求必须有 deadline；禁止无 deadline 请求、无界队列、无界 stream、无界 stdout/stderr、无界 request / response。
- RUST-TOOL-FR-041：所有 Rust sidecar 必须显式声明 max in-flight、queue size、queue wait、request / response size、deadline、retry、cancel、shutdown drain 与 overload typed error。
- RUST-TOOL-FR-042：resource limit / backpressure / deadline / cancellation 默认值必须采用本文档冻结基线；模块可通过部署配置收紧或在模块 PRD 内声明更严格值，不得突破 hard cap，除非单独 PRD 决策。
- RUST-TOOL-FR-043：过载、排队超时、deadline exceeded、payload too large、stream idle timeout、cancelled 必须映射为稳定 typed error，并写 structured audit / metrics。
- RUST-TOOL-FR-044：Rust sidecar / PyO3 kernel 配置只允许来自部署配置、环境变量、secret manager、只读配置文件或 runtime allowlist；不得来自用户输入、Skill manifest、LLM 输出或外部 tool output。
- RUST-TOOL-FR-045：secret、token、mTLS key、数据库连接串、provider key、session / HMAC key 不得写入 tracked 文件、audit、metrics、structured logs、typed error message 或 `safe_metadata`。
- RUST-TOOL-FR-046：跨主机 sidecar 访问必须使用 mTLS 或等价服务身份校验；loopback / Unix domain socket 场景必须有 endpoint allowlist 与本机 / 文件权限边界。
- RUST-TOOL-FR-047：sidecar 必须支持 secret rotation：可通过受控 reload 或滚动重启生效；rotation 只能记录 secret version / fingerprint，不得记录 secret value。
- RUST-TOOL-FR-048：`enforce` 下配置缺失、secret 缺失、identity mismatch、证书过期、证书不受信、client identity 未授权或 secret reload 失败必须 fail closed。
- RUST-TOOL-FR-049：所有 PyO3 wheel、sidecar binary / image、native binary 与 Skill-owned Rust artifact 必须由 CI / 部署流水线预构建；runtime 启动或业务请求路径不得触发编译、依赖下载或 artifact 替换。
- RUST-TOOL-FR-050：Rust 产物必须携带或关联 checksum、SBOM、Cargo.lock digest、git commit、toolchain version、target triple、Cargo features、build profile、contract / proto hash 与 provenance record。
- RUST-TOOL-FR-051：Python runtime、sidecar client、Skill platform handler 只能加载 runtime allowlist 中且 checksum / provenance 校验通过的 Rust artifact；校验失败在 `enforce` 下必须 fail closed。
- RUST-TOOL-FR-052：Rust 发布门禁必须包含供应链审查：`cargo audit`、`cargo deny`、license / advisory 策略、SBOM 生成与 artifact 签名或等价 provenance 校验。
- RUST-TOOL-FR-053：每个 Rust kernel / sidecar 必须建立性能基线，覆盖 Python baseline、Rust implementation、PyO3 FFI 或 sidecar RPC overhead、P50/P95/P99 latency、throughput、CPU、memory 与 payload size。
- RUST-TOOL-FR-054：性能回归必须阻断 `enforce` 发布；默认 P95 latency 不得高于 Python legacy 110%，memory / CPU 不得无解释劣化，模块 PRD 可设置更严格 SLO。
- RUST-TOOL-FR-055：任何 Rust-owned 持久状态、sidecar schema、event log、artifact metadata 或 bundle/runtime registry 变更必须有 schema version、migration lock、preflight、dry-run、备份、恢复、replay 校验与 rollback / roll-forward runbook。
- RUST-TOOL-FR-056：最终交付版必须下线重复 Python legacy 语义；Rust canonical 稳定后，Python 只保留 facade / client / DTO adapter，不再保留重复状态机、写路径、安全策略或 sanitizer 语义。
- RUST-TOOL-FR-057：任一 Rust sidecar / PyO3 kernel 进入 `enforce` 前必须具备 dashboard、alert、SLO、health/readiness/version 诊断、drain / restart / rollback / restore runbook 与演练记录。

## 5. Workspace 与 crate 命名冻结

主体 Rust workspace 目录冻结为 `native/`；Skill-owned Rust runtime 目录冻结为 `skill/<skill-name>/native/`。首批主体 crate 命名冻结如下，后续实现、CI、Python facade、测试与文档必须使用这些名称。

| Crate | 职责 | 波次 | 状态 |
|---|---|---|---|
| `maf_core_types` | Core enum / struct / JSON schema / serde contract | Wave 1 | 首批冻结 |
| `maf_lifecycle` | Task / node / mailbox / interrupt / cancel transition table | Wave 1 | 首批冻结 |
| `maf_runtime_store` | SQLite repository、future PostgreSQL contract、transaction、lease、idempotency | Wave 2 | 首批冻结 |
| `maf_event_log` | Event append、replay、cursor、SSE snapshot support | Wave 2 | 首批冻结 |
| `maf_task_dispatcher` | Task queue、active registry、cancellation token、bundle revision pinning | Wave 2 | 首批冻结 |
| `maf_skill_runtime` | Skill manifest、bundle fingerprint、trust gate、PyO3 policy kernel、Skill Sandbox sidecar binary target | Wave 3 | 首批冻结 |
| `maf_mcp_runtime` | MCP protocol、transport state、tool binding、schema validation | Wave 3；MCP Phase 0 / 1 已先行 | Phase 1 skeleton 已落地；Phase 2-5 待完成 |
| `maf_artifact_store` | Artifact/upload/file path、hash、quota、retention、archive safety | Wave 4 | 首批冻结 |
| `maf_auth_core` | Password / HMAC / session / captcha primitives | Wave 4 | 首批冻结 |
| `maf_data_access` | Readonly DB adapter、row shape、timeouts | Wave 4 | 首批冻结 |
| `maf_audit_sanitizer` | Audit payload sanitizer、event serializer privacy filter、redaction rule | Wave 4 | 首批冻结 |
| `maf_orchestration_kernel` | DAG validation、scheduler、completion policy、backpressure | 条件候选 | 不进入必做 Rust 化目标集创建 |

约束：`maf_orchestration_kernel` 仅作为条件候选 crate 名保留，不进入必做 Rust 化目标集创建；未进入实际实现的 crate 不得提交空目录、空 crate 或占位测试。

## 6. 依赖边界概览

| 类别 | 工具 / crate | 用途 |
|---|---|---|
| Python bridge | `maturin`、`pyo3` | PyO3 wheel 构建与 Python extension |
| 基础序列化 | `serde`、`serde_json`、`schemars` | contract、schema、round-trip |
| 错误与日志 | `thiserror`、`tracing`、`tracing-subscriber` | stable error、structured logs |
| async / service | `tokio`、`tonic`、`prost`；`axum` 仅可选用于本地 health/debug/admin HTTP endpoint | sidecar runtime、gRPC / tonic + protobuf 正式协议 |
| storage | `sqlx` | SQLite / PostgreSQL adapter；生产化策略由 store PRD 决策 |
| security / util | `sha2`、`hmac`、`pbkdf2`、`uuid`、`time`、`regex` | hash、auth、id、时间、校验 |
| validation | `jsonschema` | MCP / Skill payload schema validation |
| test / quality | `proptest`、`insta`、`rstest`、`criterion`、`cargo-fuzz`、`cargo-audit`、`cargo-deny`、`cargo-llvm-cov`、`cargo-nextest` | property、snapshot、benchmark、fuzz、audit、coverage |

## 7. Rust dependency 技术栈冻结

以下核心依赖冻结为 Rust 化长期技术栈；新增替代依赖必须更新对应 PRD，并说明替代原因、维护风险、license / security 影响和迁移计划。

| 类别 | 冻结依赖 | 用途 |
|---|---|---|
| Async runtime | `tokio` | sidecar async runtime、I/O、task 管理 |
| gRPC / protobuf | `tonic`、`prost` | Python ↔ sidecar 生产协议、protobuf codegen |
| Serialization / bridge | `serde`、`serde_json` | contract serialization、JSON bridge、golden fixtures |
| Schema | `schemars` | JSON schema / contract schema generation |
| Error | `thiserror` | typed error、stable error mapping |
| Observability | `tracing`、`tracing-subscriber` | structured logs、span、metrics bridge |
| Python bridge | `pyo3`、`maturin` | PyO3 extension、wheel build |
| Storage | `sqlx` | SQLite adapter 与 PostgreSQL-compatible repository contract |
| ID / time | `uuid`、`time` | ID、timestamp、TTL |
| Crypto / hash | `sha2`、`hmac`、`pbkdf2` | hash、HMAC、password/session primitives |
| Validation | `regex`、`jsonschema` | input/schema/policy validation |
| Property / snapshot / param tests | `proptest`、`insta`、`rstest` | property、golden/snapshot、parameterized tests |
| Benchmark | `criterion` | performance baseline 与 regression benchmark |
| Audit / policy / coverage / runner | `cargo-audit`、`cargo-deny`、`cargo-llvm-cov`、`cargo-nextest` | vulnerability、license/advisory、coverage、test runner |

可选依赖 / 工具：

- `axum`：仅可选用于本地 health/debug/admin HTTP endpoint，不得替代 gRPC / tonic 生产协议。
- `cargo-fuzz`：对不可信输入边界强制启用；可使用单独 pinned nightly toolchain，但只允许用于 fuzz job，不得改变生产 Rust stable toolchain。

## 8. Rust toolchain / edition / MSRV 策略冻结

Rust toolchain 最终策略冻结为：

1. `rust-toolchain.toml` 必须固定具体 stable 版本，不得使用裸 `stable` channel。
2. Cargo workspace 默认使用 Rust edition 2024。
3. MSRV 等于 `rust-toolchain.toml` 固定版本。
4. Toolchain 升级必须单独 PR，更新 `CHANGELOG.md`，并跑完整 Rust / Python 回归。
5. 当前已落地 workspace 使用 `native/rust-toolchain.toml` 固定 Rust `1.95.0`、edition 2024、MSRV 1.95；后续 toolchain 升级必须单独 PR，更新 PRD / CHANGELOG，并跑完整 Rust / Python 回归。本文不再为未来 workspace 预留另一个待定版本号。

### 8.1 CI / 发布产物矩阵冻结

任一 Rust 代码进入 `native/` 或 `skill/<skill-name>/native/` 后，必须同步建立 CI 与发布产物标准。冻结矩阵如下：

| 类别 | 冻结要求 | 说明 |
|---|---|---|
| 必跑门禁 | `cargo fmt --check`、`cargo clippy --workspace --all-targets --all-features -- -D warnings`、`cargo test --workspace --all-features`、`cargo nextest run --workspace --all-features`、`cargo audit`、`cargo deny check`、`cargo llvm-cov --workspace --all-features --summary-only` | `cargo nextest` 不替代 `cargo test`；`cargo llvm-cov` 还必须执行 80% / 90% coverage threshold |
| PyO3 wheel | `maturin` build / smoke | 产物必须预构建；runtime 启动和请求路径不得编译 Rust |
| sidecar binary | Cargo build | binary 必须来自固定构建产物路径和 allowlist |
| 生产 sidecar 交付 | Linux container image / binary 为主 | 由外部进程管理器或容器编排管理 |
| 本地开发平台 | macOS arm64 | 用于开发、调试、smoke；不代表生产基线 |
| 生产部署平台 | Linux x86_64 | 生产部署基线与 sidecar image / binary 主目标 |
| 非必需平台 | Windows | 暂不作为必需发布目标；后续如需支持必须另行补 PRD / CI |
| Python 版本 | Python 3.13 系列 | 跟随当前 Conda `multi_agent` 环境；PyO3 wheel 与 Python sidecar client smoke 必须覆盖 |

### 8.2 Coverage / fuzz 门禁冻结

`cargo-llvm-cov` 与 `cargo-fuzz` 的最终门禁冻结如下：

1. `cargo-llvm-cov` 对所有 Rust crate 必跑并生成覆盖率报告。
2. 普通 Rust crate 最低 line coverage 为 **80%**。
3. 安全敏感 crate 最低 line coverage 为 **90%**，包括 `maf_skill_runtime`、`maf_mcp_runtime`、`maf_artifact_store`、`maf_auth_core`、`maf_data_access`、`maf_audit_sanitizer`。
4. `cargo-fuzz` 对不可信输入边界强制启用，至少覆盖：
   - Skill manifest / allowlist / sandbox policy；
   - MCP JSON-RPC / schema / output sanitizer；
   - artifact path / archive / filename sanitizer；
   - audit redaction / secret masking；
   - DB readonly policy / row shaping。
5. fuzz 可使用单独 pinned nightly toolchain，但只允许用于 fuzz job；不得改变生产 Rust stable toolchain、MSRV 或 `rust-toolchain.toml` 固定版本策略。
6. PR 级别必须运行 bounded fuzz smoke；nightly / release gate 必须运行更长 fuzz job，并保留可审查日志或 artifact。

## 9. Runtime config / feature flag 命名冻结

所有 Rust kernel / sidecar / 子模块统一使用以下 runtime config 命名规则：

```text
MAF_RUST_<COMPONENT>_MODE=off|shadow|enforce
```

语义：

- `off`：完全走 Python legacy path。
- `shadow`：Python legacy path 仍为主路径，Rust kernel / sidecar 旁路执行并比较结果，只记录差异和指标，不影响用户结果。
- `enforce`：Rust kernel / sidecar 成为主路径；失败时按该模块 rollback policy 决定 fail closed 或回退。

默认值必须是 `off`；进入生产 `enforce` 前必须先经过 `shadow`。新增 Rust 模块不得自定义另一套 enable/disable 环境变量。

### 9.1 Shadow compare 差异处理冻结

Shadow compare 差异处理策略冻结：`shadow` 模式下，Python legacy path 永远是用户可见结果来源；Rust kernel / sidecar 结果只用于旁路对比。差异必须写入 structured audit / metrics，至少包含 component、input fingerprint、legacy output fingerprint、rust output fingerprint、error code、duration；不得记录完整 prompt、完整 rows、secret、真实文件路径或敏感 payload。shadow 差异不得影响用户结果；只有差异率、错误率、性能指标达到对应专题 PRD 的 promotion threshold 后，才能进入 `enforce`。

### 9.2 `shadow` → `enforce` promotion threshold 冻结

任一 Rust kernel / sidecar / 子模块从 `shadow` 进入 `enforce` 前，必须满足全局最低门槛；各专题 PRD 可以更严格，但不得更宽松。

最低门槛：至少连续 7 天且不少于 1000 次有效 shadow 样本；若 7 天内不足 1000 次有效样本，必须继续 shadow 到样本数达标。达标期内还必须同时满足：

- contract mismatch rate = 0。
- panic / crash = 0。
- P95 latency 不高于 Python legacy path 的 110%。
- error rate 不高于 Python legacy path。
- audit / redaction / secret leak 测试 100% 通过。
- rollback 演练通过。
- 对应专题的 regression tests、cargo tests、clippy、fmt 全部通过。

不满足任一条件时，不得进入 `enforce`。

### 9.3 `enforce` 失败处理策略冻结

`enforce` 模式下，Rust kernel / sidecar 失败默认 **fail closed**；只有对应 PRD 显式声明可 fallback，且 fallback 不会放宽安全、权限、数据一致性、路径、secret、外部输入校验或审计约束时，才允许回退 Python legacy path。

必须 fail-closed 的模块 / 场景：

- Auth / HMAC / session / captcha primitives。
- Artifact / upload / path normalization / archive safety。
- Skill sandbox / native binary / script execution。
- MCP tool input/output schema validation / sanitization。
- Service binding / allowlist / trust gate。
- Audit sanitizer / secret redaction。
- DB readonly enforcement。
- 任何会放宽权限、安全、路径、secret、外部输入校验或数据一致性的失败。

可 fallback 的场景：

- Rust pure performance kernel 失败，且 Python legacy 能提供同等安全约束。
- Sidecar health failure，且 Python legacy path 已证明满足同等 contract。
- Shadow / migration 期间的非安全路径。
- Fallback 事件必须写 structured audit，并包含 component、mode、error code、reason、fallback target。

| Component | Config key | 对应模块 |
|---|---|---|
| `CORE` | `MAF_RUST_CORE_MODE` | `maf_core_types` |
| `LIFECYCLE` | `MAF_RUST_LIFECYCLE_MODE` | `maf_lifecycle` |
| `RUNTIME_STORE` | `MAF_RUST_RUNTIME_STORE_MODE` | `maf_runtime_store` |
| `EVENT_LOG` | `MAF_RUST_EVENT_LOG_MODE` | `maf_event_log` |
| `TASK_DISPATCHER` | `MAF_RUST_TASK_DISPATCHER_MODE` | `maf_task_dispatcher` |
| `SKILL_RUNTIME` | `MAF_RUST_SKILL_RUNTIME_MODE` | `maf_skill_runtime` |
| `MCP_RUNTIME` | `MAF_RUST_MCP_RUNTIME_MODE` | `maf_mcp_runtime` |
| `ARTIFACT_STORE` | `MAF_RUST_ARTIFACT_STORE_MODE` | `maf_artifact_store` |
| `AUTH_CORE` | `MAF_RUST_AUTH_CORE_MODE` | `maf_auth_core` |
| `DATA_ACCESS` | `MAF_RUST_DATA_ACCESS_MODE` | `maf_data_access` |
| `AUDIT_SANITIZER` | `MAF_RUST_AUDIT_SANITIZER_MODE` | `maf_audit_sanitizer` |

## 10. Sidecar 进程管理冻结

Sidecar 进程管理最终方案冻结为：

1. 生产环境由外部进程管理器 / 容器编排管理 sidecar，例如 Docker Compose、systemd、Kubernetes、Supervisor 或部署平台。
2. Python runtime 不负责生产 sidecar 生命周期，不在生产请求路径中 spawn / restart / kill sidecar 进程。
3. Python runtime 只负责 sidecar client connect、health/readiness/version check、shutdown drain 协调、protocol compatibility check 与 fallback / fail-closed。
4. 本地开发 / 测试环境可以提供 Python launcher、脚本或 test fixture 一键拉起 sidecar；该 launcher 只属于 dev/test，不作为生产运行方式。
5. 所有 sidecar 必须输出 structured logs / metrics，并交由外部进程管理器或部署平台采集。

## 11. Protobuf schema 归属与版本策略冻结

所有生产 sidecar 的 protobuf schema 统一归属主体 Rust workspace 下的 `native/proto/maf/<domain>/v1/`。`v1` / `v2` 表示协议 schema 的主版本 namespace，不表示实施阶段或 PRD 版本。

初始 domain 冻结为：

| Domain | 目录 | 用途 |
|---|---|---|
| common | `native/proto/maf/common/v1/` | shared error、health、version、metadata、fingerprint 等通用 message |
| runtime | `native/proto/maf/runtime/v1/` | dispatcher / store / event sidecar protocol |
| skill | `native/proto/maf/skill/v1/` | Skill Sandbox sidecar protocol 与 Skill runtime policy 相关 protocol |
| mcp | `native/proto/maf/mcp/v1/` | MCP Runtime sidecar protocol |

Rust sidecar server 与 Python sidecar client 都必须从同一 proto source 生成或校验，不允许手写两套不一致 DTO。

版本规则：

- 兼容变更继续留在 `v1`，例如新增 optional 字段、新增 message、新增 RPC method、新增旧客户端可安全忽略的 enum 值。
- 破坏兼容变更必须新建 `v2` package，例如删除字段、改字段类型、改字段编号、改变已有字段语义、改变 RPC request / response 的必要结构，或导致旧客户端无法安全处理新响应。
- `v1` 字段编号不得复用；删除字段必须 reserved。
- Python client 与 Rust server 必须有 protocol compatibility tests 和 golden fixtures。

### 11.1 Protocol compatibility / rolling upgrade 冻结

Python sidecar client、PyO3 facade、Rust sidecar server 与 Rust kernel contract artifact 的兼容策略冻结如下。

#### 11.1.1 Compatibility handshake

所有 sidecar 必须在 health / readiness / version response 中返回：

| 字段 | 要求 |
|---|---|
| `component` | 组件名，必须与 Python client 期望一致 |
| `build_version` | sidecar binary / image build version |
| `protocol_version` | proto package major version，例如 `maf.runtime.v1` |
| `schema_hash` | protobuf / JSON schema / contract artifact hash |
| `error_code_table_hash` | Rust error code table hash |
| `supported_features` | sidecar 支持的能力集合 |
| `min_client_version` / `max_client_version` | sidecar 声明的兼容 Python client 区间 |

PyO3 kernel / Python facade 也必须在 import 或初始化阶段校验 contract artifact，至少包含 component、contract_version、schema_hash、error_code_table_hash 与 supported_features。

#### 11.1.2 兼容窗口

兼容 minor 变更允许滚动升级，包括新增 optional 字段、新增旧端可忽略的 event / metrics 字段、新增 RPC method、新增旧端不消费的 feature flag。

不兼容变更必须升级到新的 major version，例如：

- proto package 从 `v1` 升级到 `v2`；
- contract artifact major version 变化；
- 删除字段、复用字段编号、改变字段类型或字段语义；
- 改变已有 error code 语义；
- 改变 fail-closed / retry / security policy；
- 旧客户端无法安全忽略的新必填字段。

不兼容变更不得在 `enforce` 流量中混跑，除非实现了明确的 dual-stack Python client / Rust server，并有双版本兼容测试。

#### 11.1.3 Rolling upgrade 顺序

生产滚动升级必须遵守：

1. 先发布可被旧 Python client 兼容的 sidecar / PyO3 artifact，或先发布支持旧新两套协议的 dual-stack Python client。
2. readiness 必须在 compatibility handshake 通过后才为 ready。
3. Python client 在 connect、首次调用、sidecar reconnect、sidecar version 变化时都必须重新校验兼容性。
4. `shadow` 阶段发现不兼容时，可回退 Python legacy path，并记录 `rust.protocol_incompatible`。
5. `enforce` 阶段发现不兼容时必须 fail closed；只有对应 PRD 明确允许且不会放宽安全 / 权限 / 数据一致性约束的只读降级例外。

#### 11.1.4 审计与测试

协议不兼容必须输出脱敏 structured audit / metrics，至少包含 component、expected version、actual version、schema fingerprint、error code table fingerprint 与 mode，不记录 raw payload。

每个 sidecar / PyO3 kernel 必须提供：

- compatibility matrix tests；
- old client / new sidecar smoke；
- new client / old sidecar smoke；
- protocol incompatible fail-closed tests；
- golden fixtures；
- dual-stack 测试（仅当声明支持 dual-stack）。

### 11.2 Sidecar network exposure / service discovery / security boundary 冻结

Rust sidecar 的网络暴露策略冻结为 **内部可访问、禁止外部直连**。

明确允许的访问方式：

1. Python runtime / 受控内部组件通过 Unix domain socket 访问。
2. Python runtime / 受控内部组件通过 `127.0.0.1` / loopback 访问。
3. 同主机、同 Pod 或内部容器网络访问。
4. 通过私有服务发现访问。
5. 跨主机内网访问时必须有网络隔离，并启用 mTLS 或等价服务身份校验。

明确禁止：

1. sidecar 对公网暴露。
2. 前端、浏览器、终端用户、普通 Skill、外部系统直接调用 sidecar。
3. Skill manifest、LLM 输出、用户输入、外部 tool output 指定任意 sidecar 地址、端口、socket path 或 binary path。
4. health、readiness、metrics、debug、admin endpoint 对公网开放。
5. 未授权 client 调用 sidecar。

服务发现策略：

- sidecar endpoint 必须来自部署配置、环境变量或 runtime allowlist。
- Python runtime 必须在 connect 前校验 endpoint scheme、host、port / socket path、component、protocol compatibility 与 allowlist。
- dev/test launcher 可使用 loopback 端口，但必须标记为 dev/test；不得作为生产暴露策略。
- 生产跨主机访问必须由内网、service mesh、Kubernetes service、mTLS 或等价机制约束身份与网络边界。

故障处理：

- `shadow` 阶段发现 endpoint 不安全时，可回退 Python legacy path，并写 `rust.sidecar_exposure_denied` audit event。
- `enforce` 阶段发现公网绑定、未授权 client、未配置 mTLS 的跨主机访问、service discovery 不在 allowlist、debug/metrics 外网可达时，必须 fail closed。
- 任何 fallback 都不得放宽安全、权限、数据一致性、路径、secret 或外部输入校验约束。

### 11.3 Config / secrets / identity 管理冻结

Rust sidecar / PyO3 kernel 的配置、secret 与身份管理策略冻结如下。

#### 11.3.1 配置来源

允许的配置来源：

1. 部署配置；
2. 环境变量；
3. secret manager / deployment secret injection；
4. 只读配置文件；
5. Python runtime 显式注入的 runtime allowlist。

禁止的配置来源：

1. 用户输入；
2. Skill manifest；
3. LLM 输出；
4. 外部 tool output；
5. 前端请求；
6. 未 allowlist 的本地路径或远程 URL。

sidecar endpoint、socket path、service name、mTLS 配置、DB DSN、provider key、HMAC / session key、artifact storage root、sandbox policy root 等均必须来自允许来源。

#### 11.3.2 Secret 边界

secret、token、mTLS private key、数据库连接串、provider key、session / HMAC key、password hash pepper 等敏感值：

- 不得写入 tracked 文件；
- 不得进入 audit、metrics、structured logs、typed error `message` 或 `safe_metadata`；
- 不得作为 shadow diff、retry event、correction event、health / readiness / version response 的原始字段；
- 只允许记录脱敏 `secret_id`、`version`、`fingerprint` 或 `configured=true/false`。

secret 文件如由部署系统注入，必须只读挂载并使用最小权限；mTLS private key 不得被普通 Skill、脚本、外部 tool 或前端路径读取。

#### 11.3.3 Identity / mTLS

跨主机 sidecar 通信必须使用 mTLS 或等价服务身份校验。Python client 必须校验 sidecar identity、component、protocol version 与 allowlist；sidecar 必须校验 client identity 或受控内部通道身份。

loopback / Unix domain socket 场景仍必须校验 endpoint allowlist；Unix domain socket 必须依赖受控目录与文件权限。

#### 11.3.4 Rotation / reload

sidecar 必须支持 secret rotation，允许两种方式：

1. 受控 reload：仅内部触发，reload 后重新校验 identity / compatibility / readiness；
2. 滚动重启：通过外部进程管理器 / 容器编排替换实例。

rotation 不得中断已接受且可安全完成的请求；无法保证安全时必须进入 shutdown drain。rotation 事件只记录 secret version / fingerprint 与结果，不记录 secret value。

#### 11.3.5 故障处理

`shadow` 阶段配置 / identity / secret 校验失败时，可回退 Python legacy path，并写脱敏 audit event。`enforce` 阶段以下情况必须 fail closed：

- 必需配置缺失；
- secret 缺失或不可读取；
- mTLS certificate 过期、不受信、component mismatch；
- client identity 未授权；
- endpoint 不在 allowlist；
- secret reload 失败；
- secret 出现在 audit / metrics / error / logs 的泄露风险。

## 12. Typed error / error code / retry-correction policy 冻结

Rust typed error、Python exception、CapabilityExecutionError、gRPC sidecar error 与 API error 的稳定策略冻结如下。

### 12.1 Typed error schema

所有 Rust kernel / sidecar error 必须至少包含：

| 字段 | 要求 |
|---|---|
| `code` | lowercase snake_case string；不使用 dotted code |
| `message` | 面向 API / audit 的安全说明；不得包含敏感细节 |
| `retriable` | 是否允许自动重试的必要条件，但不是充分条件 |
| `category` | 稳定分类，用于 retry / correction / fail-closed 策略 |
| `safe_metadata` | 仅允许脱敏结构化 metadata，例如 component、duration、input fingerprint、retry_after_ms |

禁止把 raw provider error、secret、真实文件路径、数据库连接串、base_url、完整 prompt、完整 rows、原始外部 tool output 放进 `message` 或 `safe_metadata`。

`category` 初始冻结为：`validation`、`auth`、`permission`、`policy`、`security`、`contract`、`state_conflict`、`unavailable`、`timeout`、`rate_limited`、`transient`、`internal`。新增 category 必须更新本 PRD。

### 12.2 Error code 前缀

error code 前缀按组件固定：

| 组件 | 前缀 |
|---|---|
| Core | `core_` |
| Lifecycle | `lifecycle_` |
| Runtime store | `runtime_store_` |
| Dispatcher | `dispatcher_` |
| Event log | `event_log_` |
| Skill runtime | `skill_runtime_` |
| MCP runtime | `mcp_runtime_` |
| Artifact store | `artifact_` |
| Auth core | `auth_` |
| Data access | `data_access_` |
| Audit sanitizer | `audit_sanitizer_` |

`runtime_store_unavailable` 与 `dispatcher_unavailable` 作为首批 runtime sidecar error code 冻结。

### 12.3 gRPC / Python / API 映射

1. gRPC sidecar error 必须使用 protobuf typed error message 承载，不依赖字符串拼接解析。
2. Python sidecar client 必须把 protobuf typed error 映射为现有 Python exception 或 `CapabilityExecutionError`。
3. API 层只暴露稳定 `code` 与安全 `message`，必要时暴露 `retriable`；不得透出 raw Rust / provider / OS error。
4. Rust error code table 必须作为 contract artifact 生成或导出，并被 Python facade / CI 校验。

### 12.4 自动重试策略

自动重试只允许在以下条件全部满足时发生：

1. typed error `retriable=true`；
2. 操作具备 idempotency key 或已证明无副作用；
3. category 属于可重试类别，例如 `unavailable`、`timeout`、`rate_limited`、`transient`、可安全重试的 `state_conflict`；
4. retry policy 明确 max attempts、backoff、jitter、deadline；
5. 每次重试写入脱敏 retry audit event，至少包含 component、code、category、attempt、max_attempts、backoff_ms、idempotency fingerprint。

Dispatcher / Store / Event sidecar 进入 `enforce` 后，写路径失败可以对同一个 Rust sidecar 执行幂等重试；不允许 fallback 到 Python legacy store。重试耗尽后返回 `runtime_store_unavailable` / `dispatcher_unavailable` 等稳定 typed error。

### 12.5 自动修正策略

自动修正只允许处理系统生成的结构化内容，例如：

- LLM planner 输出 JSON 格式错误；
- enum 大小写 / alias 可确定归一；
- 缺少可由系统补齐的默认字段；
- schema 字段名轻微不匹配，且 allowlist 可验证。

自动修正不得改变用户真实意图，不得补造权限，不得放宽 allowlist / schema / sandbox / path / readonly / sanitizer 规则，不得把安全失败改写为成功。自动修正必须写 structured audit，记录 correction kind、source fingerprint 与结果，不记录敏感 payload。

### 12.6 必须 fail-closed 的 error

以下 error 不得自动重试或自动修正，除非对应 PRD 明确允许同等安全约束下的只读或幂等重试：

- permission / auth 失败；
- service binding 越权；
- Skill allowlist / trust gate / sandbox policy 失败；
- MCP schema / output sanitizer 失败；
- artifact path / archive safety 失败；
- DB readonly policy 失败；
- secret / prompt / rows 泄露风险；
- contract mismatch；
- lifecycle 非法状态转移。

## 13. Observability / audit / metrics / structured output validation 冻结

Rust observability、结构化输出校验与失败重试策略冻结如下。

### 13.1 结构化输出校验

所有 Rust kernel / sidecar 输出到 Python、API、audit、metrics 或 shadow compare 的结构化内容，必须在消费前通过 schema / protobuf / JSON schema / contract artifact 校验。

必须校验的输出包括：

- gRPC response / error response；
- PyO3 return object / serialized JSON；
- structured audit event；
- metrics event；
- shadow compare diff event；
- retry event；
- auto-correction event；
- sidecar health / readiness / version response；
- Skill / MCP / artifact / DB / auth 等安全敏感模块输出。

校验失败必须转换为稳定 typed error，默认 `category=contract`、`retriable=false`、fail closed。只有当失败来源被判定为 transient / incomplete transport response，且操作具备 idempotency key 或已证明无副作用，并且 retry policy 明确允许时，才可以设置 `retriable=true` 并自动重试。

结构化输出校验不得把非法输出“修成成功”。自动修正仍只允许处理系统生成的结构化内容，且必须满足第 12.5 节约束。

### 13.2 Trace context 与统一字段

Python 调用 Rust kernel / sidecar 时必须透传 trace context。Rust structured event 至少包含以下脱敏字段中适用项：

| 字段 | 要求 |
|---|---|
| `trace_id` | 全链路 trace id；缺失时由入口生成 |
| `conversation_id` / `task_id` / `node_id` | 仅记录业务 id，不记录 prompt / rows |
| `component` | Rust component / crate / sidecar 名称 |
| `mode` | `off` / `shadow` / `enforce` |
| `sidecar_version` | sidecar binary / image version，PyO3 kernel 可为空 |
| `protocol_version` | proto package / contract artifact version |
| `duration_ms` | Rust 边界耗时 |
| `error_code` / `category` / `retriable` | 来自 typed error schema |
| `attempt` / `max_attempts` | retry event 必填 |
| `input_fingerprint` / `output_fingerprint` | hash / fingerprint，不记录原始 payload |
| `redaction_applied` | 是否应用脱敏 |

禁止字段：raw provider error、secret、token、API key、真实文件路径、数据库连接串、base_url、完整 prompt、完整 rows、原始外部 tool output。

### 13.3 必备事件类型

所有专题必须支持以下结构化事件类型中适用项：

- `rust.shadow_diff`
- `rust.retry`
- `rust.retry_exhausted`
- `rust.correction`
- `rust.structured_output_validation_failed`
- `rust.fail_closed`
- `rust.fallback`
- `rust.sidecar_health_changed`
- `rust.protocol_incompatible`

事件必须先通过 schema 校验，再进入 audit / metrics sink。schema 校验失败本身必须产出最小安全 fallback audit event，只包含 component、event kind、error code、input fingerprint 与 duration。

### 13.4 自动重试与观测闭环

发生可重试错误时，系统必须按第 12.4 节 retry policy 自动重试，并为每次 attempt 记录 `rust.retry`。重试耗尽必须记录 `rust.retry_exhausted`，返回稳定 typed error，并按对应模块策略 fail closed 或受限只读降级。

结构化输出校验错误的重试规则：

1. schema / protobuf / contract mismatch 默认不可重试，必须 fail closed；
2. sidecar transport half-response、timeout、temporary unavailable 等 transient error 可在幂等前提下重试；
3. retry 后仍不符合 schema，必须 fail closed；
4. security / auth / permission / sanitizer / readonly / sandbox / allowlist / lifecycle transition error 不得通过 retry 或 correction 放宽。

### 13.5 Metrics 最低集

每个 Rust kernel / sidecar 至少输出以下 metrics：

- request count；
- error count by `code` / `category`；
- retry count / retry exhausted count；
- fail-closed count；
- fallback count；
- shadow mismatch count；
- duration histogram / P95；
- structured output validation failure count；
- redaction applied count。

sidecar 还必须输出 health、readiness、liveness、version 与 queue / lease / active task 等模块相关 metrics。

## 14. Resource limit / backpressure / deadline / cancellation 冻结

Rust sidecar resource limit v1 默认生产基线冻结如下。模块专题可以收紧，或在对应 PRD 中声明更严格 hard cap；不得用普通部署配置突破本文档 hard cap。确需放宽 hard cap 时，必须单独 PRD 决策、补压测和故障注入证据。

### 14.1 全局硬规则

| 项 | 冻结值 |
|---|---|
| 所有请求必须有 deadline | 必须 |
| 无界队列 / 无界 stream / 无界 stdout/stderr | 禁止 |
| 默认 retry max attempts | 3 次总尝试，含 initial attempt；即最多 2 次 retry |
| retry backoff | 100ms 起，指数退避，最大 1s，±20% jitter |
| health deadline | 1s |
| readiness / version deadline | 2s |
| shutdown drain | 30s |
| audit / metrics event 单条大小 | 64KB |
| 默认 request size | 1MB，模块未覆盖时适用 |
| 默认 response size | 4MB，模块未覆盖时适用 |
| 超限行为 | 返回 typed error，默认 fail closed |
| 非幂等请求自动重试 | 禁止 |

必备 typed error 语义：

- overloaded / queue full；
- queue wait timeout；
- deadline exceeded；
- payload too large；
- stream idle timeout；
- cancelled；
- shutdown draining。

具体 error code 使用组件前缀，例如 `runtime_store_overloaded`、`dispatcher_deadline_exceeded`、`skill_runtime_payload_too_large`、`mcp_runtime_stream_idle_timeout`。

### 14.2 Dispatcher / Store / Event sidecar 限制

| 项 | 冻结值 |
|---|---|
| max in-flight | `min(64, cpu * 4)`，最低 8 |
| queue size | 1024 |
| queue 等待上限 | 2s |
| task submit deadline | 3s |
| state transition deadline | 2s |
| event append deadline | 2s |
| lease acquire / renew deadline | 1s |
| event replay deadline | 10s |
| event 单条 payload | 256KB |
| replay page | 1000 events 或 1MB，先到为准 |
| enforce 写失败 fallback | 禁止，只能同 sidecar 幂等 retry |

### 14.3 Skill Sandbox sidecar 限制

| 项 | 冻结值 |
|---|---|
| max concurrent executions | `min(8, cpu)`，默认 4 |
| per-skill concurrent | 2 |
| queue size | 64 |
| queue 等待上限 | 10s |
| 默认执行 timeout | 60s |
| hard timeout | 300s |
| stdout / stderr | 各 1MB |
| 单次 structured result | 4MB |
| 输出 artifact 默认上限 | 32MB，超出走 artifact policy |
| cancel grace | 5s 后强杀进程树 |
| retry | 只有进程未启动或明确幂等时允许 |

### 14.4 MCP Runtime sidecar 限制

以下限制适用于普通短 MCP tool call。MCP 长任务 / 完整流式 SSE 的持续时间、idle timeout、reconnect 与任务状态治理以 `docs/prd/backend/17-MCP长任务流式SSEPRD.md` 为准；Rust MCP sidecar 后续实现不得把本表 hard cap 用来禁止已显式配置的长任务流。

| 项 | 冻结值 |
|---|---|
| max concurrent tool calls | 16 |
| per MCP server concurrent | 4 |
| queue size | 128 |
| list_tools deadline | 10s |
| call_tool 默认 deadline | 60s |
| call_tool hard cap | 300s |
| raw tool output cap | 8MB，之后截断或拒绝，按 tool / sanitizer policy 决定 |
| sanitized output cap | 4MB |
| stream idle timeout | 30s |
| side-effecting tool retry | 默认禁止 |

### 14.5 Artifact / Auth / DataAccess / Audit 限制

| 项 | 冻结值 |
|---|---|
| auth primitive deadline | 1s |
| artifact / path / hash deadline | 5s |
| DB readonly deadline | 默认 10s，hard cap 30s |
| DB row limit | 500 rows |
| DB column limit | 100 columns |
| DB result size | 10MB |
| upload preview | 10MB |
| archive operation hard cap | 60s |
| auth / path / redaction / readonly policy retry | 禁止 |

### 14.6 Backpressure / cancellation / shutdown

1. queue full 或 queue wait timeout 必须立即返回 typed overload error，不得无限等待。
2. Python client 必须向 sidecar 透传 deadline 和 cancellation signal。
3. sidecar 收到 cancellation 后必须停止未开始的排队请求；已启动的进程型任务按模块 cancel grace 清理。
4. shutdown drain 期间不得接受新写入 / 新执行请求；允许完成已接收且未超 deadline 的请求。
5. shutdown drain 到期后必须终止剩余任务并输出 structured audit。
6. 所有 backpressure / cancel / deadline / shutdown event 必须进入 metrics，至少包含 component、mode、queue_depth、in_flight、deadline_ms、duration_ms、error_code。


## 15. Build artifact provenance / SBOM / supply-chain 冻结

Rust 产物供应链策略冻结为“CI / 部署预构建 + allowlist 加载 + provenance 校验”。最终交付版不得依赖运行时编译、请求路径下载或临时替换 native artifact。

### 15.1 适用产物

| 产物 | 必须来源 | 必须附带元数据 |
|---|---|---|
| PyO3 wheel | CI / 部署流水线 `maturin` 构建 | package version、Python ABI、target triple、git commit、toolchain、Cargo.lock digest、checksum、SBOM、contract hash |
| sidecar binary / image | CI / 部署流水线 Cargo / image build | image digest / binary sha256、build profile、target triple、features、proto hash、SBOM、provenance |
| native binary | CI / 部署流水线 Cargo build | binary sha256、exit protocol version、contract hash、SBOM、provenance |
| Skill-owned Rust artifact | Skill 项目 CI / 平台部署流水线 | Skill bundle revision、adapter、contract_version、artifact id、checksum、SBOM、provenance |

### 15.2 加载与校验规则

1. Runtime 只能加载部署配置 / runtime allowlist 中声明的 Rust artifact。
2. artifact allowlist 必须校验 component、artifact id、version、checksum、contract / proto hash、target triple 与 provenance record。
3. `cargo build`、`cargo run`、`rustc`、依赖下载、动态替换 wheel / binary / image 在 runtime 启动和业务请求路径中一律禁止。
4. `cargo audit`、`cargo deny`、license / advisory policy、SBOM 生成与 checksum / provenance 校验属于 release gate；失败不得发布。
5. 生产 artifact 不得依赖未审查的 git/path dependency；确需使用必须 pin 到不可变 revision，并在 PRD / cargo-deny 策略中说明。
6. provenance / SBOM / checksum 只能记录 artifact 与 dependency 元数据，不得包含 secret、token、DSN、provider key 或本机真实敏感路径。
7. `enforce` 下 artifact 缺失、checksum mismatch、provenance 缺失、SBOM 缺失、contract hash mismatch 或 allowlist 未授权必须 fail closed，并输出脱敏 typed error / audit。

## 16. Benchmark / performance regression / SLO 冻结

Rust 化不是只为“能跑”，而是为了长期交付级 Agent runtime 的可预测性能、低抖动与可回归治理。每个 Rust 模块必须建立可重复 benchmark 与 SLO，不得仅凭单次手工 smoke 决定上线。

### 16.1 必测指标

| 指标 | 要求 |
|---|---|
| latency | P50 / P95 / P99，至少覆盖小 / 中 / 大 payload |
| throughput | 单实例吞吐、并发场景吞吐、queue wait |
| CPU / memory | 平均、峰值、RSS / heap trend；不得有无界增长 |
| boundary overhead | PyO3 FFI 往返、gRPC sidecar RPC、serialization / deserialization 成本 |
| error / retry | retriable / non-retriable error rate、retry amplification |
| payload | input / output size、truncation、sanitization cost |

### 16.2 默认性能门禁

1. 每个模块必须先记录 Python legacy baseline，再记录 Rust implementation baseline。
2. 进入 `shadow` 前必须有本地 benchmark 证据；进入 `enforce` 前必须有 CI / release benchmark 证据。
3. 默认 P95 latency 不得高于 Python legacy path 的 110%；若模块主要为安全 / 一致性目的而非性能目的，仍必须证明劣化在可接受 SLO 内，并由专题 PRD 明确说明。
4. Rust implementation 的 error rate 不得高于 legacy；retry 不得造成不可控放大。
5. CPU / memory 出现持续劣化、RSS 单调增长或 queue backlog 无法 drain 时，必须阻断发布。
6. Benchmark 结果必须纳入 release artifact 或 CI artifact，支持后续 PR 对比。
7. 性能回归处理必须优先修复；不得通过放宽 timeout、queue 或 payload hard cap 掩盖问题。

## 17. State migration / backup / restore / disaster recovery 冻结

凡是 Rust 模块拥有或改变持久状态，就必须按状态系统而不是普通库函数治理。最终交付版必须可迁移、可恢复、可演练。

### 17.1 适用状态

- Dispatcher / Store / Event sidecar 的 SQLite schema、event log、lease、cursor、bundle pin。
- 未来 PostgreSQL-compatible contract 对应 schema 与 migration artifact。
- Artifact metadata、upload metadata、retention metadata 与 archive manifest。
- MCP / Skill bundle activation registry、runtime allowlist snapshot、sidecar endpoint registry。
- Rust contract artifact、schema hash、error code table、transition table snapshot 的版本记录。

### 17.2 迁移与容灾规则

1. 每次状态 schema 变更必须有 schema version、forward migration、rollback / roll-forward 策略与 compatibility note。
2. migration 前必须执行 preflight、dry-run、backup 与 migration lock，禁止并发写入破坏一致性。
3. backup 必须可恢复；仅创建备份文件但没有 restore drill 不算通过。
4. Event log / cursor / lease / bundle pin 迁移后必须执行 replay 校验，证明 terminal event、cursor 与 active state 一致。
5. 破坏性迁移无备份、无 restore drill、无 replay 校验时一律禁止进入 `enforce`。
6. state migration 失败必须 fail closed；不得用不完整新 schema 继续接受写入。
7. DR runbook 必须说明 RPO / RTO、备份位置、恢复命令、校验命令、回滚 / 前滚策略与审计事件。

## 18. Python legacy path decommission 冻结

`off` / `shadow` / legacy path 是发布治理工具，不是最终架构。最终交付版必须收敛到 Rust canonical source + Python thin facade / client，不得长期保留双写或双语义实现。

### 18.1 下线条件

1. 对应 Rust module 已进入 `enforce`，并连续通过稳定窗口：至少 30 天生产运行或 10000 次有效 enforce 样本，取更严格者；模块 PRD 可进一步收紧。
2. contract drift = 0、panic / crash = 0、critical / high incident = 0。
3. rollback drill、restore drill、compatibility matrix、security / redaction / secret leak 测试全部通过。
4. Python legacy path 不再作为自动 fallback；生产恢复依赖 artifact / deployment rollback，而不是隐式 Python 语义接管。
5. 删除 PR 必须同时删除重复状态机、写路径、安全策略、sanitizer、schema / error code 定义与测试假设，只保留 facade / client / DTO adapter。
6. 删除后必须保留 golden fixtures、contract artifact 与 regression tests，防止 Rust canonical source 漂移。

### 18.2 禁止状态

- `enforce` 已上线但 Python 仍拥有另一套可被隐式调用的状态机 / store 写路径。
- Rust 和 Python 分别维护同名 enum、状态转移、error code 或 sanitizer 规则。
- 回滚依赖“悄悄切回 Python 写路径”而不是部署级 rollback / restore。
- 为了兼容旧测试而保留已无生产职责的 Python 业务语义。

## 19. Ops runbook / incident / rollback drill 冻结

每个 Rust sidecar / PyO3 kernel 进入 `enforce` 前，必须具备可执行运维手册，而不是只在开发者脑内运行。

### 19.1 必备运维资产

| 资产 | 最低要求 |
|---|---|
| dashboard | health、readiness、version、request rate、latency、error rate、queue depth、in-flight、retry、panic/crash、memory、CPU |
| alert | unavailable、readiness failed、protocol mismatch、contract mismatch、queue full、deadline spike、error rate spike、secret / identity failure、migration failure |
| runbook | 诊断、drain、restart、rollback、restore、replay 校验、secret rotation、artifact quarantine |
| incident policy | severity、owner、升级路径、用户影响判断、审计要求、事后复盘 |
| drill evidence | rollback drill、restore drill、compatibility failure drill、identity / secret failure drill、overload drill |

### 19.2 进入 `enforce` 的运维门禁

1. health / readiness / version endpoint 或 PyO3 contract probe 必须可被自动化探测。
2. dashboard 与 alert 必须覆盖 SLO 和本文档已冻结的 fail-closed 场景。
3. rollback / restore / drain / restart 命令必须在 staging 或等价环境演练通过。
4. 事故处理必须能定位到 component、mode、version、artifact digest、schema hash、error code 与 trace id。
5. 无 runbook、无告警、无演练证据或告警不可达时，不得进入 `enforce`。

## 20. CI 与质量门禁

冻结必跑门禁：

```bash
cargo fmt --check
cargo clippy --workspace --all-targets --all-features -- -D warnings
cargo test --workspace --all-features
cargo nextest run --workspace --all-features
cargo audit
cargo deny check
cargo llvm-cov --workspace --all-features --summary-only
```

按模块补充：

- PyO3：`maturin` wheel build smoke、Python 3.13 import smoke、panic boundary tests。
- sidecar：Cargo binary build、Linux x86_64 image / binary smoke、binary start/health/shutdown smoke、client compatibility tests、external process manager / dev launcher smoke、proto compatibility tests、rolling upgrade compatibility matrix、golden fixtures、structured output validation tests、resource limit / backpressure / deadline / cancellation tests、config / secret / identity tests、artifact checksum / SBOM / provenance verification、benchmark / performance regression gate、runbook / alert / rollback drill evidence。
- storage：migration fixture、SQLite compatibility tests、future PostgreSQL adapter contract tests、backup / restore / replay / migration lock drill。
- security/path：property tests、bounded fuzz smoke、nightly / release fuzz、coverage threshold gate。

## 21. Rollout / rollback

1. 已存在的 `native/` workspace 与 `maf_mcp_runtime` skeleton 只代表 MCP Phase 1 基线；后续新增 crate、空 workspace 扩展或占位测试仍必须先有实现 PRD，不得提交空 crate 或空测试。
2. 每个 crate / sidecar / 子模块使用统一 `MAF_RUST_<COMPONENT>_MODE=off|shadow|enforce` runtime config 控制启用。
3. `shadow` / dev 阶段 PyO3 import 失败、sidecar health 失败或 protocol / contract 不兼容时，可回退 Python legacy path；`enforce` 阶段默认 fail closed，只有对应 PRD 显式允许且不放宽安全 / 权限 / 数据一致性约束时才可 fallback。
4. 稳定期前旧 Python implementation 保留。
5. 生产滚动升级必须先通过 compatibility handshake、readiness 和兼容矩阵测试；breaking change 必须进入 v2 / contract major version 或 dual-stack 路径。
6. 生产 sidecar endpoint 必须通过内部通道和 allowlist 发现；`enforce` 下不安全暴露或未授权访问必须 fail closed。
7. 生产 sidecar resource limit 允许按部署配置收紧或在 hard cap 内扩容；突破 hard cap 必须单独 PRD。
8. 生产 sidecar secret / identity 允许通过受控 reload 或滚动重启轮换；`enforce` 下缺失、过期、不匹配或泄露风险必须 fail closed。
9. 生产 Rust artifact 必须通过 checksum / SBOM / provenance / allowlist 校验；校验失败不得启动或接流量。
10. 进入 `enforce` 前必须完成 benchmark / SLO gate；发布后性能回归触发 rollback 或阻断继续放量。
11. Rust-owned 状态变更必须先完成 backup / restore / replay drill；migration 失败必须 fail closed。
12. Rust canonical 稳定后必须执行 Python legacy path decommission；最终生产恢复依赖 artifact / deployment rollback，不依赖隐式 Python 语义 fallback。
13. `enforce` 前必须具备 runbook、dashboard、alert 与 rollback / restore / overload / identity failure 演练证据。

## 22. 验收标准

| 编号 | 验收项 | 证明方式 |
|---|---|---|
| RUST-TOOL-AC-001 | 本地 Conda `multi_agent` 环境可执行 Rust 构建说明 | 文档 + 命令输出 |
| RUST-TOOL-AC-002 | CI 包含 fmt、clippy、test、nextest、audit、deny 必跑门禁 | CI 配置审查 / 命令输出 |
| RUST-TOOL-AC-003 | PyO3 wheel、sidecar binary、Linux image / binary 有清晰发布路径 | build 文档审查 / artifact 列表 |
| RUST-TOOL-AC-004 | runtime 请求路径不会编译 Rust 或下载依赖 | 代码审查 + fail test |
| RUST-TOOL-AC-005 | `shadow` 差异只进入脱敏 audit / metrics，不影响用户结果 | shadow compare tests |
| RUST-TOOL-AC-006 | `enforce` 启用前满足全局最低 promotion threshold | shadow report + test evidence + rollback drill |
| RUST-TOOL-AC-007 | `enforce` failure 默认 fail closed，允许 fallback 的场景有显式 PRD 依据和 audit | failure injection + audit evidence |
| RUST-TOOL-AC-008 | protobuf schema 使用统一 `native/proto/maf/<domain>/v1/` 归属与版本策略 | proto tree review + compatibility tests |
| RUST-TOOL-AC-009 | 核心依赖使用冻结技术栈，替代依赖有 PRD 依据 | dependency review + cargo deny/audit |
| RUST-TOOL-AC-010 | `rust-toolchain.toml` 固定具体 stable 版本、edition 2024、MSRV 一致 | toolchain file review + CI output |
| RUST-TOOL-AC-011 | macOS arm64 开发 smoke、Linux x86_64 生产构建基线、Python 3.13 PyO3/client smoke 明确覆盖 | CI / 本地 smoke evidence |
| RUST-TOOL-AC-012 | Windows 未被列为必需发布目标 | CI matrix / release 文档审查 |
| RUST-TOOL-AC-013 | 所有 Rust crate 生成 `cargo-llvm-cov` 覆盖率报告并满足 80% / 90% line coverage 阈值 | coverage report / CI gate |
| RUST-TOOL-AC-014 | 不可信输入边界启用 `cargo-fuzz`，PR bounded smoke 与 nightly / release 长跑 job 均有证据 | fuzz logs / CI artifacts |
| RUST-TOOL-AC-015 | typed error schema、error code prefix、retry / correction / fail-closed 策略被实现并有映射测试 | generated error table + Python/gRPC/API tests |
| RUST-TOOL-AC-016 | Rust structured output / audit / metrics / shadow event 均通过 schema 校验；校验失败按 retry / fail-closed 策略处理 | schema validation tests + retry audit + failure injection |
| RUST-TOOL-AC-017 | Rust tracing / metrics 可被 Python trace context 串联，且不泄露敏感 payload | observability smoke + redaction snapshot |
| RUST-TOOL-AC-018 | Python client / PyO3 facade 与 Rust sidecar / kernel contract handshake、rolling upgrade、协议不兼容 fail-closed 均有测试 | compatibility matrix + readiness tests + audit evidence |
| RUST-TOOL-AC-019 | sidecar 仅通过受控内部通道访问；公网绑定、未授权 client、非 allowlist service discovery 在 `enforce` 下 fail closed | endpoint validation tests + security/failure injection |
| RUST-TOOL-AC-020 | sidecar max in-flight、queue、deadline、payload size、cancel、shutdown drain、backpressure 均有默认值、配置边界和故障注入测试 | resource limit tests + overload/deadline/cancel/shutdown evidence |
| RUST-TOOL-AC-021 | sidecar config / secret / identity 只来自允许来源；secret 不泄露；rotation / reload / identity mismatch fail-closed 可验证 | config source tests + redaction snapshot + mTLS/identity failure injection |
| RUST-TOOL-AC-022 | Rust artifact checksum、SBOM、Cargo.lock digest、provenance 与 runtime allowlist 校验可验证；请求路径不编译 / 下载 / 替换产物 | release artifact review + load failure injection + code review |
| RUST-TOOL-AC-023 | 每个 Rust 模块有 Python baseline、Rust baseline、FFI / sidecar overhead 与 P50/P95/P99、CPU、memory、throughput 性能门禁 | benchmark report + CI / release regression gate |
| RUST-TOOL-AC-024 | Rust-owned 状态迁移具备 schema version、migration lock、preflight、dry-run、backup、restore、replay 校验与 DR runbook | migration tests + restore drill + replay evidence |
| RUST-TOOL-AC-025 | Rust canonical 稳定后重复 Python legacy 语义下线，只保留 facade / client / DTO adapter | decommission PR + grep / architecture guard + regression tests |
| RUST-TOOL-AC-026 | `enforce` 前具备 dashboard、alert、SLO、runbook 与 rollback / restore / overload / identity failure 演练证据 | ops checklist + drill records + alert smoke |

## 23. 风险

| 风险 | 缓解 |
|---|---|
| Rust 工具链污染 Python 开发路径 | 工具链独立文档，Python fallback 保留 |
| macOS / Linux wheel 差异 | CI matrix 与本地 smoke 分开定义 |
| sidecar 运维复杂 | 生产交给外部进程管理器 / 容器编排；dev/test 提供 launcher、health、metrics、rollback 验证；`enforce` 前必须有 runbook、alert 与演练证据 |
| Rust 供应链产物不可追溯 | 所有 wheel / binary / image / Skill artifact 必须有 checksum、SBOM、Cargo.lock digest、provenance 与 allowlist 校验 |
| 性能优化变成不可量化主观判断 | 每个模块保留 Python baseline、Rust baseline 与 release benchmark，性能回归阻断发布 |
| 状态迁移或恢复失败造成数据不可用 | 所有 Rust-owned 状态变更必须有 migration lock、备份、restore drill、replay 校验与 DR runbook |
| Python legacy path 长期残留导致双语义漂移 | Rust canonical 稳定后执行 decommission PR，Python 只保留 facade / client / DTO adapter |
