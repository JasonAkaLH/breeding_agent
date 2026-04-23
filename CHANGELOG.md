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
