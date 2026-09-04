# 主 Agent 统一 Tool Result 50k Token 预算实施计划

## 状态与依据

- 日期：2026-09-04
- 分支：`main`
- 状态：`implemented_verified_backend_only`；Checkpoint A～F已完成
- 设计依据：`2026-09-04-unified-tool-result-50k-token-budget-design.md`
- 设计复审：100/100 Pass，0 Blocking、0 Major、0 Minor
- 实施范围：Backend-first；Frontend业务卡片20,000字符/80,000-byte限制移除和
  `model_unavailable`专用文案后续独立发布
- 非范围：历史结果迁移/重投影、数据库 schema、Projection revision、Rust/proto、外部 MCP/Skill、
  镜像、部署与 `prod`

### 实际执行记录

| 检查点 | 提交 | 结果 |
|---|---|---|
| Checkpoint A | `bb15b83d` | model-bound详细Tokenization、Offset裁剪、10秒超时、required fail-closed和14项聚焦测试完成 |
| Checkpoint B | `961dbe61` | worker完整脱敏candidate、父进程50k-token Projection及无192 KiB Store完成；22项聚焦回归通过、2项平台skip |
| Checkpoint C | `a5b3765c` | MCP Call terminal commit后按Run model生成Projection；Selector移除20k/80k二次预算并按绑定模型preflight；MCP integrations 589项通过、2项平台skip |
| Checkpoint D | `b97f4906` | Agent Projector异步化；Skill结果按Run模型单次50k-token预算；AgentItem超128 KiB只引用预算后安全Projection；Orchestration 197项和D聚焦48项通过 |
| Checkpoint E | `adc59956` | 模型transport/timeout/auth/rate-limit/5xx与缺配置按typed边界映射`model_unavailable`；AgentRun、`agent.run.failed`、`task.failed`同码，普通异常保持`execution_crash`；远端MCP terminal后Tokenization失败零重试/零Tool重放；正式门禁175项通过 |
| Checkpoint F | `7292a0df` | API 652、E2E 12、Observability 41及其他无关分层通过；三模型真实Tokenization均单请求完成50k offset裁剪；4项既有基线失败如实保留；静态、Ruff、受保护文件门禁闭合 |

## 1. 完成声明

只有同时满足以下条件，才可宣称本轮 Backend 仓库实现完成：

1. 每个新 MCP、普通/Legacy/delegated Skill 业务 Result 在完整返回并完成本地解析、校验、脱敏后，
   按发起 Tool Call 的 Agent Run 绑定 model edition 调用一次 Provider `/tokenization`；
2. `total_tokens <= 50_000` 时完整保留；超限时只使用
   `offset_mapping[50_000][0]` 裁剪，不使用字符/UTF-8 bytes、二分、重试或第二次复算；
3. `/tokenization` 响应的 model、item 数、`total_tokens`、`token_ids` 与 `offset_mapping` 合同完整
   校验；有效 timeout 不超过 10 秒，Provider-required 路径零 `tiktoken` fallback；
4. MCP Parser worker 只输出一份完整安全 candidate；父进程完成 Tokenization 后生成既有
   `mcp-result-parser.v2` / `maf.mcp.parsed_result_projection.v2` user/agent projection；
5. Projection Store 不再使用192 KiB、Backend业务view与Selector不再使用20,000字符/80,000-byte
   业务预算；MCP raw 64 MiB安全上限保持不变；Frontend业务卡片旧限制留待后续独立发布，本轮不
   宣称端到端UI目标完成；
6. AgentItem 131,072-byte单行合同保持不变；超限的token-bounded Result通过identity-bound
   reference/receipt承载，模型请求解析时不得恢复未预算raw；
7. `AgentCallResultProjector.project(...)`及其必要内部路径异步化，所有生产和测试调用点改为
   `await`，不使用同步阻塞包装或新增prepare/finalize对象层；
8. MCP terminal Call或Skill Invocation TaskNode的durable completed authority先于Tokenization；
   Tokenization失败保留可信结果、AgentRun/Task以`model_unavailable`终态化且Tool零重放；
9. Tokenization在Agent Lease active phase内等待；MCP Tool返回后停止Tool heartbeat，不把结果处理
   伪装成Tool仍在运行；
10. Backend实时与历史API稳定输出`model_unavailable`；当前旧Frontend安全走现有通用失败文案；
11. 既有历史projection逐字节不变、零历史reproject、零raw补读、零Artifact CAS、零数据库迁移、
    零Tool网络重放；
12. 聚焦测试、Backend分层回归、静态检查、最终diff审查与受保护文件门禁全部闭合。

## 2. 严格范围与执行规则

预计修改：

- `src/core/errors.py`
- `src/integrations/token_counter.py`
- `src/integrations/mcp/result_parsing/models.py`
- `src/integrations/mcp/result_parsing/projections.py`
- `src/integrations/mcp/result_parsing/worker.py`
- `src/integrations/mcp/result_parsing/service.py`
- `src/integrations/mcp/result_parsing/projection_store.py`
- `src/integrations/mcp/gateway_models.py`
- `src/integrations/mcp/gateway.py`
- `src/integrations/mcp/dispatch_coordinator.py`
- `src/integrations/mcp/selector_context.py`
- `src/integrations/mcp/result_artifact_projection.py`
- `src/capabilities/mcp_tool/executor.py`
- `src/orchestration/agent_loop/result_projection.py`
- `src/orchestration/agent_loop/capability_invoker.py`
- `src/orchestration/agent_loop/transient_results.py`
- `src/orchestration/agent_loop/result_artifacts.py`
- `src/api/runtime.py`
- `src/api/agent_projection.py`
- 对应既有测试、相关目录 `AGENTS.md`、本文、设计、`docs/AGENTS.md`、`CHANGELOG.md`

实际实施只修改经引用搜索或红测证明必要的文件；上表不是必须全部改动的配额。

明确禁止：

- 修改 `mcp-result-parser.v2`、`maf.mcp.parsed_result_projection.v2` 或Skill projection revision；
- 修改AgentItem 131,072-byte、MCP raw 64 MiB、数据库表/列/约束或Rust wire合同；
- 修改Tool选择、参数、授权、Endpoint policy、协议协商、OCR start/poll/ack、远端Task业务语义；
- 读取raw作为前端fallback，或让Selector、历史API重新执行Tool/Tokenization；
- 修改Frontend `MCP_MAX_CODE_POINTS` / `MCP_MAX_UTF8_BYTES`或专用错误文案、构建镜像、修改
  `docker_cmd.md`、部署开发环境或触碰 `prod`。

开始每个Checkpoint前执行`git status --short --branch`；只暂存计划内文件，始终保留用户未跟踪的
`test.json`。每次修改后检查相关`AGENTS.md`与`CHANGELOG.md`。大检查点通过后创建独立commit并推送。

## 3. Checkpoint 0：基线与红测试

### 3.1 基线

运行并记录：

```bash
conda run -n multi_agent python -m unittest tests.integrations.test_token_counter
conda run -n multi_agent python -m unittest \
  tests.integrations.mcp.test_result_parser_worker \
  tests.integrations.mcp.test_selector_context \
  tests.orchestration.test_agent_result_projection \
  tests.orchestration.test_agent_invocation \
  tests.api.test_execution_singleflight
git diff --check
```

既有失败只记录证据，不为清零无关失败扩大范围。

### 3.2 红测试矩阵

先补失败测试，证明当前实现缺少：

- 三model edition显式请求与response model一致校验；
- `total_tokens/token_ids/offset_mapping`数组长度、offset shape、字符范围和单调性校验；
- 49,999/50,000/50,001以及共享emoji offset边界；
- Provider不可用且配置缺失时也禁止fallback；显式大timeout封顶10秒；零自动重试；
- MCP worker输出完整安全candidate而非20k/80k最终projection；
- 超过192 KiB的合法v2 projection可stage/load；
- Selector与业务view不再二次20k/80k裁剪；
- Skill Projector异步调用一次Tokenization，全部调用方await；
- token-bounded Result超过AgentItem 128 KiB时使用reference/receipt，resolver不恢复未预算raw；
- Tool durable completed先于Tokenization；Tokenization失败后Call/TaskNode保持completed且零Tool重放；
- `ModelUnavailableError -> agent.run.failed/task.failed code=model_unavailable`，其他异常仍为
  `execution_crash`。

红测试必须只因上述合同缺失而失败，不提交红态。

## 4. Checkpoint A：Provider Tokenization与通用错误合同

### 4.1 Typed error

在`src/core/errors.py`定义通用`ModelUnavailableError`，公开code固定为`model_unavailable`。
`TokenizationError`作为其具体子类或等价typed实现，保留既有捕获兼容。

### 4.2 详细Tokenization结果

在`src/integrations/token_counter.py`完成最小泛化：

1. 定义immutable详细结果，至少包含`total_tokens`与字符`offset_mapping`；
2. Provider response parser验证顶层model、data数量、每项整数token IDs、二维整数offset、范围和数组
   数量一致；验证完成后可丢弃不参与裁剪的token IDs，避免长期缓存大数组；
3. 新增同步/异步详细入口，强制显式`model_edition`并复用现有配置、headers和endpoint；
4. 新增共享async token-budget helper，返回`text/total_tokens/truncated/cutoff`，内部只发一次请求；
5. `total_tokens > limit`时只取`offset_mapping[limit][0]`；若offset合同无法提供安全切点则fail
   closed，不猜测字符数；
6. 现有`get_num_of_tokens_*`继续返回整数，内部从详细response提取count；count-only cache继续生效，
   Tool Result详细入口不从整数cache伪造offset；
7. Provider-required配置不可用和请求失败统一抛typed error；旧非required调用保持现有兼容行为；
8. timeout始终`min(configured, 10.0)`，`httpx`层不增加retry。

### 4.3 Checkpoint门禁

```bash
conda run -n multi_agent python -m unittest tests.integrations.test_token_counter
conda run -n multi_agent ruff check \
  src/core/errors.py src/integrations/token_counter.py \
  tests/integrations/test_token_counter.py
git diff --check
```

Checkpoint commit：`feat(llm): add model-bound tokenization offsets`

## 5. Checkpoint B：MCP完整安全candidate与Projection承载

### 5.1 Worker职责

在`result_parsing`内定义strict、非持久化的安全candidate：

- 只包含生成现有user/agent projection所需的脱敏structured/text/metadata和truncation输入；
- URL、secret assignment和敏感key继续在隔离worker内清洗；
- structured duplicate、`isError`、output schema和raw/model digest语义不变；
- worker第二条IPC消息只发送一份candidate，不发送两份已裁剪projection；
- 父进程验证candidate exact shape、JSON value、checkpoint绑定和来源，不信任任意worker对象；
- worker不接收model edition、API key、base URL，也不访问网络。

`MCPResultServiceOutcome`改为返回validated candidate。malformed/tool_error继续使用现有checkpoint和安全
错误路径。

### 5.2 Parent projection

父进程新增async projection步骤：

1. 接收Agent Run绑定model edition；
2. 对candidate唯一业务文本调用Checkpoint A helper；
3. 用预算后文本构造现有v2 `user_view`与`agent_projection`；
4. structured超限使用string `structured_preview`，不得作为残缺JSON value；
5. truncation flag只进入既有字段，不拼入业务正文；
6. 删除`MAX_PROJECTION_CODE_POINTS`、`MAX_PROJECTION_UTF8_BYTES`及相关字符/byte裁剪；
7. 保持v2 envelope exact keys，不增加model/token字段。

### 5.3 Projection Store

- 删除`MAX_PROJECTION_ENVELOPE_BYTES = 192 * 1024`作为stage/load/consume内容接收条件；
- private文件仍复验owner、mode、regular file、link count、size、SHA和binding；
- load按manifest/handle的expected size读取，不用新的magic KB上限替代；
- MCP raw 64 MiB和worker 512 MiB address-space上限保持不变；
- AgentItem不受影响。

### 5.4 门禁

```bash
conda run -n multi_agent python -m unittest \
  tests.integrations.mcp.test_result_parser_worker \
  tests.integrations.mcp.test_result_artifact_projection
conda run -n multi_agent ruff check src/integrations/mcp/result_parsing \
  tests/integrations/mcp/test_result_parser_worker.py
git diff --check
```

Checkpoint commit：`feat(mcp): budget complete safe result projections`

## 6. Checkpoint C：MCP terminal顺序、模型绑定与Selector

### 6.1 绑定实际model edition

- `AgentCallInvoker`把`run.binding.model_edition`作为closed内部执行metadata交给`mcp.dispatch`；
- Gateway/Coordinator只接受非空、属于当前Run binding的值，不读取系统默认模型；
- legacy MCP executor存在Agent Run时使用同一metadata；缺失可信binding的required路径fail closed。

### 6.2 先terminal completed，再Tokenization

调整Gateway/Coordinator handoff：

1. Gateway完成远端Tool、raw持久化和Parser checkpoint，返回completed outcome与safe candidate；
2. Coordinator先seal/commit现有terminal success authority；
3. commit成功后才await父进程projection/Tokenization并发布Projection；
4. `ModelUnavailableError`不得被现有best-effort projection catch吞掉；它在terminal success之后向
   Agent层传播；
5. raw、Call、receipt和checkpoint保持completed；不得进入failed/unknown或discard raw；
6. ordinary、approval、remote Task和OCR最终business result走同一顺序；workflow内部控制结果不扩大
   为业务Result；
7. crash或非模型projection失败保持现有safe-hide/补偿语义，绝不重放Tool。

### 6.3 Selector与API business view

- 删除Selector对新projection的20,000字符/80,000-byte历史结果预算；
- 新result entry逐条使用已持久化的50,000-token projection，不把同轮多个Result合并成一个Result；
- 历史v2逐字节只读，不调用Tokenization或Tool；
- Selector完整prompt仍由既有context preflight按实际Selector模型计算总预算；
- Artifact/API只读取已验证v2 projection，不读取raw fallback。

### 6.4 门禁

```bash
conda run -n multi_agent python -m unittest discover \
  -s tests/integrations/mcp -p 'test_*.py'
conda run -n multi_agent python -m unittest discover \
  -s tests/capabilities/mcp_tool -p 'test_*.py'
conda run -n multi_agent ruff check \
  src/integrations/mcp/gateway.py \
  src/integrations/mcp/dispatch_coordinator.py \
  src/integrations/mcp/selector_context.py \
  src/capabilities/mcp_tool/executor.py
git diff --check
```

Checkpoint commit：`feat(mcp): finalize tool calls before tokenization`

## 7. Checkpoint D：异步Skill Projector与128 KiB引用承载

### 7.1 Projector异步化

- `AgentCallResultProjector.project(...)`改为async并接收必填model edition；
- 在现有single projector边界内先strict JSON、canonical raw、敏感值清洗，再await共享token-budget
  helper；
- `_skill`及必要成功路径异步化；失败、waiting和固定小控制结果不得误触发不必要的Provider调用；
- production的两个直接调用点全部改为await；所有42个现有源码/测试引用逐一核对并迁移；
- 禁止`asyncio.run()`、`run_until_complete()`或线程阻塞包装。

### 7.2 Skill结果承载

- 删除`MODEL_VIEW_MAX_CODE_POINTS`、`MODEL_RESULT_MAX_BYTES`作为业务裁剪依据；
- 50,000-token以内的完整安全model view可放入AgentItem时inline；
- 超过50,000 Token时以合法string preview承载预算后文本并设置既有truncation flag；
- token-bounded envelope超过AgentItem 128 KiB时，stage预算后的安全projection而非未预算raw；
- receipt继续绑定Call/result/owner/Run/Task/SHA；resolver复验后只恢复预算后projection；
- 原始Skill输出若按现有业务规则形成Artifact可继续保留，但不得借Artifact resolver绕过50,000-token
  model/result预算；
- ordinary、Legacy、delegated路径分别覆盖；不修改Skill源码或外部bundle。

### 7.3 Durable完成与Lease

- 测试证明统一Invocation Service先提交completed TaskNode/result，再调用async Projector；
- Tokenization在`capability_wave` Agent Lease active phase内；失败保持TaskNode completed，提交或传播
  `model_unavailable`且同一Call零Skill重放；
- cancellation继续取消在途HTTP请求并沿用现有cancel语义，不映射为model unavailable。

### 7.4 门禁

```bash
conda run -n multi_agent python -m unittest \
  tests.orchestration.test_agent_result_projection \
  tests.orchestration.test_agent_invocation \
  tests.orchestration.test_agent_context_builder \
  tests.orchestration.test_agent_context_preflight \
  tests.orchestration.test_agent_result_artifacts
conda run -n multi_agent python -m unittest discover \
  -s tests/capabilities/skill_tool -p 'test_*.py'
conda run -n multi_agent ruff check \
  src/orchestration/agent_loop/result_projection.py \
  src/orchestration/agent_loop/capability_invoker.py \
  src/orchestration/agent_loop/transient_results.py \
  src/orchestration/agent_loop/result_artifacts.py
git diff --check
```

Checkpoint commit：`feat(agent): apply token budgets to skill results`

## 8. Checkpoint E：Backend `model_unavailable`传播

### 8.1 错误分类

- Provider Tokenization的typed error直接使用通用`ModelUnavailableError`；
- 主Agent sampling、Selector等阻断Task的模型transport、timeout、认证、限流和5xx按明确异常类型映射；
- 已收到有效模型响应后的协议错误、context-too-large、本地invariant、取消和lease lost保持原错误；
- 禁止按异常字符串或Provider response正文猜测分类。

### 8.2 Runtime与公开Event

- `_mark_task_failed()`只对`ModelUnavailableError`选择`model_unavailable`，其他异常继续
  `execution_crash`；
- `agent_loop_orchestrator.fail(...)`、AgentRun safe error、`agent.run.failed`与`task.failed`使用同一
  code；
- `src/api/agent_projection.py`允许该closed字段通过实时与历史投影；
- 公开payload不包含endpoint、响应正文、API key、模型输入或Tool正文；
- Backend-first发布下旧Frontend收到未知code仍走现有default文案，不修改Frontend源码。

### 8.3 门禁

```bash
conda run -n multi_agent python -m unittest \
  tests.api.test_execution_singleflight \
  tests.api.test_uploads \
  tests.api.test_streaming_write_after_completion \
  tests.orchestration.test_agent_loop
conda run -n multi_agent ruff check \
  src/core/errors.py src/api/runtime.py src/api/agent_projection.py
git diff --check
```

Checkpoint commit：`feat(api): propagate model unavailable failures`

## 9. Checkpoint F：完整回归、静态证明与文档闭合

### 9.1 静态证明

执行引用与旧预算扫描：

```bash
rg -n '20_000|20,000|80_000|80,000|192 \* 1024|MAX_PROJECTION_ENVELOPE_BYTES' \
  src/integrations/mcp src/orchestration/agent_loop src/capabilities/mcp_tool src/api
rg -n '\.project\(' src tests --glob '*.py'
rg -n 'asyncio\.run|run_until_complete' src/orchestration/agent_loop src/integrations/mcp
```

每个命中必须分类为已移除业务预算、保留的非本功能合同或测试证据；不能只追求零命中。

### 9.2 Backend分层门禁

```bash
conda run -n multi_agent python -m compileall -q src tests
conda run -n multi_agent python -m unittest discover -s tests/core -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/lifecycle -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/main_agent -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/mcp_tool -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/skill_tool -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/e2e -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/observability -p 'test_*.py'
conda run -n multi_agent ruff check <本次变更Python文件>
git diff --check
```

没有schema/proto/Rust变更时不运行破坏性migration；仍以静态diff证明这些范围未修改。Frontend不在本轮
实现范围，不宣称业务卡片旧字符/byte限制已经删除或专用中文文案已经发布。

### 9.3 真实Provider smoke

使用Git-ignored本地配置，只输出model、HTTP status、token/offset数量和裁剪后token数，不输出Endpoint、
API key或响应正文。对三款配置模型分别验证：

- 中英文、emoji、组合字符；
- 72,000+ Token输入；
- 第50,001个Token起始offset裁剪；
- response model与请求一致；
- 每个逻辑Result单次请求。

该smoke只调用Provider `/tokenization`，不执行MCP Tool、Skill、数据库修改、镜像或部署。

### 9.4 文档与终态

- 更新相关`AGENTS.md`与`CHANGELOG.md`；
- 在本文追加每个Checkpoint commit、测试数量、已知skip/失败和真实Provider证据；
- 最终`git status`只允许计划内变更和用户既有`?? test.json`；
- 验证`docker_cmd.md`存在、0600、Git-ignored且未跟踪；本轮不得读取或修改其内容；
- 创建最终implementation commit并推送GitHub/Gitee；不构建镜像、不部署。

### 9.5 实际验证记录

- `compileall`通过；Storage 573项（14 skip）、Lifecycle 48项、Orchestration 198项、
  main_agent capability 17项、MCP capability 15项、Skill capability 4项、API 652项、E2E 12项、
  Observability 41项全部通过；`skill/sql-query`本地目录不存在，记为N/A；
- Integrations 822项中819项通过、2项skip，只保留1项早于本目标的已知transport-family基线失败：
  `test_client_rejects_negotiated_version_incompatible_with_transport_family`；本轮一度改名导致的
  fault-matrix失败已恢复原测试入口并单独复跑通过；
- Core 54项中51项通过，3项失败均来自`97139173`新增
  `TaskStoragePort.list_skill_recovery_candidate_task_ids`后旧280-method golden未同步；本目标未修改
  persistence port，按基线规则记录而不扩大范围；
- API/E2E共享fixture显式注入确定性Tokenization结果；普通业务fixture保持完整Result，专用oversized
  fixture才模拟50,000-token offset截断。生产Provider-required fail-closed未放宽；
- 静态扫描仅保留delegated Skill instruction的20,000-code-point输入合同；全部
  `AgentCallResultProjector.project()`生产/测试调用均已`await`。现有`asyncio.run`只位于MCP runtime
  同步桥和operator migration，不在本Result投影路径；剩余byte上限均为raw/mapping/checkpoint/manifest
  基础设施安全边界，不裁剪业务Result；
- 三模型真实`POST /tokenization`使用同一中英文、emoji和组合字符输入：
  `deepseek-v4-flash-ga-260731`与`deepseek-v4-pro-ga-260813`均返回180,001 tokens、字符切点50,000；
  `glm-5-2-260617`返回209,998 tokens、字符切点42,858。三者均`truncated=true`、保留50,000-token
  budget且每个逻辑Result恰好1次HTTP请求；未输出Endpoint、API key或响应正文；
- `docker_cmd.md`存在、权限0600、Git-ignored且未跟踪；未读取或修改其内容。最终工作树仅允许本
  Checkpoint文件与用户既有`?? test.json`。

## 10. 发布、回滚与后续

本计划结束状态是`implemented_verified_backend_only`，不是`published`或`deployed`。

- Backend后续可独立构建发布；旧Frontend对`model_unavailable`使用现有通用fallback；
- Frontend另立小范围任务和独立版本，同时删除`frontend/src/domain/artifacts.ts`的
  `MCP_MAX_CODE_POINTS` / `MCP_MAX_UTF8_BYTES`业务裁剪并增加专用中文错误文案；
- 回滚按Checkpoint逆序撤销代码；数据库、历史结果和Projection v2无需回滚迁移；
- 回滚后的旧Backend可能把新写入的大v2 projection安全显示为unavailable，不得读取raw补偿；
- 开发环境真实MCP/Skill新Task、镜像、部署和`prod`均需要用户另行授权。
