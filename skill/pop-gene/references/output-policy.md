# 群体遗传输出策略

## 输出归属

计算服务返回的 JSON 属于计算事实；用户可见产物由 Skill 层组织和返回，包括 HTML 图、
中文解读、报告和会话状态。

默认 PCA-only 流程直接在聊天中解释 PCA，并提供 PCA HTML 图。完整报告只在用户要求时生成。

## 会话状态

会话状态至少记录：

- 输入文件和推断格式。
- `prepared_genotype_id`。
- 已完成阶段和摘要。
- 已生成的 HTML 图和报告。
- 可选下一步。

用户追问且没有提供新输入时，复用活动会话。

## 完整报告章节

完整报告应包含：

```text
1. Data overview
2. PCA
3. ADMIXTURE
4. Genetic distance and PCoA
5. Phylogenetic tree
6. Breeding interpretation
7. Evidence boundaries
```

常规答复不要展示内部 JSON 路径、临时目录或服务运行细节。
