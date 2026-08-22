# Phase 5：API、SSE、Frontend 与 Observability 适配 PRD

- **日期**：2026-08-22
- **状态**：in_progress（P5-A green；P5-B pending）
- **文档审阅**：document-perfectization第二次全量审计100/100通过；后端投影、事件与指标检查点已闭合
- **父总纲**：`00-统一同模型AgentLoop总纲PRD.md`
- **上游**：Phase 0～4必须`proof_complete`
- **主责需求**：FR-21、FR-22
- **主责NFR**：可观测性、API/Frontend兼容、可访问性
- **直接参与者**：最终用户、API/Frontend维护者、Lifecycle/History维护者、Observability与可访问性审查者
- **目标结果**：在不切换真实执行入口的前提下，让API、SSE、history和Frontend完整消费Agent投影，并准备可审计cutover-readiness报告。

## 1. 目标与价值

Agent Loop改变内部执行轨迹，但用户仍依赖Task、TaskNode、SSE、Interrupt、history和下载/披露合同。Phase 5先完成
外部兼容与观测，使Phase 6只承担控制面切换和旧代码删除，而不同时发明Frontend/API语义。

## 2. 进入条件

- Core Loop、final publisher、waiting/recovery均在test assembly闭合；
- Task/Run/Node/Interrupt状态映射和Agent events已经有durable contract；
- 当前API DTO、SSE replay、history和Frontend reducer基线已锁定。

### 2.1 当前证据与受影响模块

| 锚点 | 当前事实 | 本阶段影响 |
|---|---|---|
| `src/api/dto.py`、`routes/tasks.py::get_task_graph` | Graph DTO/route当前读取Node/Edge | 改为testable empty-edge invocation ledger投影 |
| `src/api/sse.py`、Task/history routes | 已有event replay和assistant history合同 | 增加Agent events并保持外部endpoint/terminal语义 |
| `frontend/src/api/taskEvents.ts`、`domain/taskEvents.ts` | 消费graph、planner/soft-skill reasoning和terminal events | 增加Agent事件/reducer，保留旧runtime兼容到Phase 6 |
| `frontend/src/App.tsx`及approval/status组件 | 负责refresh、waiting、approval和history展示 | 适配多waiting/final replay并保持可访问性 |
| API/observability/Frontend tests | 已覆盖SSE、restore、approval和event conflict | 以fake events/test-only assembly证明新投影 |

## 3. 范围与非范围

### 3.1 范围内

- AgentRun到Task/TaskNode/Interrupt DTO投影；
- `/tasks/{id}/graph` empty-edge invocation ledger；
- Agent durable events、transient reasoning和低基数metrics；
- final delta/final、fallback disclosure、download声明和history恢复；
- Frontend task reducer、progress、waiting/approval/interrupt和refresh restore；
- 可访问性回归；
- test-only API assembly/fake events；
- 固定`docs/prd/backend/unified-agent-loop/cutover-readiness.md`证据产物。

### 3.2 非范围

- 不让真实用户请求进入Agent Loop；
- 不增加runtime feature flag、request selector或shadow副作用；
- 不删除Planner/Replanner事件解析前的兼容代码；
- 不改变公开endpoint path/method；
- 不新增UI产品面或交互控件；
- 不部署`prod`。

## 4. 外部状态投影

| AgentRun | Task | TaskNode/Interrupt |
|---|---|---|
| `running` | `running` | current calls pending/running/terminal |
| `waiting_for_input` | `running` | 至少一个Node waiting_for_input和open Interrupt |
| `waiting_for_dependency` | `running` | 至少一个Node waiting_for_dependency和remote authority |
| `completed` | `completed` | final receipt存在，final node completed |
| `failed` | `failed` | closed fatal receipt |
| `cancelled` | `cancelled` | closed cancel receipt |

API不得新增Task waiting status。状态不一致为fatal consistency error，不按宽松值猜测。

## 5. Graph兼容投影

`GET /api/v1/tasks/{task_id}/graph`继续存在，但语义固定为只读调用账本：

- nodes来自TaskNode invocation/final ledger；
- DTO需要的`criticality/dependency_type`固定投影为`required/hard`；
- `edges=[]`；route不得调用TaskEdge repository；
- `task.graph_created`在Run初始化时发出`edge_count=0`，保持Frontend进入running；
- 新路径不产生`task.graph_updated`、Planner/Replanner事件；
- 文档明确graph不能推断未来调用顺序。

## 6. Event合同

| Event | 可见性 | 最小低敏字段 |
|---|---|---|
| `agent.run.started` | audit | model option digests、routing mode |
| `agent.sample.started/completed` | audit | sample ID、tool count、usage、duration、outcome |
| `agent.tool_call.accepted` | audit | call ID、capability kind、ordinal、argument digest |
| `agent.tool_result.committed` | audit | call ID、status、error code、artifact count、digest |
| `agent.run.waiting/resumed` | frontend | reason kind、interrupt ID、remaining count |
| `agent.context.compacted` | audit | covered range/digest、token counts、outcome |
| `agent.run.lease_lost` | audit | closed phase/reason、lease revision；无token |
| `agent.run.completed/failed/cancelled` | frontend/audit | terminal outcome、counts、duration |
| `agent.reasoning_delta` | transient | delta、ordinal、sample ID；不得durable |

现有Node、MCP、Skill、`main_agent.output_delta/final`和fallback events继续生效。新路径不产生
`planner.reasoning_delta`/`soft_skill.reasoning_delta`。

## 7. Metrics

至少实现源设计定义的active/total/time-to-final/sample/tool-call/waiting/resume/compaction/aborted/final-publish和lease
metrics。Label只允许closed outcome、capability kind、reason kind、phase等低基数枚举；禁止task/conversation/call/
capability ID、用户标识和模型文本。

Waiting Task不占model/capability worker，但仍受Task资源和MCP lease配额；保留30 active Task backpressure。

## 8. Frontend行为

- 保持现有消息、Task progress、approval dialog、Interrupt、Artifact/download和history体验；
- reasoning只渲染transient `agent.reasoning_delta`；tool result不渲染为assistant answer；
- 多waiting按event的interrupt/node ID逐个呈现，回答后若仍waiting继续显示；
- refresh/reconnect从Task graph/history/open Interrupt恢复，不依赖DAG edges；
- final delta重放按event ID去重；
- 旧客户端看到unknown Agent audit event时安全忽略，仍可依赖Task/Node/final事件完成。

## 9. 可访问性

本设计不新增控件。若实现修改approval、interrupt或progress DOM，必须保持：

- Dialog焦点进入、trap和关闭/完成后的合理焦点恢复；
- 键盘可操作和语义label；
- waiting切换不重复抢焦点；
- screen-reader可辨别状态与错误；
- refresh后恢复不产生重复announcement。

无DOM变化也必须记录对应组件测试未受影响，不能把可访问性标记为无条件N/A。

## 10. 功能需求与验收

| ID | Requirement | Acceptance |
|---|---|---|
| AL-P5-01 | Task/SSE/Interrupt/history公开合同保持。 | 旧客户端fixture和新Agent fixture均通过。 |
| AL-P5-02 | `/graph`返回nodes、固定字段和empty edges且不读TaskEdge。 | Repository spy调用数为0。 |
| AL-P5-03 | Agent events闭合、低敏并可replay。 | Contract/leak/duplicate event tests。 |
| AL-P5-04 | Reasoning只transient。 | Durable event/history/audit无delta正文。 |
| AL-P5-05 | Metrics标签低基数。 | Label allowlist和secret/high-cardinality拒绝tests。 |
| AL-P5-06 | Final history/fallback/download合同保持。 | Live与refresh restore一致。 |
| AL-P5-07 | 多waiting逐项展示且不提前complete。 | Reducer/App integration tests。 |
| AL-P5-08 | Frontend可访问性保持。 | Focus、keyboard、semantics tests。 |
| AL-P5-09 | Test-only assembly不可被真实请求选择。 | Runtime config/route inventory无Agent switch。 |
| AL-P5-10 | 生成固定cutover-readiness报告。 | `cutover-readiness.md`包含README定义的commit、阶段状态、入口、tests、docs、schema和blockers字段。 |

## 11. 失败与降级

- unknown audit event：Frontend忽略，不改变Task terminal；
- event ID同ID不同payload：进入resync/error，不覆盖已有事实；
- SSE断线：按现有cursor replay，必要时Task/history/graph恢复；
- graph投影不一致：后端fail closed，不返回伪edge；
- final delta发布后commit未完成：恢复重放同ID；
- fallback metadata缺失/非法：安全隐藏notice但不篡改assistant正文；
- metric backend失败不得改变Run业务终态，但必须记录观测故障。

### 11.1 跨阶段NFR协作

| NFR | 本阶段责任 | 后续复验 |
|---|---|---|
| 可观测性 | 主责：closed events、transient reasoning和低基数metrics | Phase 6/7全入口及泄漏扫描复验 |
| API/Frontend兼容 | 主责：Task/SSE/Interrupt/history/empty-edge graph | Phase 6切换、Phase 7物理删除后复验 |
| 可访问性 | 主责：approval/interrupt/progress focus、keyboard、semantics | Phase 6/7完整Frontend门禁复验 |
| Provider/安全/性能/final/recovery | 消费Phase 0～4事实并只做安全projection | Event/history/metric leak、waiting资源和final replay tests |

## 12. 测试与门禁

最低后端域：API DTO、task query/graph、Task SSE、main-agent history/fallback、observability。Frontend必须完整运行：

```bash
cd frontend
npm test -- --run
npm run typecheck
npm run build
```

Agent投影只能通过test-only assembly、fake events和fixtures验证。不得增加请求级feature flag或生产route到新Loop。

后端必须创建/运行精确Agent投影与metrics目标，并保留现有Task/SSE/history回归：

```bash
conda run -n multi_agent python -m unittest \
  tests.api.test_agent_task_projection \
  tests.api.test_task_query \
  tests.api.test_task_events_sse \
  tests.api.test_main_agent_llm \
  tests.observability.test_agent_metrics
```

上述新模块不存在、零测试或non-zero exit均失败。

## 13. 风险、假设与开放问题

| 风险 | 缓解/阻断条件 |
|---|---|
| `/graph`名称让客户端误以为存在未来DAG | 文档固定invocation-ledger语义、edges空和repository spy |
| 新旧event并存造成reducer冲突 | Phase 5只test fixture；Phase 6同检查点删除旧生产/消费分支 |
| Metric label泄漏ID/文本或基数失控 | Closed allowlist、leak scan和high-cardinality拒绝tests |
| Waiting/refresh重复Dialog或抢焦点 | Reducer幂等、focus/keyboard/announcement tests |
| Cutover遗漏入口或文档 | 固定`cutover-readiness.md`字段与Phase 6前双向inventory校验 |

已确认假设：现有Task/SSE/history endpoint保持；旧客户端可以忽略未知Agent audit event并继续依赖Task/final合同。
开放问题：无。

## 14. Git检查点与回滚

- API/Frontend兼容代码可additive落地，但真实events仍来自旧runtime直到Phase 6；
- 不删除旧Planner/Replanner event parsing，Phase 6随旧runtime统一清理；
- 回滚删除Agent-only DTO projection/reducer分支，不改Task持久数据；
- 未通过Frontend三门禁或可访问性回归时保持`blocked`。

## 15. 完成与交接

AL-P5-01～10、后端API/observability和Frontend三门禁通过；`cutover-readiness.md`字段闭合且无未知入口；真实执行入口
仍为旧DAG。

交付Phase 6：Task/API投影、SSE/history/event/metric合同、Frontend reducer和固定`cutover-readiness.md`。Phase 6
不得依赖DAG edge或Planner/Replanner事件作为新路径事实源。

### 15.1 当前实施证据

- P5-A新增test-only Agent Task/history/graph投影，waiting继续映射Task `running`；状态漂移fail closed，Run初始化投影
  `task.graph_created(edge_count=0)`，Agent graph固定
  `required/hard`和empty edges，repository spy确认不读取TaskEdge。
- Durable Agent event采用closed低敏字段；reasoning delta仅通过transient broker且不写audit/durable storage。19项源设计
  指标名称完整闭合，label只接受closed outcome/kind/reason/phase，metric backend故障不改变业务结果。
- Agent final在live与Sidecar-like refresh投影中保持确定性单Message、`stream_status=complete`、fallback metadata与既有
  Artifact/download安全链；真实`build_api_runtime`未装配Agent projection或请求开关。
- P5-A canonical 41项、API discover 493项、storage discover 398项（7项既有外部PG环境skip）、observability discover
  39项通过。P5-B的Frontend、多waiting、可访问性三门禁和`cutover-readiness.md`尚未执行，因此Phase 5不得标记
  `proof_complete`。
