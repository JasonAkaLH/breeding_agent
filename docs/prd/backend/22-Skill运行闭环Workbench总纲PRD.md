# Skill 运行闭环 Workbench 总纲 PRD

- **项目**：breeding_agent
- **范围**：后端平台层 Skill 运行闭环、内部 Workbench capability、Skill 执行后验证与受控重编排
- **文档状态**：总体设计稿，待拆分阶段实施 PRD
- **日期**：2026-06-17
- **目标模块**：`src/orchestration/`、`src/capabilities/`、`src/api/runtime.py`、`src/integrations/agent_skills/`、Skill 平台准入契约

## 1. 背景与问题

当前 breeding_agent 的后端已经形成“通用主代理框架 + 外部 Skill 能力池”的结构。Skill 是平台外部资源：平台负责加载、准入、编排、执行、审计和最终汇总，不应把某些具体 Skill 的业务形态写入主框架规则。

现有平台基线：

- `WorkflowPlan` 是无环 DAG，核心字段为 `nodes`、`metadata`、`max_replans`、`max_dynamic_nodes`。
- `WorkflowPlanValidator` 会校验 capability、JSON serializable payload、依赖存在与无环。
- `LLMWorkflowProvider` 只可信任 LLM 输出 public capability 高层 DAG，再由 `WorkflowExpander` 展开 `skill.*` macro。
- `RuntimeReplanner` 已能在节点完成后或任务完成前追加 revised plan，但仍受重排次数和动态节点预算约束。
- `SkillWorkflowProvider` 对直接 Skill plan 默认 `max_replans=0`、`max_dynamic_nodes=0`，导致 Skill 执行链偏一次性。
- v2 Skill 已通过 `skill.contract.yaml` 进入 capability 池，平台具备读取 capability、runtime、entrypoint、input schema、output contract、resource policy 的基础。

下一步要增强的是平台对 Skill 的运行闭环：不是把 plan 改成 cyclic graph，也不是为具体 Skill 定制流程，而是在每一版无环 DAG 外层增加受控 Observe -> Verify -> Replan 机制。系统应根据 Skill contract、运行策略、输入/输出契约和安全边界自动插入或追加内部工作台节点，形成可验证的 digest，再供最终回答或平台诊断使用。

## 2. 目标

- **G1 保持 DAG 不变**：每一版 `WorkflowPlan` 继续是无环 DAG，不引入循环图、LangGraph 式 cyclic state graph 或新的 public plan schema。
- **G2 后端内部增强**：第一版不新增前端 API，不改变 SSE、artifact、interrupt、resume 的协议字段。
- **G3 Workbench 内部 capability**：新增 `workbench.*` 作为 `public=False` 的后端内部 capability，Planner、用户和 public capability API 不可见。
- **G4 Contract / policy 驱动**：Workbench 启用规则必须来自平台策略与 Skill contract 的通用字段，例如 execution mode、answer mode、input schema、output contract、artifact policy、resource policy、quality policy；不得根据具体 Skill 名称定制。
- **G5 受控预算**：只有被平台策略明确允许的 Skill plan 才可开启有限预算；普通 Skill 默认保持现有一次性行为。
- **G6 Finalizer 可消费但不强制**：Workbench output 必须形成短、结构化、脱敏的 digest；只有当 Skill 的 answer mode 或平台策略需要 finalizer 时，该 digest 才进入 `main_agent.respond` 的 dependency context。
- **G7 平台侧测试优先**：具体 Skill 测试集属于 Skill 资源维护者；平台侧重点补 Skill 准入契约、consumer contract tests、capability health / diagnostics。

## 3. 非目标

- 不把 `WorkflowPlan` 改成循环图或持久化 state graph。
- 不新增前端页面、前端 API、artifact 下载协议、interrupt/resume 协议或 SSE event schema。
- 不让 LLM Planner 直接看到、选择或生成 `workbench.*`。
- 不把具体业务算法或领域处理逻辑塞进主框架；Workbench 只做平台层画像、校验、摘要和验证。
- 不替代各 Skill 自己的业务测试；平台只验证 Skill 与主框架之间的 consumer contract。
- 不要求所有 Skill 立即修改 contract；MVP 可先由平台静态策略表驱动，contract 扩展放到后续阶段。

## 4. 干系人与受影响系统

| 对象 | 关注点 |
| --- | --- |
| 终端用户 | 不需要理解 Workbench；最终回答和下载体验不因内部节点泄漏或重复回答而变差。 |
| Skill 维护者 | 通过通用 contract / policy 声明输入、输出、artifact、answer mode 和质量需求，不被平台要求适配某个硬编码 Skill 名称。 |
| 主代理 / Planner | 只看 public capability，不看内部 `workbench.*`；最终回答只消费安全 digest。 |
| 编排与运行时 | 保持 DAG、预算、runtime replan、interrupt/resume、artifact 语义一致。 |
| 前端 / API 消费方 | 协议字段不变；内部节点在 SSE / graph / history 中必须被泛化、脱敏或隐藏。 |
| 运维 / 审计 | 能看到内部诊断和拒绝原因，但 audit 不记录敏感 payload 或原始文件内容。 |

## 5. 当前代码基线

| 代码位置 | 当前事实 | 对本 PRD 的影响 |
| --- | --- | --- |
| `src/orchestration/models.py` | `WorkflowPlan` 只有 nodes / metadata / replan budgets。 | 继续用版本化 DAG 表达运行闭环，不改 plan schema。 |
| `src/orchestration/workflow_plan_validator.py` | 校验 capability、payload JSON、依赖存在和无环。 | `workbench.*` 必须注册到 `CapabilityRegistry`，并保持 DAG 合法。 |
| `src/orchestration/skill_workflow_provider.py` | `skill.*` 可展开为 Skill node + finalizer；当前预算为 0。 | 最适合承接 MVP 初始插入和平台预算策略。 |
| `src/orchestration/workflow_expander.py` | 展开 macro、补全全局 finalizer、继承 macro budgets。 | 需要保证 Workbench 链路不破坏已有 finalizer 规则。 |
| `src/orchestration/service.py` | 运行时 replan 不允许改写已存在节点的 capability 或 dependencies；新增节点受 `max_dynamic_nodes` 限制。 | Phase 2 不能简单把已创建 finalizer 的依赖改到新 verifier 上，必须新增 finalizer 或初始延迟创建。 |
| `src/capabilities/main_agent/runtime_replanner.py` | LLM replanner 只能输出 public capability DAG，且 sanitize node outputs。 | Workbench replanner 应是 deterministic 内部 replanner，放在 LLM replanner 前。 |
| `src/capabilities/main_agent/prompt_builder.py` | finalizer 可读取 dependency outputs，并有 allowlist / 敏感字段过滤。 | Workbench output 应复用 `summary`、`highlights`、`caveats`、`structured_content`、`satisfaction` 等通用字段。 |
| `src/integrations/agent_skills/skill_capabilities.py` | Skill 只有位于 public root、contract 合法、capability id 合法且不重复时才注册。 | Phase 3 可把 Workbench 策略纳入 Skill 准入诊断。 |
| `src/api/routes/tasks.py` | task graph API 当前返回 node id 与 capability id。 | 内部节点不仅要处理 SSE，也要处理 graph response 的展示脱敏。 |

## 6. 总体设计原则

1. **Plan 仍是 DAG**：运行闭环由“新一版 DAG + 预算 + replan decision”表达，不在单个 plan 内引入环。
2. **Public / internal 分层**：Planner 和用户只看到 `skill.*` / `main_agent.respond`；`workbench.*` 只由后端 expander / deterministic replanner 注入。
3. **Contract / policy 优先**：Workbench 不按 Skill 名称分支；只按 contract 字段、platform policy、execution mode、answer mode、output contract、artifact policy、resource policy 和显式 quality policy 决定 stage。
4. **先确定性，后智能化**：MVP 先用平台策略和固定链路验证；Phase 2 再基于 node output 逐步追加。
5. **Digest 优先**：Workbench 不把原始文件、完整 rows、SQL、schema DDL、handler、runtime、路径或 secret 放进 prompt / SSE / audit 可见 payload。
6. **Finalizer 尊重 answer mode**：`answer_mode=direct` 的 Skill 不得因为 Workbench 而重复追加主代理回答节点；只有 `requires_finalizer`、`none + finalizer policy` 或显式策略要求时，Workbench digest 才进入 finalizer。
7. **Fail closed**：策略不匹配、输出 contract 缺失、digest 超限、敏感字段检测失败时，Workbench 节点应失败或降级为 audit-only caveat，不能静默放行并声称验证通过。

## 7. Workbench Capability 设计

### 7.1 Capability 列表

| Capability | 作用 | 输入 | 输出摘要 |
| --- | --- | --- | --- |
| `workbench.data_profile` | 对 Skill 输入、上传 artifact metadata 或上游输出摘要做轻量画像。 | artifact metadata、input schema 摘要、上游 output 摘要。 | 数据规模、字段候选、文件/文本类型、缺失摘要。 |
| `workbench.schema_match` | 判断输入或输出是否匹配 selected input schema / output contract。 | schema id、contract 摘要、data profile。 | matched/missing/ambiguous 字段、置信度、需补信息。 |
| `workbench.preflight_validate` | 按通用 quality policy 做执行前可用性检查。 | selected schema、input digest、platform policy。 | 可执行性、阻断原因、非阻断 warning。 |
| `workbench.domain_validate` | 按 Skill contract 暴露的 domain/quality policy 做领域边界校验。 | output digest、domain policy、request intent 摘要。 | domain warnings、blocking errors、业务边界说明。 |
| `workbench.artifact_inspect` | 检查 Skill 产物是否存在、类型是否符合 output contract、是否可被前端下载。 | output files / artifacts metadata、output contract。 | artifact 完整性、缺失项、文件摘要。 |
| `workbench.report_verify` | 验证 Skill 输出的用户可见报告或最终 digest 是否覆盖 contract 要求的关键事实和风险。 | Skill output digest、artifact inspect、domain validate。 | report completeness、finalizer highlights/caveats。 |

### 7.2 Descriptor 要求

所有 `workbench.*` descriptor 必须满足：

- `public=False`
- `kind="workbench"`
- `source="builtin"`
- 不注册 planner payload policy
- 本地 internal instance 支持执行
- 默认不出现在 `/api/v1/capabilities` public 列表

### 7.3 输出契约

`WorkbenchOutputContractV1` required 字段：

```json
{
  "schema_version": "workbench.output.v1",
  "workbench_kind": "data_profile | schema_match | preflight_validate | domain_validate | artifact_inspect | report_verify",
  "target_capability_id": "skill.<id>",
  "target_node_id": "task:skill_execute",
  "summary": "短摘要，适合 finalizer 或诊断使用",
  "satisfaction": {
    "satisfied": true,
    "reason_code": "verified | warning | missing_required_output | artifact_missing | domain_boundary",
    "replan_recommended": false
  }
}
```

optional 字段：

```json
{
  "highlights": ["可以安全进入最终回答的事实"],
  "caveats": ["需要最终回答或诊断提醒的边界或风险"],
  "structured_content": {
    "safe_digest": {},
    "blocking": false,
    "confidence": "low | medium | high"
  }
}
```

禁止字段：

- `sql`、`schema_ddl`、`guard_pass_token`
- `raw_output`、完整 `rows`、完整文件内容
- `storage_ref`、`storage_key`、`path`、`file_path`、`mount_path`
- `handler`、`runtime`、`entrypoint`、`script`
- `token`、`secret`、`password`、`api_key`、`authorization`

## 8. 平台策略模型

MVP 采用平台静态策略表，策略 key 不得是具体 Skill 名称，必须是 contract / capability descriptor / execution config 可推导的通用属性。

建议策略输入：

| 策略输入 | 来源 | 用途 |
| --- | --- | --- |
| `execution_mode` | `runtime.mode` 或 resolved execution config | 区分 `python_subprocess`、`platform_service`、`delegated_main_agent`。 |
| `answer_mode` | `runtime.answer_mode` 或 entrypoint answer mode | 决定是否允许 finalizer 消费 Workbench digest。 |
| `input_schema_count` / `schema_selector` | `input_schemas` / `schema_selector` | 决定是否需要 `schema_match`。 |
| `output_required_fields` | `outputs.*.required` | 决定 output contract verify。 |
| `output_artifact_policy` | `outputs.*.artifacts` | 决定是否需要 `artifact_inspect`。 |
| `resource_policy` | `resource_policy` | 控制 Workbench 不读取越界资源。 |
| `quality_workbench` | Phase 3 contract 可选字段 | 精确声明 stage、预算、digest 需求。 |

策略输出：

```yaml
workbench_policy:
  enabled: true
  stages:
    - schema_match
    - artifact_inspect
    - report_verify
  finalizer_digest_mode: none | when_finalizer_exists | required
  max_replans: 0
  max_dynamic_nodes: 0
  event_visibility: masked_frontend | audit_only
```

默认规则：

- 未命中平台策略的 Skill 不启用 Workbench。
- `answer_mode=direct` 默认 `finalizer_digest_mode=none`，Workbench 只做 audit / health，不追加主代理 finalizer。
- `answer_mode=requires_finalizer` 默认 `finalizer_digest_mode=when_finalizer_exists`，Workbench digest 可进入已有 finalizer。
- `answer_mode=none` 只有在策略显式声明 `finalizer_digest_mode=required` 时才新增 finalizer。
- runtime replan 预算必须在 initial plan 阶段确定；后续 revised plan 不得提升预算。

## 9. Initial Expansion 与 Runtime Replanner 取舍

### 9.1 MVP：Initial Expansion 插入固定 Workbench DAG

推荐第一阶段在 `SkillWorkflowProvider` 或其附近的 policy helper 中完成：

1. `skill.*` macro 展开为 Skill execute node。
2. 根据 `WorkbenchPolicy` 在 Skill node 前后插入固定 Workbench nodes。
3. 对已有 finalizer 的 Skill，把 finalizer 依赖连到最后一个 Workbench verifier，而不是直接依赖 Skill node。
4. 对 `answer_mode=direct` 的 Skill，只追加 audit/health 型 Workbench 节点，不新增 finalizer。
5. 对 `answer_mode=none` 的 Skill，只有策略显式要求 finalizer 时才新增 task-level finalizer。

优点：

- 图结构确定，便于测试和审计。
- 不消耗 runtime replan budget。
- 避免运行时重排改写已创建 finalizer 依赖被拒。
- 第一版实现面小，不触碰 public API。

缺点：

- 可能执行不必要的 Workbench stage。
- 无法根据 Skill output 动态选择下一步。
- 对长耗时 Skill 可能增加固定尾部延迟。

### 9.2 Phase 2：RuntimeReplanner 逐步追加

在 `CompositeRuntimeReplanner` 中新增 `WorkbenchRuntimeReplanner`，顺序放在 `MainAgentRuntimeReplanner` 前：

1. 观察完成的 `skill.*` 或 `workbench.*` output。
2. 读取 `WorkbenchPolicy` 和已执行 stage。
3. 如果仍需检查，追加下一批 `workbench.*` nodes。
4. 必要时追加新的 finalizer，或 orphan 旧的 pending finalizer；不得原地修改已存在节点依赖。
5. 不得提高 `max_replans` 或 `max_dynamic_nodes`；预算只能来自 initial plan。

优点：

- 更贴近运行闭环，按输出事实决定下一步。
- 可减少不必要检查。
- 可在 Skill output 表示 `satisfaction.replan_recommended` 时补足验证。

缺点：

- 需要严格管理预算和 node id。
- finalizer 依赖处理更复杂。
- `task.graph_updated` / `node.started` / task graph API 等输出需要更强内部节点脱敏。

### 9.3 不推荐：让 LLM Replanner 规划 Workbench

不让 `MainAgentRuntimeReplanner` 生成 `workbench.*`：

- 它的设计边界是 public-only revised DAG。
- Workbench 是内部平台阶段，不应进入 public capability prompt。
- LLM 输出内部节点会扩大 prompt 注入、能力泄漏和不可测试风险。

## 10. 模块改动边界

### 10.1 `SkillWorkflowProvider`

MVP 主要入口：

- 解析 Skill capability id / manifest / answer mode 后，查询 `WorkbenchPolicy`。
- 给命中策略的 Skill plan 设置受控预算。
- 插入固定 Workbench nodes。
- 把 Workbench strategy 写入 plan metadata，便于测试和审计。

### 10.2 `WorkflowExpander`

需要保持：

- public `skill.*` macro 展开后仍是内部 DAG。
- 多 Skill finalizer 只产生一个 task-level finalizer。
- Workbench node 不被误当作 public Skill dependency 丢弃。

### 10.3 `RuntimeReplanner`

Phase 2 新增 deterministic replanner：

- 只读取 `RuntimeReplanContext` 中的安全 output。
- 只追加 `workbench.*` 或必要 finalizer。
- 不调用 LLM。
- 不改写已存在节点 capability / dependencies。
- 不提高 initial plan 预算。

### 10.4 `CapabilityRegistry` / `InstanceRegistry`

注册 Workbench descriptors 和本地执行实例：

- descriptor `public=False`
- instance 只支持 `workbench.*`
- 不进入 planner payload policy

### 10.5 `CapabilityExecutor`

新增 `WorkbenchExecutor`：

- 只实现平台层 digest / validation。
- 返回 `CapabilityExecutionResult.output_payload`。
- 默认不生成用户可下载 artifact。
- 事件默认 audit-only；必要的 frontend 事件使用泛化文案。

### 10.6 `main_agent` prompt / finalizer

复用现有 dependency context：

- allowlist 增加 Workbench digest 字段时必须同步敏感字段过滤。
- finalizer prompt 增加规则：Workbench digest 是验证事实和风险提示，不是用户可见执行步骤。
- direct Skill 的 Workbench digest 默认不得触发第二个主代理回答。

### 10.7 Skill contract / profile

MVP 不要求修改 Skill contract。

Phase 3 可增加可选字段：

```yaml
quality_workbench:
  enabled: true
  domain_kind: generic
  stages: [schema_match, artifact_inspect, report_verify]
  finalizer_digest_mode: when_finalizer_exists
  max_replans: 1
  max_dynamic_nodes: 3
```

## 11. 前端与 API 兼容边界

本专题不新增 API，也不改变 DTO / SSE schema。但是需要保证内部节点不泄漏实现细节。

MVP 接受的行为：

- 前端仍收到标准 task/node 事件。
- 内部 Workbench 节点可表现为泛化的“结果校验中 / 产物检查中”，但不展示 handler、runtime、路径、SQL、schema DDL。
- artifact 列表不新增 Workbench 内部 JSON artifact。

必须实现的后端脱敏：

- `node.started` payload 对 `metadata.internal_node=True` 的节点隐藏真实 `capability_id` 或映射为泛化 `internal.validation`。
- `task.graph_updated` 的 `added_node_ids` 如包含内部节点，应保持协议字段但避免暴露语义化内部实现名称；可以使用稳定 opaque node id 或 audit-only 详细映射。
- `GET /api/v1/tasks/{task_id}/graph` 必须对内部节点做隐藏、泛化或 opaque id 映射；不得直接返回 `workbench.*` capability id 给普通前端视图。
- task summary / history artifact 只展示最终回答和 Skill output file，不展示 Workbench digest artifact。
- 审计侧可保留真实内部 node id / capability id，但必须继续执行敏感字段脱敏。

## 12. 非功能要求

| 维度 | 要求 |
| --- | --- |
| 性能 | Workbench 节点默认轻量执行；单节点必须有 timeout policy；MVP 不读取完整文件和完整 rows。 |
| 资源 | Workbench output digest 必须有大小上限；进入 finalizer dependency context 前必须再次经过 allowlist 和敏感字段过滤。 |
| 安全 | 内部 capability、node id、handler、runtime、路径、SQL、schema DDL、secret 不得进入 public prompt、frontend event、graph API 或 history artifact。 |
| 兼容 | 不改变 API/SSE schema；只允许在既有字段内泛化、隐藏或脱敏内部节点。 |
| 可观测性 | 内部真实节点、stage、拒绝原因和 budget 消耗必须可审计，但 audit payload 不记录原始敏感内容。 |
| 可回滚 | MVP 必须受 feature flag 控制；关闭后 Skill plan 回到现有一次性行为。 |

建议 feature flag：

```yaml
workbench:
  enabled: false
  rollout_scope: disabled | audit_only | fixed_dag | runtime_replan
```

## 13. 分阶段交付

### 13.1 MVP：固定 Workbench DAG 与安全 digest

改动模块：

- `src/capabilities/workbench/`
- `src/orchestration/skill_workflow_provider.py`
- `src/orchestration/workflow_expander.py`
- `src/api/runtime.py`
- `src/capabilities/main_agent/prompt_builder.py`
- `src/api/routes/tasks.py`
- `tests/orchestration/`
- `tests/capabilities/workbench/`
- `tests/api/`

关键数据结构：

- `WorkbenchPolicy`
- `WorkbenchStage`
- `WorkbenchOutputContractV1`
- node metadata：`internal_node`、`workbench_stage`、`target_skill_node_id`、`target_capability_id`

验收标准：

- `workbench.*` 不出现在 public capability list 和 planner prompt。
- Workbench policy 按 contract / policy 属性命中，不按 Skill 名称命中。
- `answer_mode=direct` 不新增重复主代理 finalizer。
- `answer_mode=requires_finalizer` 的 finalizer 可以消费 Workbench digest。
- finalizer 的 dependency context 包含 Workbench safe digest，且不包含禁止字段。
- SSE 和 graph API 不泄漏内部 capability id、handler、runtime、路径、SQL、schema DDL 或 storage ref。
- interrupt/resume、artifact 下载、SkillExecutor 现有回归不退化。

主要风险：

- 固定链路可能增加耗时。
- finalizer 规则与多 Skill 汇总容易重复或漏依赖。
- 内部 node id / capability id 可能通过现有事件或 graph API 泄漏，需要配套脱敏。

### 13.2 Phase 2：确定性 Runtime Workbench Loop

改动模块：

- `src/orchestration/workbench_replanner.py`
- `src/orchestration/runtime_replanner.py`
- `src/orchestration/service.py` 的内部事件脱敏辅助
- `tests/orchestration/test_workbench_replanner.py`

关键数据结构：

- `WorkbenchReplanState`
- `WorkbenchStageDecision`
- `WorkbenchBudget`

验收标准：

- Skill output 完成后，`WorkbenchRuntimeReplanner` 能追加下一批内部节点。
- 不修改已存在节点 dependencies。
- 不提高 initial plan 预算。
- 超过 `max_replans` 或 `max_dynamic_nodes` 时 fail closed 并记录审计。
- LLM Runtime Replanner 仍只能输出 public DAG。
- pending finalizer 不会提前消费未验证 Skill output。

主要风险：

- 运行时图更新会增加状态机复杂度。
- old finalizer orphan / new finalizer append 需要明确 task history 语义。
- 预算不足时用户可能看到未验证但已完成的 direct answer，需要策略约束。

### 13.3 Phase 3：Skill Contract 准入与健康诊断

改动模块：

- `skill.contract.yaml` 可选字段扩展
- `src/integrations/agent_skills/skill_capabilities.py`
- Skill runtime diagnostics / health
- 文档与 Skill 构建指南

关键数据结构：

- `quality_workbench` contract section
- `SkillCapabilityDiagnostic` 新增 workbench 策略诊断原因
- capability health payload

验收标准：

- Workbench 策略优先来自 Skill contract，静态表只作为兼容 fallback。
- contract invalid 的 Workbench 策略不影响内置 capability，但该 Skill 产生诊断并按策略 fail closed 或降级。
- 平台 consumer contract tests 不依赖真实 Skill 测试集。
- Skill 维护者能通过文档知道如何声明 workbench stages 和 output digest。

主要风险：

- Contract 扩展会增加 Skill 维护者负担。
- 不同 Skill 的成熟度不同，过早强制可能导致现有 Skill 无法注册。
- health diagnostics 若变成 public API，会扩大范围；建议先 audit/internal。

## 14. 最小 Consumer Contract Tests

平台侧至少需要以下测试：

1. **Capability 可见性**：`workbench.*` 注册为 `public=False`，`CapabilityRegistry.list(public_only=True)` 不返回。
2. **Planner 隔离**：LLM planner prompt 不包含 `workbench.*`。
3. **策略来源**：Workbench policy 按 contract / policy 属性命中，不按具体 Skill 名称命中。
4. **Answer mode**：`direct` 不追加重复 finalizer；`requires_finalizer` 可把 verifier digest 接入已有 finalizer。
5. **Output 契约**：Workbench output 缺 required 字段时失败；包含敏感字段时失败或剔除。
6. **Prompt 消费**：`main_agent.respond` dependency context 包含 safe digest，不包含禁止字段。
7. **Runtime 预算**：Phase 2 追加节点受 initial `max_replans/max_dynamic_nodes` 限制。
8. **No mutation**：Phase 2 不改写已有 node dependencies，改图只能新增或 orphan pending node。
9. **Event / graph 脱敏**：内部节点的 frontend 事件和 task graph response 不暴露 `workbench.*`、path、handler、runtime、SQL、schema DDL、storage_ref。
10. **Interrupt / Resume**：resume 后不会重复执行已完成 Workbench stage。
11. **Artifact 边界**：Workbench 不创建前端可展示 artifact；Skill output file 展示链路保持不变。
12. **Health / diagnostics**：Phase 3 contract 中非法 workbench stage 能产生诊断。

## 15. 关键验收场景

### 15.1 Requires-finalizer Skill

1. Planner 或 soft binding 选择某个 `answer_mode=requires_finalizer` 的 `skill.*`。
2. 系统按通用 policy 展开为 Skill execute + Workbench verifier + finalizer。
3. Workbench digest 进入 finalizer dependency context。
4. 最终回答综合 Skill output 和 Workbench safe digest，不暴露内部节点。

### 15.2 Direct-answer Skill

1. Planner 或 soft binding 选择某个 `answer_mode=direct` 的 `skill.*`。
2. 系统可以按 policy 执行 audit/health 型 Workbench。
3. 系统不得追加第二个 `main_agent.respond` 最终回答节点。
4. Workbench 诊断进入 audit / health，不改变用户可见回答，除非 contract 明确改为需要 finalizer。

### 15.3 Artifact-producing Skill

1. Skill output contract 声明 artifact policy。
2. Workbench 通过 metadata 检查 artifact 是否存在、类型是否符合 contract、是否可下载。
3. Workbench 不读取文件原文，不创建前端可展示 artifact。
4. Finalizer 如存在，只能提示用户使用前端下载卡片，不编造下载链接。

### 15.4 Structured-output Skill

1. Skill 输出脱敏结构化摘要、count、satisfaction 或等价安全 digest。
2. Workbench 只消费通用摘要字段，不读取原始查询、schema 细节、字段全集、完整 rows 或等价敏感明细。
3. Finalizer 只消费安全摘要和 capped sample。

## 16. 风险与控制

| 风险 | 控制 |
| --- | --- |
| 内部 capability 泄漏到前端 | `public=False` 之外增加 SSE 和 graph API masking；详细内部信息 audit-only。 |
| Workbench 变成业务算法实现 | 明确 Workbench 只做平台画像/校验/digest，业务逻辑仍在 Skill。 |
| 固定 DAG 增加延迟 | MVP stage 控制在轻量、可跳过；Phase 2 动态化。 |
| Direct Skill 被重复 finalizer | 默认 `direct` 不接 finalizer；需要 finalizer 必须显式 contract/policy 决策。 |
| Runtime replan 提前/重复 finalizer | MVP 初始依赖连好；Phase 2 使用新增 finalizer 或 orphan pending finalizer，不改已有依赖。 |
| Skill contract 扩展影响注册 | Phase 3 先 optional + diagnostics，成熟后再变成准入要求。 |
| 本地没有外部 Skill 测试集 | 平台 consumer contract tests 使用 fake Skill output 和 mock artifacts。 |
| Prompt 泄漏敏感 output | Workbench output schema 禁止敏感字段；finalizer dependency context allowlist 和敏感词过滤双重控制。 |

## 17. Rollout 建议

1. 默认 feature flag 关闭。
2. 先启用 `audit_only`，只记录 Workbench policy decision 和 would-run stages，不改 DAG。
3. 再启用 `fixed_dag`，只覆盖 contract / policy 明确命中的 Skill 类型。
4. 通过平台 consumer contract tests 和真实手工 smoke 验证 digest 是否改善最终回答或诊断质量。
5. 稳定后引入 `runtime_replan`。
6. 最后把策略迁移进 `skill.contract.yaml` 可选字段，并补 Skill 构建指南。

## 18. 后续拆分建议

本总纲后续建议拆成三份实施 PRD：

- `22-01-Workbench内部Capability与固定DAG插入PRD.md`
- `22-02-WorkbenchRuntimeReplanner与预算闭环PRD.md`
- `22-03-SkillContract质量工作台策略与健康诊断PRD.md`

拆分前，本文件作为总体方向、跨阶段不变量和验收矩阵的事实源；阶段 PRD 不得放宽本总纲的 public/internal、安全、answer mode、预算和脱敏边界。
