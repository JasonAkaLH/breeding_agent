# PRD 目录索引

本目录按产品侧面组织 PRD，避免后续前端设计与既有后端主代理框架 PRD 混放。

## 目录结构

| 目录 | 范围 | 状态 |
|---|---|---|
| `docs/prd/backend/` | 后端主代理框架、Skill runtime、LLM runtime、对话记忆、编排与 API 契约 | 当前正式基线 |
| `docs/prd/backend/postgresql-state-platform/` | PostgreSQL State Platform 防死锁、写队列、runtime integration 与 SQLite migration/cutover Phase PRD | Phase 拆分已落地；待实施 |
| `docs/prd/backend/prompt-envelope/` | 大语言模型提示词信封、动态上下文预算、KV Cache 友好组装与多大语言模型调用档案分步 PRD | Phase 拆分已落地；待实施 |
| `docs/prd/MCP/` | MCP Runtime 长任务流式 SSE、Rust sidecar 联合改造 Phase PRD、MCP Client 四版本兼容 PRD，以及官方 SDK 引入轨道 | Phase 拆分已冻结；client 四版本兼容与官方 SDK PRD 已新增 |
| `docs/prd/frontend/` | 前端产品体验、页面结构、交互与视觉设计 | 已开始，v1 业务对话台 PRD 草案 |

## 后端 PRD 入口

后端 PRD 的总览入口是：`docs/prd/backend/00-主代理框架PRD.md`。

- 对话记忆与压缩 PRD：`docs/prd/backend/10-对话上下文记忆与压缩PRD.md`。
- Skill 输出文件 Artifact 与下载 PRD：`docs/prd/backend/11-Skill输出文件Artifact与下载PRD.md`。
- Skill 一等 Capability 能力池 PRD：`docs/prd/backend/12-Skill一等Capability能力池PRD.md`。
- Skill 动态加载与热部署 PRD：`docs/prd/backend/13-Skill动态加载与热部署PRD.md`。
- MCP Runtime 实现需求 PRD：`docs/prd/backend/14-MCPRuntime实现需求PRD.md`。
- Skill Executor 实现需求 PRD：`docs/prd/backend/15-SkillExecutor实现需求PRD.md`。
- Rust 化 Runtime 模块评估 PRD：`docs/prd/backend/16-Rust化Runtime模块评估PRD.md`。
- MCP 长任务与流式 SSE PRD：`docs/prd/backend/17-MCP长任务流式SSEPRD.md`。
- 失败自检、恢复与 Fallback 控制层 PRD：`docs/prd/backend/18-失败自检恢复与Fallback控制层PRD.md`。
- 失败自检、恢复与 Fallback 控制层分步 PRD：`docs/prd/backend/failure-recovery/README.md`。
- PostgreSQL State Platform 防死锁与写队列 Phase PRD：`docs/prd/backend/postgresql-state-platform/README.md`。
- 大语言模型提示词信封分步 PRD：`docs/prd/backend/prompt-envelope/README.md`。
- Rust 化实施专题拆分入口：`docs/prd/rust/README.md`。
- MCP Runtime 联合改造 Phase PRD 入口：`docs/prd/MCP/README.md`。
- MCP Client 四版本兼容 PRD 入口：`docs/prd/MCP/compatibility/README.md`。
- MCP Client 官方 SDK 引入与四版本完整兼容 PRD 入口：`docs/prd/MCP/official-sdk-compatibility/README.md`（首批 remote HTTP/HTTPS ordinary tools；stdio 仍需 sandbox-gated 后续 PRD）。

新增或补齐后端能力范围时，应同步更新：
1. `docs/prd/backend/00-主代理框架PRD.md` 的专题索引；
2. 对应专题 PRD 文件；
3. `CHANGELOG.md` 的当天开发记录。


## 前端 PRD 入口

前端 v1 PRD 入口是：`docs/prd/frontend/00-前端业务对话台PRD.md`。

当前前端 v1 严格基于已实现后端 API，定位为内部业务用户对话台；后续如补充调试台、权限、上传、历史中心，应新增或拆分专题 PRD。

## 文档维护口径

- `docs/prd/backend/*.md`：描述“应具备什么能力、边界和验收口径”。
- 如果实现已经超过 PRD 粒度，应优先补 PRD，再继续扩展实现或前端设计。
