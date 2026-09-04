# MCP Selector 授权前参数校验实施计划

依据：`2026-09-04-mcp-selector-pre-approval-argument-validation-design.md`

状态：`implemented_verified`
日期：2026-09-04
目标分支：`main`

## 目标与边界

修复Selector可把不符合选中Tool `inputSchema`的参数封装为授权请求的问题。生产路径必须在任何Pending Action、Interrupt、
Grant或远端Call副作用前，完成附件物化、schema校验和基于最终arguments的route/预算/failed/rejected fingerprint门禁；
原始输出无效时复用现有一次repair，双失败以`selector_invalid_output`安全结束。Gateway保留实际调用前的同一校验防线。

不修改Server Router、主Agent Tool Result承载、approval/MRTR恢复语义、持久化schema、历史任务、Frontend、Rust/proto、外部
MCP Server、Skill revision、镜像、部署或`prod`。不直接修改开发库，不复活`task-c722595307e4`，未跟踪`test.json`保持
未读取、未修改、未暂存。

## Checkpoint 0：红测锁定真实失败形态

- 在`tests/capabilities/mcp_dispatch/test_selector_router_executor.py`增加具体`MCPToolSelector`测试：第一次`{}`被最终
  validator拒绝，第二次合法参数成功；两次拒绝后抛`MCPSelectorOutputError`；未传validator保持原行为；
- 锁定validator必须先返回物化后的action，再由context门禁计算最终fingerprint：最终fingerprint在failed/rejected集合时
  repair或拒绝，只有物化前fingerprint命中时不得误拒绝；
- 在`tests/integrations/mcp/test_dispatch_coordinator.py`以要求必填字段的catalog复现`lims_project_statistics + {}`语义：
  repair成功时只对合法参数生成一次授权，双失败时Interrupt/Grant/Call均为零且错误为`selector_invalid_output`；
- 在`tests/api/test_user_mcp_recovery_startup.py`的真实SQLite aggregate authority路径补同形回归，证明双失败时
  `mcp_pending_tool_action`、approval Interrupt、Grant和`mcp_call_record`均为零，repair成功时只持久化修复后的最终参数；
- 为非具体`MCPSelectorPort`增加单次授权前门禁回归，证明测试/扩展Selector不能绕过schema和最终fingerprint校验；
- 在现有OCR附件Coordinator场景上增加物化调用计数，证明先物化后校验且只物化一次。

验证：上述新增测试在生产代码修改前仅因缺少validator接口或授权前门禁而失败，既有无关测试不作为红测目标。

## Checkpoint A：共享 Tool 参数校验

- 新增`src/integrations/mcp/argument_validation.py`，定义稳定内部异常和
  `validate_mcp_tool_arguments(schema, arguments)`纯函数；
- 完整复用Gateway当前Draft 7 / Draft 2020-12选择及`jsonschema`校验行为；
- 异常消息保持固定，不携带参数值、schema正文、Endpoint或凭据；
- `MCPGateway.call_tool()`改用共享函数，并继续精确映射为`MCPGatewayError("mcp_tool_arguments_invalid")`；
- 删除Gateway原私有重复函数，但不改catalog冻结、output schema或Adapter自身纵深校验。

验证：共享函数单测与既有Gateway合法/非法参数测试通过，Gateway公开错误码和远端零调用行为不变。

## Checkpoint B：Selector 最终 action 门禁

- 为`MCPToolSelector.select()`增加可选keyword-only同步`action_validator`；
- 每次attempt顺序固定为parse → action validator → 最终context校验 → return；
- validator返回值必须是`MCPSelectorAction`，否则作为稳定`MCPSelectorOutputError`进入repair；
- 把现有context校验提取为Selector模块单一内部函数，具体Selector和Coordinator兼容路径共同复用；
- `MCPSelectorOutputError`继续进入现有一次repair，附件等其他领域异常继续原样传播；
- 未传validator、Run-bound生成器、Server Router及Selector prompt合同保持不变。

验证：Checkpoint 0的Selector红测转绿，现有Selector、Agent MCP binding和prompt安全测试全部通过。

## Checkpoint C：Coordinator 先物化、后校验、再授权

- 在Coordinator增加单一同步helper，接收当前catalog、attachments、binding mode和action；
- 对`call_tool`解析descriptor，调用现有附件物化；以物化后的最终arguments调用共享schema validator；失败转换为固定
  `MCPSelectorOutputError`，成功返回最终action；
- 具体`MCPToolSelector`通过`action_validator`在repair循环内调用helper，再对最终action执行共享context/fingerprint门禁；
- 其他`MCPSelectorPort`返回后由Coordinator顺序执行同一helper和最终context门禁，无repair；
- 删除Selector成功后的第二次附件物化；workflow识别、fingerprint、Grant、Pending Action和Call全部只消费最终action；
- Approval/MRTR恢复的既有Pending Action不重跑Selector，仍由Gateway最终schema校验。

验证：普通参数repair、真实aggregate双失败零副作用、自定义Port门禁、最终fingerprint和OCR单次物化测试全部通过；本次真实
失败形态不再先弹授权框。

## Checkpoint D：相关回归与静态门禁

- 运行`tests/capabilities/mcp_dispatch/test_selector_router_executor.py`；
- 运行`tests/integrations/mcp/test_dispatch_coordinator.py`、附件物化、Gateway、pending action与approval恢复相关测试；
- 运行`tests/orchestration/test_agent_mcp_binding.py`及MCP显式Server E2E；
- 运行API中用户MCP aggregate recovery、普通recovery和runtime wiring相关测试；
- 运行MCP integrations分层回归，以及风险相称的API/Orchestration/E2E全量；
- 运行compileall、变更面Ruff、共享validator重复实现扫描和`git diff --check`。

如全量门禁存在与本次无关的既有失败，必须给出精确测试名和对照证据，不得把它记录为通过或顺手修复。

## Checkpoint E：检查点与账本

- 复核最终diff只包含共享validator、Selector、Coordinator、聚焦测试和对应文档；
- 检查本次变更是否影响根目录、`src/`、`tests/`或`docs/`的`AGENTS.md`职责索引；
- 更新设计状态、实施计划、`docs/AGENTS.md`和`CHANGELOG.md`；
- 创建范围清晰的代码与账本Git检查点；不构建镜像、不部署、不直接修改数据库、不推送Git，除非用户另行明确要求。

## 完成声明

共享校验、最终action门禁、附件单次物化、具体Selector repair、自定义Port兜底、Gateway纵深防线、零副作用回归和相关自动
门禁已全部闭合。MCP integrations 587项（2项环境skip）、Orchestration 200项、API 651项、E2E 12项、compileall、变更面
Ruff、共享实现扫描与`git diff --check`均通过。当前真实失败Task保持历史终态，后续验证应使用新Task。

License Requirement：复用现有Python、`jsonschema`、Selector repair、附件物化、Gateway、unittest与现有测试链；无新增依赖、
第三方代码或许可变化。
