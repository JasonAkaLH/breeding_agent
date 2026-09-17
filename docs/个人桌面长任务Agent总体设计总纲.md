# 个人桌面长任务 Agent 总体设计总纲

- 日期：2026-07-17
- 状态：设计已确认；实现尚未开始
- 目标平台：macOS、Windows、Linux
- 产品形态：个人本地桌面工作台
- 产品主旨：主 Agent 能够通过 Runtime 受控 spawn 子 Agent，将长任务拆分为可独立调度、取消和恢复的 Child Run；所有权限、上下文和交接都由主 Agent 决策并由 Runtime 仲裁
- 核心决策：Rust daemon 是唯一可信控制 runtime；模型决定下一步，Runtime 决定是否以及如何执行

## 1. 背景

当前仓库以 Python/FastAPI 服务端、React 前端、静态 workflow/DAG 编排为主要产品形态，并已经包含一批 Rust runtime、event log、artifact、safety、MCP 与 sidecar 基础 crate。现有顶层控制流更适合预定义节点执行：任务通常由 planner 生成有限图，再由调度器执行至图结束。它不能自然表达 Claude Code、Codex 一类 Agent 的核心循环：模型观察最新结果、选择下一动作、遭遇错误后换方案，并持续运行到验收完成。

本设计不再扩展服务端产品，而是一次性转向安装在个人电脑上的桌面 Agent。用户不默认使用 Git，因此任务状态、差异、恢复和历史不能依赖 Git 仓库。产品必须支持本地文件、后台长任务、显式 Workspace 授权、严格且不可关闭的沙箱、本地加密历史、可逆文件修改，以及未来受控接入 Skill、Plugin 和 MCP 的稳定边界。

受控多 Agent 协作是产品的核心能力，而不是可选扩展。主 Agent 可以按目标、角色和边界拆分工作并请求 spawn 子 Agent；Runtime 负责验权、调度、隔离、持久化和恢复。子 Agent 之间不直接通信或移交权限、上下文和任务，所有交接必须回到主 Agent 决策，再由 Runtime 验证并投递。

## 2. 设计目标

1. 让 Agent 在安全且持续产生可验证进展时自动运行到完成，不以固定轮数作为正常停止条件。
2. 将受控子 Agent spawn 作为核心产品能力：每次成功 spawn 都产生可独立调度、取消和恢复的 Child Run，权限只能缩小，上下文最小化，任何跨子 Agent 交接必须经主 Agent 决策和 Runtime 仲裁。
3. UI 关闭、daemon 崩溃或系统重启后，未完成任务能够从持久状态恢复。
4. 模型只能提出动作；所有文件、进程、网络、权限、完成和恢复决策由 Rust Runtime 裁决。
5. 首版只允许官方打包并签名的工具，同时为未来 Skill、Plugin、MCP 保留进程隔离和版本化协议。
6. 沙箱严格且不可关闭；三平台必须提供一致的安全语义，不能静默降级。
7. Agent 引起的文件修改必须可审计、可恢复，且备份失败时禁止修改真实文件。
8. 任务、事件、备份和 artifact 默认只在本地加密保存；不默认上传遥测或历史。
9. 不绑定 Git、GitHub、服务器账号或云端执行环境。

## 3. 非目标

- 不设计服务端 Agent runtime、云端任务调度或远程 Workspace。
- 不保留 FastAPI 作为个人版的本地后台，也不维护新旧 API 双轨兼容。
- 首版不开放第三方 Skill、Plugin、MCP，也不支持自动发现、注册和调用用户自定义脚本。
- 首版不提供 Git commit、branch、push、PR 等内建操作。
- 工作台不替代完整 IDE，只提供任务执行所需的轻量文件、diff、artifact 和 PTY 视图。
- 不允许 Full Access、关闭沙箱、任意网络或读取操作系统密钥。
- 不要求产品完全排除 Python。Python、Node 等可以作为受限 worker 或用户项目工具，但不能成为可信控制面。

## 4. 总体架构

```text
Tauri Desktop Workbench
        │ authenticated, versioned local IPC
        ▼
Rust Agent Daemon (agentd)
        ├── Agent Control Plane
        ├── Runtime Authority
        ├── Model Gateway
        ├── Execution Coordinator
        ├── Event / Checkpoint Store
        ├── Policy / Sandbox Broker
        ├── Change Journal / Backup Store
        └── Sub-agent Runtime
                    │ short-lived capabilities
                    ▼
          Sandboxed Tool Workers
```

### 4.1 Desktop Host

Tauri Desktop 负责工作台 UI、系统文件选择器、托盘、通知、升级和 WebView 安全边界。它不是任务状态所有者，不直接访问任务数据库，也不直接执行模型或工具。UI 关闭不终止 Run；重新打开后通过事件 cursor 重建状态。

### 4.2 Rust Agent Daemon

`agentd` 是唯一可信 Agent 与执行控制 runtime，也是任务状态权威。内部按 crate/module 契约拆分，而不是堆成无边界单体：

- `agent-core`：Agent Loop、上下文、计划、反思和完成提议。
- `runtime-authority`：Run 状态机、取消、恢复、审批、完成门禁和审计。
- `model-gateway`：云端与本地模型适配；没有文件、进程和工具权限。
- `execution-coordinator`：工具调用、并发、资源、子 Agent spawn、交接和生命周期调度。
- `event-store`：append-only event、checkpoint、snapshot、replay。
- `policy-engine`：风险等级、权限子集和 capability lease。
- `sandbox-core`：三平台统一的文件、进程和网络安全语义。
- `change-journal` 与 `backup-store`：文件变化、备份、diff、冲突和恢复。

关键模块通过显式 command/event contract 协作。Prompt、模型输出或工具结果不能修改 Runtime policy。

### 4.3 Tool Worker

Shell、PTY、文件工具以及未来扩展在隔离 worker 中运行。Worker 不读取 daemon 数据库、根密钥或 Provider 凭据，只能使用 Runtime 发放的短期最小 capability。Worker 崩溃只能影响单次 Tool Call，不能破坏 Run 状态。

Python 可以受限用于官方工具、OS 兼容桥、迁移、构建或测试脚本，也可以在受控 Shell 中运行用户项目。Python 进程不能进入 daemon 地址空间或绕过 capability protocol。官方 Python 工具若被打包，不能依赖用户机器恰好安装了特定 Python 版本。

### 4.4 未来扩展边界

Skill、Plugin、MCP 不动态加载进 daemon 地址空间。未来统一通过版本化工具协议和独立进程接入，复用相同的签名、manifest、risk、approval、sandbox、event 和 audit 机制。首版保留协议和 registry seam，但关闭第三方入口。

## 5. 持久状态、事件日志与恢复

### 5.1 核心对象

```text
Workspace
└── Thread
    ├── Run
    │   ├── Turn
    │   ├── Tool Call
    │   ├── Checkpoint
    │   └── Child Run
    └── Historical Runs (read-only)
```

- `Workspace`：用户授权的一个或多个本地根目录及安全策略。
- `Thread`：用户看到的长期会话。
- `Run`：一次可恢复任务执行。
- `Turn`：一次模型决策及其 observation。
- `Tool Call`：一次可审计副作用操作。
- `Checkpoint`：恢复 Agent 结构化状态所需的快照。
- `Child Run`：有父子关系、独立上下文和权限子集的子 Agent。

完成后的 Run 只读。继续历史任务会创建带 lineage 的新 Run，而不是改写旧历史。

### 5.2 Run 状态机

```text
queued
  → running
  → waiting_model
  → waiting_approval
  → waiting_resource
  → needs_review
  → paused
  → cancelling
  → completed | cancelled | failed
```

临时模型、网络或资源故障进入可恢复等待。重复无进展或结果不确定进入 `needs_review`。只有确认不可恢复的错误进入 `failed`；只有通过完成门禁才进入 `completed`。

### 5.3 Event Log

本地加密 SQLite + WAL 承载 append-only event log。Event Log 是事实来源，snapshot 只用于加速。每个事件包含顺序号、Run、来源、时间、schema 版本和完整性校验。主要事件至少覆盖：

```text
RunCreated
ModelTurnRequested / ModelTurnCompleted
PlanUpdated
ToolProposed / ToolAuthorized / ToolStarted
ToolCompleted / ToolFailed / ToolOutcomeUnknown
CheckpointCreated
SubAgentSpawnProposed / SubAgentSpawnAuthorized / SubAgentSpawnRejected
SubAgentSpawned / SubAgentPaused / SubAgentResumed
SubAgentCompleted / SubAgentFailed / SubAgentCancelled
ContextEnvelopeCreated / ContextRequested / ContextEnvelopeUpdated
HandoffProposed / HandoffAccepted / HandoffRejected / HandoffDelivered
ApprovalRequested / ApprovalResolved
RunPaused / RunResumed
CompletionProposed / CompletionVerified
RunCompleted
```

数据库迁移或完整性校验失败时，产品以只读保护模式启动，禁止修改 Workspace。

### 5.4 意图—结果日志

所有副作用先记录意图，再执行并记录结果：

```text
ToolProposed
→ ToolAuthorized
→ ToolStarted
→ ToolCompleted | ToolFailed | ToolOutcomeUnknown
```

daemon 崩溃后可以区分未执行、成功、明确失败和结果未知。结果未知的非幂等操作不能盲目重试，必须先 reconciliation；无法确认时进入 `needs_review`。

### 5.5 Checkpoint

自动 checkpoint 边界包括：工具结果、实质性计划更新、用户输入或审批、上下文压缩前、子 Agent 创建或回收、完成提议前，以及暂停、退出和升级前。

Checkpoint 保存目标、验收条件、当前计划、已确认事实、未解决问题、Workspace 授权、上下文引用、artifact、子 Agent、重试/无进展计数和最近安全恢复点。文件内容由 Change Journal 与 Backup Store 管理，不塞入 checkpoint。

### 5.6 恢复流程

1. 校验事件日志、数据库和 schema 版本。
2. 加载最近兼容 checkpoint。
3. 重放 checkpoint 之后的事件。
4. 找出未结束的 Tool Call 和 worker。
5. 对照进程、Change Journal 和结果 artifact 进行 reconciliation。
6. 确定安全的 Run 自动恢复。
7. 结果不确定或权限变化的 Run 进入 `needs_review`。
8. UI 从事件 cursor 重新订阅。

## 6. Agent Loop 与上下文

### 6.1 顶层循环

```text
恢复 Run
→ 组装上下文
→ 请求模型决策
→ 校验结构化输出
→ 权限与风险判定
→ 执行工具
→ 写入 observation
→ 更新计划和 checkpoint
→ 再次请求模型决策
```

循环不设置普通固定轮数上限。只要仍有安全、可验证且产生进展的路径，就继续执行。停止条件仅包括完成门禁通过、用户取消、不可绕过的安全边界、持续无进展、无法确认的外部副作用或长期不可用的必要资源。

### 6.2 模型与 Runtime 的权责

模型可以更新计划、请求工具、请求子 Agent、分析错误、寻找替代方案、请求真正缺失的用户信息或提议完成。模型不能直接访问文件、进程、网络和密钥，不能扩大权限、跳过政策、伪造工具结果或自行把 Run 置为完成。

Rust Runtime 对每次模型输出进行 schema、能力、权限、风险和状态机校验。工具错误作为结构化 observation 返回模型，不自动终止任务。

### 6.3 上下文分层

每轮上下文按以下层次组装：

1. 不可变规则与安全策略。
2. 用户目标、验收条件和 Workspace。
3. 当前计划、进度和未解决问题。
4. 当前相关文件、diff、诊断和 artifact 工作集。
5. 最近模型决策和工具结果。
6. 更早历史的结构化摘要与引用。

安全策略、用户原始要求和关键验收条件不能由摘要替换。

### 6.4 上下文压缩

接近 Provider 上下文阈值时，Runtime 使用当前结构化状态、历史事件范围和 artifact 引用生成候选摘要，校验必需字段后保存摘要并创建 checkpoint。摘要必须保留用户目标、禁止事项、已作决策、计划状态、文件变化、验证证据、失败方案及原因、未解决问题、子 Agent 结果和下一步。

原始事件不因压缩删除，摘要可以审计和重建。

### 6.5 Provider 能力门禁

Provider 适配器声明 streaming、tool calling、structured output、context size、reasoning 参数、并行工具调用、输入模态和本地/远程属性。能力不足的模型不能担任主 Agent，但可以用于摘要、分类等受限任务。

### 6.6 DAG 的新定位

静态 DAG 不再控制顶层 Agent 生命周期，仅保留为可选的确定性流程、用户模板、复合工具或验证流水线：Agent Loop 是大脑，DAG 是工具，Rust Runtime 是裁判。

## 7. 官方工具、严格沙箱与网络

### 7.1 官方工具协议

每个官方工具携带签名 manifest，声明 ID、版本、输入/输出 schema、平台、所需能力、风险等级、幂等性、超时、资源上限和 artifact 类型。执行流程为：

```text
Agent 提议 Tool Call
→ Tool Registry 校验
→ Policy Engine 判定风险与权限
→ 必要时审批
→ Sandbox Broker 发放短期 capability
→ Worker 执行
→ 规范化结果写入 Event Log
```

首版官方工具覆盖 Workspace 文件读写与搜索、精确 patch、diff、受控 Shell/PTY、进程控制、官方 HTTP 获取、artifact、Change Journal/恢复、计划/验证和子 Agent 管理。

### 7.2 Workspace 绝对边界

用户通过系统文件选择器显式授权 Workspace root。沙箱必须阻止 `..`、symlink、junction、UNC、大小写和短文件名等越界方式，限制子进程访问未授权目录，并保证子 Agent 只能继承权限子集。授权范围始终显示在工作台。

### 7.3 三平台后端

- macOS：Seatbelt/App Sandbox 类策略与受控进程环境。
- Linux：namespace、mount 隔离、Landlock/seccomp 与资源限制。
- Windows：AppContainer/restricted token、Job Object 与目录能力授权。

统一 conformance suite 定义安全语义。平台不能满足某能力时必须禁用相关工具，不能以警告代替隔离。

### 7.4 Shell

Shell/PTY 必须位于授权 Workspace，不继承 daemon 密钥和完整环境，限制 CPU、内存、进程数和输出，记录命令、cwd、进程树与结果，并在取消时终止完整进程树。后台进程必须显式注册。

Shell 永久没有原始网络 socket。批准高风险命令也不能突破 Workspace 和网络边界。

### 7.5 Network Broker

模型 Provider 网络与工具网络分离。官方网络工具只能通过 Rust Network Broker 访问域名白名单。Broker 校验 DNS、重定向、TLS、请求/响应大小，禁止本机、局域网和云 metadata 地址，并审计临时域名授权。Shell 需要联网时必须使用官方 HTTP、包获取或未来专用工具，不能获得 raw network。

### 7.6 风险等级

- `R0`：Workspace 内只读，默认允许。
- `R1`：小范围且可恢复的文件修改；Run 已授权时自动执行。
- `R2`：Shell、批量修改、启动服务、恢复旧版本；首次或范围变化时审批。
- `R3`：临时网络域名、敏感文件类型、大规模删除；逐次审批。
- `R4`：关闭沙箱、系统密钥、任意网络或越界访问；永久禁止。

审批产生与 Run、范围和有效期绑定的 capability lease。模型风险标注只作提示，最终等级由 Rust Policy Engine 计算。

## 8. Change Journal、Diff 与恢复

### 8.1 修改路径

普通文件工具先读取当前内容和 hash，保存 pre-image，在内存或临时文件应用变化，再校验真实文件仍等于 base hash，最后原子替换并写入 ChangeSet。

可能写文件的 Shell、格式化器和代码生成器不能直接写真实 Workspace，必须在临时事务视图中运行：

```text
创建事务视图
→ 命令在事务视图执行
→ 计算与真实 Workspace 的差异
→ 选择需要提交的变化
→ 备份真实文件 pre-image
→ 校验外部冲突
→ Rust Runtime 应用
→ 记录 ChangeSet
```

事务视图可由平台 Copy-on-Write 或按需物化 staging 实现，但必须保持相同语义。构建缓存、临时文件和未选择产物不进入长期 Change Journal。

### 8.2 ChangeSet

ChangeSet 记录来源 Run/Turn/Tool Call、修改目的、文件新增/修改/移动/删除、前后 hash、diff、backup 引用、验证结果、时间、风险和恢复状态。多文件操作不伪装为文件系统原子事务；崩溃后按文件 journal reconciliation。

### 8.3 Diff 实现

修改前建立包含规范化路径、文件类型、内容 hash、大小、权限、编码、换行符和 backup 引用的基线。mtime/size 仅用于快速排除，内容 hash 是权威。

事务视图与基线的比较语义：

- 基线有、事务无：删除。
- 基线无、事务有：新增。
- 两边存在且 hash 不同：修改。
- 旧路径删除且新路径 hash 相同：重命名候选；底层仍按删除和新增记录。

文本 diff 保留原编码与 CRLF/LF，使用可读性优先的 patience/histogram 策略划分区块，再以 Myers 类算法得到精确行级增删，输出 unified diff 与结构化 hunk。恢复不依赖 diff：pre-image、post-image 引用和 hash 才是权威，diff 可以重新生成。

二进制文件只展示 hash、大小、MIME 和安全元数据，可选本地预览，不生成伪文本 diff。大文件完整计算 hash，但 UI 和模型上下文默认只加载摘要及有限区块。

### 8.4 外部并发修改

若真实文件在事务期间发生变化，Runtime 使用 `base`、Agent staging 的 `ours` 和当前真实 `current` 做三方比较。`current == base` 时可应用；三者不同则尝试三方合并；有冲突时禁止覆盖并进入 `needs_review`。

### 8.5 Backup Store

Backup Store 位于 Workspace 外，内容寻址、分块、去重并本地加密。第一次修改保存完整 pre-image，后续版本使用压缩 delta，并定期生成完整 baseline。新增文件记录“原先不存在”，删除文件保留完整 pre-image。备份失败时拒绝真实文件修改。

### 8.6 恢复

用户可以恢复单文件、ChangeSet、Run 修改集合或 pinned checkpoint 对应状态。恢复前先备份当前版本并创建新的 ChangeSet，因此恢复本身也可撤销，历史不被重写。

### 8.7 留存策略

- 活跃、等待和 `needs_review` Run 的备份不自动过期。
- terminal Run 的备份保留 30 天。
- 每个文件最多保留 100 个可恢复版本。
- pinned checkpoint 保留到用户主动删除。
- 被恢复的版本重新进入 30 天保护期。
- 默认额度为 `10 GiB` 与磁盘容量 `5%` 中较小者。
- 接近系统低磁盘下限时暂停文件修改。
- 不静默删除保护期内备份；清理动作写入审计事件。

任务和 ChangeSet 元数据可长期只读保留；过期且未 pinned 的大体积内容可按策略清理。

## 9. 受控多 Agent 协作

### 9.1 产品能力与信任模型

子 Agent spawn 是首版核心产品能力，不能退化为主 Agent 内的一次普通模型调用。每次获准 spawn 都创建 daemon 内持久化的独立 `Child Run`，拥有独立模型上下文、计划、事件、checkpoint、资源预算和取消令牌，能够被独立调度、暂停、取消和崩溃恢复，但不拥有新的可信 runtime。首版只允许 `Main Agent → Child Agent` 一层关系；只有主 Agent 可以请求 spawn，子 Agent 不能创建孙 Agent。

### 9.2 Spawn 契约

主 Agent 通过结构化 `ChildSpawnProposal` 请求创建子 Agent，至少包含 parent Run、objective、acceptance criteria、agent role、expected output、workspace scope、allowed tools、write scope、context/artifact 引用、prohibited actions、风险上限、资源/成本预算和幂等键。Runtime 在启动子 Agent 前必须验证父 Run 状态、角色与工具签名、权限子集、上下文引用完整性、写 lease 冲突、预算和平台能力，并先写入 spawn 意图事件。

验证通过后，Runtime 分配 Child Run ID、生成 capability lease 和 Context Envelope，再启动独立子 Agent loop；验证失败或资源不足时返回结构化拒绝或延迟原因，不创建半初始化 Child Run。模型只能请求角色和并发，不能指定绕过政策的执行方式，也不能声称子 Agent 已经启动。

### 9.3 权限投影与写入边界

子 Agent 的有效权限是父 Run 当前有效权限、spawn 请求范围、角色上限和 Runtime policy 的交集，只能缩小，不能在创建、恢复、交接或审批过程中扩大。子 Agent 默认只读；可写子 Agent 必须获得明确路径子集，两个活跃 Child Run 不能持有重叠写 lease。子 Agent 不接收 daemon 根密钥、Provider 凭据、父 capability token 或未授权环境变量。

子 Agent 需要新增权限时只能提交结构化请求，由主 Agent 决定是否继续，再由 Runtime 按正常风险和审批流程裁决。修改先形成候选 ChangeSet，再经过统一备份、hash 和冲突门禁；Child Run 不直接提交或覆盖其他 Run 的修改。

### 9.4 Context Envelope

Runtime 为每个 Child Run 生成版本化、带来源和完整性校验的最小必要 `Context Envelope`。信封包含目标、验收条件、角色、禁止事项、已接受决策、必要事实、授权范围以及只读的 context/artifact 引用；默认不复制父会话完整历史、其他子 Agent 上下文、未接受的推测或无关 Workspace 内容。

子 Agent 不能读取 sibling context 或共享可变记忆。执行中缺少上下文时只能向主 Agent 提交 `ContextRequest`；主 Agent 选择是否补充，Runtime 重新校验引用和权限并发布新版本信封。所有信封版本及其来源写入 Event Log，保证恢复和审计时能够重建子 Agent 实际看到的内容。

### 9.5 主 Agent 中介交接

子 Agent 之间不建立直接通信或任务移交通道。子 Agent 只能向主 Agent 提交结构化 `HandoffProposal`，包含 status、claims、evidence、artifact、候选 ChangeSet、unresolved、建议的接手角色、后续目标和所需上下文。主 Agent 负责接受、拒绝、合并或重新分派；Runtime 校验 proposal 来源、artifact/hash、权限和幂等键，并记录对应 handoff 事件。

重新分派时，主 Agent 必须提交新的 `ChildSpawnProposal`，引用已接受的 handoff artifact。Runtime 据此创建新的 Child Run 或新版本 Context Envelope；只有 `HandoffDelivered` 事件落盘后，接手子 Agent 才能看到交接内容。禁止 Child-to-Child 消息、直接指定 sibling 为接收者、共享 capability、共享可写任务板、直接转交完整上下文或绕过主 Agent 修改任务所有权。

### 9.6 调度、完成与恢复

子 Agent 返回结构化 status、claims、evidence、artifact、候选 ChangeSet、unresolved 和 next actions。Child 完成不等于父任务完成；主 Agent 必须审查和吸收结果。并发数量由 Runtime 根据 CPU、内存、磁盘、Provider 配额、写冲突、优先级和成本决定。

取消 Parent 默认取消全部 Child；Child 失败只向主 Agent 返回结构化失败。审批统一路由到父 Run 和工作台。daemon 重启后分别恢复 Parent、Child、Context Envelope、handoff 状态和未完成 lease；重复 spawn 或 handoff 通过幂等键去重。Parent 完成前必须回收或取消全部 Child，不得存在运行中、尚未审查完成或已接受但尚未投递的交接。

## 10. 完成门禁、持续执行与故障恢复

### 10.1 验收契约

Run 维护 objective、acceptance criteria、required verification、constraints、prohibited actions、expected artifacts 和 known unknowns。Agent 推导的验收项必须与用户明确要求区分。

### 10.2 组合式完成门禁

模型提交包含 claims、验收状态、证据、artifact 和 known gaps 的完成提议，然后经过三层门禁：

1. **确定性 Runtime 检查**：无在途工具/子 Agent、未处理失败、未提交 staging、未知结果、文件冲突或待审批；Change Journal 与真实 hash 一致；要求的构建和测试成功。
2. **独立 Verifier**：以只读独立上下文检查声明、证据、覆盖范围、警告/跳过和遗漏。代码及文件修改类任务必须经过 Verifier。
3. **策略与用户门禁**：执行用户要求的人工审查、高风险确认和本地验收 hook。

结果只能是 `accepted`、`continue`、`needs_review` 或 `blocked`。模型不能直接结束 Run。

### 10.3 进展账本

可观测进展包括验收项完成、计划推进、新证据、错误实质变化、artifact 产生、失败数量减少和 Verifier 缺口关闭。重复解释、改写计划文字或重复相同失败调用不算进展。

检测停滞后依次执行诊断、更新假设和计划、换工具/路径、请求独立批评、缩小问题和更小验证。只有经过多轮恢复仍无新证据时才进入 `needs_review`。

### 10.4 故障策略

- Provider 限流、超时和 5xx：持久化指数退避后自动继续。
- 凭据失效：等待用户修复，不丢失 Run。
- 幂等 worker 崩溃：限定次数重试。
- 非幂等结果未知：先 reconciliation，禁止盲目重试。
- 内存压力：降低并发、压缩上下文。
- 低磁盘：暂停写入，保留只读诊断。
- 输出过大：完整内容存 artifact，模型只接收摘要和引用。
- daemon/系统重启：从 event 与 checkpoint 恢复。

### 10.5 资源断路器

正常执行不使用固定轮数上限。用户可以设置费用、时间和并发预算；Runtime 保留防止失控循环的本地硬安全上限。接近上限时先压缩、降并发和重规划；达到上限进入 `needs_review`，不伪装成失败或完成。命中安全边界时拒绝相关动作，但允许 Agent 尝试合规替代方案。

## 11. 桌面工作台与本地 IPC

### 11.1 后台生命周期

daemon 以当前用户身份运行，不要求管理员权限。macOS 使用用户级 LaunchAgent，Windows 使用用户登录启动机制，Linux 优先 `systemd --user` 并提供桌面自动启动回退。每个 profile 只运行一个 daemon 实例，可管理多个 Workspace 和 Run。

UI 退出后任务继续；系统重启后恢复。没有 UI 时可以继续使用未过期 capability lease，需要新审批时暂停对应 Run 并发送通知。

### 11.2 IPC

macOS/Linux 使用当前用户专属 Unix domain socket，Windows 使用仅当前用户可访问的 named pipe。首次配对使用本地随机凭据和 challenge-response；凭据放入 OS Keychain/Credential Manager。协议包含版本、request ID 和 profile ID。WebView 不能直接访问 daemon socket，必须经过 Tauri Rust host。不开放 localhost HTTP 服务。

UI 通过 `subscribe(after_event_seq)` 订阅事件；断线后从最后确认 cursor 继续。

### 11.3 工作台

工作台包含会话/任务列表、对话、计划和验收条件、长任务时间线、工具与错误、Workspace 授权、审批、文件 diff/ChangeSet、备份恢复、artifact、轻量 PTY、子 Agent、模型费用和资源状态。子 Agent 视图必须展示 Parent/Child 树、spawn 请求与裁决、角色、权限差异、Context Envelope 来源和版本、资源预算、产物以及 handoff 的 proposed/accepted/rejected/delivered 状态。

Workspace root、读写范围、网络白名单、活跃 lease 和子 Agent 权限始终可见。收回授权后相关 Run 立即暂停，历史保持只读。

审批界面必须展示工具、命令、cwd、文件、域名、风险、有效期、理由和拒绝后的替代路径。支持批准本次、批准当前 Run 的明确范围、拒绝、拒绝并要求换方案；不提供关闭沙箱或永久任意授权。

PTY 中的用户输入也写入审计事件，但密码和秘密使用专用安全输入且不记录。用户接管 PTY 不扩大沙箱权限。

### 11.4 升级

更新包必须签名。升级前停止创建新的副作用操作，在安全边界创建 checkpoint 和数据库元数据备份，再安装并执行 IPC、数据库和 sandbox 健康检查。失败时回滚二进制；数据迁移必须保留兼容恢复路径。Desktop 与 daemon 支持有限相邻版本协议兼容。

## 12. 本地加密、隐私与同步预留

### 12.1 Profile 隔离与密钥

每个 profile 独立保存 Event/Checkpoint、Change Journal、Backup、Artifact、Model History 和诊断日志，不共享数据库、IPC 凭据和密钥。

OS 凭据库保存根密钥，根密钥包裹数据库、Backup、Artifact 和未来 Export/Sync 数据密钥。Provider API key/OAuth token 只在凭据库保存，不进入数据库、日志、checkpoint 或模型上下文。具体加密库在实施依赖评审中选择，但必须满足数据库静态加密、AEAD blob、密钥轮换和完整性校验语义。

### 12.2 数据保护

Backup 和 artifact 分块、去重后加密，带版本、nonce 和完整性标签。临时文件位于受控目录并及时清理，不产生长期明文 prompt cache。完整性失败进入只读保护模式。SSD 上的删除以销毁数据密钥和索引引用为主要保密手段，不声称物理覆盖保证。

### 12.3 模型外发

只有 Model Gateway 可以向用户选择的远程 Provider 发送内容。发送前选择最小必要上下文、排除无关文件、检测常见密钥/证书/敏感文件、遮盖不需要的敏感值、校验 endpoint，并记录发送的本地引用和大致数据量。工作台提供“本轮发送上下文”视图。使用本地模型时不产生模型外部网络请求。

### 12.4 默认隐私

首版无产品遥测、云端任务历史、后台行为分析和自动崩溃上传。诊断日志本地加密且不记录凭据。用户可以手动生成脱敏诊断包，并在导出前查看包含项。

### 12.5 云同步预留

首版不实现账号、同步服务器或云端执行，只预留稳定对象 ID、device ID、event lineage、schema version、encryption epoch、tombstone、sync cursor、ciphertext hash 和冲突分支字段。

未来同步必须设备端加密、云端仅保存 ciphertext 与最小索引；本地仍是状态权威。并发修改创建 Run/Thread 分支，不静默覆盖历史。

## 13. 三平台交付与一次性替换

### 13.1 建议 Workspace

```text
apps/desktop/
bins/agentd/
bins/tool-worker/
crates/agent-core/
crates/runtime-authority/
crates/event-store/
crates/model-gateway/
crates/tool-protocol/
crates/tool-runtime/
crates/policy-engine/
crates/sandbox-core/
crates/sandbox-macos/
crates/sandbox-linux/
crates/sandbox-windows/
crates/change-journal/
crates/backup-store/
crates/subagent-runtime/
crates/local-ipc/
crates/artifact-store/
crates/platform-integration/
```

现有 `native/` crate 只在契约匹配时迁移。不会为复用而保留服务端状态语义、Python orchestration 或静态 DAG 顶层控制。

### 13.2 一次性替换

个人版不保留 FastAPI 本地后台、旧 REST/SSE 兼容层、双 runtime UI 或双写数据库。新任务只能由 Rust daemon 创建。旧 Thread/Task/Artifact 通过一次性本地迁移工具校验、规范化、加密导入并标记为 `legacy` 只读。旧 Run 不能恢复执行或伪造新 checkpoint；无法映射字段保存在 legacy envelope，缺失引用明确标记。迁移可重复且以稳定 ID 去重，在确认新历史可读前不清理旧数据。

### 13.3 打包

- macOS：签名、notarization 和 DMG/应用包。
- Windows：代码签名的用户级安装包，优先 MSIX 或等价方案。
- Linux：签名 AppImage，并提供至少一种主流发行版安装包。

安装包包含 Desktop、agentd、官方 worker、sandbox backend、加密存储和更新/回滚元数据，不依赖用户预装 Rust、Node 或 Python。

## 14. 验证与发布门禁

### 14.1 单元与属性测试

覆盖 Run 状态机、event replay、checkpoint 兼容、capability 子集、路径规范化、diff/三方冲突、backup 去重与恢复、模型 schema、完成门禁和无进展检测；子 Agent 额外覆盖 spawn 幂等、权限投影、角色/工具门禁、Context Envelope 最小化与完整性、ContextRequest 补充、handoff 仲裁和重复投递去重。

### 14.2 故障注入

覆盖每个 Tool Call 阶段终止 daemon、文件写入中断、checkpoint 失败、数据库损坏、worker 无响应、Provider 超时/限流、磁盘耗尽、UI/daemon 版本不一致。

### 14.3 Sandbox Conformance

三平台统一测试路径逃逸、symlink/junction、子进程逃逸、环境凭据、raw socket、DNS 重绑定、本机/局域网、子 Agent 权限扩大、sibling context 泄漏、伪造或越权 handoff、直接 Child-to-Child 通信、IPC 冒充和 worker 脱离进程树。关键用例失败时该能力不得发布。

### 14.4 端到端与长时间测试

覆盖 UI 关闭后继续、daemon/系统恢复、多轮失败后换方案、多次上下文压缩、子 Agent spawn/暂停/取消/恢复、并发与写冲突、主 Agent 中介交接及拒绝/重派、修改/恢复/撤销恢复、外部编辑冲突、Verifier 驳回后继续、网络白名单与临时授权，以及多小时运行、反复 Provider 重连、UI 重启和 backup 清理。

### 14.5 发布条件

- 三平台核心功能和 sandbox conformance 通过。
- 崩溃恢复和 backup/restore 往返通过。
- 迁移可重复且不修改旧数据。
- 安装包签名和更新回滚有效。
- 无已知 Workspace 越界或未授权网络路径。
- 生成完整 SBOM 和第三方许可证清单。

## 15. 已确认的硬性产品决策

1. 产品是个人本地桌面 Agent，不是服务器 Agent。
2. 正式支持 macOS、Windows、Linux。
3. 使用 Agent 工作台 UI，支持本地文件和后台任务。
4. Rust daemon 是唯一可信控制 runtime；Python 等仅能受限使用。
5. 严格沙箱不可关闭，Workspace 授权始终可见。
6. 工具网络使用白名单 Broker；Shell 永远没有 raw network。
7. 首版仅官方打包签名工具，未来再开放 Skill/Plugin/MCP。
8. 数据纯本地加密保存，预留端到端加密同步契约。
9. 不绑定 Git/GitHub；Change Journal 提供差异、历史和恢复。
10. 文件修改前必须有备份，恢复本身也可撤销。
11. 受控子 Agent spawn 是核心产品能力；每次 spawn 创建可独立调度、取消和恢复的 Child Run，权限只能缩小，上下文最小化，所有跨子 Agent 交接必须经主 Agent 决策和 Runtime 仲裁，禁止直接 Child-to-Child 通信。
12. 使用风险分级授权和组合式完成门禁。
13. 只要有安全、可验证的进展路径，Agent 尽量运行到完成。
14. 新 runtime 一次性替换旧架构；旧历史只读导入。

## 16. 参考依据

- OpenAI Codex 开源仓库：<https://github.com/openai/codex>
- 本地 Claude Code 参考仓库：`/Users/yinpeihai/Code_workspace/claude-code`
- JetBrains Local History 默认保留五个工作日：<https://www.jetbrains.com/help/idea/local-history.html>
- VS Code Local History 默认每文件 50 条：<https://code.visualstudio.com/docs/editing/userinterface#_local-history>
- Google Drive 普通版本可能在 30 天或 100 个更新版本后清理：<https://support.google.com/drive/answer/2409045>
- OneDrive 版本历史通常保留 30 天：<https://support.microsoft.com/en-us/onedrive/restore-a-previous-version-of-a-file-stored-in-onedrive>
- Apple Time Machine 本地快照通常保留约 24 小时：<https://support.apple.com/en-euro/102154>

这些产品的留存策略仅作为默认值研究依据。本设计采用“活跃任务不失效、terminal 任务 30 天、每文件 100 版本、pinned 永久”的组合策略，并由本产品自己的安全和恢复契约负责。
