# MCP `auto` 协议协商最小实施计划

依据：`2026-08-31-mcp-auto-protocol-negotiation-design.md`

设计提交：`33b1dba2`；硬伤审查提交：`51535242`

状态：`complete`

目标分支：`main`

## 1. 完成声明与范围

完成后必须同时满足：

1. `streamable_http + auto` 先执行一次 `2026-07-28` modern initialize；
2. 只有该 initialize 抛出 `MCPProtocolError` 或 `MCPRemoteError` 时，先成功关闭 modern candidate，再执行一次请求 `2025-11-25` 的 unpinned legacy initialize；
3. auth、其他 client/network error、Endpoint、timeout、取消和本地异常不创建 legacy candidate；
4. legacy initialize 接受本地支持的实际协商版本，不再逐个 pinned 尝试历史版本；
5. 实际协商为 `2025-11-25` 时启用既有 Tasks adapter，较早版本保持基础 adapter；
6. 首次成功后由现有 `_active`、`MCPNegotiatedSession` 和 Gateway scope 固定，`tools/list`、`tools/call` 失败不重新协商；
7. 显式协议继续 pinned，`legacy_http_sse + auto` 继续固定 `2024-11-05`，durable recovery 合同不变；
8. 删除旧 `safe_auto_downgrade_version` authority，MCP package 入口仍可导入。

生产代码只允许修改：

- `src/integrations/mcp/user_client.py`
- `src/integrations/mcp/adapter_2026.py`
- `src/integrations/mcp/__init__.py`

不修改 DTO、数据库、schema、配置、Endpoint Policy、认证、Gateway、Health Runner、Router/Selector、Frontend、Rust、镜像、部署、外部 MCP Server 或 `prod`；不保存 QA Endpoint、Header、凭据和响应正文；不新增依赖、缓存、字段、通用策略类或候选注册框架。

## 2. Checkpoint A：聚焦红测

### 2.1 新增 auto 状态机测试

新增 `tests/integrations/mcp/test_user_mcp_auto_negotiation.py`，使用确定性 fake adapter 和 factory，不访问网络。fake 只提供 initialize 结果/异常、`negotiated_session`、关闭计数、Tool 调用计数和必要委托接口。

先写下列合同并在旧实现上确认精确失败：

- modern initialize 成功后不调用 legacy factory，后续操作仍委托 modern；
- 普通 `MCPProtocolError`、普通 `MCPRemoteError` 及既有 unsupported/method-not-found 子类各自只创建一个 legacy candidate；
- `MCPAuthRequiredError`、其他 `MCPClientError`、`TimeoutError`、`asyncio.CancelledError` 和本地异常均原样抛出，legacy factory 调用次数为零；
- fallback 前 modern 精确关闭一次；modern close 失败时原样停止，legacy factory 调用次数为零；
- legacy initialize 失败时原样抛出，且没有第三个 candidate；
- legacy 成功后 `_active` 固定；后续 `list_tools()` 失败不创建新 candidate；
- legacy 实际协商为 `2025-11-25` 时才包裹 Tasks adapter，实际为 `2025-06-18` 或 `2025-03-26` 时不包裹；
- wrapper 的 `negotiated_session`、`server_capabilities`、close/cancel/call 委托继续来自最终 active adapter。

旧实现必须至少因“普通 typed protocol/remote error 不 fallback”和“fallback 仍由错误内容映射到 pinned 版本”失败；红测不提交。

### 2.2 Factory 参数合同

在同一测试文件通过受控 factory seam 断言：

- `streamable_http + auto` 的 legacy candidate 固定从 `2025-11-25` 发起，`pinned_protocol_version=False`，且 initialize 前不预先启用 Tasks wrapper；
- 显式 `2025-11-25`、`2025-06-18`、`2025-03-26` 继续 `pinned_protocol_version=True`；
- 显式 `2025-11-25` 继续使用 Tasks adapter；
- `legacy_http_sse + auto` 继续请求 pinned `2024-11-05`；
- `2026-07-28` 显式协议继续只创建 modern adapter。

不要通过访问私有 transport 字段验证参数；只在 factory/helper 边界捕获构造参数与 adapter 类型，避免把测试绑到 HTTP 实现细节。

### 2.3 删除旧 helper 的测试迁移

修改 `tests/integrations/mcp/test_2026_07_28_adapter.py`：

- 删除 `safe_auto_downgrade_version` import 和只验证旧错误内容映射的测试；
- 保留 HTTP 400 structured unsupported response 被解析为 `MCPUnsupportedProtocolVersionError` 的 adapter 合同，只移除 helper 映射断言；
- 不削弱 2026 discover、header、tasks 或错误类型覆盖。

## 3. Checkpoint B：最小实现

### 3.1 Factory 构造

修改 `src/integrations/mcp/user_client.py`：

- `_legacy_adapter()` 仅增加内部 `pinned_protocol_version` 与是否预包裹 2025 Tasks 的窄参数，默认值保持显式协议和 legacy SSE 现状；
- `streamable_http + auto` 的 legacy factory 不再接收错误推导版本，固定构造请求 `2025-11-25`、`pinned_protocol_version=False` 的未包裹基础 adapter；
- 向 `_AutoNegotiatingAdapter` 提供一个只负责包裹现有 `MCP2025TasksAdapter` 的 factory；
- 不修改 `create_task_recovery()`，继续按调用时持久化的实际版本创建 pinned recovery client。

### 3.2 两阶段 initialize

收窄 `_AutoNegotiatingAdapter.initialize()`：

1. 先 initialize 当前 modern adapter；
2. 只捕获 `(MCPProtocolError, MCPRemoteError)`；
3. 成功 await modern close 后创建唯一 legacy candidate；
4. legacy initialize 成功后读取其 `MCPNegotiatedSession`；
5. 仅当 `negotiated_protocol_version == "2025-11-25"` 时，把已初始化的基础 adapter 包进 Tasks adapter；不重复发起握手；
6. 返回已完成的 session，并让所有后续属性和方法继续经 `_active` 委托。

不捕获 `BaseException`，不解析异常字符串、HTTP status、metadata 或 response body；不在 `list_tools()`、`call_tool()`、cancel、close 中添加 fallback；不增加循环或重试次数配置。

### 3.3 删除旧 authority

- 从 `src/integrations/mcp/adapter_2026.py` 删除 `safe_auto_downgrade_version()` 及仅为它服务的 import/export；
- 从 `src/integrations/mcp/__init__.py` 删除对应 package import 与 `__all__` 项；
- 清理由本次删除造成的未使用 import，不清理无关代码。

## 4. Checkpoint C：验证

先运行聚焦门禁：

```bash
conda run -n multi_agent python -m unittest \
  tests.integrations.mcp.test_user_mcp_auto_negotiation \
  tests.integrations.mcp.test_2026_07_28_adapter
```

再运行批准范围的相关回归：

```bash
conda run -n multi_agent python -m unittest \
  tests.integrations.mcp.test_2025_11_25_task_recovery \
  tests.integrations.mcp.test_protocol_version_negotiation \
  tests.integrations.mcp.test_streamable_http_versions \
  tests.integrations.mcp.test_user_mcp_gateway \
  tests.integrations.mcp.test_user_mcp_health
```

运行 MCP integrations 目录门禁，覆盖共享 factory 的其他使用方：

```bash
conda run -n multi_agent python -m unittest discover \
  -s tests/integrations/mcp -p 'test_*.py'
```

静态、入口与范围门禁：

```bash
conda run -n multi_agent python -m compileall -q \
  src/integrations/mcp tests/integrations/mcp
conda run -n multi_agent ruff check \
  src/integrations/mcp/user_client.py \
  src/integrations/mcp/adapter_2026.py \
  src/integrations/mcp/__init__.py \
  tests/integrations/mcp/test_user_mcp_auto_negotiation.py \
  tests/integrations/mcp/test_2026_07_28_adapter.py
conda run -n multi_agent python -c 'import src.integrations.mcp'
if rg -n 'safe_auto_downgrade_version' src tests; then exit 1; fi
git diff --check
```

最终 diff 必须证明：生产变化只有批准的三个文件；测试只覆盖新 auto 合同和旧 helper 删除；没有凭据、Endpoint、fixture 响应正文、DTO、schema、配置、Frontend、Rust、部署或 `prod` 变化。

## 5. Checkpoint D：文档闭合与提交

- 把设计状态更新为 `implemented_verified`；
- 把本计划更新为 `complete`，记录实际红测、绿测数量与命令结果；
- 同步 `docs/AGENTS.md` 和 `CHANGELOG.md`；
- 检查模块职责、目录结构和测试入口未变化，因此除非事实发生变化，不修改其他层级 `AGENTS.md`；
- 提交一个范围清晰的实现检查点，建议提交信息：`fix(mcp): negotiate auto protocols by handshake`；
- 提交后复查工作树，保留所有无关用户改动。

## 6. 停止条件

以下任一情况出现时停止扩张并回到设计确认，不自行兜底：

- legacy initialize 无法通过现有 unpinned `MCPClient` 接受本地支持版本；
- Tasks adapter 必须在握手前介入才能保持现有行为；
- Gateway/Health 需要新增状态字段或改变生命周期才能读取协商结果；
- 实现必须修改批准范围外的生产模块、schema、配置或外部服务。

测试 fixture 或 import 的必要机械调整不算生产范围扩张，但必须在完成证据中逐项列明。

## 7. 回滚

回退实现检查点即可恢复原错误内容映射和 pinned legacy fallback；没有数据库、数据、缓存、schema、配置、镜像、部署或外部服务回滚步骤。QA 服务不参与自动验证，无需环境回滚。

## 8. 完成证据（2026-08-31）

- 旧实现聚焦红测共运行 24 项：新 auto 合同出现 8 个 error 和 1 个 failure，精确证明普通 typed protocol/remote error 不 fallback、close 未进入切换路径、缺少按实际版本包裹 Tasks，以及 factory 仍构造 pinned candidate；同次运行另发现 1 个既有 fixture 期望写窄，按 fixture 实际两个支持版本修正后未改变产品合同。
- 新增 `test_user_mcp_auto_negotiation.py`，以 fake adapter/factory 锁定一次切换、typed 异常边界、关闭顺序、无第三候选、Tool 阶段不切换、显式 pin、legacy SSE auto 和 Tasks 包裹；并以真实 `MCPClient` + fake transport 证明 unpinned `2025-11-25` initialize 接受 `2025-06-18`/`2025-03-26`，后续 notification 使用实际协商版本。
- 最小实现只修改 `user_client.py`、`adapter_2026.py` 和 MCP package/2026 adapter export；删除 `safe_auto_downgrade_version` 后，业务源码与测试零引用，package import 成功。
- 聚焦 auto + 2026 adapter 25/25、Tasks/recovery/version/Gateway/Health 相关回归 72/72、MCP integrations 554 项通过（2 项按既有环境条件跳过）。compileall、变更面 Ruff、package import、旧 helper 零引用和 `git diff --check` 通过。
- 用户提供的 `germQA`、`nruseryQA`、`darbQA`、`doeQA`、`gwsQA` 与 OCR 真实目标仅用于进程内脱敏 smoke，Endpoint、Header、凭据和响应正文均未写入仓库：5 个 legacy SSE QA 目标的鉴权原始 GET 可达并返回标准 endpoint event，但现有 policy-bound legacy client 均返回 `legacy_sse_connect_failed`，因此不记为连接通过，也不在本目标内修改 transport；Streamable HTTP OCR 目标在 modern initialize 返回 typed `MCPAuthRequiredError`（401），active adapter 保持 modern 且没有 fallback，验证了认证失败停止规则，但因缺少授权未形成正向协议协商证据。
- 未修改 DTO、数据库、schema、配置、Endpoint Policy、认证、Gateway、Health Runner、Router/Selector、Frontend、Rust、镜像、部署、外部 MCP Server 或 `prod`；无新增依赖、持久化字段或跨会话缓存。

License Requirement：复用现有 Python、MCP adapters、typed errors、Gateway scope 与 unittest；无新增依赖或许可变化。
