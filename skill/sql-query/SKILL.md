---
name: sql-query
description: >-
  通过项目级受控平台服务安全回答品种、审定、基因型、表型和业务数据库类只读查询问题。适用于查询品种信息、审定公告、申请审定、审定年份/地区、粳稻/籼稻类型、基因型测序数据、QTN/变异位点、表型记录、数据库结果摘要、表格预览、自然语言转只读查询，以及询问查询口径或筛选条件的场景。
---

# SQLQuery Skill（Platform Service）

## 总纲

使用此 Skill 通过项目级受控平台服务回答品种、审定、基因型、表型和业务数据库类只读查询问题。

平台执行事实源由 `skill.contract.yaml` 决定。SQL guard、schema context、handler、service allowlist 和连接配置属于平台内部实现，不进入主代理 prompt，也不向用户暴露。用户可见查询口径、示例和安全边界必须优先从 references 读取。

## 工作流

1. 判断查询意图和目标数据域。
2. 提取用户给出的实体、条件、时间/地区/类型限定和期望输出。
3. 如果关键实体缺失，先追问一个最重要的问题。
4. 让平台服务准备 schema context、生成候选只读查询、执行 guard 校验、只读执行和结果筛选。
5. 基于平台返回的业务摘要、preview 和诊断生成最终回答。
6. 对 follow-up questions，沿用当前会话中的查询语境和结果摘要；必要时再次发起受控只读查询。

## 资源导航

- `references/usage.md`：查询输入、输出策略和澄清方式。
- `references/query-examples.md`：常见自然语言查询示例和口径表达。
- `references/data-boundaries.md`：只读边界、安全提示和禁止事项。

## 边界

- 只读：不执行写入、删除、更新、DDL、权限变更、多语句 SQL 或高风险管理操作。
- 不暴露数据库连接信息、账号、密码、LLM key、完整 prompt、handler、service、内部配置或本机路径。
- 不把本 Skill 转成普通脚本执行，也不让普通脚本直接绑定数据库、内部 LLM、secret 或完整环境变量。
- 缺少关键查询实体时返回可控澄清，不编造 SQL 或业务数据。
