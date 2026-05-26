# Test Spec — Phase 4 PostgreSQL Fresh Cutover 与 SQLite 历史废弃

- **日期**：2026-05-26
- **状态**：已按 fresh start 决策更新
- **关联 PRD**：`05-Phase4-SQLiteToPostgreSQLMigrationCutoverPRD.md`

## 1. Test Goals

证明 PostgreSQL fresh cutover 能在空 DB 上安全建立 canonical state schema、阻止未 ready runtime serving、拒绝 SQLite migration/import，并且所有报告和工具都不泄漏生产 secret。

## 2. Test Categories

| Category | Coverage |
| --- | --- |
| Runtime schema manifest | SQLite runtime metadata 编译为 PostgreSQL DDL；业务表和 State Platform 表完整。 |
| Schema reconciliation | 缺表/缺列只产生 create/add-column；类型 drift fail closed；破坏性 SQL guard。 |
| PostgreSQL runtime assembly | backend=postgresql 时使用 PostgreSQLStorage；production sqlite fail closed；未 cutover ready 先 bootstrap 后阻止 serving。 |
| Fresh cutover CLI | readiness 汇总、raw DSN 拒绝、`--sqlite-path` 拒绝、redacted output。 |
| Migration-disabled shim | 旧 migration 入口必须失败，禁止 SQLite import。 |
| SQLite cleanup | 默认 dry-run；非 dry-run 要 confirmation；只能处理 runtime dir 内候选文件。 |
| Regression | storage/api/observability/core/lifecycle/orchestration/integrations/capabilities/e2e 分层回归。 |

## 3. Required Cases

| Case | Expected |
| --- | --- |
| manifest contains runtime tables | conversation/message/task/event/artifact/auth/pending context/memory 等 runtime 表进入 manifest。 |
| DDL has no destructive SQL | `DROP TABLE`、`DROP DATABASE`、`TRUNCATE`、`ALTER DROP COLUMN`、`DELETE FROM`、`DROP INDEX` 全部被拒绝。 |
| empty inspection creates schema | 空 PostgreSQL inspection 生成 create runtime schema + create state schema 动作。 |
| type drift fail closed | 不安全类型差异抛出 `PostgresSchemaDriftError`。 |
| PostgreSQL backend ready | DSN + cutover ready 下 runtime config public output 不泄漏 DSN。 |
| PostgreSQL backend configured | runtime bootstrap 被调用；不存在 cutover readiness gate；真实连接/权限/schema 错误才阻止 serving。 |
| legacy migration rejected | `scripts/postgresql_state_migration.py` 返回失败并说明 fresh cutover 禁止 SQLite history import。 |
| cutover rejects sqlite path | `scripts/postgresql_state_cutover.py --sqlite-path ...` 返回失败。 |
| cleanup confirmation | 非 dry-run cleanup 未确认时报错；确认后 archive/delete 只处理 runtime dir 内文件。 |
| no tracked secret | tracked 文件不含生产 DSN、账号、密码、token。 |

## 4. Verification Commands

```bash
conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/observability -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/core -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/lifecycle -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/main_agent -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/e2e -p 'test_*.py'
conda run -n multi_agent python -m compileall -q src/state src/storage src/api/runtime.py scripts/postgresql_state_cutover.py scripts/postgresql_state_migration.py scripts/validate_postgresql_state_platform_runtime.py
git diff --check
```

## 5. Production Evidence Gate

Phase 4 code can be merged after local regression passes. Production serving now depends on PostgreSQL backend/DSN plus successful schema bootstrap/runtime smoke; `MAF_STATE_PLATFORM_CUTOVER_READY` is no longer a runtime gate.
