---
name: field-design
description: >-
  基于用户上传或粘贴的材料清单生成田间试验设计、fieldbook、种植顺序和布局预览。适用于随机区组设计/RCBD、对角线增广设计、间比法/Interval、重复数/区组数设置、田块列数、CK 起始位置与间隔、对照材料布置、蛇形或顺序排布、生成 CSV fieldbook 或 HTML 布局预览等请求；也适用于回答 ped_id、hyb_check、set、blocks、ncols、ck_spec 等字段和设计参数如何填写。
---

# 试验设计智能体

## 总纲

使用此 Skill 帮用户完成三类田间试验设计：

- `RCBD`：随机完全区组设计 / randomized complete block design。
- `Diagonal`：对角线增广设计 / diagonal augmented design。
- `Interval`：间比法 / interval contrast design。

`SKILL.md` 是 agent-facing 总纲；平台执行事实源由 `skill.contract.yaml` 和当前 selected input schema 决定。用户可见的数据字段、参数口径和示例必须优先从下方列出的 references 读取。

## 工作流

1. 确认设计类型、材料清单和当前设计所需参数。
2. 如果缺少材料、设计类型或 selected schema 的必填字段，进行结构化追问；不要编造默认业务参数。
3. 用户追问数据格式、参数含义、示例或利弊时，带着本总纲、当前 schema 和 references 回答，并保持 interrupt open。
4. 让平台执行层完成设计生成、布局渲染和 artifact 归档。
5. 将平台返回的结构化结果视为事实源，回复中展示设计模式、核心参数、前 10 行 planting-order 预览和 artifact 入口。

## 资源导航

- `references/usage.md`：欢迎语、设计类型选择、总体流程和输出说明。
- `references/material-data.md`：材料表列名、`ped_id`/`hyb_check`/`set` 字段语义和 CSV 示例。
- `references/rcbd.md`：RCBD 所需参数、`hyb_check` 解释和示例。
- `references/diagonal.md`：对角线增广的 `ncols`、check 标记和约束。
- `references/interval.md`：间比法 `ncols`、CK 识别、`ck_spec` 格式和示例。

## 边界

- 不暴露脚本路径、handler、service、token、数据库/LLM 配置、本机绝对路径或内部运行目录。
- 不把 `skill.contract.yaml` 或 input schema 原文贴给用户；只解释用户可见业务字段。
- 不要求用户理解平台 contract；用户只需提供材料和设计参数。
- 不在缺少必需输入时假装已完成设计。
