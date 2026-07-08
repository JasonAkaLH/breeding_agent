# 种质资源 MCP tool 使用指南

本 Skill 使用当前 MCP 上下文中已经注册的种质资源只读工具。不要在回答中暴露 endpoint、鉴权、header 或部署信息。

## 工具清单

| Tool | 用途 | 输入 |
| --- | --- | --- |
| `germ_query_org_crops` | 查询当前租户机构下可用作物列表 | `{}` |
| `germ_query_org_crop_traits` | 按作物 ID 查询可用于检索的性状编码和类型 | `{ "cropId": 123 }` |
| `germ_query_germ_fields` | 查询种质扩展字段定义，用于组装 `searchFieldData` | `{}` |
| `germ_search` | 分页搜索种质信息 | 需要传齐所有字段，未使用条件传默认值 |

## 推荐调用顺序

1. **用户问有哪些作物**：直接调用 `germ_query_org_crops`。
2. **用户提供作物名称但没有 cropId**：先调用 `germ_query_org_crops`，按名称匹配候选；候选不唯一时让用户选择。
3. **用户要按性状筛选**：先调用 `germ_query_org_crop_traits` 获取 `traitCode` 和 `traitType`，再调用 `germ_search`。
4. **用户要按扩展字段筛选**：先调用 `germ_query_germ_fields` 获取字段 `key`，再调用 `germ_search`。
5. **用户只按种质名称搜索**：可以直接调用 `germ_search`，将名称放入 `germNamesList`。

## 输出整理

- 工具返回 JSON 字符串时，先解析为 JSON 再总结。
- 如果字段很多，优先展示作物 ID/名称、种质 ID/名称、来源、作物、分页总数等核心字段。
- 不确定字段含义时，说明“远程 MCP 返回字段名为 ...”，不要臆造业务含义。
