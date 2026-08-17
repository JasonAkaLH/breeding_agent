# `$` 用户级 MCP Server Soft Binding 设计

日期：2026-08-17

状态：设计已确认并完成仓库实现；自动回归与fake MCP E2E已通过，真实OCR discover-only smoke待受控环境引用

适用范围：`main` 分支开发环境的聊天输入解析、用户级 MCP Server 选择、`mcp.dispatch` 初始工作流、Tool discovery、Tool Selector、授权、历史消息展示和前后端回归

不适用范围：`prod` 分支和生产部署。生产发布必须另行审查和批准。

## 1. 问题与目标

当前前端支持 `/Skill` Slash Command。它由前端从 active `skill.*` capability 生成菜单并提交 `soft_skill_binding`，后端验证后仍由主代理判断是否使用 Skill。

用户级 MCP 目前只有自动 Server Router。用户不能像选择 Skill 一样主动指定某个 MCP Server，也不能在提交前看到可用 Server 候选。MCP Tool Catalog 按任务临时发现，不持久化，也不进入全局 Capability Registry，因此不能直接照搬 `/Skill` 的 capability 列表模型。

本设计增加 `$` 命令，使用户可以显式选择一个当前 `enabled + available` 的 MCP Server。选择后系统必须连接该 Server 并执行一次临时 Tool discovery；是否真正调用 Tool、调用哪个 Tool以及参数是什么，仍由受限 Tool Selector 和现有授权机制决定。

## 2. 核心语义

`$` 命令采用“Server 确定绑定、Tool 软执行”模型：

```text
用户选择 $Server
→ Server 固定且不可改路由
→ initialize + tools/list 必须执行
→ Selector 可以 call_tool / finish / stop
→ 只有 call_tool 进入授权与执行
```

这里的 Soft Binding 只表示 Tool 调用不是强制的；它不表示 Server Router 可以改选其他 Server。

## 3. 已确认决策

1. `$` 绑定整个 MCP Server，不绑定具体 Tool。
2. `$` 后使用 Server 显示名称进行检索和展示；提交身份始终是稳定 `server_id`。
3. 同名 Server 的直接文本匹配视为冲突，必须通过菜单点选具体 Server。
4. 包含空格的显示名称通过菜单点选，不增加引号语法。
5. 菜单只显示当前用户 `enabled + available`、未删除的 Server。
6. `$` 只在消息开头触发，与 `/Skill` 命令互斥。
7. `$` 绑定后跳过 Server Router，禁止 Selector `route_another_server`。
8. Tool discovery 必须发生；Selector 决定 `call_tool`、`finish` 或 `stop`。
9. `call_tool` 继续使用现有允许一次、始终允许、拒绝和 fingerprint 防重放机制。
10. 同一 Server 内允许多 Tool 调用，继续受每任务 20 次调用预算约束。
11. 非空文字或至少一个已选附件即可提交；两者都没有时阻止。
12. 附件只向 Selector 提供安全摘要，本轮不实现文件正文到 MCP 的传输桥接。
13. `$` badge 只对当前一条消息生效，提交成功或失败后清除。
14. 当前消息和历史恢复都显示提交时的 `$Server` badge。
15. 公共请求只提交稳定 `server_id`；命令文本和显示名称由后端从 owner-scoped Server 记录生成。
16. binding 验证必须先于 Message、Task、附件绑定、审计和任何远端网络副作用。

## 4. 明确不做

- 不设计 `$Server.tool_name` 或直接 Tool 命令。
- 不持久化 Tool Catalog、Tool Schema、MCP Client 或连接。
- 不把用户 MCP Tool 注册为全局 capability。
- 不允许 `$` 绑定后改路由到其他 Server。
- 不自动把聊天附件上传为 OCR `/uploads`、URL、base64、resource URI 或其他 MCP 参数。
- 不把本地路径、文件正文、base64、远端不可识别的内部上传 ID 交给 Selector。
- 不取消或合并 Tool 授权。
- 不修改 MCP Endpoint Policy、凭据加密、协议协商、远端 Task recovery 或 unknown/no-replay 语义。
- 不修改或部署 `prod`。
- 不为本功能回填旧历史消息的 `$` badge。

## 5. 总体数据流

```text
GET 当前用户 MCP Server
        ↓
前端过滤 enabled + available
        ↓
输入/点选 $显示名称
        ↓
保存一次性 selected server_id badge
        ↓
提交 capability_id=mcp.dispatch + mcp_server_binding
        ↓
后端按认证用户重新验证 Server
        ↓
生成内部 mcp_dispatch_server_id / explicit_command mode
        ↓
MCPDispatchWorkflowProvider 构建 dispatch + finalizer
        ↓
Gateway 打开固定 Server scope并临时 tools/list
        ↓
Selector call_tool / finish / stop
        ↓
call_tool 执行授权；finish/stop 不调用
        ↓
main_agent.respond 生成最终回答
```

## 6. 前端命令模型

### 6.1 安全 Server Profile

前端复用 `GET /api/v1/mcp/servers`，只把以下字段用于 `$` 候选：

- `server_id`
- `display_name`
- `routing_description`
- `transport`
- `enabled`
- `health_status`

不得把 Endpoint、凭据、Header、Tool Catalog 或内部错误正文放入候选模型。前端只保留本页面使用的安全 Profile，不持久化凭据或 Tool 信息。

App 是聊天页候选 Profile 的唯一 owner：

- 用户登录成功后加载一次；
- 登出或认证失效时立即清空；
- MCP 设置面板创建、编辑、重测、启停、删除完成后通过 callback 通知 App 重新加载；
- 用户点击候选刷新入口时可以显式重新加载；
- 输入 `$` 和过滤候选不得逐键发送网络请求。

候选缓存只用于交互；提交时后端必须重新验证 Server。

### 6.2 语法

```text
$OCR服务 识别这份文档
```

规则：

- 去除消息前导空白后，第一个字符必须是 `$`。
- `$` 不在消息中间触发。
- 直接文本 token 到第一个空白结束。
- 显示名称包含空格时必须点选菜单。
- 未知 token 返回 `not_found`；多个同名候选返回 `conflict`。
- 以 `$` 开头的未知或冲突输入必须阻止提交，不降级成普通文本。
- 直接文本匹配对拉丁字母大小写不敏感，保留 Unicode 原文且不做拼音/音译；规范化后重复仍视为冲突。

### 6.3 菜单与选择

输入 `$` 后显示 MCP Server listbox。候选可以通过下列安全字段搜索：

- `$命令文本`
- 显示名称
- 路由描述
- transport

菜单必须支持鼠标、Enter、Space、上下箭头和 Escape，并提供明确的 MCP Server 可访问名称。现有 Slash menu组件若复用，ARIA label、空状态和冲突文案必须参数化，不能继续固定为“Skill 命令列表”。候选只包含 `enabled=true AND health_status=available` 且未删除的 Server。

点选后：

- 保存稳定 `server_id`；
- 显示可移除 `$显示名称` badge；
- 从输入框移除命令前缀；
- 关闭菜单并重置 active index；
- 清除已选择的 Skill badge。

选择 `/Skill` 时必须对称清除 MCP badge。

### 6.4 提交资格与生命周期

满足以下任一条件即可提交：

- 命令后存在非空任务文字；
- 当前 composer 至少有一个已选附件。

两者都没有时保留 badge并提示“请说明任务或添加附件”。任务处于运行、取消或 Interrupt 等待状态时，遵循现有 composer gate，不得选择新的 `$` Server。

提交成功或失败后都清除 MCP badge。失败时若附件已上传，继续使用现有补偿和草稿恢复行为。

同一次 Enter、发送按钮或确认回调最多触发一个消息提交和一个 MCP discovery；提交开始后沿用现有 busy gate，禁止重复发送。

## 7. 提交 API 合同

前端提交：

```json
{
  "capability_id": "mcp.dispatch",
  "routing_mode": "force_capability",
  "metadata": {
    "mcp_server_binding": {
      "server_id": "mcp-7598..."
    }
  }
}
```

`mcp_server_binding` 是用户可提交的显式绑定合同，只允许：

| 字段 | 要求 |
|---|---|
| `server_id` | 非空字符串，长度受限，只作为后端 owner-scoped 查询键 |

禁止未知字段。API 客户端不得提交命令文本、显示名称、Endpoint、凭据、Tool、Schema 或 Server 状态作为信任依据。

## 8. 后端验证与内部 metadata

后端必须在保存用户 Message、创建 Task 或绑定附件前解析并验证 `mcp_server_binding`，随后执行：

1. 请求必须是 `capability_id=mcp.dispatch` 和 `routing_mode=force_capability`。
2. 使用当前认证用户与 `server_id` 查询 Server。
3. Server 必须满足：
   - owner 等于当前认证用户；
   - `enabled=true`；
   - `health_status=available`；
   - `deletion_pending=false`；
   - `deleted_at is null`。
4. 验证失败统一返回 HTTP 409 和 `{"detail":{"code":"mcp_bound_server_unavailable"}}`，不得暴露其他用户 Server 是否存在。
5. 验证成功后生成系统内部：
   - `mcp_dispatch_server_id`
   - `mcp_binding_mode=explicit_command`
   - `forced_by_mcp_command=true`
   - 由 Server 当前显示名称生成的脱敏 `mcp_command`
6. 内部 metadata 必须由后端生成；用户直接提交同名内部字段仍由 sanitizer 删除。

验证必须先于所有持久化和网络副作用。验证失败时不得创建或修改：

- 用户 Message；
- Task/TaskNode/Edge；
- conversation current task；
- 附件绑定；
- MCP intent/outbox/lease；
- 审计或远端连接。

验证成功后，根用户 Message 在首次保存时写入独立 public metadata：

```json
{
  "mcp_server_badge": {
    "server_id": "mcp-7598...",
    "display_name": "OCR服务",
    "command": "$OCR服务",
    "binding_mode": "explicit_command"
  }
}
```

`mcp_server_badge` 与 orchestration internal metadata 分离。公开历史接口只允许返回该安全 badge；不得返回 `mcp_dispatch_server_id`、Endpoint、凭据、Tool或Catalog。

Task 的 `requested_capability_id` 固定为 `mcp.dispatch`。显式 `$` 绑定会 supersede 当前 pending Skill context，与现有 force-capability 行为一致。

Gateway 建立连接前继续执行 owner、security version、health、Endpoint Policy 和 lease 校验。提交后状态漂移必须失败关闭，不得改选其他 Server、Skill 或普通 LLM。

## 9. 初始 `mcp.dispatch` 工作流

当前 `MCPDispatchWorkflowProvider` 已能基于内部 `mcp_dispatch_server_id` 重建 dispatch + finalizer。实施时必须明确支持初始显式绑定，而不只用于 Interrupt resume。

计划固定包含：

1. 一个 required `mcp.dispatch` 节点，input payload 只能是 `{server_id}`；
2. 一个依赖 dispatch 的 required `main_agent.respond` finalizer；
3. 禁止 Server Router 改写 `server_id`。

显式绑定不经过 Planner Server Router，但最终回答仍由主代理按任务范围生成。

会话标题只使用去除 `$` 命令后的用户任务文字；只有附件而无文字时使用现有文件/默认标题策略，不把 `$Server` 命令本身作为标题主体。

## 10. Tool discovery 与 Selector

绑定成功后必须打开所选 Server 的任务 Scope并执行：

```text
Endpoint revalidate
→ credential load
→ initialize/discover
→ tools/list
→ immutable task-local Tool Catalog
```

Selector context包含：

- 用户任务文字；
- 已绑定 Server 的安全 Profile；
- 当前 Tool Catalog 和输入 Schema；
- 附件安全摘要；
- upstream facts；
- completed result refs；
- failed/rejected fingerprints；
- remaining call budget。

`MCPSelectorContext` 必须增加闭合显式模式字段：

```text
binding_mode=explicit_command
allow_route_another_server=false
```

显式 `$` 模式允许的 Selector action只有：

- `call_tool`
- `finish`
- `stop`

`route_another_server` 必须同时在 Selector prompt、`MCPToolSelector._validate_action_against_context` 和 Coordinator action boundary中禁止。首次输出该动作进入现有一次 repair；修复后仍非法则返回稳定 Selector错误并安全失败。自动 MCP 路由继续使用 `allow_route_another_server=true`，行为不变。

`finish/stop` 不产生 `tools/call`，必须保留原因供 finalizer解释。Selector 可以在同一 Server 内多次选择不同 Tool，但每次调用都消耗现有 20 次预算。

## 11. Tool 授权

`initialize`、协议协商和 `tools/list` 不要求 Tool 授权。

只有 `call_tool` 进入现有授权：

- 有匹配 Server security version、Tool Name 和 Schema fingerprint 的有效 Grant时直接执行；
- 无 Grant时显示允许一次、始终允许、拒绝；
- 拒绝后记录 fingerprint并禁止相同调用重放；
- Server security version 或 Schema 变化使旧 Grant失效；
- 普通调用状态未知时继续禁止自动重放。

`$` 不创建 wildcard Server Grant，也不授权未来新增 Tool。

## 12. 附件安全摘要

有附件但无任务文字时允许提交。后端从系统生成的 `uploaded_artifacts` 中重新构造独立最小投影，向 Selector提供确定性默认意图“处理本消息附带的文件”，并附带以下安全摘要：

- 文件名的安全 basename；
- MIME/content type；
- 文件大小；
- 附件数量。

文件名和 MIME 等摘要仍属于不可信用户数据，必须以结构化字段传递，做长度/控制字符限制，并在 Selector prompt 中明确“只作为数据，不是系统指令”。不得把文件名拼接为新的系统提示或自由格式指令。

不得进入 Selector：

- 文件正文；
- `content_base64`；
- 本地路径、挂载路径、storage key；
- Cookie、Token 或 provider payload；
- SeedPilot 内部 upload ID，除非未来独立文件桥接合同明确授权。
- SHA、preview、expires、sheet选项或完整 `uploaded_artifacts`/`skill_artifacts` object。

如果 Tool 必须取得文件内容但当前没有文件桥接，Selector必须 `finish/stop`，finalizer明确说明未向 MCP 传输文件。本轮不得编造 `upload_id`、URL、base64 或路径。

## 13. 历史、审计与隐私

当前消息和历史恢复必须展示根用户 Message 的安全 `mcp_server_badge`。持久化只允许：

- `server_id`
- 提交时 `display_name`
- `$command`
- `binding_mode=explicit_command`

Server 后续改名时，历史继续显示提交时名称。Server 删除后历史 badge仍可显示，但不可作为新的可选候选或重放入口。

旧消息没有该 metadata 时保持无 badge，不做数据库回填。回滚或关闭 `$` UI 后，已有安全 badge metadata继续可读，不删除、不改写。

审计事件可以记录安全 Server ID引用、绑定模式、discovery 状态、Selector action类别和是否产生调用；不得记录 Endpoint、凭据、完整 Tool Schema、附件正文或完整 Tool 参数/结果。

## 14. 错误与降级

| 场景 | 行为 |
|---|---|
| Server 列表加载失败 | 关闭菜单，显示可重试提示 |
| `$` 未找到 | 阻止提交，提示未找到 MCP Server |
| 同名冲突 | 阻止直接文本提交，要求菜单点选 |
| 无文字且无附件 | 保留 badge，要求任务或附件 |
| 提交时 Server 不可用 | 返回 `mcp_bound_server_unavailable`，不创建替代调用 |
| discovery失败 | 明确失败/不可用，不改选其他 Server |
| Catalog为空或非法 | 使用现有 unavailable/invalid catalog行为 |
| Selector `route_another_server` | repair一次，仍非法则安全失败 |
| Selector `finish/stop` | 零 Tool调用并保留原因 |
| 用户拒绝 Tool | 不重放、不换 Server，finalizer说明未执行 |
| 附件需要文件桥接 | 不编造参数，finish/stop并说明限制 |
| Server执行中失效 | 继续使用现有 invalidation、取消和 unknown/no-replay边界 |

结构性 binding错误返回 422；通过结构校验但 Server 不属于当前用户或状态不可用时统一返回第8节定义的409合同。

任何错误都不得静默回退到其他 MCP、Skill 或未披露的纯 LLM回答。

## 15. 测试与验收

### 15.1 前端

- `$` 只在消息开头触发。
- menu只包含 `enabled + available` Server。
- 显示名称、描述和 transport搜索有效。
- 未知、同名冲突、空格名称点选行为确定。
- `/` 与 `$` badge互斥。
- badge选择、移除、成功/失败后清理符合一次性生命周期。
- 非空文字或附件任一存在即可提交。
- 前端公共 binding只提交 `server_id`。
- App候选在登录加载、登出清空、设置变更后刷新，输入过滤不逐键请求。
- 同一次用户发送最多产生一次 submit和一次 discovery。
- 当前消息与历史恢复显示提交时 `$Server` badge。

### 15.2 API与信任边界

- 合法 owner/enabled/available Server绑定成功。
- 跨用户、disabled、unavailable、deleted统一拒绝。
- 伪造内部 `mcp_dispatch_server_id`、Endpoint、Tool或凭据被删除/拒绝。
- 未知 binding字段、控制字符、超长字段拒绝。
- 提交与执行间状态漂移失败关闭。
- binding拒绝发生在 Message/Task/附件/审计副作用前。
- 后端忽略客户端显示文本并从 owner-scoped Server生成badge。
- 409/422错误合同分别覆盖状态拒绝和结构拒绝。
- 公开历史只返回 `mcp_server_badge`，不返回 orchestration internal metadata。

### 15.3 Orchestration与MCP

- 显式绑定生成固定 dispatch + finalizer plan。
- Server Router调用次数为零。
- discovery必定发生，Catalog不持久化。
- Selector `finish/stop` 产生零 `tools/call`。
- 显式模式下 `route_another_server` repair/拒绝。
- 自动路由模式仍允许既有 `route_another_server`。
- `call_tool` 继续经过 Grant/授权/fingerprint/预算。
- 同一 Server多 Tool循环和20次预算不回归。
- Interrupt、取消、断线、remote task recovery 和 unknown/no-replay不回归。

### 15.4 附件与隐私

- 只有附件时可以提交并生成安全默认意图。
- Selector只看到 basename、类型、大小和数量。
- 恶意文件名被限制并始终作为结构化不可信数据处理，不能注入 Selector 指令。
- 原文、路径、base64、storage key、Token、内部 upload ID不泄漏。
- 缺少文件桥接时产生可解释零调用结果。
- 完整 upload summary、SHA、preview、expires和内部ID不进入Selector。

### 15.5 验证分层

- 前端完整测试、typecheck、build。
- 用户级 MCP integration/API/storage定向回归。
- 真实 OCR Server discovery smoke验证固定 Server后可完成 `initialize + tools/list`。
- 真实 smoke不要求附件传输或 OCR Tool执行。
- 完整 API套件的既有无关失败必须单独披露，不得伪装为本功能回归。

## 16. 非功能要求

| 维度 | 要求 |
|---|---|
| 安全 | 后端以认证 owner和稳定 server_id为权威；内部 metadata不可伪造 |
| 隐私 | 不向前端/Planner/Selector泄漏 Endpoint、凭据、文件正文或内部路径 |
| 可访问性 | `$` listbox、badge、冲突/空状态支持键盘、焦点和实时提示 |
| 可靠性 | Server状态漂移失败关闭，不自动改路由或重放 |
| 兼容性 | `/Skill`、普通输入、Interrupt、MCP自动路由和现有Grant语义不变 |
| 资源 | Tool Catalog仍按任务临时存在，任务结束/取消后释放 |
| 可观测性 | 记录绑定模式、Server安全引用、discovery/Selector类别，不记录敏感内容 |

## 17. 风险、假设、迁移与回滚

### 17.1 已接受风险

- 用户输入 `$` 后，即使 Selector最终 `finish/stop`，系统也会携带当前配置的认证信息连接远端 Server并执行 Tool discovery。这是 `$` 命令的明确外部网络副作用，不需要 Tool授权。
- 远端 Tool Name、描述、annotations和Schema属于不可信数据；它们只能作为 Selector结构化输入，不能作为系统指令。
- 频繁使用 `$` 会增加远端 discovery和本地临时Scope负载，但Catalog仍不持久化，资源按任务释放。
- 只有附件但没有文件桥接时，用户可能得到“已发现工具但未执行”的结果；finalizer必须明确披露。

### 17.2 假设

- 当前 `GET /api/v1/mcp/servers` 是认证用户读取自己 Server Profile 的权威来源。
- `mcp.dispatch` 的现有Gateway、Selector、授权、预算和finalizer链路可复用；本设计只增加显式绑定模式。
- 旧历史消息不具备可靠的提交时 Server显示名称，因此不回填badge。

### 17.3 迁移与回滚

- 新metadata是加法字段，不修改现有Message/Task schema；旧客户端和旧历史继续工作。
- 回滚前端时停止生成 `$` binding，但保留历史 `mcp_server_badge` 数据。
- 回滚backend时，新客户端提交的 binding可能被旧runtime忽略或拒绝，因此前后端必须同一开发版本发布/回滚。
- 回滚不得删除Message、Task、Grant、MCP调用记录或badge metadata。
- `prod`不在本轮，不存在生产数据迁移或回滚动作。

## 18. 主要实施面

| 范围 | 主要文件/组件 |
|---|---|
| `$` domain/parser | 新的 frontend domain模块及测试，或抽取可复用command parser |
| Profile状态/menu/badge | `frontend/src/App.tsx`、`MCPSettingsPanel.tsx` callback、命令菜单组件、样式和历史消息模型 |
| API metadata | `frontend/src/api/types.ts`、`client.ts`、`src/api/dto.py`、`runtime.py` |
| Server binding验证 | 用户级 MCP config service/storage查询、system metadata sanitizer |
| 初始工作流 | `src/capabilities/mcp_dispatch/workflow.py`、workflow router/service |
| Selector约束 | `models.py`、`selector.py`、`dispatch_coordinator.py` |
| 附件摘要 | upload/artifact安全投影与 MCP Selector context组装 |
| 历史与审计 | 根用户Message安全metadata、公开历史投影、前端恢复、MCP audit字段allowlist |
| 文档 | 用户级 MCP PRD、API更新日志、AGENTS索引、CHANGELOG |

## 19. 发布边界与完成条件

实施只进入 `main`。完成条件：

1. 第15节全部定向验收通过；
2. 前端完整测试/typecheck/build通过；
3. MCP integration、相关API/storage回归通过；
4. 真实OCR Server固定绑定discovery smoke通过；
5. 附件正文未传输的安全边界有测试证据；
6. 相关PRD、API文档、AGENTS和CHANGELOG同步；
7. `prod`未修改、未构建、未部署。

本设计实施完成不代表附件桥接、OCR Tool调用或生产发布完成。
