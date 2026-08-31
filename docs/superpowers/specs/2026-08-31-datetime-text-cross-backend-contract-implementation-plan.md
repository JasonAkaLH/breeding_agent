# DateTimeText 跨后端时间合同修复实施计划

依据：`2026-08-31-datetime-text-cross-backend-contract-design.md`

设计基线：`main@e19a19f4`

状态：`planned`；业务代码尚未修改

目标分支：`main`

## 1. 完成声明与边界

本计划只完成四件事：

1. 收紧普通 `DateTimeText` 为 UTC-naive 写入/读取合同；
2. 新增内部 `AwareUTCDateTimeText`，并精确迁移已批准的 31 个安全/证据字段；
3. 修复 PostgreSQL owner mutation guard 已确认的 aware 普通字段写入；
4. 用真实 PostgreSQL submission admission → route materialization → Event 写入回归证明原
   503 根因已消失。

严格排除 PostgreSQL/SQLite schema 或数据修改、API/SSE、Frontend、通用 datetime helper、
比较逻辑重构、历史数据扫描/迁移、当前失败任务、镜像、部署和 `prod`。

执行顺序固定为“聚焦红测 → 最小生产修改 → 相关回归 → diff 审查 → green commit”。
测试 fixture 只在严格类型确实使现有错误 awareness 输入失败时修改，不提前批量重写。

## 2. Checkpoint A：红测锁定合同和原故障

### 2.1 时间类型红测

新增：

- `tests/storage/test_sqlalchemy_datetime_types.py`

使用 SQLAlchemy 自带 SQLite/PostgreSQL dialect 对象直接验证 TypeDecorator，不启动数据库。

红测必须覆盖：

1. 当前 `DateTimeText` PostgreSQL result 收到 aware UTC 时仍返回 aware；新断言要求 UTC-naive，
   因而修改前失败；
2. 普通类型接受 naive bind：PostgreSQL bind 结果为同一瞬间的 aware UTC，SQLite bind 结果为
   无偏移 ISO 文本；
3. 普通类型拒绝 aware bind；
4. 普通类型读取 SQLite 旧 aware ISO 文本时转换为 UTC-naive；
5. 新 aware 类型接受任意有效 aware offset，并将 bind/result 规范为 UTC；
6. 新 aware 类型拒绝 naive bind/result；
7. `None` 在两种类型中原样通过。

不增加非 datetime 输入、DST、任意字符串容错或其他未发生场景的测试。

### 2.2 字段归类红测

修改：

- `tests/storage/test_shared_sqlalchemy_declarations.py`

增加一个显式 15 Row / 31 字段映射，使用实际 SQLAlchemy column type 断言：

- 映射内字段的类型精确为 `AwareUTCDateTimeText`；
- 映射外 138 个时间字段的类型精确为 `DateTimeText`；
- 总数精确为 61 Row / 169 字段。

不依赖类名子串或文档文本进行断言。

### 2.3 PostgreSQL submission 红测

修改：

- `tests/storage/test_submission_admission_postgres_integration.py`

在现有独立 PostgreSQL 测试类中增加一个聚焦用例：

1. 使用现有 `_request()` 创建 UTC-naive Task 并执行 `admit_submission()`；
2. 使用同一 `PostgreSQLStorage` 新 session 读取 Task；
3. 最终断言 Task 时间为 naive、与 admission record 的 `created_at` 严格相等；
4. 用最小 `ApiRuntime` 实例调用真实 `materialize_route_decision()`；
5. 最终断言存在 `task.accepted` 与
   `mcp.rollout.route_assigned` 两条 Event；
6. teardown 精确删除本用例 Event，不遗留隔离数据库数据。

修改生产代码前先运行该最终形态测试；预期失败输出必须落在 Task awareness 不一致或
`submission_task_materialization_conflict`，作为红测证据，不在测试代码中保留旧行为断言。

Runtime 测试装配只提供该方法真实需要的 storage、event broker、audit reference signer 和空
rollout metric recorder，不启动完整服务或外部依赖。

### 2.4 红测命令

```bash
conda run -n multi_agent python -m unittest \
  tests.storage.test_sqlalchemy_datetime_types \
  tests.storage.test_shared_sqlalchemy_declarations
conda run -n multi_agent python -m unittest \
  tests.storage.test_submission_admission_postgres_integration.SubmissionAdmissionPostgresIntegrationTest.test_route_materialization_uses_naive_postgres_task_time
```

第二条需要由环境提供 `MAF_POSTGRES_SUBMISSION_ADMISSION_TEST_DSN` 或既有 fallback
`MAF_POSTGRES_TEST_DSN`。缺 DSN 可以记录 skip，但不能作为红测或最终验收证据。

Checkpoint A 不单独提交失败状态；红测证据确认后直接进入 Checkpoint B。

## 3. Checkpoint B：最小实现并恢复 green

### 3.1 共享类型

修改 `src/storage/sqlalchemy_base.py`：

1. 为 `DateTimeText` 引入 `timezone.utc`；
2. bind 时以 `value.utcoffset() is not None` 判断 aware，aware 直接 `ValueError`；
3. PostgreSQL bind 对合法 naive 值临时附加 UTC，SQLite 保持无偏移 `isoformat()`；
4. result 统一先取得 datetime；aware 值转 UTC 后移除 `tzinfo`，naive 值原样返回；
5. 新增 `AwareUTCDateTimeText`，复用同一物理 dialect 映射；bind/result 都要求有效 aware，
   并转换为 UTC；
6. 两个类型保持 `cache_ok = True`，不新增公共 helper、配置项或依赖。

### 3.2 字段声明

修改 `src/storage/sqlalchemy_models.py`：

1. 从共享 base 导入 `AwareUTCDateTimeText`；
2. 只把设计文档列出的 15 Row / 31 字段替换为新类型；
3. 其余 138 个字段保持 `DateTimeText`；
4. 不调整列名、nullable、default、index、constraint 或 Row 结构。

### 3.3 已确认普通 writer

修改 `src/storage/postgres/repositories.py`：

- 将 owner mutation guard 的 `updated_at=datetime.now(timezone.utc)` 改为仓库既有的
  `datetime.now(timezone.utc).replace(tzinfo=None)` 形式。

不改同文件 rollout raw SQL 路径；那些字段属于 approved aware 集合。

### 3.4 必要 fixture 修正

先运行聚焦测试，只修复实际违反新普通合同的 fixture。已知候选包括：

- `tests/storage/test_agent_storage_postgres_integration.py`
- `tests/storage/test_submission_preparation_receipt_sqlite.py`
- `tests/storage/test_submission_preparation_receipt_postgres_integration.py`
- `tests/storage/test_conversation_file_sheet_selection_postgres_integration.py`

规则：

- 普通 Task、Conversation、Agent、receipt、file 时间改为 naive；
- rollout、CP7、master-key、legacy migration 测试时间继续 aware；
- 同一测试同时覆盖两种域时，拆分为两个具名 clock，不放宽生产类型。

### 3.5 聚焦 green 门禁

```bash
conda run -n multi_agent python -m unittest \
  tests.storage.test_sqlalchemy_datetime_types \
  tests.storage.test_shared_sqlalchemy_declarations \
  tests.storage.test_submission_admission_sqlite \
  tests.api.test_submission_preparation_callbacks \
  tests.storage.test_submission_preparation_receipt_sqlite \
  tests.storage.test_mcp_rollout_ledger \
  tests.storage.test_mcp_shadow_audit_samples \
  tests.storage.test_mcp_cp7_safety_ledger \
  tests.storage.test_user_mcp_server_repository
conda run -n multi_agent python -m unittest \
  tests.storage.test_submission_admission_postgres_integration \
  tests.storage.test_submission_preparation_receipt_postgres_integration \
  tests.storage.test_agent_storage_postgres_integration \
  tests.storage.test_conversation_file_sheet_selection_postgres_integration
conda run -n multi_agent ruff check \
  src/storage/sqlalchemy_base.py \
  src/storage/sqlalchemy_models.py \
  src/storage/postgres/repositories.py \
  tests/storage/test_sqlalchemy_datetime_types.py \
  tests/storage/test_shared_sqlalchemy_declarations.py \
  tests/storage/test_submission_admission_postgres_integration.py
git diff --check
```

PostgreSQL 模块最终必须在隔离 DSN 环境零 skip。green 后创建代码检查点：

```text
fix(storage): normalize SQLAlchemy datetime awareness
```

## 4. Checkpoint C：相关全量验证与文档闭合

依次执行：

```bash
conda run -n multi_agent python -m compileall -q src tests
conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/lifecycle -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m unittest \
  tests.storage.test_postgres_runtime_schema_manifest \
  tests.storage.test_postgres_schema_reconciler
git diff --check
```

本次不运行 Frontend、Rust、镜像或外部 MCP/Skill smoke，因为生产 diff 不进入这些边界。

最终检查：

1. 生产 diff 只包含设计批准的三个文件；
2. 测试 diff 只包含新合同、原故障回归和实际失败 fixture；
3. PostgreSQL schema manifest 无变化；
4. 真实 submission 回归写入两条 Event，PostgreSQL 模块零 skip；
5. 无 API/SSE、Frontend、schema、migration、配置、依赖或当前失败任务修改；
6. `docker_cmd.md` 仍存在、被忽略且未被读取、跟踪或修改；
7. 更新设计/计划状态、`docs/AGENTS.md` 和 `CHANGELOG.md`，只记录实际执行结果与 skip。

文档闭合提交：

```text
docs: close datetime storage contract fix
```

## 5. 回滚

本次无 schema 或数据变更。若实现回归，先回退文档闭合提交，再回退单一代码检查点即可；
不执行数据库回滚、数据改写或历史任务清理。

License Requirement：复用现有 Python、SQLAlchemy、SQLite/PostgreSQL 和 unittest；无新增依赖
或许可变化。
