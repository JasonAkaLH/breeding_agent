# Phase 4 PRD — SQLite 到 PostgreSQL Migration、Cutover 与 Rollback

- **日期**：2026-05-26
- **状态**：后续待实施
- **前置**：Phase 3 runtime integration / readiness / runbook 已通过
- **关联测试规格**：`test-spec-05-Phase4-SQLiteToPostgreSQLMigrationCutover.md`
- **范围**：SQLite 数据导出、PostgreSQL import、校验、shadow compare、cutover、rollback、backup/restore drill、生产 DSN 接入
- **非范围**：不重新设计 State Platform kernel；不把 SQLite 作为 production fallback；不伪造生产 migration evidence

## 1. Goals

1. 把 legacy SQLite 状态数据迁移到 PostgreSQL business tables 和 State Platform schema。
2. 提供可重复、可审计、可回滚的 migration workflow。
3. 在 cutover 前完成数据校验、shadow compare、backup/restore drill。
4. 接入用户后续提供的远端 PostgreSQL 地址，但不得提交真实 DSN。
5. 明确 rollback 条件和 legacy SQLite 下线条件。

## 2. Functional Requirements

| ID | Requirement | Acceptance |
| --- | --- | --- |
| P4-FR-1 | Migration 必须从 SQLite 读取并写入 PostgreSQL，保持 conversation/task/message/event/artifact/auth 等业务状态一致。 | Row count、checksum、referential checks 通过。 |
| P4-FR-2 | Migration 必须可 dry-run。 | dry-run 不修改目标生产表，输出脱敏报告。 |
| P4-FR-3 | Migration 必须可 resume。 | 中断后可从 checkpoint 继续，不重复写或破坏 idempotency。 |
| P4-FR-4 | Cutover 前必须 shadow compare。 | API read path 或 sampled query compare 无阻断差异。 |
| P4-FR-5 | Rollback 必须有明确条件和步骤。 | Runbook 演练通过。 |
| P4-FR-6 | 真实生产 DSN 不进入 tracked 文件。 | secret/static scan 通过。 |

## 3. Migration Scope

迁移对象至少覆盖：

- conversation
- message
- task
- task node / edge
- event log
- artifact metadata
- interrupt / cancellation / mailbox
- pending skill context
- conversation memory summary
- auth username token currentness

大对象文件本体仍遵循 artifact store 设计，不直接塞入 PostgreSQL business state。

## 4. Cutover Strategy

1. 备份 SQLite 和 PostgreSQL 目标库。
2. 执行 dry-run migration。
3. 执行 import 到 PostgreSQL staging schema 或目标 schema。
4. 执行 row count / checksum / sampled semantic validation。
5. 启动 shadow read compare 或双读校验窗口。
6. 冻结写入窗口或使用 command queue drain。
7. 切换 production backend 到 PostgreSQL。
8. 观察 readiness、queue backlog、dead-letter、API/SSE smoke。
9. 满足稳定窗口后记录 cutover evidence。

## 5. Rollback Strategy

- Cutover 后若出现 migration mismatch、readiness critical、data loss suspicion、write queue stuck、auth isolation failure，立即 rollback。
- Rollback 只能回到 cutover 前一致性快照，不允许 PostgreSQL partial writes 和 SQLite writes 双主长期并存。
- Rollback 后必须保留 incident report、diff report 和 blocked command/dead-letter evidence。

## 6. Non-functional Requirements

- Migration reports must redact DSN、secret、token、raw prompt/user payload。
- Migration scripts must have bounded runtime, progress reporting, and retry only for idempotent operations。
- Production cutover requires operator confirmation outside code review; Codex must not perform remote destructive production actions without explicit authority。

## 7. Risks and Mitigations

| Risk | Mitigation |
| --- | --- |
| 历史 SQLite 数据存在脏数据。 | staging import + validation report + explicit remediation table。 |
| Cutover 期间新写入丢失。 | queue drain / write freeze / command checkpoint。 |
| Rollback 形成双主。 | rollback runbook 禁止长期双写；只回到明确快照。 |
| 生产 secret 泄漏。 | DSN 只走 env / git-ignored config；static scan。 |

## 8. Exit Criteria

- dry-run、import、validation、shadow compare、backup/restore drill 全部有证据。
- cutover runbook 执行完成并记录 operator / timestamp / artifact。
- rollback drill 完成或有受控演练证据。
- SQLite production fallback 未启用。
