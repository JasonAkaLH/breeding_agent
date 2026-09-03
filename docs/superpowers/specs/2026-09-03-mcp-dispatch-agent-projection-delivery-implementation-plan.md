# MCP Dispatch 实际 Tool Result 交付实施计划

依据：`2026-09-03-mcp-dispatch-agent-projection-delivery-design.md`

## 状态

`planned`

设计已由用户确认，并完成三轮限定硬伤审计：累计 4 个 Major 已闭合，当前 0 Blocking / 0 Major；
未评 Minor，也不宣称完整 95 分信心门。本计划仅将已批准设计转换为可执行检查点，尚未修改生产
代码。

计划基线为 `main@634fa002`。范围外未跟踪文件 `test.json` 必须保持未读取、未修改、未暂存。

## 完成声明

只有同时满足以下条件，才可声明仓库实施完成：

1. 主 Agent 仍通过全局唯一 `mcp.dispatch.server_id` 选择初始 Server；Selector action、独立
   Server Router、Gateway、approval、resume 和 no-replay 控制流不变；
2. 新 Result Parser projection 持久化可信 `agent_projection_truncated`，旧 v1 projection 只读兼容
   为完整性 unknown，不迁移、不重投影、不访问网络；
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

先增加最小失败断言，分别证明旧实现存在以下缺口：

1. parser projection 没有可信 agent truncation 字段；
2. durable projection reader只返回字符串，不能区分 complete/truncated/legacy unknown；
3. 没有 closed、多结果、自描述的 Agent result bundle；
4. outer projector 对超限 `agent_projection` 仍可能普通字符串减半且不更新 truncation；
5. FINISH 只返回 Selector reason / generic text；
6. STOP、route exhausted、后续 unavailable/denied/terminal error 会丢弃先前成功结果；
7. 现有 E2E 只把 Selector reason sentinel 当结果，未执行实际 MCP Tool。

旧实现必须只在新增断言失败。若既有测试先失败，查明并隔离环境或用户改动后再继续；不得修改生产
代码掩盖非预期红灯。

## Checkpoint A：Result Parser 完整性元数据

### 修改范围

- `src/integrations/mcp/result_parsing/projections.py`
- `src/integrations/mcp/result_parsing/worker.py`
- `src/integrations/mcp/result_parsing/service.py`
- `src/integrations/mcp/result_parsing/projection_store.py`
- `src/integrations/mcp/result_artifact_projection.py`
- 对应 `tests/integrations/mcp/test_result_parsing.py`
- 对应 `tests/integrations/mcp/test_result_parser_worker.py`
- `tests/integrations/mcp/test_result_artifact_projection.py`
- `tests/integrations/mcp/test_historical_result_reprojection.py`

### 实现

1. 在 `projections.py` 增加 immutable `MCPBoundedAgentProjection(content: str, truncated: bool)`；
   `build_agent_projection()` 返回该值。`truncated` 的判断依据是 sanitizer 后完整 body 与最终 bounded
   body 是否一致；不得从 Tool 正文中的标记反推。worker只把 `content` 写入正文、把 bool 写入
   metadata，调用方不得重新计算。
2. 新 writer 使用 `maf.mcp.parsed_result_projection.v2`，在既有 closed envelope 上增加唯一布尔字段
   `agent_projection_truncated`；`user_view.projection_truncated` 继续只描述 user view，二者不得混用。
3. Result Service 对 v1/v2 分别 exact-validate：
   - v1 仍只接受原五字段；
   - v2 只接受原字段加 `agent_projection_truncated: bool`；
   - 未知 schema、缺字段、额外字段和非布尔值 fail closed。
4. Projection Store 显式维护 `{v1, v2}` closed reader 集合，`PROJECTION_SCHEMA` 作为当前 writer schema
   指向 v2；stage时解析出的实际 schema 必须进入 `MCPProjectionStagingHandle`，publish结果继续携带
   同一 schema。manifest closed shape、binding、SHA、文件身份和 192 KiB envelope 上限不变；schema
   由受 SHA 保护的 projection envelope及内存 handle共同证明，不修改旧 manifest合同。
5. `result_artifact_projection.py` 只使用 staging/published handle中相互匹配的实际 schema写 Artifact
   metadata，禁止用当前 writer常量猜测；旧 Artifact metadata继续保留 v1，不批量回写。
6. 不修改 raw parsed model、validated checkpoint schema、output schema validation、public user view、
   durable raw Artifact 或历史 reproject 调度策略。

### 红绿验证

- 短结果：v2 `agent_projection_truncated=false`；
- ASCII、多字节和 escape-heavy 超限结果：v2 flag 为 true，正文仍满足 20,000 code points / 80,000
  bytes；
- v1 fixture 继续可 stage/load，v2 fixture可 stage/load；未知 v3 和 shape drift 被拒绝；
- projection SHA/manifest/file tamper 仍 fail closed；
- 原敏感 key、URL、secret assignment 和 raw 泄漏断言保持通过。

聚焦命令：

```bash
conda run -n multi_agent python -m unittest \
  tests.integrations.mcp.test_result_parsing \
  tests.integrations.mcp.test_result_parser_worker \
  tests.integrations.mcp.test_result_artifact_projection \
  tests.integrations.mcp.test_historical_result_reprojection
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
   - `source_truncated: bool | None`；
   - projection schema/version。
2. `MCPPublishedAgentProjectionAuthority` 在现有 owner/Task/Node/Call/Server/version/ref/SHA/parser/
   checkpoint 校验后：
   - v2 返回 projection 正文和可信 bool；
   - v1 返回正文和 `source_truncated=None`；
   - Artifact `projection_schema`、projection envelope schema 与 closed reader必须一致；
   - 任一漂移继续抛现有 typed authority error，不降级读取 raw。
3. Selector context 继续只得到有界的 projection 正文列表，保持当前 prompt 和“最新优先、恢复 sequence
   顺序”策略；它不是 terminal carrier缓存。
4. 在同一 authority owner 中增加只读 terminal bundle builder。每次终态从 durable branch Call、
   terminal receipt 和 published projection重新加载，禁止复用上一 Selector step 的 context。
5. builder 输出 exact `maf.mcp.agent_result_bundle.v1` 对象：
   - 顶层只允许 `schema/result_count/included_count/omitted_count/truncated/completeness_known/results`；
   - result 只允许 `call_sequence/content/source_truncated/carrier_truncated`；
   - sequence 必须唯一升序；三个 count 必须自洽；
   - v1 source 使 `completeness_known=false`，但不自动声称 `truncated=true`；
   - 任何 source/carrier truncation或 omitted result 使 `truncated=true`。
6. bundle builder 在最终结构化 JSON 上预算，计入字段和转义；保留最新结果后恢复 sequence，整项省略
   更新 `omitted_count`，单项收缩按 UTF-8 边界并设置 `carrier_truncated=true`。
7. branch 最多读取现有 20 个 Call；不扫描 Conversation/Task全部 Artifact，不做 Gateway I/O，不增加
   cache、数据库字段或后台任务。

### 红绿验证

- 单结果、多结果、跨 automatic route sequence 和 restart 构建字节确定；
- 结果正文伪造 schema、分隔符、计数字段或 truncation 文本不能改变 closed bundle；
- 最新优先省略后 count/truncated 正确，输出仍按 sequence 升序；
- v1/v2 混合时 `source_truncated` 与 `completeness_known` 正确；
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

1. `_MCP_MODEL_KEYS` 保持不变；`agent_projection` 允许 closed bundle对象，但必须在 sanitizer 后再次
   exact-validate schema、字段集合、类型、count、sequence、nullable source flag 和布尔一致性。
2. 非 bundle 的历史 `agent_projection` 字符串只按现有兼容规则读取；新 coordinator不得再产生该
   形态。不得影响 OCR `text` 路径。
3. `_fit_inline_model_result()` 不再对 bundle 使用通用字符串减半。增加 MCP bundle-aware final fit：
   - 预算包含整个 `model_view`、canonical JSON、model-result envelope 和 Tool preflight；
   - 优先保留最新 result，整项删除时更新 included/omitted count；
   - 必须保留一项时按 UTF-8 边界缩减 `content`，设置 `carrier_truncated=true`；
   - 同步设置 bundle `truncated=true` 和顶层 `model_view.truncated=true`；
   - 不改变 `source_truncated` 和 `completeness_known`；
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
- generic `text`、OCR、legacy string `agent_projection`、Skill和delegated现有测试不退化；
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
   - result count/sequence/completeness正确；
   - 首尾 sentinel存在；
   - generic safe-reference文案不存在；
   - raw/ref/path/receipt/checkpoint/credential不存在。
3. 主 Agent fixture 必须先验证输入中存在业务 sentinel，再返回基于结果的最终回答；禁止使用无条件
   常量回答伪造“基于结果”。
4. 断言 Gateway business Tool只调用一次，Selector仍只在固定 Server内选择 Tool，Server Router在
   explicit binding零调用。
5. 再增加 oversized fixture，证明 main Agent能看见 `truncated/completeness`，不会把有界结果误报为
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
  tests.integrations.mcp.test_selector_context \
  tests.integrations.mcp.test_dispatch_coordinator \
  tests.orchestration.test_agent_result_projection \
  tests.capabilities.mcp_dispatch.test_selector_router_executor \
  tests.e2e.test_mcp_server_explicit_agent_loop

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
  src/integrations/mcp/result_artifact_projection.py \
  src/integrations/mcp/selector_context.py \
  src/integrations/mcp/dispatch_coordinator.py \
  src/orchestration/agent_loop/result_projection.py \
  tests/integrations/mcp/test_result_parsing.py \
  tests/integrations/mcp/test_result_parser_worker.py \
  tests/integrations/mcp/test_result_artifact_projection.py \
  tests/integrations/mcp/test_historical_result_reprojection.py \
  tests/integrations/mcp/test_selector_context.py \
  tests/integrations/mcp/test_dispatch_coordinator.py \
  tests/orchestration/test_agent_result_projection.py \
  tests/e2e/test_mcp_server_explicit_agent_loop.py

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
- v1/v2 schema集合closed，v1不被重写，未知版本fail closed；
- 所有 terminal output入口均由表驱动测试覆盖，waiting/interrupt不误携带业务结果；
- 数据库 model/migration、API DTO、Frontend、Rust、Skill、镜像、部署和 `prod` 零diff；
- `docker_cmd.md` 继续存在、ignored且untracked，全程未读取；
- Git staged paths不包含 `test.json` 或其他用户无关文件。

### 文档与检查点

自动验证完成后同步：

- 设计状态：`implemented_automated_pending_dev_smoke`；
- 本计划状态和每个 Checkpoint 的实际测试数、skip、commit；
- `src/integrations/AGENTS.md`：projection v1/v2 reader、v2 writer和 terminal bundle authority；
- `src/orchestration/AGENTS.md`：MCP closed bundle与显式 truncation投影；
- `tests/AGENTS.md`、`docs/AGENTS.md`、`CHANGELOG.md`；
- 最终 diff、Git status和每个检查点提交可独立回滚。

## Checkpoint G：开发环境真实新会话验收

本检查点需要独立的部署/环境操作授权；计划获批本身不授权构建镜像、部署或修改远端数据库。

获得授权后，只在 `main` 对应开发环境创建全新会话，使用与原问题等价的项目统计请求：

1. 确认主 Agent选择预期 `server_id`；
2. 确认 Selector只在当前 Server Tool catalog中选择项目统计 Tool；
3. 确认一次成功 MCP Tool Call生成 validated projection，FINISH reason可为空；
4. 从低敏 Agent result evidence确认 bundle schema、count、sequence、completeness和业务 sentinel存在；
5. 确认最终回答引用实际项目统计，不再出现“无法直接查询”的兜底；
6. 确认无相同参数重复 Tool call、无unknown replay，日志/Event/AgentItem不含 credential、Endpoint、
   raw storage path、result ref或业务原文转储。

只记录 Task/Conversation的安全引用、状态、调用计数、projection大小/截断布尔和最终回答是否命中验收
条件；不得把完整业务清单、凭据或内部路径写入文档和 Git。

真实验收通过后，才可把设计与计划更新为 `implemented_verified`。若外部 Server、授权或部署状态
阻断，保留 `implemented_automated_pending_dev_smoke` 并记录精确缺口，不自动重跑历史失败 Task。

## 回滚

- 每个 Checkpoint 独立提交，按 E → D → C → B → A 逆序回滚；
- D 回滚恢复旧 terminal output，但必须与 C/B 的新 bundle producer一起评估，禁止留下新 writer、旧
  reader不匹配；
- A writer回滚前必须先回滚所有 v2 consumer；已落盘 v2 projection 文件不删除、不改写，若旧代码
  无法读取则停止回滚并采用向前修复；
- 无数据库/schema/data、Artifact内容迁移、Frontend、Rust或外部 Server回滚；
- 回滚不得重放任何 MCP Tool，也不得修改历史 Task。

License Requirement：复用现有 Python、Result Parser worker、durable projection store、Agent Tool
Result projector、unittest与仓库工具链；不新增依赖、第三方代码或许可变化。
