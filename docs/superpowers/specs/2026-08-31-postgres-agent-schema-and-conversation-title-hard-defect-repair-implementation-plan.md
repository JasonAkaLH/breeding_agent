# PostgreSQL Agent Schema 与会话标题硬伤修复实施计划

依据：`2026-08-31-postgres-agent-schema-and-conversation-title-hard-defect-repair-design.md`

设计提交：`bfb8a223`；硬伤修订提交：`86fddc2b`

状态：`planned`

目标分支：`main`

目标环境：main 开发环境；不涉及 `prod`

## 1. 完成声明与范围

完成必须同时满足：

1. PostgreSQL 中存在任一设计批准的 7 个旧 Agent physical contract 时，backend 在执行任何
   schema mutation 前以 `agent_schema_migration_required` 停止；
2. 当前开发库通过已验证备份、隔离恢复、事务迁移和 post verify 删除
   `task.root_node_id`、5 个 `task_node` 字段和 `task_edge`；
3. 正常 Task 能发布 final `TaskNode`、Artifact、assistant message 和 Event，不再因旧字段触发
   `IntegrityError/execution_crash`；
4. `<ds_safety>...` 和 `ds_safety>...` 均被视为无效自动标题，普通标题与后续重试行为不变；
5. `conv-web-11950b85c13388` 及所有既有 conversation、失败 Task、消息和事件不被修正、重放或
   删除；
6. 只发布新的 backend 开发镜像 `0.1.28`；未修改的 frontend 和 Runtime Sidecar 继续使用
   `0.1.27`，不制造无代码变化的重打包。

生产代码和 operator 变更范围固定为：

- `src/api/conversation_titles.py`
- `src/state/postgres/schema_reconciler.py`
- `src/storage/postgres/bootstrap.py`
- `src/storage/agent_schema_migration.py`
- `scripts/migrate_unified_agent_postgres_schema.py`（新增）

测试范围固定为：

- `tests/api/test_conversation_titles.py`
- `tests/storage/test_postgres_schema_reconciler.py`
- `tests/storage/test_agent_schema_destructive_migration.py`
- `tests/scripts/test_migrate_unified_agent_loop_schema.py`
- `tests/scripts/test_migrate_unified_agent_postgres_schema.py`（新增）

不修改 DTO、API 路由、数据库业务字段、Frontend、Rust/Sidecar 协议、LLM Provider、模型配置、
历史标题、旧失败 Task 或 `prod`；不新增依赖、通用 migration framework 或标题审核器。

## 2. Checkpoint A：冻结现场证据与聚焦红测

### 2.1 现场只读基线

在任何代码或数据库写入前，重新只读确认并记录脱敏结果：

- 当前分支、HEAD、工作树及 `docker_cmd.md` 存在/权限/ignored/untracked 状态；
- 目标数据库为 `biobin_dev`；
- `task.root_node_id`、`task_edge` 和 5 个 `task_node` 旧字段仍存在；
- `criticality`、`dependency_type` 仍为无默认值 `NOT NULL`；
- `task-d5253b81b794` 保持 `failed/execution_crash`，不把它当作迁移对象；
- MCP aggregate 表继续为 final，避免把已完成迁移重新纳入本次范围。

输出只包含数据库名、对象名、nullable/default、计数、digest 和安全状态，不输出 DSN、密码、
Header、用户消息正文或标题生成原始输出。

### 2.2 标题红测

先修改 `tests/api/test_conversation_titles.py`，在旧实现上确认以下测试失败：

- title generator 返回 `<ds_safety>用户询问模型身份，属于正常技术问题` 时，异步标题任务结束后
  conversation title 仍为 `None`；
- generator 返回 `ds_safety>用户询问模型身份，属于正常技术问题` 时结果相同；
- `标题：龙粳33品种查询` 或普通 `龙粳33品种查询` 继续归一化为合法标题；
- 第一次控制标记结果被拒绝后，第二轮用户消息仍可沿用既有逻辑重试并写入合法标题。

测试必须经过真实 `_generate_and_store_conversation_title` 路径证明“不写库”，不能只断言正则。

### 2.3 Schema fail-fast 红测

先修改 `tests/storage/test_postgres_schema_reconciler.py`，在旧实现上确认：

- `task.root_node_id`；
- `task_edge`；
- `task_node.criticality/dependency_type/retry_policy/timeout_policy/resource_class`

分别不能触发 Agent operator-only 原因，形成精确红测。再增加 bootstrap 红测，证明旧实现仍会
继续到 additive bootstrap，而不是抛出 `agent_schema_migration_required`。

红测阶段不提交，不修改数据库。

## 3. Checkpoint B：标题最小修复

修改 `src/api/conversation_titles.py`：

1. 在 `_first_non_empty_line` 后、`_strip_label_prefix` 和两端字符清理前检查原始首行；
2. 使用现有 `re` 依赖拒绝规范的开头 ASCII 标签：
   `^<[A-Za-z_][A-Za-z0-9_.:-]*>`；
3. 另以精确前缀拒绝本次无法反推左尖括号的 `ds_safety>`；
4. 命中后直接返回 `None`，不剥离后继续使用，不记录原始内容，不抛异常；
5. 不修改手动 rename 校验、24 字符截断、标题 prompt、LLM options 或异步 CAS。

运行：

```bash
conda run -n multi_agent python -m unittest tests.api.test_conversation_titles
conda run -n multi_agent ruff check \
  src/api/conversation_titles.py tests/api/test_conversation_titles.py
```

通过后形成独立检查点，建议提交信息：
`fix(api): reject control-tag conversation titles`。

## 4. Checkpoint C：PostgreSQL 启动 fail-fast

### 4.1 Reconciler 只读识别

修改 `src/state/postgres/schema_reconciler.py`：

- 为设计中冻结的 7 个旧对象声明唯一常量集合；
- `plan_postgres_schema_reconciliation()` 检查 inspected tables/columns 与该集合的交集；
- 每个命中生成稳定、可测试的
  `agent_schema_cutover_required:<table-or-column>` operator-only reason；
- 不向 `actions` 添加 `DROP`、`UPDATE`、backfill 或兼容默认值；
- 不把任意未知额外列升级为错误；只识别批准集合；
- MCP aggregate operator-only 识别保持原样。

### 4.2 Bootstrap 错误映射

修改 `src/storage/postgres/bootstrap.py`：

- initial plan 中存在 Agent cutover reason 时优先抛出
  `PostgresSchemaDriftError("agent_schema_migration_required")`；
- 没有 Agent reason、只有 MCP aggregate reason 时继续抛出
  `mcp_dispatch_aggregate_migration_required`；
- 两类 drift 同时存在时先报告 Agent migration，Agent迁移完成后下一次启动再报告仍存在的 MCP
  drift，禁止把两个 operator 混成自动顺序；
- fail-fast 必须发生在执行 runtime/state DDL 前；已有 lock/timeout statements 不算 schema
  mutation。

### 4.3 测试

扩充 `tests/storage/test_postgres_schema_reconciler.py`：

- 表驱动覆盖 7 个旧对象；
- 断言 reason 稳定且 SQL script 不含对应 destructive SQL；
- bootstrap 断言 Agent/MCP/两者同时存在时错误码与优先级；
- fresh manifest 继续只有既有 publication backfill，无 operator-only reason；
- 现有 MCP aggregate test 不改弱。

运行：

```bash
conda run -n multi_agent python -m unittest \
  tests.storage.test_postgres_schema_reconciler
conda run -n multi_agent ruff check \
  src/state/postgres/schema_reconciler.py \
  src/storage/postgres/bootstrap.py \
  tests/storage/test_postgres_schema_reconciler.py
```

通过后形成独立检查点，建议提交信息：
`fix(storage): fail fast on legacy agent schema`。

## 5. Checkpoint D：PostgreSQL 单库 operator

### 5.1 共享核心收窄

在 `src/storage/agent_schema_migration.py` 中复用并公开最小 PostgreSQL-only seam，保持既有
三 backend operator 的命令、receipt 和执行顺序不变：

- 旧 DAG 对象集合继续只有一个 owner；新 operator 禁止复制字段名列表；
- PostgreSQL inventory 支持在 caller 已持有的 connection/transaction 中执行，避免 apply
  持 advisory lock 后另开连接观察漂移；
- 保留表行数、Agent ledger 表行数/digest、legacy object inventory 和 schema digest 进入
  canonical report；Agent ledger 固定为不受本次删列影响的
  `agent_run/agent_item/agent_final_receipt`；
- post inventory 的期望变化只允许：删除 `task_edge`、可选 legacy planner表和批准列；所有保留
  表行数与 Agent ledger digest 必须不变；
- 复用现有 PostgreSQL advisory lock key和事务 drop 语义；不修改 SQLite/Sidecar helper。

既有 `tests/storage/test_agent_schema_destructive_migration.py` 和
`tests/scripts/test_migrate_unified_agent_loop_schema.py` 必须证明三 backend operator 行为完全保持。

### 5.2 CLI 合同

新增 `scripts/migrate_unified_agent_postgres_schema.py`，只接受环境变量名，不接受明文 DSN 参数。
子命令固定为：

- `report`
  - 对源库生成 `0600`、no-clobber canonical report；
  - 若携带 `--match-source-report`，则把当前恢复库 inventory 与源 report 做 exact semantic
    comparison，成功后生成 `0600` restore receipt；
- `apply`
  - 必须携带源 report path、expected report SHA 和 restore receipt；
  - 在同一 advisory lock/事务内重新检查 source inventory；
  - source drift、receipt drift、缺权限、lock失败或postcondition失败均拒绝并回滚；
  - 当前已经精确处于 expected post state 时返回 `already_applied`，其他partial状态拒绝；
- `verify`
  - 只读生成 post report，验证 legacy objects为空且保留数据与源 report一致。

CLI 输出只允许 safe JSON：result、reason_code、report/receipt SHA、legacy object names和计数；禁止
输出 DSN、host、username、SQL参数、数据正文、绝对敏感路径或driver异常正文。退出码固定为：

- `0`：成功或 exact replay；
- `2`：配置、报告、receipt、权限或环境错误；
- `3`：source/restore/post inventory冲突或partial migration。

### 5.3 Operator 红绿测试

新增 `tests/scripts/test_migrate_unified_agent_postgres_schema.py`，使用 fake/临时 PostgreSQL seam
验证：

- report只读、canonical SHA、0600、no-clobber和DSN延迟读取；
- source/restore semantic mismatch拒绝生成receipt；
- apply缺expected SHA或receipt、source drift、advisory lock失败均零DDL；
- 成功路径在一个transaction内按固定集合执行drop并在commit前验证post inventory；
- postcondition失败rollback；
- exact replay返回稳定结果，不再次drop；
- partial schema拒绝，不猜测续迁；
- 日志和异常输出不含DSN或数据；
- 不打开SQLite文件、不调用Sidecar probe。

运行：

```bash
conda run -n multi_agent python -m unittest \
  tests.scripts.test_migrate_unified_agent_postgres_schema \
  tests.scripts.test_migrate_unified_agent_loop_schema \
  tests.storage.test_agent_schema_destructive_migration
conda run -n multi_agent ruff check \
  src/storage/agent_schema_migration.py \
  scripts/migrate_unified_agent_postgres_schema.py \
  tests/scripts/test_migrate_unified_agent_postgres_schema.py \
  tests/scripts/test_migrate_unified_agent_loop_schema.py \
  tests/storage/test_agent_schema_destructive_migration.py
```

通过后形成独立检查点，建议提交信息：
`feat(storage): add postgres agent schema operator`。

## 6. Checkpoint E：自动验证与范围审计

先运行组合聚焦门禁：

```bash
conda run -n multi_agent python -m unittest \
  tests.api.test_conversation_titles \
  tests.storage.test_postgres_schema_reconciler \
  tests.storage.test_agent_schema_destructive_migration \
  tests.scripts.test_migrate_unified_agent_loop_schema \
  tests.scripts.test_migrate_unified_agent_postgres_schema
```

再运行受影响目录回归：

```bash
conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/scripts -p 'test_*.py'
```

静态与范围门禁：

```bash
conda run -n multi_agent python -m compileall -q src scripts tests
conda run -n multi_agent ruff check \
  src/api/conversation_titles.py \
  src/state/postgres/schema_reconciler.py \
  src/storage/postgres/bootstrap.py \
  src/storage/agent_schema_migration.py \
  scripts/migrate_unified_agent_postgres_schema.py \
  tests/api/test_conversation_titles.py \
  tests/storage/test_postgres_schema_reconciler.py \
  tests/storage/test_agent_schema_destructive_migration.py \
  tests/scripts/test_migrate_unified_agent_loop_schema.py \
  tests/scripts/test_migrate_unified_agent_postgres_schema.py
git diff --check
```

最终 diff 必须证明：无数据库业务字段、Frontend、Rust、Provider、配置、依赖和历史数据修正；
三 backend旧operator未被削弱；新 operator没有第二套legacy字段集合。

## 7. Checkpoint F：候选 backend 与部署命令准备

远端writer停止前完成候选构建和可用性确认：

- 只构建 `linux/amd64` backend开发镜像
  `registry.cn-hangzhou.aliyuncs.com/biobin/breeding-agent-backend-dev:0.1.28`；
- 验证镜像不包含根 `config.yaml`、不新增迁移artifact或凭据；
- 临时容器使用隔离SQLite配置运行package import、strict config bootstrap和`/api-doc` health
  smoke，不在迁移前连接仍含旧字段的远端PostgreSQL；
- 用户授权发布时只推送backend `0.1.28`并验证远端manifest/digest，使服务器在维护窗口内无需
  临时等待构建或推送；
- frontend-dev和runtime-sidecar-dev保持`0.1.27`。

受保护 `docker_cmd.md` 编辑前必须在仓库外创建权限不高于`0600`的备份；只做：

- backend tag从`0.1.27`更新到`0.1.28`，但此时不执行替换；
- 在停止旧backend前增加“operator已完成 + 新镜像严格bootstrap成功”的硬门禁；
- 保留frontend/Sidecar tag、网络、端口、volume、secret、Skill和MCP配置原样。

编辑后验证文件仍存在、`0600`、ignored/untracked、未进入Git，命令块语法通过。不得读取后在
回复或日志中输出敏感内容。

## 8. Checkpoint G：隔离 PostgreSQL 迁移演练

候选镜像和部署命令就绪后、远端apply前执行：

1. 再次检查 `docker_cmd.md` 保护状态；
2. 在仓库外创建 `0700`工作目录，报告、receipt和dump均为`0600`；
3. 停止远端 backend 和其他目标库 writer；无法确认writer已停则不继续；
4. 用新 operator 对 `175.6.25.109:15432/biobin_dev` 生成source report；
5. 使用官方 `postgres:17` client容器生成custom dump，不在命令或日志展开DSN；
6. 启动一次性本地 PostgreSQL 17，恢复dump并生成source/restore exact-match receipt；
7. 先在恢复库执行apply和verify，再让backend `0.1.28`候选镜像连接恢复库执行严格bootstrap
   smoke，证明DDL、postcondition和镜像内代码兼容；
8. 恢复库演练失败时停止，远端库保持不变并恢复旧backend服务。

隔离演练不得把report、receipt、dump、DSN或本地绝对敏感路径写入仓库或Git object。

## 9. Checkpoint H：远端迁移、部署与真实smoke

只有 Checkpoint G 全绿且远端writers仍停止时执行：

1. 对源库重新验证当前inventory仍与source report SHA一致；
2. 运行operator `apply`；
3. 运行operator `verify`；
4. 只读确认旧对象全部不存在、其他表行数与Agent ledger digest不漂移；
5. 确认既有 `conv-web-11950b85c13388`、`task-d5253b81b794` 和错误标题均未更新；
6. 服务器拉取已发布backend `0.1.28`，按受保护命令执行严格bootstrap预检；
7. 预检成功后才替换旧backend，确认healthcheck通过。

任何partial schema、digest/row count变化、bootstrap失败或非预期DDL都停止发布。apply未提交时直接
保持旧库；apply已提交后验证失败则使用已验证dump恢复整个开发库并恢复旧backend，不手工补列。

部署新backend后创建全新conversation并发送“你是谁？”：

- Task从accepted进入completed；
- final TaskNode、Artifact、assistant message和`agent.final_output` Event存在；
- 没有`IntegrityError`或`execution_crash`；
- 标题为普通标题或空，不得包含`<ds_safety>`、`ds_safety>`或规范开头控制标签；
- 旧conversation和旧失败Task仍保持原值。

smoke失败只记录安全错误码/状态，不输出回答正文、凭据或provider request内容。

## 10. Checkpoint I：文档闭合与最终提交

- 把设计状态更新为`implemented_verified`；
- 把本计划更新为`complete`，记录红测、绿测、隔离演练、远端migration、镜像digest与真实smoke
  的实际证据；
- 更新`src/api/AGENTS.md`、`src/state/AGENTS.md`、`src/storage/AGENTS.md`、
  `scripts/AGENTS.md`、`tests/AGENTS.md`、`docs/AGENTS.md`和`CHANGELOG.md`中实际受影响的入口；
- 推送Git或镜像前再次确认没有DSN、dump、receipt、错误标题原始内容、用户消息正文或
  `docker_cmd.md`进入tracked diff；
- 复查工作树，保留无关用户修改。

建议最终账本提交信息：
`docs: close agent schema and title repair`。

## 11. 停止条件

出现任一情况立即停止，不扩大设计：

- 目标不是`main`开发库或writers无法确认停止；
- `pg_dump`、隔离restore或source/restore inventory不一致；
- 当前源库report SHA与apply前inventory不一致；
- 需要删除批准集合之外的表/列或修改业务行才能迁移；
- 新operator必须接触SQLite/Sidecar才能完成当前PostgreSQL修复；
- bootstrap fail-fast必须执行destructive SQL才能识别旧schema；
- 标题修复需要通用内容审核、第二次LLM调用或历史数据更新；
- 远端apply出现partial schema、row count或Agent digest漂移；
- 需要修改Frontend、Rust、Provider、配置、依赖或`prod`。

测试fixture、公开helper命名和文档索引的必要机械调整不算范围扩张，但必须在完成证据中列明。

## 12. 回滚

- 标题与bootstrap代码：回退对应Git检查点并重建backend；
- operator代码：回退operator检查点；不影响尚未执行的数据库；
- 迁移未提交：事务rollback后保持源库不变；
- 迁移已提交但验证失败：使用隔离验证过的custom dump恢复整个开发库，并恢复旧backend；
- 已完成迁移但仅新backend业务smoke失败：优先forward fix；若回退旧backend，接受它只能回到
  迁移前代码能力，不重新创建旧DAG字段；
- 不以手写`ADD COLUMN`、默认值兼容、旧Task重放或标题数据修正作为回滚。

License Requirement：复用现有Python、SQLAlchemy、psycopg、Docker、PostgreSQL 17 client和
unittest；无新增依赖或许可变化。
