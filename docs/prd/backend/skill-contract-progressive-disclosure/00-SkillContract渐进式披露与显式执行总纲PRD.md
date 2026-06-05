# Skill Contract 渐进式披露与显式执行总纲 PRD

- **状态**：document-perfectization 加固完成，待实施
- **日期**：2026-06-05
- **目标模块**：Skill runtime、Skill capability 注册、main-agent soft binding / planner / replanner、SkillExecutor、slot_collection、Skill 资源读取、项目级 `skill/*` bundle
- **目标结果**：把膨胀的 `SKILL.md` frontmatter 拆分为轻量 `SKILL.md`、平台执行契约 `skill.contract.yaml`、机器输入 schema `schemas/*.input.yaml` 与按需资源读取；所有新格式 Skill 通过显式 `skill.*` 节点执行，不再依赖 main-agent 内部隐式 `auto_run`。

## 1. 背景与问题

当前项目级 Skill 把发现展示、公开说明、执行契约和参数 schema 都塞进 `SKILL.md` 顶部 YAML，带来四类问题：

1. `SKILL.md` 同时承担 Codex Skill 说明、平台 runtime manifest 与参数 schema，职责混杂。
2. `public_usage.parameters` 与顶层 `parameters` 重复描述同一业务概念，维护成本高。
3. 全局 `manifest.required` 不能表达“按试验方法动态必填”，例如 field-design 的 `ck_spec` 对 Interval 必填、对 RCBD 不必填。
4. main-agent 仍可通过 `match_skills -> scripts[].auto_run -> run script` 隐式执行 Skill，任务图看不到显式 `skill.*` 节点，不利于 v2 契约、checkpoint 与 time travel。

目标 Skill bundle 结构：

```text
SKILL.md                  # 轻量 agent-facing 入口
skill.contract.yaml       # 平台 capability / runtime / entrypoint / output 契约
schemas/*.input.yaml      # 机器可读输入 schema，按场景/方法拆分
references/*              # 主代理/用户帮助/补槽可按需读取的说明资源
scripts/ 或 runtime/       # 实际执行实现
```

## 2. 目标

- **G1 轻量 `SKILL.md`**：新格式 `SKILL.md` frontmatter 只保留 `name` 与 `description`；正文只保留用途摘要、资源索引和主代理边界。
- **G2 平台 contract 独立**：capability 注册、执行模式、entrypoint、output contract、schema selector、资源策略由 `skill.contract.yaml` 承载。
- **G3 输入 schema 渐进披露**：参数 schema 从 `SKILL.md` 移到 `schemas/*.input.yaml`，按业务场景/方法拆分。
- **G4 动态必填语义正确**：`required` 作用域是“当前 selected input schema”，不是全局 Skill manifest。
- **G5 主代理按需读取资源**：主代理通过 `SkillResourceService` 按 `capability_id + resource_id/path + audience` 读取 bundle 内资源；不做逐文件白名单，采用默认可读 + 黑名单 + 硬安全规则。
- **G6 显式执行**：新格式 Skill 只能在被规划为显式 `skill.*` node 后执行；main-agent 不再内部执行 Skill scripts。
- **G7 移除新格式 `auto_run`**：`auto_run` / `run_by_default` 不属于新 contract；entrypoint 由 selected input schema 显式绑定。
- **G8 兼容旧格式**：未迁移旧 Skill 通过 legacy adapter 继续运行；存在新 contract 时优先新 contract，禁止新旧执行契约混用。
- **G9 v2/checkpoint 友好**：schema selection、resource read、slot_collection、entrypoint execution、output contract validation 都作为显式事件记录。

## 3. 非目标

- 不实现通用本地 Codex Skill runtime；本专题只约束本项目后端平台 Skill。
- 不允许主代理读取脚本、handler、secret、config 或任意越界路径。
- 不让 LLM 决定最终可执行性；LLM 只参与意图判断、schema 选择辅助、补槽问题生成和候选参数抽取，后端负责校验与裁决。
- 不把 SQL guard、数据库权限、OCR 远端配置等内部业务安全规则塞进 input schema；input schema 只描述进入 Skill 前的用户输入契约。
- 不要求用户必须 slash command；自然语言仍可由 planner/router/replanner 触发显式 `skill.*` node。

## 4. 干系人与受影响系统

| 类别 | 对象 | 关注点 |
| --- | --- | --- |
| 终端用户 | 业务对话台用户、slash command 用户、普通自然语言用户 | 不需要理解内部 Skill 结构；能自然提问、补参、下载结果；普通自然语言和 slash 都能触发正确能力。 |
| Skill 作者 | 项目级 `skill/*` 维护者 | `SKILL.md` 编写负担降低；参数 schema 可按方法拆分；执行契约有稳定、可测试的机器格式。 |
| 主代理 | `main_agent.respond`、soft binding、planner/replanner | 只消费 public profile 和按需 public resource，不直接执行脚本、不读取内部实现。 |
| Skill runtime | `src/integrations/agent_skills/`、`src/capabilities/skill_tool/` | 统一 contract/schema/resource 解析、schema selection、input resolution、slot_collection、output validation。 |
| 编排与状态 | `src/orchestration/`、`src/api/runtime.py`、event log/checkpoint/time travel | 所有 Skill 执行必须是显式 `skill.*` 节点，关键选择和资源读取事件可恢复、可审计。 |
| 前端 | slash command 菜单、任务图、waiting-input UX、artifact 下载 | capability/display name 来自 contract；补槽继续通过 interrupt/resume；任务图能看到 Skill node。 |
| 运维/安全 | audit、secret 管理、资源读取边界 | bundle 内默认可读不等于 prompt 可泄漏；硬黑名单、脱敏、审计和路径边界必须 fail closed。 |

## 5. 术语与边界

| 术语 | 定义 |
| --- | --- |
| AgentSkillManifest | 从 `SKILL.md` 读取的轻量 agent-facing 描述，只包含 `name`、`description`、body 与资源导航文本。 |
| SkillContract | 从 `skill.contract.yaml` 读取的平台执行事实源，负责 capability、routing、runtime、entrypoints、schemas、outputs、resource policy。 |
| Input Schema | `schemas/*.input.yaml` 中的机器可读输入契约；`required` 只在当前 selected schema 内生效。 |
| Public Resource | 可进入主代理或补槽 LLM prompt 的说明资源；必须经 `SkillResourceService` 读取、裁剪、脱敏和审计。 |
| Machine Resource | 给 SkillExecutor、schema selector、validator 使用的机器资源，例如 input schema；默认不得原文进入主代理 prompt。 |
| 显式 Skill node | 任务图中 capability_id 为 `skill.*` 的节点；新格式 Skill 的唯一执行入口。 |
| 隐式 auto-run | `main_agent.respond` 内部通过 `match_skills -> scripts[].auto_run` 直接执行脚本的 legacy 行为；新格式禁止。 |

## 6. 当前证据摘要

| 证据 | 当前行为 |
| --- | --- |
| `Skill构建指南.md` | 当前把 `capability_id`、`public_usage`、`parameters`、`scripts`、`execution` 等都列为 `SKILL.md` frontmatter 字段。 |
| 系统 `skill-creator/SKILL.md` | 标准 Codex Skill 只要求 `name` / `description` frontmatter；正文用于 instructions/resources，强调 progressive disclosure。 |
| `src/integrations/agent_skills/parser.py` | 只解析 `SKILL.md` frontmatter；`parameters` / `scripts` / `outputs` 来自顶部 YAML；正文路径索引不会被 runtime 自动读取。 |
| `src/integrations/agent_skills/parameters.py` | `SkillParameterSpec.required` 当前是全局参数 required，不能表达按 schema 的 required_now。 |
| `src/capabilities/main_agent/executor.py` | 存在 `_resolve_skill_matches()` + `_run_auto_scripts()` 隐式路径。 |
| `src/capabilities/skill_tool/executor.py` | 显式 `SkillExecutor` 已作为 `skill.*` node executor 存在；当前仍依赖旧 manifest execution/scripts/parameters。 |
| `src/orchestration/skill_workflow_provider.py` | `skill.*` public capability 可展开为 Skill node + finalizer；这是新设计应保留并强化的主路径。 |
| `skill/field-design/SKILL.md` | RCBD / Diagonal / Interval 参数平铺在一个 `parameters` 下，`ck_spec` 只能设为非 required。 |
| `skill/sql-query/SKILL.md` | platform_service contract 与 public_usage 也放在 `SKILL.md` frontmatter 中。 |

## 7. 总体设计原则

1. **职责分离**：`SKILL.md` 面向 agent/human，`skill.contract.yaml` 面向平台，`schemas/*.input.yaml` 面向参数解析/补槽/校验，`references/*` 面向按需说明。
2. **显式节点**：所有新格式 Skill 执行必须出现在任务图中，不能藏在 `main_agent.respond` 内部。
3. **schema-first 补槽**：slot_collection 的 `missing/invalid` 来自 selected input schema；resume 时恢复 `selected_schema_id`，不重新猜、不看旧 `manifest.required`。
4. **受控按需读取**：主代理可以按需读取 Skill 公开资源，但读取必须通过平台服务、受 bundle 边界和黑名单控制、可审计。
5. **默认可读但不裸奔**：Skill bundle 内资源读取不做逐文件白名单；默认允许读取 bundle 内文件，但强制应用路径越界保护、硬黑名单、audience 策略、大小限制、脱敏和审计。
6. **旧格式只兼容**：legacy adapter 只为现有未迁移 Skill 服务；新规范和新项目级 Skill 不支持 `auto_run`。

## 8. 交付拆分

| 顺序 | PRD | 交付目标 | 依赖 |
| --- | --- | --- | --- |
| 01 | [`01-契约解析与LegacyAdapterPRD.md`](01-契约解析与LegacyAdapterPRD.md) | 解析 `skill.contract.yaml`、注册 capability、建立 legacy adapter；不改变执行路径。 | 无 |
| 02 | [`02-InputSchema与SchemaSelectorPRD.md`](02-InputSchema与SchemaSelectorPRD.md) | 解析 `schemas/*.input.yaml`，实现 schema selection 与 selected-schema 作用域 required。 | 01 |
| 03 | [`03-SkillResourceService按需读取PRD.md`](03-SkillResourceService按需读取PRD.md) | 建立按需资源读取服务、默认 allow + denylist、安全裁剪/脱敏/审计。 | 01 |
| 04 | [`04-PublicProfile与主代理适配PRD.md`](04-PublicProfile与主代理适配PRD.md) | 主代理消费 public profile 与 resource index；用法问题可读资源，执行请求进入显式 `skill.*`。 | 01、02、03 |
| 05 | [`05-SkillExecutorV2与SlotCollectionV2PRD.md`](05-SkillExecutorV2与SlotCollectionV2PRD.md) | 新格式 `skill.*` node 按 selected schema 补槽、执行 entrypoint、校验 output contract。 | 01、02、03 |
| 06 | [`06-项目级Skill迁移PRD.md`](06-项目级Skill迁移PRD.md) | 迁移 field-design、field-analysis、rice-genie、OCR、SQLQuery 到新结构。 | 01-05 |
| 07 | [`07-文档API测试与Legacy下线门禁PRD.md`](07-文档API测试与Legacy下线门禁PRD.md) | 文档/API/测试矩阵收口，定义 legacy auto-run 下线门禁。 | 01-06 |

每个编号 PRD 都是可独立验收的交付单元；本总纲只维护跨阶段不变量、总体目标和链接索引。

## 9. 跨阶段不变量

- 新格式 Skill 的执行事实源只能是 `skill.contract.yaml` + selected `schemas/*.input.yaml`。
- 新格式 Skill 不接受 `auto_run` / `run_by_default`；出现即 fail closed。
- `manifest.required` 不参与新格式缺参判定；缺参只来自 selected schema 的 required/required_when/constraints。
- 主代理不得读取 `scripts/`、`runtime/`、`schemas/`、`native/`、`config.yaml`、secret、token、credential、`.env`、`.git` 或越界路径原文。
- `SKILL.md` 正文中的资源索引只是导航，不是 runtime 事实源。
- LLM 生成的 schema choice、参数候选、追问文本都必须由后端 allowlist/schema/validator 裁决。
- checkpoint/time travel 需要能恢复 selected schema、resolved inputs、slot_collection、entrypoint 与 output contract 状态。
- legacy adapter 只服务未迁移 Skill；新格式 contract 存在时禁止新旧执行契约混用。


## 10. 跨阶段非功能要求

| 维度 | 要求 | 验证方式 |
| --- | --- | --- |
| 兼容性 | 未迁移 legacy Skill 继续按当前 `SKILL.md` frontmatter 路径工作；新格式 Skill 出现 contract 后不得再读取旧 `scripts/parameters/execution` 作为执行事实源。 | legacy parser/capability/executor 回归 + 新格式冲突 fixture。 |
| 安全与隐私 | prompt-facing 读取必须拒绝脚本、runtime、schema 原文、config、secret、越界路径和二进制原文；audit 不记录文件原文或敏感值。 | ResourceService 安全单测、脱敏快照、路径穿越/软链负向测试。 |
| 可恢复性 | running task 必须绑定 `skill_bundle_revision`、`selected_schema_id`、`selected_entrypoint` 与 slot_collection state；runtime refresh 不改变已运行节点的 contract 解析结果。 | interrupt/resume、bundle revision retention、checkpoint/time travel 回归。 |
| 可观测性 | contract load、schema selection、resource read、input resolution、slot open、entrypoint start、output validation 都有结构化事件；拒绝事件也要审计。 | event payload 单测/API 回归。 |
| 性能与资源 | public profile 只携带摘要和资源索引；资源正文按需读取并裁剪；schema selector 候选集只来自当前 Skill contract allowlist。 | profile 大小断言、resource truncation 测试、selector allowlist 测试。 |
| 可测试性 | 每个阶段必须能用 fixture 验证，无需真实 LLM 或真实远端服务；LLM 分支使用 fake provider 固定输出。 | 阶段 PRD 指定的 unit/integration/API/e2e 测试。 |

## 11. 全局验收标准

- 01-07 全部完成门禁通过。
- 五个项目级公开 Skill 均完成新结构迁移，并从 `/api/v1/capabilities` 以 contract registry 注册。
- field-design Interval 缺 `ncols/ck_spec` 时进入 slot_collection；RCBD 不把 `ck_spec` 视为缺参。
- slash command、普通自然语言、planner/replanner 都能触发显式 `skill.*` node。
- 用法类问题只读取 public resource 并回答，不执行 Skill。
- 所有资源读取有 bundle 边界、黑名单、裁剪、脱敏和 audit event。
- 新格式 examples 与文档不再包含 `auto_run`。
- targeted backend/API/e2e 回归通过；CHANGELOG 记录 License Requirement；无新增依赖或许可风险。

## 12. 风险与控制

| 风险 | 控制 |
| --- | --- |
| 拆出 contract 后 registry 与 legacy path 行为不一致 | 01 建立 LegacyAdapter 与 diagnostic，旧格式测试必须保持通过。 |
| schema selector 误选导致错补槽或错执行 | 02/05 要求 selected schema 持久化，低置信或 ambiguous 只补 schema selector，不执行 entrypoint。 |
| 默认可读策略泄漏内部实现或 secret | 03 以硬黑名单、路径边界、audience policy、脱敏和审计 fail closed。 |
| 主代理继续走隐式 auto-run | 04/05 明确新格式禁止 main-agent 内部执行，执行请求必须进入 `skill.*` node。 |
| 迁移期间半新半旧导致不可恢复状态 | 06 要求单 Skill 原子迁移；回滚方式是移除/禁用该 Skill contract。 |
| legacy 下线过早影响外部用户 Skill | 07 定义下线门禁，legacy adapter 不在本专题实施中立即删除。 |
