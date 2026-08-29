# MCP Server Profile 模型可见性最小实施计划

依据：`2026-08-29-mcp-server-profile-model-visibility-design.md`
设计提交：`ceed9aab`
状态：`complete`
目标分支：`main`

## 1. 完成声明

唯一目标是在 Outer Agent 的单一 `mcp.dispatch` Tool description中追加当前安全 MCP
Server Profile的`server_id + display_name/name + routing_description` canonical JSON，使模型
能与Skill description一起做语义选择。

完成时只修改一处生产文件、一处测试文件和状态文档；`server_id` schema enum、MCP执行链、
Skill、DTO、数据库、前端与`prod`全部不变。

## 2. 范围保护

- 开始实施前确认`main`工作树只包含本计划文档变更。
- 不读取或修改`docker_cmd.md`，不触碰Git-ignored运行配置。
- 不新增独立MCP Server Tool、Schema `oneOf`、配置项、截断、分页或fallback。
- 不修改`UserMCPServerProfile`、Profile过滤、Registry、Context Builder或Provider adapter。
- 不新增依赖、迁移、网络调用、镜像构建或部署。

## 3. Checkpoint A：聚焦红测

只扩展`tests/orchestration/test_agent_tool_catalog.py`，新增一个聚焦用例：

1. 构造两个输入顺序相反的安全Profile。
2. 构建两个Catalog并取得`mcp.dispatch` Tool description。
3. 断言旧实现缺少`available_mcp_servers`，先得到精确红测。
4. 实现后解析追加的canonical JSON并断言：
   - 两个`server_id/name/routing_description`完整存在；
   - 按`server_id`稳定排序，输入顺序不影响description；
   - 引号、换行和伪指令仍是JSON字符串数据；
   - untrusted notice存在；
   - description不含`endpoint_url`、credential、transport、health或内部Tool字段；
   - `server_id` enum仍是排序后的两个ID；
   - 同Catalog中的Skill description不变。

红测命令：

```bash
conda run -n multi_agent python -m unittest \
  tests.orchestration.test_agent_tool_catalog.AgentToolCatalogTest.test_mcp_dispatch_description_exposes_safe_server_profiles
```

## 4. Checkpoint B：最小生产实现

只修改`src/orchestration/agent_loop/tool_catalog.py`：

1. 增加一个private纯函数，接收静态description和
   `CapabilityVisibilityContext.safe_mcp_server_profiles`。
2. 按`server_id.encode("utf-8")`排序Profile。
3. 投影每项精确三键：`server_id`、`name=display_name`、`routing_description`。
4. 使用既有`json`模块生成`ensure_ascii=False/sort_keys=True/compact separators`的
   canonical JSON，并追加到静态description。
5. 仅在`descriptor.capability_id == "mcp.dispatch"`时使用动态description；其他Tool继续使用
   原`descriptor.description`。
6. `_mcp_dispatch_schema`保持零变更。

实现后先重跑Checkpoint A红测，再运行整个Tool Catalog测试文件。

Implementation commit：`feat(agent): expose MCP server profiles to model routing`

## 5. Checkpoint C：相关回归与范围审计

执行：

```bash
conda run -n multi_agent python -m unittest \
  tests.orchestration.test_agent_tool_catalog \
  tests.orchestration.test_agent_catalog_preflight \
  tests.orchestration.test_agent_loop
conda run -n multi_agent python -m compileall -q \
  src/orchestration/agent_loop tests/orchestration/test_agent_tool_catalog.py
conda run -n multi_agent ruff check \
  src/orchestration/agent_loop/tool_catalog.py \
  tests/orchestration/test_agent_tool_catalog.py
git diff --check
```

最终差异必须证明：

- 生产代码只有`tool_catalog.py`变化；
- `_mcp_dispatch_schema`、Skill路径和其他模块零变更；
- 无Endpoint、凭据或内部Tool字段进入动态description；
- 未修改前端、数据库、Rust、配置、镜像或部署文件。

## 6. 文档闭合

- 把设计状态更新为`implemented`。
- 把本计划状态更新为`complete`并记录实际红测、绿测和提交。
- 把`CHANGELOG.md`从“仅设计”更新为实际实现与验证结果。
- 检查本次代码变化未改变目录职责、入口或测试入口，因此除既有设计/计划索引外不修改其他
  `AGENTS.md`。
- 最终工作树干净，`prod`未修改或部署。

## 7. 回滚

回退唯一Implementation commit，恢复静态`mcp.dispatch` description并移除对应聚焦测试；无需
回滚数据库、用户MCP配置、前端、镜像或远端服务。

License Requirement：复用现有Python、Agent Tool Catalog、JSON与context preflight能力；
无新增依赖或许可变化。

## 8. 完成证据（2026-08-29）

- 红测在旧代码上以`mcp.dispatch` description缺少换行后的Profile JSON精确失败。
- `2444f196`只修改`tool_catalog.py`和`test_agent_tool_catalog.py`：动态description包含排序后的
  `server_id/name/routing_description`与untrusted notice，现有Schema enum和Skill description
  保持不变。
- Tool Catalog、Catalog preflight和Agent Loop共19项通过；compileall、Ruff与
  `git diff --check`通过。
- DTO、数据库、MCP执行链、前端、Rust、配置、镜像和`prod`零变更；未发起网络调用或部署。
