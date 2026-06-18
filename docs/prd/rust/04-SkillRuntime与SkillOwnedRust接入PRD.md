# Skill Runtime 与 Skill-owned Rust 接入 PRD

- **状态**：部分落地（`maf_skill_runtime` policy kernel/contract artifact 与 `native/proto/maf/skill/v1/` sandbox proto 基线已落地，Python manifest/execution facade 已消费 Rust-owned execution mode、默认执行模式、默认 answer_mode 与 x_runtime.rust metadata guard；`SkillSandboxService` Rust service kernel、`SkillSandboxGrpcService` tonic/prost binding 与 `maf-skill-sandbox` 二进制入口已开始承载 version / compatibility / readiness、client version range 校验、handler allowlist policy、loopback-only serve config、sandbox root 配置、相对 argv 执行、timeout、stdin 上限、stdout/stderr 并发有界 drain、`env_clear` 最小环境、Unix process-group cleanup、lingering descendant stdio bounded wait 与 path / symlink escape fail-closed；Python `SkillSandboxGrpcClient` 已可连接外部 Rust sandbox binary 并调用 `ValidatePolicy` / `ExecuteSandboxed` RPC，且已按 Rust contract 校验 client version range 并拒绝缺失/短头/截断/多余 h2c gRPC payload；`SkillPlatformHandlerRegistry` 在 `shadow` 下记录安全字段 / fingerprint / duration 组成的 Rust policy diff、在 `enforce` 下要求 Rust policy client 并 fail-closed 禁止 Python trust gate 放行，`SkillScriptRunner` enforce 下要求 Rust sandbox client 并 fail-closed 禁止 Python subprocess legacy fallback；Rust policy JSON bridge 与 Python 预构建 PyO3 module facade / contract gate 已落地并由 `MAF_SKILL_POLICY_PYO3_MODULE` 优先接入；`maf_skill_runtime_pyo3` PyO3 crate、`maturin` wheel build 与本地 import smoke、Ubuntu 22.04 x86_64 / Python 3.13 `manylinux_2_35` wheel CI 目标已落地；Skill Runtime artifact provenance / benchmark / shadow→enforce promotion / ops readiness / legacy decommission policy 已由 Rust contract 导出，并提供 Python fail-closed validator 作为进入 enforce 与下线 legacy 的证据门禁；Skill Sandbox binary 的 CI SBOM / provenance / manifest 上传口径、PRD04 evidence ledger / fail-closed 校验脚本与 `MAF_SKILL_SANDBOX_ARTIFACT_MANIFEST_PATH` / `MAF_SKILL_SANDBOX_ARTIFACT_ALLOWLIST_PATH` enforce artifact allowlist 门禁已接入；真实部署 release allowlist promotion、真实 benchmark / ops drill 证据、跨平台/容器级进程树清理强化、coverage/fuzz 长跑与 legacy 下线仍待完成）
- **日期**：2026-05-14
- **来源基线**：`docs/prd/backend/16-Rust化Runtime模块评估PRD.md` RUST-P0-005、RUST-P0-006、RUST-P1-004、8.2.3、9.4、9.7；`.codex/skills/breeding-skill-builder/references/Skill构建指南.md`
- **影响范围**：`src/integrations/agent_skills/`、`src/capabilities/skill_tool/`、Skill manifest、platform-service handler、Skill-owned `native/`

## 1. 问题陈述

Skill 已是一等 capability 来源。随着项目级 Skill 增多，manifest parsing、bundle fingerprint、public root guard、service binding、handler allowlist、script sandbox policy 和 Skill-owned Rust adapter contract 必须从散落 Python 规则演进为更强类型、更可审计的 runtime kernel。

## 2. 核心原则

**Skill 来兼容框架，不是框架兼容 Skill。**

框架只接受规范化 Skill contract：`skill.*` capability、manifest 声明、runtime allowlist、platform-service handler、artifact/event/audit contract。任何 Skill-owned Rust runtime 都必须在这些边界内运行，不能要求框架新增专属 route、executor、capability kind、前端协议或 secret 注入。

## 3. 目标

1. 用 Rust policy kernel 固化通用 Skill manifest parser、execution config、bundle fingerprint、trust gate、public root guard、service binding、handler allowlist 与 `x_runtime.rust` contract 校验。
2. 明确 Skill-owned Rust runtime 的三种可接受 adapter：PyO3 wheel、native binary、sidecar service。
3. 通过 Rust Skill Sandbox sidecar / isolated process manager 承接不可信或进程型执行边界，保证普通用户级 Skill 无法要求 runtime 编译 Rust、下载依赖、执行任意 native binary 或启动服务。
4. 保证 platform-service Skill 的 service binding 继续满足 manifest 声明 + runtime allowlist 双重授权。

## 4. 非目标

1. 不为任何具体业务 Skill 写专属 Rust runtime。
2. 不把 Skill-owned domain logic 放进主体框架 `native/`。
3. 不把 Rust 定义为第四种 `execution.mode`。
4. 不允许 `python_subprocess` 绑定受控 DB、内部 LLM、secret 或完整环境变量。
5. 不把不可信脚本执行、native binary adapter 或进程资源限制长期留在 Python subprocess runner 内。

## 5. 最终架构冻结

通用 Skill Runtime 最终方案冻结为 **Rust policy kernel + Rust sandbox sidecar 双层架构**。

```text
Python SkillExecutor / Skill facade
  ├─ PyO3: maf_skill_runtime policy kernel
  │   ├─ manifest parsing
  │   ├─ bundle fingerprint
  │   ├─ public root guard
  │   ├─ service binding / handler allowlist
  │   └─ x_runtime.rust contract validation
  └─ gRPC client: Rust Skill Sandbox sidecar / isolated process manager
      ├─ python_subprocess execution boundary
      ├─ native binary adapter execution boundary
      ├─ timeout / resource / stdout-stderr limits
      ├─ output file and artifact handoff policy
      └─ execution audit and stable error mapping
```

`maf_skill_runtime` 是通用 Skill Runtime 的主体 crate / package 名；它可以同时提供 PyO3 policy kernel 与 sandbox sidecar binary target。若后续确需拆出独立 crate，必须先更新本 PRD 与 workspace 命名决策。

生产级 Python ↔ Skill Sandbox sidecar 协议沿用全局 sidecar 冻结决策：gRPC / tonic + protobuf。HTTP JSON 仅允许本地开发或极早期 spike，不得作为生产协议。

`platform_service` trusted handler 仍由 Python runtime 装配和调用，但调用前必须经过 Rust policy kernel 的 allowlist / trust gate 校验。Skill-owned Rust runtime 仍只允许 Skill 自己在 `skill/<skill-name>/native/` 内按指南提供 PyO3 wheel / native binary / sidecar adapter；框架不为具体 Skill 做专属适配。

## 6. 功能需求

### 6.1 通用 Skill Runtime Rust policy kernel

- RUST-SKILL-FR-001：manifest parser 必须确定性解析 `name`、`description`、`capability_id`、`execution`、`inputs`、`outputs`、`parameters`、`x_runtime`。
- RUST-SKILL-FR-002：bundle fingerprint 必须覆盖 `SKILL.md`、声明脚本、handler metadata 与受控 native artifact metadata。
- RUST-SKILL-FR-003：public root guard 必须阻止绝对路径、`..`、symlink escape 与未授权 handler module。
- RUST-SKILL-FR-004：service binding 必须执行 manifest 声明 + runtime allowlist 双重授权，缺失或越权 fail closed。
- RUST-SKILL-FR-005：script sandbox policy 必须限制 runtime、cwd、timeout、stdout/stderr、output file、环境变量与 stdin。
- RUST-SKILL-FR-006：所有 Skill execution result 必须归一化为标准 output / artifact / event / audit contract。
- RUST-SKILL-FR-007：通用 Skill Runtime policy kernel 必须通过 PyO3 extension 暴露给 Python facade；不得把纯 policy 校验做成网络 sidecar 主路径。
- RUST-SKILL-FR-008：`platform_service` trusted handler 调用前必须经过 Rust policy kernel 的 allowlist / trust gate 校验。
- RUST-SKILL-FR-009：manifest validation result、bundle fingerprint result、service binding decision、Skill execution result、artifact/event/audit handoff 必须按 Skill contract schema 校验；校验失败必须 fail closed，不得被修正为成功。
- RUST-SKILL-FR-010：Skill policy PyO3 facade 必须校验 contract_version、schema_hash、error_code_table_hash、supported_features 与 `x_runtime.rust` contract_version；不兼容时在 `enforce` 下 fail closed。

### 6.2 Skill Sandbox sidecar / isolated process manager

- RUST-SANDBOX-FR-001：所有不可信或进程型执行边界，包括 `python_subprocess`、native binary adapter、外部脚本执行、资源限制、timeout、stdout/stderr 限额和执行审计，最终必须进入 Rust Skill Sandbox sidecar / isolated process manager。
- RUST-SANDBOX-FR-002：Skill Sandbox sidecar 必须通过 gRPC / tonic + protobuf 接受 Python runtime 调用，提供 health、readiness、liveness、version、shutdown drain、structured logs 与 metrics。
- RUST-SANDBOX-FR-003：sandbox 必须限制 cwd、env、stdin/stdout/stderr、output file、timeout、进程树清理、退出码映射和 audit payload。
- RUST-SANDBOX-FR-004：sandbox health 失败、protocol version 不兼容或执行 policy 校验失败时，Python runtime 必须 fail closed；只有显式 feature flag 允许时才可回退旧 Python subprocess path。
- RUST-SANDBOX-FR-005：生产环境 Skill Sandbox sidecar 生命周期必须由外部进程管理器 / 容器编排管理；Python runtime 不得在生产请求路径中 spawn / restart / kill sandbox sidecar。
- RUST-SANDBOX-FR-006：Skill policy / sandbox `enforce` failure 默认 fail closed；service binding、allowlist、trust gate、script/native execution、sandbox policy 失败不得 fallback 到更宽松路径。
- RUST-SANDBOX-FR-007：`maf_skill_runtime` 属于安全敏感 crate，line coverage 必须不低于 90%；Skill manifest、allowlist、sandbox policy 必须启用 `cargo-fuzz`。
- RUST-SANDBOX-FR-008：Skill Runtime typed error 必须使用 `skill_runtime_` 前缀；service binding、allowlist、trust gate、script/native execution、sandbox policy error 不得自动修正或重试到更宽松路径。
- RUST-SANDBOX-FR-009：Skill Sandbox sidecar response、stdout/stderr summary、artifact handoff metadata、structured audit / metrics / retry event 必须按 protobuf / contract schema 校验；校验失败默认 fail closed。
- RUST-SANDBOX-FR-010：Skill sandbox 自动重试只允许在进程尚未开始执行或请求具备明确 idempotency key 且 retry 不会重复副作用时发生；执行结果 validation、policy、allowlist、sandbox、secret/redaction 错误不得自动重试到更宽松路径。
- RUST-SANDBOX-FR-011：Skill Sandbox sidecar 必须提供 compatibility handshake；Python client 必须校验 component、protocol_version、schema_hash、error_code_table_hash、build_version、supported_features 与 client version range。
- RUST-SANDBOX-FR-012：Skill Sandbox sidecar 不得被前端、用户、普通 Skill 或外部系统直连；只允许 Python runtime / 受控内部组件通过内部通道访问。
- RUST-SANDBOX-FR-013：Skill Sandbox sidecar endpoint 必须来自部署配置 / runtime allowlist；Skill manifest、用户输入、LLM 输出或外部 tool output 不得指定任意 sandbox sidecar 地址。
- RUST-SANDBOX-FR-014：Skill Sandbox sidecar 必须执行本文档冻结的并发、per-skill 并发、queue、queue wait、timeout、stdout/stderr、structured result、artifact、cancel grace 与 retry 限制；禁止无界脚本执行和无界输出。
- RUST-SANDBOX-FR-015：Skill Sandbox sidecar identity、mTLS key / cert、sandbox root、artifact handoff root、service allowlist 与 secret policy 必须来自部署配置 / secret manager / runtime allowlist，不得来自 Skill manifest、用户输入、LLM 输出或外部 tool output。
- RUST-SANDBOX-FR-016：Skill Sandbox 不得向普通 Skill、脚本或 native adapter 注入未授权 secret；`enforce` 下 identity mismatch、secret policy 失败、allowlist secret 缺失或 secret 泄露风险必须 fail closed。

### 6.3 Skill-owned Rust runtime 接入

- RUST-SKILL-FR-020：Skill-owned Rust source 只允许位于 `skill/<skill-name>/native/`。
- RUST-SKILL-FR-021：`x_runtime.rust` 只能作为 metadata；不能触发自动编译、自动执行或自动加载动态库。
- RUST-SKILL-FR-022：PyO3 / native binary / sidecar adapter 必须共享同一 Rust core crate 与 contract tests。
- RUST-SKILL-FR-023：adapter、contract_version、artifact path、sidecar service 必须被 runtime allowlist 显式支持。
- RUST-SKILL-FR-024：Skill 请求执行路径不得运行 `cargo build`、`cargo run`、`rustc`，不得下载 crates，不得动态链接任意本地路径。
- RUST-SKILL-FR-025：移除 Skill bundle 后，主体框架不得残留该 Skill 的 module、binary、sidecar config、进程、端口或 capability 注册。
- RUST-SKILL-FR-026：Skill-owned Rust sidecar adapter 如被允许，只能通过 framework runtime allowlist 和内部网络接入；不得要求公网暴露、前端直连或为具体 Skill 增加专属 route。
- RUST-SKILL-FR-027：`x_runtime.rust` 不得声明 secret value、sidecar endpoint、mTLS key、外部下载 URL 或任意本地路径；只能声明 adapter metadata / contract_version / package 名等非敏感 metadata。
- RUST-SKILL-FR-028：Skill policy PyO3 wheel、Skill Sandbox sidecar binary / image 与 Skill-owned Rust artifact 必须由 CI / 部署流水线预构建，具备 checksum、SBOM、Cargo.lock digest、contract_version、bundle revision 与 provenance；runtime 只能加载 allowlist 产物。
- RUST-SKILL-FR-029：Skill Runtime / Sandbox 必须建立 manifest parsing、fingerprint、allowlist decision、sandbox execution、stdout/stderr handling、artifact handoff 的 Python baseline 与 Rust benchmark；P95/P99、queue wait、CPU、memory 与 process cleanup 成本必须纳入 SLO。
- RUST-SKILL-FR-030：Skill bundle activation registry、allowlist snapshot、artifact metadata 与 Skill-owned sidecar registry 如由 Rust 维护，必须具备 migration lock、backup、restore 与 rollback / roll-forward runbook。
- RUST-SKILL-FR-031：Rust Skill Runtime canonical 稳定后，旧 Python subprocess sandbox policy / trust gate 重复语义必须下线；最终生产只保留 Python facade / platform handler adapter。
- RUST-SKILL-FR-032：Skill Runtime / Sandbox 进入 `enforce` 前必须具备 dashboard、alert、SLO、drain / restart / rollback、process cleanup、artifact quarantine 与 secret / identity failure 演练证据。

## 7. Manifest 约定

Rust metadata 示例必须与 `.codex/skills/breeding-skill-builder/references/Skill构建指南.md` 保持一致：

```yaml
x_runtime:
  rust:
    adapter: pyo3        # pyo3 | binary | sidecar
    core_crate: example_skill_core
    package: example_skill_pyo3
    contract_version: 1
execution:
  mode: platform_service
  trust_scope: project
  handler: skill.example.platform_handler
  handler_module: runtime/example/platform_handler.py
  handler_factory: build_handler
```


Skill Sandbox sidecar protobuf schema 必须归属 `native/proto/maf/skill/v1/`，并复用 `native/proto/maf/common/v1/`；breaking change 必须新建 `maf.skill.v2`。

Protocol compatibility / rolling upgrade 策略冻结：Skill policy kernel 的 contract major version、Skill Sandbox sidecar 的 `maf.skill.v1` / `v2` proto version、以及 Skill bundle `x_runtime.rust.contract_version` 必须分别校验。`shadow` 阶段不兼容可回退 Python legacy path 并写 audit；`enforce` 阶段不兼容 fail closed。Skill-owned Rust adapter 的 breaking change 必须由 Skill 自身升级 contract_version，框架不为具体 Skill 编写兼容特判。

Runtime config 必须遵守统一命名：`MAF_RUST_SKILL_RUNTIME_MODE`=off|shadow|enforce；默认 `off`，生产 `enforce` 前必须经过 `shadow`。

Shadow compare 差异处理策略冻结：`shadow` 模式下，Python legacy path 永远是用户可见结果来源；Rust kernel / sidecar 结果只用于旁路对比。差异必须写入 structured audit / metrics，至少包含 component、input fingerprint、legacy output fingerprint、rust output fingerprint、error code、duration；不得记录完整 prompt、完整 rows、secret、真实文件路径或敏感 payload。shadow 差异不得影响用户结果；只有差异率、错误率、性能指标达到对应专题 PRD 的 promotion threshold 后，才能进入 `enforce`。进入 `enforce` 前还必须满足全局最低 promotion threshold；本专题可更严格，不得更宽松。

Enforce 失败处理策略冻结：`enforce` 模式下 Rust kernel / sidecar 失败默认 fail closed；只有对应 PRD 显式声明可 fallback，且 fallback 不会放宽安全、权限、数据一致性、路径、secret、外部输入校验或审计约束时，才允许回退 Python legacy path。fallback 事件必须写 structured audit。

Structured output validation 策略冻结：Skill Runtime 结构化输出必须先经过 Rust policy kernel / sidecar contract 校验，再进入 Python SkillExecutor、artifact store、event stream 或 audit sink。校验失败时必须返回 `skill_runtime_` 前缀 typed error；只有 transport 层 transient 且未启动 Skill 进程，或 Skill adapter 明确声明幂等并提供 idempotency key 时，才允许自动重试。

### 7.5 最终交付门禁

1. 供应链：Skill policy wheel、Sandbox sidecar、Skill-owned wheel / binary / image 必须有 checksum、SBOM、Cargo.lock digest、bundle revision、contract_version 与 provenance；`x_runtime.rust` 不能绕过 runtime allowlist。
2. 性能：manifest parse、bundle fingerprint、allowlist、sandbox launch、stdout/stderr capture、artifact handoff 与 cancel cleanup 必须有 Python baseline、Rust baseline 和 P50/P95/P99 / CPU / memory 证据。
3. 状态 / 容灾：Skill bundle registry、allowlist snapshot、sidecar registry 或 artifact handoff metadata 若由 Rust 持久化，必须有 backup、restore、migration lock 与 rollback / roll-forward runbook。
4. Python legacy 下线：Rust Skill Runtime canonical 稳定后，Python 只保留 facade / platform handler adapter；旧 Python sandbox policy 或 trust gate 不得继续作为隐式 fallback。
5. 运维：Sandbox unavailable、queue full、timeout、process tree cleanup failure、artifact handoff failure、secret / identity mismatch、public endpoint denial 必须有告警和演练。


当前 repo-local 收口补充：

- `.github/workflows/rust-quality.yml` 已新增 `maf-skill-sandbox` Linux x86_64 release binary 构建，并通过 `scripts/rust_artifact_provenance.py` 生成 / 上传 SBOM、provenance 与 manifest。
- `docs/prd/rust/evidence/prd04/skill_runtime_release_gates.json` 与 `scripts/validate_prd04_skill_runtime_evidence.py` 形成 PRD04 evidence ledger；CI 可用 `--allow-pending` 确认缺口显式存在，严格模式在真实 allowlist、benchmark、shadow promotion、ops drill 与 decommission evidence 缺失时保持 fail-closed。
- `build_api_runtime()` 在配置 `MAF_SKILL_SANDBOX_ENDPOINT` 且 `MAF_RUST_SKILL_RUNTIME_MODE=enforce` 时要求 `MAF_SKILL_SANDBOX_ARTIFACT_MANIFEST_PATH` 与 `MAF_SKILL_SANDBOX_ARTIFACT_ALLOWLIST_PATH` 同时存在，并要求 manifest 精确出现在 allowlist 后才会构造 `SkillSandboxGrpcClient`。

## 8. Sidecar 进程管理冻结

生产环境由外部进程管理器 / 容器编排管理 Rust Skill Sandbox sidecar / isolated process manager。Python runtime 只负责 sandbox client connect、health/readiness/version check、protocol compatibility check、shutdown drain 协调与 fail-closed / fallback。

本地开发 / 测试环境可以提供 Python launcher、脚本或 fixture 一键拉起 sandbox sidecar；该 launcher 不作为生产运行方式。

Sidecar network exposure 策略冻结：Skill Sandbox sidecar 和被 runtime allowlist 接纳的 Skill-owned sidecar adapter 均不得被普通 Skill、用户、前端或外部系统直连。生产访问必须经 Python runtime / 受控内部组件，使用 Unix domain socket、loopback、同 Pod / 内部网络、私有服务发现或 mTLS 内网。`enforce` 下发现公网绑定、未授权 client、manifest 指定任意 endpoint 或未配置 mTLS 的跨主机访问时，必须 fail closed。

Resource limit / backpressure 策略冻结：

| 项 | 冻结值 |
|---|---|
| max concurrent executions | `min(8, cpu)`，默认 4 |
| per-skill concurrent | 2 |
| queue size | 64 |
| queue 等待上限 | 10s |
| 默认执行 timeout | 60s |
| hard timeout | 300s |
| stdin | 1MB |
| stdout / stderr | 各 1MB，并由 Rust sandbox 并发 drain 后只保留前缀 |
| 单次 structured result | 4MB |
| 输出 artifact 默认上限 | 32MB，超出走 artifact policy |
| cancel grace | 5s 后强杀进程树 |
| shutdown drain | 30s |
| retry | 只有进程未启动或明确幂等时允许 |

queue full、per-skill concurrency exceeded、execution timeout、hard timeout、stdin too large、stdout/stderr too large、structured result too large、cancel grace exceeded 必须返回 `skill_runtime_` 前缀 typed error，并写 structured audit / metrics。

Config / secrets / identity 策略冻结：Skill Runtime / Sandbox sidecar 配置只允许来自部署配置、secret manager、只读配置或 runtime allowlist。Skill manifest 只能引用非敏感 metadata，不能携带 secret value、endpoint、socket path、mTLS key、证书私钥或外部下载 URL。platform-service handler 需要 secret 时，必须通过 runtime service registry / allowlist 提供受控服务，不得把 secret 注入普通脚本、stdout/stderr、artifact 或 audit payload。secret rotation 通过受控 reload 或滚动重启完成，并必须重新校验 trust gate / allowlist / service binding。

## 9. 测试策略

| 层级 | 测试 |
|---|---|
| Rust unit | manifest parser、path guard、fingerprint、service allowlist、sandbox policy |
| Rust property | path escape、symlink、metadata mutation、allowlist denial |
| Rust coverage | `cargo-llvm-cov` line coverage ≥90% |
| Rust fuzz | Skill manifest、allowlist、sandbox policy bounded smoke + nightly / release 长跑 |
| Supply chain | Skill policy wheel / sandbox sidecar / Skill-owned artifact checksum、SBOM、provenance、allowlist denial |
| Performance | manifest / fingerprint / allowlist / sandbox / artifact handoff Python baseline vs Rust P50/P95/P99、CPU、memory |
| Migration / DR | Skill registry / allowlist snapshot / sidecar registry backup、restore、migration lock（如由 Rust 持久化） |
| Ops | dashboard / alert smoke、timeout / queue / cleanup / rollback / artifact quarantine drill |
| Decommission | Python sandbox policy / trust gate duplicate semantics removal guard |
| Python integration | `tests/integrations/agent_skills`、`tests/capabilities/skill_tool`、sandbox sidecar client tests、dev launcher tests、endpoint allowlist tests |
| Skill adapter contract | PyO3 / binary / sidecar golden tests（存在时）、structured result schema validation、contract version compatibility matrix |
| Security | runtime cargo build/download/native arbitrary execution denial tests、sandbox resource/timeout/stdout-stderr limit tests、public bind / manifest endpoint denial tests、queue/concurrency/cancel denial tests、secret injection / identity mismatch denial tests |

## 10. 验收标准

| 编号 | 验收项 | 证明方式 |
|---|---|---|
| RUST-SKILL-AC-001 | 通用 Skill Runtime 保持 service binding 双重授权 | integration tests |
| RUST-SKILL-AC-002 | 普通 Skill 无法执行任意 native artifact | denial tests |
| RUST-SKILL-AC-003 | Skill-owned Rust adapter 不改变标准输出契约 | Skill adapter contract tests |
| RUST-SKILL-AC-004 | 框架没有为具体 Skill 增加专属分支 | grep / architecture guard |
| RUST-SKILL-AC-005 | 不可信或进程型 Skill 执行边界进入 Rust Skill Sandbox sidecar | sidecar integration tests / security tests |
| RUST-SKILL-AC-006 | Skill Runtime coverage / fuzz 达到安全敏感门禁 | `cargo-llvm-cov` report + fuzz logs |
| RUST-SKILL-AC-007 | Skill Runtime 结构化输出校验失败 fail closed；可重试场景不重复副作用 | schema validation + retry/fault injection |
| RUST-SKILL-AC-008 | Skill policy / sandbox / Skill-owned Rust adapter contract version 不兼容 fail closed，兼容版本可滚动升级 | compatibility matrix + audit evidence |
| RUST-SKILL-AC-009 | Skill Sandbox / Skill-owned sidecar adapter 仅内部可访问，manifest 任意 endpoint 与公网绑定被拒绝 | endpoint allowlist + security/failure injection |
| RUST-SKILL-AC-010 | Skill Sandbox 并发、per-skill 队列、timeout、stdout/stderr、result size、cancel grace 限制生效 | resource/backpressure tests + process cleanup evidence |
| RUST-SKILL-AC-011 | Skill Runtime / Sandbox config、secret、identity 只来自允许来源；manifest secret/endpoint 被拒绝 | parser denial tests + redaction snapshot + identity failure injection |
| RUST-SKILL-AC-012 | Skill policy wheel、Sandbox sidecar、Skill-owned artifact checksum、SBOM、bundle revision、contract_version、provenance 与 allowlist 校验可验证 | release artifact review + parser/load failure injection |
| RUST-SKILL-AC-013 | Skill Runtime benchmark 覆盖 manifest、fingerprint、allowlist、sandbox execution、artifact handoff 与 process cleanup | benchmark report + SLO gate |
| RUST-SKILL-AC-014 | Rust-owned Skill registry / allowlist / sidecar registry 状态有 backup、restore、migration lock 与 rollback / roll-forward 证据 | migration / restore tests（存在持久状态时） |
| RUST-SKILL-AC-015 | Rust Skill Runtime canonical 稳定后旧 Python sandbox policy / trust gate 重复语义下线 | decommission PR + architecture guard |
| RUST-SKILL-AC-016 | `enforce` 前具备 dashboard、alert、runbook 与 sandbox unavailable / timeout / cleanup / secret / identity 演练证据 | ops checklist + drill records |

## 11. 风险

| 风险 | 缓解 |
|---|---|
| Skill 作者误以为 Rust 是 execution mode | 指南与 parser 双重拒绝 `runtime: rust` |
| native binary 逃逸 sandbox | 固定 build artifact + allowlist + Rust sandbox sidecar + timeout + audit |
| Skill-owned Rust 回流主体框架 | ownership guard，主体只依赖 generic contract |
| Skill Rust 产物供应链不可追溯 | wheel / binary / image 必须有 checksum、SBOM、bundle revision、contract_version、provenance 与 allowlist 校验 |
| 旧 Python sandbox policy 残留 | Rust canonical 稳定后删除重复 trust gate / sandbox policy，Python 仅保留 facade / adapter |
