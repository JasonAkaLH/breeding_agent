# 主代理框架 PRD（后端总览）

- **项目**：multi_agent_framework
- **范围**：后端主代理框架
- **文档状态**：正式版（已补齐至 Skill Executor 实现需求 PRD；PRD 目录为当前文档基线）
- **日期**：2026-05-12
- **说明**：本文件为后端 PRD 总览入口。后端专题 PRD 统一放在 `docs/prd/backend/`；前端 PRD 后续放在 `docs/prd/frontend/`。

## 0. 目录定位

本 PRD 只覆盖后端：主代理框架、SQLQuery 能力链路、状态存储、API、LLM runtime 与后端可观测性。

前端产品体验、页面结构、交互与视觉设计不在本文件展开；后续前端设计应以 `docs/prd/frontend/` 为入口，并引用本目录中的后端 API / 事件 / 能力契约。

## 1. 项目背景

本项目面向内部付费用户，目标是构建一个办公助手后端。当前优先建设的是主代理框架，而不是具体功能 Agent 本身；后续文档 RAG、SQLQuery、数据分析、农业生物信息分析等能力将在该框架之上接入。

本框架不依赖 LangChain、LangGraph、AutoGen 等现成 Agent 框架，采用 Python 为主、异步优先的服务端架构；性能热点未来可下沉到 C++，但不作为一期前提。

## 2. 一期目标与非目标摘要

### 2.1 一期目标

一期需交付一个可支撑业务扩展的主代理内核，至少覆盖：
- 任务拆分
- Agent 注册与发现
- 资源调度与执行
- 上下文传递
- 会话状态
- 任务队列
- 观测日志
- 记忆系统（以会话延续型记忆为主）
- 实时事件流
- 用户主动中断任务
- 首个可验收业务样例绑定为 **SQLQuery 只读查询链路**

### 2.2 一期不做但必须预留接口

- 人工审批
- 权限控制
- 通用工具调用平台
- 多实例生产化部署能力
- 完整长期记忆系统
- 跨任务知识沉淀 / 任务知识复用型记忆

## 3. 总体架构摘要

### 3.1 产品与部署形态

- 产品形态：前后端分离
- 对外形态：HTTP / API 服务
- 服务模型：异步任务驱动的对话式 Agent 服务
- 业务查询数据源：公司现有 MySQL 数据库
- 主框架状态库目标：PostgreSQL
- 本地测试状态库：SQLite

### 3.2 架构核心原则

- 主代理优先面向 **capability** 编排，而不是直接面向 tool
- 任务采用**混合型 DAG**，允许受控动态扩展
- 系统内部采用：**状态机主干 + 结构化 mailbox + Interrupt/Resume**
- 硬停止的正式语义为：**Task Context Termination**
- 背压策略：**严格拒绝型**
- 配额策略：**系统级 + capability 级**，并预留未来用户级配额兼容

## 4. 后端专题 PRD 索引

| 专题 | 文件 | 适合阅读场景 |
|---|---|---|
| 产品目标与范围 | `docs/prd/backend/01-产品目标与范围.md` | 了解项目背景、后端边界、术语 |
| 编排模型与资源调度 | `docs/prd/backend/02-编排模型与资源调度.md` | 主代理拆分、DAG、调度、背压、配额 |
| 协作协议与任务生命周期 | `docs/prd/backend/03-协作协议与任务生命周期.md` | mailbox、interrupt/resume、取消、状态机 |
| 状态存储与迁移策略 | `docs/prd/backend/04-状态存储与迁移策略.md` | SQLite / PostgreSQL、mailbox DDL、迁移 |
| API 与核心数据模型 | `docs/prd/backend/05-API与核心数据模型.md` | API、Conversation/Task/Node 等对象模型 |
| SQLQuery MVP 设计 | `docs/prd/backend/06-SQLQuery-MVP设计.md` | SQLQuery 路由、SQL Guard、Schema Context Builder、MVP 验收 |
| SQLQuery LLM 增强与真实库验证 | `docs/prd/backend/07-SQLQuery-LLM增强与真实库验证.md` | prompt schema、LLM fallback、MySQL 只读适配器 |
| 主代理 Skill 兼容与真实 LLM Runtime | `docs/prd/backend/08-主代理Skill兼容与真实LLM运行时.md` | 普通主代理消息、Skill 上下文、真实 provider smoke |
| 高层 DAG 规划与 SQLQuery 宏能力边界 | `docs/prd/backend/09-高层DAG规划与SQLQuery宏能力边界.md` | public capability、planner validator、macro expander |
| 对话上下文记忆与压缩 | `docs/prd/backend/10-对话上下文记忆与压缩PRD.md` | 多轮对话记忆、Planner / 主代理上下文注入、两级压缩策略 |
| Skill 输出文件 Artifact 与下载 | `docs/prd/backend/11-Skill输出文件Artifact与下载PRD.md` | Skill 产出 HTML / CSV / XLSX / PDF 等文件、managed artifact、下载鉴权、安全边界 |
| Skill 一等 Capability 能力池 | `docs/prd/backend/12-Skill一等Capability能力池PRD.md` | 将项目 Skill 注册为 `skill.*` public capability、Planner / Replanner 可发现、统一能力池 |
| Skill 动态加载与热部署 | `docs/prd/backend/13-Skill动态加载与热部署PRD.md` | 新聊天首次任务前动态刷新 Skill runtime bundle，实现公开 Skill 热加载、原子激活与运行中任务保护 |
| MCP Runtime 实现需求 | `docs/prd/backend/14-MCPRuntime实现需求PRD.md` | 按 MCP latest spec 2025-11-25 设计外部 MCP server / tools 接入、标准通信、capability 包装与安全治理 |
| Skill Executor 实现需求 | `docs/prd/backend/15-SkillExecutor实现需求PRD.md` | 定义 `skill.*` 一等执行器的职责边界、service binding、安全约束、artifact/event 归一化与 SQLQuery Skill 化前置要求 |

## 5. 当前已定的关键决策摘要

### 5.1 主框架共性决策

- 当前只写后端主代理框架 PRD，不先展开前端与具体功能 Agent 产品实现。
- capability 是稳定能力契约；agent 是执行实体；tool 是底层操作接口。
- 子代理执行采用混合模式：优先专用任务型 Agent，必要时允许受限 ReAct Worker。
- 同一 `conversation_id` 内任务串行执行。
- 主代理采用受规则、状态机与完成判定约束的编排型闭环，而不是自由试错式纯 ReAct。
- 任务优先级采用“两层模型”：控制类动作独立最高优先级；普通任务按来源驱动排序，并允许少量结构化权重作为同类内排序依据。
- 主框架与 capability 是明确上下级关系：主框架只管拆解、编排、分发；SQLQuery 等 capability 只管各自执行。

### 5.2 协作与生命周期决策

- 结构化 mailbox 采用统一信封 + channel + typed payload 模型。
- mailbox 生命周期采用 **分级 ACK**。
- 强 ACK 用于控制类 / interrupt 类消息；轻 ACK 用于普通协作类消息。
- 停止处理的正式语义是终止 task context，而不是直接定义为“杀线程”。

### 5.3 状态存储决策

- 主框架状态不落公司业务 MySQL。
- 本地先 SQLite，同构迁移到 PostgreSQL。
- PostgreSQL 存结构化状态与索引，不直接存大对象正文。
- PostgreSQL DDL 采用 ORM Model + migration 生成；一期索引策略为基础索引 + 少量关键增强索引。

### 5.4 SQLQuery 决策

- SQLQuery 是一期首个 MVP 样例，外部只暴露 `sql_query.query` 宏能力；`sql_query.*` 内部节点不作为外部请求入口。
- SQLQuery 只允许只读查询；MySQL 只读执行必须通过 SQL Guard 通过令牌后才能执行。
- MySQL 连接串与只读账号只允许通过本地 `config.yaml` 或部署环境变量注入，不得在仓库内硬编码；仍保留 SQL Guard 作为数据库权限之外的第二层保护。
- SQLQuery 的 SQL 生成与结果筛选默认可接入 LLM；当前默认 workflow 尾节点为 `sql_query.result_filtering`，负责从 `LIKE` 召回候选中筛掉不符合用户真实需求的行，并把筛选后的表格交给主代理整合。

### 5.5 主代理与 LLM Runtime 决策

- `capability_id=None` 的普通消息默认进入 `main_agent.respond`；显式 `sql_query` / `sql_query.query` 进入 SQLQuery 固定 workflow。
- 主代理可读取 Codex Skill 兼容的 `SKILL.md` 元数据、上传 artifact 脱敏上下文与受控脚本输出，用于构造提示词。
- 主代理真实 LLM provider 必须通过可测试 seam 绑定；自动化测试默认使用 fake / injected stream，真实 provider 只在显式配置或手工 smoke 中验证。
- 主代理与 SQLQuery 的 LLM 审计事件不得记录 API key、完整 prompt、完整 rows、base_url 等敏感信息。


### 5.6 对话记忆与上下文压缩决策

- v1 记忆系统定位为 conversation 内会话延续型记忆，不做跨会话长期用户画像或知识沉淀。
- 对话记忆上下文注入 LLM Planner / 自动规划阶段与 `main_agent.respond` 最终回答阶段，保证追问、省略主语和纠错能正确影响路由与回答。
- SQLQuery 内部 LLM 节点暂不直接消费完整对话记忆；如需上下文补全，应先在 public 规划层把当前轮问题合成为明确问题。
- 记忆压缩采用两级策略：Level 1 删除 capability 业务中间产物；Level 2 对较早对话历史做摘要压缩并保留最近若干轮原文。
- 记忆上下文必须按 account / conversation 隔离，并禁止注入 SQL、guard token、schema DDL、完整 rows、完整 prompt、API key、base_url 等敏感或高成本内容。

### 5.7 Skill 输出文件与下载决策

- Skill 生成的 HTML、CSV、XLSX、PDF、图片等文件必须由平台统一收集为 managed artifact，不能由 Skill 暴露本地路径或自定义下载接口。
- 下载入口必须复用 task / conversation owner 鉴权，前端只使用 `artifact_id` / `download_url`，不得看到服务器真实路径。
- 输出文件内容默认不进入主代理 prompt；prompt 只注入文件名、类型、大小、摘要等安全 metadata。
- v1 HTML 文件默认按附件下载，不作为站内可信页面直接 inline 渲染；未来如需预览应单独设计 sandbox / CSP。

### 5.8 Skill 一等 Capability 能力池决策

- 项目级 Skill 应可升级为 `skill.*` public capability，进入与 `main_agent.respond`、`sql_query.query` 相同的 `CapabilityRegistry` public 能力池。
- Planner / Runtime Replanner / `/api/v1/capabilities` 必须从同一 public capability pool 发现公开 Skill，避免深度思考阶段看不到已注册 Skill。
- v1 推荐采用 “Skill public macro → `main_agent.respond` forced skill” 模型：LLM 只选择 `skill.*` capability，系统注入可信 forced skill metadata，继续复用主代理受控 Skill runtime。
- 后续结构化 / 脚本型 / 项目级可信 Skill 应按 `docs/prd/backend/15-SkillExecutor实现需求PRD.md` 演进为 generic Skill Executor 执行模型，forced `main_agent.respond` 仅作为兼容路径。
- 默认只公开仓库项目级 `skill/` 下的 Skill；用户级 `~/.codex/skills` 不默认公开给业务 Planner 或 API。

### 5.9 Skill 动态加载与热部署决策

- 当前 Skill 加载模型是 runtime 启动期一次性扫描；新增、删除或修改 `SKILL.md` 默认不会在不重启服务的情况下进入 Planner / Replanner / API 能力池。
- 后续热部署应以 **Skill runtime bundle** 为刷新单位，同步包含 `SkillCatalog`、`skill.*` descriptors、capability 映射、macro providers、主代理 forced skill 执行所需 manifest 与 revision 信息。
- “每次开启新聊天动态加载 Skill”的可靠后端边界是新 conversation 的首次任务提交前，而不是前端仅生成本地 `conversation_id` 的点击动作。
- 刷新必须原子激活：成功则新任务使用新 bundle，失败则保留上一份可用 bundle，内置 capability 不受影响。
- 运行中任务应记录 `skill_bundle_revision` 并继续使用其规划时的 Skill 快照；生产级热部署还应为公开 Skill 脚本与必要资源建立 package snapshot。

### 5.10 MCP Runtime 决策

- MCP Runtime 在本项目中首先是 **MCP client runtime**，用于接入外部 MCP server 暴露的 tools；不在该专题内把本平台反向实现为 MCP server。
- MCP 通信协议层必须按 MCP latest spec 2025-11-25 设计，基于 JSON-RPC 2.0、lifecycle negotiation、standard transports、Streamable HTTP / stdio 抽象、`MCP-Protocol-Version` 与 `MCP-Session-Id` 等标准语义。
- MCP 原始 tool 不直接成为 orchestration 概念；外部工具必须先经过 `src/integrations/mcp/` 适配，再由业务 capability 或受控 generic MCP capability 进入 `CapabilityRegistry`。
- Planner 只允许看到本地审核后的 public capability 描述和 payload allowlist；不得直接看到未审核 tool description、server endpoint、auth token、任意 headers 或完整 tool schema。
- MCP tool 默认不公开；只有 allowlist 且低风险、只读、幂等、输入输出清晰的 tool 才可配置为 generic public capability。
- destructive / write / credentialed external 类 tool 必须走业务 capability、Interrupt / confirmation 与审计，不允许 generic public 直达。
- v1 远程 server 必须支持 Streamable HTTP；stdio 是 MCP 标准 transport，但必须显式配置并受沙箱、进程生命周期和权限治理约束。

### 5.11 Skill Executor 决策

- Skill Executor 是通用执行壳，不承载 SQLQuery、数据分析、报告生成等业务逻辑；业务语义必须放在 Skill 包、领域服务或 MCP tool 背后。
- `skill.*` capability 的执行必须按 Skill bundle revision 固定版本，避免新聊天热刷新影响运行中任务。
- script Skill 应由 generic Skill Executor 执行并归一化为 `CapabilityExecutionResult`、artifact、event 与 audit；不应长期依附 `main_agent.respond` 私有脚本路径。
- service binding 必须采用“manifest 声明 + runtime allowlist”双重授权；普通 public Skill 和用户级 Skill 默认不能获得 MySQL readonly、内部 LLM、secret 等受控资源。
- Skill Executor 与 MCP Tool Executor 对等，分别承接 `skill.*` 与 `mcp.*` 能力来源；orchestration 不应再为具体业务 Skill 写特判。
- SQLQuery 后续迁移为 `skill.sql_query` 必须先满足 Skill Executor 的受控执行、service binding、artifact/event 归一化与安全审计要求。

## 6. 当前验收基线与归档证据

一期范围内承诺的“主代理最小内核 + SQLQuery 只读 MVP + FastAPI/SSE/cancel/query API”已完成；该结论仅覆盖一期冻结范围，不包含 PostgreSQL 正式化、第二 capability、长期记忆专题、跨任务知识沉淀、主代理 / 通用子代理 LLM 化等后续增强主题。

| 验收口径 | 证据 | 结论 |
|---|---|---|
| 能提交任务 | `tests/api/test_message_submission.py`、`tests/e2e/test_sql_query_happy_path.py` | 通过 |
| 能观察状态和事件流 | `tests/api/test_task_query.py`、`tests/api/test_task_events_sse.py`、`tests/e2e/test_sql_query_happy_path.py` | 通过 |
| 能取消任务 | `tests/api/test_task_cancel.py`、`tests/e2e/test_cancel_late_result_ignored.py` | 通过 |
| 会话延续型记忆最小字段可被持久化并恢复 | `tests/storage/test_sqlite_conversation_repository.py`、`tests/storage/test_sqlite_task_repository.py`、`tests/storage/test_sqlite_interrupt_repository.py` | 通过 |
| 能跑通 SQLQuery 只读链路 | `tests/capabilities/sql_query/test_orchestration_flow.py`、`tests/e2e/test_sql_query_happy_path.py` | 通过 |
| 能阻断危险 SQL | `tests/capabilities/sql_query/test_sql_guard.py`、`tests/e2e/test_sql_query_guard_block.py`、`tests/observability/test_audit_jsonl.py` | 通过 |

关键验收链路：

- Happy path：提交消息后生成 DAG，SQL Guard 通过，只读执行完成，summary / artifact 落地，事件流收敛为 `task.completed`。
- Guard blocked：危险 SQL 被 `sql_query.sql_guard_blocked` 审计记录阻断，任务收敛为 `failed`，保留 `block_reason` 与 `route_context`。
- Interrupt / Resume：缺少必要业务信息时触发 interrupt；用户补充信息后恢复原 task，而不是创建新 task。
- Cancel + late result ignored：节点运行中取消任务后，迟到结果不回写 `completed`，审计保留 `task.late_result_discarded`。
- Observability / Audit：JSONL 审计具备 `event_type / task_id / payload`，blocked SQL 与 cancel 路径均有可复核字段。

已知一期边界：

1. SSE broker 为单进程内存实现，适合本地与一期最小闭环，不代表多实例生产方案。
2. cancel 采用语义终止，不依赖数据库物理 kill。
3. interrupt / resume 已具备验收闭环，但公开 API 仍以一期既定最小面为主，没有扩展为完整前端交互产品面。

## 7. 相关配套文档

- PRD 总目录：`docs/prd/README.md`
- 前端 PRD 预留入口：`docs/prd/frontend/README.md`
- 数据库结构说明：`docs/MySQL数据库表结构说明.md`
- SQLQuery prompt 输入模板：`docs/SQLQuery提示词输入模板.md`
- 对话上下文记忆与压缩 PRD：`docs/prd/backend/10-对话上下文记忆与压缩PRD.md`
- Skill 输出文件 Artifact 与下载 PRD：`docs/prd/backend/11-Skill输出文件Artifact与下载PRD.md`
- Skill 一等 Capability 能力池 PRD：`docs/prd/backend/12-Skill一等Capability能力池PRD.md`
- Skill 动态加载与热部署 PRD：`docs/prd/backend/13-Skill动态加载与热部署PRD.md`
- MCP Runtime 实现需求 PRD：`docs/prd/backend/14-MCPRuntime实现需求PRD.md`
- Skill Executor 实现需求 PRD：`docs/prd/backend/15-SkillExecutor实现需求PRD.md`

## 8. 使用建议

- 做全局规划时先读本文件。
- 做局部设计或开发计划时优先读取对应专题文档。
- 做 SQLQuery 实现或提示词设计时，配合 `docs/prd/backend/06-SQLQuery-MVP设计.md`、`docs/prd/backend/07-SQLQuery-LLM增强与真实库验证.md` 与 `docs/SQLQuery提示词输入模板.md` 一起阅读。
- 做前端设计时，不要把前端范围追加到本文件；应在 `docs/prd/frontend/` 新建独立 PRD，并引用本目录中的后端接口和事件契约。

## 9. 后续专题设计与演进项

以下事项不阻碍当前 PRD 作为正式基线，但建议在后续专题设计中继续细化：
- PostgreSQL 最终 DDL 文件生成方式与索引增强细节。
- PostgreSQL 部署完成后的索引优化、JSONB 查询策略与正式 DDL 生成流程。
- 任务优先级权重的更细粒度策略。
- Schema Context Builder 的更强评估样例与调优工具。
- 前端 PRD：对话界面、任务流、事件流、SQLQuery 结果与主代理 Skill 命中状态展示。
