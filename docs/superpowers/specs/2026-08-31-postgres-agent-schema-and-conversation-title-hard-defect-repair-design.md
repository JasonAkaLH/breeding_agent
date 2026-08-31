# PostgreSQL Agent Schema 与会话标题硬伤修复设计

- 日期：2026-08-31
- 状态：`written_review_pending`
- 目标分支：`main`
- 目标环境：main 开发环境；不涉及 `prod`

## 1. 背景与已确认事实

远端开发库 `biobin_dev` 中，`conv-web-11950b85c13388` 的唯一 Task
`task-d5253b81b794` 已失败。数据库证据表明模型采样成功并提交了
`assistant_message`，随后在发布最终回答时触发 `IntegrityError`，Agent Run 以
`execution_crash` 终止。

当前代码创建最终 `TaskNodeRow` 时只写 Agent invocation 字段；远端 PostgreSQL
`task_node` 却仍保留以下已退役 DAG 字段：

- `criticality`：`NOT NULL` 且无默认值；
- `dependency_type`：`NOT NULL` 且无默认值；
- `retry_policy`；
- `timeout_policy`；
- `resource_class`。

因此最终节点插入必然违反远端旧 Schema 的非空约束。现有 Unified Agent Loop
Phase 7 已定义并实现跨 backend 破坏性迁移，但当前部署没有完成该迁移；PostgreSQL
bootstrap 也没有拒绝这类额外旧列，导致不兼容实例能够启动，直到真实请求落库才失败。

同一 conversation 的标题为：

```text
ds_safety>用户询问模型身份，属于正常技
```

当前标题归一化会取模型输出第一条非空行，剥除两端标点后截断至 24 个字符。输入
`<ds_safety>用户询问模型身份，属于正常技术问题` 会精确产生上述标题，说明模型控制标签
被误当作普通标题写入。标题任务独立异步执行，因此其写入与主 Task 成败没有因果关系。

## 2. 目标与成功标准

本次只修复两个已确认硬伤：

1. 旧 Unified Agent Schema 不再允许新 backend 启动，并通过既有受控迁移使开发库与当前
   Agent-only Schema 对齐。
2. 标题模型返回标签式控制内容时，不再把控制标签或其解释文本写入 conversation title。

完成后：

- 新普通聊天能够生成并持久化最终 assistant message，Task 正常完成；
- 旧 DAG physical contracts 存在时，backend 启动直接报告
  `agent_schema_migration_required`；
- `<ds_safety>...` 不产生标题；正常中文标题行为不变；
- 不修改任何既有 conversation、消息、Task、Run、Artifact 或 Event 数据。

## 3. 方案决策

采用“既有迁移 authority + 启动 fail-fast + 标题控制标签整条拒绝”。

不采用以下替代方案：

- 不给旧 `criticality` / `dependency_type` 增加默认值，也不把已删除字段重新加入 ORM；
  这会恢复已退役 DAG 持久化合同。
- 不在 bootstrap 中自动执行 `DROP TABLE` 或 `DROP COLUMN`；破坏性变更必须继续由 operator
  在备份和恢复验证后执行。
- 不剥离 `<ds_safety>` 后继续使用剩余文本；标签后的内容仍是分类解释，不是可靠标题。
- 不增加第二次 LLM 调用、备用模型、通用内容审核或基于用户消息的另一套标题生成器。

## 4. PostgreSQL Schema 修复

### 4.1 启动门禁

PostgreSQL schema inspection 增加 Unified Agent Loop Phase 7 旧物理合同识别，范围固定为：

- `task.root_node_id`；
- `task_node.criticality`；
- `task_node.dependency_type`；
- `task_node.retry_policy`；
- `task_node.timeout_policy`；
- `task_node.resource_class`；
- `task_edge` 表。

发现任一项时，reconciliation plan 只生成 operator-only 原因，不生成破坏性 SQL。
`bootstrap_postgres_database` 将该原因映射为稳定错误码
`agent_schema_migration_required`。若只有既有 MCP aggregate drift，继续保持
`mcp_dispatch_aggregate_migration_required`，不改变已发布语义。

该门禁只覆盖已冻结的 Phase 7 删除集合，不建设通用“额外列即失败”的 Schema
比较框架。

### 4.2 受控迁移

复用 `scripts/migrate_unified_agent_loop_schema.py` 和
`src/storage/agent_schema_migration.py`，不新增第二个 PostgreSQL-only drop 脚本。执行顺序固定为：

1. 确认 `main` 与开发环境，停止 backend/Sidecar 等相关 writer；
2. 在仓库外准备权限受限的 state、backup、restore 目录；
3. 运行只读 `report` 并核对 blocker 和待删除 inventory；
4. 运行 `backup`；
5. 在隔离目标运行 `restore-check`；
6. 运行 `apply`；
7. 重新生成 report，确认三个 backend 的旧 DAG 字段均为空集合；
8. 启动新 backend，确认 bootstrap 通过。

迁移不得输出或提交 DSN、凭据、备份路径中的敏感数据。根目录 `docker_cmd.md` 继续为
Git-ignored 本地文件；只补充上述迁移前置和启动后 smoke，不进入 Git。

## 5. 会话标题修复

在 `normalize_generated_conversation_title` 取得第一条非空行后、执行 label stripping、字符
trim 和长度截断前，增加一个窄拒绝规则：若该行以 XML-like ASCII 标签开头，则返回
`None`。标签形状固定为：

```regex
^<[A-Za-z_][A-Za-z0-9_.:-]*>
```

这会覆盖已出现的 `<ds_safety>`，也符合提示词“只输出名称本身”的既有合同。规则不扫描
标题中部、不解析 XML、不维护 provider 标签列表，也不评价普通标题语义。

无效结果继续走现有失败语义：本轮不写标题；如果 conversation 后续仍无标题，下一轮用户
消息可触发既有重试。不会将无效标签写入 audit payload，也不记录模型原始标题。

现有错误标题以及其他历史数据全部保持不变。

## 6. 数据流与错误处理

### 6.1 新部署

```text
backend bootstrap
  -> inspect PostgreSQL schema
  -> legacy Agent physical contract?
       yes: fail agent_schema_migration_required
       no: continue existing safe reconciliation
```

### 6.2 标题生成

```text
LLM raw title
  -> first non-empty line
  -> leading control tag?
       yes: return None, do not write title
       no: existing normalize / truncate / validate / CAS write
```

Migration失败时保持 writers 停止，不启动不匹配 binary；按既有 operator receipt 和已验证备份
恢复。标题校验失败是既有 fail-open 辅助能力边界，不影响主 Task 执行。

## 7. 测试与验收

### 7.1 自动测试

- `tests/api/test_conversation_titles.py`
  - 精确输入 `<ds_safety>用户询问模型身份，属于正常技术问题`，断言不写标题；
  - 普通中文标题继续通过；
  - 现有生成失败后下一轮重试行为不变。
- `tests/storage/test_postgres_schema_reconciler.py`
  - `task_node` 任一旧 DAG 字段产生 Agent migration operator-only 原因；
  - 旧 `task_edge` 或 `task.root_node_id` 同样被拒绝；
  - MCP aggregate drift 的既有错误码不变；
  - fresh/current Schema 无新 action。
- `tests/storage/test_agent_schema_destructive_migration.py`
  - 复验迁移后旧字段为空集合，Agent 数据 inventory 保持。
- 运行相关 API、Storage 测试、compileall、Ruff 和 `git diff --check`。

### 7.2 远端开发环境

1. 迁移前只读报告必须精确显示当前旧字段。
2. backup 与隔离 restore-check 必须成功后才能 apply。
3. apply 后 `task_node` 不再包含 5 个旧字段，`task.root_node_id` 和 `task_edge` 也不存在。
4. backend bootstrap 成功。
5. 使用新 conversation 发送“你是谁？”：Task 完成、assistant message 可见、无
   `execution_crash`。
6. 标题允许为正常标题或暂时为空，但不得包含 `<ds_safety>`、`ds_safety>` 或其他开头控制
   标签。

旧失败 Task 不复活、不重放、不补写 assistant message。远端验证只面向 `main` 开发环境，
不代表 `prod` 已发布。

## 8. 发布与回滚

代码验证通过后，后续实施计划可安排新的 main 开发镜像版本，并同步受保护
`docker_cmd.md`。镜像构建、推送和远端部署属于实施阶段，需按当时用户授权执行。

回滚分两类：

- 代码回滚：恢复前一镜像；若数据库已经完成 Phase 7 migration，不重新创建旧 DAG 字段。
- 迁移回滚：仅在迁移失败或验证失败时，使用 operator 已验证备份和对应旧代码整体恢复；
  不手写反向字段猜测。

标题校验回滚只需撤销窄拒绝规则，不涉及数据迁移。历史错误标题不在任何回滚步骤中修改。

## 9. 明确不在范围

- 修改当前或其他既有 conversation title；
- 修复、重放或删除既有失败 Task；
- 自动生成 fallback title；
- 通用 LLM 输出审核、Provider 适配或模型切换；
- 新数据库字段、配置项、依赖、Frontend 或 Rust 协议改造；
- `prod` 数据库、镜像或部署变更。

License Requirement：复用现有 Python、SQLAlchemy、PostgreSQL migration operator 与 unittest，
无新增依赖或许可变化。
