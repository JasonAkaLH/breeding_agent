# API 更新日志

> 维护口径：这里只记录 API 的“最新外部行为”和客户端迁移提醒；字段级细节、请求 / 响应 schema、事件 payload 与错误码以 `/api-doc` 正式接口文档为准。后续 API 行为变化继续追加到本文件顶部。

## 2026-05-28 至 2026-05-29

### 本次更新摘要

本轮主要收口了 Skill 外部调用入口、缺参 interrupt 续接、文件下载判定和恢复类 no-op 行为。表格上传编码兼容仍处在 PRD / 分阶段设计阶段，不计入已上线 API 行为。

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

### 仅规划中，未计入已上线 API

- 表格上传编码兼容与表头规范化目前仍是 PRD / 阶段拆分内容，尚未作为 `/api/v1/conversations/uploads` 的已上线行为写入本更新日志。
