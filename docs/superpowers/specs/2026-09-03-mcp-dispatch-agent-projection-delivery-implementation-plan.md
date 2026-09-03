# MCP Dispatch 实际 Tool Result 交付实施计划

依据：`2026-09-03-mcp-dispatch-agent-projection-delivery-design.md`

## 状态

`planned_hard_defects_resolved`

用户已批准退役parser/projection v1，不恢复旧内容；最新限定硬伤审计中保留的Artifact API Major已按
v2-only clean cutover决策闭合，当前0 Blocking / 0 Major。用户明确不要求增加运行中调用门禁；未评
Minor，不宣称完整95分信心门；本计划尚未修改生产代码。

计划基线为 `main@634fa002`。范围外未跟踪文件 `test.json` 必须保持未读取、未修改、未暂存。

## 完成声明

只有同时满足以下条件，才可声明仓库实施完成：

1. 主 Agent 仍通过全局唯一 `mcp.dispatch.server_id` 选择初始 Server；Selector action、独立
   Server Router、Gateway、approval、resume 和 no-replay 控制流不变；
2. 新 Result Parser 使用 `mcp-result-parser.v2` 并只读写
   `maf.mcp.parsed_result_projection.v2`；旧 v1 projection不读取、不迁移、不重投影且不修改原数据；
3. 同一 branch 的 durable completed Call/receipt/projection authority 能确定性构建
   `maf.mcp.agent_result_bundle.v1`，计数、sequence、source/carrier truncation 自洽；
4. bundle 的最终预算覆盖 JSON 转义、`model_view` 固定字段和 Agent Tool Result envelope；任何结果
   截断或整项省略均显式可见，不再发生无标记字符串减半；
5. 所有会提交给主 Agent 的 terminal `mcp.dispatch` Tool result，只要已有 validated completed
   results，就携带 bundle，同时保留真实 completed/stopped/failed 状态和 safe error；
6. 没有业务结果的 discover-only completed 继续返回现有安全 `text`；waiting、input-required、
   approval interrupt、cancel 和 OCR 固定工作流保持既有行为；
7. 下一次 `AgentModelRequest` 的对应 provider tool-call message 包含 closed bundle 和业务 sentinel，
   不含 raw、storage path、result ref、credential、receipt 或 checkpoint；
8. 聚焦、相关和 Backend 门禁通过，最终 diff 不含数据库 schema、公开 API、Frontend、Rust、
   外部 MCP Server、部署文件或 `prod` 变化。
9. 新旧版本不交叉：明确v1或null revision的pre-v2 completed result返回
   `mcp_result_projection_revision_retired`；部署前必须只读证明可恢复branch中没有已完成且revision
   非v2的Call，不能把尚未产生completed result的等待任务误计为旧projection；
10. 公开API的DTO和路由不变：有效v2 MCP Artifact继续返回`ready`业务视图，指向v1 projection的历史
    MCP Artifact通过既有`projection_invalid`安全返回`unavailable`，不返回storage ref、下载地址、
    raw result或内部引用。

若只完成自动测试而未获得开发环境部署/真实新会话授权，状态最多为
`implemented_automated_pending_dev_smoke`，不得写成 `implemented_verified`。

## Checkpoint 0：基线与红测边界

### 基线确认

- 当前分支必须为 `main`，起始 commit 必须包含 `634fa002`；
- 记录已有 tracked worktree 状态并避开所有无关修改；
- 只确认根目录 `docker_cmd.md` 存在、仍被 Git ignore 且未被跟踪，禁止读取或输出其内容；
- `test.json` 和其他范围外未跟踪文件不得进入任何命令参数或 Git 暂存区。

### 首轮红测文件

- `tests/integrations/mcp/test_result_parsing.py`
- `tests/integrations/mcp/test_result_parser_worker.py`
- `tests/integrations/mcp/test_selector_context.py`
- `tests/integrations/mcp/test_dispatch_coordinator.py`
- `tests/orchestration/test_agent_result_projection.py`
- `tests/e2e/test_mcp_server_explicit_agent_loop.py`
- `tests/api/test_conversation_messages_artifacts.py`

先增加最小失败断言，分别证明旧实现存在以下缺口：

1. parser projection 没有可信 agent truncation 字段；
2. durable projection reader只返回字符串，不能证明 source truncation；
3. 没有 closed、多结果、自描述的 Agent result bundle；
4. outer projector 对超限 `agent_projection` 仍可能普通字符串减半且不更新 truncation；
5. FINISH 只返回 Selector reason / generic text；
6. STOP、route exhausted、后续 unavailable/denied/terminal error 会丢弃先前成功结果；
7. 现有 E2E 只把 Selector reason sentinel 当结果，未执行实际 MCP Tool。
8. 现有Artifact API回归把v1 projection当作`ready`，没有锁定v2-only切换后的v2 ready / v1
   `projection_invalid` unavailable边界。

旧实现必须只在新增断言失败。若既有测试先失败，查明并隔离环境或用户改动后再继续；不得修改生产
代码掩盖非预期红灯。

## Checkpoint A：Result Parser 完整性元数据

### 修改范围

- `src/integrations/mcp/result_parsing/projections.py`
- `src/integrations/mcp/result_parsing/worker.py`
- `src/integrations/mcp/result_parsing/service.py`
- `src/integrations/mcp/result_parsing/projection_store.py`
- `src/integrations/mcp/result_parsing/historical_reprojection.py`
- `src/integrations/mcp/durable_result_lifecycle.py`
- `src/integrations/mcp/result_artifact_projection.py`
- 对应 `tests/integrations/mcp/test_result_parsing.py`
- 对应 `tests/integrations/mcp/test_result_parser_worker.py`
- `tests/integrations/mcp/test_result_artifact_projection.py`
- `tests/integrations/mcp/test_historical_result_reprojection.py`
- `tests/integrations/mcp/test_durable_result_lifecycle.py`
- `tests/api/test_conversation_messages_artifacts.py`

### 实现

1. 在 `projections.py` 增加 immutable `MCPBoundedAgentProjection(content: str, truncated: bool)`；
   `build_agent_projection()` 返回该值。`truncated` 的判断依据是 sanitizer 后完整 body 与最终 bounded
   body 是否一致；不得从 Tool 正文中的标记反推。worker只把 `content` 写入正文、把 bool 写入
   metadata，调用方不得重新计算。
2. 将 `PARSER_REVISION` 升级为 `mcp-result-parser.v2`；checkpoint schema字段集合继续为v1，但其中
   `parser_revision` 必须为v2。新 projection使用 `maf.mcp.parsed_result_projection.v2`，在既有closed
   envelope上增加唯一布尔字段 `agent_projection_truncated`；`user_view.projection_truncated` 继续只
   描述user view，二者不得混用。
3. 在 `projection_store.py` 建立唯一 shared v2 envelope validator并返回validated mapping：只接受原
   字段加 `agent_projection_truncated: bool`，未知/v1 schema、缺字段、额外字段和非布尔值全部fail
   closed。Result Service接收worker输出、Projection Store `stage/load`和durable consumer必须复用该
   validator，不复制shape判断。
4. Projection Store只接受projection v2；`PROJECTION_SCHEMA` 指向v2。manifest closed shape、binding、
   SHA、文件身份和192 KiB envelope上限不变；旧v1文件和manifest原样保留但不load。
5. `result_artifact_projection.py` 只把固定v2 schema写入新Artifact metadata；已有v1 metadata不回填、
   不移除、不CAS改写。
6. `historical_reprojection.py` 必须在 `_has_reprojection_authority()` 及任何 `_mark_unavailable()`、
   Projection Store或raw authority调用前检查receipt revision：明确v1或null的pre-v2结果进入closed
   `revision_retired`计数并立即跳过，不load、不reproject、不读取raw、不做metadata CAS；revision为v2
   时才沿v2 valid/reprojection路径，其他未知非空revision返回既有`projection_invalid`计数但同样零
   修改。不得以“当前revision不匹配”为由删除旧projection metadata。
7. `MCPDurableResultReconcileSummary` 增加closed `business_revision_retired`，只透传historical summary
   计数；不把Task、Call、业务正文或revision字符串写入日志/Event，不改变删除判定。
8. 不修改 raw parsed model、validated checkpoint字段集合、output schema validation、public user
   view、durable raw Artifact或数据库schema。
9. 不修改`src/api/artifact_responses.py`、公开DTO或路由。更新现有API测试：有效v2 binding/envelope经
   当前Projection Store stage/publish后，任务Artifact和会话历史均继续返回`ready`；历史v1 fixture
   不得通过v2-only `stage`伪造，而应使用独立于当前writer的冻结legacy fixture，断言两个API均返回
   `availability=unavailable`、`outcome=succeeded`、`unavailable_reason=projection_invalid`、空
   `storage_ref`和空下载地址，直接下载返回404且不出现raw或内部引用。

### 红绿验证

- 短结果：checkpoint revision和projection schema均为v2，`agent_projection_truncated=false`；
- ASCII、多字节和 escape-heavy 超限结果：v2 flag 为 true，正文仍满足 20,000 code points / 80,000
  bytes；
- v2 fixture可stage/load；v1、未知v3和shape drift在shared persistent validator被拒绝；
- historical reprojector遇到v1/null revision只增加`revision_retired`，未知非空revision只增加closed
  `projection_invalid`；两者的Artifact metadata/ref/SHA和raw authority调用数均不变；durable lifecycle
  summary准确透传retired计数且不触发删除；
- projection SHA/manifest/file tamper 仍 fail closed；
- Artifact API对v2 fixture继续返回`ready`；对冻结v1 fixture在任务和历史响应中均返回既有
  `projection_invalid` unavailable，storage ref为空且无下载能力；
- 原敏感 key、URL、secret assignment 和 raw 泄漏断言保持通过。

聚焦命令：

```bash
conda run -n multi_agent python -m unittest \
  tests.integrations.mcp.test_result_parsing \
  tests.integrations.mcp.test_result_parser_worker \
  tests.integrations.mcp.test_result_artifact_projection \
  tests.integrations.mcp.test_historical_result_reprojection \
  tests.integrations.mcp.test_durable_result_lifecycle \
  tests.api.test_conversation_messages_artifacts
```

建议检查点提交：

```text
fix(mcp): record agent projection completeness
```

## Checkpoint B：Durable projection authority 与 canonical bundle

### 修改范围

- `src/integrations/mcp/selector_context.py`
- `tests/integrations/mcp/test_selector_context.py`

### 实现

1. 增加私有 immutable completed-projection value，至少包含：
   - `call_sequence: int`；
   - `content: str`；
   - `source_truncated: bool`。
2. `MCPPublishedAgentProjectionAuthority` 在现有 owner/Task/Node/Call/Server/version/ref/SHA/parser/
   checkpoint 校验后：
   - parser revision和projection schema必须均为v2；
   - v2 返回projection正文和可信bool；
   - v1/null revision返回`mcp_result_projection_revision_retired`，不得调用Projection Store；
   - 其他未知非空revision返回typed unsupported/authority错误，仍不得调用Projection Store；
   - Artifact `projection_schema`、projection envelope schema与v2 validator必须一致；
   - 任一漂移继续抛现有 typed authority error，不降级读取 raw。
3. Selector context 继续只得到有界的 projection 正文列表，保持当前 prompt 和“最新优先、恢复 sequence
   顺序”策略；它不是 terminal carrier缓存。
4. 在同一 authority owner 中增加只读 terminal bundle builder。每次终态从 durable branch Call、
   terminal receipt 和 published projection重新加载，禁止复用上一 Selector step 的 context。
5. builder 输出 exact `maf.mcp.agent_result_bundle.v1` 对象：
   - 顶层只允许 `schema/result_count/included_count/omitted_count/truncated/results`；
   - result 只允许 `call_sequence/content/source_truncated/carrier_truncated`；
   - sequence 必须唯一升序；三个 count 必须自洽；
   - 任何 source/carrier truncation或 omitted result 使 `truncated=true`。
6. bundle builder 在最终结构化 JSON 上预算，计入字段和转义；保留最新结果后恢复 sequence，整项省略
   更新 `omitted_count`，单项收缩按 UTF-8 边界并设置 `carrier_truncated=true`。
7. branch 最多读取现有 20 个 Call；不扫描 Conversation/Task全部 Artifact，不做 Gateway I/O，不增加
   cache、数据库字段或后台任务。

### 红绿验证

- 单结果、多结果、跨 automatic route sequence 和 restart 构建字节确定；
- 结果正文伪造 schema、分隔符、计数字段或 truncation 文本不能改变 closed bundle；
- 最新优先省略后 count/truncated 正确，输出仍按 sequence 升序；
- v2 source truncation正确；任何v1/null revision completed result使整个terminal carrier以
  revision-retired fail closed；其他未知revision以typed unsupported/authority错误fail closed，且都不
  影响旧Artifact；
- receipt、Artifact metadata、projection schema、SHA、owner、Call或Server version漂移 fail closed；
- terminal builder重复调用只读且结果一致，Gateway 调用数保持零。

聚焦命令：

```bash
conda run -n multi_agent python -m unittest \
  tests.integrations.mcp.test_selector_context
```

建议检查点提交：

```text
fix(mcp): build durable agent result bundles
```

## Checkpoint C：Agent Tool Result closed 投影与最终预算

### 修改范围

- `src/orchestration/agent_loop/result_projection.py`
- `tests/orchestration/test_agent_result_projection.py`

### 实现

1. `_MCP_MODEL_KEYS` 保持不变；`agent_projection` 只允许 closed bundle对象，必须在 sanitizer 后再次
   exact-validate schema、字段集合、类型、count、sequence和布尔一致性。
2. 非bundle的 `agent_projection` 字符串以`agent_result_invalid`拒绝；旧AgentItem已经持久化为
   model-result envelope，不会重新经过projector。不得影响OCR `text`路径。
3. `_fit_inline_model_result()` 不再对 bundle 使用通用字符串减半。增加 MCP bundle-aware final fit：
   - 预算包含整个 `model_view`、canonical JSON、model-result envelope 和 Tool preflight；
   - 优先保留最新 result，整项删除时更新 included/omitted count；
   - 必须保留一项时按 UTF-8 边界缩减 `content`，设置 `carrier_truncated=true`；
   - 同步设置 bundle `truncated=true` 和顶层 `model_view.truncated=true`；
   - 不改变 `source_truncated`；
   - 连最小 closed bundle都放不下时返回 `agent_result_projection_too_large`。
4. `projection_truncated` 必须反映最终 model-effective bundle，不再仅依赖进入 projector 前的原始
   `truncated`。
5. raw canonical hash、Artifact refs、continuation locator、Skill/delegated投影和 128 KiB Tool-result
   preflight保持原合同。

### 红绿验证

- 合法 bundle完整 inline；unknown key、错误 count、乱序/重复 sequence、非布尔 flag、非法 null
  和嵌套 raw key全部拒绝；
- 20,000-code-point、80,000-byte、escape-heavy和多字节边界不会无标记减半；
- outer shrink 后 content/count/truncation自洽，首尾 sentinel按是否截断得到确定性断言；
- generic `text`、OCR、Skill和delegated现有测试不退化；legacy string `agent_projection`新增拒绝断言；
- 输出不包含 `business_result/structured_content/raw_result/result_ref/path/credential/receipt`。

聚焦命令：

```bash
conda run -n multi_agent python -m unittest \
  tests.orchestration.test_agent_result_projection
```

建议检查点提交：

```text
fix(agent): preserve MCP bundle completeness
```

## Checkpoint D：Coordinator 全终态交付

### 修改范围

- `src/integrations/mcp/dispatch_coordinator.py`
- `tests/integrations/mcp/test_dispatch_coordinator.py`

### 实现

1. 扩展 Coordinator 已持有的 `MCPSelectorContextBuilderPort`，直接调用同一个
   `MCPDurableSelectorContextBuilder` 实例的 terminal bundle方法；`src/api/runtime.py` 零diff，不新增
   constructor参数、第二套 store、parser、builder 或 cache。
2. 在任何 branch/outbox terminal mutation之前，从 durable Call/receipt/projection重建 bundle。若
   completed Call authority冲突，改以现有 typed authority code失败收敛；不得先提交 completed branch
   再改写为 failed。
3. 统一覆盖所有会形成 committed Agent Tool result 的终态：
   - Selector FINISH；
   - Selector STOP；
   - automatic route exhausted；
   - 当前/后续 Server unavailable；
   - 后续 Tool denied；
   - selector/router/gateway/materialization/call-reservation/step-limit 等终态错误；
   - remote/approval/MRTR continuation最终完成或失败后的相同终态边界。
4. 有 bundle 时合并：
   - `output_payload["agent_projection"] = bundle`；
   - `output_payload["truncated"] = bundle["truncated"]`；
   - 不设置通用成功 `text`；
   - 原 `mcp_status`、Capability error、safe error、event、result ref和branch summary持久化不变。
5. 没有 completed result 时：
   - discover-only completed保留现有 `text`；
   - stopped/failed保持原错误，不伪造 bundle；
   - approval/input-required/waiting/remote pending继续返回现有 continuation payload。
6. OCR `workflow_kind` 成功仍使用既有 `external_text` / `text`，不包装成普通 bundle。
7. terminal carrier构建失败不触发 Tool/Gateway重放，不读取 raw，不把 `result_ref` 放入模型 allowlist。
8. 遇到任何v1/null revision completed result时返回`mcp_result_projection_revision_retired`并保留原
   branch/call/receipt/Artifact；其他未知revision返回typed unsupported/authority错误。不得继续拼装
   部分v2 bundle而隐瞒任何非v2结果。

### 红绿验证

至少增加以下表驱动场景，每项断言 Gateway call count、branch/outbox终态和 output payload：

- call completed → empty reason FINISH；
- call completed → nonempty reason FINISH；
- prior completed → STOP；
- prior completed → route exhausted；
- prior completed → next Server unavailable；
- prior completed → next Tool denied；
- prior completed → selector/router/gateway terminal error；
- no completed → discover-only/STOP/error；
- completed authority drift → typed failed + zero replay；
- v1/null revision completed result → revision-retired + Artifact零修改 + Gateway零调用；未知revision
  → typed unsupported + 同样零修改/零调用；
- approval/MRTR/remote waiting → no premature bundle，continuation final → bundle；
- OCR fixed workflow unchanged。

聚焦命令：

```bash
conda run -n multi_agent python -m unittest \
  tests.integrations.mcp.test_dispatch_coordinator \
  tests.capabilities.mcp_dispatch.test_selector_router_executor
```

建议检查点提交：

```text
fix(mcp): deliver results on terminal dispatch outcomes
```

## Checkpoint E：统一 Agent Loop E2E

### 修改范围

- `tests/e2e/test_mcp_server_explicit_agent_loop.py`
- 只有断言证明缺口时，才允许修改直接相关的 Agent context测试；不得改主 Agent prompt引导模型猜结果

### 实现与验证

1. 替换现有“Selector reason sentinel”伪结果用例：Selector 第一次返回 `call_tool`，fake Gateway 返回
   带首尾 sentinel 的真实 `CallToolResult`，Result Parser发布 projection，Selector 第二次返回空 reason
   FINISH。
2. 捕获下一次主 Agent请求并断言：
   - 对应 provider call ID 的 tool message只出现一次；
   - `model_view.agent_projection.schema=maf.mcp.agent_result_bundle.v1`；
   - result count/sequence/truncation正确；
   - 首尾 sentinel存在；
   - generic safe-reference文案不存在；
   - raw/ref/path/receipt/checkpoint/credential不存在。
3. 主 Agent fixture 必须先验证输入中存在业务 sentinel，再返回基于结果的最终回答；禁止使用无条件
   常量回答伪造“基于结果”。
4. 断言 Gateway business Tool只调用一次，Selector仍只在固定 Server内选择 Tool，Server Router在
   explicit binding零调用。
5. 再增加 oversized fixture，证明 main Agent能看见source/carrier/top-level `truncated`，不会把有界结果误报为
   完整；不要求模型在测试中自行推理自然语言。

聚焦命令：

```bash
conda run -n multi_agent python -m unittest \
  tests.e2e.test_mcp_server_explicit_agent_loop
```

建议检查点提交：

```text
test(mcp): prove agent result handoff end to end
```

## Checkpoint F：相关回归、静态证明与状态闭合

### 自动回归

按由窄到宽顺序运行：

```bash
conda run -n multi_agent python -m unittest \
  tests.integrations.mcp.test_result_parsing \
  tests.integrations.mcp.test_result_parser_worker \
  tests.integrations.mcp.test_result_artifact_projection \
  tests.integrations.mcp.test_historical_result_reprojection \
  tests.integrations.mcp.test_durable_result_lifecycle \
  tests.integrations.mcp.test_selector_context \
  tests.integrations.mcp.test_dispatch_coordinator \
  tests.orchestration.test_agent_result_projection \
  tests.capabilities.mcp_dispatch.test_selector_router_executor \
  tests.e2e.test_mcp_server_explicit_agent_loop \
  tests.api.test_conversation_messages_artifacts

conda run -n multi_agent python -m unittest discover \
  -s tests/integrations/mcp -p 'test_*.py'

conda run -n multi_agent python -m unittest discover \
  -s tests/orchestration -p 'test_*.py'

conda run -n multi_agent python -m unittest discover \
  -s tests/capabilities/mcp_tool -p 'test_*.py'

conda run -n multi_agent python -m unittest discover \
  -s tests/api -p 'test_*.py'

conda run -n multi_agent python -m compileall -q src tests

conda run -n multi_agent ruff check \
  src/integrations/mcp/result_parsing/projections.py \
  src/integrations/mcp/result_parsing/worker.py \
  src/integrations/mcp/result_parsing/service.py \
  src/integrations/mcp/result_parsing/projection_store.py \
  src/integrations/mcp/result_parsing/historical_reprojection.py \
  src/integrations/mcp/durable_result_lifecycle.py \
  src/integrations/mcp/result_artifact_projection.py \
  src/integrations/mcp/selector_context.py \
  src/integrations/mcp/dispatch_coordinator.py \
  src/orchestration/agent_loop/result_projection.py \
  tests/integrations/mcp/test_result_parsing.py \
  tests/integrations/mcp/test_result_parser_worker.py \
  tests/integrations/mcp/test_result_artifact_projection.py \
  tests/integrations/mcp/test_historical_result_reprojection.py \
  tests/integrations/mcp/test_durable_result_lifecycle.py \
  tests/integrations/mcp/test_selector_context.py \
  tests/integrations/mcp/test_dispatch_coordinator.py \
  tests/orchestration/test_agent_result_projection.py \
  tests/e2e/test_mcp_server_explicit_agent_loop.py \
  tests/api/test_conversation_messages_artifacts.py

conda run -n multi_agent python -c 'import src.integrations.mcp; import src.orchestration.agent_loop'

git diff --check
```

若红测证明必须修改清单外文件，应停止当前 Checkpoint、记录证据并先更新计划；不得以“必要 fixture”
为由静默扩大范围。

### 静态证明

- `selector.py` action、prompt 和 Server/Tool选择逻辑零行为变化；
- Gateway `list_tools/call_tool`、approval、remote Task、MRTR、resume envelope、no-replay和cancel零行为
  变化；
- bundle正文只来自 published agent projection，禁止 raw/result-ref/file fallback；
- parser/projection只接受v2；v1/null revision只由historical/continuation退役门识别并跳过，旧数据
  不被重写，未知非空版本fail closed；
- 所有 terminal output入口均由表驱动测试覆盖，waiting/interrupt不误携带业务结果；
- Artifact API生产代码、DTO和路由零diff；测试证明v2保持`ready`，v1历史projection沿既有
  `projection_invalid`安全降级且不暴露storage ref、下载或raw；
- 数据库 model/migration、Frontend、Rust、Skill、镜像、部署和 `prod` 零diff；
- `docker_cmd.md` 继续存在、ignored且untracked，全程未读取；
- Git staged paths不包含 `test.json` 或其他用户无关文件。

### 文档与检查点

自动验证完成后同步：

- 设计状态：`implemented_automated_pending_dev_smoke`；
- 本计划状态和每个 Checkpoint 的实际测试数、skip、commit；
- `src/integrations/AGENTS.md`：parser/projection v2-only、v1 retired skip和terminal bundle authority；
- `src/orchestration/AGENTS.md`：MCP closed bundle与显式 truncation投影；
- `tests/AGENTS.md`、`docs/AGENTS.md`、`CHANGELOG.md`；
- 最终 diff、Git status、每个检查点提交和“Cutback 与不可逆兼容边界”规定的A compatibility floor。

## Checkpoint G：开发环境真实新会话验收

本检查点需要独立的部署/环境操作授权；计划获批本身不授权构建镜像、部署或修改远端数据库。

获得授权后，只在 `main` 对应开发环境创建全新会话，使用与原问题等价的项目统计请求：

1. 部署前只读统计waiting/approval/input-required/remote-pending branch中已完成且revision非v2（包括
   v1/null revision）的Call；结果必须为0，否则停止部署，不自动取消、不修改这些任务。没有completed
   Call的等待branch不计入；
2. 确认主 Agent选择预期 `server_id`；
3. 确认 Selector只在当前 Server Tool catalog中选择项目统计 Tool；
4. 确认一次成功 MCP Tool Call生成 parser/projection v2及validated truncation，FINISH reason可为空；
5. 从低敏 Agent result evidence确认 bundle schema、count、sequence、truncation和业务 sentinel存在；
6. 确认最终回答引用实际项目统计，不再出现“无法直接查询”的兜底；
7. 确认无相同参数重复 Tool call、无unknown replay，日志/Event/AgentItem不含 credential、Endpoint、
   raw storage path、result ref或业务原文转储。

只记录 Task/Conversation的安全引用、状态、调用计数、projection大小/截断布尔和最终回答是否命中验收
条件；不得把完整业务清单、凭据或内部路径写入文档和 Git。

真实验收通过后，才可把设计与计划更新为 `implemented_verified`。若外部 Server、授权或部署状态
阻断，保留 `implemented_automated_pending_dev_smoke` 并记录精确缺口，不自动重跑历史失败 Task。

## Cutback 与不可逆兼容边界

- 任何环境写入首个v2 projection之前，可按E → D → C → B → A逆序回滚；
- 一旦写入v2，A成为该环境不可回退的compatibility floor：禁止运行v1-only binary。D/C/B可以逆序
  cutback，但必须继续保留parser/projection v2 writer、shared exact reader和v1 historical skip；
- A本身发生故障或需要撤销时只能向前修复；不得为恢复v1 reader而删除v2结果、改写receipt或重放
  MCP Tool；
- 无数据库/schema/data、Artifact内容迁移、Frontend、Rust或外部 Server回滚；
- cutback不得重放任何MCP Tool，也不得修改历史Task或retired v1数据。

License Requirement：复用现有 Python、Result Parser worker、durable projection store、Agent Tool
Result projector、unittest与仓库工具链；不新增依赖、第三方代码或许可变化。
