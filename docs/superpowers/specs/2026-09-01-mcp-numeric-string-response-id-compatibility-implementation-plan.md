# MCP 数字字符串响应 ID 全协议兼容实施计划

依据：`2026-09-01-mcp-numeric-string-response-id-compatibility-design.md`

## 状态

`published_not_deployed_with_external_smoke_gap`

用户已批准实施；生产代码、测试和自动门禁已闭合，真实smoke的两个外部缺口按原计划如实记录。

## 完成声明

只有同时满足以下条件，才可声明实施完成：

1. 当前 Python MCP clients 保持整数 request ID；整数 expected 只接受类型精确同值整数 response 或规范十进制字符串 response；
2. 字符串 expected 只接受类型精确同值字符串 response，不新增反向兼容；
3. bool、float、null、leading-zero、plus、whitespace、decimal、exponent、Unicode digit和真实 mismatch 全部拒绝；
4. 2024 Legacy direct/persistent/streaming、2025 JSON/SSE/base/recovery 与2026 stateless JSON/SSE均使用唯一规则；
5. Legacy exact-first 并发不串线，显式区分unknown、buffered匹配与streaming匹配，所有无sink result使用64 MiB有界匿名provisional spool，所有失败/取消/关闭路径清理；
6. transport/version gate、请求body、原始response bytes、业务ID、schema、配置和外部Server不变；
7. 自动门禁通过，真实 `/sse` 只读smoke协商2024并发现9个Tool，既有OCR auto继续协商2025。

外部 smoke 因网络、认证或服务状态失败时必须记录精确缺口，不得替代确定性测试或误报通过。

## Checkpoint A：共享规则红测与实现

### 测试范围

- `tests/integrations/mcp/test_protocol_version_negotiation.py`

新增纯 helper 矩阵：

- expected整数：整数exact、规范字符串alias、正数/零/负数；
- expected字符串：字符串exact；
- 单向拒绝：字符串expected + 整数response；
- 类型拒绝：bool、float、null、list/object；
- 字符串拒绝：`"01"`、`"+1"`、空白、`"1.0"`、指数、Unicode digit、未知值；
- 非response message拒绝；
- exact返回原mapping，alias返回浅副本且输入mapping不变。

### 红门禁

```bash
conda run -n multi_agent python -m unittest \
  tests.integrations.mcp.test_protocol_version_negotiation
```

旧实现应因 helper 不存在而失败；不得先改生产代码规避红测。

### 生产实现

- `src/integrations/mcp/protocol.py`

新增唯一 response-ID normalizer：

- 先确认 message 是 JSON-RPC response；
- exact 必须满足 expected/raw具体类型同为 `int` 或同为 `str`，且值相同；
- alias 只允许具体整数 expected 与 `raw == str(expected)` 的具体字符串；
- exact返回原mapping，alias只浅拷贝并替换顶层`id`，其余返回`None`。

不得调用普通 `str(raw) == str(expected)`，不得修改输入mapping或放宽 `json_rpc_message_kind()`。

### 绿门禁

重复纯 helper 模块并运行变更面 Ruff。

## Checkpoint B：2025 base、2026 与 recovery 消费点

### 红测

修改：

- `tests/integrations/test_mcp_client.py`
- `tests/integrations/mcp/test_streamable_http_versions.py`
- `tests/integrations/mcp/test_2026_07_28_adapter.py`
- `tests/integrations/mcp/test_2025_11_25_task_recovery.py`

锁定：

1. 2025 initialize、tools/list的JSON及POST SSE response接受字符串化整数ID；
2. base client `_handle_stream_events` 与 final message 使用同一规则；
3. 2026 JSON、request-scoped SSE及JSON/SSE双final仅ID类型不同、规范后内容相同时成功；
4. 2026真实内容冲突、多个final、非规范/类型错误ID继续拒绝；
5. `MCP2025TaskRecoveryClient` 的tasks/get、tasks/result、tasks/cancel分别接受字符串化整数ID；
6. recovery remote taskId、safe ref和binding mismatch断言不变。

旧实现必须只在新增字符串alias用例失败。

### 最小实现

- `src/integrations/mcp/client.py`
  - `_require_message()` 返回normalizer结果或抛既有mismatch；
  - `_handle_stream_events()` 对response事件使用同一normalizer作匹配，不修改server request/notification。
- `src/integrations/mcp/adapter_2026.py`
  - JSON body与每个SSE final先规范化，再做final数量、JSON/SSE冲突和result/error检查。
- `src/integrations/mcp/adapter_2025_tasks.py`
  - recovery-only response envelope在顶层ID检查时复用normalizer。

不得修改MRTR/Tasks业务字段、request registered callback、cancel request ID、durable recovery identity或transport headers。

### 绿门禁

运行上述四个测试模块和变更面 Ruff。

## Checkpoint C：Legacy direct/persistent correlation

### 红测

- `tests/integrations/mcp/test_legacy_http_sse_transport.py`
- `tests/integrations/mcp/test_legacy_http_sse_persistent_reader.py`

新增fake server可选择原样或字符串化response ID，并覆盖：

- initialize + tools/list的persistent SSE字符串alias；
- direct POST JSON response字符串alias；
- response message和`sse_events`中的ID均被规范为expected整数；
- 同时pending整数`1`与字符串`"1"`，两类response乱序时exact各自胜出；
- 字符串exact buffered pending与整数alias streaming pending同时存在时，字符串response保留buffered result且不写入streaming sink；
- bool/float/非规范字符串/unknown不命中，unknown count与timeout保持；
- 整数原样响应及现有并发乱序测试不退化。

### 最小实现

- `src/integrations/mcp/transport_legacy_http_sse.py`

增加私有type-aware pending matcher：

1. 无await地扫描pending，先用shared normalizer寻找返回原mapping的exact；
2. exact不存在时再寻找返回副本的单向alias；
3. 返回真实pending key、pending对象和normalized message；
4. result selector把匹配结果转换为显式私有target，至少携带normalized response ID和可空result sink；只有完全未匹配才返回`None`；
5. `_handle_sse_event()` 用真实pending key完成future与remove，并构造normalized `MCPStreamEvent`副本；
6. direct POST由transport或base final gate规范化，但response message与events必须保持一致。

不得以“sink为`None`”表示unknown，不得直接依赖`dict.get()`处理type-aware exact，不得改pending key、request body或unknown计数规则。

### 绿门禁

运行Legacy transport/persistent reader聚焦模块与Ruff。

## Checkpoint D：Legacy 无 sink result 有界 provisional spool

### 红测

- `tests/integrations/mcp/test_mcp_streaming_response.py`
- `tests/integrations/mcp/test_legacy_http_sse_persistent_reader.py`

确定性fixture必须把顶层JSON字段排成`jsonrpc -> result -> id`，并覆盖：

1. ID-before-result且匹配target有sink时继续直接写入目标sink；
2. result-before-ID且字符串alias时先进入provisional spool，ID解析后分块写入正确sink并形成正常`_mcpResultRef`；
3. 使用内部test seam把hard cap收窄，精确证明超限抛既有`MCPResultTooLargeError`；runtime默认仍固定64 MiB，不新增公共配置；
4. unknown/mismatch关闭provisional但不abort其他pending；
5. 已关联后的write/finalize failure abort目标sink；
6. parse failure、reader cancel和close关闭provisional并经既有fail-pending清理受影响sink；
7. provisional raw不进入message、event、metadata、manifest、公开ref或异常文本；
8. known buffered pending在ID前置和后置两种成员顺序下都从有界spool恢复原业务result，不计为unknown；
9. 字符串exact buffered pending与整数alias streaming pending同时存在时，字符串response命中buffered pending且不触碰streaming sink。

### 最小实现

- `src/integrations/mcp/streaming_response.py`
- `src/integrations/mcp/transport_legacy_http_sse.py`

在现有 `_JSONRPCResultExtractor` 内替换“selector无sink即无界bytearray”的分支：

- 以`tempfile.TemporaryFile()`持有匿名provisional bytes；
- 内部构造参数默认`MAX_DURABLE_MCP_RESULT_BYTES`，仅测试可传较小值，不暴露应用配置；
- 每次write累计size并在越界前抛`MCPResultTooLargeError`；
- selector返回显式私有target；target存在但sink为空表示known buffered pending，`None`只表示unknown；
- prefix尚无顶层`id`字段时保持unresolved，不调用selector或提前计为unknown；
- 开始result时未取得sink就写provisional，不论ID成员位于result之前还是之后；finish解析完整envelope后以shared normalizer取得或复验target；
- streaming target以64 KiB分块replay到目标sink并复用现有control materialize/durable finalize；buffered target从有界spool解析并恢复原业务result；
- 只有selector真正未匹配时才关闭provisional并返回足以让Legacy记录unknown的有界response envelope，不保留业务result；
- abort/finally对provisional close幂等，目标sink继续复用既有abort authority；
- OSError映射到既有temporary-storage typed failure，不暴露路径。

不得新建持久化store、manifest、数据库对象、后台janitor或第二种result ref。

### 绿门禁

运行streaming response、Legacy persistent reader、temporary results相关聚焦测试与Ruff。

## Checkpoint E：相关/全量回归与真实smoke

### 自动验证

按由窄到宽执行：

```bash
conda run -n multi_agent python -m unittest \
  tests.integrations.mcp.test_protocol_version_negotiation \
  tests.integrations.test_mcp_client \
  tests.integrations.mcp.test_streamable_http_versions \
  tests.integrations.mcp.test_2026_07_28_adapter \
  tests.integrations.mcp.test_2025_11_25_task_recovery \
  tests.integrations.mcp.test_legacy_http_sse_transport \
  tests.integrations.mcp.test_legacy_http_sse_persistent_reader \
  tests.integrations.mcp.test_mcp_streaming_response

conda run -n multi_agent python -m unittest \
  tests.integrations.mcp.test_user_mcp_auto_negotiation \
  tests.integrations.mcp.test_user_mcp_gateway \
  tests.integrations.mcp.test_user_mcp_health

conda run -n multi_agent python -m unittest discover \
  -s tests/integrations/mcp -p 'test_*.py'

conda run -n multi_agent python -m compileall -q \
  src/integrations/mcp tests/integrations/mcp

conda run -n multi_agent ruff check \
  src/integrations/mcp/protocol.py \
  src/integrations/mcp/client.py \
  src/integrations/mcp/adapter_2026.py \
  src/integrations/mcp/adapter_2025_tasks.py \
  src/integrations/mcp/streaming_response.py \
  src/integrations/mcp/transport_legacy_http_sse.py \
  tests/integrations/mcp/test_protocol_version_negotiation.py \
  tests/integrations/test_mcp_client.py \
  tests/integrations/mcp/test_streamable_http_versions.py \
  tests/integrations/mcp/test_2026_07_28_adapter.py \
  tests/integrations/mcp/test_2025_11_25_task_recovery.py \
  tests/integrations/mcp/test_legacy_http_sse_transport.py \
  tests/integrations/mcp/test_legacy_http_sse_persistent_reader.py \
  tests/integrations/mcp/test_mcp_streaming_response.py

conda run -n multi_agent python -c 'import src.integrations.mcp'
git diff --check
```

静态证明：

- 生产ID兼容规则只能存在于shared helper；
- 不出现真实Endpoint、key、动态UUID、Session ID、响应正文或Tool descriptor；
- transport/version gate、配置、DTO、schema、Frontend、Rust和部署文件零diff；
- `docker_cmd.md`继续存在、ignored且untracked，不读取内容；
- 范围外未跟踪`test.json`不得读取、暂存、修改或删除。

### 真实脱敏smoke

使用修改后的实际factory：

1. 历史QA `/sse + legacy_http_sse + auto`只执行initialize、tools/list、close；预期协商`2024-11-05`并发现9个Tool；
2. 已验证OCR `streamable_http + auto`只执行initialize、tools/list、close；预期继续协商`2025-11-25`；
3. 不调用业务Tool；输出只保留adapter、版本、布尔状态、capability keys和Tool数量/名称。

## Checkpoint F：状态同步与提交

全部门禁通过后：

- 设计状态更新为`implemented_verified`；
- 本计划更新为`complete`并记录红测、测试数、skip、静态门禁和真实smoke结果；
- 同步`docs/AGENTS.md`、`src/integrations/AGENTS.md`、`tests/AGENTS.md`与`CHANGELOG.md`；
- 最终diff复核后创建单一实现检查点。

建议提交信息：

```text
fix(mcp): accept numeric string response IDs
```

## 回滚

回滚单一实现检查点即可恢复旧严格ID比较和旧parser分支；没有数据、schema、缓存、配置、外部Server、镜像或部署回滚。

License Requirement：复用现有Python、MCP protocol/adapters、temporary result sink、typed errors、unittest和仓库工具链；无新增依赖或许可变化。

## 实施结果

- Checkpoint A：共享`normalize_json_rpc_response_id()`先红后绿；只接受类型精确exact或整数expected对应的规范十进制字符串alias，反向、bool、float、null、非规范字符串和非response均拒绝，输入mapping不变。
- Checkpoint B：`MCPClient`、2026 adapter和独立2025 Tasks recovery client统一接入helper；三个2025 session版本的JSON/SSE、2026 JSON/SSE双final及tasks/get/result/cancel字符串ID回归闭合。
- Checkpoint C：Legacy pending改为无await的type-aware exact-first matcher，别名命中时同步规范化message与event；direct/persistent initialize/list及并发乱序保持通过。
- Checkpoint D：Legacy result selector以显式私有target区分unknown、known buffered和streaming；所有无sink result进入64 MiB有界匿名`TemporaryFile`，buffered恢复原result，streaming以64 KiB分块replay，unknown不暴露业务result，取消/关闭/失败清理闭合。
- 自动验证：8个聚焦模块88项、Gateway/Health/auto相关61项、MCP integrations 567项通过（2项既有环境skip）；compileall、变更面Ruff、package import、shared rule唯一性、敏感内容扫描和`git diff --check`通过。
- 真实Legacy smoke：修改后的实际`UserMCPClientFactory`使用`legacy_http_sse + auto`和用户提供的header完成initialize，成功即证明requested/negotiated均为`2024-11-05`且字符串化整数response ID已关联；随后外部Server在tools/list前发送仅含`jsonrpc + result`、不含`id`的非法envelope，现有fail-closed校验拒绝并最终`legacy_response_timeout`，因此未取得9个Tool，不扩大范围吞掉该独立服务端错误。
- OCR smoke：现有输入只有占位Bearer，未发送无效凭据或误报`2025-11-25`通过；自动auto/2025回归已闭合，但真实OCR仍是外部凭据缺口。
- 镜像发布：用户明确批准保持版本号后，基于源码commit `85d22f1c`只重建并覆盖推送`registry.cn-hangzhou.aliyuncs.com/biobin/breeding-agent-backend-dev:0.1.29`；本地及远端OCI index digest均为`sha256:6956c93d1f6b1f8cc2eb620ac22675a706d8c55f94c2590b3a13536042e8f267`，远端包含`linux/amd64` manifest `sha256:5ddb53b6b1f70fbb1638b903c9581f3bb01b686ae692f6400f015300a690d5d6`和attestation。推送前验证镜像不含`/app/config.yaml`、MCP package可导入且数字字符串ID断言通过；Frontend、Runtime Sidecar未重建，`docker_cmd.md`因tag不变未修改，镜像尚未部署。
- 未修改transport/version gate、request body、原始response bytes、业务ID、配置、DTO、schema、数据库、Frontend、Rust、依赖、部署、外部Server或`prod`；未读取或暂存范围外`test.json`。
