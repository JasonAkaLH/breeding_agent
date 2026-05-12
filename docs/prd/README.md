# PRD 目录索引

本目录按产品侧面组织 PRD，避免后续前端设计与既有后端主代理框架 PRD 混放。

## 目录结构

| 目录 | 范围 | 状态 |
|---|---|---|
| `docs/prd/backend/` | 后端主代理框架、SQLQuery、LLM runtime、对话记忆、编排与 API 契约 | 当前正式基线 |
| `docs/prd/frontend/` | 前端产品体验、页面结构、交互与视觉设计 | 已开始，v1 业务对话台 PRD 草案 |

## 后端 PRD 入口

后端 PRD 的总览入口是：`docs/prd/backend/00-主代理框架PRD.md`。

- 对话记忆与压缩 PRD：`docs/prd/backend/10-对话上下文记忆与压缩PRD.md`。
- Skill 输出文件 Artifact 与下载 PRD：`docs/prd/backend/11-Skill输出文件Artifact与下载PRD.md`。
- Skill 一等 Capability 能力池 PRD：`docs/prd/backend/12-Skill一等Capability能力池PRD.md`。
- Skill 动态加载与热部署 PRD：`docs/prd/backend/13-Skill动态加载与热部署PRD.md`。
- MCP Runtime 实现需求 PRD：`docs/prd/backend/14-MCPRuntime实现需求PRD.md`。

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
