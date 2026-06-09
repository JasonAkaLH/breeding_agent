---
name: field-analysis
description: >-
  分析随机区组、对角线增广等田间试验表型数据并生成章节化 field-analysis-report-v1 报告。适用于田间试验数据、表型结果、RCBD/随机区组试验、Diagonal/对角线增广设计、check 对比、描述统计、方差分析、LSD 分组、数据质量检查、空间校正诊断、多地点稳定性分析，以及询问 loc_id、rep_num、entry_id、ped_id、trait、value、check_type、ranges、pass 等输入列含义的场景。
---

# 田间数据分析智能体

## 总纲

使用此 Skill 对田间测试表型数据进行分析，重点支持：

- `rcbd`：randomized complete block design / 随机区组设计。
- `diagonal`：diagonal augmented design / 对角线增广设计。

默认运行完整章节化报告。此 Skill 面向田间测试和高级试验评估；默认回答不要引入 GCA、parent combining ability、hybrid BLUP、heritability 或 variance components 等非本报告范围内容。

平台执行事实源由 `skill.contract.yaml` 和当前 selected input schema 决定；用户可见输入列、报告章节和解释规则必须优先从 references 读取。

## 工作流

1. 确认用户提供了田间表型数据，并明确设计类型为 `rcbd` 或 `diagonal`。
2. 如果缺少输入文件或设计类型，发起结构化追问；不要猜测设计类型。
3. 让平台执行层生成完整报告和 artifact。
4. 以报告中的 `chapters` 为主要事实入口，先概述章节状态，再提炼关键发现。
5. 用户追问时优先围绕当前报告摘要、章节和 artifact 解释；缺少报告事实时，请用户提供报告或重新上传数据。

## 资源导航

- `references/usage.md`：欢迎语、适用场景、整体流程和输出说明。
- `references/field-data.md`：输入列、设计类型、性状方向和数据准备说明。
- `references/report-structure.md`：章节结构、状态、字段解释和追问入口。

## 边界

- 不编造统计显著性、LSD 分组、空间校正或稳定性结果；只依据平台返回事实。
- 不暴露脚本路径、内部运行目录、handler、service、token、数据库/LLM 配置或本机绝对路径。
- 不把 input schema 或 contract 原文贴给用户；只解释用户可见字段和业务口径。
