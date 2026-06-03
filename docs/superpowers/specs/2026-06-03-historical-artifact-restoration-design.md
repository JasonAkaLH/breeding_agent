# 历史消息可展示 Artifact 恢复设计

## 背景

当前任务实时完成时，前端会调用 `/api/v1/tasks/{task_id}/artifacts`，把 SQLQuery `filtered_query_result` / `query_result_preview` 解析为表格卡片。但历史消息恢复走 `/api/v1/conversations/{conversation_id}/messages`，该接口只返回 `message` 文本，不返回 task artifacts。对于恢复中的任务，前端在加载 artifacts 后又可能刷新历史消息，从而用纯文本历史消息覆盖刚挂上的 artifact 卡片。

## 目标

- 历史对话、刷新页面、切换会话后，能恢复用户可见 artifact 卡片。
- SQLQuery 表格结果在历史消息中继续显示表格卡片。
- 文件 artifact 在仍 active 且可下载时继续显示下载卡片。
- OCR 原文类展示 artifact 在历史消息中继续显示对应卡片。
- 保持 `/api/v1/tasks/{task_id}/artifacts` 兼容，不破坏实时任务完成加载。
- 不把内部审计 / 调试 artifact 暴露成聊天气泡卡片。

## 非目标

- 不把所有 artifact 原样展示到历史气泡。
- 不把 SQL guard report、generated SQL、schema snapshot、intent summary 作为默认 UI 卡片展示。
- 不改变 artifact 持久化位置；SQLQuery JSON artifact 仍保存在状态库 `artifact.storage_ref`。
- 不新增文件存储迁移或 DB schema 变更。
- 不把文件内容嵌入 history message response；文件仍通过平台 download URL 获取。

## 推荐方案

采用方案 A：增强历史消息接口，让每条历史 `MessageResponse` 携带该消息对应 task 的“可展示 artifacts”。前端历史恢复复用现有 `parseCapabilityArtifactDisplays` 解析逻辑。

## 后端设计

### API DTO

`MessageResponse` 新增字段：

```python
artifacts: list[ArtifactResponse] = []
```

兼容性：新增字段对旧客户端是向后兼容的；不改变已有字段含义。

### Artifact 选择规则

历史消息只返回 display artifacts：

- SQLQuery 表格类：
  - `artifact_role == "filtered_query_result"`
  - `artifact_role == "query_result_preview"`
  - 或 artifact id 包含上述稳定角色名
- OCR 原文类：
  - `artifact_role == "ocr_raw_text"`
- 文件类：
  - `artifact_type == file`
  - 且 `is_active_skill_output_file(parse_file_storage_ref(...))` 为真

默认排除：

- `generated_sql`
- `guard_report`
- `schema_context_snapshot`
- `intent_summary`
- main-agent text artifact
- 其他未知 JSON / summary artifact

过滤应以 artifact payload metadata 为主，以 artifact id 稳定角色名为兼容兜底。解析失败的 JSON artifact 不进入历史展示集合。

### 查询策略

为避免 N+1，历史消息接口应批量加载当前消息列表里所有 assistant task 的 artifacts：

1. 先读取 conversation messages。
2. 收集 `message.role == assistant` 且 `message.task_id` 非空的 task ids。
3. 一次性读取这些 task 的 artifacts。
4. 按 task id 分组、过滤 display artifacts、转为 `ArtifactResponse`。
5. user message 返回空 artifacts。

可先在 API 层用现有 `list_artifacts_for_task` 实现最小正确版本；如果测试或性能显示需要，再下沉为 StoragePort 批量接口。长期交付版本优先实现批量接口，SQLite / PostgreSQL 同构。

### StoragePort

新增只读方法：

```python
async def list_artifacts_for_tasks(self, task_ids: set[str]) -> dict[str, list[Artifact]]
```

约束：

- 空集合返回 `{}`。
- 只读，无副作用。
- 每个 task 内按 `created_at, artifact_id` 稳定排序。
- SQLite / PostgreSQL 行为一致。

## 前端设计

### API type

`MessageResponse` 增加：

```ts
artifacts?: ArtifactResponse[]
```

字段可选，兼容旧后端或测试 mock。

### 历史消息转换

`messageFromHistory()` 对 assistant message 执行：

```ts
const artifactDisplays = parseCapabilityArtifactDisplays(message.artifacts ?? [])
```

并写入：

```ts
artifactDisplays: artifactDisplays.length > 0 ? artifactDisplays : undefined
```

这样实时完成和历史恢复都复用同一 display parser / card 组件。

### 覆盖竞态修复

任务完成时 `loadArtifacts()` 若随后触发 `loadConversationMessages()`，历史消息已带 display artifacts，因此不会再覆盖丢卡片。测试需要覆盖这个路径。

## 安全与隐私

- 历史接口不得返回内部 guard token；现有 SQL Guard artifact 已不包含 token，本设计仍不展示 guard report。
- SQLQuery 表格 artifact 是用户查询结果，可恢复展示；generated SQL 默认不展示，避免把内部查询细节作为 UI 结果扩散。
- 文件 artifact 的 `storage_ref` 对前端仍为空，只返回下载元数据和 download URL。
- 不把原始上传文件内容挂到历史消息 artifacts 中。

## 测试计划

### 后端

- conversation messages response includes SQLQuery `filtered_query_result` artifact for assistant task。
- internal SQLQuery artifacts are excluded：`generated_sql`、`guard_report`、`schema_context_snapshot`、`intent_summary`。
- file artifact active 时返回 download metadata；inactive / expired 文件不返回。
- user message artifacts 为空。
- SQLite / PostgreSQL storage 批量 artifact 查询排序与空输入一致。

### 前端

- `messageFromHistory()` 能从 history `artifacts` 恢复 `data_query` display。
- 刷新 / 切换会话后 SQLQuery 表格卡片仍显示。
- restored task 完成后，`loadArtifacts()` 后再 `loadConversationMessages()` 不会丢失卡片。
- 文件 artifact 历史恢复后显示下载卡片。
- 内部 artifacts 不产生卡片。

## 验收标准

1. SQLQuery 新任务完成后显示表格卡片。
2. 刷新页面后，同一历史 assistant message 仍显示表格卡片。
3. 切换会话再返回，表格卡片仍显示。
4. 文件 artifact 历史恢复后仍显示下载卡片。
5. 内部 SQLQuery artifacts 不显示为聊天卡片。
6. `/api/v1/tasks/{task_id}/artifacts` 行为保持兼容。
7. 无 DB schema 变更，无新增依赖。

## 风险与缓解

- 风险：历史消息响应变大。缓解：只返回 display artifacts，排除内部 artifact；文件不内嵌内容。
- 风险：历史消息批量查询增加接口复杂度。缓解：用 StoragePort 单一批量方法隔离 SQLite / PostgreSQL 差异。
- 风险：未知 artifact 被错误展示。缓解：默认 deny，只有明确 display role / file active / OCR raw text 才展示。
