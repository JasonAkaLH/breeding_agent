# 主代理框架 PRD（后端总览）

- **项目**：breeding_agent
- **范围**：后端主代理框架
- **文档状态**：正式版（已补齐至 Rust 化 Runtime 模块评估 PRD；PRD 目录为当前文档基线）
- **日期**：2026-05-13
- **说明**：本文件为后端 PRD 总览入口。后端专题 PRD 统一放在 `docs/prd/backend/`；跨后端与 Rust sidecar 的 MCP 联合实施 Phase PRD 放在 `docs/prd/MCP/`；前端 PRD 放在 `docs/prd/frontend/`。

## 0. 目录定位

本 PRD 只覆盖后端：主代理框架、数据查询 Skill 能力链路、状态存储、API、LLM runtime 与后端可观测性。

前端产品体验、页面结构、交互与视觉设计不在本文件展开；后续前端设计应以 `docs/prd/frontend/` 为入口，并引用本目录中的后端 API / 事件 / 能力契约。

## 1. 项目背景

本项目面向内部付费用户，目标是构建一个办公助手后端。当前优先建设的是主代理框架，而不是具体功能 Agent 本身；后续文档 RAG、数据查询 Skill、数据分析、农业生物信息分析等能力将在该框架之上接入。

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
- 首个可验收业务样例绑定为 **数据查询 Skill 只读查询链路**

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
| 可移除数据查询 Skill 设计 | 对应 Skill bundle 自带 docs | 领域路由、查询安全、Schema Context 与验收由 Skill 物理归属维护 |

| 主代理 Skill 兼容与真实 LLM Runtime | `docs/prd/backend/08-主代理Skill兼容与真实LLM运行时.md` | 普通主代理消息、Skill 上下文、真实 provider smoke |
| 高层 DAG 规划与 数据查询 Skill 边界 | 对应可移除 Skill bundle 自带的边界文档 | public capability、planner validator、Skill workflow expander |
| 对话上下文记忆与压缩 | `docs/prd/backend/10-对话上下文记忆与压缩PRD.md` | 多轮对话记忆、Planner / 主代理上下文注入、两级压缩策略 |
| Skill 输出文件 Artifact 与下载 | `docs/prd/backend/11-Skill输出文件Artifact与下载PRD.md` | Skill 产出 HTML / CSV / XLSX / PDF 等文件、managed artifact、下载鉴权、安全边界 |
| Skill 一等 Capability 能力池 | `docs/prd/backend/12-Skill一等Capability能力池PRD.md` | 将项目 Skill 注册为 `skill.*` public capability、Planner / Replanner 可发现、统一能力池 |
| Skill 动态加载与热部署 | `docs/prd/backend/13-Skill动态加载与热部署PRD.md` | 新聊天首次任务前动态刷新 Skill runtime bundle，实现公开 Skill 热加载、原子激活与运行中任务保护 |
| MCP Runtime 实现需求 | `docs/prd/backend/14-MCPRuntime实现需求PRD.md` | 按 MCP latest spec 2025-11-25 设计外部 MCP server / tools 接入、标准通信、capability 包装与安全治理 |
| Skill Executor 实现需求 | `docs/prd/backend/15-SkillExecutor实现需求PRD.md` | 定义 `skill.*` 一等执行器的职责边界、service binding、安全约束、artifact/event 归一化与 数据查询 Skill 化前置要求 |
| Skill Contract 渐进式披露与显式执行 | `docs/prd/backend/skill-contract-progressive-disclosure/README.md` | 将项目级 Skill bundle 拆分为轻量 `SKILL.md`、`skill.contract.yaml`、`schemas/*.input.yaml` 与按需资源读取，统一 v2 Skill 走显式 `skill.*` node，并删除 v1 manifest auto_run 注册/执行路径 |
| Rust 化 Runtime 模块评估 | `docs/prd/backend/16-Rust化Runtime模块评估PRD.md` | 评估主体 runtime substrate、Skill/MCP、storage/event、artifact 与 deterministic kernel 的 Rust native 下沉边界；不针对单个业务 Skill 做专项优化 |
| MCP 长任务与流式 SSE | `docs/prd/backend/17-MCP长任务流式SSEPRD.md` | 将 MCP Runtime 从单条 SSE 兼容升级为完整长任务流式 SSE、断线恢复、progress、task status、取消与 API/SSE 事件桥接 |
| 失败自检、恢复与 Fallback 控制层 | `docs/prd/backend/18-失败自检恢复与Fallback控制层PRD.md` | 节点异常归一、retry/timeout、SSE 重连、artifact 重试、upload warning、审计隔离、sidecar bounded retry 与 LLM provider fallback 策略 |
| 表格上传编码兼容与表头规范化分步实施 | `docs/prd/backend/table-upload-normalization/README.md` | CSV / JSON / Excel 上传编码兼容、表头技术噪声清洗、Excel sheet 选择 interrupt、prompt-safe 摘要上限与 Skill artifact 规范化输入 |
| 对话文件本地资源文件系统 | `docs/prd/backend/20-对话文件本地资源文件系统PRD.md` | 对话上传文件本地持久化、`index.md` 文件索引、Skill workspace manifest / mount_path 与删除清理语义 |
| 对话文件历史与智能选择分步实施 | `docs/prd/backend/conversation-file-history-selection/README.md` | 将合并后的 PRD 21 拆成数据模型、上传历史、memory 安全、selector shadow、interrupt 绑定、灰度发布六个可独立验收阶段；阶段零至阶段五已实施，父兼容入口保留在 `docs/prd/backend/21-对话文件历史与智能选择PRD.md` |
| Skill 运行闭环 Workbench 分步实施 | `docs/prd/backend/skill-workbench/README.md` | 将 Workbench 总纲拆成 Policy/runtime state/stage placement、内部 capability/executor、runtime loop/finalizer/Skill refinement、事件 graph prompt 脱敏、contract quality diagnostics 五个阶段；父兼容入口保留在 `docs/prd/backend/22-Skill运行闭环Workbench总纲PRD.md` |
| 能力缺失 LLM fallback 披露 | `docs/prd/backend/23-能力缺失LLMFallback披露PRD.md` | 无匹配 Skill/MCP/capability 时由 Planner/Replanner 标记 fallback，任务 completed 停止 Workbench，并通过正文、事件、metadata 和前端 notice 披露事实；父兼容入口保留，实施拆分见 `docs/prd/backend/capability-missing-fallback/README.md` |
| 失败自检、恢复与 Fallback 控制层分步实施 | `docs/prd/backend/failure-recovery/README.md` | 将 18 总纲拆成节点执行保护壳、前端恢复、审计/Sidecar、LLM provider fallback、端到端 rollout 五份可独立实施 PRD |
| PostgreSQL State Platform 防死锁与写队列 Phase | `docs/prd/backend/postgresql-state-platform/README.md` | 将生产级 PostgreSQL 状态平台拆为 driver/contract、schema/write queue、handler/read store/service、runtime/observability、SQLite migration/cutover 五个可独立验收 Phase |
| 大语言模型提示词信封分步实施 | `docs/prd/backend/prompt-envelope/README.md` | 将 prompt 组装拆成测试基线、核心模型、主代理迁移、记忆候选、工具信息分层、多调用场景档案、消息原生运行时、供应商缓存八个可独立验收阶段 |
| MCP Runtime 联合改造 Phase | `docs/prd/MCP/README.md` | 把 MCP 长任务流式 SSE 与 Rust MCP sidecar 作为同一最终交付目标拆成 Phase PRD |

## 5. 当前已定的关键决策摘要

### 5.1 主框架共性决策

- 当前只写后端主代理框架 PRD，不先展开前端与具体功能 Agent 产品实现。
- capability 是稳定能力契约；agent 是执行实体；tool 是底层操作接口。
- 子代理执行采用混合模式：优先专用任务型 Agent，必要时允许受限 ReAct Worker。
- 同一 `conversation_id` 内任务串行执行。
- 主代理采用受规则、状态机与完成判定约束的编排型闭环，而不是自由试错式纯 ReAct。
- 任务优先级采用“两层模型”：控制类动作独立最高优先级；普通任务按来源驱动排序，并允许少量结构化权重作为同类内排序依据。
- 主框架与 capability 是明确上下级关系：主框架只管拆解、编排、分发；数据查询 Skill 等 capability 只管各自执行。

### 5.2 协作与生命周期决策

- 结构化 mailbox 采用统一信封 + channel + typed payload 模型。
- mailbox 生命周期采用 **分级 ACK**。
- 强 ACK 用于控制类 / interrupt 类消息；轻 ACK 用于普通协作类消息。
- 停止处理的正式语义是终止 task context，而不是直接定义为“杀线程”。

### 5.3 状态存储决策

- PostgreSQL State Platform 的生产化已拆分为专题 Phase PRD：Phase 0 driver/contract、Phase 1 schema/write queue kernel、Phase 2 command handlers/read store/StateService、Phase 3 runtime integration/fail-closed/observability、Phase 4 SQLite -> PostgreSQL migration/cutover；Phase 0-3 不执行数据迁移，Phase 4 单独处理 migration / cutover / rollback。
- 主框架状态不落公司业务 MySQL。
- 本地先 SQLite，同构迁移到 PostgreSQL。
- PostgreSQL 存结构化状态与索引，不直接存大对象正文。
- PostgreSQL DDL 采用 ORM Model + migration 生成；一期索引策略为基础索引 + 少量关键增强索引。

### 5.4 数据查询 Skill 决策

- 数据查询 Skill 是一期首个 MVP 样例，当前已迁移为可移除项目级 Skill bundle：外部只暴露 `skill.data_lookup`；领域 runtime、配置与测试归属 `skill/<domain-query>/`，系统 runtime 只保留 generic Skill loader。
- 数据查询 Skill 只允许只读查询；MySQL 只读执行必须通过 SQL Guard 通过令牌后才能执行。
- MySQL 连接串与只读账号只允许通过本地 `config.yaml` 或部署环境变量注入，不得在仓库内硬编码；仍保留 SQL Guard 作为数据库权限之外的第二层保护。
- 数据查询 Skill 的 SQL 生成与结果筛选默认可接入 LLM；当前 `skill.data_lookup` domain engine 尾阶段为 result filtering，负责从 `LIKE` 召回候选中筛掉不符合用户真实需求的行，并把筛选后的表格交给主代理整合。

### 5.5 主代理与 LLM Runtime 决策

- `capability_id=None` 的普通消息默认进入 `main_agent.respond` 或由 LLM Planner 选择公开能力；显式 数据查询 Skill 入口使用 `skill.data_lookup`。
- 主代理可读取 Agent Skill 兼容的 `SKILL.md` 元数据、上传 artifact 脱敏上下文与受控脚本输出，用于构造提示词。
- 主代理真实 LLM provider 必须通过可测试 seam 绑定；自动化测试默认使用 fake / injected stream，真实 provider 只在显式配置或手工 smoke 中验证。
- 主代理与 数据查询 Skill 的 LLM 审计事件不得记录 API key、完整 prompt、完整 rows、base_url 等敏感信息。


### 5.6 对话记忆与上下文压缩决策

- v1 记忆系统定位为 conversation 内会话延续型记忆，不做跨会话长期用户画像或知识沉淀。
- 对话记忆上下文注入 LLM Planner / 自动规划阶段与 `main_agent.respond` 最终回答阶段，保证追问、省略主语和纠错能正确影响路由与回答。
- 数据查询 Skill 内部 LLM 节点暂不直接消费完整对话记忆；如需上下文补全，应先在 public 规划层把当前轮问题合成为明确问题。
- 记忆压缩采用两级策略：Level 1 删除 capability 业务中间产物；Level 2 对较早对话历史做摘要压缩并保留最近若干轮原文。
- 记忆上下文必须按 account / conversation 隔离，并禁止注入 SQL、guard token、schema DDL、完整 rows、完整 prompt、API key、base_url 等敏感或高成本内容。

### 5.7 Skill 输出文件与下载决策

- Skill 生成的 HTML、CSV、XLSX、PDF、图片等文件必须由平台统一收集为 managed artifact，不能由 Skill 暴露本地路径或自定义下载接口。
- 下载入口必须复用 task / conversation owner 鉴权，前端只使用 `artifact_id` / `download_url`，不得看到服务器真实路径。
- 输出文件内容默认不进入主代理 prompt；prompt 只注入文件名、类型、大小、摘要等安全 metadata。
- v1 HTML 文件默认按附件下载，不作为站内可信页面直接 inline 渲染；未来如需预览应单独设计 sandbox / CSP。

### 5.8 Skill 一等 Capability 能力池决策

- 项目级 Skill 应可升级为 `skill.*` public capability，进入与 `main_agent.respond` 相同的 `CapabilityRegistry` public 能力池。
- Planner / Runtime Replanner / `/api/v1/capabilities` 必须从同一 public capability pool 发现公开 Skill，避免深度思考阶段看不到已注册 Skill。
- v1 推荐采用 “Skill public macro → `main_agent.respond` forced skill” 模型：LLM 只选择 `skill.*` capability，系统注入可信 forced skill metadata，继续复用主代理受控 Skill runtime。
- 后续结构化 / 脚本型 / 项目级可信 Skill 应按 `docs/prd/backend/15-SkillExecutor实现需求PRD.md` 演进为 generic Skill Executor 执行模型，forced `main_agent.respond` 仅作为兼容路径。
- 默认只公开仓库项目级 `skill/` 下的 Skill；用户级 `~/用户级本地 Skills 目录` 不默认公开给业务 Planner 或 API。

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

- Skill Executor 是通用执行壳，不承载 数据查询 Skill、数据分析、报告生成等业务逻辑；业务语义必须放在 Skill 包、领域服务或 MCP tool 背后。
- `skill.*` capability 的执行必须按 Skill bundle revision 固定版本，避免新聊天热刷新影响运行中任务。
- script Skill 应由 generic Skill Executor 执行并归一化为 `CapabilityExecutionResult`、artifact、event 与 audit；不应长期依附 `main_agent.respond` 私有脚本路径。
- service binding 必须采用“manifest 声明 + runtime allowlist”双重授权；普通 public Skill 和用户级 Skill 默认不能获得 MySQL readonly、内部 LLM、secret 等受控资源。
- Skill Executor 与 MCP Tool Executor 对等，分别承接 `skill.*` 与 `mcp.*` 能力来源；orchestration 不应再为具体业务 Skill 写特判。
- 数据查询 Skill 后续迁移为 `skill.data_lookup` 必须先满足 Skill Executor 的受控执行、service binding、artifact/event 归一化与安全审计要求。

### 5.12 表格上传编码兼容与表头规范化决策

- 表格上传兼容应位于系统上传 / 解析层，不应要求每个 Skill 处理 BOM、编码、Excel sheet 或脏表头。
- 原始上传 bytes 与 `sha256` 必须保留；执行专用 `skill_artifacts` 使用规范化后的 UTF-8 CSV / JSON 内容。
- 表头只做技术噪声清洗，例如 BOM、外层成对引号、不可见字符、首尾空白和全角空格；不做 `材料编号 -> ped_id` 等业务语义映射。
- CSV / JSON 应支持常见文本编码候选；`.xlsx` / `.xls` 应通过受控依赖读取并转为规范化 CSV。
- 多 sheet Excel 不自动猜测或合并，必须通过 interrupt / resume 或显式 metadata 选择 sheet 后再执行。
- 该能力不得修改 `skill/**`，不得把完整文件内容写入 prompt、SSE、对话记忆或普通 audit；prompt-safe 上传摘要必须有列数、sheet 数和清洗映射上限。
- 该专题已拆为 `docs/prd/backend/table-upload-normalization/` 下的父总纲与阶段 PRD，实施时必须按阶段门禁推进。

### 5.13 对话文件本地资源决策

- 用户上传文件不是 RAG 输入，而是 conversation-scoped 本地文件资源；Skill 脚本应读取 workspace 中的真实文件副本。
- DB 中的 `conversation_file_resource` 是权限、状态、分页和重建索引的事实源；每个 conversation 目录下的 `index.md` 是面向人和模型的物化投影。
- 上传 API、列表 API、删除 API 和 `metadata.upload_ids` 保持向后兼容；响应只允许增加旧前端可忽略字段。
- 新 Skill 优先读取 `resource_manifest_path` 与 `files[].mount_path`；旧 Skill 的 `uploaded_artifacts[].content` / `content_base64` 只作为兼容层保留。
- 单文件删除必须标记 DB `deleted` 并物理删除对应本地资源目录；conversation 删除必须清理该 conversation 文件目录。
- 图片文件上传阶段不自动生成描述或 OCR；PDF 后续可接受控文本抽取 / OCR adapter，但失败不得阻塞文件作为 Skill 输入。

### 5.14 对话文件历史与智能选择决策

- 该专题已拆为 `docs/prd/backend/conversation-file-history-selection/` 下的总纲与阶段 PRD，实施时必须按阶段门禁推进；原 `docs/prd/backend/21-对话文件历史与智能选择PRD.md` 保留为父兼容入口。
- `ConversationFileResource` 是 active/deleted、权限、分页和 selector candidate 的事实源；`file_upload` message 只作为 conversation history 中的上传事件快照和展示入口。
- 上传接口成功即写入 `message_type=file_upload` 的结构化历史消息，记录 `filename`、`upload_id`、`description_summary`、`description_status` 与 `file_status`；上传成功定义包含原始文件、DB resource、file_upload message 和最新 `index.md`。
- `file_upload` 使用 `role=system`，但历史 API、前端和 memory 只能通过 public `message_type` allowlist 暴露该类 system message；不得泛化展示或注入其他 internal system message。
- 文件名、摘要、preview/OCR/PDF 文本全部视为不可信 file-derived data，只能作为历史事实和文件定位线索，不能覆盖系统指令或安全约束。
- 当前 conversation active 文件可作为默认文件上下文；task attachment 只记录显式上传、selector 选择、interrupt answer、sheet selection 等本轮实际 provenance，避免把“文件池存在”误记为“本轮已使用”。
- selector 是 conversation file context 之上的缩窄、消歧、缺文件和 provenance 写入机制；显式 `metadata.upload_ids` 优先，普通问答不强制 selector，但 required file、明确单文件指代、同名/多候选缩窄、recent usage continuation、interrupt answer 恢复或正文 `upload_id` 精准选择不得被 active context 短路。
- 多候选、同名文件、低置信或 required file 缺失时复用现有 interrupt，使用 `file_selection_ambiguous` / `no_files_in_conversation` 等稳定 reason_code，不新增公开 API 或前端点选组件。
- 正文 / interrupt answer 中的 `upload_id` 精准选择只接受当前生成格式 `upl-` + 12 位十六进制字符的完整 token；未知、越权、deleted 或不属于当前 conversation 的 id 不得交给 LLM 猜测或静默忽略。
- recent usage 必须来自 task attachment / selector binding / interrupt answer / sheet selection 等实际使用 provenance，不得只根据上传时间推断。
- deleted 文件保留为历史事实，但必须在 API、前端卡片和 prompt 中标记不可复用，且不得进入 active context、selector、binding 或 Skill manifest。
- `index.md` 是 DB 投影而非权限事实源；重写失败必须写 DB durable repair marker，并按当场重试、后台退避、下次访问懒修复恢复；repair pending 时 selector / rollback 都必须以 DB resource 为准，不得信任旧 index。
- selector rollout mode 只接受 `disabled`、`shadow`、`enforce_narrow`、`enforce_guarded_multi`；旧 `enforce` 不保留兼容 alias，运行时遇到非法值必须 fail-closed 到 `disabled` 并记录 `conversation_file.file_selector_config_invalid` audit。
- `disabled` 回滚模式停止 selector attachment 与 selector audit，但保留 active conversation file context、`file_upload` history 展示和 deleted 不可复用约束；`index.md` repair pending 时继续以 DB resource 构造 active context / selector candidates，不得信任旧投影。
- guarded multi-select 默认关闭；只有 `enforce_guarded_multi` 且 allow_multiple / 明确比较合并意图成立时才允许自动多绑定，audit 必须区分 `multi_select_auto_bound` 与 `multi_select_confirmed_by_user`。
- 未来 Skill 文件需求必须由 contract/schema 的 `file_selection` 最终字段驱动，平台不得硬编码当前 Skill 名称；不得接受 `file_intent`、旧 schema `type: file/artifact/data` 或别名字段作为交付契约；实施时必须同步更新 `breeding-skill-builder` 的模板、checklist 和指南。
- 第一阶段不 backfill 旧文件，避免伪造历史上传时序；旧 active resources 仍可通过文件池和 selector 使用。

### 5.15 Rust 化 Runtime 决策

- 主体框架 Rust 化不应为任何具体业务 Skill 重新引入 native capability、专属 route、专属 executor 或前端协议。
- `ApiRuntime` 不作为整体迁移对象；应把 task dispatcher、event log、bundle revision pinning、cancellation token、storage lease 等 runtime substrate 抽成 Rust sidecar / kernel。
- 优先 Rust 化确定性、安全敏感、并发敏感和可重放模块：`src/core/` contract、`src/lifecycle/` 状态机、`src/storage/` durable store、通用 Skill runtime trust gate、MCP protocol/runtime、artifact/upload/file safety。
- LLM provider glue、FastAPI route、DTO、主代理 prompt 产品语义和前端 UI 不应整体 Rust 化；只在 sanitizer、token budget、大 payload 处理等热点处抽小 kernel。
- Skill-owned Rust runtime 必须放在各自 Skill bundle 内部，并按 `git@gitee.com:biobin/breeding-skill-builder.git` 的 `references/Skill构建指南.md` 的 Rust 型 Skill runtime 限制适配框架 contract；框架不反向兼容某个 Skill 的任意 Rust 形态。

## 6. 当前验收基线与归档证据

一期范围内承诺的“主代理最小内核 + 数据查询 Skill 只读 MVP + FastAPI/SSE/cancel/query API”已完成；该结论仅覆盖一期冻结范围，不包含 PostgreSQL 正式化、第二 capability、长期记忆专题、跨任务知识沉淀、主代理 / 通用子代理 LLM 化等后续增强主题。

| 验收口径 | 证据 | 结论 |
|---|---|---|
| 能提交任务 | `tests/api/test_message_submission.py`、`tests/e2e/test_data_query_happy_path.py` | 通过 |
| 能观察状态和事件流 | `tests/api/test_task_query.py`、`tests/api/test_task_events_sse.py`、`tests/e2e/test_data_query_happy_path.py` | 通过 |
| 能取消任务 | `tests/api/test_task_cancel.py`、`tests/e2e/test_cancel_late_result_ignored.py` | 通过 |
| 会话延续型记忆最小字段可被持久化并恢复 | `tests/storage/test_sqlite_conversation_repository.py`、`tests/storage/test_sqlite_task_repository.py`、`tests/storage/test_sqlite_interrupt_repository.py` | 通过 |
| 能跑通 数据查询 Skill 只读链路 | `skill/<domain-query>/tests/` | 通过 |
| 能阻断危险 SQL | `skill/<domain-query>/tests/` | 通过 |

关键验收链路：

- Happy path：提交消息后生成 DAG，SQL Guard 通过，只读执行完成，summary / artifact 落地，事件流收敛为 `task.completed`。
- Guard blocked：危险 SQL 在 `skill.data_lookup` 内部 guard 阶段被阻断，任务收敛为 `failed`；审计记录保留 `block_reason` 与脱敏 `route_context`，前端只展示安全失败提示。
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
- 具体数据类 Skill 的数据库结构说明由对应 Skill bundle 自带 docs 维护。
- 具体数据类 Skill 的 prompt 输入模板由对应 Skill bundle 自带 docs 维护。
- 对话上下文记忆与压缩 PRD：`docs/prd/backend/10-对话上下文记忆与压缩PRD.md`
- Skill 输出文件 Artifact 与下载 PRD：`docs/prd/backend/11-Skill输出文件Artifact与下载PRD.md`
- Skill 一等 Capability 能力池 PRD：`docs/prd/backend/12-Skill一等Capability能力池PRD.md`
- Skill 动态加载与热部署 PRD：`docs/prd/backend/13-Skill动态加载与热部署PRD.md`
- Skill Contract 渐进式披露与显式执行 PRD：`docs/prd/backend/skill-contract-progressive-disclosure/README.md`
- MCP Runtime 实现需求 PRD：`docs/prd/backend/14-MCPRuntime实现需求PRD.md`
- Skill Executor 实现需求 PRD：`docs/prd/backend/15-SkillExecutor实现需求PRD.md`
- Rust 化 Runtime 模块评估 PRD：`docs/prd/backend/16-Rust化Runtime模块评估PRD.md`
- MCP 长任务与流式 SSE PRD：`docs/prd/backend/17-MCP长任务流式SSEPRD.md`
- 失败自检、恢复与 Fallback 控制层 PRD：`docs/prd/backend/18-失败自检恢复与Fallback控制层PRD.md`。
- 表格上传编码兼容与表头规范化 PRD：`docs/prd/backend/19-表格上传编码兼容与表头规范化PRD.md`。
- 对话文件本地资源文件系统 PRD：`docs/prd/backend/20-对话文件本地资源文件系统PRD.md`。
- 对话文件历史与智能选择兼容入口：`docs/prd/backend/21-对话文件历史与智能选择PRD.md`。
- 对话文件历史与智能选择分步 PRD：`docs/prd/backend/conversation-file-history-selection/README.md`。
- Skill 运行闭环 Workbench 兼容入口：`docs/prd/backend/22-Skill运行闭环Workbench总纲PRD.md`。
- Skill 运行闭环 Workbench 分步 PRD：`docs/prd/backend/skill-workbench/README.md`。
- 能力缺失 LLM fallback 披露兼容入口：`docs/prd/backend/23-能力缺失LLMFallback披露PRD.md`。
- 能力缺失 LLM fallback 披露分步 PRD：`docs/prd/backend/capability-missing-fallback/README.md`。
- 统一同模型 Agent Loop 分阶段 PRD：`docs/prd/backend/unified-agent-loop/README.md`（总纲与8篇阶段PRD逐篇100/100、实施计划99/100通过；Phase 0进行中且P0-A green；Phase 6完成前当前DAG仍是已实现运行时基线）。
- 失败自检、恢复与 Fallback 控制层分步 PRD：`docs/prd/backend/failure-recovery/README.md`。
- Rust 化实施专题拆分入口：`docs/prd/rust/README.md`
- MCP Runtime 联合改造 Phase PRD：`docs/prd/MCP/README.md`

## 8. 使用建议

- 做全局规划时先读本文件。
- 做局部设计或开发计划时优先读取对应专题文档。
- 做具体数据类 Skill 实现或提示词设计时，应读取该 Skill bundle 自带 docs；系统级 PRD 只描述 generic Skill loader / Executor 边界。
- 做前端设计时，不要把前端范围追加到本文件；应在 `docs/prd/frontend/` 新建独立 PRD，并引用本目录中的后端接口和事件契约。

## 9. 后续专题设计与演进项

以下事项不阻碍当前 PRD 作为正式基线，但建议在后续专题设计中继续细化：
- PostgreSQL 最终 DDL 文件生成方式与索引增强细节。
- PostgreSQL 部署完成后的索引优化、JSONB 查询策略与正式 DDL 生成流程。
- 任务优先级权重的更细粒度策略。
- Schema Context Builder 的更强评估样例与调优工具。
- 前端 PRD：对话界面、任务流、事件流、数据查询 Skill 结果与主代理 Skill 命中状态展示。
