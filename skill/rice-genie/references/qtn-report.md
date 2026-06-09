# QTN 报告结构与解读规则

## 解读规则

- 按 QTN reference 中的坐标和位点定义匹配 records。
- 基因型类别来自 VCF genotype 语义：reference/wild type、heterozygous、mutant/variant type、missing 等。
- 只有当样本为突变型且 reference 将 variant allele 标记为有利/Superior 或等价含义时，才计入 favorable detected variant。
- 野生型不能声称存在变异表型；杂合型除非有明确定义，否则按 context-dependent 处理。
- context-dependent、unknown、missing、complex 和 unmatched cases 不计入 favorable counts。
- Indel、multi-alt、missing 等 review notes 应用中性语气解释，不夸大为确定缺陷或优势。

## 默认客户报告结构

默认报告标题：

```markdown
## 水稻基因型体检报告
```

多样本默认 section order：

1. `一、多样本对比总览`：表格列为 `样本编号`、`优良变异总数`、`检出率`、`核心优势`、`推荐用途`。
2. `样本 [sample] 深度解读`：最多对前三个 samples 写深度解读。
3. `差异对比`：比较产量、抗性、品质、氮高效、株型、逆境或粒型等有证据支持的差异。
4. `育种建议`：给出证据限定、可操作但不夸大的利用建议。
5. `证据边界`：简洁说明结论仅来自当前 320-QTN 检测结果。

每个深度样本优先使用以下小节顺序：产量潜力分析、抗性评价、环境适应性、品质与株型、氮高效利用（仅在有证据时出现）。
