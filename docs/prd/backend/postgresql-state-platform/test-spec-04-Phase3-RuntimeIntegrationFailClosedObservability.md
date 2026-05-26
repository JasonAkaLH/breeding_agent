# Test Spec — Phase 3 Runtime Integration、Fail-closed 与 Observability

- **日期**：2026-05-26
- **状态**：待实施
- **关联 PRD**：`04-Phase3-RuntimeIntegrationFailClosedObservabilityPRD.md`

## 1. Test Goals

证明 State Platform 能以受控方式接入 runtime，并在生产配置缺失或边界冲突时 fail closed，同时具备可观测与运维入口。

## 2. Target Tests

| Test file | Coverage |
| --- | --- |
| `tests/api/test_state_platform_runtime_assembly.py` | Runtime factory、dev/test SQLite、production PostgreSQL assembly。 |
| `tests/api/test_state_platform_production_fail_closed.py` | 缺 DSN、缺 driver、migration not ready、production sqlite reject、sidecar conflict。 |
| `tests/observability/test_state_platform_health.py` | health/readiness fields、redaction、degraded/not-ready conditions。 |

## 3. Required Cases

| Case | Expected |
| --- | --- |
| dev default keeps sqlite | dev/test 未配置 PostgreSQL 时现有测试可继续 SQLite。 |
| production missing dsn fails | production + PostgreSQL backend 无 DSN 时 fail closed。 |
| production sqlite reject | production + SQLite backend 明确拒绝。 |
| migration not ready | readiness=false，不执行隐式 DDL。 |
| sidecar writer conflict | State Platform writer 和 RuntimeSidecar enforce writer 冲突时 fail closed 或 sidecar shadow-only。 |
| readiness fields | DB、migration、backlog、oldest pending、dead-letter、worker heartbeat 均存在。 |
| health redaction | health/audit 不含 DSN、账号、密码、token、raw payload。 |
| runbook review | runbook 覆盖 drain、lease recovery、dead-letter、backup/restore。 |

## 4. Verification Commands

```bash
conda run -n multi_agent python -m unittest tests.api.test_state_platform_runtime_assembly tests.api.test_state_platform_production_fail_closed
conda run -n multi_agent python -m unittest tests.observability.test_state_platform_health
git diff --check
```

## 5. Release Gate

Phase 3 完成只表示生产 State Platform runtime seam 可部署；不表示历史 SQLite 数据已经迁移，也不表示可以 cutover。进入真实生产前必须完成 Phase 4。
