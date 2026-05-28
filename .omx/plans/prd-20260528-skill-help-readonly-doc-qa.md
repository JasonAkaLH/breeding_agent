# PRD: Skill Help 只读文档问答

状态：Ready for implementation  
日期：2026-05-28  
来源设计：`docs/superpowers/specs/2026-05-28-skill-help-readonly-doc-qa-design.md`

## 1. 目标

新增系统级只读 capability `skill_help.respond`，让用户只能通过显式 `/skill-help` 查看某个项目 Skill 的 `SKILL.md` 说明，而不会执行目标 Skill、不会触发 interrupt、不会生成业务 artifact，也不会暴露内部代码结构。普通/自然语言对话不做 Skill Help 解析，默认不进入该 capability。

成功后：

- `/skill-help field-design hyb_check 有什么要求` 返回基于 `field-design` `SKILL.md` 的普通助手文本。
- `/skill-help skill.field_design ...`、`/skill-help field-design ...`、`/skill-help 试验设计智能体 ...` 均能解析同一 Skill。
- 自然语言“field-design 的 hyb_check 有什么要求？”不进入 `skill_help.respond`；只有 `/skill-help field-design hyb_check 有什么要求` 进入只读 help。
- `SKILL.md` 未说明的内容明确回答“该 Skill 文档未说明”。
- 答案不包含脚本路径、Rscript/PowerShell 命令、handler/module/runtime、`source_path`、本地路径、allowlist 或调试文件。
- pending interrupt / pending Skill context 不会被 `/skill-help` 误消费或误 supersede。

## 2. 当前证据与约束

| 证据 | 文件 | 影响 |
| --- | --- | --- |
| `parse_skill_file` 已读取 `SKILL.md` 正文，未知 frontmatter 进入 `metadata`，`source_path`、`scripts` 也在 manifest 中 | `src/integrations/codex_skills/parser.py:23-52` | help 能复用 manifest，但必须构造 safe view，不能把 manifest 全量给 LLM |
| `SkillCatalog.get` 只按 Skill name 匹配 | `src/integrations/codex_skills/catalog.py:31-35` | 需要新增解析 helper 支持 capability id、display name、loose/最长前缀匹配 |
| `SkillRuntimeState` 支持 active/revision bundle 和 `catalog_for_revision` | `src/integrations/codex_skills/skill_runtime_state.py:83-108` | help 必须使用任务绑定 revision，不能临时扫描磁盘 |
| `WorkflowRouter` 当前只特殊路由 `main_agent.*` 和 `skill.*` | `src/orchestration/workflow_router.py:14-22` | `skill_help.respond` 必须新增 provider 分支；且只能由 explicit `/skill-help` / force capability 进入，不能落入或暴露给 auto planner |
| LLM planner / runtime replanner 当前只把 `main_agent.respond` 和 `skill.*` 视为 answer-producing | `src/orchestration/llm_workflow_provider.py:230-232`, `src/capabilities/main_agent/runtime_replanner.py:242-244` | explicit help 计划必须加入 answer-producing 判定，避免被追加 finalizer；auto planner 不得选择 help |
| 主代理 prompt 当前把匹配 Skill 的 `manifest.body` 原样注入 | `src/capabilities/main_agent/prompt_builder.py:44-53` | help 不能复用匹配即注入路径；safe view 必须先过滤 |
| 主代理执行时匹配 Skill 后会进入 `_run_auto_scripts`，可能产生 interrupt/artifact | `src/capabilities/main_agent/executor.py:87-145`, `src/capabilities/main_agent/executor.py:447-466` | help 必须独立 executor，不能走 `MainAgentRespondCapability` forced Skill 路径 |
| runtime 当前只注册 main agent descriptors，然后注册 skill runtime descriptors | `src/api/runtime.py:2050-2068` | 需要注册 `skill_help.respond` descriptor 与 local instance |
| runtime 已有 skill revision 注入 metadata | `src/api/runtime.py:431-432`, `src/api/runtime.py:445-454` | help workflow/provider 可以读取 `skill_bundle_revision` |
| frontend slash command 只从 active `skill.*` 派生 | `frontend/src/domain/slashCommands.ts:26-38` | `/skill-help` 必须作为内置命令单独加入，不可依赖 `skill_help.respond` 自动派生 |
| frontend direct slash metadata 只有 `forced_by_slash_command` 与 `slash_command` | `frontend/src/domain/slashCommands.ts:93-102` | `/skill-help` 需要额外 metadata，例如 `skill_help=true`，且 capability id 为 `skill_help.respond` |
| pending interrupt 时当前提交会先调用 `handleInterruptAnswer` | `frontend/src/App.tsx:786-805` | `/skill-help` 必须在前端阻止或旁路；MVP 选择阻止，防止误消费 interrupt answer |
| slash menu 目前会显示 `sourcePath` | `frontend/src/components/SlashCommandMenu.tsx:52-55` | `/skill-help` 及其候选不得显示内部路径 |
| `field-design` `SKILL.md` 含 `scripts/run_field_design.py`、wrapper、Rscript/PowerShell 命令 | `skill/field-design/SKILL.md:90-115`, `skill/field-design/SKILL.md:208-219` | safe view 必须剔除内部命令块和实现说明 |
| frontend artifact loader 可将普通 text artifact 展示为助手文本 | `frontend/src/domain/artifacts.ts:150-154`, `frontend/src/App.tsx:1083-1098` | help capability 可返回 text artifact，无需 main-agent finalizer |

## 3. 非目标

- 不读取 Skill 脚本、源码、测试、上传文件内容或 runtime 配置来补充答案。
- 不实现跨 Skill / 多文档 RAG。
- 不通过 `/skill-help` 执行业务 Skill。
- 不改变现有 `skill.*` slash command 的强制执行语义。
- 不在本计划中重写所有 `SKILL.md` 的内容结构；只更新 `Skill构建指南.md` 的 authoring 规则，并让 safe view 对现有文档 fail-closed。
- MVP 不做 pending interrupt 期间的只读旁路；前端阻止该提交并提示用户先处理/取消当前补充信息。
- 不实现自然语言 Skill Help 触发；不通过 LLM 判断普通对话是否应该进入 `skill_help.respond`。

## 4. 需求

### R1. Builtin capability 注册

- 新增 public builtin capability `skill_help.respond`。
- descriptor 的 `kind/source` 必须是 builtin/help 语义，不得是 `skill`。
- `/api/v1/capabilities` 可暴露该 capability 供前端识别，但前端 `/skill-help` 仍作为内置命令处理，而不是从 `skill.*` 派生。
- 注册 local execution instance，`Scheduler` 能选中支持 `skill_help.respond` 的实例。

### R2. 显式 `/skill-help` 路由

- 前端输入 `/skill-help ...` 时必须提交：
  - `routing_mode=force_capability`
  - `capability_id=skill_help.respond`
  - metadata 包含 `skill_help=true`、`slash_command=/skill-help`
- 后端在 `submit_message` 中提供兜底：若 content 以 `/skill-help` 开头，即使前端未设置 capability，也 canonical 到 `skill_help.respond`。
- `WorkflowRouter` 对 `skill_help.respond` 必须进入 `SkillHelpWorkflowProvider`，生成单节点 plan。
- 该路径不得创建 `skill.*` 节点，不得进入 `SkillWorkflowProvider`。

### R3. 普通对话 / auto planner 隔离

- 除非用户显式输入 `/skill-help`，系统不得执行 Skill Help 意图解析。
- 不新增自然语言 deterministic pre-router；不通过 LLM 判断普通对话是否应该进入 `skill_help.respond`。
- `skill_help.respond` 在 auto planning 时不得作为 LLM planner 可选 capability 暴露；只允许 explicit force 路由使用。
- “field-design 的 hyb_check 有什么要求？”等不带 `/skill-help` 的问题保持既有普通对话 / planner 路由，不生成 help 节点。

### R4. Skill 引用解析

- 支持 capability id 精确匹配：`skill.field_design`。
- 支持 Skill name 精确匹配：`field-design`。
- 支持 display name 精确匹配：`试验设计智能体`。
- 支持 normalized loose match：大小写不敏感，去除空格、下划线、连字符和常见全角空白。
- 支持 `/skill-help` 命令中的最长前缀匹配和引号匹配，避免 `OCR 文档识别` 被拆成 `OCR`。
- 多匹配/无匹配不调用 LLM。

### R5. Safe Skill 文档视图

- executor 不得把 `SkillManifest` 全量或原始 `body` 直接交给 LLM。
- safe view 允许：name、display_name、description、triggers、用户可见 inputs/outputs/parameters、用户可见正文段落、字段/格式/约束说明。
- safe view 必须剔除：`source_path`、script path、handler/module/factory/runtime/execution mode、Rscript/Python/shell/PowerShell 命令块、wrapper/bundled scripts/sidecar/native/Rust/service allowlist、secret-like 字段、内部 JSON/debug 路径。
- 正文过滤 fail-closed：无法判断是否用户可见的段落默认不进入 safe view。
- 对现有 `field-design` 中 `Input Schema` 类用户说明应保留；对 `Run RCBD` 命令块、PowerShell/Rscript 示例、wrapper 描述应剔除。

### R6. LLM 回答与 fallback

- `SkillHelpRespondCapability` 使用非流式 text generator 或等价 LLM seam 生成回答。
- prompt 必须要求：严格基于 safe view；文档未说明则答“该 Skill 文档未说明”；不暴露内部结构；来源只写 display name/name。
- 未知 Skill、多匹配、空 `/skill-help`、safe view 为空等 explicit help 场景不调用 LLM，直接返回普通助手文本。
- LLM 失败返回可恢复错误文本或 capability error；不得改路由执行目标 Skill。
- 输出为 text artifact + `skill_help.output_final` / audit-only 脱敏事件；不产生业务 artifact 或 interrupt。

### R7. Pending interrupt / pending Skill context 安全

- 前端存在 active `pendingInterrupt` 时，`/skill-help` MVP 阻止提交并提示“请先回答或取消当前补充信息请求后再查看 Skill 帮助”。
- 后端 explicit `skill_help.respond` 不能调用 `mark_pending_skill_context_superseded`；只读帮助不应替代当前待补全业务 Skill。
- 后端 explicit `skill_help.respond` 不能被当作 pending context continuation，也不能写入 interrupt answer。

### R8. 前端展示

- slash 菜单增加内置 `/skill-help` 项。
- `/skill-help` 及其候选展示不得包含 `source_path`。
- 提交后用户消息展示可保留 `/skill-help ...` 或清晰显示用户 help 查询，不显示为目标业务 Skill badge。
- 结果按普通助手消息展示；不显示 Skill 运行状态、不显示“等待补充信息”、不显示上传卡片、不显示业务 artifact 卡片。

### R9. Skill 构建指南同步

- 更新 `Skill构建指南.md`：要求 Skill 作者把用户可见用途、输入格式、字段含义、参数约束、示例模板写清楚。
- 指南必须提醒：内部脚本路径、运行命令、handler/runtime/source_path 等不属于用户可见 help 内容；即使文档保留内部执行说明，系统 help safe view 会剔除。

## 5. 实施检查点

### CP-0: TDD 失败测试先行

先新增后端和前端测试，锁定以下旧行为缺口：

- `skill_help.respond` 未注册时 force capability 不可用。
- 当前 `WorkflowRouter` 会把 `skill_help.respond` 落到 default provider。
- 当前 auto planner 可能看到所有 public capabilities；help 必须从 auto planner 可选列表排除。
- 当前 LLM/replanner answer-producing 判定会给非 `skill.*` help 节点追加 finalizer。
- 当前 frontend slash parser 会把 `/skill-help` 当 unknown slash。
- 当前 pending interrupt 会把提交当 interrupt answer。
- 当前直接使用 `SKILL.md` body 会泄露 `scripts/run_field_design.py`、Rscript、PowerShell。

验收：新增测试在当前实现下至少部分失败，证明覆盖真实缺口。

### CP-1: 新增 Skill Help 后端 capability

新增文件建议：

- `src/capabilities/skill_help/__init__.py`
- `src/capabilities/skill_help/workflow.py`
- `src/capabilities/skill_help/executor.py`
- `src/capabilities/skill_help/doc_view.py`
- `src/capabilities/skill_help/intent.py`

实现内容：

- `SKILL_HELP_CAPABILITY_DESCRIPTORS`，包含 `skill_help.respond`。
- `SKILL_HELP_PLANNER_PAYLOAD_POLICIES`，system payload 负责注入 `user_message` 和 source；planner 字段最小化。
- `build_local_skill_help_instance()`。
- `SkillHelpWorkflowProvider.build_plan()` 返回单节点 `skill_help.respond`，metadata 记录 `skill_bundle_revision`、`skill_help_source`。
- `SkillHelpExecutor` 实现 `supports/execute`，只支持 `skill_help.respond`。

验收：registry / scheduler / executor tests 通过。

### CP-2: Runtime 注册与 router 接线

修改：

- `src/api/runtime.py`
  - 注册 help descriptor 和 planner payload policy。
  - 注册 help local instance。
  - `CompositeExecutor` 增加 `SkillHelpExecutor`。
  - assembly 注入 `skill_runtime_state.catalog_for_revision`、text generator/audit seam。
  - explicit `skill_help.respond` 不 supersede pending Skill context。
  - auto 模式不得把 `skill_help.respond` 暴露给 LLM planner；可通过 planner capability filter / provider policy / validator denylist 实现。
- `src/orchestration/workflow_router.py`
  - 构造参数增加 `skill_help_provider`。
  - `capability_id == "skill_help.respond"` 或 `startswith("skill_help.")` 路由到 help provider。
- `src/orchestration/llm_workflow_provider.py`
- `src/capabilities/main_agent/runtime_replanner.py`
  - `_is_answer_producing` 增加 `capability_id == "skill_help.respond"`。

验收：force `skill_help.respond` 生成单节点 help plan；无 default planner；无 main agent finalizer。

### CP-3: Slash-only command parser 与 Skill 引用解析 helper

实现 `SkillHelpCommandParser` / `SkillReferenceResolver`：

- `parse_skill_help_command(content)`：只识别 raw `/skill-help`，返回 skill_ref/question。
- `resolve_skill_reference(catalog, registry, text)`：支持 capability id/name/display_name/normalized/longest-prefix/quoted forms。
- 不实现 `detect_skill_help_intent`，不扫描普通自然语言消息，不维护执行动词 denylist / 文档类 allowlist。
- ambiguous result 返回候选，不调用 LLM。

接线：

- `ApiRuntime.submit_message` 在 `_ensure_supported_capability` 前只做 backend `/skill-help` fallback canonicalization。
- requested capability 为空且消息不以 `/skill-help` 开头时，不调用任何 Skill Help resolver。
- explicit `/skill-help` 设置 metadata `skill_help_source=slash_command`。

验收：各种 `/skill-help` 引用形态通过；非 `/skill-help` 自然语言不会进入 help。

### CP-4: Safe view builder 与 prompt

实现 `SafeSkillDocView`：

- 从 manifest frontmatter 结构化输出用户可见字段。
- 从 body 过滤用户可见 markdown：
  - 移除 fenced code blocks，尤其 language 为 powershell/bash/sh/python/r/text 且含命令/路径的块。
  - 移除含内部关键词的段落/行：`scripts/`、`.py`、`.R`、`Rscript`、`PowerShell`、`Set-Variable`、`wrapper`、`backend executes`、`runtime:`、`source_path`、`debugging`、`outputs/`、`bundled R scripts` 等。
  - 保留用户说明段，如 Welcome Message、Input Schema、字段含义、Workflow 选择规则、参数约束；若段落混杂内部内容则只保留安全句子。
- 对 safe view 为空时直接返回 deterministic fallback，不调用 LLM。
- prompt 构造函数只接受 safe view JSON/markdown，不接受 raw manifest。
- LLM 返回后可做轻量 post-check：若包含明显内部泄露词，替换为安全失败消息或重新生成一次（MVP 可先安全失败）。

验收：sentinel/internal-token tests 证明 prompt/answer 不含内部路径/命令。

### CP-5: Skill Help executor 输出合同

- deterministic error/help responses 使用同一 text artifact 输出路径。
- LLM 成功生成后返回：
  - `output_payload.response_text`
  - `response_source=skill_help`
  - `skill_help_source`
  - `resolved_skill_name/display_name/capability_id`
  - text artifact（producer node 为 `skill_help.respond` 节点）
  - frontend-visible final/progress event 可选；至少依赖 `task.completed` 后 artifact loading 显示普通文本。
- 不返回 `interrupt`。
- 不返回 file/json/data-query artifacts。
- 审计事件脱敏，不含 raw prompt、source_path 或内部命令。

验收：前端 loadArtifacts 可显示 text artifact，且不会显示业务 artifact 卡片。

### CP-6: 前端内置 `/skill-help`

修改：

- `frontend/src/domain/slashCommands.ts`
  - 抽象 `ComposerCommand` 或扩展 `SlashCommand` 支持 `kind: 'skill' | 'builtin'`。
  - `deriveSlashCommands` 改为合并 active `skill.*` 与内置 `/skill-help`。
  - `/skill-help` direct parse 只剥离 `/skill-help`，content 保留 `skill_ref + question` 给后端解析。
  - ready metadata 增加 `skill_help=true`。
  - `/skill-help` 不参与 `sourcePath` 搜索/展示。
- `frontend/src/components/SlashCommandMenu.tsx`
  - builtin command 不显示 source path。
- `frontend/src/App.tsx`
  - pendingInterrupt 存在且 intent 为 `/skill-help` 时阻止提交，显示提示，不调用 `handleInterruptAnswer`。
  - 提交 `/skill-help` 时 `capabilityId='skill_help.respond'`。
  - 结果保持普通助手消息展示。

验收：slash tests 与 App tests 通过。

### CP-7: 指南与文档同步

- 更新 `Skill构建指南.md` 的 `SKILL.md` authoring 规则。
- 可在 `CHANGELOG.md` 的 Unreleased 增加一条简短记录：Skill Help 只读文档问答计划/实现状态。
- 若新增 API capability 或前端 slash 语义影响用户文档，补充 README / API doc 仅在本次实现确实改变公开接口说明时执行。

验收：指南明确“help 可回答的数据格式/字段值必须写在 `SKILL.md` 用户可见段落中”。

### CP-8: 回归与手工 smoke

最小验证：

```bash
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/integrations/codex_skills -p 'test_*.py'
cd frontend && npm test -- --run
cd frontend && npm run build
```

人工 smoke：

1. 启动 fullstack。
2. 输入 `/skill-help field-design hyb_check 有什么要求`。
3. 期望普通助手文本回答字段规则，来源为“试验设计智能体 Skill 文档”。
4. 确认没有 `skill.field_design` node、无 interrupt 卡片、无业务 artifact。
5. 输入“field-design 的 hyb_check 有什么要求？”验证不会进入 `skill_help.respond`，仍走既有普通对话 / planner 路由。
6. 在 pending interrupt 状态输入 `/skill-help ...`，确认前端阻止而不是提交补充答案。

## 6. 验收标准

- AC-1：`/skill-help field-design hyb_check 有什么要求` 产生 `skill_help.respond` 节点，不产生 `skill.field_design` 节点。
- AC-2：回答包含 `hyb_check` 的文档规则；如果问题未在文档说明，回答“该 Skill 文档未说明”。
- AC-3：回答、prompt 记录、audit payload、artifact text 均不含 `scripts/run_field_design.py`、`Rscript`、`Set-Variable`、`source_path`、`handler`、`runtime` 等内部泄露词。
- AC-4：capability id、Skill name、display name、含空格 display name 均可解析。
- AC-5：未知/多匹配 Skill 不调用 LLM，并返回候选/澄清文本。
- AC-6：非 `/skill-help` 的自然语言文档问题不会进入 help；执行类请求仍进入正常 Skill/主代理路线。
- AC-7：pending interrupt 时 `/skill-help` 不调用 interrupt answer，不 supersede pending Skill context。
- AC-8：前端 slash 菜单显示 `/skill-help`，提交 capability id 为 `skill_help.respond`，不展示 source path。
- AC-9：任务完成后前端按普通助手消息显示 help 文本，不显示等待补充卡片或业务 artifact 卡片。
- AC-10：所有新增/相关回归测试通过；无新增依赖或 license 风险。

## 7. 风险与缓解

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| Safe view 过滤过松导致内部命令泄露 | 用户看到内部路径/代码结构 | fail-closed 过滤 + internal-token tests + post-check |
| Safe view 过滤过严导致回答“未说明”过多 | 用户体验下降 | 保留结构化 frontmatter、Input Schema、Welcome、字段/参数段；后续逐步改善 Skill authoring |
| Auto planner 误选 help | 普通问题被错误变成文档问答 | `skill_help.respond` 从 auto planner 可选列表排除；仅 `/skill-help` explicit force 可用 |
| `skill_help.respond` 未接入 answer-producing | 多一个 main_agent finalizer，可能重复/泄露 | planner/replanner tests 锁定 |
| pending context 被 help supersede | 用户原业务补全链丢失 | explicit help 不调用 supersede；前端 pending interrupt 阻止；storage/API tests |
| 前端内置命令与现有 skill command 冲突 | slash UX 混乱 | builtin command 单独 registry，不参与 skill command conflict |

## 8. License Requirement

本计划不引入新 Python/Node/Rust 依赖。实施结束前最终说明需记录：`License Requirement：无依赖/许可变更，未触发 cargo-deny 风险`。如实现中新增依赖，必须同步更新依赖快照并执行相应 license gate。
