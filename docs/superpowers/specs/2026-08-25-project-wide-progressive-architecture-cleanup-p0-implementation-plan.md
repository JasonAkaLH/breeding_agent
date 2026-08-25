# 全仓业务代码渐进式架构清理 P0 实施计划

## 状态与依据

- 日期：2026-08-25
- 分支：`main`
- 状态：规范基线`bafae8d`经3轮完整审查、2轮收敛修订，以`100/100`、`0 Blocking / 0 Major / 0 Minor`通过`document-perfectization`信心门；P0实施已在`3cf44b1`激活，Checkpoint A～F完成，Checkpoint G待开始
- 总设计：`docs/superpowers/specs/2026-08-24-project-wide-progressive-architecture-cleanup-design.md`
- 规范设计基线：`7b36cad70979aa4d5d6ded186dc00befa80d8054`
- 计划编写时 HEAD：`c3ee64dc8b35b998672cb5217281e425cc0656dc`
- 计划编写时 tree：`546a041d9a6ddf3d0fc73ad8790f4eff49005f55`
- 范围：只实施总设计 P0，建立完整 inventory、公开合同和高风险行为锁；不进入 P1，不修改业务实现
- 生产边界：不修改或部署 `prod`，不执行 schema/data migration，不连接生产服务，不读取 `docker_cmd.md` 正文

本计划执行用户已批准的渐进路线。P0 完成后只允许生成 P1 的独立实施计划；不得把 P1～P8 的结构迁移提前塞入本计划。

## 1. P0 完成声明

只有同时满足以下条件，P0 才能标记为 `complete`：

1. 在 P0 实际开始 HEAD 上重新取得 branch、commit、tree、clean/owned diff 和测试基线；不得直接复用设计期测试数字。
2. 完整 tracked code/config universe 与 inventory path 集合精确相等，`unclassified=0`。
3. 每个 `business_source` 都有且只有一个 P1～P7 source owner；P8只拥有finding处置与最终审计，不替代活跃源码的分层owner；跨计划 seam 有唯一 authority、兼容理由、禁止扩张规则和退出条件。
4. finding 只使用 `exact_duplicate|structural_candidate|reviewed_no_change|deferred_behavior`，每项有证据、owner 和退出条件；P0 不处理 finding。
5. Python 公开 imports、`__all__`、签名、module/object identity、关键 pickle 合同以及四条 `StoragePort` alias 已由直接断言锁定。
6. `StoragePort` 的 259 个 async method 名称、async 属性和 `inspect.signature` 被精确锁定；P0 不创建窄 port。
7. SQLite、PostgreSQL、RuntimeSidecar 三个 Python Agent repository 的公开路径、MRO/effective surface、共同支持 operation、已知 unsupported/lease 缺口与 P4 唯一 backend selection trace 已冻结。
8. Cancellation 的 off/shadow/enforce、client 缺失、错误、SQL/Sidecar 调用次数和顺序已冻结；尤其 enforce/no-client 保持当前 exact error，且下游写入为零。
9. Agent waiting/continuation、Lifecycle recovery、MCP Dispatch bounded edge、MCP Coordinator/Gateway、API startup/shutdown 和 file-selection authority 的逐场景 trace 可比较；逻辑 call-site ID 不绑定文件、函数名或行号。
10. Frontend 指定的附件、Interrupt、stale scope、CP7 late/unknown、cancel reconcile 和 artifact completion 行为已由既有或最小新增测试锁定。
11. 三个 P7 Rust workstream 的 root public surface 已枚举；六份 checked-in contract 与现有 export binary byte-for-byte 一致；memory/SQLite parity 与 SQLite reopen/durability 证据已登记。
12. Unified schema apply/receipt/restore-all、相关 CLI/help/error/exit contract 已由现有 Scripts 测试映射；P0 不修改 Scripts 业务代码。
13. PostgreSQL P5 profile 已列出真实 DSN、权限、并发和 skip=0 要求，但 P0 没有触及 PG 业务语义时不伪报真实 PG PASS。
14. Backend、Frontend、Rust 的 P0 适用本地门禁通过，目标测试不得出现未解释的零收集、失败或 skip。
15. P0 最终 diff 只包含最小 characterization tests、P0 ledger/inventory、索引与 CHANGELOG；`src/**` 业务实现、Frontend 非测试源码、Native 生产源码和 Scripts 业务代码为零变化。
16. P1 handoff 明确给出 StoragePort 基线、Cancellation 当前 trace、owner/seam 风险和 P1 允许处理/禁止处理的边界。

代码行数、lint 数或 snapshot 数量不是 P0 完成证据。

## 2. 严格范围

### 2.1 允许新增或修改

P0 ledger 与索引：

- `docs/superpowers/specs/2026-08-25-project-wide-progressive-architecture-cleanup-p0-baseline.md`
- `docs/superpowers/specs/2026-08-25-project-wide-progressive-architecture-cleanup-p0-inventory.tsv`
- 本实施计划、总设计状态、`docs/AGENTS.md`、根 `CHANGELOG.md`

最小 Python characterization tests：

- `tests/core/test_public_contract_compatibility.py`（新增）
- `tests/api/test_runtime_public_contract.py`（新增）
- `tests/api/test_user_mcp_runtime_wiring.py`（只补 composition trace 缺口）
- `tests/orchestration/test_public_contract_compatibility.py`（新增）
- `tests/orchestration/test_agent_loop.py`（只补 waiting trace 缺口）
- `tests/storage/test_agent_repository_contract.py`（新增）
- `tests/storage/test_rust_runtime_sidecar_contract.py`（只补 Cancellation trace 缺口）
- `tests/orchestration/test_agent_continuation.py`（只补 multi-waiting/locator trace 缺口）
- `tests/lifecycle/test_agent_run_recovery.py`（只补逐分支 recovery trace 缺口）
- `tests/capabilities/mcp_dispatch/test_selector_router_executor.py`（只补 public seam identity/count 缺口）
- `tests/integrations/mcp/test_dispatch_coordinator.py`（只补 Coordinator functional seam trace 缺口）
- `tests/integrations/mcp/test_user_mcp_gateway.py`（只补 Gateway side-effect order 缺口）
- `tests/api/test_user_mcp_aggregate_recovery_startup.py`（只补 lifecycle order/failure trace 缺口）
- `tests/api/test_mcp_runtime_registration.py`（只补 shutdown order 缺口）
- `tests/api/test_conversation_file_selection.py`（只补 bounded file-selection trace 缺口）
- `tests/scripts/test_migrate_unified_agent_loop_schema.py`（只补设计级顺序断言缺口）

Frontend characterization tests：

- `frontend/src/App.test.tsx`
- `frontend/src/api/taskEvents.test.ts`
- `frontend/src/domain/taskEvents.test.ts`

只有 audit 证明上述文件无法承载最小断言时，才允许在同一 owner 的既有测试目录新增一个语义命名的测试文件；必须先在 baseline ledger 记录原因，禁止新建通用 test helper、fixture framework 或 snapshot runner。

### 2.2 明确禁止修改

- `src/**` 所有业务实现；
- `frontend/src/**` 中的非测试 `.ts/.tsx/.css`；
- `native/**` 生产源码、Cargo/Proto/build.rs、fuzz target；
- `scripts/**` 业务与验证脚本；
- 现有 tests 的 fixture 架构、公共 helper 或与 P0 无关的断言；
- `.github/**`、Docker、依赖 manifest/lock、部署配置；
- Git-ignored 外部 `skill/`、`runtime/` 和生成物；
- `prod`、schema/data、真实用户数据、外部 MCP control service。

如果 characterization 无法在不改业务代码的情况下写出，登记为 P1～P8 的 pre-migration test prerequisite，并停止对应切片；不得在 P0 顺手重构业务实现以方便测试。

## 3. 最小交付物格式

P0 只提交两个 evidence artifact，不新建生成器或验证平台。

### 3.1 Inventory TSV

`2026-08-25-project-wide-progressive-architecture-cleanup-p0-inventory.tsv` 每个 tracked path 恰好一行，UTF-8、tab 分隔，固定列为：

```text
path	classification	reason_code	owner_plan	finding_ids	p0_disposition	project_exit_state
```

字段约束：

- `classification`：`business_source|test|contract_or_build_dependency|explicit_out_of_scope`；
- `reason_code`：只使用 `runtime_business|package_lifecycle_business|operational_business|behavior_test|contract_fixture|build_dependency|validation_dependency|documentation|deployment_config|vendored_or_generated|project_metadata|non_business_asset`，不写自由散文；
- `owner_plan`：业务源码只能是 `P1|P2|P3|P4|P5|P6|P7` 中一个；非业务项为 `NA`；P8 finding owner只记录在baseline finding register；
- `finding_ids`：无 finding 为 `-`，多个 ID 用逗号分隔；
- `p0_disposition`：`candidate|reviewed_no_change|test_lock|contract_dependency|out_of_scope`；
- `project_exit_state`：P0 时业务源码为 `pending`，非业务项为 `NA`；P8 只允许把业务项收敛为 `changed|reviewed_no_change`。

Inventory初始集合来自P0 start commit的`git ls-files`；后续每个checkpoint提交前按当前HEAD重新枚举，并把本计划新增的tests/docs/index行加入TSV。Baseline单独保留不可变的P0 start path set摘要；最终TSV必须与P0 final HEAD的tracked set精确相等。不得扫描ignored runtime或仓外目录；集合比较只使用临时目录，临时文件不提交到仓库。

### 3.2 Baseline Markdown

`2026-08-25-project-wide-progressive-architecture-cleanup-p0-baseline.md` 固定包含：

1. P0 start branch/commit/tree、工作树 owned/unowned 状态；
2. inventory 分类枚举、tracked set equality 与 owner uniqueness 结果；
3. public contract 表；
4. dependency/owner/seam 表；
5. behavior-lock matrix；
6. finding register；
7. PostgreSQL P5 profile；
8. gate records；
9. P1 handoff；
10. P0 未运行项及准确原因。

Gate record 只记 commit、scope、cwd、原生命令/CI run、平台、ran/fail/skip 和结论。禁止记录 credential、DSN、Tool 参数、raw result、用户正文或绝对外部 artifact path。

### 3.3 测试断言形式

- 公开合同使用 literal expected lists/maps、`inspect.signature`、`is`、`__module__` 和最小 pickle round-trip；
- 顺序合同使用现有 fake/spy/barrier 收集闭合 token 序列；
- call-site ID 使用 `entry/scenario + phase + callee operation + ordinal`；source location 只作为 ledger metadata；
- 不生成 Golden JSON framework，不增加通用 snapshot serializer，不对真实副作用执行新旧双跑；
- 测试中不得依赖行号、私有 helper 的物理文件位置或 wall-clock sleep。

## 4. Checkpoint 0：冻结 P0 实际起点

### 4.1 起点检查

执行 P0 前先重读根、`docs/`、`tests/`、`frontend/`、`native/`、`scripts/` 的 `AGENTS.md`，然后记录：

- `git status --short --branch`；
- `git rev-parse HEAD` 与 `git rev-parse HEAD^{tree}`；
- 将实际40位commit同时记录为baseline字段`p0_start_commit`；最终验证时把该值载入任务专用变量`P0_START_COMMIT`，空值或非40位小写hex必须失败；
- P0 start HEAD 相对设计业务基线 `c8da6cc…` 的业务/测试/CI/依赖 diff；
- 当前分支必须为 `main`；
- 用户已有变更的 owned/unowned path；
- `docker_cmd.md` 只核验存在、`0600`、ignored、untracked metadata，禁止读取正文。

若 P0 start HEAD 在 `src/frontend/native/scripts` 出现本计划外的新业务变化，先重新发现 inventory、合同和测试入口；不得以计划编写时的文件/数量覆盖当前事实。

### 4.2 基线命令

先运行最小 smoke，确认测试收集与环境可用：

```bash
conda run -n multi_agent python -m compileall -q src scripts tests
conda run -n multi_agent python -m unittest tests.core.test_contracts
conda run -n multi_agent python -m unittest tests.storage.test_sqlite_bootstrap
conda run -n multi_agent python -m unittest tests.orchestration.test_agent_loop tests.orchestration.test_agent_continuation
conda run -n multi_agent python -m unittest tests.lifecycle.test_agent_run_recovery
```

```bash
cd frontend
npm test -- --run src/domain/taskEvents.test.ts src/api/taskEvents.test.ts
```

```bash
conda run -n multi_agent python scripts/run_rust_quality_gates.py --run --only cargo_fmt
```

失败必须先判定为起点失败或环境缺口；P0 不得通过改变旧行为来消除失败。

### 4.3 Checkpoint 0 输出

创建 baseline Markdown 的起点与 gate-record 章节，但不提交空壳、占位符或未验证 PASS。该内容与 Checkpoint A 一起提交。

## 5. Checkpoint A：完整 inventory、owner 与 finding register

### 5.1 Tracked universe 分类

首次以P0 start commit的完整`git ls-files`建立分类；后续按3.1的current-HEAD规则增量对账，至少覆盖：

- `src/`、完整 `frontend/`、完整 `native/`、`scripts/`；
- `tests/`、`.github/`、Docker 与根级 Python/Node/Rust/构建/依赖配置；
- checked-in contracts、Proto、build.rs、workflow；
- docs、fixtures、assets 和明确生成/外部边界。

每一行必须根据真实用途分类，不能按后缀盲分。特别规则：

- 独立Frontend test文件、Rust integration tests与fuzz targets归`test`；含内联`#[cfg(test)]`的Rust业务文件仍按主用途归`business_source`，测试section只在baseline中标成content-scope note；
- `native/proto/**`、manifest/lock、checked-in contract、workflow归 `contract_or_build_dependency`；
- package lifecycle 实际调用的 `frontend/scripts/**` 运行/构建逻辑归 `business_source`，纯验证脚本按真实用途分类；
- Git-ignored 外部 `skill/` 不进入 tracked universe；
- `docker_cmd.md` 不进入 inventory，也不读取正文。

### 5.2 Owner 与 finding

每个`business_source`只指定一个P1～P7 source owner。目录映射是完整inventory的review责任，不授权对应计划改动超出总设计的职责；具体结构finding仍须单独证明。闭合映射为：

- `src/core/**` → P1；仅persistence/shared contract与Cancellation边界可进入P1结构修改，其他Core源码可以`reviewed_no_change`；
- `src/orchestration/**`、`src/capabilities/**` → P2；
- `src/integrations/**`、`src/mysql_engine.py` → P3；
- `src/api/**` → P4；
- `src/auth/**`、`src/state/**`、`src/lifecycle/**`、`src/storage/**` → P5；
- Frontend业务源码及被package lifecycle调用的`frontend/scripts/**` → P6；
- Native业务源码与根`scripts/**` operational business → P7。

Finding register 每项记录 ID、分类、paths、相似处、语义差异、行为风险、finding owner、退出条件。P8只拥有已证明的dead/duplicate finding和最终闭合审计；即使finding计划在P8处置，path的`owner_plan`仍保持其P1～P7分层owner。看似重复但承载error、lock、transaction、protocol或fallback差异的代码必须标`reviewed_no_change`，不能标exact duplicate。

### 5.3 Dependency 与 authority map

记录当前 exact imports 与 bounded edges：

- 四条 `StoragePort` alias；
- Lifecycle → P2 Agent recovery；
- P3 MCP Coordinator → P2 MCP Dispatch public selector/router/outcome；
- P5 Agent repository adapters → P2 Agent persistence models/enums/errors；
- P4 composition → P5 concrete repository construction/selection/injection；
- P4 bounded file-selection authority；
- Frontend App/Attachment/Task Runtime owner；
- Rust root declaration 与 private implementation dependency role。

每条 edge 包含 exact symbols、唯一 owner、兼容理由、禁止扩张规则、普通结构检查点的 expected delta 和计划退出点。

### 5.4 验证与提交

- tracked path set 与 TSV path set精确相等；
- duplicate path、空 classification、业务项 owner 非唯一、unknown enum均为失败；
- baseline 文档中 finding IDs 与 TSV 引用互相可解析；
- `git diff --check`；
- 提交前对暂存内容运行`git diff --cached --check`；
- diff 只能包含 inventory、baseline、计划索引和 CHANGELOG。

Checkpoint commit：

```text
docs(cleanup): inventory P0 source universe
```

## 6. Checkpoint B：Python 公开合同

### 6.1 Core 与 StoragePort

新增 `tests/core/test_public_contract_compatibility.py`，直接锁定：

- `src.core.contracts.StoragePort`；
- `src.core.StoragePort`；
- `src.storage.interfaces.StoragePort`；
- `src.storage.StoragePort`；
- 四条路径 `is` 同一 canonical object；
- 259 个 method 的精确名称集合、async 属性和 `inspect.signature`；方法定义顺序不是兼容合同；
- 不允许缺失、重复、额外 method 或 catch-all；
- Core 不导入 API、Storage 实现、SQLAlchemy、MCP transport 或 capability private module。

已有 `tests/core/test_contracts.py` 和 `tests/storage/test_sqlite_bootstrap.py` 保留，不重写其 helper。

### 6.2 API Runtime

新增 `tests/api/test_runtime_public_contract.py`，锁定：

- `src.api.__all__ == ["ApiRuntime", "build_api_runtime", "create_app"]`；
- 三条导出对象与定义模块对象 identity；
- `ApiRuntime.__init__` 与 `build_api_runtime` 完整参数顺序、kind、default 和 keyword-only 形状；
- routes实际消费的公开方法/属性，以及现有API测试实际替换的class/parameter monkeypatch seam；先在baseline枚举exact names，不snapshot所有下划线私有成员；
- 使用一次全新Python解释器subprocess验证首次import `src.api`不读取ApiRuntime应用级env/config且不构造runtime；只允许当前Core contract选择读取`MAF_RUST_CORE_MODE`与`MAF_CORE_LIFECYCLE_PYO3_MODULE`，其他`APP_|MAF_|MCP_` key读取失败；subprocess不连接网络、不启动服务、不输出环境值；
- `build_api_runtime` 仍在完整构造后赋值 runtime holder；测试只观察现有 seam，不调用外部服务。

FastAPI path/DTO/SSE 继续复用 `tests/api/test_route_contract.py`、现有 DTO 和 task-event tests；不新增完整 OpenAPI snapshot 文件。

### 6.3 Main Agent 与 Agent Loop

新增 `tests/orchestration/test_public_contract_compatibility.py`，锁定：

- `src.capabilities.main_agent.__all__` 的五个公开对象；
- `src.orchestration.agent_loop.__all__` 的完整 literal list、顺序和对象 identity；
- `AgentRun`、`AgentItem`、`AgentModelBinding`、`AgentCallOutcomeCommit`、`AgentContinuationLocator`、`AgentResumeKind`、`InvocationRequest`、`InvocationResult` 的 `__module__`；
- 对当前可 pickle 的公开 dataclass/enum 做最小 round-trip，并断言 class identity；不可 pickle 对象只锁 module/signature，不为 P0 改实现；
- public errors 的 class、message/code 和 retriable metadata keys 使用现有构造路径直接断言。

### 6.4 定向门禁与提交

```bash
conda run -n multi_agent python -m unittest \
  tests.core.test_contracts \
  tests.core.test_public_contract_compatibility \
  tests.storage.test_sqlite_bootstrap \
  tests.api.test_runtime_public_contract \
  tests.api.test_route_contract \
  tests.orchestration.test_public_contract_compatibility
```

预期：所有目标模块收集数大于零、失败为零、skip 为零。更新 baseline public-contract 表与 gate record 后提交：

```text
test(contracts): freeze P0 Python public surfaces
```

## 7. Checkpoint C：Agent repositories、composition 与 Cancellation

### 7.1 三种 Python Agent repository

新增 `tests/storage/test_agent_repository_contract.py`：

- 锁定 `SQLiteAgentRepository`、`PostgreSQLAgentRepository`、`RuntimeSidecarAgentRepository` 的公开 import path、`__module__`、constructor signature、MRO；
- 从 class/MRO 计算 effective public async method surface，使用 literal expected set；
- 列出三者共同且当前支持的 operation，并对相同 fixture 比较结果/error/call-count trace；
- unsupported/N/A 必须对应当前真实 contract 理由；
- 明确断言 RuntimeSidecar adapter 当前缺少 Agent lease methods，登记 `BEHAVIOR-SIDECAR-AGENT-LEASE-001`，不得补方法；
- SQL Agent repository 的独立 Session/`BEGIN IMMEDIATE`/CAS/commit/rollback/shield trace不与 `SQLiteStorage._run` 混合；
- State+Collaboration 继续共享同一 Session 与单一 callback/commit 边界。

真实 PostgreSQL 不在本 checkpoint 执行；PostgreSQL subclass/MRO/override surface 用静态与本地合同锁定，真实 operation profile写入 baseline供 P5 使用。

### 7.2 P4 composition 唯一 selector

在 `tests/api/test_runtime_public_contract.py` 和 `tests/api/test_user_mcp_runtime_wiring.py` 中用现有 patch seam 锁定：

- off/shadow/enforce 的 mode/evidence/client-availability check；
- SQL adapter 初选、enforce Sidecar 替换与 DI 各执行一次；
- enforce 缺 client/evidence 保持当前 exact failure；
- P5 adapter 不读取 mode、不选择 backend、不做 SQL fallback；
- concrete repository imports只用于 construction/selection/registration/DI。

### 7.3 Cancellation writer

只在现有 `tests/storage/test_rust_runtime_sidecar_contract.py` 补缺失 trace：

- legacy 与 AgentRun admission 入口分别保持；
- off、shadow、enforce逐场景记录 SQL write、Sidecar write、audit/shadow sink、error 和最终 task/node状态；
- client 缺失和 client error 的现有行为精确锁定；
- enforce/no-client 锁定 exact error，所有下游写入为零；
- 不把 Cancellation writer加入 259-method aggregate，也不定义 P1 Protocol。

### 7.4 定向门禁与提交

```bash
conda run -n multi_agent python -m unittest \
  tests.storage.test_agent_repository_contract \
  tests.storage.test_agent_storage_sqlite \
  tests.storage.test_agent_postgres_schema_contract \
  tests.storage.test_runtime_sidecar_agent_repository \
  tests.storage.test_rust_runtime_sidecar_contract \
  tests.api.test_runtime_public_contract \
  tests.api.test_user_mcp_runtime_wiring \
  tests.lifecycle.test_task_cancellation
```

本地目标失败/skip为零；需要真实 PG 的既有测试不得混进这个本地 PASS。更新 baseline 后提交：

```text
test(storage): freeze P0 repository and cancellation seams
```

## 8. Checkpoint D：Agent waiting、continuation 与 Lifecycle recovery

### 8.1 Stable logical call-site IDs

Baseline 为每个入口/场景记录：

```text
entry/scenario + phase + callee operation + ordinal
```

同时记录 kind、count、order 和当前 source-location metadata。一个逻辑 ID 对应两个实际调用点、调用减少或顺序改变都失败；owner 内一对一搬家只允许更新 location metadata。

### 8.2 Waiting 与 multi-waiting

在 `tests/orchestration/test_agent_loop.py`、`tests/orchestration/test_agent_continuation.py` 复用现有 fake，补足以下精确 trace：

- sample/result ownership → TaskNode running/started → capability → revalidation/events → waiting binding → interrupt/question/waiting event → outcome → waiting set → lease release；
- 一次 answer 只移除一个 call；remaining waiting 非空时 model=0、resume wave=0；
- waiting 清空后复用原 Run/model binding恢复，已执行 Tool/Capability不重放；
- locator cache miss从 durable carrier重建；
- 6.6 的 parallel、lease token、terminal continuation、authority snapshot 和 TaskNode preprojection偏差只锁定，不修复。

### 8.3 Lifecycle recovery 分支

在 `tests/lifecycle/test_agent_run_recovery.py` 为下列分支各建立独立 ordered trace，禁止压成统一链：

1. continuation preload后 duplicate/terminal在 acquire 前 `ack → return`；
2. normal active reserved：`acquire → reload → resolve → reload/fence → commit → ack → reload`；
3. post-resolve concurrent committed/terminal：`ack → return`，无新 commit、ack 后无 reload；
4. remaining waiting：`release_waiting`，model/resume=0；
5. waiting cleared：复用 handle `run_claimed`，无统一 release；
6. ack-loss retry沿同一 durable identity，Tool/Capability重放=0；
7. cancel race与final-candidate按当前 barrier outcome；
8. crash recovery：`reconcile/early terminal-or-waiting → acquire → abort outstanding → run_claimed`，resolver/ack=0。

每条 trace 同时断言 lease、writer、ack、model、Tool 和 storage call counts。

### 8.4 定向门禁与提交

```bash
conda run -n multi_agent python -m unittest \
  tests.orchestration.test_agent_loop \
  tests.orchestration.test_agent_continuation \
  tests.orchestration.test_agent_invocation \
  tests.lifecycle.test_agent_run_recovery \
  tests.api.test_agent_continuation
```

更新 seam 表和 gate record 后提交：

```text
test(agent): freeze P0 continuation and recovery traces
```

## 9. Checkpoint E：MCP 与 API 高风险 authority

### 9.1 MCP Dispatch bounded edge

用 `tests/capabilities/mcp_dispatch/test_selector_router_executor.py` 与 `tests/integrations/mcp/test_dispatch_coordinator.py` 锁定：

- P2 public executor、`MCPDispatchOutcome`、selector/router contracts 的 import/object identity；
- P3 Coordinator、Gateway、transport、`selector_context` 仍是 concrete owner；
- 普通、显式绑定、route-another-server、repair、rejection、approval/resume 场景的 selector/router/context/Outcome/LLM call-site IDs、kinds、counts和顺序；
- 任何普通结构迁移 expected delta 都是0；
- 不允许复制、内联、缓存或绕过后减少调用；
- 17个 fault boundary 继续证明原始 Tool/job-start 第二次调用为0，合法 poll/get/ack按 operation计数。

### 9.2 Gateway 与 Coordinator

只补现有覆盖没有直接断言的顺序：

- Gateway bootstrap 的 endpoint revalidation → credential read → adapter/client；
- call 当前真实的4次accepting guard（public admission 1次、execute内发送前/原始返回后/normalize后各1次）、registration callback、唯一 Tool send；
- Coordinator reservation → may-have-dispatched → terminal/no-replay；
- Historical reprojection 网络、credential、client调用均为0；
- raw、pending payload、projection、CP7 candidate、credential domains不合并。

使用确定性 fake/spy，不连接真实 MCP Server。

### 9.3 API lifecycle 与 file-selection

在现有 API tests 锁定：

- startup：sentinel → aggregate/dispatch/Agent recovery → Ready → post-ready work；
- shutdown：quiesce → cancel/gather → CP7 close → service close → engine dispose；
- `BEHAVIOR-API-LIFECYCLE-001` 的部分 startup 失败、shutdown 首错阻断后续 cleanup保持；
- P4 file-selection 的 candidate decision、selector LLM、attachment binding、TaskNode/Interrupt persistence、audit/durable event的 call-site IDs/kinds/counts/order；
- file-selection 不迁入 Slot/Orchestration，不产生第二 owner。

### 9.4 定向门禁与提交

```bash
conda run -n multi_agent python -m unittest \
  tests.capabilities.mcp_dispatch.test_selector_router_executor \
  tests.integrations.mcp.test_dispatch_coordinator \
  tests.integrations.mcp.test_mcp_dispatch_fault_injection \
  tests.integrations.mcp.test_user_mcp_gateway \
  tests.integrations.mcp.test_historical_result_reprojection \
  tests.api.test_user_mcp_aggregate_recovery_startup \
  tests.api.test_mcp_runtime_registration \
  tests.api.test_conversation_file_selection
```

本 checkpoint 不改 transport/adapter/runtime wiring，因此 Linux Result Parser与真实外部MCP smoke记为 `N/A: production path not touched`，不是 PASS 或 pending。更新 baseline 后提交：

```text
test(runtime): freeze P0 MCP and API authority traces
```

## 10. Checkpoint F：Frontend 行为锁

### 10.1 先复用现有覆盖

逐项把现有测试映射到 baseline：

- upload failure + keep-open Interrupt；
- 旧 generation/conversation/task/assistant 异步结果不得写入新 scope；
- CP7 unknown/late-result 不提前结束订阅；
- cancel request等待终态、missed SSE reconcile、stale event不覆盖terminal；
- terminal artifact加载完成后才清 runtime；
- reducer ignored/conflict/late/unknown 返回当前约定的 state identity；
- attachment upload/rollback/history/optimistic turn的现有顺序。

现有断言已经直接覆盖的场景不复制测试。只有缺少 exact call-count/order 或 stale-scope写入断言时，才在三个允许文件中增加最小 case。

### 10.2 不允许锁入修复性预期

P0 不增加文案、视觉、DOM、ARIA、focus、scroll、MCP菜单键盘、localStorage、API文本fallback或upload refresh改进断言。发现现存问题只写入 `deferred_behavior`。

### 10.3 定向门禁与提交

在 `frontend/` 工作目录运行：

```bash
npm test -- --run src/App.test.tsx src/api/taskEvents.test.ts src/domain/taskEvents.test.ts
npm run typecheck
npm run build
```

若没有新增 Frontend test，Checkpoint F 只更新 baseline，不制造空测试提交；若有新增断言，提交：

```text
test(frontend): freeze P0 async behavior locks
```

## 11. Checkpoint G：Rust 与 Operational Scripts 合同

### 11.1 Rust public surface

对 `maf_runtime_sidecar`、`maf_skill_runtime`、`maf_mcp_runtime` 当前 root-defined public const/type/enum/struct/free fn/async fn建立 ledger，记录：

- canonical crate-root path；
- item kind与完整公开签名；
- generics/where、visibility、`async|const|unsafe|extern ABI`；
- `cfg/cfg_attr`、`deprecated`、`must_use`等 outer attributes；
- root declaration、wrapper/assembly和private contract dependency的symbol role。

P0 不修改 Rust 源码，也不发明统一 export manifest。P7 计划以此清单选择具体 compile/type-name fixture。

### 11.2 六份 checked-in contract

运行现有 tests/export binaries，逐项验证 Core、Lifecycle、Runtime Sidecar、Skill Runtime、Safety、MCP Runtime checked-in JSON 与 canonical export bytes一致。不得更新 contract 文件来迎合测试。

同时登记：

- Runtime Sidecar memory/SQLite同 fixture parity；
- SQLite reopen/durability；
- Cargo dependencies/features/lock未变化；
- 当前 fuzz workflow遗漏 `mcp_runtime_protocol`，只作为 P7 MCP workstream 的既定 gate gap，不在 P0 修改 workflow。

### 11.3 Scripts

复用 `tests/scripts/test_migrate_unified_agent_loop_schema.py` 等既有测试，锁定：

- apply：SQLite → PostgreSQL locked mutation → Sidecar data → Sidecar semantic probe；
- receipt：`restore_verified → applying_sqlite → sqlite_applied → applying_postgres → postgres_applied → applying_sidecar → sidecar_applied → verified → completed`；
- restore-all：Sidecar data → PostgreSQL `pg_restore` → SQLite → Sidecar semantic probe → restored；
- CLI flag/help、env、role、stdout/stderr、error、exit、no-clobber、cleanup与fault prefix。

P0 不改 Scripts；若现有 test 对某一设计级顺序缺少直接断言，只在对应 `tests/scripts/**` 增加最小断言，并单独说明缺口。

### 11.4 定向门禁与提交

```bash
conda run -n multi_agent python -m unittest \
  tests.core.test_rust_contract_artifact \
  tests.lifecycle.test_rust_lifecycle_contract \
  tests.storage.test_rust_runtime_sidecar_contract \
  tests.integrations.agent_skills.test_rust_skill_runtime_contract \
  tests.integrations.test_rust_safety_contract \
  tests.integrations.mcp.test_rust_mcp_runtime_contract \
  tests.scripts.test_migrate_unified_agent_loop_schema
```

```bash
conda run -n multi_agent python scripts/run_rust_quality_gates.py --run --only cargo_fmt
conda run -n multi_agent python scripts/run_rust_quality_gates.py --run --only cargo_clippy
conda run -n multi_agent python scripts/run_rust_quality_gates.py --run --only cargo_test
```

P0未触及Rust业务、PyO3、packaging或fuzz源码，因此Ubuntu、manylinux、fuzz job记为 `N/A: production path not touched`。更新 baseline 后提交：

```text
docs(cleanup): record P0 native and script contracts
```

若确有新增 Scripts test，使用独立 `test(scripts)` commit，不与 ledger 提交混合。

## 12. Checkpoint H：P0 全量门禁与 P1 handoff

### 12.1 Backend canonical

按总设计第19.2节逐域运行，不用自建 runner：

```bash
conda run -n multi_agent python -m compileall -q src scripts tests
conda run -n multi_agent python -m unittest discover -s tests/core -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/lifecycle -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/integrations/agent_skills -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/main_agent -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/mcp_dispatch -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/mcp_tool -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/skill_tool -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/e2e -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/observability -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/scripts -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/deployment -p 'test_*.py'
```

每个命令单独记录ran/fail/skip，测试数以当次输出为准。`tests/integrations/agent_skills`没有`__init__.py`，不会被父目录discover递归收集，因此必须单独运行并单独记数。

Full suite中与P0 untouched production path无关、且明确声明为其他平台专属的既有skip，要逐项记录为该平台切片N/A，不能写成PASS；P0新增与本checkpoint目标测试的skip必须为0。

### 12.2 Frontend 与 Rust

```bash
cd frontend
npm test -- --run
npm run typecheck
npm run build
```

```bash
conda run -n multi_agent python scripts/run_rust_quality_gates.py --run --only cargo_fmt
conda run -n multi_agent python scripts/run_rust_quality_gates.py --run --only cargo_clippy
conda run -n multi_agent python scripts/run_rust_quality_gates.py --run --only cargo_test
```

### 12.3 P1 handoff

Baseline 文档最后冻结：

- 四条 `StoragePort` identity与259-method literal baseline；
- 每个 method当前 signature，但不在P0预先决定窄域；
- P1必须产出的 method→narrow-domain→owner-plan→consumer handoff约束；
- Cancellation current trace与独立non-aggregate边界；
- P1允许修改的Core contract/tests和禁止迁移的P2～P7 private helper；
- P1开始前必须重跑的定向 tests；
- 所有 `deferred_behavior`，明确禁止P1借结构迁移修复。

P1计划只有在P0最终commit clean、inventory/contract/trace闭合后生成；P0不得直接创建窄port或修改consumer。

### 12.4 最终审查与提交

- 每次提交前的`git diff --cached --check`均已通过；最终使用baseline中已校验的任务专用变量运行`git diff --check "${P0_START_COMMIT}..HEAD"`检查全部P0 committed diff，并再次运行`git diff --check`检查未暂存内容；
- 相对P0 start commit，生产业务路径diff为空；
- inventory仍与final tracked set相等；若P0新增tests/docs，必须将它们加入inventory并重新验证；
- baseline引用的test symbol真实存在；
- 无未决占位标记、无未解释PASS、无credential/raw/user content；
- 检查所有受影响 `AGENTS.md` 和 `CHANGELOG.md`；
- 只暂存本计划owned files。

最终提交：

```text
docs(cleanup): close P0 behavior baseline
```

P0状态改为`complete`；如果仅存在设计允许且未触及生产路径的平台N/A，不使用`platform_pending`。若P0实际修改了某个平台相关测试/contract且目标平台门禁无法运行，则对应切片为`platform_pending`，P0不得宣称完整完成。

## 13. PostgreSQL P5 profile

P0只定义P5必须取得的隔离真实PostgreSQL证据，不执行DDL或数据迁移。P5计划必须为受影响domain逐项映射：

- auth CAS；
- AgentRun/AgentItem/lease/atomic outcome；
- Task/Node CAS；
- mailbox、interrupt、event order；
- owner guard、claim takeover；
- rollout role separation与legacy migration role；
- conversation delete并发；
- fresh bootstrap与drift rollback。

真实门禁要求：明确non-prod DSN、隔离数据库/role、目标测试收集大于零、失败=0、skip=0、清理成功。日志和baseline不得写DSN或credential。缺少环境时P5对应切片不得开始，不能用SQLite/mocked PG替代。

## 14. Checkpoint 与 commit 顺序

| Checkpoint | 允许内容 | 必需门禁 | Commit |
|---|---|---|---|
| A | inventory、baseline起点、owner/finding | set equality、enum/owner检查、diff check | `docs(cleanup): inventory P0 source universe` |
| B | Python公开合同tests与ledger | Core/API/Orchestration contract tests | `test(contracts): freeze P0 Python public surfaces` |
| C | Agent repo/composition/Cancellation tests与ledger | Storage/API/Lifecycle定向 | `test(storage): freeze P0 repository and cancellation seams` |
| D | waiting/continuation/recovery trace tests与ledger | Orchestration/Lifecycle/API定向 | `test(agent): freeze P0 continuation and recovery traces` |
| E | MCP/API authority trace tests与ledger | MCP Dispatch/Integrations/API定向 | `test(runtime): freeze P0 MCP and API authority traces` |
| F | 必要Frontend最小断言与ledger | focused frontend、typecheck、build | 条件性 `test(frontend)` |
| G | Rust/Scripts ledger；条件性Scripts test | six contracts、Rust quality、Scripts定向 | `docs(cleanup)`；条件性 `test(scripts)` |
| H | final ledger、索引、CHANGELOG、P1 handoff | Backend/Frontend/Rust全量与final diff | `docs(cleanup): close P0 behavior baseline` |

每个checkpoint只暂存owned files并在commit前运行`git diff --cached --check`；不提交红测试，不把多个领域测试挤成一个巨型commit，不squash检查点。

## 15. 停止条件

出现以下任一情况，立即把当前检查点标为`failed`并停止该切片：

- 为写 characterization 必须修改业务实现；
- 公开 import/signature/module/object identity 与当前基线不一致；
- 测试暴露旧实现不满足总设计描述，且需要业务决策才能选择期望；
- trace发现SQL、lock、CAS、commit/rollback、LLM、Tool、network、worker或subscription调用数与现有测试/实现矛盾；
- 需要schema/data migration或真实生产连接；
- 目标测试零收集、失败或required平台skip；
- inventory无法给业务path指定唯一owner；
- 发现用户无关diff与owned文件冲突；
- 任何步骤需要读取、移动、删除或跟踪`docker_cmd.md`。

停止时只记录事实、最小复现和所属后续计划；不得把行为bug修复混入P0。

## 16. 回滚

- P0没有外部业务副作用、schema/data或生产资源；
- 未提交的owned文档/测试使用`apply_patch`反向修改；
- 已提交检查点按逆序`git revert`，不使用`git reset --hard`、checkout覆盖或工作树清理；
- 回滚测试提交不删除用户原有测试和fixture；
- 如运行隔离临时数据库或服务仅用于既有测试，按原测试清理流程关闭；清理失败必须报告且gate不记PASS；
- 回滚后再次核验工作树、业务diff、inventory tracked set和`docker_cmd.md` metadata。

## 17. 风险与控制

| 风险 | 控制 |
|---|---|
| Inventory手工遗漏或重复 | tracked set与TSV set双向精确比较；`unclassified=0` |
| 把相似代码误判exact duplicate | finding记录语义差异；涉及error/lock/transaction/protocol/fallback默认`reviewed_no_change` |
| 过度snapshot私有实现 | 只锁公开合同、设计列出的bounded seam和可观察副作用；不锁行号/私有文件位置 |
| Trace测试因时间并发不稳定 | 使用现有fake/spy/barrier，不用wall-clock sleep或真实外部调用 |
| Test-only工作变成新平台 | 只用直接unittest/Vitest/Rust assertions；禁止runner/schema/serializer/framework |
| P0提前做P1抽象 | 生产源码零diff；P1只接收method baseline与边界，不接收实现 |
| 已知bug被误修 | 6.6与Frontend延期项统一`deferred_behavior`，测试锁当前结果 |
| 外部平台证据被夸大 | 未触及production path的PG/Linux/manylinux/MCP标N/A；required但不可用才标pending |
| Ledger泄露敏感信息 | 只记安全ID/计数/状态/命令；禁止DSN、credential、raw、用户正文 |
| 长任务再次发散 | 每checkpoint严格owned paths与完成声明；相邻改善登记到owner plan，不实施 |

## 18. 需求追踪

| 总设计要求 | P0落点 |
|---|---|
| FR-01 | A inventory/owner/finding，H final tracked equality与P1 handoff |
| FR-02 | B Python public contracts，E API/MCP seam，G Rust/CLI contracts |
| FR-03～FR-04 | C～G最小characterization、call-count/order trace、禁止双跑 |
| FR-05 | A owner/DAG/bounded edge，D/E exact logical call-site IDs |
| FR-06 | B StoragePort 259-method baseline，C Cancellation trace，H P1 handoff |
| FR-07 | D waiting/continuation/Lifecycle recovery |
| FR-08 | E MCP public/concrete seam、Gateway/Coordinator/Historical |
| FR-09 | B ApiRuntime/factory，C unique selector，E lifecycle/file-selection |
| FR-10 | C三Agent adapter/transaction/session，13 PostgreSQL profile |
| FR-11 | F Frontend behavior locks |
| FR-12 | G Rust public/byte contracts与Scripts sequence |
| FR-13～FR-14 | A finding register；P0只登记不删除/修复 |
| FR-15～FR-16 | 14小提交，15停止，16回滚，H最终diff与缺口状态 |
| 全部NFR | 2严格范围、3最小证据、各checkpoint gate、17风险控制 |

## 19. P0 最终报告

完成后按 AI Slop Cleaner 格式报告：

```text
AI SLOP CLEANUP REPORT

Scope: P0 inventory、公开合同与高风险行为锁；业务实现零修改
Behavior lock: 新增/复用的具体测试、完整门禁及平台N/A/pending
Simplifications: N/A；P0只建立后续安全清理边界
Fallback review: grounded/masking/deferred findings及其owner
Changed files: inventory、baseline、最小tests、索引、CHANGELOG
Checks: 每条命令的PASS/FAIL/N/A与实际ran/fail/skip
Remaining risks: 未触及平台、仓外消费者、P1前置风险
Deferred work: P1～P8及所有行为修复finding
```

P0报告不得宣称业务代码已清理；它只证明后续结构迁移有可执行的行为和兼容基线。
