# Phase 2：Invocation Kernel 与 Skill/MCP 适配 PRD

- **日期**：2026-08-22
- **状态**：in_progress（P2-A green；下一步P2-B）
- **文档审阅**：document-perfectization第二次全量审计100/100通过；P2-A实现证据已闭合
- **父总纲**：`00-统一同模型AgentLoop总纲PRD.md`
- **上游**：Phase 0、Phase 1必须`proof_complete`
- **主责需求**：FR-13、FR-15、FR-16、FR-20、FR-24
- **主责NFR**：安全与隐私、Tool catalog容量
- **直接参与者**：Orchestration/Capability维护者、Skill作者、MCP集成方、Prompt/LLM Runtime与安全审查者
- **目标结果**：形成唯一、安全、可恢复的一次Capability调用内核，并把现有Skill与MCP映射为Agent Tool；不拥有循环继续/停止权。

## 1. 目标与价值

Agent Loop不应复制当前`OrchestrationService`中的实例选择、TaskNode、Artifact、Event、Interrupt、取消和late-result
逻辑，也不应直接依赖具体Skill/MCP executor。本阶段提取`CapabilityInvocationService`，让旧DAG和未来Agent Loop
在pre-cutover期间复用同一个“一次调用”生命周期。

同时，本阶段固定模型可见Tool Catalog、可信system payload、Skill执行模式和MCP内部模型绑定，防止模型输出绕过
owner/server/bundle/credential权限。

## 2. 进入条件

- Phase 0提供AgentToolDescriptor、provider-safe name映射和AgentModelBinding；
- Phase 1提供AgentRun/Item、TaskNode账本、atomic outcome API和LeaseController；
- 当前CapabilityRegistry、CompositeExecutor、Skill runtime和MCP Coordinator/Router/Selector可复用；
- 旧DAG执行基线测试已锁定。

### 2.1 当前证据与受影响模块

| 锚点 | 当前事实 | 本阶段影响 |
|---|---|---|
| `src/orchestration/service.py` | 同时承担DAG调度和一次调用生命周期 | 提取唯一Invocation Kernel，旧DAG行为保持接入 |
| `src/orchestration/registry.py`、`composite_executor.py` | 已提供请求可见能力与executor聚合 | 作为Catalog/Kernel复用入口，不复制registry/executor |
| `src/integrations/agent_skills/public_profile.py` | 已有净化PublicSkillProfile | Delegated activation只消费该安全投影 |
| `src/capabilities/skill_tool/executor.py` | 明确拒绝delegated_main_agent | 新ActivationService承接delegated，executable仍走SkillExecutor |
| `src/capabilities/mcp_dispatch/server_router.py`、`selector.py` | 只接收text generator | 注入Run-bound model binding，保留内部严格schema |
| `tests/capabilities/`、`tests/integrations/agent_skills/`、`tests/integrations/mcp/` | 已有Skill/MCP安全和恢复基线 | 证明公共Kernel行为保持及Agent适配边界 |

## 3. 范围与非范围

### 3.1 范围内

- `CapabilityInvocationService`单一生命周期；
- `AgentToolCatalogBuilder`和`CapabilityInvocationPolicy`；
- model字段allowlist、JSON schema、system payload覆盖、parallel/can-suspend声明；
- Tool catalog完整预算preflight；
- delegated/executable Skill适配；
- Run-bound MCP Router/Selector binding；
- 复用MCP discovery、authorization、approval/MRTR/Tasks、Result Parser和no-replay；
- 旧DAG service行为保持地调用公共Kernel。

### 3.2 非范围

- 不决定调用后是否再次采样；
- 不实现multi-call wave调度；
- 不发布最终回答；
- 不完成Agent waiting resume；
- 不改变MCP transport、Server内Tool discovery、Endpoint Policy、Result Parser或call budget；
- 不读取delegated Skill manifest/resource正文；
- 不建立用户可选择的Agent入口或dual runtime。

## 4. Invocation Kernel合同

Kernel输入至少包含：Run/call identity、当前claim/revision引用、capability ID、policy过滤后的model payload、可信system
payload、assigned instance、cancellation token和安全model binding引用。

Kernel唯一负责：

1. selected route/capability authority校验；
2. instance selection；
3. TaskNode start CAS；
4. `CapabilityExecutionRequest`构造；
5. CompositeExecutor调用；
6. staged Artifact与Event处理；
7. Interrupt、missing input、remote pending映射；
8. ordinary/fatal/cancel/late-result分类；
9. 调用Phase 1原子outcome API。

AgentLoop、旧DAG service和API runtime都不得复制上述生命周期。

## 5. Tool Catalog与Policy

`AgentToolCatalogBuilder`只消费`CapabilityRegistry.list_for_request(..., public_only=True)`等价安全视图，继续遵守
owner、public/enabled、execution path、Skill pinned revision和用户MCP Server Profile可见性。

`CapabilityInvocationPolicy`至少包含：

```text
model_allowed_fields
input_schema
system_payload_factory
parallel_safe
can_suspend
```

无policy的Capability不进入catalog。执行前再次过滤/校验；system字段覆盖模型同名值。初始所有现有Capability默认
`parallel_safe=false`，只有明确contract和隔离回归后才可调整。

## 6. Catalog预算门禁

- 当前请求全部可见native Tool schemas计入PromptEnvelope non-history预算；
- 不裁剪Tool name、input schema、authority enum或必保system规则；
- 本阶段Preflight只计算并返回closed decision，不执行compaction：
  - `fits`：当前context与完整catalog可采样；
  - `history_compaction_required`：必保non-history可容纳、存在可压缩的covered range，且当前history使总输入超限；
  - `fatal_required_segments_too_large`：稳定规则、完整catalog、当前用户输入和最小suffix本身已超限；
- 没有eligible compactable range时不得返回`history_compaction_required`，直接返回
  `fatal_required_segments_too_large`；
- Phase 3收到`history_compaction_required`后使用同模型压缩history并重新调用Preflight；压缩后仍不适配且没有新的
  eligible range时才以`agent_tool_catalog_too_large` fatal；Phase 2不得越权生成summary或删减history；
- 记录tool count、schema bytes和token estimate，不记录schema正文；
- 不实现lazy capability discovery；
- Outer catalog只含public Skills和一个`mcp.dispatch`，不展开Server Tool list。

## 7. Skill适配

### 7.1 Delegated Skill

`delegated_main_agent`路由到`DelegatedSkillActivationService`，不进入`SkillExecutor`：

- 按AgentRun pinned bundle revision解析；
- 只从`PublicSkillProfile.to_dict()`安全字段生成activation；
- resource index只保留公开ID/title/description/audience，不加载正文；
- 持久化`skill_activation` item与projection digest；
- activation仅当前Run有效，不能由tool result或用户文本伪造；
- 即使manifest含scripts也不得执行；
- `SkillManifest.body`、resource正文、内部path/config/secret不进入上下文。

### 7.2 Executable Skill

`python_subprocess`/`platform_service`继续由SkillExecutor执行。模型只可提供policy允许字段；effective user message、
artifact context、bundle revision、用户身份由system注入。`direct/requires_finalizer/none`都返回tool result，不创建
独立DAG finalizer；answer mode只控制output projection/回答资格提示。

## 8. MCP适配

- Outer Agent只调用`mcp.dispatch`；
- 自动模式`server_id` schema只含当前安全Profile IDs；显式server ID由system注入且模型不可覆盖；
- Router/Selector每次从AgentRun解析同一`AgentModelBinding`，不得选择其他edition；
- Selector继续使用严格内部action schema和现有repair/budget；
- discovery、credentials、authorization、pending action、MRTR/Tasks、durable result和Result Parser不改；
- raw result和完整Server Tool list不进入AgentItems。

## 9. 功能需求与验收

| ID | Requirement | Acceptance |
|---|---|---|
| AL-P2-01 | 所有调用生命周期只有一个Kernel实现。 | 静态/spy tests证明Agent与旧DAG不复制executor lifecycle。 |
| AL-P2-02 | Model字段allowlist与system字段覆盖fail closed。 | Prompt injection不能覆盖owner/server/revision/artifact context。 |
| AL-P2-03 | Catalog只含请求可见且有policy的Capability。 | 跨owner、disabled/private、无policy项不出现。 |
| AL-P2-04 | 完整catalog Preflight返回closed、可执行的预算decision。 | 覆盖fits、需要history compaction、必保segment直接fatal、热重载后变化及no-schema-leak。 |
| AL-P2-05 | Delegated Skill产生安全activation且不执行脚本。 | Projection/digest/pinned revision及manifest body拒绝tests。 |
| AL-P2-06 | Executable Skill可信上下文不可被模型覆盖。 | 三answer mode和input/artifact/revision tests。 |
| AL-P2-07 | MCP Router/Selector使用Run固定edition。 | ordinary、approval和remote恢复binding identity相同。 |
| AL-P2-08 | MCP现有安全路径不退化。 | discovery、authorization、budget、Result Parser、no-replay完整回归。 |
| AL-P2-09 | 旧DAG可行为保持地调用公共Kernel。 | 用户可见Task/Node/Artifact/Event/Interrupt结果与基线一致。 |

## 10. 失败模式

- capability name合法但不在catalog：提交failed `unknown_tool`，不调用executor；
- 参数schema/allowlist失败：failed tool result，可由模型纠正；
- system authority缺失或损坏：fatal，不使用模型值兜底；
- public catalog必保segments超预算：采样前fatal；只有history导致超限时返回typed compaction request，不裁剪Tool；
- delegated activation pinned revision不存在：failed/fatal按authority完整性分类，不读取当前热重载bundle猜测；
- MCP unavailable：返回安全ordinary result；用户MCP不得回退legacy global MCP；
- capability产生waiting：原子提交authority并交给Phase 4恢复，不生成terminal result。

### 10.1 跨阶段NFR协作

| NFR | 本阶段责任 | 后续复验 |
|---|---|---|
| 安全与隐私 | 主责：可见性、参数/system authority、Skill/MCP projection | Phase 3～7执行上下文/event/history leak scan |
| Tool catalog容量 | 主责：完整schema和closed Preflight decision | Phase 3执行compaction/re-preflight，Phase 6/7做全入口回归 |
| Provider同模型 | MCP Router/Selector只消费Phase 0 binding | Phase 4恢复与Phase 6 assembly验证edition不变 |
| 恢复/no-replay | Kernel保留capability authority和late-result边界 | Phase 4验证continuation/aborted/no-replay |
| 可维护性 | 唯一Invocation lifecycle，旧DAG行为保持复用 | Phase 6静态证明无第二实现 |

## 11. 测试计划

最低域：

```bash
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/main_agent -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/mcp_dispatch -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/mcp_tool -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/integrations/agent_skills -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/integrations/mcp -p 'test_*.py'
```

阶段实施计划可按改动先跑聚焦模块，但退出时上述受影响域和旧DAG behavior-preservation回归必须通过。真实MCP smoke
保留为Phase 7门禁，本阶段用fake/隔离integration证明协议和安全边界。

每条discover命令必须发现非零测试；`Ran 0 tests`即失败。

### 11.1 P2-A实施证据

- 新增`CapabilityInvocationService`、`InvocationRequest/Result`和语义化`InvocationCommitPort`，不依赖
  `WorkflowNodePlan`或`OrchestrationRequest`；Run/call/revision/claim/model binding/cancellation引用已预留为Agent输入合同；
- route authority、instance selection、Node start、执行metadata authority、唯一`CapabilityExecutionRequest`构造、唯一executor
  调用以及completed/failed/waiting/remote pending/late-result分类集中在Kernel；静态扫描确认Orchestration范围只有该处调用；
- `LegacyDAGInvocationCommitPort`只实现现有TaskNode、Artifact、Event、Interrupt、slot collection、remote binding持久化投影，
  旧`OrchestrationService._execute_node`降为DTO映射和Kernel委托；Agent fixture仅注入Phase 1 atomic writer提交outcome；
- P2-A聚焦20项、`tests/orchestration` discover 187项、compileall和diff检查通过；旧DAG scheduler、Planner/Replanner、API入口
  及真实route均未改变。P2-A green，Phase 2继续`in_progress`进入P2-B。

## 12. 风险、假设与开放问题

| 风险 | 缓解/阻断条件 |
|---|---|
| 旧DAG接公共Kernel时行为漂移 | 前后Task/Node/Artifact/Event/Interrupt golden与调用spy一致，否则回滚抽取 |
| 模型字段覆盖owner/server/revision | Policy双重过滤和system override tests，缺authority fatal |
| Delegated activation泄漏manifest/resource正文 | 只消费PublicSkillProfile并做path/secret/body leak scan |
| Catalog预算职责跨到Phase 3 | 本阶段只返回closed decision；summary生成测试必须为零 |
| MCP binding在approval/remote恢复后换edition | Locator保存Run binding引用，Phase 4重复identity test |

已确认假设：PublicSkillProfile足以作为本阶段delegated安全activation；现有MCP authority/Result Parser可通过Kernel复用。
开放问题：无。

## 13. Git检查点与回滚

- 旧DAG调用公共Kernel属于行为保持重构，必须在独立检查点锁定前后行为；
- 新Agent Tool适配只通过test assembly调用，不增加runtime route/feature flag；
- 回滚恢复旧DAG私有生命周期代码和删除未接流量Agent adapter；
- 不删除Planner、Replanner、finalizer或storage字段。

## 14. 完成与交接

AL-P2-01～09、Skill/MCP安全回归和旧路径行为保持测试通过；不存在第二份invocation lifecycle；没有用户请求进入新Loop。

交付Phase 3：`CapabilityInvocationService`、Catalog/Policy、closed Catalog Preflight、Delegated activation、executable
Skill适配和Run-bound MCP binding。Phase 3不得依赖WorkflowNodePlan或具体executor内部对象。
