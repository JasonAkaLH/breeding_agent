# 统一同模型 Agent Loop 分阶段 PRD 索引

- **日期**：2026-08-22
- **状态**：实现进行中；Phase 0～Phase 3 `proof_complete`；P4-A green，下一步P4-B
- **适用分支**：`main`
- **架构来源**：`docs/superpowers/specs/2026-08-21-unified-agent-loop-design.md`
- **拆分来源**：`docs/superpowers/specs/2026-08-21-unified-agent-loop-prd-decomposition-design.md`
- **实施计划**：`docs/superpowers/specs/2026-08-22-unified-agent-loop-implementation-plan.md`（三轮document-perfectization，99/100；Phase 0～Phase 2已完成）
- **总目标**：以同一模型持续完成规划、Tool选择、结果观察、纠错、上下文压缩和最终回答；全部执行与恢复入口最终统一进入同一AgentRun，不保留DAG runtime或旧任务兼容恢复。

## 目录权威

本文档组描述已批准但尚未实施的未来架构。Phase 6完成前，当前运行时事实仍以源码和
`docs/prd/backend/00-主代理框架PRD.md`中的已实现DAG基线为准；Phase 6完成后，本目录成为任务编排的当前PRD
入口，旧DAG文档必须按总纲处置矩阵更新为rewrite、superseded或historical。

权威顺序：

1. 用户最新明确确认；
2. 统一Agent Loop架构设计；
3. PRD拆分设计；
4. 本目录总纲；
5. 各阶段PRD；
6. 后续实施计划。

阶段PRD不得改变同模型、无`maxTurns`、不恢复旧DAG Task、保留当前MCP discovery以及单一控制面等产品决策。

## 阶段文件

| 阶段 | PRD | 主责 | 当前状态 |
|---|---|---|---|
| 总纲 | `00-统一同模型AgentLoop总纲PRD.md` | 全局不变量、FR/NFR追踪、阶段门禁、文档处置 | approved |
| Phase 0 | `01-阶段零-现状基线与AgentModelContractPRD.md` | Agent Model Contract、provider门禁、现状/PRD inventory | proof_complete |
| Phase 1 | `02-阶段一-AgentRunAgentItem与TaskLease存储PRD.md` | Agent durable state、原子操作、单一Task lease、三backend parity | proof_complete |
| Phase 2 | `03-阶段二-InvocationKernel与SkillMCP适配PRD.md` | Invocation Kernel、Tool Catalog/Policy、Skill/MCP适配 | proof_complete |
| Phase 3 | `04-阶段三-核心AgentLoop与FinalOutputPRD.md` | 核心Loop、multi-call、compaction、唯一final output | proof_complete |
| Phase 4 | `05-阶段四-WaitingContinuation与RecoveryPRD.md` | Waiting、Continuation、Crash Recovery、Cancel | in_progress（P4-A green） |
| Phase 5 | `06-阶段五-APISSEFrontend与Observability适配PRD.md` | API/SSE/history/graph、Frontend、事件指标、可访问性 | pending |
| Phase 6 | `07-阶段六-全入口CleanCutover与DAGRuntime删除PRD.md` | 全入口切换、DAG runtime/wiring删除、单控制面 | pending |
| Phase 7 | `08-阶段七-破坏性Schema删除与最终门禁PRD.md` | DAG storage/proto物理删除、恢复演练、最终证明 | pending |

## 严格依赖

```text
Phase 0 Model Contract
  -> Phase 1 Agent Storage / Lease
  -> Phase 2 Invocation / Skill / MCP
  -> Phase 3 Core Loop / Final
  -> Phase 4 Waiting / Recovery
  -> Phase 5 API / Frontend / Observability
  -> Phase 6 Clean Cutover + DAG Runtime Delete
  -> Phase 7 Destructive Schema Delete + Final Proof
```

上游未满足退出门禁时，下游不得标记`in_progress`或`proof_complete`。允许在同一开发窗口顺序实施相邻阶段，但不得
合并门禁、跳过检查点或用后续测试替代当前阶段失败证据。

## 阶段状态

| 状态 | 含义 |
|---|---|
| `pending` | 上游未完成或尚未开始 |
| `blocked` | 必需代码、环境、权限或外部证据缺失；必须记录原因和解除条件 |
| `in_progress` | 当前阶段测试/实现正在受审检查点进行 |
| `proof_complete` | 当前阶段全部退出门禁通过，但尚未完成控制面切换 |
| `cutover_complete` | Phase 6全部入口切换且旧DAG runtime/wiring已删除 |
| `complete` | Phase 7全部自动、真实环境、备份恢复和文档门禁通过 |

文档生成、测试fixture存在、单backend通过、skip或not-run都不构成阶段完成。

## 全局不变量

- Agent controller、Tool选择、compaction、MCP Router/Selector和最终回答固定同一model edition；
- Agent Loop没有`maxTurns`、`max_replans`或`max_dynamic_nodes`终止条件；
- Tool-call sample和result slots在副作用前持久化，outcome按claim/revision原子提交；
- 不确定副作用不自动重放，late result不能覆盖新owner状态；
- delegated Skill只使用安全`PublicSkillProfile`投影，不读取manifest/resource正文；
- Outer Agent只看到每个public Skill和一个`mcp.dispatch`，不展开Server内完整Tool list；
- MCP discovery、授权、MRTR/Tasks、Result Parser和内部call budget保持现有安全设计；
- hidden reasoning、raw MCP result、上传正文和敏感内部信息不进入durable Agent上下文；
- Phase 6后只存在Agent Loop控制面；Phase 7后不存在DAG storage/proto运行合同；
- 不迁移、不恢复旧DAG Task，不部署`prod`；
- `docker_cmd.md`不得读取、移动、跟踪或删除。

## 验证口径

阶段PRD必须给出精确命令，统一使用仓库现有入口。Phase 6/7完整后端证明使用以下canonical命令集合；阶段PRD可在
当前阶段先跑其子集，但不得用聚焦子集替代最终门禁：

```bash
conda run -n multi_agent python -m compileall -q src tests
conda run -n multi_agent python -m unittest discover -s tests/core -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/lifecycle -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/integrations/agent_skills -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/main_agent -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/mcp_dispatch -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/mcp_tool -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/e2e -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/observability -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/scripts -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/deployment -p 'test_*.py'

cd frontend
npm test -- --run
npm run typecheck
npm run build

conda run -n multi_agent python scripts/run_rust_quality_gates.py --run --only <required gate>
```

Phase 1和Phase 7要求真实测试PostgreSQL DSN；Phase 5～7要求Frontend完整三门禁；受影响的Rust contract必须从统一
Rust gate脚本验证。`--skip-unavailable`只用于诊断，不能作为required gate通过证据。Phase 7还要求仓库外备份恢复
演练和受控真实MCP smoke；缺失时保持`blocked`，除非用户对MCP smoke明确批准书面waiver。

任何`unittest discover`门禁必须实际发现至少一个测试；`Ran 0 tests`、命令不存在或non-zero exit均为失败，不得记为
通过。

## 固定证据产物

以下文件在对应阶段实施时创建并由后续阶段消费；路径和closed字段是阶段handoff合同，不得改成进程内状态或临时
聊天记录：

| 产物 | 生成阶段 | 必需字段 |
|---|---|---|
| `active-prd-inventory.md` | Phase 0 | document path、matched legacy terms、`preserve/rewrite/supersede_at_phase6/historical`、replacement authority、owner phase、status、evidence command |
| `cutover-readiness.md` | Phase 5 | commit、Phase 0～5状态、start/resume/cancel/recovery入口清单、test evidence、active-doc状态、remaining blockers、DAG physical schema inventory |
| `dag-runtime-deletion-report.md` | Phase 6 | deleted runtime/wiring/config/events/tests、replacement tests、zero-runtime-reference scans、remaining Phase 7 schema/proto inventory、rollback checkpoint |
| `destructive-migration-evidence.md` | Phase 7 | code/schema versions、脱敏backup refs/digests、restore evidence、SQLite/PG/Sidecar migrations、full gates、static scans、MCP smoke或waiver、remaining gaps |

证据文件不得写credential、DSN、raw result、用户正文、绝对敏感路径或`docker_cmd.md`内容。未生成、字段不闭合或证据与
当前commit不匹配时，下游阶段保持`blocked`。

## Git与回滚

- 每阶段至少一个范围清晰的Git检查点；
- Phase 0～5属于pre-cutover proof：Phase 1 schema为additive，Phase 2允许旧DAG调用公共Kernel的行为保持重构，
  其余新Agent能力只允许test-only assembly；
- Phase 6在同一受审commit序列完成所有入口切换和DAG runtime/wiring删除，不保留feature flag或fallback；
- Phase 7前必须在仓库外生成并验证SQLite/PostgreSQL/Sidecar备份；
- Phase 7后只能成对恢复代码和数据，或继续forward fix。

## 文档维护

每个阶段状态变化时必须同步：

1. 本README状态；
2. 对应阶段PRD的证据与未验证项；
3. `docs/prd/README.md`、`docs/prd/backend/00-主代理框架PRD.md`和`docs/AGENTS.md`；
4. 受影响模块的`AGENTS.md`；
5. `CHANGELOG.md`。

整组PRD已批准，详细实施计划已通过审查；本次计划审查不授权业务实现，收到明确实施指令前不得修改业务代码。
