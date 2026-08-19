# MCP Dispatch 聚合恢复加固开发计划

## 状态

- 日期：2026-08-18
- 状态：**96% — Pass with recorded assumptions**；通过95%信心门，Blocking=0、Major=0
- 实施状态：**Phase 0～4仓库实现与本地SQLite cutover已完成**；待用户OCR人工smoke，且真实
  PostgreSQL validation DSN未提供，不能声明跨backend外部验收完成。
- 设计依据：
  `docs/superpowers/specs/2026-08-18-mcp-dispatch-aggregate-recovery-hardening-design.md`
- 设计基线提交：`4a0eb30`
- 目标分支：`main`
- 部署边界：只实施和验证本地开发/CI代码，不修改、构建或部署`prod`

## 复审记录

用户批准在95%信心门内自主循环修订。本计划完成四轮审阅：初稿可实施性审计、Phase依赖和
migration重排、锁序/retention/故障边界一致性、最终文件/命令/追踪门禁。

| 维度 | 置信度 | 最终结果 |
|---|---:|---|
| design目标与范围一致性 | 98% | 18项FR、8项NFR全部映射，不扩展Sidecar/prod/OCR桥接 |
| checkpoint依赖与可执行性 | 96% | red测试不提交，P0持久化映射闭合，Coordinator和apply顺序无倒置 |
| schema、迁移与回滚 | 95% | SQLite备份/retry和PostgreSQL约束接管明确；真实PG仍是外部完成证据 |
| security、privacy与retention | 96% | 三种容量域、AAD、secure read、payload/result/candidate生命周期分离 |
| concurrency与崩溃恢复 | 96% | 固定锁序、claim/cancel线性化和17个注入hook逐项闭合 |
| testability与交付门禁 | 97% | 每个green checkpoint有定向module，最终全量后端/前端/Rust相关门禁明确 |

最终文档置信度为96%。代码与migration的实际完成证据、全量回归例外和外部缺口见第16节；
本地完成仍不代表真实PostgreSQL、OCR人工smoke或生产部署完成。

## 1. 计划选择

评估过三种实施切分：

1. **按Storage、Integration、API横向分层。** 单层改动集中，但schema、Repository和consumer
   会在多个提交之间长期不兼容，无法安全验证崩溃边界。
2. **五个Phase内拆测试先行的垂直checkpoint。** 每个checkpoint先在本地运行能证明缺口的
   失败测试，再完成最小实现和定向回归；只提交green checkpoint。Phase 1至Phase 3连续
   实施，期间不重启本地服务。
3. **一次性大提交。** 最快形成表面闭环，但17个持久化边界无法定位回归，也难以回滚。

采用方案2。每个checkpoint可独立代码审阅和做源码级Git revert；一旦执行P3.5非加法schema
迁移，数据回滚只能遵守第13节的受控规则，不能把Git revert冒充schema rollback。只有
Phase 1至Phase 3全部完成、迁移preflight通过后，才允许运行新schema的backend。

## 2. 交付目标与非目标

本计划交付以下结果：

1. 修复CP7 terminal candidate的UTC-aware整秒clock，恢复当前Task failed但MCP聚合仍active的
   不一致任务，且不重放OCR Tool。
2. 把普通多Call、Tool approval、MRTR和remote Task统一到SQL aggregate writer、dispatch
   claim、resume cursor和统一finalizer。
3. 审批参数使用32 MiB独立AES-GCM payload authority；resume envelope继续保持64 KiB且不含
   Tool实际I/O。
4. normalized Tool result在candidate前进入64 MiB上限的durable content-addressed store，
   candidate/receipt v2绑定内容SHA和字节数，reader保留v1兼容。
5. startup按固定证据优先级恢复，17个崩溃边界只能得到零/一次网络调用、可信等待/终态或
   unknown/no-replay。
6. SQLite/PostgreSQL完成同义schema、锁序、CAS、迁移分类和回滚测试；前端审批/SSE合同保持
   兼容。

本计划不实施Sidecar enforce authority、附件到MCP Tool的新传输协议、OCR参数桥接、旧失败
任务自动重跑、全局Artifact不可变改造、生产部署或CP7-B退役。

## 3. 已核实基线与实施含义

| 当前基线 | 证据位置 | 实施含义 |
|---|---|---|
| Outbox仅有`pending|claimed|completed|aborted`，claim API按一次性resume设计 | `src/core/models.py`、`src/core/contracts.py` | 必须一次完成新Enum、claim shape、resume cursor和约束迁移，不能靠字符串旁路 |
| SQLite outbox CHECK需要重建表；CP7字段当前只做nullable additive migration | `src/storage/sqlite/models.py`、`bootstrap.py` | 旧库必须离线分类、备份、重建并核对行数/索引；普通startup不得静默改写 |
| PostgreSQL CP7写入集中在`_run_cp7_authority_sync`，fresh schema版本为v5 | `src/storage/postgres/repositories.py`、`src/state/postgres/runtime_schema.py` | 新aggregate方法全部进入扩展锁序；schema manifest升级并测试constraint replacement |
| Coordinator目前分步reserve/mark/finish Call，approval重新扫描Interrupt | `src/integrations/mcp/dispatch_coordinator.py` | 用pending action和聚合Repository替换分步mutation；恢复不重新运行Selector选择已批准action |
| Gateway创建`durable=False` result sink，`close_task`会清理未promote结果 | `src/integrations/mcp/gateway.py`、`temporary_results.py` | authority路径必须使用durable sink，commit前验证manifest/正文；cleanup改看SQL lifecycle |
| 当前durable result发布会`os.replace`同ref，manifest缺owner/node/call | `src/integrations/mcp/temporary_results.py` | 改为no-clobber exact-idempotent发布，manifest补齐authority身份并拒绝漂移 |
| `MCPRecoveryCipher`私有JSON上限64 KiB且AAD要求Call ref | `src/integrations/mcp/credentials.py` | 新pending payload cipher独立文件格式；MRTR sealed state只保存payload引用，不复制arguments |
| 2026 Adapter先保存MRTR state/remote binding，Coordinator再写Node/outbox | `src/integrations/mcp/adapter_2026.py`、`dispatch_coordinator.py` | 明确prepublication/adoption；startup先采用完整evidence再做unknown收敛 |
| remote worker已有binding claim和remote-task continuation outbox | `src/integrations/mcp/recovery_worker.py` | 保留独立remote claim；terminal通过aggregate writer回写dispatch resume cursor |
| startup先跑CP7 reconcile，再恢复普通MCP Call；列表存在10,000默认上限 | `src/api/runtime.py` | 拆出分页aggregate reconciler并固定candidate/remote/MRTR/action/unknown顺序 |
| `_await_existing_execution`无限等待，运行handle以task ID清理 | `src/api/runtime.py` | 改为generation/token单航班和30秒等待，不允许第二执行并发启动 |

实施责任边界：

| 组件/参与者 | 本计划职责 | 硬依赖 |
|---|---|---|
| Core/Storage | closed model、StoragePort、SQLite/PostgreSQL schema、aggregate CAS和migration | 现有owner guard、Task/Node/Interrupt/Event表 |
| MCP Integration | payload/result/candidate文件authority、Coordinator、Adapter和remote worker | MCP recovery domain key、CP7 candidate store、Gateway |
| API/Orchestration | approval/MRTR Answer入口、startup reconciler装配、执行单航班 | durable Message/Attachment/Artifact和completion policy |
| Frontend | 只做既有approval event/DTO/SSE兼容回归 | 当前MCPApprovalDialog、task event reducer |
| Operator/验证环境 | 停止旧writer、运行report/apply、提供disposable PostgreSQL和本地smoke | canonical state config、非app operator DSN |
| 最终用户 | 只在代码/迁移门禁通过后验证新OCR Task | 不复活或重放旧失败Task |

外部依赖不新增第三方包。缺少真实PostgreSQL、文件权限控制或可产生MRTR/remote结果的受控
Server时，只能记录相应验证缺口，不能降低自动fixture和no-replay门禁。

## 4. 全局执行规则

每个checkpoint都遵守以下顺序：

1. 先在本地写最小失败测试，读取失败输出并确认失败原因与本checkpoint一致；红灯只作为
   本地TDD证据，不单独提交到`main`。
2. 只实现使该测试通过的生产变更；不顺手重构无关MCP、Storage或Lifecycle代码。
3. 运行checkpoint定向测试、相邻回归、`python -m compileall`或前端typecheck。
4. 只有本checkpoint测试和相邻回归转绿后，才审阅`git diff`和`git diff --check`、暂存精确
   文件并提交；任何建议checkpoint都必须是green checkpoint。
5. 若变更模块职责、入口、测试入口或约束，同步对应`AGENTS.md`和`CHANGELOG.md`。

工作区已有用户维护的`.omx/plans`删除和根`AGENTS.md`修改，实施期间不得恢复、覆盖、暂存或
提交这些变更。不得读取、移动、删除、跟踪或输出`docker_cmd.md`；任何工作树操作都不得使用
`git stash --all`、`git reset --hard`或会影响未跟踪本地文件的命令。

Phase 0可以形成代码checkpoint，但本次选择不单独启动它。Phase 1至Phase 3之间禁止启动
backend、执行真实MCP调用或让旧/新writer混跑；只运行隔离单测和fresh数据库集成测试。

## 5. 依赖顺序与里程碑

```text
Phase 0：clock + durable result + 当前故障止血测试
  -> Phase 1：一次性schema基础 + aggregate writer + ordinary多Call
    -> Phase 2：pending action + approval原子恢复
      -> Phase 3：MRTR/remote adoption + startup恢复 + 受控cutover
        -> 首次允许重启本地服务
          -> Phase 4：预算/单航班 + candidate/result GC + observability + 全故障注入
```

Phase 1的schema checkpoint一次性声明后续Phase需要的表和nullable字段，避免对同一非additive
outbox迁移多次重建；Phase 2至Phase 4只启用相应行为，不再改变既定outbox状态合同。

| 阶段结束 | 允许创建源码checkpoint | 允许启动backend | 允许操作现有开发数据库 |
|---|---|---|---|
| Phase 0 | 是，必须green | 否 | 否 |
| Phase 1 | 是，必须green | 否 | 只读`--report`，不得apply |
| Phase 2 | 是，必须green | 否 | 只读`--report`，不得apply |
| Phase 3 P3.1-P3.4 | 是，必须green | 否 | 只读`--report`，不得apply |
| Phase 3 P3.5 | 是 | 仅report/apply/全回归成功后 | 停旧writer后受控apply |
| Phase 4 | 是 | 是 | 新schema下正常运行及GC验证 |

## 6. Phase 0：P0止血与authority结果基础

### P0.1 固定当前故障和恢复基线

先新增失败测试，不改生产逻辑。P0.1只是P0.2至P0.4的本地red基线，不形成独立Git提交；
各测试随修复它的green checkpoint提交：

- 在`tests/integrations/mcp/test_cp7_terminal_results.py`覆盖naive UTC、非UTC aware、非整秒及
  exact v1 candidate读取。
- 在`tests/integrations/mcp/test_user_mcp_temporary_results.py`证明普通Gateway result在
  `close_task`和新store实例后丢失。
- 在`tests/api/test_user_mcp_recovery_startup.py`构造Task=`failed`、Node=`running`、Call
  active+`may_have_dispatched`、intent dispatched、outbox claimed、无candidate/receipt，断言
  当前startup不会完整收敛且网络spy为0。
- 在`tests/integrations/mcp/test_dispatch_coordinator.py`建立post-admission candidate seal失败
  后Node/Task/Call残留的组合回归。

验证命令：

```text
conda run -n multi_agent python -m unittest \
  tests.integrations.mcp.test_cp7_terminal_results \
  tests.integrations.mcp.test_user_mcp_temporary_results \
  tests.integrations.mcp.test_dispatch_coordinator \
  tests.api.test_user_mcp_recovery_startup
```

失败测试必须逐项记录预期failure message，避免把已有无关失败当作红灯证据。

P0.1无独立checkpoint；red/green输出摘要记录在P0.2至P0.4的实施日志中。

### P0.2 接入统一terminal clock和candidate/receipt v2双读

主要文件：

- `src/core/models.py`
- `src/integrations/mcp/cp7_terminal_results.py`
- `src/integrations/mcp/dispatch_coordinator.py`
- `src/api/runtime.py`
- `src/integrations/mcp/recovery_worker.py`
- `src/storage/sqlite/models.py`
- `src/storage/sqlite/repositories.py`
- `src/storage/sqlite/bootstrap.py`
- `src/storage/postgres/repositories.py`
- `src/state/postgres/runtime_schema.py`
- `tests/integrations/mcp/test_cp7_terminal_results.py`
- `tests/integrations/mcp/test_cp7_artifacts.py`
- `tests/storage/test_cp7_schema_contract.py`
- `tests/storage/test_user_mcp_postgres_schema_contract.py`

实施内容：

1. 新增单一`terminal_now_fn`默认实现，返回aware UTC整秒；Coordinator普通/失败writer和remote
   terminal sealer都注入该clock，删除`_utcnow_naive().replace(microsecond=0)`式调用。
2. 扩展`MCPValidatedTerminalResultCandidate`和`MCPTerminalResultReceipt`的v2 result content
   SHA、size、store kind字段；failed/cancelled必须为null。
3. `cp7_terminal_results.py`只让new writer写exact v2，reader按无歧义schema分流v1/v2；unknown
   schema、额外字段和同ID不同内容fail closed。
4. SQLite/PostgreSQL receipt row、row/model mapper和Repository commit同步增加nullable content
   SHA/size/store kind；v2 completed写入时三项必填，legacy v1 receipt允许null。fresh schema和
   SQLite additive column测试必须在本checkpoint转绿，不能把持久化映射推迟到Phase 1。
5. PostgreSQL fresh schema版本在首次持久化合同变化时从v5升为v6；Phase 1继续补齐同一v6
   最终合同。由于Phase 3前禁止启动或apply，不会有“部分v6”数据库进入运行环境。
6. 保留legacy v1 candidate/receipt模型兼容，不能重写既有candidate文件。

阶段门禁：三条writer产生相同UTC格式；v1 fixture可读、v2 round-trip可比、损坏schema拒绝。

建议checkpoint：`fix(mcp): use aware terminal clock and v2 candidates`

### P0.3 把authority Tool result改为durable、bounded、no-clobber

主要文件：

- `src/integrations/mcp/temporary_results.py`
- `src/integrations/mcp/gateway.py`
- `src/integrations/mcp/cp7_terminal_results.py`
- 新增`tests/integrations/mcp/test_mcp_durable_results.py`
- 更新Gateway、streaming response和temporary result测试

实施内容：

1. authority Gateway统一`create_sink(..., durable=True)`，manifest增加owner/Task/Node/Call/scope、
   content SHA、size和store kind；shadow/non-authority路径保持当前临时语义。
2. 数据和manifest使用no-clobber、`0600`、file+directory fsync、secure read；相同ref只允许
   exact-idempotent，禁止`os.replace`覆盖。
3. sink流式计数64 MiB；首个超限字节abort并返回closed`mcp_result_too_large`，不能把截断正文
   交给Selector。
4. `close_task`不删除被active Call/candidate/receipt引用的durable结果。P0至P3禁止删除任何
   durable result；既有janitor只继续处理non-durable临时文件。orphan、24小时宽限和
   data+manifest删除统一等P4.3 lifecycle authority完成后启用。
5. candidate封存前逐字节验证manifest；v2 completed candidate绑定content SHA/size/store kind。

阶段门禁：结果跨`close_task`和进程重建可读；64 MiB成功、首个超限字节失败；link/mode/owner/
inode/SHA漂移拒绝；磁盘不足在当前Call admission前不创建新Call。

建议checkpoint：`feat(mcp): persist authoritative tool results durably`

### P0.4 最小post-admission和终态Task收敛

主要文件：

- `src/api/runtime.py`
- `src/integrations/mcp/dispatch_coordinator.py`
- `src/storage/sqlite/repositories.py`
- `src/storage/postgres/repositories.py`
- `tests/api/test_user_mcp_recovery_startup.py`
- `tests/storage/test_user_mcp_terminal_projection.py`

实施内容：

1. candidate seal/commit后的预期错误不再只失败Task；调用窄版CP7收敛使Node/Call/intent/outbox
   与Task一致。
2. startup不再因Task已终态就跳过active MCP authority；当前坏任务形态走unknown/no-replay，
   保留已有`task.failed`事件并新增audit-only reconciliation事件。
3. Phase 0 startup在ordinary unknown前先识别candidate/receipt、remote binding、MRTR sealed
   state和open approval，避免误杀等待中的任务；不在此checkpoint实现完整续作。
4. 所有恢复使用网络spy断言0次调用。

Phase 0门禁：P0.1红灯全部转绿；当前坏任务安全收敛；不启动本地backend。

建议checkpoint：`fix(mcp): converge post-admission failures safely`

## 7. Phase 1：schema基础与ordinary aggregate writer

### P1.1 先锁定核心模型、StoragePort和schema合同

新增/修改：

- `src/core/models.py`：outbox新状态、resume reason、dispatch completion mode、pending action、
  candidate lifecycle、durable result lifecycle、migration state、Call continuation字段和累计
  预算字段。
- `src/core/contracts.py`：新增aggregate方法；保留legacy读取接口，标记分步mutation待移除。
- `src/storage/sqlite/models.py`：新表、索引、CHECK和唯一约束。
- `src/state/postgres/runtime_schema.py`：补齐P0.2已开启的v6最终schema并生成等价DDL/索引。
- `src/state/postgres/schema_reconciler.py`：识别需离线替换的outbox CHECK，不允许app启动时滚动混跑。
- `tests/storage/test_cp7_schema_contract.py`
- `tests/storage/test_user_mcp_postgres_schema_contract.py`
- `tests/storage/test_postgres_runtime_schema_manifest.py`
- `tests/storage/test_sqlite_bootstrap.py`

先写schema失败测试，固定：

- outbox状态`pending|claimed|active|waiting_approval|waiting_input|remote_pending|completed|aborted`；
- `claimed|active`必须有claim三元组，其他状态必须为null；
- resume cursor五种reason及receipt/Answer nullable组合；
- pending action、candidate lifecycle和result lifecycle闭合字段/状态；
- v2 receipt nullable迁移字段与new writer必填规则；
- 索引支持status+updated_at+stable ID的keyset pagination；
- append-only receipt仍禁止UPDATE/DELETE，两个lifecycle表允许受控CAS。

建议checkpoint：`test(storage): define MCP aggregate schema contract`

### P1.2 实现只读cutover分类器、fresh schema和迁移状态机

主要文件：

- `src/storage/sqlite/bootstrap.py`
- `src/storage/postgres/bootstrap.py`
- `src/state/postgres/schema_reconciler.py`
- 新增`scripts/migrate_mcp_dispatch_aggregate.py`
- 新增`tests/scripts/test_migrate_mcp_dispatch_aggregate.py`
- SQLite/PostgreSQL schema/migration测试

本checkpoint只交付可独立验证的只读report、fresh schema、SQLite backup helper和migration
state machine，不对含业务行的旧库开放apply。复杂旧行收敛依赖P1.3至P3.4的aggregate writer，
最终`--apply`入口只在P3.5接通。工具使用`build_state_platform_runtime_config`选择backend：
SQLite显式`--database-path`，PostgreSQL只允许`--dsn-env`读取环境变量，禁止raw DSN参数。

P1.2可用命令：

```text
--report   只读分类，输出closed计数和blocker reason，不输出业务ID/payload
```

新增`mcp_dispatch_aggregate_migration`状态行，closed状态为
`planned|backed_up|applying|applied|failed`，只记录backend/schema version、canonical report
SHA、backup basename/SHA、状态/revision/timestamp，不记录DSN、绝对路径或业务ID。

SQLite migration helper合同：

1. file-backed数据库的备份basename固定为
   `<database-name>.pre-mcp-aggregate-v1.<report-sha前12位>.bak`，与源库同目录，使用SQLite
   backup API、`0600`、O_EXCL/no-clobber、file+directory fsync；in-memory测试显式跳过备份。
2. 同basename已存在时，只能在migration row为`backed_up|applying`、report SHA一致、mode/owner/
   SQLite header/`PRAGMA integrity_check`和文件SHA全部一致时采用；否则fail closed。
3. 重建函数在独占事务复制并分类旧行，核对行数、主键、索引、foreign key和
   `PRAGMA integrity_check`；P1.2只对empty/fresh fixture执行，业务行fixture只生成plan。
4. 普通`bootstrap_sqlite_database()`检测到legacy outbox且存在业务行时返回
   `mcp_dispatch_aggregate_migration_required`，不得静默重建。

PostgreSQL P1.2只验证fresh v6 schema、migration plan和`NOT VALID` DDL生成；最终apply在
operator环境默认只读取`MAF_POSTGRES_STATE_OPERATOR_DSN`，candidate测试通过显式
`--dsn-env CP7_POSTGRES_VALIDATION_DSN`使用disposable validation库。两者都使用advisory lock、
3秒lock timeout和30秒statement timeout；app runtime DSN不得拥有或调用schema mutation。

建议checkpoint：`feat(storage): plan MCP aggregate cutover safely`

### P1.3 实现dispatch claim、resume cursor和ordinary admission

主要文件：

- `src/core/contracts.py`
- `src/storage/sqlite/repositories.py`
- `src/storage/postgres/repositories.py`
- 新增`tests/storage/test_mcp_dispatch_aggregate_repository.py`
- 扩展`tests/storage/test_user_mcp_cp7_postgres_integration.py`

新增Repository命令：

```text
claim_mcp_dispatch
renew_mcp_dispatch_claim
release_or_recover_mcp_dispatch_claim
admit_approved_mcp_action
```

首轮只覆盖无需用户等待的ordinary action。所有命令使用expected revision和30秒lease；renew
间隔上限10秒。admission保留CP7 candidate/epoch、Task enforce、shadow off、八detector和
Gateway authorization门禁，并在同一事务创建Call、设置branch active、消费action、转换
intent/outbox。无有效claim不得创建Call或释放network gate。

P1.3同时为SQLite/PostgreSQL repository注入closed `PendingActionPayloadReader` protocol；测试用
fake reader返回与action row完全匹配的validated snapshot。真实binary/AES-GCM reader到P2.1才
实现，runtime装配和Coordinator callsite到P2.3才启用。reader缺失、返回正文与SHA/AAD/identity
不匹配时admission必须失败，不能退回caller-authored“已验证”布尔值或plaintext arguments。

SQLite使用单一`BEGIN IMMEDIATE`；PostgreSQL扩展`_run_cp7_authority_sync`的锁序。双claim、
双admission、Server删除与admission竞态必须单赢家。

所有aggregate命令和PostgreSQL测试锁定同一顺序：

```text
owner guard -> target Server -> intent -> dispatch outbox -> pending action
-> branch -> Call(s) -> terminal candidate -> terminal receipt/projection
-> remote binding -> remote task outbox -> dispatch resume cursor -> Task -> Node
-> Interrupt -> Answer -> Grant
```

pending payload和durable result不是SQL锁：调用方先用O_NOFOLLOW持有validated只读descriptor，
Repository在对应action/Call锁后重验descriptor stat、文件SHA、manifest/AAD和SQL identity，再提交
状态；不能在持锁事务内重复读取32/64 MiB正文，也不能只信任事务外的布尔结果。

建议checkpoint：`feat(mcp): add aggregate dispatch admission`

### P1.4 实现ordinary terminal commit、finalizer和unknown convergence

新增Repository命令：

```text
commit_mcp_call_terminal
finalize_mcp_dispatch
converge_mcp_unknown_no_replay
cancel_mcp_dispatch
```

实施内容：

1. terminal commit用valid dispatch claim验证v1/v2 candidate和v2 durable result，原子写receipt、
   Call、branch、outbox last receipt/ordinary cursor；ordinary completed保持outbox active。
2. failed/cancelled和FINISH/STOP/deny/step limit统一进入finalizer，由历史Call选择`*_no_call`或
   `*_after_call`，原子终结intent/outbox/branch/Node/Task。
3. unknown convergence只有在`may_have_dispatched`且无terminal/MRTR/remote evidence时成立；
   late candidate继续复用CP7 late-result projection且不恢复Task。
4. cancel和admission使用同一owner guard；测试两个锁赢家及Gateway transport spy。
5. 相同candidate重试幂等；同Call不同candidate、同ref不同manifest阻断Ready。

建议checkpoint：`feat(mcp): commit and finalize dispatch aggregates`

### P1.5 实现Selector恢复投影并准备Coordinator切换

主要文件：

- `src/integrations/mcp/dispatch_coordinator.py`
- `src/capabilities/mcp_dispatch/models.py`
- `src/capabilities/mcp_dispatch/selector.py`
- `src/orchestration/service.py`
- `src/api/runtime.py`
- Coordinator、selector、resume v2和startup测试

实施内容：

1. 新建单一Selector context builder：root Message/binding、attachment/dependency投影来自v2设计，
   completed refs按Call sequence，fingerprint/预算/Server来自SQL；初次与恢复逐字段一致。
2. terminal receipt提交响应丢失后，builder可以从receipt/cursor生成逐字段相同context，不重发
   已完成Tool。
3. 为Coordinator增加aggregate adapter接口和fixture，但P1不切换production callsite，也不引入
   临时plaintext/in-memory action authority。正式切换必须等P2.1/P2.2 payload+action完整后在
   P2.3一次完成。

Phase 1门禁：Repository层模拟两次ordinary Call时outbox只在finalizer终态，Selector恢复投影
逐字段一致，SQLite/PostgreSQL并发测试通过；production Coordinator仍走旧路径且不启动backend。

建议checkpoint：`feat(mcp): build durable selector recovery context`

## 8. Phase 2：持久化审批action

### P2.1 实现pending-action payload cipher和文件authority

新增：

- `src/integrations/mcp/pending_action_payloads.py`
- `tests/integrations/mcp/test_pending_action_payloads.py`

实施exact binary v1、32 MiB canonical JSON、独立AAD前缀、96-bit nonce、`0600/0700`、
O_EXCL/no-clobber、secure read和file+directory fsync。process-local crypto gate容量1；等待期间
续租dispatch claim并响应取消。测试覆盖magic/version/length/trailing bytes、AAD每一字段漂移、
nonce长度、密文篡改、symlink/hardlink/mode/owner/inode、ENOSPC/EDQUOT、24小时orphan和
单次RSS增量不超过128 MiB。

payload只在没有action row的事务失败文件超过24小时，或action consumed且对应ordinary/MRTR
Call已有可信terminal/unknown projection后删除。`input_required`不是终态；MRTR continuation完成
前必须保留原payload。startup和janitor都从action/Call证据判断，不能仅按文件mtime删除。

不得修改现有`MCPRecoveryCipher.MAX_TASK_PRIVATE_JSON_BYTES`或把审批前action伪装成
`MCPSealedState`。

建议checkpoint：`feat(mcp): add encrypted pending action payloads`

### P2.2 实现pending action和approval原子事务

新增Repository命令：

```text
suspend_mcp_for_approval
accept_mcp_tool_approval
```

事务必须原子维护pending action、唯一open Interrupt、Answer、Node、outbox cursor和
`always_allow` Grant。deny在同一事务按历史Call走stopped finalizer；allow设置approved并把
outbox转pending。双击Answer、Answer与cancel、Answer与Server/schema/security漂移都单赢家。

`src/api/runtime.py`在识别`mcp_tool_approval_required`时直接调用aggregate API，不能先走
`InterruptService.record_answer`。普通非MCP Interrupt保持现有路径。

建议checkpoint：`feat(mcp): commit tool approvals atomically`

### P2.3 切换Coordinator精确action恢复和frontend兼容

主要文件：

- `src/integrations/mcp/dispatch_coordinator.py`
- `src/api/runtime.py`
- `tests/api/test_user_mcp_grants_and_call_control.py`
- `tests/integrations/mcp/test_dispatch_coordinator.py`
- `frontend/src/domain/taskEvents.test.ts`
- `frontend/src/App.test.tsx`

实施内容：

1. Selector action先封存payload并写pending action；有效Grant可以同一aggregate事务直接approve+
   admit，不能绕过action authority。
2. approval resume通过Interrupt ID反查action，secure-read原arguments，复验版本后admit；不再
   运行Selector重新选择已批准action。
3. 保持事件名、DTO、required_fields、`safe_call_ref`兼容；opaque safe ref不作为authority。
4. approval成功后的SSE重订阅继续复用`bb1cc55`行为；连续不同Tool分别审批，同action不重复。
5. 在同一checkpoint把Coordinator所有ordinary退出分支切换到aggregate Repository；不得先切换
   再补payload。完成后用下表的旁路writer清单逐项`rg`生产调用方，删除StoragePort公共旁路或
   保留只接受legacy v1终态恢复的封闭wrapper。

Phase 2门禁：两次连续ordinary Call、allow_once/always_allow/deny、双Answer、重启等待、版本
漂移和连续OCR Tool审批测试通过；旧分步writer无新状态生产调用方；不启动backend。

建议checkpoint：`refactor(mcp): resume exact approved actions`

### Phase 2旁路writer清理表

| 现有公共mutation | 最终处置 |
|---|---|
| `save_mcp_branch_record` | 初始/终态branch写入折叠到aggregate；StoragePort只保留read，删除生产直写 |
| `reserve_mcp_call`、`mark_mcp_call_may_have_dispatched`、`finish_mcp_call` | 从StoragePort和生产调用方删除，逻辑只存在于admission/terminal aggregate内部helper |
| `claim_mcp_dispatch_resume_outbox`、`reclaim_mcp_dispatch_resume_outbox`、`abort_mcp_dispatch_resume_outbox` | 由claim/recover/finalizer新命令取代；旧名不得接受新outbox状态 |
| `admit_mcp_tool_call` | 由`admit_approved_mcp_action|admit_mrtr_continuation`取代 |
| `finalize_mcp_dispatch_no_call`、`finalize_mcp_dispatch_intent` | 由统一`finalize_mcp_dispatch`取代，历史Call决定completion mode |
| `commit_authoritative_mcp_terminal_result` | 保留封闭compat wrapper只接收legacy v1 candidate并委托`commit_mcp_call_terminal`；不得沿用旧的单Call完成dispatch语义 |
| `publish_mcp_remote_task_binding` | 分为prepublish低层写入和aggregate adoption；只有后者可改变Call/branch/outbox/Node |
| `update_mcp_remote_task_binding_status` | 保留非终态poll状态更新；terminal状态必须委托aggregate terminal commit |

清理证明必须同时搜索接口声明、SQLite/PostgreSQL实现和所有`src/`调用方；测试fixture可以通过
新aggregate helper构造状态，不得为了旧测试保留生产旁路。

## 9. Phase 3：MRTR、remote Task与startup闭环

### P3.1 改造MRTR prepublication/adoption和Answer事务

主要文件：

- `src/integrations/mcp/credentials.py`
- `src/integrations/mcp/adapter_2026.py`
- `src/integrations/mcp/dispatch_coordinator.py`
- 2026 adapter、credentials、Coordinator、recovery测试

实施内容：

1. MRTR sealed evidence只保存request state、validated input requests、Tool、action/payload ref和
   Call身份，保持64 KiB上限，不复制arguments正文。
2. Adapter prepublish完整evidence后，`suspend_mcp_for_input`aggregate事务采用evidence、创建唯一
   Interrupt、Call=`input_required`、branch清理、Node/outbox waiting并释放claim。
3. 新增`accept_mcp_mrtr_answer`，原子提交Answer、Node和`mrtr_answer` cursor；generic Interrupt
   路径不得分步写相同聚合。
4. 崩溃在prepublication/adoption之间时startup只采用完整匹配evidence；不完整或漂移进入
   unknown/no-replay。

建议checkpoint：`feat(mcp): adopt MRTR evidence atomically`

### P3.2 实现MRTR continuation专用admission

新增`admit_mrtr_continuation`：验证原Call、accepted Answer、sealed evidence、原approval/
Grant、Server/schema/security、claim和预算，创建`continuation_of_call_ref`新Call并释放network
gate。continuation不创建新pending action、不重复审批；arguments只从原payload读取。sealed
state在可信terminal或unknown projection后删除。

测试覆盖Answer后崩溃、admission响应丢失、版本漂移、claim丢失、多轮MRTR、取消和零重放。

建议checkpoint：`feat(mcp): admit MRTR continuations safely`

### P3.3 统一remote binding adoption和terminal提交

主要文件：

- `src/integrations/mcp/credentials.py`
- `src/integrations/mcp/adapter_2026.py`
- `src/integrations/mcp/recovery_worker.py`
- `src/storage/sqlite/repositories.py`
- `src/storage/postgres/repositories.py`
- remote task worker/2025/2026/runtime测试

实施内容：

1. CreateTask响应先保存`published_at=NULL`加密binding；`publish_mcp_remote_task`aggregate事务
   采用binding并原子设置Call/branch/outbox/Node remote waiting状态。
2. remote worker只使用现有binding claim poll，不重发原`tools/call`；remote input继续由remote
   outbox+`tasks/update`处理，dispatch outbox保持remote_pending。
3. remote terminal先写durable result和candidate，再以binding claim调用同一terminal commit；
   completed写`remote_terminal`cursor回pending，failed/cancelled直接finalize。
4. prepublication/adoption、terminal/cursor和input/cancel每个边界做响应丢失测试。

建议checkpoint：`refactor(mcp): commit remote tasks through aggregate writer`

### P3.4 抽出固定startup aggregate reconciler

新增`src/integrations/mcp/aggregate_recovery.py`，`ApiRuntime.start()`只装配并调用。固定流程：

1. 修复candidate/result GC marker；
2. strict enumerate active candidate；
3. candidate/receipt；
4. remote binding；
5. MRTR evidence/Answer；
6. pending/approved action；
7. available v1/v2 envelope；
8. expired active claim；
9. unknown/no-replay；
10. aggregate invariants后Ready。

所有列表用status+updated_at+stable ID keyset pagination，每批最多1000；startup不访问远端。
新增`tests/api/test_user_mcp_aggregate_recovery_startup.py`覆盖Task终态、取消、open Interrupt、
accepted Answer、legacy v1、损坏v2、remote/MRTR和current bad task。

建议checkpoint：`refactor(mcp): centralize aggregate startup recovery`

### P3.5 切断旁路writer并执行首次本地cutover验证

代码门禁：

- `rg`证明Coordinator、API approval、remote worker和startup不再直接拼接旧Call/outbox/Node/
  Task mutation。
- SQLite/PostgreSQL aggregate方法锁序测试和真实PostgreSQL rollback/CAS测试通过。
- schema report在未停旧writer时拒绝apply；旧binary读取新Enum的测试明确失败。

本checkpoint才为迁移工具开放：

```text
--apply --expected-report-sha <64位SHA>
```

apply重新读取并比较report，任何行数/分类/schema/Server或authority证据漂移都拒绝。SQLite在
同一独占事务创建新表、按classification映射旧outbox、切换表后调用已完成的aggregate
reconciliation处理Interrupt/MRTR/remote/receipt/终态Task，再核对整体authority。PostgreSQL在
同一事务执行add column/table → 添加新`NOT VALID`约束 → 删除旧CHECK → 分类/aggregate收敛 →
`VALIDATE CONSTRAINT`；事务失败会恢复旧CHECK和全部旧行。新约束验证完成后才提交，不存在
“无有效CHECK”可被app观察的窗口。退出码固定：0=`reported|applied|already_applied`，
2=`usage_or_precondition_failed`，3=`authority_conflict_or_corruption`。stdout/stderr只输出closed
status、计数、reason和report SHA。

首次允许重启前：

1. 停止旧backend writer，不读取或执行`docker_cmd.md`。
2. 运行migration `--report`；保存只含closed计数/reason的本地报告。
3. 处理所有blocker后用该report SHA运行`--apply --expected-report-sha`；验证SQLite backup
   mode/完整性/migration row或PostgreSQL constraint/migration row状态。
4. 确认没有旧backend实例，再启动新版本一次；startup完成全部本地reconciliation后才Ready。
5. 验证旧失败OCR任务没有新网络调用；业务重试只创建新Task。

Phase 3门禁：Phase 1至3定向回归全绿、migration dry-run/apply/retry全绿、startup网络spy为0，
才允许进入本地人工测试。

建议checkpoint：`feat(mcp): complete aggregate recovery cutover`

## 10. Phase 4：长期加固与完整验收

### P4.1 持久预算和执行单航班

- branch Call预算20次；MRTR continuation计入网络Call，remote poll/update不计。
- Task/Node持久Selector step上限64、approval round上限20；invalidated approval计入round。
- `_schedule_execution`改为generation/token CAS；旧handle只能删除自己的generation。
- `_await_existing_execution`最多30秒，超时不并发启动第二执行，由claim/startup接管。

建议checkpoint：`fix(mcp): bound dispatch work across restarts`

### P4.2 Candidate active/archive lifecycle

新增`src/integrations/mcp/cp7_terminal_lifecycle.py`和定向测试：receipt同事务创建retained marker；
active/archive三文件精确移动、fsync、30天保留、deleting marker恢复；8,000告警、10,000 active
硬上限；archive每批1000 keyset，不进入Ready全量扫描。append-only receipt不修改。启用janitor
前按receipt keyset为P0至P3已提交candidate insert-or-compare retained marker；缺文件或binding
漂移按Ready策略处理，未完成backfill时归档/删除保持disabled。

建议checkpoint：`feat(mcp): archive consumed terminal candidates safely`

### P4.3 Durable result lifecycle和GC

扩展temporary result store与Repository：terminal commit insert-or-compare retained row；finalizer
设置24小时eligible；Artifact逐字节复制验证后接管；orphan分类；data+manifest删除由deleting
marker恢复。active consumer、candidate或未完成Artifact复制时禁止删除。启用删除前按v2
receipt/candidate keyset回填P0至P3 result lifecycle，v1仅在result ref仍可secure-read时回填；
无法回填的正常legacy缺失只收敛对应Task，identity/内容漂移阻断Ready。backfill未完成时
durable result deletion保持disabled。

建议checkpoint：`feat(mcp): manage durable result retention safely`

### P4.4 Observability和安全错误

扩展`src/integrations/mcp/observability.py`、`audit.py`和相关API错误映射：

- audit-only `maf.user_mcp.aggregate_transition.v1`只接收closed字段/Enum；
- 指标只使用design允许的低基数labels；
- 不记录ID、SHA、arguments、用户输入、附件、endpoint、credential或结果正文；
- `execution_crash`不能把`str(exc)`发给前端；metric失败不改变业务终态。

建议checkpoint：`feat(mcp): observe aggregate recovery safely`

### P4.5 17个持久化边界故障注入

新增`tests/integrations/mcp/test_mcp_dispatch_fault_injection.py`和storage/API辅助fixture。每个边界
必须记录：注入点、崩溃前SQL/文件authority、重启动作、网络调用次数、最终projection和Ready
结果。边界与design编号1至17完全一致，不合并证明。

SQLite全部执行；涉及锁/CAS/constraint的边界在真实PostgreSQL validation环境重复。只看HTTP
结果、不核对SQL和本地工件不算通过。

| # | 注入hook与负责checkpoint | 恢复后必须断言 |
|---:|---|---|
| 1 | intent armed后、outbox insert前；P1.2/P3.4 | 补建exact outbox或安全失败；网络0；Ready不被正常单Task阻断 |
| 2 | payload fsync后、action/Interrupt事务前；P2.1/P2.2 | 24小时内保留orphan且无action；到期受控清理；网络0 |
| 3 | action/Interrupt commit后、响应前；P2.2 | 重试返回同一action/Interrupt，不能产生第二审批；网络0 |
| 4 | Answer aggregate事务及并发双提交；P2.2 | 一个accepted Answer/一个Grant/一个cursor；另一提交already-answered或conflict |
| 5 | admission commit后、network gate前；P1.3/P1.4 | Call已may-have-dispatched，崩溃后unknown/no-replay；实际网络0但不得猜测重放 |
| 6 | transport write后、远端响应前；P1.4 | unknown/no-replay；总网络调用1 |
| 7 | durable result fsync后、candidate前；P0.3/P1.4 | 无可信candidate时unknown/no-replay，result保留供lifecycle分类；调用不增加 |
| 8 | candidate三件套fsync后、receipt前；P0.2/P1.4/P3.4 | startup提交同一candidate/receipt；不再次调用Tool |
| 9 | receipt/aggregate commit后、调用方收到响应前；P1.4/P1.5 | retry=already-committed，从cursor恢复Selector/finalizer；调用不增加 |
| 10 | ordinary Call completed后、dispatch finalizer前；P1.4/P1.5 | completed保持dispatch active或由finalizer终结，不能遗留terminal outbox再admit |
| 11 | MRTR evidence fsync后、aggregate adoption前；P3.1/P3.4 | 完整evidence生成同一Interrupt/waiting_input；不完整evidence unknown；不重发旧Call |
| 12 | MRTR Answer commit后、continuation admission前；P3.1/P3.2 | exact continuation最多发送1次，不重新Selector/审批旧action |
| 13 | remote binding prepublish后、aggregate adoption前；P3.3/P3.4 | exact binding被采用并只poll；漂移时unknown；原`tools/call`不重发 |
| 14 | remote terminal candidate后、resume cursor commit前；P3.3/P3.4 | binding claim提交terminal和`remote_terminal`cursor；poll/Tool调用不增加 |
| 15 | candidate lifecycle=`archiving`与三文件移动之间；P4.2 | startup完成或回滚精确移动，active strict enumeration无额外/缺失文件 |
| 16 | candidate lifecycle=`deleting`与archive三文件删除之间；P4.2 | startup完成删除并写deleted；预期缺文件不判corruption |
| 17 | result lifecycle=`deleting`与data/manifest删除之间；P4.3 | startup完成两文件删除；active consumer绝不会进入deleting |

每行fixture都要持久化`network_call_count_before_crash`和重启后的增量断言。只有schema/digest/
identity/fork/内容漂移阻断Ready；正常单Task不可恢复必须收敛该Task并允许服务Ready。

建议checkpoint：`test(mcp): cover aggregate crash boundaries`

### P4.6 完整回归、人工smoke与文档收口

后端定向门禁：

```text
conda run -n multi_agent python -m compileall -q src tests
conda run -n multi_agent python -m unittest discover -s tests/core -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/integrations/mcp -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/lifecycle -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/main_agent -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/mcp_tool -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/skill_tool -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/e2e -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/observability -p 'test_*.py'
```

本仓库当前不存在只读外部挂载`skill/sql-query`，不把缺失的Git-ignored Skill checkout伪装成
失败或成功证据；若验证环境挂载该目录，再运行其本地测试并单独记录。

前端门禁：

```text
cd frontend
npm test -- --run src/api/taskEvents.test.ts src/domain/taskEvents.test.ts \
  src/components/MCPRuntimeStatus.test.tsx src/App.test.tsx
npm test -- --run
npm run typecheck
npm run build
```

完整仓库门禁执行根`AGENTS.md`规定的后端suite；Rust contract/evidence相关测试必须通过。只有
修改native/Rust contract或contract hash时才运行完整`scripts/run_rust_quality_gates.py --run`
并记录环境差异；否则运行其plan/fuzz manifest检查和现有Python Rust-contract回归。

本地人工smoke：

1. 新建含当前2.3 MB图片的OCR任务，批准每个不同Tool；确认v2 envelope小于4 KiB且没有正文。
2. approval后SSE自动继续；普通多Call、MRTR或remote Task按Server能力完成。
3. 选择一个已封存candidate/等待状态执行受控backend restart，确认不重复`tools/call`。
4. 用户取消赢得admission竞态时网络调用为0；admission已赢时只允许best-effort cancel或unknown。
5. 记录安全task/call引用和closed结果，不复制业务payload到实施日志。

同步：design、实施计划状态、`docs/AGENTS.md`、相关src/tests `AGENTS.md`和`CHANGELOG.md`。最后运行
`git diff --check`，审阅完整diff并创建范围清晰的最终checkpoint。

建议checkpoint：`feat(mcp): complete aggregate recovery hardening`

## 11. Checkpoint验证矩阵

所有Python定向行都使用精确前缀
`conda run -n multi_agent python -m unittest`加表中module；每个green checkpoint额外运行
`conda run -n multi_agent python -m compileall -q src tests`和`git diff --check`。标记PG的行
还必须在disposable PostgreSQL validation环境运行对应module，不能把skip计为通过。

| Checkpoint | 必须通过的定向module/命令 |
|---|---|
| P0.2 | `tests.integrations.mcp.test_cp7_terminal_results tests.integrations.mcp.test_cp7_artifacts tests.storage.test_cp7_schema_contract tests.storage.test_user_mcp_postgres_schema_contract` |
| P0.3 | `tests.integrations.mcp.test_mcp_durable_results tests.integrations.mcp.test_user_mcp_temporary_results tests.integrations.mcp.test_user_mcp_gateway tests.integrations.mcp.test_mcp_streaming_response` |
| P0.4 | `tests.api.test_user_mcp_recovery_startup tests.storage.test_user_mcp_terminal_projection tests.integrations.mcp.test_dispatch_coordinator` |
| P1.1 | `tests.storage.test_cp7_schema_contract tests.storage.test_user_mcp_postgres_schema_contract tests.storage.test_postgres_runtime_schema_manifest tests.storage.test_sqlite_bootstrap` |
| P1.2 | `tests.scripts.test_migrate_mcp_dispatch_aggregate tests.storage.test_sqlite_bootstrap tests.storage.test_postgres_schema_reconciler`，PG |
| P1.3 | `tests.storage.test_mcp_dispatch_aggregate_repository tests.storage.test_user_mcp_cp7_postgres_integration tests.storage.test_user_mcp_server_atomic`，PG |
| P1.4 | `tests.storage.test_mcp_dispatch_aggregate_repository tests.storage.test_user_mcp_terminal_projection tests.lifecycle.test_interrupt_resume`，PG |
| P1.5 | `tests.capabilities.mcp_dispatch.test_selector_router_executor tests.integrations.mcp.test_dispatch_coordinator tests.orchestration.test_mcp_dispatch_resume_v2` |
| P2.1 | `tests.integrations.mcp.test_pending_action_payloads tests.integrations.mcp.test_user_mcp_credentials` |
| P2.2 | `tests.storage.test_mcp_dispatch_aggregate_repository tests.storage.test_sqlite_interrupt_repository tests.api.test_user_mcp_grants_and_call_control`，PG |
| P2.3 | 后端：`tests.integrations.mcp.test_dispatch_coordinator tests.api.test_user_mcp_grants_and_call_control tests.api.test_mcp_long_task_events`；前端：`cd frontend`后运行`npm test -- --run src/domain/taskEvents.test.ts src/App.test.tsx`和`npm run typecheck` |
| P3.1 | `tests.integrations.mcp.test_2026_07_28_adapter tests.integrations.mcp.test_user_mcp_credentials tests.integrations.mcp.test_dispatch_coordinator` |
| P3.2 | `tests.integrations.mcp.test_2026_07_28_adapter tests.integrations.mcp.test_dispatch_coordinator tests.api.test_user_mcp_recovery_startup` |
| P3.3 | `tests.integrations.mcp.test_user_mcp_recovery_worker tests.integrations.mcp.test_2025_11_25_task_recovery tests.integrations.mcp.test_phase3_runtime_tasks tests.storage.test_mcp_recovery_claims`，PG |
| P3.4 | `tests.api.test_user_mcp_aggregate_recovery_startup tests.api.test_user_mcp_recovery_startup tests.orchestration.test_mcp_dispatch_resume_v2` |
| P3.5 | `tests.scripts.test_migrate_mcp_dispatch_aggregate tests.storage.test_user_mcp_cp7_postgres_integration tests.storage.test_user_mcp_postgres_schema_contract tests.api.test_user_mcp_runtime_wiring`，PG；再运行migration report/apply/retry隔离smoke |
| P4.1 | `tests.integrations.mcp.test_dispatch_coordinator tests.api.test_user_mcp_grants_and_call_control tests.api.test_user_mcp_recovery_startup` |
| P4.2 | `tests.integrations.mcp.test_cp7_terminal_results tests.storage.test_mcp_dispatch_aggregate_repository` |
| P4.3 | `tests.integrations.mcp.test_mcp_durable_results tests.storage.test_mcp_dispatch_aggregate_repository tests.api.test_user_mcp_aggregate_recovery_startup` |
| P4.4 | `tests.integrations.mcp.test_audit tests.api.test_mcp_plaintext_audit tests.observability.test_user_mcp_rollout_metrics` |
| P4.5 | `tests.integrations.mcp.test_mcp_dispatch_fault_injection`，并在PG重复标记边界 |
| P4.6 | 第10节完整后端、前端、Rust相关门禁和本地人工smoke |

新test module必须随创建它的checkpoint落库；表中module路径是计划合同，不允许通过删除测试
或改成泛化mock绕过。PG行只有真实连接、无skip、事务/CAS断言执行才算green。

## 12. FR/NFR到checkpoint追踪

| 需求 | 主checkpoint |
|---|---|
| FR-001 | P3.4、P4.6 |
| FR-002 | P0.2 |
| FR-003 | P0.3、P4.3 |
| FR-004 | P1.1、P1.3、P3.4 |
| FR-005 | P2.1、P2.2 |
| FR-006 | P2.2、P2.3 |
| FR-007 | P1.3、P2.3、P3.2 |
| FR-008 | P1.4、P1.5 |
| FR-009 | P3.1、P3.2 |
| FR-010 | P3.3、P3.4 |
| FR-011 | P3.4、P3.5 |
| FR-012 | P0.4、P1.4、P3.4 |
| FR-013 | P4.2 |
| FR-014 | P4.1 |
| FR-015 | P2.3、P4.6 |
| FR-016 | P1.5 |
| FR-017 | P1.4 |
| FR-018 | P4.3 |
| NFR-001 | P0.3、P2.1、P4.4 |
| NFR-002 | P4.5 |
| NFR-003 | P1.2至P1.4、P2.2、P3.3 |
| NFR-004 | P0.3、P2.1、P3.4、P4.2 |
| NFR-005 | P0.2、P1.2、P3.5 |
| NFR-006 | P2.1、P4.2、P4.3 |
| NFR-007 | P4.4 |
| NFR-008 | P0.4、P3.4、P3.5 |

## 13. Checkpoint级回滚规则

- P0仅新增双读writer/reader和止血逻辑；回滚前确认没有v2 candidate/receipt被旧binary读取。
- P1 schema apply后禁止直接启动旧binary；代码回滚只能保留新schema并由新版本收敛，或在零
  新状态证明后执行受控逆迁移。
- P2产生pending action/payload后，回滚前必须收敛或失效所有waiting/approved action并删除
  eligible payload；旧版本不能重建精确arguments。
- P3产生waiting_input/remote_pending新outbox语义后，必须使用新版本完成MRTR/remote收敛；
  不得退回会重发原Tool的旧worker。
- P4 lifecycle marker存在时，回滚前先用新版本完成archiving/deleting；不能让旧janitor把
  受控半移动/半删除误判为corruption。

任何回滚都不得自动复活旧失败Task或重放`may_have_dispatched=true` Call。

## 14. 完成定义

只有以下全部成立，开发计划才算实施完成：

1. 18项FR和8项NFR均有自动化证据，17个崩溃边界逐项通过。
2. SQLite和真实PostgreSQL aggregate schema、锁序、CAS、迁移/回滚测试等价。
3. current坏任务零网络重放收敛；新OCR任务不再出现resume envelope too large或approval后停滞。
4. ordinary多Call、approval、MRTR、remote Task、cancel和restart满足统一状态合同。
5. v1/v2 envelope及candidate兼容，64 KiB envelope、32 MiB payload、64 MiB result和保留期门禁
   精确生效。
6. 前端定向测试、typecheck/build、后端相关/完整回归、必要Rust门禁和`git diff --check`通过。
7. `AGENTS.md`索引、design、实施计划和`CHANGELOG.md`与实际代码一致。
8. 没有用户工作区变更、业务I/O、credential、endpoint或本地敏感部署信息进入提交/日志。

## 15. 已记录假设与风险

- 本轮authority以SQL为准；Sidecar enforce跨authority原子性不在范围内。
- 真实PostgreSQL validation DSN/角色由现有候选验证环境提供；缺少该证据时可以完成本地代码，
  但不能声明整个计划完成。
- 32 MiB AES-GCM使用one-shot实现并由crypto gate=1限制；RSS门禁失败时必须回到design审批，
  不能临时发明未记录的分块格式。
- 当前20 MiB上传上限使32 MiB canonical arguments可覆盖Base64/JSON开销；若上传政策改变，需
  重新评估payload上限。
- remote Server能力决定人工smoke是否能覆盖MRTR/remote Task；自动fixture仍是完成硬门禁，
  缺少真实Server能力应记录为外部验证缺口而非伪造成功。

当前没有需要改变产品范围的开放问题。任何提高容量/保留期、增加新密钥领域、部署`prod`、
修改Sidecar authority或自动修复旧失败任务的请求，都必须先更新design并重新审批。

## 16. 2026-08-19实施结果

Phase 0～4已按green checkpoint落地，主要提交从`4eb8676`开始，最终阶段包括：

- `9075033`：固定十阶段startup aggregate recovery；
- `2ce9b36`：closed report、SHA防漂移与SQLite/PostgreSQL受控apply；
- `db5ec70`：64 Selector step、20 approval round与Task generation singleflight；
- `18df4b6`：candidate active/archive、30天保留和部分移动/删除恢复；
- `fde3dfc`：durable result 24小时宽限、held snapshot互斥与双文件删除恢复；
- `bc7b3fe`：closed aggregate transition与`execution_crash`前端脱敏；
- `6c57f1c`：编号1～17的故障注入proof矩阵。

本地`runtime/dev.sqlite3`在确认无旧backend writer与文件占用后完成：

```text
report: legacy + 0 Call rows + 0 outbox rows + apply_eligible=true
apply: applied
same-report retry: already_applied
final report: both tables=final, migration_required=false
migration row: applied, revision=3
integrity: ok
backup: mode=0600, link_count=1
```

验证结果：

- compileall、core 46、storage 357（6项外部环境skip）、lifecycle 25、orchestration 167、
  main-agent capability 65、MCP-tool capability 14以及本次MCP/API定向144项通过；
- integrations 616项中615项通过；唯一失败是既有shadow manifest错误文案断言期望
  `every closed scenario`，实现返回`every current scenario exactly once`，与本变更无关；
- API 463项中457项通过；6项失败集中在缺少本地Git-ignored Skill checkout及既有legacy Skill
  输入行为，与本变更定向回归无重叠；`tests/capabilities/skill_tool`没有可发现测试；
- E2E MCP soft-binding已通过；cancel-late-result用例仍因现有取消路径主动cancel执行handle而等不到
  `task.late_result_discarded`，属于既有生命周期测试/实现矛盾；
- frontend 21个文件、298项测试、typecheck与production build通过；仅保留既有chunk size警告；
- 真实PostgreSQL validation DSN未配置，对应skip不计为通过；未运行`prod`部署或真实OCR人工smoke。

本轮当前实际SQLite authority在cutover前没有legacy Call/receipt，因此candidate/result backfill无需
产生行；migration对含无法唯一分类的legacy业务行继续退出3，不在startup猜测迁移或删除。下一步
只允许启动本地新backend并由用户创建新OCR Task验证；旧失败Task不自动复活或重放。
