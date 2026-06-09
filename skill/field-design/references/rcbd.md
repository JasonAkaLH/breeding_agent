# 随机区组 / RCBD

## 适用场景

RCBD 适合用户要求随机区组、随机完全区组、重复数、区组数、blocks/reps/replicates，或希望每个 entry 在完整区组中重复出现的场景。

## 必需输入

- 材料清单：推荐列为 `ped_id,hyb_check,set`。
- 设计类型：`rcbd` / 随机区组。
- `blocks`：区组数/重复数。当前 schema 中可作为可选字段收集，但用户执行 RCBD 时通常需要明确重复数。

RCBD 不需要 `ck_spec`；不要向 RCBD 用户要求 Interval 的 CK 起始位置和间隔参数。

## `hyb_check` 解释

RCBD 中通常将 `hyb_check = 0` 解释为普通试验材料，非零值解释为 checks。

```csv
ped_id,hyb_check,set
A001,0,A
A002,0,A
CK01,1,A
B001,0,B
B002,0,B
CK02,1,B
```

示例请求：

```text
请用这个 materials.csv 做 RCBD，3 个重复。
```
