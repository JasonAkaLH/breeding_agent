# 历史消息可展示 Artifact 恢复设计

## 状态

- 状态：已复审，进入实施计划输入。
- 决策：采用后端历史消息接口返回 display-only artifacts 的方案。
- DB schema：不变更。
- 依赖：不新增。

## 问题陈述

当前任务实时完成时，前端调用 `/api/v1/tasks/{task_id}/artifacts`，把 SQLQuery `filtered_query_result` / `query_result_preview` 解析为表格卡片。但历史消息恢复走 `/api/v1/conversations/{conversation_id}/messages`，该接口当前只返回 `message` 文本，不返回 task artifacts。对于恢复中的任务，前端在加载 artifacts 后又可能刷新历史消息，从而用纯文本历史消息覆盖刚挂上的 artifact 卡片。

用户可见结果已经持久化在状态库 `artifact` 表中；缺口是历史消息 API 和前端历史恢复没有把可展示 artifact 重新挂回 assistant message。

## 用户、干系人和受影响系统

| 对象 | 影响 |
| --- | --- |
| 终端用户 | 刷新页面、重新登录、切换会话后，应继续看到 SQLQuery 表格、文件下载、OCR 原文等结果卡片。 |
| 前端业务对话台 | 历史消息恢复需要从 `MessageResponse.artifacts` 构建 `artifactDisplays`。 |
| API runtime | `/api/v1/conversations/{conversation_id}/messages` 需要补充每条 assistant message 的 display artifacts。 |
| 状态存储 | 继续使用现有 `artifact` 表和 `list_artifacts_for_conversation` 查询；不新增表和字段。 |
| Artifact 下载链路 | 文件 artifact 仍通过 `/api/v1/artifacts/{artifact_id}/download` 下载，不把文件内容嵌入历史消息。 |

## 当前代码证据

| 证据 | 说明 |
| --- | --- |
| `src/api/routes/conversations.py:94-115` | 历史消息接口当前只构造 `MessageResponse` 文本字段，没有 artifacts。 |
| `src/api/dto.py:255-263` | `MessageResponse` 当前没有 `artifacts` 字段。 |
| `src/api/routes/tasks.py:329-438` | task artifact 接口已有 `_artifact_response` 和文件 active 过滤逻辑。 |
| `src/core/contracts.py:209-211` | StoragePort 已有 `list_artifacts_for_task` 和 `list_artifacts_for_conversation`，可避免新增存储接口。 |
| `frontend/src/App.tsx:617-620` | 历史消息恢复只调用 `messageFromHistory()`。 |
| `frontend/src/App.tsx:1511-1544` | 任务完成加载 artifacts 后，恢复任务路径可能再次加载历史消息并覆盖当前消息。 |
| `frontend/src/App.tsx:2679-2690` | `messageFromHistory()` 当前只保留文本和完成状态，不挂 artifactDisplays。 |
| `frontend/src/domain/artifacts.ts:51-182` | 前端已有统一 artifact display parser，支持 SQLQuery、文件、OCR。 |

## 目标

- 历史对话、刷新页面、切换会话后，必须恢复用户可见 artifact 卡片。
- SQLQuery 表格结果必须在历史 assistant message 中继续显示表格卡片。
- 文件 artifact 在仍 active 且可下载时必须继续显示下载卡片。
- OCR 原文类展示 artifact 必须在历史消息中继续显示对应卡片。
- `/api/v1/tasks/{task_id}/artifacts` 必须保持兼容，不破坏实时任务完成加载。
- 历史消息接口必须只返回 display-only artifacts，不把内部审计 / 调试 artifact 暴露成聊天气泡卡片。

## 非目标

- 不把所有 artifact 原样展示到历史气泡。
- 不把 `generated_sql`、`guard_report`、`schema_context_snapshot`、`intent_summary` 作为默认 UI 卡片展示。
- 不改变 artifact 持久化位置；SQLQuery JSON artifact 仍保存在状态库 `artifact.storage_ref`。
- 不新增文件存储迁移、DB 表、DB 字段或状态迁移。
- 不把文件内容或原始上传文件内容嵌入 history message response。
- 不改变 conversation message 的持久化格式；artifact 仍作为 task artifact 独立持久化。

## 方案

增强 `/api/v1/conversations/{conversation_id}/messages`：每条历史 `MessageResponse` 增加 `artifacts` 字段，字段内容为该 assistant message 对应 task 的可展示 artifacts。前端历史恢复复用现有 `parseCapabilityArtifactDisplays`，实时完成和历史恢复使用同一套展示规则。

## 后端设计

### API DTO

`MessageResponse` 新增字段：

```python
artifacts: list[ArtifactResponse] = []
```

兼容性要求：

- 新增字段不改变现有字段含义。
- user message、无 task_id 的 message、无可展示 artifact 的 message 必须返回空数组。
- 旧前端若忽略新增字段，应继续可用。

### Artifact 查询策略

历史消息接口必须避免每条 message 单独查询 task artifacts。

执行顺序：

1. 调用 `runtime.sync_assistant_history_messages(conversation_id)`，保持现有历史 assistant message 同步语义。
2. 调用 `runtime.storage.list_messages_for_conversation(conversation_id)` 读取 messages。
3. 从 assistant messages 收集非空 `task_id`。
4. 若 task id 集合非空，调用既有 `runtime.storage.list_artifacts_for_conversation(conversation_id)` 一次性读取该会话 artifacts。
5. 在 API 层按 task id 分组，并只保留当前 messages 涉及的 task ids。
6. 对每个 assistant message 按 display artifact 规则过滤并转为 `ArtifactResponse`。

本轮不新增 StoragePort 方法。这样可复用 SQLite / PostgreSQL 现有同构能力，避免扩大 storage contract、runtime sidecar contract 和 fake storage 改动面。

### Artifact display 过滤规则

历史消息只返回 display artifacts。

必须返回：

| 类别 | 条件 |
| --- | --- |
| SQLQuery filtered result | JSON payload 中 `artifact_role == "filtered_query_result"`，或 artifact id 包含 `filtered_query_result`。 |
| SQLQuery query preview | JSON payload 中 `artifact_role == "query_result_preview"`，或 artifact id 包含 `query_result_preview`。 |
| OCR raw text | JSON payload 中 `artifact_role == "ocr_raw_text"`，且 payload 含前端可解析的 raw text/text/markdown。 |
| 文件 artifact | `artifact_type == file` 且 `is_active_skill_output_file(parse_file_storage_ref(storage_ref))` 为真。 |

必须排除：

| 类别 | 排除原因 |
| --- | --- |
| main-agent text artifact | `message.content` 已承载最终回答，重复返回会混淆前端 fallback text。 |
| `generated_sql` | 内部执行细节，不作为默认用户结果卡片。 |
| `guard_report` | 内部安全审计，不作为默认用户结果卡片。 |
| `schema_context_snapshot` | 内部 schema 上下文，不作为默认用户结果卡片。 |
| `intent_summary` | 内部路由解释，不作为默认用户结果卡片。 |
| 未知 JSON / summary artifact | 默认 deny，避免误展示内部或未来 artifact。 |
| JSON 解析失败的非文件 artifact | 无法确认 display role，默认不返回。 |

实现应把 display 过滤 helper 放在 API routes 可复用位置，供 conversation messages 和后续需要 display-only artifacts 的接口共享。`/tasks/{task_id}/artifacts` 继续使用现有“返回 task artifacts（文件需 active）”语义，不改成 display-only。

### ArtifactResponse 转换

文件 artifact 必须复用与 task artifact 下载链路一致的转换语义：

- `storage_ref` 返回空字符串。
- 返回 `filename`、`mime_type`、`size_bytes`、`sha256`、`download_url`、`source_file_count`、`archive_format`、`retention_status`。
- `download_url` 仍为 `/api/v1/artifacts/{artifact_id}/download`。

非文件 display artifact 返回原 `storage_ref`，供前端 parser 解析 JSON 表格或 OCR 内容。

## 前端设计

### API type

`MessageResponse` 增加：

```ts
artifacts?: ArtifactResponse[]
```

字段在 TypeScript 中保持可选，兼容旧 mock 或旧后端响应。

### 历史消息转换

`messageFromHistory()` 对 assistant message 执行：

```ts
const artifactDisplays = parseCapabilityArtifactDisplays(message.artifacts ?? [])
```

并写入：

```ts
artifactDisplays: artifactDisplays.length > 0 ? artifactDisplays : undefined
```

要求：

- user message 不解析 artifacts。
- assistant 历史 message 的 `finalContentLoaded` / `replyCompleted` 现有语义保持不变。
- 实时完成和历史恢复必须共用 `parseCapabilityArtifactDisplays`，不复制 SQLQuery / file / OCR 解析规则。

### 覆盖竞态修复

任务完成时 `loadArtifacts()` 若随后触发 `loadConversationMessages()`，历史消息已带 display artifacts，因此不会再覆盖丢卡片。前端回归测试必须锁定该 restored-task 完成路径。

## 安全、隐私和权限

- 历史 messages route 已调用 `require_conversation_owner`，只允许会话 owner 读取该会话消息和关联 display artifacts。
- 文件下载仍由 `/api/v1/artifacts/{artifact_id}/download` 执行 `require_task_owner` 和 active file 校验。
- 历史接口不得返回内部 guard token；并且默认不返回 guard report。
- SQLQuery 表格 artifact 是用户查询结果，可恢复展示；generated SQL 默认不展示。
- 文件 artifact 的 `storage_ref` 对前端仍为空，只返回下载元数据和 download URL。
- 原始上传文件内容不得通过 message artifacts 返回。

## 非功能要求

| 维度 | 要求 |
| --- | --- |
| 性能 | 历史消息接口不得按 message 数产生 N+1 artifact 查询；每次历史加载最多额外一次 conversation artifact 查询。 |
| 可靠性 | artifact JSON 解析失败时必须跳过该 artifact，不得导致整个历史消息接口失败。 |
| 兼容性 | `/tasks/{task_id}/artifacts`、artifact download、现有 message 字段保持兼容。 |
| 安全 | display artifact 过滤必须默认 deny；未知 artifact 不展示。 |
| 可维护性 | 后端 display 过滤和 ArtifactResponse 转换应有单一 helper，避免 conversation route 与 task route 复制不一致。 |
| 可观测性 | 本轮不新增事件；通过 API/前端测试验证恢复行为。若后续发现 payload 过大，再单独设计分页或 artifact projection。 |

## 边界和失败模式

| 场景 | 预期行为 |
| --- | --- |
| assistant message 没有 task_id | 返回 `artifacts: []`。 |
| task 没有 display artifact | 返回 `artifacts: []`，前端只显示文本。 |
| artifact JSON 解析失败 | 跳过该 artifact；接口仍返回其它消息和 artifacts。 |
| 文件 artifact 已过期或 inactive | 不返回该文件 artifact。 |
| artifact 属于同会话但不属于当前 message task | 不挂到该 message。 |
| 同一 task 同时有 `filtered_query_result` 和 `query_result_preview` | 两者都可返回；前端 parser 优先显示 filtered result。 |
| 历史接口返回旧格式无 `artifacts` | 前端按空数组处理。 |

## 测试计划

### 后端

| 测试层 | 建议文件 | 验证点 |
| --- | --- | --- |
| API | `tests/api/test_conversation_messages.py` 或现有 conversations route 测试文件 | 历史 assistant message 返回 SQLQuery `filtered_query_result` display artifact。 |
| API | 同上 | `generated_sql`、`guard_report`、`schema_context_snapshot`、`intent_summary` 不返回。 |
| API | 同上 | active file artifact 返回 download metadata；inactive / expired file 不返回。 |
| API | 同上 | user message 和无 task_id assistant message 返回空 artifacts。 |
| API | 同上 | malformed JSON artifact 被跳过且接口 200。 |
| Storage/API integration | 现有 SQLite / PostgreSQL storage 相关测试 | 确认复用 `list_artifacts_for_conversation` 不改变排序和删除清理行为。 |

### 前端

| 测试层 | 建议文件 | 验证点 |
| --- | --- | --- |
| Unit | `frontend/src/domain/artifacts.test.ts` | 现有 parser 继续识别 history artifacts。 |
| App | `frontend/src/App.test.tsx` | `messageFromHistory()` 从 history `artifacts` 恢复 SQLQuery data_query display。 |
| App | `frontend/src/App.test.tsx` | restored task 完成后，`loadArtifacts()` 后再 `loadConversationMessages()` 不丢 SQLQuery 卡片。 |
| App | `frontend/src/App.test.tsx` | 历史 file artifact 显示下载卡片。 |
| App | `frontend/src/App.test.tsx` | 内部 artifacts 不产生卡片。 |

### 建议验证命令

```bash
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_*.py'
cd frontend && npm test -- --run
cd frontend && npm run build
```

若实现只触及本功能相关文件，可先运行更窄的新增/修改测试，再运行上述分层回归。

## 验收标准

| 编号 | 验收项 |
| --- | --- |
| AC1 | SQLQuery 新任务完成后仍显示表格卡片。 |
| AC2 | 刷新页面后，同一历史 assistant message 仍显示 SQLQuery 表格卡片。 |
| AC3 | 切换会话再返回，表格卡片仍显示。 |
| AC4 | 文件 artifact 历史恢复后仍显示下载卡片，且下载仍走 `/api/v1/artifacts/{artifact_id}/download`。 |
| AC5 | 内部 SQLQuery artifacts 不显示为聊天卡片，也不通过 history message artifacts 返回。 |
| AC6 | `/api/v1/tasks/{task_id}/artifacts` 行为保持兼容。 |
| AC7 | 无 DB schema 变更，无新增依赖。 |
| AC8 | malformed artifact 不影响历史消息接口整体返回。 |

## 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| 历史消息响应变大 | 只返回 display artifacts，文件不内嵌内容；继续依赖 SQLQuery 已有 row/token 裁剪。 |
| 内部 artifact 被误展示 | display 过滤默认 deny，测试覆盖内部 SQLQuery artifacts 排除。 |
| conversation route 与 task artifact route 转换逻辑分叉 | 抽出或复用 ArtifactResponse helper；task route 保持全量 task artifacts 语义，history route 使用 display-only filter。 |
| 旧 mock 缺少 `artifacts` 导致前端测试失败 | 前端类型设为可选，解析时使用 `message.artifacts ?? []`。 |

## 明确假设

- SQLQuery 表格结果 artifact 是允许恢复展示的用户结果；generated SQL 和 guard/schema/intent artifacts 不是默认用户结果。
- 当前会话历史一次性加载全部 messages；本设计不引入历史分页。
- 当前 `list_artifacts_for_conversation` 的返回规模可支撑本阶段历史恢复；如果未来长会话 artifact 数量导致响应过大，应另行设计 task-id 批量查询或 display projection。
