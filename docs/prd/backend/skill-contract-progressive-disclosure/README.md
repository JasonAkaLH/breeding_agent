# Skill Contract 渐进式披露专题 PRD

本目录维护项目级 Skill bundle v2-only clean cutover 的正式 PRD：轻量 `SKILL.md`、平台执行契约 `skill.contract.yaml`、机器输入 schema `schemas/*.input.yaml`、按需资源读取 `SkillResourceService`、显式 `skill.*` 节点执行，以及 v1 manifest 注册/执行路径删除。

## 文档入口

| 顺序 | 文档 | 说明 | 状态 |
| --- | --- | --- | --- |
| 00 | [`00-SkillContract渐进式披露与显式执行总纲PRD.md`](00-SkillContract渐进式披露与显式执行总纲PRD.md) | 跨阶段目标、不变量、总体架构、验收标准与风险控制 | 加固完成，待实施 |
| 01 | [`01-SkillContract解析与注册PRD.md`](01-SkillContract解析与注册PRD.md) | `skill.contract.yaml` 解析、capability 注册、v2 diagnostics；无 contract Skill 不注册 | 待实施 |
| 02 | [`02-InputSchema与SchemaSelectorPRD.md`](02-InputSchema与SchemaSelectorPRD.md) | `schemas/*.input.yaml` 解析、schema selector、selected-schema 作用域 required | 待实施 |
| 03 | [`03-SkillResourceService按需读取PRD.md`](03-SkillResourceService按需读取PRD.md) | bundle 内默认可读 + 黑名单 + audience policy 的按需资源读取、安全裁剪、脱敏与审计 | 待实施 |
| 04 | [`04-PublicProfile与主代理适配PRD.md`](04-PublicProfile与主代理适配PRD.md) | 主代理 public profile、soft binding、用法问题 resource read、执行请求显式 `skill.*` | 待实施 |
| 05 | [`05-SkillExecutorV2与SlotCollectionV2PRD.md`](05-SkillExecutorV2与SlotCollectionV2PRD.md) | v2 SkillExecutor、InputResolver v2、slot_collection v2、output contract validation | 待实施 |
| 06 | [`06-项目级Skill迁移PRD.md`](06-项目级Skill迁移PRD.md) | field-design、field-analysis、rice-genie、OCR、SQLQuery 的项目级迁移 | 待实施 |
| 07 | [`07-文档API测试与旧路径删除PRD.md`](07-文档API测试与旧路径删除PRD.md) | 文档、API、测试矩阵与 v1 manifest 注册/执行路径删除 | 待实施 |

## 交付关系

```text
01 SkillContract 解析/注册
 ├─ 02 InputSchema/SchemaSelector
 ├─ 03 SkillResourceService
 ├─ 04 PublicProfile/主代理适配（依赖 02、03 的公开摘要与资源读取）
 └─ 05 SkillExecutorV2/SlotCollectionV2（依赖 02、03 的 schema/resource 能力）
       └─ 06 项目级 Skill 迁移
             └─ 07 文档/API/测试/旧路径删除
```

03 可与 02 并行实施；04/05 必须同时依赖 01-03 的稳定契约。每个编号 PRD 都是可独立验收的交付单元，00 只维护跨阶段不变量和总体口径。

## 关联 PRD

- `docs/prd/backend/08-主代理Skill兼容与真实LLM运行时.md`
- `docs/prd/backend/12-Skill一等Capability能力池PRD.md`
- `docs/prd/backend/13-Skill动态加载与热部署PRD.md`
- `docs/prd/backend/15-SkillExecutor实现需求PRD.md`
- `docs/superpowers/specs/2026-06-04-skill-slot-dialogue-design.md`
