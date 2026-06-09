# 材料表字段

推荐材料清单包含以下列：

```csv
ped_id,hyb_check,set
```

## 字段含义

- `ped_id`：样本名称或材料代号；必须能唯一标识材料。不要把 `ped_id` 改写为 `sample_id`。
- `hyb_check`：是否对照/材料类型标记；不同设计对取值有不同解释。
- `set`：试验分组或集合；推荐使用 `A`、`B`、`C` 等稳定分组值。

## `hyb_check` 取值口径

- RCBD 中通常将 `hyb_check = 0` 解释为试验材料，非零值解释为 checks。
- Diagonal 中通常将 `hyb_check = 2` 解释为 diagonal check material，且至少需要一个 check 和一个非 check entry。
- Interval 中通常将 `hyb_check = 0` 解释为试验材料，非零值解释为 CK；CK 的 `ped_id` 必须全局唯一。

## 通用 CSV 示例

```csv
ped_id,hyb_check,set
A001,0,A
A002,0,A
CK01,1,A
B001,0,B
B002,0,B
CK02,1,B
```
