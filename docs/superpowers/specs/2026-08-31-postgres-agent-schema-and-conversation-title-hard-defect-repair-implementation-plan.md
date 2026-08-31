# PostgreSQL Agent Schema 与会话标题硬伤修复实施计划

依据：`2026-08-31-postgres-agent-schema-and-conversation-title-hard-defect-repair-design.md`

设计提交：`bfb8a223`；首轮硬伤修订提交：`86fddc2b`

状态：`planned`

目标分支：`main`

目标环境：main 开发环境；不涉及 `prod`

## 1. 完成声明与范围

用户已确认硬切边界：完全舍弃旧 DAG physical schema，但保留 conversation、message、Task、
TaskNode行、Agent ledger、Artifact、Event、MCP配置及其他业务数据；不重建数据库，不修复历史标题，
不复活旧失败Task。

完成必须同时满足：

1. 发现设计批准的7个旧对象时，backend在执行schema mutation前以
   `agent_schema_migration_required`停止；
2. 开发库在停写维护窗口内通过单一PostgreSQL事务删除`task_edge`、
   `task.root_node_id`和5个`task_node`旧字段；
3. 除被明确舍弃的旧schema外，保留表行数和Agent ledger digest不变；
4. 新普通Task能发布final TaskNode、Artifact、assistant message和Event，不再触发
   `IntegrityError/execution_crash`；
5. `<ds_safety>...`和`ds_safety>...`均不写入自动标题，普通标题与后续重试行为不变；
6. 只发布backend-dev `0.1.28`，frontend-dev和runtime-sidecar-dev继续使用`0.1.27`。

生产代码只修改：

- `src/api/conversation_titles.py`
- `src/state/postgres/schema_reconciler.py`
- `src/storage/postgres/bootstrap.py`

测试只修改：

- `tests/api/test_conversation_titles.py`
- `tests/storage/test_postgres_schema_reconciler.py`

不修改`src/storage/agent_schema_migration.py`，不新增migration operator、report/receipt协议、数据库
业务字段、依赖、Frontend、Rust/Sidecar、Provider、配置或`prod`。

## 2. Checkpoint A：现场基线与红测

### 2.1 只读现场基线

在任何代码或数据库写入前重新确认：

- `main`、HEAD、工作树和`docker_cmd.md`保护状态；
- 目标数据库为`biobin_dev`；
- `task_edge`、`task.root_node_id`和5个`task_node`旧字段仍存在；
- `criticality/dependency_type`仍为无默认值`NOT NULL`；
- `conv-web-11950b85c13388`与`task-d5253b81b794`保持原值；
- MCP aggregate schema继续为final。

只输出对象名、nullable/default、计数、digest和安全状态；不输出DSN、密码、用户消息正文或模型
原始标题。

### 2.2 标题红测

在`tests/api/test_conversation_titles.py`中先增加并确认旧实现失败：

- generator返回`<ds_safety>用户询问模型身份，属于正常技术问题`时，标题任务结束后title仍为
  `None`；
- generator返回`ds_safety>用户询问模型身份，属于正常技术问题`时结果相同；
- 普通中文标题及`标题：龙粳33品种查询`继续正常；
- 第一次控制标记被拒绝后，第二轮消息仍可重试并写入合法标题。

测试必须经过`_generate_and_store_conversation_title`真实路径证明不写库，不能只测试正则。

### 2.3 Schema红测

在`tests/storage/test_postgres_schema_reconciler.py`中增加表驱动红测，分别覆盖：

- `task.root_node_id`；
- `task_edge`；
- `task_node.criticality/dependency_type/retry_policy/timeout_policy/resource_class`。

旧实现必须因没有Agent operator-only reason和没有`agent_schema_migration_required`失败。红测阶段
不提交，不修改数据库。

## 3. Checkpoint B：标题最小修复

修改`src/api/conversation_titles.py`：

1. 在取得第一条非空行后、label stripping与两端字符清理前检查；
2. 拒绝`^<[A-Za-z_][A-Za-z0-9_.:-]*>`；
3. 精确拒绝`^ds_safety>`；
4. 命中时返回`None`，不剥离后继续使用，不记录原始内容，不抛异常；
5. 不修改手动rename、24字符截断、prompt、LLM options或异步CAS。

验证：

```bash
conda run -n multi_agent python -m unittest tests.api.test_conversation_titles
conda run -n multi_agent ruff check \
  src/api/conversation_titles.py tests/api/test_conversation_titles.py
```

建议检查点提交：`fix(api): reject control-tag conversation titles`。

## 4. Checkpoint C：PostgreSQL启动fail-fast

### 4.1 Reconciler识别

修改`src/state/postgres/schema_reconciler.py`：

- 为7个旧对象声明唯一常量集合；
- `plan_postgres_schema_reconciliation()`只读检查交集；
- 命中时追加稳定的`agent_schema_cutover_required:<object>` operator-only reason；
- 不生成DROP、UPDATE、backfill或兼容默认值；
- 不把未知额外对象纳入本次规则；
- MCP aggregate识别保持原样。

### 4.2 Bootstrap错误映射

修改`src/storage/postgres/bootstrap.py`：

- initial plan包含Agent reason时抛出
  `PostgresSchemaDriftError("agent_schema_migration_required")`；
- 只有MCP aggregate reason时继续抛出
  `mcp_dispatch_aggregate_migration_required`；
- 两者同时存在时先报告Agent migration；
- 必须在runtime/state DDL前fail-fast。

### 4.3 测试

扩充`tests/storage/test_postgres_schema_reconciler.py`：

- 表驱动覆盖7个旧对象；
- reason稳定且plan不包含destructive SQL；
- Agent/MCP/两者同时存在时错误码和优先级正确；
- fresh manifest保持现有行为；
- 现有MCP aggregate测试不改弱。

验证：

```bash
conda run -n multi_agent python -m unittest \
  tests.storage.test_postgres_schema_reconciler
conda run -n multi_agent ruff check \
  src/state/postgres/schema_reconciler.py \
  src/storage/postgres/bootstrap.py \
  tests/storage/test_postgres_schema_reconciler.py
```

建议检查点提交：`fix(storage): reject legacy agent schema at startup`。

## 5. Checkpoint D：自动验证与范围审计

聚焦测试：

```bash
conda run -n multi_agent python -m unittest \
  tests.api.test_conversation_titles \
  tests.storage.test_postgres_schema_reconciler
```

受影响目录回归：

```bash
conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
```

静态门禁：

```bash
conda run -n multi_agent python -m compileall -q src tests
conda run -n multi_agent ruff check \
  src/api/conversation_titles.py \
  src/state/postgres/schema_reconciler.py \
  src/storage/postgres/bootstrap.py \
  tests/api/test_conversation_titles.py \
  tests/storage/test_postgres_schema_reconciler.py
git diff --check
```

最终diff必须证明只有3个生产文件和2个测试文件发生功能变化；既有migration core、schema业务字段、
Frontend、Rust、Provider、配置、依赖和历史数据均未修改。

## 6. Checkpoint E：候选backend与部署命令准备

维护窗口前完成：

- 构建`linux/amd64` backend-dev `0.1.28`；
- 验证镜像不包含根`config.yaml`、凭据或迁移artifact；
- 使用隔离SQLite配置完成package import、strict config bootstrap和`/api-doc` health smoke；
- 用户授权时只推送backend-dev `0.1.28`并验证远端manifest/digest；
- frontend-dev和runtime-sidecar-dev保持`0.1.27`。

编辑受保护`docker_cmd.md`前，在仓库外创建`0600`备份。只允许：

- backend tag更新为`0.1.28`；
- 在停止旧backend前增加“旧schema已硬切 + 新镜像bootstrap成功”门禁；
- 其他镜像tag、网络、端口、volume、secret、Skill和MCP配置不变。

编辑后验证文件仍存在、`0600`、ignored/untracked且未进入Git，命令语法通过。

## 7. Checkpoint F：PostgreSQL旧Schema硬切

本步骤是唯一数据库破坏性操作。执行前再次确认用户授权边界：只舍弃旧DAG schema，保留其他
数据。

### 7.1 停写、快照与备份

1. 停止远端backend和其他`biobin_dev` writer；无法确认停写则不继续；
2. 记录所有保留表行数以及`agent_run/agent_item/agent_final_receipt` digest；
3. 使用官方`postgres:17` client容器创建custom-format完整`pg_dump`；
4. 备份目录权限`0700`、dump权限`0600`；
5. 运行`pg_restore --list`，失败则不继续；
6. 再次确认目标数据库名和7个旧对象与基线完全一致。

### 7.2 DBeaver单事务DDL

在DBeaver关闭auto-commit，对`biobin_dev`执行单一事务：

```sql
BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '30s';
SELECT pg_advisory_xact_lock(5566807924744996692);

DROP TABLE public.task_edge;
ALTER TABLE public.task DROP COLUMN root_node_id;
ALTER TABLE public.task_node DROP COLUMN criticality;
ALTER TABLE public.task_node DROP COLUMN dependency_type;
ALTER TABLE public.task_node DROP COLUMN retry_policy;
ALTER TABLE public.task_node DROP COLUMN timeout_policy;
ALTER TABLE public.task_node DROP COLUMN resource_class;
```

DDL不使用`IF EXISTS`，任何现场漂移都必须让事务失败。此时先不提交。

### 7.3 提交前后检查

在同一事务中确认：

- 7个旧对象全部不存在；
- 除`task_edge`外所有保留表行数与基线一致；
- `agent_run/agent_item/agent_final_receipt` digest与基线一致；
- `conv-web-11950b85c13388`、`task-d5253b81b794`和现有标题未更新。

全部一致才执行`COMMIT`；否则执行`ROLLBACK`。commit后重复只读检查，并用backend `0.1.28`
候选镜像执行严格PostgreSQL bootstrap预检。预检失败时停止部署，不手写补列。

## 8. Checkpoint G：部署与真实smoke

bootstrap预检成功后才按受保护命令替换backend：

- backend `0.1.28` healthcheck通过；
- 创建全新conversation并发送“你是谁？”；
- Task进入completed；
- final TaskNode、Artifact、assistant message和`agent.final_output` Event存在；
- 没有`IntegrityError`或`execution_crash`；
- 新标题为普通标题或空，不包含`<ds_safety>`、`ds_safety>`或规范开头控制标签；
- 旧conversation、失败Task和错误标题保持原值。

smoke只记录安全状态和错误码，不输出回答正文、凭据或provider请求内容。

## 9. Checkpoint H：文档闭合

- 把设计状态更新为`implemented_verified`；
- 把本计划更新为`complete`并记录测试、备份、DDL、postcheck、镜像digest和真实smoke证据；
- 按实际变化更新`src/api/AGENTS.md`、`src/state/AGENTS.md`、`docs/AGENTS.md`和
  `CHANGELOG.md`；其他AGENTS索引保持不变；
- 确认DSN、dump、计数明细、用户消息正文和`docker_cmd.md`未进入tracked diff；
- 复查工作树并保留无关用户修改。

建议账本提交：`docs: close agent schema and title repair`。

## 10. 停止条件

出现任一情况立即停止：

- 目标不是`main`开发库或writers无法确认停止；
- `pg_dump`或`pg_restore --list`失败；
- 旧对象集合与基线不一致；
- 需要删除批准范围之外的对象或修改保留表业务行；
- DDL事务、行数、digest或bootstrap检查失败；
- 标题修复需要通用审核、第二次LLM调用或历史数据更新；
- 需要修改migration core、Frontend、Rust、Provider、配置、依赖或`prod`。

## 11. 回滚

- DDL提交前失败：执行`ROLLBACK`，恢复旧backend服务；
- DDL提交后发现数据或schema异常：使用完整dump恢复`biobin_dev`并恢复旧backend；
- 硬切成功但新backend业务smoke失败：回退backend代码/镜像；不重新创建旧DAG schema；
- 标题或startup代码问题：回退对应Git检查点并重建backend；
- 禁止用手写`ADD COLUMN`、兼容默认值、旧Task重放或标题数据修正回滚。

License Requirement：复用现有Python、SQLAlchemy、PostgreSQL、DBeaver、Docker与unittest；无新增
依赖或许可变化。
