# 阶段二：后端 Full Fallback 闭环 PRD

> **Phase 6 authority notice（2026-08-23）**：本文中的旧任务编排名词仅保留为历史设计或兼容语境，不再描述当前执行控制面。当前任务入口、Tool调用、补充输入、恢复、取消和最终输出以 `docs/prd/backend/unified-agent-loop/` 为唯一authority；不得据本文恢复旧控制面或读取旧Task。

- **Status**：Ready for implementation
- **Date**：2026-06-25
- **Parent PRD**：`docs/prd/backend/23-能力缺失LLMFallback披露PRD.md`
- **Depends On**：阶段一 Plan Metadata 契约
- **Phase Goal**：完成 full fallback 后端交付闭环：无匹配业务能力时，Planner 输出 full fallback，任务 completed，Runtime 发事件，MainAgent prompt 和正文后处理披露事实，assistant history metadata 持久化，且不生成下载 artifact。

## 1. In Scope

1. Planner 区分三类输出：
   - 正常业务 DAG；
   - 普通 `main_agent.respond`；
   - 带 `capability_missing_fallback.scope=full` 的 `main_agent.respond`。
2. Planner 输入包含：
   - 用户原始请求；
   - effective question；
   - history / memory context；
   - public capability 列表；
   - 排除 `main_agent.respond` 后的业务能力摘要。
3. 可用业务能力摘要：
   - 来源必须是 `CapabilityRegistry.list(public_only=True)` 或等价 public contract 读取路径；
   - 必须排除 `main_agent.respond`；
   - 只允许暴露 public `capability_id` / `name` / `description`；
   - 不得暴露 handler、runtime、source path、sandbox、内部模块名、secret 或文件系统路径；
   - 超过预算时必须设置 `available_capabilities_truncated=true` 并保留 `available_capability_count`。
4. 显式缺失能力路径：
   - 用户点名不存在 Skill / MCP / capability 时也必须进入 full fallback；
   - 强制 Skill 路由、slash / soft skill binding 指向不可用 Skill 时不得 hard-fail 为常规路径；
   - 旧 `required_skill_missing` / unsupported binding 行为必须改为 fallback completed + disclosure，除非请求本身不是可接受的 API contract。
5. Runtime：
   - 发现 plan/node fallback metadata 后发送一次 `capability.missing_fallback` frontend 事件；
   - task 成功走到 completed；
   - 将 fallback metadata 传给 final `main_agent.respond`。
6. MainAgent：
   - prompt 注入 fallback 事实、可用业务能力摘要和 artifact 禁止边界；
   - 不声称已调用 Skill/MCP/工具；
   - 不声称生成平台文件或下载链接。
7. 后端正文后处理：
   - 只要 `disclosure_required=true`，保存前统一 prepend 标准披露段，并对完全相同前缀去重；
   - 拦截或改写明显违规表述，例如“文件已生成”“请点击下载”“已调用某某 Skill”“后台正在生成 artifact”。
8. history：
   - `_persist_assistant_history_message()` 或等价路径写入精简 `Message.metadata.capability_missing_fallback`；
   - conversation messages API 返回 metadata。
9. artifact：
   - fallback 不创建用户可下载平台 artifact；
   - 内部 final text artifact / event 不被误认为下载 artifact。

## 2. Out of Scope

- 前端 `CapabilityFallbackNotice` 展示留到阶段三。
- partial fallback 和 Replanner 后发现能力缺失留到阶段四。
- 不新增数据库 schema；复用 message metadata。

## 3. Functional Requirements

| 编号 | 要求 | 验收 |
| --- | --- | --- |
| P2-R1 | 无匹配业务能力时 Planner 输出 full fallback metadata。 | Planner 单测。 |
| P2-R2 | 普通闲聊/解释不带 fallback metadata。 | Planner/main_agent 回归。 |
| P2-R3 | Runtime 发送一次 `capability.missing_fallback`，visibility 为 frontend。 | runtime event 测试。 |
| P2-R4 | fallback task 最终 `completed`。 | API/runtime 测试。 |
| P2-R5 | MainAgent prompt 注入 fallback 约束和可用业务能力摘要。 | prompt builder 单测。 |
| P2-R6 | assistant 正文保存前强制披露。 | executor/runtime 单测。 |
| P2-R7 | assistant message metadata 持久化并由 history API 返回。 | API/history 测试。 |
| P2-R8 | fallback 不生成用户可下载平台 artifact。 | artifact/API 测试。 |
| P2-R9 | 点名不存在 Skill/MCP/capability、slash / soft skill binding 指向不可用 Skill 时走 fallback completed + disclosure，不走常规 hard-fail。 | API / soft binding / runtime 测试。 |
| P2-R10 | 可用业务能力摘要 public-only、排除 `main_agent.respond`，并对截断和敏感字段做收敛。 | registry summary / sanitizer 单测。 |

## 4. Edge Cases

| 场景 | 期望 |
| --- | --- |
| 过滤后没有业务能力 | 正文披露“当前能力库没有可用业务能力，本次仅使用通用 LLM 回答”。 |
| 可用业务能力列表被截断 | metadata / prompt 标记 `available_capabilities_truncated=true`，不夸大系统能力。 |
| 用户点名不存在 Skill/MCP | `reason_code=skill_missing` 或 `mcp_missing`，任务 completed，正文披露没有调用该能力。 |
| soft skill binding 指向不可用 Skill | 作为强制能力缺失 fallback 处理；不得继续返回旧式 `required_skill_missing` 失败作为目标语义。 |
| LLM 忘记披露 | 后端 prepend 标准披露段。 |
| LLM 声称生成下载文件 | 后端改写或阻断为“可复制文本，不是系统生成的下载文件”。 |
| fallback LLM 调用失败 | 任务可失败；这是 LLM fallback 自身失败，不是能力缺失语义。 |

## 5. 测试计划

```bash
python -m pytest tests/orchestration/ -k "planner or llm_workflow_provider"
python -m pytest tests/capabilities/main_agent/
python -m pytest tests/api/ -k "task or event or conversation or artifact"
```

## 6. 完成标准

- 清空业务能力、仅保留 `main_agent.respond` 时，真实产物请求会 completed + 正文披露 + history metadata 保留 + 无下载 artifact。
- 普通解释类请求无 fallback metadata。
- 后端闭环不依赖前端才能满足事实披露。
