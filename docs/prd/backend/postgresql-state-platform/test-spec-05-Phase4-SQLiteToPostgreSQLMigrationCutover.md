# Test Spec — Phase 4 SQLite 到 PostgreSQL Migration、Cutover 与 Rollback

- **日期**：2026-05-26
- **状态**：后续待实施
- **关联 PRD**：`05-Phase4-SQLiteToPostgreSQLMigrationCutoverPRD.md`

## 1. Test Goals

证明 SQLite -> PostgreSQL migration 可 dry-run、可 resume、可校验、可 cutover、可 rollback，并且不泄漏生产 secret。

## 2. Test Categories

| Category | Coverage |
| --- | --- |
| Migration unit | row mapping、type conversion、idempotency、checkpoint。 |
| Migration integration | SQLite fixture -> PostgreSQL test schema import。 |
| Validation | row count、checksum、referential integrity、sampled semantic reads。 |
| Shadow compare | SQLite legacy reads vs PostgreSQL State Platform reads。 |
| Cutover smoke | API submit/list/task/SSE/auth/upload/artifact read smoke。 |
| Rollback drill | revert config, restore snapshot, verify no dual-primary writes。 |
| Secret scan | DSN/token/password 不进入 tracked files / reports。 |

## 3. Required Cases

| Case | Expected |
| --- | --- |
| dry-run no writes | dry-run 不修改目标 schema，输出脱敏计划。 |
| import preserves counts | 核心表 row count 与 source 一致或差异有解释。 |
| checksum match | 关键业务字段 checksum match。 |
| resume after interruption | 中断后恢复不重复写、不破坏 idempotency。 |
| invalid source row | 脏数据进入 remediation report，不静默丢弃。 |
| shadow read compare | sampled conversation/task/message/event reads 一致。 |
| cutover smoke | production backend 切 PostgreSQL 后 API/SSE smoke 通过。 |
| rollback restores service | rollback 后服务恢复到 cutover 前快照语义。 |
| no tracked secret | git tracked 文件不含生产 DSN、账号、密码、token。 |

## 4. Verification Commands

具体命令需在 Phase 4 实现 migration scripts 后补入，但必须包含：

```bash
conda run -n multi_agent python -m unittest tests/storage/test_sqlite_to_postgres_migration
conda run -n multi_agent python -m unittest tests/api/test_state_platform_cutover_smoke
conda run -n multi_agent python -m unittest tests/observability/test_state_platform_migration_evidence
git diff --check
```

## 5. Production Evidence Gate

Phase 4 只有在真实 PostgreSQL test/staging 环境完成 migration evidence 后才可进入 production cutover。用户提供远端生产地址前，不得宣称 production migration 已验证。
