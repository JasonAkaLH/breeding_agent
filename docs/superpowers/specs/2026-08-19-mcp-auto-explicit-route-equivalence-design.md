# MCP auto与显式绑定路由等价性设计

## 状态

- 日期：2026-08-19
- 分支：`main`
- 状态：设计已确认，尚未实施
- 范围：只在Orchestration的selected-route交接边界归一化MCP路由metadata；API、恢复模块与执行链零修改
- 自主复审：4轮（范围、现有consumer、恢复覆盖、最终一致性）；文档置信度96%

## 背景与故障证据

用户期望`auto`与显式`$Server`绑定只在“由谁选择Server”上不同。一旦Server已经选定，
discovery、附件物化、Tool approval、Gateway调用、结果处理与恢复必须使用同一套实现。

最新失败Task `task-af3c0637231a`证明当前合同违反了该原则：

- Task使用`routing_mode=auto`，Planner正确选择OCR Server；
- 当前消息持久化了唯一2,326,771-byte PNG附件；
- 根Message没有显式binding context，执行metadata因此保留`mcp_binding_mode=automatic`；
- 已验证成功的OCR附件物化与异步workflow路径只在
  `mcp_binding_mode=explicit_command`时启用；
- Selector连续20次生成缺少`source.type`的`start_parse_job`参数；
- 远端每次返回232-byte `isError=true / INVALID_ARGUMENT /
  Unsupported source type: None`；
- branch最终以`stopped_after_call`结束，Task表面为completed，但没有OCR正文。

这不是Server选择错误，而是路由来源泄漏进执行语义。

## 核心原则

```text
explicit route ─┐
                ├─> canonical selected route ─> 现有执行链
automatic route ┘
```

`automatic|explicit`只描述Server选择来源。路由层完成选择后，必须把两条路径归一化为
相同的既有执行合同。下游执行链不再接收到可导致功能分叉的路由来源。

## 目标

1. auto与explicit选中同一Server后，交给Executor的功能性metadata和input payload一致。
2. 复用当前已通过真实OCR smoke的显式绑定执行路径，不复制或重写执行逻辑。
3. 由同一个交接点覆盖auto初次执行、approval恢复和startup恢复，不在三条路径分别打补丁。
4. 保持显式绑定的UI badge和审计来源准确；auto不得显示成用户主动绑定。
5. 最新失败场景由20次错误Call变为1次成功OCR workflow并返回非空正文。

## 非目标

- 不修改`dispatch_coordinator.py`、`attachment_materialization.py`、
  `job_workflows.py`或`gateway.py`。
- 不修改`runtime.py`、`MCPDispatchWorkflowProvider`或resume envelope reader/writer；它们继续提供
  当前已经校验过的Server authority，路由交接层只做最终归一化。
- 不修改Tool discovery、Selector、参数物化、approval、pending payload、Call预算、
  result store或finalizer的业务源码与合同；auto只改为向它们提供与explicit相同的selected-server
  输入，因此会自然采用现有显式路径的附件workflow和单Server分支约束。
- 不修改Storage schema或64 KiB v2 resume envelope合同。
- 不修改现有多MCP DAG、Replanner或Artifact跨Node交接。
- 不修复streamed `_mcpResultRef`隐藏标准`isError=true`的独立Gateway缺陷；该问题继续单独记录。
- 不部署外部`ocr_mcp`源码或修改远端服务。

## 现有分叉

显式绑定在API preflight阶段产生持久化`ResolvedMCPServerBinding`，并向运行时提供：

```text
mcp_dispatch_server_id=<server>
mcp_binding_mode=explicit_command
forced_by_mcp_command=true
mcp_command=<display command>
```

auto路径由Planner在`mcp.dispatch.input_payload.server_id`中选择Server，但没有root Message
binding context。Orchestration执行和v2恢复因此默认填入：

```text
mcp_binding_mode=automatic
```

`binding_mode`本应是路由来源，却被现有执行链同时用作“Server是否已选定并可使用可信附件
workflow”的兼容开关，最终导致两条功能路径不同。

## 设计

### 1. 路由metadata归一化

新增一个路由层纯适配器，例如：

```text
normalize_selected_mcp_route(
    capability_id,
    input_payload,
    request_metadata,
    node_metadata
) -> {
    normalized_node_metadata,
    selection_source
}
```

仅当以下条件全部满足时归一化：

- `capability_id == "mcp.dispatch"`；
- `input_payload`只有一个非空`server_id`；
- Server选择来源能从已有system-managed request metadata闭合判断为`automatic|explicit`。

来源判定规则：

- `explicit`：request中由既有root binding authority生成的
  `forced_by_mcp_command=true`和非空`mcp_command`必须同时存在，且request中的
  `mcp_binding_mode=explicit_command`、`mcp_dispatch_server_id`与Node payload一致；
- `automatic`：request中同时不存在`forced_by_mcp_command`和`mcp_command`；已有
  `mcp_binding_mode=automatic`或仅有恢复用`mcp_dispatch_server_id`不属于显式marker；
- `forced_by_mcp_command`和`mcp_command`只出现一部分、值冲突或Server ID不一致：路由阶段失败关闭。

用户提交的同名metadata已经由现有API denylist移除；适配器不接受用户字段作为来源证据。

归一化后的功能性执行metadata固定使用现有兼容合同：

```text
mcp_binding_mode=explicit_command
```

这里的`explicit_command`不再被解释为“用户亲自选择”，而只表示“路由层已经选定并锁定
Server，可以进入现有selected-server执行路径”。不新增第三种执行模式，避免修改执行链。

适配器只在Node metadata中覆盖`mcp_binding_mode`。它不得把
`mcp_dispatch_server_id`、`forced_by_mcp_command`、`mcp_command`或selection source复制到Node；
Server继续只通过既有`input_payload.server_id`交给Executor。这样auto与explicit进入Executor的
功能性路由字段精确相同，而显示命令和选择来源仍停留在路由/UI边界。

### 2. 选择来源与执行metadata隔离

适配器返回一个瞬时、闭合的来源值：

```text
mcp_route_selection_source=automatic|explicit
```

`mcp_route_selection_source`只作为audit-only事件字段使用，不写入request/node metadata，因而
不需要在下游再次过滤，也不传给Coordinator、materializer、workflow或Gateway。

- explicit继续由Runtime从root Message的persisted binding context重建system-managed marker；
- auto由Planner/Router选中`mcp.dispatch + server_id`且不存在显式binding marker得出来源；
- 不根据是否存在附件、Server名称或Tool名称推断来源。

现有public badge只负责UI显示并保持不变，不作为适配器authority。auto只记录audit-only路由事件，
不生成显式badge。

### 3. 唯一交接入口

在`OrchestrationService._execute_node`把`WorkflowNodePlan`转换为
`CapabilityExecutionRequest`之前统一调用路由适配器。适配发生在Node进入running和发出
`node.started`事件之前，确保归一化失败时零网络调用且Node不会留下running残留。预期的路由合同
错误使用现有Node失败收敛路径记录`node.failed`，再交给现有completion policy收敛Task；不扩展
生命周期状态机。

这是唯一业务调用点。它覆盖Planner、固定Provider、Runtime Replanner、approval恢复和startup
恢复产生的全部`mcp.dispatch` Node，不在Provider或API Runtime分别打补丁。

处理结果：

- explicit现有metadata规范化后保持等价；
- auto的`server_id`不变，但执行`mcp_binding_mode`变成现有selected-server合同；
- 每个多MCP DAG Node独立归一化，不合并Node、不改变边或Server选择；
- 非MCP Node和不合法的MCP payload保持现有fail-closed行为；
- Task assignment、owner/Server availability与版本校验继续由现有authority执行，不在适配器复制。

### 4. approval与恢复

`MCPDispatchWorkflowProvider`和API startup代码保持不变。它们可以继续在重建Node时携带
当前的`automatic`来源标记，因为同一个交接适配器会在Executor调用前把已锁定Server统一为
selected-server执行合同。

恢复来源按优先级确定：

1. root Message存在合法persisted binding context时，现有Runtime生成完整显式marker，适配器判定
   来源为`explicit`；
2. 不存在binding context时，现有intent/envelope校验仍负责锁定同一`server_id`，适配器在没有
   显式marker时判定来源为`automatic`；
3. marker冲突、Server身份漂移或来源无法闭合时，在路由交接阶段失败关闭。

两种来源最终都输出同一个`mcp_binding_mode=explicit_command`执行合同。

现有startup对intent、outbox、v2 envelope和Server身份的校验顺序不变；适配器不读取、不修改、
不重写v2 envelope。startup仍不得重新运行Planner/Router，也不得选择其他Server。

### 5. 路由来源审计

纯适配器先在Node状态变化前完成。READY→RUNNING claim成功后、`node.started`和网络调用前记录
一个audit-only `mcp.route_normalized`事件，只包含：

```text
selection_source=automatic|explicit
execution_contract=selected_server
```

不记录Server ID、用户文字、Tool、附件、参数或result。现有Coordinator输出的
`binding_mode=explicit_command`从本变更起只解释为selected-server执行合同，不能再用作“用户显式
选择”的分析维度；选择来源必须读取新的route audit事件。现有显式UI badge仍是用户选择来源的
唯一UI依据，auto不会生成badge。

## 数据流

### auto初次执行

```text
Planner选择server_id
→ WorkflowNodePlan(mcp.dispatch)
→ 唯一route handoff adapter(source=automatic)
→ mcp_binding_mode=explicit_command
→ 现有Executor/Coordinator路径
```

### explicit初次执行

```text
用户$Server
→ persisted binding preflight
→ WorkflowNodePlan(mcp.dispatch)
→ 唯一route handoff adapter(source=explicit)
→ mcp_binding_mode=explicit_command
→ 同一现有Executor/Coordinator路径
```

### auto恢复

```text
intent + v2 envelope锁定server_id
→ 既有resume reader重建WorkflowNodePlan
→ 唯一route handoff adapter(source=automatic)
→ mcp_binding_mode=explicit_command
→ 同一现有恢复执行路径
```

## 不变量

1. Server选择来源不得改变Tool参数、workflow、approval或终态语义。
2. 同一Server、附件和用户请求的auto/explicit功能性Executor输入必须相同。
3. `mcp_route_selection_source`不得进入执行metadata、pending payload或v2 envelope。
4. auto不得获得显式用户binding badge。
5. intent已存在后，恢复只能使用其锁定Server，不重新路由。
6. 非MCP Node的metadata不得被路由适配器修改。
7. 路由归一化失败必须发生在Tool网络调用之前。
8. selected-server合同关闭Coordinator内部的`route_another_server`能力；跨多个MCP只能由现有
   Planner/Replanner产生多个`mcp.dispatch` Node，每个Node独立选择和归一化。本轮不改该DAG。
9. 预期路由校验错误必须使当前Node和Task按现有completion policy达到一致终态，不得残留
   `ready|running` Node或发起Executor调用。

## 错误处理

- 缺失或多余`server_id`：保留现有`mcp.dispatch` payload错误。
- 未知选择来源：路由阶段失败关闭。
- explicit binding context与intent不一致：保留现有authority conflict。
- auto intent/envelope Server不一致：恢复authority corruption，阻断执行。
- partial explicit marker或marker/payload Server冲突：`mcp_selected_route_invalid`，Node启动前按
  现有completion policy失败收敛。
- Server不可用或版本漂移：使用现有Server校验错误，不在路由适配器降级或换Server。
- 执行链产生的Tool错误继续使用现有行为；本设计不改变Gateway错误解释。

## 兼容性

- 不迁移或重写既有Task、intent、outbox或v2 envelope。
- 已终态历史Task保持不变，不自动复活20次错误Call的旧任务。
- explicit绑定API、root Message private context和public badge合同保持不变。
- legacy v1 resume reader保持不变。
- 回滚时只撤销route adapter及其调用点，执行链无需回滚。

## 测试与验收

### 路由单测

- auto `mcp.dispatch + server_id`归一化为selected-server执行metadata；
- explicit归一化结果与现有metadata等价；
- selection source只进入audit，不进入Executor metadata；
- 非MCP Node完全不变；
- malformed payload和unknown source失败关闭。
- 路由错误不调用Executor，Node/Task没有`ready|running`残留。

### 等价性测试

对同一Server、附件、Task assignment和请求分别构造auto与explicit：

- Executor收到相同`input_payload`；
- 功能性metadata相同；
- Coordinator生成相同Tool name、materialized arguments SHA和workflow kind；
- 均只产生1个业务Call；
- 均得到非空OCR正文和一致终态。
- 两个不同Server的既有多MCP Plan保留两个Node和原有边，每个Node独立归一化且不触发
  Coordinator内部`route_another_server`。

### 恢复测试

- auto approval恢复仍使用canonical selected route；
- auto startup v2恢复不再回到`automatic`执行分支；
- explicit恢复行为不变；
- intent/envelope Server冲突失败关闭且零网络调用。
- 恢复Provider和API Runtime的输出保持原样，证明修复只发生在统一route handoff。

### 真实回归

使用用户的2,326,771-byte PNG和普通auto请求“提取一下图片中的文字”：

- 只产生1个`start_parse_job`业务Call；
- OCR durable result非空；
- 最终答案包含图片中的实际文字；
- Task、两Node、branch、intent、outbox和receipt全部终态一致；
- v2 envelope不含Base64或Tool I/O。

## 实施文件边界

允许的业务源码修改只有：

- 新增`src/orchestration/mcp_route_handoff.py`：纯route adapter与闭合错误类型；
- 修改`src/orchestration/service.py`：唯一调用点、route audit和预期错误的现有失败收敛；
- 增加对应`tests/orchestration/`测试。

真实OCR回归可以扩充现有API/integration测试，但不得因此修改API或integration业务源码。最终
`git diff --name-only`若出现`src/api/`、`src/capabilities/mcp_dispatch/`、
`src/integrations/mcp/`或`src/storage/`业务源码，视为范围失败并停止合入。

## 已知独立缺陷

普通streamed Tool result可能在Adapter中被替换为`_mcpResultRef`，使Gateway无法看到内部
`isError=true`。本路由设计通过让auto OCR复用现有workflow路径解决当前用户场景，但不宣称
修复该Gateway缺陷。它必须在独立设计和修复中处理。

## 回滚

1. 停止新MCP提交并等待当前Node收敛。
2. 回滚route adapter及Orchestration中的唯一调用点和audit-only事件。
3. 不修改数据库、v2 envelope、pending payload、receipt或历史Task。
4. 恢复后auto重新使用旧`automatic`执行metadata；explicit路径不受影响。
