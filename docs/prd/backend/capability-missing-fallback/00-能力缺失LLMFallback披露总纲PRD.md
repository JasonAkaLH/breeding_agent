# 能力缺失 LLM Fallback 披露总纲 PRD（分步实施版）

> **Phase 6 authority notice（2026-08-23）**：本文中的旧任务编排名词仅保留为历史设计或兼容语境，不再描述当前执行控制面。当前任务入口、Tool调用、补充输入、恢复、取消和最终输出以 `docs/prd/backend/unified-agent-loop/` 为唯一authority；不得据本文恢复旧控制面或读取旧Task。

- **Status**：Phase split umbrella, implementation pending
- **Date**：2026-06-25
- **Parent PRD**：`docs/prd/backend/23-能力缺失LLMFallback披露PRD.md`
- **Scope**：Planner/Replanner、Executor 弱兜底、Runtime event、MainAgent prompt、assistant history metadata、前端 Workbench/History、审计证据和 artifact 边界

## 1. 总目标

当用户请求需要业务 Skill、MCP 工具或内置 capability，而当前 public capability 库没有匹配业务能力时，系统必须：

1. 不假装已执行缺失能力。
2. 允许通用 LLM 基于用户请求、历史上下文和可用能力摘要生成纯文本回答、草案、可复制内容或下一步建议。
3. 任务以 `completed` 完成并停止 Workbench。
4. 在 assistant 正文、`capability.missing_fallback` 事件、assistant message metadata 和前端 `CapabilityFallbackNotice` 中披露事实。
5. 不生成用户可下载平台 artifact，不提供下载按钮，不声称文件已生成。

父 PRD 是产品语义和字段定义的唯一来源；本目录只定义实施阶段、依赖和阶段验收。

## 2. 全局不变量

| 不变量 | 要求 |
| --- | --- |
| 终态 | fallback 成功回答后 `task.status=completed`；仅 LLM fallback 自身或系统存储/运行时错误才可失败。 |
| 触发主路径 | Planner/Replanner 基于 public capability 列表和上下文判断；Executor 不做通用意图正则分类。 |
| 普通 respond | 闲聊、解释、总结、草案、翻译等无需业务能力的请求继续走普通 `main_agent.respond`，不带 fallback metadata。 |
| 披露 | 后端必须保证正文前缀；前端 notice 是增强展示，不得替代正文披露。 |
| artifact | fallback 内容只可作为文本/代码块/步骤展示，不生成平台下载 artifact。 |
| metadata | history metadata 保存精简结构，不保存 raw prompt、完整历史、内部路径、handler、secret 或文件内容。 |
| 事件 | 新事件为 `capability.missing_fallback`，`visibility=frontend`，同一 task 默认只发一次。 |
| 回滚 | 旧前端不识别事件时仍能看到正文披露；旧历史无 metadata 时不显示 notice、不报错。 |

## 3. 阶段依赖

```text
P0 现状清理与基线锁定
  -> P1 Plan metadata contract
  -> P2 后端 full fallback 闭环
  -> P3 前端 notice / history 恢复
  -> P4 partial fallback / Replanner / 审计硬化
```

- P1 依赖 P0：避免 schema 实施时叠加与父 PRD 冲突的 hard-fail 或 executor 正则逻辑。
- P2 依赖 P1：没有合法 plan/node metadata，Runtime、MainAgent 和 history 无稳定事实源。
- P3 依赖 P2：前端 notice 必须消费真实 SSE event 与 history metadata，而不是前端自行推断能力缺失。
- P4 依赖 P2，建议依赖 P3：partial fallback 和 Replanner 审计需要完整后端闭环；前端展示可以复用 P3 notice。

## 4. 跨阶段总体验收

1. 清空业务能力，仅保留 `main_agent.respond`，发送“帮我生成一个田间图文件”：任务 completed，Workbench 停止，正文和 notice 披露未调用能力，无下载 artifact。
2. 发送“解释一下什么是随机区组设计”：普通回答，无 fallback metadata、无 notice。
3. 点名不存在 Skill 或 MCP 工具：任务 completed，正文披露没有调用该 Skill/MCP，`reason_code` 对应 `skill_missing` 或 `mcp_missing`。
4. 业务能力存在但参数不足：走 interrupt / slot collection，不标记 fallback。
5. 业务能力存在但执行失败：走执行失败/重试/错误恢复，不标记 capability missing。
6. 刷新历史：有 fallback metadata 的 assistant 气泡继续显示 notice；旧消息无 metadata 不报错。
7. partial fallback：已执行能力、缺失能力和 LLM 补充范围在正文、metadata、notice 和审计中可区分。

## 5. 发布门禁

- 阶段二前不得宣称用户可见 fallback 体验完成。
- 阶段三前不得宣称前端运行态/历史结构化 notice 完成。
- 阶段四前不得宣称 partial fallback 和 Replanner 后发现能力缺失完整支持。
- 任一阶段发现普通闲聊误标 fallback、fallback 生成下载 artifact、或正文未披露，必须阻断发布。
