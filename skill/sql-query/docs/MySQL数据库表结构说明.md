# SQLQuery Capability相关数据库结构说明

## 在本项目的数据库功能

MySQL数据库查询主要有两个领域的数据需要查询。

1. 审定品种库数据，包含了各个育种机构提交审定的作物品种，分为五中作物：

   - 玉米（表名：`corn_varieties`)
   - 水稻（表名：`rice_varieties`)
   - 棉花（表名：`cotton_varieties`)
   - 小麦（表名：`wheat_varieties`)
   - 大豆（表名：`soybean_varieties`)

   目前只支持这五种作物的查询。

2. 基因型数据库，包含各种品种的基因数据

----

## 如何判断用户的问题是关于"审定品种库"？

1. 用户问题中包含"审定品种"、"审定"、"品种"、"品种审定"、"品种审定公告"、"申请审定"等关键词。

2. 用户或许不会直接使用关键词，而是使用一些描述，此时你需要利用你的农学专业知识，判断用户想要查询什么。
3. 当用户查询某个品种的品种信息时，你需要根据用户的问题，判断用户想要查询哪个品种的品种信息。

----

## 如何判断用户的问题是关于"基因库"？

1. 用户问题中包含"基因"、"基因组"、"基因型"、"基因型分析"、"基因型测序"、"基因型测序数据"、"QTN"、"变异"、"变异位点"、"粳稻"、"籼稻"、"粳籼稻"、"籼粳稻"、"粳型"、"籼型"等关键词。

2. 用户或许不会直接使用关键词，而是使用一些描述，此时你需要利用你的农学专业知识，判断用户想要查询什么。

3. 当用户查询某个品种的基因信息时，你需要根据用户的问题，判断用户想要查询哪个品种的基因信息。

## 数据库结构

### 以下是有关基因型数据库的数据库结构

​    -- ----------------------------

​    -- Table structure for variety

​    -- ----------------------------

​    DROP TABLE IF EXISTS `variety`;

​    CREATE TABLE `variety`  (

​    `variety_id` int(11) NOT NULL AUTO_INCREMENT COMMENT '自增ID',

​    `variety_name` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '品种名称',

​    PRIMARY KEY (`variety_id`) USING BTREE,

​    UNIQUE INDEX `variety_name`(`variety_name`) USING BTREE,

​    INDEX `idx_variety_name`(`variety_name`) USING BTREE

​    ) ENGINE = InnoDB AUTO_INCREMENT = 3944 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '水稻品种信息表' ROW_FORMAT = Dynamic;

​    -- ----------------------------

​    -- Table structure for variety_genotype

​    -- ----------------------------

​    DROP TABLE IF EXISTS `variety_genotype`;

​    CREATE TABLE `variety_genotype`  (

​    `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT '唯一自增ID',

​    `variety_id` int(11) NOT NULL COMMENT '品种ID，外键，指向variety表',

​    `qtn_id` int(11) NOT NULL COMMENT 'QTN位点ID，外键，指向qtn表',

​    `genotype` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '基因型',

​    `phenotype` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '表现型',

​    PRIMARY KEY (`id`) USING BTREE,

​    UNIQUE INDEX `uq_variety_qtn`(`variety_id`, `qtn_id`) USING BTREE,

​    INDEX `idx_qtn`(`qtn_id`) USING BTREE,

​    INDEX `idx_variety`(`variety_id`) USING BTREE,

​    CONSTRAINT `fk_qtn` FOREIGN KEY (`qtn_id`) REFERENCES `qtn` (`qtn_id`) ON DELETE CASCADE ON UPDATE CASCADE,

​    CONSTRAINT `fk_variety` FOREIGN KEY (`variety_id`) REFERENCES `variety` (`variety_id`) ON DELETE CASCADE ON UPDATE CASCADE

​    ) ENGINE = InnoDB AUTO_INCREMENT = 384476 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '品种-位点-基因型主表，同一品种每个QTN仅有一条记录' ROW_FORMAT = Dynamic;

​    -- ----------------------------

​    -- Table structure for qtn

​    -- ----------------------------

​    DROP TABLE IF EXISTS `qtn`;

​    CREATE TABLE `qtn`  (

​    `qtn_id` int(11) NOT NULL AUTO_INCREMENT COMMENT 'QTN内部唯一自增ID',

​    `qtn_seq` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT 'QTN序号（如QTN1）',

​    `gene_name` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '基因名称',

​    `gene_id_msu7` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '基因ID_MSU7',

​    `gene_id_rap` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '基因ID_RAP',

​    `gene_desc` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '基因描述',

​    `chr` varchar(256) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '染色体Chr',

​    `pos_7_0` int(11) DEFAULT NULL COMMENT '位点7.0',

​    `ref_genotype` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '参考基因型',

​    `alt_genotype` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '变异基因型',

​    `effect_info` varchar(256) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '性状类型',

​    PRIMARY KEY (`qtn_id`) USING BTREE,

​    UNIQUE INDEX `qtn_seq`(`qtn_seq`) USING BTREE,

​    INDEX `idx_gene_name`(`gene_name`) USING BTREE,

​    INDEX `idx_chr_pos`(`chr`, `pos_7_0`) USING BTREE

​    ) ENGINE = InnoDB AUTO_INCREMENT = 361 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = 'QTN位点主表' ROW_FORMAT = Dynamic;

​    -- ----------------------------

​    -- Table structure for rice_comp

​    -- ----------------------------

​    DROP TABLE IF EXISTS `rice_comp`;

​    CREATE TABLE `rice_comp`  (

​    `id` int(11) NOT NULL AUTO_INCREMENT COMMENT '自动增长主键',

​    `variety_id` int(11) DEFAULT NULL COMMENT '品种ID，外键，指向variety表',

​    `variety_name` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '品种名称',

​    `all_indica_comp` decimal(15, 8) NOT NULL COMMENT '全部籼稻成分比例(%)',

​    `indica_mix_comp` decimal(15, 8) NOT NULL COMMENT '籼稻混合成分比例(%)',

​    `indica_aus_comp` decimal(15, 8) NOT NULL COMMENT '籼稻aus成分比例(%)',

​    `indica_ind_comp` decimal(15, 8) NOT NULL COMMENT '籼稻ind成分比例(%)',

​    `all_japonica_comp` decimal(15, 8) NOT NULL COMMENT '全部粳稻成分比例(%)',

​    `japonica_mix_comp` decimal(15, 8) NOT NULL COMMENT '粳稻混合成分比例(%)',

​    `japonica_temp_comp` decimal(15, 8) NOT NULL COMMENT '粳稻温带成分比例(%)',

​    `japonica_trop_comp` decimal(15, 8) NOT NULL COMMENT '粳稻热带成分比例(%)',

​    `indica_japonica_mix_comp` decimal(15, 8) NOT NULL COMMENT '籼粳混合成分比例(%)',

​    PRIMARY KEY (`id`) USING BTREE,

​    INDEX `rice_comp_ind01`(`variety_id`) USING BTREE,

​    CONSTRAINT `kb_variety` FOREIGN KEY (`variety_id`) REFERENCES `variety` (`variety_id`) ON DELETE SET NULL ON UPDATE SET NULL

​    ) ENGINE = InnoDB AUTO_INCREMENT = 3945 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '水稻成分比例表' ROW_FORMAT = Dynamic;

​    =========================================================================================================

​    \## 连接关系

​    -- variety_genotype.variety_id can be joined with variety.variety_id

​    -- variety_genotype.qtn_id can be joined with qtn.qtn_id

​    -- rice_comp.variety_id can be joined with variety.variety_id

----

### 以下是有关品种审定数据库的数据库结构

-- ----------------------------

-- Table structure for corn_varieties

-- ----------------------------

DROP TABLE IF EXISTS `corn_varieties`;

CREATE TABLE `corn_varieties`  (

`id` int(11) NOT NULL AUTO_INCREMENT COMMENT '自动增长主键',

`year` int(11) DEFAULT NULL COMMENT '年份',

`is_gmo` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '是否转基因',

`approval_num` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '审定编号',

`crop_name` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '作物名称',

`variety_name` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '品种名称',

`applicant` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '申请者',

`breeder` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '育种者',

`variety_source` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '品种来源',

`characteristics` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '特征特性',

`yield_performance` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '产量表现',

`cultivation_tips` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '栽培技术要点',

`approval_opinion` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '审定意见',

`gm_trait` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '转基因目标性状',

`transgenic_name` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '转化体名称',

`transgenic_owner` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '转化体所有者',

`biosafety_cert_num` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '农业转基因生物安全证书编号',

`female_parent` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '母本',

`male_parent` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '父本',

`breeding_method` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '选育方法',

`pilot_test_yield` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '初试产量',

`pilot_test_increase` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '初试比对照增产',

`repeat_test_yield` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '复试产量',

`repeat_test_increase` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '复试比对照增产',

`regional_test_avg_yield` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '区试平均产量',

`regional_test_avg_increase` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '区试平均比对照增产',

`prod_test_yield` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '生试产量',

`prod_test_increase` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '生试比对照增产',

`control` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '对照',

`suitable_area` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '适种区域',

`variety_type` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '品种类型',

`growing_days` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '生育日数 (天)',

`act_temp` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '≥10℃活动积温 (℃)',

`leaf_sheath_color` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '叶鞘色',

`leaf_color` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '叶色',

`stem_color` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '茎秆色',

`primary_branches` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '雄穗第一分枝数',

`glume_color` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '颖壳色',

`anther_color` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '花药色',

`stigma_color` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '花丝色',

`plant_height` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '株高 (cm)',

`ear_height` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '穗位 (cm)',

`adult_leaves` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '成株叶片数',

`ear` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '果穗',

`cob_color` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '轴色',

`ear_length` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '穗长 (cm)',

`ear_diameter` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '穗粗 (cm)',

`rows_per_ear` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '穗行数',

`kernel_shape` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '粒型',

`kernel_color` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '粒色',

`hundred_kernel_wt` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '百粒重 (g)',

`thousand_grain_wt` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '千粒重 (g)',

`crude_protein` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '粗蛋白',

`crude_fat` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '粗脂肪',

`crude_starch` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '粗淀粉',

`branch_starch_pct` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '支链淀粉 (占淀粉)',

`taste_quality` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '食味品质',

`big_spot_disease` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '大斑病',

`stalk_smut_rate` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '丝黑穗病发病率',

`stem_rot_rate` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '茎腐病发病率',

`fusarium_ear_rot` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '镰孢穗腐病',

`resistance_id` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '抗性鉴定',

PRIMARY KEY (`id`) USING BTREE

) ENGINE = InnoDB AUTO_INCREMENT = 25949 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '玉米审定品种信息表' ROW_FORMAT = Dynamic;

-- ----------------------------

-- Table structure for rice_varieties

-- ----------------------------

DROP TABLE IF EXISTS `rice_varieties`;

CREATE TABLE `rice_varieties`  (

`id` int(11) NOT NULL AUTO_INCREMENT COMMENT '自动增长主键',

`ref_var_id` int(11) DEFAULT NULL COMMENT '品种ID，外键，指向variety表',

`year` int(11) DEFAULT NULL COMMENT '年份',

`is_gmo` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '是否转基因',

`approval_num` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '审定编号',

`crop_name` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '作物名称',

`variety_name` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '品种名称',

`applicant` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '申请者',

`breeder` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '育种者',

`variety_source` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '品种来源',

`characteristics` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '特征特性',

`yield_performance` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '产量表现',

`cultivation_tips` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '栽培技术要点',

`approval_opinion` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '审定意见',

`transgenic_name` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '转化体名称',

`transgenic_owner` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '转化体所有者',

`biosafety_cert_num` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '农业转基因生物安全证书编号',

`female_parent` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '母本',

`male_parent` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '父本',

`breeding_method` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '选育方法',

`pilot_test_yield` decimal(18, 2) DEFAULT NULL COMMENT '初试产量（每亩公斤或者千克）',

`pilot_test_increase` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '初试比对照增产',

`repeat_test_yield` decimal(18, 2) DEFAULT NULL COMMENT '复试产量（每亩公斤或者千克）',

`repeat_test_increase` decimal(10, 4) DEFAULT NULL COMMENT '复试比对照增产',

`regional_test_avg_yield` decimal(18, 2) DEFAULT NULL COMMENT '区试平均产量（每亩公斤或者千克）',

`regional_test_avg_increase` decimal(10, 4) DEFAULT NULL COMMENT '区试平均比对照增产',

`prod_test_yield` decimal(18, 2) DEFAULT NULL COMMENT '生试产量（每亩公斤或者千克）',

`prod_test_increase` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '生试比对照增产',

`control` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '对照',

`suitable_area` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '适种区域',

`variety_type` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '品种类型',

`growing_days` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '生育日数(天)',

`act_temp` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '≥10℃活动积温(℃)',

`main_stem_leaves` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '主茎叶数',

`plant_height` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '株高(cm)',

`ear_length` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '穗长(cm)',

`grain_shape` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '粒型',

`grains_per_ear` decimal(11, 2) DEFAULT NULL COMMENT '每穗粒数(粒)',

`thousand_grain_weight` decimal(11, 2) DEFAULT NULL COMMENT '千粒重(g)',

`brown_rice_rate` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '出糙率',

`milled_rice_rate` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '整精米率',

`chalky_grain_rate` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '垩白粒率',

`chalkiness` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '垩白度',

`amylose_content` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '直链淀粉含量',

`gel_consistency` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '胶稠度(mm)',

`crude_protein` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '粗蛋白（干基）',

`taste_quality` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '食味品质',

`national_quality_standard` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '国家《优质稻谷》标准',

`leaf_blast` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '叶瘟',

`neck_blast` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '穗颈瘟',

`cold_resistance_shell_rate` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '耐冷性鉴定空壳率',

PRIMARY KEY (`id`) USING BTREE,

INDEX `FK_fk_rice_variety_var01`(`ref_var_id`) USING BTREE,

CONSTRAINT `FK_fk_rice_variety_var01` FOREIGN KEY (`ref_var_id`) REFERENCES `variety` (`variety_id`) ON DELETE SET NULL ON UPDATE SET NULL

) ENGINE = InnoDB AUTO_INCREMENT = 19611 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '水稻审定品种信息表' ROW_FORMAT = Dynamic;

-- ----------------------------

-- Table structure for cotton_varieties

-- ----------------------------

DROP TABLE IF EXISTS `cotton_varieties`;

CREATE TABLE `cotton_varieties`  (

`id` int(11) NOT NULL AUTO_INCREMENT COMMENT '自动增长主键',

`year` int(11) DEFAULT NULL COMMENT '年份',

`is_gmo` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '是否转基因',

`approval_num` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '审定编号',

`crop_name` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '作物名称',

`variety_name` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '品种名称',

`applicant` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '申请者',

`breeder` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '育种者',

`variety_source` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '品种来源',

`chars` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '特征特性',

`yield_perf` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '产量表现',

`cult_tips` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '栽培技术要点',

`appr_opin` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '审定意见',

`transgenic_name` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '转化体名称',

`transgenic_owner` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '转化体所有者',

`biosafety_cert_num` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '农业转基因生物安全证书编号',

`female_parent` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '母本',

`male_parent` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '父本',

`breed_meth` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '选育方法',

`pilot_test_yield` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '初试产量',

`pilot_test_increase` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '初试比对照增产',

`repeat_test_yield` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '复试产量',

`repeat_test_increase` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '复试比对照增产',

`reg_test_avg_yield` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '区试平均产量',

`reg_test_avg_increase` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '区试平均比对照增产',

`prod_test_yield` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '生试产量',

`prod_test_increase` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '生试比对照增产',

`control` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '对照',

`suit_area` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '适种区域',

`var_type` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '品种类型',

`grow_days` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '生育日数(天)',

`first_fruit_branch_node` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '第一果枝节位（节）',

`cotton_bolls_per_plant` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '单株果枝数（个）',

`boll_shape` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '棉铃形状',

`boll_set` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '结铃性',

`fruits_per_plant` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '单株结铃（个）',

`boll_wgt` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '单铃重(g)',

`fiber_index` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '子指(g)',

`cotton_fiber_strength` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '断裂比强度(cN/tex)',

`pre_frost_flower_rate` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '霜前花率(%)',

`cotton_fiber_upper_half_len` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT 'HVICC纤维上半部平均长度(mm)',

`boll_breaking_strength` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '断裂强度',

`markh_val` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '马克隆值',

`boll_elong` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '断裂伸长率(%)',

`fiber_uni` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '纤维整齐度',

`reflect` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '反射率(%)',

`yellow_degree` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '黄度',

`sp_uni` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '纺纱均匀指数',

`resistance_id` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '抗性鉴定',

PRIMARY KEY (`id`) USING BTREE

) ENGINE = InnoDB AUTO_INCREMENT = 2364 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '棉花审定品种信息表' ROW_FORMAT = Dynamic;

-- ----------------------------

-- Table structure for soybean_varieties

-- ----------------------------

DROP TABLE IF EXISTS `soybean_varieties`;

CREATE TABLE `soybean_varieties`  (

`id` int(11) NOT NULL AUTO_INCREMENT COMMENT '唯一自增ID',

`year` int(11) DEFAULT NULL COMMENT '年份',

`is_gmo` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '是否转基因',

`approval_num` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '审定编号',

`crop_name` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '作物名称',

`variety_name` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '品种名称',

`applicant` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '申请者',

`breeder` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '育种者',

`variety_source` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '品种来源',

`characteristics` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '特征特性',

`yield_performance` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '产量表现',

`cultivation_tips` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '栽培技术要点',

`approval_opinion` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '审定意见',

`transgenic_name` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '转化体名称',

`transgenic_owner` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '转化体所有者',

`biosafety_cert_num` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '农业转基因生物安全证书编号',

`female_parent` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '母本',

`male_parent` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '父本',

`breeding_method` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '选育方法',

`pilot_test_yield` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '初试产量',

`pilot_test_increase` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '初试比对照增产',

`repeat_test_yield` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '复试产量',

`repeat_test_increase` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '复试比对照增产',

`regional_test_avg_yield` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '区试平均产量',

`regional_test_avg_increase` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '区试平均比对照增产',

`prod_test_yield` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '生试产量',

`prod_test_increase` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '生试比对照增产',

`control` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '对照',

`suitable_area` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '适种区域',

`variety_type` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '品种类型',

`growing_days` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '生育日数(天)',

`act_temp` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '≥10℃活动积温(℃)',

`plant_type` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '株型',

`pod_habit` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '结荚习性',

`plant_height` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '株高(cm)',

`node_count` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '主茎节数',

`has_branch` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '有无分枝',

`effective_branch_num` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '有效分枝数',

`bottom_pod_height` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '底荚高度(cm)',

`pods_per_plant` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '单株有效荚数',

`grains_per_plant` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '单株粒数',

`grain_weight_per_plant` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '单株粒重(g)',

`flower_color` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '花色',

`leaf_shape` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '叶型',

`puberulent_color` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '茸毛色',

`pod_shape` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '荚形',

`mature_pod_color` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '成熟荚色',

`seed_shape` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '种子形状',

`seed_coat_color` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '种皮色',

`seed_umbilicus_color` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '种脐色',

`is_glossy` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '有无光泽',

`hundred_grain_weight` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '百粒重(g)',

`protein_content` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '蛋白质含量(%)',

`fat_content` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '脂肪含量(%)',

`oleic_acid_content` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '油酸含量(%)',

`mosaic_virus` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '花叶病毒病',

`disc_nematode` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '胞囊线虫病',

`disease_spot` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '灰斑病',

`soluble_sugar_content` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '可溶性糖含量(%)',

`transgenic_trait` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '转基因特征特性',

PRIMARY KEY (`id`) USING BTREE

) ENGINE = InnoDB AUTO_INCREMENT = 4229 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '大豆审定品种信息表' ROW_FORMAT = Dynamic;

-- ----------------------------

-- Table structure for wheat_varieties

-- ----------------------------

DROP TABLE IF EXISTS `wheat_varieties`;

CREATE TABLE `wheat_varieties`  (

`id` int(11) NOT NULL AUTO_INCREMENT COMMENT '自动增长主键',

`year` int(11) DEFAULT NULL COMMENT '年份',

`is_gmo` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '是否转基因',

`approval_num` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '审定编号',

`crop_name` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '作物名称',

`variety_name` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '品种名称',

`applicant` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '申请者',

`breeder` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '育种者',

`variety_source` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '品种来源',

`characteristics` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '特征特性',

`yield_performance` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '产量表现',

`cultivation_tips` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '栽培技术要点',

`approval_opinion` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '审定意见',

`transgenic_name` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '转化体名称',

`transgenic_owner` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '转化体所有者',

`biosafety_cert_num` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '农业转基因生物安全证书编号',

`female_parent` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '母本',

`male_parent` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '父本',

`breeding_method` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '选育方法',

`pilot_test_yield` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '初试产量',

`pilot_test_increase` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '初试比对照增产',

`repeat_test_yield` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '复试产量',

`repeat_test_increase` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '复试比对照增产',

`regional_test_avg_yield` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '区试平均产量',

`regional_test_avg_increase` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '区试平均比对照增产',

`prod_test_yield` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '生试产量',

`prod_test_increase` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '生试比对照增产',

`control` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '对照',

`suitable_area` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '适种区域',

`variety_type` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '品种类型',

`growing_days` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '生育日数(天)',

`seedling_shape` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '幼苗形状',

`plant_height` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '株高(cm)',

`ear_length` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '穗长(cm)',

`ear_type` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '穗型',

`awn_type` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '芒型',

`kernel_color` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '粒色',

`grains_per_ear` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '每穗粒数(粒)',

`thousand_grain_wt` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '千粒重(g)',

`test_weight` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '容重(g/L)',

`protein_content` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '蛋白质含量',

`wet_gluten_content` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '湿面筋含量',

`water_absorption_rate` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '吸水率(%)',

`dough_stability` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '面团稳定时间(min)',

`max_extension_resistance` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '最大拉伸阻力(Rm.E.U)',

`extension_area` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '拉伸面积(平方厘米)',

`resistance_id` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '抗性鉴定',

PRIMARY KEY (`id`) USING BTREE

) ENGINE = InnoDB AUTO_INCREMENT = 6126 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '小麦审定品种信息表' ROW_FORMAT = Dynamic;