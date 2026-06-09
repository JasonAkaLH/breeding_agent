# 对角线增广

## 适用场景

Diagonal / 对角线增广适合用户要求对角线增广设计、diagonal checks、沿对角线布置对照、对照比例或田块列数 `ncols` 的场景。

## 必需输入

- 材料清单：推荐列为 `ped_id,hyb_check,set`。
- 设计类型：`diagonal` / 对角线增广。
- `ncols`：田块每行列数，用于固定布局宽度。

## `hyb_check` 解释

Diagonal 中通常将 `hyb_check = 2` 解释为 diagonal check material，且至少需要一个 check 和一个非 check entry。

```csv
ped_id,hyb_check,set
A001,0,A
A002,0,A
DCK01,2,A
B001,0,B
B002,0,B
DCK02,2,B
```

可接受额外约束：对照比例、排布方式、随机化和随机种子等。
