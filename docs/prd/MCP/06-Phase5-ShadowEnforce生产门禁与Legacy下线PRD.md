# Phase 5：Shadow / Enforce 生产门禁与 Legacy 下线 PRD

- **范围**：shadow compare / production enforce / SLO / security hardening / ops runbook / rollback drill / Python legacy decommission
- **状态**：待实现
- **日期**：2026-05-14
- **前置依赖**：Phase 4

## 1. 目标

Phase 5 把 MCP Runtime 联合改造从“功能可用”推进到“最终交付级生产能力”：通过 shadow、SLO、安全、运维、恢复、回滚和 legacy 下线门禁后，Rust MCP sidecar 成为 canonical runtime。

## 2. Shadow 要求

1. `MAF_RUST_MCP_RUNTIME_MODE=shadow` 下，Python legacy path 是唯一用户可见结果来源。
2. Rust sidecar 旁路执行或对比时不得造成外部副作用；side-effecting tool 默认不得 shadow 重放真实调用。
3. shadow diff 只记录 fingerprint、duration、error code、status，不记录 raw prompt、raw MCP id、完整参数、完整输出、secret、真实 endpoint。
4. shadow 样本必须覆盖普通短调用、SSE 短调用、多事件 stream、progress、task、cancel、reconnect、error、oversized output。
5. 差异必须可归因：protocol mismatch、sanitizer mismatch、timeout、remote error、redaction mismatch、event mapping mismatch。
6. shadow 使用真实外部 MCP server 时，只有 read-only / idempotent tool 才允许旁路调用；其它工具只能使用 recorded fixture、fake server 或 server 明确提供的 dry-run / sandbox 环境。

## 3. Enforce 晋级门槛

任一生产 MCP tool 进入 Rust sidecar enforce 前，至少满足：

1. 连续 7 天 shadow 无高危差异；
2. 不少于 1000 次有效 shadow 样本，或对应业务低频工具经负责人签字豁免但必须有完整 fault injection；
3. contract mismatch rate = 0；
4. panic / crash = 0；
5. raw secret / raw MCP id / raw endpoint leakage = 0；
6. P95 latency 不高于 Python legacy 110%，或有明确 SLO 豁免；
7. error rate 不高于 legacy；
8. durable registry restart recovery 演练通过；
9. rollback drill 通过；
10. dashboard、alert、runbook、on-call 分级可用。
11. MCP 2025-11-25 conformance matrix 全部通过；任何协议升级必须先更新 fixtures、contract hash、Phase PRD 与 compatibility tests。

## 4. Enforce 行为

1. `MAF_RUST_MCP_RUNTIME_MODE=enforce` 下，Rust sidecar 是 MCP protocol、transport、task registry、sanitizer、bundle activation 的 canonical path。
2. sidecar unavailable、protocol incompatible、schema hash mismatch、identity mismatch、secret missing、registry unavailable、sanitizer failure、bundle activation failure 默认 fail closed。
3. 只有无副作用 read-only health / metrics / snapshot 允许受限降级。
4. 任何 fallback 必须由 PRD 显式允许，且不得放宽安全、权限、数据一致性、schema、sanitizer 或审计。
5. enforce 下所有 typed error 必须稳定映射到 Python `CapabilityExecutionError` 和 API/SSE 可理解事件。
6. enforce 下如果外部 MCP server 协议版本、capability 或 tool-level metadata 与激活时快照不一致，必须重新执行 bundle validation；失败时保留旧 active bundle 或 fail closed。

## 5. 运维与恢复

必须具备：

1. sidecar health / readiness / liveness / version dashboard；
2. MCP external server unavailable alert；
3. stream idle / reconnect exhausted alert；
4. durable registry write failure alert；
5. sanitizer failure / redaction failure alert；
6. bundle quarantine 操作；
7. sidecar drain / restart runbook；
8. registry backup / restore / replay 校验；
9. rollback to previous sidecar artifact / previous MCP bundle；
10. incident 分级与 on-call 操作说明。

## 6. Python legacy 下线

Rust MCP sidecar canonical 稳定后，必须删除或封存重复 Python legacy 语义：

| Python legacy 语义 | 下线要求 |
|---|---|
| JSON-RPC canonical parse / validate | 只保留 facade DTO 校验，不再作为生产 protocol parser |
| SSE parser / router | 不保留重复生产路径 |
| output sanitizer canonical logic | 转为 facade safety check 或删除重复语义 |
| bundle activation canonical logic | 只保留 sidecar client / descriptor sync |
| long-task registry | 只保留 sidecar-backed facade，不保留 in-memory 生产路径 |

## 7. 测试策略

| 层级 | 测试 |
|---|---|
| Shadow | diff classification、no user-visible impact、no side-effect replay |
| Enforce | sidecar canonical path、failure fail closed、typed error mapping |
| Performance | P50/P95/P99、CPU、memory、payload size、stream idle |
| Security | redaction, endpoint, identity, secret, public bind, raw id leakage |
| Recovery | sidecar restart、registry restore、bundle rollback、stream reconnect |
| Ops | dashboard / alert smoke、runbook drill、rollback drill |
| Decommission | architecture guard 防止旧 Python MCP semantic 被重新使用 |

## 8. 验收标准

| 编号 | 验收项 | 证明方式 |
|---|---|---|
| MCP-P5-AC-001 | shadow 样本和差异指标达到晋级门槛 | shadow report |
| MCP-P5-AC-002 | enforce 下 Rust sidecar 是 canonical MCP runtime | architecture review + integration tests |
| MCP-P5-AC-003 | sidecar critical failure fail closed | fault injection |
| MCP-P5-AC-004 | SLO、dashboard、alert、runbook、rollback drill 完成 | ops evidence |
| MCP-P5-AC-005 | Python legacy duplicate semantics 下线 | decommission PR |
| MCP-P5-AC-006 | 用户可见 API/SSE 与历史行为兼容 | e2e regression |
| MCP-P5-AC-007 | MCP 2025-11-25 conformance matrix 作为 enforce 前置门禁 | conformance report |
| MCP-P5-AC-008 | 非幂等 / 有副作用 tool 不被 shadow 重放真实调用 | shadow safety audit |

## 9. 退出门禁

Phase 5 通过后，才可以宣称本项目具备最终交付级 MCP Runtime：Rust sidecar 承载 MCP protocol / transport / long-task durable registry / sanitizer，Python 保留 facade / API / event bridge / capability wrapper。
