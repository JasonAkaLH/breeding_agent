# Strong Conversation Delete Design

日期：2026-05-26

状态：document-perfectization 已复审，进入实施计划前版本

## 1. 背景

当前用户删除历史会话时，前端会等待 `DELETE /api/v1/conversations` 返回后才从历史列表移除条目。后端删除路径会同步完成运行中任务取消、artifact 文件清理和多表物理删除。迁移到远端 PostgreSQL 后，长历史会话会放大 DB 往返、索引维护、WAL 写入和多表删除成本，导致用户看到删除响应较慢。

本设计针对“单个会话历史轮数很多”的删除场景，不改变用户确认删除后的强物理删除语义。

## 2. 当前状态证据

| 证据 | 当前行为 | 设计影响 |
| --- | --- | --- |
| `frontend/src/App.tsx` 的 `handleDeleteConversation` | 前端等待 `api.deleteConversation` 返回后才移除历史条目。 | 必须补条目级删除中状态，不能只靠全局提示。 |
| `src/api/routes/conversations.py` 的 `DELETE /api/v1/conversations` | route 先 owner 校验，再调用 runtime 删除并返回 `deleted_counts`。 | route 需要把 owner 校验结果传递给 runtime，避免重复读 conversation。 |
| `src/api/runtime.py` 的 `delete_conversation` | runtime 同步取消任务、删除 artifact 文件、调用 storage 物理删除。 | 需要拆出不会因客户端断开而取消的 deletion runner。 |
| `src/storage/sqlite/repositories.py` 的 `delete_conversation` | 先把 task / mailbox / interrupt id 拉回 Python，再逐表 delete。 | PostgreSQL 路径必须改为 set-based delete，避免长历史下 Python 搬运大 ID 列表。 |
| `src/core/enums.py` / `src/core/rust_contracts/core_contract.json` | 当前 `ConversationStatus` 只有 `active`、`archived`、`locked`。 | 必须新增 `deleting`、`deleting_failed`，并同步 Rust/core contract。 |
| `src/storage/sqlite/repositories.py` 的 `list_conversations_for_username` | 当前按 username 返回所有状态。 | 用户列表必须只返回 `active`，删除中/失败会话不可见。 |

## 3. 已确认决策

1. 删除采用强物理删除语义：成功返回表示相关业务数据已经物理删除完成。
2. 删除中的历史条目必须在对应条目上显示 spinner；不做全局 loading。
3. 删除某一条历史时，只锁住该条目，用户可以继续切换和使用其他会话。
4. 超长历史会话删除不设置应用层短超时；后端应一直执行到物理删除成功或真实失败。
5. 用户刷新、关闭浏览器或网络断开后，已经确认的删除仍由后端继续执行。
6. 删除失败后，普通用户列表不再显示该会话，后台保留 `deleting_failed` 状态供追踪和后续重试。

## 4. 目标

- 让长历史会话删除具备生产级强一致语义。
- 避免长历史删除依赖浏览器连接生命周期。
- 删除中会话不能继续写入、重命名、读取历史或订阅 SSE。
- 普通用户视图只展示可正常使用的 active 会话。
- PostgreSQL 删除路径使用集合化 SQL，避免把大量 task/message/event id 搬到 Python。
- 文件 I/O 与 DB 事务边界清晰，避免长事务里执行慢文件操作。
- 删除失败可追踪，不让普通用户端出现“已确认删除的会话又复活”。
- 支持进程重启后接管 `deleting` 状态会话继续删除。

## 5. 非目标

- 不做逻辑删除立即返回的后台异步清理体验。
- 不新增面向普通用户的批量删除 API。
- 不要求普通用户 UI 展示删除失败会话。
- 不做历史 SQLite 数据迁移或恢复。
- 不引入外部任务队列系统；优先使用现有 FastAPI runtime 内部托管任务能力。
- 不在本阶段提供完整管理后台；但必须提供最小运维可观测与重试入口，避免 `deleting_failed` 只能靠直接改数据库处理。

## 6. 用户、系统与受影响方

| 角色 / 系统 | 诉求 |
| --- | --- |
| 普通聊天用户 | 点击删除后看到对应历史条目正在删除；其他会话可继续使用；删除确认后该会话不会复活。 |
| API runtime | 在客户端断开后仍完成删除；同一 conversation 删除互斥；不阻塞其他 conversation。 |
| PostgreSQL state platform | 用短事务和集合化 SQL 完成长历史多表删除，避免无索引扫描和应用层大 ID 搬运。 |
| 运维 / 管理员 | 可观测 `deleting` / `deleting_failed` 状态、失败阶段和错误码，并能触发重试。 |
| 前端 | 用条目级 spinner 表达删除进度，不用前端超时误判删除失败。 |

## 7. 当前代码影响面

- 前端删除入口：`frontend/src/App.tsx` 的 `handleDeleteConversation`。
- 前端 API client：`frontend/src/api/client.ts` 的 `deleteConversation`。
- 前端类型与测试：`frontend/src/api/types.ts`、`frontend/src/App.test.tsx`、`frontend/src/api/client.test.ts`。
- API route：`src/api/routes/conversations.py` 的 `DELETE /api/v1/conversations`。
- DTO：`src/api/dto.py` 的 `DeleteConversationResponse`。
- runtime：`src/api/runtime.py` 的 `delete_conversation`、`_delete_conversation_file_artifacts`、task cancel 相关逻辑。
- storage contract：`src/core/contracts.py`。
- core model / enum：`src/core/models.py`、`src/core/rust_contracts/core_contract.json`、`native/crates/maf_core_types/src/lib.rs`。
- SQLAlchemy models / repositories：`src/storage/sqlite/models.py`、`src/storage/sqlite/repositories.py`、`src/storage/postgres/`。
- PostgreSQL schema manifest / reconciler：`src/state/postgres/runtime_schema.py`、`src/state/postgres/schema_reconciler.py`。
- API 文档：`docs/api/api-doc.html` 需要说明删除中、断线继续和普通用户不可见规则。

## 8. 状态模型与 schema

新增 conversation 状态：

- `active`：普通可用会话。
- `deleting`：用户已确认删除，后端正在托管物理删除。
- `deleting_failed`：物理删除失败，普通用户不可见，后台可追踪和重试。

最终删除成功后不保留 `deleted` conversation 行；conversation 及相关业务行被物理删除。

建议新增 deletion 元数据列，或等价的独立 deletion tracking 表。若使用 conversation 列，字段至少包括：

- `delete_runner_id`：脱敏 runner id / correlation id。
- `delete_requested_at`：用户确认删除并标记 `deleting` 的时间。
- `delete_started_at`：runner 开始执行物理删除的时间。
- `delete_finished_at`：成功或失败结束时间。
- `delete_failed_at`：失败时间。
- `delete_error_code`：稳定错误码。
- `delete_error_summary`：脱敏错误摘要，不写敏感配置或完整堆栈。
- `delete_phase`：`marking` / `cancelling_tasks` / `deleting_files` / `deleting_db` / `completed` / `failed`。

普通用户可见规则：

- `list_conversations_for_username` 默认只返回 `active`。
- `get messages`、`submit message`、`rename`、`upload`、`SSE subscribe` 对 `deleting` / `deleting_failed` 会话返回不可操作错误；普通用户 API 使用 404 口径，避免泄露已确认删除的历史。
- 运维重试入口可读取 `deleting_failed`，但必须脱敏错误信息。

## 9. 删除请求流程

`DELETE /api/v1/conversations` 的目标流程：

1. 认证用户。
2. owner 校验并读取 conversation；route 将该 conversation 或 owner 校验结果传给 runtime，runtime 不重复读取同一行。
3. 获取跨进程有效的 conversation 删除互斥锁。生产 PostgreSQL 下优先使用 row lock / advisory lock；进程内 task map 只作为同进程去重，不作为唯一互斥机制。
4. 如果 conversation 已经是 `deleting`，请求等待已有删除任务完成。
5. 如果 conversation 是 `deleting_failed`，普通用户 DELETE 返回 404；运维重试走单独入口。
6. 如果 conversation 是 `active`，将状态更新为 `deleting`，写入 deletion 元数据并提交。
7. 启动或接管 runtime 内部 deletion runner。
8. route 使用 `asyncio.shield` 或等价机制等待 runner，使 HTTP 客户端取消不会取消 runner。
9. 如果 HTTP 连接保持，route 等待 runner 完成后返回结果。
10. 如果 HTTP 客户端断开，runner 不被取消，继续执行物理删除。
11. 删除成功后返回 `deleted=true`；删除失败则返回明确错误，并保留后台失败状态。

幂等规则：

- 同一 conversation 重复 DELETE 不启动第二个物理删除任务。
- 同一进程内重复 DELETE 应等待同一个 deletion runner。
- 跨进程重复 DELETE 必须通过 PostgreSQL lock / 状态条件更新防止双 runner。
- 进程重启后的 `deleting` 恢复扫描需要重新接管未完成删除。

## 10. Deletion runner 物理删除流程

Deletion runner 负责完成不可被客户端断开取消的物理删除：

1. 将 `delete_phase` 更新为 `cancelling_tasks`。
2. 读取该 conversation 下未完成 task。
3. 对未完成 task 执行现有 cancel 流程，并取消本进程内 active execution handle。
4. 将 `delete_phase` 更新为 `deleting_files`。
5. 一次性读取该 conversation 下所有 artifact refs。
6. 同步删除 active skill output 文件；文件删除必须幂等，目标文件已不存在应视为成功。
7. 将 `delete_phase` 更新为 `deleting_db`。
8. 开启 DB 事务。
9. 在事务内执行 PostgreSQL 友好的 set-based delete。
10. commit 成功后，conversation 行不存在。
11. 失败时回滚 DB 事务，记录 `deleting_failed`、错误摘要、失败阶段、失败时间和可重试标记。

文件 I/O 不放入 DB 删除事务。这样可以避免一边持有大量行锁，一边等待文件系统操作。

如果文件删除已经部分成功但 DB 删除失败，conversation 保持 `deleting_failed` 且普通用户不可见；后续重试必须把缺失文件视为幂等成功，继续完成 DB 删除。

## 11. PostgreSQL 删除策略

长历史会话删除必须使用集合化 SQL：

- 避免将大量 `task_id`、`message_id`、`event_id` 拉回 Python。
- 优先使用 `DELETE ... USING` 或子查询条件。
- 删除顺序仍遵守依赖关系：delivery / answers / checkpoints / interrupts / mailbox / events / artifacts / graph / summaries / messages / tasks / conversation。
- 大表删除条件必须有索引覆盖。
- PostgreSQL 路径必须有独立 repository 实现或 dialect 分支；不得继续复用 SQLite physical delete 中的大 ID Python 列表策略作为生产路径。

示例方向：

```sql
DELETE FROM artifact a
USING task t
WHERE a.task_id = t.task_id
  AND t.conversation_id = :conversation_id;
```

索引检查必须覆盖以下删除条件：

| 表 | 删除条件 | 索引要求 |
| --- | --- | --- |
| `message` | `conversation_id` 或关联 `task_id` | `conversation_id` 已有；若使用 task 子查询需确认 task 侧索引。 |
| `task` | `conversation_id` | `task(conversation_id, created_at)` 已有。 |
| `event_record` | `conversation_id` / `task_id` | 已有 conversation/task 索引。 |
| `artifact` | `task_id` | 已有 `artifact(task_id, created_at)`。 |
| `task_node` | `task_id` | 已有 `task_node(task_id, status)`。 |
| `task_edge` | `task_id` | 需确认 `task_edge(task_id, to_node_id)` 覆盖删除。 |
| `mailbox_message` | `conversation_id` / `task_id` | 当前已有 task 索引；需要新增或确认 conversation_id 索引。 |
| `mailbox_delivery` | `message_id` | 需要确认或新增 message_id 索引。 |
| `interrupt` | `conversation_id` / `task_id` | 已有 conversation/status 与 task/node 索引。 |
| `interrupt_answer` | `interrupt_id` | 需要确认或新增 interrupt_id 索引。 |
| `checkpoint` | `task_id` | 已有 `checkpoint(task_id, node_id, created_at)`。 |
| `conversation_pending_skill_context` | `conversation_id` | 已有 conversation/status/updated 索引。 |

## 12. 锁与并发

- 同一 conversation 删除互斥。
- `deleting` / `deleting_failed` 状态禁止新消息、rename、upload、SSE 订阅和新的 task 写入。
- 其他 conversation 不受影响，可以继续读写。
- 删除 runner 应与当前 conversation guard 协作，避免删除中产生新的 message/task/event。
- 不能用全局 runtime lock 包住完整删除流程；否则长历史删除会阻塞无关会话。
- PostgreSQL row lock / advisory lock 是生产互斥来源；runtime 内部 per-conversation task map 只解决同进程重复执行。

## 13. 前端用户体验

历史条目删除中表现：

- 对应历史条目显示 spinner。
- 禁用该条目的 rename/delete/select。
- 其他历史条目仍可点击、发送、上传和接收 SSE。
- 不设置前端自动超时。
- DELETE 成功后移除该条目。
- 如果浏览器刷新或重新登录，普通 history list 不再返回 `deleting` / `deleting_failed` 会话；因此删除中的条目不需要跨刷新恢复 spinner。
- DELETE 返回失败时，如果列表刷新后该 conversation 不再出现，前端不把该条目恢复为可用；如果仍处于当前内存列表，则显示错误并等待下一次 history refresh 收敛。

如果删除当前会话：

- 当前会话区应停止该会话的 SSE subscription。
- 当前会话 workspace 切换到新空白 conversation。
- 历史条目继续显示删除中直到 DELETE 返回或 history refresh 不再返回该 conversation。

## 14. API 响应契约

现有响应字段保留兼容：

- `conversation_id`
- `deleted`
- `cancelled_task_ids`
- `deleted_counts`

新增字段：

- `delete_status`: `completed` / `failed`
- `runner_id`: 脱敏删除执行 id，用于日志关联
- `started_at`
- `finished_at`
- `error_code`：仅失败时返回稳定错误码

普通成功响应只在物理删除完成后返回。普通用户请求如果等待期间 runner 失败，API 应返回 5xx 或稳定业务错误，并携带 `error_code`；但该会话仍保持普通用户不可见的 `deleting_failed` 状态。

## 15. 失败处理与运维入口

失败来源包括：

- task cancel 失败。
- artifact 文件删除失败。
- PostgreSQL 删除 SQL 失败。
- 进程重启时删除 runner 中断。

处理规则：

- 已标记 `deleting` 的 conversation 不恢复到普通用户可见列表。
- 失败后标记为 `deleting_failed`。
- 记录错误摘要、失败阶段、失败时间、runner id。
- 后续由管理员或系统重试机制接管，不在普通聊天 UI 暴露失败条目。

最低运维要求：

- 提供只读诊断命令或脚本，列出 `deleting` / `deleting_failed` conversation 的脱敏状态、失败阶段、错误码和更新时间。
- 提供重试命令或受控内部入口，将 `deleting_failed` 重新接管到 deletion runner。
- 运维入口不得输出数据库密码、API token、provider base_url 等敏感信息。

## 16. 恢复与重启

应用启动时应扫描 `deleting` 状态的 conversation：

- 如果存在未完成删除，重新启动 deletion runner。
- 如果存在 `deleting_failed`，不自动展示给普通用户。
- `deleting_failed` 是否自动重试取决于后续运维策略；本阶段至少保证诊断和手动重试，不静默复活。

如果应用在文件删除后、DB 删除前崩溃，启动恢复会重新进入 runner；缺失文件必须按幂等成功处理，然后继续 DB 删除。

## 17. 功能需求矩阵

| ID | Requirement | Acceptance criteria |
| --- | --- | --- |
| FR-1 | 用户确认删除后，conversation 标记为 `deleting`。 | DELETE 启动后普通 list 不再返回该 conversation；submit/rename/messages/SSE 返回 404。 |
| FR-2 | 删除 runner 不受客户端断开影响。 | 测试中取消/断开等待 DELETE 的客户端任务后，runner 仍完成物理删除。 |
| FR-3 | 删除成功代表物理删除完成。 | 返回成功后 conversation/message/task/event/artifact 等业务表均无该 conversation 相关行。 |
| FR-4 | 长历史 PostgreSQL 删除使用 set-based SQL。 | 测试或 SQL 编译断言不出现 Python 大 ID 列表拼接作为生产 PostgreSQL 删除路径。 |
| FR-5 | 删除中只锁目标会话。 | 删除长历史会话期间，另一个 conversation 可以 list messages、submit message 和接收 SSE。 |
| FR-6 | 删除失败不让普通用户看到会话复活。 | 模拟 artifact 或 DB 删除失败后，conversation 状态为 `deleting_failed`，普通 list/messages 返回不可见。 |
| FR-7 | 前端条目级反馈。 | 点击删除后只有目标历史条目显示 spinner 并禁用自身操作，其他条目仍可用。 |
| FR-8 | 进程重启恢复。 | 启动扫描接管 `deleting` conversation，并最终完成物理删除或进入 `deleting_failed`。 |
| FR-9 | 运维可追踪。 | 诊断入口能脱敏输出 runner id、phase、error_code、更新时间；重试入口能重新接管 `deleting_failed`。 |

## 18. 非功能需求

| 维度 | Requirement |
| --- | --- |
| 一致性 | 普通用户 API 对 `deleting` / `deleting_failed` 统一不可见；成功响应不得早于物理删除完成。 |
| 性能 | 删除 SQL 应集合化并使用索引；DB 事务内不得执行文件 I/O；不能用全局 runtime lock 阻塞无关会话。 |
| 可靠性 | 客户端断开不得取消 runner；进程重启后可恢复 `deleting`；文件删除重试幂等。 |
| 安全/隐私 | 删除失败错误摘要必须脱敏；普通用户不能访问删除中/失败会话。 |
| 可观测性 | 每个删除 runner 有 correlation id、phase、开始/结束时间、错误码。 |
| 兼容性 | 保留现有 DELETE 响应核心字段；新增字段向后兼容。 |

## 19. 测试策略

后端测试：

- 删除 active conversation 成功后所有相关业务表物理清空，auth token 保留。
- 删除中会话不允许 submit message / rename / list messages / SSE subscribe。
- 重复 DELETE 同一 conversation 不启动重复 runner。
- HTTP 等待 runner 完成后才返回成功。
- 模拟客户端断开时 runner 继续完成。
- artifact 文件删除失败时 conversation 进入 `deleting_failed`，普通列表不可见。
- 应用启动扫描 `deleting` conversation 并恢复删除。
- PostgreSQL delete path 使用 set-based delete，不依赖 Python 大量 ID 列表。
- 缺失 artifact 文件在重试中视为幂等成功。
- 删除期间另一个 conversation 可正常读写。

前端测试：

- 删除目标历史条目显示 spinner。
- 删除中只禁用目标条目操作，其他会话仍可使用。
- 当前会话删除时关闭当前 SSE 并切换新 workspace。
- DELETE 成功后移除条目。
- DELETE 失败但 history refresh 不返回该会话时不恢复条目。
- 不存在前端自动超时导致的失败状态。

集成 / smoke：

- 远端 PostgreSQL 下创建长历史会话，验证删除期间其他会话读写不阻塞。
- 删除完成后 list/messages/tasks/artifacts 均不可见。
- 断开浏览器连接后，后端日志显示 runner 继续并完成。
- 应用重启后扫描并接管 `deleting` 会话。

## 20. Rollout

1. 增加状态枚举、schema reconciler、core/Rust contract 和 repository contract。
2. 增加 deletion 元数据字段或 tracking 表，并补 schema drift 测试。
3. 增加 storage set-based delete 和 artifact refs 批量读取。
4. 增加 deletion runner、跨进程互斥、客户端断开保护和启动恢复扫描。
5. 收紧普通用户 API 对 `deleting` / `deleting_failed` 的不可见规则。
6. 前端接入条目级 spinner 和删除中禁用。
7. 增加脱敏诊断/重试运维入口。
8. 在远端 PostgreSQL 做长历史删除 smoke。
9. 观察日志、runner phase 和删除耗时后，再决定是否需要完整管理后台。

## 21. 风险与缓解

| Risk | Impact | Mitigation |
| --- | --- | --- |
| 长 DB 删除导致 PostgreSQL 压力升高 | 影响同库其他请求 | set-based delete、索引覆盖、短事务、conversation 级互斥、不用全局 runtime lock。 |
| 文件删除成功但 DB 删除失败 | 文件已少量缺失、DB 仍保留删除中记录 | `deleting_failed` 普通用户不可见；重试时文件缺失按幂等成功处理。 |
| 进程重启中断 runner | 删除停在 `deleting` | 启动恢复扫描重新接管。 |
| 反向代理连接超时 | 客户端收不到成功响应 | runner 继续；刷新后普通用户不再看到该会话；日志可追踪 runner。 |
| 多 worker 重复 runner | 可能双重取消/删除 | PostgreSQL row/advisory lock 与条件状态更新作为生产互斥来源。 |
| 普通用户误以为删除失败会话丢失 | 支持压力 | API 文档说明删除确认后失败不复活；运维可诊断/重试。 |

## 22. 记录假设

- 生产部署允许 FastAPI runtime 托管长生命周期 deletion runner；如果未来改为多实例水平扩展，PostgreSQL lock 和启动恢复扫描仍是必需边界。
- 当前阶段不建设面向普通用户的删除失败恢复 UI；删除失败由运维或系统重试处理。
- 反向代理或浏览器连接中断不等于撤销删除意图；这是已确认产品语义。
