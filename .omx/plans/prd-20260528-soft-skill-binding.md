# PRD：Skill 软绑定实施计划

日期：2026-05-28  
来源设计：`docs/superpowers/specs/2026-05-28-soft-skill-binding-design.md`  
状态：待实施  
执行模式建议：TDD 优先，分检查点提交；可用 `$team` 并行前端 / 后端 / 文档测试车道，或 `$ultragoal` 做持久目标执行。

## 1. 目标与成功标准

把业务对话台中 `/skill-name` 从“直接硬执行 Skill”改为“软绑定 Skill 语境”：用户点名 Skill 后，主代理基于公开 Skill profile 判断是回答用法、追问缺参，还是触发内部 Skill 执行。外部 submit-message API 不再允许 `force_capability + capability_id=skill.*` 直接硬执行，以防旧客户端绕过主代理判断。

成功标准：

1. `/field-design hyb_check 怎么填？` 只返回字段/格式解释，不运行 `skill.field_design`。
2. `/field-design 用这个 CSV 做 RCBD，3 个重复` 在上传和参数齐全时直接执行内部 Skill，不二次确认。
3. `/field-design 帮我做 RCBD` 在缺材料或参数时生成 open interrupt，前端展示具体 question / required fields。
4. 外部 API 直接提交 `routing_mode=force_capability, capability_id=skill.*` fail-closed，错误码稳定为 `direct_skill_execution_disabled`。
5. 软绑定 prompt 只注入 allowlisted public Skill profile，不注入 raw `SKILL.md` body，不泄漏 `source_path`、script path、wrapper、Rscript、handler、sidecar、secret、DB/LLM provider 等内部信息。
6. 非 slash 普通消息、LLM 自动规划、内部 `skill.*` DAG 展开、Skill finalizer、interrupt 续答不回归。

## 2. 当前代码事实

- 前端 slash command 当前从 active `skill.*` capability 派生命令，并保留 `sourcePath` 字段；ready intent 只返回 slash metadata，没有 soft binding metadata（`frontend/src/domain/slashCommands.ts:3-20`, `frontend/src/domain/slashCommands.ts:26-37`, `frontend/src/domain/slashCommands.ts:93-102`）。
- API submit 当前在 `force_capability` 时只要求存在 `capability_id`，随后 canonicalize + `_ensure_supported_capability`，会把 `requested_capability_id` 保存到 task；没有外部 direct `skill.*` 禁止逻辑（`src/api/runtime.py:319-324`, `src/api/runtime.py:378-385`）。
- API 当前只要 requested capability 是 `skill.*` 就设置 pending context defer metadata（`src/api/runtime.py:429-432`），软绑定后该逻辑不能误作用到初始主代理判断请求。
- `WorkflowRouter` 当前只要 requested capability 以 `skill.` 开头就交给 `SkillWorkflowProvider`，这仍应保留给系统内部 DAG，但外部 submit 不应再直接到达这条路径（`src/orchestration/workflow_router.py:14-22`）。
- `SkillWorkflowProvider` 当前负责把 public `skill.*` macro 展开成真实 Skill 执行或 forced main-agent Skill 执行；这是内部 execute 阶段可复用的确定性执行入口（`src/orchestration/skill_workflow_provider.py:31-78`）。
- `MainAgentWorkflowProvider` 当前对普通主代理计划设置 `max_replans=0`、`max_dynamic_nodes=0`；软绑定初始主代理计划需要只在 soft binding 请求上开放有限 replan budget（`src/capabilities/main_agent/workflow.py:23-40`）。
- `MainAgentRespondCapability.execute()` 当前一开始就解析 Skill match 并调用 `_run_auto_scripts`，因此软绑定不能复用 existing forced Skill match，否则会在判断前运行脚本（`src/capabilities/main_agent/executor.py:87-100`）。
- 当前 main-agent prompt builder 会把 `match.manifest.body` 原样注入 LLM；软绑定必须绕开这条 raw body 注入路径，改用 public profile（`src/capabilities/main_agent/prompt_builder.py:44-53`）。
- `SkillManifest` 已包含 `name`、`description`、`triggers`、`inputs`、`outputs`、`scripts`、`parameters`、`metadata`，足够从 manifest 构造 allowlisted public profile（`src/integrations/codex_skills/manifest.py:12-23`）。
- `build_skill_capability_registry()` 已能根据 public roots 生成 active public `skill.*` descriptor，并提供 `display_name` / `description` / `source_path` 等字段；soft profile 需要复用 public 判断但不能把 `source_path` 暴露给 LLM（`src/integrations/codex_skills/skill_capabilities.py:39-83`, `src/integrations/codex_skills/skill_capabilities.py:101-118`）。
- runtime replanner 已有 `RuntimeReplanner` interface 与 `CompositeRuntimeReplanner`，可插入 deterministic soft-binding replanner，并在 runtime assembly 中排在 `MainAgentRuntimeReplanner` 之前（`src/orchestration/runtime_replanner.py:14-35`, `src/orchestration/runtime_replanner.py:43-56`, `src/api/runtime.py:2240-2249`）。

## 3. 范围

### 3.1 包含

- 后端 submit-message 外部 direct `skill.*` 禁止。
- soft binding metadata 解析、校验、标准化与审计。
- 前端 slash submit 从 hard skill force 改为 `main_agent.respond + metadata.soft_skill_binding`。
- public Skill profile builder。
- 主代理 soft-binding decision flow。
- deterministic soft Skill replanner。
- 缺参 interrupt / finalizer 回归。
- `Skill构建指南.md`、API 文档、相关测试更新。
- 项目级 Skill `SKILL.md` 如需要，补充用户可见 `public_usage` 信息，避免依赖 raw body 回答字段格式。

### 3.2 不包含

- 不新增 `/skill-help` capability。
- 不引入 LangChain / LangGraph / AutoGen。
- 不把 raw `SKILL.md` body 注入软绑定 prompt。
- 不改变内部 workflow expander / SkillWorkflowProvider 对系统内部 `skill.*` macro 的执行能力。
- 不做富文本 token composer 或多轮参数 wizard 重设计。

## 4. 关键设计决策

### D1. 外部 direct `skill.*` fail-closed

API 边界拒绝外部 `force_capability + capability_id=skill.*`，返回稳定错误码 `direct_skill_execution_disabled`。内部 planner / replanner 生成的 `skill.*` 节点不受影响。

理由：用户已明确“不保留硬执行接口，以防不看文档的同事还在调老接口”。静默兼容会延续误执行风险。

### D2. `/skill-name` 强制的是主代理判断，不是 Skill 执行

前端 slash selected / direct parse 后提交：

```json
{
  "routing_mode": "force_capability",
  "capability_id": "main_agent.respond",
  "metadata": {
    "forced_by_slash_command": true,
    "slash_command": "/field-design",
    "soft_skill_binding": {
      "capability_id": "skill.field_design",
      "command": "/field-design"
    }
  }
}
```

### D3. LLM 只判断是否执行绑定 Skill；DAG 由系统生成

主代理输出 answer 或内部 execute signal。execute signal 必须绑定到同一个 `soft_skill_binding.capability_id`；deterministic replanner 验证后复用 Skill macro provider 展开。LLM 不输出任意 DAG，也不能指定内部 node / handler / script。

### D4. Public profile allowlist

新增 `src/integrations/codex_skills/public_profile.py`（建议命名）负责从 `SkillManifest` 生成 public profile。字段只允许业务层信息，禁止包含 `source_path`、`scripts[].path`、raw body、内部 runtime 等。测试采用 allowlist + forbidden token 双保险。

### D5. `public_usage` 作为长期 Skill 写作入口

`SkillManifest.metadata` 已可承载未知 frontmatter。建议在项目级 `SKILL.md` frontmatter 增加可选 `public_usage` mapping，例如：

```yaml
public_usage:
  overview: ...
  input_formats:
    - name: material_data
      description: ...
      example_columns: [ped_id, hyb_check, set]
  examples:
    - /field-design 用这个 CSV 做 RCBD，3 个重复
  outputs:
    - CSV fieldbook
    - HTML layout preview
```

profile builder 优先使用 `public_usage`，fallback 到 description / triggers / parameters / outputs；不得 fallback 到 raw body。

## 5. 分阶段实施计划

### CP-0：测试先行与基线保护

目标：先写失败测试锁住新语义和防泄漏边界。

新增 / 修改测试：

- `tests/api/test_soft_skill_binding.py`
- `tests/integrations/codex_skills/test_public_skill_profile.py`
- `tests/capabilities/main_agent/test_soft_skill_binding.py`
- `tests/orchestration/test_soft_skill_replanner.py`
- `frontend/src/domain/slashCommands.test.ts`
- `frontend/src/App.test.tsx`
- 如涉及 menu sourcePath 下线，更新 `frontend/src/components/SlashCommandMenu.test.tsx`

验收：新增测试在实现前应失败，且失败原因对应新语义缺失，而不是测试 fixture 错误。

### CP-1：API 边界与 metadata 标准化

文件：`src/api/runtime.py`、`src/api/dto.py`（如需 schema 类型注释）、`tests/api/test_soft_skill_binding.py`。

步骤：

1. 在 submit 前置校验中识别外部 direct `skill.*`：`routing_mode=force_capability` 且 canonical capability 以 `skill.` 开头时拒绝。
2. 新增 soft binding 解析 helper：校验 `metadata.soft_skill_binding.capability_id` 是 active public Skill descriptor。
3. 标准化 metadata：写入 `soft_skill_binding.capability_id`、`soft_skill_binding.command`、`soft_skill_binding_source`、`skill_bundle_revision`。
4. drop 用户伪造的内部字段：继续防 `forced_skill_name` / `macro_source` / `skill_execution_mode` / `forced_skill_capability_id` 等进入执行 metadata。
5. soft binding 请求如果 `capability_id` 为空或 auto，可规范成 `main_agent.respond`；如果显式 force，则只允许 `main_agent.respond`。
6. soft binding 初始请求不设置 `defer_task_completed_until_pending_skill_context_processed`；该 flag 只应由内部 Skill 执行节点相关路径设置。
7. direct reject 记录 audit-only 事件或结构化日志，避免泄漏用户全文或上传内容。

验收：API 测试覆盖 reject / accept / invalid binding / malicious metadata。

### CP-2：前端 slash soft submit

文件：`frontend/src/domain/slashCommands.ts`、`frontend/src/App.tsx`、`frontend/src/api/client.ts`、相关测试。

步骤：

1. 修改 `SlashSubmitIntent.ready` 类型，加入 `capabilityId: 'main_agent.respond'` 与 `metadata.soft_skill_binding`。
2. `readyIntent()` 使用 selected command 的 `capabilityId` 填入 binding，而不是作为 submit capability。
3. `App.tsx` submit 时对 slash ready intent 传 `capabilityId=main_agent.respond`。
4. 保持 upload metadata merge、unknown slash blocked、conflict blocked、IME 安全逻辑不变。
5. Slash menu 展示不应继续暴露 `sourcePath`；如果仍需搜索，可仅内部搜索 capability id / display name / description，不在 UI 展示路径。

验收：前端测试断言 `/field-design xxx` 请求体不含 `capability_id=skill.field_design`，而含 `metadata.soft_skill_binding.capability_id=skill.field_design`。

### CP-3：Public Skill Profile Builder

文件：`src/integrations/codex_skills/public_profile.py`、`src/integrations/codex_skills/__init__.py`、`tests/integrations/codex_skills/test_public_skill_profile.py`。

步骤：

1. 定义 `PublicSkillProfile` dataclass 或 plain dict builder。
2. 输入：`SkillManifest`、`capability_id`、可选 descriptor display info。
3. 输出字段：`capability_id`、`name`、`display_name`、`description`、`triggers`、`parameters`、`inputs`、`outputs`、`public_usage`。
4. 参数摘要来自 `manifest.parameters`：保留 name/type/required/source/default/enum/aliases/patterns 的用户可见形式；不输出内部 resolver diagnostics。
5. `public_usage` 只读取 manifest metadata 中 allowlisted keys；不读取 `manifest.body`。
6. 添加 forbidden token scanner 测试：`source_path`、`scripts/run_`、`Rscript`、`wrapper`、`platform_service`、`handler`、`sidecar`、绝对路径、token / secret / DSN 均不得出现在 JSON 序列化后的 profile 中。

验收：对 field-design / OCR / rice-genie / field-analysis / sql-query profile 均通过 no-leak 断言。

### CP-4：主代理 soft-binding decision flow

文件：`src/capabilities/main_agent/executor.py`、`src/capabilities/main_agent/prompt_builder.py` 或新增 `soft_skill_binding.py`、相关测试。

步骤：

1. 在 `MainAgentRespondCapability.execute()` 开头检测 `soft_skill_binding`；该路径必须发生在 `_resolve_skill_matches()` 和 `_run_auto_scripts()` 之前，避免判断前执行脚本。
2. 解析绑定 Skill manifest；缺失则返回普通 answer 或结构化 error，不 fallback 到任意 Skill。
3. 构造 decision prompt：包含用户问题、脱敏上传摘要、public profile、固定 JSON schema。
4. 使用现有 LLM runtime 做静默 decision 生成：可复用 stream generator 收集 JSON，或注入独立 decision generator；实现时优先选择最少侵入方式。
5. 解析 decision：仅允许 `answer|execute`；低置信 / JSON 无效 / target mismatch / 非绑定 Skill 一律降级 answer。
6. answer 路径调用正常 streaming answer prompt，但注入 public profile 而不是 raw Skill body，且禁用 auto scripts。
7. execute 路径返回 output payload：`soft_skill_decision` + `satisfaction.replan_recommended=true`，不展示伪最终答案。
8. 输出 audit-only 事件：`soft_skill_binding.decision`，记录 decision / reason_code / confidence / target capability，不记录 raw prompt。

验收：询问型输入不产生 script result；execute 型输入产生 deterministic signal；无效 JSON 安全降级。

### CP-5：Deterministic Soft Skill Replanner

文件：建议 `src/orchestration/soft_skill_replanner.py` 或 `src/capabilities/main_agent/soft_skill_replanner.py`、`src/api/runtime.py` assembly、`tests/orchestration/test_soft_skill_replanner.py`。

步骤：

1. 实现 `RuntimeReplanner`：只识别 completed main-agent node output 中的 `soft_skill_decision.decision=execute`。
2. 验证 target capability 等于 request metadata 的 binding capability。
3. 验证 target still active public Skill in same `skill_bundle_revision`。
4. 构造 public macro plan：单个 `WorkflowNodePlan(capability_id=target_skill, input_payload={user_message})`，metadata 标记 `macro_source=soft_skill_binding_replanner`。
5. 通过现有 `WorkflowExpander` 展开，复用 `SkillWorkflowProvider` finalizer / executor 逻辑。
6. `max_replans` / `max_dynamic_nodes` 只在 soft-binding initial main-agent plan 上开放；普通主代理仍为 0。
7. 在 runtime assembly 的 `CompositeRuntimeReplanner` 中排在 `MainAgentRuntimeReplanner` 之前。

验收：execute signal 可以展开到 Skill 执行节点；target mismatch / missing binding / budget exhausted 均拒绝。

### CP-6：MainAgentWorkflowProvider soft budget

文件：`src/capabilities/main_agent/workflow.py`、相关 orchestration tests。

步骤：

1. `build_plan()` 检测 request metadata 是否存在 valid soft binding。
2. 软绑定 main-agent node metadata 携带 binding summary 与 revision。
3. 软绑定 plan 设置 `max_replans=1`、`max_dynamic_nodes` 足够容纳目标 Skill macro expansion 和 finalizer（建议最小 4，若多节点 Skill 扩展则按现有 expander worst-case 调整）。
4. 非软绑定 plan 保持 `max_replans=0`、`max_dynamic_nodes=0`。

验收：普通对话不获得重编排预算；软绑定 execute 可以进入 replanner。

### CP-7：Skill public usage 与指南 / API 文档

文件：`Skill构建指南.md`、`docs/api/api-doc.html`、`skill/*/SKILL.md`、Skill manifest tests。

步骤：

1. 在 `Skill构建指南.md` 增加 soft binding / public usage 标准：说明写什么、禁止写什么、如何描述数据格式与字段值。
2. 为项目级 Skill 补充 `public_usage`（至少 field-design、field-analysis、ocr、rice-genie、sql-query），避免软绑定回答依赖 raw body。
3. API 文档更新 submit-message：direct `skill.*` 禁止；点名 Skill 使用 `metadata.soft_skill_binding`。
4. capability list 文档说明 `skill.*` capability 仍用于发现和内部编排，不代表客户端可直接 request。

验收：manifest contract 测试验证 public_usage 存在、无内部实现泄漏。

### CP-8：端到端回归与手工 smoke

步骤：

1. 后端分层测试：api / integrations / capabilities/main_agent / orchestration / e2e 相关 unittest。
2. 前端 `npm test -- --run`、`npm run build`。
3. 本地全栈 smoke：`python scripts/run_fullstack_dev.py` 后用 Chrome/Browser 验证：
   - `/field-design hyb_check 怎么填？` 不执行 Skill。
   - `/field-design 帮我做 RCBD` 展示具体缺参卡片。
   - `/field-design 用上传 CSV 做 RCBD，3 个重复` 直接执行。
4. License Requirement：若未改依赖，最终报告写明“无依赖/许可变更，未触发 cargo-deny 风险”。

## 6. 接口与数据契约

### 6.1 Soft binding metadata

```json
{
  "soft_skill_binding": {
    "capability_id": "skill.field_design",
    "command": "/field-design"
  },
  "soft_skill_binding_source": "slash_command"
}
```

Canonical metadata 应由后端规范化，不信任用户附带的 `skill_name`、`forced_skill_name` 或 revision。

### 6.2 Decision output

```json
{
  "decision": "answer | execute",
  "confidence": "low | medium | high",
  "reason_code": "user_asks_usage | explicit_execution_request | unsafe_or_unclear",
  "answer_intent": "input_format | parameter_meaning | usage_example | capability_fit | other",
  "target_capability_id": "skill.field_design"
}
```

### 6.3 Execute output payload

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

## 7. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| LLM 误执行询问型输入 | execute 需要 valid JSON + medium/high confidence + target matches binding；失败默认 answer；测试覆盖“怎么填/支持什么/示例是什么”。 |
| 软绑定 answer 泄漏内部实现 | public profile allowlist；不读取 raw body；forbidden token tests。 |
| 老客户端因 hard skill API 下线失败 | fail-closed 错误码明确；API 文档同步；audit 记录 direct reject。 |
| execute 后缺参仍泛化等待 | CP-8 验证 open interrupt；继续使用 manifest / structured missing_input contract。 |
| 普通 main_agent 被意外重编排 | 只有 soft binding plan 开 replan budget；普通 plan 保持 0。 |
| LLM 输出任意 DAG | LLM 只输出 bound execute signal；DAG 由 deterministic replanner 构造。 |
| Skill public_usage 维护成本 | 指南和 manifest tests 将其变为项目级 Skill contract，fallback 保证已有参数仍可解释。 |

## 8. 验证命令

后端 targeted：

```bash
conda run -n multi_agent python -m unittest tests.api.test_soft_skill_binding
conda run -n multi_agent python -m unittest tests.integrations.codex_skills.test_public_skill_profile
conda run -n multi_agent python -m unittest tests.capabilities.main_agent.test_soft_skill_binding
conda run -n multi_agent python -m unittest tests.orchestration.test_soft_skill_replanner
```

后端 regression：

```bash
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/main_agent -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/e2e -p 'test_*.py'
```

前端：

```bash
cd frontend
npm test -- --run
npm run build
```

手工 smoke：

```bash
python scripts/run_fullstack_dev.py
```

## 9. Follow-up staffing guidance

推荐执行路径：`$ultragoal` 作为 durable goal owner，必要时配合 `$team` 并行。

可并行车道：

1. **Backend API / orchestration lane（executor/debugger）**：CP-1、CP-5、CP-6。
2. **Main-agent / profile lane（executor/test-engineer）**：CP-3、CP-4。
3. **Frontend lane（executor）**：CP-2。
4. **Docs / Skill contract lane（writer/test-engineer）**：CP-7。
5. **Verification lane（verifier）**：CP-8，跨 lane 整体验收。

Team launch hint：

```text
$team 按 .omx/plans/prd-20260528-soft-skill-binding.md 和 .omx/plans/test-spec-20260528-soft-skill-binding.md 执行，分 backend-api-orchestration、main-agent-profile、frontend、docs-skill-contract、verification 五个 lane。
```

Ultragoal hint：

```text
$ultragoal 以 Skill 软绑定为目标，按 CP-0 到 CP-8 checkpoint 推进并记录每阶段测试证据。
```

Ralph fallback：仅当用户明确要求单 owner 持续修复 / 验证循环时使用 `$ralph`；本计划本身可拆分并行，默认不建议 Ralph 独占执行。
