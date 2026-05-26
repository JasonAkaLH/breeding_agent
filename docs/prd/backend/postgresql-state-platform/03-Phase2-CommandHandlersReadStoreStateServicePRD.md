# Phase 2 PRD — Command Handlers、ReadStore 与 StateService

- **日期**：2026-05-26
- **状态**：待实施
- **前置**：Phase 1 queue kernel 已通过
- **关联测试规格**：`test-spec-03-Phase2-CommandHandlersReadStoreStateService.md`
- **范围**：command handler framework、ReadStore、read-not-blocked、StateService submit / execute-and-wait / command group
- **非范围**：不接生产 API runtime；不做 SQLite 数据迁移；不配置远端生产库

## 1. Goals

1. 建立业务 command handler registry，覆盖 conversation、message、task、event、artifact、auth、interrupt、cancel、mailbox、pending skill context 等状态写入类型。
2. 每个 handler 声明 payload schema、partition rule、idempotency rule、全局锁顺序、retry policy、result schema。
3. 实现 PostgreSQL ReadStore，读最后已提交业务表，不读 pending queue，不使用写锁。
4. 实现 StateService：`submit_command()`、`execute_command_and_wait()`、`transactional_command_group()`。
5. 用真实 PostgreSQL integration 证明 writer 未提交时 reader 返回旧 committed snapshot。

## 2. Functional Requirements

| ID | Requirement | Acceptance |
| --- | --- | --- |
| P2-FR-1 | 所有 production write command type 必须注册 handler。 | Registry tests 证明无 orphan command type。 |
| P2-FR-2 | Handler transaction 不得执行 LLM/HTTP/Skill/MCP/file IO。 | Static / unit tests 覆盖 forbidden external IO seam。 |
| P2-FR-3 | ReadStore 不读 pending queue，不使用 `FOR UPDATE`。 | SQL inspection + integration tests。 |
| P2-FR-4 | ReadStore 在 writer 未提交时读取旧 committed snapshot。 | Real PostgreSQL MVCC test。 |
| P2-FR-5 | `execute_command_and_wait()` 支持 success/fail/timeout/cancel/idempotent replay。 | StateService tests。 |
| P2-FR-6 | `transactional_command_group()` 不允许跨外部 IO。 | Handler / service tests。 |

## 3. Partition Rules

| Scope | Partition key |
| --- | --- |
| Conversation / message / pending skill context | `conversation:{conversation_id}` |
| Task / node / edge / event / artifact / interrupt / mailbox | `task:{task_id}` |
| Auth token currentness | `auth:{username}` |
| Migration / cutover gate | `system:migration` |

跨 scope command 必须声明主 partition，并按全局锁顺序访问其他表。

## 4. Non-functional Requirements

- Read path P95 不应因 queue backlog 等待 pending command。
- Unknown handler error 默认 fail closed。
- Handler result 和 error metadata 必须脱敏。
- Fake/fault tests 可覆盖 error policy，但 MVCC read-not-blocked 必须有真实 PostgreSQL evidence 才能生产声明。

## 5. Implementation Plan

1. 新增 `src/state/postgres/handlers.py` 与 handler registry。
2. 新增 `src/state/postgres/read_store.py`。
3. 新增 `src/state/service.py`。
4. 新增 handler、read store、StateService tests。
5. 不修改 API runtime canonical path。

## 6. Risks and Mitigations

| Risk | Mitigation |
| --- | --- |
| Handler 里混入外部 IO，扩大事务时间。 | Contract 和 static tests 禁止。 |
| ReadStore 意外读 pending queue 造成读等待。 | Read tests 明确 pending command invisible。 |
| Command group 被滥用为长事务。 | 限制只做短 DB transaction；deadline tests。 |

## 7. Exit Criteria

- Handler registry、ReadStore、StateService targeted tests 通过。
- 真实 PostgreSQL read-not-blocked integration evidence 完成或明确 Not-tested。
- API runtime 尚未切换 production path。
