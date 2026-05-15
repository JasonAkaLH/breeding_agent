# Core 与 Lifecycle Rust Kernel PRD

- **状态**：部分落地（`maf_core_types` / `maf_lifecycle` contract artifact、Python facade artifact 校验与 lifecycle transition artifact 驱动已落地；PyO3 extension、enforce、benchmark、ops 与 legacy 下线仍待完成）
- **日期**：2026-05-14
- **来源基线**：`docs/prd/backend/16-Rust化Runtime模块评估PRD.md` RUST-P0-001、RUST-P0-002、9.1、9.2
- **影响范围**：`src/core/`、`src/lifecycle/`、Python facade、跨模块 contract tests

## 1. 问题陈述

当前 core contract 与 lifecycle 状态规则由 Python dataclass / enum / service 分散维护。随着 Skill、MCP、dispatcher、storage、event replay 增多，状态与 contract 漂移风险上升。Core 与 Lifecycle 是最适合先 Rust 化的低 I/O、强确定性 kernel。

## 2. 目标

1. 建立 `maf_core_types` 作为唯一 canonical core type/schema 来源。
2. 建立 `maf_lifecycle` 作为唯一 task / node / mailbox / interrupt / cancel transition table 来源。
3. 通过 Python facade / adapter 保持现有 import path、API 与测试行为不变。
4. 用 Rust schema、golden fixtures 与 property tests 固化 enum、默认值、错误码、非法状态转移、取消、interrupt/resume、late result 处理。
5. 采用“生成 contract artifact + 手写薄 facade”的混合策略，避免首版为了完整 Python codegen 过度复杂化。

## 3. 非目标

1. 不迁移 FastAPI route、dependency 或 app composition。
2. 不迁移 LLM planner、prompt builder 或业务策略。
3. 不改变现有 task/node/event/artifact 的外部 JSON contract。
4. 不引入 dispatcher sidecar；dispatcher 属于 `03` 专题。
5. 不迁移 FastAPI DTO / Pydantic DTO；这些 DTO 继续留在 Python，但必须映射到 Rust canonical contract。

## 4. 功能需求

- RUST-CORE-FR-001：`Task`、`TaskNode`、`EventRecord`、`Artifact`、`CapabilityExecutionResult` 等跨模块对象必须可 serde round-trip。
- RUST-CORE-FR-002：`maf_core_types` 必须是 core enums、core structs、JSON schema / serde contract、stable error code 的唯一 canonical source；Python 不得另行定义冲突默认值、枚举含义、字段语义或 error code 语义。
- RUST-CORE-FR-003：Rust error 必须映射为 Python typed exception 和稳定错误码，并遵守全局 typed error schema：`code`、`message`、`retriable`、`category`、`safe_metadata`。
- RUST-CORE-FR-004：Core/Lifecycle Rust kernel 必须通过 PyO3 extension 暴露给 Python facade；sidecar service 与 subprocess native binary 不得作为主路径。
- RUST-CORE-FR-005：Core/Lifecycle `enforce` failure 默认 fail closed；只有 serde/validation pure performance fallback 且 Python legacy 已证明同等 contract 时，才允许显式 fallback。
- RUST-CORE-FR-006：Python `src/core` 只能作为 facade / adapter 保留现有 import path；Python facade 采用手写薄层，但必须通过 Rust 生成 / 导出的 contract artifact 与 golden fixtures 校验。
- RUST-CORE-FR-007：Rust canonical source 必须生成或导出 JSON schema、error code table、enum/value snapshot、golden fixtures；Core / Lifecycle 共用的 artifact 必须可被 CI 校验；error code table 必须使用 `core_` / `lifecycle_` 前缀。
- RUST-CORE-FR-008：Core/Lifecycle PyO3 return object、generated artifact、shadow compare event 与 structured audit event 必须通过 schema / contract artifact 校验；校验失败映射为 typed error，默认不可自动修正或重试。
- RUST-CORE-FR-009：Core/Lifecycle PyO3 facade 在 import / 初始化 / 首次调用前必须校验 component、contract_version、schema_hash、error_code_table_hash、transition_table_hash 与 supported_features；不兼容 contract 在 `enforce` 下必须 fail closed。
- RUST-CORE-FR-010：Core/Lifecycle PyO3 wheel 与 generated contract artifact 必须由 CI / 部署流水线预构建，具备 checksum、SBOM、Cargo.lock digest、toolchain / target / feature / build profile 元数据与 provenance；runtime 只能加载 allowlist 校验通过的 artifact。
- RUST-CORE-FR-011：Core/Lifecycle 必须建立 Python baseline、Rust baseline 与 PyO3 FFI overhead benchmark，覆盖 serde round-trip、schema validation、typed error mapping 与 transition table 判定；性能回归不得进入 `enforce`。
- RUST-LIFE-FR-001：task/node/mailbox/interrupt/cancel 转移必须由 `maf_lifecycle` 统一 transition table 判定。
- RUST-LIFE-FR-002：非法状态转移必须 fail-closed，不得静默忽略。
- RUST-LIFE-FR-003：取消后 late result 不得把 terminal task 改回 completed。
- RUST-LIFE-FR-004：interrupt answer / resume 必须保持同一 task context，不得创建隐式新 task。
- RUST-LIFE-FR-005：Python `src/lifecycle` 只能作为 facade / adapter 保留现有 import path；不得独立定义与 Rust 冲突的状态转移规则、默认值或错误码语义。
- RUST-LIFE-FR-006：`maf_lifecycle` 必须生成或导出 transition table snapshot，并作为 Python facade / golden tests 的校验输入。
- RUST-LIFE-FR-007：lifecycle 非法状态转移、contract mismatch、structured output validation failure 必须 fail closed；不得通过 retry / correction 把非法转移改为成功。
- RUST-LIFE-FR-008：Lifecycle transition table 的 breaking change 必须升级 contract major version，并提供旧新 contract compatibility tests；不得在同一 `enforce` 流量中混用不兼容 transition table。
- RUST-LIFE-FR-009：Rust canonical source 稳定后，重复 Python transition table 必须下线；最终生产不得保留可隐式接管的 Python 状态机语义。
- RUST-LIFE-FR-010：Core/Lifecycle `enforce` 前必须具备 contract probe、panic / crash alert、artifact rollback 与 decommission runbook；无 runbook 或无演练证据不得进入最终生产路径。

## 5. 接入方式与 canonical source 冻结

Core types 与 Lifecycle transition table 的 Rust 接入方式冻结为 **PyO3 extension**；Python 保留 facade 和 feature flag fallback。不得为 core/lifecycle 引入 sidecar service 或 subprocess native binary 作为主路径。

Canonical source 策略冻结：`maf_core_types` 与 `maf_lifecycle` 是唯一 canonical source。Rust 负责 core enums / structs、stable error code、JSON schema / serde contract、task / node / mailbox / interrupt / cancel transition table；Python `src/core` / `src/lifecycle` 只保留兼容 facade / adapter。

选择理由：core/lifecycle 属于高频、纯规则、低 I/O、强确定性 kernel；进程外 sidecar 或 subprocess 会引入不必要的网络 / 进程边界成本。

## 6. Python facade 与生成 artifact 策略

Python facade 生成策略冻结为 **生成 contract artifact + 手写薄 facade**。

1. Rust canonical source 负责生成或导出 JSON schema、error code table、enum/value snapshot、transition table snapshot 与 golden fixtures。
2. Python facade 保持手写薄层，不要求首版生成完整 Python 代码。
3. facade 必须保持现有 import path 尽量稳定。
4. 初期允许 Python facade 调用 Rust kernel 后再映射为现有 dataclass / enum。
5. facade 必须支持 feature flag 回退纯 Python implementation；`off` / `shadow` 阶段允许保留 Python legacy。
6. Python facade 负责适配现有 dataclass / Pydantic / API DTO、把 Rust typed error 映射为现有 Python exception、做最小格式转换。
7. Python facade 禁止承载独立状态机逻辑、独立 enum 语义、独立默认值语义、独立 error code 语义。
8. CI 必须校验 Python facade 与 Rust 生成 artifact 一致，包括 schema、error code、enum/value snapshot、transition table snapshot 与 golden fixtures。
9. FastAPI DTO / Pydantic DTO 继续留在 Python，但必须映射到 Rust canonical contract。
10. panic boundary 必须被测试覆盖。
11. 稳定进入 `enforce` 后，Rust 判定为准；重复 Python transition table 必须删除，只保留 Python facade / adapter。
12. Python facade 必须在加载 Rust artifact 时校验 contract compatibility；校验失败在 `shadow` 下可回退 Python legacy path，在 `enforce` 下 fail closed。
13. PyO3 wheel 与 generated artifact 必须由 CI / 部署流水线预构建并校验 checksum / SBOM / provenance；Python import 路径不得触发编译或下载依赖。
14. Rust canonical 稳定后必须删除重复 Python transition table、enum 语义与 error code 语义；最终生产回滚依赖 artifact / deployment rollback，而不是隐式 Python 状态机 fallback。


Runtime config 必须遵守统一命名：`MAF_RUST_CORE_MODE` / `MAF_RUST_LIFECYCLE_MODE`=off|shadow|enforce；默认 `off`，生产 `enforce` 前必须经过 `shadow`。

Shadow compare 差异处理策略冻结：`shadow` 模式下，Python legacy path 永远是用户可见结果来源；Rust kernel / sidecar 结果只用于旁路对比。差异必须写入 structured audit / metrics，至少包含 component、input fingerprint、legacy output fingerprint、rust output fingerprint、error code、duration；不得记录完整 prompt、完整 rows、secret、真实文件路径或敏感 payload。shadow 差异不得影响用户结果；只有差异率、错误率、性能指标达到对应专题 PRD 的 promotion threshold 后，才能进入 `enforce`。进入 `enforce` 前还必须满足全局最低 promotion threshold；本专题可更严格，不得更宽松。

Enforce 失败处理策略冻结：`enforce` 模式下 Rust kernel / sidecar 失败默认 fail closed；只有对应 PRD 显式声明可 fallback，且 fallback 不会放宽安全、权限、数据一致性、路径、secret、外部输入校验或审计约束时，才允许回退 Python legacy path。fallback 事件必须写 structured audit。Core / Lifecycle 进入 `enforce` 后，Rust schema、error code 与 transition table 判定为准，Python facade 不得覆盖 Rust 判定。

## 7. 测试策略

| 层级 | 测试 |
|---|---|
| Rust unit | enum/struct serde、transition table、typed error mapping、retry=false fail-closed cases |
| Rust property | 任意状态序列不产生非法 terminal 回退；cancel/late result invariants |
| Python golden | 现有 `tests/core`、`tests/lifecycle` 行为锁定 |
| Cross-language | 同一 JSON fixture Rust/Python round-trip 一致；Python facade 不得与 Rust schema / transition table 漂移 |
| Generated artifact | JSON schema、error code table、enum/value snapshot、transition table snapshot、golden fixtures 与 Python facade 一致 |
| Structured output | PyO3 return object、shadow diff event、audit event schema validation；validation failure fail-closed |
| Compatibility | old/new facade 与 old/new contract artifact matrix；contract mismatch fail-closed |
| Supply chain | PyO3 wheel checksum / SBOM / provenance / allowlist load failure injection |
| Benchmark | Python baseline、Rust baseline、PyO3 overhead、transition table P50/P95/P99、CPU、memory |
| Decommission | duplicate Python transition table / enum / error semantics removal guard |
| Ops | contract probe、panic / crash alert、artifact rollback drill |
| Regression | `tests/api`、`tests/e2e` 中状态相关用例保持通过 |

## 8. 验收标准

| 编号 | 验收项 | 证明方式 |
|---|---|---|
| RUST-CORE-AC-001 | core schema 有单一 canonical source | Rust crate + facade 审查 |
| RUST-CORE-AC-002 | Python 旧测试全量通过 | `tests/core`、`tests/lifecycle` |
| RUST-CORE-AC-003 | 状态机 property tests 覆盖 cancel/resume/late result | `cargo test` + property test 输出 |
| RUST-CORE-AC-004 | panic 不穿透 Python | panic boundary test |
| RUST-CORE-AC-005 | `maf_core_types` / `maf_lifecycle` 是唯一 canonical source，Python 只保留 facade | architecture guard + golden fixtures |
| RUST-CORE-AC-006 | `enforce` 后删除重复 Python transition table | grep / code review / regression tests |
| RUST-CORE-AC-007 | Python facade 采用手写薄层，且与 Rust 生成 artifact 一致 | generated artifact diff + CI gate |
| RUST-CORE-AC-008 | Core/Lifecycle 结构化输出与 audit/shadow event 通过 contract 校验，非法输出 fail closed | schema validation + failure injection |
| RUST-CORE-AC-009 | PyO3 facade contract handshake、schema/error/transition hash 校验与不兼容 fail-closed 可验证 | compatibility matrix + import smoke |
| RUST-CORE-AC-010 | PyO3 wheel / generated artifact checksum、SBOM、provenance 与 allowlist 加载校验可验证 | release artifact review + import failure injection |
| RUST-CORE-AC-011 | Core/Lifecycle benchmark 覆盖 Python baseline、Rust baseline 与 PyO3 overhead，性能回归阻断 `enforce` | criterion / Python benchmark report + CI gate |
| RUST-CORE-AC-012 | Rust canonical 稳定后重复 Python transition table / enum / error code 语义下线 | decommission PR + grep / architecture guard |
| RUST-CORE-AC-013 | Core/Lifecycle `enforce` 前具备 contract probe、panic / crash alert 与 artifact rollback 演练 | ops checklist + drill evidence |

## 9. 风险

| 风险 | 缓解 |
|---|---|
| Python/Rust schema 双写漂移 | Rust schema 作为 canonical，Python facade 只做薄转换，并用生成 artifact + golden fixtures 阻断漂移 |
| 一次迁移过大 | 先 core enum/schema，再 lifecycle transition table |
| 错误码影响前端或审计 | 增加 stable error mapping golden tests |
| PyO3 wheel 供应链不可追溯 | CI 预构建并强制 checksum、SBOM、provenance 与 allowlist 校验 |
| Python legacy 状态机残留导致双语义 | Rust canonical 稳定后删除重复 transition table，只保留 facade / adapter |
