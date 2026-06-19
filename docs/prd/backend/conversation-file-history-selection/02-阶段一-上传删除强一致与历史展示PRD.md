# 阶段一：上传删除强一致与历史展示 PRD

- **编号**：后端 PRD 21-Phase 1
- **日期**：2026-06-19
- **状态**：待实施
- **前置阶段**：阶段零数据模型与 Repository 基线
- **目标模块**：upload API、conversation file resource repository、history API、frontend message history / file card

## 1. 阶段目标

把上传、摘要回填、删除与 conversation history 对齐：上传成功必须同时拥有原始文件、DB resource、`file_upload` message 和最新 `index.md`；历史 API 和前端可展示文件上传卡片，并在 deleted 时明确不可复用。

## 2. 范围

### In scope

- 上传成功链路接入 `upsert_file_upload_message()`。
- 摘要生成 ready / failed 后幂等更新同一条 file_upload message。
- 删除文件时标记 resource deleted、标记 file_upload message deleted、重写 `index.md`。
- `index.md` 重写失败时 fail closed 或记录 durable repair marker。
- 历史 API 返回 `message_type`、`metadata`、`updated_at`。
- 前端 history 恢复保留 `message_type=file_upload` 的 public system message，并渲染文件上传卡片。

### Out of scope

- 不实现 selector 强制模式。
- 不改变 `metadata.upload_ids` 显式绑定语义。
- 不 backfill 旧上传文件的 file_upload history。
- 不新增前端文件选择控件。

## 3. 上传成功强一致

上传 API 只有在原始文件、DB resource、`file_upload` message 和最新 `index.md` 均完成后，才能返回上传成功。

```text
POST /api/v1/conversations/uploads
  -> 校验权限 / 文件类型 / 大小
  -> 保存原始文件
  -> 构造 ConversationFileResource projection
  -> begin DB transaction
     -> 保存 ConversationFileResource
     -> upsert_file_upload_message()
     -> commit
  -> 从最新 DB resource 重写 index.md
  -> 返回 UploadFileResponse
```

失败补偿规则：

- 原始文件写入失败：不写 DB，不写 history，返回上传失败。
- DB transaction 失败：rollback resource + message，删除刚写入的文件目录和内存 upload record，返回上传失败。
- `index.md` 重写失败：当场从 DB authoritative resources 立即重建一次；若仍失败，回滚或标记删除本次新增 resource + file_upload message，删除刚写入的原始文件目录和内存 upload record，返回上传失败，并写 durable repair marker + audit。

### 3.1 Durable repair marker

`index.md` 是 DB 的投影，不是权限事实源。任何重写失败都必须由 DB 持久 repair marker 记录；audit event、日志或内存 flag 不能替代 marker。

repair marker 至少包含：

| 字段 | 要求 |
| --- | --- |
| `conversation_id` | 需要修复索引的 conversation。 |
| `repair_kind` | 固定为 `conversation_file_index`。 |
| `status` | `pending | repairing | resolved | failed`。 |
| `reason_code` | 写入失败、权限异常、IO 异常、并发冲突等稳定原因码。 |
| `affected_upload_ids` | 本次上传 / 删除影响的 upload_id 列表，可为空但字段必须存在。 |
| `attempt_count` / `next_retry_at` | 后台退避重试调度信息。 |
| `created_at` / `updated_at` / `resolved_at` | 生命周期时间。 |

repair 触发时机：

1. **当场重试一次**：上传 / 删除路径发现 `index.md` 重写失败后，立即从 DB 全量 active/deleted resources 重建一次，不从旧 `index.md` 增量修。
2. **后台退避重试**：仍失败时写 `pending` marker，后台 repair worker / runtime task 按退避策略重试，例如 5 秒、30 秒、2 分钟。
3. **下次访问懒修复**：后续上传、删除、列文件、提交消息或 selector 访问该 conversation 时，如果发现 `pending` marker，应先尝试 repair；失败时继续以 DB 为事实源，但不得使用旧 `index.md` 做自动选择依据。

selector / prompt 约束：

- repair pending 时，selector candidates 只能来自 DB `ConversationFileResource` active resources。
- repair pending 时，不得把旧 `index.md` 作为 active 文件可用性依据。
- repair 成功后 marker 更新为 `resolved`，并记录对应 audit event。

## 4. 摘要回填

如果上传时 `description_summary` 为空，仍插入 `file_upload` message，并记录 `description_status=pending` 或 `unavailable`。摘要后续 ready / failed 时必须 upsert 同一条 message：

- 不新增重复 message。
- 不改变 `message_id` / `created_at`。
- 只更新 `content` / `metadata` / `updated_at`。
- metadata 仍按 canonical replacement 投影。

## 5. 删除文件

删除流程必须同时处理 resource、index、history 和 repair：

```text
DELETE /api/v1/conversations/uploads/{upload_id}
  -> 校验 conversation/user/status
  -> mark ConversationFileResource deleted
  -> mark_file_upload_message_deleted()
  -> 重写 index.md
  -> 物理删除本地资源目录或按既有删除策略清理
```

若 `index.md` 重写失败，后端必须保留 DB deleted 事实，记录 durable repair marker 并自动修复；repair 完成前，后续 selector 阶段不得基于旧 index 认定 deleted 文件仍可用。删除 API 只有在 deleted 事实已持久化且 repair marker / audit 已写入后，才可返回删除事实成功。

## 6. 历史 API

`GET /api/v1/conversations/{conversation_id}/messages` 在同一个 `messages[]` 序列中返回普通 public chat messages 和 `file_upload` messages，按 `created_at` 排序。

响应字段向后兼容扩展：

- `message_type`
- `metadata`
- `updated_at`

历史 API 必须执行 public message type allowlist：默认只返回 `chat` 与 `file_upload`；不得因为 `file_upload` 使用 `role=system` 就暴露其他 internal system message。

## 7. 前端展示

前端必须：

- 类型增加 `message_type?: "chat" | "file_upload" | string` 与 `metadata?: Record<string, unknown>`。
- `messageFromHistory()` 保留 `message_type=file_upload`，即使其 `role=system`。
- 其他 `role=system` 且不在 public allowlist 的消息继续隐藏。
- `message_type=file_upload` 渲染为文件上传卡片，不渲染为普通用户气泡。
- 卡片展示 `filename`、`upload_id`、`description_summary`、`description_status`、`file_status`。
- `description_status=pending` 时显示“文件摘要生成中”或只展示文件名 / upload_id。
- `description_status=failed` 时展示文件名 / upload_id，并提示摘要不可用。
- `file_status=deleted` 时显示“文件已删除 / 不可再用于任务”。
- 展示字段从 metadata 读取，不解析 content。
- 不展示路径、`storage_key`、原始内容或 base64 内容。

## 8. 审计事件

本阶段至少记录：

```text
conversation_file.file_upload_message_upserted
conversation_file.file_upload_message_marked_deleted
conversation_file.file_upload_index_repair_required
```

事件不得包含完整文件正文、`storage_key`、本地路径、secret 或 provider raw payload。

## 9. 测试计划

| 测试 | 断言 |
| --- | --- |
| 上传成功返回后 history 可读 | 同一 conversation history 立即返回 `message_type=file_upload`。 |
| 上传 DB 失败补偿 | 无 resource、无 message、无残留文件目录。 |
| 上传 index 写失败 fail closed | 当场重试仍失败时不返回上传成功；补偿本次新增 resource / message / 文件目录，并写 repair marker + audit。 |
| repair marker 生命周期 | 当场重试失败后写 pending marker；后台 / 懒修复成功后标记 resolved；selector repair pending 时不信旧 index。 |
| 摘要回填 | 更新同一条 message，不改变 `created_at`。 |
| 删除文件 | resource deleted、file_upload metadata deleted、index 重写、前端显示不可复用。 |
| 历史 API allowlist | internal system message 不返回。 |
| 前端卡片 | file_upload 卡片展示字段正确，隐藏禁止字段。 |

推荐命令：

```bash
python -m pytest tests/api/test_uploads.py
python -m pytest tests/api/test_route_contract.py
cd frontend && npm test -- --run
cd frontend && npm run typecheck
```

## 10. 阶段验收

- 新上传文件成功后 history 中存在稳定 file_upload message。
- 摘要回填、删除和 history 展示幂等可靠。
- 任一强一致步骤失败时不产生“上传成功但 history/index 不完整”的状态。
- 前端能展示 active / pending / failed / deleted 文件上传卡片。
