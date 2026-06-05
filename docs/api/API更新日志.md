# API 更新日志

## 2026-06-05

### 本次更新摘要

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

> 维护口径：这里只记录 API 的“最新外部行为”和客户端迁移提醒；字段级细节、请求 / 响应 schema、事件 payload 与错误码以 `/api-doc` 正式接口文档为准。后续 API 行为变化继续追加到本文件顶部。

## 2026-06-01

### 本次更新摘要

表格上传编码兼容与表头规范化已从 PRD 进入后端与前端行为：上传层统一处理 CSV / JSON 多编码、技术性表头清洗与 Excel spreadsheet metadata；多 sheet Excel 不再交给 LLM 猜测，而是通过结构化 interrupt 让用户选择 sheet。

### `/api/v1/conversations/uploads` 与 `/api/v1/conversations/{conversation_id}/uploads`

- CSV / JSON 上传支持 `utf-8-sig`、`utf-8`、`gb18030`、`big5`、`shift_jis`、`cp932` 等确定性编码候选；解码必须完整成功，不使用 ignore / replace 静默吞错。
- 表头 / JSON 顶层 key 会清理 BOM、零宽字符、不可见控制字符、全角空格与外层成对引号；不会做业务语义列名映射。
- Excel `.xlsx` / `.xls` 上传返回 `file_type=spreadsheet`。单有效 sheet 自动生成执行用 UTF-8 CSV；多有效 sheet 返回 `requires_sheet_selection=true`、`excel_sheets`、`excel_sheet_count`。
- `UploadPreviewResponse` 新增 `source_encoding`、`original_columns`、`column_normalizations`、`column_count`、`columns_truncated`、`normalized_content_type`、`requires_sheet_selection`、`selected_sheet`、`excel_sheets`、`excel_sheet_count`、`excel_sheets_truncated` 等字段。
- Prompt-safe 上传摘要仍不返回完整 `content`、`content_base64` 或原始行数据；宽表 / 多 sheet 摘要通过 `*_truncated` 与 count 字段说明裁剪。
- `application/vnd.ms-excel` 若没有 Excel 后缀或 Excel magic bytes，仍按 CSV 兼容处理。

### `/api/v1/conversations/chat-messages`、`/api/v1/tasks/interrupts` 与 `/api/v1/tasks/interrupts/answer`

- 任务引用未选择 sheet 的多 sheet Excel upload 时，会先进入 `sheet_selection_required` open interrupt，不会把未确定 sheet 的内容传给 Skill。
- `required_fields.upload_sheet_selections` 使用 `required_upload_ids`、`options_by_upload_id`、`labels_by_upload_id` 的稳定结构，前端可为每个 upload 渲染独立 sheet 选择控件。
- 回答时提交 `answer_payload.upload_sheet_selections={upload_id: sheet_name}`；服务端校验 upload 归属、sheet allowlist 与任务作用域，合法后 resume 当前任务并使用所选 sheet 的规范化 CSV。
- `metadata.upload_ids` 中任一 upload 缺失、过期或不属于当前会话时，消息提交返回 400，且不会创建 message / task。
- `sheet_selection_required` 回答若引用缺失 / 过期 upload 或非法 sheet，返回 400，并保持原 interrupt open、node waiting_for_input，不保存无效 answer。

### `/api/v1/tasks/cancel`

- 取消后如果后端节点晚返回或晚抛错，任务终态仍保持 `cancelled`；晚到结果不会再把任务覆盖成 `failed` / `completed`。


## 2026-05-28 至 2026-05-29

### 本次更新摘要

本轮主要收口了 Skill 外部调用入口、缺参 interrupt 续接、文件下载判定和恢复类 no-op 行为。

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

### `/api/v1/tasks/interrupts` 与 `/api/v1/tasks/interrupts/answer`

- Skill 缺参统一表现为 open interrupt，前端应按 interrupt 的 `reason_code`、`question`、`required_fields` 渲染补充输入卡片。
- 回答 interrupt 后，服务端复用原 interrupted Skill 节点和原 finalizer，任务 root 保持稳定；客户端 payload 中伪造的内部 resume 字段会被忽略。
- 同一任务内多轮补参会合并已接受的历史 answer payload 与上传 artifact metadata。用户先上传文件、再补标量参数时，不需要重复上传文件。

### `/api/v1/conversations/{conversation_id}/tasks`

- 查询不存在的 conversation task 列表时返回 200 空集合：`tasks=[]`。
- 前端恢复 / 轮询流程可以把该结果视为“没有可恢复任务”，不需要按错误弹窗处理。

### `/api/v1/conversations/uploads` 与 `/api/v1/conversations/{conversation_id}/uploads`

- 查询不存在的 conversation upload 列表时返回 200 空集合：`uploads=[]`。
- 删除未知 upload 返回 200，`deleted=false`，用于幂等清理。
- 上传列表只返回文件元数据，不返回 `content` / `content_base64`。
- 已删除 upload 再被消息引用时，会进入 `missing_upload_ids`，不会作为可用 uploaded artifact 进入后续执行。

### `/api/v1/artifacts/{artifact_id}/download`

- 文件下载只信任平台 artifact descriptor 中的 `download_url=/api/v1/artifacts/{artifact_id}/download`。
- 客户端不要从 assistant 文本中提取 `sandbox:/mnt/data`、`file://`、`/mnt/data`、本地绝对路径或 `outputs/...` 作为下载链接。
- 如果任务没有 file artifact 或 artifact 缺少合法 `download_url`，前端应展示普通文本、失败说明或补充信息卡片，而不是下载按钮。

### `/api/v1/capabilities`

- `source_path` 以相对 Skill root 的路径返回，例如 `field-design/SKILL.md`，不带项目级 `skill/` 前缀。
- `skill.*` capability 只用于能力发现和系统内部编排；外部消息提交仍按 `main_agent.respond + metadata.soft_skill_binding` 进入。
