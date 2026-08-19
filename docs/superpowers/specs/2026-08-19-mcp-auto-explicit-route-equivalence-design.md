# MCP auto与显式绑定路由等价性设计

## 状态

- 日期：2026-08-19
- 分支：`main`
- 状态：设计已确认，尚未实施
- 范围：只在Orchestration的selected-route交接边界归一化MCP路由metadata；API、恢复模块与执行链零修改
- `document-perfectization`：2轮设计审阅修订及实施计划一致性复审；100/100，Pass

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
4. 保持显式绑定已有的root context、UI badge和audit evidence；route handoff不得重新推断或伪造
   auto/explicit来源。
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

### 1. route authority与来源分离

Server选择来源回答“谁选了Server”，route authority回答“这个Server是否允许进入执行”。二者
不得混用：

- explicit来源继续由既有root Message persisted binding context、public badge和
  `mcp.server_binding_resolved` audit evidence记录；
- automatic来源发生在Planner/Router选择时；本轮不新增或重算来源事件；
- route handoff不得根据用户文字、附件、Tool名称、Node metadata或marker缺失来推断来源；
- 下游`mcp_binding_mode=explicit_command`只表示selected-server执行合同，不再作为选择来源证据。

删除恢复/执行交接阶段的`automatic|explicit`来源重推断，也不新增
`mcp.route_normalized`事件。这样不会把remote continuation等未携带显式marker的合法路径误记为
automatic，并避免为了审计增加Storage/Event I/O和新的失败面。

### 2. route authority校验

新增一个无I/O、无异常的路由层纯适配器，例如：

```text
normalize_selected_mcp_route(
    capability_id,
    input_payload,
    node_metadata,
    pinned_server_id_present,
    pinned_server_id,
    available_server_ids
) -> {
    normalized_node_metadata,
    rejection_code
}
```

输入authority只能来自已有可信运行时状态：

- `pinned_server_id_present`按system-managed request metadata中是否存在
  `mcp_dispatch_server_id`计算，`pinned_server_id`保留其raw值；用户提交的同名metadata已被现有
  API denylist移除；
- `available_server_ids`只取`OrchestrationRequest.available_mcp_servers`，不得从提示词、Planner
  metadata或附件内容构造；
- Node payload仍必须精确为一个非空`server_id`，不得包含Endpoint、credential、Tool或Schema。
- payload Server和可信固定ID只在authority比较时使用`.strip()`后的值；适配器不得改写原
  `input_payload`。`pinned_server_id`字段一旦存在但不是非空字符串，必须拒绝，不能降级使用allowlist。

校验顺序固定为：

1. 非`mcp.dispatch`：原样返回，不适用authority校验；
2. payload不是exact `{"server_id": <non-empty string>}`：原样返回，由现有Executor继续产生
   `mcp_dispatch_payload_invalid`，不得新增重复校验错误；
3. 存在`pinned_server_id`：payload Server必须与其相等，`available_server_ids`不能覆盖冲突；
4. 不存在`pinned_server_id`：payload Server必须属于非空`available_server_ids`；
5. 3或4不满足：返回`mcp_selected_route_not_authorized`，不得调用Executor或网络。

这形成两层防护：route handoff限制模型只能使用可信固定ID或当前请求的Server allowlist，现有
Coordinator继续按Task owner重新验证Server存在、enabled、available且未删除。本轮不复制
Coordinator的owner、config/security version或健康状态校验。

### 3. canonical selected-server metadata

authority通过后，适配器执行精确投影：

```text
normalized = copy(node_metadata)
remove normalized.mcp_dispatch_server_id
remove normalized.forced_by_mcp_command
remove normalized.mcp_command
set normalized.mcp_binding_mode = "explicit_command"
```

删除动作是必需合同，不只是“不要新增”：v2恢复当前可能已经把
`mcp_dispatch_server_id`放在Node metadata中。投影后，Server只通过既有
`input_payload.server_id`进入Executor，显示命令、选择来源和恢复固定ID均不进入功能性metadata。

这里的`explicit_command`不再解释为“用户亲自选择”，只表示“Server已经由路由authority选定并
锁定，可以进入现有selected-server执行路径”。不新增第三种执行模式，不修改执行链consumer。

### 4. 唯一交接入口与失败收敛

在`OrchestrationService._execute_node`把`WorkflowNodePlan`转换为
`CapabilityExecutionRequest`之前调用适配器。这是唯一业务调用点，覆盖Planner、固定Provider、
Runtime Replanner、approval恢复、startup恢复和remote continuation产生的全部待执行
`mcp.dispatch` Node，不在Provider或API Runtime分别打补丁。

现有`_assert_mcp_continuation_execution_owned(request)`必须保持第一道门禁；固定顺序为：

```text
continuation ownership → route authority → scheduler → Executor
```

无有效continuation lease的worker不得执行route CAS或修改Node状态。

- authority通过：使用canonical Node metadata继续既有READY→RUNNING→Executor流程；
- `mcp_selected_route_not_authorized`：以当前Node状态做CAS并直接收敛为FAILED，记录不含Server ID、
  用户文字或payload的`node.failed` code，跳过scheduler/Executor/network，再由现有completion
  policy收敛Task；
- CAS因取消状态变化失败：返回latest cancellation authority并由现有Task取消路径收敛；其他CAS
  丢失一律抛确定性route rejection conflict，当前worker不得把空output当作其他worker的完成结果、
  不得继续下游或执行网络调用；
- malformed payload：不由适配器收敛，继续使用现有Executor错误路径，保持错误码兼容。

该处理只复用既有Node状态和completion policy，不新增生命周期状态、不修改普通Node失败语义。
每个多MCP DAG Node独立校验和归一化，不合并Node、不改变边或Server选择。

### 5. approval与恢复

`MCPDispatchWorkflowProvider`和API startup代码保持不变。恢复不重新判断auto/explicit：

- approval或startup已提供`mcp_dispatch_server_id`时，将其作为`pinned_server_id`；
- intent、outbox和v2 envelope的Server一致性仍由既有恢复authority先行验证；
- route handoff只验证Node payload与固定ID一致并生成canonical metadata；
- 不读取、修改或重写v2 envelope，不重新运行Planner/Router，也不选择其他Server。

### 6. 非功能约束

- 适配器必须为纯函数，不执行Storage、网络、Event/Audit或时间相关I/O；相对于附件、消息、Tool
  参数和结果大小为O(1)，实际CPU成本只允许来自有界Node metadata浅拷贝和当前Server ID集合投影；
- 不记录Server ID、文件名、用户文字、附件、Tool、参数、result或credential；
- 不增加正常MCP调用次数、Selector step、approval round或resume envelope大小；
- 非MCP Node必须保持逐字段不变；malformed MCP payload必须保留既有错误码；
- route authority拒绝必须在scheduler、Executor和Tool网络调用前完成。

## 数据流

### auto初次执行

```text
Planner选择server_id
→ WorkflowNodePlan(mcp.dispatch)
→ available_server_ids authority校验
→ 唯一route handoff adapter
→ mcp_binding_mode=explicit_command
→ 现有Executor/Coordinator路径
```

### explicit初次执行

```text
用户$Server
→ persisted binding preflight
→ WorkflowNodePlan(mcp.dispatch)
→ pinned_server_id authority校验
→ 唯一route handoff adapter
→ mcp_binding_mode=explicit_command
→ 同一现有Executor/Coordinator路径
```

### auto恢复

```text
intent + v2 envelope锁定server_id
→ 既有resume reader重建WorkflowNodePlan
→ pinned_server_id authority校验
→ 唯一route handoff adapter
→ mcp_binding_mode=explicit_command
→ 同一现有恢复执行路径
```

## 不变量

1. Server选择来源不得改变Tool参数、workflow、approval或终态语义。
2. 同一Server、附件和用户请求的auto/explicit功能性Executor输入必须相同。
3. 选择来源和显式命令字段不得进入执行metadata、pending payload或v2 envelope。
4. auto不得获得显式用户binding badge。
5. intent已存在后，恢复只能使用其锁定Server，不重新路由。
6. 非MCP Node的metadata不得被路由适配器修改。
7. route authority失败必须发生在scheduler、Executor和Tool网络调用之前。
8. selected-server合同关闭Coordinator内部的`route_another_server`能力；跨多个MCP只能由现有
   Planner/Replanner产生多个`mcp.dispatch` Node，每个Node独立选择和归一化。本轮不改该DAG。
9. route authority错误必须使当前Node和Task按现有completion policy达到一致终态，不得残留
   `ready|running` Node或发起Executor调用；malformed payload继续走既有Executor失败路径。
10. 外部提示词最多影响automatic在当前可信allowlist内的选择，不得选择allowlist外、跨owner、
    已禁用、删除中或不可用的Server，也不得注入Endpoint、credential或执行metadata。

## 错误处理

- 缺失或多余`server_id`：保留现有`mcp.dispatch` payload错误。
- explicit binding context与intent不一致：保留现有authority conflict。
- auto intent/envelope Server不一致：恢复authority corruption，阻断执行。
- 固定Server与payload冲突、allowlist为空或不包含payload Server：
  `mcp_selected_route_not_authorized`，Node启动前按现有completion policy失败收敛。
- Server不可用或版本漂移：使用现有Server校验错误，不在路由适配器降级或换Server。
- 执行链产生的Tool错误继续使用现有行为；本设计不改变Gateway错误解释。

## 兼容性

- 不迁移或重写既有Task、intent、outbox或v2 envelope。
- 已终态历史Task保持不变，不自动复活20次错误Call的旧任务。
- explicit绑定API、root Message private context和public badge合同保持不变。
- 既有`mcp.server_binding_resolved`继续作为explicit来源audit evidence；不新增推断型来源事件。
- legacy v1 resume reader保持不变。
- 回滚时只撤销route adapter及其调用点，执行链无需回滚。

## Rollout与迁移

- 不新增feature flag、Storage schema、数据迁移、历史Task重写或v2 envelope迁移；
- 重启服务前停止新提交并等待当前本地执行handle按既有生命周期收敛，避免把进程内Plan当作新的
  持久化合同；
- 部署时已经进入Executor或已有`may_have_dispatched`证据的Node继续按既有聚合恢复/no-replay规则
  收敛，不中断、不重放；
- 部署后的新Task、用户随后触发的approval恢复，以及既有startup v2 intent在下一次route
  handoff使用新authority与canonical metadata合同；
- 实施后先运行Orchestration定向回归，再重启本地前后端并执行用户PNG的全新auto Task smoke；
- 本设计不授权`prod`部署。生产发布需另行确认目标分支、回滚窗口和运行中MCP Node状态。

## 测试与验收

### 路由单测

- auto exact `mcp.dispatch + server_id`只有在Server属于`available_server_ids`时归一化；
- explicit、approval和startup固定ID只有在payload Server与`pinned_server_id`一致时归一化；
- pinned ID存在时不能被allowlist中的另一个Server覆盖；
- canonical投影主动删除`mcp_dispatch_server_id`、`forced_by_mcp_command`和`mcp_command`，只保留
  `mcp_binding_mode=explicit_command`；
- 带前后空白但非空的Server ID按`.strip()`值做authority比较且原payload不变；无效固定ID不能
  降级使用allowlist；
- 非MCP Node完全不变；
- malformed payload原样交给现有Executor，保持`mcp_dispatch_payload_invalid`且Coordinator零调用；
- allowlist外或固定ID冲突时不调用scheduler/Executor，Node/Task没有`ready|running`残留；
- 用户metadata和Planner metadata不能伪造pinned ID、allowlist或执行模式。

### 等价性测试

对同一Server、附件、Task assignment和请求分别构造auto与explicit：

- Executor收到相同`input_payload`；
- 功能性metadata相同；
- auto/explicit进入Executor前均不含选择来源、显示命令或重复Server ID；
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
- v2恢复Node已有的`mcp_dispatch_server_id`在Executor前被canonical投影移除。
- remote continuation不需要`forced_by_mcp_command`或来源重推断；pending MCP Node依靠固定ID或当前
  allowlist完成authority校验。

### 真实回归

使用用户的2,326,771-byte PNG和普通auto请求“提取一下图片中的文字”：

- 只产生1个`start_parse_job`业务Call；
- OCR durable result非空；
- 最终答案包含图片中的实际文字；
- Task、两Node、branch、intent、outbox和receipt全部终态一致；
- v2 envelope不含Base64或Tool I/O。

## 实施文件边界

允许的业务源码修改只有：

- 新增`src/orchestration/mcp_route_handoff.py`：无I/O的有界纯route adapter与闭合result contract；
- 修改`src/orchestration/service.py`：唯一调用点及authority拒绝的Node失败收敛；
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
2. 回滚route adapter及Orchestration中的唯一调用点；没有新增audit事件需要回滚。
3. 不修改数据库、v2 envelope、pending payload、receipt或历史Task。
4. 恢复后auto重新使用旧`automatic`执行metadata；explicit路径不受影响。
