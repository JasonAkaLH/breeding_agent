# 能力缺失 LLM Fallback 披露 PRD

> **Phase 6 authority notice（2026-08-23）**：本文中的旧任务编排名词仅保留为历史设计或兼容语境，不再描述当前执行控制面。当前任务入口、Tool调用、补充输入、恢复、取消和最终输出以 `docs/prd/backend/unified-agent-loop/` 为唯一authority；不得据本文恢复旧控制面或读取旧Task。

- Status: Reviewed draft, phase-split ready for implementation
- Date: 2026-06-25
- Scope: 后端 Planner/Replanner/Runtime/MainAgent、前端 Workbench/History、审计事件

## 1. 问题背景

当用户请求需要业务能力、Skill、MCP 工具或内置 capability 执行，但当前能力库没有匹配能力时，系统不能假装已执行能力，也不能让 Workbench 持续显示运行中。

能力缺失不等于必须失败。系统可以把用户原始请求、历史上下文和当前可用能力摘要交给通用 LLM，让 LLM 尝试给出纯文本回答、草案、可复制内容或下一步建议。但系统必须明确告知用户：本次没有调用匹配 Skill / 能力，因为能力库中没有匹配的可执行能力。

本 PRD 解决的是通用机制：**没有对应 Skill / MCP / capability 时如何停止 Workbench、如何回答、如何披露事实、如何保留历史和审计证据**。不得针对某一次对话历史做特判。

## 2. 目标

1. 统一处理 `capability missing`、`skill missing`、`MCP capability missing` 和内置业务能力缺失。
2. 允许 LLM fallback：把用户原始请求、历史上下文和当前可用业务能力列表传给 LLM。
3. 强制事实披露：不得掩盖没有调用匹配能力的事实。
4. Workbench 必须停止：fallback 回答完成后任务为 `completed`，不是运行中或失败。
5. 前端运行态和历史记录都保留结构化提示。
6. 对真实产物请求，LLM 可输出可复制文本，但不得声称已生成系统 artifact、文件或下载链接。
7. Planner/Replanner 是主判断路径；Executor 只做弱兜底，不做通用意图正则分类。

## 3. 非目标

- 不引入新的任务终态，例如 `completed_with_warning`。
- 不引入全局 severity / level 体系。
- 不让前端或后端为 LLM fallback 内容生成平台文件 artifact / 下载文件。
- 不用单次历史对话文本做特判。
- 不把 `main_agent.respond` 伪装成已经执行了缺失业务能力。
- 不在主代理身份 prompt 中硬编码具体能力清单；主代理只知道系统具备可扩展 Skill / capability 机制，具体可用能力来自运行时上下文。

## 4. 术语与判定边界

| 术语 | 定义 |
| --- | --- |
| capability | 后端 `CapabilityRegistry` 中注册的可执行能力，包括 Skill、MCP、内置业务能力和 `main_agent.respond`。 |
| 业务能力 | 除 `main_agent.respond` 外的 public capability。用于实际执行查询、生成、转换、调用工具等业务动作。 |
| Skill missing | 用户显式或隐式请求某个 Skill，但当前 Skill 能力库没有匹配项，或 soft skill binding 指向不可用 Skill。 |
| MCP missing | 用户点名或请求某个 MCP 工具能力，但当前 MCP 能力库没有匹配项。 |
| capability missing fallback | 无匹配业务能力时，系统转为通用 LLM 回答，并强制披露事实的降级路径。 |
| full fallback | 没有调用任何匹配业务能力，最终回答全部由通用 LLM 生成。 |
| partial fallback | 已调用部分可用业务能力，但用户请求中的某些能力缺失，最终由 LLM 补充说明、草案或可复制内容。 |

### 4.1 哪些请求应触发 capability missing fallback

Planner/Replanner 必须基于用户原文、effective question、对话历史和 public capability 列表判断。以下场景应触发：

1. 用户请求执行某类业务动作，但 public capability 列表中没有匹配业务能力。
   - 例如生成平台文件、导出报告、生成田间图、查询特定数据库、调用外部工具。
2. 用户显式点名不存在的 Skill、MCP 工具或 capability。
3. 用户请求的任务可拆分，其中一部分有能力可执行，另一部分无能力可执行。
4. soft skill binding 或强制 Skill 路由指向不可用 Skill。
5. Replanner 在执行过程中发现后续需要新增能力，但 registry 中不存在。

### 4.2 哪些请求不应触发 capability missing fallback

以下场景应直接使用正常 `main_agent.respond`，不标记能力缺失：

1. 普通闲聊、解释概念、总结已有对话、改写文本、翻译、头脑风暴等无需业务能力的通用 LLM 请求。
2. 用户明确只要求“给我建议/草案/思路”，且没有声称需要系统调用 Skill、MCP、数据库、文件生成或业务工具。
3. 业务能力存在，但输入不足；这应走现有 interrupt / slot collection / 参数补全路径，而不是能力缺失。
4. 能力存在但执行失败；这属于执行失败、重试或错误恢复，不是 capability missing。

## 5. 当前系统状态与证据

| 事实 | 证据 | 对 PRD 的影响 |
| --- | --- | --- |
| `WorkflowPlan` 和 `WorkflowNodePlan` 已有 `metadata` 字段。 | `src/orchestration/models.py` | 可以承载 `capability_missing_fallback`，不需要新增核心 dataclass 字段。 |
| 当前 planner JSON schema 顶层只允许 `nodes`，node 只允许 `node_id/capability_id/depends_on/input_payload`。 | `src/orchestration/planner_contract.py` 的 `PLANNER_OUTPUT_JSON_SCHEMA` | 必须扩展 schema、parser、repair prompt，否则 fallback metadata 会被拒绝或丢失。 |
| Planner prompt 当前要求“兜底对话、解释、汇总使用 main_agent.respond”。 | `src/orchestration/planner_contract.py` | 需要补充“普通 main_agent.respond”和“能力缺失 fallback main_agent.respond”的区别。 |
| assistant message 存储支持 metadata，历史接口会暴露 metadata。 | `src/storage/sqlite/models.py`、`src/api/routes/conversations.py` | 可以在历史恢复时展示 fallback notice。 |
| `_persist_assistant_history_message()` 当前只从 final text artifact 写 assistant message content，没有写 metadata。 | `src/api/runtime.py` | 需要定义从 plan/node/event/result 到 assistant message metadata 的传播路径。 |
| `EventVisibility` 当前只有 `frontend/internal/audit_only`。 | `src/core/enums.py` / runtime contract | `capability.missing_fallback` 不能写成“frontend + audit”双 visibility；本 PRD 选择单条 `frontend` 事件，审计查询也可检索该事件。 |
| 前端消息模型已有 `metadata?: Record<string, unknown>`，但运行时 patch 类型和气泡渲染尚无 fallback notice。 | `frontend/src/App.tsx` | 需要补 runtime SSE 解析、history metadata 解析、`CapabilityFallbackNotice` 渲染。 |
| 当前 Skill 匹配主要来自规则匹配。 | `src/integrations/agent_skills/matcher.py` | 缺失判断不能依赖 executor 正则；应由 Planner/Replanner 基于 capability registry 和上下文判断。 |

## 6. 核心产品语义

### 6.1 任务终态

能力缺失 fallback 是一次成功完成的降级回答：

- `task.status = completed`。
- Workbench 停止，发送按钮恢复为可提交状态。
- 事件和 assistant message metadata 记录 fallback 事实。
- 不设置 `failed`，除非 LLM fallback 自身执行失败或存储/运行时发生真正系统错误。

### 6.2 用户可见披露

采用双重披露：

1. **assistant 正文开头必须披露事实点**：
   - 没有调用匹配 Skill / 能力；
   - 原因是能力库中没有匹配的可执行能力；
   - 后续内容由通用 LLM 基于请求和历史上下文生成。
2. **前端结构化提示**：
   - 运行中 / 完成前在 Workbench 状态区展示；
   - 完成后 / 历史恢复在 assistant 气泡顶部保留。

为避免 LLM 漏披露，后端不得只依赖 prompt 约束。只要存在 `capability_missing_fallback.disclosure_required=true`，最终保存和展示的 assistant 正文必须由后端保证带披露前缀。

建议标准正文前缀：

- full fallback：
  > 本次回答没有调用 Skill/能力，因为能力库中没有匹配的可执行能力。以下内容由通用 LLM 基于你的请求和历史上下文生成，仅供参考。
- partial fallback：
  > 本次仅调用了部分可用能力；对于缺失能力的部分，没有调用匹配 Skill/能力，因为能力库中没有匹配的可执行能力。以下标注范围内的内容由通用 LLM 基于请求、历史上下文和已执行结果生成，仅供参考。

### 6.3 真实产物边界

当用户请求生成文件、导出表格、生成田间图、生成 HTML/CSV/PDF 等真实产物，而能力库没有匹配能力时：

- 允许 LLM 输出 Markdown 表格、CSV 文本、HTML 代码块、布局草案、伪代码或手工步骤。
- 不生成平台文件 artifact。
- 不提供下载按钮。
- 不声称“文件已生成”“后台正在生成”“可以下载”。
- 必须说明这是可复制内容，不是系统调用 Skill 后生成的文件。

说明：系统内部已有 final text artifact / event 用于 assistant 历史同步时，不属于用户可下载的平台文件 artifact。实施时不得破坏现有 assistant 历史同步机制。

## 7. Fallback 数据结构

统一字段名：`capability_missing_fallback`。

### 7.1 运行时完整结构

```json
{
  "enabled": true,
  "scope": "full|partial",
  "reason_code": "capability_missing|skill_missing|forced_skill_missing|mcp_missing",
  "missing_capability_summary": "用户需要随机区组田间图生成能力，但当前能力库没有匹配 Skill。",
  "attempted_capability_summary": "已调用文件读取能力；未调用田间图生成能力。",
  "fallback_content_scope": "田间图布局、文件生成和下载入口部分由 LLM 生成说明或草案。",
  "available_capabilities": [
    {
      "capability_id": "skill.example",
      "name": "Example Skill",
      "description": "..."
    }
  ],
  "available_capability_count": 1,
  "available_capabilities_truncated": false,
  "llm_fallback_allowed": true,
  "artifact_generation_allowed": false,
  "disclosure_required": true,
  "memory_context_used": true,
  "source_message_ids": ["msg-..."],
  "source_message_count": 3
}
```

Required fields:

- `enabled`
- `scope`
- `reason_code`
- `missing_capability_summary`
- `fallback_content_scope`
- `llm_fallback_allowed`
- `artifact_generation_allowed`
- `disclosure_required`

Partial fallback additionally requires `attempted_capability_summary`。

### 7.2 Prompt 与历史 metadata 差异

- **LLM prompt**：可以接收可用业务能力摘要、用户原始请求、effective question、对话 memory context、已执行节点结果摘要。
- **assistant message metadata**：保存精简结构，避免历史膨胀。不得保存完整历史正文、完整 prompt、内部 handler、runtime、source path、secret、文件系统路径。

assistant message metadata 建议保留：

```json
{
  "enabled": true,
  "scope": "full",
  "reason_code": "capability_missing",
  "missing_capability_summary": "...",
  "attempted_capability_summary": "...",
  "fallback_content_scope": "...",
  "llm_fallback_allowed": true,
  "artifact_generation_allowed": false,
  "disclosure_required": true,
  "memory_context_used": true,
  "source_message_count": 3,
  "source_message_ids": ["msg-..."]
}
```

### 7.3 可用能力列表来源

`available_capabilities` 由后端从 `CapabilityRegistry.list(public_only=True)` 生成。

过滤规则：

- 不包含 `main_agent.respond`。
- 覆盖 public Skill、MCP、内置业务 capability。
- 只暴露 public contract 的 id/name/description；不得暴露 handler、runtime、source path、sandbox、内部模块名。
- 可复用 planner 当前 public capability budget；如果超过预算，必须设置 `available_capabilities_truncated=true`，并保留 `available_capability_count`。
- 如果过滤后为空，用户提示应表达：
  > 当前能力库没有可用业务能力，本次仅使用通用 LLM 回答。

## 8. Planner / Replanner 设计

### 8.1 Planner 主路径

Planner 输入包括：

- 用户原始请求；
- effective question；
- 历史上下文 / memory context；
- 当前 public capability 列表；
- 排除 `main_agent.respond` 后的可用业务能力列表摘要；
- 本 PRD 的判定规则摘要。

Planner 输出允许三类结果：

1. 正常 DAG：使用已有业务能力。
2. 普通 `main_agent.respond`：用于闲聊、解释、总结、草案等不需要业务能力的请求，不设置 fallback metadata。
3. 带 `capability_missing_fallback` 的 DAG：使用已有部分能力，并在最终 `main_agent.respond` 节点降级回答。

Planner 不得输出不存在的 capability id。缺失能力只能通过 `capability_missing_fallback` 表达。

### 8.2 Planner JSON schema 要求

必须扩展 `PLANNER_OUTPUT_JSON_SCHEMA`：

- 顶层允许 `metadata`。
- node 允许 `metadata`。
- `metadata.capability_missing_fallback` 必须符合本 PRD 的结构。
- `additionalProperties` 仍应保持收敛；只开放必要字段，避免 LLM 任意输出。
- Repair prompt 必须知道 fallback metadata 字段，否则上一轮合法 fallback 输出可能被 repair 误删。
- `build_plan_from_llm_output()` 必须把顶层 metadata 和 node metadata 解析进 `WorkflowPlan` / `WorkflowNodePlan`。

建议 schema 形状：

```json
{
  "nodes": [
    {
      "node_id": "final_response",
      "capability_id": "main_agent.respond",
      "depends_on": [],
      "input_payload": {},
      "metadata": {
        "capability_missing_fallback": {
          "enabled": true,
          "scope": "full",
          "reason_code": "capability_missing",
          "missing_capability_summary": "...",
          "fallback_content_scope": "...",
          "llm_fallback_allowed": true,
          "artifact_generation_allowed": false,
          "disclosure_required": true
        }
      }
    }
  ],
  "metadata": {
    "capability_missing_fallback": {
      "enabled": true,
      "scope": "full",
      "reason_code": "capability_missing",
      "missing_capability_summary": "...",
      "fallback_content_scope": "...",
      "llm_fallback_allowed": true,
      "artifact_generation_allowed": false,
      "disclosure_required": true
    }
  }
}
```

### 8.3 Plan 与 Node 双层携带

采用双层结构：

- `WorkflowPlan.metadata.capability_missing_fallback` 保存全局 fallback 事实，供审计、事件和前端使用。
- 最终 `main_agent.respond` 节点 metadata 也携带同一结构或精简结构，供主代理 prompt 注入。

如果二者不一致，运行时必须选择更保守的披露策略：只要任一层要求披露，就必须披露。

### 8.4 Partial Fallback

Planner / Replanner 可以输出“部分能力 + fallback finalizer”。

示例：

- 文件读取能力存在；
- 田间图生成 Skill 不存在；
- Planner 先规划文件读取节点，再规划 final `main_agent.respond` 节点；
- final 节点 metadata 标记：田间图生成和文件下载部分由 LLM fallback 处理。

Partial fallback 披露必须包含：

- 已调用哪些能力；
- 缺少什么能力；
- 哪些部分由 LLM 生成。

`fallback_content_scope` 和 `attempted_capability_summary` 必填。

### 8.5 Replanner 语义

Replanner 发现后续需要新能力但 registry 中不存在时：

- 不得编造不存在的 capability 节点；
- 输出带 `capability_missing_fallback` 的 final `main_agent.respond`；
- 保留已完成能力结果作为上游上下文；
- final 回答必须区分已执行事实和 LLM fallback 内容。

## 9. Executor 弱兜底设计

主路径应在 Planner / Replanner 中完成。Executor 只做弱兜底，不做通用正则分类。

弱兜底触发条件：

1. 强制 Skill 缺失；
2. Planner / Replanner 明确要求 Skill fallback disclosure；
3. soft skill binding 不可用；
4. 用户点名不存在的 MCP 工具 / capability；
5. Plan metadata 已带 fallback，但 final node metadata 缺失，需要补传给 main_agent。

弱兜底行为：

- 不 hard-fail；
- 生成或补齐 `capability_missing_fallback`；
- 注入 main_agent prompt；
- 发出 `capability.missing_fallback` 事件；
- 走 LLM fallback；
- 任务最终 `completed`。

Executor 不应根据某些中文动词、某一次对话内容或正则词表自行判定“用户想调用能力”。这类判断属于 Planner/Replanner。

## 10. Runtime、事件与审计

### 10.1 新增事件

新增事件：`capability.missing_fallback`。

`visibility = EventVisibility.FRONTEND`。

说明：当前 `EventVisibility` 是单值枚举，只有 `frontend/internal/audit_only`。本 PRD 选择发一条 `frontend` 事件；该事件同时作为审计证据由事件存储和审计查询读取，不额外发重复 `audit_only` 事件。

建议 payload：

```json
{
  "capability_missing_fallback": {
    "enabled": true,
    "scope": "full|partial",
    "reason_code": "capability_missing",
    "missing_capability_summary": "...",
    "attempted_capability_summary": "...",
    "fallback_content_scope": "...",
    "llm_fallback_allowed": true,
    "artifact_generation_allowed": false,
    "disclosure_required": true
  }
}
```

运行时用途：

- 前端 Workbench 展示结构化提示；
- 审计记录为何未调用能力；
- 与 assistant message metadata 保持一致。

### 10.2 事件发送时机

- Planner 产出 fallback plan 后，任务执行开始前或 finalizer 节点执行前应发送一次事件。
- Partial fallback 可在已执行能力完成后、final `main_agent.respond` 执行前发送，以便 payload 包含 `attempted_capability_summary`。
- 同一 task 默认只发送一次 `capability.missing_fallback`。如 replanner 后才发现缺失，则发送时机以后发现为准。

### 10.3 Assistant message metadata 传播

最终 assistant 历史消息必须保留精简 `capability_missing_fallback`：

```json
{
  "capability_missing_fallback": {
    "enabled": true,
    "scope": "full",
    "reason_code": "capability_missing",
    "missing_capability_summary": "...",
    "fallback_content_scope": "...",
    "llm_fallback_allowed": true,
    "artifact_generation_allowed": false,
    "disclosure_required": true,
    "memory_context_used": true,
    "source_message_count": 3
  }
}
```

传播路径要求：

1. Plan metadata / final node metadata 产生 fallback 结构。
2. Runtime 发送 `capability.missing_fallback` 事件。
3. MainAgent prompt 收到 fallback 结构。
4. Final answer text 在保存前完成披露兜底。
5. `_persist_assistant_history_message()` 或等价保存路径写入 `Message.metadata.capability_missing_fallback`。
6. `GET /api/v1/conversations/{conversation_id}/messages` 返回 metadata。
7. 前端 history restore 根据 metadata 复原 notice。

## 11. MainAgent Prompt 约束

当 final `main_agent.respond` 收到 `capability_missing_fallback` 时，prompt 必须包含：

1. 本次没有调用匹配 Skill / 能力。
2. 原因是能力库没有匹配的可执行能力。
3. 允许基于通用 LLM 生成解释、草案、可复制内容或建议。
4. 不得声称已执行 Skill / 工具 / MCP。
5. 不得声称生成了平台 artifact、文件或下载链接。
6. 如果是 partial fallback，必须区分：
   - 已执行能力结果；
   - 缺失能力；
   - LLM fallback 生成的内容范围。
7. 如果 `available_capabilities` 为空或被截断，应如实说明能力库可用性，不夸大系统能力。

Prompt 约束不能替代后端正文后处理；它只是减少 LLM 违规概率。

## 12. 后端正文后处理

保存 assistant message 前检查：

- 如果存在 `capability_missing_fallback.disclosure_required=true`；
- 则最终正文必须以标准披露段或等价事实披露开头。

推荐实现策略：

- 简化为“只要 fallback metadata 存在，就统一 prepend 标准披露段”，并对已存在完全相同前缀做去重。
- 不依赖复杂中文语义检测判断 LLM 是否已经披露，避免漏判。

动态文案：

- full：说明本次未调用匹配 Skill/能力，原因是能力库没有匹配能力，以下为 LLM fallback 内容。
- partial：说明已调用哪些能力，缺少什么能力，哪些部分由 LLM 生成。

后处理还必须拦截或改写明显违规表述：

- “文件已生成”
- “请点击下载”
- “已调用某某 Skill”
- “后台正在生成 artifact”

如果 LLM 输出的是可复制 CSV/HTML/Markdown，应标注“这是可复制文本，不是系统生成的下载文件”。

## 13. 前端设计

### 13.1 Workbench

- 收到 `capability.missing_fallback` 时，在 Workbench 状态区展示能力缺失降级提示。
- 任务完成后 Workbench 正常停止。
- `loading_artifacts` 不算 active，不显示停止按钮。
- fallback notice 不使用全局 `severity` / `level` 字段。

### 13.2 Assistant 气泡

新增专用 `CapabilityFallbackNotice`：

- 不引入通用 severity / level。
- 视觉上按 warning 样式展示。
- 数据结构不叫 `level` 或 `severity`。
- 展示内容包括：
  - 未调用匹配能力；
  - full / partial 区分；
  - 缺少能力摘要；
  - partial 时显示已调用能力和 fallback 内容范围。

建议文案：

- full：`未调用匹配能力：能力库中没有匹配的可执行 Skill/能力，本次由通用 LLM 生成回答。`
- partial：`部分能力缺失：已调用部分能力；以下范围由通用 LLM 补充生成。`

### 13.3 历史恢复

- 从 message metadata 解析 `capability_missing_fallback`。
- 重新显示 `CapabilityFallbackNotice`。
- 正文中仍保留披露文字，确保复制、分享、API 消费不丢事实。
- 旧历史消息没有 metadata 时，不显示 notice，不做迁移。

### 13.4 SSE 与状态合并

- `capability.missing_fallback` 事件更新当前 assistant message 的 fallback notice 状态。
- 如果 final history reload 返回 metadata，应以 metadata 为准恢复 notice。
- 如果 SSE 事件收到但最终 message metadata 缺失，前端本轮可以保留运行态 notice；刷新后消失视为后端 bug，应由测试覆盖。

## 14. 边界场景与失败模式

| 场景 | 期望行为 |
| --- | --- |
| 只有 `main_agent.respond` 可用 | full fallback；披露“当前能力库没有可用业务能力，本次仅使用通用 LLM 回答”。 |
| 用户普通闲聊 | 正常 `main_agent.respond`；不显示 fallback notice。 |
| 用户点名不存在 Skill | full fallback；`reason_code=skill_missing`；披露没有调用该 Skill。 |
| 用户点名不存在 MCP 工具 | full fallback；`reason_code=mcp_missing`；披露没有调用该 MCP 工具。 |
| 部分能力存在 | 先调用可用能力，再 partial fallback；披露已执行和未执行边界。 |
| 能力存在但参数不足 | 走 interrupt / 参数补全；不标记 capability missing。 |
| 能力存在但执行失败 | 走执行失败/重试/错误恢复；不标记 capability missing。 |
| LLM 忘记披露 | 后端 prepend 标准披露段。 |
| LLM 声称生成下载文件 | 后端改写或阻断违规表述，最终只保留可复制文本说明。 |
| Planner 输出非法 fallback metadata | repair prompt 修复；仍失败则按 planner 错误处理，不假装执行。 |
| SSE 中断后刷新 | history metadata 恢复 notice。 |
| 旧历史无 metadata | 不显示 notice，不报错。 |

## 15. 需求矩阵

| 编号 | 模块 | 需求 | 验收方式 |
| --- | --- | --- | --- |
| R1 | Planner | 可输出普通 `main_agent.respond` 与 fallback `main_agent.respond`，二者通过 metadata 区分。 | Planner 单测。 |
| R2 | Planner schema | 顶层和 node metadata 支持 `capability_missing_fallback`。 | schema/parser 单测。 |
| R3 | Replanner | 后续发现能力缺失时输出 final fallback node，不编造能力。 | Replanner 单测/集成测试。 |
| R4 | Runtime event | fallback task 发出一次 `capability.missing_fallback` frontend 事件。 | runtime event 测试。 |
| R5 | MainAgent prompt | fallback metadata 注入 prompt，约束不得假装调用能力。 | prompt builder 单测。 |
| R6 | 后处理 | fallback 正文强制 prepend 披露，拦截下载/artifact 误导。 | executor/runtime 单测。 |
| R7 | Assistant history | 完成后 assistant message metadata 保留 fallback 结构。 | API/history 测试。 |
| R8 | Frontend runtime | SSE 收到事件后显示 `CapabilityFallbackNotice`。 | frontend event reducer/component 测试。 |
| R9 | Frontend history | 刷新历史后仍显示 notice。 | App/history 测试。 |
| R10 | Artifact 边界 | fallback 不生成用户可下载平台 artifact。 | 后端 artifact/API 测试。 |
| R11 | 普通闲聊 | 不误标能力缺失。 | planner/main_agent 测试。 |
| R12 | Partial fallback | 展示已调用能力、缺失能力和 LLM 范围。 | 集成测试。 |

## 16. 测试计划

### 16.1 后端单元测试

- `planner_contract`：
  - schema 接受顶层 `metadata.capability_missing_fallback`；
  - schema 接受 node `metadata.capability_missing_fallback`；
  - schema 拒绝未知 capability id；
  - repair prompt 保留 fallback metadata。
- `llm_workflow_provider`：
  - full fallback plan metadata 传递；
  - partial fallback final node metadata 传递；
  - 普通 `main_agent.respond` 不带 fallback metadata。
- `main_agent`：
  - prompt 注入 fallback 结构；
  - full/partial 文案不同；
  - artifact/download 禁止语义进入 prompt。
- Runtime：
  - `capability.missing_fallback` 只发一次；
  - event visibility 为 `frontend`；
  - task 最终 `completed`；
  - assistant message metadata 持久化；
  - 后端 prepend 披露。

### 16.2 前端测试

- SSE event reducer 识别 `capability.missing_fallback`。
- Workbench 状态区显示 fallback notice。
- `MessageBubble` 渲染 `CapabilityFallbackNotice`。
- `messageFromHistory` 从 metadata 恢复 notice。
- 旧历史无 metadata 不显示 notice。
- `loading_artifacts` 不触发 active/停止按钮。

### 16.3 集成 / 手工验收

1. 清空业务 Skill，仅保留 main agent，发送“帮我生成一个田间图文件”。
   - 预期：Workbench 停止，任务 completed，正文和 notice 均披露未调用能力，无下载 artifact。
2. 安装一个可读取文件的能力，但无田间图生成能力，发送“读取文件并生成田间图”。
   - 预期：文件读取执行，田间图部分 partial fallback。
3. 发送“解释一下什么是随机区组设计”。
   - 预期：普通回答，无 fallback notice。
4. 点名不存在的 `$unknown-skill` 或 MCP 工具。
   - 预期：LLM fallback + 披露。
5. 刷新页面或重新打开会话。
   - 预期：assistant 气泡仍显示 notice。

## 17. Rollout、兼容性与迁移

- 不需要数据库迁移：message metadata 已存在。
- 旧历史消息没有 `capability_missing_fallback` 时不显示 notice。
- 旧前端如果不识别 `capability.missing_fallback`，仍能看到 assistant 正文披露。
- 新前端必须容忍 metadata 缺字段或字段类型异常，按“不显示 notice，但保留正文”降级。
- 新事件名应加入前端事件类型解析和后端事件测试，未知事件继续忽略。

## 18. 安全、隐私与审计

- metadata 不保存完整历史正文、完整 prompt、用户上传文件内容或内部能力实现细节。
- prompt 可使用 history context，但保存 metadata 只保留引用和计数，例如 `source_message_count`、可选 `source_message_ids`。
- available capability 摘要只来自 public contract。
- 审计必须能回答：为什么没有调用能力、缺少什么能力、是否使用 LLM fallback、是否允许生成 artifact。
- 不得在披露中暴露内部路径、handler 名称、runtime 类型、sandbox 实现或 secret。

## 19. 当前实现差距

当前工作区存在未提交草稿改动，其中部分不符合本 PRD，不能直接视为最终实现：

- 曾尝试 hard-fail `required_skill_missing`，但本 PRD 要求 fallback task `completed`。
- 曾尝试在 executor 中用正则判断执行意图，但本 PRD 要求主路径放在 Planner / Replanner。
- 已有主代理身份 prompt 去硬编码能力的方向与本 PRD 兼容。
- `loading_artifacts` 不算 active 的方向与本 PRD 兼容。
- 需要补齐 planner schema、repair prompt、metadata 解析、`capability.missing_fallback` 事件、message metadata、prompt 注入和正文后处理。

实施前应先整理或回滚不符合本 PRD 的草稿改动，然后按本 PRD 重新实现。

## 20. 验收标准

1. 无匹配业务能力时，Planner 输出 `capability_missing_fallback`，任务最终 `completed`。
2. 普通闲聊、解释、总结不会被污染为 capability missing fallback。
3. LLM prompt 收到用户原始请求、历史上下文和可用业务能力列表摘要。
4. assistant 正文明确披露未调用匹配 Skill / 能力。
5. 如果 LLM 漏披露，后端自动 prepend 标准披露段。
6. 前端运行时显示 `CapabilityFallbackNotice`。
7. 刷新历史后仍显示 `CapabilityFallbackNotice`。
8. 没有生成用户可下载平台 artifact 或下载链接。
9. Partial fallback 能区分已调用能力、缺失能力和 LLM 生成范围。
10. 点名不存在 Skill / MCP capability 时，也走 LLM fallback + 披露。
11. `capability.missing_fallback` 事件 `visibility=frontend`，并可作为审计证据查询。
12. assistant message metadata 保留精简 `capability_missing_fallback`。
13. Planner schema、parser、repair prompt 均支持 fallback metadata。
14. 旧历史无 metadata 时前端正常展示普通消息，不报错。


## 21. Phase 拆分与实施顺序

本 PRD 保留为能力缺失 LLM fallback 披露的产品语义总纲和字段事实源。实施层面拆分到 `docs/prd/backend/capability-missing-fallback/README.md`，按阶段推进、验证和回滚。

| Phase | 阶段 PRD | 目标 | 可独立验收边界 |
| --- | --- | --- | --- |
| Phase 0 | `docs/prd/backend/capability-missing-fallback/01-阶段零-现状清理与基线锁定PRD.md` | 清理或回滚与本 PRD 冲突的 hard-fail、Executor 正则意图判断等草稿方向，锁定普通 main agent、Workbench 停止和 history 基线。 | 不新增 fallback contract；确认后续实施不建立在冲突草稿上。 |
| Phase 1 | `docs/prd/backend/capability-missing-fallback/02-阶段一-PlanMetadata契约PRD.md` | 扩展 Planner schema、repair prompt、parser 和 plan/node metadata 传播，使 `capability_missing_fallback` 成为合法收敛 contract。 | 合法 fallback metadata 不被 schema/repair/parser 丢弃，未知字段仍收敛。 |
| Phase 2 | `docs/prd/backend/capability-missing-fallback/03-阶段二-后端FullFallback闭环PRD.md` | 完成 full fallback 后端交付闭环：Planner 区分普通 respond 与 fallback respond，Runtime 事件、MainAgent prompt、正文后处理、history metadata 和 artifact 禁止边界闭环。 | 无匹配业务能力时任务 completed、正文披露、history metadata 保留、无下载 artifact。 |
| Phase 3 | `docs/prd/backend/capability-missing-fallback/04-阶段三-前端Notice与历史恢复PRD.md` | 前端消费 `capability.missing_fallback` 和 history metadata，在 Workbench 与 assistant 气泡展示 `CapabilityFallbackNotice`。 | 运行态和刷新历史后均可见 notice；旧历史和异常 metadata 兼容。 |
| Phase 4 | `docs/prd/backend/capability-missing-fallback/05-阶段四-PartialFallback与Replanner审计PRD.md` | 支持 partial fallback、Replanner 后发现能力缺失、事件去重和审计一致性。 | 已执行能力、缺失能力和 LLM fallback 范围在正文、metadata、notice、审计中一致。 |

推荐执行顺序：

```text
Phase 0 现状清理 / 基线测试
  -> Phase 1 Planner schema / parser / metadata contract
  -> Phase 2 后端 full fallback completed 闭环
  -> Phase 3 前端运行态 notice / history 恢复
  -> Phase 4 partial fallback / Replanner / 审计硬化
```

Phase 2 是后端 full fallback 的交付门槛；Phase 3 之前不得宣称结构化前端 notice 完成；Phase 4 之前不得宣称 partial fallback 与 Replanner 后发现能力缺失完整支持。每个 Phase 文件都应作为交付级代码设计使用，不以缩减版或临时实现作为验收口径。
