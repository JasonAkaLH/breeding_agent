# PRD 目录索引

本目录按产品侧面组织 PRD，避免后续前端设计与既有后端主代理框架 PRD 混放。

## 目录结构

| 目录 | 范围 | 状态 |
|---|---|---|
| `docs/prd/backend/` | 后端主代理框架、SQLQuery、LLM runtime、编排与 API 契约 | 当前正式基线 |
| `docs/prd/frontend/` | 前端产品体验、页面结构、交互与视觉设计 | 预留，后续展开 |

## 后端 PRD 入口

后端 PRD 的总览入口是：`docs/prd/backend/00-主代理框架PRD.md`。

新增或补齐后端能力范围时，应同步更新：
1. `docs/prd/backend/00-主代理框架PRD.md` 的专题索引；
2. 对应专题 PRD 文件；
3. `docs/dev_processes/README.md` 的阶段索引；
4. `CHANGELOG.md` 的当天开发记录。

## 与开发流程文档的关系

- `docs/prd/backend/*.md`：描述“应具备什么能力、边界和验收口径”。
- `docs/dev_processes/*.md`：描述“按什么阶段实现、怎么验证、当前完成到哪里”。
- 如果实现已经超过 PRD 粒度，应优先补 PRD，再继续扩展实现或前端设计。
