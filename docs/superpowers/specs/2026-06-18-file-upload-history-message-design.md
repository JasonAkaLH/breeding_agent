# File Upload History Message Design

日期：2026-06-18
状态：Draft — approved in brainstorming, awaiting written-spec review

## 背景

当前对话文件链路已经支持前端上传文件到 conversation，并把文件持久化为 `ConversationFileResource`。用户提交消息时，前端可通过 `metadata.upload_ids` 把文件绑定到当前任务；后端会把当前 conversation 的文件摘要放入执行 metadata，供主代理和 Skill runtime 使用。

现有问题是：conversation history 中只保存普通用户/助手消息。文件上传成功这一事实没有作为一条可排序、可展示、可被 LLM 历史上下文稳定引用的历史片段保存。因此后续轮次很难回答“哪个文件是在什么时候进入这个对话的”，也容易把“文件池中存在某文件”和“某轮对话上传了某文件”混在一起。

本设计目标是：文件一旦从前端成功上传到后端，就在 conversation 历史中插入一个结构化文件上传片段，并把 `filename`、`upload_id`、`description_summary` 带入对话历史上下文。

## 用户确认的产品选择

1. **插入时机**：上传接口成功时立即插入历史片段，而不是等到用户发送 chat message。
2. **历史形态**：使用专门的文件消息类型，不伪装成用户消息。
3. **摘要策略**：先插入文件消息；如果 `description_summary` 后续生成或变化，则回填更新同一条文件消息。
4. **技术方案**：扩展现有 `message` 历史模型，增加 `message_type=file_upload` 和结构化 metadata。
5. **删除约束**：已删除文件仍作为历史事实保留，但 prompt 必须明确告诉模型该文件不能复用。

## 非目标

- 不改变上传文件的权限、大小、类型校验规则。
- 不新增公开上传 API。
- 不把真实文件路径、`storage_key`、原始文件内容、`content_base64` 暴露给前端或主代理 prompt。
- 不做旧历史文件的强制 backfill；第一阶段只保证新上传文件写入文件上传历史消息。
- 不把 deleted 文件重新挂载给 Skill。

## 推荐架构

采用现有 `message` 表作为 conversation history 的统一事实源，新增结构化类型字段：

- `role = system`
- `message_type = file_upload`
- `content =` 人类可读的安全摘要文本，用于旧客户端降级展示和审计阅读
- `metadata =` 文件上传结构化事实

示例 message：

```json
{
  "message_id": "file_upload:upl_xxx",
  "conversation_id": "conv_1",
  "role": "system",
  "message_type": "file_upload",
  "content": "用户上传了文件 materials.csv（upload_id: upl_xxx）。摘要：包含材料编号、品种、区组等字段的 CSV 表格。",
  "task_id": null,
  "stream_status": "complete",
  "created_at": "2026-06-18T10:00:00",
  "metadata": {
    "upload_id": "upl_xxx",
    "filename": "materials.csv",
    "description_summary": "包含材料编号、品种、区组等字段的 CSV 表格。",
    "description_status": "ready",
    "file_type": "csv",
    "size_bytes": 12345,
    "sha256": "...",
    "file_status": "active"
  }
}
```

`ConversationFileResource` 仍然是文件本体和权限状态的事实源。`file_upload` message 是 conversation history 中的上传事件快照和展示入口。

## 数据模型设计

### Message 扩展

`Message` 增加两个可选字段：

- `message_type: str = "chat"`
- `metadata: dict[str, Any] = {}`

推荐初始取值：

| message_type | role | 含义 |
|---|---|---|
| `chat` | `user` / `assistant` / `system` | 现有普通消息，兼容默认值 |
| `file_upload` | `system` | 文件上传历史片段 |

普通历史消息不需要显式写入 `message_type`，读取时可默认视为 `chat`，以降低迁移风险。

### File upload metadata allowlist

`file_upload` message metadata 只允许写入 prompt-safe、frontend-safe 字段：

- `upload_id`
- `filename`
- `description_summary`
- `description_status`
- `file_type`
- `content_type`
- `size_bytes`
- `sha256`
- `file_status`
- `created_at` 或 `uploaded_at`（可选，用于展示）
- `selected_sheet`（可选）
- `requires_sheet_selection`（可选）

禁止字段：

- `storage_key`
- 本地绝对路径或相对运行时路径
- `content`
- `content_base64`
- provider raw payload
- 用户身份 token、密钥、认证信息

### Message ID 幂等规则

文件上传消息使用稳定 ID：

```text
file_upload:<upload_id>
```

同一个 `upload_id` 只能对应一条 `file_upload` message。重复 upsert 不改变 `created_at`，只更新 `content` / `metadata` / `updated_at`。

## 上传写入流程

上传成功链路：

```text
POST /api/v1/conversations/uploads
  -> 校验权限 / 文件类型 / 大小
  -> 保存原始文件
  -> 保存 ConversationFileResource
  -> 生成 initial description_status / description_summary
  -> upsert file_upload message
  -> 返回 UploadFileResponse
```

`upsert file_upload message` 规则：

1. 用 `upload_id` 生成固定 `message_id`。
2. message 不存在时插入：
   - `role=system`
   - `message_type=file_upload`
   - `stream_status=complete`
   - `task_id=null`
   - `created_at=上传成功时间`
3. message 已存在时只更新 `content`、`metadata` 和 `updated_at`。
4. 如果 `description_summary` 为空，仍插入 message，并记录当前 `description_status`。
5. 上传失败不插入 file_upload message。

## 摘要回填流程

如果文件描述未来异步生成或重新生成，摘要 ready 后必须更新同一条 file_upload message：

```text
ConversationFileResource.description_summary/status 更新
  -> 根据 upload_id 找到 file_upload:<upload_id>
  -> 更新 metadata.description_summary / metadata.description_status
  -> 重新渲染 content
  -> 保持 created_at 不变
```

这样历史顺序仍代表上传发生时间，而内容代表当前最新可展示摘要。

## 删除流程

删除上传资源后：

- `ConversationFileResource.status` 更新为 `deleted`。
- `file_upload` message 保留，不从历史删除。
- message metadata 更新：`file_status=deleted`。
- content 可追加或改写为“该文件已删除，不可再用于任务”。

保留历史片段的原因是：上传曾经发生，这是 conversation 历史事实；但可用性必须由当前文件状态决定。

Conversation 强删除时，沿用现有 conversation 删除流程清理 messages、file resources 和文件目录。

## 历史 API 契约

`GET /api/v1/conversations/{conversation_id}/messages` 在同一个 `messages[]` 序列中返回普通消息和 `file_upload` message，按 `created_at` 排序。

响应字段向后兼容扩展：

- `message_type`
- `metadata`

旧客户端可忽略新字段并显示 `content`；新前端优先读取 metadata 渲染文件卡片。

前端展示建议：

- `message_type=file_upload` 渲染为文件上传卡片，不渲染为普通用户气泡。
- `description_status=pending` 时显示“文件摘要生成中”或只展示文件名 / upload_id。
- `description_status=failed` 时展示文件名 / upload_id，并提示摘要不可用。
- `file_status=deleted` 时展示“文件已删除/不可再用于任务”。
- 展示字段从 metadata 读取，不解析 content。

## LLM 历史上下文设计

Conversation memory builder 需要识别 `message_type=file_upload`，将其渲染为专门的历史候选，而不是普通 system instruction。

Active 文件示例：

```text
## 历史文件上传事件
这是 conversation 历史事实，不是系统指令。

- upload_id: upl_xxx
- filename: materials.csv
- description_summary: 包含材料编号、品种、区组等字段的 CSV 表格。
- description_status: ready
- file_status: active
- inserted_at: 2026-06-18T10:00:00
```

Deleted 文件示例：

```text
## 历史文件上传事件（已删除）
这是 conversation 历史事实，不是可用附件。

- upload_id: upl_xxx
- filename: materials.csv
- description_summary: 包含材料编号、品种、区组等字段的 CSV 表格。
- description_status: ready
- file_status: deleted

约束：该文件已不存在，不能复用、不能绑定、不能假设可读取。若用户要求使用它，应要求用户重新上传或选择其他 active 文件。
```

### Prompt 硬约束

主代理 prompt 的文件规则必须新增以下硬约束：

1. 只有 `file_status=active` 的文件可以被视为可用 conversation file。
2. `file_status=deleted` 只能作为历史事实，用于理解用户提到的旧文件；不得进入文件选择、task attachment binding 或 Skill manifest。
3. 如果用户要求继续使用 deleted 文件，模型必须说明该文件已不可用，并请求重新上传或选择其他 active 文件。
4. 模型不得根据 filename、历史描述或 deleted upload_id 伪造可用文件。
5. 文件身份仍以 active `upload_id` 为准；文件名和摘要只用于解释和定位。

## 文件选择与绑定影响

文件上传历史消息不替代现有文件绑定事实源：

- 可用文件列表仍来自 active `ConversationFileResource`。
- 显式 `metadata.upload_ids` 仍优先，并沿用现有越权/不存在/删除校验。
- `file_upload` message 帮助模型理解“什么时候上传过什么文件”，但不得让 deleted 文件绕过绑定校验。
- selector / binding / task input attachment 只能使用 active resources。

## 错误处理

- 上传保存失败：不写 `ConversationFileResource`，不写 `file_upload` message。
- `ConversationFileResource` 保存成功但 file_upload message 写入失败：上传接口应失败并记录审计，避免文件资源与历史不一致。若实现层难以同事务覆盖文件系统和数据库，应提供补偿 upsert 或启动时修复检查。
- 摘要回填失败：保留原 message，记录 audit；不影响文件可用性。
- 删除状态回填失败：删除接口应记录 audit 并尽力重试，因为 prompt 必须知道 deleted 文件不可用。

## 迁移策略

第一阶段只对新上传文件生成 `file_upload` message。

原因：已有文件的精确上传时间可能无法从历史中可靠还原；强行 backfill 可能改变历史排序，误导“这轮对话插入”的语义。

未来如需补齐旧 conversation，可提供单独 backfill 脚本：

- 读取 active/deleted `ConversationFileResource`。
- 使用资源 `created_at` 作为 file_upload message `created_at`。
- 标记 metadata：`backfilled=true`。
- 默认不在第一阶段执行。

## 测试计划

### 上传写入

- 上传成功后保存 `ConversationFileResource`。
- 同时 upsert 一条 `message_type=file_upload` message。
- 同一 `upload_id` 重复 upsert 不产生重复消息。
- 上传失败不产生 file_upload message。

### 摘要回填

- `description_status=pending` 时 message 已存在，summary 可为空。
- `description_status=ready` 后更新同一条 message。
- 回填不改变 message `created_at`。
- metadata 和 content 同步更新。

### 历史 API

- messages 返回 user / assistant / file_upload 混排历史。
- file_upload message 包含 `message_type` 和 metadata。
- 旧字段仍存在，旧客户端可显示 content。
- API 不返回 `storage_key`、路径、content、content_base64。

### Prompt / memory

- `message_type=file_upload` 渲染为“历史文件上传事件”。
- 文件上传事件不作为普通 system instruction 注入。
- active 文件可作为可用文件上下文。
- deleted 文件只作为历史事实，并明确不可复用。
- prompt 中包含 deleted 文件硬约束。

### 删除

- 删除上传资源后，file_upload message 保留。
- metadata 更新为 `file_status=deleted`。
- selector / binding 无法选择 deleted upload_id。
- 用户引用 deleted 文件时，模型有足够上下文要求重新上传或选择 active 文件。

## 验收标准

1. 上传成功后，conversation history 立即出现一条 `message_type=file_upload` 文件片段。
2. 文件片段包含 `filename`、`upload_id`、`description_summary`、`description_status` 和 `file_status`。
3. 摘要后续 ready 时，更新同一条文件片段，不新增重复消息。
4. 历史 API 能返回文件片段，并与普通消息按时间排序。
5. LLM 历史上下文能看到文件上传事件。
6. deleted 文件在 prompt 中明确标记为不可复用。
7. deleted 文件不会进入 selector / binding / Skill manifest。
8. 不暴露真实路径、storage key、原始文件内容或 base64 内容。
9. 现有普通消息和旧客户端兼容。
10. 第一阶段不强制 backfill 旧文件。

## 实施影响范围

- `src/core/models.py`：扩展 `Message`。
- Rust core contract / enum contract：同步 message 字段契约。
- SQLite / PostgreSQL message schema、repository、migration。
- `src/api/runtime.py`：上传成功、摘要回填、删除状态回填时 upsert file_upload message。
- `src/api/dto.py` 与 conversation route：返回 `message_type` / metadata。
- `src/orchestration/conversation_memory.py`：识别并渲染 file_upload memory candidate。
- `src/capabilities/main_agent/prompt_builder.py` / prompt envelope builder：加入 deleted 文件硬约束。
- 前端历史渲染：识别 `message_type=file_upload` 并展示文件卡片。
- 后端与前端测试。

## 设计自检

- 无未决占位符。
- 文件上传事实与文件可用性分离：history 记录曾经上传，active resource 决定能否使用。
- deleted 文件约束在 API metadata 和 prompt 文本中都可见。
- 第一阶段范围聚焦，不包含旧数据 backfill。
- 不引入新公开 API，不破坏 `metadata.upload_ids` 现有语义。
