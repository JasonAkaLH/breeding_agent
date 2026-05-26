# Strong Conversation Delete Design

日期：2026-05-26

## 1. 背景

当前用户删除历史会话时，前端会等待 `DELETE /api/v1/conversations` 返回后才从历史列表移除条目。后端删除路径会同步完成运行中任务取消、artifact 文件清理和多表物理删除。迁移到远端 PostgreSQL 后，长历史会话会放大 DB 往返、索引维护、WAL 写入和多表删除成本，导致用户看到删除响应较慢。

本设计针对“单个会话历史轮数很多”的删除场景，不改变用户确认删除后的强物理删除语义。

## 2. 已确认决策

1. 删除采用强物理删除语义：成功返回表示相关业务数据已经物理删除完成。
2. 删除中的历史条目必须在对应条目上显示 spinner；不做全局 loading。
3. 删除某一条历史时，只锁住该条目，用户可以继续切换和使用其他会话。
4. 超长历史会话删除不设置应用层短超时；后端应一直执行到物理删除成功或真实失败。
5. 用户刷新、关闭浏览器或网络断开后，已经确认的删除仍由后端继续执行。
6. 删除失败后，普通用户列表不再显示该会话，后台保留 `deleting_failed` 状态供追踪和后续重试。

## 3. 目标

- 让长历史会话删除具备生产级强一致语义。
- 避免长历史删除依赖浏览器连接生命周期。
- 删除中会话不能继续写入、重命名、读取历史或订阅 SSE。
- 普通用户视图只展示可正常使用的 active 会话。
- PostgreSQL 删除路径使用集合化 SQL，避免把大量 task/message/event id 搬到 Python。
- 文件 I/O 与 DB 事务边界清晰，避免长事务里执行慢文件操作。
- 删除失败可追踪，不让普通用户端出现“已确认删除的会话又复活”。

## 4. 非目标

- 不做逻辑删除立即返回的后台异步清理体验。
- 不新增批量删除 API。
- 不要求普通用户 UI 展示删除失败会话。
- 不做历史 SQLite 数据迁移或恢复。
- 不引入外部任务队列系统；优先使用现有 FastAPI runtime 内部托管任务能力。

## 5. 当前代码影响面

- 前端删除入口：`frontend/src/App.tsx` 的 `handleDeleteConversation`。
- 前端 API client：`frontend/src/api/client.ts` 的 `deleteConversation`。
- 前端类型与测试：`frontend/src/api/types.ts`、`frontend/src/App.test.tsx`、`frontend/src/api/client.test.ts`。
- API route：`src/api/routes/conversations.py` 的 `DELETE /api/v1/conversations`。
- DTO：`src/api/dto.py` 的 `DeleteConversationResponse`。
- runtime：`src/api/runtime.py` 的 `delete_conversation`、`_delete_conversation_file_artifacts`、task cancel 相关逻辑。
- storage contract：`src/core/contracts.py`。
- core model / enum：`src/core/models.py`、`src/core/rust_contracts/core_contract.json`。
- SQLAlchemy models / repositories：`src/storage/sqlite/models.py`、`src/storage/sqlite/repositories.py`、`src/storage/postgres/`。
- PostgreSQL schema manifest / reconciler：`src/state/postgres/runtime_schema.py`、`src/state/postgres/schema_reconciler.py`。

## 6. 状态模型

新增 conversation 状态：

- `active`：普通可用会话。
- `deleting`：用户已确认删除，后端正在托管物理删除。
- `deleting_failed`：物理删除失败，普通用户不可见，后台可追踪和重试。

最终删除成功后不保留 `deleted` conversation 行；conversation 及相关业务行被物理删除。

普通用户可见规则：

- `list_conversations_for_username` 默认只返回 `active`。
- `get messages`、`submit message`、`rename`、`upload`、`SSE subscribe` 对 `deleting` / `deleting_failed` 会话返回不可操作错误，建议对普通用户保持 404 口径，避免泄露已确认删除的历史。
- 后台诊断或管理员重试接口不属于本阶段普通用户 API。

## 7. 删除请求流程

`DELETE /api/v1/conversations` 的目标流程：

1. 认证用户。
2. owner 校验并读取 conversation。
3. 获取 conversation 级删除互斥锁。
4. 如果 conversation 已经是 `deleting`，请求等待已有删除任务完成。
5. 如果 conversation 是 `active`，将状态更新为 `deleting` 并提交。
6. 启动或接管 runtime 内部 deletion runner。
7. 如果 HTTP 连接保持，route 等待 runner 完成后返回结果。
8. 如果 HTTP 客户端断开，runner 不被取消，继续执行物理删除。
9. 删除成功后返回 `deleted=true`；删除失败则返回明确错误，并保留后台失败状态。

幂等规则：

- 同一 conversation 重复 DELETE 不启动第二个物理删除任务。
- 同一进程内重复 DELETE 应等待同一个 deletion runner。
- 进程重启后的 `deleting` 恢复扫描需要重新接管未完成删除。

## 8. Deletion runner 物理删除流程

Deletion runner 负责完成不可被客户端断开取消的物理删除：

1. 读取该 conversation 下未完成 task。
2. 对未完成 task 执行现有 cancel 流程，并取消本进程内 active execution handle。
3. 一次性读取该 conversation 下所有 artifact refs。
4. 同步删除 active skill output 文件；文件删除失败则进入 `deleting_failed`。
5. 开启 DB 事务。
6. 在事务内执行 PostgreSQL 友好的 set-based delete。
7. commit 成功后，conversation 行不存在。
8. 失败时回滚 DB 事务，记录 `deleting_failed`、错误摘要、失败时间和可重试标记。

文件 I/O 不放入 DB 删除事务。这样可以避免一边持有大量行锁，一边等待文件系统操作。

## 9. PostgreSQL 删除策略

长历史会话删除必须使用集合化 SQL：

- 避免将大量 `task_id`、`message_id`、`event_id` 拉回 Python。
- 优先使用 `DELETE ... USING` 或子查询条件。
- 删除顺序仍遵守依赖关系：delivery / answers / checkpoints / interrupts / mailbox / events / artifacts / graph / summaries / messages / tasks / conversation。
- 大表删除条件必须有索引覆盖。

示例方向：

```sql
DELETE FROM artifact a
USING task t
WHERE a.task_id = t.task_id
  AND t.conversation_id = :conversation_id;
```

SQLite 测试路径可以保留兼容实现，但 PostgreSQL runtime 路径应使用专门优化实现，避免继承 SQLite repository 的 Python ID 搬运行为。

## 10. 锁与并发

- 同一 conversation 删除互斥。
- `deleting` / `deleting_failed` 状态禁止新消息、rename、upload、SSE 订阅和新的 task 写入。
- 其他 conversation 不受影响，可以继续读写。
- 删除 runner 应与当前 conversation guard 协作，避免删除中产生新的 message/task/event。
- PostgreSQL 可使用 row lock 或 advisory lock；runtime 内部也保留 per-conversation task map，避免同进程重复执行。

## 11. 前端用户体验

历史条目删除中表现：

- 对应历史条目显示 spinner。
- 禁用该条目的 rename/delete/select。
- 其他历史条目仍可点击、发送、上传和接收 SSE。
- 不设置前端自动超时。
- DELETE 成功后移除该条目。
- DELETE 返回失败时，如果列表刷新后该 conversation 不再出现，前端不把该条目恢复为可用；如果仍处于当前内存列表，则显示错误并等待下一次 history refresh 收敛。

如果删除当前会话：

- 当前会话区应停止该会话的 SSE subscription。
- 当前会话 workspace 切换到新空白 conversation。
- 历史条目继续显示删除中直到 DELETE 返回或 history refresh 不再返回该 conversation。

## 12. API 响应契约

现有响应字段保留兼容：

- `conversation_id`
- `deleted`
- `cancelled_task_ids`
- `deleted_counts`

建议新增字段：

- `delete_status`: `completed` / `failed`
- `runner_id`: 脱敏删除执行 id，用于日志关联
- `started_at`
- `finished_at`
- `error_code`：仅失败时返回稳定错误码

普通成功响应仍只在物理删除完成后返回。

## 13. 失败处理

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

## 14. 恢复与重启

应用启动时应扫描 `deleting` 状态的 conversation：

- 如果存在未完成删除，重新启动 deletion runner。
- 如果存在 `deleting_failed`，不自动展示给普通用户。
- 是否自动重试 `deleting_failed` 可作为后续运维策略；本阶段至少保证可追踪，不静默复活。

## 15. 测试策略

后端测试：

- 删除 active conversation 成功后所有相关业务表物理清空，auth token 保留。
- 删除中会话不允许 submit message / rename / list messages / SSE subscribe。
- 重复 DELETE 同一 conversation 不启动重复 runner。
- HTTP 等待 runner 完成后才返回成功。
- 模拟客户端断开时 runner 继续完成。
- artifact 文件删除失败时 conversation 进入 `deleting_failed`，普通列表不可见。
- 应用启动扫描 `deleting` conversation 并恢复删除。
- PostgreSQL delete path 使用 set-based delete，不依赖 Python 大量 ID 列表。

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

## 16. Rollout

1. 先增加状态枚举、schema reconciler 和 repository contract。
2. 增加 deletion runner 和 storage set-based delete，保持 route 响应兼容。
3. 前端接入条目级 spinner 和删除中禁用。
4. 加入启动恢复扫描。
5. 在远端 PostgreSQL 做长历史删除 smoke。
6. 观察日志和删除耗时后，再决定是否增加管理员重试接口。

## 17. 风险与缓解

- 长 DB 删除导致 PostgreSQL 压力升高：通过 set-based delete、索引覆盖、短事务和 conversation 级互斥降低风险。
- 文件删除成功但 DB 删除失败：conversation 保持 `deleting_failed`，普通用户不可见，后台可重试或人工处理。
- 进程重启中断 runner：启动恢复扫描重新接管 `deleting`。
- 反向代理连接超时：客户端可能收不到成功响应，但后端 runner 继续，刷新后普通用户不再看到该会话。
- 普通用户误以为删除失败会话丢失：这是已确认的产品语义，删除意图成立后失败不会让会话复活。
