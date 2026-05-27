# Skill Executor 实现需求 PRD

- **范围**：后端 / Skill capability executor / Skill runtime / service binding / capability 统一执行层
- **文档状态**：PRD 草案
- **日期**：2026-05-12
- **关联文档**：
  - `Skill构建指南.md`
  - `docs/数据查询 Skill-Skill化迁移计划.md`
  - `docs/prd/backend/08-主代理Skill兼容与真实LLM运行时.md`
  - `docs/prd/backend/11-Skill输出文件Artifact与下载PRD.md`
  - `docs/prd/backend/12-Skill一等Capability能力池PRD.md`
  - `docs/prd/backend/13-Skill动态加载与热部署PRD.md`
  - `docs/prd/backend/14-MCPRuntime实现需求PRD.md`
- **外部参考**：
  - OpenAI Codex Agent Skills：<https://developers.openai.com/codex/skills>
  - OpenAI Codex Customization - Skills：<https://developers.openai.com/codex/concepts/customization#skills>

## 1. 一句话结论

Skill Executor 是框架层的**通用执行适配器**，负责把已被 Planner / Router 选中的 `skill.*` capability 安全、可观测、可调度地执行起来，并把执行结果归一化为系统标准的 `CapabilityExecutionResult`。

它不负责业务决策，也不内置 数据查询 Skill、数据分析、报告生成等业务语义。业务逻辑必须放在 Skill 包、领域服务或 MCP tool 背后。

建议边界定义如下：

> Skill Executor 是通用能力执行壳，负责 Skill 的版本解析、输入校验、权限控制、受控执行、输出归一化、artifact 生成和审计；业务决策、业务算法、外部协议细节、最终 UI 展示都不属于 Skill Executor。

## 2. 背景

当前系统已经具备以下能力：

1. `SkillRuntimeState` / `SkillRuntimeBundle` 支持新聊天前动态刷新 Skill catalog 和 public `skill.*` capability。
2. `SkillCapabilityRegistry` 能将项目级 Skill 映射为 `CapabilityDescriptor`。
3. `SkillWorkflowProvider` 能把 `skill.*` macro 展开成 forced `main_agent.respond`。
4. `MainAgentExecutor` 能匹配 Skill、注入 `SKILL.md` 指令、执行 auto-run Python script，并收集 Skill 输出文件 artifact。
5. `MCPToolExecutor` 已作为 generic executor 证明外部能力可以通过统一 capability 契约接入。

但现有 Skill 执行模型仍有明显限制：

- `skill.*` 实际执行仍依附 `main_agent.respond`，不是独立可调度 executor；
- Skill script 执行逻辑散落在 `MainAgentExecutor` 内，难以被其他 capability 复用；
- `SkillScriptRunner` 只提供最小 Python 子进程执行，不支持项目级受控 service binding；
- 对 数据查询 Skill 这类结构化、强安全、强可观测的能力来说，forced main agent 路径不足以承载长期演进；
- 如果要让“所有业务 capability 都来自 Skill 和 MCP tools”，必须有一个 generic Skill Executor 与 generic MCP Executor 对等。

因此需要新增 Skill Executor 实现需求，作为后续 数据查询 Skill 化和 capability 统一治理的前置基线。

## 3. Codex Skill 机制对本项目的启发

OpenAI Codex Skills 的核心机制是：

1. Skill 是可复用 workflow 的作者格式。
2. Skill 目录包含 `SKILL.md`，可选 `scripts/`、`references/`、`assets/`。
3. Codex 使用渐进式披露：先用 name / description / path 做发现，只有选中 Skill 后才加载完整 `SKILL.md`。
4. references 和 scripts 也应在需要时才读取或执行。
5. 如果 workflow 需要外部系统，官方建议与 MCP 配合使用。

本项目应借鉴的是：

- Skill 的发现层只暴露轻量 metadata；
- Skill 的完整指令、脚本和资源只在选中后加载；
- Skill executor 只负责执行与治理，不应把所有业务能力塞进框架内核；
- 外部系统接入优先通过 MCP 或受控 service binding，而不是让 Skill 脚本继承完整环境和任意访问权限。

本项目不需要完全复刻 Codex 本地 runtime。当前后端仍应保持服务化、安全受控、可测试和可审计。

## 4. 目标

### 4.1 产品目标

1. 让项目级 Skill 可以作为真正的一等 public capability 被 Planner / Router / API 调用。
2. 支撑并固化 数据查询 Skill 当前完成态：数据查询 Skill 已迁移为 `skill.data_lookup` platform-service，旧 native capability 已删除。
3. 将业务 capability 来源统一收口为 Skill 与 MCP tools。
4. 降低 orchestration 对具体业务能力的认知，使框架更轻量。
5. 保留用户体验：用户仍通过自然语言发起任务，不需要理解 Skill Executor 内部机制。

### 4.2 工程目标

1. 新增 generic `SkillExecutor`，与 `MCPToolExecutor` 一样作为 `CompositeExecutor` 的一员。
2. 抽出可复用 Skill execution service，避免 `MainAgentExecutor` 和 `SkillExecutor` 各自复制脚本执行、参数解析、artifact 处理逻辑。
3. 支持固定 `skill_bundle_revision`，确保运行中任务不受后续 Skill 热刷新影响。
4. 建立项目级可信 Skill 的 service binding 机制，为 数据查询 Skill 提供 MySQL readonly / 数据查询 Skill LLM 等受控依赖。
5. 统一 Skill 输出到 `CapabilityExecutionResult`、artifact、event、audit。
6. 保持 async / await 调度模型，不在核心链路引入同步阻塞业务逻辑。

### 4.3 用户、维护者与影响面

| 角色 / 系统 | 关注点 | 本 PRD 对应承诺 |
|---|---|---|
| 业务用户 | 仍通过自然语言使用能力，不理解 executor 细节 | `skill.*` 由 Planner / Router 自动选择，最终仍输出自然语言回答和必要 artifact |
| Skill 作者 | 如何把可复用工作流声明为平台能力 | 通过 `SKILL.md` metadata、脚本或平台服务 entrypoint 声明输入输出和执行模式 |
| 后端维护者 | 避免业务能力继续侵入 orchestration | Skill Executor 只承接通用执行壳，业务逻辑放在 Skill 包、领域服务或 MCP tool 背后 |
| 安全 / 运维 | 控制数据库、LLM、外部系统等敏感依赖 | service binding 必须 manifest 声明 + runtime allowlist 双重授权 |
| 前端 | 稳定展示任务进度、最终回答和 artifact | 消费 `skill.progress`、artifact metadata 和 final answer，不依赖 数据查询 Skill 原生 capability id |
| 数据查询 Skill 迁移 | 从 native capability 迁移为 `skill.data_lookup` | 先满足本 PRD 的平台服务 entrypoint、受控 service binding、artifact/event 归一化 |

## 5. 非目标

1. 不复刻完整 Codex 本地 runtime。
2. 不支持任意 shell、任意本地命令、任意依赖安装。
3. 不允许 Skill 脚本继承完整环境变量或读取 secret。
4. 不把 数据查询 Skill 业务逻辑写进 Skill Executor。
5. 不让 Planner 直接指定脚本路径、运行命令、service token 或本地文件路径。
6. 不在本 PRD 中实现 Skill 市场、后台审核、在线权限配置 UI。
7. 不把 MCP 协议通信细节放进 Skill Executor；MCP 仍由 `src/integrations/mcp/` 与 `MCPToolExecutor` 负责。
8. 不引入 LangChain、LangGraph、AutoGen 等 Agent 框架。

## 6. 术语

| 术语 | 定义 |
|---|---|
| Skill | 包含 `SKILL.md` 的可复用工作流包，可包含脚本、参考资料和资源。 |
| Skill capability | 从 public Skill 映射出的 `skill.*` capability。 |
| Skill Executor | 本 PRD 要实现的 generic executor，负责执行已选中的 `skill.*` capability。 |
| Skill execution service | 可复用的 Skill 执行服务，封装参数解析、脚本调用、输出处理、artifact 处理。 |
| Skill bundle revision | `SkillRuntimeState` 为某次 Skill catalog 快照生成的 revision，用于运行中任务版本固定。 |
| Service binding | 平台为受信 Skill 注入的受控服务依赖，例如 MySQL readonly、LLM runtime、artifact writer。 |
| Project trusted Skill | 仓库 `skill/` 下、通过 manifest 声明并被 runtime allowlist 允许使用受控服务的 Skill。 |
| Instruction-only Skill | 只提供 `SKILL.md` 指令，不执行脚本的 Skill。 |
| Script Skill | manifest 中声明了可执行 entrypoint 的 Skill。 |

## 7. 总体设计

### 7.1 分层关系

目标执行链路：

```text
用户请求
→ Planner / Router 选择 skill.*
→ SkillWorkflowProvider 生成 skill execution node
→ Scheduler 分配本地 skill executor instance
→ SkillExecutor 执行 selected Skill
→ Skill execution service 解析输入、运行 entrypoint、收集输出
→ Artifact / Event / Audit 写入系统
→ Final answer synthesizer 根据结果生成自然语言回答（如需要）
```

### 7.2 与现有 forced main agent 路径的关系

当前 `SkillWorkflowProvider` 会把 `skill.*` 展开成 forced `main_agent.respond`。实现 Skill Executor 后，应逐步演进为：

```text
skill.* macro
→ skill capability execution node
→ 可选 final answer synthesizer node
```

兼容期内可保留 forced main agent 路径作为 fallback，但新结构化 / 脚本型 / service-binding Skill 应优先走 Skill Executor。

### 7.3 与 MCP Tool Executor 的关系

Skill Executor 和 MCP Tool Executor 对等：

| 维度 | Skill Executor | MCP Tool Executor |
|---|---|---|
| capability id | `skill.*` | `mcp.*` |
| 能力来源 | 本地 / 项目 Skill bundle | 外部 MCP server tool discovery |
| 主要治理 | Skill manifest、脚本、service binding | MCP server config、tool allowlist、schema |
| 外部系统访问 | 优先 service binding 或调用 MCP capability | MCP protocol tools/call |
| 输出 | `CapabilityExecutionResult` / artifact / event | `CapabilityExecutionResult` / sanitized output / event |

二者都不应把业务语义写入 orchestration。

### 7.4 v1 执行模式决策

为避免“service binding + 子进程脚本”扩大权限边界，v1 必须明确区分三类 Skill 执行模式：

| execution.mode | 适用对象 | 是否允许受控 service binding | 执行方式 | 关键限制 |
|---|---|---:|---|---|
| `delegated_main_agent` | instruction-only Skill、兼容期 prompt 型 Skill | 否 | 展开到内部 final answer / main agent 路径 | 不执行脚本；只注入 Skill 指令和安全上下文 |
| `python_subprocess` | 普通脚本型 Skill，适合确定性预处理、文件生成、小工具 | 否 | 复用受限 `SkillScriptRunner` 子进程 | 最小环境；无 secret；无 DB / 内部 LLM；不承诺可 import 项目源码 |
| `platform_service` | 项目级可信 Skill，例如当前 `skill.data_lookup` | 是，必须 allowlist | Skill Executor 调用平台注册的 async handler / domain service | 不允许 manifest 任意动态 import；handler 必须由 runtime 显式注册 |

v1 不允许普通 `python_subprocess` Skill 直接获得 MySQL readonly、内部 LLM、HTTP client、完整环境变量或 secret。需要这些资源的 Skill 必须走 `platform_service`，并通过 runtime allowlist 绑定到平台已注册 handler。

`platform_service` 不是把业务逻辑写入 Skill Executor；Skill Executor 只负责根据 capability id 查找被 allowlist 授权的 handler，真正业务逻辑仍放在对应 Skill bundle（例如 `skill/<domain-query>/runtime/`）或领域服务内。

## 8. Skill Executor 职责边界

### 8.1 应负责

Skill Executor 必须负责：

1. **Skill 解析**
   - 根据 `capability_id` 找到 Skill name；
   - 根据 `skill_bundle_revision` 找到对应 Skill catalog；
   - 解析 manifest、scripts、inputs、outputs、metadata。

2. **调用校验**
   - 确认 capability id 属于 active / retained bundle；
   - 确认 Skill 是 public；
   - 校验 planner payload policy；
   - 校验必填参数；
   - 拒绝未知 capability、未知 revision、非法脚本路径、未授权 service。

3. **输入构造**
   - 注入用户问题；
   - 注入允许的 uploaded artifacts 摘要；
   - 注入安全 metadata；
   - 注入 dependency outputs 的脱敏摘要；
   - 根据 manifest 参数声明进行确定性解析和可选 LLM 解析。

4. **执行控制**
   - 执行 instruction-only 或 script entrypoint；
   - 支持 timeout；
   - 支持取消；
   - 限制 stdout / stderr / 输出大小；
   - 捕获异常并映射为 `CapabilityExecutionError`。

5. **受控 service binding**
   - 根据 manifest 与 runtime allowlist 判断可用服务；
   - 只向受信 Skill 提供必要服务；
   - 禁止把 secret 原样传入脚本环境；
   - 记录 service 使用审计。

6. **输出归一化**
   - 校验 script JSON object 输出；
   - 校验 manifest `outputs.required`；
   - 生成 `output_payload`；
   - 生成 artifacts；
   - 生成 frontend / audit events；
   - 支持输出文件 artifact manager。

7. **可观测性**
   - 记录 skill selected / started / completed / failed；
   - 记录 entrypoint、revision、duration、output size；
   - 记录 service binding 使用；
   - 不记录 secret、完整 prompt、数据库连接串。

### 8.2 不应负责

Skill Executor 不应负责：

1. 判断用户意图是否需要 数据查询 Skill、数据分析或报告生成；这是 Planner / Router / Skill matcher 的职责。
2. 数据查询 Skill 的业务算法，例如数据库路由、SQL 生成、SQL Guard、结果筛选。
3. MCP 协议 lifecycle、tools/list、tools/call。
4. 最终 UI 展示和前端组件逻辑。
5. 任意依赖安装或 runtime 环境变更。
6. 任意跨 Skill 编排策略；复杂 DAG 仍由 orchestration 生成和调度。
7. 直接读取 `config.yaml`；配置只允许 runtime 启动期读取并通过受控依赖注入。

## 9. 功能需求

### 9.1 capability 支持范围

Skill Executor 应支持所有 active public `skill.*` capability。

要求：

- `supports(capability_id)` 只对 active bundle 中的 `skill.*` 返回 true；
- 如果 request metadata 带 `skill_bundle_revision`，必须按该 revision 查找；
- revision 不存在时返回 fail-closed 错误；
- 不允许执行不在 public roots 下的 Skill capability。

建议错误码：

| 场景 | 错误码 |
|---|---|
| capability 未注册 | `skill_capability_not_registered` |
| bundle revision 不存在 | `skill_bundle_revision_missing` |
| manifest 缺失 | `skill_manifest_missing` |
| Skill 非 public | `skill_not_public` |

### 9.2 SkillWorkflowProvider 演进

`SkillWorkflowProvider` 应支持两种展开模式：

1. **legacy forced main agent mode**：保留当前兼容行为；
2. **executor mode**：生成真正的 `skill.*` execution node。

建议通过 runtime 配置或 capability metadata 判断：

```yaml
execution:
  mode: delegated_main_agent | python_subprocess | platform_service
  answer_mode: direct | requires_finalizer | none
```

推荐默认：

- instruction-only Skill：短期可继续 delegated main agent；
- 普通 script Skill：优先走 `python_subprocess` Skill Executor；
- project trusted Skill：必须走 `platform_service` Skill Executor；
- `skill.data_lookup`：必须走 `platform_service` Skill Executor；
- `answer_mode=requires_finalizer` 时，WorkflowProvider / LLMWorkflowProvider 必须追加内部 final answer 节点；`answer_mode=direct` 时不得重复追加回答节点。

### 9.3 输入 payload 策略

Planner 不应自由构造 Skill 参数。

Skill capability 的 payload policy 应来自 manifest 和系统白名单：

- planner 只允许传入 `subtask_label`、`parent_question` 等低风险字段；
- `query` / `user_message` 应由系统从 `effective_user_message` 注入；
- uploaded artifacts 由系统按权限注入摘要；
- dependency outputs 由系统按 allowlist 注入摘要。

禁止：

- planner 指定脚本路径；
- planner 指定 service binding；
- planner 指定本地文件路径；
- planner 传入 secret、环境变量名、数据库连接串。

### 9.4 参数解析

应复用或抽出当前 `resolve_skill_inputs_with_llm` 能力。

策略：

1. 优先确定性解析；
2. 对文本标量参数可选 LLM 解析；
3. LLM 解析结果必须通过类型校验和枚举校验；
4. 缺少必填参数时，应返回可审计的 missing input 结果；
5. 不应因为参数缺失而让脚本空跑。

### 9.5 instruction-only Skill 执行

对于没有脚本的 Skill，有两种可接受路径：

1. 交给 final answer synthesizer 读取 Skill body 生成回答；
2. 作为 Skill Executor 的 instruction result 输出给后续 main agent finalizer。

短期推荐保留 delegated main agent，以降低迁移风险。长期如果 main agent 从 public capability 中移除，则需要把 answer synthesizer 作为内部系统节点承接 instruction-only Skill。

### 9.5.1 answer_mode 与最终回答节点

Skill capability 必须显式或默认确定回答模式，避免重复回答或缺少最终回答：

| answer_mode | 含义 | 默认适用 |
|---|---|---|
| `direct` | Skill 自身输出就是用户可见最终回答 | 文本生成、报告生成等 answer-producing Skill |
| `requires_finalizer` | Skill 输出结构化结果，需要内部 final answer synthesizer 汇总 | `skill.data_lookup`、MCP 查询类结果、结构化数据分析 |
| `none` | Skill 只产生 artifact 或副产物，不自动生成自然语言回答 | 纯文件生成、后台准备类 Skill |

验收要求：

- `requires_finalizer` 只追加一个内部 final answer 节点；
- `direct` 不得再追加第二个主代理回答节点；
- `none` 必须仍有明确 task completion 语义和 artifact 查询入口；
- 兼容期内如果缺少 `answer_mode`，`delegated_main_agent` 默认视为 `direct`，`python_subprocess` 默认视为 `requires_finalizer`，`platform_service` 必须显式声明。

### 9.6 script Skill 执行

script Skill 必须满足：

- 只执行 manifest 声明的 entrypoint；
- v1 仅支持 Python；
- 脚本路径必须在 Skill 包内；
- 禁止绝对路径、`..`、symlink 逃逸；
- stdin 必须是 JSON；
- stdout 必须是 JSON object；
- stderr 仅用于受限诊断；
- timeout 必须生效；
- 输出大小必须有限制。

当前 `SkillScriptRunner` 的安全限制应保留，并补充 service binding 机制。

普通 `python_subprocess` Skill 的附加限制：

- 不继承完整环境变量；
- 不注入项目根目录到 `PYTHONPATH`；
- 不承诺可 import `src.*` 项目源码；
- 不允许使用 MySQL readonly、内部 LLM、artifact writer 以外的真实文件路径或任意平台 service；
- 如需复用项目领域能力，应迁移为 `platform_service`，由 runtime 注册 handler。

### 9.7 service binding

#### 9.7.1 目标

service binding 解决“受信 Skill 需要平台服务，但不能读取 secret”的问题。

例如 数据查询 Skill 需要：

- MySQL readonly adapter；
- 数据查询 Skill 内部 LLM text generator；
- artifact writer；
- 可选 progress event emitter。

#### 9.7.2 manifest 声明

建议支持：

```yaml
execution:
  mode: platform_service
  handler: skill.data_lookup.platform_handler
  answer_mode: requires_finalizer
  trust_scope: project
  services:
    - mysql_readonly
    - llm.non_stream
    - artifact_writer
    - progress_events
```

#### 9.7.3 runtime allowlist

runtime 必须有显式 allowlist：

```python
trusted_skill_services = {
    "skill.data_lookup": ("mysql_readonly", "llm.non_stream", "artifact_writer", "progress_events"),
}
trusted_skill_handlers = {
    "skill.data_lookup": "skill.data_lookup.platform_handler",
}
```

要求：

- manifest 声明和 runtime allowlist 必须同时允许；
- `execution.handler` 必须命中 runtime 预注册 handler，不允许根据 manifest 字符串任意 import；
- 用户级 Skill 默认不能申请 service；
- public Skill 不等于 trusted Skill；
- service binding 失败必须 fail closed；
- audit 只记录 service 名称，不记录 secret。

#### 9.7.4 service 调用方式

优先采用进程内服务注入：

```text
SkillExecutor
→ SkillExecutionContext
→ 受控 service adapters
→ domain engine
```

v1 对 service-bound Skill 的正式路径是 `platform_service`：Skill Executor 在父进程内调用 runtime 预注册的 async handler，并向 handler 传入受控 `SkillExecutionContext`。handler 可以调用领域服务，但不得把 secret、DB URL 或完整 config 暴露给 Skill manifest、event、artifact 或用户可见输出。

如果后续继续探索“子进程脚本 + service binding”，必须另写 PRD 或在本 PRD 后续修订中补充 IPC 安全设计。可接受方向包括：

1. 父进程代理服务调用；
2. 子进程只拿到临时受限 capability handle；
3. 独立权限边界、请求签名、超时、调用审计与 token 撤销机制。

在 v1 落地前，`python_subprocess` 不允许绑定受控 service。

### 9.8 artifact 输出

Skill Executor 必须复用 Skill 输出文件 artifact 规范。

输出类型：

1. 结构化 JSON artifact；
2. summary artifact；
3. file artifact；
4. text artifact；
5. domain-specific metadata，例如 `domain_kind="data_query"`。

要求：

- artifact id 稳定、可追踪；
- producer node id 指向 Skill execution node；
- artifact metadata 能表达 domain kind / artifact role；
- 前端不应再依赖 capability id 字符串猜测 artifact 类型。

### 9.9 event 与 audit

建议事件：

| event_type | visibility | 说明 |
|---|---|---|
| `skill.execution_started` | audit-only | Skill executor 开始执行 |
| `skill.execution_completed` | audit-only | Skill executor 成功完成 |
| `skill.execution_failed` | audit-only | Skill executor 失败 |
| `skill.entrypoint_started` | audit-only | 某脚本 / entrypoint 开始 |
| `skill.entrypoint_completed` | audit-only | 某脚本 / entrypoint 完成 |
| `skill.entrypoint_failed` | audit-only | 某脚本 / entrypoint 失败 |
| `skill.input_missing` | frontend 或 audit-only，按场景 | 缺少必填参数 |
| `skill.service_bound` | audit-only | 已绑定受控服务 |
| `skill.service_denied` | audit-only | service binding 被拒绝 |
| `skill.progress` | frontend | 长流程 Skill 的业务进度 |

`skill.progress` payload 建议：

```json
{
  "capability_id": "skill.data_lookup",
  "skill_name": "data-lookup",
  "stage": "execute_query",
  "message": "正在检索数据库"
}
```

### 9.10 错误映射

Skill Executor 必须把错误归一化为 `CapabilityExecutionError`。

建议错误码：

| 错误码 | retriable | 说明 |
|---|---:|---|
| `skill_capability_not_registered` | false | capability 不在 active bundle |
| `skill_bundle_revision_missing` | true | 任务引用的 revision 不存在，通常是 runtime 保留策略问题 |
| `skill_input_validation_failed` | false | 输入 payload 不满足 schema |
| `skill_input_missing` | false | 缺少必填业务参数 |
| `skill_entrypoint_not_allowed` | false | entrypoint 未声明或不允许执行 |
| `skill_script_timeout` | true | 脚本超时 |
| `skill_script_failed` | false | 脚本返回非 0 或输出非法 |
| `skill_output_validation_failed` | false | 输出不满足 manifest contract |
| `skill_service_denied` | false | service binding 未授权 |
| `skill_service_failed` | 按服务判断 | 受控服务调用失败 |
| `skill_execution_cancelled` | false | 任务取消 |

### 9.11 取消与超时

要求：

- Scheduler / CancellationService 取消任务时，Skill Executor 应停止等待脚本或 service 调用；
- 子进程脚本必须被 kill 并回收；
- 进程内 service 调用应尽量支持 async cancellation；
- 超时必须产生明确错误码；
- cancel / timeout 不应留下未关闭的 DB connection、HTTP client、临时文件。

### 9.12 动态 Skill bundle revision

要求：

- 新任务使用 active revision；
- 运行中任务 retain planning revision；
- executor 执行时按 request metadata 中的 revision 解析 Skill；
- 任务结束后 release revision；
- revision 被错误清理时必须 fail closed 并记录 audit。

这与 `13-Skill动态加载与热部署PRD.md` 保持一致。

## 10. API 与 runtime 装配

### 10.1 API runtime

`build_api_runtime()` 应装配：

- `SkillRuntimeState`；
- `SkillExecutor`；
- `SkillExecutionService`；
- `SkillScriptRunner`；
- `SkillOutputArtifactManager`；
- 可选 `SkillServiceRegistry`。

`CompositeExecutor` 目标形态：

```python
CompositeExecutor([
    SkillExecutor(...),
    MCPToolExecutor(...),
    FinalAnswerExecutor(...),
])
```

兼容期可继续保留 `MainAgentExecutor`，但业务 Skill 不应长期依附它。

### 10.2 InstanceRegistry

需要注册本地 Skill executor instance：

```text
inst-skill-local
supported_capabilities = active skill.* ids
```

当前 `Scheduler` 按 `ExecutionInstance.supported_capabilities` 做精确 capability id 匹配，不支持 wildcard / marker。因此 v1 必须在 Skill bundle 激活时同步更新 `InstanceRegistry` 中 Skill executor instance 的具体 `supported_capabilities`。

如果未来希望使用 generic marker 或 wildcard，需要先修改 `Scheduler` 选择语义并补充独立测试；不得在当前精确匹配 Scheduler 下只注册 `"skill.*"` 或类似占位符。

### 10.3 CapabilityRegistry 同步

Skill bundle 刷新成功后，必须同步：

1. 移除旧 skill descriptors；
2. 注册新 skill descriptors；
3. 更新 payload policies；
4. 更新 capability id → Skill name mapping；
5. 更新 Skill executor instance 的 active `skill.*` supported capabilities；
6. 保证 Skill Executor 使用同一 active bundle。

### 10.4 rollout 与 rollback

Skill Executor 应按可回滚方式上线：

1. 首先抽出 Skill execution service，但保持 `MainAgentExecutor` 行为不变；
2. 新增 `SkillExecutor` 后先仅对测试 Skill 开启 executor mode；
3. 对项目级真实 Skill 采用 manifest / runtime 配置双开关；
4. 如 executor mode 失败，可把对应 Skill 回退为 `delegated_main_agent` 或禁用该 Skill capability；
5. Skill bundle refresh 失败时继续保留上一份 active bundle，不得清空 capability pool；
6. 数据查询 Skill 迁移过渡期可以保持 native 数据查询 Skill 与 `skill.data_lookup` 并行对比；当前完成态必须移除 native capability、旧 provider/executor/replanner 与旧 request alias。

## 11. 与 数据查询 Skill 化的关系

本 PRD 是 数据查询 Skill 化的前置 PRD。

数据查询 Skill 迁移目标：

```text
legacy_query.query native capability（已删除）
→ skill.data_lookup public capability
```

但 Skill Executor 不直接实现 数据查询 Skill。

正确关系：

```text
SkillExecutor
  负责执行 skill.data_lookup 的壳

数据查询 Skill domain engine
  负责 intent route / schema / SQL generation / guard / readonly execution / filtering

skill/<domain-query>/SKILL.md
  负责声明何时使用、输入输出、entrypoint、service 需求
```

禁止把 数据查询 Skill 业务逻辑写入 generic Skill Executor。

## 12. 安全需求

### 12.1 默认最小权限

- Skill 默认无 DB、LLM、HTTP、文件系统特殊权限；
- public capability 不等于 trusted；
- project trusted 必须显式声明 + runtime allowlist 双重通过；
- 用户级 Skill 默认不能使用 service binding。

### 12.2 secret 保护

禁止：

- 将 API key、DB URL、账号密码写入 script stdin；
- 将 secret 写入子进程环境变量；
- 将完整 `config.yaml` 传给 Skill；
- 在 audit / event / artifact 中记录 secret。

### 12.3 路径与文件

- 脚本路径必须位于 Skill package 内；
- 禁止 `..` 和绝对路径；
- 禁止 symlink script；
- 输出文件必须进入平台 managed artifact；
- 禁止返回本地绝对下载路径。

### 12.4 输出治理

- 输出大小有限制；
- JSON 输出必须可序列化；
- artifact metadata 必须脱敏；
- 对后续主代理可见的 Skill 输出需要做粗粒度清洗，避免 prompt injection 与 secret 泄漏。

## 13. 可观测性需求

必须记录：

1. Skill capability id；
2. Skill name；
3. Skill source path summary；
4. skill bundle revision；
5. entrypoint name；
6. execution mode；
7. duration_ms；
8. output_size_bytes；
9. service names；
10. status；
11. error_type / error_code。

不得记录：

- secret；
- 完整 prompt；
- DB URL；
- provider base_url；
- 大体量 rows；
- 用户上传文件全文。

## 14. 前端影响

短期前端不需要知道 Skill Executor 的存在。

前端只消费：

- task / node events；
- `skill.progress`；
- artifacts；
- final answer text。

数据查询 Skill 已迁移为 `skill.data_lookup`；前端当前应以 `skill.data_lookup`、artifact metadata 和 `skill.progress` stage 为主，旧 `legacy_query.*` 字符串仅可作为历史 artifact/event 展示 fallback。

## 15. 测试计划

### 15.1 integrations 测试

新增或补充：

- Skill manifest execution metadata parsing；
- service binding allow / deny；
- SkillRuntimeState revision retain / release；
- SkillScriptRunner 安全路径、timeout、stdout 限制、JSON 输出校验。

### 15.2 capabilities 测试

新增 `tests/capabilities/skill_tool/`：

1. `SkillExecutor.supports()` 识别 active `skill.*`；
2. 显式执行 script Skill 成功；
3. 缺少必填参数返回 `skill_input_missing`；
4. 非法输出返回 `skill_output_validation_failed`；
5. service binding 未授权返回 `skill_service_denied`；
6. 子进程 timeout 返回 `skill_script_timeout`；
7. artifact 输出被正确收集；
8. audit events 不含 secret。
9. `python_subprocess` Skill 不能获得 service binding；
10. `platform_service` Skill 只能调用 runtime 预注册 handler，manifest 任意 handler 字符串会被拒绝。

### 15.3 orchestration 测试

- Planner 可选择 `skill.*`；
- WorkflowProvider 可生成 Skill execution node；
- final answer 节点按规则追加；
- dynamic skill refresh 后新旧 revision 隔离；
- orchestration 不需要知道具体 Skill 业务语义。
- `answer_mode=direct` 不追加重复回答节点；
- `answer_mode=requires_finalizer` 追加且只追加一个 final answer 节点；
- Skill bundle 刷新后 `InstanceRegistry` 中 Skill executor instance 的 supported capabilities 与 active public Skill 同步。

### 15.4 API 测试

- `/api/v1/capabilities` 返回 public Skill；
- 显式提交 `capability_id="skill.test_tool"` 可执行；
- 新聊天刷新 Skill 后新 capability 可见；
- 运行中任务不受 Skill 修改影响；
- service binding 配置错误 fail closed。
- executor mode feature flag / manifest 开关关闭时可回退到 legacy forced main agent 路径。

### 15.5 e2e 测试

- 一个项目级测试 Skill 从自然语言触发到最终回答完整闭环；
- 一个 script Skill 生成 artifact 并被前端 / API 查询；
- 数据查询 Skill 已完成 Skill 化：`skill.data_lookup` e2e 通过，native 数据查询 Skill capability 已删除，旧 `data_query` / `legacy_query.query` 请求被拒绝。

### 15.6 推荐命令

```bash
conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/e2e -p 'test_*.py'
```

前端涉及 数据查询 Skill artifact 展示时：

```bash
cd frontend
npm test -- --run
npm run build
```

## 16. 分阶段交付建议

### Phase 1：Skill execution service 抽取

- 从 `MainAgentExecutor` 中抽出参数解析、脚本执行、输出处理公共服务；
- 不改变现有行为；
- 用现有 main_agent Skill 测试证明行为不退化。

### Phase 2：SkillExecutor 最小可用

- 新增 `src/capabilities/skill_tool/`；
- 支持 `python_subprocess` script Skill 执行；
- 输出 `CapabilityExecutionResult`；
- 暂不支持 service binding；
- 显式 `skill.test_tool` API 可跑通。

### Phase 3：service binding

- 增加 `SkillServiceRegistry` / `SkillExecutionContext`；
- 支持 `platform_service` project trusted Skill；
- 支持 allowlist；
- 完成安全测试。

### Phase 4：WorkflowProvider 切换

- `skill.*` `python_subprocess` / `platform_service` 默认走 SkillExecutor；
- instruction-only Skill 仍可 delegated main agent；
- Planner / Replanner / Macro expander 适配 execution mode 与 answer_mode。

### Phase 5：数据查询 Skill 化试点（已完成）

- `skill.data_lookup` 以 `platform_service` 执行，handler key 为 `skill.data_lookup.platform_handler`；
- 数据查询 Skill 输出、artifact、event 已由 Skill/domain path 承担；
- native 数据查询 Skill capability、旧 provider/executor/replanner 和旧 request alias 已清理，`data_query` / `legacy_query.query` 请求返回 unsupported capability。

## 17. 验收标准

Skill Executor 主题完成时必须满足：

1. 系统存在 generic Skill Executor，可执行 public `skill.*` script capability。
2. Skill execution 逻辑不再只存在于 `MainAgentExecutor` 私有方法中。
3. `skill_bundle_revision` 在执行期被严格使用。
4. script path、runtime、stdin/stdout、timeout、输出大小都有安全约束。
5. 输出能转换为 `CapabilityExecutionResult`、artifacts、events。
6. service binding 有明确 allowlist，普通 Skill 无法访问受控服务。
7. audit 不记录 secret、完整 prompt、DB URL、provider base_url。
8. orchestration 只按 `skill.*` 通用规则编排，不知道具体 Skill 业务逻辑。
9. 至少一个测试 Skill 通过 API 显式调用、自动规划调用和 artifact 查询闭环。
10. 数据查询 Skill 已迁移到 `skill.data_lookup`，orchestration 核心不含 数据查询 Skill native 分支；旧 `data_query` / `legacy_query.query` 请求被拒绝。
11. `python_subprocess`、`platform_service`、`delegated_main_agent` 三种执行模式边界清晰，且测试覆盖未授权 service / handler 被拒绝。
12. `answer_mode` 控制 final answer 追加行为，测试覆盖 direct 不重复、requires_finalizer 追加一次。
13. Skill bundle 刷新会同步 CapabilityRegistry、payload policies、Skill executor instance supported capabilities 与 executor active bundle。

## 18. 关键决策

1. Skill Executor 是通用执行壳，不承载业务逻辑。
2. `skill.*` capability 的业务来源是 Skill manifest 与 Skill 包，不是 Python 内置 descriptor。
3. script Skill 应优先由 Skill Executor 执行，而不是 forced `main_agent.respond`。
4. instruction-only Skill 可以在兼容期继续走 delegated main agent。
5. service binding 必须“manifest 声明 + runtime allowlist”双重通过。
6. 用户级 Skill 默认不能获得数据库、内部 LLM 等受控服务。
7. 数据查询 Skill 化必须以本 PRD 的 Skill Executor 为前置，不应直接把 数据查询 Skill 塞进现有主代理 forced skill 路径。
8. v1 中受控 service binding 只允许 `platform_service`，普通 `python_subprocess` Skill 不绑定受控服务。
9. `platform_service` handler 必须 runtime 预注册，manifest 不能触发任意动态 import。
