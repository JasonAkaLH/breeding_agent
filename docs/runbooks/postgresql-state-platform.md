# PostgreSQL State Platform Runbook

## Readiness checklist

- `MAF_STATE_STORE_BACKEND=postgresql` 只在目标环境确实要启用 PostgreSQL canonical state 时设置。
- `MAF_POSTGRES_STATE_DSN` 只通过部署环境或 git-ignored `config.yaml` bootstrap 提供，禁止提交到 tracked 文件。
- `MAF_STATE_PLATFORM_CUTOVER_READY` / `cutover_ready` 已从 runtime gate 中移除；不要再用它控制启动。
- Queue backlog、oldest pending age、dead-letter count、worker heartbeat 和 API error rate 可观测。
- RuntimeSidecar writer 不得对同一 canonical state tables 处于 enforce writer 模式。

## Startup schema bootstrap

1. 启动会在 PostgreSQL backend 下执行 no-drop schema bootstrap：`CREATE TYPE/TABLE/INDEX IF NOT EXISTS`，并使用 advisory lock、`lock_timeout`、`statement_timeout` 控制启动期持锁风险。
2. 生产账号不需要也不应拥有删除表/删除库权限。
3. Runtime 不再检查 cutover readiness；若 PostgreSQL DSN、driver、权限、连接或 schema bootstrap 失败，则 fail closed。
4. 禁止通过手工 SQL 执行 `DROP TABLE`、`TRUNCATE`、`ALTER TABLE DROP COLUMN`、`DELETE FROM` 等破坏性修复；需要新 PRD/变更评审。

## Fresh cutover gate

- 本项目已锁定 PostgreSQL fresh start：不迁移 SQLite 历史数据，不做 row-count/checksum/shadow compare。
- Cutover CLI 只用于 readiness 汇总，必须拒绝 raw DSN CLI 参数和 `--sqlite-path`。
- Cutover 阻断条件：schema 未 ready、runtime smoke 未 ready、queue backlog 非 0、dead-letter 非空、SQLite history abandonment 未确认。
- SQLite 不是 production fallback；rollback 是回到部署配置/镜像上一版，不是回迁旧 SQLite。

## Dead-letter triage

1. Inspect redacted dead-letter metadata: command type, partition category, SQLSTATE/error code, attempt count, timestamps.
2. Do not replay commands containing unknown security/contract errors without manual remediation.
3. For transient SQLSTATEs (`40P01`, `40001`, `55P03`, `57014`), verify the root cause has cleared before replay.
4. Replay through the State Platform command API, not by mutating business tables directly.

## Worker drain and lease recovery

1. Set worker drain mode in deployment orchestration.
2. Wait for in-flight leased commands to finish or lease expiry to pass.
3. Confirm stale leases are reclaimable and same-partition ordering is preserved.
4. Restart workers gradually and watch duplicate-claim / dead-letter metrics.

## SQLite cleanup after cutover

1. Cleanup is operator-only and defaults to dry-run.
2. Candidate files must be inside the configured runtime directory.
3. Non-dry-run cleanup requires explicit confirmation.
4. Prefer archive first (`*.postgresql-fresh-cutover-archive`); delete only after external backup/retention decision.
5. Cleanup reports must not contain DSN/password/token/raw user payload.

## Strong conversation deletion recovery

1. 普通用户删除会话后，conversation 会先进入 `deleting`，普通 list/messages/task/upload/SSE/artifact/cancel/interrupt 路由均按不可见处理；成功后业务行被物理删除，不保留 `deleted` conversation 行。
2. 删除请求没有前端/用户侧自动超时承诺。HTTP 客户端刷新、断网或取消等待不会取消后端 deletion runner；若 API 进程重启，启动恢复会扫描 `deleting` 并重新接管。
3. 失败会话保持 `deleting_failed`，普通用户不可见，不允许直接改回 `active`。错误摘要必须脱敏，不得包含 DSN、password、token、API key、provider base_url 或完整堆栈。
4. 诊断失败/进行中删除：

```bash
python scripts/conversation_delete_ops.py --json list --include-deleting
```

5. 重试单个 `deleting_failed` 会话并重新进入 deletion runner：

```bash
python scripts/conversation_delete_ops.py --json retry --conversation-id <conversation_id>
```

6. 运维脚本只从 `MAF_POSTGRES_STATE_DSN`（或 `--dsn-env` 指定的环境变量）读取 PostgreSQL DSN，拒绝 raw DSN CLI 参数；输出必须保持脱敏。
7. 如果重试再次失败，保留 `deleting_failed` 元数据并根据 `delete_phase`、`delete_error_code` 与审计日志定位是 task cancel、artifact 文件、还是 PostgreSQL set-based delete 阶段失败。缺失 artifact 文件应视为幂等成功，不要手工恢复文件来“凑齐”删除。
