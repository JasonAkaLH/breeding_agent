# Test Spec: Skill Help 只读文档问答

状态：Ready for TDD
关联计划：`.omx/plans/prd-20260528-skill-help-readonly-doc-qa.md`
来源设计：`docs/superpowers/specs/2026-05-28-skill-help-readonly-doc-qa-design.md`

## 1. 测试目标

验证 `skill_help.respond` 提供 slash-only 只读 Skill 文档问答：只有显式 `/skill-help` 才进入该 capability；普通/自然语言对话不做 Skill Help 解析、不默认进入 help；该能力能解析目标 Skill、基于 `SKILL.md` 安全视图回答、不会执行目标 Skill、不会泄露内部代码结构，并且前端 `/skill-help` 不误入普通 Skill slash 或 interrupt answer 流程。

## 2. 测试矩阵

| ID | 层级 | 场景 | 输入 | 期望 |
| --- | --- | --- | --- | --- |
| T1 | orchestration | help capability 注册 | runtime startup | `skill_help.respond` public descriptor、planner policy、local instance 存在 |
| T2 | orchestration | force help 路由 | `requested_capability_id=skill_help.respond` | 单节点 `skill_help.respond` plan，不落 default planner |
| T3 | orchestration | answer-producing 判定 | LLM planner 输出 help node | 不追加 `main_agent.respond` finalizer |
| T4 | api | explicit slash fallback | content `/skill-help field-design ...`、capability 空 | 后端 canonical 到 `skill_help.respond` |
| T5 | api | explicit slash from frontend | capability `skill_help.respond`、content `field-design ...` | 不生成 `skill.field_design` 节点 |
| T6 | api | pending Skill context | active pending context + `/skill-help ...` | 不 supersede pending context，不作为 continuation |
| T7 | api/frontend | pending interrupt | UI 有 pending interrupt + `/skill-help ...` | 不调用 answer interrupt；显示阻止提示 |
| T8 | capability | capability id 解析 | `skill.field_design` | 解析 field-design |
| T9 | capability | Skill name 解析 | `field-design` | 解析 field-design |
| T10 | capability | display name 解析 | `试验设计智能体` | 解析 field-design |
| T11 | capability | 含空格 display name | `/skill-help "OCR 文档识别" 需要什么输入` | 解析 OCR 文档识别 |
| T12 | capability | 无引号最长前缀 | `/skill-help OCR 文档识别 需要什么输入` | 解析完整 display name，而不是只解析 `OCR` |
| T13 | capability | 未知 Skill | `/skill-help unknown x` | 不调用 LLM，返回候选列表 |
| T14 | capability | 多匹配 Skill | 构造 normalized 冲突 catalog | 不调用 LLM，返回澄清候选 |
| T15 | capability | safe view 内部过滤 | field-design SKILL.md | prompt 不含 `scripts/run_field_design.py`、`Rscript`、`Set-Variable`、`source_path` |
| T16 | capability | 文档未说明 | 问不存在字段 | 回答包含“该 Skill 文档未说明” |
| T17 | capability | 数据格式模板 | 问 `hyb_check` / 输入表 | 回答可包含安全 CSV 模板，不编造完整枚举 |
| T18 | api | 非 slash 自然语言文档问题 | `field-design 的 hyb_check 有什么要求？` | 不进入 `skill_help.respond`；保持既有普通对话 / planner 路由 |
| T19 | api | 执行类请求 | `帮我跑 field-design 做 RCBD` | 不进入 `skill_help.respond`；保持既有 Skill / 主代理路线 |
| T20 | api | 非 slash 混合意图 | `field-design 怎么用，顺便帮我跑` | 不进入 `skill_help.respond`；不做 Skill Help 解析 |
| T21 | frontend | slash menu | 输入 `/` | 出现内置 `/skill-help` |
| T22 | frontend | submit metadata | `/skill-help field-design x` | `capabilityId=skill_help.respond`，metadata `skill_help=true` |
| T23 | frontend | menu 不泄露路径 | `/skill-help` 菜单/候选 | 不显示 `source_path` |
| T24 | frontend | 普通结果展示 | help task completed + text artifact | 助手气泡显示普通文本，无业务 artifact 卡片 |

## 3. 后端测试文件建议

### `tests/capabilities/skill_help/test_doc_view.py`（新增）

覆盖 safe view 和解析：

1. `test_resolves_skill_by_capability_id_name_display_name_and_normalized_alias`
   - 构造含 `capability_id=skill.field_design`、`name=field-design`、`display_name=试验设计智能体` 的 manifest。
   - 断言四种引用都唯一解析。

2. `test_resolves_quoted_and_unquoted_display_name_with_spaces_by_longest_prefix`
   - 构造 `display_name=OCR 文档识别` 和另一个 `OCR` 前缀候选。
   - `/skill-help "OCR 文档识别" 需要什么输入` 与 `/skill-help OCR 文档识别 需要什么输入` 都解析完整 display name。

3. `test_ambiguous_or_unknown_skill_returns_slash_help_result_without_llm`
   - 注入 fake text generator，统计调用次数。
   - unknown / duplicate normalized match 时调用次数为 0。

4. `test_safe_view_removes_internal_paths_commands_and_runtime_details`
   - 使用真实 `skill/field-design/SKILL.md` 或内联 manifest。
   - 断言 safe view JSON/markdown 不含：`scripts/run_field_design.py`、`Rscript`、`Set-Variable`、`PowerShell`、`runtime:`、`source_path`、`wrapper`、`.R`。
   - 断言仍保留：`ped_id`、`hyb_check`、`set`、`RCBD`、`Diagonal`、`Interval`。

5. `test_prompt_requires_unspecified_answer_and_user_visible_source_only`
   - 生成 prompt。
   - 断言包含“该 Skill 文档未说明”和“来源”。
   - 断言不包含本地路径。

### `tests/capabilities/skill_help/test_executor.py`（新增）

覆盖 capability 输出合同：

1. `test_executor_answers_from_safe_view_and_returns_text_artifact`
   - fake generator 返回固定答案。
   - result 无 interrupt、无 file/json artifact，只有 text artifact。
   - output_payload 含 `response_source=skill_help` 与 resolved skill metadata。

2. `test_executor_does_not_call_llm_for_unknown_or_ambiguous_skill`
   - fake generator call count = 0。
   - 返回普通 text artifact，说明未知/候选。

3. `test_executor_fails_closed_when_safe_view_is_empty`
   - body 全是内部命令/路径。
   - 不调用 LLM，返回“该 Skill 文档未提供可安全展示的说明”。

4. `test_executor_post_check_blocks_internal_leakage`
   - fake generator 返回含 `scripts/run_field_design.py` 的答案。
   - 期望安全失败/过滤后答案不含泄露词。

### `tests/orchestration/test_skill_help_workflow_provider.py`（新增）

1. `test_skill_help_workflow_provider_builds_single_node_plan`
   - request force `skill_help.respond`。
   - plan 只有一个 node，capability 为 `skill_help.respond`，metadata 带 `skill_bundle_revision`。

2. `test_workflow_router_routes_skill_help_to_help_provider`
   - fake default/main/skill/help providers。
   - `skill_help.respond` 命中 help provider，不命中 default。

3. `test_llm_provider_treats_skill_help_as_answer_producing`
   - fake planner 输出单个 `skill_help.respond` 节点。
   - expanded plan 不追加 `main_agent.respond`。

4. `test_runtime_replanner_treats_skill_help_as_answer_producing`
   - runtime replan 输出 help node。
   - 不追加 main agent finalizer。

### `tests/api/test_skill_help_api.py`（新增）

1. `test_runtime_registers_skill_help_capability_and_instance`
   - `GET /api/v1/capabilities` 含 `skill_help.respond`。
   - descriptor public active，kind/source 非 `skill`。

2. `test_slash_help_fallback_canonicalizes_to_skill_help_capability`
   - 提交 content `/skill-help field-design hyb_check 有什么要求`，capability 空。
   - task.requested_capability_id 为 `skill_help.respond`。

3. `test_forced_skill_help_does_not_create_business_skill_node`
   - 提交 capability `skill_help.respond`，content `field-design ...`。
   - nodes 只含 `skill_help.respond`，不含 `skill.field_design`。

4. `test_non_slash_skill_doc_question_does_not_route_to_skill_help`
   - content `field-design 的 hyb_check 有什么要求？`。
   - task/nodes 不进入 `skill_help.respond`；assert 无 help node。

5. `test_execution_request_is_not_routed_to_skill_help`
   - content `帮我跑 field-design 做 RCBD`。
   - 不进入 `skill_help.respond`。

6. `test_auto_planner_cannot_select_skill_help`
   - fake planner 尝试输出 `skill_help.respond`。
   - plan validation / capability filter 拒绝或修复为非 help，除非 request 是 explicit `/skill-help`。

7. `test_skill_help_does_not_supersede_pending_skill_context`
   - 预置 pending skill context。
   - 提交 force `skill_help.respond`。
   - pending context 仍 active，未 superseded。

8. `test_skill_help_no_interrupt_no_business_artifacts`
   - 完成后 interrupts 列表为空。
   - artifacts 只有 text 类型，无 file/json/data-query artifact。

## 4. 前端测试文件建议

### `frontend/src/domain/slashCommands.test.ts`

新增/调整：

1. `it('adds builtin /skill-help without relying on skill capabilities')`
   - capabilities 只有 main_agent 或空 skill list。
   - commands 仍包含 `/skill-help`。

2. `it('submits /skill-help as builtin skill_help.respond with metadata')`
   - input `/skill-help field-design hyb_check 有什么要求`。
   - intent ready，`capabilityId=skill_help.respond`，content 为 `field-design hyb_check 有什么要求`，metadata 含 `skill_help: true`、`slash_command: '/skill-help'`。

3. `it('does not mark builtin /skill-help as skill command conflict')`
   - 即使存在 `skill.skill_help` 或类似 capability，builtin `/skill-help` 仍稳定。

4. `it('does not include sourcePath in builtin help command search/display model')`
   - command.sourcePath 为空或 undefined。

### `frontend/src/App.test.tsx`

新增/调整：

1. `it('shows /skill-help in slash menu and submits builtin help capability')`
   - 打开 slash menu。
   - 选择 `/skill-help`。
   - 输入 `field-design hyb_check 有什么要求`。
   - mock submitMessage 收到 `capabilityId: 'skill_help.respond'`，metadata `skill_help: true`。

2. `it('blocks /skill-help while an interrupt answer is pending')`
   - 构造 pending interrupt UI。
   - 输入 `/skill-help field-design x`。
   - 断言未调用 `answerInterrupt` / `handleInterruptAnswer` 对应 API。
   - 显示提示“请先回答或取消当前补充信息请求后再查看 Skill 帮助”。

3. `it('renders skill help result as ordinary assistant text')`
   - mock task completed + text artifact。
   - 断言助手气泡显示 help 文本。
   - 不显示 interrupt card、Skill status、file/data artifact card。

4. `it('does not display source_path for /skill-help menu item')`
   - menu 文本中无 `SKILL.md` 路径。

### `frontend/src/components/SlashCommandMenu.test.tsx`

- 覆盖 builtin command meta：无 sourcePath 时不渲染 path meta；若普通 skill 有 sourcePath 仍保持既有行为或按新 UX 规则展示。

## 5. 安全 / No-leak 断言

使用 sentinel：`INTERNAL_SHOULD_NOT_LEAK_20260528`。

后端测试必须扫描：

- safe view serialized text。
- LLM prompt。
- `CapabilityExecutionResult.output_payload`。
- text artifact `storage_ref`。
- event payload / audit payload seam。

禁止出现：

```text
scripts/run_field_design.py
Rscript
Set-Variable
PowerShell
source_path
handler
module
runtime:
factory
allowlist
outputs/field-design
*_result.json
INTERNAL_SHOULD_NOT_LEAK_20260528
```

允许出现的用户可见词示例：

```text
ped_id
hyb_check
set
RCBD
Diagonal
Interval
试验设计智能体
该 Skill 文档未说明
来源：试验设计智能体 Skill 文档
```

## 6. 回归命令

后端 targeted：

```bash
conda run -n multi_agent python -m unittest tests.capabilities.skill_help.test_doc_view
conda run -n multi_agent python -m unittest tests.capabilities.skill_help.test_executor
conda run -n multi_agent python -m unittest tests.orchestration.test_skill_help_workflow_provider
conda run -n multi_agent python -m unittest tests.api.test_skill_help_api
```

后端分层回归：

```bash
conda run -n multi_agent python -m unittest discover -s tests/core -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/integrations/codex_skills -p 'test_*.py'
```

前端：

```bash
cd frontend
npm test -- --run
npm run build
```

## 7. 手工 smoke

1. 启动本地前后端。
2. 输入 `/skill-help field-design hyb_check 有什么要求`。
3. 期望普通助手文本包含：`hyb_check=0` 的规则、非零/Diagonal/Interval 中相应规则、来源为“试验设计智能体 Skill 文档”。
4. 确认 Chrome/前端无“等待补充信息”、无上传卡片、无业务 Skill 状态卡片。
5. 检查任务节点：没有 `skill.field_design`。
6. 输入 “field-design 的 hyb_check 有什么要求？” 验证不会进入 `skill_help.respond`。
7. 输入 “帮我跑 field-design 做 RCBD” 验证不会进入 `skill_help.respond`，保持既有执行路由。
8. 触发一个 Skill missing-input interrupt 后输入 `/skill-help field-design x`，确认前端阻止而非提交补充答案。

## 8. 通过标准

- T1-T24 全部通过。
- No-leak sentinel 扫描通过。
- `/skill-help` 与普通 `skill.*` slash command 都保持可用；非 `/skill-help` 对话不进入 `skill_help.respond`。
- pending interrupt / pending Skill context 不被 help 请求消费或 supersede。
- 所有 targeted + 分层回归通过。
- License Requirement：无依赖/许可变更，未触发 cargo-deny 风险。
