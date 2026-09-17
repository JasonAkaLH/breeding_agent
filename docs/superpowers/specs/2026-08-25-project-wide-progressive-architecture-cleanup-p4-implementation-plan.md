# 全仓业务代码渐进式架构清理 P4 实施计划

## 1. 状态与硬边界

- 日期：2026-08-25
- 分支：`main`
- 状态：`complete`
- P4 start commit：`477b467bd0ab574f140ed7879f38e619fe210531`
- P4 start tree：`e8d0a679fa58c0342bd4611fb64cd6567fbf3191`
- P4 start tracked set：1055

P4只处理`src/api/**`内三类已证实结构问题：重复的request→runtime访问器、`ApiRuntime`中连续且单一owner的上传/附件职责块、两个private helper的过宽persistence annotation。公开`ApiRuntime`/`build_api_runtime`/`src.api` identity、签名、factory monkeypatch seam、repository selector、HTTP/OpenAPI/SSE、Interrupt/Slot/file-selection业务authority、startup/shutdown/recovery顺序、schema/data、依赖、`prod`均不变。

## 2. ai-slop-cleaner finding register

| Finding | 分类 | 证据 | P4处置 |
|---|---|---|---|
| `P4-RUNTIME-ACCESS-001` | exact duplication | `api/auth.py`及6个route module各有一份完全相同的单行`_runtime(request)` | 建立单一private accessor，原模块import alias为`_runtime`，保持调用名与返回对象identity |
| `P4-UPLOAD-OWNER-001` | structural candidate | `ApiRuntime`第2451～2900行的12个连续方法只拥有上传、会话文件、sheet selection和对应Interrupt桥接 | 原样迁入`ConversationUploadRuntimeMixin`；`ApiRuntime`继承一次，不改方法body/签名/调用顺序 |
| `P4-PERSISTENCE-BOUNDARY-001` | boundary violation | module helper `_mark_remote_continuation_dispatched`使用aggregate；memory builder helper误写concrete `SQLiteStorage`但实际也接收PostgreSQL | 分别改用`MCPRemoteTaskStoragePort`与已有`ConversationMemoryStoragePort`；公开`ApiRuntime.__init__.storage: StoragePort`保持兼容签名 |
| `P4-RUNTIME-MONOLITH-001` | reviewed_no_change | `runtime.py`约13,878行，`ApiRuntime`约9,851行，`build_api_runtime`约1,988行 | P4只迁移低风险连续upload块；不把文件大小当完成指标 |
| `P4-COMPLEXITY-001` | reviewed_no_change | API有29个C901；其中Interrupt、MCP recovery、startup/shutdown、factory及file selection承担闭合状态/顺序 | 不为降低C901拆控制流；只有独立行为计划可改写 |
| `P4-API-EXACT-DUP-001` | reviewed_no_change | 三语句以上AST exact duplicate为0 | 不制造通用helper库；短helper只有runtime accessor满足exact同owner条件 |
| `P4-LIFECYCLE-GAP-001` | deferred behavior | partial startup与shutdown首错阻断后续cleanup为P0既有行为 | 继续由direct trace测试冻结，不在结构清理中修复 |

## 3. Checkpoints

### Checkpoint A：计划、范围与基线

以下183项focused baseline必须先绿：

- runtime public/factory/state/route：11；
- aggregate startup、Agent continuation、MCP task restart：22；
- conversation file selection与uploads：103；
- Slot v2、Skill input resolution、pending Skill context：47。

同时冻结：

- `ApiRuntime`与`build_api_runtime`完整signature；
- `src.api`三项导出identity；
- factory内`PostgreSQLAgentRepository → SQLiteAgentRepository → RuntimeSidecarAgentRepository`三个直接assignment及patch object identity；
- startup和shutdown marker的exact count/order；
- file-selection唯一mixin/domain owner与完整调用trace；
- API import仅允许既有两个Core Rust contract mode key读取。

提交：`docs(cleanup): plan P4 API runtime boundaries`

### Checkpoint B：复用request runtime accessor

新增一个API-private accessor，只执行`return request.app.state.runtime`。`api/auth.py`和6个route module以`runtime_from_request as _runtime`导入，删除7份本地函数。不得改变FastAPI dependency、route签名、request state读取时点或异常。

新增直接测试证明7个module的`_runtime`对象identity相同，并复跑route/runtime public合同和完整API route contract。

提交：`refactor(api): reuse runtime request accessor`

### Checkpoint C：分离上传/附件runtime职责

新增`src/api/upload_runtime.py`，定义`ConversationUploadRuntimeMixin`，原样接管以下12个方法：

- `save_upload`、`ensure_upload_allowed`、`list_uploads`、`delete_upload`；
- `resolve_uploads_for_message`、`resolve_conversation_uploads_for_message`；
- `_upload_context_metadata`、`_conversation_file_context_metadata_for_task`；
- `_raise_missing_uploads`、`_normalize_upload_sheet_selections`；
- `_open_sheet_selection_interrupt`、`_sheet_selection_question`。

只移动定义与必需imports；body、decorator、signature、异常、storage/file-store调用、Task/Node/Interrupt/Event顺序逐语法树相同。`ApiRuntime`继承`ConversationFileSelectionRuntimeMixin`和`ConversationUploadRuntimeMixin`各一次；公开构造签名及route surface不变。新增结构测试冻结mixin defining module、12-method exact set、ApiRuntime可见签名与原类无重复定义。

功能门禁至少包含uploads、file selection、MCP explicit binding、Slot v2和pending Skill context；任何upload/sheet/Interrupt trace变化立即回滚本检查点。

提交：`refactor(api): isolate upload runtime owner`

### Checkpoint D：收窄private persistence annotations

- `_mark_remote_continuation_dispatched`只依赖`MCPRemoteTaskStoragePort`；
- `_resolve_conversation_memory_builder`只依赖P2已建立的`ConversationMemoryStoragePort`；
- `ApiRuntime.__init__`的`StoragePort` annotation与四路径identity保持原样，作为已冻结公开compat seam；
- concrete SQLite/PostgreSQL/Runtime Sidecar imports与三次backend assignment仍只存在composition/factory；P5 adapter不得取得mode selector。

新增contract test只扫描private helper annotations和合法aggregate seam，不改运行时代码。

提交：`refactor(api): narrow private persistence ports`

### Checkpoint E：全量门禁与终态handoff

逐域运行Backend canonical；P4直接运行API全量并复跑startup/shutdown、Interrupt/Slot、file-selection、upload、Agent backend selector与cross-layer continuation suites。Frontend/Rust、真实PostgreSQL、Linux Parser与真实外部MCP仅在实际触及对应路径时运行，否则记为`N/A: production path not touched`。

同步本计划、`docs/AGENTS.md`、`src/api/AGENTS.md`与`CHANGELOG.md`，冻结P5 handoff。最终生产diff只允许一个runtime accessor、一个upload mixin、对应原模块删除/继承/import及private annotations。

提交：`docs(cleanup): close P4 API runtime boundaries`

## 4. 必须保持的合同

- `ApiRuntime`、`build_api_runtime`、`create_app`导出object identity和完整签名不变；
- factory显式class/parameter monkeypatch seam、三个repository constructor identity/assignment顺序不变；
- import不构造runtime、不新增env读取；runtime holder完整构造后再赋值；
- master-key sentinel→aggregate reconciliation→dispatch recovery→Agent recovery→Ready→post-ready remote task顺序不变；
- shutdown quiesce→cancel/gather→CP7 close→service close→engine dispose顺序不变；partial-startup/shutdown-first-error gap保持；
- answer/cancel/recovery只使用当前Interrupt、Slot和Agent authority，不复制Orchestration/P5状态机；
- file-selection仍是唯一bounded API business authority，LLM/storage/attachment/TaskNode/Interrupt/event call sites/kinds/counts/order不变；
- upload/save/delete/sheet selection的owner、status、rollback/repair、message/event与文件I/O时序不变；
- routes/projection/components不新增concrete repository import或backend selector；P5 selector保持0。

## 5. 停止与回滚

若迁移方法需要修改body才能通过、若测试或外部patch依赖原defining module、若`ApiRuntime`/factory签名或route surface变化、若startup/shutdown/Interrupt/file-selection/upload trace变化、若需要修改P5实现或schema/data，则停止该候选并保留已绿检查点。

每个检查点独立commit，逆序revert即可；无schema/data rollback。29个C901、Interrupt约4,000行职责块、lifecycle约1,850行职责块和1,988行factory均不因文件大小自动进入P4，P8也不得把未证明候选当dead/duplicate删除。

## 6. 实施终态

### 6.1 Checkpoints

| Checkpoint | Commit | 结果 |
|---|---|---|
| Plan/audit | `b38d10d` | 183项baseline、7份accessor复制、12-method upload块与29个C901分类闭合 |
| Runtime accessor | `2f834bc` | 7个consumer以原`_runtime`名复用单一private accessor |
| Upload runtime owner | `eb7b0e5` | 12个方法迁入唯一mixin；12/12迁移前后AST exact |
| Private narrow ports | `2be4ab3` | Remote continuation与memory builder使用既有窄port；公开aggregate seam保持 |
| Final ledger | 本文终态提交 | Backend canonical、API索引与P5 handoff闭合 |

### 6.2 Gate record

| Scope | ran/fail/skip | 结果 |
|---|---:|---|
| Python compileall `src scripts tests` | completed/0/0 | PASS |
| Core / Storage / Lifecycle | 48+410+42 / 0 / 7 | PASS；7项真实PostgreSQL profile为未触及平台N/A |
| Integrations / Agent Skills | 712+211 / 0 / 2 | PASS；2项Linux Result Parser在macOS N/A |
| Orchestration | 112/0/0 | PASS |
| Capabilities | 50/0/0 | PASS（Main Agent 17、MCP Dispatch 15、MCP Tool 15、Skill Tool 3） |
| API | 452/0/0 | PASS；相对P3新增6项owner/accessor/port直接合同 |
| E2E / Observability / Scripts / Deployment | 7+39+63+3 / 0 / 0 | PASS |
| Backend合计 | 2149/0/9 | PASS；平台skip无新增 |
| Upload/file-selection/MCP-binding/Slot/pending focused | 157/0/0 | PASS |
| Runtime owner/public focused | 28/0/0 | PASS；包含signature、selector、startup/shutdown与private port |
| Upload method AST | 12/12 exact | PASS；只改变defining owner，不改变body/decorator/signature |
| API AST exact duplicate | 0 groups | 三语句以上与start相同；7份单行accessor已归一 |
| API C901 / 全仓Ruff审计 | 29 / 162 C901 + 7 F401 + 3 F841 | 与P4 start及P0相同；未运行`--fix` |
| Frontend / Rust / real external MCP | N/A | P4未触及对应生产路径或外部I/O；不冒充新证据 |

### 6.3 Final invariants与handoff

- P4 final tracked set为1061，排序path SHA-256为`1b3203b4650b007a37083bae9ff0b3b54f9abf9f74d6f40adda5ac0a8843928a`；相对start只新增本计划、2个private业务module和3份直接测试。
- request→runtime只有`runtime_access.runtime_from_request`一个owner；7个原module的`_runtime`均为同一object alias，路由签名与读取时点不变。
- upload/save/delete/resolve/sheet-selection 12个方法只有`ConversationUploadRuntimeMixin`一个defining owner；`ApiRuntime`继承并暴露同一function object，原类无重复声明。
- `runtime.py`从13,878行降为13,438行；减少来自职责移动，不删除控制流。Interrupt约4,000行、lifecycle约1,850行与factory约1,988行保持原位。
- `ApiRuntime`、`build_api_runtime`、`create_app` identity和完整签名不变；三repository constructor patch identity、direct assignment与backend selector顺序不变。
- startup/shutdown、partial-startup/shutdown-first-error、Interrupt/Slot、file-selection和MCP recovery authority保持原call sites/kinds/counts/order；未建立第二owner。
- private remote continuation和memory builder使用窄ports；公开`ApiRuntime.__init__.storage: StoragePort`是唯一冻结的API aggregate compat annotation。
- P5只接管Storage/State/Lifecycle adapter、shared mapper/metadata、transaction/lock/CAS及真实PostgreSQL切片；不得把backend mode selector从P4复制进adapter，也不得回写P4 runtime控制流。
- schema/data、依赖、`prod`、Frontend、Rust和`docker_cmd.md`正文均未触及；License Requirement无变化。
