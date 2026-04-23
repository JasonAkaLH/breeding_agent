# 全局变更日志

本文件是 **multi_agent_framework 仓库的总变更记录**，按时间倒序汇总代码、文档以及仓库其他路径的重要变更。

面向全体协作者——**包括人类开发者与任意 AI 编码助手**。用于快速了解当前工程状态、最近改动，以及跨模块影响面，不依赖任何工具本地记忆。

> 语言：全部条目使用中文。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/)。

---

## [Unreleased]

### 2026-04-23 — 基于正式 PRD 产出一期开发计划与测试规格

- 新增 `.omx/plans/prd-20260423-main-agent-framework-phase1.md`，按主框架最小内核 + NL2SQL MVP 垂直闭环策略，整理一期开发步骤、验收标准、风险与执行建议。
- 新增 `.omx/plans/test-spec-20260423-main-agent-framework-phase1.md`，把 TDD 顺序、分层测试范围、关键用例与阶段门槛落成 companion test spec。
- 新增 `.omx/context/main-agent-framework-phase1-plan-*.md` 上下文快照，记录本轮计划产出的任务目标、事实依据、约束与待确认项。
- 新增 `docs/一期开发计划.md`，把开发计划转为仓库内正式文档，并按 `docs/prd/` 的细分 PRD 拆成“范围 + Phase”两层结构，便于后续协同讨论与持续更新。
- 新增 `docs/dev_processes/README.md` 与 `docs/dev_processes/Phase-0~7` 系列文档，参考外部 `PRD/dev_processes/` 的写法，把一期开发计划拆为可执行的阶段文档，并将 `docs/一期开发计划.md` 收口为总览入口。
- 进一步收紧 Phase 4 / Phase 5 边界：明确 Phase 4 负责主代理通用编排标准，Phase 5 负责让 NL2SQL 按该标准接入，避免主代理内核反向适配首个 capability。
- 将原 Phase 6 再拆分为两份文档：Phase 6 专注 FastAPI/SSE 对外接口接入，Phase 7 专注一期验收与第二阶段评估，保证每份文档只聚焦一类工作。
- 收紧文档措辞，避免把泛化“记忆能力”表述误读为一期额外范围；当前仓库文档继续只保留主 PRD 已定义的记忆系统/长期记忆边界。
- 新增记忆系统边界决策：一期只做会话延续型记忆，不做跨任务知识沉淀；相关 PRD 与开发流程文档已同步收口。
- 补充记忆存放位置口径：一期会话记忆属于主框架状态存储，本地阶段落 SQLite，后续正式化同构迁移到 PostgreSQL。
- 补充会话记忆最小字段边界：明确一期只对会话连续性恢复所需字段负责，并将该字段集作为后续存储与验收口径。
- 新增会话记忆落库规则：恢复判断必需信息走独立字段，补充上下文走 JSON / refs / summary，且一期不单独建设 memory 专用表。
- 补充 SQLite → PostgreSQL 迁移修改清单：除字段类型升级外，还需同步复核 engine/session、repository、ORM、序列化、索引与回归测试。
- 新增 `docs/dev_processes/Phase-2-SQLite状态存储表结构草案.md`，把 Phase 2 的存储规则翻译成具体 SQLite 表结构、索引与独立列/JSON 字段映射。
- 补记 Phase 0 完成状态：在 `docs/dev_processes/Phase-0-冻结一期范围与验收边界.md` 中显式标记“已完成”，并勾选该阶段验收清单，反映当前仓库内已冻结的一期范围、边界与验收口径事实。
- 启动 Phase 1 开发：新增 `src/core/` 共享模型、状态枚举、基础错误与 contract 定义，并补齐 `tests/core/` 的首批红绿测试。
- 新增 `docs/一期核心模块边界.md`，把一期模块职责、允许/禁止依赖方向，以及“core 面向未来 capability 通用、但不过度抽象”的边界写成正式文档。
- 正式落地当前最小测试命令：采用 `conda run -n multi_agent python -m unittest discover -s tests/core -p 'test_*.py'` 作为 Phase 1 / core 的可执行回归命令，并同步更新 `README.md`、`AGENTS.md` 与 Phase 1 文档。
- 补记 Phase 1 完成状态：在 `docs/dev_processes/Phase-1-建立核心契约与共享模型.md` 中显式标记“已完成”，并勾选该阶段验收清单。
- 启动并完成 Phase 2 开发：新增 `src/storage/` 与 `src/storage/sqlite/`，落地 SQLite base / session / bootstrap、ORM model、repository 与 async storage façade。
- 为 Phase 2 补齐 `InterruptAnswer` 共享模型与 `StoragePort` 最小增量，保证存储层不重复发明第二套 contract，同时保持 `core` 不引入 SQLAlchemy 细节。
- 新增 `tests/storage/` 回归测试，覆盖 bootstrap、conversation/message、task/task_node/task_edge/artifact、event/mailbox、interrupt/interrupt_answer/checkpoint 的 round-trip 与约束行为。
- 正式落地 Phase 2 最小测试命令：采用 `conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_*.py'`，并同步更新 `README.md`、`AGENTS.md` 与 Phase 2 文档。
- 补记 Phase 2 完成状态：在 `docs/dev_processes/Phase-2-落地SQLite状态存储与仓储抽象.md` 中显式标记“已完成”，并勾选该阶段验收清单。
- 启动并完成 Phase 3 开发：新增 `src/lifecycle/`，落地 `task_state_machine`、`mailbox_service`、`interrupt_service`、`cancellation_service`、`conversation_guard` 以及 mailbox message type / typed payload 基础设施。
- 为 Phase 3 补齐生命周期前置状态与存储能力：扩展 `NodeStatus` 生命周期过渡状态，并为 `StoragePort` 与 SQLite repository 增加 active task lookup、interrupt / checkpoint 查询、mailbox message / delivery 列表查询等 primitive。
- 新增 `tests/lifecycle/` 回归测试，覆盖强/轻 ACK、TTL 重试与过期、interrupt/resume、Task Context Termination、conversation 串行保护，并回归验证 `tests/core` 与 `tests/storage` 未被破坏。
- 正式落地 Phase 3 最小测试命令：采用 `conda run -n multi_agent python -m unittest discover -s tests/lifecycle -p 'test_*.py'`，并同步更新 `README.md`、`AGENTS.md` 与 Phase 3 文档。
- 补记 Phase 3 完成状态：在 `docs/dev_processes/Phase-3-实现生命周期与协作协议.md` 中显式标记“已完成”，并勾选该阶段验收清单。
- 新增 `docs/任务上下文终止状态流转图.md`，用 Mermaid 图整理主动停止（Task Context Termination）时 task / node / interrupt / checkpoint / mailbox delivery / late result 的收敛路径，便于后续沟通与评审。
- 新增 `docs/任务上下文终止状态流转图.png`，将主动停止状态流转图的位图版归档到仓库 `docs/` 目录，便于直接预览与分享。
- 启动并完成 Phase 4 开发：新增 `src/orchestration/`，落地 capability registry、instance registry、scheduler、workflow/task plan 标准、completion policy、strict reject backpressure 与 orchestration service。
- 为 Phase 4 补齐编排前置能力：扩展 `StoragePort` 与 SQLite storage，增加活跃任务、task node、task edge、事件列表等编排所需查询接口，并保持编排层只面向 capability contract 与 workflow plan 标准编程。
- 新增 `tests/orchestration/` 回归测试，覆盖 registry/scheduler、completion policy、strict reject 背压与 mock/fake capability flow 的编排闭环验证。
- 正式落地 Phase 4 最小测试命令：采用 `conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'`，并同步更新 `README.md`、`AGENTS.md` 与 Phase 4 文档。
- 补记 Phase 4 完成状态：在 `docs/dev_processes/Phase-4-打通编排调度与最小运行闭环.md` 中显式标记“已完成”，并勾选该阶段验收清单。
- 修订 `docs/主代理编排能力流程图.png`：将“required 失败但可重排”从错误地回到 completion policy 的画法，调整为进入“受控重编排入口 -> Workflow / Task Plan 生成（修订） -> 再次 Dispatch”的闭环，并删除 `.codex/generated_images/` 下对应临时原件。
- 启动并完成 Phase 5 开发：新增 `src/capabilities/nl2sql/` 与 `src/integrations/`，落地 NL2SQL workflow provider、capability executor、六个节点能力单元，以及 MySQL 只读执行适配层。
- 复用现有 `src/nl2sql/` 资产与 `configs/nl2sql/*.yaml`，将 schema context、路由规则与 SQL Guard 规则通过 capability 层封装接入 Phase 4 的通用 orchestration 标准，而不是反向修改主代理内核。
- 新增 `tests/capabilities/nl2sql/` 回归测试，覆盖 intent route、schema context prepare、SQL guard、readonly execute、result summarize 与基于 orchestration 的 NL2SQL 闭环验证。
- 正式落地 Phase 5 最小测试命令：采用 `conda run -n multi_agent python -m unittest discover -s tests/capabilities/nl2sql -p 'test_*.py'`，并同步更新 `README.md`、`AGENTS.md` 与 Phase 5 文档。
- 补记 Phase 5 完成状态：在 `docs/dev_processes/Phase-5-接入NL2SQL-MVP能力链路.md` 中显式标记“已完成”，并勾选该阶段验收清单。
- 新增 `docs/NL2SQL子代理结构图.svg`，把当前 NL2SQL 子代理（workflow provider + executor + 六个 capability 节点 + readonly integration）的结构图直接生成到 `docs/` 目录，并避免在 `.codex/` 保留原件。
- 新增 `docs/NL2SQL-LLM版本改造方案.md`，整理当前启发式 MVP 升级到真正 LLM 版本的改造目标、实施步骤、风险与验收标准。
- 新增 `docs/LLM接入阶段建议.md`，明确区分 NL2SQL 内部 LLM 接入与主代理 / 通用子代理 LLM 接入的推荐阶段，并说明为何不建议把 LLM 混进 Phase 6 / Phase 7。
- 新增 `docs/LLM接入阶段建议图.svg`，把 LLM 接入时机建议画成阶段流程图，明确“先做 NL2SQL 内部 LLM，再做主代理 / 通用子代理 LLM”的推荐顺序。
- 新增 `docs/NL2SQL子代理结构图.png` 与 `docs/LLM接入阶段建议图.png`，将两张 SVG 文档图转换为 PNG 版本，便于直接预览与嵌入其他文档或外部材料。
- 更新 `AGENTS.md`：新增规则，要求在开始分析、设计、编码或文档修改前先查看 `CHANGELOG.md` 最近相关条目，先了解此前已完成工作再继续推进。

### 2026-04-22 — 初始化主代理框架设计基线并建立仓库级设计资产

- 建立并持续收口主代理框架 PRD，明确主框架只负责任务拆解、编排、分发，具体业务能力以下层 capability 形式接入。
- 将 PRD 重构为“总览 + 专题”结构，新增 `docs/prd/` 专题文档，降低单文档耦合度并方便后续按模块做计划与实现。
- 补齐 NL2SQL 首个 MVP 相关设计资产，包括数据库结构说明、Prompt 输入模板、业务路由规则、schema 元数据与 SQL Guard 规则。
- 新增 `src/nl2sql/` 的 schema context builder 骨架与基础模型定义，用于后续按 TDD 推进 capability 落地。
- 导出当前 `multi_agent` 环境依赖到根目录 `requirements.txt`，并补充仓库规则：优先基于现有依赖实现功能。
- 新增根目录 `CHANGELOG.md`，作为仓库级开发记录入口，并在仓库规则中明确要求每日开发结束时手动补记当天工作内容。
- 收口主代理思维模式、完成判定闭环、优先级两层模型、模块划分与主框架/Capability 上下级边界等关键架构决策。
- 更新 `.gitignore`，把 `.codex/` 作为本地运行时目录忽略，不再纳入版本控制。
