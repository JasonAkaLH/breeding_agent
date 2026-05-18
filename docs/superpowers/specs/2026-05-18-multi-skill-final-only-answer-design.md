# 多 Skill DAG 仅展示最终回答设计

日期：2026-05-18
状态：已确认方向，已完成 PRD confidence 审查
目标：把当前“每个 skill 中间回答 + 最终汇总”的口径调整为“用户只看到最终回答”，同时保留 skill 执行过程的后端审计、进度、调试和 dependency context 能力。

## 1. 问题陈述

当前多 skill DAG 已引入 `response_role=intermediate|final`、全局 finalizer、final answer 优先写入 history/memory 等机制。这个设计解决了多个 skill 结果互相覆盖的问题，但当前产品口径已经调整：前端聊天正文不需要展示各个 skill 的中间产物，只需要展示任务级最终答案。

本设计要避免两类问题：

1. 用户看到多个 skill 的局部回答，误以为任务已经分段完成或互相矛盾。
2. 系统为了中间回答多次调用 `main_agent.respond`，增加 LLM 成本和回答编排复杂度。

## 2. 用户、利益相关方与受影响系统

| 类别 | 说明 |
| --- | --- |
| 终端用户 | 只关心一次任务的最终业务答案，不需要看到各 skill 中间回答。 |
| 前端业务对话台 | 继续展示进度与最终回答；不得把 skill 中间输出渲染成聊天正文。 |
| 后端编排层 | `WorkflowExpander` / `SkillWorkflowProvider` 需要保证 final-only DAG。 |
| 主代理 capability | `main_agent.respond` 作为唯一用户可见最终回答生成器。 |
| Skill executor | 继续执行 skill，并把安全输出提供给 finalizer dependency context。 |
| 存储、history、memory | 继续优先保存/读取 `response_role=final` 的 text artifact。 |

## 3. 当前状态与证据

- `SkillWorkflowProvider._build_executor_plan()` 当前对 `answer_mode="requires_finalizer"` 追加 `main_agent.respond`，且 metadata 为 `response_role="intermediate"`。证据：`src/orchestration/skill_workflow_provider.py`。
- `WorkflowExpander.expand()` 当前会在多个 skill macro 后追加全局 finalizer，并保留 macro 内部 intermediate finalizer。证据：`src/orchestration/workflow_expander.py`。
- `MainAgentExecutor` 已支持 `response_role`、`answer_scope` 和 `auto_skill_matching_enabled=false`，并在 `main_agent.output_delta/output_final` payload 中带 role。证据：`src/capabilities/main_agent/executor.py`。
- assistant history 已通过 `select_final_text_artifact()` 优先选择 final text artifact。证据：`src/api/runtime.py` 与 `src/orchestration/answer_selection.py`。
- 当前 frontend 会消费 `main_agent.output_delta/output_final`，因此如果后端仍发 intermediate delta，前端需要过滤；如果后端不再生成 intermediate，则前端仅需保持兼容防御。证据：`frontend/src/domain/taskEvents.ts`。
- 当前调度只会执行依赖已 `COMPLETED` 的节点；required dependency 失败时下游 finalizer 不会继续执行。证据：`src/orchestration/service.py` 与 `src/orchestration/completion_policy.py`。

## 4. 目标与非目标

### 目标

1. 多 skill DAG 的用户可见聊天正文只来自一个 `main_agent.respond(response_role="final")`。
2. `requires_finalizer` skill 不再自动生成 per-skill intermediate `main_agent.respond`。
3. 全局 finalizer 直接依赖所有 answer-relevant skill execute 节点，并能读取它们的安全 output payload。
4. finalizer 必须抑制自动 skill matching，避免重复调用 skill。
5. skill 执行事件、进度、artifact 和结构化输出继续保留给审计、调试和最终回答使用。
6. 单 skill、direct skill、普通 main-agent 问答保持兼容。

### 非目标

- 不新增数据库字段。
- 不引入 Artifact schema migration。
- 不引入 LangChain、LangGraph、AutoGen 等外部 agent 框架。
- 不在首轮实现长 dependency context compaction。
- 不删除后端 skill execution events。
- 不在首轮实现“部分 skill 失败后仍强制生成部分最终回答”的调度语义；首轮沿用现有 required dependency fail-closed 语义。
- 不改变 Rust runtime、skill sandbox、MCP runtime 或数据库迁移边界。

## 5. 设计决策

采用 **final-only DAG**：

1. Planner 仍可把用户请求拆成一个或多个 skill 节点。
2. Skill macro 展开后只产生 skill execute 节点；`requires_finalizer` 表示“需要最终回答消费该 skill 输出”，不再表示“该 skill 后面要单独回答用户”。
3. WorkflowExpander 在 DAG 末尾保证存在且只存在一个 task-level finalizer：`main_agent.respond(response_role="final")`。
4. finalizer 直接依赖所有 answer-relevant skill execute 节点；不依赖 hidden intermediate answer，因为首轮不生成 hidden answer。
5. finalizer metadata 固定包含：
   - `response_role="final"`
   - `answer_scope="task"`
   - `auto_skill_matching_enabled=false`
   - `finalizer_source="workflow_expander"` 或保留 planner 显式来源
6. 前端聊天正文只展示 final main-agent 输出；skill execution events 只能作为进度/审计信息展示。

## 6. 目标数据流

```text
用户请求
  ↓
Planner 拆成一个或多个 skill capability 节点
  ↓
WorkflowExpander 展开/归一化 DAG
  ↓
skill.A.execute
skill.B.execute
skill.C.execute
  ↓
global_final_answer: main_agent.respond
  depends_on = [skill.A.execute, skill.B.execute, skill.C.execute]
  response_role = final
  auto_skill_matching_enabled = false
  ↓
最终回答写入 SSE / artifact / assistant history / conversation memory
```

## 7. 功能需求

| 编号 | 需求 | 验收方式 |
| --- | --- | --- |
| FR-1 | `requires_finalizer` skill macro 不得追加 `response_role=intermediate` main-agent nodes。 | WorkflowExpander / SkillWorkflowProvider 单元测试。 |
| FR-2 | 多个 answer-relevant skill execute nodes 后必须且只能有一个 final `main_agent.respond`。 | DAG 展开测试与 API 集成测试。 |
| FR-3 | 单个 `requires_finalizer` skill 仍必须生成一个 final answer node，确保显式 skill 调用有用户可见答案。 | 单 skill 回归。 |
| FR-4 | Planner 显式提供覆盖所有 skill 输出的 task-level `main_agent.respond` 时，系统不得追加重复 finalizer。 | explicit finalizer 去重测试。 |
| FR-5 | finalizer 必须接收所有 answer-relevant skill execute nodes 的 dependency outputs。 | final prompt / dependency context 测试。 |
| FR-6 | 除非 `forced_skill_name` 明确存在，finalizer 不得触发 auto skill matching。 | MainAgentExecutor 测试。 |
| FR-7 | frontend chat body 不得渲染 `response_role=intermediate` deltas/finals；缺少 role 的事件保持 legacy-compatible。 | frontend reducer / task event 测试。 |
| FR-8 | assistant history 和 conversation memory 必须优先存取 final answer，再 fallback 到 legacy first text artifact。 | API / memory 回归。 |
| FR-9 | direct skill 在单 skill 显式调用中保持现有 direct 输出；在多 skill DAG 中其 output payload/artifact 可作为 finalizer 上游事实，但最终聊天正文仍由 finalizer 生成。 | direct + multi-skill 回归。 |

## 8. 非功能需求

| 维度 | 要求 |
| --- | --- |
| 稳定性 | 首轮必须复用现有 DAG、metadata、artifact 与 event 机制，不做 schema migration。 |
| 性能 / 成本 | 多 skill 场景减少 per-skill main-agent 调用，只保留一个最终 LLM 回答调用。 |
| 安全 / 隐私 | finalizer 只消费现有 safe dependency output；不得把 raw upload content 或 blocked memory metadata 直接注入 prompt。 |
| 可观测性 | skill execution events 与 output payload 继续记录，便于排查 skill 是否执行。 |
| 兼容性 | 旧任务、无 `response_role` artifact、普通 main-agent 问答和 direct skill 不应被破坏。 |
| 可测试性 | 每个 DAG 分支、finalizer 去重、frontend 过滤、history/memory fallback 都必须有自动化回归。 |

## 9. 组件设计

### 9.1 SkillWorkflowProvider

`answer_mode=requires_finalizer` 的含义调整为：该 skill 输出需要进入最终回答，而不是该 skill 后面必须追加一个用户可见回答节点。

推荐行为：

- `direct` skill：单 skill 显式调用保持现有 direct 输出能力。
- `requires_finalizer` skill：只生成 skill execute 节点；由上层 DAG finalizer 统一回答。
- skill execute node metadata 继续保留 `skill_name`、`skill_execution_mode`、`skill_answer_mode`、`skill_bundle_revision`。
- `skill_finalizer_added` metadata 应调整为 false 或替换为更准确的 `skill_requires_finalizer=true`，避免误导后续调试。

### 9.2 WorkflowExpander

WorkflowExpander 负责保证最终回答存在且唯一：

- 多 skill：追加一个全局 `main_agent.respond(response_role=final)`。
- 单 `requires_finalizer` skill：追加一个 final `main_agent.respond`，角色是 `final`。
- Planner 已显式提供覆盖所有 answer-relevant skill 节点的 `main_agent.respond` 时，只标记其为 final，不重复追加。
- Planner 显式 finalizer 只覆盖部分 skill 时，仍需追加全局 finalizer 或扩展该显式 finalizer 依赖，使最终回答覆盖所有 answer-relevant outputs；首选追加全局 finalizer，避免隐式改写 planner 节点语义。
- 不再生成 `response_role=intermediate` 的 main-agent 节点。保留 `RESPONSE_ROLE_INTERMEDIATE` 常量仅用于历史兼容和前端过滤。

### 9.3 MainAgentExecutor

`response_role=final` 的 main-agent 节点：

- 使用所有直接 dependency outputs 构造 prompt。
- 抑制自动 skill matching，除非存在显式 `forced_skill_name`。
- prompt 必须明确要求：只输出最终结论；不要输出每个 skill 的中间回答；如果上游缺少某个必要结果，应说明缺失，不要否定已有成功结果。
- `main_agent.output_delta` 与 `main_agent.output_final` 继续携带 `response_role=final`，便于前端和 history/memory 选择。

### 9.4 API / SSE / History / Memory

- SSE 可继续发送 skill progress / execution events。
- 用户聊天正文只应来自 final main-agent output。
- assistant history 和 conversation memory 继续使用 `select_final_text_artifact()`。
- 如果 no-role legacy task 没有 final artifact，继续按既有 fallback 选择第一个非空 text artifact。

### 9.5 Frontend

首轮需要一个小的防御性过滤：

- 渲染 `main_agent.output_delta/output_final` 时：
  - `response_role` 缺失：按 legacy 主回答处理。
  - `response_role="final"`：作为聊天正文展示。
  - `response_role="intermediate"`：不作为聊天正文展示。
- skill progress / execution events 可以继续作为任务状态、进度或调试信息展示，但不得进入 assistant message 正文。

## 10. 错误处理与边界情况

| 场景 | 首轮行为 |
| --- | --- |
| 某个 required skill 失败 | 沿用现有调度：下游 finalizer 不执行，task 进入 failure / replan 路径。 |
| 所有 skill 成功但 finalizer 失败 | task 失败或进入现有 retry / error path；不退回展示 skill 中间回答。 |
| Planner 已有完整 task finalizer | 标记/保留为 final，不重复追加。 |
| Planner 只有部分 finalizer | 追加全局 finalizer 覆盖所有 answer-relevant skill outputs。 |
| 单 `requires_finalizer` skill 显式调用 | 仍生成一个 final answer，保证用户有可见结果。 |
| 单 `direct` skill 显式调用 | 保持 direct 输出，不强制额外 finalizer。 |
| 多 skill 中包含 direct skill | direct skill 输出作为 finalizer 上游事实；聊天正文仍以 finalizer 为准。 |
| dependency context 过长 | 沿用现有 safe projection；压缩策略作为后续优化。 |
| 旧 intermediate events 被前端收到 | 前端过滤 `response_role=intermediate`，避免渲染为聊天正文。 |

## 11. 验收标准

1. 多 skill DAG 中只有一个用户可见 `main_agent.respond(response_role=final)`。
2. 不再为每个 `requires_finalizer` skill 自动生成 `response_role=intermediate` 的 main-agent 节点。
3. 单 `requires_finalizer` skill 仍有一个 final answer。
4. finalizer 直接依赖所有 answer-relevant skill execute 节点，并能看到各 skill 的 `response_text` / safe output payload。
5. finalizer 不触发自动 skill matching。
6. 前端聊天区只展示最终回答；skill 中间输出不作为聊天正文展示。
7. assistant history / conversation memory 保存最终回答。
8. direct skill、普通 main-agent 问答和 legacy no-role artifact 保持兼容。
9. 自动化测试覆盖 DAG、main-agent、API、frontend 与 history/memory 选择。

## 12. 测试计划

### 12.1 Workflow / Orchestration

- 修改 `tests/orchestration/test_workflow_expander.py`：
  - 多 skill plan 只追加一个 finalizer，不生成 intermediate finalizers。
  - 单 `requires_finalizer` skill 追加一个 final finalizer。
  - Planner 显式完整 finalizer 时不重复追加。
  - Planner 显式部分 finalizer 时仍保证最终全局 finalizer 覆盖所有 skill。
  - `direct` skill 单独调用不追加 finalizer；multi-skill 中 direct output 可进入 finalizer dependency list。

### 12.2 MainAgentExecutor

- 保留并强化 `tests/capabilities/main_agent/test_main_agent_workflow_and_executor.py`：
  - finalizer metadata 抑制 auto skill matching。
  - forced skill 优先于 suppression。
  - final prompt 包含所有上游 skill 输出，并有 final-only 指令。

### 12.3 API 集成

- 修改 `tests/api/test_main_agent_loop_orchestration.py`：
  - fake planner 返回两个 skill，最终 nodes 中只有一个 `main_agent.respond`。
  - events 中只有一个用户回答用 `main_agent.output_final(response_role=final)`。
  - conversation assistant message 等于最终汇总。
  - answer-only skill 的 `response_text` 仍进入 finalizer prompt。

### 12.4 History / Memory

- 保留 `tests/orchestration/test_answer_selection.py` 与 conversation memory 回归：
  - final artifact 优先。
  - no-role legacy artifact fallback。

### 12.5 Frontend

- 修改 `frontend/src/domain/taskEvents.test.ts` 或相关 reducer 测试：
  - `response_role="final"` delta 进入聊天正文。
  - `response_role="intermediate"` delta/final 不进入聊天正文。
  - 无 role 的 legacy delta 保持可见。

## 13. 依赖与集成点

| 位置 | 变更类型 |
| --- | --- |
| `src/orchestration/skill_workflow_provider.py` | 停止为 `requires_finalizer` skill 生成 intermediate finalizer。 |
| `src/orchestration/workflow_expander.py` | 改 finalizer 追加条件、answer-relevant node 收集和 explicit finalizer 去重。 |
| `src/capabilities/main_agent/prompt_builder.py` | 收紧 final-only prompt 指令。 |
| `src/capabilities/main_agent/executor.py` | 保持 role event/output 与 suppression 行为，按测试补缺。 |
| `src/api/runtime.py` / `src/orchestration/conversation_memory.py` | 继续使用 final artifact 选择 helper，必要时传入 events 增强选择。 |
| `frontend/src/domain/taskEvents.ts` | 对 intermediate role 做聊天正文过滤。 |

## 14. 风险、假设与后续

| 类型 | 内容 | 处理 |
| --- | --- | --- |
| 假设 | 用户接受首轮 fail-closed：required skill 失败时不生成部分最终回答。 | 已记录为非目标；如需部分汇总，需要单独设计可选 dependency / partial finalizer。 |
| 风险 | finalizer dependency output 过长导致 prompt 截断。 | 首轮沿用现有 safe projection；后续补 compaction。 |
| 风险 | direct skill text artifact 在多 skill task 中被误选为 history。 | 继续优先 final artifact，并用 API 回归覆盖。 |
| 风险 | 前端 legacy event 与新 role 混合导致显示不一致。 | 明确 role 过滤规则：final/无 role 可见，intermediate 不可见。 |
| 后续 | 如果未来需要“调试模式展示中间结果”，应作为显式 debug/ops feature，不应复用默认用户聊天正文。 | 后续 PRD 处理。 |

## 15. 实施边界

本设计是对当前多 skill 回答编排的口径收敛。实现应优先复用现有 `answer_roles`、`answer_selection`、`WorkflowExpander`、`MainAgentExecutor` 与 API history/memory 选择逻辑。任何扩大到 schema migration、Rust runtime、MCP runtime 或 raw artifact prompt 注入的改动都超出本设计范围。
