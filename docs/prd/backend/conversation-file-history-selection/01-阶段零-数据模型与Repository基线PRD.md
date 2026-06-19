# 阶段零：数据模型与 Repository 基线 PRD

- **编号**：后端 PRD 21-Phase 0
- **日期**：2026-06-19
- **状态**：待实施
- **上游依赖**：`docs/prd/backend/20-对话文件本地资源文件系统PRD.md`
- **下游阶段**：阶段一上传删除强一致、阶段二 memory 安全、阶段四 selector 绑定
- **目标模块**：`src/core/models.py`、`src/storage/`、`src/api/dto.py`、history API repository tests

## 1. 阶段目标

建立后续 `file_upload` history 与 selector provenance 所需的兼容数据基线：

1. `Message` 模型和历史 DTO 支持 `message_type`、`metadata`、`updated_at`，旧数据默认仍视为普通 chat。
2. Storage 提供专用 file upload history repository 契约，避免业务代码用通用 `save_message()` 拼装 public system message。
3. 建立 file_upload metadata canonical projection 和禁止字段测试，后续阶段只复用该投影。
4. 建立 public message type allowlist，确保只暴露 `chat` 与 `file_upload`，不泛化暴露 internal system message。

## 2. 范围

### In scope

- `Message` / `MessageResponse` 兼容字段扩展。
- 旧 message row 读取时默认 `message_type=chat`、`metadata={}`。
- file upload projection DTO / dataclass。
- `upsert_file_upload_message()` 与 `mark_file_upload_message_deleted()` repository 契约及 tests-first 覆盖。
- history API public allowlist 的 repository / API 层测试。

### Out of scope

- 本阶段不把上传流程接入 `file_upload` message。
- 本阶段不改 selector、memory 渲染、前端文件卡片或 Skill contract parser。
- 本阶段不 backfill 旧上传文件历史。

## 3. 数据模型要求

### 3.1 Message 扩展

`Message` 增加字段：

```python
message_type: str = "chat"
metadata: dict[str, Any] = {}
updated_at: datetime | None = None
```

初始类型：

| message_type | role | 含义 | public history 默认返回 |
| --- | --- | --- | --- |
| `chat` | `user` / `assistant` / `system` | 现有普通消息；旧数据读取时默认视为 `chat` | user/assistant chat 返回；internal system chat 不返回 |
| `file_upload` | `system` | 文件上传历史片段 | 返回 |

### 3.2 file_upload message ID

文件上传消息使用稳定 ID：

```text
file_upload:<upload_id>
```

不得为同一 `upload_id` 创建多条 file_upload message。

### 3.3 metadata allowlist

必填 / 常用字段：

- `schema_version`
- `upload_id`
- `filename`
- `description_summary`
- `description_status`
- `file_type`
- `content_type`
- `size_bytes`
- `sha256`
- `file_status`
- `uploaded_at`

可选字段：

- `selected_sheet`
- `requires_sheet_selection`
- `row_count`
- `column_count`
- `sheet_names`
- `description_updated_at`

禁止字段：

- `storage_key`
- 本地绝对路径或相对运行时路径
- `mount_path`
- `content`
- `content_base64`
- provider raw payload
- 用户身份 token、密钥、认证信息

metadata 必须由后端从 `ConversationFileResource` 构造 canonical replacement，不能透传用户 metadata，也不能任意 merge。

## 4. Repository 契约

Storage contract 必须提供专用方法或等价 repository command：

```python
async def upsert_file_upload_message(
    projection: FileUploadMessageProjection,
    *,
    now: datetime,
) -> Message: ...

async def mark_file_upload_message_deleted(
    conversation_id: str,
    upload_id: str,
    *,
    deleted_at: datetime,
) -> Message | None: ...
```

`upsert_file_upload_message()` 必须保证：

1. `message_id` 固定为 `file_upload:<upload_id>`。
2. 首次插入才设置 `created_at`，优先使用 `ConversationFileResource.created_at`，缺失时使用 `now`。
3. 后续回填只更新 `content`、`metadata`、`updated_at`，不得改变 `message_id`、`conversation_id`、`created_at`。
4. 若同 ID 已存在但 `message_type != file_upload` 或 `conversation_id` 不一致，必须失败并记录 audit，不能覆盖普通 chat message。
5. 强制写入 `role=system`、`message_type=file_upload`、`task_id=null`、`stream_status=complete`。
6. metadata 每次从当前 resource 重新投影完整安全字段。

`mark_file_upload_message_deleted()` 必须保证：

1. 已存在 file_upload message 时，保留 message，设置 `metadata.file_status=deleted`，重新渲染 `content`，保持 `created_at` 不变，并更新 `updated_at=deleted_at`。
2. message 不存在时，阶段零不自动创建 deleted 历史消息，避免给旧文件隐式 backfill；记录 audit 后 no-op。
3. 不得让 deleted message 回到 active 状态，除非未来另有明确恢复文件语义。

## 5. Public allowlist

历史读取必须执行 public message type allowlist：

- 返回普通 user/assistant `chat` message。
- 返回 `message_type=file_upload` 的 system message。
- 默认隐藏其他 `role=system` 或未知 internal message。
- 未知 `message_type` 默认不作为 public system message 返回，除非未来显式加入 allowlist。

## 6. 测试计划

新增或调整 tests-first 覆盖：

| 测试 | 断言 |
| --- | --- |
| Message schema 兼容旧 row | 旧数据读取得到 `message_type=chat`、`metadata={}`，历史响应向后兼容。 |
| upsert 首次插入 | 固定 ID、system/file_upload、created_at 使用 resource 时间、metadata 只含 allowlist。 |
| upsert 幂等更新 | 摘要回填更新同一条 message，`created_at` 不变，不新增重复消息。 |
| upsert 冲突保护 | 同 ID 非 file_upload 或 conversation 不一致时 fail closed。 |
| mark deleted | 保留 message，设置 `file_status=deleted`，更新时间，不改变 `created_at`。 |
| mark deleted no-op | 旧文件无 message 时不 backfill，只记录 audit。 |
| public allowlist | history 不暴露 internal system chat，只返回 `chat` 与 `file_upload`。 |
| metadata 禁止字段 | projection 永不包含路径、storage_key、正文、base64、secret。 |

推荐命令：

```bash
python -m pytest tests/api/test_route_contract.py
python -m pytest tests/api/test_uploads.py -k "message or history or file_upload"
```

## 7. 阶段验收

- `Message` / DTO 字段兼容旧数据。
- repository contract tests 全绿。
- file_upload projection 安全字段和禁止字段被自动化测试锁定。
- public allowlist 不暴露 internal system message。
- 上传主路径行为未改变；本阶段不声明 file_upload history 已上线。
