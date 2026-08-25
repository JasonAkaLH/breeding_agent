# 全仓业务代码渐进式架构清理 P0 Baseline

## 1. 状态与边界

- P0状态：`active`；Checkpoint 0～E完成，Checkpoint F待开始
- P0 start commit：`3cf44b14853c383e71bae07d0770f715b38a9d34`
- P0 start tree：`6087fbbabbf80da25c1332b57f651bf83dba24cd`
- 分支：`main`
- 设计规范基线：`7b36cad70979aa4d5d6ded186dc00befa80d8054`
- P0计划规范基线：`bafae8d6424e807a3286e17a5e6611b1e9c05167`
- P0目标：只建立完整inventory、公开合同与高风险行为锁；业务实现零修改
- 非目标：P1窄port、P2～P8结构迁移、行为修复、schema/data migration、`prod`与外部验证平台

P0开始时工作树clean。相对业务设计基线`c8da6ccdf89eed5851cb5a79385cf583560a3c93`，只有`CHANGELOG.md`、`docs/AGENTS.md`、总设计和P0计划变化；`src/`、`frontend/`、`native/`、`scripts/`、`tests/`、CI与依赖零变化。

`docker_cmd.md`只核验metadata：存在、权限`0600`、Git ignored、untracked；未读取正文。

## 2. Inventory

### 2.1 起点集合

- P0 start tracked paths：1039
- 排序path列表SHA-256：`39eebac293a9aa4d2af2c7318fd5a7fa4220879063397e1887dd1b9c51befc0d`
- Source：`git -c core.quotePath=false ls-files`
- Ignored/runtime/仓外文件不进入集合

Checkpoint A增加baseline和inventory两个validation dependency，并把已存在的总设计/P0计划从普通documentation重分类为validation dependency，集合为1041行。Checkpoint B新增三份public contract tests，集合为1044行；Checkpoint C新增一份Agent repository contract test，当前集合为1045行。每个checkpoint均以cached+owned-untracked集合做双向精确比较，提交后以新HEAD重验。

### 2.2 分类结果

| Classification | 数量 |
|---|---:|
| `business_source` | 320 |
| `test` | 418 |
| `contract_or_build_dependency` | 68 |
| `explicit_out_of_scope` | 239 |
| **合计** | **1045** |

`unclassified=0`。业务源码owner分布：P1=8、P2=51、P3=112、P4=25、P5=61、P6=22、P7=41；合计320且每个path恰好一个source owner。P8只拥有finding处置与最终审计，不替代source owner。

### 2.3 Source owner

| Owner | Paths | P0审查边界 |
|---|---|---|
| P1 | `src/core/**` | persistence/shared contract与Cancellation边界；其他Core只能有证据地`reviewed_no_change` |
| P2 | `src/orchestration/**`、`src/capabilities/**` | Agent Loop/Capabilities/continuation/Prompt |
| P3 | `src/integrations/**`、`src/mysql_engine.py` | protocol/client/Gateway/Parser/Skills/LLM/database adapter |
| P4 | `src/api/**` | composition、HTTP/SSE、DTO、bounded file-selection |
| P5 | `src/auth/**`、`src/state/**`、`src/lifecycle/**`、`src/storage/**` | auth/state/lifecycle/storage与Python adapters |
| P6 | Frontend业务源码、`frontend/scripts/prepare_mathjax_assets.mjs` | App、controllers/reducers/components与package lifecycle |
| P7 | Native业务源码、根`scripts/**` operational business | 三Rust runtime workstreams与Operational Scripts |

## 3. 当前公开合同

| Contract | 当前证据 | 锁定状态 |
|---|---|---|
| 四条`StoragePort`路径 | 259个唯一async method；四条路径`is`同一对象 | Checkpoint B literal name/async/signature PASS |
| `src.api.__all__` | `ApiRuntime, build_api_runtime, create_app` | Checkpoint B identity PASS |
| `src.capabilities.main_agent.__all__` | 5个公开对象 | Checkpoint B identity/module PASS；两个Callable alias保持`collections.abc` |
| `src.orchestration.agent_loop.__all__` | 65个公开对象 | Checkpoint B identity/module PASS |
| `ApiRuntime.__init__` / `build_api_runtime` | 完整parameter order/kind/annotation/default/return | Checkpoint B literal shape PASS |
| `src.api` fresh import | 只读取Core contract mode两个key，不读取其他应用key、不构造runtime | Checkpoint B isolated subprocess PASS |
| Python Agent repositories | SQLite=16、PostgreSQL=16、RuntimeSidecar=13个public async methods；共同surface 13，Sidecar当前真实成功operation 11 | Checkpoint C literal import/module/constructor/MRO/surface/signature与backend trace PASS |
| RuntimeSidecar Agent lease | 缺`acquire_task_lease|renew_task_lease|release_waiting_task_lease` | `BEHAVIOR-SIDECAR-AGENT-LEASE-001`，不得补能力 |
| RuntimeSidecar recovery/cancel | `list_recoverable_runs`因未知`agent_run_list` policy抛`KeyError`；`cancel_agent_run`真实fixture因response validation抛`AgentStorageConflict`且Run/Task不变 | `BEHAVIOR-SIDECAR-AGENT-RECOVERY-LIST-001`、`BEHAVIOR-SIDECAR-AGENT-CANCEL-001`；不得在P0修复 |
| FastAPI/DTO/SSE | 复用route、DTO、task-event tests | Checkpoint B/E映射，不新增完整OpenAPI snapshot |
| Rust checked-in contracts | Core/Lifecycle/Runtime Sidecar/Skill Runtime/Safety/MCP Runtime六份 | Checkpoint G byte-level验证 |

## 4. Dependency、authority与bounded seams

| Seam | Exact current symbols/owner | 普通结构检查点约束 |
|---|---|---|
| StoragePort aliases | `src.core.contracts.StoragePort`、`src.core.StoragePort`、`src.storage.interfaces.StoragePort`、`src.storage.StoragePort` | identity/name/async/signature delta=0 |
| Core rollout digest → P3 | `src.core.models.canonical_mcp_rollout_drill_observation_digest`函数内局部import `src.integrations.mcp.rollout_evidence.canonical_evidence_content_digest` | 当前唯一bounded reverse import；不扩张，未来消除须另立owner迁移 |
| API import → Core contract mode | `src.api`首次import经Core enum读取`MAF_RUST_CORE_MODE`、`MAF_CORE_LIFECYCLE_PYO3_MODULE` | 只允许这两个key；其他应用env/config与runtime construction为0 |
| Lifecycle → P2 recovery | `AgentContinuationLocator(Service)`、`AgentLeaseController/Handle`、Agent models/errors、`AgentAtomicWriter`、`AgentRunRepository`、`AgentTaskLeaseStore` | logical call-site IDs/kinds/counts/order exact；无第二状态机 |
| P3 → P2 MCP Dispatch | `MCPDispatchOutcome`、selector/router、selector models/context/fingerprint | imports/object identity与functional calls exact；无复制/内联/缓存绕过 |
| P5 Agent adapters → P2 contracts | Agent models/enums/errors/persistence payloads | contract-only；不得调用Agent Loop controller/service |
| P4 → P5 composition | `SQLiteAgentRepository`、`PostgreSQLAgentRepository`、`RuntimeSidecarAgentRepository` | 三次直接assignment只在`build_api_runtime` composition root；P5 adapter mode/backend selector=0 |
| P4 file-selection | `ConversationFileSelectionRuntimeMixin` + file-selection domain | candidate/LLM/attachment/TaskNode/Interrupt/event trace exact；不迁入Slot/P2 |
| Frontend | App → controllers/components → domain/wire | App/Attachment/Task Runtime owner不复制；initial submit与answer owner不互换 |
| Rust root/private | root public declarations；private kernel/adapter/service | root identity/attrs保留；private不得调用root assembly wrapper |

### 4.1 Checkpoint D stable logical call-site IDs

Location metadata绑定Checkpoint D生产源码位置；owner内一对一搬家只更新location，logical ID、kind、count与order不得变化。

| Logical ID | `entry/scenario + phase + callee + ordinal` | 当前location | Exact trace/约束 |
|---|---|---|---|
| `D-RUN-WAIT-ENTRY-REL-01` | runner already-waiting + entry + `release_waiting` + 1 | `runner.py:137` | acquire后model/sample/capability/outcome均0，直接release |
| `D-RUN-WAIT-WAVE-OUT-01` | runner new-waiting + outcome + `commit_agent_call_outcome` + 1 | `runner.py:253` | acquire → model → sample commit → capability → outcome commit |
| `D-RUN-WAIT-WAVE-REL-02` | runner new-waiting + release + `release_waiting` + 2 | `runner.py:273` | outcome后唯一release；renew=0 |
| `D-REC-PRELOAD-ACK-01` | continuation duplicate/terminal + preload + `ack` + 1 | `agent_run_recovery.py:270-286` | load Run/items → ack → return；acquire/resolve/commit/reload=0 |
| `D-REC-ACTIVE-RESOLVE-01` | continuation active + resolve + `resolve_authority` + 1 | `agent_run_recovery.py:288-304` | acquire → reload → resolve |
| `D-REC-POST-BARRIER-ACK-01` | concurrent committed/terminal + post-resolve + `ack` + 1 | `agent_run_recovery.py:305-316` | reload/fence → ack → return；commit/resume/ack后reload=0 |
| `D-REC-ACTIVE-COMMIT-01` | continuation active + commit + `commit_agent_call_outcome` + 1 | `agent_run_recovery.py:317-335` | resolution validation后唯一commit，随后ack → reload |
| `D-REC-REMAINING-REL-01` | continuation remaining-waiting + release + `release_waiting` + 1 | `agent_run_recovery.py:337-350` | remaining非空时resume/model/Tool=0 |
| `D-REC-CLEARED-RUN-01` | continuation waiting-cleared + resume + `run_claimed` + 1 | `agent_run_recovery.py:351-358` | 复用原handle/binding；无统一release，final-candidate按loop state映射 |
| `D-CRASH-EARLY-REC-01` | crash terminal/waiting + reconcile + `reconcile_agent_run_consistency` + 1 | `agent_run_recovery.py:189-196` | reconcile后立即return；acquire/abort/resume=0 |
| `D-CRASH-ACTIVE-ABORT-01` | crash active reserved + abort + `commit_agent_call_outcome` + N | `agent_run_recovery.py:198-232` | reconcile → acquire → reload/items → 每个outstanding按ordinal abort |
| `D-CRASH-ACTIVE-RUN-02` | crash active + resume + `run_claimed` + 2 | `agent_run_recovery.py:234-247` | abort全部完成后唯一resume；resolver/ack=0 |
| `D-API-LOCATOR-DURABLE-01` | API locator cache miss + rebuild + `from_safe_dict` + 1 | `runtime.py:4332-4365` | interrupt carrier miss → Run → items；从waiting result durable locator重建 |

### 4.2 Checkpoint E stable logical call-site IDs

| Logical ID | `entry/scenario + phase + callee + ordinal` | 当前location | Exact trace/约束 |
|---|---|---|---|
| `E-MCP-SELECT-01` | dispatch automatic/explicit + select + `selector.select` + 1..2 | `dispatch_coordinator.py:758,766` | normal=1；repair最多2；rejection无Tool send |
| `E-MCP-ROUTE-01` | dispatch route-another + route + `server_router.route` + 1..2 | `dispatch_coordinator.py:2995,3000` | 只用remaining owner-scoped profiles；repair最多2 |
| `E-MCP-RESERVE-01` | dispatch call + reserve + `reserve_mcp_call` + 1 | `dispatch_coordinator.py:2109` | reserve在registration与唯一Tool send前 |
| `E-MCP-REGISTER-02` | dispatch call + may-have-dispatched + `mark_mcp_call_may_have_dispatched` + 2 | `dispatch_coordinator.py:2191,2483` | 普通failure=1；approval-resume registration+heartbeat=2，均保持当前幂等写 |
| `E-MCP-SEND-03` | dispatch call + send + `gateway.call_tool` + 3 | `dispatch_coordinator.py:2220` | 原始Tool/job-start第二次调用=0；17 fault boundary按各自network delta |
| `E-MCP-TERMINAL-04` | dispatch call + terminal/no-replay + `finish_mcp_call` + 4 | `dispatch_coordinator.py:2437,2700` | terminal或unknown/no-replay唯一闭合；不同分支不统一链 |
| `E-GW-BOOT-01` | gateway scope + bootstrap + endpoint/credential/client + 1..5 | `gateway.py:770-795` | endpoint revalidate → credential read → client → initialize → list tools |
| `E-GW-GUARD-01..04` | gateway call + accepting + guard + 1..4 | `gateway.py:1030,1102,1334,1343` | public admission 1次；execute发送前、raw后、normalize后各1次 |
| `E-GW-CALLBACK-01` | gateway call + registration + callback + 1 | `gateway.py:1051-1180` | created → registered → 唯一Tool send |
| `E-API-START-01` | API startup + pre-ready + sentinel/aggregate/dispatch/Agent recovery + 1..4 | `runtime.py:8161-8202` | sentinel → admission → aggregate → dispatch → Agent recovery；post-ready work随后 |
| `E-API-SHUTDOWN-01` | API shutdown + close + quiesce/tasks/CP7/services/engine + 1..N | `runtime.py:9649-9725` | 固定顺序；首错仍阻断后续cleanup |
| `E-FILE-SELECT-01` | file selection + decide/persist + candidate/LLM/TaskNode/Interrupt/events + 1..N | `file_selection_runtime.py:29-734` | P4 mixin/domain唯一owner；audit-only事件与attachment/sheet binding顺序保持 |

## 5. Finding register

Ruff只作为审计入口：当前`src scripts`有162个C901、7个F401、3个F841，共172个信号。C901不自动等于需要拆分；只有结合owner、side effect和合同证据后才成为finding。P0不运行`--fix`。

| Finding ID | 类型 | Owner | 证据与边界 | 退出条件 |
|---|---|---|---|---|
| `P0-P1-STORAGE-PORT-001` | `structural_candidate` | P1 | `src/core/contracts.py`约1613行，StoragePort 259 methods | 四路径identity不变；259方法恰好映射一次到窄域；不建catch-all |
| `P0-P1P3-CORE-ROLLOUT-DIGEST-001` | `reviewed_no_change` | P1/P3 | Core rollout observation digest函数局部import P3 canonical evidence digest | P0锁唯一symbol/function scope；不得扩张Core→Integrations imports |
| `P0-P1P4-IMPORT-CORE-CONTRACT-001` | `reviewed_no_change` | P1/P4 | fresh API import读取两个Core Rust contract mode key | 保持当前allowed set；P4不得新增应用env/config import-time读取 |
| `P0-P2-AGENT-SEAMS-001` | `structural_candidate` | P2/P5 seam | runner/invoker/lease/continuation/task projection与Lifecycle recovery交接 | waiting/recovery逐分支trace exact；无第二authority |
| `P0-P2-MEMORY-001` | `structural_candidate` | P2 | `conversation_memory.py`约1801行，多阶段memory/prompt职责 | 仅在P2按owner拆分；token/LLM/prompt结果不变 |
| `P0-P3-SKILLS-001` | `structural_candidate` | P3 | execution/missing-input/slot/input-resolution多个大模块 | schema/value/resolution/execution边界清楚；隐私/fallback不变 |
| `P0-P3-MCP-COORDINATOR-001` | `structural_candidate` | P3 | `dispatch_coordinator.py`约3510行；`dispatch`/`_call_tool`高复杂度 | phase清晰、Coordinator唯一owner、17 fault/no-replay exact |
| `P0-P3-MCP-GATEWAY-001` | `structural_candidate` | P3 | `gateway.py`约2382行，scope/call/catalog/shared state交织 | single shared state与external I/O owner不变 |
| `P0-P3-GATEWAY-GUARDS-001` | `reviewed_no_change` | P3 | Gateway当前共有4个accepting guards，而非计划初始假设的2个 | 锁定public 1 + execute 3；结构迁移不得合并或删除 |
| `P0-P3-MCP-REGISTER-HEARTBEAT-001` | `reviewed_no_change` | P3/P5 | approval-resume对may-have-dispatched执行registration+heartbeat两次幂等写，普通failure为一次 | 分场景锁call count；不得统一去重 |
| `P0-P3-RESULT-PARSER-001` | `structural_candidate` | P3 | service/worker supervision与cleanup阶段 | decoder独立；spawn/pickle/timeout/cleanup/projection exact |
| `P0-P4-RUNTIME-001` | `structural_candidate` | P4 | `runtime.py`约13878行；factory复杂度135 | stable facade/factory/patch seam；startup/shutdown与selector exact |
| `P0-P4-FILE-SELECTION-001` | `structural_candidate` | P4 | bounded business authority位于API mixin/domain | 原位整理；LLM/storage/attachment/Interrupt/event exact |
| `P0-P5-SQLITE-001` | `structural_candidate` | P5 | SQLite repositories约16895行、models约2204行 | domain逐项迁移；同Session/transaction/lock/CAS不变 |
| `P0-P5-POSTGRES-001` | `structural_candidate` | P5 | PostgreSQL repositories/session含专用override/role逻辑 | shared pure基础与PG override分离；真实PG门禁 |
| `P0-P5-AGENT-ADAPTERS-001` | `reviewed_no_change` | P5 | 三Agent adapters相似但surface/backend/transaction不同；同一missing-run fixture语义相同而SQL Session trace与Sidecar RPC trace不同 | 只比较共同operation；不合并authority、不补lease、不SQL fallback |
| `P0-P6-APP-001` | `structural_candidate` | P6 | `App.tsx`约3814行，message/attachment/task effects交织 | App/Attachment/Task Runtime owner唯一；DOM/行为不变 |
| `P0-P6-TASK-EVENTS-001` | `structural_candidate` | P6 | domain taskEvents约1578行 | wire/reducer/controller边界；state identity与event semantics不变 |
| `P0-P7-RUNTIME-SIDECAR-001` | `structural_candidate` | P7 | root lib约4008行、sqlite adapter约2014行 | root public定义保留；kernel/service/codec/backend合同不变 |
| `P0-P7-SKILL-RUNTIME-001` | `structural_candidate` | P7 | Skill Runtime root lib约2209行 | policy/process/service/codec边界；PyO3/wire/error不变 |
| `P0-P7-MCP-RUNTIME-001` | `structural_candidate` | P7 | MCP Runtime root lib约2135行 | contract/SDK/JSON-RPC/sanitizer/registry边界；不接预备registry |
| `P0-P7-SCRIPTS-001` | `structural_candidate` | P7 | 11个operational/migration/SQL业务脚本 | 只合并完全等价pure helper/engine lifecycle；CLI/SQL/receipt顺序不变 |
| `P0-P8-PY-UNUSED-IMPORT-001` | `structural_candidate` | P8 | Ruff F401=7，分布于API runtime、Skill resource、Parser content、Agent invocation | 逐项证明无patch/import/registration/type side effect后删除，否则`reviewed_no_change` |
| `P0-P8-PY-UNUSED-VAR-001` | `structural_candidate` | P8 | Ruff F841=3，API runtime两个、Prompt envelope一个 | 证明删除不改变evaluation/exception/trace后才删 |
| `BEHAVIOR-ORCH-PARALLEL-001` | `deferred_behavior` | P2 | sibling cancel/异常策略非对称 | 结构迁移锁当前结果；行为修复另立任务 |
| `BEHAVIOR-ORCH-LEASE-001` | `deferred_behavior` | P2 | heartbeat token与Invocation旧token现状 | trace锁定；不顺手传播新token |
| `BEHAVIOR-ORCH-TERMINAL-CONTINUATION-001` | `deferred_behavior` | P2/P5 | terminal/active/result/ack矩阵非对称 | 逐分支锁定；不统一快路径 |
| `BEHAVIOR-ORCH-AUTHORITY-SNAPSHOT-001` | `deferred_behavior` | P2/P4/P5 | resume入口在lease前读取/claim authority | 入口trace exact；不改锁序 |
| `BEHAVIOR-ORCH-TASKNODE-PREPROJECTION-001` | `deferred_behavior` | P2 | TaskNode可先可见且outcome后失败无补偿 | 锁当前两阶段结果；不新增补偿 |
| `BEHAVIOR-PARSER-CLEANUP-CANCEL-001` | `deferred_behavior` | P3 | cleanup join/terminate/kill可取消 | barrier锁当前阶段；不加shield/finally |
| `BEHAVIOR-API-LIFECYCLE-001` | `deferred_behavior` | P4 | startup部分失败、shutdown首错阻断cleanup | trace锁定；不修复错误策略 |
| `BEHAVIOR-SIDECAR-AGENT-LEASE-001` | `deferred_behavior` | P2/P4/P5/P7 | enforce选择Sidecar Agent adapter但其缺3个lease methods | 保持supported/unsupported与当前失败；不补方法/SQL fallback |
| `BEHAVIOR-SIDECAR-AGENT-RECOVERY-LIST-001` | `deferred_behavior` | P2/P5/P7 | Sidecar adapter公开`list_recoverable_runs`调用gRPC `agent_run_list`，但Rust contract无该operation policy，当前抛exact `KeyError` | 保持当前失败；P0不补policy、不改error映射 |
| `BEHAVIOR-SIDECAR-AGENT-CANCEL-001` | `deferred_behavior` | P2/P5/P7 | 真实Sidecar `cancel_agent_run`返回的error envelope未通过response validation，当前转为`AgentStorageConflict(runtime_store_response_invalid)`且Run/Task保持running | 保持current state/error；P0不改Rust/Python contract或terminal transition |

### 5.1 Exact duplicate审计

一次性AST body scan在`src/`与根`scripts/`发现22组三语句以上的完全相同function body。它只是语法等价证据，不证明authority/contract等价。首批处置分类：

| Finding ID | 类型 | Owner | 范围 | 结论/退出 |
|---|---|---|---|---|
| `P0-P3-DUP-SKILL-PARSING-001` | `exact_duplicate` | P3 | Agent Skills JSON object loader与`_string_tuple`各3份 | P3先锁schema/error/fallback，再选择单一private owner |
| `P0-P3-DUP-MCP-HELPERS-001` | `exact_duplicate` | P3 | selector/coordinator attachment helpers、safety/minute helpers | P3按调用trace验证后合并；不跨security authority |
| `P0-P3-DUP-RUNTIME-STATE-001` | `exact_duplicate` | P3 | Skill/MCP runtime state retain/release bodies | 只有revision语义完全一致才复用，否则`reviewed_no_change` |
| `P0-P5-DUP-STORAGE-HELPERS-001` | `exact_duplicate` | P5 | artifact/conversation filename sanitizer、两处SQL splitter | P5分别证明安全/SQL方言错误一致后再决定 |
| `P0-CROSS-OWNER-SIMILAR-001` | `reviewed_no_change` | P3/P4/P5 | invalidation buses、gRPC frame helpers、runtime response helpers、trivial cleaners | 跨owner抽象会制造反向依赖；本项目默认不合并 |

剩余语法重复为短小constructor/trivial normalization或已被上述组覆盖，P0记录为`reviewed_no_change`；P8必须基于当时HEAD重新证明，不能凭AST hash直接删除或合并。

## 6. Behavior-lock matrix

| Domain | 当前覆盖入口 | P0动作 | 状态 |
|---|---|---|---|
| Python public/StoragePort | Core contracts、SQLite bootstrap | literal identity/signature/pickle/import tests | Checkpoint B PASS（13项新增，focused合计43项） |
| Agent adapters/Cancellation | Agent storage、runtime-sidecar contract、runtime wiring | surface/MRO/common fixture/transaction/selector/off-shadow-enforce/AgentRun trace | Checkpoint C PASS（focused 102项） |
| Agent waiting/recovery | Agent Loop、continuation、Lifecycle recovery | 逐分支stable logical ID、order/count、durable locator trace | Checkpoint D PASS（focused 29项） |
| MCP/API authority | selector/router、Coordinator/Gateway、startup/file-selection | public identity、17 fault proofs、order/count、lifecycle与P4 owner trace | Checkpoint E PASS（focused 161项） |
| Frontend | App/taskEvents tests | 复用覆盖，缺口才加最小断言 | Checkpoint F pending |
| Rust/Scripts | 六contract tests、migration tests、Rust quality | 记录root public surface与sequence证据 | Checkpoint G pending |

## 7. PostgreSQL P5 profile

P0不连接真实PostgreSQL。P5在迁移对应domain前必须使用隔离non-prod DSN，逐项覆盖auth CAS、AgentRun/Item/lease/atomic outcome、Task/Node CAS、mailbox/interrupt/event order、owner guard/claim takeover、rollout与legacy migration role separation、conversation delete并发、fresh bootstrap和drift rollback。

Required profile必须：目标收集>0、failure=0、skip=0、临时DB/role清理成功；日志不得记录DSN/credential。没有真实profile时对应P5切片不得开始，SQLite/mock不能替代。

## 8. Gate records

| Scope | CWD / command | Platform | ran/fail/skip | 结论 |
|---|---|---|---|---|
| Compile | repo / `python -m compileall -q src scripts tests` via conda env | macOS/Python 3.13 | completed/0/0 | PASS |
| Core smoke | repo / `unittest tests.core.test_contracts` | macOS/Python 3.13 | 8/0/0 | PASS |
| SQLite smoke | repo / `unittest tests.storage.test_sqlite_bootstrap` | macOS/Python 3.13 | 21/0/0 | PASS |
| Agent smoke | repo / Agent Loop + continuation modules | macOS/Python 3.13 | 4/0/0 | PASS |
| Recovery smoke | repo / Lifecycle recovery module | macOS/Python 3.13 | 12/0/0 | PASS |
| Frontend event smoke | `frontend/` / two taskEvents files | macOS/Node | 51/0/0 | PASS |
| Rust fmt | repo / existing Rust quality gate `cargo_fmt` | macOS/Rust toolchain | completed/0/0 | PASS |
| Ruff audit | repo / `ruff check src scripts --select C90,F401,F841` | macOS/Python env | 172 signals | audit observation，不是质量PASS/FAIL gate |
| Checkpoint B public contracts | repo / Core+API+Orchestration public contract focused suite | macOS/Python 3.13 | 43/0/0 | PASS |
| Checkpoint C repository/cancellation | repo / 8-module Storage/API/Lifecycle focused suite | macOS/Python 3.13 | 102/0/0 | PASS；另观察到1条unclosed SQLite connection ResourceWarning，不计测试失败，P0不改生产清理语义 |
| Checkpoint C changed-test compile/Ruff | repo / 4份受影响test files | macOS/Python 3.13 | completed/0/0 | PASS |
| Checkpoint D continuation/recovery | repo / 5-module Orchestration/Lifecycle/API focused suite | macOS/Python 3.13 | 29/0/0 | PASS |
| Checkpoint E MCP/API authority | repo / 8-module Capability/Integration/API focused suite（含17 boundary proof调度） | macOS/Python 3.13 | 161/0/0 | PASS |
| Checkpoint E changed-test compile/Ruff | repo / 6份受影响test files | macOS/Python 3.13 | completed/0/0 | PASS |

所有记录绑定P0 start commit`3cf44b14853c383e71bae07d0770f715b38a9d34`。测试数量以后续checkpoint当次输出为准，旧PASS不能替代受影响门禁。

## 9. 外部与平台状态

- 真实PostgreSQL：`N/A`，P0未触及PG业务实现；profile已为P5定义。
- Linux Result Parser：`N/A`，P0未触及worker/resource/cleanup生产路径。
- manylinux/PyO3：`N/A`，P0未触及bridge/packaging生产路径。
- fuzz：`N/A`，P0未触及parser/validation/sanitizer/policy生产路径；`mcp_runtime_protocol`遗漏留给P7计划。
- 真实外部MCP：`N/A`，P0未触及transport/adapter/runtime wiring。

以上均不是PASS；若后续P0测试/contract变更实际触发目标平台要求，状态必须改为`platform_pending`并停止对应切片。

## 10. P1 handoff（P0期间只维护约束）

- 输入：四条StoragePort identity、259 method name/async/signature literal baseline。
- P1产物：每个method恰好一个narrow domain、owner plan与consumer handoff；无catch-all。
- Cancellation：独立non-aggregate writer；off/shadow/enforce/no-client trace必须先绿。
- 禁止：迁移P2～P7 private helper、改变Agent/Sidecar authority、修复deferred behavior、修改schema/data。
- P1开始条件：P0 final commit clean；inventory/contract/trace闭合；Backend/Frontend/Rust适用全量门禁通过。

P0完成前，本节不能被解释为P1实施授权。
