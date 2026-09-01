# MCP `auto` HTTP 握手拒绝降级设计

## 状态

`approved_planned`

用户已复核本文并批准实施；详细步骤见同日 implementation plan，当前尚未修改生产代码。

## 问题与证据

当前 `streamable_http + protocol_preference=auto` 先由 `MCP2026Adapter.initialize()` 执行无 Session 的 `server/discover`。`_AutoNegotiatingAdapter.initialize()` 只有在该调用抛出 `MCPProtocolError` 或 `MCPRemoteError` 时才关闭现代 candidate，并执行一次 unpinned `2025-11-25` initialize。

一次经用户授权的只读真实连接证明存在以下兼容形态：

- 2026 `server/discover` 收到带普通 JSON body 的 HTTP 400，语义为服务端要求先建立 MCP Session；
- 同一 Endpoint、Transport 与凭据使用 unpinned `2025-11-25` initialize 成功，实际协商为 `2025-11-25`，服务端颁发 Session ID 并公开 `tools` capability；
- Endpoint、Header、凭据和原始响应正文不写入仓库、fixture、自动测试或日志。

该服务是正常的 2025 session-era Streamable HTTP Server，但没有实现 2026 stateless request handling。现有 transport 将带 body 的非结构化 HTTP 400 映射为 `MCPClientError(code="mcp_http_error")`，因此 auto 状态机不会进入已存在且能够成功的第二阶段。

## 协议依据

- MCP `2026-07-28` 删除协议级 Session 与 `initialize/initialized`，请求携带自身协议和 client metadata；`server/discover` 是可选的 stateless discovery RPC。
- MCP `2025-11-25` 及更早的 session-era Streamable HTTP Server 可以在 initialize 响应中颁发 `Mcp-Session-Id`，并对缺少该 Header 的后续请求返回 HTTP 400。

因此本问题不是请求 JSON、Bearer 或 Endpoint Policy 错误，而是现代 candidate 已得到服务端明确 HTTP 响应、但 2026 握手没有成立。

## 目标

- 让 2026 初始 `server/discover` 的明确 HTTP 握手拒绝进入现有一次性 2025 fallback。
- 不依赖 `MCP_SESSION_REQUIRED` 或任何其他响应正文、Server framework、Header 或错误文案。
- 保持认证、限流、服务故障、网络、timeout、取消和运行期错误不降级。
- 保持显式 transport、显式协议、实际协商版本固定和 durable recovery 合同不变。
- 以最小生产修改解决已复现的真实 2025 Server 兼容问题。

## 非目标

- 不修改外部 OCR MCP Server。
- 不自动切换 `streamable_http` 与 `legacy_http_sse`。
- 不新增协议候选、逐版本 pinned 循环或跨连接缓存。
- 不修改 `StreamableHTTPTransport` 的通用 HTTP 错误映射。
- 不在 `tools/list`、`tools/call`、cancel、recovery 或其他运行期路径增加 fallback。
- 不新增异常公共类型、配置项、DTO、schema、数据库字段、Frontend、Rust、镜像、部署或 `prod` 变化。

## 已批准方案

### 唯一判定入口

在 `src/integrations/mcp/user_client.py` 增加一个私有纯判定函数，由 `_AutoNegotiatingAdapter.initialize()` 独占使用。该函数只接受：

1. 现有 `MCPProtocolError`；
2. 现有 `MCPRemoteError`；
3. `MCPClientError` 且同时满足：
   - `mcp_error_code == "mcp_http_error"`；
   - `metadata.status_code` 的类型精确为 `int`（不接受 `bool` 或字符串）；
   - `metadata.status_code` 精确属于 `{400, 404, 405}`。

不读取 `str(error)`、response body、framework detail、Endpoint、Server name 或认证 metadata。未知、缺失、字符串化或其他 status code 均不匹配。

### 状态机

```text
2026-07-28 MCP2026Adapter.initialize()
  ├─ success
  │    -> keep modern adapter
  ├─ existing MCPProtocolError / MCPRemoteError
  │    -> close modern -> one unpinned 2025-11 initialize
  ├─ mcp_http_error with exact status 400 / 404 / 405
  │    -> close modern -> one unpinned 2025-11 initialize
  └─ auth / 429 / 5xx / endpoint / network / timeout / cancellation / local error
       -> propagate without fallback
```

三个 HTTP 状态只在现代 adapter 的第一次 `initialize()` 调用边界解释为握手不兼容候选：

- `400` 覆盖 session-era Server 对无 Session 非 initialize 请求的标准拒绝；
- `404` 覆盖 Endpoint 存在但不公开现代 discovery route/method 的拒绝；
- `405` 覆盖现代 discovery POST 在服务端不允许的拒绝。

即使相同状态出现在 Tool 或恢复路径，也不会经过该判定入口。

### 错误边界

| 现代初始化结果 | 行为 |
|---|---|
| success | 固定 2026 adapter |
| `MCPProtocolError` / `MCPRemoteError` | 一次 2025 fallback |
| `mcp_http_error` + 400/404/405 | 一次 2025 fallback |
| `MCPAuthRequiredError`，含 401/403 | 原样失败 |
| `mcp_http_error` + 429 | 原样失败 |
| `mcp_http_error` + 5xx | 原样失败 |
| 其他 `MCPClientError`，含 transport/session/timeout code | 原样失败 |
| `EndpointPolicyError`、`TimeoutError`、`CancelledError`、本地异常 | 原样失败 |
| modern close 失败 | 原样失败，不创建 legacy candidate |
| legacy candidate 任意失败 | 原样失败，不创建第三候选 |

该方案不把 HTTP 400 普遍解释为协议不兼容；解释权只存在于 `streamable_http + auto` 的现代初始化边界。

本文只替代上游 `2026-08-31` 设计“其他 `MCPClientError` 一律停止”这一行的窄边界；其 transport 显式、两候选、close-before-switch、实际版本固定、Tasks wrapper 和恢复合同继续有效。

## 最小实现面

### `src/integrations/mcp/user_client.py`

- 导入现有 `MCPClientError`。
- 增加私有常量或内联闭合集合 `{400, 404, 405}`。
- 增加私有纯判定函数，严格处理异常类型、稳定错误码和整数 status。
- `_AutoNegotiatingAdapter.initialize()` 捕获 `MCPClientError` 后调用该函数：不匹配则裸 `raise`；匹配则复用现有 close、legacy factory、initialize 和 Tasks wrapper 逻辑。
- 非 `MCPClientError` 继续不捕获。

不修改 `src/integrations/mcp/transport_http.py`、`adapter_2026.py`、Gateway、Health 或调用路径。

### `tests/integrations/mcp/test_user_mcp_auto_negotiation.py`

- 新增 HTTP 400/404/405 初始化错误各自只 fallback 一次的表驱动回归。
- 锁定 modern 精确 close 一次、legacy 精确 initialize 一次、请求版本仍为 unpinned `2025-11-25`。
- 新增或扩充 HTTP 429/500 不 fallback 回归。
- 保留 auth、network、timeout、cancel、local error、close failure、legacy failure、Tool 阶段失败和显式 pin 既有合同。
- fake error 只携带稳定 code/status，不使用真实响应 body、Endpoint 或凭据。

## 验证

实施完成声明至少需要：

1. `tests.integrations.mcp.test_user_mcp_auto_negotiation` 全绿；
2. 2026 adapter、Streamable HTTP、Gateway、Health、2025 Tasks/recovery 相关回归全绿；
3. MCP integrations 全量回归；
4. `compileall`、变更面 Ruff、MCP package import 与 `git diff --check` 通过；
5. 静态检查确认生产变化仅在批准的 auto owner，不出现响应正文匹配；
6. 用户提供目标只做进程内脱敏 initialize smoke：最终实际协商为 `2025-11-25`，不调用 Tool，不保存 Endpoint、Header、凭据或响应正文。

若外部 smoke 因网络、认证或服务状态无法运行，必须如实记录为验证缺口，不影响确定性自动测试的完成状态，也不得宣称真实连接通过。

## 风险与回滚

主要风险是现代 Endpoint 对 `server/discover` 返回 400/404/405、但 2025 initialize 同样失败，因此额外产生一次只读初始化请求。状态机仍只有两个 candidate，第二次失败原样返回，不会重放 Tool 或业务请求。

回滚只需恢复 `_AutoNegotiatingAdapter.initialize()` 的旧异常边界和对应测试；没有数据、schema、缓存、外部 Server 或部署回滚。

## 参考

- MCP 2026-07-28 release：<https://blog.modelcontextprotocol.io/posts/2026-07-28/>
- MCP 2025-11-25 Streamable HTTP：<https://modelcontextprotocol.io/specification/2025-11-25/basic/transports>
- 上游设计：`docs/superpowers/specs/2026-08-31-mcp-auto-protocol-negotiation-design.md`

License Requirement：复用现有 Python、MCP adapters、typed client errors、Gateway scope 与 unittest；无新增依赖或许可变化。
