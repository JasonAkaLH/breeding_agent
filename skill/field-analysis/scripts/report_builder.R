build_chapters <- function(df, trait_summary, anova, lsd, spatial, stability) {
  high_cv <- trait_summary$trait[!is.na(trait_summary$cv) & trait_summary$cv > 20]
  multi_location <- length(unique(df$loc_id)) >= 2
  anova_failed <- any(vapply(anova$by_trait, function(x) identical(x$status, "failed"), logical(1)))

  list(
    data_overview = list(
      title = "数据概览",
      status = "completed",
      summary = sprintf("数据包含 %s 个地点、%s 个材料、%s 个性状、%s 条有效观测。",
                        length(unique(df$loc_id)), length(unique(df$ped_id)), length(unique(df$trait)), nrow(df)),
      recommended_questions = c("这个试验有哪些性状？", "每个地点的数据量是否均衡？")
    ),
    data_quality = list(
      title = "数据质量",
      status = if (length(high_cv) > 0) "completed_with_warnings" else "completed",
      summary = if (length(high_cv) > 0) {
        sprintf("部分性状 CV 偏高，需要谨慎解读：%s。", paste(high_cv, collapse = ", "))
      } else {
        "主要性状 CV 未出现明显高风险。"
      },
      risk_flags = if (length(high_cv) > 0) sprintf("CV 偏高性状：%s", paste(high_cv, collapse = ", ")) else NULL,
      recommended_questions = c("哪些性状数据质量最好？", "哪些性状需要谨慎解释？")
    ),
    descriptive_stats = list(
      title = "描述统计",
      status = "completed",
      summary = "已生成性状、材料和地点层面的均值、CV、排名等描述统计。",
      data_refs = c("traits.trait_summary", "materials.by_trait", "locations.summary_by_trait"),
      recommended_questions = c("哪些材料排名最高？", "不同地点的均值差异大吗？")
    ),
    check_comparison = list(
      title = "对照比较",
      status = "completed",
      summary = "已计算材料相对核心 check 的百分比表现和超过 check 的地点比例。",
      data_refs = c("materials.by_trait", "locations.materials_by_trait"),
      recommended_questions = c("哪些材料超过 check？", "哪些材料在多个地点都超过 check？")
    ),
    anova = list(
      title = "方差分析",
      status = if (anova_failed) "completed_with_warnings" else "completed",
      summary = if (anova_failed) "部分性状 ANOVA 模型失败，结果需查看具体失败原因。" else "已按数据结构尝试生成 ANOVA 表。",
      data_refs = c("analyses.anova"),
      recommended_questions = c("哪些性状材料差异显著？", "ANOVA 模型是否可靠？")
    ),
    lsd_grouping = list(
      title = "LSD 多重比较",
      status = if (all(vapply(lsd$by_trait, function(x) identical(x$status, "failed"), logical(1)))) "failed" else "completed_with_warnings",
      summary = "已参考旧脚本 LSD.test 策略生成材料分组；模型自由度不足时会标记失败。",
      data_refs = c("analyses.lsd_grouping"),
      recommended_questions = c("哪些材料属于最高 LSD 分组？", "产量性状有哪些材料差异不显著？")
    ),
    spatial_adjustment = list(
      title = "空间校正",
      status = if (all(vapply(spatial$by_trait, function(x) identical(x$status, "not_applicable"), logical(1)))) "not_applicable" else "completed_with_warnings",
      summary = "已检查 ranges/pass 坐标覆盖；坐标足够的性状会生成轻量空间校正结果。",
      data_refs = c("analyses.spatial_adjustment"),
      recommended_questions = c("是否需要空间校正？", "空间校正后排名是否变化？")
    ),
    stability = list(
      title = "多环境稳定性",
      status = if (multi_location) "completed" else "not_applicable",
      summary = if (multi_location) "已生成跨地点均值、地点间 CV、表现排名和稳定性排名。" else "当前数据少于两个地点，不适合做多环境稳定性分析。",
      data_refs = c("analyses.stability"),
      recommended_questions = c("哪些材料表现稳定？", "哪些材料高产但稳定性较差？")
    )
  )
}

build_report <- function(df, input_path, design, run_id, profile = "full_report") {
  trait_summary <- make_trait_summary(df)
  material_summary <- make_material_summary(df)
  location_summary <- make_location_summary(df)
  material_location <- make_material_location_summary(df)
  anova <- run_anova(df)
  lsd <- run_lsd_grouping(df)
  spatial <- run_spatial_adjustment(df)
  stability <- run_stability(df)

  trait_dataset <- compact_dataset(trait_summary, c("trait", "direction", "observations", "material_count", "location_count", "rep_count", "mean", "stddev", "cv", "min", "max", "check_mean", "quality"))
  material_dataset <- compact_by_trait(material_summary, "trait", c("ped_id", "entry_id", "mean", "stddev", "min", "max", "rep_count", "location_count", "rank", "pct_check_mean", "pct_trial_mean", "locations_above_check", "pct_locations_above_check"))
  location_dataset <- compact_by_trait(location_summary, "trait", c("loc_id", "observations", "material_count", "mean", "stddev", "cv", "min", "max", "check_mean"))
  material_location_dataset <- compact_by_trait(material_location, "trait", c("loc_id", "ped_id", "entry_id", "mean", "rep_count", "rank", "pct_check_mean", "pct_location_mean"))

  report <- list(
    format = "field-analysis-report-v1",
    metadata = list(
      design = design,
      analysis_profile = profile,
      run_id = run_id,
      input = input_path,
      counts = list(
        observations = nrow(df),
        traits = length(unique(df$trait)),
        materials = length(unique(df$ped_id)),
        locations = length(unique(df$loc_id)),
        reps = length(unique(df$rep_num))
      ),
      required_fields = required_input_fields(),
      output_policy = list(
        drop_empty_fields = TRUE,
        drop_empty_sections = TRUE,
        drop_all_null_fields = TRUE,
        use_catalog_records = TRUE
      )
    ),
    schema_note = list(
      format = "field-analysis-report-v1",
      description = "Business-oriented field trial analysis report. Repeated result sets use *_fields plus records arrays.",
      legacy_tables = "Legacy table names are not part of the main schema."
    ),
    chapters = build_chapters(df, trait_summary, anova, lsd, spatial, stability),
    traits = list(
      trait_summary_fields = trait_dataset$fields,
      trait_summary = trait_dataset$records
    ),
    materials = list(
      material_summary_fields = material_dataset$fields,
      by_trait = material_dataset$by_trait
    ),
    locations = list(
      location_summary_fields = location_dataset$fields,
      summary_by_trait = location_dataset$by_trait,
      material_location_fields = material_location_dataset$fields,
      materials_by_trait = material_location_dataset$by_trait
    ),
    analyses = list(
      anova = anova,
      lsd_grouping = lsd,
      spatial_adjustment = spatial,
      stability = stability
    )
  )
  drop_empty_nodes(report)
}

write_report_json <- function(report, output_file) {
  if (!requireNamespace("jsonlite", quietly = TRUE)) stop("Package 'jsonlite' is required to write JSON output.")
  json <- jsonlite::toJSON(report, pretty = TRUE, auto_unbox = TRUE, na = "null", null = "null")
  writeLines(enc2utf8(json), output_file, useBytes = TRUE)
}
