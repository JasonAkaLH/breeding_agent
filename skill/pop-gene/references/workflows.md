# 群体遗传分析流程

## 默认流程

用户提供基因型数据但未指定分析时，默认运行：

```text
prepare-genotype -> pca -> PCA HTML plot -> Chinese PCA interpretation
```

PCA 完成后停止。除非用户明确要求或确认下一步，否则不要自动运行 ADMIXTURE、遗传距离、
PCoA、系统发育树或完整报告。

## 基因型准备

准备阶段会识别输入格式、过滤明显不可用标记，并生成供后续分析复用的
`prepared_genotype_id`。默认 MAF 阈值应尽量低，以去除单态标记同时保留大部分有效标记。

## PCA

默认请求 5 个特征向量。解释时优先使用：

- `tables.scores`：绘制散点图。
- `tables.eigenvalues`：解释方差比例。
- `tables.tracy_widom`：判断显著主成分的证据。

## ADMIXTURE

除非用户指定，默认 K 范围为 `2..8`。解释时关注：

- `tables.cv_error`
- `summary.recommended_k`
- `tables.structure_barplot`
- 按 `mixed_threshold` 识别混合材料。

## 遗传距离和 PCoA

默认使用 IBS 遗传距离，并包含排序可视化。解释最近材料对时保持谨慎：

- 近零距离可能表示重复、近重复或亲缘关系很近。
- PCoA 是可视摘要，不是最终分类学结论。

## 系统发育树

树构建以遗传距离结果为基础。除非用户指定 UPGMA，默认优先使用 BIONJ。解释时说明方法、
主要分支、近缘材料对，以及拓扑对标记集和过滤策略的依赖。
