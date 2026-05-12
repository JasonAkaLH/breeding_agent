---
name: sql-query
capability_id: skill.sql_query
description: 安全回答品种、审定、基因型、表型和数据库类只读查询问题；适用于需要从受控 MySQL 只读库检索业务数据并返回表格预览的请求。
triggers:
  - 查询品种
  - 审定信息
  - 基因型
  - 表型数据
  - 数据库查询
  - 查一下
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
  handler: sql_query.query
  answer_mode: requires_finalizer
  services:
    - mysql_readonly
    - llm.sql_query
    - artifact_writer
    - progress_events
---

# SQL Query

## Use when
- 用户需要查询品种、审定、基因型、表型或数据库中的只读业务数据。

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
- 缺少关键查询实体时返回可控澄清，不编造 SQL。
