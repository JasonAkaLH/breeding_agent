# 前端 PRD 索引

本目录用于存放前端产品体验、页面结构、交互与视觉设计 PRD。

## 当前正式入口

| 文档 | 范围 | 状态 |
|---|---|---|
| `00-前端业务对话台PRD.md` | 基于当前后端能力的 v1 业务用户对话台：主代理 streaming、数据查询 Skill 表格预览、任务状态、取消与 artifacts 展示 | 草案 |

## 当前 v1 产品决策摘要

- 前端第一版定位为 **业务用户对话台**，不是研发/运维调试台。
- v1 严格基于当前已实现 API，不要求后端新增接口作为前置条件。
- 默认发送 `capability_id=null`，由后端自动规划；数据库/品种/审定/基因型类问题由主代理自动调用 数据查询 Skill 后整合回答，不再要求用户手动选择模式。
- 数据查询 Skill 默认把表格结果交给主代理整合回答，前端展示主代理回答 + 简表预览，不默认展示 SQL / schema / guard / 审计细节。
- v1 不做用户/权限系统，不做通用文件上传，不做研发调试台。

## 建议阅读顺序

1. `docs/prd/frontend/00-前端业务对话台PRD.md`
2. `docs/prd/backend/00-主代理框架PRD.md`
3. `docs/prd/backend/05-API与核心数据模型.md`
4. `docs/prd/backend/08-主代理Skill兼容与真实LLM运行时.md`
5. 具体可移除数据类 Skill 的前端结果展示契约（由对应 Skill bundle 自带 docs 维护）
- [Slash Skill Command PRD Set](slash-skill-command/README.md)：拆分为前端 slash Skill command MVP 与 pending Skill context continuation 两份 PRD。
