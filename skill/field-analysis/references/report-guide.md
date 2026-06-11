# Field Analysis Report Guide

## Report Sections

The report is organized around these chapters when supported by the data:

- `trait_preflight`: trait type screening, check-type normalization, numeric
  traits sent to model analysis, categorical traits summarized in the skill
  layer, and skipped traits with reasons.
- `data_overview`: trial scale, locations, materials, and trait inventory.
- `data_quality`: CV, coverage, check distribution, and risk notes.
- `descriptive_stats`: trait, material, and location summaries.
- `check_comparison`: material performance relative to checks.
- `anova`: ANOVA model and significance.
- `lsd_grouping`: LSD grouping after ANOVA when model status supports it.
- `spatial_adjustment`: ranges/pass coverage and lightweight spatial diagnostics.
- `stability`: multi-location stability when enough locations are available.

Chapter status values:

- `completed`
- `completed_with_warnings`
- `not_applicable`
- `failed`
- `skipped`

## Finalizer Contract

The compute backend returns factual JSON. The main model/finalizer must turn
that JSON into a natural Chinese breeding report. The finalizer may improve
ordering, wording, emphasis, and explanation, but must not invent statistics or
biological conclusions absent from the JSON.

Use this reader-friendly order for the first successful response:

1. Analysis completion: design, run id, and one-sentence result orientation.
2. Trait preflight: input trait count, numeric traits used for full model
   analysis, categorical traits summarized descriptively, skipped traits, and
   check-type normalization.
3. Trial overview: observations, materials, locations, reps/blocks, and traits.
4. Data quality: CV/quality labels, missingness or warning chapters, and whether
   a trait should be interpreted cautiously.
5. Trait and material highlights: the first few numeric trait summaries and top
   materials, using `ped_id`, mean, rank, percent of check mean, and locations
   above check when available.
6. Categorical traits: category frequency, dominant category by material or
   location, and missingness. Do not report mean, LSD, or BLUP for categorical
   traits.
7. Statistical evidence: ANOVA status, key terms with F/p/significance labels,
   and LSD grouping status/leading groups when available.
8. 材料 BLUP：如果 `analyses.hybrid_blup` 可用，展示前列记录，并说明
   `model`、`status`、`singular_fit`、`hyb_blup` 和 `rank_hyb_blup`；
   如果不可用，直接说明状态或原因。
9. Check comparison, spatial diagnostics, and stability only when those facts
   are present or chapter status explains why they are unavailable.
10. HTML report link and focused follow-up suggestions.

For follow-up answers, select only the relevant facts and answer directly. A
specific trait question should start from that trait's summary, then material
ranking, then ANOVA/LSD/check facts if available.

## Evidence Rules

Do not use generic statements such as "处理间存在显著差异" unless the current
JSON includes a p-value or significance field supporting that sentence.

If a requested fact is absent, say which section is missing, failed, skipped, or
not applicable. Use explicit phrases such as "当前 JSON 未提供该性状的 LSD 分组"
instead of silently omitting the limitation.

If a trait is not in the numeric analysis set, explain its preflight status:
categorical, empty, constant, too few numeric observations, or unsupported mixed
type. Categorical traits may be summarized with counts and proportions only.

Keep the response human-readable:

- Prefer compact paragraphs plus short tables or bullets over raw JSON dumps.
- Avoid long module checklists unless the user asks for full technical details.
- Explain statistical results in breeding language, but keep the evidence
  boundary visible.
- Do not expose internal paths, script names, service payloads, or debug files
  unless the user asks for debugging.

Useful follow-up prompts:

- 哪些材料产量最高？
- 哪些性状数据质量最好？
- 哪些性状进入了完整模型分析，哪些被跳过？
- 分类性状各类别分布如何？
- 哪些材料超过 check？
- 哪些性状的材料差异显著？
- 空间校正后排名有没有变化？
- 哪些材料跨地点表现稳定？

## Boundaries

This skill is for field testing and phenotype evaluation. 当当前
`field-analysis-report` 包含 breedstat2 返回的 `analyses.hybrid_blup`
时，可以解释材料 BLUP。不要推断配合力、亲本表现、遗传力或方差组分，除非后续流程明确支持
这些分析。

Follow-up answers should use the active report/session facts. Prefer targeted
JSON extraction over rereading the HTML, and do not ask users to provide the
JSON path again when `.field-analysis-session.json` or returned `session_json`
is available.
