# Phase 4 PRD — PostgreSQL Fresh Cutover 与 SQLite 历史废弃

- **日期**：2026-05-26
- **状态**：已按 fresh start 决策更新，实施中
- **前置**：Phase 3 runtime integration / readiness / runbook 已通过
- **关联测试规格**：`test-spec-05-Phase4-SQLiteToPostgreSQLMigrationCutover.md`
- **范围**：远端 PostgreSQL 作为新的 canonical state store；启动期 no-drop schema bootstrap/reconciliation；schema readiness；空状态 API/SSE smoke；SQLite 文件显式 operator cleanup
- **非范围**：不迁移 SQLite 历史数据；不做 row-count/checksum/shadow-read parity；不把 SQLite 作为 production fallback；不提交真实 DSN/密码

## 1. Locked Decision

PostgreSQL 是新的生产状态起点。历史 SQLite 数据不重要，必须被视为 legacy local artifact，而不是迁移源。

因此 Phase 4 不再实现 SQLite -> PostgreSQL import。任何 `--sqlite-path`、row-count/checksum、shadow compare 或 dual-primary 方案都应被拒绝或标记为 superseded。

## 2. Goals

1. 在空 PostgreSQL DB 上创建/校验当前 runtime 所需表、索引、枚举和 State Platform 操作表。
2. 所有 schema bootstrap 必须 no-drop / no-truncate / no-destructive，并适配无删除表/库权限的生产账号。
3. Runtime 不再读取 cutover_ready 开关；PostgreSQL 被选中后，启动只受 DSN、driver、schema bootstrap、真实连接/权限错误和 writer conflict 约束。
4. 保持读不阻塞写队列：API 读走普通 snapshot/read committed 查询，写入通过 PostgreSQL canonical storage + State Platform write queue 设计承载。
5. SQLite cleanup 仅作为显式 operator 动作，默认 dry-run，可 archive/delete，但不得自动迁移或自动删除。

## 3. Functional Requirements

| ID | Requirement | Acceptance |
| --- | --- | --- |
| P4-FR-1 | PostgreSQL runtime schema manifest 覆盖 conversation/task/message/event/artifact/auth/pending context/memory 等业务表，以及 State Platform 操作表。 | manifest checksum 稳定，DDL 使用 PostgreSQL 类型并包含所有 runtime 表。 |
| P4-FR-2 | 启动期 schema bootstrap 只能创建缺失对象或安全 add column。 | DDL 静态 guard 拒绝 `DROP TABLE`、`DROP DATABASE`、`TRUNCATE`、`ALTER DROP COLUMN`、`DELETE FROM`、`DROP INDEX`。 |
| P4-FR-3 | Runtime assembly 支持 PostgreSQL backend。 | `MAF_STATE_STORE_BACKEND=postgresql` + DSN + cutover ready 时使用 PostgreSQLStorage；production sqlite fail closed。 |
| P4-FR-4 | `cutover_ready` 不得影响 runtime startup。 | config 中即使残留该字段也会被忽略；PostgreSQL backend 只按 DSN/driver/schema/bootstrap 结果 fail closed。 |
| P4-FR-5 | 旧 migration 脚本/入口不得导入 SQLite。 | migration shim 返回失败并提示 fresh cutover；cutover CLI 拒绝 `--sqlite-path` 和 raw DSN。 |
| P4-FR-6 | SQLite cleanup 是显式 operator-only。 | 默认 dry-run；非 dry-run 要 confirmation；候选文件必须在 runtime dir 内。 |
| P4-FR-7 | Secret 不进入 tracked 文件和公共输出。 | DSN 仅由 env/git-ignored config 提供；公开 report 只显示 `<configured>`。 |

## 4. Cutover Strategy

1. Operator 在部署环境提供 git-ignored `config.yaml` 或环境变量：backend、DSN、schema。
2. Runtime 或 operator preflight 执行 no-drop schema bootstrap：`CREATE TYPE/TABLE/INDEX IF NOT EXISTS` + advisory lock + lock timeout。
3. 若 readiness 未通过，启动应 fail closed；不会进入 serving。
4. readiness 通过后启动 API，执行空状态登录、会话创建、消息提交、刷新恢复、SSE smoke。
5. 观察 queue backlog、dead-letter、worker heartbeat 和 API error rate。
6. 确认稳定后，可对本地 SQLite 文件执行 dry-run cleanup；需要明确确认才 archive/delete。

## 5. Rollback Strategy

- Fresh cutover 没有历史数据回滚要求；rollback 是回到部署前镜像/配置，不是回迁 SQLite。
- 若 PostgreSQL schema/readiness/API smoke 失败，关闭 PostgreSQL serving gate，修复 schema/config 后重试。
- 不允许 SQLite 与 PostgreSQL 长期双主写入。

## 6. Non-functional Requirements

- Schema bootstrap 必须 bounded timeout，避免启动期长时间持锁。
- 读路径不得获取会阻塞 writer queue 的行锁。
- 写入 deadlock / serialization / lock timeout 必须通过 bounded retry / dead-letter 处理，不可无限重试。
- 所有 operator 报告必须 redacted。

## 7. Exit Criteria

- PostgreSQL driver 进入依赖快照并通过 runtime driver gate。
- Fresh schema DDL/manifest/reconciler/forbidden SQL tests 通过。
- API runtime PostgreSQL assembly + production fail-closed tests 通过。
- Migration shim 和 cutover CLI 明确拒绝 SQLite import/raw DSN。
- Storage/API/observability/core/lifecycle/orchestration/integration/capability/e2e 回归通过。
