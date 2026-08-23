# API 更新日志：`main` 与 `prod` 全量差异扫描

> 扫描日期：2026-07-10
>
> 比较基线：`main@bd0fd2d` 对 `prod@e7ede32`
>
> 比较方向：本文默认描述“`main` 相对 `prod`”的变化；反向差异会单独标注。
>
> 适用对象：前端、第三方 API 客户端、部署维护人员、后端开发与测试人员。

## 2026-08-23 增量：统一Agent Loop clean cutover

`POST /api/v1/conversations/chat-messages`点名公开Skill时，客户端直接提交`routing_mode=force_capability`和当前`capability_id=skill.*`；不再提交`main_agent.respond`或`metadata.soft_skill_binding`。点名`mcp.dispatch`仍必须使用已验证的`metadata.mcp_server_binding`，Server authority由后端固定，模型不可改写。

任务SSE以`agent.reasoning_delta`作为统一Agent瞬时reasoning，以`agent.run.completed|failed|cancelled`作为Run终态；旧Planner、soft-Skill、replan和main-agent-output事件已退出正式合同。Reasoning不持久化、不replay；成功最终回答只由唯一`agent.final_output` Artifact/Message/event/receipt原子发布。`GET /api/v1/tasks/{task_id}/graph`保留响应shape，但现在是empty-edge活动账本，`edges=[]`，不表达依赖调度。

HTTP endpoint与顶层请求/响应schema未新增、未删除；这是消息提交、SSE终态和graph语义的breaking behavior cutover。旧Task不迁移、不恢复，旧客户端必须更新Skill提交与Agent事件消费后才能使用新任务路径。

## 2026-08-20 增量：MCP Result typed业务视图

`GET /api/v1/tasks/{task_id}/artifacts`与Conversation history/message Artifact现在只从已发布且通过owner/Task/Node/Call/raw/schema/source/parser/projection identity复验的task-private projection读取MCP业务结果。公共响应固定为`artifact_type=mcp_result`、`storage_ref=""`和`mcp_business_result.schema=maf.mcp.business_result_view.v1`；`availability=ready`时，`primary`严格为`structured | structured_preview | text | empty`之一，media/resource仅返回闭合metadata，整个视图不超过20,000 code points/80,000 bytes。`availability=unavailable`只允许`safe_hide | projection_missing | historical_authority_invalid | projection_invalid`。

前端只按该schema与闭合variant展示清洗后的业务JSON或纯文本，不再按Artifact ID前缀识别结果，不读取raw `storage_ref`，也不提供raw展开或下载。非法、超预算或digest不匹配的projection安全降级为unavailable；direct download继续404。

## 2026-08-20 历史增量：MCP Result公共读取进入safe-hide（已升级为typed业务视图）

`GET /api/v1/tasks/{task_id}/artifacts`和Conversation history中的MCP Result Artifact不再返回原始JSON正文。响应固定使用`artifact_type=mcp_result`、`storage_ref=""`，并新增`mcp_business_result`；Result Parser尚未发布业务投影时返回`availability=unavailable`与`unavailable_reason=safe_hide`。direct download继续404，客户端不得从`storage_ref`、Artifact ID或其他字段恢复raw正文。

## 2026-08-20 历史增量：MCP Tool原始返回改为Text Artifact（已被safe-hide替代）

该历史行为曾把内部`source_kind=mcp_result`投影为公共`text`和原始JSON正文，现已由上方safe-hide合同替代；内部文件与生命周期仍保留，公共读取不再返回正文。

`GET /api/v1/artifacts/{artifact_id}/download`不再允许MCP结果artifact，直接请求返回404；该下载接口继续服务active Skill output文件。

## 2026-08-19 增量：MCP Tool完整原始返回Artifact与闭合状态

成功的用户级MCP业务Call现在把receipt绑定的完整durable JSON结果投影为现有公共file Artifact；客户端继续通过
`GET /api/v1/tasks/{task_id}/artifacts`和`GET /api/v1/artifacts/{artifact_id}/download`读取，不新增endpoint，
`storage_ref`仍不会对外暴露。文件名为按Call顺序生成的安全名称，例如`01-start_parse_job-result.json`。

`TaskSummaryResponse`和completed assistant `MessageResponse`加法新增最多20项的
`mcp_result_artifact_projections`。每项严格包含`schema`、不可逆`safe_call_ref`、
`status=ready|deferred|permanent_failure`、闭合`reason_code`和`artifact_count=0|1`；非法、冲突或超限历史
fail closed为空列表。任务SSE加法新增`mcp.result_artifact_projection`同形事件。客户端应在`deferred`时提示完整文件
仍在生成，在`permanent_failure`时提示文件未能保留；不得展示内部reason、result ref、storage key或原始Tool返回。
`ready`只清除提示，不应提前关闭Task SSE或主动触发Artifact终态加载。旧客户端忽略新增字段/事件仍兼容。

## 2026-08-17 增量：`$` 用户级 MCP Server Soft Binding

`POST /api/v1/conversations/chat-messages` 新增 closed `metadata.mcp_server_binding={"server_id":"..."}`。该字段只能与 `routing_mode=force_capability`、`capability_id=mcp.dispatch` 同时出现；反向组合也成立，强制 `mcp.dispatch` 缺少binding返回422。binding请求的metadata只允许 `mcp_server_binding`、`upload_ids`、`upload_sheet_selections`、`deep_thinking`、`main_agent_reasoning_effort`。目标Server不存在、跨用户、disabled、unavailable、待删除或已删除统一返回409 `mcp_bound_server_unavailable`，user-scoped runtime不可承载时返回503 `mcp_feature_unavailable`。

成功提交后，Server身份固定且不经过Server Router；系统执行 `initialize + tools/list`，Selector只能在当前Server内选择 `call_tool`、`finish`或`stop`。只有 `call_tool` 进入既有逐Tool授权和20次预算。当前消息显式附件只以basename、MIME、size和count摘要进入Selector，文件正文、路径、base64、storage key、SHA和内部upload ID不会发送给MCP。消息历史新增安全 `mcp_server_badge`，private binding context不会通过公共历史返回。

## 2026-08-17 增量：用户级 MCP 公网 HTTP/HTTPS Endpoint Policy

`POST/PATCH /api/v1/mcp/servers` 的请求和响应 schema 不变。行为调整为任意公网 HTTP/HTTPS Endpoint 经 URL、DNS、IP 与重定向校验后均可保存；公网 HTTP 可携带现有 Bearer/API Key/static headers 认证，记录脱敏的 `plaintext_http` / `credential_over_plaintext_http` 安全布尔值。私网、回环、链路本地、云元数据、多播、保留及未指定地址始终返回 422 安全原因码，不再支持管理员域名/CIDR allowlist 例外。前端新建 HTTP 或从 HTTPS 改为 HTTP 时显示明文传输风险确认；保存失败在当前 MCP 设置 Modal 内显示具体安全原因并保留表单。具体 `tools/call` 的一次允许/始终允许/拒绝授权流程不变。

## 2026-08-13 增量：MCP 灰度不可用任务事件

任务 SSE 新增加法兼容事件 `mcp.runtime_unavailable`。当当前灰度、回滚或 assembly 配置无法为带用户自定义 MCP Server 的新任务分配安全单路径时，后端持久化并推送该事件；payload 只含 `status=unavailable` 与闭集 `reason_code`，不含用户名、Server endpoint、凭据、Tool 参数或结果。客户端应明确展示“当前任务的 MCP 暂不可用”，不得把它解释为无工具、自动切换到 global legacy，或提示重放原调用。任务创建响应与既有 REST schema 不变。

## 2026-08-12 增量：用户级 MCP 配置 API

新增当前认证用户隔离的 MCP Server 配置端点：`GET/POST /api/v1/mcp/servers`、`GET/PATCH/DELETE /api/v1/mcp/servers/{server_id}` 和 `POST /api/v1/mcp/servers/{server_id}/test`。POST 与手动 test 异步返回 `testing`；安全校验通过但连接/发现失败的配置保留为 `unavailable`；空 Tool List 同样为 `unavailable`。响应只含 `credential_configured`，不返回凭据明文、密文或 Nonce。下文 `main@bd0fd2d` 对 `prod@e7ede32` 的“新增路径：0”是 2026-07-10 历史扫描结论，不包含本次增量。

## 2026-08-12 增量：用户级 MCP 授权与任务控制 API

新增 `GET /api/v1/mcp/grants`、`DELETE /api/v1/mcp/grants/{grant_id}`、`DELETE /api/v1/mcp/servers/{server_id}/grants`，用于查询、撤销和按 Server 清空当前认证用户的“始终允许”授权；跨用户或未知授权统一返回 404。新增 `POST /api/v1/tasks/{task_id}/mcp-calls/{call_ref}/continue` 与 `POST /api/v1/tasks/{task_id}/mcp-calls/{call_ref}/cancel`，两者先校验 task owner，未知调用返回 404、已终态返回 409、接受控制返回 202。任务 SSE 增加 `mcp.server_routed`、发现/排队、授权、调用、执行状态未知、输入请求与远端任务状态事件；所有前端事件只携带显示名、计数、状态和平台安全引用，不携带 Endpoint、凭据、Tool 参数/结果、Schema、Session 或远端原始 ID。

## 1. 执行摘要

本次扫描以两个分支的 FastAPI 路由、Pydantic DTO、OpenAPI 文档、SSE 事件、运行时逻辑、持久化实现、前端 API client 和回归测试为依据，替换本文件原有的增量历史记录。

### 1.1 总体结论

| 检查维度 | 结论 | 兼容性判断 |
|---|---|---|
| HTTP endpoint 路径 | 无新增、无删除、无改名 | 兼容 |
| HTTP method | 无变化 | 兼容 |
| 请求体顶层字段 | OpenAPI 无变化 | 兼容 |
| query / path / form 参数 | OpenAPI 无变化 | 兼容 |
| 响应 schema | 2 个公开响应模型发生变化 | 需要客户端适配 |
| SSE | 新增 `capability.missing_fallback` 前端事件 | 加法兼容；建议处理 |
| 消息提交行为 | 新增模型级 reasoning effort 校验、会话文件智能选择、能力缺失 fallback | 行为变化 |
| 上传与删除行为 | schema 不变；一致性、补偿、历史投影和错误处理增强 | 行为变化 |
| SeedPilot 外部路径 | API 代理路径一致；`prod` 的 API 文档页比 `main` 多 Nginx 内容重写 | `main` 存在文档页子路径风险 |

### 1.2 需要优先处理的客户端变化

1. `GET /api/v1/config/model-editions` 的 `options[]` 新增**必填** `reasoning_efforts`。
2. `GET /api/v1/conversations/{conversation_id}/messages` 的每条消息新增 `message_type`、`metadata`、`updated_at`；客户端需要识别 `role=system` 且 `message_type=file_upload` 的公开文件历史消息。
3. SSE 客户端应识别 `capability.missing_fallback`，并明确展示“当前回答未调用缺失能力、不会生成可下载文件”。
4. 客户端不得继续把 reasoning effort 写死为全局枚举；必须使用所选模型返回的 `reasoning_efforts.options`。

---

## 2. 扫描范围与判定方法

### 2.1 已扫描范围

- `src/api/`：路由、DTO、SSE、上传、文件选择和 runtime 装配。
- `src/core/`、`src/storage/`：消息模型、会话文件资源、SQLite 持久化和一致性操作。
- `src/orchestration/`：Planner / Replanner、能力缺失 fallback、会话记忆和工作流行为。
- `src/integrations/`：模型版本、reasoning effort、LLM 请求选项和 Skill 文件契约。
- `frontend/src/api/`、`frontend/src/domain/`：请求参数、响应类型、SSE reducer 和历史恢复。
- `docker/nginx.conf`：`/seedpilot` 外部访问路径。
- `tests/api/`、`tests/orchestration/`、`tests/integrations/`：公开行为和失败边界。

### 2.2 OpenAPI 结构化比较结果

由两个分支的独立归档快照分别生成 OpenAPI，并比较 `paths`、operations 和 `components.schemas`：

- 新增路径：0
- 删除路径：0
- method 变化：0
- 新增公开 schema：
  - `ReasoningEffortConfigResponse`
  - `ReasoningEffortOptionResponse`
- 已有 schema 的字段变化：
  - `ModelEditionOptionResponse` 新增必填 `reasoning_efforts`
  - `MessageResponse` 新增 `message_type`、`metadata`、`updated_at`
- 删除公开 schema 或字段：0

> 注意：`SubmitMessageRequest.metadata` 是开放字典。部分新增行为通过其中的可选键触发，因此不会表现为 OpenAPI 顶层字段变化。

---

## 3. Endpoint 与 method 全量结论

以下公开接口在 `main` 与 `prod` 中路径和 method 相同。

### 3.1 鉴权

| Method | Path | 差异结论 |
|---|---|---|
| `POST` | `/api/v1/auth/login` | 无公开契约差异 |
| `GET` | `/api/v1/auth/me` | 无公开契约差异 |
| `POST` | `/api/v1/auth/logout` | 无公开契约差异 |
| `POST` | `/api/v1/auth/refresh-token` | 无公开契约差异 |

### 3.2 会话与消息

| Method | Path | 差异结论 |
|---|---|---|
| `POST` | `/api/v1/conversations/chat-messages` | 路径/schema 不变；模型、选文件、fallback 行为变化 |
| `GET` | `/api/v1/conversations` | 无公开契约差异 |
| `PATCH` | `/api/v1/conversations` | 无公开契约差异 |
| `DELETE` | `/api/v1/conversations` | 无公开契约差异 |
| `GET` | `/api/v1/conversations/{conversation_id}/messages` | `MessageResponse` 增加字段和公开文件历史消息 |
| `GET` | `/api/v1/conversations/{conversation_id}/tasks` | 无公开契约差异 |

### 3.3 会话文件

| Method | Path | 差异结论 |
|---|---|---|
| `POST` | `/api/v1/conversations/uploads` | form/schema 不变；保存一致性和历史投影增强 |
| `GET` | `/api/v1/conversations/{conversation_id}/uploads` | query/schema 不变；读取前增加索引修复 |
| `DELETE` | `/api/v1/conversations/uploads` | body/schema 不变；删除一致性增强，新增 400 映射 |

### 3.4 任务、interrupt 与 artifact

| Method | Path | 差异结论 |
|---|---|---|
| `GET` | `/api/v1/tasks/{task_id}` | 无 schema 差异 |
| `GET` | `/api/v1/tasks/{task_id}/events` | 新增 SSE 事件 `capability.missing_fallback` |
| `POST` | `/api/v1/tasks/cancel` | 无公开契约差异 |
| `GET` | `/api/v1/tasks/{task_id}/interrupts` | schema 不变；文件选择复用现有 interrupt 流程 |
| `GET` | `/api/v1/tasks/{task_id}/graph` | 无公开契约差异 |
| `GET` | `/api/v1/tasks/{task_id}/artifacts` | 无公开契约差异 |
| `GET` | `/api/v1/artifacts/{artifact_id}/download` | 无公开契约差异 |

### 3.5 能力与配置

| Method | Path | 差异结论 |
|---|---|---|
| `GET` | `/api/v1/capabilities` | 最终实现一致；两分支都会在返回列表前刷新 Skill |
| `GET` | `/api/v1/config/model-editions` | `options[].reasoning_efforts` 成为必填响应字段 |

开发者文档路由 `/api-doc`、`/api-doc/API更新日志.md`、`/api-doc/api-changelog.md` 在两个分支中也保持一致；FastAPI 的 `/openapi.json`、`/docs`、`/redoc` 应用路由没有业务 schema 差异。

---

## 4. 详细差异

## 4.1 模型选项与 reasoning effort

### 影响接口

- `GET /api/v1/config/model-editions`
- `POST /api/v1/conversations/chat-messages`

### 响应变化

`prod` 中的模型选项只有：

```json
{
  "value": "model-a",
  "label": "Model A"
}
```

`main` 中每个模型选项必须返回：

```json
{
  "value": "model-a",
  "label": "Model A",
  "reasoning_efforts": {
    "default": "medium",
    "disabled_default": "minimal",
    "options": [
      {
        "value": "minimal",
        "label": "Minimal",
        "allow_when_thinking_disabled": true
      },
      {
        "value": "medium",
        "label": "Medium",
        "allow_when_thinking_disabled": false
      }
    ]
  }
}
```

字段含义：

| 字段 | 类型 | 约束 |
|---|---|---|
| `default` | `string` | 开启深度思考且客户端未指定 effort 时使用；必须引用 `options[].value` |
| `disabled_default` | `string \| null` | 关闭深度思考时的默认值；非空时必须引用 disabled-safe option |
| `options[].value` | `string` | 当前模型自己的 effort 值；不再是全局固定枚举 |
| `options[].label` | `string` | 客户端展示文本 |
| `options[].allow_when_thinking_disabled` | `boolean` | `deep_thinking=false` 时是否允许使用 |

### 请求行为变化

`SubmitMessageRequest` 顶层结构不变：

```json
{
  "conversation_id": "conv-1",
  "content": "请分析这份数据",
  "model_edition": "model-a",
  "metadata": {
    "deep_thinking": true,
    "main_agent_reasoning_effort": "medium"
  }
}
```

`main` 的校验逻辑：

- `model_edition` 仍是顶层可选字段，值必须来自模型配置接口。
- `metadata.main_agent_reasoning_effort` 按所选模型校验，不再使用全局 `minimal | high | max` 假设。
- `deep_thinking=true` 且未显式提供 effort 时，使用该模型的 `default`。
- `deep_thinking=false` 且未显式提供 effort 时，使用该模型的 `disabled_default`。
- `deep_thinking=false` 时显式提交非 disabled-safe effort，返回 HTTP 400。
- 模型没有 disabled-safe option 时，不允许关闭深度思考；客户端应将开关固定为开启。
- 多模型配置下，无法确定所选模型时会 fail closed，而不是静默套用其他模型默认值。
- 后端启动时会验证所有模型的 `reasoning_efforts`：缺失、重复值、无效 default 或无效 disabled default 都会阻止错误配置进入运行态。

### 客户端迁移要求

- 不要写死 effort 列表。
- 切换模型时同步切换 effort 选项和默认值。
- 未由用户显式选择 effort 时可以省略 `metadata.main_agent_reasoning_effort`，让服务端使用模型默认值。
- 严格响应解码器必须把 `reasoning_efforts` 加入 `ModelEditionOptionResponse`。

兼容性：**响应模型对严格客户端属于破坏性变化；请求字段本身保持兼容。**

## 4.2 历史消息响应与文件上传历史

### 影响接口

`GET /api/v1/conversations/{conversation_id}/messages`

### `MessageResponse` 字段变化

| 字段 | `prod` | `main` | 说明 |
|---|---|---|---|
| `message_type` | 不存在 | `string`，默认 `chat` | 区分普通聊天和 `file_upload` |
| `metadata` | 不存在 | `object`，默认 `{}` | 公开、安全的消息元数据 |
| `updated_at` | 不存在 | `datetime \| null` | 支持文件状态更新等历史投影 |
| 其他字段 | 已存在 | 保持 | `artifacts` 等原字段不变 |

### 公开消息过滤逻辑

`main` 只返回以下历史消息：

1. `message_type=chat` 且 `role=user | assistant`；
2. `message_type=file_upload`、`role=system`，并且消息 ID 中包含有效稳定 `upload_id`。

其他内部 `system` 消息仍不对客户端公开。

### 文件历史消息示例

```json
{
  "message_id": "file_upload:upl-0123456789ab",
  "conversation_id": "conv-1",
  "role": "system",
  "content": "文件上传：materials.csv\n- upload_id: upl-0123456789ab\n- 状态: active\n- 描述状态: ready",
  "task_id": null,
  "stream_status": "complete",
  "created_at": "2026-07-10T08:00:00Z",
  "message_type": "file_upload",
  "metadata": {
    "schema_version": 1,
    "upload_id": "upl-0123456789ab",
    "filename": "materials.csv",
    "description_status": "ready",
    "description_summary": "材料信息表",
    "file_type": "csv",
    "content_type": "text/csv",
    "size_bytes": 1024,
    "file_status": "active",
    "uploaded_at": "2026-07-10T08:00:00Z"
  },
  "updated_at": "2026-07-10T08:00:01Z",
  "artifacts": []
}
```

`file_upload.metadata` 只允许以下字段通过 API 边界：

- `schema_version`、`upload_id`、`filename`
- `description_summary`、`description_status`
- `file_type`、`content_type`、`size_bytes`、`sha256`
- `file_status`、`uploaded_at`、`description_updated_at`
- `selected_sheet`、`requires_sheet_selection`
- `row_count`、`column_count`、`sheet_names`

路径、`storage_key`、完整 `content`、`content_base64`、provider 原始 payload、token、secret、authorization 等字段不会公开。

删除文件后，同一稳定消息会更新为 `metadata.file_status=deleted`。客户端可以保留历史展示，但不得把已删除文件重新作为任务附件。

## 4.3 会话文件上传、列表与删除的一致性

### 公开参数与 schema

以下内容在两个分支间不变：

- 上传仍使用 `multipart/form-data`：`conversation_id` + `file`。
- 列表 query 仍是 `limit`、`cursor`、`include_deleted`。
- 删除请求仍是 `{ "conversation_id": "...", "upload_id": "..." }`。
- `UploadFileResponse`、`UploadListResponse`、`DeleteUploadResponse` 字段无 OpenAPI 差异。
- TSV 支持在两个最终分支均已存在，不属于本次分支差异。

### `main` 的行为增强

#### 上传

- 上传成功会同时保存 conversation-scoped 文件资源，并创建或更新稳定的 `file_upload:<upload_id>` 历史消息。
- 文件资源与历史投影采用组合持久化语义，避免“文件成功但历史消息缺失”。
- 本地文件、描述文件、数据库组合写入或索引写入失败时，接口 fail closed，不返回部分成功。
- 对可恢复的索引写入失败进行重试；持续失败时记录 repair marker 并补偿本轮上传。
- 补偿会清理本轮产生的数据库状态、本地文件和内存态；补偿本身失败会留下可审计修复标记。

#### 列表

- 读取会话文件前会尝试处理到期的 index repair marker。
- DB 中的文件资源状态是权威事实，`index.md` 不作为 API 返回的唯一事实源。

#### 删除

- 删除会原子更新文件资源状态与对应文件历史消息，再重写索引并清理本地目录。
- 即使索引重写或本地目录清理失败，数据库中的 `deleted` 事实仍保持，不会把文件重新暴露为 active。
- `main` 新增 `UploadValidationError -> HTTP 400` 映射；`prod` 的删除路由只显式处理权限/不存在场景。

兼容性：**响应 schema 兼容，但失败时机和错误码更严格；调用方不得把超时或 400 当作已成功删除/上传。**

## 4.4 会话文件智能选择

### 影响接口

- `POST /api/v1/conversations/chat-messages`
- `GET /api/v1/tasks/{task_id}/interrupts`
- `GET /api/v1/tasks/{task_id}/events`

没有新增 endpoint，也没有新增顶层必填参数。

### 选择模式

由服务端环境变量 `MAF_CONVERSATION_FILE_SELECTOR_MODE` 控制：

| 模式 | 行为 |
|---|---|
| `disabled` | 默认值；不运行智能选择 |
| `shadow` | 生成选择决策和审计证据，但不自动绑定或改变用户流程 |
| `enforce_narrow` | 只对可验证的窄引用自动绑定，否则澄清 |
| `enforce_guarded_multi` | 在窄引用基础上，按 Skill profile 或明确多文件语义处理多选 |

旧的笼统值 `enforce` 不被接受；未知值会降级为 `disabled` 并记录配置审计事件。

### 参数与优先级

1. `metadata.upload_ids` 仍是最高优先级的显式文件引用；存在时跳过智能 selector。
2. 未显式提供 upload ID 时，服务端可以根据消息文本、稳定 `upload_id`、原始文件名、明确的文件序号和近期任务附件使用记录匹配文件。
3. 启用 selector 且进入选择路径时，`metadata.file_selection` 或 `metadata.file_requirement_profile` 可提供可选 profile；`metadata.soft_skill_binding` 中也接受同名 profile。

profile 支持：

```json
{
  "required": true,
  "allow_multiple": false,
  "expected_content": ["材料编号", "性状数据"],
  "supported_file_types": ["csv", "spreadsheet"],
  "helpful_columns": ["material_id", "trait"],
  "disambiguation_hint": "优先选择材料信息表"
}
```

运行时还支持 `source`、`user_file_reference`、`context_notes`，但普通客户端通常无需提交这些后端推导字段。

以下旧字段在 selector 校验路径中会被拒绝并返回 HTTP 400：

- `metadata.file_intent`
- profile 内的 `needs_file`、`intent`、`accepted_file_types`、`expected_inputs`
- `requires_file`、`required_file`、`default_allow_multiple` 等旧别名
- 未知 profile 字段或非法 `source`

### 决策行为

- 明确引用有效单文件：自动绑定。
- 明确请求多文件，且 profile 允许或文本明确表达比较/合并：可绑定多个文件。
- 文件必需但会话没有 active 文件：创建现有 interrupt，要求上传或补充文件。
- 重名文件、低置信度、无效 selector JSON、未知 upload ID、数量不匹配：创建或保持 interrupt，不猜测绑定。
- 删除文件、跨会话文件、伪造或不存在的精确 upload ID：在 LLM selector 和任务执行前 fail closed。
- 替换上传后继续沿用 interrupt provenance；多 sheet 文件继续复用既有 sheet-selection interrupt。
- 传给 selector 的候选只包含 prompt-safe metadata，不包含文件正文、真实路径或 `storage_key`。

## 4.5 能力缺失 LLM fallback

### 影响接口与事件

- `POST /api/v1/conversations/chat-messages`
- `GET /api/v1/tasks/{task_id}/events`
- `GET /api/v1/conversations/{conversation_id}/messages`

### `main` 的新行为

当 Planner / Replanner 或 soft Skill binding 指向已经下线、未注册或不可用的能力时，`main` 可以把本轮收敛为受约束的 `main_agent.respond`：

- 对可 fallback 的缺失能力，不再仅因能力消失而直接失败。
- assistant 正文必须以 `【能力缺口说明】` 明示当前缺口。
- 回答只能提供通用说明、草案或可手工复核建议。
- 不执行缺失 Skill / MCP，不得声称已经调用成功。
- `artifact_generation_allowed` 固定为 `false`，不得生成可下载文件。
- full fallback 不进入无意义的重复 replan；partial fallback 会说明已完成能力和未覆盖能力。

直接从外部请求提交不允许的 `capability_id=skill.*` 仍可能在任务创建前返回 HTTP 400；新增 fallback 主要覆盖合法编排流程中的缺失能力，以及已保存但现已失效的 soft binding。

### 新 SSE 事件

事件类型：`capability.missing_fallback`

```json
{
  "event_type": "capability.missing_fallback",
  "payload": {
    "enabled": true,
    "scope": "full",
    "reason_code": "skill_missing",
    "missing_capability_summary": "点名的 Skill 当前未注册或不可用",
    "fallback_content_scope": "仅提供可手工复核的通用建议",
    "llm_fallback_allowed": true,
    "artifact_generation_allowed": false,
    "disclosure_required": true,
    "memory_context_used": false,
    "source_message_count": 1
  }
}
```

枚举约束：

- `scope`: `full | partial`
- `reason_code`: `capability_missing | skill_missing | forced_skill_missing | mcp_missing`
- `attempted_capability_summary`: 仅 partial fallback 时可出现
- `source_message_ids`: 可选、受长度和数量限制

assistant 历史消息会在 `metadata.capability_missing_fallback` 中持久化同一份安全摘要。前端应同时支持实时 SSE 和历史 metadata 恢复，并忽略未列入契约的字段。

## 4.6 SeedPilot 外部路径与 Nginx 差异

### API 代理路径

两个分支都支持：

- `/seedpilot/api/*` 重写到后端 `/api/*`
- `/seedpilot/openapi.json` 重写到 `/openapi.json`
- `/seedpilot/api-doc` 和 `/seedpilot/api-doc/*` 代理到后端 API 文档路由
- `/seedpilot/docs`、`/seedpilot/redoc` 代理到 FastAPI 文档页

因此本文前述 `/api/v1/*` 是后端 canonical path；部署在 SeedPilot 子路径下时，外部调用地址为 `/seedpilot/api/v1/*`。

### `prod` 相对 `main` 的反向差异

`prod` 在 `/seedpilot/api-doc` 和 `/seedpilot/api-doc/*` location 中额外配置：

- 清空上游 `Accept-Encoding`
- `sub_filter_once off`
- 把响应正文中的 `/api-doc/` 改为 `/seedpilot/api-doc/`
- 把 `/openapi.json` 改为 `/seedpilot/openapi.json`

`main` 当前缺少这组 API 文档页内容重写。业务 JSON API 代理不受影响，但 API 文档 HTML 中使用根路径的链接时，可能落到被 Nginx 隔离的站点根路径并返回 404。合并或发布时应保留 `prod` 的这组 Nginx 规则。

---

## 5. 明确没有差异的事项

为避免把提交历史误判为最终分支差异，以下事项在两个最终分支中等价：

- 所有 FastAPI 业务 endpoint 的 path 和 method。
- 鉴权请求与响应 schema。
- `SubmitMessageRequest` 顶层字段。
- 上传 form 字段、上传/列表/删除 response schema。
- TSV 作为 CSV-family 输入的支持。
- `GET /api/v1/capabilities` 返回前刷新 Skill 列表的行为。
- task summary、graph、interrupt 列表、artifact 列表与下载接口的公开 schema。
- SeedPilot 业务 API 的 `/seedpilot/api/* -> /api/*` 代理路径。

---

## 6. 客户端迁移清单

### 必须完成

- [ ] 更新 `ModelEditionOptionResponse`，把 `reasoning_efforts` 视为必填。
- [ ] reasoning effort UI 按当前模型动态渲染，不使用全局常量。
- [ ] 关闭深度思考前检查 `allow_when_thinking_disabled` 和 `disabled_default`。
- [ ] 更新 `MessageResponse` 类型，接收 `message_type`、`metadata`、`updated_at`。
- [ ] 历史列表支持 `role=system` + `message_type=file_upload`，同时继续隐藏其他 system message。
- [ ] `file_status=deleted` 的文件只作历史展示，不重新附加。

### 强烈建议

- [ ] 处理 SSE `capability.missing_fallback` 并显示能力缺口提示。
- [ ] 从 assistant history 的 `metadata.capability_missing_fallback` 恢复同一提示。
- [ ] 遇到文件选择 interrupt 时沿用既有 interrupt 提交流程，不新建任务。
- [ ] 对上传/删除失败执行列表刷新或状态核对，不推断部分成功。
- [ ] SeedPilot 部署保留 `prod` 的 `/api-doc` 和 `/openapi.json` `sub_filter`。

### 不应执行

- [ ] 不要提交服务端真实路径、`storage_key`、完整文件内容或 `content_base64`。
- [ ] 不要继续使用 `metadata.file_intent` 或旧 file requirement 字段。
- [ ] 不要把能力缺失 fallback 当作 Skill 已执行成功或文件已生成。
- [ ] 不要把 assistant 正文中的普通文本链接当作 artifact 下载凭证。

---

## 7. 回归验证建议

### 契约验证

1. 分别生成 `main` 和 `prod` 的 `/openapi.json`，比较 paths、methods 和 schemas。
2. 对模型配置响应执行严格 JSON schema 解码。
3. 对历史消息执行旧 `chat` 消息、新 `file_upload` 消息和内部 system message 过滤测试。

### 行为验证

1. 对每个模型验证默认 effort、disabled-safe effort、非法 effort 和未知模型。
2. 验证显式 `metadata.upload_ids`、文件名引用、重名文件、删除文件、跨会话文件和多文件选择。
3. 注入上传 DB、描述文件、索引和本地清理失败，确认接口不会返回部分成功。
4. 模拟 stale soft Skill binding，确认任务完成、正文有披露、SSE/历史 metadata 可恢复且没有下载 artifact。

### 部署验证

在 SeedPilot 前缀下至少 smoke：

- `/seedpilot/api/v1/config/model-editions`
- `/seedpilot/openapi.json`
- `/seedpilot/api-doc`
- `/seedpilot/api-doc/API更新日志.md`
- `/seedpilot/docs`
- `/seedpilot/redoc`

---

## 8. 发布建议

本次 `main -> prod` API 合并可按以下优先级执行：

1. 先升级客户端类型和模型 reasoning effort UI。
2. 再部署后端模型契约、消息历史、文件一致性和 fallback 逻辑。
3. 保留 `prod` 已有的 SeedPilot API 文档 `sub_filter`，不要用 `main` 的较弱 Nginx location 覆盖。
4. 完成 OpenAPI diff、关键 API 回归、SSE 回放和 SeedPilot 子路径 smoke 后再切换流量。

> 维护口径：从本次扫描起，本文件只描述 `main` 与 `prod` 当前基线的 API 差异，不再保留已经同步完成的旧增量条目。字段级最终真值仍以目标分支实际生成的 `/openapi.json`、SSE 实现和发布镜像 smoke 结果为准。
