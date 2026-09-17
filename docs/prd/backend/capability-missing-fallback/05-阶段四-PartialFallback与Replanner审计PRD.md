# 阶段四：Partial Fallback、Replanner 与审计硬化 PRD

> **Phase 6 authority notice（2026-08-23）**：本文中的旧任务编排名词仅保留为历史设计或兼容语境，不再描述当前执行控制面。当前任务入口、Tool调用、补充输入、恢复、取消和最终输出以 `docs/prd/backend/unified-agent-loop/` 为唯一authority；不得据本文恢复旧控制面或读取旧Task。

- **Status**：Ready for implementation
- **Date**：2026-06-25
- **Parent PRD**：`docs/prd/backend/23-能力缺失LLMFallback披露PRD.md`
- **Depends On**：阶段二后端 full fallback 闭环；建议阶段三前端 notice 已完成
- **Phase Goal**：支持复杂 DAG：部分能力可执行、部分能力缺失时先执行可用能力，再由 final `main_agent.respond` 做 partial fallback；Replanner 后发现新增能力缺失时也能降级完成，并让事件、metadata、正文、notice 和审计一致。

## 1. In Scope

1. Planner partial fallback：
   - 可用业务能力先执行；
   - final `main_agent.respond` 携带 `capability_missing_fallback.scope=partial`；
   - 必填 `attempted_capability_summary` 和 `fallback_content_scope`。
2. Replanner partial fallback：
   - 运行中发现后续需要 registry 不存在的能力时，不编造 capability node；
   - 输出 final fallback node；
   - 保留已完成节点结果作为上游上下文。
3. Replanner scope 判定：
   - 如果 task 已有成功完成的业务能力结果，fallback scope 必须为 `partial`；
   - 如果 task 尚无任何成功完成的业务能力结果，fallback scope 必须为 `full`；
   - 两种 scope 都必须走 completed + disclosure，不得编造 capability node 或 hard-fail。
4. Runtime event：
   - partial 可在已执行能力完成后、finalizer 前发送；
   - 同一 task 默认只发送一次；
   - payload 与 final history metadata 尽量一致。
5. MainAgent prompt / 正文：
   - 区分已执行能力结果、缺失能力和 LLM fallback 内容范围；
   - partial 标准披露文案包含 `attempted_capability_summary`。
6. 前端 notice：
   - 展示已调用能力摘要和 fallback 内容范围；
   - 复用阶段三组件和 metadata parser。
7. 审计：
   - 能回答为什么没有调用某能力、缺少什么能力、已执行什么、fallback 是否允许 artifact、是否使用 LLM fallback。

## 2. Out of Scope

- 不引入新的 task 终态。
- 不引入全局 severity / level。
- 不实现前端手动重试或安装能力入口。
- 不对具体业务能力做特判。

## 3. Functional Requirements

| 编号 | 要求 | 验收 |
| --- | --- | --- |
| P4-R1 | Planner 可输出“部分能力 + final fallback node”。 | planner 集成测试。 |
| P4-R2 | `scope=partial` 必填 `attempted_capability_summary`。 | schema/validator 测试。 |
| P4-R3 | Replanner 发现能力缺失时输出 final fallback node，不编造 capability。 | replanner 测试。 |
| P4-R4 | final answer 区分已执行事实与 LLM fallback 内容。 | main_agent/runtime 测试。 |
| P4-R5 | 同一 task 默认只发一次 `capability.missing_fallback`。 | runtime event 测试。 |
| P4-R6 | history metadata、event payload 和前端 notice 对 partial 字段一致。 | API + frontend 测试。 |
| P4-R7 | 审计查询可检索 fallback 证据。 | audit/event store 测试。 |
| P4-R8 | Replanner 根据已成功完成业务能力结果判定 full vs partial scope；无已执行结果时不得误标 partial。 | replanner / runtime 测试。 |

## 4. Edge Cases

| 场景 | 期望 |
| --- | --- |
| 部分能力执行成功，后续缺失 | 已执行结果作为上下文，final partial fallback completed。 |
| Replanner 发现缺失但尚无已完成业务能力 | final full fallback completed，披露没有调用匹配能力。 |
| 部分能力执行失败 | 这是执行失败/重试/错误恢复，不应伪装成 partial fallback。 |
| Replanner 多次发现同一缺失 | 事件去重，同一 task 默认只保留一次可见 fallback event。 |
| plan/node fallback 不一致 | Runtime 选择更保守披露策略，并记录一致化后的 metadata。 |
| 前端只收到 final history metadata | 仍能展示 partial notice。 |

## 5. 手工验收场景

1. 安装文件读取能力，但无田间图生成能力，发送“读取文件并生成田间图”。
   - 预期：文件读取执行；田间图生成和下载部分 partial fallback；任务 completed；notice 显示已调用能力和缺失范围。
2. Replanner 执行中发现后续需要不存在的 MCP 工具。
   - 预期：不编造 MCP capability；final fallback completed；审计记录 `reason_code=mcp_missing`。
3. 普通闲聊穿插在同一会话后再次发送解释类问题。
   - 预期：普通回答不继承前一轮 fallback notice。

## 6. 测试计划

```bash
python -m pytest tests/orchestration/ -k "replanner or partial or fallback"
python -m pytest tests/capabilities/main_agent/
python -m pytest tests/api/ -k "event or audit or conversation"
cd frontend && npm test -- --run
```

## 7. 完成标准

- partial fallback 在后端、前端、history 和审计中语义一致。
- Replanner 后发现能力缺失不会造成任务卡住、编造能力或 hard-fail。
- full fallback 回归仍通过，普通 LLM 请求仍不误标。
