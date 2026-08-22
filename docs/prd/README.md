# PRD 目录索引

本目录按产品侧面组织 PRD，避免后续前端设计与既有后端主代理框架 PRD 混放。

## 目录结构

| 目录 | 范围 | 状态 |
|---|---|---|
| `docs/prd/backend/` | 后端主代理框架、Skill runtime、LLM runtime、对话记忆、编排与 API 契约 | 当前正式基线 |
| `docs/prd/backend/postgresql-state-platform/` | PostgreSQL State Platform 防死锁、写队列、runtime integration 与 SQLite migration/cutover Phase PRD | Phase 拆分已落地；待实施 |
| `docs/prd/backend/prompt-envelope/` | 大语言模型提示词信封、动态上下文预算、KV Cache 友好组装与多大语言模型调用档案分步 PRD | Phase 拆分已落地；待实施 |
| `docs/prd/backend/unified-agent-loop/` | 统一同模型Agent Loop总纲与Phase 0～7实施依赖PRD：Model Contract、Agent Storage/Lease、Invocation/Skill/MCP、核心Loop、Continuation、API/Frontend、Clean Cutover和破坏性Schema删除 | Phase 0～5 proof_complete；P6-A因外部Agent Skill required skip保持blocked |
| `docs/prd/backend/table-upload-normalization/` | 表格上传编码兼容、表头技术清洗、Excel sheet 选择与 prompt-safe 摘要上限分步 PRD | Phase 拆分已落地；待实施 |
| `docs/prd/backend/20-对话文件本地资源文件系统PRD.md` | 对话上传文件的本地持久化、`index.md` 物化索引、Skill workspace manifest 与删除清理语义 | MVP 已落地；后续补 LLM/OCR 描述流水线 |
| `docs/prd/backend/21-对话文件历史与智能选择PRD.md` | 合并上传历史消息与聊天式文件选择的父兼容入口：统一 `file_upload` 历史、active resource 事实源、conversation file context、selector 消歧、recent usage 与 deleted 不可复用语义 | 兼容入口；阶段零至阶段五已实施 |
| `docs/prd/backend/conversation-file-history-selection/` | 对话文件历史与智能选择分步 PRD：数据模型、上传历史、memory 安全、selector shadow、interrupt 绑定、灰度发布六阶段 | 阶段零至阶段五已实施；后续仅保留 guarded multi-select 放量/观测增强 |
| `docs/prd/backend/22-Skill运行闭环Workbench总纲PRD.md` | 平台层 Skill 运行闭环 Workbench、内部 `workbench.*` capability、执行后验证与受控重编排总纲 | 兼容入口；已拆分 runtime replan 主线阶段 PRD |
| `docs/prd/backend/skill-workbench/` | Skill 运行闭环 Workbench 分步 PRD：Policy/runtime state/stage placement、内部 capability/executor、runtime loop/finalizer/Skill refinement、事件 graph prompt 脱敏、contract quality diagnostics | 已拆分 runtime replan 主线；待实施 |
| `docs/prd/backend/23-能力缺失LLMFallback披露PRD.md` | 无匹配 Skill/MCP/capability 时的 LLM fallback、事实披露、Workbench 停止、事件审计与历史提示契约 | 父兼容入口；已拆分 Phase PRD，待实施 |
| `docs/prd/backend/capability-missing-fallback/` | 能力缺失 LLM fallback 披露分步 PRD：现状清理、Plan metadata 契约、后端 full fallback、前端 notice/history、partial fallback/Replanner 审计五阶段 | Phase 拆分已落地；待实施 |
| `docs/prd/backend/skill-contract-progressive-disclosure/` | Skill Contract v2-only 渐进式披露、显式 skill.* 执行、input schema、ResourceService 与 v1 manifest 路径删除 | v2-only 复审完成；待实施 |
| `docs/prd/MCP/` | MCP Runtime 长任务流式 SSE、Rust sidecar、Client 多版本兼容、官方 SDK，以及用户级按需 MCP 轨道 | 用户级按需阶段一已实现；阶段二、三及既有 Rust 长任务 Phase 持续治理 |
| `docs/prd/MCP/user-scoped-on-demand/` | 用户专属 MCP 配置、凭据加密、按需 Gateway、两级路由、工具授权、任务执行、灰度切换与旧全局 Runtime 下线 | 阶段一已实现；阶段二、三待实施 |
| `docs/prd/frontend/` | 前端产品体验、页面结构、交互与视觉设计 | v1 业务对话台与发送时上传文件专题已落地 |
| `docs/prd/frontend/deferred-message-upload/` | 对话台文件选择/拖拽后的浏览器草稿暂存、发送时上传与现有 conversation file resource API 衔接 | 已实施，待最终合并 |

## 后端 PRD 入口

后端 PRD 的总览入口是：`docs/prd/backend/00-主代理框架PRD.md`。

- 对话记忆与压缩 PRD：`docs/prd/backend/10-对话上下文记忆与压缩PRD.md`。
- Skill 输出文件 Artifact 与下载 PRD：`docs/prd/backend/11-Skill输出文件Artifact与下载PRD.md`。
- Skill 一等 Capability 能力池 PRD：`docs/prd/backend/12-Skill一等Capability能力池PRD.md`。
- Skill 动态加载与热部署 PRD：`docs/prd/backend/13-Skill动态加载与热部署PRD.md`。
- MCP Runtime 实现需求 PRD：`docs/prd/backend/14-MCPRuntime实现需求PRD.md`。
- Skill Executor 实现需求 PRD：`docs/prd/backend/15-SkillExecutor实现需求PRD.md`。
- Skill Contract 渐进式披露 PRD：`docs/prd/backend/skill-contract-progressive-disclosure/README.md`。
- Rust 化 Runtime 模块评估 PRD：`docs/prd/backend/16-Rust化Runtime模块评估PRD.md`。
- MCP 长任务与流式 SSE PRD：`docs/prd/backend/17-MCP长任务流式SSEPRD.md`。
- 失败自检、恢复与 Fallback 控制层 PRD：`docs/prd/backend/18-失败自检恢复与Fallback控制层PRD.md`。
- 失败自检、恢复与 Fallback 控制层分步 PRD：`docs/prd/backend/failure-recovery/README.md`。
- 表格上传编码兼容与表头规范化分步 PRD：`docs/prd/backend/table-upload-normalization/README.md`。
- 表格上传编码兼容与表头规范化历史兼容入口：`docs/prd/backend/19-表格上传编码兼容与表头规范化PRD.md`。
- 对话文件本地资源文件系统 PRD：`docs/prd/backend/20-对话文件本地资源文件系统PRD.md`。
- 对话文件历史与智能选择兼容入口：`docs/prd/backend/21-对话文件历史与智能选择PRD.md`。
- 对话文件历史与智能选择分步 PRD：`docs/prd/backend/conversation-file-history-selection/README.md`。
- Skill 运行闭环 Workbench 兼容入口：`docs/prd/backend/22-Skill运行闭环Workbench总纲PRD.md`。
- Skill 运行闭环 Workbench 分步 PRD：`docs/prd/backend/skill-workbench/README.md`。
- 能力缺失 LLM fallback 披露兼容入口：`docs/prd/backend/23-能力缺失LLMFallback披露PRD.md`。
- 能力缺失 LLM fallback 披露分步 PRD：`docs/prd/backend/capability-missing-fallback/README.md`。
- PostgreSQL State Platform 防死锁与写队列 Phase PRD：`docs/prd/backend/postgresql-state-platform/README.md`。
- 大语言模型提示词信封分步 PRD：`docs/prd/backend/prompt-envelope/README.md`。
- 统一同模型 Agent Loop 分阶段 PRD：`docs/prd/backend/unified-agent-loop/README.md`。
- Rust 化实施专题拆分入口：`docs/prd/rust/README.md`。
- MCP Runtime 联合改造 Phase PRD 入口：`docs/prd/MCP/README.md`。
- MCP Client 多版本兼容 PRD 入口：`docs/prd/MCP/compatibility/README.md`（Python Gateway 五版本已实现；Rust Sidecar 仍保持已验证的 `2025-11-25` 边界）。
- MCP Client 官方 SDK 引入与当前四版本完整兼容 PRD 入口：`docs/prd/MCP/official-sdk-compatibility/README.md`；`2026-07-28` 由用户级按需 MCP 三阶段 PRD 增量治理。
- 用户级按需 MCP 三阶段 PRD：`docs/prd/MCP/user-scoped-on-demand/01-用户级MCP配置凭据与按需GatewayPRD.md`、`02-MCP两级路由授权与任务执行闭环PRD.md`、`03-按需MCP灰度切换与旧Runtime下线PRD.md`。

新增或补齐后端能力范围时，应同步更新：
1. `docs/prd/backend/00-主代理框架PRD.md` 的专题索引；
2. 对应专题 PRD 文件；
3. `CHANGELOG.md` 的当天开发记录。


## 前端 PRD 入口

前端 v1 PRD 入口是：`docs/prd/frontend/00-前端业务对话台PRD.md`。

发送时上传文件专题入口是：`docs/prd/frontend/deferred-message-upload/01-deferred-message-upload-prd.md`，测试规格见同目录 `test-spec-deferred-message-upload.md`。

当前前端 v1 严格基于已实现后端 API，定位为内部业务用户对话台；对话文件上传已升级为草稿附件 + 已保存文件面板体验，文件选择/拖拽先作为发送框上方的浏览器草稿附件，随消息发送时再调用既有上传 API，发送后才进入右侧文件面板。后续如补充调试台、权限、历史中心或更完整文件预览，应新增或拆分专题 PRD。

## 文档维护口径

- `docs/prd/backend/*.md`：描述“应具备什么能力、边界和验收口径”。
- 如果实现已经超过 PRD 粒度，应优先补 PRD，再继续扩展实现或前端设计。
