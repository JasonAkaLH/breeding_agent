# 全局变更日志

本文件是 **multi_agent_framework 仓库的总变更记录**，按时间倒序汇总代码、文档以及仓库其他路径的重要变更。

面向全体协作者——**包括人类开发者与任意 AI 编码助手**。用于快速了解当前工程状态、最近改动，以及跨模块影响面，不依赖任何工具本地记忆。

> 语言：全部条目使用中文。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/)。

---

## [Unreleased]

### 2026-04-27 — 强化宽泛问题的第一性原理处理

- 主代理 Prompt 新增“第一性原理理解用户需求”约束：不假定用户知道自己要什么、该选哪个 capability 或该提供哪些参数，优先推断真实目标并给出可验证的初步答案/下一步。
- SQLQuery SQL 生成 Prompt 新增 `first_principles_need_inference` 策略，明确宽泛但仍在支持范围内的问题应优先生成概览查询，而不是直接要求用户补充库名或路径。
- 新增 SQLQuery `variety_overview` 路由与 schema profile，用于“查一下龙粳33”这类只给品种名的宽泛问题，同步覆盖审定品种库与基因型/籼粳成分信息。
- SQLQuery intent route 会将宽泛品种名查询路由到综合概览，不再在“审定品种库/基因型数据库”之间抛出冷冰冰的手动选择中断。
- SQLQuery deterministic fallback 可生成单条只读 `UNION ALL` 概览 SQL；对“龙粳/籼/稻”等水稻品种线索优先缩窄到水稻审定表 + 基因型相关表，避免跨全作物表慢扫。
- 用本地真实后端 API 验证“查一下龙粳33”可完成：任务 6 个 SQLQuery 节点全部 completed，返回 2 行，分别覆盖 `approval_variety_db` 与 `genotype_db`。
- 补充 SQLQuery intent / schema context / SQL generate / prompt builder 与主代理 Prompt 回归测试。
- 修复等待补充信息任务锁住会话的问题：新增 `GET /api/v1/tasks/{task_id}/interrupts` 与 `POST /api/v1/tasks/{task_id}/interrupts/{interrupt_id}/answer`，前端检测到 `waiting_for_input` 后展示澄清问题，并把下一次输入作为 interrupt answer 继续原任务。
- 补全 `waiting_for_input` 前端追问 UI：助手消息展示“需要补充信息”卡片、字段与可选示例，输入区显示“下一条消息会继续当前任务”的补充模式状态条，并保留显式“取消当前任务”按钮。
- 加固 interrupt 竞态：当前端已看到节点 `waiting_for_input` 但后端 open interrupt 尚未可见时，继续保持任务锁定和轮询，避免把下一条用户输入误当成新任务提交。
- 新增当前会话未完成任务列表：后端提供会话任务列表接口，前端展示未完成任务数量、任务摘要、状态、活动节点数，并提供逐项“停止任务”按钮供用户主动终止任务。
- 未完成任务列表默认折叠隐藏，只在标题栏保留数量、展开/收起和刷新入口，避免占用主对话区域。
- SQLQuery 结果表格增加横向滚动与气泡内宽度约束，避免宽表列数较多时撑出助手消息框。
- SQLQuery intent route 新增显式库名 / route_id 优先匹配：当用户补充“审定品种库”或“基因型数据库”时优先采用该路由，再回退到宽泛品种概览或 keyword 打分。
- `routing_rules.yaml` 补齐审定品种库 / 基因型数据库的库名关键词，并为审定品种库水稻 crop alias 增加“稻 / 粳 / 粳稻 / 籼稻”，使“龙粳18 + 审定品种库”这类补充信息可直接推断为水稻。
- 审定品种库单品种查询默认升级为业务详情输出：schema 裁剪保留申请者、育种者、品种来源、特征特性、产量表现、栽培要点、适种区域与审定意见等核心字段；确定性 SQL fallback 也会按详情/列表场景选择字段。
- SQLQuery 默认产品链路恢复内部 `result_summarize` 尾节点：SQLQuery 子代理可先生成可复用结果摘要，同时 `sql_execute_readonly` 继续提供表格 preview。
- SQLQuery 尾节点从 `result_summarize` 重命名并改造为 `result_filtering`：SQL 生成阶段继续用 `LIKE` 召回候选品种，尾节点通过 LLM 输出 `keep_row_indexes` 筛选真正符合用户需求的行，最终返回 `filtered_query_result` 表格；无 LLM 或 LLM 失败时保守保留候选表格并记录 fallback。
- 收紧 SQLQuery 单品种编号筛选规则：当用户查询“龙粳18”这类明确编号品种时，`result_filtering` 会只保留规范化等于“龙粳18”或“龙粳18号”的行，并剔除“龙粳1836 / 龙粳1823”等编号后继续追加数字的其他品种；该规则在无 LLM 或 LLM 误保留时也会作为后过滤生效。
- 修复 interrupt 反复追问时复用同一 `interrupt_id` 可能导致节点停在 `waiting_for_input` 但无 open interrupt 的问题；interrupt id 现在带 reason/input 指纹，避免“无限转圈”式悬挂。
- 排查并取消本地人工验证会话中遗留的 `task-7ff47608987d`：该任务停在 `sql_query.intent_route` 的 `waiting_for_input`，旧前端未支持 interrupt answer，导致后续普通提交被 ConversationSerialGuard 以 409 busy 拒绝。
- 新增默认自动规划 provider：用户不再需要显式选择 capability；数据库/品种/审定/基因型类问题会自动展开为 `SQLQuery` 六节点宏能力，再交给 `main_agent.respond` 结合上游结果生成自然语言最终回答，普通问题仍保持单主代理路径。
- SQLQuery 默认运行链路保留末端 `result_summarize` 节点，内部 DAG 回到 intent route、schema context、SQL generate、SQL guard、readonly execute、result summarize 六节点；主代理接收 SQLQuery 上游摘要结果做最终对话式整合。
- SQLQuery SQL 生成新增品种名 LIKE 匹配约束：LLM prompt 明确禁止 `variety_name = ...`，LLM 若返回严格等值条件会触发 fallback；确定性 fallback 统一使用 `variety_name LIKE '%关键词%'`。
- 主代理 Prompt 新增“上游能力结果上下文”注入，只暴露 SQLQuery 摘要、路由、行数等安全字段，让主代理可基于 capability 结果整合回答，而不是重新猜测或忽略已完成节点。
- 前端业务对话台移除“普通对话 / SQLQuery”手动模式选择，展示为“当前模式：自动规划”，数据库类输入仅提示主代理会自动判断是否调用 SQLQuery；自动 SQLQuery 路径仍会在主代理最终回答下展示折叠的结果卡片，未完成任务列表中的空 capability 也显示为“自动规划”。
- 完成完整 LLM Planner runtime 接入：默认自动规划入口先尝试 LLM 生成 public-only 高层 DAG，再经过 public validator、macro expander 与 internal validator 后执行；planner 输出非法、引用 internal capability 或 provider 不可用时自动 fallback 到确定性 `AutoWorkflowProvider`。
- Planner prompt 现在注入当前 public capability 清单并明确数据库问题优先规划 `sql_query.query -> main_agent.respond`；系统会强制用真实用户问题覆盖当前 public capability 的 `user_message` / `user_question`，并在 SQL-only 或未连线主代理计划中自动追加/重连主代理 finalizer，确保主代理接收上游结果上下文。
- 新增正式的 per-capability planner payload allowlist：`CapabilityPayloadPolicy` / `PlannerPayloadPolicy` 默认 fail-closed，策略随 capability 注册进入 `CapabilityRegistry`，LLM Planner prompt 会展示每个 public capability 允许的 payload 字段；只有 capability 明确声明的字段才会从 LLM planner payload 进入执行图，系统生成字段始终覆盖 planner 字段。当前 `main_agent.respond` 与 `sql_query.query` 均只是首批注册策略，由各自 capability 模块声明真实用户输入注入规则。
- `build_api_runtime` 新增 planner 注入 seam（fake text generator / LLM config / factory / reasoning effort / enable 开关），`ApiRuntime` 支持异步 workflow provider，并新增 `workflow.plan_built` audit-only 事件记录 planner route、fallback 状态与原因。
- 补充 LLM Planner orchestration 与 API 集成测试，覆盖成功展开、单主代理、payload 覆盖保护、capability registry payload policy、未来 capability 自定义 allowlist、自动 finalizer、未连线主代理重连、provider 异常 fallback、internal capability fallback 与非法 JSON fallback。
- 新增 `docs/Capability接入指南.md`，明确后续新增 capability 时在 `src/capabilities/<name>/`、`src/api/runtime.py`、planner payload allowlist、macro provider、AutoWorkflowProvider、测试与文档中分别要加入什么，避免把新能力逻辑写成 SQLQuery 式特判。
- 前端对话框新增上游能力执行感知：根据 `node.started` / `node.completed` 事件在助手气泡内展示“正在执行 SQLQuery / 主代理 / 未来 capability”的当前步骤，避免长时间只显示等待回答。
- 前端 Markdown 渲染补齐 GitHub 风格基础表格解析与横向滚动样式，使主代理返回的 Markdown 表格不再作为普通段落显示。
- 补齐 SQLQuery 内部 LLM 默认装配：真实 API runtime 现在会用 `config.yaml` 创建 SQLQuery 文本生成器，并同时传给 `sql_query.sql_generate` 与 `sql_query.result_filtering`；显式注入的 `llm_text_generator` 仍保持最高优先级，fake backend 明确关闭真实 SQLQuery LLM。
- SQLQuery SQL 生成 Prompt 与确定性 fallback 增加“X 系列 / 名字里带 X / 名称中包含 X”识别规则；例如“龙粳系列”会生成 `variety_name LIKE '%龙粳%'`，避免无 WHERE 条件只返回前 50 条无关数据。

### 2026-04-27 — 新增主代理思考控制

- 前端业务对话台在“当前模式”右侧新增“思考强度”下拉菜单（显示为“最底” / “低” / “中” / “高”，实际值为 `minimal` / `low` / `medium` / `high`，默认“中”）和“深度思考”开关。
- 提交消息时通过 metadata 独立传递 `main_agent_reasoning_effort` 与 `deep_thinking`：前者控制 reasoning effort，后者控制 LLM 请求 `extra_body.thinking.type` 为 `enabled` 或 `disabled`。
- 主代理执行器支持请求级 reasoning effort 与 thinking 开关覆盖，将 provider 返回的 `reasoning_content` 以 `main_agent.reasoning_delta` 前端事件输出，并在 LLM audit metadata 中记录实际档位和 thinking 状态。
- 调整主代理 LLM runtime 绑定，使真实 LLM client 可通过 `generate_text_with_thinking()` 同时产出 answer / reasoning 内容；保留旧的一参 fake streamer 兼容。
- 修复主代理 executor 默认 LLM fallback 仍调用 `stream_text()` 导致过滤 `reasoning_content` 的问题；已用真实 provider 探测确认字段位于 `choices[0].delta.reasoning_content` / `delta.model_extra.reasoning_content`，并用本地后端 API 验证可转发为 `main_agent.reasoning_delta`。
- 前端对话气泡新增安全 Markdown 渲染，并在助手消息中以“思考内容”框展示深度思考返回的 reasoning 内容；开启深度思考但 provider 未返回 `reasoning_content` 时也会显示占位提示。
- “思考内容”在正文开始并完成返回后默认折叠约 3 行，右上角提供展开/收起开关，内容文字统一使用灰色弱化展示。
- 补充前端 API/UI、Markdown 渲染、LLM client、主代理 executor 与 API runtime 回归测试，并通过前端测试/build 与后端相关测试。

### 2026-04-27 — 完成前端业务对话台 v1 与全栈启动脚本

- 基于 `docs/prd/frontend/00-前端业务对话台PRD.md` 新增 `docs/dev_processes/frontend/` Phase 0~5 开发过程文档，并补充 Ralph 执行版 PRD / Test Spec。
- 新增 `frontend/` React + TypeScript + Vite + Ant Design 前端业务对话台，支持普通主代理 streaming、SQLQuery 模式、任务状态、取消、capability 状态、artifact 摘要与简表预览降级展示。
- 新增前端 API/SSE client、task event reducer、SQLQuery artifact parser 与 Vitest/React Testing Library 覆盖，默认不展示 SQL / DAG / schema / audit 技术细节。
- 新增 `scripts/run_fullstack_dev.py`，默认使用仓库真实 FastAPI runtime 拉起后端和 Vite 前端，支持人工验证；如需 UI-only 验证可显式加 `--fake-backend` 使用 deterministic fake provider/数据库适配器。
- 调整 Vite build 分包策略，将 React、Ant Design、rc 组件和通用依赖拆成独立 vendor chunk，避免 Ant Design 依赖集中进入单个大 chunk。
- 更新 `README.md`、`AGENTS.md` 与 `docs/dev_processes/README.md`，记录前端工程、验证命令和全栈启动方式。

### 2026-04-27 — 补充前端业务对话台技术选型建议

- 在 `docs/prd/frontend/00-前端业务对话台PRD.md` 中新增技术选型建议与约束草案，推荐 v1 采用 `React + TypeScript + Vite + Ant Design + EventSource/SSE`。
- 明确 Next.js、Tailwind + shadcn/ui、WebSocket、Vercel AI SDK、AG-UI、Redux/Zustand 等备选方案的 v1 结论与暂不选原因。

### 2026-04-27 — 整理后端开发过程文档目录

- 将后端 Phase 0~8.2 开发过程文档统一迁移到 `docs/dev_processes/backend/`，并保留 `docs/dev_processes/README.md` 作为跨领域开发流程总入口。
- 同步更新 `README.md`、`AGENTS.md`、PRD 索引、一期计划与相关专题文档中的开发过程路径引用，避免继续指向旧的 `docs/dev_processes/Phase-*.md` 根目录路径。

### 2026-04-27 — 新增前端业务对话台 PRD 草案

- 基于当前后端 FastAPI / SSE / capability / artifact 实现事实，新增 `docs/prd/frontend/00-前端业务对话台PRD.md`，明确前端 v1 定位为内部业务用户对话台，而非研发调试台。
- 明确 v1 仅依赖现有 API：普通对话走 `main_agent.respond`，数据库查询走 `sql_query.query`，SQLQuery 默认展示自然语言摘要与简表预览。
- 更新 `docs/prd/frontend/README.md` 与 `docs/prd/README.md`，将前端 PRD 状态从预留改为已开始，并记录 v1 非目标与后续后端 API 增强项。

### 2026-04-27 — 同步当前实现基线的文档、PRD 与 Agent 规则

- 更新 `AGENTS.md`、`README.md`、`docs/一期核心模块边界.md`、`docs/一期开发计划.md` 与 `docs/SQLQuery-LLM版本改造方案.md`，移除“仓库仍保持最小化/架构未定”等过期表述，改为当前后端实现目录职责、PRD 入口、SQLQuery LLM 落点和后续维护口径。
- 将后端 PRD 统一迁移到 `docs/prd/backend/`，新增 `docs/prd/README.md` 与 `docs/prd/frontend/README.md`，并更新 `docs/prd/backend/00-主代理框架PRD.md` 的 backend / frontend 边界、专题索引与使用建议。
- 新增 `docs/prd/backend/07-SQLQuery-LLM增强与真实库验证.md`、`08-主代理Skill兼容与真实LLM运行时.md`、`09-高层DAG规划与SQLQuery宏能力边界.md`，把 Phase 5.5、Phase 8、Phase 8.1、Phase 8.2 已超出原 PRD 粒度的能力纳入正式 PRD。
- 同步更新 `docs/dev_processes/README.md`、`docs/LLM接入阶段建议.md`、SQLQuery prompt 模板及各阶段文档中的 PRD 路径引用，移除旧 `docs/主代理框架PRD.md` 与根级 `docs/prd/0x-*` 引用。
- 更新 `AGENTS.md` 编码风格规则，把早期“避免过早抽象”改为更短的“避免无证据提前泛化，也避免在已有稳定复用需求时继续复制粘贴”。

### 2026-04-27 — 完成 Phase 8.2 主代理真实 LLM Runtime 绑定

- 新增 `docs/dev_processes/Phase-8.2-主代理真实LLM运行时绑定与Smoke验证.md`，并在 `docs/dev_processes/README.md`、`docs/LLM接入阶段建议.md` 中补齐索引和阶段说明。
- 为主代理真实 LLM 增加 runtime 显式装配参数：`main_agent_llm_config`、`main_agent_llm_config_path`、`main_agent_llm_client_factory`、`main_agent_reasoning_effort`；保留 fake stream 最高优先级，默认测试仍不访问真实 provider。
- `LLMClient` 新增 `safe_metadata()`；主代理 `main_agent.llm_call` / `main_agent.llm_fallback` 现在记录不含 secret 的 provider metadata，并过滤 API key、prompt、base_url 等敏感 key；fallback diagnostic 只记录异常类型，不记录 provider 原始异常文本。
- 新增 `scripts/smoke_main_agent_llm.py` 作为显式手工 smoke 入口，并同步更新 `README.md` 与 `AGENTS.md`；本地 `config.yaml` smoke 已验证普通主代理消息可通过真实 provider 完成。
- 补充主代理 runtime factory、safe metadata、fallback 安全事件相关测试，并回归 `tests/integrations`、`tests/capabilities/main_agent`、`tests/api`、`tests/orchestration`。

### 2026-04-27 — 收紧 SQLQuery Prompt Schema 与 MySQL 只读适配器

- SQLQuery schema context 现在向 prompt 注入 `selected_column_details`，把 `schema_metadata.yaml` 中裁剪后的字段名、`sql_type` 与描述一起传给 LLM。
- SQL 生成 prompt 要求 LLM 回填 `column_types_used`，并在 `sql_generate` 中校验 `columns_used`、`column_types_used` 与 SQL 字段引用必须匹配裁剪后的 schema。
- 确认 `multi_agent` 环境中 `PyMySQL` 可用，并将 `PyMySQL==1.1.2` 写入 `requirements.txt`。
- 完善 `MySQLReadonlyAdapter`：支持懒加载复用 SQLAlchemy engine、注入 `engine_factory` 测试 seam，并提供同步 / 异步关闭入口释放连接池。
- 用真实只读库核对 `schema_metadata.yaml` 表/字段存在性，并通过真实 MySQL smoke 查询与 SQLQuery workflow 验证 adapter 可查到“龙粳33”；补充 adapter 集成测试覆盖 guard token、runner seam、engine 复用与 dispose。

### 2026-04-27 — 补齐 Phase 8.1 LLM Planner 前置契约

- 按 Phase 8.1 文档计划补齐 LLM Planner 前置契约：新增 `WorkflowPlanValidator`，校验 public capability、重复节点、未知依赖、环形依赖与 JSON-serializable input payload。
- 新增 `WorkflowExpander`，将高层 `sql_query.query` 宏能力展开为 SQLQuery 固定内部子工作流，并正确改写上游 / 下游依赖。
- 新增 Planner 输出 JSON schema、fake LLM 输出解析 seam 与回归测试，继续保持“不实现完整 LLM Planner、不开放 SQLQuery 内部节点给 Planner”的阶段边界。
- 更新 Phase 8.1 文档状态与补齐记录，明确首轮 public SQLQuery 边界与本次 Planner 前置契约均已完成。

### 2026-04-24 — 完成 SQLQuery LLM 增强与 Phase 8 主代理首轮能力

- 建立 Phase 5.5 / Phase 8 / Phase 8.1 的专题文档与阶段索引：新增 SQLQuery LLM 增强专题、Codex Skill 兼容层专题、SQLQuery 宏能力与 LLM 动态 DAG 规划设计稿，并同步更新 `docs/dev_processes/README.md` 与 `docs/LLM接入阶段建议.md`。
- 收口 SQLQuery 公开命名与能力边界：对外保留 `sql_query` / `sql_query.query`，新增 public capability `sql_query.query`（展示名 `SQLQuery`），默认隐藏并拒绝外部直接调用 `sql_query.*` 内部节点及旧 `nl2sql*` / `sqlquery*` id。
- 完成 Phase 8 主代理首轮实现：新增 `main_agent.respond`、`MainAgentWorkflowProvider`、`CompositeExecutor` 与 workflow router；普通消息默认进入主代理，显式 SQL 查询进入固定 SQLQuery workflow。
- 落地 Codex Skill 兼容层与主代理 prompt 上下文：新增 `src/integrations/codex_skills/` 与 `src/capabilities/main_agent/`，支持解析 `SKILL.md`、识别 IO / scripts 扩展、匹配 skill、受控 Python 脚本 runner、上传 artifact 脱敏 metadata 注入，以及 `main_agent.output_delta` / `main_agent.output_final` streaming 事件。
- 完成 Phase 5.5 SQLQuery LLM 首轮 TDD 实施：新增 `src/capabilities/sql_query/prompt_builders.py`、`llm_utils.py`，改造 `sql_generate` 与 `result_summarize` 支持注入 LLM 文本生成器、结构化 JSON 输出、clarify / reject、rows preview、确定性 fallback 与 `sql_query.llm_call` / `sql_query.llm_fallback` 审计事件。
- 整理 LLM client seam：将根目录 `llm_client.py` 移入 `src/integrations/llm_client.py`，改用 `yaml.safe_load` 与初始化期配置读取，补齐 `generate_text()`、`generate_text_with_thinking()`、`stream_text()` 与 `ReasoningEffort` 范围收窄。
- 修复 interrupt/resume 相关竞态：避免 late open interrupt 覆盖 answered 状态，并在恢复调度时重置旧 workflow 节点状态、先落库 `WAITING_FOR_INPUT` 再暴露 open interrupt。
- 补充主代理、Skill 兼容层、SQLQuery LLM、observability 与 e2e 测试；新增 `tests/integrations` 与 `tests/capabilities/main_agent` 最小测试命令，并同步更新 `README.md`、`AGENTS.md`。
- 将本地 `config.yaml` 与本周工作周报草稿加入 `.gitignore`，新增 `docs/工作周报模板.md`；将 Phase 5.5 / SQLQuery 示例查询统一为“龙粳33”，并用真实只读数据库核对基础品种、基因型预览、籼粳成分与审定信息。

### 2026-04-23 — 完成一期 Phase 0~7 开发计划、实现与验收

- 产出一期计划与测试规格：新增 `.omx/plans/prd-20260423-main-agent-framework-phase1.md`、`.omx/plans/test-spec-20260423-main-agent-framework-phase1.md` 与上下文快照，并将计划转为 `docs/一期开发计划.md`、`docs/dev_processes/README.md` 和 Phase 0~7 系列文档。
- 冻结一期边界与记忆口径：收紧 Phase 4 / Phase 5 分工、拆分 Phase 6 / Phase 7，明确一期只做会话延续型记忆，不做跨任务知识沉淀；会话记忆作为主框架状态数据落 SQLite，后续同构迁移到 PostgreSQL。
- 完成 Phase 1 core：新增 `src/core/` 共享模型、状态枚举、基础错误与 contract 定义，补齐 `tests/core/`，并新增 `docs/一期核心模块边界.md`。
- 完成 Phase 2 storage：新增 `src/storage/` 与 `src/storage/sqlite/`，落地 SQLite base / session / bootstrap、ORM model、repository、async storage façade、`InterruptAnswer` 与 `StoragePort` 增量；新增 `docs/dev_processes/Phase-2-SQLite状态存储表结构草案.md` 与 `tests/storage/`。
- 完成 Phase 3 lifecycle：新增 `src/lifecycle/`，落地 task state machine、mailbox、interrupt/resume、cancellation、conversation serial guard、typed payload 基础设施与相关存储 primitive；新增 `tests/lifecycle/` 与任务上下文终止状态流转图（Markdown / PNG）。
- 完成 Phase 4 orchestration：新增 `src/orchestration/`，落地 capability / instance registry、scheduler、workflow/task plan、completion policy、strict reject backpressure、orchestration service 与所需状态查询 seam；新增 `tests/orchestration/` 并修订主代理编排能力流程图。
- 完成 Phase 5 SQLQuery MVP：新增 `src/capabilities/sql_query/` 与 `src/integrations/`，复用 `src/sql_query/` 与 `configs/sql_query/*.yaml` 接入 intent route、schema context、SQL generate、SQL Guard、readonly execute、result summarize 六节点 workflow；新增 `tests/capabilities/sql_query/`、SQLQuery 子代理结构图，以及 SQLQuery LLM 改造方案和 LLM 接入阶段建议图文档。
- 完成 Phase 6 API/SSE：新增 `src/api/`、FastAPI app、DTO、消息提交/任务查询/SSE/取消/任务图/产物/能力目录接口、进程内事件 broker 与 JSONL audit logger；补齐 live fan-out、late result discard、取消事件与 `tests/api/`。
- 完成 Phase 7 验收与二期评估输入：新增 `tests/e2e/`、`tests/observability/`，覆盖 happy path、guard blocked、interrupt/resume、cancel late result ignored 与 JSONL audit；新增 `docs/一期验收报告.md` 与 `docs/第二阶段评估输入.md`，并同步更新各阶段最小测试命令到 `README.md`、`AGENTS.md` 与 Phase 文档。
- 更新仓库规则：在 `AGENTS.md` 中新增开始分析、设计、编码或文档修改前必须先阅读 `CHANGELOG.md` 最近相关条目的要求。

### 2026-04-22 — 初始化主代理框架设计基线并建立仓库级设计资产

- 建立并持续收口主代理框架 PRD，明确主框架只负责任务拆解、编排、分发，具体业务能力以下层 capability 形式接入。
- 将 PRD 重构为“总览 + 专题”结构，新增 `docs/prd/` 专题文档，降低单文档耦合度并方便后续按模块做计划与实现。
- 补齐 SQLQuery 首个 MVP 相关设计资产，包括数据库结构说明、Prompt 输入模板、业务路由规则、schema 元数据与 SQL Guard 规则。
- 新增 `src/sql_query/` 的 schema context builder 骨架与基础模型定义，用于后续按 TDD 推进 capability 落地。
- 导出当前 `multi_agent` 环境依赖到根目录 `requirements.txt`，并补充仓库规则：优先基于现有依赖实现功能。
- 新增根目录 `CHANGELOG.md`，作为仓库级开发记录入口，并在仓库规则中明确要求每日开发结束时手动补记当天工作内容。
- 收口主代理思维模式、完成判定闭环、优先级两层模型、模块划分与主框架/Capability 上下级边界等关键架构决策。
- 更新 `.gitignore`，把 `.codex/` 作为本地运行时目录忽略，不再纳入版本控制。
