# MCP Selector 授权前参数校验设计

状态：`approved_pending_spec_review`
日期：2026-09-04
目标分支：`main`

## 1. 问题与证据

开发库对话`conv-web-d27d044651bf58`的只读证据显示：实验室管理Server健康且成功发现11个Tool，Selector选择
`lims_project_statistics`，但持久化Pending Action的参数精确为`{}`。用户完成`always_allow`后，Gateway才执行
`inputSchema`校验并返回`mcp_tool_arguments_invalid`；业务Call尚未创建，branch以`failed_no_call`终态化。

根因是Selector目前只校验action结构、Tool名称、预算和重复指纹，参数schema校验位于Gateway实际调用边界。于是无效参数
能够先被封装为Pending Action并触发授权，且错过Selector已有的一次repair机会。

## 2. 决策

采用“Selector可注入最终action validator，Coordinator负责物化与schema校验”的方案：

1. Selector每次解析并完成现有context校验后，调用可选同步validator；
2. Coordinator提供生产validator，先按现有规则物化附件参数，再校验选中Tool的`inputSchema`；
3. validator返回物化后的action，Selector只在全部校验通过后返回；
4. schema不匹配转换为稳定、无原始参数或schema内容的`MCPSelectorOutputError`，进入现有一次repair；
5. Gateway保留同一共享校验器作为实际调用前的纵深防线。

不采用Selector直接校验原始参数，因为OCR等显式附件流程必须先由Coordinator补齐`source`。不采用Coordinator单次后置校验，
因为那会避免错误授权却浪费现有repair能力。

## 3. 组件边界

### 3.1 共享参数校验

新增无状态MCP参数校验模块，复用当前Gateway的Draft 7 / Draft 2020-12选择和`jsonschema`行为。它只接受schema与
arguments，成功无返回值，失败抛稳定的内部参数校验异常；异常文本不得包含参数值、外部schema文本或凭据。

Gateway捕获该内部异常并继续对外映射为`MCPGatewayError("mcp_tool_arguments_invalid")`，因此实际调用边界和既有错误合同不变。

### 3.2 Selector 扩展

`MCPToolSelector.select()`新增可选keyword-only `action_validator`。每次尝试的固定顺序为：解析JSON → 现有context校验 →
外部validator → 返回。validator返回最终`MCPSelectorAction`；其`MCPSelectorOutputError`进入原有repair循环，其他异常不吞并。

未传validator的既有调用保持原行为，Server Router职责不变，Selector仍只在单个已选Server内选择Tool和arguments。

### 3.3 Coordinator 生产校验

正常生产装配使用具体`MCPToolSelector`，在其repair循环内注入validator。对`call_tool`：

- 从当前已冻结catalog解析descriptor；
- 调用现有`materialize_mcp_attachment_action()`；若返回物化结果，用其arguments替换action；
- 对最终arguments调用共享schema校验；
- 失败时抛稳定`MCPSelectorOutputError`，成功时返回最终action。

附件物化异常继续使用既有`MCPAttachmentMaterializationError`路径，不被伪装成Selector格式错误。Selector成功返回后不再重复附件
物化；workflow识别基于最终arguments执行。Approval/MRTR恢复路径继续读取既有Pending Action并由Gateway最终校验，不重新调用Selector。

对测试或扩展代码传入的其他`MCPSelectorPort`实现，Coordinator仍在其返回后、任何副作用前执行同一物化与校验；由于该窄Port
没有repair接口，无效参数直接按`selector_invalid_output`结束。这样不扩大Port合同，同时保证任何Selector实现都不能绕过授权前门禁。

## 4. 数据流与副作用顺序

新顺序为：发现catalog → Selector尝试 → 附件物化 → schema校验 → 必要时repair → 生成有效action → 查询Grant/创建
Pending Action/弹授权 → Gateway再次校验 → 预留并调用Tool。

因此无效参数在授权副作用之前被拒绝。第一次无效、repair有效时，只使用修复后的参数生成指纹、Pending Action和授权引用；第一次
无效参数不写Pending Action、Interrupt、Grant、Call或业务Event。

## 5. 失败边界

- 原始输出无效、repair有效：继续既有授权或调用流程；
- 两次schema均无效：以`selector_invalid_output`安全终止，不创建Pending Action、不弹授权、不保存Grant、不调用MCP；
- 附件物化失败：保留既有具体attachment错误码；
- Gateway最终校验失败：继续`mcp_tool_arguments_invalid`，作为竞态、旧Pending Action或边界漂移的纵深保护；
- schema自身异常：按参数校验失败处理，不把不可信schema诊断注入repair prompt。

不新增重试次数，不改变调用预算、approval round、fingerprint、Sidecar、数据库schema或持久化格式。

## 6. 测试与验收

1. Selector单测：validator拒绝`{}`后第二次合法参数成功；两次拒绝后抛`MCPSelectorOutputError`；未配置validator行为不变；
2. Coordinator回归：普通Tool要求必填字段时，`{}`不会创建approval/Pending Action，repair合法后才产生一次授权；
3. 双失败回归：终态为`selector_invalid_output`且Pending Action、Interrupt、Grant、Call均为零；
4. 附件回归：OCR显式附件先物化再校验，只物化一次且既有workflow行为不变；
5. Gateway回归：合法参数继续调用，无效参数仍在实际调用边界返回`mcp_tool_arguments_invalid`；
6. 运行MCP Dispatch聚焦测试、相关Integrations/API/E2E、compileall、变更面Ruff和`git diff --check`。

## 7. 范围外

不修改Server Router、主Agent Tool Result承载、approval语义、历史Pending Action、数据库数据/schema、Frontend、Rust/proto、
外部MCP Server、Skill revision、镜像、部署或`prod`。当前失败Task不自动重放或复活。

License Requirement：复用现有Python、`jsonschema`、Selector repair、附件物化、Gateway与unittest；无新增依赖、第三方代码或
许可变化。
