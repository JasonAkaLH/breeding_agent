# Thinking 状态感知的模型 Reasoning Effort 配置实施计划

依据：`2026-08-27-thinking-state-reasoning-effort-configuration-design.md`

设计提交：`d4154ae`

状态：`ready_for_implementation`

目标分支：`main`

## 1. 完成声明

唯一目标是把当前 per-model reasoning effort 合同迁移为“公共 effort 目录 +
`thinking.enabled/disabled` 独立 `supported/default` 策略”，并让前端在关闭 thinking 后仍能选择当前
模型支持的强度。

完成必须同时满足：

- 五个当前模型均使用新 schema；
- DeepSeek V4 Flash/Pro GA 与 GLM-5.2 在 enabled/disabled 下均可选择六档；
- 豆包 Seed 2.1 Pro/Turbo 在 enabled 下可选择四档、disabled 下只可选择 `minimal`；
- 切换 thinking 或模型时合法值保留，非法值才回退到新状态默认值；
- 后端在 provider 调用前拒绝非法状态/effort 组合；
- 旧 schema 不存在生产 fallback；
- 自动化门禁和 52 组合脱敏真实 smoke 通过；
- 本地敏感 `config.yaml` 完成迁移但仍被 Git 忽略且不进入任何提交；
- 不修改数据库、Rust sidecar、部署、`prod` 或无关 LLM/Agent 行为。

## 2. 基线与约束

### 2.1 已确认基线

- 当前分支：`main`。
- 批准设计基线：`d4154ae`。
- 当前后端 schema：`default / disabled_default / options[].allow_when_thinking_disabled`。
- 当前前端在 `effectiveDeepThinking=false` 时禁用思考强度 Select。
- 2026-08-27 已完成 enabled/disabled 共 52 个真实 provider 组合探测：46 个接受、6 个豆包
  disabled 非 minimal 组合返回 HTTP 400 `InvalidParameter`。
- 现有相关回归基线：后端 reasoning/API 定向 15 项通过；前端相关 3 项通过。

### 2.2 敏感配置边界

根 `config.yaml` 含本地凭据并由 `.gitignore` 明确忽略。实施时：

- 只手术式修改五个模型的 `reasoning_efforts` block；
- 不打印文件、密钥、base URL、数据库 DSN 或无关字段；
- 不使用 `git add -f`，不创建可被 Git 扫描的明文副本；
- 每个提交前验证 `config.yaml` 仍被忽略且未被跟踪；
- 版本化 schema authority 由源码、测试 fixtures、API 文档和本计划承载；
- 不宣称本地配置迁移等同开发/生产部署配置已经发布。

`docker_cmd.md` 继续遵守绝对保护规则：不读取内容、不移动、不修改、不暂存；只允许用文件状态和
`git check-ignore` 验证其仍存在、权限不高于 `0600`、被忽略且未被跟踪。

### 2.3 实施原则

- 先写能在旧实现上失败的定向测试，再修改实现。
- 不保留双 schema parser、API response union 或前端兼容 decoder。
- 不新增依赖，不抽取无关 App/LLM abstraction。
- 每个检查点只提交自身范围；本地 `config.yaml` 永不进入提交。
- 中间提交只用于开发检查点，不单独作为可部署版本；发布单位是全部检查点的完整序列。

## 3. Checkpoint A：后端配置合同、Resolver 与 API 投影

### A1. 先建立红测

新增 `tests/integrations/test_model_editions.py`，直接锁定配置 dataclass、parser 和启动校验：

- 正确解析公共 options 与 enabled/disabled policy；
- `options` 展示顺序保持不变；
- 中文 label 原样保留，缺失 label 回退 value；
- options 空值、重复 value 失败；
- supported 空值、重复 value、引用未知 option 失败；
- 公共 option 未被任一状态引用时以 orphan error 失败；
- enabled supported 为空、default 为空或 default 不属于 supported 时失败；
- disabled supported 为空且 default 为 null 时合法；
- disabled supported 为空但 default 非空时失败；
- disabled supported 非空但 default 为空或不属于 supported 时失败；
- 旧 `default / disabled_default / allow_when_thinking_disabled` schema 失败；
- 新旧字段混用失败；
- `policy_for()`、`supported_values()`、`supports()`、`default_for()` 无 I/O 查询准确。

先把 `tests/integrations/test_llm_request_options.py` 的两模型 fixture 改为新 schema，并新增：

- DeepSeek disabled `high/max` 合法；
- 豆包 disabled `low/medium/high` 非法；
- enabled/disabled 未显式 effort 时各自使用状态 default；
- 已选中模型状态策略时，factory fallback 不得覆盖该状态 default；
- enabled/disabled 显式未知 effort 失败；
- disabled supported 为空时拒绝关闭 thinking；
- 多模型缺失 model edition 继续 fail closed；
- injected registry 继续不读取根配置。

红测命令：

```bash
conda run -n multi_agent python -m unittest \
  tests.integrations.test_model_editions \
  tests.integrations.test_llm_request_options
```

预期：旧实现因缺少新 dataclass/policy、仍读取旧字段或仍按 disabled-safe 布尔值判断而失败。不得为
让红测变绿先降低断言。

### A2. 替换配置值对象与 parser

修改 `src/integrations/model_editions.py`：

- `ReasoningEffortOption` 只保留 `value`、`label`；
- 新增 `ReasoningEffortStatePolicy(default, supported)`；
- 新增 `ReasoningEffortThinkingPolicy(enabled, disabled)`；
- `ReasoningEffortConfig` 改为 `options + thinking`；
- 实现 `policy_for()`、`supported_values()`、`supports()`、`default_for()`；
- `_parse_reasoning_efforts()` 解析公共 options 和两个必填状态策略；
- supported 使用 YAML sequence；parser 保留原始顺序和重复项，让 validator 能明确拒绝重复；
- 在拥有 model edition 上下文的解析/校验边界报告旧字段和新旧混用错误；
- label 缺失继续回退 value；不新增跨模型默认值。

`validate_model_reasoning_effort_configs()` 按批准设计一次性收集模型级错误。错误必须包含模型、状态、
字段或 offending value，但不能包含完整配置或敏感顶层字段。

### A3. 更新请求 resolver

修改 `src/integrations/llm_request_options.py`：

- 根据 `thinking_enabled` 选择 `cfg.thinking.enabled` 或 `cfg.thinking.disabled`；
- 显式 effort 只按当前 policy 的 supported 校验；
- 未显式 effort 时使用当前 policy default；
- disabled supported 为空时返回“该模型不能关闭 deep thinking”；
- 移除 `disabled_safe_values()`、`disabled_default`、`has_value()`、
  `allows_when_thinking_disabled()` 路径；
- 保留 model edition 选择和 injected registry 边界。

一旦 registry 选中了模型配置，candidate 必须是“显式 effort，否则当前状态 default”；
`fallback_reasoning_effort` 不得覆盖状态 default。该参数只可保留在没有 registry 的低层兼容 seam；
`build_agent_model_binding()` 不再把 factory 的 `main_agent_reasoning_effort` 作为配置模型 fallback。
`_resolve_main_agent_stream_binding()` 中与 injected stream/static metadata 有关的既有 factory 参数保持原职责，
不借本任务扩大删除范围。

稳定错误至少区分：

- unknown effort for model/state；
- known effort but unsupported in current state；
- disabled state unsupported；
- missing/invalid state default。

### A4. 更新 API DTO 与投影

修改：

- `src/api/dto.py`；
- `src/api/runtime.py`；
- `src/api/routes/config.py` 仅在 DTO 构造确有需要时修改。

新增公开 DTO：

- `ReasoningEffortStatePolicyResponse`；
- `ReasoningEffortThinkingPolicyResponse`。

`ReasoningEffortConfigResponse` 改为 `options + thinking`；`ReasoningEffortOptionResponse` 删除
`allow_when_thinking_disabled`。`ApiRuntime.model_editions_payload()` 必须从已校验值对象原样投影，不在
序列化层补默认值、重新排序或过滤错误。

### A5. 迁移后端 fixtures 与本地五模型配置

机械迁移以下版本化测试配置，只改变 reasoning schema：

- `tests/api/support.py`；
- `tests/api/test_conversation_titles.py`；
- `tests/api/test_model_edition_selection.py`；
- `tests/api/test_skill_output_artifacts.py`；
- `tests/api/test_user_mcp_runtime_wiring.py`；
- `tests/api/test_user_mcp_task_assignment_restart.py`；
- `tests/integrations/test_agent_model_gate.py`；
- `tests/integrations/test_llm_client.py`；
- `tests/integrations/test_llm_request_options.py`；
- `tests/integrations/test_llm_runtime.py`；
- `tests/fixtures/unified_agent_loop_clean_archive_config.yaml`。

需要表达 DeepSeek-like 测试能力时，disabled supported 必须包含该 fixture 声明的全部合法档位；需要表达
豆包约束时，disabled 只包含 `minimal`；需要表达强制 thinking 时，disabled 使用
`default: null, supported: []`。

本地忽略文件 `config.yaml` 的五个 block 按真实 smoke 结果迁移：

- DeepSeek V4 Flash/Pro GA、GLM-5.2：enabled/disabled 都是六档，默认分别为 high/minimal；
- 豆包 Seed 2.1 Pro/Turbo：enabled 四档、disabled 仅 minimal，默认分别为 high/minimal。

不得输出 `config.yaml` diff；只用 parser 加载成功、模型/状态/effort 的脱敏摘要和 Git ignore 状态验证。

### A6. 后端绿测与提交

定向验证：

```bash
conda run -n multi_agent python -m unittest \
  tests.integrations.test_model_editions \
  tests.integrations.test_llm_request_options \
  tests.integrations.test_llm_client \
  tests.integrations.test_llm_runtime \
  tests.integrations.test_agent_model_gate

conda run -n multi_agent python -m unittest tests.api.test_model_edition_selection
```

静态检查：

- 生产 Python/TypeScript 不再引用旧字段；
- 旧字段只允许保留在历史设计、API 历史说明以及明确的 rejection tests；
- `config.yaml` 和 `docker_cmd.md` 均保持 ignored/untracked；
- `git diff --check` 通过。

检查点提交：

```text
feat(llm): model reasoning efforts by thinking state
```

提交只含版本化源码和测试；本地 `config.yaml` 不暂存。

## 4. Checkpoint B：前端状态感知的强度选择

### B1. 先更新前端 fixtures 并建立红测

修改 `frontend/src/App.test.tsx` 的三类 reasoning config：

- DeepSeek-like：两种状态都支持全部目录项；
- Doubao-like：enabled 四档，disabled 仅 minimal；
- Force-thinking：enabled 有 high，disabled 为空且 default null。

新增或改写测试：

- 替换“thinking 关闭时禁用 Select”为“thinking 关闭时 Select 仍可操作”；
- DeepSeek-like disabled 下展示并可选择 high/max，提交保持 `deepThinking=false`；
- DeepSeek-like enabled high 切到 disabled 后仍为 high；
- Doubao enabled high 切到 disabled 后回退 minimal；
- Doubao disabled 下只展示 minimal；
- 从 DeepSeek disabled max 切到豆包时回退 minimal；
- 从豆包 disabled minimal 切到 DeepSeek 时保留 minimal；
- force-thinking 模型继续显示只读“已开启”并提交 enabled/high；
- 损坏响应继续显示配置错误并禁用发送。

修改 `frontend/src/api/client.test.ts` 的 model-editions response fixture，并保留“App 提供的 effort 在
disabled 状态也被精确透传”断言。把任何声称“只有 thinking enabled 才提交 effort”的旧测试名称改为真实
语义，客户端实现不增加配置推断。

红测命令：

```bash
cd frontend
npm test -- --run src/App.test.tsx src/api/client.test.ts
```

### B2. 更新类型与 App helpers

修改 `frontend/src/api/types.ts`：

- `ReasoningEffortOption` 删除 disabled 布尔值；
- 新增 `ReasoningEffortStatePolicy` 和 `ReasoningEffortThinkingPolicy`；
- `ReasoningEffortConfig` 改为 `options + thinking`。

在 `frontend/src/App.tsx` 现有顶部 pure helper 区内手术式替换，不另建抽象层：

- `validReasoningConfig()` 验证目录、两个状态、supported 引用和 default；
- `reasoningPolicyFor(option, thinkingEnabled)` 返回当前状态 policy；
- `supportedReasoningEfforts()` 按公共 options 顺序过滤 supported membership；
- `forceDeepThinking()` 以 disabled supported 是否为空判断；
- `resolveEffectiveReasoningEffort()` 按“合法保留、非法回退当前状态 default”实现。

### B3. 更新交互

- `reasoningEffortOptions` 只映射当前 effective thinking 状态支持项；
- Select 的 disabled 条件删除 `!effectiveDeepThinking`，只保留 interaction lock、配置非法或当前选项为空；
- thinking Switch 的 onChange 对开启和关闭都调用同一 effective resolver；
- 现有 effect 继续负责模型切换和 force-thinking 收敛，避免额外 state；
- 提交和 interrupt resume 继续使用同一个 `effectiveDeepThinking/effectiveReasoningEffort`；
- 不修改 `frontend/src/api/client.ts` 的透传职责，除非类型编译要求最小调整。

### B4. 前端绿测与提交

```bash
cd frontend
npm test -- --run src/App.test.tsx src/api/client.test.ts
npm run typecheck
npm run build
```

确认 production build 只保留既有体积 warning，不新增 TypeScript、React effect 或 a11y 错误。

检查点提交：

```text
feat(frontend): select efforts with thinking disabled
```

## 5. Checkpoint C：可重复的脱敏真实矩阵 Smoke

### C1. 脚本合同红测

新增 `tests/integrations/test_model_reasoning_matrix_smoke.py`，通过 fake client/factory 覆盖：

- 未带 `--live` 时只生成 52-case plan，不执行网络调用；
- `--live` 时逐模型覆盖 enabled/disabled × 公共 options 全目录；
- expected supported + accepted 和 expected unsupported + rejected 都是 match；
- expected supported 被拒绝、expected unsupported 被接受都是 mismatch；
- HTTP rejection 只保留 status 和稳定 provider code；
- transport/client error 标记 inconclusive，不能误判 unsupported；
- 输出不含 prompt、answer、api key、base URL、header、exception 原文或 request ID；
- 每个 client 最终关闭；
- exit code：0=plan/matched，2=config/usage error，3=inconclusive，4=capability mismatch。

### C2. 实现脚本

新增 `scripts/smoke_model_reasoning_matrix.py`：

- 参数：`--config`、`--live`、`--json`、`--timeout-seconds`；
- 使用现有 `yaml`、`LLMClient` 和 OpenAI SDK 异常类型，不新增依赖；
- 先调用生产 parser/validator，确保预期矩阵来自同一 authority；
- 不带 `--live` 时输出脱敏 planned cases；
- live 时使用短提示、`max_retries=0`、指定 timeout；
- 最多每个模型一个 in-flight probe，模型之间可并行，避免单模型突发和不必要串行等待；
- 成功响应只记录 accepted，不记录回答或长度；
- provider 4xx/5xx 只记录安全 code/status；
- 网络错误进入 inconclusive，不修改配置、不重试、不猜测能力。

更新 `scripts/AGENTS.md`，把新脚本登记为手工真实 provider smoke；明确它不进入服务启动、请求路径或默认
CI。

### C3. Fake 绿测与提交

```bash
conda run -n multi_agent python -m unittest tests.integrations.test_model_reasoning_matrix_smoke
conda run -n multi_agent python scripts/smoke_model_reasoning_matrix.py \
  --config config.yaml --json
```

第二条命令必须只输出 planned matrix，不发网络请求，不泄露本地配置。

检查点提交：

```text
test(llm): add redacted reasoning matrix smoke
```

## 6. Checkpoint D：API 文档、索引与变更记录

修改：

- `docs/api/API更新日志.md`；
- `docs/api/api-doc.html`；
- `tests/api/test_developer_docs.py`；
- `docs/superpowers/specs/2026-08-27-thinking-state-reasoning-effort-configuration-design.md`；
- 本实施计划；
- `docs/AGENTS.md`；
- `CHANGELOG.md`。

文档必须说明：

- 新 `options + thinking.enabled/disabled` response schema；
- 两种状态各自的 `supported/default`；
- 前端关闭 thinking 后仍允许选择合法强度；
- 非法 model/state/effort 组合返回 HTTP 400；
- 旧 schema 对严格客户端是破坏性响应变化；前后端必须锁步升级；
- provider smoke 是兼容性证据，不是效果评测；
- `prod` 和外部部署配置未因本地实现自动更新。

`API更新日志.md` 保留既有 main/prod 历史扫描基线，并新增 2026-08-27 后续 contract 变更说明；不得把历史
commit 对比改写成当前部署事实。静态 HTML 的行为说明和 schema 关键词与 OpenAPI DTO 保持一致。

文档测试：

```bash
conda run -n multi_agent python -m unittest tests.api.test_developer_docs
git diff --check
```

实现完成时把设计和本计划状态更新为 `implemented/complete`，记录实际测试数量与真实 smoke 结果，不提前
写成完成。

检查点提交：

```text
docs: document thinking-state reasoning efforts
```

## 7. Final Gate：完整验证与真实验收

### 7.1 Python 静态与定向门禁

```bash
conda run -n multi_agent python -m compileall -q src tests scripts

conda run -n multi_agent python -m unittest \
  tests.integrations.test_model_editions \
  tests.integrations.test_llm_request_options \
  tests.integrations.test_llm_client \
  tests.integrations.test_llm_runtime \
  tests.integrations.test_agent_model_gate \
  tests.integrations.test_model_reasoning_matrix_smoke

conda run -n multi_agent python -m unittest \
  tests.api.test_model_edition_selection \
  tests.api.test_conversation_titles \
  tests.api.test_skill_output_artifacts \
  tests.api.test_user_mcp_runtime_wiring \
  tests.api.test_user_mcp_task_assignment_restart \
  tests.api.test_developer_docs
```

对本次修改的 Python 文件运行仓库现有 Ruff 入口；不得顺手修复无关历史告警。

### 7.2 受影响全域回归

```bash
conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/e2e -p 'test_*.py'

cd frontend
npm test -- --run
npm run typecheck
npm run build
```

若宽测试出现既有平台 skip 或外部环境失败，必须记录精确测试名和原因；不得把未运行或失败项记为通过。

### 7.3 真实 Provider 52 组合矩阵

自动化全部通过后再执行：

```bash
conda run -n multi_agent python scripts/smoke_model_reasoning_matrix.py \
  --config config.yaml \
  --live \
  --json \
  --timeout-seconds 20
```

验收：

- 总 case 数为 52；
- 46 个配置支持组合 accepted；
- 6 个豆包 disabled 非 minimal 组合 rejected；
- mismatch=0、inconclusive=0；
- 输出不含回答、密钥、base URL、header、请求正文或 request ID。

若 provider 行为与 2026-08-27 证据不同，停止验收：先判断是网络不确定性还是能力漂移，再回到配置和测试
更新；不得自动放宽或降级。

### 7.4 本地 UI/API smoke

在当前本地开发后端/前端重建后，用新会话验证：

1. DeepSeek 或 GLM：关闭 thinking，强度下拉可用并展示六档；选择 high 后提交成功，request metadata 保持
   `deep_thinking=false + high`。
2. 豆包：开启时可选 high；切换关闭后自动回退 minimal，下拉仍可用但只有 minimal。
3. 切回 DeepSeek/GLM 时，当前 minimal 合法并保留；再次选择其他 disabled 合法档位可提交。
4. `/api/v1/config/model-editions` 返回新 schema，不出现旧字段。

只创建新的本地测试会话/Task，不复活或重放旧终态 Task，不部署 `prod`。

### 7.5 最终仓库检查

- `git diff --check` 通过；
- `git status --short --untracked-files=all` 只包含本任务预期版本化变更；
- `config.yaml`、`docker_cmd.md` 仍 ignored/untracked；
- 不存在旧 schema 的生产引用；
- `AGENTS.md`/`CHANGELOG.md` 已按实际影响同步；
- 无新增依赖或 license 变化。

## 8. 回滚

本功能无数据库或持久化数据迁移，但 code、API response、frontend 和本地配置是一个锁步合同：

- 未完成全部检查点前不得把中间提交部署为可用版本；
- 回滚时按 D → C → B → A 的逆序回滚版本化提交；
- 本地 `config.yaml` 只在代码回到旧 parser 后恢复旧 reasoning block；
- 不允许新后端配旧前端、旧后端配新前端或新 parser 配旧 config 的混合运行；
- smoke 脚本和文档可随整体版本回滚，不影响数据；
- 旧 Task/Message/AgentRun 无 schema 变化，不需要恢复、重放或修复。

## 9. 风险与停止条件

### 9.1 主要风险

- 忽略文件 `config.yaml` 漏迁移，导致本地启动失败；
- 某个内联测试 fixture 仍使用旧 schema，造成宽 API/Integrations 回归失败；
- 前端只过滤显示选项但提交仍使用陈旧 state；
- 模型切换和 thinking 切换 effect 相互触发，产生一次错误提交或渲染抖动；
- provider 接受组合但实际忽略 effort，被误写成效果证明；
- 静态 API 文档仍保留旧字段，外部客户端按错误 schema 实现。

### 9.2 停止条件

出现以下任一情况必须停止并先修正，不继续后续检查点：

- 需要加入旧 schema 生产 fallback 才能让测试通过；
- 需要读取、输出、提交本地凭据或 `docker_cmd.md` 内容；
- provider 52 组合出现 mismatch 或 inconclusive；
- 前端无法在不增加第二套状态的情况下满足已批准切换规则；
- 发现外部客户端或部署必须与本仓库同批协调，而当前没有相应授权；
- 修改范围扩展到数据库、Rust sidecar、部署或 `prod`。

## 10. 预计版本化文件清单

生产源码：

- `src/integrations/model_editions.py`
- `src/integrations/llm_request_options.py`
- `src/api/dto.py`
- `src/api/runtime.py`
- `frontend/src/api/types.ts`
- `frontend/src/App.tsx`
- `scripts/smoke_model_reasoning_matrix.py`

测试与 fixtures：

- `tests/integrations/test_model_editions.py`
- `tests/integrations/test_llm_request_options.py`
- `tests/integrations/test_llm_client.py`
- `tests/integrations/test_llm_runtime.py`
- `tests/integrations/test_agent_model_gate.py`
- `tests/integrations/test_model_reasoning_matrix_smoke.py`
- `tests/api/support.py`
- `tests/api/test_conversation_titles.py`
- `tests/api/test_model_edition_selection.py`
- `tests/api/test_skill_output_artifacts.py`
- `tests/api/test_user_mcp_runtime_wiring.py`
- `tests/api/test_user_mcp_task_assignment_restart.py`
- `tests/api/test_developer_docs.py`
- `tests/fixtures/unified_agent_loop_clean_archive_config.yaml`
- `frontend/src/App.test.tsx`
- `frontend/src/api/client.test.ts`

文档与索引：

- `docs/api/API更新日志.md`
- `docs/api/api-doc.html`
- `docs/superpowers/specs/2026-08-27-thinking-state-reasoning-effort-configuration-design.md`
- `docs/superpowers/specs/2026-08-27-thinking-state-reasoning-effort-configuration-implementation-plan.md`
- `scripts/AGENTS.md`
- `docs/AGENTS.md`
- `CHANGELOG.md`

本地非版本化配置：

- `config.yaml`（必须迁移，但始终 ignored/untracked）

只有 DTO 构造或路由类型确实要求时才修改 `src/api/routes/config.py`；只有 typecheck 证明需要时才修改
`frontend/src/api/client.ts`。不得因“顺手整理”扩大清单。

## 11. License Requirement

计划仅复用现有 Python、PyYAML、OpenAI-compatible SDK、FastAPI/Pydantic、React/TypeScript、Vitest 和
YAML 配置机制。实现不新增第三方依赖、供应链输入或许可变更。
