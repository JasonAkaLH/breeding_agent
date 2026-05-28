# Skill Help 只读文档问答设计

日期：2026-05-28
状态：已通过 document-perfectization 复审，待实施计划
范围：为主代理增加按需读取项目 Skill `SKILL.md` 的只读问答能力，仅支持显式 `/skill-help` 触发；普通/自然语言对话不自动进入该 capability。

## 问题陈述

用户在执行业务 Skill 前或排查缺参提示时，经常只想了解某个 Skill 的用途、字段含义、输入格式、参数约束或示例模板。当前系统只有“自动匹配/强制执行 Skill”链路：主代理在匹配或强制执行时才注入 `SKILL.md` 正文，且匹配后可能运行脚本、生成 artifact 或触发 interrupt。这不适合只读解释场景。

需要一个系统级、只读、按需加载的 Skill 文档问答能力：只有用户显式使用 `/skill-help` 时，系统才读取对应 `SKILL.md` 并基于文档回答；普通/自然语言对话不做 Skill Help 解析、不进入 `skill_help.respond`、不把 Skill 文档发送给 LLM；目标 Skill 不被执行；答案不暴露内部代码结构。

## 当前状态与证据

- `src/integrations/codex_skills/parser.py` 已将 `SKILL.md` 解析为 `SkillManifest`，其中 `display_name`、`capability_id` 等未知 frontmatter 字段进入 `metadata`。
- `src/integrations/codex_skills/catalog.py` 的 `SkillCatalog` 当前只支持按 Skill name 获取 manifest。
- `src/integrations/codex_skills/skill_runtime_state.py` 已维护 active bundle 与 revision；新任务会在 metadata 中携带 `skill_bundle_revision`。
- `src/orchestration/workflow_router.py` 当前只特殊路由 `main_agent.*` 与 `skill.*`；新增 `skill_help.respond` 只能由显式 `/skill-help` / force capability 进入，不能落入默认 LLM planner 或被自动 planner 选择。
- `src/orchestration/llm_workflow_provider.py` 与 `src/capabilities/main_agent/runtime_replanner.py` 当前只把 `main_agent.respond` 和 `skill.*` 视为 answer-producing；`skill_help.respond` 必须补入，否则会被追加不必要的 finalizer。
- `src/capabilities/main_agent/prompt_builder.py` 当前把匹配 Skill 的 `manifest.body` 直接注入主代理 prompt；新能力不能沿用“匹配即注入/执行”的路径。
- `frontend/src/domain/slashCommands.ts` 当前只从 active `skill.*` capabilities 派生 slash 命令；`/skill-help` 必须作为内置命令单独注册，不能依赖 `skill_help.respond` 的 capability id 自动派生。
- `skill/field-design/SKILL.md` 等 Skill 正文既包含用户可见输入说明，也包含脚本路径、Rscript 命令、wrapper 等内部实现说明；安全视图不能直接把完整正文原样作为可复述内容。

## 目标

- 支持用户通过 `/skill-help` 显式查看 Skill 用法、字段说明、数据格式、参数约束或示例模板。
- 只在显式 `/skill-help` 触发时读取目标 Skill 的 `SKILL.md`；普通/自然语言对话不做 Skill Help 解析，不把 Skill 文档发送给 LLM。
- 回答必须严格基于对应 `SKILL.md`；文档未说明时明确回答“该 Skill 文档未说明”。
- 不执行目标 Skill，不产生业务 artifact，不触发 interrupt，不消费 pending interrupt answer。
- 不向用户暴露内部代码结构、脚本路径、handler/module/runtime、source_path、本地路径、服务 allowlist 或调试命令。

## 非目标

- 不实现通用知识库或跨文档 RAG。
- 不读取 Skill 脚本、源码、测试、运行时配置或上传文件内容来补充答案。
- 不用 `/skill-help` 触发业务执行。
- 不让前端承担 Skill 文档解析、字段解释或权限判断。
- 不保证能回答 `SKILL.md` 未写明的业务规则。
- 不对普通/自然语言对话做 Skill Help 意图解析；不通过 LLM 自动判断是否应该进入 `skill_help.respond`。
- MVP 不改变 conversation serial guard；如果当前 conversation 已有 active / waiting task，`/skill-help` 是否允许旁路查询需要在实施计划中作为独立检查点验证，不能静默吞掉为 interrupt answer。

## 用户、干系人与受影响系统

| 对象 | 需求 / 影响 |
| --- | --- |
| 业务用户 | 在不运行 Skill 的情况下了解某个 Skill 的用途、输入格式、字段含义和下一步准备材料。 |
| 主代理 / planner | 默认不解析 Skill Help 意图；只有显式 `/skill-help` 才进入只读文档问答，其他对话保持既有路由。 |
| 项目级 Skill 作者 | 需要让 `SKILL.md` 的用户可见说明可被系统安全解释，同时内部实现细节不会外泄。 |
| 后端编排系统 | 新增只读 capability、路由、执行器、payload policy、answer-producing 判定与审计。 |
| 前端业务对话台 | 新增内置 `/skill-help` slash 命令，并按普通助手消息展示结果。 |

## 推荐方案

采用独立只读内置 capability：`skill_help.respond`。

该 capability 与普通项目 Skill 执行链路分离：它读取当前激活或任务绑定 revision 的 `SkillCatalog`，解析用户指定的 Skill，构造安全 Skill 文档视图，调用 LLM 生成面向用户的说明。它永远不调用 `SkillExecutor`，也不触发目标 Skill 的脚本、artifact、upload interrupt 或等待补充信息链路。

已拒绝方案：

- 复用 `main_agent.respond` 并加入 `skill_help_mode`：会把主代理业务对话、Skill 匹配和只读文档问答耦合在一起，难以保证“不执行”。
- 把 `skill-help` 做成普通项目 Skill：语义上会形成 Skill 解释 Skill 的循环，也容易误入现有 Skill 匹配/执行链路。

## 功能需求

| ID | 要求 | 验收 |
| --- | --- | --- |
| FR-1 | 系统必须注册 public builtin capability `skill_help.respond`。 | `/api/v1/capabilities` 可返回该 capability，且 `kind/source` 为 builtin/help 类语义，不是 `skill`。 |
| FR-2 | 显式 `/skill-help` 必须优先进入 `skill_help.respond`，不得进入目标业务 Skill 执行链路。 | `/skill-help field-design ...` 不产生 `skill.field_design` 节点。 |
| FR-3 | 非 `/skill-help` 的普通/自然语言对话不得自动进入 `skill_help.respond`。 | “field-design 的 hyb_check 有什么要求？”在默认对话中不生成 help 计划；只有加 `/skill-help` 才进入 help。 |
| FR-4 | Skill 解析必须支持 capability id、Skill name、display name 和 normalized loose match。 | `skill.field_design`、`field-design`、`试验设计智能体` 均解析到同一 Skill。 |
| FR-5 | display name 或问题文本含空格时，解析必须使用最长前缀匹配或引号语法，而不是只取第一个空格 token。 | `/skill-help OCR 文档识别 需要什么输入` 能识别 `OCR 文档识别`。 |
| FR-6 | 文档问答必须只基于 `SKILL.md` 的安全视图。 | prompt / safe view 不含脚本路径、handler/module/runtime、source_path、本地路径或调试命令。 |
| FR-7 | 文档未说明的内容必须回答“该 Skill 文档未说明”。 | 对不存在字段/枚举提问时不编造。 |
| FR-8 | `/skill-help` 输出必须是普通 assistant 文本。 | 无 artifact、无 interrupt、无 Skill 运行状态卡片。 |
| FR-9 | 未知或多匹配 Skill 必须不调用 LLM。 | 返回候选/澄清消息，LLM fake 未被调用。 |
| FR-10 | LLM planner / runtime replanner 在 auto 模式不得选择 `skill_help.respond`。 | planner public capability 列表或 plan validation 使 help 只可由 explicit force 使用。 |

## 非功能需求

- **安全 / 隐私**：必须默认剔除内部代码结构、路径、脚本、handler/module/runtime、source_path、allowlist、secret-like 字段；不能把上传文件内容作为文档问答依据。
- **可靠性**：无匹配、多匹配、空问题、过长文档、LLM 失败都必须有可解释失败路径；未知 Skill 不应进入 planner 兜底执行；`skill_help.respond` 不应被 auto planner 选中。
- **兼容性**：不得破坏现有 `skill.*` slash command、pending Skill context、upload interrupt、artifact 展示和主代理普通对话。
- **可观测性**：后端应记录脱敏审计事件，例如 `skill_help.invoked`、`skill_help.resolve_failed`、`skill_help.llm_failed`，只包含 capability id / Skill name / display name / reason / revision，不记录内部路径或原始 prompt。
- **性能**：普通对话不加载所有 Skill 正文进 LLM；只读问答只加载一个目标 Skill 的安全视图。过长正文应先裁剪用户可见部分。

## 路由入口

### 显式命令

支持以下形态：

```text
/skill-help field-design hyb_check 有什么要求
/skill-help skill.field_design hyb_check 有什么要求
/skill-help 试验设计智能体 输入数据应该是什么格式
/skill-help "OCR 文档识别" 需要什么输入
```

规则：

- `/skill-help` 优先级最高；一旦识别，不进入普通 Skill 自动匹配，也不强制调用目标业务 Skill。
- 命令后 Skill 引用通过最长前缀匹配解析：先尝试 capability id、Skill name、display name，再尝试 normalized loose match；带空格 display name 可用引号，也必须支持无引号最长匹配。
- 命令后没有具体问题时，返回该 Skill 的概览：用途、输入要求、主要参数/字段、输出、限制和下一步建议。
- 只有 `/skill-help` 且没有 Skill 引用时，返回用法和可见 Skill 列表，不调用 LLM。
- 未知 Skill 返回可见候选列表。
- 多个 Skill 匹配时返回澄清问题和候选列表，不调用 LLM。

### 普通对话不触发

除非用户消息以 `/skill-help` 开头，系统不得执行 Skill Help 意图解析，也不得让 LLM planner 在 auto 模式选择 `skill_help.respond`。

示例：

- “field-design 的 hyb_check 有什么要求？”→ 不进入 `skill_help.respond`；保持既有普通对话 / planner 路由。
- “试验设计智能体需要什么表头？”→ 不进入 `skill_help.respond`；保持既有普通对话 / planner 路由。
- “帮我跑 field-design 做 RCBD 设计”→ 保持既有业务 Skill / 主代理执行链路。
- `/skill-help field-design hyb_check 有什么要求` → 进入 `skill_help.respond`。

## 后端架构

### Capability 与执行器

新增独立模块，建议命名为 `src/capabilities/skill_help/`：

- `SkillHelpRespondCapability`：实现 `skill_help.respond`。
- `SkillHelpExecutor`：只支持 `skill_help.respond`。
- `SkillHelpWorkflowProvider`：为显式 force capability 构造单节点计划。
- `skill_doc_view.py` 或等价 helper：负责 Skill 解析、安全视图构造、正文裁剪和泄露防护。
- `workflow.py`：提供 descriptor、payload policy、local execution instance。

注册要求：

- `CapabilityRegistry` 注册 `skill_help.respond` public descriptor。
- `InstanceRegistry` 注册支持 `skill_help.respond` 的本地 instance。
- `CompositeExecutor` 包含 `SkillHelpExecutor`。
- `WorkflowRouter` 对 `requested_capability_id == "skill_help.respond"` 或 `startswith("skill_help.")` 必须路由到 `SkillHelpWorkflowProvider`，不得落入默认 LLM planner。该 requested capability 只应由 `/skill-help` / explicit force 设置。
- `LLMWorkflowProvider` 在 auto planning 时不得把 `skill_help.respond` 暴露为可选 public capability；若 explicit force 已生成 help node，`LLMWorkflowProvider._is_answer_producing` 与 `MainAgentRuntimeReplanner._is_answer_producing` 必须把它视为 answer-producing，避免追加主代理 finalizer。

### Planner payload policy

`skill_help.respond` 的 payload policy 必须由系统填充可信字段：

```json
{
  "user_message": "<effective_user_message>",
  "skill_ref": "<slash 或 policy 解析出的引用，可为空>",
  "skill_question": "<去掉命令和 Skill 引用后的问题，可为空>",
  "source": "slash_command"
}
```

Planner 不应在 auto 模式构造 `skill_help.respond` payload；该 payload 仅由 `/skill-help` 确定性解析生成。executor 仍必须重新解析并校验 `skill_ref`，不得信任前端或 planner 传入的目标 Skill。

### Skill 解析规则

从当前任务绑定的 `skill_bundle_revision` 对应 catalog 解析 Skill；无 revision 时使用 active catalog。不得临时扫描磁盘。

解析顺序：

1. capability id 精确匹配，例如 `skill.field_design`；
2. Skill name 精确匹配，例如 `field-design`；
3. display name 精确匹配，例如 `试验设计智能体`；
4. normalized loose match：大小写不敏感，去除空格、下划线、连字符和常见全角空白。

解析结果必须唯一。多匹配或无匹配均不调用 LLM。

## Safe Skill 文档视图

发送给 LLM 的内容不是原始 manifest 全量对象，也不是未处理的 `SKILL.md` 全文，而是安全视图。

允许包含：

- Skill name；
- display name；
- description；
- triggers；
- 用户可见输入字段：name、type、required、default、enum、aliases、description；
- 用户可见输出字段；
- 用户可见参数说明、业务约束、数据格式和示例模板；
- 从 `SKILL.md` 正文提取的用户可见段落。

必须剔除或改写：

- `source_path` 和任何本地文件路径；
- script path、handler、module、factory、runtime、execution mode；
- Rscript / Python / shell / PowerShell 命令块；
- wrapper、bundled scripts、sidecar、native/Rust、service allowlist 等内部实现细节；
- secret-like key、token、环境变量值；
- 内部 JSON 文件路径、调试路径、测试结构。

正文处理必须 fail-closed：如果无法可靠判断某段是否用户可见，应默认不放入 LLM safe view。不能只依赖 prompt 让 LLM“不说出来”。

## LLM 回答约束

Prompt 必须包含以下硬约束：

- 只能回答目标 Skill 的文档问题。
- 必须严格基于提供的 Skill 安全文档视图。
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

Slash command 菜单新增内置项：

```text
/skill-help    查看 Skill 用法、输入格式、字段含义，不执行 Skill
```

实现要求：

- `/skill-help` 不能从 `skill.*` capability 派生；必须由前端内置 command registry 或 `deriveComposerCommands` 合并生成。
- `/skill-help` 菜单项不展示 `source_path`，不参与普通 Skill command 冲突检测。
- 用户输入 `/skill-help ...` 后，前端提交 `capability_id = "skill_help.respond"`，`routing_mode = "force_capability"`。
- metadata 至少包含 `skill_help=true`、`slash_command="/skill-help"`；不得把目标业务 Skill capability id 作为本轮 `capability_id`。
- 返回内容按普通 assistant 消息展示。
- 不展示 Skill 运行状态、不展示“等待补充信息”、不展示上传卡片、不展示 artifact。
- 如果当前存在 pending interrupt，前端不得把 `/skill-help ...` 当作 interrupt answer 直接提交；MVP 可阻止并提示“请先回答/取消当前补充信息请求后再查看 Skill 帮助”，或在实施计划中明确实现只读旁路。无论哪种选择，都必须测试不消费 pending interrupt。

可选增强：在用户输入 `/skill-help ` 后，前端复用 capabilities 列表展示候选 Skill，如 `field-design`、`field-analysis`、`rice-genie`、`ocr`、`sql-query`；候选展示只含 display name、Skill name、capability id 和描述，不显示路径。

## 错误处理与失败模式

| 场景 | 行为 |
| --- | --- |
| `/skill-help` 后没有 Skill 引用 | 返回用法和可见 Skill 列表，不调用 LLM。 |
| 找不到 Skill | 返回“没有找到该 Skill”，并列出可见 Skill 名称/展示名。 |
| 匹配到多个 Skill | 不调用 LLM，直接要求用户明确选择，附候选列表。 |
| display name 含空格 | 使用最长前缀或引号解析，不把第一个词误判为完整 Skill。 |
| 问题超出文档 | 回答“该 Skill 文档未说明”。 |
| 用户要求执行 | `/skill-help` 场景仍只解释；不带 `/skill-help` 的自然语言请求保持既有普通对话 / Skill 路由，不进入 help。 |
| `SKILL.md` 过长 | 安全裁剪，优先保留用户可见说明、输入、输出、参数、示例和约束。 |
| safe view 为空或仅剩内部内容 | 返回“该 Skill 文档未提供可安全展示的说明”，不调用或不继续 LLM。 |
| LLM 失败 | 返回可恢复错误，不执行目标 Skill。 |
| pending interrupt 存在 | 不消费为 interrupt answer；按前端交互要求阻止或旁路。 |

## 测试计划

后端测试：

1. `skill_help.respond` descriptor、payload policy、instance 和 executor 注册成功。
2. `WorkflowRouter` 对 force `skill_help.respond` 生成单节点 help 计划，不落入 default planner。
3. `/skill-help field-design hyb_check 有什么要求` 路由到 `skill_help.respond`，不路由到 `skill.field_design`。
4. capability id、Skill name、display name、含空格 display name 都能解析同一个 Skill。
5. 多匹配/未知 Skill 不调用 LLM。
6. safe view / prompt 不包含 `source_path`、脚本路径、handler/module/runtime、Rscript、PowerShell、wrapper 等内部字段或命令块。
7. 文档未说明时，prompt 和响应约束要求返回“该 Skill 文档未说明”。
8. `/skill-help` 不产生 interrupt、不创建业务 artifact、不执行目标 Skill。
9. 非 `/skill-help` 的自然语言文档问题不会生成 `skill_help.respond` 计划；auto planner 也不能选择该 capability。
10. explicit help node 被视为 answer-producing，不追加 main_agent finalizer。
11. 任务绑定 `skill_bundle_revision` 时，从对应 revision 读取 Skill 文档。
12. pending interrupt 场景下，`/skill-help` 不会写入 interrupt answer。

前端测试：

1. Slash 菜单出现内置 `/skill-help`，且不依赖 `skill.*` capability 派生。
2. 提交 `/skill-help ...` 时目标 capability 是 `skill_help.respond`，metadata 标记 `skill_help=true`。
3. `/skill-help` 菜单项和候选列表不显示 `source_path`。
4. 返回结果按普通助手消息展示。
5. 不显示“等待补充信息”、上传卡片、Skill 执行状态或 artifact。
6. 未知 Skill 和多匹配错误按普通 assistant 消息展示。
7. pending interrupt 存在时，输入 `/skill-help ...` 不调用 interrupt answer 提交逻辑。

## 验收标准

- 用户通过 `/skill-help` 询问 Skill 用法、字段、参数、输入输出或数据格式时，系统能基于目标 `SKILL.md` 安全视图回答。
- 显式 `/skill-help` 不会执行目标 Skill。
- 非 `/skill-help` 的普通/自然语言对话不会自动进入 `skill_help.respond`。
- 文档未说明的内容不会被编造。
- 答案不会暴露内部代码结构、本地路径、脚本命令或调试信息。
- 前端展示为普通助手回复，不出现 interrupt 或 Skill 执行态。
- pending interrupt 不会被 `/skill-help` 误消费。

## 实施边界与顺序

1. 后端新增 `src/capabilities/skill_help/`，包含 capability、executor、workflow descriptor、payload policy 与 safe view builder。
2. 注册 `skill_help.respond` descriptor、instance、executor，并更新 `WorkflowRouter`、LLM planner / runtime replanner answer-producing 判定。
3. 路由层增加 `/skill-help` 确定性解析；不得实现自然语言触发或 deterministic pre-router。
4. 扩展 Skill 解析 helper，支持 capability id / name / display name / normalized loose / 最长前缀匹配。
5. 前端 slash command 模块改为支持内置 `/skill-help`，并处理 pending interrupt 不误消费。
6. 增加后端和前端回归测试。
7. 更新 `Skill构建指南.md`：要求 Skill 作者把用户可见用法、输入格式、字段含义、示例模板写清楚，并避免在用户可见段落混入内部路径/命令；若必须保留内部执行说明，系统会在 help safe view 中剔除。

## 风险、假设与开放问题

| 类型 | 内容 | 处理 |
| --- | --- | --- |
| 假设 | `SKILL.md` 是 Skill 用户说明的唯一权威来源。 | 已写入非目标：不读取脚本/源码补充答案。 |
| 风险 | 现有 Skill 正文混有内部命令，若 safe view 只靠 prompt 约束可能泄露。 | 必须实现安全视图剔除/改写，并加测试。 |
| 风险 | `skill_help.respond` 如果暴露给 auto planner，普通问题可能误入 help；如果未加入 router / instance / answer-producing 判定，explicit help 会被默认 planner 或 finalizer 误处理。 | 已纳入后端架构和测试计划：auto 不可选，explicit 可路由。 |
| 风险 | 前端现有 slash 命令只支持 `skill.*`，内置 `/skill-help` 若未单独注册会被当 unknown slash 阻止。 | 已纳入前端实现要求。 |
| 假设 | MVP 可先阻止 pending interrupt 期间的 `/skill-help`，只要不误消费 interrupt answer。 | 实施计划需确认是否做只读旁路；无论选择哪种都必须测试。 |

License Requirement：本设计不引入新依赖，不涉及 Rust/Cargo 依赖或许可策略变更；实施时如新增依赖需按仓库规则重新评估。
