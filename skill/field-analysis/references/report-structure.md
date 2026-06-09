# Field Analysis 报告结构

主报告格式为 `field-analysis-report-v1`。

## 优先解释章节

- `data_overview`：trial scale 和 inventory。
- `data_quality`：CV、coverage、check distribution 和 risk notes。
- `descriptive_stats`：trait、material 和 location summaries。
- `check_comparison`：相对 checks 的表现。
- `anova`：ANOVA model 和 significance。
- `lsd_grouping`：ANOVA 后的 LSD grouping。
- `spatial_adjustment`：ranges/pass coverage 和轻量空间校正诊断。
- `stability`：location count 支持时的 multi-location stability 分析。

章节状态包括 `completed`、`completed_with_warnings`、`not_applicable`、`failed`、`skipped`。

回复用户时先总结章节状态，再解释关键性状、材料、地点或 check 对比；不要倾倒所有记录。
