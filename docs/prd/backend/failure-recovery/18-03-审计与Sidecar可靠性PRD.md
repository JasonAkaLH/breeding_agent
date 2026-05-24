# 18-03 审计与 RuntimeSidecar 可靠性 PRD

- **项目**：breeding_agent
- **范围**：audit sink 隔离、RuntimeSidecar enforce bounded retry、shadow/enforce 边界
- **文档状态**：分步 PRD（继承总纲 PRD 18）
- **日期**：2026-05-24
- **上游总纲**：`docs/prd/backend/18-失败自检恢复与Fallback控制层PRD.md`

## 1. 背景

现有 shadow / safety 路径已采用“旁路失败不影响用户结果”的口径，但事件发布中的 audit sink 写入仍可能阻断 SSE 投递。RuntimeSidecar contract 已有 retry policy 和 retry plan helper，但 enforce 写路径尚未统一接入 bounded retry。

## 2. 目标

- audit sink failure 不得阻断 SSE 投递。
- audit sink failure 必须有降噪诊断和 recovered 记录。
- RuntimeSidecar enforce transient 必须按 contract bounded retry。
- sidecar retry 必须满足幂等键、same sidecar、typed retriable error 和 max attempts 条件。
- shadow 模式继续不影响用户结果，enforce 模式最终失败继续 fail closed。

## 3. 非目标

- 不吞掉事件持久化失败。
- 不在 shadow 模式新增 retry 影响主路径延迟。
- 不把 sidecar enforce 失败回退 Python legacy。
- 不对缺幂等键或不可重试 typed error 做 retry。

## 4. Audit sink 隔离需求

| ID | 需求 | 默认行为 |
|---|---|---|
| FR-03-1 | 事件发布时 audit sink 写入必须 best-effort | 启用 |
| FR-03-2 | audit sink 抛异常后仍必须投递 SSE queue | 启用 |
| FR-03-3 | audit sink 连续失败必须降噪记录 warning | 启用 |
| FR-03-4 | audit sink 恢复后必须记录 recovered | 启用 |
| FR-03-5 | 事件持久化失败仍必须失败，不得吞掉 | 启用 |

诊断字段：最近错误类型、最近失败时间、连续失败次数、总失败次数。

## 5. Sidecar bounded retry 需求

| ID | 需求 | 默认行为 |
|---|---|---|
| FR-03-6 | enforce 写路径必须使用统一 sidecar retry helper | 仅 enforce |
| FR-03-7 | retry 必须满足 contract retriable error | 启用 |
| FR-03-8 | retry 必须具备 idempotency key | 启用 |
| FR-03-9 | retry 必须同一 sidecar | 启用 |
| FR-03-10 | retry exhausted 后 fail closed | 启用 |

适合 retry 的操作：append event、save task、save node、save artifact、cancellation token，前提是具备稳定幂等键和 contract 允许。

不可 retry：schema / contract mismatch、permission denied、artifact allowlist mismatch、enforce artifact 不可信、缺幂等键、可能产生重复外部副作用的操作。

## 6. 审计脱敏契约

sidecar retry 审计字段：

- component
- operation
- attempt
- max attempts
- error code
- backoff ms
- idempotency key fingerprint
- same sidecar
- final status
- duration ms

不得记录：原始 payload、secret、token、原始 idempotency key、文件路径、数据库连接信息。

## 7. 与节点 retry 的边界

sidecar enforce 写失败属于状态写路径可靠性问题。sidecar helper 最终失败后不得交给节点 retry 重复执行能力，避免重复调用外部服务或掩盖状态一致性问题。

## 8. Shadow / enforce 语义

| 模式 | 行为 |
|---|---|
| shadow | Python 结果用户可见；sidecar 调用失败只写 shadow diff / audit；audit 失败也不影响用户结果 |
| enforce | sidecar 是主路径；符合 bounded retry 条件时 retry；耗尽或不可重试时 fail closed；不回退 Python |

## 9. 验收标准

- audit sink 失败不阻断 SSE。
- audit sink 连续失败降噪。
- audit sink 恢复有 recovered 记录。
- 事件持久化失败仍失败。
- sidecar transient retry 成功。
- sidecar retry 耗尽 fail closed。
- sidecar 不可重试错误不 retry。
- 缺 idempotency key 不 retry。
- same sidecar 条件不满足不 retry。
- sidecar retry 审计不含原始 payload 或 secret。
- shadow sidecar 失败不影响用户结果。
- enforce 失败不回退 Python。

## 10. 测试要求

- `tests/api` 或 `tests/observability` 覆盖 audit sink failure 不阻断 SSE。
- `tests/storage` 覆盖 sidecar retry plan 接入、耗尽、不可重试、缺幂等键。
- `tests/integrations` 覆盖 shadow/enforce 语义不回退。
- 审计 payload 测试必须验证脱敏字段。

## 11. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 审计失败被隐藏 | 降噪 warning、计数器、recovered 记录 |
| sidecar retry 掩盖 enforce 配置问题 | 只对 typed retriable error retry；contract/config mismatch 不 retry |
| 重试导致重复写 | 必须有幂等键且 same sidecar |
