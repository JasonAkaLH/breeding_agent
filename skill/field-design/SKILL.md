---
name: field-design
description: >-
  基于用户上传或粘贴的材料清单生成田间试验设计、fieldbook、种植顺序和布局预览。适用于随机区组设计/RCBD、对角线增广设计、间比法/Interval、重复数/区组数设置、田块列数、CK 起始位置与间隔、对照材料布置、蛇形或顺序排布、生成 CSV fieldbook 或 HTML 布局预览等请求；也适用于回答 ped_id、hyb_check、set、blocks、ncols、ck_spec 等字段和设计参数如何填写。
---

# 试验设计智能体

使用此 Skill 帮用户完成三类田间设计任务：

- `RCBD`：随机完全区组设计 / randomized complete block design。
- `Diagonal`：对角线增广设计 / diagonal augmented design。
- `Interval`：间比法 / interval contrast design。

`SKILL.md` 是 agent-facing 操作指南；平台执行事实源由同目录 `skill.contract.yaml` 和当前 selected input schema 决定。不要从本文推断执行入口、脚本路径、handler、service 或内部配置。

## 欢迎语

当用户裸调用本 Skill，或首次表达试验设计需求但材料清单/设计类型/关键参数不足时，先用下面文本问候并收集缺失项：

```text
欢迎使用试验设计智能体。目前支持随机区组试验设计（RCBD）、对角线增广试验设计和间比法试验设计（Interval）。你只需要提供试验材料清单，并告诉我要做哪一种设计即可开始：如果做 RCBD，请提供区组数/重复数；如果做对角线增广设计，请提供田块列数 ncols；如果做间比法设计，请先提供材料清单和田块列数 ncols，我会识别 CK 后请你按编号补充每个 CK 的起始位置和间隔数量。

需要的材料表推荐列名是：

ped_id,hyb_check,set

对应中文含义是：

样本名称/材料代号(ped_id),是否对照/材料类型标记(hyb_check),试验分组/集合(set)

你可以直接上传 CSV/Excel 材料文件，或者把材料表粘贴过来。
```

缺信息时只问当前最小必要项。必需输入可用前不要暗示已经开始设计。

## 选择设计类型

- 用户提到随机区组、随机完全区组、RCBD、重复数、区组数、blocks、reps、replicates，或每个 entry 都应在完整区组中重复出现时，选择 `RCBD`。
- 用户提到对角线增广、diagonal augmented design、diagonal checks、对照比例、田块列数 `ncols`，或沿对角线布置对照时，选择 `Diagonal`。
- 用户提到间比法、Interval、CK 起始位置、check intervals，或按起始位置和间隔固定插入 CK 时，选择 `Interval`。

如果用户没有明确设计类型，先让用户在 RCBD / Diagonal / Interval 中选择，不要自行猜测。

## 输入与补参

材料清单优先使用以下列：

```text
ped_id,hyb_check,set
```

字段含义：

- `ped_id`：样本名称或材料代号；必须能唯一标识材料。
- `hyb_check`：是否对照/材料类型标记；不同设计对取值有不同解释。
- `set`：试验分组或集合；推荐使用 `A`、`B`、`C` 等稳定分组值。

设计补参规则：

- RCBD 需要材料清单、设计类型和 `blocks`（重复数/区组数）。
- Diagonal 需要材料清单、设计类型和 `ncols`；可接受对照比例、排布方式、随机化和随机种子等额外约束。
- Interval 需要材料清单、设计类型、`ncols`，并在识别 CK 后继续收集每个 CK 的起始位置和间隔数量。首次交互不要要求 Interval 的 `reps`。

对于 `hyb_check`：

- RCBD 中通常将 `hyb_check = 0` 解释为试验材料，非零值解释为 checks。
- Diagonal 中通常将 `hyb_check = 2` 解释为 diagonal check material，且至少需要一个 check 和一个非 check entry。
- Interval 中通常将 `hyb_check = 0` 解释为试验材料，非零值解释为 CK；CK 的 `ped_id` 必须全局唯一。

详细字段口径按需读取：

- `references/material-data.md`：材料表字段说明。
- `references/rcbd.md`：RCBD 规则。
- `references/diagonal.md`：对角线增广规则。
- `references/interval.md`：Interval 与 CK 参数规则。
- `references/usage.md`：总体用法、示例和输出说明。

## 工作流

1. 确认材料清单、设计类型和当前设计所需参数。
2. 如果缺少材料、设计类型或 selected schema 的必填字段，进行结构化追问；不要编造默认业务参数。
3. 让平台执行层完成设计生成、布局渲染和 artifact 归档。
4. 将平台返回的结构化结果视为事实源，不把内部 JSON 或执行细节直接暴露给用户。
5. 用户回答中先说明设计模式和核心参数，再展示前 10 行 planting-order 预览，最后给出完整 CSV fieldbook 和 HTML layout preview 的下载/查看入口。

## 输出策略

默认最终回复应包含：

- 设计类型和关键参数摘要。
- 前 10 行 planting-order Markdown 表格。
- 完整 fieldbook CSV 和 HTML 布局预览链接。
- 必要时说明哪些输入假设或用户提供参数影响了排布。

默认不要展示 raw JSON、内部文件路径或调试字段。只有用户明确要求调试材料时，才解释可见 artifact 与业务字段，不暴露内部实现路径。

## 边界

- 不在回答中暴露脚本路径、handler、service、token、数据库/LLM 配置、本机绝对路径或内部运行目录。
- 不把 `skill.contract.yaml` 或 input schema 原文贴给用户；只解释用户可见业务字段。
- 不要求用户理解平台 contract；用户只需提供材料和设计参数。
- 不在缺少必需输入时假装已完成设计。
