# API 更新日志

## 2026-06-09

### 更新摘要

API 有几处会影响客户端接入的变化：v2 Skill 的补参状态改为后端持久化，`/api/v1/conversations/chat-messages` 同时处理普通新消息和 interrupt 继续输入，interrupt 回答改为提交用户原文并支持开放性追问，历史消息可以带回可展示 artifact，上传接口开始接受 VCF 文件，LLM 选项也会随请求传到更多内部调用。字段细节仍以 `/api-doc` 和 `/openapi.json` 为准；这里记录客户端需要改动或关注的行为。

### `/api/v1/conversations/chat-messages`、`/api/v1/tasks/interrupts` 与 `/api/v1/tasks/interrupts/answer`

- 推荐客户端复用 `POST /api/v1/conversations/chat-messages` 提交 interrupt 状态下的下一条用户消息。目标 conversation 存在 open interrupt 时，服务端优先把该消息路由成 interrupt turn，不创建新 task；没有 open interrupt 时仍按普通新消息创建 task。
- 回答 interrupt 时可在 `metadata.interrupt_id` 指定目标 interrupt；单 open interrupt 可省略，多个 open interrupt 未指定会返回 400。上传回答继续放 `metadata.upload_ids`，多 sheet 选择放 `metadata.upload_sheet_selections`。
- `MessageAcceptedResponse.action` 用来区分提交类型：`task_accepted` 表示新任务，`interrupt_resumed` 表示正式回答并恢复同一个 task，`interrupt_clarification_answer` 表示纯追问/澄清，`interrupt_mixed_processed` 表示同一条消息里同时包含补参和追问/引导，`interrupt_schema_switched` 表示已在当前 Skill 内切换 input schema。后面三类通常通过 `assistant_message` 返回解释；客户端再根据 `answer_payload.will_resume` / `answer_payload.requires_confirmation` 判断是否继续保持 interrupt open。
- 兼容端点 `POST /api/v1/tasks/interrupts/answer` 仍保留；它适合旧客户端或已经持有结构化 `AnswerInterruptRequest` 的集成。新聊天式客户端优先只接入 `chat-messages`。
- v2 Skill 的缺参状态现在由后端 `SlotCollection` / `SlotEvent` 保存。`Interrupt.required_fields` 只给客户端一个 `_slot_collection_ref` 摘要，客户端不要修改它，也不要把它当成真实状态表。
- v2 slot 回答走顶层 `client_request_id` 和 `answer`，例如 `{task_id, interrupt_id, client_request_id, answer:{text:"12列"}}`。旧版 `answer_payload` 继续用于非 v2 slot、sheet 选择等老路径。
- v2 slot 只接收用户原文或上传选择；`answer` 对象只允许 `text`、`upload_ids`、`sheet_selections` / `upload_sheet_selections`。缺少 `client_request_id`、`answer` 不是对象、提交 `design=...` / `ncols=...` 这类字段形 payload，或者夹带内部 resume 字段，都会被服务端拒绝。
- `client_request_id` 是同一个 interrupt turn 的幂等键。v2 open interrupt 使用 `interrupt_turn:{interrupt_id}:{client_request_id}` 记录 turn summary；重复提交同一个键不会再次调用 planner、LLM 问答或 slot 抽取，也不会重复调度脚本，服务端会返回同一份 `assistant_message` 和 `answer_payload` 摘要。已经 answered / 非 open 的 v2 interrupt 只允许用原 `client_request_id` replay 既有 summary；新 key 会 fail closed，避免 stale interrupt 重跑。
- 同一个 v2 `SlotCollection` 如果在更高 revision 再次补齐并进入 ready 状态，脚本调度会按 `collection_id + revision` 做幂等。这样可以避免旧 revision 的调度记录阻止新的 ready revision 恢复执行。客户端不需要提交 revision，也不需要额外确认；以 `action=interrupt_resumed`、`answer_payload.will_resume=true` 以及 `node.ready_to_resume` / `node.resuming` 为准。
- v2 slot interrupt 中，用户可以直接问开放性问题，例如“这个数据要什么格式？”、“这几种设计区别和利弊是什么？”。后端会先进入 interrupt-open query split planner，把同一条 Query 拆成 `slot_answer`、`skill_question`、`off_topic_guidance`、`schema_switch`、`ambiguous` 等部分；没有 open interrupt 的普通 Query 不走这个 planner。
- 如果当前 interrupt turn 是追问、低置信、LLM 输出无效、off-topic 引导，或 schema switch 需要确认，`chat-messages` 响应可能是 `action=interrupt_clarification_answer`、`interrupt_mixed_processed` 或 `interrupt_schema_switched`；兼容 answer endpoint 响应为 `action=clarification_answer` / `mixed_processed` / `schema_switched`。响应会带 `assistant_message`，并在 `answer_payload` 中返回 `processed_parts`、`slot_status`、`will_resume`、`requires_confirmation`、`active_slot_collection_id`、可选 `schema_switch`。只要 `will_resume=false` 或 `requires_confirmation=true`，客户端应继续保持 interrupt 输入态。
- 正式回答成功后，`chat-messages` 响应为 `action=interrupt_resumed`；兼容 answer endpoint 响应为 `action=resumed`，并写入 `task.interrupt_answered`。事件 payload 只带 `interrupt_id`、`slot_collection_id`、`client_request_id` 等安全字段；继续订阅同一个 `task_id` 的 SSE，看 `node.ready_to_resume`、`node.resuming` 和任务终态。
- schema switch 仅允许在当前 interrupted Skill 内切换 input schema。若用户未说明是否复用旧参数，服务端会先保存待确认 proposal 并询问；用户回复“复用”后才复制新旧 schema 同名字段，回复“不复用”则保留新 schema 的空 collection（目标 schema 的 const 选择值除外）。旧有但新 schema 不存在的字段会丢弃，新 schema 需要但旧 collection 没有的字段继续留空。即使新 collection 已 ready，schema switch 后也只有在用户明确“确认执行/直接跑”等且 planner 高置信确认时才会调度脚本。
- interrupt open 状态下的无关问题不会被硬拒绝，也不会创建新 task；服务端会带着当前 Skill/slot 上下文引导用户回到任务，并保持 interrupt open。

- v2 slot 前端直接展示后端给的 `question`，聊天历史保存用户原话。不要把 `design=对角线增广` 这类内部键值展示给用户。
- 用户说“刚才”“上次”“前面文件”时，后端会走单独的 History Recall 模式。普通参数抽取和历史召回分开处理，客户端不用自己拼完整历史。

### `/api/v1/conversations/chat-messages` 与 `/api/v1/config/model-editions`（普通新任务参数）

- `model_edition` 仍是顶层请求字段，候选值来自 `GET /api/v1/config/model-editions`。客户端不要自己猜模型枚举，也不要提交 `trim_max_tokens`。
- `metadata.deep_thinking` 和 `metadata.main_agent_reasoning_effort` 会随该请求传给主代理、Soft Skill、planner、SQLQuery、Skill slot 抽取 / 追问、会话标题和记忆解析等 LLM 调用。
- 如果 `metadata.deep_thinking=false`，服务端会把伪造的 `high` / `max` 推理强度降成 `minimal`。后台 LLM 分支不向前端暴露 `reasoning_content`，历史消息只保存最终 answer content。
- v2 Skill 首次执行时，后端可以按已选 input schema 从用户原文里抽参数。客户端仍然只提交用户原文、上传 ID 和模型选项，不解析业务字段。

### `/api/v1/conversations/{conversation_id}/messages`

- `MessageResponse` 新增可选 `artifacts` 数组。读取历史消息时，assistant 消息可能带回可展示 artifact，用来在刷新或切换历史会话后恢复数据查询表格卡片、OCR 原文卡片和仍 active 的文件下载卡片。
- 历史 artifact 继续使用 `ArtifactResponse` 的脱敏结构。内部 SQL、坏 JSON、inactive file artifact 不会作为可展示 artifact 返回。
- 实时任务仍以 SSE 和 `/api/v1/tasks/{task_id}/artifacts` 为主。`messages[].artifacts` 主要用于历史恢复，以及任务结束后补齐展示。

### `/api/v1/conversations/uploads` 与 `/api/v1/conversations/{conversation_id}/uploads`

- 上传白名单新增 `.vcf` 和 `.vcf.gz`，返回 `file_type=vcf`。`.vcf.gz` 按复合扩展名识别，不会因为 `application/gzip` 就放行普通 `.gz`。
- VCF / VCF.GZ 按二进制文件处理，不做 CSV / JSON / Excel 表格预览解码。prompt-safe 通道只暴露文件名、大小、摘要等安全元数据，Skill 执行通道继续按 artifact 机制传原始字节。

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

### `/api/v1/tasks/interrupts` 与 `/api/v1/tasks/interrupts/answer`

- Skill 缺参统一表现为 open interrupt；当前前端直接展示 interrupt 的 `question` 文本，`required_fields` 仅用于构造回答 payload、上传引用和 sheet 选择兼容。
- 回答 interrupt 后，服务端复用原 interrupted Skill 节点和原 finalizer，任务 root 保持稳定；客户端 payload 中伪造的内部 resume 字段会被忽略。
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
