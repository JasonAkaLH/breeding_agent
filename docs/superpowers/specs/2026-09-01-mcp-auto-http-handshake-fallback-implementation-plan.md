# MCP `auto` HTTP 握手拒绝降级实施计划

依据：`2026-09-01-mcp-auto-http-handshake-fallback-design.md`

## 状态

`approved_ready_to_implement`

用户已复核书面设计并批准实施。本计划完成后才允许修改生产代码。

## 完成声明

只有同时满足以下条件，才可声明实施完成：

1. 2026 modern initialize 的稳定 `mcp_http_error` + 精确整数 status 400/404/405 各自关闭 modern candidate 并只进入一次 unpinned `2025-11-25` initialize；
2. `MCPProtocolError`、`MCPRemoteError` 既有 fallback 行为不变；
3. auth、429、5xx、network、timeout、cancel、local error、modern close failure、legacy failure 和 Tool 阶段错误不新增 fallback；
4. 显式协议与 `legacy_http_sse + auto` 继续 pinned；
5. 自动门禁通过，最终 diff 没有响应正文匹配、transport 改造或范围外变化；
6. 用户提供目标通过脱敏真实 auto initialize，实际协商为 `2025-11-25`，不调用 Tool。

若第 6 项因外部状态不能运行，必须记录精确缺口，不得宣称真实连接通过。

## Checkpoint A：锁定红测

### 修改范围

- `tests/integrations/mcp/test_user_mcp_auto_negotiation.py`

### 测试

1. 表驱动构造 `MCPClientError(code="mcp_http_error", metadata={"status_code": status})`；
2. 对 400、404、405 分别断言：
   - modern initialize 一次；
   - modern close 一次；
   - legacy factory 一次且收到 `2025-11-25`；
   - legacy initialize 一次；
3. 对 429、500 分别断言原异常抛出、modern 不关闭、legacy factory 零调用；
4. 保留现有 auth/network/timeout/cancel/local error 回归，不合并或放宽其断言。

### 红门禁

```bash
conda run -n multi_agent python -m unittest \
  tests.integrations.mcp.test_user_mcp_auto_negotiation
```

旧实现必须只在新 HTTP 400/404/405 fallback 用例失败；若其他既有用例失败，先查明原因，不进入生产修改。

## Checkpoint B：最小生产实现

### 修改范围

- `src/integrations/mcp/user_client.py`

### 实现

1. 导入既有 `MCPClientError`；
2. 增加私有 closed status 集合 `{400, 404, 405}`；
3. 增加私有纯 helper：
   - `MCPProtocolError` / `MCPRemoteError` 返回 true；
   - 其他异常必须是 `MCPClientError`；
   - error code 必须精确为 `mcp_http_error`；
   - `type(metadata.status_code) is int`；
   - status 必须属于 closed 集合；
4. `_AutoNegotiatingAdapter.initialize()` 只捕获 `MCPClientError`：
   - helper false 时裸 `raise`，保留 traceback/type/metadata；
   - helper true 时复用既有 close-before-switch 和唯一 legacy candidate 代码；
5. 非 `MCPClientError` 不捕获；list/call/cancel/close/recovery 不调用 helper。

不得修改 `transport_http.py`、`adapter_2026.py`、Gateway、Health 或异常公共导出。

### 绿门禁

```bash
conda run -n multi_agent python -m unittest \
  tests.integrations.mcp.test_user_mcp_auto_negotiation
```

## Checkpoint C：相关回归与静态证明

按由窄到宽顺序运行：

```bash
conda run -n multi_agent python -m unittest \
  tests.integrations.mcp.test_2026_07_28_adapter \
  tests.integrations.mcp.test_streamable_http_versions \
  tests.integrations.mcp.test_protocol_version_negotiation \
  tests.integrations.mcp.test_2025_11_25_task_recovery \
  tests.integrations.mcp.test_user_mcp_gateway \
  tests.integrations.mcp.test_user_mcp_health

conda run -n multi_agent python -m unittest discover \
  -s tests/integrations/mcp -p 'test_*.py'

conda run -n multi_agent python -m compileall -q \
  src/integrations/mcp tests/integrations/mcp

conda run -n multi_agent ruff check \
  src/integrations/mcp/user_client.py \
  tests/integrations/mcp/test_user_mcp_auto_negotiation.py

conda run -n multi_agent python -c 'import src.integrations.mcp'

git diff --check
```

静态复核：

- `MCP_SESSION_REQUIRED`、真实 Endpoint、Bearer 和响应正文不得进入变更；
- 生产代码只允许一个 auto owner 文件变化；
- 测试只使用稳定错误类型、code 和 status；
- `docker_cmd.md` 必须继续存在、ignored 且 untracked，不读取其内容。

## Checkpoint D：真实脱敏 smoke

使用当前实际 `UserMCPClientFactory.create_from_validated_endpoint()` 与 `protocol_preference=auto`，对用户提供的 Server 只执行：

1. Endpoint Policy revalidation；
2. auto initialize；
3. 读取内存中的 `MCPNegotiatedSession`；
4. close。

只输出：成功/失败、adapter 类别、requested/negotiated version、transport family、pinned flag、Session ID 是否存在、capability key。不得输出 Endpoint、Header、凭据、Session ID、响应正文或 Tool descriptor；不得执行 `tools/list` 或 `tools/call`。

预期结果：modern 400 进入一次 legacy initialize，最终 negotiated version 为 `2025-11-25` 且 Session ID 存在。

## Checkpoint E：状态同步和实现检查点

自动与真实验证完成后：

- 设计状态更新为 `implemented_verified`；
- 本计划更新为 `complete` 并记录实际测试数、skip 和真实 smoke 结果；
- `docs/AGENTS.md` 与 `CHANGELOG.md` 同步最终状态；
- 检查本次变化是否需要更新 `src/integrations/AGENTS.md` 与 `tests/AGENTS.md` 的 auto 行为摘要；
- `git diff --check`、最终 diff、Git status 复核后创建单一实现检查点。

建议提交信息：

```text
fix(mcp): fallback auto on HTTP handshake rejection
```

## 回滚

回滚实现检查点即可恢复旧异常边界；设计提交保持历史证据。无数据库、schema、缓存、外部服务、镜像或部署回滚。

License Requirement：复用现有 Python、MCP adapters、typed errors、unittest 与仓库工具链；无新增依赖或许可变化。
