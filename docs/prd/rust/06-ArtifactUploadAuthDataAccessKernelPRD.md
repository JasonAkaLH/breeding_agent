# Artifact / Upload / Auth / DataAccess Rust Kernel PRD

- **状态**：待实现（聚合 PRD、四个子模块边界、coverage / fuzz 门禁已冻结）
- **日期**：2026-05-14
- **来源基线**：`docs/prd/backend/16-Rust化Runtime模块评估PRD.md` RUST-P0-008、RUST-P1-001、RUST-P1-003、RUST-P1-005、9.6、10.3
- **影响范围**：`src/storage/artifact_files.py`、`src/api/upload_store.py`、`src/auth/`、`src/integrations/mysql_readonly.py`、`src/mysql_engine.py`、audit/event serialization

## 1. 问题陈述

artifact 文件、上传、路径、hash、quota、archive safety、auth primitives、readonly DB adapter 与 audit sanitizer 都处于安全敏感边界。它们不是产品语义，但承载外部输入、文件系统、secret、DB row 与审计数据，适合拆成 Rust deterministic kernels。

## 2. 目标

1. 将 path normalization、escape check、hash、quota、retention、archive safety Rust 化。
2. 将 password hash verify、HMAC、captcha verify、session token / TTL core 逐步 Rust 化。
3. 将 readonly DB adapter 的 timeout、row decoding、result shape 与只读约束抽为 Rust candidate kernel。
4. 将 audit payload sanitizer、event serializer、privacy filter 固化为可测试 Rust kernel。

## 3. 非目标

1. 不改变 API download response、auth dependency、cookie/session wiring。
2. 不在本 PRD 中做 PostgreSQL productionization；DB production adapter 后续单独决策。
3. 不让 Skill 直接读取 DB secret 或完整 rows。
4. 不把业务查询逻辑放进主体框架。

## 4. 聚合边界冻结

`06-ArtifactUploadAuthDataAccessKernelPRD.md` 保持为一个聚合 PRD，不再拆成四份独立 PRD。内部冻结为四个子模块：

| 子模块 | 对应 crate / 归属 | 可独立实现 | 可独立 feature flag |
|---|---|---:|---:|
| Artifact / upload / file safety | `maf_artifact_store` | 是 | 是 |
| Auth primitives | `maf_auth_core` | 是 | 是 |
| DataAccess readonly kernel | `maf_data_access` | 是 | 是 |
| Audit / event sanitizer | `maf_audit_sanitizer` | 是 | 是 |

四个子模块共享同一安全验收基线：不得泄露 secret、token、base_url、完整 prompt、完整 rows、真实文件路径；所有外部输入必须 fail closed，并有 size limit、redaction、audit。

## 5. Audit / Event sanitizer crate 归属冻结

Audit / Event sanitizer 的主体 crate 归属冻结为 `maf_audit_sanitizer`，负责 audit payload sanitizer、event serializer privacy filter、redaction rule 与敏感字段屏蔽。该 crate 归属本聚合 PRD 的 Wave 4 安全热点范围，不并入 `maf_core_types`。

冻结理由：`maf_core_types` 必须保持纯 contract / schema 归属；redaction、privacy filter 与 audit policy 会随安全策略演进，不应污染 core schema。

## 6. 功能需求

- RUST-SAFE-FR-001：本 PRD 不拆分为四份独立 PRD；四个子模块可分别实现、分别 feature flag、分别验收，但共享本 PRD 的安全验收基线。
- RUST-SAFE-FR-002：Artifact/Auth/DataAccess/Audit `enforce` failure 默认 fail closed；auth、path/archive safety、DB readonly enforcement、redaction 失败不得 fallback 到更宽松路径。
- RUST-SAFE-FR-003：`maf_artifact_store`、`maf_auth_core`、`maf_data_access`、`maf_audit_sanitizer` 均属于安全敏感 crate，line coverage 必须不低于 90%。
- RUST-SAFE-FR-004：artifact path / archive / filename sanitizer、audit redaction / secret masking、DB readonly policy / row shaping 必须启用 `cargo-fuzz`；PR 级别 bounded fuzz smoke，nightly / release gate 跑更长 fuzz job。
- RUST-SAFE-FR-005：Artifact/Auth/DataAccess/Audit typed error 必须分别使用 `artifact_`、`auth_`、`data_access_`、`audit_sanitizer_` 前缀；path/archive safety、auth、DB readonly、redaction error 不得自动修正或重试到更宽松路径。
- RUST-SAFE-FR-006：Artifact/Auth/DataAccess/Audit structured output、audit payload、metrics event、redaction result、readonly DB result shape 必须按 contract schema 校验；校验失败必须 fail closed。
- RUST-SAFE-FR-007：本聚合 PRD 的自动重试只允许在 readonly、幂等、无权限放宽且不会重复副作用的 transient error 中发生；auth/path/archive/redaction/readonly policy validation failure 不得自动重试到更宽松路径。
- RUST-SAFE-FR-008：Artifact/Auth/DataAccess/Audit PyO3 facade 或 sidecar client 必须校验 component、contract/protocol version、schema_hash、error_code_table_hash 与 supported_features；安全敏感 contract 不兼容在 `enforce` 下必须 fail closed。
- RUST-SAFE-FR-009：Artifact/Auth/DataAccess/Audit 若以 sidecar 形态实现，sidecar 不得被前端、用户、普通 Skill 或外部系统直连；只允许 Python runtime / 受控内部组件通过内部通道访问。
- RUST-SAFE-FR-010：安全热点 sidecar endpoint 必须来自部署配置 / runtime allowlist；`enforce` 下公网绑定、未授权 client、非 allowlist service discovery 或未配置 mTLS 的跨主机访问必须 fail closed。
- RUST-SAFE-FR-011：Artifact/Auth/DataAccess/Audit 必须执行本文档冻结的 deadline、DB row/column/result、upload preview、archive hard cap 与 retry 限制；auth/path/redaction/readonly policy validation failure 禁止自动重试。
- RUST-SAFE-FR-012：artifact storage root、auth pepper / HMAC / session key、readonly DB DSN、audit redaction salt / policy、mTLS identity 必须来自部署配置 / secret manager / runtime allowlist，不得来自用户输入、Skill manifest、LLM 输出或外部 tool output。
- RUST-SAFE-FR-013：`enforce` 下安全热点 secret 缺失、证书过期、identity mismatch、DB DSN 泄露风险、auth key rotation 失败或 redaction policy 缺失必须 fail closed。
- RUST-SAFE-FR-014：Artifact/Auth/DataAccess/Audit PyO3 wheel、sidecar binary / image 或 native artifact 必须由 CI / 部署流水线预构建，具备 checksum、SBOM、Cargo.lock digest、contract / schema hash 与 provenance；runtime 只能加载 allowlist 产物。
- RUST-SAFE-FR-015：安全热点必须建立 path normalization、archive safety、hash/quota、auth primitive、readonly row shaping、audit redaction 的 Python baseline 与 Rust benchmark；P95/P99、CPU、memory、result size 与 redaction throughput 必须纳入 SLO。
- RUST-SAFE-FR-016：artifact metadata、retention metadata、upload metadata、audit redaction policy snapshot 或 readonly service registry 如由 Rust 持久化，必须具备 migration lock、backup、restore 与 rollback / roll-forward runbook。
- RUST-SAFE-FR-017：安全热点 Rust canonical 稳定后，重复 Python path/auth/DB readonly/audit sanitizer 语义必须下线；最终生产只保留 Python API / facade / service registry adapter。
- RUST-SAFE-FR-018：安全热点进入 `enforce` 前必须具备 dashboard、alert、SLO、artifact quarantine、restore、secret rotation、identity mismatch、redaction failure 与 DB limit failure 演练证据。


### 6.1 Artifact / upload / file safety

- RUST-FILE-FR-001：storage key、artifact id、filename、upload id 必须经过 path normalization 与 escape check。
- RUST-FILE-FR-002：managed artifact 必须有 size、sha256、retention metadata。
- RUST-FILE-FR-003：zip/archive 生成和清理必须防 zip-slip、路径穿透、symlink 泄漏。
- RUST-FILE-FR-004：上传 preview 必须限制大小、格式、UTF-8、行数、列数与截断标记。

### 6.2 Auth primitives

- RUST-AUTH-FR-001：password hash verify、HMAC、session token TTL 计算必须有稳定错误码和 constant-time 比较策略。
- RUST-AUTH-FR-002：Rust auth kernel 不得接触 HTTP cookie/session wiring。
- RUST-AUTH-FR-003：captcha verify 的外部 provider glue 留在 Python，Rust 只承载 token/state 校验原语。

### 6.3 DataAccess readonly kernel

- RUST-DB-FR-001：readonly adapter 必须强制 timeout、row count limit、column count limit、result size limit。
- RUST-DB-FR-002：row decoding 与 result shape 必须稳定，不暴露连接串、secret、内部 DSN。
- RUST-DB-FR-003：DB service 只能通过 runtime service registry / allowlist 提供给 trusted platform-service handler。
- RUST-DB-FR-004：DB adapter 错误必须映射为可审计、可脱敏的 stable error。

### 6.4 Audit / event serialization

- RUST-AUDIT-FR-001：audit payload sanitizer 必须屏蔽 secret、token、base_url、完整 prompt、完整 rows、真实文件路径。
- RUST-AUDIT-FR-002：event serializer 必须保持前端事件 schema 兼容。
- RUST-AUDIT-FR-003：privacy filter 必须有 snapshot / golden tests。
- RUST-AUDIT-FR-004：Audit / Event sanitizer 必须归属 `maf_audit_sanitizer`，不得并入 `maf_core_types`。


Runtime config 必须遵守统一命名：`MAF_RUST_ARTIFACT_STORE_MODE` / `MAF_RUST_AUTH_CORE_MODE` / `MAF_RUST_DATA_ACCESS_MODE` / `MAF_RUST_AUDIT_SANITIZER_MODE`=off|shadow|enforce；默认 `off`，生产 `enforce` 前必须经过 `shadow`。

Shadow compare 差异处理策略冻结：`shadow` 模式下，Python legacy path 永远是用户可见结果来源；Rust kernel / sidecar 结果只用于旁路对比。差异必须写入 structured audit / metrics，至少包含 component、input fingerprint、legacy output fingerprint、rust output fingerprint、error code、duration；不得记录完整 prompt、完整 rows、secret、真实文件路径或敏感 payload。shadow 差异不得影响用户结果；只有差异率、错误率、性能指标达到对应专题 PRD 的 promotion threshold 后，才能进入 `enforce`。进入 `enforce` 前还必须满足全局最低 promotion threshold；本专题可更严格，不得更宽松。

Enforce 失败处理策略冻结：`enforce` 模式下 Rust kernel / sidecar 失败默认 fail closed；只有对应 PRD 显式声明可 fallback，且 fallback 不会放宽安全、权限、数据一致性、路径、secret、外部输入校验或审计约束时，才允许回退 Python legacy path。fallback 事件必须写 structured audit。

Structured output validation 策略冻结：Artifact metadata、upload preview、archive result、auth primitive result、readonly DB row shape、audit sanitizer output 与 metrics event 必须先校验再进入 API、前端、Skill service 或 audit sink。validation failure 必须使用对应前缀 typed error，并禁止输出未校验 payload。

Protocol compatibility / rolling upgrade 策略冻结：四个安全热点子模块可分别 feature flag、分别升级，但每个子模块都必须有独立 contract compatibility handshake。兼容 minor 变更允许滚动升级；breaking change 必须升级 contract major version 或 sidecar proto `v2`。`enforce` 阶段不兼容不得回退到更宽松 Python path。

Sidecar network exposure 策略冻结：本聚合 PRD 的默认形态可以是 PyO3 kernel；若某子模块后续采用 sidecar，仍必须遵守内部可访问原则。artifact、auth、DB readonly、audit sanitizer sidecar 不得直接向前端、用户、普通 Skill 或外部系统暴露；artifact download / upload、auth HTTP、DB service binding 等外部入口继续由 Python API / runtime service registry 管控。

Resource limit / backpressure 策略冻结：

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
| audit / metrics event 单条大小 | 64KB |
| auth / path / redaction / readonly policy retry | 禁止 |

deadline exceeded、DB row / column / result size exceeded、upload preview too large、archive timeout、redaction output too large 必须返回对应前缀 typed error，并写 structured audit / metrics。readonly DB 和安全策略失败不得通过 retry 放宽。

Config / secrets / identity 策略冻结：Artifact/Auth/DataAccess/Audit 的 storage root、retention policy、auth secret、HMAC/session key、readonly DB DSN、audit sanitizer redaction policy 与 sidecar identity 只允许来自部署配置、secret manager、只读配置或 runtime allowlist。DB DSN、auth key、session secret、redaction salt、artifact root 真实路径不得进入 audit、metrics、typed error、preview 或 frontend event。secret rotation 必须通过受控 reload 或滚动重启完成；rotation 失败时不得回退到更宽松 Python path。


### 6.5 最终交付门禁冻结

1. 供应链：所有安全热点 wheel / binary / sidecar image 必须具备 checksum、SBOM、Cargo.lock digest、contract / schema hash 与 provenance，并由 runtime allowlist 校验。
2. 性能：path/archive/hash、auth primitive、readonly row shaping、audit redaction 必须有 Python baseline、Rust baseline、P50/P95/P99、CPU、memory、payload/result size 指标；安全模块不得通过放宽 hard cap 掩盖性能问题。
3. 状态 / 容灾：artifact metadata、retention metadata、upload metadata、redaction policy snapshot 或 readonly service registry 如由 Rust 持久化，必须有 backup、restore、migration lock 与 rollback / roll-forward runbook。
4. Python legacy 下线：Rust canonical 稳定后，Python 只保留 API / facade / service registry adapter；旧 path/auth/DB readonly/audit sanitizer 语义不得继续作为隐式 fallback。
5. 运维：artifact quarantine、secret rotation、identity mismatch、redaction failure、DB limit failure、archive timeout、restore drill 必须有告警和演练。

## 7. 测试策略

| 层级 | 测试 |
|---|---|
| Rust property | path normalization、archive path、quota、redaction |
| Rust unit | auth primitives、hash、TTL、row shape、error mapping |
| Fuzz | archive/path/filename sanitizer、upload metadata parser、audit redaction/secret masking、DB readonly policy/row shaping；PR bounded smoke + nightly / release 长跑 |
| Rust coverage | `cargo-llvm-cov` line coverage ≥90% |
| Supply chain | wheel / binary / sidecar image checksum、SBOM、contract hash、provenance、allowlist denial |
| Performance | path / archive / hash / auth / row shaping / redaction Python baseline vs Rust P50/P95/P99、CPU、memory、result size |
| Migration / DR | artifact / upload / retention metadata、redaction policy、service registry backup、restore、migration lock（如由 Rust 持久化） |
| Ops | dashboard / alert smoke、artifact quarantine、secret rotation、identity / redaction / DB limit failure drill |
| Decommission | Python path/auth/DB/audit duplicate semantics removal guard |
| Python regression | artifact API、upload API、auth service、readonly DB adapter tests |
| Security | secret/path/rows 不进入前端或 audit；validation failure no loose retry；public bind / direct-call denial；DB/upload/archive limits；secret / identity mismatch denial |
| Structured output | artifact/auth/DB/audit output schema validation、retry/fail-closed injection |
| Compatibility | per-submodule contract version/schema hash/error table hash compatibility matrix |
| Network boundary | sidecar endpoint allowlist、mTLS/internal-only validation（采用 sidecar 时） |

## 8. 验收标准

| 编号 | 验收项 | 证明方式 |
|---|---|---|
| RUST-SAFE-AC-001 | artifact/upload 防路径穿透和 symlink 泄漏 | property + API tests |
| RUST-SAFE-AC-002 | auth primitive 行为与 Python baseline 一致 | golden tests |
| RUST-SAFE-AC-003 | readonly DB result shape 兼容且脱敏 | adapter contract tests |
| RUST-SAFE-AC-004 | audit sanitizer 不泄露敏感字段 | snapshot tests |
| RUST-SAFE-AC-005 | 安全敏感子模块 coverage / fuzz 达标 | `cargo-llvm-cov` report + fuzz logs |
| RUST-SAFE-AC-006 | 安全热点结构化输出校验失败 fail closed，readonly 幂等 transient 才允许重试 | schema validation + fault injection |
| RUST-SAFE-AC-007 | 安全热点子模块 contract compatibility handshake 与不兼容 fail-closed 可验证 | compatibility matrix + failure injection |
| RUST-SAFE-AC-008 | 安全热点 sidecar 不被外部直连，endpoint 非 allowlist / 公网绑定在 `enforce` 下 fail closed | endpoint allowlist + security/failure injection |
| RUST-SAFE-AC-009 | Auth/Artifact/DataAccess/Audit deadline、DB row/column/result、upload preview、archive hard cap 限制生效 | resource/backpressure tests + security evidence |
| RUST-SAFE-AC-010 | Artifact/Auth/DataAccess/Audit config、secret、identity 只来自允许来源，secret 不泄露，rotation / mismatch fail-closed | config source tests + redaction snapshot + identity failure injection |
| RUST-SAFE-AC-011 | 安全热点 artifact checksum、SBOM、contract / schema hash、provenance 与 runtime allowlist 校验可验证 | release artifact review + load failure injection |
| RUST-SAFE-AC-012 | 安全热点 benchmark 覆盖 path/archive/hash、auth primitive、readonly row shaping、audit redaction 与资源指标 | benchmark report + SLO gate |
| RUST-SAFE-AC-013 | Rust-owned artifact/upload/retention metadata、redaction policy 或 service registry 状态有 backup、restore、migration lock 与 rollback / roll-forward 证据 | migration / restore tests（存在持久状态时） |
| RUST-SAFE-AC-014 | Rust canonical 稳定后重复 Python path/auth/DB readonly/audit sanitizer 语义下线 | decommission PR + architecture guard |
| RUST-SAFE-AC-015 | `enforce` 前具备 dashboard、alert、runbook 与 artifact quarantine / secret rotation / identity / redaction / DB limit 演练证据 | ops checklist + drill records |

## 9. 风险

| 风险 | 缓解 |
|---|---|
| 文件路径跨平台差异 | macOS/Linux path fixture 与 property tests |
| Auth 行为细节漂移 | Python baseline golden tests，逐原语迁移 |
| DB adapter 与业务 Skill 边界混淆 | 主体只提供 readonly service contract，不承载业务逻辑 |
| 安全热点 Rust 产物供应链不可追溯 | CI 产物必须有 checksum、SBOM、contract hash、provenance 与 allowlist 校验 |
| Python 安全策略残留导致双语义 | Rust canonical 稳定后删除重复 path/auth/DB/audit sanitizer 逻辑，Python 仅保留 API / facade / adapter |
