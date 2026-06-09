# 间比法 / Interval CK 参数

## 适用场景

Interval / 间比法适合用户要求 CK 起始位置、check intervals，或按固定起始位置和间隔插入 CK 的场景。

## 必需输入

- 材料清单：推荐列为 `ped_id,hyb_check,set`，且能识别 CK。
- 设计类型：`interval` / 间比法。
- `ncols`：田块每行列数。
- `ck_spec`：每个 CK 的起始位置和间隔，格式为 `ck_no,start_pos,interval`；多个 CK 用分号分隔。

首次交互不要要求 Interval 的 `reps`。

## `hyb_check` 与 CK 识别

Interval 中通常将 `hyb_check = 0` 解释为试验材料，非零值解释为 CK；CK 的 `ped_id` 必须全局唯一。

```csv
ped_id,hyb_check,set
V1,0,A
V2,0,A
CK1,1,A
V3,0,B
V4,0,B
CK2,1,B
```

CK 参数示例：

```text
1,1,10; 2,5,10
```

含义：CK 编号 1 从位置 1 开始每 10 个插入；CK 编号 2 从位置 5 开始每 10 个插入。
