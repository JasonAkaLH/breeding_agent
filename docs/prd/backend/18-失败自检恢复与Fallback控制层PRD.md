# 失败自检、恢复与 Fallback 控制层 PRD

- **项目**：breeding_agent
- **范围**：后端执行可靠性、前端恢复体验、审计与 Runtime sidecar 可靠性、LLM provider fallback 策略
- **文档状态**：PRD 草案（已完成设计确认，待实施计划拆解）
- **日期**：2026-05-24
- **关联文档**：`docs/失败自检恢复与Fallback待补清单.md`

## 0. 背景与问题

当前系统已经形成任务状态、节点执行、事件持久化、SSE、Skill/MCP runtime、Rust sidecar shadow/enforce、前端刷新恢复等基础能力。现有机制的总体倾向是 **fail-closed + 可审计降级 + 前端状态补偿恢复**。

静态扫描发现，系统仍存在一组跨层缺口：

1. 节点执行边界异常可能直接变成任务级 crash，绕过节点失败与重规划链路。
2. 节点 `retry_policy` / `timeout_policy` 已有模型字段，但执行层尚未统一消费。
3. 前端事件流断开后缺少自动重连，虽然后端已经支持 replay。
4. 任务完成后 artifact 加载失败缺少明确重试入口。
5. 上传 ID 缺失会被收集，但缺少用户提示或 fail early。
6. 事件发布中的审计写入失败可能阻断 SSE 投递。
7. Runtime sidecar enforce 写路径尚未统一使用 contract 中定义的 bounded retry。
8. Planner provider failure 是否降级到主代理缺少正式产品策略。
9. 主代理 LLM provider failure 是否支持备用 provider / 更友好失败提示缺少正式产品策略。

本 PRD 将这些缺口收敛为统一的 **可靠性控制层** 需求，而不是在各处零散补丁式修复。

## 1. 目标

本 PRD 目标是设计并约束一套统一的失败自检、恢复与 fallback 控制层，使系统满足：

- 节点级异常能优先归属到节点失败，而不是直接任务 crash。
- 节点 retry / timeout 策略真实生效，且与取消、重规划、外部服务副作用边界一致。
- 前端事件流短断可自动恢复，并利用后端 replay 补齐事件。
- artifact 加载失败不导致用户丢失成功任务结果，用户可重试加载。
- 上传文件缺失不再静默忽略。
- 审计 sink 短暂失败不阻断用户可见事件投递。
- sidecar enforce transient failure 在安全条件下 bounded retry，最终失败仍 fail closed。
- Planner / 主代理 provider fallback 由显式配置、审计与 metadata 控制，不隐式改变任务路线。
- 所有新增 fallback 都有状态、事件或审计证据。

## 2. 非目标

本 PRD 不做：

- 不重写整个编排引擎为事件驱动架构。
- 不引入 LangChain、LangGraph、AutoGen 等外部 Agent 框架。
- 不改变 SQL 安全边界。
- 不把权限失败、安全拒绝、schema/contract 校验失败包装成成功。
- 不把 Rust / safety / MCP enforce 失败退回 Python legacy。
- 不默认启用多 provider failover。
- 不给所有 Skill 默认打开重试。
- 不在前端展示内部审计细节。
- 不在本 PRD 中实现具体代码；实现需后续实施计划拆解。

## 3. 核心原则

### 3.1 安全边界不 fallback

以下失败必须继续 fail closed：

- SQL 写入风险、多语句、系统库访问、越权表访问。
- 账号权限不匹配、跨账号上传、会话归属不一致。
- Rust / safety / MCP enforce 模式下缺少受信 artifact、contract mismatch、schema mismatch、error table mismatch 或必需 sidecar 不可用。
- required Skill / MCP 配置不可用。
- 输入或输出无法通过 schema / contract 校验且没有安全确定性 fallback。
- 用户显式指定某能力但该能力不存在、不可用或被权限拒绝。

### 3.2 可恢复失败进入可靠性控制层

以下失败应该被可靠性控制层接管：

- 能力执行抛异常。
- 调度器找不到可用实例。
- 能力返回 `retriable=True` 的结构化错误。
- MCP / DB / sidecar transient error。
- 节点执行超过 timeout。
- SSE 事件流断开。
- artifact 加载失败。
- 审计 sink 临时失败。
- 上传 ID 缺失。

接管不等于成功；应按失败类型进入重试、节点失败、前端恢复、用户提示或旁路审计降级。

### 3.3 不隐式改变任务路线

Planner provider failure、主代理 LLM provider failure 默认不隐式改变任务路线。任何 provider fallback 必须满足：

- 显式配置开关启用。
- 写入 audit-only 事件。
- 写入 task / response metadata。
- 有成本边界。
- 有测试覆盖。
- 用户可见结果不能被伪装成完全等同于主路径成功。

### 3.4 控制层只做执行保护，不做业务判断

可靠性控制层只理解通用执行信号：是否可重试、是否超时、是否取消中、是否安全失败、是否节点可归属、是否需要用户恢复、是否可进入重规划。它不理解 SQLQuery、OCR、RCBD 或某个具体业务 Skill 的领域语义。

业务能力仍需在自身边界内返回结构化结果。

## 4. 总体架构

可靠性控制层分为五个子层：

1. **后端节点执行保护层**：异常归一、retry、timeout、取消中断、节点失败事件。
2. **任务恢复与重规划层**：复用现有 completion / replan，让更多失败正确进入该链路。
3. **前端恢复体验层**：SSE 自动重连、artifact 重试、上传缺失提示、可重试错误展示。
4. **旁路可靠性层**：audit sink 失败隔离、Runtime sidecar bounded retry、shadow/enforce 语义一致。
5. **LLM fallback 策略层**：Planner / 主代理 provider fallback 的显式配置、审计、metadata 与用户语义。

## 5. 后端节点执行保护层需求

### 5.1 节点执行保护壳

所有节点执行必须经过统一保护壳。保护壳负责：

- 读取节点 retry 策略。
- 读取节点 timeout 策略。
- 执行能力调用。
- 捕获能力调用异常。
- 判断失败是否可重试。
- 检查任务是否正在取消。
- 记录每次 attempt。
- 产出最终节点结果：成功、等待输入或失败。

### 5.2 节点失败分类

#### A. 可重试业务失败

来源包括：能力返回 `retriable=True`、MCP transient、DB transient、sidecar transient、主代理 LLM transient。

行为：未超过 retry 上限时 backoff 后重试；超过上限后节点失败，并记录最终错误码与 attempt 数。

#### B. 不可重试业务失败

来源包括：安全拦截、权限拒绝、输入/输出 schema 不合法、required 依赖不可用、能力返回 `retriable=False`。

行为：不重试，直接节点失败；如果计划允许，由完成策略进入重规划判断。

#### C. 等待用户输入

来源包括：能力返回 interrupt、Skill 缺必需输入、SQLQuery 需要用户确认数据库或作物。

行为：不算失败，不触发 retry，不触发 replan，节点进入等待输入。

#### D. 执行边界异常

来源包括：无可用执行实例、执行器抛异常、能力内部未捕获异常、运行时适配层普通异常。

行为：能归属当前节点则转为节点失败；默认不可重试，除非异常被明确映射为 transient。

#### E. 系统级不可归属异常

来源包括：任务记录读取不到、核心状态写入失败、事件持久化失败、状态机不可恢复不一致。

行为：保留任务级 crash 处理，不强行转节点失败。

### 5.3 Retry 策略

节点 retry 策略应支持：

```yaml
max_attempts: 1
initial_backoff_ms: 500
max_backoff_ms: 5000
backoff_multiplier: 2
jitter_percent: 20
retry_on: []
```

兼容原则：现有节点默认 `max_attempts=1`，即不改变现有行为。只有明确配置的节点才允许多次尝试。

### 5.4 Timeout 策略

节点 timeout 初期只要求支持单次 attempt 超时：

```yaml
seconds: 60
```

可后续扩展 overall timeout，但本 PRD 初始实现不强制要求。

### 5.5 Attempt 审计事件

前端仍只接收高层节点事件；attempt 细节进入 audit-only：

- `node.attempt_started`
- `node.attempt_failed`
- `node.retry_scheduled`
- `node.timeout`
- `node.retry_exhausted`

审计字段至少包含：task id、node id、capability id、attempt index、max attempts、error code、error type、retriable、next backoff、duration、cancellation observed。

### 5.6 取消优先级

取消优先级最高。保护壳必须在 attempt 前、attempt 后、backoff 前检查任务取消状态。任务取消中不得启动新 attempt，不得继续等待 backoff，不得写入晚到成功结果。

### 5.7 与重规划关系

保护壳不直接 replan。它只负责把失败正确落成 `node.failed`。完成策略根据 required 节点失败与 replan 预算决定继续、重规划或任务失败。

### 5.8 各能力默认策略

- 主代理：默认不改变现有行为；如开启节点 retry，应控制成本。
- Skill：默认不重试；只有声明安全可重试的 Skill 才允许多次 attempt。
- SQLQuery：优先使用内部 DB retry 和 LLM fallback，外层默认不叠加 retry。
- MCP：普通 transient 可节点 retry；长任务创建成功后不建议重复整个节点。
- Runtime sidecar：核心状态写失败不交给节点 retry，走 sidecar 专用 bounded retry。

## 6. 前端恢复体验需求

### 6.1 SSE 自动重连

事件流断开后，前端应：

1. 查询任务状态。
2. 终态任务按终态处理。
3. active 任务进入 reconnecting 状态并重新订阅。
4. 使用指数退避：初始 500ms，最大 5000ms，倍数 2。
5. 利用后端 replay 补齐断线期间事件。
6. 利用 event id 去重，避免重复渲染。

状态查询失败时，不得误判任务失败；应保留当前 UI 状态并继续恢复。

### 6.2 reconnecting 用户表现

事件流断开但任务仍 active 时，前端显示轻量提示：

```text
连接中断，正在恢复任务进度……
```

恢复成功后清除提示。用户仍应可以点击取消。

### 6.3 artifact 加载失败可重试

任务完成但 artifact 加载失败时：

- 任务真实状态仍为 completed。
- assistant 气泡进入 result_load_failed 展示状态。
- 气泡保留 task id。
- 展示“重新加载结果”入口。
- 重试只重新加载 artifact，不重新执行任务，不重复写消息。

用户提示：

```text
任务已完成，但结果加载失败。
```

### 6.4 上传文件缺失提示

普通对话 / 自动路由中，如 upload id 缺失：

- 可以继续创建任务。
- 必须产生前端可见 warning。
- metadata 中只记录缺失数量或脱敏事实。
- 主代理 prompt 不得伪造文件存在。

显式文件类 Skill 中，如 required upload 缺失：

- 应 fail early。
- 不创建注定缺输入的长任务。
- 前端提示重新上传。

跨账号 / 越权上传必须直接权限拒绝，不进入 fallback。

### 6.5 上传 warning 事件

新增前端可见事件：

```text
upload.warning
```

payload：

```json
{
  "code": "upload_missing",
  "missing_count": 1,
  "message": "有附件未找到或已失效，本次任务未包含该附件。"
}
```

不得暴露 upload id、本地路径、storage key 或其他敏感信息。

## 7. 审计与 Runtime sidecar 可靠性需求

### 7.1 审计写入失败隔离

事件发布中的审计写入必须 best-effort：

- 审计成功：正常记录。
- 审计失败：记录轻量内部诊断，继续投递 SSE。
- 事件持久化失败：仍然是核心失败，不得吞掉。

### 7.2 审计失败诊断

应维护轻量诊断信息：

- 最近一次 audit sink error type。
- 最近一次失败时间。
- 连续失败次数。
- 总失败次数。

日志应降噪：第一次失败立即 warning，连续失败期间按时间窗口记录，恢复成功时记录 recovered。

### 7.3 事件发布顺序

推荐顺序：

1. 业务逻辑保存事件。
2. 发布器尝试写审计。
3. 不管审计成功失败，都继续投递订阅队列。
4. 投递失败不回滚已保存事件；前端可通过 replay 恢复。

### 7.4 Runtime sidecar bounded retry

enforce 写路径必须通过统一 sidecar retry helper。只有同时满足以下条件才允许 retry：

- 错误是 contract 定义的 retriable error。
- 操作允许 retry。
- 操作具备 idempotency key。
- 当前仍连接同一个 sidecar。
- 未超过最大次数。
- 当前任务或请求未取消。
- backoff 未超过限制。

不满足任一条件，继续 fail closed。

### 7.5 可 retry 与不可 retry 操作

适合 retry：

- append event：event id 作为幂等键。
- save task：稳定 task id 与版本语义。
- save node：稳定 node id 与状态版本语义。
- save artifact：稳定 artifact id 且写入幂等。
- cancellation token：稳定 token key。

不适合 retry：

- 缺幂等键写操作。
- contract 判断不可重试错误。
- schema / contract mismatch。
- permission denied。
- artifact allowlist mismatch。
- enforce artifact 不可信。
- 可能产生重复外部副作用的操作。

### 7.6 sidecar retry 审计

每次 retry 写脱敏审计，字段包括：component、operation、attempt、max attempts、error code、backoff ms、idempotency key fingerprint、same sidecar、final status、duration ms。

不得记录原始 payload、secret、token、原始 idempotency key、文件路径、数据库连接信息。

### 7.7 与节点 retry 的边界

sidecar enforce 写失败属于状态写路径可靠性问题。能在 sidecar helper 内安全重试的，在 sidecar helper 内完成；最终失败不得交给节点 retry 包起来重复执行能力。

### 7.8 shadow / enforce 语义

shadow 模式：Python 结果用户可见，sidecar 失败只写审计，不新增 shadow retry。

enforce 模式：sidecar 是主路径，符合 bounded retry 条件时可重试；重试耗尽或不可重试错误时 fail closed，不退回 Python legacy。

## 8. LLM fallback 策略需求

### 8.1 总体原则

LLM fallback 会改变任务路线、模型、质量、成本与信任边界。因此：

- 默认保持当前行为。
- fallback 必须由配置启用。
- fallback 必须写审计。
- fallback 必须进入 metadata。
- fallback 后的结果必须能区分来源。
- provider failure 与非法输出 / 安全失败 / 权限失败必须区分。

### 8.2 Planner provider fallback

新增配置：

```yaml
planner:
  provider_failure_fallback: disabled | main_agent_only
```

默认 `disabled`。

#### disabled

Planner provider failure 仍为 planning_failed。

#### main_agent_only

仅当 planner provider / network failure 时启用，使用确定性主代理单节点计划。不得用于：

- planner 输出非法。
- planner 选择非法能力。
- 用户显式指定 Skill 失败。
- 认证或权限失败。

触发后必须写入：

- task metadata：`planner_source=fallback`、`planner_fallback_reason=provider_failed`、`original_planner_error_type`。
- audit-only 事件：`planner.provider_fallback`。

### 8.3 主代理 provider fallback

主代理 LLM provider failure 初始推荐只做更明确失败提示，不默认 provider failover。

默认行为：

- 返回结构化失败。
- 前端展示“模型服务暂时不可用，请稍后重试”。
- 如节点 retry 开启，可由节点保护壳重试一次。
- 不把模板化错误提示作为正常 assistant 成功消息写入历史。

可选 failover 配置：

```yaml
main_agent:
  provider_fallback:
    mode: disabled | failover
    chain:
      - primary
      - backup_a
```

启用后，仅 provider/network/timeout transient 可切换 backup。prompt 构造错误、schema 错误、权限错误不 failover。

### 8.4 成本边界

provider fallback 与节点 retry 不得组合爆炸。推荐：

- 未配置 backup provider 时：可考虑 `main_agent node max_attempts=1 或 2`。
- 配置 backup provider 时：`provider chain=primary+1 backup`，节点 `max_attempts=1`。

## 9. 端到端数据流

### 9.1 主流程

1. 校验会话、账号、上传附件。
2. 创建用户消息。
3. 创建任务。
4. 保存 accepted 事件。
5. 构建任务计划。
6. 创建任务节点。
7. 节点进入执行保护壳。
8. 执行 attempt。
9. attempt 成功、等待输入、失败或超时。
10. 节点状态落库。
11. 发出节点事件。
12. 完成策略判断继续、等待、重规划、失败或完成。
13. 任务终态落库。
14. 前端通过 SSE 或 replay 获取事件。
15. 前端加载 artifact 或展示失败 / 等待输入 / 取消状态。

### 9.2 Retry 数据流

```text
node.started
  -> attempt 1 started
  -> capability execution
  -> success / waiting / error / timeout
  -> if retriable and attempts remain: retry_scheduled -> backoff -> next attempt
  -> final node completed / waiting / failed
```

### 9.3 Replan 数据流

```text
required node failed + replan budget remains
  -> replan available
  -> build revised plan
  -> continue execution

required node failed + no budget
  -> task.failed
```

### 9.4 前端恢复数据流

SSE 中断但任务 active：

```text
stream error -> get task status -> active -> show reconnecting -> backoff -> resubscribe -> replay -> dedupe -> continue
```

artifact 加载失败：

```text
task.completed -> load artifacts failed -> result_load_failed -> user retries -> load artifacts -> update message
```

上传缺失：

```text
ordinary task: resolve uploads -> missing -> create task -> upload.warning -> continue
explicit file skill: resolve uploads -> required missing -> reject early
permission mismatch: reject + security audit
```

## 10. 状态与事件契约

### 10.1 节点状态

节点最终状态仅允许：

- `completed`
- `waiting_for_input`
- `failed`
- `cancelled`
- `blocked_by_cancellation`

`pending` / `running` 只允许作为中间状态，不应成为长期终态。

### 10.2 前端可见事件

继续使用现有事件，并新增：

- `upload.warning`

前端可见事件只表达用户需要知道的状态。

### 10.3 审计事件

新增或强化：

- `node.attempt_started`
- `node.attempt_failed`
- `node.retry_scheduled`
- `node.timeout`
- `node.retry_exhausted`
- `planner.provider_fallback`
- `main_agent.provider_fallback`
- `audit.sink_failed`
- `audit.sink_recovered`
- `runtime.sidecar_retry_scheduled`
- `runtime.sidecar_retry_exhausted`

### 10.4 错误分类字段

统一错误字段建议：

```text
error_code
error_category
retriable
safe_message
diagnostic_type
attempt
max_attempts
```

`error_category` 建议取值：

- `business_error`
- `validation_error`
- `permission_error`
- `security_denied`
- `provider_transient`
- `provider_permanent`
- `timeout`
- `runtime_boundary_error`
- `storage_error`
- `sidecar_transient`
- `sidecar_permanent`
- `cancelled`

`safe_message` 不得包含 prompt 原文、secret、token、数据库连接、本地文件路径或原始 sidecar payload。

## 11. 验收标准

### 11.1 后端执行

- 能力抛异常后节点失败，而不是任务直接 crash。
- 无可用实例后节点失败，而不是任务直接 crash。
- retriable 第一次失败、第二次成功。
- retriable 耗尽后节点失败并记录 retry exhausted。
- non-retriable 不重试。
- timeout 后节点失败。
- waiting input 不重试。
- cancellation during backoff 停止 retry。
- late result discarded。
- node.failed 后现有 replan 逻辑能接管。

### 11.2 前端恢复

- SSE 断开且 active task 自动重连。
- replay 事件不重复渲染。
- 状态查询失败不误判失败。
- completed 后 artifact 加载成功。
- artifact 首次失败后可 retry。
- retry 成功后替换气泡。
- upload warning 可见但不污染历史正文。
- 显式文件能力缺附件 fail early。

### 11.3 审计与 sidecar

- audit sink 失败不阻断 SSE。
- audit sink 连续失败降噪。
- audit sink 恢复有 recovered 记录。
- sidecar transient retry 成功。
- sidecar retry 耗尽 fail closed。
- sidecar 不可重试错误不 retry。
- shadow sidecar 失败不影响用户结果。
- enforce 失败不回退 Python。

### 11.4 LLM fallback

- planner disabled 走主代理。
- planner provider failure fallback disabled -> planning failed。
- planner provider failure fallback enabled -> main agent only。
- planner invalid output 不 fallback。
- explicit Skill failure 不 fallback。
- main agent provider failure 显示模型服务不可用。
- backup provider 成功记录 metadata。
- backup provider 失败返回结构化失败。
- provider fallback 审计脱敏。
- retry 与 provider chain 不组合爆炸。

## 12. 测试要求

实施时至少补充：

- 后端 orchestration / lifecycle / api 分层回归。
- frontend reducer / App / client 回归。
- storage / runtime sidecar contract 回归。
- integrations MCP / Skill / LLM fallback seam 回归。
- e2e 覆盖 SSE replay + reconnect + artifact retry 的用户路径。

推荐验证命令沿用项目现有分层 unittest 与前端 Vitest/build 命令；涉及 Rust sidecar 或依赖变更时必须按仓库 License Requirement 运行 cargo-deny 相关检查。

## 13. 推进顺序建议

虽然本 PRD 是大而全设计，实施仍建议分阶段：

1. 后端节点执行保护壳：异常归一、retry、timeout、取消优先级、replan 接入。
2. 前端恢复体验：SSE 重连、artifact retry、upload warning / fail early。
3. 审计与 sidecar 可靠性：audit sink 隔离、sidecar bounded retry。
4. LLM fallback 策略：配置、审计、metadata、前端错误文案。
5. 端到端回归与文档更新。

## 14. 未决策项

本 PRD 已明确默认策略，但以下能力是否启用仍需实施前按配置确认：

- Planner provider failure 是否在生产启用 `main_agent_only` fallback。
- 主代理是否配置 backup provider chain。
- 哪些 Skill 声明可安全重试。
- 文件类 Skill 的 `requires_uploaded_artifact` manifest 字段命名与迁移方式。

默认实现必须在这些策略未启用时保持现有行为。
