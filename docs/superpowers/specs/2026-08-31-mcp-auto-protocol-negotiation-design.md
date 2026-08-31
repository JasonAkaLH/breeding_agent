# MCP `auto` 协议协商最小设计

## 状态

`written_review_pending`

对话中的目标、范围和方案 A 已获批准；本文等待用户复核后再生成实施计划。当前没有业务代码变更。

## 问题与证据

当前 `streamable_http + protocol_preference=auto` 固定先创建 `2026-07-28` adapter。只有 `server/discover` 返回少数预期错误类型时，`safe_auto_downgrade_version()` 才根据错误内容选择一个 pinned legacy 版本。由此产生两个问题：

1. 服务端以 HTTP 200 业务 JSON、普通 JSON-RPC error 或其他非标准响应表达“不支持当前协议”时，现代握手失败但不会进入 legacy 协商；
2. legacy client 使用 pinned 版本，没有利用标准 `initialize` 已提供的版本协商能力。

一次只读 QA 探测证明真实服务可能在现代 POST 上返回非标准业务响应，同时在 legacy `initialize` 中接受客户端提出的较新版本并返回实际兼容版本。该服务及其凭据只作为问题证据，不成为设计依赖、fixture 或自动回归环境。

外部对比结论：

- MCP `2025-11-25` lifecycle要求客户端提出其支持的最新版本，服务端可以返回自身支持的版本；客户端接受受支持结果并在连接内固定。
- Codex `0.149.1` 和本地 cc-agent 都在连接初始化时确定版本，成功后不因 `tools/list` 或工具调用失败而切换协议。
- Codex 把网络瞬态重试与协议/时代选择分开；认证或网络失败不表示协议不兼容。

## 目标

- 从本地支持的最新协议开始尝试。
- 不依赖标准 unsupported error、错误文案、HTTP body 或状态码才能进入下一候选。
- 只在初始化阶段、且服务端已经响应但当前 MCP 握手未成立时切换一次。
- 成功后在当前 Gateway scope 内固定 adapter、transport family 和协商版本。
- 保持显式协议、显式 transport、恢复协议 pin 和运行时执行语义不变。

## 非目标

- 不自动切换 `streamable_http` 与 `legacy_http_sse`。
- 不逐个 pinned 尝试所有历史版本。
- 不增加跨会话缓存、数据库字段、schema 或数据迁移。
- 不修改 Endpoint Policy、认证、Health Runner、Gateway、Router/Selector、Frontend、协议列表或外部 MCP Server。
- 不为任意未知异常增加 fallback，也不处理假设性的未来协议。

## 已批准决策

### Transport 继续显式

`UserMCPServer.transport` 仍是执行约束：

- `streamable_http` 的 auto 只在该 transport 内协商现代与初始化式协议；
- `legacy_http_sse` 的 auto 继续固定为该 transport 当前唯一允许的 `2024-11-05`；
- auto 不根据 URL 后缀、Content-Type 或失败结果改变 transport。

### Streamable HTTP 两阶段协商

`streamable_http + auto` 按固定两阶段执行：

```text
2026-07-28 MCP2026Adapter.initialize()
  ├─ success
  │    -> keep current adapter and negotiated session
  ├─ MCPProtocolError or MCPRemoteError
  │    -> close modern candidate
  │    -> one legacy initialize request at 2025-11-25, unpinned
  │    -> accept a locally supported negotiated version
  └─ auth / endpoint / network / timeout / cancellation / local error
       -> fail without protocol fallback
```

不建立 `2025-11-25 → 2025-06-18 → 2025-03-26` pinned 循环。第二阶段由服务端 `initialize` 响应选择共同版本。

### 会话内固定

不增加持久化字段：

- `UserMCPServer.protocol_preference=auto` 继续表示用户意图；
- `_AutoNegotiatingAdapter._active` 保存当前选中的 adapter；
- `MCPNegotiatedSession.negotiated_protocol_version` 与 `transport_family` 保存实际结果；
- Gateway scope 持有 adapter，scope 关闭后丢弃，下次新连接重新协商；
- 现有 durable task recovery 继续使用调用时已经保存的实际协议版本，不重新 auto 选择。

## 最小实现

### `src/integrations/mcp/user_client.py`

- `_legacy_adapter()` 增加内部 pinned 参数；显式协议和 legacy SSE 调用继续传 `True`。
- auto 的 legacy factory固定请求 `2025-11-25`，并传 `pinned_protocol_version=False`。
- `_AutoNegotiatingAdapter.initialize()`：
  - modern 成功时保持现状；
  - 只捕获初始化阶段的 `MCPProtocolError` 与 `MCPRemoteError`；
  - 切换前必须成功关闭 modern candidate；
  - legacy 成功后读取 `negotiated_session`；
  - 仅当实际版本为 `2025-11-25` 时再包裹现有 `MCP2025TasksAdapter`，较早版本保持基础 adapter。
- legacy candidate 失败时原样抛出，不再创建其他候选。
- `list_tools()`、`call_tool()`、cancel、close 和 durable recovery不增加 fallback。

不新增通用策略类、缓存管理器、配置项或候选注册框架。

### `src/integrations/mcp/adapter_2026.py`

新策略不再消费 `safe_auto_downgrade_version()`。删除该函数、对应 export 与只验证旧错误内容映射的测试，避免仓库同时保留两套 auto authority。

### `src/integrations/mcp/__init__.py`

同步删除 `safe_auto_downgrade_version` 的 package import 与 `__all__` 项，保证删除 helper 后公共包入口仍可正常导入。

## 错误行为

候选切换只依据现有异常类型，不检查异常字符串或 response metadata：

| 初始化结果 | 行为 |
|---|---|
| modern success | 固定 modern adapter |
| `MCPProtocolError` | 关闭 modern，尝试一次 legacy initialize |
| `MCPRemoteError` | 关闭 modern，尝试一次 legacy initialize |
| `MCPAuthRequiredError` | 立即失败 |
| 其他 `MCPClientError`，含连接与 timeout | 立即失败 |
| `EndpointPolicyError`、`TimeoutError`、`CancelledError`、本地异常 | 立即失败 |
| legacy candidate 任意失败 | 返回该失败，不继续尝试 |

这里的认证失败特指 transport/adapter 已形成的 typed `MCPAuthRequiredError`；不得解析普通 `MCPRemoteError` 的 message 来猜测认证语义，普通 remote error 仍按已批准规则进入一次 legacy candidate。

成功后的 tool discovery、catalog validation 或工具执行失败不属于协议协商，绝不改变 `_active`。

## 测试设计

聚焦测试必须覆盖：

1. modern 成功，不创建 legacy candidate；
2. unsupported、method-not-found、普通 `MCPProtocolError` 与普通 `MCPRemoteError` 都只切换一次；
3. auth、其他 client/network error、timeout 和 cancellation 不创建 legacy candidate；
4. modern candidate 在切换前精确关闭一次，关闭失败时停止；
5. legacy 从 `2025-11-25` 发起 unpinned initialize并接受较早的本地支持版本；
6. 实际协商为 `2025-11-25` 时使用 Tasks adapter，较早版本不误启用 Tasks；
7. legacy candidate 失败时无第三次尝试；
8. 握手成功后的 `tools/list` 失败不切换；
9. 显式协议仍 pinned 且不降级；
10. `legacy_http_sse + auto` 仍为 `2024-11-05`；
11. Gateway/Health 使用同一 factory 时能够取得正确 `negotiated_session` 和 catalog协议版本。
12. `import src.integrations.mcp` 成功，且业务源码与测试中 `safe_auto_downgrade_version` 零引用。

自动测试使用确定性 fake adapter/transport，不访问 QA 服务，不保存 Endpoint、Header、凭据或响应正文。

## 验证范围

- 新增/更新 auto client聚焦单元测试；
- 运行 2026 adapter、2025 Tasks、task recovery、Gateway 与 Health相关回归；
- 运行 compileall、变更面 Ruff 和 `git diff --check`；
- 验证 MCP package入口可导入，并以静态搜索确认旧 helper 零引用；
- 最终 diff确认无 DTO、数据库、schema、配置、Frontend、Rust、镜像、部署或 `prod` 变化。

## 风险与回滚

主要风险是把现代服务端的普通 remote initialization error识别为 candidate 不兼容，额外执行一次 legacy initialize。该行为正是已批准的“收到响应但握手未成立则尝试下一候选”规则；范围被严格限制在第一次初始化，认证、网络、timeout和运行期错误均不会触发。

回滚时恢复原 `_AutoNegotiatingAdapter` 和 `safe_auto_downgrade_version()` 即可；没有数据、schema、缓存或外部服务回滚。

## 参考

- MCP 2025-11-25 lifecycle：<https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle#version-negotiation>
- MCP 2026-07-28 compatibility：<https://modelcontextprotocol.io/specification/2026-07-28/basic/lifecycle#backward-compatibility-with-initialization-based-versions>
- Codex 0.149.1 protocol mode：<https://github.com/openai/codex/blob/rust-v0.149.1/codex-rs/rmcp-client/src/protocol_mode.rs>

License Requirement：复用现有 Python、MCP adapters、typed errors、Gateway scope 与 unittest；无新增依赖或许可变化。
