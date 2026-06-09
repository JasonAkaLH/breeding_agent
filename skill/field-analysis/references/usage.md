# Field Analysis 用法

## 欢迎语

```text
欢迎使用田间数据分析智能体。目前支持随机区组试验（RCBD）和对角线增广试验（Diagonal）的田间表型数据分析。你只需要提供田间数据文件，并告诉我是 RCBD 还是 Diagonal 设计，我会生成章节化分析报告，包含数据质量、性状统计、材料表现、check 对比、方差分析、LSD 分组、空间校正诊断和稳定性分析等内容。

需要的数据表推荐列名是：loc_id,rep_num,entry_id,ped_id,trait,value,check_type,ranges,pass。
可选列包括：value_trend,env_id,plot_id,block,num,female_ped_name,male_ped_name。
你可以直接上传 CSV/JSON 文件，并说明设计类型：rcbd 或 diagonal。
```

缺信息时只问当前最小必要项：田间表型数据文件、设计类型，以及可选运行编号。

## 输出说明

默认最终回复包含：

1. 本次数据规模、设计类型和分析范围。
2. 章节完成状态摘要。
3. 数据质量风险和显著发现。
4. 主要性状、材料表现、check 对比、ANOVA/LSD/空间/稳定性结论中有证据支持的部分。
5. 可下载或可查看报告 artifact 的入口。

不要把旧表名作为面向用户的 schema 暴露。不要把内部中间 JSON 全量贴给用户，除非用户明确要求调试或机器读取。
