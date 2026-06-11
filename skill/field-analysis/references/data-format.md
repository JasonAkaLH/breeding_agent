# Field Data Format

## Accepted Files

Use CSV or Excel (`.csv`, `.xlsx`). Data should be a wide table: fixed
metadata columns first, followed by one or more trait columns.

Required fixed columns, in order:

```text
loc_id,rep_num,ranges,pass,entry_id,ped_id,check_type
```

Trait columns start immediately after `check_type`.

Example:

```text
loc_id,rep_num,ranges,pass,entry_id,ped_id,check_type,T002,T057,T0166
```

## Column Meanings / 固定列含义

| Column | Meaning |
| --- | --- |
| `loc_id` | 地点/环境编号，用于区分试验地点或环境。 |
| `rep_num` | 重复/区组编号，用于 RCBD 或重复观测。 |
| `ranges` | 田间行号/垄号，用于空间诊断和田间位置描述。 |
| `pass` | 田间列号/走向编号，用于空间诊断和田间位置描述。 |
| `entry_id` | 小区或参试条目编号，通常对应一个田间小区。 |
| `ped_id` | 材料编号或品种/组合名称；同一材料可出现在多行。 |
| `check_type` | 对照标记；空值表示测试材料，非空表示对照材料。 |

Every trait column should contain numeric values where available. Empty or
non-numeric trait cells are ignored for that trait.

Until a trait metadata file exists, `T0166` is treated as lower-is-better and
other traits are treated as higher-is-better.
