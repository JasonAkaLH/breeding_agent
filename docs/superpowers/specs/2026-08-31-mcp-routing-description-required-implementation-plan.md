# MCP `routing_description` 必填最小实施计划

依据：`2026-08-31-mcp-routing-description-required-design.md`

设计提交：`b365fe89`；硬伤审查提交：`89197f7b`

状态：`complete`

目标分支：`main`

## 1. 完成声明与范围

完成后必须同时满足：

1. 新建 MCP Server 缺失、`null`、空串或纯空白 `routing_description` 时，在副作用前返回 422；
2. PATCH 省略该字段时保留原值，显式提交 `null`、空串或纯空白时返回 422；
3. 合法描述继续 trim，并保持 2000 字符和既有控制字符限制；
4. 前端新建和编辑表单都阻止空白描述提交；
5. 存量空描述不迁移，Service、数据库、Repository 和 MCP 执行链不修改。

生产代码只允许修改：

- `src/api/dto.py`
- `frontend/src/api/types.ts`
- `frontend/src/components/MCPSettingsPanel.tsx`

不新增依赖、错误码、配置、schema、迁移、网络调用、镜像、部署或 `prod` 变更。

## 2. Checkpoint A：聚焦红测

### 2.1 后端 DTO

修改 `tests/api/test_user_mcp_dto.py`，增加聚焦合同：

- Create 缺失、`None`、空串、纯空白均抛出 `ValidationError`；
- Create 合法描述被 trim；
- PATCH 省略字段后 `model_dump(exclude_unset=True)` 不包含该键；
- PATCH 显式 `None`、空串、纯空白均失败，合法描述被 trim。

### 2.2 后端 API

修改 `tests/api/test_user_mcp_api.py`，增加两个聚焦用例：

- 缺失描述的 POST 返回 422，Server 列表保持为空；
- 先创建合法 Server，再 PATCH 显式 `null`，断言返回 422，原描述和配置版本保持不变。

### 2.3 前端表单

修改 `frontend/src/components/MCPSettingsPanel.test.tsx`：

- 新建表单只填写名称和 HTTPS Endpoint、描述留空时，显示“请输入路由描述”，且不调用 `createMCPServer`；
- 现有成功创建用例补填合法描述，继续证明原 HTTP 风险确认和 HTTPS 直提交流程。

先只运行新增测试，确认旧实现精确失败；不提交红色状态。

## 3. Checkpoint B：最小实现与 fixture 收敛

### 3.1 后端 DTO

修改 `src/api/dto.py`：

- Create 字段删除 `default=""`；
- Create validator 在 trim 后同时拒绝空值和既有不支持控制字符；
- PATCH 字段继续以 `None` 作为省略默认值；validator 对显式 `None` 返回错误，对字符串执行相同的 trim、非空和控制字符校验；
- 不增加 model validator、Service fallback 或共享 helper。

### 3.2 前端

- `frontend/src/api/types.ts`：Create 请求的 `routing_description` 删除可选标记；
- `frontend/src/components/MCPSettingsPanel.tsx`：共用表单项增加 Ant Design `required + whitespace` 规则和明确提示；
- 不改变保存函数、PATCH payload、HTTP 风险确认或错误映射。

### 3.3 必要 fixture

运行聚焦相关测试，只为实际走 Create DTO 或 POST 创建入口且缺少描述的旧 fixture 补充最小合法值。不得修改直接构造 Core/Profile 的空描述恢复或兼容 fixture，因为它们不属于新写入 API 契约。

## 4. Checkpoint C：验证

后端聚焦门禁：

```bash
conda run -n multi_agent python -m unittest \
  tests.api.test_user_mcp_dto \
  tests.api.test_user_mcp_api \
  tests.api.test_user_mcp_grants_and_call_control \
  tests.integrations.mcp.test_user_mcp_config_service
```

前端门禁：

```bash
cd frontend
npm test -- --run src/components/MCPSettingsPanel.test.tsx src/api/client.test.ts
npm run typecheck
```

静态与范围门禁：

```bash
conda run -n multi_agent python -m compileall -q src/api tests/api tests/integrations/mcp
conda run -n multi_agent ruff check \
  src/api/dto.py \
  tests/api/test_user_mcp_dto.py \
  tests/api/test_user_mcp_api.py
git diff --check
```

最终 diff 必须证明生产代码只有批准的三个文件变化；测试变化只覆盖新合同和必要 fixture。若聚焦证据发现更广的真实契约依赖，先记录并重新评估，不扩张实现边界。

## 5. 文档闭合与提交

- 把设计状态更新为 `implemented_verified`；
- 把本计划更新为 `complete` 并记录实际红绿测试；
- 同步 `docs/AGENTS.md` 和 `CHANGELOG.md`；
- 检查目录职责、入口和测试入口未变化，因此不修改其他层级 `AGENTS.md`；
- 提交一个范围清晰的实现检查点，工作树保持干净。

## 6. 回滚

回退实现检查点即可恢复旧 DTO 默认值、前端可选类型和表单行为。没有数据库、数据、配置、镜像或部署回滚步骤。

## 7. 完成证据（2026-08-31）

- 旧实现红测精确证明：Create 缺失/空白描述未报错、POST 返回 202、PATCH 显式 `null` 返回 200，前端空白描述实际调用保存 API。
- 最小实现只修改 `src/api/dto.py`、`frontend/src/api/types.ts` 和 `frontend/src/components/MCPSettingsPanel.tsx` 三个生产文件。
- 新合同转绿：后端聚焦 3 项、前端聚焦 1 项通过；相关后端 38 项、前端 2 文件 39 项通过。
- 前端 typecheck、production build、Python compileall、变更面 Ruff 与 `git diff --check` 通过；build 仅保留既有的大 chunk 警告。
- 只为 5 个实际走 Create DTO/POST 的旧合法 fixture 补充描述；直接构造存量/Core Profile 的空描述兼容测试未修改。
- 未修改 schema、迁移、Service、Repository、MCP Router/Selector/Gateway、执行链、配置、依赖、镜像、部署或 `prod`。

License Requirement：复用现有 Python、Pydantic、FastAPI、React、TypeScript、Ant Design、unittest 和 Vitest；无新增依赖或许可变化。
