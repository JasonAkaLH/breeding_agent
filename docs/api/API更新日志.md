# API 更新日志

## 2026-06-24

### 更新摘要

本次扩展会话文件上传白名单，允许 TSV 表格作为 Skill 输入文件。接口路径、请求体结构和响应 schema 不变；TSV 归入现有 CSV-family 表格处理，响应仍使用 `file_type=csv`。

### `/api/v1/conversations/uploads` 与 `/api/v1/conversations/{conversation_id}/uploads`

- 上传白名单新增 `.tsv`、`text/tab-separated-values` 与 `text/tsv`。TSV 按 CSV-family 表格解析，preview 返回表头、行数、编码和 `normalized_content_type=text/csv`。
- TSV 的 `original_filename` 保留 `.tsv`，执行通道兼容字段 `skill_artifacts[].content` 使用归一化 CSV 文本；新 Skill 仍应优先通过 `resource_manifest_path` 读取 `files[].mount_path` 中的原始 TSV 文件副本。
- Prompt-safe response / `uploaded_artifacts` 继续不返回完整 `content`、`content_base64` 或原始行数据。

## 2026-06-19

### 前端接入结论

这次文件历史与自然语言选文件能力 **没有新增 API endpoint，也没有要求前端新增请求参数**。前端主要需要处理两件事：

1. 历史消息里可能出现 `message_type=file_upload` 的文件上传卡片。
2. 用户自然语言提到文件时，后端可能用现有 interrupt 追问，前端按原来的 interrupt 流程提交下一条输入即可。

### 变化清单

| 问题 | 结论 | 前端怎么做 |
|---|---|---|
| 是否新增接口 | 没有 | 继续使用现有上传、删除、历史、消息提交和 interrupt 接口。 |
| 请求参数是否变化 | 没有新增必填参数 | 显式引用文件仍放 `metadata.upload_ids`；sheet 选择仍放 `metadata.upload_sheet_selections`；回答 interrupt 仍放 `metadata.interrupt_id`。 |
| 响应是否变化 | 历史消息可能多一种公开 system message | `GET /api/v1/conversations/{conversation_id}/messages` 可能返回 `role=system` + `message_type=file_upload`。这类消息展示成文件上传卡片。 |
| 调用时机是否变化 | 上传 / 删除主流程不变 | 上传或删除后，继续刷新 uploads 列表；需要展示聊天历史时刷新 messages。 |
| 调用逻辑是否变化 | 文件选择可由后端自然语言处理 | 用户说“刚才那个文件 / materials.csv / 第一份表 / upl-xxxx”时，不需要前端新增点选控件；后端会自动选择或发起 interrupt。 |
| deleted 文件怎么处理 | 可展示，不可复用 | `metadata.file_status=deleted` 的 file_upload 卡片只做历史展示，不要重新放入发送附件或任务附件。 |

### 继续使用的接口

- 上传：`POST /api/v1/conversations/uploads`
- 文件列表：`GET /api/v1/conversations/{conversation_id}/uploads`
- 删除上传：`DELETE /api/v1/conversations/uploads`
- 读取历史：`GET /api/v1/conversations/{conversation_id}/messages`
- 提交新消息 / 回答 interrupt：`POST /api/v1/conversations/chat-messages`

### 前端只需要注意

- 只展示 `file_upload` metadata 中的安全字段：`filename`、`upload_id`、`description_summary`、`description_status`、`file_status`。
- 继续隐藏其他 internal system message。
- 文件选择澄清仍按普通 interrupt 展示 `question`，用户下一条输入继续提交到 `chat-messages`。
- 不要展示或依赖路径、`storage_key`、完整文件内容、`content_base64`、selector 审计细节。

## 2026-06-17

### 更新摘要

本次只调整前端文件附加体验和调用时机，API 契约不变：未新增 endpoint，未修改上传、列表、删除或消息提交接口的路径、method、必填参数、请求体结构和响应 schema。

### 上传文件与消息提交

- 外部系统仍可沿用既有接入方式：先调用 `POST /api/v1/conversations/uploads` 获取 `upload_id`，再在 `POST /api/v1/conversations/chat-messages` 的 `metadata.upload_ids` 中引用。
- 新前端选择或拖拽文件后会先保存在浏览器草稿状态，用户发送消息时才调用同一个上传 API；这是客户端调用时机变化，不是服务端 API 变化。
- 右侧文件面板只读取已经保存到后端的 conversation 文件资源，仍使用 `GET /api/v1/conversations/{conversation_id}/uploads` 和 `DELETE /api/v1/conversations/uploads`。

## 2026-06-16

### 更新摘要

对话上传文件已从临时上传升级为 conversation-scoped 本地文件资源。客户端现有上传、列表、删除和消息提交方式保持兼容；新增字段均可忽略。Skill 运行时会通过后端受控 workspace 挂载文件，客户端不接触本地真实路径。

### `/api/v1/conversations/uploads` 与 `/api/v1/conversations/{conversation_id}/uploads`

- 上传接口仍使用 `multipart/form-data` 的 `conversation_id` 与 `file` 字段；保留原响应字段。
- 上传成功后服务端会把原始文件保存到本地 conversation 文件目录，并维护该 conversation 的文件索引 `index.md`。
- `UploadFileResponse` 新增可选 `status` 与 `description_status`；旧客户端可忽略。
- 文件列表支持可选 `limit`、`cursor`、`include_deleted` 查询参数；默认仍只返回 active 文件。
- 删除单个 upload 时，服务端会标记 DB 状态并物理删除对应本地文件资源目录；删除响应仍为 `{ upload_id, deleted }`。
- 客户端仍通过消息 `metadata.upload_ids` 引用文件；不要提交或依赖服务端本地路径。

### Skill 文件输入提示

- 新 Skill 应通过运行时 payload 中的 `resource_manifest_path` 读取 `files[].mount_path`，由脚本操作 workspace 中的真实文件副本。
- `uploaded_artifacts[].content` / `content_base64` 仍保留用于旧 Skill 兼容，但不再是新 Skill 文件输入的主接口。

## 2026-06-09

### 更新摘要

本次更新重点影响客户端的 interrupt 续接、历史消息恢复、上传文件类型和模型选项传递。字段定义以 `/api-doc` 与 `/openapi.json` 为准；这里只记录需要客户端关注的 API 行为变化。

### `/api/v1/conversations/chat-messages`

- 同一端点同时处理普通新消息和 open interrupt 下的继续输入：目标会话存在 open interrupt 时优先作为 interrupt turn 处理，不创建新 task；没有 open interrupt 时按普通新消息创建 task。
- 回答 interrupt 时在 `metadata.interrupt_id` 指定目标 interrupt；只有一个 open interrupt 时可省略，存在多个 open interrupt 且未指定会返回 400。
- 上传引用继续放在 `metadata.upload_ids`；多 sheet 选择继续放在 `metadata.upload_sheet_selections`。客户端不要提交业务字段形 payload（例如 `design=...`、`ncols=...`）或内部 resume 字段。
- `client_message_id` 是本轮 interrupt turn 的幂等键；服务端响应中的 `answer_payload.client_request_id` 可用于前端/外部系统关联本次回答处理结果；同一 key 重复提交会返回同一份处理摘要，不会重复恢复任务。
- `MessageAcceptedResponse.action` 用于区分结果：
  - `task_accepted`：创建新任务。
  - `interrupt_resumed`：补参已接受并恢复原 task。
  - `interrupt_clarification_answer`：只回答了用户的追问或澄清，interrupt 仍保持 open。
  - `interrupt_mixed_processed`：同一条消息同时包含补参和追问/引导。
  - `interrupt_schema_switched`：当前 Skill 内切换了 input schema。
- 当响应包含 `assistant_message`、`answer_payload.will_resume=false` 或 `answer_payload.requires_confirmation=true` 时，客户端应继续展示 interrupt 输入态，并等待用户下一条回复；`will_resume=true` 表示补充信息已接受并恢复原任务。

### `/api/v1/tasks/interrupts`

- interrupt 列表仍用于读取当前 open interrupt；新的回答入口统一使用 `POST /api/v1/conversations/chat-messages`。
- v2 Skill 的 `Interrupt.required_fields` 只提供前端展示所需的 `_slot_collection_ref` 摘要；其中 slot collection 以 `collection_id + revision` 标识当前补槽状态版本，客户端不得修改、回传或把它当作可编辑参数表。
- `question` 是前端应展示给用户的追问文本；聊天历史保存用户原话，不应展示内部键值形式。

### `/api/v1/tasks/{task_id}/events`

- interrupt 正式恢复后，客户端继续订阅原 `task_id` 的 SSE，并以 `node.ready_to_resume`、`node.resuming` 和任务终态作为恢复/完成依据。
- `task.interrupt_answered`、节点恢复事件和任务终态只包含客户端安全字段；客户端只应依赖 `/api-doc` 声明的事件字段。

### `/api/v1/conversations/{conversation_id}/messages`

- `MessageResponse` 新增可选 `artifacts` 数组。读取历史消息时，assistant 消息可能带回可展示 artifact，用于 History Recall 场景恢复数据查询表格卡片、OCR 原文卡片和仍 active 的文件下载卡片。
- 实时任务展示仍以 SSE 和 `/api/v1/tasks/{task_id}/artifacts` 为主；`messages[].artifacts` 主要用于刷新或切换历史会话后的展示恢复。

### `/api/v1/conversations/uploads` 与 `/api/v1/conversations/{conversation_id}/uploads`

- 上传白名单新增 `.vcf` 与 `.vcf.gz`；响应 `file_type=vcf`。文本类上传继续返回 `file_type=text` 并在 preview 中提供 `char_count` / `line_count`。`.vcf.gz` 按复合扩展名识别，普通 `.gz` 不会仅因 `application/gzip` 被接受。
- VCF / VCF.GZ 按二进制文件处理，不做 CSV / JSON / Excel 预览解码；客户端只应依赖返回的文件元数据和后续 Skill 结果。

### `/api/v1/config/model-editions` 与新消息提交

- `model_edition` 仍使用顶层请求字段，候选值来自 `GET /api/v1/config/model-editions`；客户端不要自定义模型枚举或提交 `trim_max_tokens`。
- `metadata.deep_thinking` 与 `metadata.main_agent_reasoning_effort` 会影响本次请求的回答模式。关闭 deep thinking 时，高推理强度会被服务端降级；历史消息只保存最终 answer content。

### `/api/v1/artifacts/{artifact_id}/download`

- 下载接口需要 Authorization；客户端下载应使用 artifact descriptor 中的 `download_url` 并通过带 Bearer token 的请求发起。
- 不要把 assistant 正文里的链接当作下载入口；前端应优先展示 artifact 下载卡片或下载按钮。

## 2026-06-05

### 更新摘要

Skill 后端接入切换为 v2-only Skill Contract：公开能力只从 `skill.contract.yaml` 注册；`SKILL.md` 只保留轻量说明；执行输入由 `schemas/*.input.yaml` 和 slot_collection v2 驱动；主代理答疑只能通过 `SkillResourceService` 按需读取 prompt-facing resources。

### `/api/v1/capabilities`

- `skill.*` capability 只来自同 bundle 的 v2 contract；无 contract、contract invalid、旧 frontmatter-only Skill 不注册、不进入能力列表。
- 能力发现只暴露 capability/display/routing 摘要和相对 `source_path`；不会暴露脚本路径、handler、runtime、service、内部配置或 secret。

### `/api/v1/conversations/chat-messages`

- 外部 direct `capability_id=skill.*` 仍 fail closed，错误码为 `direct_skill_execution_disabled`。
- slash / API 点名 Skill 仍使用 `main_agent.respond + metadata.soft_skill_binding`；自然语言规划继续推荐 `capability_id=null`。
- v2 Skill 缺参 interrupt 的 `_slot_collection.schema_version=2`，包含 `selected_schema_id`、`selected_entrypoint`、`invalid`、`resource_hints`，客户端不得伪造内部 resume 字段。

### Skill ResourceService

- 主代理只可读取 contract 授权给 `main_agent` audience 的 prompt-facing resources；不得读取 `scripts/`、`runtime/`、`schemas/`、`native/`、`configs/`、`config.yaml`、`.env`、`.git` 或 secret/token/credential 路径原文。
- 资源读取会产生 audit-only `skill.resource_read` 事件，事件 payload 不包含原文内容。

> 维护口径：这里记录 API 的外部行为和客户端迁移提醒；字段级细节、请求 / 响应 schema、事件 payload 与错误码以 `/api-doc` 正式接口文档为准。API 行为变化追加到本文件顶部。

## 2026-06-01

### 更新摘要

表格上传编码兼容与表头规范化已从 PRD 进入后端与前端行为：上传层统一处理 CSV / JSON 多编码、技术性表头清洗与 Excel spreadsheet metadata；多 sheet Excel 不再交给 LLM 猜测，而是通过结构化 interrupt 让用户选择 sheet。

### `/api/v1/conversations/uploads` 与 `/api/v1/conversations/{conversation_id}/uploads`

- CSV / JSON 上传支持 `utf-8-sig`、`utf-8`、`gb18030`、`big5`、`shift_jis`、`cp932` 等确定性编码候选；解码必须完整成功，不使用 ignore / replace 静默吞错。
- 表头 / JSON 顶层 key 会清理 BOM、零宽字符、不可见控制字符、全角空格与外层成对引号；不会做业务语义列名映射。
- Excel `.xlsx` / `.xls` 上传返回 `file_type=spreadsheet`。单有效 sheet 自动生成执行用 UTF-8 CSV；多有效 sheet 返回 `requires_sheet_selection=true`、`excel_sheets`、`excel_sheet_count`。
- `UploadPreviewResponse` 新增 `source_encoding`、`original_columns`、`column_normalizations`、`column_count`、`columns_truncated`、`normalized_content_type`、`char_count`、`line_count`、`size_bytes`、`file_type`、`requires_sheet_selection`、`selected_sheet`、`excel_sheets`、`excel_sheet_count`、`excel_sheets_truncated` 等字段。
- Prompt-safe 上传摘要仍不返回完整 `content`、`content_base64` 或原始行数据；宽表 / 多 sheet 摘要通过 `*_truncated` 与 count 字段说明裁剪。
- `application/vnd.ms-excel` 若没有 Excel 后缀或 Excel magic bytes，仍按 CSV 兼容处理。

### `/api/v1/conversations/chat-messages` 与 `/api/v1/tasks/interrupts`

- 任务引用未选择 sheet 的多 sheet Excel upload 时，会先进入 `sheet_selection_required` open interrupt，不会把未确定 sheet 的内容传给 Skill。
- `required_fields.upload_sheet_selections` 使用 `required_upload_ids`、`options_by_upload_id`、`labels_by_upload_id` 的稳定结构，前端可为每个 upload 渲染独立 sheet 选择控件。
- 回答时通过 `chat-messages` 提交 `metadata.upload_sheet_selections={upload_id: sheet_name}`；服务端校验 upload 归属、sheet allowlist 与任务作用域，合法后 resume 当前任务并使用所选 sheet 的规范化 CSV。
- `metadata.upload_ids` 中任一 upload 缺失、过期或不属于当前会话时，消息提交返回 400，且不会创建 message / task。
- `sheet_selection_required` 回答若引用缺失 / 过期 upload 或非法 sheet，返回 400，并保持原 interrupt open、node waiting_for_input，不保存无效 answer。

### `/api/v1/tasks/cancel`

- 取消后如果后端节点晚返回或晚抛错，任务终态仍保持 `cancelled`；晚到结果不会再把任务覆盖成 `failed` / `completed`。


## 2026-05-28 至 2026-05-29

### 更新摘要

主要收口 Skill 外部调用入口、缺参 interrupt 续接、文件下载判定和恢复类 no-op 行为。

### `/api/v1/conversations/chat-messages`

- 外部客户端不要再直接提交 `capability_id=skill.*`。直接提交会返回 400，错误包含 `direct_skill_execution_disabled`，且不会创建任务或消息。
- Slash command 或 API 点名 Skill 时，统一提交：
  - `capability_id=main_agent.respond`
  - `metadata.soft_skill_binding.capability_id=<目标 skill.*>`
  - 可选 `metadata.soft_skill_binding.command=<slash command>`
- 服务端会先由主代理判断“答疑 / 追问缺参 / 内部执行 Skill”，目标 Skill 只作为内部节点参与任务图，不再作为外部消息提交入口。
- Soft-bound Skill 的追问会带入同一会话历史；用户连续追问时不需要重复完整上下文。
- Prompt Envelope 路径已接入 final token preflight：最终输入预算按 `floor(trim_max_tokens * 0.75)` 计算，失败后最多尝试一次历史压缩，再失败 fail closed。对外 REST / SSE schema 不变。

### `/api/v1/tasks/{task_id}/events`

- Soft-bound Skill 判断为答疑时，会通过 transient SSE `main_agent.output_delta` 流式输出。
- `main_agent.output_delta` 只用于当前实时连接展示，不入库、不 replay；刷新后的历史展示应以最终 assistant message / final 事件为准。
- Prompt Envelope、profile、token preflight 等渲染观测事件属于 audit-only，不进入前端 SSE。

### `/api/v1/tasks/interrupts`

- Skill 缺参统一表现为 open interrupt；当前前端直接展示 interrupt 的 `question` 文本，`required_fields` 仅用于展示补参上下文、上传引用和 sheet 选择。
- 回答 interrupt 后，服务端复用原 interrupted Skill 节点和原 finalizer，任务 root 保持稳定；客户端消息 metadata 中伪造的内部 resume 字段会被忽略。
- 同一任务内多轮补参会合并已接受的历史 answer payload 与上传 artifact metadata。用户先上传文件、再补标量参数时，不需要重复上传文件。

### `/api/v1/conversations/{conversation_id}/tasks`

- 查询不存在的 conversation task 列表时返回 200 空集合：`tasks=[]`。
- 前端恢复 / 轮询流程可以把该结果视为“没有可恢复任务”，不需要按错误弹窗处理。

### `/api/v1/conversations/uploads` 与 `/api/v1/conversations/{conversation_id}/uploads`

- 查询不存在的 conversation upload 列表时返回 200 空集合：`uploads=[]`。
- 删除未知 upload 返回 200，`deleted=false`，用于幂等清理。
- 上传列表只返回文件元数据，不返回 `content` / `content_base64`。
- 已删除 upload 再被消息引用时，会进入 `missing_upload_ids`，不会作为可用 uploaded artifact 进入执行阶段。

### `/api/v1/artifacts/{artifact_id}/download`

- 文件下载只信任平台 artifact descriptor 中的 `download_url=/api/v1/artifacts/{artifact_id}/download`。
- 客户端不要从 assistant 文本中提取 `sandbox:/mnt/data`、`file://`、`/mnt/data`、本地绝对路径或 `outputs/...` 作为下载链接。
- 如果任务没有 file artifact 或 artifact 缺少合法 `download_url`，前端应展示普通文本、失败说明或补充信息卡片，而不是下载按钮。

### `/api/v1/capabilities`

- `source_path` 以相对 Skill root 的路径返回，例如 `field-design/SKILL.md`，不带项目级 `skill/` 前缀。
- `skill.*` capability 只用于能力发现和系统内部编排；外部消息提交仍按 `main_agent.respond + metadata.soft_skill_binding` 进入。
