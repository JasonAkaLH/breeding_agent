# 主代理框架 PRD

- **项目**：multi_agent_framework
- **范围**：后端主代理框架
- **文档状态**：正式版
- **日期**：2026-04-22

## 1. 项目背景
本项目面向内部付费用户，目标是构建一个办公助手后端。当前阶段不优先做具体功能 Agent，而是先完成主代理框架设计，确保后续文档 RAG、NL2SQL、数据分析、农业生物信息分析等能力可以稳定接入。

本框架不依赖 LangChain、LangGraph、AutoGen 等现成 Agent 框架，采用 Python 为主、异步优先的服务端架构；性能热点未来可下沉到 C++，但不作为一期前提。

## 2. 一期目标与非目标
### 2.1 一期目标
一期需交付一个可支撑业务扩展的主代理内核，至少覆盖：
- 任务拆分
- Agent 注册与发现
- 资源调度与执行
- 上下文传递
- 会话状态
- 任务队列
- 观测日志
- 记忆系统
- 实时事件流
- 用户主动中断任务
- 首个可验收业务样例绑定为 **NL2SQL 只读查询链路**

### 2.2 一期不做但必须预留接口
- 人工审批
- 权限控制
- 通用工具调用平台
- 多实例生产化部署能力
- 完整长期记忆系统

## 3. 产品形态与部署策略
- 产品形态为**前后端分离**，本 PRD 仅覆盖后端。
- 对外形态为 **HTTP / API 服务**。
- 服务模型为**异步任务驱动的对话式 Agent 服务**。
- 用户提交消息后，后端立即返回 `task_id`，后台异步执行。
- 支持**实时流式事件回传**。
- 架构按**分布式可扩展**设计，但**首期支持单服务器部署**。
- 当前 LLM 与生信分析能力均通过外部 API 提供，本机主要负责异步编排、状态管理、记忆、事件流与审计。
- 业务查询数据源已确定为公司现有 MySQL 数据库。
- 主框架状态库的长期目标为 PostgreSQL，但当前远端 PostgreSQL 尚未部署完成。
- 在 PostgreSQL 可用前，本地测试阶段先使用 SQLite 作为状态库存根。
- 状态存储层必须从一开始保留数据库适配接口，确保后续可从 SQLite 平滑切换到 PostgreSQL。

## 3.1 状态存储策略（当前决定）
### 3.1.1 已确定事项
- **业务查询数据源**：公司现有 MySQL 数据库，仅供 NL2SQL 业务查询使用。
- **主框架状态库目标**：PostgreSQL。
- **本地测试状态库**：SQLite。

### 3.1.2 设计约束
- 不将公司业务 MySQL 直接作为主框架状态库。
- 主框架自己的会话、任务、节点、事件、产物索引等状态数据，应走独立状态存储抽象。
- 一期代码实现必须优先面向状态存储接口，而不是把 SQL 语句或 ORM 模型直接写死到某一数据库方言。
- SQLite 只用于本地开发与测试阶段验证，不作为长期生产方案。
- PostgreSQL 部署完成并提供连接信息后，再继续补充基于 PostgreSQL 的主框架状态库详细设计。

### 3.1.3 一期需预留的接口能力
状态存储层至少应预留以下抽象能力：
- conversation 存取
- message 存取
- task / task_node 状态迁移
- artifact 元数据存取
- event record 写入与查询
- capability / instance registry 持久化接口（即使一期可先做简化）

## 4. 核心术语
- **Capability**：主代理进行任务拆分、能力匹配与资源调度时使用的稳定能力契约。
- **Agent**：一个或多个 capability 的具体执行实体。
- **Tool**：Agent 执行过程中调用的底层操作接口。
- **Instance**：Agent 在运行时的具体实例，可本地也可远程。
- **Task**：一次用户消息触发的异步执行单元。
- **Node**：Task DAG 中的执行节点。
- **Artifact**：节点产出的中间结果或最终结果引用。
- **Event**：对前端和日志系统输出的状态变化或执行事件。

主代理优先面向 capability 编排，而不是直接面向 tool。

## 5. 逻辑架构
一期建议至少包含以下逻辑组件：
- **API 接入层**：接收消息、查询任务、取消任务、输出事件流。
- **会话服务**：维护 `conversation_id`、消息顺序、串行约束。
- **主代理编排器**：负责任务拆分、DAG 生成、节点策略装配。
- **Capability Registry**：维护稳定能力目录。
- **Instance Registry**：维护运行态实例、心跳、负载与健康状态。
- **调度器**：在资源约束下选择节点执行实例。
- **执行适配层**：统一封装本地执行器与外部 RESTful API。
- **状态存储**：持久化会话、任务、节点、事件、产物引用。
- **记忆服务**：维护用户账户与会话连续性记忆。
- **审计日志服务**：输出 JSONL 结构化日志。
- **事件流服务**：将任务状态和节点事件推送给前端。

## 6. 编排模型
### 6.1 任务结构
- 一期采用**混合型 DAG**：先生成主干 DAG，执行中允许受控动态扩展。
- 支持中间结果被多个下游节点复用。
- 设计参考成熟工作流 / DAG 编排逻辑，不自造完全脱离实践的模型。

### 6.2 任务拆分指导方针
- 主代理在**预定义任务拆分指导方针**下工作。
- 拆分依据以**结构化规则**为主，prompt / 推理为辅。
- 结构化规则先由人定义业务拆分原则，后续逐步沉淀为配置。
- 主代理不可完全依赖自由 prompt 推理决定任务图。

### 6.3 子代理执行范式
- 一期采用**混合模式**：优先专用任务型 Agent，必要时允许受限 ReAct Worker。
- 受限 ReAct Worker 只可在当前 capability、节点策略、资源约束允许的范围内选择 tool。
- 不开放全局大 tool pool，不允许无边界自由扩展执行路径。

## 7. 注册、发现与资源调度
### 7.1 注册与发现
采用混合模式：
- **Capability Registry**：静态定义系统“会什么”。
- **Instance Registry**：动态反映“现在谁在线、谁可用、谁负载高”。
- 调度时先按 capability 匹配，再按实例状态和资源状态选择执行者。

### 7.2 统一执行协议
子代理统一视为“可执行实例”，一期物理部署可单机，但逻辑上统一支持：
- 本地执行逻辑
- 外部 RESTful API 适配器
- 未来远程 worker / 独立服务

### 7.3 资源调度策略
资源调度采用**混合策略**：
1. 先用硬性规则过滤可执行 Agent / 节点 / 资源。
2. 再在可行集合内基于实时负载、资源占用、能力匹配等动态评分选择。

一期资源维度至少覆盖：
- 执行并发
- Agent 能力资源
- 外部资源（模型额度、数据库连接、检索资源、CPU/内存/GPU 预算）

### 7.3.1 背压策略
一期背压策略采用：**严格拒绝型（B）**。

核心原则：
- 优先保护已经进入运行态的任务，保证已接收任务的结果质量与执行完整性。
- 不因资源不足而降级任务质量，不允许通过裁剪关键节点、跳过必需步骤或弱化执行链路来“糊弄用户”。
- 在资源池达到阈值时，不采用长队列缓冲，而是直接拒绝新任务进入。

高压下的默认动作：
- 已运行任务继续执行
- 新任务直接拒绝
- 不构造长等待队列
- 将拒绝行为写入事件流与审计日志

### 7.3.2 配额策略
一期配额策略采用：**系统级 + capability 级**。

#### 系统级配额
用于限制整个系统的总资源占用，建议至少覆盖：
- 总活跃任务数
- 总运行节点数
- 总外部 LLM 并发数
- 总 MySQL 活跃查询数
- 总外部生信 API 并发数

#### capability 级配额
用于限制单类能力对系统资源的占用上限，避免某一类 capability 吃满系统资源。建议至少覆盖：
- `nl2sql.sql_generate`
- `nl2sql.sql_execute_readonly`
- `nl2sql.result_summarize`
- `bioinfo.external_job`

#### 触发拒绝规则
当任一关键资源池达到拒绝阈值时：
- 不再接受新的 task
- 不再调度新的高成本节点
- 返回明确的“当前繁忙，请稍后重试”结果
- 记录拒绝原因、触发资源池、触发时间与对应 capability

### 7.3.3 用户级配额预留兼容
一期暂不实现复杂的用户级配额，但必须在模型与策略接口上预留兼容：
- 不同用户等级 / 套餐可定义不同资源额度
- 不同用户等级可对应不同活跃任务上限
- 不同用户等级可定义不同 capability 可用范围或优先级权重
- 配额判定接口不应写死为“仅看系统级与 capability 级”，后续应可插入 `account_id` / `plan_id` 维度

这意味着一期虽然先不真正执行用户分层配额，但后续接入高付费用户更高额度时，不应推翻现有配额架构。

## 8. 协作共享、会话与记忆
### 8.1 协作共享模型
采用**分层共享**：
- 主代理/调度层持有全局任务图状态、共享状态摘要、资源视图。
- 子代理默认只拿当前子任务所需上下文、共享状态摘要、相关依赖产物。
- 不默认向所有 Agent 全量复制所有信息。

系统内部采用：
- **状态机主干**：任务、节点、依赖、产物、取消、恢复等状态的唯一真相源。
- **结构化 mailbox**：用于主代理与子代理、子代理与子代理之间的定向协作消息传递。
- **Interrupt/Resume**：用于向用户请求补充信息并在回答后恢复原节点执行。

mailbox 不采用自由文本消息形态，而采用带有明确字段、消息类型、生命周期状态与审计信息的结构化记录。

### 8.1.1 协作协议总体原则
- 任务、节点、依赖、产物和取消状态必须以状态存储中的显式状态机为唯一真相源。
- mailbox 记录协作行为，不直接充当任务真相状态。
- mailbox 消息可以触发状态变化，但不能替代状态机本身。
- 用户补充信息通过 interrupt/resume 协议处理，不允许子代理直接与用户形成无状态私聊链路。

### 8.1.2 统一 mailbox 信封
所有内部协作消息建议共享统一外层字段：
- `message_id`
- `conversation_id`
- `task_id`
- `node_id`
- `from_agent`
- `to_agent` 或 `to_role`
- `channel`
- `message_type`
- `payload`
- `status`
- `priority`
- `created_at`
- `resolved_at`

### 8.1.3 mailbox channel 划分
一期建议使用统一 mailbox 基础设施，但逻辑上区分三类 channel：

1. **`orchestrator_control`**
   - 用于主代理 ↔ 子代理通信
   - 主要承载任务指派、状态汇报、取消、恢复等控制面消息

2. **`peer_collaboration`**
   - 用于子代理 ↔ 子代理通信
   - 主要承载依赖请求、依赖结果、产物引用共享、局部上下文请求等协作面消息

3. **`interrupt_resume`**
   - 用于用户澄清与恢复流程
   - 主要承载 `clarification_request`、`clarification_answer`、`resume_notice` 等消息

### 8.1.4 主 Agent 与子 Agent 通信
主 Agent 不直接参与业务实现，因此其通信 payload 重点是编排与控制信息，而非业务细节本身。

建议主 Agent ↔ 子 Agent 常用消息类型：
- `node_assignment`
- `node_status_report`
- `node_result_report`
- `node_blocked_report`
- `cancel_notice`
- `resume_notice`

这类消息的 payload 重点应包括：
- capability / node 标识
- 输入引用
- deadline / timeout
- retry policy
- resource budget
- cancel / resume token
- checkpoint 引用

### 8.1.5 子 Agent 与子 Agent 通信
子 Agent 之间的通信重点在于依赖和产物协作，不直接承担全局编排职责。

建议子 Agent ↔ 子 Agent 常用消息类型：
- `dependency_request`
- `dependency_response`
- `artifact_reference_share`
- `peer_context_request`
- `peer_context_response`

这类消息的 payload 重点应包括：
- 所需 dependency / artifact 标识
- 可复用结果引用
- 局部上下文需求
- 依赖是否满足以及满足方式

子 Agent 之间允许协作，但所有协作消息必须经过统一 mailbox 记录并可被系统审计，不允许形成不可见的旁路通信。

### 8.1.6 Interrupt / Resume 协议
当子代理在执行过程中发现缺少完成任务所必需的信息时，应通过结构化 interrupt 机制上抛，而不是临时写自然语言消息等待人工判断。

建议 `interrupt` 对象至少包含：
- `interrupt_id`
- `task_id`
- `node_id`
- `source_agent`
- `question`
- `reason_code`
- `required_fields`
- `status`（`open / answered / expired / cancelled`）
- `answer_payload`
- `created_at`
- `answered_at`

恢复执行时，系统应通过 `resume_notice` + `checkpoint_ref` + 用户补充答案，使原节点回到 `ready_to_resume`，再进入 `resuming` 并继续后续处理。

### 8.1.7 节点状态建议补充
为支持协作与恢复，一期节点状态建议至少覆盖：
- `pending`
- `ready`
- `running`
- `waiting_for_dependency`
- `waiting_for_input`
- `ready_to_resume`
- `resuming`
- `completed`
- `failed`
- `cancelling`
- `cancelled`

### 8.1.8 mailbox 生命周期模型（分级 ACK）
一期采用 **C / 分级模型**：
- **强 ACK**：用于控制类、interrupt 类、恢复类关键消息
- **轻 ACK**：用于普通协作类消息

#### 强 ACK 消息建议
建议纳入强 ACK 的消息类型：
- `node_assignment`
- `cancel_notice`
- `resume_notice`
- `clarification_request`
- `clarification_answer`
- `node_blocked_report`

强 ACK 推荐状态流转：
- `pending`
- `delivered`
- `acknowledged`
- `resolved`
- `expired`
- `cancelled`

#### 轻 ACK 消息建议
建议纳入轻 ACK 的消息类型：
- `status_report`
- `dependency_request`
- `dependency_response`
- `artifact_reference_share`
- `peer_context_request`
- `peer_context_response`
- `node_result_report`

轻 ACK 推荐状态流转：
- `pending`
- `delivered`
- `resolved`
- `expired`

### 8.1.9 TTL 与过期策略建议
不同类型消息应配置不同 TTL，而不是统一默认值。

#### 强 ACK 消息 TTL 建议
- `node_assignment`：短 TTL，若未 ACK 应尽快重投或上抛
- `cancel_notice`：极短 TTL，优先级最高，超时后必须升级为取消失败审计
- `resume_notice`：短 TTL，超时后应阻止节点无限挂起
- `clarification_request`：中等 TTL，需配合前端展示与用户回复窗口
- `clarification_answer`：短 TTL，收到后应尽快恢复节点

#### 轻 ACK 消息 TTL 建议
- `status_report`：短 TTL，可过期丢弃，不要求完整重放
- `dependency_request` / `dependency_response`：中等 TTL，过期后允许由编排器重新发起
- `artifact_reference_share`：中等 TTL，若产物索引仍在，可补发引用
- `peer_context_request` / `peer_context_response`：短到中等 TTL，根据局部协作窗口决定

### 8.1.10 重试与补偿策略建议
- 强 ACK 消息未在 TTL 内进入 `acknowledged`，应触发有限重试
- 强 ACK 消息重试次数耗尽后，不应静默失败，必须产生结构化审计事件
- 轻 ACK 消息可允许较少重试，必要时由编排器重新生成协作请求
- `cancel_notice` 与 `resume_notice` 不应无限重试，避免系统进入重复风暴

### 8.1.11 ACK 规则建议
- 强 ACK 消息要求接收方显式确认已接收或已接受处理
- 轻 ACK 消息可通过进入 `resolved` 或产生对应结果事件视为隐式确认
- mailbox ACK 不等于业务完成；ACK 仅表示消息已被接收/接受处理
- 真正的业务完成仍以状态机和节点结果为准

### 8.1.12 过期后的处理建议
- `node_assignment` 过期：编排器应重新调度或标记分配失败
- `cancel_notice` 过期：应生成取消传播异常事件，不能默默忽略
- `clarification_request` 过期：可将 interrupt 标记为 `expired`，并结束或回退任务
- `dependency_request` 过期：由编排器决定重发、换实例或终止节点
- `status_report` 过期：通常只影响观测，不应反向污染任务真相状态

### 8.1.13 mailbox 与状态机的职责边界
- **状态机负责系统事实**：当前节点在哪个状态、是否可恢复、是否已取消、依赖是否满足。
- **mailbox 负责协作语义**：谁请求了什么、谁回复了什么、是否已经 resolved。
- **事件流负责前端与审计输出**：向前端展示阶段变化，并向日志系统输出结构化事件。

### 8.2 会话模型
- 同一 `conversation_id` 内任务**串行执行**。
- 前端在活跃任务未完成时不再发送新消息，后端也需做串行兜底校验。
- 记忆按**用户账户**组织，通过数据库关联 `account_id/user_id` 与 `conversation_id`。

### 8.3 会话记忆持久化
同一 `conversation_id` 下，一期至少持久化：
- 消息历史
- 任务状态（DAG、节点状态、执行结果摘要）
- 关键中间产物（可复用节点产物、关键分析结论、检索结果摘要）

账户体系采用混合模式：一期先接收上游系统传入的用户标识，后续预留内置认证/权限能力。

## 9. 节点策略、失败处理与取消
### 9.1 节点策略来源
节点关键性、依赖类型、失败策略、重试策略、timeout 策略采用：
- **规则模板自动推导 + 人工可覆盖**

执行 Agent 不临场发明策略，只按节点元数据执行。

### 9.2 节点策略建议字段
每个节点建议至少具备以下策略元数据：
- `criticality`: `required | optional | fallback`
- `dependency_type`: `hard | soft`
- `on_failure`: `fail_task | retry | skip | fallback`
- `retry_policy`: 次数、间隔、适用错误类型
- `timeout_policy`: queue / start / execution / heartbeat / deadline
- `resource_class`: 对资源级别的要求

### 9.3 取消机制
一期采用**硬停止语义**，但其正式定义不是“杀线程”，而是：

> **Task Context Termination（任务上下文终止）**

即：当用户发起“停止处理 / 停止思考”后，系统应立即终止当前 task context 在主框架中的有效性，阻止其继续占用主流程资源、继续调度新节点或继续回写迟到结果。

### 9.3.1 为什么不直接定义为“杀线程”
- 当前系统中的执行单元不只包括本地执行上下文，还包括外部 MySQL 查询和外部 LLM 调用。
- MySQL 查询与外部 LLM 当前都**不保证**存在可靠的主动取消接口。
- 因此，“停止处理”的核心语义不应写成“强制杀死某个线程 / 进程”，而应写成“终止这次任务在系统中的执行上下文”。
- 这样可以保证 PRD 不被某种具体线程模型绑定，同时便于状态机、mailbox、interrupt/resume、日志审计保持一致。

### 9.3.2 Task Context Termination 的系统动作
收到停止请求后，系统应立即执行以下动作：

1. **冻结 DAG 调度**
   - 不再为该 task 启动任何新节点
   - 不再接受该 task 的新扩展节点加入执行队列

2. **终止本地执行上下文**
   - 将该 task 从调度器活跃上下文中移除
   - 回收本地调度槽位、资源预算、队列占位、mailbox 活跃资格

3. **关闭协作链路**
   - 相关 `cancel_notice` 写入 mailbox
   - 未完成的协作消息标记为 `cancelled` 或 `expired`
   - 未完成的 interrupt 进入 `cancelled` 或 `expired`
   - 相关 checkpoint 标记失效，不允许再恢复到本次 task

4. **禁止结果回写**
   - 即使外部调用稍后返回结果，也不得再写回主任务结果
   - 迟到结果一律视为 `late_result` / `orphaned_result`

### 9.3.3 状态机收敛规则
#### Task 状态
建议状态收敛路径：
- `running -> cancelling -> cancelled`
- 若存在无法完全收敛的外部迟到调用，可收敛为：
  - `running -> cancelling -> cancellation_partial`

#### Node 状态
建议节点在停止时按以下规则收敛：
- `pending / ready` -> `blocked_by_cancellation`
- `running` -> `cancelling -> cancelled` 或 `orphaned`
- `waiting_for_dependency / waiting_for_input / ready_to_resume / resuming` -> `cancelled`
- 已 `completed` 节点保持不变，但其产物不再参与后续恢复或继续执行

### 9.3.4 数据库查询取消能力规范
对于 MySQL 查询，一期采用：**B / 物理取消可选增强**。

具体规范：
- 一期默认按**语义终止**处理，而不是把“硬停止”建立在数据库物理取消能力上。
- 当 task context 被终止后，系统不再等待该查询返回，也不再接受其结果进入主任务结果链。
- 若数据库驱动、连接或执行器未来支持更强的 query cancel / connection kill / worker abort，可作为后续增强能力接入。
- 物理取消能力不是当前主框架成立的前提条件。
- 若查询结果在 task 已终止后返回，该结果一律视为迟到结果，不得进入主任务结果，只能保留审计记录。

### 9.3.5 MySQL 只读账号前提
当前 NL2SQL 链路关于数据库账号的正式约束为：
- 当前仅配置一个 MySQL 账号：`chatu:chatu123`
- 该账号已确认具备只读权限
- 一期不设计多账号切换、权限编排或按 capability 分配数据库账号的机制
- SQL Guard 继续保留，作为数据库只读权限之外的第二层防线

### 9.3.6 外部 LLM 迟到结果处理规范
对于外部 LLM 调用，一期采用：**B / 丢弃 + 审计记录**。

具体规范：
- 若供应商无 cancel 接口，则不等待物理取消完成
- 系统应在本地立即终止该 task context
- 外部 LLM 的迟到响应一律视为 `late_result`
- 迟到结果不得回写主任务结果、不得进入后续会话执行上下文、不得参与 artifact 复用
- 迟到结果应记录结构化审计事件，例如 `llm_result_discarded_after_termination`

### 9.3.7 迟到结果可见性边界
外部 LLM 与 MySQL 的迟到结果，一期采用：**A / 仅审计可见**。

这意味着：
- 迟到结果事件写入状态记录与 JSONL 审计日志
- 前端不展示 `late_result` / `orphaned_result` 这类内部技术事件
- 对用户而言，只看到“任务已停止”或“任务已结束”，不暴露额外内部实现细节
- 该策略旨在保持前端体验干净，同时保留足够的排障证据

### 9.3.8 资源回收要求
Task Context Termination 发生后，应至少回收或关闭：
- 调度并发槽位
- capability 资源占位
- 任务队列资格
- mailbox 活跃投递资格
- interrupt / resume 恢复资格
- 本地临时上下文与检查点引用（逻辑失效即可）

### 9.3.9 审计与事件要求
停止处理必须产生结构化审计记录；其中部分事件可用于前端，部分仅用于审计。建议至少包括：
- `task.cancellation_requested`
- `task.context_termination_started`
- `node.cancellation_propagated`
- `task.context_terminated`
- `task.cancellation_partial`
- `task.late_result_discarded`
- `task.orphaned_result_detected`

日志中至少记录：
- 谁发起停止
- 停止发起时间
- 停止时有哪些运行中节点
- 哪些节点收敛为 `cancelled`
- 哪些节点收敛为 `orphaned`
- 哪些迟到结果被丢弃
- 哪些资源被回收

### 9.3.10 结果边界
被取消任务视为**彻底作废**：
- 后续同一会话不复用该任务的中间产物
- 不允许从该 task 的 checkpoint 继续恢复
- 其产物只保留审计与排障价值，不再进入执行上下文

## 10. 实时事件流与观测日志
### 10.1 实时事件流粒度
对前端开放**执行可见粒度**事件，至少包括：
- 用户消息与助手输出
- 任务整体状态变化
- DAG 节点状态变化
- 子代理开始 / 结束
- 阶段性进度信息

### 10.2 一期事件流传输建议
一期建议优先使用 **SSE**：
- 当前主要是后端向前端单向推送执行状态
- SSE 更适合浏览器接入、实现简单、维护成本低
- 后续若存在双向协商型实时控制需求，再评估 WebSocket

### 10.3 日志与审计
一期观测能力包括：
- 运行日志
- 任务链路追踪
- 结构化事件审计

日志落地为**JSONL 结构化日志文件**，至少支持按以下字段检索：
- `conversation_id`
- `task_id`
- `agent_id`
- `node_id`

需记录关键决策、任务拆分结果、Agent 选择原因、状态迁移、错误异常、取消过程与资源释放结果。

## 10.4 当前已知数据库接入参考
仓库当前已有 `src/mysql_engine.py`，其中提供了基于 SQLAlchemy `create_engine` + `QueuePool` 的 MySQL 访问示例，可作为一期 NL2SQL 数据源接入参考。

基于当前仓库事实，需要强调：
- 该示例可作为数据库连通性与连接池参数参考。
- 主代理框架核心业务逻辑仍必须保持 async 边界。
- 正式执行链路中，数据库访问不得直接阻塞事件循环；应通过明确的异步执行边界进行封装。
- 数据库连接信息在正式实现中应通过配置或密钥管理注入，不应以硬编码形式作为长期方案。
- 一期 NL2SQL MVP 默认只允许只读查询，不允许写入、DDL 或高风险 SQL。
- 当前 `src/mysql_engine.py` 只提供数据库连接示例，本身并不构成“只读保证”；正式实现必须引入独立的只读执行约束。
- 正式执行时应优先使用数据库层面的只读账号 / 最小权限账号，而不是仅依赖 SQL 文本校验。
- 当前 NL2SQL 执行链路中仅配置一个 MySQL 账号：`chatu:chatu123`。
- 该账号已确认是只读账号，因此一期不设计多账号切换与权限编排机制。
- 一期仍保留 SQL Guard，作为数据库只读权限之外的第二层保护。

## 10.5 NL2SQL MVP 任务 DAG 细化草案
### 10.5.1 首阶段建议 Capability 拆分
一期 NL2SQL MVP 建议至少拆成以下 capability：
- `nl2sql.intent_route`：识别用户是否为数据库查询意图，并提取查询目标
- `nl2sql.schema_context_prepare`：获取可用 schema、表、字段、必要约束信息
- `nl2sql.sql_generate`：基于自然语言与 schema 生成 SQL 草案
- `nl2sql.sql_guard`：校验 SQL 是否只读、是否单语句、是否命中禁用模式
- `nl2sql.sql_execute_readonly`：执行只读 SQL 并返回结构化结果
- `nl2sql.result_summarize`：将 SQL 结果整理为用户可读回复

其中：
- `nl2sql.sql_guard`、`nl2sql.sql_execute_readonly` 建议为**专用任务型 Agent / 执行器**
- `nl2sql.sql_generate` 可由外部 LLM 支撑
- `nl2sql.result_summarize` 可由外部 LLM 或轻量 summarizer 执行

### 10.5.2 首阶段标准 DAG
建议一期标准链路默认生成如下主干 DAG：

1. **节点 A：意图识别与任务路由**
   - capability：`nl2sql.intent_route`
   - 作用：判断是否进入 NL2SQL 链路，抽取查询主题、时间范围、维度、指标
   - criticality：`required`

2. **节点 B：Schema 上下文准备**
   - capability：`nl2sql.schema_context_prepare`
   - 作用：收集本次任务允许访问的库、表、字段、示例映射、字段说明
   - criticality：`required`
   - 依赖：A

3. **节点 C：SQL 生成**
   - capability：`nl2sql.sql_generate`
   - 作用：生成候选 SQL 与生成说明
   - criticality：`required`
   - 依赖：A、B

4. **节点 D：SQL 安全校验**
   - capability：`nl2sql.sql_guard`
   - 作用：检查只读约束、语法风险、是否多语句、是否越权访问
   - criticality：`required`
   - 依赖：C

5. **节点 E：SQL 执行**
   - capability：`nl2sql.sql_execute_readonly`
   - 作用：执行通过校验的 SQL，返回结果集、行数、执行摘要
   - criticality：`required`
   - 依赖：D

6. **节点 F：结果汇总**
   - capability：`nl2sql.result_summarize`
   - 作用：将查询结果转换为用户可读答复
   - criticality：`required`
   - 依赖：E

### 10.5.3 动态扩展节点
在混合型 DAG 模型下，一期允许有限动态扩展，但必须受控。NL2SQL 场景建议只允许以下类型的扩展：
- **B1：Schema 补充节点**：当表/字段信息不足时，补充额外 schema 上下文
- **C1：SQL 重新生成节点**：当 guard 发现 SQL 不安全但可修复时，触发一次受限重生成
- **F1：结果二次解释节点**：当结果集结构复杂时，允许补充一个轻量解释节点

不允许无限循环重生成；一期建议 `sql_generate -> sql_guard` 最多只允许 1 次修复型回路。

### 10.5.4 节点策略建议
| 节点 | criticality | retry | timeout 建议 | 说明 |
|---|---|---|---|---|
| A 意图识别 | required | 0-1 次 | 10s | 失败则任务终止 |
| B Schema 准备 | required | 1 次 | 15s | 可对数据库元信息查询做有限重试 |
| C SQL 生成 | required | 1 次 | 30s | 外部 LLM 超时可重试一次 |
| D SQL Guard | required | 0 次 | 5s | 校验失败不直接重试执行，优先进入一次修复回路 |
| E SQL 执行 | required | 0-1 次 | 60s | 只对可判定的瞬时数据库错误允许有限重试 |
| F 结果汇总 | required | 1 次 | 20s | 汇总失败可回退为原始表格摘要 |

### 10.5.5 SQL 安全边界（首阶段建议）
一期 NL2SQL MVP 建议采用严格白名单策略：
- 仅允许只读查询
- 默认允许：`SELECT`、只读 `WITH ... SELECT`
- 默认拒绝：
  - `INSERT`
  - `UPDATE`
  - `DELETE`
  - `REPLACE`
  - `CREATE / ALTER / DROP / TRUNCATE`
  - 多语句执行
  - 文件导入导出相关语句
  - 显式锁表语句
  - 高风险系统表访问
- 如无法确认安全性，默认拒绝执行并返回审计事件
- 一期不提供人工覆盖、白名单豁免或管理员绕过执行路径

### 10.5.5.1 严格只读防线（必须项）
NL2SQL Capability 在一期必须被定义为**严格只读能力**，不能通过任何执行路径写入数据库。建议采用至少四层防线：

1. **Capability 合约层**
   - `nl2sql` 相关 capability 明确定义为 `read_only_query_only`
   - 不存在“查询失败后自动切换为写入型 SQL”的回退路径

2. **SQL Guard 层**
   - 所有 SQL 在进入执行节点前必须经过 `nl2sql.sql_guard`
   - 只允许 `SELECT` 与只读 `WITH ... SELECT`
   - 任何无法确定是否安全的 SQL，一律拒绝执行
   - 多语句、DDL、DML、锁表、导入导出语句一律拦截

3. **执行适配层**
   - `nl2sql.sql_execute_readonly` 只能接收 guard 已通过的 SQL
   - 执行器本身不得提供绕过 guard 的直接执行入口
   - 若执行前发现 SQL 未携带 guard 通过标记，必须拒绝执行

4. **数据库权限层**
   - 正式运行必须优先使用只读数据库账号或等价最小权限账号
   - 即使上层 guard 失效，也应由数据库权限阻止写入成功

### 10.5.5.2 拒绝策略
对于任何疑似写入、结构变更或高风险操作，一期默认执行以下策略：
- 不执行 SQL
- 输出 `nl2sql.sql_guard_blocked` 事件
- 写入 JSONL 审计日志
- 返回明确的能力边界说明：当前 NL2SQL Capability 仅支持只读查询

### 10.5.6 关键产物建议
NL2SQL 链路建议至少沉淀以下 artifact：
- `intent_summary`：本次查询意图摘要
- `schema_context_snapshot`：本次生成 SQL 使用的 schema 摘要
- `generated_sql`：生成后的 SQL 文本
- `guard_report`：SQL 校验结果
- `query_result_preview`：结果预览
- `result_summary`：面向用户的最终汇总文本

### 10.5.7 NL2SQL 专项事件建议
除通用事件外，一期建议补充以下专项事件：
- `nl2sql.intent_detected`
- `nl2sql.schema_prepared`
- `nl2sql.sql_generated`
- `nl2sql.sql_guard_passed`
- `nl2sql.sql_guard_blocked`
- `nl2sql.write_blocked`
- `nl2sql.query_executed`
- `nl2sql.result_summarized`

### 10.5.8 NL2SQL 失败边界建议
一期建议按以下边界处理失败：
- **Schema 不足**：允许一次补充上下文后重试 SQL 生成
- **SQL 不安全**：不执行，优先进入一次修复型重生成；仍失败则终止任务，并记录阻断审计事件
- **疑似写入 / DDL / 高风险语句**：直接阻断，不进入执行阶段，不允许自动放行
- **数据库瞬时连接错误**：允许有限重试
- **SQL 执行语义错误**：记录审计并终止当前任务
- **结果汇总失败**：允许降级输出结构化原始结果摘要

## 10.6 Schema Context Builder 规则草案
### 10.6.1 目标
Schema Context Builder 负责在 `intent_route` 完成后，根据路由结果、schema profile、用户问题和任务约束，生成一份**最小必要 schema 上下文**提供给 `nl2sql.sql_generate` 节点，而不是把整库 schema 原样发送给 LLM。

### 10.6.2 输入
Builder 一期建议接收以下输入：
- `route_id`
- `schema_profile_id`
- 用户原始问题
- 意图识别结果（作物类型、品种名、QTN、基因名、时间范围、指标、维度）
- `routing_rules.yaml`
- `schema_metadata.yaml`
- 当前 SQL policy profile

### 10.6.3 输出
Builder 输出建议至少包含：
- `selected_tables`
- `selected_columns`
- `join_hints`
- `route_description`
- `business_constraints`
- `sql_constraints`
- `context_summary`

该输出会作为 `schema_context_snapshot` artifact 持久化，并作为 `nl2sql.sql_generate` 的核心输入之一。

### 10.6.4 一级裁剪：按业务路线裁剪
第一层必须按 `route_id` 裁剪：
- `approval_variety_db` 只允许使用对应 profile 下的审定品种库表
- `genotype_db` 只允许使用基因型数据库相关表
- 若路由未确定，则不生成 SQL，上游先走澄清流程

### 10.6.5 二级裁剪：按子域特征裁剪
在路线内部继续做更细裁剪：
- **审定品种库路线**：优先根据作物识别结果，仅选择该作物对应单表；作物未识别时，不直接放开全部五张表，优先触发澄清
- **基因型数据库路线**：
  - 查品种基础信息时优先 `variety`
  - 查位点 / 基因信息时优先 `qtn`
  - 查品种-位点基因型时组合 `variety_genotype + variety + qtn`
  - 查籼粳成分比例时优先 `rice_comp + variety`

### 10.6.6 三级裁剪：按字段暴露规则裁剪
- 仅选择 `expose_to_llm: true` 的字段进入上下文
- 主键、自增 ID 等对生成 SQL 无帮助的字段可不进入默认上下文
- 大文本字段仅在用户问题与其直接相关时才加入上下文
- 同一张表进入上下文的字段数应受 `max_columns_per_table` 限制

### 10.6.7 Join Hint 规则
- 仅在多表查询确有必要时注入 join hints
- Join hint 必须来自 `schema_metadata.yaml` 的白名单，不允许 LLM自由猜测隐藏关联
- 一期默认优先提供显式 join 对：
  - `variety_genotype.variety_id -> variety.variety_id`
  - `variety_genotype.qtn_id -> qtn.qtn_id`
  - `rice_comp.variety_id -> variety.variety_id`
  - `rice_varieties.ref_var_id -> variety.variety_id`

### 10.6.8 业务约束注入
Builder 输出中应同时注入业务约束，而不只给表字段：
- 当前只支持两条业务路线：审定品种库、基因型数据库
- 审定品种库当前只支持五种作物
- 超出当前支持范围的问题应触发澄清或拒答，而不是强行生成 SQL
- NL2SQL Capability 只支持只读查询

### 10.6.9 SQL 约束注入
Builder 在发给 LLM 的上下文中需要明确附带：
- 仅允许只读 SQL
- 仅允许单语句
- 非聚合查询默认需要 LIMIT
- 不允许访问路由白名单外的表
- 不允许访问系统 schema

### 10.6.10 失败策略
- 路由不明确：返回 `context_build_blocked`，要求上游澄清
- 找不到匹配表：返回 `context_build_blocked`，不进入 SQL 生成
- 可用字段不足：允许一次 schema 补充节点扩展
- join 关系不明确：不猜测 join，优先降级为单表查询或触发澄清

### 10.6.11 建议事件
一期建议补充以下事件：
- `nl2sql.context_build_started`
- `nl2sql.context_build_completed`
- `nl2sql.context_build_blocked`
- `nl2sql.context_schema_trimmed`

### 10.6.12 路线级示例
#### 审定品种库路线示例
用户问题：`查询近五年水稻审定品种有哪些`

建议上下文：
- route：`approval_variety_db`
- selected_tables：`rice_varieties`
- selected_columns：`year`, `crop_name`, `variety_name`, `approval_num`, `applicant`, `breeder`
- business_constraints：当前为审定品种库、水稻子域

#### 基因型数据库路线示例
用户问题：`查询品种XX在QTN12位点上的基因型`

建议上下文：
- route：`genotype_db`
- selected_tables：`variety`, `variety_genotype`, `qtn`
- selected_columns：
  - `variety.variety_name`
  - `variety.variety_id`
  - `variety_genotype.variety_id`
  - `variety_genotype.qtn_id`
  - `variety_genotype.genotype`
  - `qtn.qtn_id`
  - `qtn.qtn_seq`
- join_hints：`variety_genotype.variety_id = variety.variety_id`, `variety_genotype.qtn_id = qtn.qtn_id`

### 10.6.13 评分维护策略
Schema Context Builder 的评分体系不应演化成一张难以维护的大型“人工评分表”。一期建议采用：

> **固定评分公式 + 少量可维护配置项**

即：
- 评分逻辑主体固化在代码中
- 人工主要维护少量高价值配置，而不是维护成百上千条问题到表/字段的打分映射

### 10.6.14 不建议采用的大评分表模式
不建议维护如下模式：
- 每类问题模式单独配置表分数
- 每个问法维护一组表级固定分值
- 每次业务变化都需要手工调整大量历史评分配置

原因：
- 用户问法天然多样，问题模式无法稳定穷举
- 维护成本极高
- 一旦 schema 或业务范围变化，规则容易失控

### 10.6.15 推荐维护内容
一期建议真正维护的是以下几类轻量配置：

1. **路由词典**
   - 由 `routing_rules.yaml` 维护
   - 决定问题更接近哪条业务路线

2. **字段别名字典**
   - 维护用户自然语言与字段语义之间的映射
   - 例如“育种者”映射到 `breeder`，“适种区域”映射到 `suitable_area`

3. **表级偏置规则**
   - 仅在少量明确业务场景下提升某些表的优先级
   - 例如在审定品种库路线下命中“玉米”时提升 `corn_varieties`
   - 在基因型数据库路线下命中“QTN”时提升 `qtn` 与 `variety_genotype`

4. **少量可调权重**
   - 例如字段命中权重、表描述命中权重、join 可达性权重
   - 这类权重应保持少量、稳定，不宜频繁人工调参

### 10.6.16 推荐评分来源
Builder 一期建议将评分来源限制在以下几类：
- route 命中
- crop / 子域命中
- 表名命中
- 表描述命中
- 字段名命中
- 字段描述命中
- 字段别名命中
- join hint 可达性
- 表级业务偏置

这意味着评分体系本质上是：
- **结构性分**：来自 schema profile、白名单、`expose_to_llm` 等静态元数据
- **语义提示分**：来自业务词典与字段别名
- **业务偏置分**：来自少量手工维护的偏置规则

### 10.6.17 维护原则
- 优先补充词典、别名和偏置规则，不优先去调复杂权重
- 优先通过真实问例和错误案例来修正规则，而不是预先枚举所有问题模式
- 对高频错例进行增量修正，避免一次性大规模改动评分体系
- 评分规则改动应保留变更理由，必要时补充测试样例或样例集

### 10.6.18 人工维护与协作方式
长期维护中，业务知识主要由产品 / 业务侧提供，系统规则整理和更新可由开发协作完成：
- 业务侧负责提供：字段语义、常见问法、路由错例、结果期望
- 开发侧负责更新：路由词典、字段别名、表偏置规则、Builder 评分实现

也就是说，Builder 评分维护不应依赖“维护一张巨大分数表”，而应依赖：
- 业务知识沉淀
- 真实问例迭代
- 少量规则与偏置更新

### 10.6.19 后续可演进方向
若后续业务规模扩大，可逐步演进到：
- 独立字段别名字典配置文件
- 独立表级偏置规则配置文件
- 基于真实问例的评估样例集
- 更细粒度的评分观测与调优工具

但一期不建议将这些全部前置实现。

### 10.6.20 缓存策略
Schema Context Builder 的缓存策略一期确定为：**B / 只缓存配置加载结果**。

具体含义：
- `routing_rules.yaml`、`schema_metadata.yaml`、`sql_guard_rules.yaml` 的加载结果可在进程内缓存
- 每次请求仍重新执行 schema context 裁剪逻辑
- 一期不依赖最终 `schema context` 结果缓存作为正确性前提
- 后续可在接口层预留结果缓存能力，但不作为一期必需项

### 10.6.21 一期缓存边界
一期建议缓存的对象仅包括：
- 路由规则配置
- schema 元数据配置
- SQL guard 规则配置

一期不建议默认缓存的对象包括：
- 最终 `selected_tables`
- 最终 `selected_columns`
- 最终 `context_summary`
- 针对具体用户问题生成的完整 prompt 输入

### 10.6.22 缓存失效策略
一期建议使用简单、可解释的缓存失效策略：
- 进程启动时加载配置
- 当配置文件版本号变化或文件内容变化时重新加载
- 不依赖复杂的分布式缓存失效广播
- 本地开发阶段可通过重启服务或显式 reload 触发刷新

### 10.6.23 选择该缓存策略的原因
采用“只缓存配置加载结果”而不缓存最终裁剪结果，主要是为了：
- 保持实现简单
- 避免 schema context 结果缓存引入额外失效逻辑
- 保证每次请求都在当前 route / hints / 问题语义下重新裁剪，减少误命中风险
- 在一期阶段优先保证正确性与可解释性，而不是过早追求极致性能

### 10.6.24 字段别名字典落地形式
字段别名字典的落地形式当前确定为：**A / 直接纳入 `schema_metadata.yaml`**。

即：
- 字段别名不额外拆分独立文件
- 由 `schema_metadata.yaml` 统一承载 schema 元数据与字段语义别名
- Builder 在加载 schema metadata 时即可同时获得字段描述、字段暴露规则与字段别名信息

### 10.6.25 字段别名字典建议结构
在 `schema_metadata.yaml` 中，建议为字段增加诸如以下结构：

```yaml
columns:
  breeder:
    sql_type: varchar(200)
    description: 育种者
    expose_to_llm: true
    aliases:
      - 育种者
      - 选育者
      - 选育单位
```

若某些别名更适合表级表达，也可在表级补充轻量别名字段，但一期以字段级别名为主。

### 10.6.26 选择该落地方式的原因
采用字段别名字典内嵌于 `schema_metadata.yaml`，主要考虑：
- 一期配置文件数量尽量少
- 字段语义与字段元数据放在同一位置，维护时更直观
- 当前项目 schema 规模仍在可控范围内，尚未大到必须拆分多份配置

### 10.6.27 后续演进边界
虽然一期将字段别名字典直接放入 `schema_metadata.yaml`，但后续若出现以下情况，可再拆分独立文件：
- 字段别名字典增长过快
- 多人频繁协作维护，导致 schema metadata 变得过大
- 需要对别名字典进行独立版本管理或审核流程

也就是说，一期采用 A，并不阻断后续演进到独立 `field_aliases.yaml` 的可能性。

## 11. API 设计
### 11.1 会话消息入口
#### `POST /api/v1/conversations/{conversation_id}/messages`
用于提交用户消息并触发新的异步任务。

请求体建议：
```json
{
  "account_id": "acc_123",
  "content": "帮我统计最近30天按地区分组的订单数",
  "routing_mode": "auto",
  "capability_id": null,
  "client_message_id": "client_msg_001",
  "metadata": {}
}
```

返回体建议：
```json
{
  "conversation_id": "conv_123",
  "message_id": "msg_123",
  "task_id": "task_123",
  "status": "accepted"
}
```

串行约束：若同一会话已有活跃任务，返回冲突错误（如 `409 Conflict`）。

### 11.2 查询任务
#### `GET /api/v1/tasks/{task_id}`
返回任务当前状态、根节点、执行摘要、取消状态、时间戳等。

建议响应字段：
- `task_id`
- `conversation_id`
- `status`
- `root_node_id`
- `active_node_count`
- `completed_node_count`
- `failed_node_count`
- `cancel_requested`
- `created_at`
- `updated_at`

### 11.3 任务事件流
#### `GET /api/v1/tasks/{task_id}/events`
用于前端订阅该任务的实时事件流，建议采用 SSE。

建议事件类型：
- `task.accepted`
- `task.graph_created`
- `task.status_changed`
- `node.started`
- `node.completed`
- `node.failed`
- `node.cancelled`
- `agent.started`
- `agent.finished`
- `message.delta`
- `task.completed`
- `task.cancelled`
- `task.cancellation_partial`

### 11.4 取消任务
#### `POST /api/v1/tasks/{task_id}/cancel`
用于用户主动停止当前任务。

建议返回：
```json
{
  "task_id": "task_123",
  "status": "cancelling",
  "accepted": true
}
```

### 11.5 查询任务图
#### `GET /api/v1/tasks/{task_id}/graph`
用于查看任务 DAG、节点状态、依赖关系、关键性标记。

### 11.6 查询任务产物
#### `GET /api/v1/tasks/{task_id}/artifacts`
用于查看已完成节点的关键产物摘要和可下载/可引用信息。

### 11.7 查询能力目录
#### `GET /api/v1/capabilities`
用于前端或后台查看系统当前支持的 capability 列表、版本与状态。

## 12. 核心数据模型
### 12.1 Conversation
| 字段 | 说明 |
|---|---|
| `conversation_id` | 会话标识 |
| `account_id` | 用户账户标识 |
| `status` | `active / archived / locked` |
| `current_task_id` | 当前活跃任务 |
| `title` | 会话标题或摘要 |
| `created_at` / `updated_at` | 时间戳 |

### 12.2 Message
| 字段 | 说明 |
|---|---|
| `message_id` | 消息标识 |
| `conversation_id` | 所属会话 |
| `role` | `user / assistant / system` |
| `content` | 消息正文 |
| `task_id` | 关联任务 |
| `stream_status` | 流式输出状态 |
| `created_at` | 时间戳 |

### 12.3 Task
| 字段 | 说明 |
|---|---|
| `task_id` | 任务标识 |
| `conversation_id` | 所属会话 |
| `root_message_id` | 触发消息 |
| `status` | `accepted / planning / running / cancelling / cancelled / completed / failed` |
| `routing_mode` | `auto / hint / force_capability` |
| `requested_capability_id` | 用户强制或提示的能力 |
| `root_node_id` | 根节点 |
| `summary` | 任务摘要 |
| `cancel_requested_at` | 取消请求时间 |
| `created_at` / `updated_at` | 时间戳 |

### 12.4 TaskNode
| 字段 | 说明 |
|---|---|
| `node_id` | 节点标识 |
| `task_id` | 所属任务 |
| `capability_id` | 节点目标能力 |
| `assigned_instance_id` | 被调度实例 |
| `status` | `pending / ready / running / completed / failed / cancelled / blocked_by_cancellation / orphaned` |
| `criticality` | `required / optional / fallback` |
| `dependency_type` | `hard / soft` |
| `retry_policy` | 重试策略 |
| `timeout_policy` | 超时策略 |
| `resource_class` | 资源类型 |
| `input_refs` | 输入引用 |
| `output_refs` | 输出引用 |
| `started_at` / `finished_at` | 时间戳 |

### 12.5 TaskEdge
| 字段 | 说明 |
|---|---|
| `from_node_id` | 上游节点 |
| `to_node_id` | 下游节点 |
| `edge_type` | `data / control / fallback` |
| `condition` | 可选触发条件 |

### 12.6 Artifact
| 字段 | 说明 |
|---|---|
| `artifact_id` | 产物标识 |
| `task_id` | 所属任务 |
| `producer_node_id` | 生产节点 |
| `artifact_type` | `text / json / file / dataset / summary` |
| `storage_ref` | 存储引用 |
| `summary` | 摘要 |
| `is_complete` | 是否完整 |
| `created_at` | 时间戳 |

### 12.7 CapabilityDefinition
| 字段 | 说明 |
|---|---|
| `capability_id` | 能力标识 |
| `name` | 能力名称 |
| `description` | 说明 |
| `input_contract` | 输入契约 |
| `output_contract` | 输出契约 |
| `tool_profile_id` | 允许的工具范围 |
| `policy_template_id` | 节点策略模板 |
| `version` | 版本 |
| `status` | `active / deprecated / disabled` |

### 12.8 AgentInstance
| 字段 | 说明 |
|---|---|
| `instance_id` | 实例标识 |
| `agent_type` | 实例类型 |
| `supported_capabilities` | 支持的能力列表 |
| `endpoint` | 本地或远程执行地址 |
| `state` | `online / busy / draining / offline` |
| `load_score` | 当前负载分值 |
| `resource_snapshot` | 资源快照 |
| `last_heartbeat_at` | 最近心跳时间 |

### 12.9 EventRecord
| 字段 | 说明 |
|---|---|
| `event_id` | 事件标识 |
| `conversation_id` | 会话标识 |
| `task_id` | 任务标识 |
| `node_id` | 节点标识（可选） |
| `agent_id` | Agent 标识（可选） |
| `event_type` | 事件类型 |
| `payload` | 结构化内容 |
| `visibility` | `frontend / internal / audit_only` |
| `created_at` | 时间戳 |

### 12.10 MailboxMessage
建议将 mailbox 逻辑拆成“消息主表 + 投递状态表”，避免把消息语义和单个接收方状态混在一起。

| 字段 | 说明 |
|---|---|
| `message_id` | mailbox 消息主键 |
| `conversation_id` | 所属会话 |
| `task_id` | 所属任务 |
| `node_id` | 所属节点（可选） |
| `parent_message_id` | 父消息，用于 request/response 串联 |
| `correlation_id` | 协作链路关联 ID |
| `from_agent` | 发送方 agent |
| `to_agent` | 接收方 agent（可选） |
| `to_role` | 接收方角色（可选） |
| `channel` | `orchestrator_control / peer_collaboration / interrupt_resume` |
| `message_type` | 结构化消息类型 |
| `ack_policy` | `strong / light` |
| `priority` | 优先级 |
| `payload` | 结构化 JSON payload |
| `payload_schema_version` | payload 版本号 |
| `created_at` | 创建时间 |
| `resolved_at` | 消息整体 resolved 时间 |

建议索引：
- `(task_id, created_at)`
- `(node_id, created_at)`
- `(channel, message_type, created_at)`
- `(correlation_id)`

### 12.11 MailboxDelivery
同一条 mailbox message 可能面向单个 agent，也可能面向某个 role 扇出到多个实例，因此建议单独建投递状态表。

| 字段 | 说明 |
|---|---|
| `delivery_id` | 投递记录主键 |
| `message_id` | 关联 `MailboxMessage` |
| `recipient_agent` | 实际接收实例 |
| `recipient_role` | 接收角色快照 |
| `status` | `pending / delivered / acknowledged / resolved / expired / cancelled` |
| `attempt_count` | 已投递尝试次数 |
| `max_attempts` | 最大尝试次数 |
| `ttl_seconds` | TTL 秒数 |
| `expires_at` | 过期时间 |
| `delivered_at` | 实际投递时间 |
| `acknowledged_at` | ACK 时间（强 ACK 时重点使用） |
| `resolved_at` | 该投递记录结束时间 |
| `next_retry_at` | 下次重试时间 |
| `last_error_code` | 最近一次投递错误码 |
| `last_error_message` | 最近一次投递错误说明 |
| `created_at` | 创建时间 |
| `updated_at` | 更新时间 |

建议约束与索引：
- `UNIQUE(message_id, recipient_agent)`
- `(status, expires_at)` 便于扫描过期消息
- `(recipient_agent, status, priority, created_at)` 便于接收方拉取待处理消息
- `(next_retry_at)` 便于重试调度

### 12.12 Interrupt
Interrupt 不建议仅作为 mailbox payload 的一部分存在，应有独立对象，便于前端、会话和恢复流程引用。

| 字段 | 说明 |
|---|---|
| `interrupt_id` | interrupt 主键 |
| `conversation_id` | 所属会话 |
| `task_id` | 所属任务 |
| `node_id` | 触发节点 |
| `source_agent` | 发起 interrupt 的 agent |
| `source_message_id` | 对应的 `clarification_request` mailbox message |
| `question` | 给用户展示的问题 |
| `reason_code` | 中断原因码 |
| `required_fields` | 结构化缺失字段定义 |
| `status` | `open / answered / expired / cancelled` |
| `expires_at` | 用户回复截止时间 |
| `created_at` | 创建时间 |
| `answered_at` | 回复时间 |
| `cancelled_at` | 取消时间 |

建议索引：
- `(conversation_id, status, created_at)`
- `(task_id, node_id)`
- `(expires_at)`

### 12.13 InterruptAnswer
若希望保留用户补充历史和多次回答痕迹，建议单独保留 answer 记录表。

| 字段 | 说明 |
|---|---|
| `interrupt_answer_id` | 回答记录主键 |
| `interrupt_id` | 关联 interrupt |
| `answer_payload` | 用户补充的结构化内容 |
| `source_message_id` | 关联前端消息 ID（可选） |
| `accepted` | 是否被系统接受为有效回答 |
| `created_at` | 创建时间 |
| `accepted_at` | 被接受时间 |

### 12.14 Checkpoint
为支持 `resume_notice` 和节点恢复，checkpoint 也建议独立持久化。

| 字段 | 说明 |
|---|---|
| `checkpoint_id` | checkpoint 主键 |
| `task_id` | 所属任务 |
| `node_id` | 所属节点 |
| `agent_id` | 创建 checkpoint 的 agent |
| `snapshot_ref` | 快照引用（对象存储、文件路径或序列化键） |
| `snapshot_kind` | 快照类型 |
| `resume_token` | 恢复令牌 |
| `source_message_id` | 关联 mailbox message（可选） |
| `created_at` | 创建时间 |
| `invalidated_at` | 失效时间 |

### 12.15 Mailbox 表结构设计原则
- mailbox 表结构应同时兼容 SQLite 本地测试与 PostgreSQL 长期目标，逻辑字段保持一致，物理类型可后续映射。
- `payload`、`required_fields`、`answer_payload` 等字段在 SQLite 阶段可落为 JSON 文本，在 PostgreSQL 阶段可映射为 JSONB。
- ACK、TTL、重试相关字段优先放在 `MailboxDelivery`，避免主表过度承担接收方状态。
- interrupt / checkpoint 不建议仅做 payload 内嵌对象，而应具备独立主键与查询入口。

### 12.16 SQLite → PostgreSQL 物理 DDL 策略
本项目当前采用：
- **本地测试状态库**：SQLite
- **目标正式状态库**：PostgreSQL

因此数据层采用的原则是：
> **逻辑模型同构，物理类型增强。**

即：
- SQLite 与 PostgreSQL 共享同一套逻辑表结构、主外键关系、状态机语义与字段命名
- SQLite 阶段优先使用通用类型落地
- PostgreSQL 阶段在不改变字段语义的前提下，对部分字段升级为更适合生产环境的物理类型

### 12.16.1 一期物理类型约定
#### SQLite 阶段
优先使用以下通用落地方式：
- ID：`TEXT`
- 时间：`TEXT`（ISO8601）
- JSON 结构：`TEXT`（存 JSON 字符串）
- 普通字符串：`TEXT`
- 计数/重试次数/优先级：`INTEGER`

#### PostgreSQL 阶段
优先升级为以下类型：
- ID：`TEXT` 或 `VARCHAR`（逻辑主键不变）
- 时间：`TIMESTAMPTZ`
- JSON 结构：`JSONB`
- 普通字符串：`TEXT`
- 计数/重试次数/优先级：`INTEGER`

### 12.16.2 不需要变更的字段原则
以下字段在 SQLite 与 PostgreSQL 间通常不需要语义变更，只需保持同名同义：
- 各类 ID 字段（若当前使用字符串 ID）
- 普通文本说明字段
- 简单整数字段
- 主外键关系字段

### 12.16.3 需要从 SQLite 升级到 PostgreSQL 的字段清单
以下字段建议在 PostgreSQL 落地时做显式类型增强；这些字段需要在后续 DDL 设计和迁移脚本中**重点标清楚**。

#### Conversation
- `created_at`: `TEXT` -> `TIMESTAMPTZ`
- `updated_at`: `TEXT` -> `TIMESTAMPTZ`

#### Message
- `created_at`: `TEXT` -> `TIMESTAMPTZ`

#### Task
- `cancel_requested_at`: `TEXT` -> `TIMESTAMPTZ`
- `created_at`: `TEXT` -> `TIMESTAMPTZ`
- `updated_at`: `TEXT` -> `TIMESTAMPTZ`

#### TaskNode
- `retry_policy`: `TEXT(JSON)` -> `JSONB`
- `timeout_policy`: `TEXT(JSON)` -> `JSONB`
- `input_refs`: `TEXT(JSON)` -> `JSONB`
- `output_refs`: `TEXT(JSON)` -> `JSONB`
- `started_at`: `TEXT` -> `TIMESTAMPTZ`
- `finished_at`: `TEXT` -> `TIMESTAMPTZ`

#### Artifact
- `created_at`: `TEXT` -> `TIMESTAMPTZ`

#### CapabilityDefinition
- `input_contract`: `TEXT(JSON)` -> `JSONB`
- `output_contract`: `TEXT(JSON)` -> `JSONB`

#### AgentInstance
- `supported_capabilities`: `TEXT(JSON)` -> `JSONB`
- `resource_snapshot`: `TEXT(JSON)` -> `JSONB`
- `last_heartbeat_at`: `TEXT` -> `TIMESTAMPTZ`

#### EventRecord
- `payload`: `TEXT(JSON)` -> `JSONB`
- `created_at`: `TEXT` -> `TIMESTAMPTZ`

#### MailboxMessage
- `payload`: `TEXT(JSON)` -> `JSONB`
- `created_at`: `TEXT` -> `TIMESTAMPTZ`
- `resolved_at`: `TEXT` -> `TIMESTAMPTZ`

#### MailboxDelivery
- `expires_at`: `TEXT` -> `TIMESTAMPTZ`
- `delivered_at`: `TEXT` -> `TIMESTAMPTZ`
- `acknowledged_at`: `TEXT` -> `TIMESTAMPTZ`
- `resolved_at`: `TEXT` -> `TIMESTAMPTZ`
- `next_retry_at`: `TEXT` -> `TIMESTAMPTZ`
- `created_at`: `TEXT` -> `TIMESTAMPTZ`
- `updated_at`: `TEXT` -> `TIMESTAMPTZ`

#### Interrupt
- `required_fields`: `TEXT(JSON)` -> `JSONB`
- `answer_payload`: `TEXT(JSON)` -> `JSONB`
- `expires_at`: `TEXT` -> `TIMESTAMPTZ`
- `created_at`: `TEXT` -> `TIMESTAMPTZ`
- `answered_at`: `TEXT` -> `TIMESTAMPTZ`
- `cancelled_at`: `TEXT` -> `TIMESTAMPTZ`

#### InterruptAnswer
- `answer_payload`: `TEXT(JSON)` -> `JSONB`
- `created_at`: `TEXT` -> `TIMESTAMPTZ`
- `accepted_at`: `TEXT` -> `TIMESTAMPTZ`

#### Checkpoint
- `created_at`: `TEXT` -> `TIMESTAMPTZ`
- `invalidated_at`: `TEXT` -> `TIMESTAMPTZ`

### 12.16.4 迁移策略
从 SQLite 切换到 PostgreSQL 时，建议按以下顺序执行：
1. 先冻结逻辑模型，确保字段语义不再变化
2. 生成 PostgreSQL 物理 DDL
3. 将 SQLite 中的时间字段转换为标准时间格式并导入 PostgreSQL
4. 将 SQLite 中的 JSON 文本字段解析后导入 PostgreSQL JSONB 字段
5. 校验主键、外键、状态字段、payload 字段的一致性
6. 在 PostgreSQL 环境重放最小状态流测试，验证 task / mailbox / interrupt / checkpoint 的核心链路

### 12.16.5 设计约束
- 不允许为了 PostgreSQL 增强而改变现有逻辑字段语义
- 不允许在 SQLite 阶段依赖 PostgreSQL 独占能力来保证核心正确性
- PostgreSQL 增强应聚焦于：查询能力、JSON 结构访问、时间字段、索引能力
- 在 PostgreSQL 地址与部署信息可用前，本阶段只定义字段升级策略，不绑定具体连接参数

### 12.17 结构化 mailbox 的物理 DDL 与迁移策略
本节专门针对以下对象给出物理落地建议：
- `MailboxMessage`
- `MailboxDelivery`
- `Interrupt`
- `InterruptAnswer`
- `Checkpoint`

目标是保证：
- SQLite 本地测试可跑通完整协作链
- PostgreSQL 上线后无需推翻 mailbox 逻辑模型
- ACK / TTL / 重试 / interrupt / resume 的关键链路可平滑迁移

### 12.17.1 总体 DDL 原则
- SQLite 与 PostgreSQL 必须保持**同名字段、同义主键、同样的主外键关系与状态语义**。
- SQLite 阶段优先采用通用类型与显式索引，不依赖数据库专有高级能力。
- PostgreSQL 阶段在不改变字段语义的前提下，对 JSON、时间和索引能力做增强。
- mailbox 相关表不应依赖触发器才能保证基本正确性；核心一致性应由应用层状态机保证。

### 12.17.2 MailboxMessage 物理 DDL 建议
#### SQLite 建议
- `message_id`: `TEXT PRIMARY KEY`
- `conversation_id`: `TEXT`
- `task_id`: `TEXT`
- `node_id`: `TEXT NULL`
- `parent_message_id`: `TEXT NULL`
- `correlation_id`: `TEXT`
- `from_agent`: `TEXT`
- `to_agent`: `TEXT NULL`
- `to_role`: `TEXT NULL`
- `channel`: `TEXT`
- `message_type`: `TEXT`
- `ack_policy`: `TEXT`
- `priority`: `INTEGER`
- `payload`: `TEXT`（JSON 字符串）
- `payload_schema_version`: `INTEGER`
- `created_at`: `TEXT`（ISO8601）
- `resolved_at`: `TEXT NULL`（ISO8601）

#### PostgreSQL 建议
- 以上字段逻辑保持不变
- `payload`: 升级为 `JSONB`
- `created_at` / `resolved_at`: 升级为 `TIMESTAMPTZ`

#### 关键索引建议
- `PRIMARY KEY(message_id)`
- `INDEX idx_mailbox_message_task_created(task_id, created_at)`
- `INDEX idx_mailbox_message_node_created(node_id, created_at)`
- `INDEX idx_mailbox_message_channel_type_created(channel, message_type, created_at)`
- `INDEX idx_mailbox_message_correlation(correlation_id)`

### 12.17.3 MailboxDelivery 物理 DDL 建议
#### SQLite 建议
- `delivery_id`: `TEXT PRIMARY KEY`
- `message_id`: `TEXT NOT NULL`
- `recipient_agent`: `TEXT NOT NULL`
- `recipient_role`: `TEXT NULL`
- `status`: `TEXT NOT NULL`
- `attempt_count`: `INTEGER NOT NULL`
- `max_attempts`: `INTEGER NOT NULL`
- `ttl_seconds`: `INTEGER NOT NULL`
- `expires_at`: `TEXT`（ISO8601）
- `delivered_at`: `TEXT NULL`
- `acknowledged_at`: `TEXT NULL`
- `resolved_at`: `TEXT NULL`
- `next_retry_at`: `TEXT NULL`
- `last_error_code`: `TEXT NULL`
- `last_error_message`: `TEXT NULL`
- `created_at`: `TEXT`
- `updated_at`: `TEXT`

#### PostgreSQL 建议
- 时间字段升级为 `TIMESTAMPTZ`
- 其余字段保持同义类型

#### 关键约束与索引建议
- `UNIQUE(message_id, recipient_agent)`
- `INDEX idx_mailbox_delivery_status_expires(status, expires_at)`
- `INDEX idx_mailbox_delivery_recipient_queue(recipient_agent, status, priority, created_at)`
- `INDEX idx_mailbox_delivery_next_retry(next_retry_at)`
- `FOREIGN KEY(message_id) REFERENCES MailboxMessage(message_id)`

### 12.17.4 Interrupt / InterruptAnswer 物理 DDL 建议
#### Interrupt
SQLite：
- `interrupt_id`: `TEXT PRIMARY KEY`
- `conversation_id`: `TEXT`
- `task_id`: `TEXT`
- `node_id`: `TEXT`
- `source_agent`: `TEXT`
- `source_message_id`: `TEXT`
- `question`: `TEXT`
- `reason_code`: `TEXT`
- `required_fields`: `TEXT`（JSON 字符串）
- `status`: `TEXT`
- `expires_at`: `TEXT`
- `created_at`: `TEXT`
- `answered_at`: `TEXT NULL`
- `cancelled_at`: `TEXT NULL`

PostgreSQL：
- `required_fields`: `JSONB`
- 所有时间字段：`TIMESTAMPTZ`

关键索引建议：
- `INDEX idx_interrupt_conversation_status_created(conversation_id, status, created_at)`
- `INDEX idx_interrupt_task_node(task_id, node_id)`
- `INDEX idx_interrupt_expires(expires_at)`

#### InterruptAnswer
SQLite：
- `interrupt_answer_id`: `TEXT PRIMARY KEY`
- `interrupt_id`: `TEXT`
- `answer_payload`: `TEXT`（JSON 字符串）
- `source_message_id`: `TEXT NULL`
- `accepted`: `INTEGER`（0/1）
- `created_at`: `TEXT`
- `accepted_at`: `TEXT NULL`

PostgreSQL：
- `answer_payload`: `JSONB`
- 时间字段：`TIMESTAMPTZ`
- `accepted`: `BOOLEAN`

关键约束与索引建议：
- `FOREIGN KEY(interrupt_id) REFERENCES Interrupt(interrupt_id)`
- `INDEX idx_interrupt_answer_interrupt_created(interrupt_id, created_at)`

### 12.17.5 Checkpoint 物理 DDL 建议
SQLite：
- `checkpoint_id`: `TEXT PRIMARY KEY`
- `task_id`: `TEXT`
- `node_id`: `TEXT`
- `agent_id`: `TEXT`
- `snapshot_ref`: `TEXT`
- `snapshot_kind`: `TEXT`
- `resume_token`: `TEXT`
- `source_message_id`: `TEXT NULL`
- `created_at`: `TEXT`
- `invalidated_at`: `TEXT NULL`

PostgreSQL：
- 时间字段升级为 `TIMESTAMPTZ`

关键索引建议：
- `INDEX idx_checkpoint_task_node(task_id, node_id)`
- `INDEX idx_checkpoint_resume_token(resume_token)`
- `INDEX idx_checkpoint_invalidated(invalidated_at)`

### 12.17.6 mailbox 专项字段升级清单（SQLite -> PostgreSQL）
为避免迁移时遗漏，这里单独重复列出 mailbox 相关对象需要升级的字段：

#### MailboxMessage
- `payload`: `TEXT(JSON)` -> `JSONB`
- `created_at`: `TEXT` -> `TIMESTAMPTZ`
- `resolved_at`: `TEXT` -> `TIMESTAMPTZ`

#### MailboxDelivery
- `expires_at`: `TEXT` -> `TIMESTAMPTZ`
- `delivered_at`: `TEXT` -> `TIMESTAMPTZ`
- `acknowledged_at`: `TEXT` -> `TIMESTAMPTZ`
- `resolved_at`: `TEXT` -> `TIMESTAMPTZ`
- `next_retry_at`: `TEXT` -> `TIMESTAMPTZ`
- `created_at`: `TEXT` -> `TIMESTAMPTZ`
- `updated_at`: `TEXT` -> `TIMESTAMPTZ`

#### Interrupt
- `required_fields`: `TEXT(JSON)` -> `JSONB`
- `answer_payload`: `TEXT(JSON)` -> `JSONB`
- `expires_at`: `TEXT` -> `TIMESTAMPTZ`
- `created_at`: `TEXT` -> `TIMESTAMPTZ`
- `answered_at`: `TEXT` -> `TIMESTAMPTZ`
- `cancelled_at`: `TEXT` -> `TIMESTAMPTZ`

#### InterruptAnswer
- `answer_payload`: `TEXT(JSON)` -> `JSONB`
- `created_at`: `TEXT` -> `TIMESTAMPTZ`
- `accepted_at`: `TEXT` -> `TIMESTAMPTZ`
- `accepted`: `INTEGER` -> `BOOLEAN`

#### Checkpoint
- `created_at`: `TEXT` -> `TIMESTAMPTZ`
- `invalidated_at`: `TEXT` -> `TIMESTAMPTZ`

### 12.17.7 mailbox 迁移策略
从 SQLite 切换到 PostgreSQL 时，mailbox 相关对象建议按以下顺序迁移：
1. 先迁移 `MailboxMessage`
2. 再迁移 `MailboxDelivery`
3. 再迁移 `Interrupt`
4. 再迁移 `InterruptAnswer`
5. 最后迁移 `Checkpoint`

这样可以保证：
- 先建立消息主记录
- 再建立投递状态
- 再恢复 interrupt / answer 链路
- 最后恢复 resume 所需 checkpoint 引用

### 12.17.8 mailbox 迁移校验项
迁移完成后，至少校验：
- `message_id` 主键唯一性
- `UNIQUE(message_id, recipient_agent)` 是否仍成立
- 所有 `source_message_id` 是否能回查到 mailbox message
- 所有 `interrupt_id` 与 `interrupt_answer` 是否一致
- 所有 `checkpoint.resume_token` 是否完整
- 所有 ACK / TTL / next_retry 时间字段格式是否正确
- 所有 JSON 文本是否已成功转为 PostgreSQL JSONB

### 12.17.9 一期设计边界
本阶段只定义 mailbox 物理 DDL 原则、字段映射与迁移步骤；以下内容留待 PostgreSQL 正式部署后细化：
- PostgreSQL 最终 DDL 文件生成方式
- 是否引入局部索引 / 部分索引 / JSONB GIN 索引
- mailbox 高吞吐场景下的冷热分表策略
- 是否需要专门的清理归档表

## 13. 结构化规则配置示意
一期不强制先实现完整 DSL，但建议预留如下配置形态：

```yaml
routing_rules:
  - when:
      intent: document_retrieval
    then:
      root_capability: document.retrieve

  - when:
      intent: mysql_query
    then:
      root_capability: mysql.nl2sql_query

node_templates:
  bioinfo.external_job:
    criticality: required
    dependency_type: hard
    retry_policy:
      max_attempts: 1
    timeout_policy:
      execution_seconds: 1800
    resource_class: external_api
```

## 14. MVP 验收闭环
### 14.1 MVP 核心目标
证明主代理框架已经具备“接收对话消息 → 生成 NL2SQL 任务 DAG → 调度多个 capability → 调用外部 LLM 与本地数据库访问适配层 → 持久化状态与记忆 → 流式回传 → 支持硬停止”的完整闭环。

### 14.2 MVP 验收场景一：NL2SQL 标准异步执行闭环
一个用户在已有 `conversation_id` 下发起一条自然语言数据查询请求，例如“统计最近 30 天按地区分组的订单数”，主代理需要：
1. 接收消息并在短时间内返回 `task_id`
2. 读取会话历史记忆与当前会话上下文
3. 生成一个最小可运行 DAG
4. 至少调度 4 类节点：
   - 任务理解 / 路由节点
   - Schema / 查询上下文准备节点
   - SQL 生成与安全校验节点
   - SQL 执行与结果汇总节点
5. 通过外部 LLM 生成 SQL，并通过数据库访问适配层访问 MySQL
6. 一期只允许执行只读 SQL；对非只读语句必须在校验阶段拦截
7. 将生成 SQL、校验结果、执行摘要、节点状态、任务状态、关键产物、最终消息持久化
8. 通过实时事件流向前端回传执行进度
9. 最终返回助手结果，并将任务置为 `completed`

### 14.3 MVP 验收场景二：硬停止闭环
在任务运行过程中，用户发起“停止处理”请求，系统需要：
1. 立即接受取消请求并返回 `cancelling`
2. 阻止新节点启动
3. 终止当前 task context，并向相关运行中节点传播取消
4. 将未启动节点标记为取消阻断
5. 释放本地调度与资源占位
6. 输出结构化取消事件与 JSONL 审计日志
7. 将任务按状态机规则收敛到 `cancelled` 或 `cancellation_partial`
8. 后续同一会话可继续发起新任务，且不复用被取消任务产物

### 14.4 MVP 最低验收标准
- 消息提交接口可稳定返回 `task_id`
- 同一会话串行约束生效
- 任务图可查询
- 至少 1 个外部 LLM 调用节点与 1 个 MySQL 执行节点可被主代理成功调度
- NL2SQL 链路可完成“自然语言 → SQL → 只读校验 → 执行 → 汇总输出”
- 非只读 SQL 能被明确拦截并记录审计事件
- 事件流可稳定输出关键状态变化
- JSONL 日志可按 `conversation_id / task_id` 检索
- Task Context Termination 链路可成功跑通

## 15. 后续专题设计与演进项
以下事项不阻碍本 PRD 作为当前正式基线，但建议在后续专题设计中继续细化：
- PostgreSQL 部署完成后的索引优化、JSONB 查询策略与正式 DDL 生成流程
- PostgreSQL 部署完成后的状态库存储详细设计（当前本地测试先用 SQLite）
- 数据库查询物理取消、只读账号前提与外部 LLM 迟到结果处理规范（已完成首版定义）
- 任务优先级与优先级权重细化（背压与配额策略已完成首版定义）
- DAG 动态扩展的具体约束边界
- PostgreSQL 最终 DDL 文件生成方式与索引增强细节
