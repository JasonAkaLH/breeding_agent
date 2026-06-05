# Skill Contract 渐进式披露专题 PRD

本目录维护项目级 Skill bundle 新结构的正式 PRD：轻量 `SKILL.md`、平台执行契约 `skill.contract.yaml`、机器输入 schema `schemas/*.input.yaml`、按需资源读取 `SkillResourceService`、显式 `skill.*` 节点执行，以及 legacy `auto_run` 路径的兼容边界。

## 文档入口

| 文档 | 说明 | 状态 |
| --- | --- | --- |
| `00-SkillContract渐进式披露与显式执行PRD.md` | 新 Skill contract 结构、主代理适配、SkillExecutor v2、slot_collection v2、ResourceService、迁移/回滚与验收测试矩阵 | 设计确认，待实施计划拆解 |

## 关联 PRD

- `docs/prd/backend/08-主代理Skill兼容与真实LLM运行时.md`
- `docs/prd/backend/12-Skill一等Capability能力池PRD.md`
- `docs/prd/backend/13-Skill动态加载与热部署PRD.md`
- `docs/prd/backend/15-SkillExecutor实现需求PRD.md`
- `docs/superpowers/specs/2026-06-04-skill-slot-dialogue-design.md`
