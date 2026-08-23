# 统一 Agent Loop Active PRD、旧测试与入口 Inventory

- **日期**：2026-08-23
- **证据Schema**：maf.unified_agent_loop.active_prd_inventory.v1
- **证据状态**：closed
- **基线Commit**：f4d6425
- **基线Tree**：d77458ead5d3ed2afd8ec0b781fbed91032f32e9
- **适用分支**：main
- **文档扫描范围**：`docs/prd/**/*.md`，排除本目录`docs/prd/backend/unified-agent-loop/`
- **测试扫描范围**：`tests/**/test_*.py`，排除新Agent替代测试与本证据测试
- **验证命令**：`conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed`

本文件已在Phase 6按实际cutover结果更新处置状态；旧实现名只作为历史匹配证据，不再构成当前运行时authority。
Phase 7相关physical contract测试保持`registered`，其余行为测试已改写或由Agent替代测试接管。Replacement authority别名如下：

| 别名 | 权威文档 |
|---|---|
| UA-P0 | `01-阶段零-现状基线与AgentModelContractPRD.md` |
| UA-P1 | `02-阶段一-AgentRunAgentItem与TaskLease存储PRD.md` |
| UA-P2 | `03-阶段二-InvocationKernel与SkillMCP适配PRD.md` |
| UA-P3 | `04-阶段三-核心AgentLoop与FinalOutputPRD.md` |
| UA-P4 | `05-阶段四-WaitingContinuation与RecoveryPRD.md` |
| UA-P5 | `06-阶段五-APISSEFrontend与Observability适配PRD.md` |
| UA-P6 | `07-阶段六-全入口CleanCutover与DAGRuntime删除PRD.md` |
| UA-P7 | `08-阶段七-破坏性Schema删除与最终门禁PRD.md` |

## 1. Active PRD disposition

| document_path | matched_legacy_terms | disposition | replacement_authority | owner_phase | status | evidence_command |
|---|---|---|---|---|---|---|
| `docs/prd/MCP/user-scoped-on-demand/02-MCP两级路由授权与任务执行闭环PRD.md` | `main_agent.respond` | `rewrite` | `UA-P2,UA-P3,UA-P4,UA-P6` | `Phase 6` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `docs/prd/backend/00-主代理框架PRD.md` | `main_agent.respond` | `rewrite` | `UA-P0,UA-P2,UA-P3,UA-P6` | `Phase 6` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `docs/prd/backend/02-编排模型与资源调度.md` | `RuntimeReplanner,main_agent.respond,max_dynamic_nodes,max_replans` | `supersede_at_phase6` | `UA-P1,UA-P2,UA-P3,UA-P6` | `Phase 6` | `superseded` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `docs/prd/backend/08-主代理Skill兼容与真实LLM运行时.md` | `main_agent.respond` | `rewrite` | `UA-P0,UA-P2,UA-P3` | `Phase 6` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `docs/prd/backend/10-对话上下文记忆与压缩PRD.md` | `main_agent.respond` | `rewrite` | `UA-P3` | `Phase 6` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `docs/prd/backend/11-Skill输出文件Artifact与下载PRD.md` | `main_agent.respond` | `rewrite` | `UA-P2,UA-P3,UA-P5` | `Phase 6` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `docs/prd/backend/12-Skill一等Capability能力池PRD.md` | `RuntimeReplanner,main_agent.respond` | `rewrite` | `UA-P2,UA-P3,UA-P6` | `Phase 6` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `docs/prd/backend/13-Skill动态加载与热部署PRD.md` | `RuntimeReplanner,main_agent.respond` | `rewrite` | `UA-P2,UA-P6` | `Phase 6` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `docs/prd/backend/14-MCPRuntime实现需求PRD.md` | `WorkflowPlan,main_agent.respond` | `rewrite` | `UA-P2,UA-P4,UA-P6` | `Phase 6` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `docs/prd/backend/15-SkillExecutor实现需求PRD.md` | `main_agent.respond` | `rewrite` | `UA-P2,UA-P3` | `Phase 6` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `docs/prd/backend/22-Skill运行闭环Workbench总纲PRD.md` | `RuntimeReplanner,WorkflowPlan,main_agent.respond,max_dynamic_nodes,max_replans` | `rewrite` | `UA-P2,UA-P3,UA-P6` | `Phase 6` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `docs/prd/backend/23-能力缺失LLMFallback披露PRD.md` | `WorkflowPlan,main_agent.respond` | `rewrite` | `UA-P2,UA-P3,UA-P5,UA-P6` | `Phase 6` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `docs/prd/backend/capability-missing-fallback/00-能力缺失LLMFallback披露总纲PRD.md` | `main_agent.respond` | `rewrite` | `UA-P2,UA-P3,UA-P5` | `Phase 6` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `docs/prd/backend/capability-missing-fallback/01-阶段零-现状清理与基线锁定PRD.md` | `main_agent.respond` | `rewrite` | `UA-P3,UA-P5` | `Phase 6` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `docs/prd/backend/capability-missing-fallback/02-阶段一-PlanMetadata契约PRD.md` | `WorkflowPlan,main_agent.respond` | `supersede_at_phase6` | `UA-P2,UA-P3,UA-P6` | `Phase 6` | `superseded` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `docs/prd/backend/capability-missing-fallback/03-阶段二-后端FullFallback闭环PRD.md` | `main_agent.respond` | `rewrite` | `UA-P2,UA-P3,UA-P5` | `Phase 6` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `docs/prd/backend/capability-missing-fallback/05-阶段四-PartialFallback与Replanner审计PRD.md` | `main_agent.respond` | `rewrite` | `UA-P3,UA-P5,UA-P6` | `Phase 6` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `docs/prd/backend/capability-missing-fallback/README.md` | `main_agent.respond` | `rewrite` | `UA-P2,UA-P3,UA-P5` | `Phase 6` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `docs/prd/backend/skill-contract-progressive-disclosure/00-SkillContract渐进式披露与显式执行总纲PRD.md` | `main_agent.respond` | `rewrite` | `UA-P2,UA-P3` | `Phase 6` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `docs/prd/backend/skill-contract-progressive-disclosure/04-PublicProfile与主代理适配PRD.md` | `main_agent.respond` | `rewrite` | `UA-P2` | `Phase 6` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `docs/prd/backend/skill-workbench/00-Skill运行闭环Workbench总纲PRD.md` | `RuntimeReplanner,WorkflowPlan,max_dynamic_nodes,max_replans` | `rewrite` | `UA-P2,UA-P3,UA-P6` | `Phase 6` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `docs/prd/backend/skill-workbench/01-阶段零-Workbench基座Policy与RuntimeStatePRD.md` | `max_dynamic_nodes,max_replans` | `rewrite` | `UA-P2,UA-P3` | `Phase 6` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `docs/prd/backend/skill-workbench/03-阶段二-RuntimeWorkbenchLoop与FinalizerDigestPRD.md` | `RuntimeReplanner,main_agent.respond,max_dynamic_nodes,max_replans` | `supersede_at_phase6` | `UA-P2,UA-P3,UA-P6` | `Phase 6` | `superseded` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `docs/prd/backend/skill-workbench/05-阶段四-Contract质量策略与健康诊断PRD.md` | `max_dynamic_nodes,max_replans` | `rewrite` | `UA-P2,UA-P3` | `Phase 6` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `docs/prd/backend/skill-workbench/README.md` | `RuntimeReplanner,WorkflowPlan,main_agent.respond` | `rewrite` | `UA-P2,UA-P3,UA-P6` | `Phase 6` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `docs/prd/frontend/00-前端业务对话台PRD.md` | `main_agent.respond` | `rewrite` | `UA-P5,UA-P6` | `Phase 6` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |

## 2. Legacy test disposition

| test_path | matched_legacy_terms | classification | replacement_authority | owner_phase | status | evidence_command |
|---|---|---|---|---|---|---|
| `tests/api/test_capabilities_list.py` | `main_agent.respond` | `migrate_behavior` | `UA-P2,UA-P5` | `Phase 5` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/api/test_conversation_memory_runtime.py` | `main_agent.respond` | `migrate_behavior` | `UA-P3,UA-P5` | `Phase 5` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/api/test_developer_docs.py` | `planner.reasoning_delta,soft_skill.reasoning_delta` | `migrate_behavior` | `UA-P5,UA-P6` | `Phase 6` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/api/test_main_agent_llm.py` | `WorkflowPlan,main_agent.respond,planner.reasoning_delta` | `migrate_then_delete_dag_shape` | `UA-P3,UA-P5,UA-P6` | `Phase 6` | `removed` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/api/test_main_agent_loop_orchestration.py` | `main_agent.respond` | `migrate_then_delete_dag_shape` | `UA-P3,UA-P6` | `Phase 6` | `removed` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/api/test_mcp_runtime_registration.py` | `main_agent.respond` | `migrate_behavior` | `UA-P2,UA-P6` | `Phase 6` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/api/test_model_edition_selection.py` | `main_agent.respond` | `migrate_behavior` | `UA-P0` | `Phase 0` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/api/test_pending_skill_context.py` | `main_agent.respond` | `migrate_behavior` | `UA-P4` | `Phase 4` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/api/test_runtime_replanner.py` | `RuntimeReplanner,main_agent.respond` | `delete_dag_shape` | `UA-P3,UA-P6` | `Phase 6` | `removed` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/api/test_skill_capability_pool.py` | `main_agent.respond` | `migrate_behavior` | `UA-P2` | `Phase 2` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/api/test_skill_dynamic_reload.py` | `main_agent.respond` | `migrate_behavior` | `UA-P2` | `Phase 2` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/api/test_skill_input_resolution_runtime.py` | `main_agent.respond` | `migrate_behavior` | `UA-P4` | `Phase 4` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/api/test_slash_force_capability.py` | `main_agent.respond` | `migrate_behavior` | `UA-P3,UA-P6` | `Phase 6` | `removed` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/api/test_soft_skill_binding.py` | `main_agent.respond` | `migrate_behavior` | `UA-P2,UA-P3` | `Phase 3` | `removed` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/api/test_task_cancel.py` | `main_agent.respond` | `migrate_behavior` | `UA-P4,UA-P5` | `Phase 5` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/api/test_task_events_sse.py` | `main_agent.respond` | `migrate_behavior` | `UA-P5` | `Phase 5` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/api/test_task_list.py` | `main_agent.respond` | `migrate_behavior` | `UA-P5` | `Phase 5` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/api/test_task_query.py` | `main_agent.respond` | `migrate_behavior` | `UA-P5` | `Phase 5` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/api/test_user_mcp_live_shadow_runtime.py` | `WorkflowPlan,main_agent.respond` | `migrate_then_delete_dag_shape` | `UA-P2,UA-P6` | `Phase 6` | `removed` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/api/test_user_mcp_phase_boundary.py` | `main_agent.respond` | `migrate_behavior` | `UA-P2,UA-P6` | `Phase 6` | `removed` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/api/test_user_mcp_task_assignment_restart.py` | `main_agent.respond` | `migrate_behavior` | `UA-P4,UA-P6` | `Phase 6` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/capabilities/main_agent/test_conversation_memory_prompt.py` | `main_agent.respond` | `migrate_behavior` | `UA-P3` | `Phase 3` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/capabilities/main_agent/test_main_agent_workflow_and_executor.py` | `main_agent.respond,max_dynamic_nodes,max_replans,soft_skill.reasoning_delta` | `migrate_then_delete_dag_shape` | `UA-P2,UA-P3,UA-P6` | `Phase 6` | `removed` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/capabilities/main_agent/test_runtime_replanner.py` | `RuntimeReplanner,WorkflowPlan,main_agent.respond,max_dynamic_nodes,max_replans` | `delete_dag_shape` | `UA-P3,UA-P6` | `Phase 6` | `removed` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/capabilities/mcp_dispatch/test_selector_router_executor.py` | `main_agent.respond` | `migrate_behavior` | `UA-P2` | `Phase 2` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/capabilities/skill_tool/test_executor.py` | `main_agent.respond` | `migrate_behavior` | `UA-P2` | `Phase 2` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/core/test_models.py` | `—` | `migrate_behavior` | `UA-P1,UA-P7` | `Phase 7` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/e2e/test_mcp_server_soft_binding.py` | `main_agent.respond` | `migrate_behavior` | `UA-P2,UA-P6` | `Phase 6` | `removed` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/integrations/agent_skills/test_execution.py` | `main_agent.respond` | `migrate_behavior` | `UA-P2` | `Phase 2` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/integrations/agent_skills/test_mini_breedstat_rcbd_skill.py` | `main_agent.respond` | `migrate_behavior` | `UA-P2` | `Phase 2` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/integrations/agent_skills/test_skill_capabilities.py` | `main_agent.respond` | `migrate_behavior` | `UA-P2` | `Phase 2` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/integrations/agent_skills/test_skill_runtime_state.py` | `main_agent.respond` | `migrate_behavior` | `UA-P2` | `Phase 2` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/integrations/mcp/test_resume_envelope.py` | `—` | `migrate_behavior` | `UA-P2,UA-P4,UA-P7` | `Phase 7` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/integrations/mcp/test_selector_context.py` | `—` | `migrate_behavior` | `UA-P2,UA-P7` | `Phase 7` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/integrations/test_mcp_runtime_state.py` | `main_agent.respond` | `migrate_behavior` | `UA-P2` | `Phase 2` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/integrations/test_runtime_sidecar_grpc_client.py` | `main_agent.respond` | `migrate_behavior` | `UA-P1,UA-P6` | `Phase 6` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/orchestration/test_auto_workflow_provider.py` | `main_agent.respond` | `delete_dag_shape` | `UA-P3,UA-P6` | `Phase 6` | `removed` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/orchestration/test_completion_policy.py` | `CompletionPolicy,WorkflowPlan,max_replans` | `delete_dag_shape` | `UA-P3,UA-P6` | `Phase 6` | `removed` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/orchestration/test_fake_capability_flow.py` | `CompletionPolicy,WorkflowPlan,max_replans` | `migrate_then_delete_dag_shape` | `UA-P2,UA-P3,UA-P6` | `Phase 6` | `removed` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/orchestration/test_llm_workflow_provider.py` | `WorkflowPlan,main_agent.respond` | `delete_dag_shape` | `UA-P0,UA-P3,UA-P6` | `Phase 6` | `removed` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/orchestration/test_mcp_dispatch_resume_v2.py` | `CompletionPolicy,TaskEdge` | `migrate_then_delete_dag_shape` | `UA-P2,UA-P4,UA-P6,UA-P7` | `Phase 7` | `removed` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/orchestration/test_mcp_route_handoff_service.py` | `CompletionPolicy,WorkflowPlan` | `migrate_then_delete_dag_shape` | `UA-P2,UA-P6` | `Phase 6` | `removed` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/orchestration/test_planner_contract.py` | `main_agent.respond` | `delete_dag_shape` | `UA-P0,UA-P6` | `Phase 6` | `removed` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/orchestration/test_planner_node_identity.py` | `WorkflowPlan,main_agent.respond` | `delete_dag_shape` | `UA-P1,UA-P6` | `Phase 6` | `removed` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/orchestration/test_planner_node_identity_flow.py` | `CompletionPolicy,RuntimeReplanner,WorkflowPlan,main_agent.respond,max_dynamic_nodes,max_replans` | `delete_dag_shape` | `UA-P1,UA-P3,UA-P6` | `Phase 6` | `removed` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/orchestration/test_runtime_replanning.py` | `CompletionPolicy,RuntimeReplanner,WorkflowPlan,max_dynamic_nodes,max_replans` | `delete_dag_shape` | `UA-P3,UA-P6` | `Phase 6` | `removed` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/orchestration/test_soft_skill_replanner.py` | `WorkflowPlan,main_agent.respond,max_dynamic_nodes,max_replans` | `delete_dag_shape` | `UA-P2,UA-P3,UA-P6` | `Phase 6` | `removed` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/orchestration/test_user_mcp_dispatch_planning.py` | `main_agent.respond` | `migrate_then_delete_dag_shape` | `UA-P2,UA-P3,UA-P6` | `Phase 6` | `removed` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/orchestration/test_workflow_expander.py` | `WorkflowPlan,main_agent.respond` | `delete_dag_shape` | `UA-P2,UA-P3,UA-P6` | `Phase 6` | `removed` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/orchestration/test_workflow_plan_validator.py` | `WorkflowPlan,main_agent.respond` | `delete_dag_shape` | `UA-P2,UA-P6` | `Phase 6` | `removed` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/orchestration/test_workflow_router.py` | `main_agent.respond` | `delete_dag_shape` | `UA-P3,UA-P6` | `Phase 6` | `removed` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/storage/test_rust_runtime_sidecar_contract.py` | `main_agent.respond` | `migrate_behavior` | `UA-P1,UA-P6,UA-P7` | `Phase 7` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/storage/test_sqlite_conversation_delete.py` | `main_agent.respond` | `migrate_behavior` | `UA-P1,UA-P6,UA-P7` | `Phase 7` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/storage/test_sqlite_task_repository.py` | `main_agent.respond` | `migrate_behavior` | `UA-P1,UA-P6,UA-P7` | `Phase 7` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `tests/storage/test_user_mcp_terminal_projection.py` | `main_agent.respond` | `migrate_behavior` | `UA-P2,UA-P4,UA-P5` | `Phase 5` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |

## 3. Execution and recovery entry inventory

| entry_id | code_anchor | current_control | replacement_authority | owner_phase | status | evidence_command |
|---|---|---|---|---|---|---|
| `ordinary_submit` | `src/api/runtime.py::submit_message` | `AgentLoopOrchestrator.start_or_resume` | `UA-P3,UA-P6 AgentLoopOrchestrator.start_or_resume` | `Phase 6` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `explicit_skill_submit` | `src/api/runtime.py::submit_message` | `same AgentRun with required first Skill Tool` | `UA-P2,UA-P3,UA-P6 required first Skill call` | `Phase 6` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `explicit_mcp_submit` | `src/api/runtime.py::submit_message` | `same AgentRun with owner-pinned required mcp.dispatch` | `UA-P2,UA-P3,UA-P6 required pinned mcp.dispatch` | `Phase 6` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `skill_missing_input_answer` | `src/api/runtime.py::answer_interrupt` | `AgentRunRecoveryCoordinator resumes original Run/call` | `UA-P4,UA-P6 original Run and call continuation` | `Phase 6` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `mcp_approval_answer` | `src/api/runtime.py::answer_interrupt` | `pending MCP authority then original Agent call resume` | `UA-P4,UA-P6 original Run and call authority` | `Phase 6` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `mcp_mrtr_answer` | `src/api/runtime.py::answer_interrupt` | `MRTR authority then original Agent call resume` | `UA-P4,UA-P6 original Run and call authority` | `Phase 6` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `mcp_remote_completion` | `src/api/runtime.py::_consume_mcp_continuation_command` | `one terminal result committed into original Agent call` | `UA-P4,UA-P6 one result into original Run and call` | `Phase 6` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `task_cancel` | `src/api/runtime.py::cancel_task` | `CancellationService plus AgentRun cancel/fencing` | `UA-P4,UA-P6 AgentRun cancel and late-result fencing` | `Phase 6` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |
| `crash_startup_recovery` | `src/api/runtime.py::_recover_agent_runs` | `AgentRun claim, reconciliation and no-replay recovery` | `UA-P4,UA-P6 AgentRun claim and no-replay recovery` | `Phase 6` | `rewritten` | `conda run -n multi_agent python scripts/validate_unified_agent_loop_evidence.py --phase 6 --require-closed` |

## 4. Cutover conclusion

- Active PRD matched set仍为26，均已标记`rewritten`或`superseded`；旧名只保留为历史匹配证据。
- Legacy test inventory已逐行标记`rewritten`、`removed`或Phase 7 `registered`，删除项均由Agent行为/E2E或纯shape处置接管。
- Execution/recovery entry set共9项，已全部改为`rewritten`并只指向Agent start/resume/cancel/recovery authority。
- Phase 6不迁移旧Task，也不读取旧DAG physical字段；Phase 7才执行物理schema/proto删除。
