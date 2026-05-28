---
name: sql-query
capability_id: skill.sql_query
display_name: 审定品种与基因型数据库查询
description: 通过项目级 Skill 平台服务安全回答品种、审定、基因型、表型和数据库类只读查询问题；适用于需要从受控 MySQL 只读库检索业务数据并返回表格预览的请求。
triggers:
  - 查询品种
  - 查询审定品种
  - 查询基因型
  - 审定信息
  - 基因型
  - 表型数据
  - 审定品种库
  - 品种审定库
  - 审定品种
  - 审定
  - 品种审定
  - 品种审定公告
  - 申请审定
  - 品种信息
  - 品种详情
  - 品种资料
  - 基因型数据库
  - 基因
  - 基因组
  - 基因型分析
  - 基因型测序
  - 基因型测序数据
  - QTN
  - 变异
  - 变异位点
  - 粳稻
  - 籼稻
  - 粳籼稻
  - 籼粳稻
  - 粳型
  - 籼型
public_usage:
  overview: >-
    面向品种、审定、基因型、表型和业务资料的只读查询问答；适合用户用自然语言询问品种信息、审定公告、基因型位点或表型记录。
  input_formats:
    - name: query
      required: true
      description: 用户自然语言查询；应说明查询对象、条件、地区、年份、品种名称、基因或性状等业务限定。
  parameters:
    - name: query
      description: 查询问题本身；系统只返回只读结果摘要和表格预览，不执行写入、修改或删除。
  examples:
    - /sql-query 查询某品种的审定信息
    - /sql-query 对比这些品种的基因型记录
    - /sql-query 查找 2020 年以后某地区的审定公告
  outputs:
    - 查询结果摘要
    - 表格预览
    - 必要的数据口径和筛选说明
parameters:
  query:
    type: string
    required: true
outputs:
  required:
    - summary
    - filtered_query_result
execution:
  mode: platform_service
  trust_scope: project
  handler: skill.sql_query.platform_handler
  handler_module: runtime/sql_query_skill/platform_handler.py
  handler_factory: build_handler
  answer_mode: requires_finalizer
  services:
    - mysql_readonly
    - llm.non_stream
    - artifact_writer
    - progress_events
---

# SQLQuery Skill（Platform Service）

## Use when
- 用户需要查询品种、审定、基因型、表型或数据库中的只读业务数据。

## Implementation
- 由通用 `SkillExecutor` 执行，公开 capability 固定为 `skill.sql_query`。
- 使用 runtime 预注册且 allowlist 通过的 `skill.sql_query.platform_handler` 平台服务 handler。
- 业务链路由本 Skill bundle 内 `runtime/sql_query_skill/` 实现，包含意图理解、schema context、SQL 生成、SQL Guard、只读执行和结果筛选。
- `answer_mode: requires_finalizer` 表示 Skill 输出结构化查询结果后，再由主代理生成最终自然语言回答。

## Workflow
1. 判断查询意图和目标数据域。
2. 准备 schema context。
3. 生成候选只读 SQL。
4. SQL 必须经过 guard 校验。
5. 只允许通过 readonly adapter 执行。
6. 对查询结果做 LLM / fallback 筛选。
7. 返回业务摘要、原始 preview、筛选后 preview 和必要诊断。

## Boundaries
- 不执行写入、删除、更新、DDL、权限变更或多语句 SQL。
- 不暴露数据库连接信息、账号、密码、LLM key 或完整 prompt。
- 不转换为 `python_subprocess`，也不让普通脚本直接绑定 MySQL、内部 LLM、secret 或完整环境变量。
- 前端和编排层只能把 `skill.sql_query` 当作公共 SQLQuery 入口；领域阶段只存在于 handler 内部。
- SQLQuery 进度与产物使用 `domain_kind=sql_query` 和 `capability_id=skill.sql_query` metadata。
- 缺少关键查询实体时返回可控澄清，不编造 SQL。
