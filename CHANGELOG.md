# 全局变更日志

本文件是 **multi_agent_framework 仓库的总变更记录**，按时间倒序汇总代码、文档以及仓库其他路径的重要变更。

面向全体协作者——**包括人类开发者与任意 AI 编码助手**。用于快速了解当前工程状态、最近改动，以及跨模块影响面，不依赖任何工具本地记忆。

> 语言：全部条目使用中文。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/)。

---

## [Unreleased]

### 2026-04-24 — 启动 Phase 5.5 SQLQuery LLM 增强专题

- 新增 Phase 8.1 设计稿 `docs/dev_processes/Phase-8.1-SQLQuery宏能力与LLM动态DAG规划.md`，明确 SQL 查询能力统一命名为 SQLQuery；对外只暴露 `sql_query.query` 宏能力，`sql_query.*` 作为内部固定子工作流节点，并为后续 LLM Planner 只生成高层 DAG 留出 validator / expander 边界。
- 调整 SQL 查询能力公开注册与 API 能力目录：新增 public capability `sql_query.query`（展示名 `SQLQuery`），默认隐藏 `sql_query.*` 内部节点；`capability_id="sql_query"` / `"sql_query.query"` 进入固定 SQL 查询 workflow。
- 完成 SQL 查询能力命名收口：源码目录、配置目录、测试目录、内部 capability id 与文档统一使用 `sql_query` / `SQLQuery`，保留既有 public capability `sql_query.query`。
- 收紧 SQLQuery 请求入口：旧 `nl2sql*` / `sqlquery*` capability id 与 `sql_query.*` 内部节点 id 均不能作为外部请求能力直接调用；外部只保留 `sql_query` / `sql_query.query`。
- 完成 Phase 8 首轮实现：新增 `main_agent.respond` 主代理 capability、`MainAgentWorkflowProvider`、`CompositeExecutor` 与 workflow router；`capability_id=None` 的普通消息默认进入主代理，显式 SQL 查询请求进入固定 SQLQuery 链路。
- 新增 `src/integrations/codex_skills/`，落地 Codex Skill 兼容层首版：解析 `SKILL.md` frontmatter/body、识别 `inputs` / `outputs` / `scripts` 扩展字段、建立 `SkillCatalog`、按 trigger/name/description 匹配 skill，并提供受控 Python `SkillScriptRunner`。
- 新增 `src/capabilities/main_agent/`，主代理可将 skill 指令、上传 artifact 脱敏 metadata、skill 脚本输出注入 prompt，并通过非 thinking streaming LLM seam 产生 `main_agent.output_delta` / `main_agent.output_final` 前端事件。
- 修复 interrupt/resume 竞态下 late open interrupt 覆盖 answered 状态的问题：SQLite interrupt repository 不再允许旧的 open interrupt 保存降级已 answered/cancelled/expired 的记录，避免恢复执行已完成但 task 长期停留 running。
- 补充主代理与 Skill 兼容层测试，覆盖 default main-agent routing、显式 SQLQuery 回归、composite executor、skill parser/catalog/matcher/script runner、主代理 prompt 注入与 streaming 输出；新增 `tests/capabilities/main_agent` 最小测试命令并同步更新 `README.md` 与 `AGENTS.md`。
- 新增 `docs/工作周报模板.md`，将本周汇报内容提炼为可复用周报模板；根目录 `本周工作周报.md` 作为本地汇报草稿保留并加入 `.gitignore`，避免临时汇报材料进入版本库。
- 将本地 `config.yaml` 加入 `.gitignore`，避免包含 API key 的本机 LLM 配置误入提交；代码仍通过注入配置或本地文件读取支持真实 provider。
- 新增 `docs/dev_processes/Phase-8-Codex-Skill兼容层与上传文件上下文驱动的主代理技能选择机制.md`，将 Codex Skill 兼容层方案固化为二期主代理能力设计稿，明确兼容 `SKILL.md` 格式、catalog/matcher、prompt 注入、上传文件 `ArtifactRef` 上下文、输入输出契约识别，以及受控执行 skill 包内声明脚本；不复刻 Codex 本地文件 / 任意 shell / plugin runtime。
- 补充 Phase 8 的 skill 脚本运行环境口径：脚本只支持 Python，runtime 不负责依赖探测或安装；后续由面向用户的 skill 构建指南明确后端 Python 版本与可用 package 列表。
- 更新 `docs/dev_processes/README.md`，把 Phase 8 作为二期主代理能力专题挂入开发流程索引，并注明其不修改一期 Phase 0 ~ Phase 7 验收结论。
- 新增 `docs/dev_processes/Phase-5.5-SQLQuery-LLM增强专题.md`，将后续 LLM 接入讨论收口到 SQLQuery 内部 LLM 化专题，明确目标、非目标、设计原则、实施切面、验收口径与讨论记录入口。
- 补充 Phase 5.5 的 LLM seam 初步结论：长期 client 入口倾向放在 `src/integrations/`，SQLQuery 只依赖最小文本生成接口，prompt 组装保留在 capability 内部，审计默认记录 metadata 与 fallback reason。
- 将根目录 `llm_client.py` 移动到 `src/integrations/llm_client.py`，并通过 `src/integrations/__init__.py` 导出 LLM client seam。
- 修复 `src/integrations/llm_client.py` 的配置读取与异步流式调用问题：改用 `yaml.safe_load`、延迟到初始化阶段读取配置、支持注入配置测试、使用 `async for` 读取 streaming chunk，并按 `delta.content` / `delta.reasoning_content` 提取输出。
- 调整 `src/integrations/llm_client.py` 的默认文本生成接口：`generate_text()` 改为非 streaming 调用，用于 SQLQuery 结构化 SQL / 摘要生成；`generate_text_with_thinking()` 继续保留 streaming 输出给需要逐 chunk 展示 reasoning / answer 的场景。
- 新增 `LLMClient.stream_text()`，作为“非 thinking + streaming 文本输出”模式，默认 `reasoning_effort=minimal`，预留给后续主代理在不需要深度思考但需要流式回传用户输出时使用。
- 收窄 `src/integrations/llm_client.py` 的 `ReasoningEffort` 类型范围，仅保留 `minimal`、`low`、`medium`、`high` 四个选项。
- 补充 Phase 5.5 的 `sql_generate` LLM 输入 / 输出契约：输入复用 `schema_context_prepare` 裁剪结果，输出采用 `answer | clarify | reject` 结构化 JSON，并明确 fallback 与 guard blocked 的边界。
- 补充 Phase 5.5 的 `result_summarize` LLM 输入 / 输出契约：摘要只解释已执行结果，默认只发送有限 rows preview，`summary` 作为稳定主字段，并明确 LLM 失败时回退确定性模板摘要。
- 补充 Phase 5.5 的测试矩阵与最小实施顺序：按 LLM seam、prompt builder、`sql_generate`、`result_summarize`、observability 与回归验收推进，默认测试全部使用 fake LLM。
- 完成 Phase 5.5 首轮 TDD 实施：新增 `tests/integrations/`、SQLQuery prompt builder / LLM JSON 工具测试、`sql_generate` LLM 主路径 / fallback / clarify / reject 测试，以及 `result_summarize` LLM 主路径 / rows preview / fallback 测试。
- 新增 `src/capabilities/sql_query/prompt_builders.py` 与 `src/capabilities/sql_query/llm_utils.py`，将 SQLQuery 专属 prompt 组装、fake/real 文本生成器兼容、JSON 提取与 rows preview 序列化限制封装在 capability 内部。
- 改造 `sql_query.sql_generate`：支持注入 `llm_text_generator`，结构化处理 `answer | clarify | reject`，LLM 失败回退当前启发式 SQL 生成，并输出 `generation_source`、`llm_mode`、`fallback_reason` 等 metadata。
- 改造 `sql_query.result_summarize`：支持注入 `llm_text_generator`，默认最多发送 20 行 `rows_preview`，LLM 摘要失败回退模板摘要，0 行结果直接走确定性摘要。
- 调整 SQLQuery workflow：`result_summarize` 额外依赖 `sql_generate` 输出以获取问题 / route 上下文，避免让 `sql_execute_readonly` 透传摘要层业务语义。
- 修复 interrupt resume 重新调度时复用旧 task node 状态与 interrupt 可见性竞态：同一 task 在回答 interrupt 后会重置既有 workflow 节点状态，并先落库 `WAITING_FOR_INPUT` 节点状态再暴露 open interrupt，避免旧状态或抢答竞态阻断恢复执行。
- 为 Phase 5.5 补齐可观测性：新增 `sql_query.llm_call` / `sql_query.llm_fallback` audit-only event，默认不记录完整 prompt、完整 rows 或 API key；补充 fake LLM e2e 与 observability 测试。
- 更新 API runtime 与测试 support，允许显式注入 fake / adapter 文本生成器；默认自动化测试仍不访问真实 LLM provider。
- 新增 `tests/integrations` 最小测试命令，并同步更新 `README.md` 与 `AGENTS.md`。
- 将 Phase 5.5 / SQLQuery 相关测试中的示例查询品种从“先玉335”替换为“龙粳33”，并用真实只读数据库查询核对该品种的基础品种、基因型预览、籼粳成分与审定信息。
- 更新 `docs/dev_processes/README.md`，把 Phase 5.5 作为一期验收后的补充专题挂入开发流程索引，并注明其不修改 Phase 0 ~ Phase 7 已完成验收结论。
- 更新 `docs/LLM接入阶段建议.md`，把 Phase 5.5 的正式开发过程文档作为 SQLQuery LLM 增强专题输出物。

### 2026-04-23 — 基于正式 PRD 产出一期开发计划与测试规格

- 新增 `.omx/plans/prd-20260423-main-agent-framework-phase1.md`，按主框架最小内核 + SQLQuery MVP 垂直闭环策略，整理一期开发步骤、验收标准、风险与执行建议。
- 新增 `.omx/plans/test-spec-20260423-main-agent-framework-phase1.md`，把 TDD 顺序、分层测试范围、关键用例与阶段门槛落成 companion test spec。
- 新增 `.omx/context/main-agent-framework-phase1-plan-*.md` 上下文快照，记录本轮计划产出的任务目标、事实依据、约束与待确认项。
- 新增 `docs/一期开发计划.md`，把开发计划转为仓库内正式文档，并按 `docs/prd/` 的细分 PRD 拆成“范围 + Phase”两层结构，便于后续协同讨论与持续更新。
- 新增 `docs/dev_processes/README.md` 与 `docs/dev_processes/Phase-0~7` 系列文档，参考外部 `PRD/dev_processes/` 的写法，把一期开发计划拆为可执行的阶段文档，并将 `docs/一期开发计划.md` 收口为总览入口。
- 进一步收紧 Phase 4 / Phase 5 边界：明确 Phase 4 负责主代理通用编排标准，Phase 5 负责让 SQLQuery 按该标准接入，避免主代理内核反向适配首个 capability。
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
- 启动并完成 Phase 5 开发：新增 `src/capabilities/sql_query/` 与 `src/integrations/`，落地 SQLQuery workflow provider、capability executor、六个节点能力单元，以及 MySQL 只读执行适配层。
- 复用现有 `src/sql_query/` 资产与 `configs/sql_query/*.yaml`，将 schema context、路由规则与 SQL Guard 规则通过 capability 层封装接入 Phase 4 的通用 orchestration 标准，而不是反向修改主代理内核。
- 新增 `tests/capabilities/sql_query/` 回归测试，覆盖 intent route、schema context prepare、SQL guard、readonly execute、result summarize 与基于 orchestration 的 SQLQuery 闭环验证。
- 正式落地 Phase 5 最小测试命令：采用 `conda run -n multi_agent python -m unittest discover -s tests/capabilities/sql_query -p 'test_*.py'`，并同步更新 `README.md`、`AGENTS.md` 与 Phase 5 文档。
- 补记 Phase 5 完成状态：在 `docs/dev_processes/Phase-5-接入SQLQuery-MVP能力链路.md` 中显式标记“已完成”，并勾选该阶段验收清单。
- 新增 `docs/SQLQuery子代理结构图.svg`，把当前 SQLQuery 子代理（workflow provider + executor + 六个 capability 节点 + readonly integration）的结构图直接生成到 `docs/` 目录，并避免在 `.codex/` 保留原件。
- 新增 `docs/SQLQuery-LLM版本改造方案.md`，整理当前启发式 MVP 升级到真正 LLM 版本的改造目标、实施步骤、风险与验收标准。
- 新增 `docs/LLM接入阶段建议.md`，明确区分 SQLQuery 内部 LLM 接入与主代理 / 通用子代理 LLM 接入的推荐阶段，并说明为何不建议把 LLM 混进 Phase 6 / Phase 7。
- 新增 `docs/LLM接入阶段建议图.svg`，把 LLM 接入时机建议画成阶段流程图，明确“先做 SQLQuery 内部 LLM，再做主代理 / 通用子代理 LLM”的推荐顺序。
- 新增 `docs/SQLQuery子代理结构图.png` 与 `docs/LLM接入阶段建议图.png`，将两张 SVG 文档图转换为 PNG 版本，便于直接预览与嵌入其他文档或外部材料。
- 更新 `AGENTS.md`：新增规则，要求在开始分析、设计、编码或文档修改前先查看 `CHANGELOG.md` 最近相关条目，先了解此前已完成工作再继续推进。
- 新增 `.omx/plans/phase6-20260423-fastapi-sse-implementation-plan.md`，整理 Phase 6 的 FastAPI / SSE / cancel / audit 实施步骤、共享 seam 缺口、测试清单，并给出"先 solo / ralph 冻结共享 seam，再视情况切 3-lane 小 team"的协作建议。
- 启动并完成 Phase 6 开发：新增 `src/api/`，落地 FastAPI app、DTO、消息提交/任务查询/SSE/取消/任务图/产物/能力目录接口，以及进程内事件 broker。
- 新增 `src/integrations/audit_logger.py`，补齐 JSONL 审计输出，并把 blocked SQL、关键状态变化与取消收敛过程纳入最小可观察性范围。
- 为 Phase 6 补齐共享 seam：扩展 `StoragePort` 与 SQLite storage 的 message/artifact 查询能力，扩展 capability registry 列表查询，补齐 orchestration 的 DAG edge/root node 持久化、capability event live fan-out 与 late result discard 语义，并让 cancellation service 输出前端可见取消事件。
- 新增 `tests/api/` 回归测试，覆盖消息提交、会话串行冲突、任务查询/图/产物、前端事件回放+live 流、取消接口与能力目录接口。
- 正式落地 Phase 6 最小测试命令：采用 `conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'`，并同步更新 `README.md`、`AGENTS.md` 与 Phase 6 文档。
- 新增 `.omx/plans/phase7-20260423-acceptance-and-phase2-evaluation-plan.md`，整理 Phase 7 的 e2e / observability / interrupt-resume 验收、一期收口报告与第二阶段评估输入的实施步骤，并给出"前半程先 solo 冻结 seam，后半程可切 3-lane 小 team"的协作建议。
- 启动并完成 Phase 7 开发：新增 `tests/e2e/` 与 `tests/observability/`，落地 happy path、guard blocked、interrupt/resume、cancel late result ignored 与 JSONL audit 验收路径。
- 为 Phase 7 补齐 acceptance seam：扩展 `build_api_runtime()` 的 deterministic 注入能力，补齐 interrupt answer + resume 的最小恢复闭环，并增强 SQL guard 审计字段以支持 observability 验收。
- 新增 `docs/一期验收报告.md` 与 `docs/第二阶段评估输入.md`，把一期收口证据与二期候选主题拆成两份独立文档，避免验收结论与下一阶段讨论混线。
- 正式落地 Phase 7 最小测试命令：采用 `conda run -n multi_agent python -m unittest discover -s tests/e2e -p 'test_*.py'` 与 `conda run -n multi_agent python -m unittest discover -s tests/observability -p 'test_*.py'`，并同步更新 `README.md`、`AGENTS.md` 与 Phase 7 文档。

### 2026-04-22 — 初始化主代理框架设计基线并建立仓库级设计资产

- 建立并持续收口主代理框架 PRD，明确主框架只负责任务拆解、编排、分发，具体业务能力以下层 capability 形式接入。
- 将 PRD 重构为“总览 + 专题”结构，新增 `docs/prd/` 专题文档，降低单文档耦合度并方便后续按模块做计划与实现。
- 补齐 SQLQuery 首个 MVP 相关设计资产，包括数据库结构说明、Prompt 输入模板、业务路由规则、schema 元数据与 SQL Guard 规则。
- 新增 `src/sql_query/` 的 schema context builder 骨架与基础模型定义，用于后续按 TDD 推进 capability 落地。
- 导出当前 `multi_agent` 环境依赖到根目录 `requirements.txt`，并补充仓库规则：优先基于现有依赖实现功能。
- 新增根目录 `CHANGELOG.md`，作为仓库级开发记录入口，并在仓库规则中明确要求每日开发结束时手动补记当天工作内容。
- 收口主代理思维模式、完成判定闭环、优先级两层模型、模块划分与主框架/Capability 上下级边界等关键架构决策。
- 更新 `.gitignore`，把 `.codex/` 作为本地运行时目录忽略，不再纳入版本控制。
