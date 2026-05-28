# Skill 软绑定设计

日期：2026-05-28  
状态：已通过方向确认，待用户复核  
范围：前端业务对话台 slash Skill 入口、后端消息提交路由、主代理 Skill 语境注入、内部 Skill 执行编排。  
替代方案：废弃此前 `/skill-help` 只读问答方案；Skill 说明、参数解释与执行判断统一收敛到 `/skill-name` 软绑定。

## 1. 背景

当前系统已经具备：

- public `skill.*` capability 注册与 `/api/v1/capabilities` 暴露。
- 前端 slash picker：用户输入 `/field-design` 可选择 Skill。
- API `routing_mode=force_capability + capability_id=skill.*` 的硬执行入口。
- `WorkflowRouter` 对 `skill.*` 的强制 SkillWorkflowProvider 路由。
- `main_agent.respond` 可注入匹配 Skill 的 `SKILL.md` 正文，并可自动运行脚本。
- Skill 缺参时可通过 manifest / structured `missing_input` 生成 open interrupt。

问题是：硬执行 slash 会把“用户点名某个 Skill”机械理解成“立即执行该 Skill”。这不符合用户真实意图。例如：

```text
/field-design hyb_check 怎么填？
```

用户是在询问字段格式，不应执行试验设计脚本。另一方面：

```text
/field-design 用这个 CSV 做 RCBD，3 个重复
```

用户执行意图明确，若输入齐全，系统应直接运行 Skill，不应再要求二次确认。

因此需要把 `/skill-name` 从“硬执行”改为“软绑定”：用户把本轮问题绑定到某个 Skill 的公开语境下，由主代理自主判断回答、追问或执行。

## 2. 核心决策

### 2.1 `/skill-name` 是软绑定，不是硬执行

`/field-design ...` 的语义是：

> 将本轮用户问题绑定到 `field-design` 的公开 Skill 语境，让主代理基于用户内容判断下一步。

主代理可作三类决策：

1. **answer**：回答 Skill 用法、输入格式、参数含义、示例或适用性，不执行 Skill。
2. **ask / interrupt**：识别为执行意图，但缺少必要输入；进入标准 Skill 缺参 interrupt 链路。
3. **execute**：执行请求明确且输入齐全；系统内部直接执行目标 Skill。

### 2.2 外部硬执行接口下线

客户端、前端、第三方调用方不再允许直接提交：

```json
{
  "routing_mode": "force_capability",
  "capability_id": "skill.field_design"
}
```

这类请求必须 fail-closed，提示调用方改用软绑定。原因是：不看文档的调用方继续走旧硬执行接口，会绕过主代理判断并重新制造误执行风险。

新的边界：

- **外部请求层**：`skill.*` 不再是可直接请求 capability。
- **内部编排层**：主代理判断为执行后，系统仍可生成内部 `skill.*` 节点执行真实 Skill。

### 2.3 不再需要 `/skill-help`

独立 `/skill-help` capability 不再保留。用户可以直接通过 `/skill-name` 询问：

```text
/field-design hyb_check 怎么填？
/ocr 支持什么文件？
/rice-genie 输入的 gene_check 数据怎么构建？
```

这些都走软绑定 answer 路径。

### 2.4 主代理只看 public skill profile

软绑定路径不得把 raw `SKILL.md` body 直接注入 LLM。系统必须构造安全的 `public skill profile`，只包含用户可见、业务层信息。

允许披露：

- `capability_id`
- `name`
- `display_name`
- `description`
- 业务用途与适用场景
- 参数名、类型、required、default、enum、aliases、patterns 的用户可见摘要
- 输入数据格式、字段含义、示例值、可接受值
- 输出内容说明
- 用户操作建议

禁止披露：

- `source_path`
- `scripts[].path`
- wrapper / handler / module / function 名称
- Python / Rscript / sidecar / platform service 内部实现细节
- 内部目录结构、内部 JSON 临时字段、私有 runtime metadata
- DB、LLM provider、secret、token、内网地址、socket path、endpoint
- 完整上传文件内容或原始敏感数据

## 3. 用户体验

### 3.1 解释型输入

输入：

```text
/field-design hyb_check 怎么填？
```

前端行为：

- picker 显示并选择 `试验设计智能体`。
- composer 可显示 Skill badge，但提交正文不包含 slash 前缀。

后端行为：

- 进入 `main_agent.respond`。
- 注入 `field-design` public profile。
- 主代理判断为 answer。
- 返回自然语言解释，不产生 Skill 节点、不运行脚本、不创建 interrupt。

### 3.2 缺参执行型输入

输入：

```text
/field-design 帮我做 RCBD
```

行为：

- 主代理判断用户想执行试验设计。
- 系统内部触发 `skill.field_design`。
- Skill 输入解析发现缺少材料文件和 `blocks`。
- 后端按 manifest / structured `missing_input` 生成 open interrupt。
- 前端显示具体补充卡片，而不是泛化“正在等待任务给出补充信息”。

### 3.3 输入齐全执行型输入

输入：

```text
/field-design 用这个 CSV 做 RCBD，3 个重复
```

且本轮已上传材料 CSV。

行为：

- 主代理判断执行意图明确。
- 系统内部直接执行 `skill.field_design`。
- 若该 Skill `answer_mode=requires_finalizer`，Skill 完成后再由 `main_agent.respond` 汇总最终回答。
- 不二次确认。

### 3.4 普通对话不受影响

未使用 slash 的普通消息继续走现有自动规划 / 主代理路径。是否自动匹配 Skill 由现有自动匹配和 LLM planner 机制决定；本设计只改变用户显式点名 Skill 的入口语义。

## 4. 请求契约

### 4.1 前端 slash 软绑定提交

前端用户选择 `/field-design` 后，不再提交 `capability_id=skill.field_design`，而提交：

```json
{
  "content": "hyb_check 怎么填？",
  "routing_mode": "force_capability",
  "capability_id": "main_agent.respond",
  "metadata": {
    "slash_command": "/field-design",
    "soft_skill_binding": {
      "capability_id": "skill.field_design",
      "command": "/field-design"
    }
  }
}
```

约束：

- `content` 是去掉 slash 前缀后的用户真实问题。
- `capability_id` 固定为 `main_agent.respond`，表示强制主代理做判断。
- `metadata.soft_skill_binding.capability_id` 记录用户点名的 Skill。
- 上传文件仍通过现有 `metadata.upload_ids` 传递，不得被 soft binding 覆盖。

### 4.2 API 软绑定提交

API 调用方若想点名 Skill，也必须使用同样的 soft binding metadata。系统可以接受 `routing_mode=auto` 或 `force_capability=main_agent.respond`，但最终都必须进入主代理判断。

推荐显式形态：

```json
{
  "content": "用这个 CSV 做 RCBD，3 个重复",
  "routing_mode": "force_capability",
  "capability_id": "main_agent.respond",
  "metadata": {
    "soft_skill_binding": {
      "capability_id": "skill.field_design"
    }
  }
}
```

### 4.3 禁止外部硬执行

以下请求在 API 边界必须拒绝：

```json
{
  "routing_mode": "force_capability",
  "capability_id": "skill.field_design"
}
```

建议错误：

```json
{
  "error": {
    "code": "direct_skill_execution_disabled",
    "message": "Direct skill capability execution is no longer supported. Use soft skill binding through main_agent.respond."
  }
}
```

该限制只作用于外部 submit-message 请求。系统内部 planner / replanner / workflow expander 仍可产生 `skill.*` 节点。

## 5. 后端架构

### 5.1 新增 soft binding 元数据模型

从 `request.metadata.soft_skill_binding` 中解析：

```json
{
  "capability_id": "skill.field_design",
  "command": "/field-design"
}
```

校验规则：

- `capability_id` 必须存在于当前 active skill capability registry。
- 必须是 public Skill。
- 必须绑定当前 `skill_bundle_revision`，避免长任务中 Skill registry 更新造成漂移。
- 用户 metadata 不能伪造内部执行字段，例如 `forced_skill_name`、`macro_source`、`skill_execution_mode`。

### 5.2 WorkflowRouter 行为

外部 submit 后的 plan 不再直接进入 SkillWorkflowProvider。软绑定请求的初始 plan 必须是：

```text
main_agent.respond
```

节点 metadata 包含：

```json
{
  "soft_skill_binding": {
    "capability_id": "skill.field_design",
    "skill_bundle_revision": "..."
  },
  "soft_skill_binding_source": "slash_command"
}
```

### 5.3 MainAgentWorkflowProvider replan budget

软绑定初始主代理节点需要允许一次内部重编排：

```text
max_replans = 1
max_dynamic_nodes >= target Skill macro expansion nodes
```

普通 `main_agent.respond` 保持 `max_replans=0`，避免非软绑定对话额外引入执行分支。

### 5.4 主代理执行器职责

`main_agent.respond` 在检测到 `soft_skill_binding` 时：

1. 解析目标 Skill manifest。
2. 构造 public skill profile。
3. 构造软绑定判断 prompt。
4. 让 LLM 先输出结构化 decision。
5. 根据 decision 走 answer 或 execute 信号。

主代理不得直接调用脚本，不得直接实例化 Skill executor。

## 6. 主代理判断协议

### 6.1 Decision JSON

LLM 判断结果必须先归一为以下结构：

```json
{
  "decision": "answer | execute",
  "confidence": "low | medium | high",
  "reason_code": "user_asks_usage | explicit_execution_request | missing_intent | unsafe_or_unclear",
  "answer_intent": "input_format | parameter_meaning | usage_example | capability_fit | other",
  "target_capability_id": "skill.field_design"
}
```

规则：

- JSON 无效、`confidence=low`、`target_capability_id` 不等于绑定 Skill 时，一律降级为 `answer`。
- 只有 `decision=execute` 且 `confidence` 为 `medium/high` 时，才允许触发内部执行。
- `execute` 只表示“用户想执行该 Skill”；输入是否齐全仍由 Skill manifest / input resolver / script `missing_input` 负责确定。
- 对询问“怎么填”“字段是什么”“示例是什么”“是否支持”等，必须选择 `answer`。

### 6.2 Answer 路径

answer 路径使用同一个主代理流式回答机制，但 prompt 只能包含 public profile。输出应：

- 直接回答用户问题。
- 可以给出数据格式和字段值示例。
- 可以说明若要执行还需要哪些用户可见输入。
- 不得暴露内部实现细节。
- 不得声称已经运行 Skill。

### 6.3 Execute 路径

execute 路径不直接输出用户最终答案，而在 `output_payload` 中返回系统内部信号：

```json
{
  "soft_skill_decision": {
    "decision": "execute",
    "target_capability_id": "skill.field_design",
    "reason_code": "explicit_execution_request",
    "confidence": "high"
  },
  "satisfaction": {
    "satisfied": false,
    "replan_recommended": true,
    "reason_code": "soft_skill_execute"
  }
}
```

该信号只用于 orchestration runtime replanner；前端不应把它当最终回答展示。

## 7. 内部执行编排

### 7.1 Deterministic soft skill replanner

新增确定性的 runtime replanner，优先处理 `soft_skill_decision.decision=execute`：

1. 验证目标 capability 等于 soft binding capability。
2. 验证目标 Skill 在当前 revision 仍存在且 active。
3. 使用现有 Skill macro provider 展开 `skill.*`。
4. 把原始用户问题、上传 artifact metadata、skill bundle revision 传给 Skill 节点。
5. 如 Skill 需要 finalizer，保留现有 finalizer 逻辑。

该 replanner 不让 LLM 输出任意 DAG。LLM 只给“是否执行绑定 Skill”的信号；真正 DAG 由系统确定性生成。

### 7.2 与现有 LLM runtime replanner 的关系

顺序建议：

1. SoftSkillBindingReplanner
2. 现有 MainAgentRuntimeReplanner

原因：软绑定 execute 是明确用户动作的内部信号，不应被通用重编排逻辑覆盖。

### 7.3 Interrupt 兼容

执行后缺参时继续复用现有链路：

- manifest / `parameters.required` pre-run 缺参 → 系统合成 open interrupt。
- 脚本运行后发现条件缺参 → structured `missing_input` → 系统合成 open interrupt。
- 无 open interrupt 的 pending Skill context 仍只是兼容兜底，应视为 Skill contract 缺陷。

本设计不改变 interrupt 结构，但要求软绑定 execute 后仍能产出具体补充卡片。

## 8. Public Skill Profile 构造

### 8.1 数据源

优先从已解析的 `SkillManifest` 生成，不进行临时磁盘扫描。使用当前 request 的 `skill_bundle_revision` 对应 catalog。

建议结构：

```json
{
  "capability_id": "skill.field_design",
  "name": "field-design",
  "display_name": "试验设计智能体",
  "description": "...",
  "triggers": ["试验设计", "RCBD"],
  "parameters": [
    {
      "name": "material_data",
      "type": "artifact",
      "required": true,
      "source": "artifact",
      "aliases": ["材料清单", "材料文件"],
      "user_description": "试验材料清单，通常由上传 CSV/JSON 提供。"
    }
  ],
  "outputs": ["answer", "CSV fieldbook", "HTML layout preview"],
  "public_usage_notes": "用户可见用法摘要。"
}
```

### 8.2 `SKILL.md` 写作要求

`Skill构建指南.md` 需要补充：

- 项目级 Skill 必须在 `SKILL.md` 中提供足够的用户可见 usage 信息，供软绑定回答使用。
- 需要解释输入数据如何构建、字段含义、示例值、必填/可选参数和输出。
- 不得把内部脚本路径、wrapper、Rscript、handler、sidecar、secret、DB、provider 细节写进用户可见说明。
- 如果 `SKILL.md` 中现有正文混有内部执行说明，public profile builder 必须过滤；长期应把正文拆成用户可见说明和内部维护说明，或给 public usage 增加专门 frontmatter / section。

### 8.3 防泄漏过滤

public profile builder 必须有 allowlist 字段策略，不使用 blacklist 作为唯一手段。测试需断言以下词/字段不出现在 profile：

- `source_path`
- `scripts`
- `scripts/run_*.py`
- `Rscript`
- `wrapper`
- `platform_service`
- `handler`
- `sidecar endpoint`
- 绝对路径、URL secret、token、DB DSN

## 9. 前端改造

### 9.1 Slash command 派生保持不变

`deriveSlashCommands()` 仍从 public active `skill.*` capability 派生 `/field-design` 等命令。

### 9.2 Submit intent 改造

`slashSubmitIntent()` 的 ready intent 不再返回“强制 capability=skill.*”，而返回 soft binding metadata：

```ts
{
  kind: 'ready',
  capabilityId: 'main_agent.respond',
  content: cleanedContent,
  metadata: {
    forced_by_slash_command: true,
    slash_command: '/field-design',
    soft_skill_binding: {
      capability_id: 'skill.field_design',
      command: '/field-design'
    }
  }
}
```

### 9.3 Pending interrupt 下的行为

如果当前 conversation 正在等待 open interrupt：

- 用户普通输入继续作为 interrupt answer。
- 用户输入新的 `/skill-name` 默认仍视为新软绑定请求，并应先走现有 conversation guard / pending context supersede 规则。
- 若当前系统已经禁止 pending 期间新任务，则前端按现有规则阻止；本设计不引入 `/skill-help` 特例。

## 10. 后端 API 兼容与迁移

### 10.1 Fail-closed 迁移

在一个版本内直接拒绝外部 `force_capability=skill.*`，不做静默兼容。这样可以尽快暴露仍在调用老接口的客户端。

### 10.2 错误可观测性

拒绝时记录 audit-only 事件或结构化日志：

```json
{
  "event_type": "skill.direct_execution_rejected",
  "capability_id": "skill.field_design",
  "reason": "external_direct_skill_execution_disabled"
}
```

日志不得包含上传文件正文、secret 或完整用户敏感输入。

### 10.3 API 文档同步

`docs/api/api-doc.html` 需要同步：

- `capability_id=skill.*` 不再是 submit-message 可请求值。
- 点名 Skill 使用 `metadata.soft_skill_binding`。
- `/api/v1/capabilities` 仍暴露 Skill 列表给前端 picker 和 API 客户端发现。

## 11. 测试矩阵

### 后端 API

- `force_capability + capability_id=skill.*` 被拒绝，错误码为 `direct_skill_execution_disabled`。
- `force_capability + capability_id=main_agent.respond + metadata.soft_skill_binding` 被接受。
- soft binding capability 不存在、非 public、非 skill 时 fail-closed。
- 用户 metadata 中伪造 `forced_skill_name`、`macro_source` 等内部字段不会生效。

### 主代理 / profile

- soft binding prompt 注入 public profile，不注入 raw `SKILL.md` body。
- profile 含参数名、类型、required/default/enum/aliases、数据格式摘要。
- profile 不含 `source_path`、script path、wrapper、Rscript、handler、platform service、sidecar、secret。
- 询问型输入返回 answer，不产生 `soft_skill_decision.execute`。
- JSON 判断失败或低置信时降级 answer。

### 编排 / replanner

- execute decision 触发 deterministic replanner 展开绑定 Skill。
- replanner 拒绝目标 capability 与绑定 capability 不一致的 decision。
- execute 后输入齐全时 Skill 运行成功并按 answer mode finalizer。
- execute 后缺参时生成 open interrupt，前端可显示具体 question / required_fields。
- 普通非 soft binding 的 `main_agent.respond` 不获得额外 replan budget。

### 前端

- `/field-design xxx` 提交 `main_agent.respond` + `soft_skill_binding`，不提交 `skill.field_design`。
- 直接 slash parse、picker 选择、上传文件 metadata merge 均保留。
- unknown slash 仍阻断提交。
- Slash menu 不展示 `source_path` 或内部路径。

### 回归

- 非 slash 普通消息行为不变。
- 自动 LLM planner 仍可在普通对话中规划 public Skill。
- Skill interrupt 卡片、上传续答、pending Skill context 现有回归继续通过。

## 12. 实施检查点建议

1. **CP-0 文档与契约测试**：写 API / frontend / profile fail tests。
2. **CP-1 API 边界**：拒绝外部 direct `skill.*`，接受 soft binding metadata。
3. **CP-2 前端 slash submit**：slash ready intent 改为 main_agent soft binding。
4. **CP-3 public profile builder**：从 SkillManifest 生成 allowlisted profile。
5. **CP-4 主代理判断协议**：软绑定 prompt、decision parse、answer fallback。
6. **CP-5 deterministic replanner**：execute 信号展开目标 Skill。
7. **CP-6 interrupt / finalizer 回归**：确保缺参卡片和成功执行链路一致。
8. **CP-7 API 文档与 Skill 构建指南**：同步 soft binding 和 public usage 要求。
9. **CP-8 全量验证**：后端分层 unittest、前端 test/build、License Requirement 记录。

## 13. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| LLM 误把询问判断为执行 | 低置信/JSON 无效默认 answer；execute 需要绑定 capability 一致且 medium/high confidence；测试覆盖典型询问。 |
| raw SKILL.md 泄漏内部实现 | 软绑定只用 public profile；profile builder 采用 allowlist；测试敏感词和路径。 |
| 老客户端继续硬执行 | API 直接 fail-closed，不静默兼容；记录 rejected 事件便于排查。 |
| execute 后缺参仍显示泛化等待 | 继续依赖 manifest / structured `missing_input` 合成 open interrupt；Skill 构建指南和 Skill contract 测试保证。 |
| 主代理 answer 后无法再执行 | 用户可继续发送明确执行请求；若本轮判断为 execute 则 deterministic replanner 接管。 |
| 内部 planner 被外部绕过 | 区分外部 submit validation 与内部 WorkflowPlan；只在 API 边界禁止 direct skill request。 |

## 14. 验收标准

- 用户使用 `/field-design hyb_check 怎么填？` 时，系统只解释字段/格式，不执行 Skill。
- 用户使用 `/field-design 用这个 CSV 做 RCBD，3 个重复` 且输入齐全时，系统直接执行 Skill，不二次确认。
- 用户使用 `/field-design 帮我做 RCBD` 且缺输入时，前端显示具体缺参 interrupt 卡片。
- 外部 API 直接请求 `capability_id=skill.*` 被拒绝。
- 软绑定 LLM prompt 不包含 raw `SKILL.md` body 或内部实现细节。
- 普通非 slash 对话、自动规划、多 Skill DAG、Skill finalizer、interrupt 续答不回归。
