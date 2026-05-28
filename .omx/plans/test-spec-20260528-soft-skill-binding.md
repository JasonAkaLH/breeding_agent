# Test Spec：Skill 软绑定

日期：2026-05-28  
对应 PRD：`.omx/plans/prd-20260528-soft-skill-binding.md`  
来源设计：`docs/superpowers/specs/2026-05-28-soft-skill-binding-design.md`  
状态：待实施

## 1. 测试目标

验证 `/skill-name` 改为软绑定后，系统满足：

- 外部 direct `skill.*` hard execution 被拒绝。
- Slash command 提交 `main_agent.respond + soft_skill_binding`。
- 主代理基于 public profile 判断 answer 或 execute。
- execute 只触发绑定 Skill 的 deterministic internal DAG。
- 缺参仍产生具体 open interrupt。
- public profile 和 prompt 不泄漏内部实现。
- 非 slash / 自动规划 / Skill finalizer / pending interrupt 现有行为不回归。

## 2. 测试分层

| 层级 | 文件建议 | 目标 |
| --- | --- | --- |
| API | `tests/api/test_soft_skill_binding.py` | submit-message 边界、metadata 校验、direct skill 禁止 |
| Integration | `tests/integrations/codex_skills/test_public_skill_profile.py` | public profile allowlist / no-leak / public_usage |
| Main agent | `tests/capabilities/main_agent/test_soft_skill_binding.py` | answer / execute decision、安全降级、不预执行脚本 |
| Orchestration | `tests/orchestration/test_soft_skill_replanner.py` | deterministic replanner、budget、target validation |
| Frontend domain | `frontend/src/domain/slashCommands.test.ts` | slash intent 改为 soft binding |
| Frontend App | `frontend/src/App.test.tsx` | submit body、upload metadata merge、pending 行为 |
| Frontend menu | `frontend/src/components/SlashCommandMenu.test.tsx` | 不展示 sourcePath / 内部路径 |
| E2E / smoke | `tests/e2e` 或手工 fullstack | answer / interrupt / execute 三类用户路径 |

## 3. API 测试用例

### API-01 direct skill force 被拒绝

请求：`routing_mode=force_capability`、`capability_id=skill.field_design`。

断言：

- 返回 4xx。
- 错误码 `direct_skill_execution_disabled`。
- 不创建 accepted task、message 或 orchestration request。
- 不保存任何会触发 Skill 的 node。
- 若有审计断言，使用 runtime/audit logger 或结构化日志；不要求 task event，因为 task 不应创建。

### API-02 soft binding force main_agent 被接受

请求：`routing_mode=force_capability`、`capability_id=main_agent.respond`、`metadata.soft_skill_binding.capability_id=skill.field_design`。

断言：

- message/task 创建成功。
- task requested capability 为 `main_agent.respond`。
- orchestration request metadata 有 canonical `soft_skill_binding.capability_id=skill.field_design`。
- metadata 有当前 `skill_bundle_revision`。
- 不设置初始 `defer_task_completed_until_pending_skill_context_processed`。

### API-03 soft binding auto 形态被规范化

请求：`routing_mode=auto`、无 capability、metadata soft binding 指向 active Skill。

断言：

- 后端 orchestration request 的 `requested_capability_id` 被规范为 `main_agent.respond`。
- task 可保留原始 routing mode 供审计，但执行计划必须从 `main_agent.respond` 开始。
- default planner 不会先于主代理判断直接规划目标 `skill.*`。

### API-04 soft binding target 不存在

请求绑定 `skill.not_exists`。

断言：4xx，错误码 `invalid_soft_skill_binding`。

### API-05 soft binding target 非 skill

请求绑定 `main_agent.respond` 或其他非 `skill.*`。

断言：4xx，错误码 `invalid_soft_skill_binding`。

### API-06 metadata 伪造内部字段无效

请求 soft binding 同时带：`forced_skill_name`、`forced_skill_capability_id`、`macro_source`、`skill_execution_mode`。

断言：

- 这些字段不进入 node execution metadata。
- 初始 plan 仍是 `main_agent.respond`。
- audit 可记录 dropped malicious keys，但不泄漏敏感内容。

### API-07 direct skill 非 force 情况

请求 `routing_mode=auto` 但显式 `capability_id=skill.field_design`。

断言：同样在创建 message/task 前 fail-closed，错误码 `direct_skill_execution_disabled`，避免调用方绕过 force check。

### API-08 pending context 行为

存在 active pending Skill context 或 open interrupt 时提交 soft binding。

断言：

- 若只是 pending Skill context 兼容兜底，soft binding 作为显式新意图 supersede 旧 pending context。
- 若存在 open interrupt 且 conversation guard 不允许新任务，前端 / 后端明确阻断新 soft binding。
- 任一情况下都不得把 `/skill-name ...` 当作旧 interrupt answer 静默消费。

## 4. Public profile 测试用例

### PROF-01 profile 基本字段

给定 field-design manifest。

断言 profile 包含：

- `capability_id=skill.field_design`
- `name=field-design`
- `display_name`
- `description`
- `parameters` 中的 `material_data`、`design`、`blocks`、`ncols`、`ck_spec`
- required/default/source/aliases/patterns 的用户可见摘要
- outputs 摘要

### PROF-02 不读取 raw body

构造 manifest body 包含唯一字符串 `INTERNAL_ONLY_BODY_SENTINEL`。

断言 profile JSON 不包含该字符串。

### PROF-03 forbidden tokens no-leak

对所有项目级 Skill profile 序列化后断言不包含：

- `source_path`
- `scripts/run_`
- `.py`
- `Rscript`
- `wrapper`
- `platform_service`
- `handler`
- `sidecar`
- `socket`
- `token`
- `secret`
- `postgresql://` / `mysql://`
- 用户本地绝对路径前缀

### PROF-04 public_usage 优先

manifest metadata 有 `public_usage` 时，profile 使用其用户可见描述；只有非项目级测试 fixture 缺少 `public_usage` 时才允许 fallback 到 description / parameters / outputs。项目级 Skill 缺少 `public_usage` 必须由 PROF-05 失败暴露。

### PROF-05 public_usage schema contract

项目级 `skill/*/SKILL.md` 均满足：

- `public_usage` 不为空；项目级 Skill 不允许用 fallback 豁免通过 completion gate。
- `public_usage` 不包含内部路径、脚本名、handler、secret。
- 输入数据字段说明足够回答“数据怎么构建、值是什么”。

## 5. Main agent 测试用例

### MA-01 询问型 slash 不执行 Skill

输入：soft binding field-design + `hyb_check 怎么填？`。

LLM decision fixture：`decision=answer`。

断言：

- `_run_auto_scripts` 不被调用。
- 输出为 text artifact / `response_source=llm`。
- `output_payload` 不含 `soft_skill_decision.execute`。
- prompt 含 public profile，不含 raw body sentinel。

### MA-02 明确执行输出 execute signal

输入：soft binding field-design + `用这个 CSV 做 RCBD，3 个重复`，metadata 有 uploaded artifact 摘要。

LLM decision fixture：`decision=execute`、`confidence=high`、target matches。

断言：

- 不直接调用 script runner。
- output payload 含 `soft_skill_decision.decision=execute`。
- satisfaction `replan_recommended=true`、`reason_code=soft_skill_execute`。
- 不生成用户可见“已完成”最终 answer。
- 不发送用户可见 `main_agent.output_delta/output_final` 正文；若框架需要结果 artifact，只能是内部/非前端展示的 decision payload。

### MA-03 低置信 execute 降级 answer

LLM decision fixture：`decision=execute`、`confidence=low`。

断言：走 answer，不产生 execute signal。

### MA-04 target mismatch 降级 answer

绑定 `skill.field_design`，LLM 输出 target `skill.ocr`。

断言：走 answer，audit 记录 reject reason。

### MA-05 JSON 无效降级 answer

LLM decision 返回 Markdown / broken JSON。

断言：走 answer，不报 task failed。

### MA-06 answer prompt no-leak

answer path 的最终 prompt 断言：

- 包含 public profile 参数摘要。
- 不包含 `manifest.body` sentinel。
- 不包含 forbidden tokens。

### MA-07 non-soft main agent 保持旧行为

普通 `main_agent.respond` 请求无 soft binding。

断言：仍走现有 `_resolve_skill_matches` / auto matching 逻辑，不受 soft decision path 影响。

## 6. Orchestration / replanner 测试用例

### ORCH-01 execute signal 展开绑定 Skill

给定 completed main-agent node output 含 execute signal。

断言 `SoftSkillBindingReplanner.build_replan()` 返回 plan：

- 包含目标 `skill.field_design` macro expansion 后节点。
- metadata 标记 `runtime_replan_source=soft_skill_binding`。
- 如 Skill `requires_finalizer`，包含 finalizer main-agent node。

### ORCH-02 target mismatch 拒绝

request metadata binding 是 `skill.field_design`，output target 是 `skill.ocr`。

断言 replanner 返回 None 或 rejected event，不执行任何 Skill。

### ORCH-03 无 soft binding 拒绝

output 有 execute signal，但 request metadata 无 soft binding。

断言不重排。

### ORCH-04 budget 控制

普通 main-agent plan `max_replans=0`。

断言即使 output 有 bogus signal，也不会应用重排。

### ORCH-05 soft plan budget

soft binding main-agent plan。

断言 `max_replans=1`、`max_dynamic_nodes>=4`。

### ORCH-06 unresolved interrupt 不重排

context `unresolved_interrupt=True`。

断言 soft replanner 不新建 execution DAG。

### ORCH-07 active revision 校验

binding revision 与 active catalog mismatch / target 不在 revision catalog。

断言 fail-closed，不 fallback 到最新 Skill。

### ORCH-08 通用 LLM replanner 不消费 soft execute 信号

SoftSkillBindingReplanner 因 target mismatch / missing binding / revision mismatch 拒绝 execute signal。

断言后续 `MainAgentRuntimeReplanner` 不会把 `satisfaction.reason_code=soft_skill_execute` 当普通 replan 推荐交给 LLM 规划任意 DAG。

## 7. Frontend 测试用例

### FE-01 direct slash ready intent

输入 `/field-design hyb_check 怎么填？`。

断言：

- `content="hyb_check 怎么填？"`。
- submit capability 是 `main_agent.respond`。
- metadata 有 `soft_skill_binding.capability_id=skill.field_design`。
- metadata 保留 `slash_command=/field-design`。

### FE-02 picker selected intent

用户通过 picker 选择 field-design 后输入正文。

断言同 FE-01。

### FE-03 unknown slash 阻断

输入 `/unknown test`。

断言：不提交；提示 unknown。

### FE-04 conflict slash 阻断

构造两个 capability 派生命令冲突。

断言：不提交；提示 conflict。

### FE-05 upload metadata merge

slash soft binding + 已上传文件。

断言 request metadata 同时包含 `soft_skill_binding` 和 `upload_ids`。

### FE-06 Slash menu 不显示 sourcePath

渲染菜单。

断言 UI 文本不包含 `skill/field-design/SKILL.md` 或其他 source path。

### FE-07 capability list 仍派生 slash

`deriveSlashCommands()` 仍从 active public `skill.*` 生成 `/field-design`。

### FE-08 pending interrupt 下 slash 行为

当 App 有 open `pendingInterrupt` 时输入 `/field-design ...`。

断言：

- 前端明确阻断新 soft binding submit，并提示需要先完成或取消当前补充信息。
- 不调用 interrupt answer API，也不把 slash 文本当旧 interrupt answer 静默提交。
- 仅后端 pending Skill context 兼容兜底（无 open interrupt 卡片）才允许 soft binding 作为新意图 supersede。

## 8. E2E / smoke 场景

### E2E-01 解释型

步骤：

1. 启动本地全栈。
2. 打开前端。
3. 输入 `/field-design hyb_check 怎么填？`。

断言：

- 返回字段解释。
- 无 Skill progress / artifact 生成。
- 后端事件无 `skill_execute` 节点。

### E2E-02 缺参执行型

输入 `/field-design 帮我做 RCBD`。

断言：

- 前端显示具体 interrupt 卡片。
- 卡片包含材料文件和重复数 / blocks 要求。
- 不是泛化“正在等待任务给出补充信息”。

### E2E-03 输入齐全执行型

上传合法材料 CSV，输入 `/field-design 用这个 CSV 做 RCBD，3 个重复`。

断言：

- 直接执行 Skill。
- 生成 CSV / HTML artifact。
- finalizer 返回用户可读总结。

### E2E-04 old API direct hard exec

使用 API client 直接提交 `capability_id=skill.field_design`。

断言：返回 `direct_skill_execution_disabled`。

## 9. 回归测试命令

Targeted：

```bash
conda run -n multi_agent python -m unittest tests.api.test_soft_skill_binding
conda run -n multi_agent python -m unittest tests.integrations.codex_skills.test_public_skill_profile
conda run -n multi_agent python -m unittest tests.capabilities.main_agent.test_soft_skill_binding
conda run -n multi_agent python -m unittest tests.orchestration.test_soft_skill_replanner
cd frontend && npm test -- --run slashCommands App SlashCommandMenu
```

Layered regression：

```bash
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/main_agent -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/e2e -p 'test_*.py'
cd frontend && npm test -- --run
cd frontend && npm run build
```

Manual smoke：

```bash
python scripts/run_fullstack_dev.py
```

## 10. Completion gate

完成实施前必须同时满足：

- API direct `skill.*` fail-closed。
- Slash 前端不再提交 `capability_id=skill.*`。
- soft binding answer / execute / missing-input 三条路径都有自动化测试。
- 现有 API 层旧 hard-skill submit 测试已迁移，不再把外部 `capability_id=skill.*` 当合法客户端请求。
- Public profile no-leak 测试覆盖所有项目级 Skill。
- `Skill构建指南.md` 和 API 文档已同步。
- 前后端 targeted + layered regression 通过，或明确记录不可运行原因。
- License Requirement 已记录；如无依赖变更，说明未触发 cargo-deny 风险。
