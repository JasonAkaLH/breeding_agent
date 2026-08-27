# 统一 Agent Loop Skill Soft Binding 回归设计

日期：2026-08-27

状态：设计已获用户批准；业务实现、测试、镜像重建和部署尚未开始

适用范围：`main` 分支、聊天页 Skill picker、`/skill-name` 命令、提交 API、统一 Agent Loop、Agent 持久化与恢复

不适用范围：MCP `$Server` 显式绑定、Skill 脚本输出容量修复、`prod`、旧失败 Task 复活、旧 DAG/Replanner 恢复

## 1. 背景与问题

旧 Soft Skill Binding 允许用户选择一个 Skill，同时由主代理判断当前消息是询问 Skill 用途，还是明确要求执行。统一 Agent Loop clean cutover 删除了旧 `main_agent.respond`、Soft Skill Decision LLM、`SoftSkillBindingReplanner` 和 DAG 动态扩展，这是正确的单控制面收敛；但选中 Skill 的 soft binding 语义没有迁移到新 Loop。

当前前端只要提交 `capability_id`，就推导 `routing_mode=force_capability`。当前后端又只要看到 `requested_capability_id`，就将对应 Tool 设为首轮 `required`，没有区分 `hint` 与 `force_capability`。因此用户选择 `bioinfo-daily` 后发送“你看看这个 Skill 是干什么的”，系统仍会执行 PubMed 检索，而不是基于 Skill 公开资料直接回答。

`RoutingMode.HINT`、Task 的 `requested_capability_id`、`PublicSkillProfile`、Skill bundle revision、`skill_activation` AgentItem、统一 Agent Tool catalog 和自动 Tool choice 均仍存在。问题是这些能力没有形成一条端到端的 selected-Skill hint 路径。

历史 Soft Skill 文档只作为产品语义和回归案例来源，不恢复其中已经退役的 DAG、Replanner、`main_agent.respond` 或独立 decision LLM。

## 2. 已确认产品决策

1. 前端点选 Skill 默认 soft binding。
2. 输入 `/skill-name` 默认 soft binding。
3. 选择 Skill 只确定当前消息的指代对象和首选能力，不等于立即执行。
4. 用户询问用途、参数、输入格式、示例或边界时，主 Agent 直接回答，不执行 Skill。
5. 用户明确要求运行、检索、分析、生成或处理数据时，统一 Agent Loop 调用选中的 Skill。
6. soft binding 只作用当前一条消息；提交成功或失败后清除前端选择。
7. 普通前端不提供 Skill `force_capability` 入口；该模式保留给内部 API、自动化和必要恢复合同。
8. MCP `$Server` 显式绑定保持现有行为：固定 Server、强制 `mcp.dispatch`/discovery、具体 Tool 软执行。本设计不修改 MCP DTO、badge、Router、Selector、授权、预算或恢复。
9. 不增加“立即执行”按钮；用户通过“运行这个 Skill”等自然语言表达执行意图。
10. 不修改各 Skill 的 `runtime.mode`。`python_subprocess`、`platform_service` 与 `delegated_main_agent` 继续按现有执行边界工作。

第 7 项的“必要恢复”只指恢复原本就以 `force_capability` 创建的 Task；任何原始 `routing_mode=hint` Task 在普通、waiting 或 startup recovery 中都必须继续保持 auto Tool choice，禁止恢复时升级为 force。

## 3. 目标与非目标

### 3.1 目标

- 让 `routing_mode=hint` 在统一 Agent Loop 中具有真实的 soft binding 语义。
- 让主 Agent 在首次采样前知道用户选中了哪个 Skill，并获得该 revision 对应的安全公开 profile。
- 保持 AgentRun durable、可恢复、幂等和单控制面的设计。
- 复用现有 Tool catalog 和主 Agent 自身的 Tool 决策，不增加一次独立判断模型调用。
- 对 informational 与 execution 两类消息形成明确、可自动验证的行为合同。
- 保持 Skill 执行、缺参 Interrupt、artifact、final output 和恢复路径不回归。

### 3.2 非目标

- 不恢复旧 Soft Skill Replanner、WorkflowPlan、DAG finalizer 或 `main_agent.respond`。
- 不修改 MCP 显式绑定语义。
- 不修复 `bioinfo-daily` 结果超过 128 KiB 的独立输出容量问题。
- 不持久化原始 `SKILL.md` 正文、脚本、handler、runtime 配置、凭据或内部路径。
- 不把 soft binding 变成 conversation 级长期设置。
- 不新增 Skill 执行按钮、强制执行开关或新的命令语法。
- 不修改数据库物理 schema；复用现有 `agent_item.kind=skill_activation`。
- 不修改或部署 `prod`。

## 4. 方案选择与复用

采用已批准的方案 A：`routing_mode=hint` 加持久化 PublicSkillProfile，统一 Agent Loop 保持 `tool_choice=auto`。

| 能力 | 复用方式 |
|---|---|
| `RoutingMode.HINT` | 复用现有 Core enum 和 Task 字段，补齐真实行为 |
| Skill picker 与 Slash parser | 保留交互、候选、冲突和一次性 badge，只改提交 intent |
| Capability Registry | 继续作为 public/enabled Skill authority |
| `PublicSkillProfile` | 直接复用现有脱敏 builder；不读取 raw body |
| Skill bundle revision | 继续固定提交时 revision，并进入 profile authority |
| `skill_activation` AgentItem | 泛化为“公开 Skill profile 已激活到当前 Agent 上下文”，不表示业务执行完成 |
| Agent Tool catalog | 继续暴露所有当前可见 public Skill Tool |
| Agent Loop | 继续由同一模型直接回答或发起 Tool call |
| SkillExecutor | 执行意图成立后原样复用 |
| Agent recovery | 从 durable items 恢复，不重新运行旧 decision 阶段 |

明确不复用旧 `main_agent.respond`、Soft Skill Decision prompt、Replanner、DAG node expansion 和旧 reasoning/decision 事件。

## 5. 前端与公共提交合同

### 5.1 Intent

前端提交调用必须显式携带 routing mode，禁止继续用“只要 capability ID 非空就强制执行”的推导规则：

| 入口 | `routing_mode` | `capability_id` |
|---|---|---|
| 普通聊天 | `auto` | `null` |
| Skill picker | `hint` | 选中的 `skill.*` |
| `/skill-name` | `hint` | 命令解析出的 `skill.*` |
| MCP `$Server` | `force_capability` | `mcp.dispatch` |

Skill picker 与 Slash 输入最终生成同一 wire contract；后端不需要信任或区分两种 UI 来源。Task 的 routing mode 与 requested capability 是唯一执行 authority。前端不再提交名称含“forced”的 Skill 控制 metadata。

### 5.2 一次性状态

- picker 与 Slash 仍显示当前已选择 Skill。
- Skill 与 MCP 选择继续互斥。
- 发送开始后沿用现有 busy gate，防止重复提交。
- 提交成功或失败后清除选中的 Skill；附件上传失败与补偿行为保持现状。
- 下一条消息没有再次选择 Skill 时，提交 `auto`。后续自然语言追问依靠对话历史，不继承 routing hint。

### 5.3 API 校验

- `hint` 必须携带 capability ID。
- `hint` 的目标必须存在、公开、启用且属于 `skill.*`。
- capability alias 仍由后端 canonicalize，最终持久化 canonical ID。
- 用户不得提交 profile、bundle revision、profile digest、`skill_activation` payload 或其他 system metadata。
- MCP binding DTO 继续要求现有 `force_capability + mcp.dispatch` 精确组合。

结构无效返回 422。目标不存在、已禁用、非公开或提交时 revision 不可用时返回低敏 409 `skill_hint_unavailable`；不得披露私有 capability 是否存在。

## 6. 后端 authority 与 admission

Skill hint 必须在 Message、Task、附件绑定、AgentRun 和 audit 副作用前完成预校验：

1. 解析 `routing_mode` 与 canonical capability ID。
2. 从 public Capability Registry 验证目标。
3. 固定当前 Skill bundle revision。
4. 从固定 revision 的 catalog 解析 Skill manifest 与 descriptor。
5. 调用现有 `build_public_skill_profile` 构建安全 profile。
6. 使用 Agent canonical payload codec 验证严格 JSON、UTF-8、SHA-256 与 128 KiB 上限。
7. 将 canonical profile、revision 和 digest 放入 server-private immutable prepared Agent handoff，供 durable initialization 使用。

prepared handoff 只是 Task 初始化前的 crash-safe 交接 authority，不进入公共 Message metadata、Conversation history 或 memory。AgentRun 初始化完成后，`skill_activation` item 是模型上下文和恢复的业务 authority。

HTTP 202 只能在 AgentRun 已完成 durable initialization 后返回。旧前端/新后端与新前端/旧后端不得混合发布；旧后端会把 `hint` 错当成 required Tool。

## 7. AgentRun 持久化设计

### 7.1 初始化原子性

有 Skill hint 时，Agent 初始化原子提交：

```text
sequence 1: user_message
sequence 2: skill_activation(binding_mode=hint)
```

无 hint 时保持现状，只提交 `user_message`。SQLite、PostgreSQL 与 Runtime Sidecar Agent repository 必须提供相同的 all-or-zero、CAS、revision、sequence 和 exact replay 语义。实现应扩展现有初始化 atomic writer 合同，不能先提交 user item、再以无恢复边界的独立写入追加 profile。

### 7.2 Profile item

复用现有 `AgentItemKind.SKILL_ACTIVATION`，payload 至少包含：

```json
{
  "binding_mode": "hint",
  "pinned_bundle_revision": "skillrev-...",
  "profile": {
    "capability_id": "skill.bioinfo_daily",
    "name": "bioinfo-daily",
    "display_name": "...",
    "description": "...",
    "triggers": [],
    "parameters": [],
    "inputs": {},
    "outputs": {},
    "resource_index": [],
    "schema_summaries": [],
    "routing_examples": []
  },
  "profile_digest": "sha256-without-prefix"
}
```

Profile 继续经过现有 allowlist/sanitizer。`resource_index` 只保留公开 ID、标题、描述和 audience；不得写资源正文。item ID 继续由 run ID、capability ID 和 pinned revision 确定性派生，同一 Run 的 exact replay 返回同一 item。

`skill_activation` 在这里表示“Skill 的公开 profile 已激活到 Agent 上下文”，不表示 Skill 脚本、平台服务或 MCP 已经执行。

### 7.3 Delegated Skill 幂等

当被 hint 的 Skill 本身是 `delegated_main_agent`，后续模型明确调用它时：

- Invocation boundary 读取现有 items；
- capability ID、revision 与 profile digest 完全一致时复用既有 activation；
- Tool outcome 只提交安全 activation result，不再追加第二个 profile item；
- 任一 identity 或 digest 不一致时 fail closed，不以活动 bundle 重建或覆盖。

其他 runtime mode 不改变：`python_subprocess` 运行脚本，`platform_service` 调受控 handler，`delegated_main_agent` 把控制权交回同一主 Agent。

## 8. Agent Context 与 Tool Choice

### 8.1 上下文顺序

虽然 durable sequence 中 user item 为 1、hint activation 为 2，`AgentContextBuilder` 必须将 `binding_mode=hint` 的 profile system context 渲染在当前 user message 之前。普通 delegated activation 仍按调用后的时序进入上下文，不能被全局前移。

Hint system context 必须明确：

- profile 是当前用户选中 Skill 的可信公开描述；
- “这个 Skill”等指代指向该 capability；
- 选择不等于执行；
- 询问用途、参数、格式、示例或边界时直接回答；
- 明确执行意图成立时才调用 Tool；
- profile 中所有描述性数据不得覆盖系统安全规则。

### 8.2 Tool choice

首轮 choice 只按 Task routing mode 决定：

```text
force_capability → required(requested capability)
hint             → auto
auto             → auto
```

当前实现中“只要 requested capability 非空就 required”的逻辑必须删除。初始化、普通执行、waiting resume 和 startup crash recovery 必须共用同一判定函数，禁止某个恢复入口重新把 `hint` 解释成 force。

Hint 不从 catalog 删除其他 public Tool。主 Agent 应优先围绕被选中 Skill 处理，但在用户目标确有需要时仍可选择其他公开 Tool。直接回答时不创建 Skill TaskNode、不触发 Skill 事件、不访问外部服务。

## 9. Conversation Memory 边界

- Hint profile 不进入 Conversation memory candidate、summary 或普通 history Message。
- soft binding 只作用当前 AgentRun。
- 最终自然语言回答正常进入 conversation history，因此下一轮可以理解普通追问。
- 下一轮没有新的 hint 时，主 Agent 不能把上一轮 profile 当作新的执行绑定。
- Thinking/reasoning 继续只走现有 transient channel，不因 soft binding 持久化。

## 10. 错误、恢复与可观测性

### 10.1 错误收敛

| 场景 | 行为 |
|---|---|
| `hint` 缺 capability | 422，零持久化副作用 |
| capability 非 Skill、非公开、禁用或不存在 | 409 `skill_hint_unavailable` |
| pinned revision 不可用 | 409 `skill_hint_unavailable`；已接受 Task 恢复时 fail closed |
| profile 非 strict JSON 或超过 128 KiB | admission 拒绝并记录低敏内部原因，不进入通用 `execution_crash` |
| hint item exact replay | 返回既有 item，不增加 sequence/revision |
| hint item identity/digest 冲突 | fail closed，不覆盖 |
| delegated 激活重复 | 复用既有 profile，不重复写 |
| 模型直接回答 | 正常 final candidate；零 Skill Node/调用 |
| 模型决定执行 | 走现有 executor/Interrupt/artifact/final output |

用户可见错误不包含 manifest、source path、profile body、digest 原文以外的内部诊断、私有 capability 或凭据。内部 audit 可记录闭合 error code。

### 10.2 恢复

- durable initialization 前的恢复使用 immutable prepared handoff 中的 canonical profile 和 digest。
- durable initialization 后只从 Agent items 恢复，不读取当前活动 Skill 版本替换 profile。
- startup recovery、waiting resume 与普通入口使用相同 routing-mode-to-tool-choice helper。
- fixed revision 在 Task 终态前保持 pinned，终态后按现有生命周期释放。
- 旧 Task 不回填 hint profile，不复活历史失败 Task。

### 10.3 事件与审计

新增 audit-only `skill.hint_bound`，最小 payload 为 capability ID、安全 revision 引用和 profile digest；不得包含 profile body、用户正文、脚本或资源正文。现有 `agent.run.started` 继续记录 routing mode，现有 Tool/Node/Skill 事件继续证明是否实际执行。

不恢复 `soft_skill_binding.decision`、`soft_skill.reasoning_delta` 或独立 answer/execute decision event。是否执行由同一 Agent sample 的 Tool call 事实体现。

## 11. 测试与验收

### 11.1 前端

- picker 与 `/skill-name` 均提交 `hint + skill.*`。
- 普通聊天提交 `auto + null`。
- MCP `$Server` 继续提交 `force_capability + mcp.dispatch`。
- Skill/MCP 选择互斥，Skill 选择成功/失败后清除。
- API client 不再从 capability ID 隐式推导 routing mode。
- unknown/conflicting Slash、附件失败补偿、busy/Interrupt gate 不回归。

### 11.2 API 与 authority

- 合法 `hint + public skill` 成功。
- 缺 capability、非 Skill、private/disabled/missing target 在副作用前拒绝。
- alias canonicalization 与 pinned revision 正确。
- 用户伪造 profile/revision/digest/internal binding 被拒绝或剥离。
- prepared handoff 与 Agent item canonical bytes/digest 精确一致。
- 新旧前后端不兼容组合由发布门禁阻止，不做静默兼容。

### 11.3 Agent storage 与 recovery

- 三 repository 路径原子提交 user + hint item，fault injection 全部 all-or-zero。
- exact replay 不增加 item、sequence 或 revision。
- profile 131071/131072 bytes 接受，131073 bytes 拒绝。
- crash before/after initialization、startup recovery、waiting resume 保持 hint 为 auto choice。
- bundle refresh 后仍使用 pinned profile；revision 缺失 fail closed。
- delegated Skill hint 后执行只保留一个 activation item。

### 11.4 Agent Loop

- `force_capability` 首轮 required；`hint` 与 `auto` 首轮 auto。
- hint profile 渲染在当前 user message 前，普通 delegated activation 不前移。
- profile 不包含 runtime、path、script、handler、配置、secret 或 raw resource body。
- informational sample 无 Tool call时直接完成。
- execution sample 调用 Tool 后走现有 outcome/final 流程。
- 没有额外 decision LLM 调用、Replanner 或 DAG shape。

### 11.5 核心业务验收

选择 `bioinfo-daily` 后发送“你看看这个 Skill 是干什么的”：

- Task completed；
- Skill 调用数为 0；
- PubMed 网络调用数为 0；
- Skill TaskNode 数为 0；
- 回答包含用途、输入和边界的公开事实。

选择 `bioinfo-daily` 后发送“检索最近七天的育种文献”：

- 模型调用 `skill.bioinfo_daily`；
- 参数解析和脚本执行路径保持现状；
- Skill 输出容量若仍超过独立 128 KiB 限制，应以该独立缺陷记录，不能把 soft binding 误记为完成端到端输出修复。

选择 `germplasm-mcp` 后询问用途：直接回答、零 MCP 调用、一个 hint activation item。明确要求查询种质时：复用该 item，进入现有 MCP 授权与执行链。

### 11.6 验证门禁

1. 前端定向测试、完整测试、typecheck、production build。
2. Agent Loop、API、Agent storage、Skill integration 与 recovery 定向测试。
3. 根 AGENTS 定义的相关后端分层回归。
4. Runtime Sidecar contract/Rust gate；若修改其 wire/contract，执行完整统一 Rust 质量门禁。
5. `git diff --check`、Ruff/compileall 和最终 diff 审查。
6. 本地前后端成对重建后真实 UI/API smoke，分别验证“询问不执行”和“明确任务会执行”。

## 12. 实施边界

预计实施面包括：

- `frontend/src/domain/slashCommands.ts`、`frontend/src/api/client.ts`、`frontend/src/App.tsx`及对应测试；
- `src/api/dto.py`、`src/api/runtime.py`、submission prepared handoff 投影及对应测试；
- `src/orchestration/agent_loop/orchestrator.py`、`context.py`、`skill_activation.py`、初始化/recovery helper及测试；
- SQLite/PostgreSQL/Runtime Sidecar Agent repository 的初始化原子提交合同及测试；
- API 文档、目录索引、CHANGELOG 与必要的 AGENTS 索引。

实施计划必须先以当前 HEAD 核对 exact seam，优先扩展现有 atomic writer 和 profile builder，不创建第二套 Skill binding service、第二模型阶段或新数据库表。

## 13. 发布与回滚

- 只在 `main` 本地开发环境实施与验收；`prod` 不变。
- 前后端必须成对构建、发布和回滚。新前端向旧后端发送 `hint` 会被旧后端错误地强制执行。
- 发布前停止新提交并等待在途 Task 收敛，再替换 backend/frontend。
- 回滚前同样停止新提交并等待所有 `routing_mode=hint` Task 终态；不得让旧后端恢复在途 hint Task。
- 本设计不修改数据库物理 schema，回滚不删除 Task、AgentRun、AgentItem、Message、Artifact 或 audit。
- 已完成的 hint Task 历史回答保持普通对话历史；其 private activation item 不进入公共历史。

## 14. 完成条件

只有同时满足以下条件才可宣称功能回归完成：

1. Skill picker 与 Slash 都使用单消息 `hint`。
2. `hint` 在所有普通与恢复入口保持 auto Tool choice。
3. 选中 Skill 的安全 profile 在首次采样前 durable、pinned、可恢复。
4. informational 消息零 Skill 执行并成功回答。
5. execution 消息可调用同一选中 Skill，且现有执行/Interrupt/final 路径不回归。
6. delegated Skill 不重复 activation。
7. MCP 显式绑定行为完全不变。
8. 前后端和相关后端/Rust门禁通过，实际本地 smoke 通过。
9. 文档、索引、CHANGELOG 与实现状态一致。
10. `prod` 未修改或部署。

License Requirement：复用现有 Python、Rust/Runtime Sidecar、FastAPI/Pydantic、React/TypeScript、Agent Loop、PublicSkillProfile 与 Skill runtime 能力；不新增第三方依赖或许可类型。
