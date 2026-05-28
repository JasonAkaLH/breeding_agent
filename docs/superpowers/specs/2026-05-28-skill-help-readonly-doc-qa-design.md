# Skill Help 只读文档问答设计

日期：2026-05-28  
状态：已通过设计评审，待实施计划  
范围：为主代理增加按需读取项目 Skill `SKILL.md` 的只读问答能力，支持显式 `/skill-help` 与高置信自然语言触发。

## 背景

当前项目 Skill 由 `SkillCatalog` 加载，`SKILL.md` 已解析为 `SkillManifest`，其中包含 Skill 名称、描述、触发语、输入输出、参数、metadata、正文与来源路径。现有主代理只会在 Skill 匹配或强制执行链路中把匹配 Skill 的正文注入 LLM prompt；这适合“执行 Skill”，但不适合用户只想了解某个 Skill 的用法、字段含义、输入格式、限制或示例。

需要新增一个能力：当用户明确询问 Skill 文档时，系统可以读取对应 `SKILL.md` 并基于文档回答，但不能执行目标 Skill，也不能暴露 Skill 内部代码结构。

## 目标

- 支持用户通过 `/skill-help` 显式查看 Skill 用法或字段说明。
- 支持高置信自然语言问题自动进入 Skill 文档问答，例如“field-design 的 hyb_check 有什么要求？”。
- 只在触发 Skill 文档问答时读取目标 Skill 的完整 `SKILL.md`；普通对话不把所有 Skill 文档发送给 LLM。
- 回答必须严格基于对应 `SKILL.md`；文档未说明时明确回答“该 Skill 文档未说明”。
- 不执行目标 Skill，不产生业务 artifact，不触发 interrupt。
- 不向用户暴露内部代码结构、脚本路径、handler/module/runtime、source_path、本地路径或服务 allowlist 等内部实现细节。

## 非目标

- 不实现通用知识库或跨文档 RAG。
- 不读取 Skill 脚本、源码、测试或运行时配置来补充答案。
- 不用 `/skill-help` 触发业务执行。
- 不让前端承担 Skill 文档解析、字段解释或权限判断。
- 不保证能回答 `SKILL.md` 未写明的业务规则。

## 推荐方案

采用独立只读内置 capability：`skill_help.respond`。

该 capability 与普通项目 Skill 执行链路分离：它读取当前激活的 `SkillCatalog`，解析用户指定的 Skill，构造安全的 Skill 文档视图，调用 LLM 生成面向用户的说明。它永远不调用 `SkillExecutor`，也不触发目标 Skill 的脚本、artifact、upload interrupt 或等待补充信息链路。

已拒绝方案：

- 复用 `main_agent.respond` 并加入 `skill_help_mode`：会把主代理业务对话、Skill 匹配和只读文档问答耦合在一起，难以保证“不执行”。
- 把 `skill-help` 做成普通项目 Skill：语义上会形成 Skill 解释 Skill 的循环，也容易误入现有 Skill 匹配/执行链路。

## 路由入口

### 显式命令

支持以下形态：

```text
/skill-help field-design hyb_check 有什么要求
/skill-help skill.field_design hyb_check 有什么要求
/skill-help 试验设计智能体 输入数据应该是什么格式
```

规则：

- `/skill-help` 优先级最高；一旦识别，不进入普通 Skill 自动匹配，也不强制调用目标业务 Skill。
- 命令后第一个参数可为 capability id、Skill name 或 display name。
- 命令后没有具体问题时，返回该 Skill 的概览：用途、输入要求、主要参数/字段、输出、限制和下一步建议。
- 未知 Skill 返回可见候选列表。
- 多个 Skill 匹配时返回澄清问题和候选列表，不调用 LLM。

### 自然语言触发

高置信触发需同时满足：

1. 能唯一识别一个 Skill；
2. 用户意图是文档帮助、用法、参数、字段含义、输入输出、限制、数据格式或示例；
3. 用户没有要求执行业务动作。

示例：

- “field-design 的 hyb_check 有什么要求？”→ `skill_help.respond`
- “试验设计智能体需要什么表头？”→ `skill_help.respond`
- “帮我跑 field-design 做 RCBD 设计”→ 业务 Skill 执行链路，不是 help
- “field-design 怎么用，顺便帮我跑一下”→ 先澄清用户要查看说明还是执行 Skill

推荐在 workflow provider / routing 层实现该识别：显式 `/skill-help` 用确定性解析，自然语言使用轻量 deterministic policy 或 planner policy，最终生成单节点 `skill_help.respond` 计划。

## Capability 输入契约

`skill_help.respond` 接收结构化 payload：

```json
{
  "user_message": "field-design 的 hyb_check 有什么要求？",
  "skill_ref": "field-design",
  "skill_question": "hyb_check 有什么要求？",
  "source": "slash_command"
}
```

字段说明：

- `user_message`：原始用户消息。
- `skill_ref`：用户指定或路由器识别出的 Skill 引用。
- `skill_question`：去掉命令和 Skill 引用后的具体问题；可为空。
- `source`：`slash_command` 或 `auto_detected`，用于审计和 prompt 约束，不影响读取范围。

## Skill 解析规则

从当前激活的 `SkillCatalog` 解析 Skill，避免临时扫描磁盘导致 revision 不一致。解析顺序：

1. capability id 精确匹配，例如 `skill.field_design`；
2. Skill name 精确匹配，例如 `field-design`；
3. display name 精确匹配，例如 `试验设计智能体`；
4. normalized loose match：大小写不敏感，去除空格、下划线和连字符，例如 `field_design` 近似 `field-design`。

解析结果必须唯一。多匹配或无匹配均不调用 LLM。

## Safe Skill 文档视图

发送给 LLM 的内容不是原始 manifest 全量对象，而是安全视图。

允许包含：

- Skill name；
- display name；
- description；
- triggers；
- 用户可见输入字段：name、type、required、default、enum、aliases、description；
- 用户可见输出字段；
- `SKILL.md` 正文；
- 用户可见参数说明与业务约束。

必须剔除：

- `source_path` 和任何本地文件路径；
- script path、handler、module、factory、runtime、execution mode；
- service allowlist、native/Rust/sidecar 细节；
- secret-like key、token、环境变量值；
- 内部命令、脚本调用方式或测试结构。

如果 `SKILL.md` 正文中含内部实现片段，capability 应在 safe view 构造或 prompt 中要求只提炼用户可见语义，不复述内部命令、路径或模块名。

## LLM 回答约束

Prompt 必须包含以下硬约束：

- 只能回答目标 Skill 的文档问题。
- 必须严格基于提供的 Skill 文档内容。
- 文档没有说明时回答“该 Skill 文档未说明”。
- 不要建议用户运行脚本、打开本地文件或查看内部路径。
- 不暴露内部代码结构、脚本、handler/module/runtime、source_path、服务 allowlist。
- 答案只包含用户可见的用法、输入要求、参数含义、数据格式、约束、示例模板和下一步建议。
- 答案末尾只展示用户可见来源，例如：`来源：试验设计智能体 Skill 文档`。

输出为普通 assistant 文本，不产生 artifact、interrupt 或 Skill 执行状态。

## 数据格式与示例回答规则

`skill_help.respond` 需要支持用户询问“需要什么格式的数据”“这个字段的值应该是什么”“能否给我一个模板”。

回答按三层证据组织：

1. **文档明确事实**：直接说明 `SKILL.md` 中写明的字段、含义、取值、约束。
2. **基于文档规则推导的模板**：可以给出安全模板，但必须标注为“根据文档规则整理的模板”。
3. **文档未说明**：对未写明的枚举、业务规则或完整样例，必须说“该 Skill 文档未说明”，不能补全猜测。

例如，当某 Skill 文档说明 `ped_id, hyb_check, set` 三列以及 `hyb_check=0` 表示普通材料、非零表示对照材料时，可以回答：

```csv
ped_id,hyb_check,set
<普通材料ID>,0,A
<对照材料ID>,1,A
```

同时说明：`1` 只是根据“非零表示对照材料”整理的代表值；若文档没有列出完整枚举，不得声称只有 `0/1` 两个取值。

## 前端交互

Slash command 菜单新增一项：

```text
/skill-help    查看 Skill 用法、输入格式、字段含义，不执行 Skill
```

提交行为：

- 用户输入 `/skill-help ...` 后，不把目标 capability 设置为业务 Skill。
- 推荐前端将 `capability_id` 设置为 `skill_help.respond`，并在 metadata 中标记 `skill_help=true`；后端仍保留解析原始命令的兜底能力。
- 返回内容按普通 assistant 消息展示。
- 不展示 Skill 运行状态、不展示“等待补充信息”、不展示上传卡片、不展示 artifact。

可选增强：在用户输入 `/skill-help ` 后，前端复用 capabilities 列表展示候选 Skill，如 `field-design`、`field-analysis`、`rice-genie`、`ocr`、`sql-query`。MVP 只要求菜单可发现和后端解析正确。

## 错误处理

- 找不到 Skill：返回“没有找到该 Skill”，并列出可见 Skill 名称/展示名。
- 匹配到多个 Skill：不调用 LLM，直接要求用户明确选择，附候选列表。
- 问题超出 `SKILL.md`：回答“该 Skill 文档未说明”。
- 用户要求执行：`/skill-help` 场景仍只解释；自然语言混合帮助与执行意图时先澄清。
- `SKILL.md` 过长：可做安全裁剪，优先保留用户可见说明、输入、输出、参数、示例和约束，不保留内部实现细节。
- LLM 输出安全：通过 prompt 和必要后处理防止内部结构泄露；来源只写 Skill 展示名或 name，不写路径。

## 测试计划

后端测试：

1. `/skill-help field-design hyb_check 有什么要求` 路由到 `skill_help.respond`，不路由到 `skill.field_design`。
2. capability id、Skill name、display name 都能解析同一个 Skill。
3. 多匹配/未知 Skill 不调用 LLM。
4. LLM prompt 的 safe view 不包含 `source_path`、脚本路径、handler/module/runtime 等内部字段。
5. 文档未说明时，prompt 和响应约束要求返回“该 Skill 文档未说明”。
6. `/skill-help` 不产生 interrupt、不创建业务 artifact、不执行目标 Skill。
7. 自然语言高置信文档问题生成单节点 `skill_help.respond` 计划。
8. 混合“解释 + 执行”意图返回澄清，不直接执行。

前端测试：

1. Slash 菜单出现 `/skill-help`。
2. 提交 `/skill-help ...` 时目标 capability 是 `skill_help.respond`，或至少保留原始命令让后端解析。
3. 返回结果按普通助手消息展示。
4. 不显示“等待补充信息”、上传卡片、Skill 执行状态或 artifact。
5. 未知 Skill 和多匹配错误按普通 assistant 消息展示。

## 验收标准

- 用户询问 Skill 用法、字段、参数、输入输出或数据格式时，系统能基于目标 `SKILL.md` 回答。
- 显式 `/skill-help` 不会执行目标 Skill。
- 高置信自然语言文档问题能自动进入 `skill_help.respond`。
- 文档未说明的内容不会被编造。
- 答案不会暴露内部代码结构或本地路径。
- 前端展示为普通助手回复，不出现 interrupt 或 Skill 执行态。

## 实施边界

建议实施顺序：

1. 后端增加 `skill_help.respond` capability 与 safe view builder。
2. 路由层增加 `/skill-help` 确定性解析与自然语言高置信 policy。
3. API/runtime 注册新 capability，并确保强制 capability 可指向 `skill_help.respond`。
4. 前端 slash menu 增加 `/skill-help` 项与提交契约。
5. 增加后端和前端回归测试。

License Requirement：本设计不引入新依赖，不涉及 Rust/Cargo 依赖或许可策略变更；实施时如新增依赖需按仓库规则重新评估。
