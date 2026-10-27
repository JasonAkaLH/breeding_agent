# 文献知识库原生 Capability 实施计划

## 1. 状态与完成口径

- 日期：2026-10-16。
- 最近修订：2026-10-27，第 8 节保留已确认的 Markdown/PDF 双格式设计摘要；新增独立的[双格式完整交付实施计划](2026-10-27-literature-knowledge-pdf-implementation-plan.md)作为后续执行入口。
- 状态：`implemented_pending_format_extension_and_external_acceptance`。原 Markdown 仓库代码已接入，PDF 扩展尚未实施；逐项既有证据和外部验收缺口见[实施验证记录](2026-10-16-literature-knowledge-implementation-evidence.md)。不表示真实环境验收或部署已完成。
- 产品依据：[文献知识库原生 Capability PRD](../../prd/backend/24-文献知识库原生CapabilityPRD.md)，包含结果复用、压缩正文载入及 2026-10-27 双格式修订。
- 代码基线：`main@c61b6ee`，以及当前工作区已获批的知识库 PRD 修订。实施前重新检查 HEAD 和差异。
- 交付范围：PRD 的 FR-01 至 FR-18、NFR-01 至 NFR-05、AC-01 至 AC-33。原 AC-01 至 AC-29 的实施记录仍有效但不能证明新增 PDF 范围；第 8 节补齐格式扩展，必须复验受影响的旧验收项。下列检查点只是开发顺序，不构成不同产品版本，不把已确认功能移到后续。
- 原 Markdown 计划已获授权实施；其实施仅使用隔离测试数据、复用现有依赖，未执行开发或生产数据库升级或部署镜像。新增 PDF 设计已确认，独立接续实施计划已经成文，尚未修改 PDF 业务代码或安装解析依赖。

完整交付必须同时满足：用户能建库、混合批量上传 Markdown / 文字型 PDF、人工确认、维护及只读共享；会话能保存选库并固定本轮范围；Agent 经独立 `knowledge` 类型完成描述选库、索引选文献、阅读 Markdown 正文和引用作答；长文分页、压缩、撤权、更新清理及重启恢复均符合验收。

历史答案是普通聊天文本，不建立答案到文献的运行时依赖。不增加 LlamaIndex、PageIndex、embedding、向量数据库、扫描 PDF 的 OCR、共享编辑者、团队管理或独立问答 Agent Loop。原第 0～G 检查点记录 Markdown 范围；PDF 扩展以第 8 节为准。

## 2. 现有代码依据与改动边界

| 现有入口 | 当前事实及实施影响 |
|---|---|
| `src/orchestration/models.py`、`registry.py` | 已有 CapabilityDescriptor 和可信上下文过滤；为三个固定知识库动作登记 `kind=knowledge`，不按文献库注册工具。 |
| `src/capabilities/mcp_dispatch/registration.py`、`src/orchestration/composite_executor.py` | 已有本地实例和 Executor 组合方式；知识库沿用该装配方式，执行逻辑独立。 |
| `src/orchestration/agent_loop/tool_catalog.py` | 当前筛选 Skill 与 `mcp.dispatch`；需要增加知识库发现、参数白名单和本轮范围输入。 |
| `src/orchestration/agent_loop/capability_invoker.py` | 同参成功结果会在 Invocation 前复用；知识库的新读取必须绕开该捷径并重新检查当前状态。上下文缓存是内存对象，不能承担范围持久化。 |
| `src/orchestration/agent_loop/result_projection.py`、`context.py`、`context_preflight.py`、`compaction.py` | 结果投影、模型正文装载、实际预算和压缩是不同入口；均须接入知识库引用，不能借用 Skill stage 或 Artifact 生命周期。 |
| `src/api/runtime.py`、`submission_admission.py`、`src/storage/runtime_sidecar_facade.py` | submit、prepared handoff 和恢复已存在；prepared v1/v2 与 execution_metadata 严格闭合。选库快照放在知识库领域存储，通过已有任务身份恢复，不向这些信封随意加字段。 |
| `src/storage/sqlalchemy_models.py`、`sqlite/`、`postgres/`、`src/state/postgres/runtime_schema.py` | SQLite/PostgreSQL 使用共享模型；PostgreSQL 有 manifest 和 schema reconciliation，新增表必须同步这些入口。 |
| `src/storage/conversation_files.py`、`path_safety.py`、`artifact_files.py` | 可借鉴安全路径、原子发布和哈希校验；知识库文件独立保存，不采用会话文件或公共 Artifact 的删除生命周期。 |
| `src/integrations/llm_runtime.py`、`llm_request_options.py`、`token_counter.py` | 复用现有模型调用和 token 计数；全文提取与问答均不另建模型 SDK。 |
| `src/api/app.py`、`routes/conversations.py`、`auth.py`、`upload_runtime.py` | 复用已认证用户、会话所有权和路由挂载方式；已有上传可在聊天前创建会话，选库 API 也需覆盖首条消息前的所有者绑定。 |
| `frontend/src/App.tsx`、`api/client.ts`、`api/types.ts`、`components/MarkdownText.tsx` | 账户设置当前打开 MCP 设置；增加知识库入口、管理页面和选库控件，复用安全 Markdown 展示。 |

以下新文件是实施定位建议，可在同一职责内合并，不为凑目录拆分文件：

- `src/capabilities/knowledge/`：领域对象、授权、库与文献服务、提取处理、工具执行器和注册。服务同时供页面 API 与 Agent 只读执行器调用。
- `src/storage/knowledge_repository.py`、`knowledge_files.py`：共享 SQLAlchemy 知识库存储和独立受管 Markdown 存储。
- `src/api/routes/knowledge.py`、`knowledge_dto.py`：管理与选库 API；`runtime.py` 只承担装配及既有任务交接处的必要接线。
- `src/orchestration/agent_loop/knowledge_results.py`：知识库结果引用验证与模型视图装载，供普通上下文和压缩共同使用。
- `frontend/src/components/KnowledgeSettingsPanel.tsx`、`KnowledgeLibrarySelector.tsx`：管理界面与选库控件；只在需要时抽出账户设置入口组件。

不修改外部 Skill、MCP Server、认证规则和受保护的 `docker_cmd/` 文件。Rust wire 和 Task authority 不因知识库新增而迁移；需要覆盖现有 Sidecar 模式的端到端恢复验证。

## 3. 实施选择

本节是基于现有代码作出的实施选择；产品行为仍以 PRD 为准。

### 3.1 存储与并发

结构化信息沿用共享 SQLAlchemy metadata。知识库仓库从 runtime 已有的 session factory 装配，避免为新领域另建数据库或继续扩大通用 StoragePort。SQLite/PostgreSQL 共用业务事务逻辑，仅在行锁、写事务和冲突映射处保留必要的方言差异。

| 记录 | 最少需要承载的内容 |
|---|---|
| KnowledgeLibrary | 所有者、名称、描述、活动或删除状态、并发修订号、目标索引修订号、索引修复和库清理状态。 |
| KnowledgeDocument | 库与文献 ID、已审核元数据、当前正文引用与 SHA-256、并发修订号、删除状态及尚未完成的清理信息。 |
| KnowledgeImport | 原始上传与 SHA-256、可选替换目标、提取状态与字段、人工确认状态、发布进度、失败原因及处理 claim。 |
| LibraryGrant | 库 ID、subject_type、subject_id、reader；所有者权限来自库记录。 |
| ConversationKnowledgeSelection | 会话 ID、auto 或 selected、已选库 ID、并发修订号。缺省为 auto。 |
| KnowledgeQueryScope | 固定范围的领域快照：root_message_id、已认证所有者、会话 ID、模式、指定库 ID、提交时可用的候选库 ID。 |

KnowledgeQueryScope 是实现 FR-13 的内部存储，不增加用户可配置概念。它不包含正文，也不成为第二份 Task authority；不对 Sidecar 模式不存在的 SQL Task 行建立外键。

同库上传、确认、替换和删除通过同一库的短事务串行化校验活动文献与草稿的 SHA-256；PostgreSQL 使用行锁，SQLite 使用写事务或等价 CAS。配合各记录的唯一约束，覆盖跨表及并发判重，不仅分别在两张表上建立唯一键就结束。模型调用和长文件 I/O 不持有数据库锁。

### 3.2 文件与发布顺序

为知识库设置独立持久根目录，默认位于现有运行数据目录下的 `knowledge/`，支持 runtime 显式配置。多 worker 必须访问同一数据库和同一持久根目录。原始文件不进入会话目录、公共静态目录或 Git。

目录仅由可信库 ID、文献或导入 ID、当前 SHA-256 定位；文件名作展示字段。原始字节原样存储和下载，UTF-8 文本视图供提取、分页及预览使用；原始存储不归一化 BOM、CRLF、空格或 Unicode。复用现有安全路径、regular-file 检查、私有权限、临时写入、原子替换和必要的 fsync 做法。

确认及替换采用可恢复的固定顺序：

1. 保留已人工确认的导入状态；先写好并验证候选正文，文件引用可由导入记录恢复。
2. 短事务内校验目标修订号与同库判重，切换当前正文及元数据、推进库索引修订号，同时记录待清理的旧受管引用。
3. 从数据库当前已入库集合生成完整索引，检查生成对应的修订号后原子发布。并发修改后不能用较旧生成结果覆盖较新索引。
4. 清理旧正文、相关暂存及全文缓存；全部完成后才标记本次操作完整成功。

新导入在正式发布前不进入可读集合；替换草稿待确认时，旧正文继续可读。切换后任何旧引用都不能通过读取接口重新寻址旧文件。索引待修复时读取应修复或明确失败，不能返回过期内容。正在清理的引用只用于恢复清理，不提供历史版本访问。

删除先在数据库标记不可读并推进索引修订号，再处理索引和文件。清理完成前保留必要状态，不先级联删除恢复所需记录。清理失败显示未完成并重试；删除会话只清理选库偏好和对应查询范围，不删除知识库。

### 3.3 异步提取与恢复

新增一个知识库处理服务，由现有 ApiRuntime.start/shutdown 管理。持久状态在导入或文献、库记录上，进程内 asyncio 队列只作唤醒优化；不引入 Celery、Redis、通用任务平台或第二套 Agent Loop。

处理服务以数据库 claim、修订号和有界并发取得待处理记录。重启后扫描未完成提取、发布、索引修复及清理工作，失效 claim 可重新取得。提取结果提交前复验导入状态及原文 SHA-256，防止丢弃草稿或删除库后被迟到结果重新发布。

提取复用已配置的主 Agent 模型及 LLMRuntime.generate_text，记录所用 model edition。大文件分段覆盖全文后汇总结构化结果，分段产物只留临时内存或可清理的受管临时区；只持久化审核所需元数据和简述。结构化输出按模型校验，不确定字段留空，缺标题或简述由所有者补全。失败支持手动重试及人工填写，不自动绕过审核。

### 3.4 本轮范围的固定与恢复

对话页将当前会话的选库保存成功作为新问题提交的前置条件。同一会话的选库写请求按用户操作顺序处理；发送入口先等待最近一次选择保存成功，再提交消息。保存失败时保留输入并阻止发送，不静默改用旧选择或 auto。已经提交的问题仍使用原快照，后续选库变化只影响下一次提问。

在 submit_message 已完成身份和会话可访问性校验、进入 admit_submission 前，为本次 message_id 创建或读取 KnowledgeQueryScope：

- 在知识库数据库事务中读取会话选库及当前授权，保存首次快照；没有会话偏好时按 auto 处理，适配首条消息尚未创建 Conversation 的情况。
- 同一 root_message_id 的重试、并发和恢复读取同一快照，并验证所有者和会话相符；不以每次尝试新生成的候选 task_id 作唯一身份。
- 快照写入失败时不准入该次新提交。快照存在不代表 Task 已准入；仍由现有 admission 决定接受、busy 或冲突，不暗写 SQL Task。
- 按正式 Task 的 root_message_id 查找快照，给新建、初始化中断恢复及已有 Run 恢复提供相同范围。内存 AgentInvocationContextStore 仅缓存装载结果。
- 不扩展严格的 prepared execution_metadata；在执行请求和 visibility context 构造处从可信快照补入知识库范围。模型和客户端 metadata 不能伪造该上下文。
- 当前会话偏好变化、后来新建或新共享的库，不改变本轮候选 ID；授权撤销和库删除仍即时约束实际读取。
- 升级前没有快照的旧 Run 沿原工具合同恢复，不给它重新计算“全部库”；新提交必须有快照。快照不可用时知识库读取明确不可用，禁止扩大范围。

指定模式保留原指定 ID 以报告失效项，实际只使用固定候选与当前权限的交集。部分失效提示并继续；全部失效要求重选。auto 的候选为空时可以明确说明后按通用知识回答。普通聊天历史继续作为普通消息，不承担当前文献证据。

### 3.5 只读工具、引用与预算

登记三个固定动作：knowledge.list_libraries、knowledge.read_index、knowledge.read_document。执行器只接受公开 schema 中的库或文献 ID、游标，身份及范围来自可信执行上下文。所有动作走同一个 LibraryAccessService；不允许模型调用管理服务。

新知识库读取不沿用已有的同参成功结果复用捷径；同一 Run 内重复调用也要检查当前范围、授权和资源状态。无旧游标的重读返回当前内容；游标包含对应资源与修订号或 SHA-256、覆盖范围，版本不符明确返回变化状态，不自动拼接新版本。

工具持久化结果只保存有界的资源引用、书目信息和实际读取范围。新增独立知识库结果合同，正文在 context 与 compaction 请求构建时按同一验证规则装载；装载复验范围、授权、当前哈希及范围边界。预期的删除、撤权或内容变化转为模型可识别的不可用状态，不套用旧 Skill Artifact 缺失导致整轮上下文失败的处理；失效来源不妨碍继续使用其他有效库。引用结构损坏、身份不符仍明确拒绝。

分页以连续文本范围保证无漏行、不切断 UTF-8 字符。每页按实际正文、元数据和 envelope 计数，适配 50,000-token 工具预算、Run 固定上下文预算与 131,072-byte 持久化 payload 合同。同批尚未消费的工具结果也纳入预算；不能只保证单页大小。结果不够一页时缩小本页范围并保留后续游标，不丢弃尾部。预算或模型服务失败明确报告，不假报读完。

普通模型请求和压缩模型请求都展开实际段落后计数。压缩保留与问题有关的文献事实、文献标识和阅读进度；不把压缩摘要或索引简述伪装成新读取的正文，也不建立跨历史答案的引用维护。正文、逐段原文不持久化为 AgentItem 或 Artifact 副本。

提示词指导描述选库、索引选文献、完整阅读、正文引用及通用知识区分。代码负责权限、范围、正文版本、分页、实际元数据和读取状态；回答组织与引用表达通过评测约束，不增加答案改写验证器。

## 4. 开发顺序与检查点

每个检查点先增加能证明对应行为的聚焦测试，再实现并运行相关检查。通过后保存独立 Git 检查点；不提交失败测试，不推送或部署。0 至 F 的仓库接入与自动回归已执行，G 的真实 PostgreSQL、真实文献模型评测仍待外部配置，不能将这些缺口记为通过。

| 检查点 | 依赖 | 主要交付 | 完成条件 |
|---|---|---|---|
| 0 基线 | 无 | PRD 对照、现有回归基线、测试夹具 | 分清已有失败与本功能缺失，无业务改动。 |
| A 存储与授权 | 0 | 领域记录、共享仓库、受管文件、统一授权 | 两种 SQL 后端、并发判重与只读边界通过。 |
| B 入库与维护 | A | 全文提取、人工确认、索引、替换、删除及恢复 | 故障注入后状态与文件一致，无旧全文残留。 |
| C 管理 API | A、B | 建库、文献、草稿、下载、共享 API | owner/reader API 和批量逐项结果准确。 |
| D 会话范围 | A、C | 选库 API、提交快照及恢复接线 | 选择持久化，三种 admission 模式及 Sidecar 恢复不漂移。 |
| E 原生工具与上下文 | B、D | knowledge 注册、只读执行、分页、引用装载、压缩与提示词 | 实际模型请求证明完整查询链、当前权限及跨压缩证据。 |
| F 前端 | C、D、E | 账户设置入口、管理、审核共享与选库 | 管理全流程及刷新、运行中修改、失效提示通过。 |
| G 整体验收 | A 至 F | 29 项验收、真实文献评测、迁移与发布交接 | 所有需求有证据，外部缺口单独列出，方可声明完整实现。 |

### 检查点 0 基线与夹具

预计涉及测试及实施记录，不修改产品行为：

- 记录 HEAD、工作区已有差异、数据库后端、admission 模式、Sidecar 是否启用及本地可运行门禁。
- 读取现有 agent invocation、context、compaction、submission recovery、conversation delete 和 MCP 设置测试，复用其 fake model、仓库夹具和测试身份。
- 准备 UTF-8 Markdown 夹具：同字节异名、仅换行或空格不同、缺书目字段、摘要外才有关键结果、超单次结果预算的索引及正文、包含不可信指令的资料。
- 夹具只用合成或已获准使用的文献；真实私有全文不加入 Git。可由测试生成长文件，避免提交大体积样本。
- 先记录聚焦既有回归结果；未配置真实 PostgreSQL、Sidecar binary 或模型服务时记录缺口，不安装或部署来掩盖缺口。

验证入口：tests/orchestration/test_agent_invocation.py、test_agent_context_builder.py、test_agent_context_preflight.py、test_agent_compaction.py；tests/api/test_submission_admission_recovery.py；tests/storage/test_conversation_file_resources.py；frontend/src/App.test.tsx 和 MCPSettingsPanel.test.tsx。

### 检查点 A 存储、文件与统一授权

预计修改：共享模型、新知识库仓库与文件存储、runtime 装配、SQLite bootstrap、PostgreSQL runtime_schema/manifest/reconciliation 相关测试。

1. 增加第 3.1 节记录及必要 CHECK、唯一键、活动记录索引、CAS 修订号。所有者使用现有规范化用户名，不改登录系统。
2. 明确领域存储的事务方法：建库与更新、草稿登记与判重、人工确认、当前正文切换、失效与清理、索引修订、共享、会话选择和范围快照。
3. 新表沿既有 additive bootstrap/schema reconciliation 创建；同步 PostgreSQL manifest 版本、校验和及 schema 测试，不重建既有业务表，不修改历史 Task/Message 数据。
4. 实现 LibraryAccessService 与 AccessSubjectResolver。当前只解析 user、接受 reader；team 用解析器替身测试未来接入，当前 API 拒绝未知主体类型。
5. 实现独立文件根、安全写读、原始哈希、原子索引和精确删除。禁止用户文件名、绝对路径或游标成为任意存储引用。
6. 授权先不增加缓存，避免失效机制；若复用已有缓存，必须覆盖共享变化后的失效测试。

验证：owner/reader 操作矩阵、跨用户与跨库隔离、共享幂等、未知用户和主体拒绝、同批及跨 worker 判重、已删哈希可重新导入、路径穿越及损坏文件拒绝。两种 SQL 后端均验证唯一性与冲突，不以 SQLite 代替 PostgreSQL 并发证明。

建议新增测试：tests/storage/test_knowledge_repository.py、test_knowledge_postgres_integration.py、test_knowledge_files.py；tests/capabilities/knowledge/test_access.py。PostgreSQL 测试使用 tests/postgres_test_support.py 的模块专用隔离 DSN 规则。

### 检查点 B 全文导入、审核、索引和清理

预计修改：知识库提取与领域服务、文件与仓库事务、ApiRuntime.start/shutdown 的处理服务接线；不把这些管理操作注册成 Agent 工具。

1. 流式保存单篇或批量 Markdown，边保存边计算原始字节 SHA-256。先登记可恢复的上传身份，再完成文件发布；失败不能留下无归属的全文副本。
2. 按同库 SHA-256 返回已有活动文献或草稿；相同标题或 DOI、不同字节不得自动合并。跨库独立复制和维护。
3. 异步提取全文元数据与检索简述；已有字段优先，不编造缺失字段。大文件全部分段处理后汇总，模型和 tokenization 错误进入可见失败状态。
4. 支持草稿编辑、逐篇或批量确认、失败重试、人工补全及丢弃。确认必须具有标题与简述；未确认草稿对 reader 与 Agent 不可见。
5. 实现第 3.2 节发布流程；每次元数据、库名称描述、文献状态变化均更新索引修订。索引包含全体有效文献，不持有独立编辑状态。
6. 实现明确目标的正文替换。同 hash 不创建新正文，与另一条文献 hash 冲突则返回重复；并发编辑与旧确认遇到修订变化返回冲突。
7. 实现文献与整库删除、共享撤销和所有受管正文清理。清理旧正文包含处理临时区和全文缓存，不生成公共 Artifact、备份或回收站。
8. 处理服务按持久状态恢复，前端可区分处理中、待确认、发布或清理未完成、成功与失败。逐项返回批量结果，不把“接受处理”标成“已入库”。

必须注入故障：上传后进程退出、提取期间丢弃或删除、人工确认重复提交、当前正文切换后退出、索引 rename 前后退出、旧文件删除失败、较旧索引迟到、两个 worker 同时确认。每种故障恢复后仅有一个当前正文，索引能修复，旧全文无可访问残留，其他库副本及历史答案不变。

建议新增测试：tests/capabilities/knowledge/test_ingestion.py、test_library_service.py；tests/storage/test_knowledge_recovery.py。正文泄漏检查同时覆盖 AgentItem、Artifact 与项目日志。

### 检查点 C 页面管理 API

预计修改：knowledge 路由与 DTO、app.py 路由注册、runtime 服务接线、API 手册及对应测试。

| 资源或操作 | 必须落实的合同 |
|---|---|
| 库列表、创建、详情及修改、删除 | 我的库与共享给我，返回有效角色；名称必填、描述可空，库与会话生命周期独立。 |
| imports 上传、查询、编辑、确认、重试与丢弃 | 支持多文件与逐篇结果；替换目标显式传入并校验；reader 不可读草稿、不可写。 |
| documents 列表、元数据编辑及删除 | 只展示已入库有效文献；保存后更新索引，修改仍校验必要字段和修订号。 |
| index 读取、content 预览及下载 | 统一授权，返回真实修订与当前原始字节；下载 SHA-256 与记录一致，不返回宿主路径。 |
| grants 查询、新增及撤销 | 只有 owner 能看和维护共享名单；对象必须是已有用户，重复共享幂等，无邀请接受流程。 |

客户端 API 不接受 owner、角色自授或任意文件路径。沿用既有认证依赖及错误风格，区分无权、重复、冲突、提取失败、发布或清理未完成；不把不存在、读取失败与没有证据混为一谈。

验证：完整 owner 流程、reader 直接调用写 API 的拒绝、未授权文件和索引访问、撤权后重复下载、分页与批量部分失败、服务重启后的草稿和管理状态。建议新增 tests/api/test_knowledge_routes.py，并复用 app/runtime 测试装配。

### 检查点 D 会话选库与本轮快照

预计修改：选库路由与 DTO、知识库范围服务、runtime.submit_message 及 AgentExecutionRequest/恢复构造接线、会话删除清理；只在必要处调整 orchestrator 的可信上下文装载。

1. 实现 PRD 的 knowledge-selection API：会话 owner 可读写 auto/selected；selected 至少一个 ID，清空不能解释为 auto，保存时校验权限及结构。
2. 支持首条消息前选库：GET 未持久化会话时返回默认 auto；首次保存偏好时，在同一 SQL 事务内仅插入创建当前用户绑定的空 Conversation 并保存偏好。并发遇到已有会话必须复验 owner/status，不以 merge 覆盖所有者或复活删除中的会话。这沿用已有上传可在聊天前建会话的行为，不依赖只加载历史的 initializeConversationWorkspace，不要求先发送聊天。
3. 落实第 3.4 节首次快照。在一个知识库事务中固定模式和候选 ID；同一 root_message_id 并发只有一个快照，正式 Task 身份从既有 admission 结果取得。
4. 新建、零 Item 初始化恢复、interrupt/resume、普通重试、重启恢复都从同一快照构造可信执行上下文，不从当前偏好补算。
5. 实现 selected 部分失效及全部失效、auto 无可用库的分支；通知用户失效项但不扩大候选。新增共享只在下一提问进入候选。
6. 新建或切换会话、刷新与重连加载各自偏好，删除会话清理偏好及范围快照，不碰文献库。

验证：选择保存、首条消息前选择、选库保存延迟后新提交的快照与最后一次成功选择一致、运行中切库、权限中途变化、同一消息并发重放、快照写入前后崩溃、admission 后 handoff 前崩溃、Sidecar off/shadow/enforce、旧 Run 无快照和新 Run 恢复。特别证明 enforce 模式没有新增 SQL Task 写入。

建议新增 tests/api/test_knowledge_selection.py、test_knowledge_scope_recovery.py；扩充既有 submission admission/runtime recovery 测试。真实 Sidecar binary 路径不能只用 mock 代替。

### 检查点 E 独立知识库工具、分页与模型上下文

预计修改：knowledge executor/registration、composite 装配、tool_catalog、capability_invoker、result_projection、knowledge_results、context、context_preflight、compaction、observability、runtime 提示词和恢复接线。

1. 注册固定三个 knowledge 描述及本地执行实例，catalog 与 policy 明确识别独立 kind。参数只放 ID 与游标，不接收当前用户、所有者或受管引用。
2. 按可信快照限定 list_libraries、read_index、read_document；每次执行及正文装载均走当前授权与状态检查。
3. 实现库描述路由、Markdown 索引读取和全文分页；返回书目信息、哈希或索引修订、实际范围、末段标记与下一游标。
4. 新增知识库引用投影，禁止正文落入普通 Skill inline、transient stage 或 Artifact 归档分支。每次新知识库调用跳过既有同参结果复用，保留唯一 Invocation lifecycle。
5. 将同一知识库引用装载器接入普通上下文与压缩；真实正文装载后的内容进入实际预算。预期的失效引用按第 3.5 节降级，旧正文不回放、新 hash 不顶替旧引用。
6. 长文累积超限时沿 closed history 压缩，压缩请求也带实际内容；摘要保留事实、来源及已读范围。不能只改 ContextBuilder 而漏掉 compaction.py。
7. 增加提示词：索引简述不是正文证据；未读完不宣称完整；只引用本轮范围内实际读取的文献；[1] 标号及文末书目，通用知识补充须区分；资料内指令不改变平台权限。
8. 观测新增 knowledge 分类及必要引用投影分类，只记录状态、数量、范围、耗时和 token；日志及诊断不记录正文、受管路径或原始工具载荷。

必须通过：同一 Run 更新后无游标的同参重读得到当前内容；旧游标拒绝混读；撤权或删除后不返回旧成功结果；有效库仍能继续读取；大于工具预算的索引和文献连续读完；一次必然触发压缩的案例从前段取得唯一实验细节并正确引用。检查实际模型请求，不仅检查持久化小引用或最终文字。

建议扩充 tests/orchestration/test_agent_invocation.py、test_agent_result_projection.py、test_agent_context_builder.py、test_agent_context_preflight.py、test_agent_compaction.py、test_agent_loop.py；新增 tests/capabilities/knowledge/test_executor.py、tests/orchestration/test_knowledge_results.py。Skill/MCP 既有结果、取消及恢复合同做相关回归。

### 检查点 F 管理页面与会话选库

预计修改：App.tsx、api/client.ts/types.ts、新管理及选库组件、必要 styles、前端测试；SSE 仅在确有新增公开状态消费时修改，不为知识库另建事件协议。

1. 账户设置按钮保留 MCP 设置，并提供文献知识库入口。管理页区分我的库与共享给我，使用 API 有效角色控制界面。
2. owner 可建库、编辑名称描述、批量上传、查看全文及提取结果、编辑及逐篇/批量确认、失败重试、丢弃、维护元数据、显式替换、删除与共享。
3. reader 仅看已入库索引和正文、下载及问答；看不到草稿、共享名单和维护控件。下载走鉴权 API。
4. 展示真实的提取、发布、索引修复及清理状态；替换和删除对话框说明当前正文及清理结果，操作未完成不假报成功。
5. 输入区域提供 auto/多选和显式返回 auto；首次发送前可选，按会话保存，运行中修改显示对下一次提问生效。同一会话的选库保存按操作顺序处理；保存中禁用新问题发送，按钮与回车共用发送入口的新问题路径也须在清空输入及调用 submitMessage 前等待最近一次选择保存成功，并复验仍为原账号和会话。保存失败则保留输入、显示错误并阻止本次发送，不静默使用旧选择或 auto；该等待不改变当前任务的补充输入和恢复路径。
6. 切换会话或账号时拒绝迟到的列表、选库及审核响应，防止旧请求覆盖当前状态；失效库明确提示，全部失效引导重选。
7. Markdown 预览复用现有安全 renderer；保留聊天、MCP、Skill picker、上传与 interrupt 的已有交互。

验证：组件权限、所有管理动作及失败状态、批量部分成功、首条消息前选择、刷新恢复、会话/账号切换竞态、运行中切换、失效提示、MCP 入口可达。执行前端全量测试、typecheck 和 build。

针对选库与发送顺序，补充以下延迟请求测试，检查实际 API 调用及本轮快照，而非只检查控件文字：

- 人为延迟选库保存，用户立即点击发送或按回车；保存成功前不提交消息，成功后的新问题仅使用保存的指定库。
- 选库保存失败时，不调用消息提交 API，不清空输入，也不以旧选择或 auto 发送；用户修正或重试保存成功后才能发送。
- 连续选择不同库时，保存顺序不被网络时序反转，直到最后一次选择保存成功才允许发送，且新问题快照与该选择一致；已有运行中问题的快照保持不变。

### 检查点 G 整体验收、迁移与发布交接

1. 按第 5 节逐项收集 AC-01 至 AC-29 证据，运行真实 PostgreSQL 并发及 Sidecar 模式恢复；不将环境 skip 记为通过。
2. 用每库几百篇的真实或获准使用文献，覆盖多个主题、相近标题、缺字段、跨库重复、长文和空库，检查路由、完整阅读、引用及通用补充。
3. 记录问题集、预期正文依据、实际选库和选文献、正文覆盖、答案引用、延迟、模型调用数与 token 消耗。不预设未经实测的准确率或性能数字；未覆盖或错误引用的验收样例必须修复后重跑。
4. 在隔离旧库副本和空库验证 additive schema、重复启动幂等、原有业务数据不变、知识库根持久化、重启后审核与清理继续完成。
5. 审查最终差异、完整相关回归及文档，更新 PRD/计划状态、AGENTS、API 文档和 CHANGELOG。保存独立 Git 检查点，未获得发布授权不推送、构建或部署。
6. 发布交接列出新增 schema/权限/持久根、迁移与备份顺序、候选后端与前端配套、最小验证和回退边界。发布前备份仅用于受控灾难恢复，不提供旧正文查询；任何恢复若会重新带回已清理正文，必须单独确认恢复点及数据影响，不能作为普通功能回退。
7. 普通回退先停止知识库新写入和处理服务，退回兼容旧业务的程序；不为回退删除新表或文献文件，不把历史答案关联回文献，不假设旧二进制一定接受新的 PostgreSQL manifest。先在隔离库验证兼容性，不兼容则采用前向修复。

## 5. 全量需求与验收映射

| PRD 功能需求 | 实施主责 | 验收 |
|---|---|---|
| FR-01 管理入口 | C、F | AC-01、AC-02 |
| FR-02 权限角色 | A、C、F | AC-14 |
| FR-03 Markdown 上传与提取 | B、C、F | AC-03、AC-06、AC-26 |
| FR-04 人工审核与发布 | B、C、F | AC-04、AC-05 |
| FR-05 SHA-256 判重 | A、B | AC-07、AC-08、AC-09 |
| FR-06 元数据及正文替换 | B、C、F | AC-10、AC-26 |
| FR-07 删除与清理 | A、B、G | AC-09、AC-10、AC-11、AC-26 |
| FR-08 数据职责 | A、B、D | AC-02、AC-06 |
| FR-09 Markdown 总索引 | B、E | AC-12 |
| FR-10 库级共享 | A、C、F | AC-13、AC-14 |
| FR-11 统一授权与团队预留 | A、C、E | AC-15、AC-16、AC-28 |
| FR-12 会话选择 | D、F | AC-17 |
| FR-13 本轮范围固定 | D、E | AC-18、AC-28 |
| FR-14 不可用库 | D、E、F | AC-19 |
| FR-15 原生能力接入 | D、E | AC-01、AC-20、AC-28 |
| FR-16 先索引后全文 | E、G | AC-21、AC-29 |
| FR-17 长文件、引用与压缩 | B、E、G | AC-22、AC-23、AC-28、AC-29 |
| FR-18 提示词与答案独立 | E、F、G | AC-24、AC-25、AC-29 |

| PRD 非功能需求 | 实施及证据 |
|---|---|
| NFR-01 异步及查询效率 | B 的处理服务与入库时提取；查询不重新提取整库，普通聊天不等待提取。 |
| NFR-02 既有预算 | E 的实际请求计数、分页与压缩；AC-22、AC-29 验证。 |
| NFR-03 状态与日志 | B、C、E、F 的处理状态和低敏日志；检查全文不持久泄漏。 |
| NFR-04 持久化 | A、B、D、F 的重启、刷新、共享及偏好恢复。 |
| NFR-05 真实文献验证 | G 的真实问题集、引用、完整阅读、调用量与耗时报告。 |

AC-27 是 G 的整体通过条件；它不能代替上表逐项验证。实施记录按 AC 编号记录命令或场景、结果和实际缺口，不另建通用证据平台。

## 6. 验证命令与执行方式

以下命令用于代码实施验证，实际结果以实施验证记录为准，不因列在计划中就视为通过。项目默认 Python 环境为 `multi_agent`；如实施环境另有等价环境，先记录解释器与依赖，不自动安装。

### 6.1 每个检查点

```bash
git status --short --branch
conda run -n multi_agent python -m unittest tests.orchestration.test_agent_invocation tests.orchestration.test_agent_context_builder tests.orchestration.test_agent_context_preflight tests.orchestration.test_agent_compaction
git diff --check
```

根据本检查点实际变更选择对应测试；上述既有测试用于 Agent 链路变更的基线和回归，不要求每次改管理 UI 都重复运行。新测试模块创建后，加入对应检查点的聚焦命令及既有分层 discover。

### 6.2 完整交付

```bash
conda run -n multi_agent python -m unittest discover -s tests/core -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/lifecycle -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/e2e -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/observability -p 'test_*.py'
conda run -n multi_agent ruff check src/capabilities/knowledge src/storage/knowledge_repository.py src/storage/knowledge_files.py src/api/routes/knowledge.py src/api/knowledge_dto.py src/orchestration/agent_loop/knowledge_results.py
npm --prefix frontend test -- --run
npm --prefix frontend run typecheck
npm --prefix frontend run build
git diff --check
```

Ruff 路径按最终实际新增或修改文件补齐；不能因清零既有无关问题扩大修改范围。PostgreSQL 使用模块专用隔离测试库；Sidecar 测试必须使用已验证 binary，记录 off/shadow/enforce 实际结果，缺 binary 或 DSN 时列缺口。

若实施发现必须调整 Rust 合同，先说明与本计划的差异及必要性，再通过 `scripts/run_rust_quality_gates.py` 运行相应门禁；不能直接执行另起的 Cargo 门禁或修改协议规避现有校验。纯 Python 接入仍需现有 Sidecar contract 和端到端恢复回归。

真实模型和文献评测使用实施时的已授权测试配置，不读取或输出部署文件中的凭据，不把私有全文、模型完整请求或凭据放入评测报告。

## 7. 实施时必须守住的边界

- 文件与数据库事务不共同原子化：靠已有记录上的发布、索引与清理进度恢复，不靠进程内锁假装完成。
- 同库判重跨活动文献与草稿：同一事务保护两者，不以两个独立唯一键代替跨表约束。
- 本轮范围不是会话偏好的实时投影：首次快照落盘后不重新扩容，恢复不从当前选择回填。
- 正文引用不是正文副本：普通上下文与压缩都装载实际内容，但持久化不归档原文，不复活旧文件。
- 历史回答不随资料变化维护：替换、删除、撤权测试同时检查既有答案与其中引用保持原样。
- 完成状态必须有验证结果：计划、代码完成、真实验收和部署分别记录，缺少真实环境证据时不宣称整体完成。

## 8. Markdown 与 PDF 格式扩展（2026-10-27）

### 8.1 已确认范围与实现选择

用户已确认同时支持 Markdown 和 PDF，扫描版 PDF 暂不处理。本节保留设计摘要，具体数据字段、API 语义、开发检查点和全量验收以[双格式完整交付实施计划](2026-10-27-literature-knowledge-pdf-implementation-plan.md)为准；双格式已实施，本地 SQLite 与真实素材结果见[PDF 实施证据](2026-10-27-literature-knowledge-pdf-implementation-evidence.md)，真实模型质量仍未整体关闭。

- 保留上传原件，原始字节的完整 SHA-256 继续承担同库判重；不按标题、解析文字或 DOI 合并跨格式文件。
- Markdown 沿用当前原文路径。PDF 在入库后台本地逐页提取已有文字层，生成带页界标记的 UTF-8 Markdown；Agent 继续只读索引与 Markdown 正文，不另设 PDF 问答工具。
- 候选使用 `pypdf`，实施时锁定经验证的版本。它提供文字提取，不执行 OCR，也不承诺图片、复杂公式和所有表格结构的视觉还原。此处是实施建议，不是用户指定的库。
- 检测到扫描页或无有效正文时明确失败；普通空白页允许保留页界。损坏、加密不可读或资源上限超出时失败，不截断后冒充成功。文献中的图片信息不作为已经提取的证据。
- 解析成功后仍走现有全文元数据提取与人工审核。审核页面区分“上传原件”和“阅读正文”，允许所有者下载草稿原 PDF 对照；共享阅读者只读取已入库内容。

技术依据：[pypdf 文字提取与局限](https://pypdf.readthedocs.io/en/stable/user/extract-text.html)、[上游许可证](https://github.com/py-pdf/pypdf/blob/main/LICENSE)，核对日期 2026-10-27。不引入独立服务、OCR、embedding 或通用文件转换平台。

### 8.2 数据、文件与恢复

1. 保持现有 `body_ref` / `sha256` 的“上传原始字节”含义，不将 PDF 解析哈希写入原件判重字段。在文献和导入记录中补充可空的解析正文引用；Markdown 不复制出第二份正文。引用包含的内容哈希用于校验解析正文；API 不暴露物理引用。
2. 私有文件仓支持受控的 `.md` / `.pdf` 原件与 `.md` 解析正文，沿用 ID 路径、原子写入、哈希校验、私有权限与删除标记。不能将 PDF 二进制假存为 `.md`。
3. 解析使用现有后台 claim 和线程执行，不阻塞请求事件循环。模型提取只接收完成的 Markdown。原件可读但解析正文缺失时，禁止确认、入索引或交给 Agent；元数据手工修正不能绕过此门禁。
4. 解析正文落盘前先以 claim/CAS 将其引用登记在持久记录中，写后再校验并推进状态；崩溃或迟到结果必须可定位、可清理，不能产生数据库未知的全文副本。重试复用有效解析正文；需要改写引用时，旧引用进入现有清理账本。
5. 正文替换以同一事务切换原件和解析正文引用，记录两者的旧引用，再走现有索引与清理流程。丢弃、删除文献与删除库均覆盖两个文件及临时文件；未完成不假报成功，跨库独立和历史答案规则不变。
6. Agent 正文版本标识绑定当前原件与实际阅读正文，保持现有 64 字符 SHA-256 合同；原件或解析正文变化后旧游标/引用不能混读。分页和压缩继续装载实际 Markdown、按实际内容计数。
7. SQLite 现有库的新增可空列需走已有兼容升级方式，验证旧 Markdown 数据重开后原件/正文语义不变；PostgreSQL 同步共享模型及 additive manifest/reconciliation，并用隔离库验证。不得通过删除旧数据库或只修改 `create_all` 冒充迁移完成。

### 8.3 API 与页面

- 上传和正文替换接受 `.md`、`.markdown`、`.pdf`，允许混合批次；原件格式需与内容验证相符，保留已有大小限制及逐项失败结果。
- 现有阅读/预览继续返回 Markdown；增加明确的原件下载入口，PDF 返回正确媒体类型、原始文件名和原件 SHA-256。解析 Markdown 导出返回自身哈希和 `.md` 文件名，不能复用 PDF 原件哈希充当下载校验。
- 草稿原件下载须校验 owner；已入库原件和正文下载均走现有只读授权。撤权或删除后两个入口都不可新读；缓存继续 `no-store`。
- 页面展示格式、解析/提取/审核真实状态及可理解的失败原因，更新上传和替换控件。PDF 审核提供原件下载与解析正文预览，已入库 PDF 可下载原 PDF 或导出 Markdown；不引入新的内嵌 PDF 编辑器。
- 更新 `knowledge.read_document` 的说明及实际阅读视图，避免把 PDF 提取文字描述为原始 Markdown 或完整视觉识别。不改变工具参数、Capability 类型、共享、选库或 Sidecar 信封。

### 8.4 开发顺序与验收

| 顺序 | 改动 | 验证 |
|---|---|---|
| 1 | 先添加双格式、解析失败、原件下载和双文件清理的定向回归 | 测试能在当前只支持 Markdown 的代码上复现不支持场景。 |
| 2 | 实现受控原件存储、解析正文引用与兼容 schema | 旧 SQLite 数据重开、同库并发判重、跨库独立；PostgreSQL 无隔离环境时明确保留缺口。 |
| 3 | 接入 PDF 解析、现有 claim/人工确认与恢复 | AC-30～AC-33，包含首尾页、空白页、扫描/混合页、损坏、加密、写失败、解析后重启、丢弃竞态、两种格式相互替换。 |
| 4 | 接入原件/正文 API 和管理页面 | 混合批次、真实媒体类型/文件名/哈希、草稿 owner 与 reader 权限、撤权、明确下载动作和失败状态。 |
| 5 | 回归知识库工具及整体验收记录 | 实际模型请求取得 PDF 首尾文字；分页/压缩和旧引用失效；旧 Markdown、普通聊天、Skill/MCP 相关回归，前端测试/typecheck/build。 |

从用户已提供的归档目录读取有代表性的文字型 PDF，至少覆盖中文、英文和多栏文献，核对解析文字顺序、首尾页及缺失情况。只在隔离临时文献库使用副本，不修改原目录、不把私有全文加入 Git；本地解析验证不等于已完成真实模型质量评测。必要时补充真实问题集和明确的模型调用授权，继续保留原真实 PostgreSQL/NFR-05 的验收缺口。

License Requirement：现有实现复用原依赖；PDF 方案拟新增 `pypdf`（上游 BSD-3-Clause），只用于本地解析。当前为文档修订，未安装依赖；具体版本、安装验证与许可证随代码实施记录。不引入 OCR 或其他新运行服务。
