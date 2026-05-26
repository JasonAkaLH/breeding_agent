# Test Spec — Phase 0 PostgreSQL Driver 与 State Platform Contract

- **日期**：2026-05-26
- **状态**：待实施
- **关联 PRD**：`01-Phase0-Driver与StatePlatformContractPRD.md`

## 1. Test Goals

1. 证明 driver ADR 包含实现前必须知道的行为证据。
2. 证明 State Platform contract 已能表达读、写、queue、handler、health/readiness。
3. 证明 error policy 对 retryable / non-retryable 错误分类稳定。
4. 证明 Phase 0 不接入 runtime、不连接远端库、不修改 SQLite 数据。

## 2. Target Tests

| Test file | Coverage |
| --- | --- |
| `tests/storage/test_state_platform_contract.py` | Contract import、method shape、command DTO、health/readiness DTO。 |
| `tests/storage/test_state_platform_error_policy.py` | SQLSTATE classification、unknown fail-closed、driver exception extractor seam。 |
| ADR review check | Driver license、Python 3.13、SQLAlchemy 2.x async、timeout/cancel、SQLSTATE、pool behavior。 |

## 3. Required Cases

| Case | Expected |
| --- | --- |
| contract exposes read/write split | `StateService` 明确区分 query、submit_command、execute_command_and_wait、command_group、health/readiness。 |
| command dto durable fields | DTO 包含 idempotency key、payload fingerprint、partition key、partition sequence、status、attempt、lease、timestamps。 |
| retryable sqlstate | `40P01`、`40001`、`55P03`、`57014` 分类为 retryable/transient。 |
| unknown error fail closed | 未知 driver / business error 默认 non-retry。 |
| redaction fields | DTO repr / public dump 不包含 DSN、token、secret、raw payload。 |
| no runtime integration | Phase 0 diff 不修改 `src/api/runtime.py` production assembly。 |

## 4. Verification Commands

```bash
conda run -n multi_agent python -m unittest tests.storage.test_state_platform_contract tests.storage.test_state_platform_error_policy
git diff --check
```

## 5. Exit Rule

若 driver ADR 缺少 SQLSTATE、timeout、cancel、license 或 Python 3.13 兼容证据，不得进入 Phase 1。
