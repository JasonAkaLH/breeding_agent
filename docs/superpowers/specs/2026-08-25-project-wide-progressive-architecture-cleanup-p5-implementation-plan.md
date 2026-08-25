# 全仓业务代码渐进式架构清理 P5 实施计划

## 1. 状态与硬边界

- 日期：2026-08-25
- 分支：`main`
- 状态：`complete`
- P5 start commit：`af6e3b09d3c4f696e7a3b3ab28b32cfe5b013d7b`
- P5 start tree：`b72ab8bf10595b0e04dcb9bad40822c6234b0b87`
- P5 start tracked set：1061

P5只处理`src/storage/**`、`src/state/**`、`src/lifecycle/**`中四类已证实结构问题：两个exact pure helper owner、被SQLite命名空间误包裹的共享SQLAlchemy declaration、PostgreSQL对SQLite private mapper/legacy helper的反向依赖、Lifecycle service的aggregate/`Any` persistence annotation。P5不改repository业务方法、session/transaction/lock/CAS、SQL/DDL/schema/data、PostgreSQL override集合、Sidecar wire/支持面、P4 backend selector、Agent recovery状态机、`prod`或P0 deferred behavior。

## 2. ai-slop-cleaner finding register

| Finding | 分类 | 证据 | P5处置 |
|---|---|---|---|
| `P5-FILENAME-SANITIZER-001` | exact duplication | artifact与conversation file store各有一份6-statement filename sanitizer | 移入单一storage-private path helper；旧路径import alias保持 |
| `P5-SQL-SPLITTER-001` | exact duplication | PostgreSQL bootstrap与State schema reconciler的13-statement SQL splitter AST完全相同 | 移入单一storage SQL text helper；原private名使用alias |
| `P5-SQLALCHEMY-DECLARATION-001` | boundary violation | `sqlite/base.py`的base/type decorators已含PostgreSQL dialect；`sqlite/models.py`的60个row class同时被PostgreSQL与State runtime schema使用 | 移入storage shared modules；SQLite旧路径显式re-export同一objects，metadata identity不变 |
| `P5-POSTGRES-PRIVATE-IMPORT-001` | boundary violation | PostgreSQL repository从SQLite repository导入13个private row mapper/fingerprint，并调用3个private legacy static helper | 建立共享pure mapper/legacy owner；PostgreSQL只保留对公开SQLite facade/classes的既有继承/通用事务复用 |
| `P5-LIFECYCLE-PORT-001` | boundary violation | Conversation、Interrupt、Mailbox、Cancellation依赖aggregate `StoragePort`，MCP Presence使用`Any` | direct或无direct-method composite P1 ports；执行body零修改 |
| `P5-REPOSITORY-MONOLITH-001` | reviewed_no_change | `sqlite/repositories.py`约16,895行；PG repository约2,857行 | 本阶段只迁移已证明pure mapper/declaration；不按文件大小拆事务代码 |
| `P5-COMPLEXITY-001` | reviewed_no_change | Storage/State/Lifecycle共29个C901，均位于schema/migration/repository/状态机复杂路径 | 不为指标重写控制流 |
| `P5-SIDECAR-LEASE-001` | deferred behavior | Runtime Sidecar Agent adapter缺P0登记的lease方法 | 不补方法、不加SQL fallback、不伪造parity |

## 3. Checkpoints

### Checkpoint A：计划、范围与基线

当前P4终态已给出Storage full 410项（7项外部PostgreSQL N/A）、Lifecycle 42项和Backend 2149项。P5另复跑117项focused baseline：

- artifact/conversation files、SQLite bootstrap、PostgreSQL runtime manifest/reconciler、Agent SQLite/PostgreSQL schema contract：69；
- Lifecycle full：42；
- Core narrow port/公开StoragePort compatibility：6。

冻结以下结构：

- `SQLiteBase.metadata`表、列、类型、约束、索引和sorted order；
- 60个row class object identity、MRO、table identity和SQLite旧路径可导入性；
- fresh PostgreSQL runtime table/index/trigger DDL与schema manifest；
- `SQLiteStorage._run`同Session构造State+Collaboration、一次callback/commit与cancellation shield；
- 三Agent repository import/MRO/effective supported/unsupported surface及P4三constructor selector trace；
- PostgreSQL override method set与private import现状；
- Lifecycle→Agent recovery functional call sites/order及Cancellation Sidecar writer独立边界。

提交：`docs(cleanup): plan P5 persistence boundaries`

### Checkpoint B：合并两个exact pure helpers

新增两个最小private module：

- filename sanitizer：保持basename、control/slash replacement、trim、fallback与200字符边界；
- SQL splitter：保持DO dollar block识别、分号、空行、尾段与statement顺序。

四个原module以原函数名import alias，删除4份本地body。直接fixture覆盖Windows/POSIX路径、控制字符、空名、长度边界，以及plain/DO block/multiple/trailing SQL；证明alias identity。不得顺带合并短clock/cleaner。

提交：`refactor(storage): reuse path and SQL helpers`

### Checkpoint C：建立共享SQLAlchemy declaration owner

新增storage shared base/models modules：

- base module原样拥有`NAMING_CONVENTION`、`SQLiteBase`、`JSONText`、`DateTimeText`；可额外提供`StorageBase = SQLiteBase`语义alias，但不创建第二metadata；
- models module原样拥有当前60个row class，唯一允许的源码差异是base import指向shared module；
- `sqlite/base.py`与`sqlite/models.py`变为显式compat re-export，旧路径和objects identity保持；
- SQLite repositories/bootstrap、PostgreSQL repositories、State runtime schema和migration内部改从shared canonical module取相同objects。

迁移前后逐项比较60个class AST（忽略source location）、metadata结构、table object identity、manifest及DDL byte equality。禁止修改任何column/type/default/constraint/index/table名，禁止生成或执行migration。

提交：`refactor(storage): share SQLAlchemy declarations`

### Checkpoint D：移除PostgreSQL对SQLite private helper的依赖

把PostgreSQL当前消费的13个pure row mapper/fingerprint与3个legacy record validator/value builder移到storage shared private modules：

- SQLite与PostgreSQL均import相同mapper function objects；SQLite原private调用名保持alias；
- `SQLiteStateRepository`的3个private static names绑定同一shared functions，不保留重复method body；
- PostgreSQL对`src.storage.sqlite.repositories`的import只允许公开`SQLiteStorage`、`SQLiteStateRepository`等现有facade/class，不得再import下划线helper；
- transaction callbacks、session owner、flush/commit/rollback、row locks、override methods与返回model不动。

迁移前后16个function AST和fixture输出exact；PostgreSQL override set及调用site count不变。

提交：`refactor(storage): share repository mappers`

### Checkpoint E：Lifecycle采用P1窄ports

只改annotation/Protocol declaration：

- `ConversationSerialGuard`→`TaskStoragePort`；
- `MailboxService`→`MailboxStoragePort`；
- `MCPTaskPresenceService`→`MCPRemoteTaskStoragePort | None`；
- `InterruptService`→Task/Interrupt/Event/Checkpoint/MCP Remote Task组合Protocol；
- `CancellationService`→Task/Interrupt/Mailbox/Checkpoint/Event组合Protocol。

组合Protocol无direct async method。AgentRun recovery interfaces、Cancellation Sidecar writer、调用body、storage call count/order和一次业务操作的多次storage调用保持。新增contract test证明Lifecycle无aggregate import/`Any` storage annotation及组合surface exact。

提交：`refactor(lifecycle): adopt narrow persistence ports`

### Checkpoint F：平台与全量门禁

先运行Storage/Lifecycle/State focused与Backend canonical。因shared metadata/models/mappers会被PostgreSQL消费，使用本机已有`postgres:17`镜像启动一次性容器并建立7个隔离数据库，分别绑定Agent、MVCC、Conversation Delete、Rollout Integration、Legacy Migration、Rollout Permissions、CP7 validation环境变量；`tests/storage`必须410项零skip。容器只含临时测试数据，测试后停止并由`--rm`回收，不访问`prod`。

Python Runtime Sidecar adapter tests包含在Storage full；Rust源码、Frontend、Linux Parser与真实外部MCP未触及时记为N/A。同步本计划、`docs/AGENTS.md`、Storage/State/Lifecycle `AGENTS.md`与`CHANGELOG.md`，冻结P6 handoff。

提交：`docs(cleanup): close P5 persistence boundaries`

## 4. 必须保持的合同

- `src.storage`、`src.storage.interfaces`、`src.core`、`src.core.contracts`四条`StoragePort`同一identity与259-method aggregate签名不变；
- SQLite/PostgreSQL公开Storage/Agent repository与State/Collaboration facade import、MRO和effective method surface不变；
- shared SQLAlchemy metadata为单一object，60个row class只定义一次；SQLite旧路径re-export同一objects；
- SQLite `_run`与Agent repository独立transaction owner均不移动，BEGIN/commit/rollback/shield及Session数不变；
- PostgreSQL override set、row lock/SKIP LOCKED/CAS、CP7全局锁序、rollout/conversation专用session/commit不变；
- Runtime Sidecar adapter不读取mode、不选择backend、不增加SQL fallback，缺lease保持真实unsupported；
- State不取得session/SQL；Lifecycle只通过窄ports访问并不复制Agent Loop/recovery状态机；
- P4仍是mode/evidence/client availability/backend selector/DI唯一owner；P5 selector=0；
- schema/data/DDL、依赖、公开DTO/error、外部I/O、`prod`与`docker_cmd.md`正文不变。

## 5. 停止与回滚

若class/function AST不能原样迁移、metadata/DDL/manifest/table identity变化、PostgreSQL private helper移除需要修改transaction body、Lifecycle port需要扩大P1合同、真实PostgreSQL目标测试出现skip/failure、或需要修改P4/P6/P7实现，则停止该候选并保留已绿检查点。

每个检查点独立commit，逆序revert即可；无schema/data rollback。巨大repository、29个C901、Sidecar lease缺口和任何业务行为修复均保持`reviewed_no_change|deferred_behavior`，不得借P5扩大目标。

## 6. 实施终态

### 6.1 Checkpoints

| Checkpoint | Commit | 结果 |
|---|---|---|
| Plan/audit | `a3c52a6` | 117项baseline、2组duplicate、shared declaration/private mapper/Lifecycle边界分类闭合 |
| Pure helpers | `5525b6b` | filename sanitizer与SQL splitter各有唯一owner，4个旧调用名为alias |
| SQLAlchemy declarations | `1455235` | base/type decorators与60个row class移到shared owner，SQLite旧路径同object re-export |
| Repository mappers | `18ce127` | 13 mapper/fingerprint与3 legacy helper共享；PostgreSQL不再import SQLite private helper |
| Lifecycle ports | `26fc0d9` | 5个service采用direct/composite P1 ports，执行body零修改 |
| Final ledger | 本文终态提交 | 七隔离库PG、Backend canonical、索引与P6 handoff闭合 |

### 6.2 Gate record

| Scope | ran/fail/skip | 结果 |
|---|---:|---|
| Python compileall `src scripts tests` | completed/0/0 | PASS |
| Core | 48/0/0 | PASS |
| Storage（fresh PostgreSQL 7 profiles） | 457/0/0 | PASS；Agent/MVCC/Conversation/Rollout/Legacy/Permissions/CP7均为隔离数据库 |
| Lifecycle | 45/0/0 | PASS；新增3项窄port合同 |
| Integrations / Agent Skills | 712+211 / 0 / 2 | PASS；2项Linux Result Parser在macOS N/A |
| Orchestration | 112/0/0 | PASS |
| Capabilities | 50/0/0 | PASS（Main Agent 17、MCP Dispatch 15、MCP Tool 15、Skill Tool 3） |
| API / E2E / Observability / Scripts / Deployment | 452+7+39+63+3 / 0 / 0 | PASS |
| Backend合计 | 2199/0/2 | PASS；真实PG将P4的7个环境skip展开为46项实际测试，P5另新增11项直接合同 |
| Shared SQLAlchemy/SQLite focused | 87/0/0 | PASS；60/60 class AST、identity、metadata、manifest与DDL合同 |
| Mapper/legacy AST | 13+3 exact | PASS；transaction methods与override set未改 |
| Storage/State/Lifecycle AST exact duplicate | 0 groups | 从start 2组降为0 |
| Storage/State/Lifecycle C901 / 全仓Ruff审计 | 29 / 162 C901 + 7 F401 + 3 F841 | 与P5 start及P0相同；未运行`--fix` |
| Frontend / Rust source / real external MCP | N/A | P5未触及对应生产源码或外部I/O；Python Sidecar adapter测试已包含在Storage gate |

PostgreSQL首次fresh 7库运行457项通过。随后在同一已被权限/schema用例修改的数据库集合上错误复跑，2个bootstrap按设计以schema drift fail-closed；未修改代码或放宽断言，而是按计划创建第二组fresh 7库，457项再次零skip通过。一次性`postgres:17`容器未挂volume，终态已停止并由`--rm`删除。

### 6.3 Final invariants与handoff

- P5 final tracked set为1072，排序path SHA-256为`fd6a34cc22dad3844ed83b6a337e653d7b35fe208c1b6c2f88d4a24e17c8164c`；相对start只新增本计划、6个shared private module和4份直接测试。
- filename sanitizer和SQL splitter各只有一个body owner；artifact/conversation与bootstrap/reconciler的原函数名均解析到同一canonical function object。
- shared base拥有唯一metadata，60个row class只在`sqlalchemy_models.py`定义；`sqlite/base.py`与`sqlite/models.py`只显式re-export同一objects，旧import路径可用。
- row declaration的module owner有意从SQLite-private改为storage-shared；这些internal SQLAlchemy rows无公开DTO/pickle合同，table object、MRO、metadata、DDL、schema manifest与旧path object identity保持。
- PostgreSQL repository从SQLite repository只import`SQLiteStateRepository`和`SQLiteStorage`两个公开既有facade；13个下划线mapper和3个legacy helper均由shared private module拥有。
- `SQLiteStorage._run`、Agent独立repository transaction、session/BEGIN/commit/rollback/shield、PG override/row lock/SKIP LOCKED/CAS、CP7锁序和Sidecar wire/unsupported surface没有代码差异。
- Lifecycle 5个service只收窄annotation/Protocol；Agent recovery/Cancellation Sidecar functional seam、storage调用次数/顺序和状态机body不变。
- P4仍是backend mode/evidence/client availability/selector/DI唯一owner；P5 adapter selector=0。P6只接管Frontend，不得回写persistence或扩大file-selection/Interrupt authority。
- schema/data、migration、依赖、`prod`、Frontend、Rust源码和`docker_cmd.md`正文均未触及；License Requirement无变化。
