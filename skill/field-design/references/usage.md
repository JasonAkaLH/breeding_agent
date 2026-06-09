# Field Design 用法

## 欢迎语

当用户首次表达试验设计需求但材料清单、设计类型或关键参数不足时，可使用：

```text
欢迎使用试验设计智能体。目前支持随机区组试验设计（RCBD）、对角线增广试验设计和间比法试验设计（Interval）。你只需要提供试验材料清单，并告诉我要做哪一种设计即可开始：如果做 RCBD，请提供区组数/重复数；如果做对角线增广设计，请提供田块列数 ncols；如果做间比法设计，请先提供材料清单和田块列数 ncols，我会识别 CK 后请你按编号补充每个 CK 的起始位置和间隔数量。

需要的材料表推荐列名是：ped_id,hyb_check,set。
你可以直接上传 CSV/Excel 材料文件，或者把材料表粘贴过来。
```

## 设计类型选择

- 用户提到随机区组、随机完全区组、RCBD、重复数、区组数、blocks、reps、replicates，或每个 entry 都应在完整区组中重复出现时，选择 `RCBD`。
- 用户提到对角线增广、diagonal augmented design、diagonal checks、对照比例、田块列数 `ncols`，或沿对角线布置对照时，选择 `Diagonal`。
- 用户提到间比法、Interval、CK 起始位置、check intervals，或按起始位置和间隔固定插入 CK 时，选择 `Interval`。
- 如果用户没有明确设计类型，先让用户在 RCBD / Diagonal / Interval 中选择，不要自行猜测。

## 输出说明

默认最终回复包含：

1. 设计类型和关键参数摘要。
2. 前 10 行 planting-order Markdown 表格。
3. 完整 fieldbook CSV 与 HTML layout preview 的下载/查看入口。
4. 必要时说明哪些输入假设或用户参数影响排布。

不要展示 raw JSON、内部路径或调试字段；只有用户明确要求调试材料时，才解释可见 artifact 与业务字段。
