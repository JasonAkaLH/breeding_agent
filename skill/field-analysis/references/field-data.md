# 田间数据字段

## 推荐必需列

```csv
loc_id,rep_num,entry_id,ped_id,trait,value,check_type,ranges,pass
```

- `loc_id`：地点或环境编号。
- `rep_num`：重复/区组编号。
- `entry_id`：材料或 entry 编号。
- `ped_id`：材料名称或材料代号。
- `trait`：性状名称或代码。
- `value`：性状观测值。
- `check_type`：check/对照类型标记。
- `ranges`：田间行或 range 编号。
- `pass`：田间列、pass 或小区位置编号。

## 可选兼容列

```csv
value_trend,env_id,plot_id,block,num,female_ped_name,male_ped_name
```

## 性状方向

- 如果存在 `value_trend`，使用该字段：`1` 表示数值越高越好，`-1` 表示数值越低越好。
- 如果没有 `value_trend`，把 `T0166` 视为 lower-is-better，其他性状默认 higher-is-better。

## 设计类型

用户需要明确数据来自 `rcbd` 或 `diagonal` 设计。缺少设计类型时先追问，不要自行猜测。
