# 多 Skill DAG 仅展示最终回答设计

日期：2026-05-18  
状态：已确认方向，待实现计划  
目标：把当前“每个 skill 中间回答 + 最终汇总”的口径调整为“用户只看到最终回答”，同时保留 skill 执行过程的后端审计、调试和 dependency context 能力。

## 背景

当前多 skill DAG 已引入 `response_role=intermediate|final`、全局 finalizer、final answer 优先写入 history/memory 等机制。这个设计解决了多个 skill 结果互相覆盖的问题，但用户现在确认新的产品口径：前端不需要展示各个 skill 的中间产物，只需要展示最终答案。

本设计不改变 skill capability 的一等地位，不引入新的数据库 schema，不要求前端理解复杂 DAG；核心变化是后端回答编排从“intermediate + final”收敛为“skill executions + one final answer”。

## 设计决策

采用 **final-only DAG**：

1. Planner 仍可把用户请求拆成多个 skill 节点。
2. WorkflowExpander 展开后只执行 skill 节点，不再为每个 `requires_finalizer` skill 自动生成用户可见的 intermediate `main_agent.respond`。
3. DAG 末尾统一追加一个 `main_agent.respond` 全局 finalizer。
4. 全局 finalizer 直接依赖所有 answer-relevant skill execute 节点。
5. 最终回答节点带 metadata：
   - `response_role="final"`
   - `answer_scope="task"`
   - `auto_skill_matching_enabled=false`
   - `finalizer_source="workflow_expander"`
6. 前端只需要展示最终 `main_agent` 输出；skill 执行事件仍可用于进度、调试或审计，但不作为聊天回答内容展示。

## 目标数据流

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

## 组件设计

### SkillWorkflowProvider

`answer_mode=requires_finalizer` 的含义调整为：该 skill 输出需要进入最终回答，而不是该 skill 后面必须追加一个用户可见回答节点。

推荐行为：

- `direct` skill：仍可直接生成用户可见 text artifact，适用于用户显式只调用单个 direct skill 的场景。
- `requires_finalizer` skill：只生成 skill execute 节点；由上层 DAG finalizer 统一回答。
- 保留 skill 输出 payload / artifact / events，作为 finalizer dependency context。

### WorkflowExpander

WorkflowExpander 负责保证最终回答存在且唯一：

- 多 skill：追加一个全局 `main_agent.respond(response_role=final)`。
- 单 skill 且需要 finalizer：追加一个 final `main_agent.respond`，角色仍是 `final`，不是 `intermediate`。
- Planner 已显式提供覆盖所有 skill 结果的 task-level `main_agent.respond` 时，只标记其为 final，不重复追加。
- 不再生成 `response_role=intermediate` 的 main-agent 节点，除非未来某个显式调试/开发模式重新开启。

### MainAgentExecutor

`response_role=final` 的 main-agent 节点：

- 使用所有直接 dependency outputs 构造 prompt。
- 抑制自动 skill matching，避免 finalizer 根据原始用户触发词重复调用 skill。
- prompt 明确要求：只输出最终结论，不输出每个 skill 的中间回答；如果部分 skill 失败，应局部说明失败，不否定其他成功结果。

### API / SSE / History / Memory

- SSE 仍可发送 skill progress / execution events，但聊天正文只应来自最终 main-agent 输出。
- assistant history 和 conversation memory 继续优先选择 `response_role=final` 的 text artifact。
- 旧任务兼容：没有 role metadata 时仍按既有 fallback 选择第一个 text artifact。

### Frontend

首轮可做最小改动：

- 正常展示最终 main-agent streaming 输出。
- 对 `response_role=intermediate` 的历史兼容事件做忽略或不渲染正文。
- skill 执行状态可以继续显示为进度/任务状态，但不是聊天回答卡片。

## 错误处理

- 某个 skill 失败：finalizer 仍运行，只汇总成功 skill 的结果，并说明失败子任务。
- 所有 skill 都失败：finalizer 输出整体失败说明，引用具体失败原因。
- finalizer 失败：task 失败或进入现有 retry / error path，不退回展示 skill 中间回答，避免用户看到半成品。
- dependency context 过长：首轮沿用现有安全投影；后续可增加内部 compaction，但不纳入本次设计。

## 验收标准

1. 多 skill DAG 中只存在一个用户可见 `main_agent.respond(response_role=final)`。
2. 不再为每个 `requires_finalizer` skill 自动生成 `response_role=intermediate` 的 main-agent 节点。
3. finalizer 直接依赖所有 answer-relevant skill execute 节点，并能看到各 skill 的 `response_text` / safe output payload。
4. finalizer 不触发自动 skill matching。
5. 前端聊天区只展示最终回答；skill 中间输出不作为聊天正文展示。
6. assistant history / conversation memory 保存最终回答。
7. 单 skill、direct skill、普通 main-agent 问答保持兼容。

## 测试计划

- WorkflowExpander：
  - 多 skill plan 只追加一个 finalizer，不生成 intermediate finalizers。
  - 单 `requires_finalizer` skill 追加一个 final finalizer。
  - Planner 显式 finalizer 时不重复追加。
- MainAgentExecutor：
  - finalizer metadata 抑制 auto skill matching。
  - final prompt 包含所有上游 skill 输出。
- API 集成：
  - fake planner 返回两个 skill，最终 nodes 中只有一个 `main_agent.respond`。
  - SSE / events 中只有一个 `main_agent.output_final(response_role=final)` 作为回答输出。
  - conversation assistant message 等于最终汇总。
- 前端回归：
  - 只展示最终回答文本。
  - skill progress 不污染聊天正文。

## 非目标

- 不新增数据库字段。
- 不引入 Artifact schema migration。
- 不引入 LangChain / LangGraph / AutoGen 等外部 agent 框架。
- 不在首轮实现长 dependency context compaction。
- 不删除后端 skill execution events；它们仍是审计和调试证据。

## 实施边界

本设计是对当前多 skill 回答编排的口径收敛，不应扩大到 Rust runtime、skill sandbox、MCP runtime 或数据库迁移。实现应优先复用现有 `answer_roles`、`answer_selection`、`WorkflowExpander`、`MainAgentExecutor` 与 API history/memory 选择逻辑。
