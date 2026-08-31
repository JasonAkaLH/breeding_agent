# MCP `routing_description` 必填最小设计

## 状态

`written_review_pending`

对话中的需求边界和方案已经获批；本文等待用户复核后再生成实施计划。当前没有业务代码变更。

## 背景

用户级 MCP Server 的 `routing_description` 会进入模型可见的安全 Profile，帮助模型选择 MCP Server。当前外部契约仍允许创建请求省略该字段或提交空白，前端表单也没有必填校验，导致合法配置可能缺少实际路由信息。

现有数据库列只保证 `NOT NULL`，允许空字符串；仓库中也可能存在空描述的存量记录。本次目标是收紧后续写入，不迁移或阻断存量数据。

## 已批准语义

- 新建 MCP Server 时必须提交 `routing_description`，trim 后必须非空。
- PATCH 可以省略 `routing_description`，省略表示保留原值。
- PATCH 显式提交 `routing_description` 时，trim 后必须非空。
- 存量空描述不迁移；省略该字段的其他配置更新不受影响。
- 最大长度继续为 2000 个字符，现有控制字符限制保持不变。

## 最小实现

### 后端 API DTO

在 `src/api/dto.py` 中收紧现有两个请求模型：

- `CreateUserMCPServerRequest.routing_description` 删除空字符串默认值，成为必填字段。
- 现有规范化 validator 在 trim 后增加非空判断。
- `PatchUserMCPServerRequest.routing_description` 保持可选；validator 仅在字段非 `None` 时执行相同的 trim、非空和控制字符校验。

FastAPI/Pydantic 继续在进入配置 Service 前返回标准 422。配置 Service、Core model、数据库、Repository 和运行时路由不增加重复校验；这与当前 `display_name` 等外部输入由 DTO 负责校验的分层一致。

### 前端契约与表单

- 在 `frontend/src/api/types.ts` 中将 `CreateMCPServerRequest.routing_description` 从可选改为必填。
- 在 `frontend/src/components/MCPSettingsPanel.tsx` 的共用新建/编辑表单项上增加必填及纯空白校验，并提供明确提示。
- 保持现有保存流程、HTTP 风险确认和错误展示不变，不新增状态或抽象。

## 数据流与错误行为

合法的新建或显式更新值在 DTO 中 trim 后进入现有 Service 和持久化链路。缺失的新建字段，以及新建或 PATCH 中的空串、纯空白值，在任何配置副作用前返回 422。PATCH 省略字段时继续由现有 `exclude_unset`/增量更新语义保留原值。

前端表单在调用 API 前阻止空白提交；后端校验仍是外部契约 authority，不依赖前端正确性。

## 验证

- DTO 回归：创建缺失、空串和纯空白均失败；合法值被 trim；PATCH 省略成功，显式空串和纯空白失败。
- API 回归：创建请求缺失 `routing_description` 返回 422，且不创建 Server。
- 前端回归：空白描述显示校验错误且不调用保存 API；合法描述仍可创建和编辑。
- 更新因创建契约变更而缺少描述的现有测试 fixture。
- 运行聚焦后端测试、聚焦前端组件测试、前端 typecheck，并检查最终 diff。

## 明确排除

- 数据库 schema、CHECK constraint 或数据迁移。
- 存量空描述的自动修复、禁用或读取过滤。
- 配置 Service、Repository、MCP Router/Selector/Gateway、模型 Profile 或执行链改造。
- 新错误码、配置项、依赖、镜像、部署和 `prod` 变更。

## 回滚

回滚 DTO 字段默认值、validator 非空判断、前端请求类型和表单规则即可恢复原行为；没有数据或 schema 回滚步骤。
