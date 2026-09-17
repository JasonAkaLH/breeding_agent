# Phase 7 破坏性迁移证据

- **日期**：2026-08-23
- **适用分支/环境**：`main`；本地受控开发环境，不是`prod`
- **证据状态**：closed
- **检查点**：P7-A `restore_proof_complete`；P7-B `migration_complete`；P7-C `complete`
- **P7-C原始最终代码检查点/tree**：`0babd5067561d0aed8cd2946943f51d73f2b99ce` / `fb50b02dd86d06ada6fa4820038c971f058c7cb7`
- **当前代码复验检查点/tree**：`af246a6f139144df385298126d5f8588b32572f8` / `db7e3b09e08275de57da305ae1f55a05846cb342`
- **破坏性迁移检查点/tree**：`0df2645e8b2b74ba44c4ff961e8ef5fd3b5db91d` / `b0b41c33eeb8f2cc4cd45267b68016cdcd4e8c0a`
- **当前决策**：AL-P7-01～10、FR-1～26和12类NFR均已闭合；统一同模型Agent Loop在`main`本地受控开发环境标记`complete`。
- **保密边界**：本文不记录DSN、credential、业务正文、仓库外绝对路径或可公开下载的backup引用。

## 1. P7-A Operator 与状态机

`src/storage/agent_schema_migration.py`和`scripts/migrate_unified_agent_loop_schema.py`提供单实例closed operator。P7-B开放
`report`、`backup`、`restore-check`、`apply`和`restore-all`。状态保存在仓库外权限`0700`的private root；receipt按
`reported -> backed_up -> restore_verified -> applying_sqlite -> sqlite_applied -> applying_postgres -> postgres_applied -> applying_sidecar -> sidecar_applied -> verified -> completed`
追加为`0600`不可变文件并以前驱SHA绑定。PostgreSQL apply持有advisory lock；不确定`applying_*`只允许精确target digest补发
`*_applied`，否则必须三backend `restore-all`。相同input digest的backup、restore-check和completed apply实际重复调用均返回原SHA。

Operator代码检查点：

- `36752d2`、`e43b2da`、`7109f50`：P7-A report/backup/实际restore、Sidecar readiness与exact retry输入复验基线；
- `0df2645`：删除DAG physical contracts，实现固定顺序apply、每后端前缀receipt、PostgreSQL advisory lock、post inventory/
  Agent digest/全表行数复验、completed exact retry复验与完成后成对restore-all。r3只保留为旧代码回滚证据；真实apply使用
  绑定本提交/tree的全新r4 report与backup set。

## 2. Canonical report 与 receipt chain

| 项目 | SHA / 状态 |
|---|---|
| report | `sha256:8079aedec551fa4036699b691fb093017cee543df2ce2d314781ead354ee82c7`；blockers为空 |
| `reported` / `backed_up` / `restore_verified` | `sha256:2120f0d845ba6b5556cd7f13b2ecfcb73d38c8df05a9c2ac4dee80d75a44b3c9` / `sha256:03eab787a6268edd979daff3cf6a9c2400f5cfc00f15f89858926d18ffd7b13b` / `sha256:f18703174882f593d0c4ceaa76a418f11d3fe1a6556be4dc36f987a8c2a3acda` |
| `sqlite_applied` / `postgres_applied` / `sidecar_applied` | `sha256:9fa4046e5deafee04ac234d08dadd53caf631e3a71729865cbe5f2e6c08d9504` / `sha256:81e0c4116e9c5a1cb487196d1c61f8e55d81b53e19af95ed6bec1fdb8c9158b1` / `sha256:d0b2daf4e8c571455adf152dd23e389c352b9c0883a643107bcc34c74908be63` |
| `verified` / `completed` | `sha256:a958cb3fb3a2a06695c9846ecc3359f59d4e76e71bb78b05be7dea0b6afb7445` / `sha256:714cb91ce601c67fc1d954c7f1b2ccea6d870b67f766c24bb4b68a2101f442a7` |
| backup set | `backup-set-8079aedec551fa40` / `sha256:f310462ee082540c80060505b49444e2efdfb00a1a1fd7a09dbf7df448d92999` |
| schema version | apply前SQLite/PostgreSQL/Sidecar均为`pre-p7`；apply后均为`agent-only-v1` |

Report只保存schema metadata、待删DAG对象、全表行数以及Agent表计数/数据digest，不保存业务值。Receipt中的
`backend_readiness`、`agent_storage`、`task_history`和`artifact_event`均为`true`。根`docker_cmd.md`仅验证
exists/ignored/untracked，未读取、移动、打包、跟踪或修改。

## 3. 仓库外 backup set

持久backup root和backup-set目录均为`0700`。所有普通文件均为owner单链接`0600`；manifest只含下列相对restore ref：

| Backend | 相对ref | 字节数 | SHA-256 |
|---|---|---:|---|
| SQLite | `sqlite.backup` | 27,832,320 | `sha256:477f78c64e1873c05e3b2ca93c6e43dacea897063eb14780875cfb1c6169fab7` |
| PostgreSQL | `postgres.dump` | 188,401 | `sha256:3166afd108d82e78c126065a1cee85ca74c58d16926364adb4596bb5a7f06e6c` |
| Runtime Sidecar | `sidecar.backup` | 204,800 | `sha256:7a0882d899db3f862741c8262e5789feb4352b22f5f216f6fffe9db79ac1badd` |

SQLite和Sidecar使用online backup API；PostgreSQL使用17版custom-format dump。写入采用O_EXCL/no-clobber、file与directory
fsync，并在restore前复验mode、owner、link count、size和digest。该backup set至少保留到P7-C全部通过且用户明确结束
rollback窗口；隔离restore临时目标不是唯一备份。

## 4. 实际隔离恢复结果

- SQLite：恢复到全新隔离文件，`integrity_check=ok`；schema digest、全表行数、Agent表计数/数据digest与report一致；Task
  history、Artifact和Event均包含在全表对照中。
- PostgreSQL：custom dump恢复到同一专用PostgreSQL 17实例内的全新隔离数据库；以repeatable-read read-only inventory
  复验schema、全表行数和Agent digest，结果与source report一致。未使用源数据库作为restore目标。
- Runtime Sidecar：online backup恢复到全新隔离文件；除SQLite integrity/schema/数据digest一致外，operator实际启动受审
  `maf-runtime-sidecar` binary并完成Version和Compatibility readiness，随后受控停止该进程。
- 应用层兼容：`tests.e2e.test_agent_loop_cutover`和`tests.api.test_agent_task_projection`证明`/graph`固定返回
  `required/hard`与`edges=[]`；StoragePort已不存在TaskEdge reader，因此不再使用已删除方法的spy。
- Exact retry：在已存在backup set和非空restore临时目标上重复同SHA `backup`/`restore-check`，返回原backup-set SHA和
  `restore_verified` receipt，未重跑restore或覆盖文件。

## 5. P7-B 自动回归与失败语义

P7-A恢复证明后，P7-B在同一受控PostgreSQL 17实例上执行实际migration，并在独立post-schema数据库运行Agent
transaction/storage-clock/fencing conformance 4项，零skip。P7-B聚焦88项、Storage 399项、API 436项、E2E 7项以及
core/lifecycle/orchestration 181项通过。Operator 13项覆盖完整apply、每段receipt、completed exact retry重新验证post
inventory、partial prefix禁止重跑、完成后restore-all、report/data/文件身份漂移、PostgreSQL/Sidecar部分失败、单实例锁和
输出脱敏。Proto用`reserved`保留已删除field number/name；真实capability内部Provider、Skill与MCP retry/timeout回归继续通过。

## 6. P7-C 完整门禁

最终代码检查点为`0babd50`/tree `fb50b02`。其中`06398b3`将Rust `h2`从0.4.14更新至0.4.16并闭合
`RUSTSEC-2026-0258`；`0babd50`修正真实MCP discovery smoke，使其创建并清理owner-bound Conversation/Task，不绕过Gateway
ownership。未新增依赖种类，许可策略无变化。

### 6.1 Canonical Backend 与真实 PostgreSQL

README列出的每条Backend命令均实际发现非零测试并成功：

| 域 | 结果 |
|---|---|
| compileall | `src tests`通过 |
| Core | 42项通过 |
| Storage | 439项通过、零skip；注入7个模块隔离真实PostgreSQL 17目标 |
| Lifecycle | 37项通过 |
| Integrations | 704项通过；2项Linux资源隔离用例在macOS按平台声明skip，并在`linux/amd64`当前源码容器逐项通过、零skip |
| Agent Skills | 209项通过 |
| Orchestration | 102项通过 |
| Main Agent | 16项通过 |
| MCP dispatch / MCP tool | 14项 / 15项通过 |
| API / E2E / Observability | 436项 / 7项 / 39项通过 |
| Scripts / Deployment | 62项 / 3项通过 |

真实PostgreSQL门禁覆盖schema manifest/reconciler、migration、app/migrator权限、transaction、MVCC/storage clock、Task lease/
fencing、rollout ledger、conversation与CP7聚合合同。七个P7-C临时测试数据库在证明完成后按精确名称删除；P7源/恢复数据库、
专用PostgreSQL容器和r3/r4持久备份保留。未访问或修改并行运行的其他PostgreSQL实例。

### 6.2 Frontend 与 Rust

- Frontend按顺序运行Vitest、typecheck、build：21个文件/307项测试通过，TypeScript检查和production build通过。一次并行运行
  因测试与build同时准备MathJax目录产生`EEXIST`，改为PRD规定的顺序执行后通过；未把该并发执行方式记为门禁结果。
- Rust统一脚本的`cargo_fmt`、`cargo_clippy -- -D warnings`、workspace/all-targets/all-features `cargo_test`和`cargo_deny`
  全部通过；`cargo_deny`的advisories、bans、licenses、sources四类检查均为`ok`，未使用`--skip-unavailable`。

### 6.3 真实 MCP 闭环

在`linux/amd64`当前源码容器、真实加密配置的隔离副本和真实网络下执行；原runtime volume保持只读来源，烟测副本在完成后删除。
安全摘要如下：

| 环节 | Closed evidence |
|---|---|
| Discovery | 1个available server；协议`2025-11-25`；5个Tool；scope正常关闭 |
| Selector | 显式绑定决策序列`call_tool -> finish` |
| Approval/Resume | 首次返回`approval_required`，`allow_once`被接受，随后恢复同一Task/Node authority |
| Ordinary Tool | 实际调用无参数只读capability discovery Tool；1个调用进入terminal，`dispatch_error=null` |
| Result Parser / Artifact | durable validated result完成投影，生成1个Task Artifact |
| 安全输出 | 仅记录上表闭合状态；credential、raw result、业务正文和可逆server引用均未输出 |
| Final | 真实MCP dispatch完成；同一代码检查点的7项AgentLoop E2E进一步证明MCP结果回到原Run并只发布一次final |

该项使用真实授权，不使用waiver。烟测脚本自身的owner-bound Task回归1项、Ruff和compileall通过。

### 6.4 静态删除与文档门禁

下列生产源码扫描结果为零：

```bash
rg -n "WorkflowPlan|WorkflowNodePlan|OrchestrationRequest|WorkflowProvider|WorkflowRouter|WorkflowExpander|WorkflowPlanValidator|CompletionPolicy|RuntimeReplanner|SoftSkillReplanner|main_agent\.respond|max_replans|max_dynamic_nodes|planner\.reasoning_delta|soft_skill\.reasoning_delta|main_agent\.output_(delta|final)|mcp_remote_task_continuation_plan|legacy_dag" src frontend/src native --glob '!frontend/src/**/*.test.*' --glob '!native/**/tests/**'

rg -n "TaskEdge|task_edge|task_edges|planner_replan_claim|root_node_id|criticality|dependency_type|retry_policy_json|timeout_policy_json|resource_class" src/core src/storage/sqlite src/storage/postgres native/crates --glob '!**/tests/**'
```

Proto仅在`reserved`声明中保留已删除field number/name；API DTO的`root_node_id=null`和`required/hard`是明示兼容投影；
destructive migration、historical docs和隔离测试中的旧名词不构成生产读取。Phase 7 evidence validator在`--require-closed`下通过，
active inventory、目录README、Phase PRD、backend索引、AGENTS和CHANGELOG均以当前Agent-only authority收口。

### 6.5 2026-08-24 当前代码收尾复验

在未修改业务实现、未重跑破坏性迁移且未接触`prod`的前提下，以`af246a6`/tree `db7e3b0`重新执行当前代码门禁：

- `compileall`通过；Core 42项、Storage 400项、Lifecycle 37项、Integrations 704项（另有2项macOS平台声明skip，继续由
  原P7-C Linux零skip证据覆盖）、Agent Skills 209项、Orchestration
  102项、Main Agent 16项、MCP Dispatch 14项、MCP Tool 15项、API 436项、E2E 7项、Observability 39项、Scripts
  62项、Deployment 3项全部通过；Storage的7个外部PostgreSQL环境skip未作为通过证据；
- 使用本机已有`postgres:17-bookworm`镜像启动精确临时容器，在7个隔离数据库中运行Agent storage、conversation delete、
  MVCC、legacy migration、rollout、rollout permission和CP7共61项真实PostgreSQL测试，零skip、零失败；容器与一次性数据库
  随后删除，未连接现有开发库或生产库；
- Frontend 21个文件/307项Vitest、typecheck和production build通过；build只报告既有large chunk warning；
- Rust统一`cargo_fmt`、`cargo_clippy -- -D warnings`、workspace `cargo_test`和`cargo_deny`通过；`cargo_deny`的
  advisories、bans、licenses、sources均为`ok`，重复依赖只产生policy允许的warning；首次受sandbox限制无法取得用户Cargo
  advisory cache锁，不计通过，获准使用同一统一脚本重跑后成功；
- Phase 6/7 `--require-closed` evidence validator通过；旧runtime/DAG配置与TaskEdge/DAG-only物理合同的生产源码扫描均为零。
  Phase 0～5的pre-cutover validator按Phase 0 PRD约束不在post-cutover HEAD恢复已删除DAG测试或重新宣称历史扫描集。

`af246a6`只修复Rust quality gate、锁文件和既有MCP SDK diagnostics兼容，不改变Agent Loop控制面、schema/proto、API或公开
MCP结果语义；因此P7-A/P7-B的r4备份、restore/apply receipt和`0babd50`受控真实MCP证据继续作为原始closed authority，
本次不伪造新的迁移、备份或外部MCP执行记录。

## 7. FR-1～FR-26 最终映射

| FR | 最终证据 |
|---|---|
| FR-1 | 7项全入口E2E及API 436项证明start/resume/recovery/cancel统一进入AgentLoopOrchestrator。 |
| FR-2 | model binding、context/compaction、MCP binding和final测试锁定同一edition。 |
| FR-3 | Agent model adapter与Loop multi-call测试覆盖原生calls及有序results。 |
| FR-4 | Agent Loop fault测试证明普通capability失败作为Tool result回模。 |
| FR-5 | final-output测试证明只有无call非空assistant sample完成。 |
| FR-6 | 长轨迹测试通过，旧轮次/replan上限生产扫描为零。 |
| FR-7 | 显式Skill/MCP E2E证明仅首次强制，随后回归普通循环。 |
| FR-8 | continuation/recovery测试及真实MCP approval恢复证明原Run/call续行。 |
| FR-9 | crash/no-replay/late-result测试证明不确定副作用不自动重放。 |
| FR-10 | multi-call deterministic wave测试证明安全并发与确定结果顺序。 |
| FR-11 | context/compaction测试证明摘要digest、有界suffix与原始items保留。 |
| FR-12 | final-output、API和E2E证明无第二模型finalizer且只发布一次。 |
| FR-13 | MCP集成全量、Linux隔离解析用例和真实MCP闭环证明现有安全链不退化。 |
| FR-14 | P6删除报告、零生产引用扫描和P7 physical删除共同证明无旧DAG恢复入口。 |
| FR-15 | Skill activation与Agent Skills 209项证明仅注入安全profile且不执行delegated脚本。 |
| FR-16 | Skill executor/Loop测试证明可信上下文注入且answer mode不生成独立finalizer。 |
| FR-17 | 三backend conformance、真实PG Storage 439项及Sidecar/Rust测试证明先持久化和原子outcome。 |
| FR-18 | multi-waiting continuation/recovery测试证明全部闭合后才继续采样。 |
| FR-19 | provider edition startup/native tool/required-choice门禁通过。 |
| FR-20 | MCP binding/resume测试与真实MCP Selector证明Router/Selector继承Run binding。 |
| FR-21 | API graph与Frontend 307项证明固定empty-edge invocation ledger。 |
| FR-22 | Observability 39项及API/SSE回归证明durable/transient边界与低基数指标。 |
| FR-23 | final-output原子测试证明唯一Artifact/Message/event/receipt producer。 |
| FR-24 | Tool catalog/preflight和Agent Skills测试证明安全public profile与完整预算。 |
| FR-25 | r4实际三backend migration、physical零引用扫描及真实timeout回归通过。 |
| FR-26 | 真实PG lease/fencing/concurrency与recovery测试证明单一Task lease语义。 |

## 8. 12类 NFR 最终映射

| NFR | 最终证据 |
|---|---|
| Provider与同模型 | edition启动门禁、binding spy、compaction/MCP/final路径测试通过。 |
| 一致性/原子性 | SQLite、真实PostgreSQL、Runtime Sidecar canonical conformance与fault matrix通过。 |
| 安全/隐私 | Tool/参数fail-closed、MCP worker隔离、投影与事件泄漏扫描、真实MCP安全摘要通过。 |
| Tool catalog | 完整schema预算、超限采样前失败、Skill public profile与MCP单Tool外层暴露测试通过。 |
| 上下文 | canonical payload上限、summary digest、suffix、CAS/restart和原始items保留测试通过。 |
| 性能/资源 | deterministic wave、backpressure、waiting零worker、无busy polling及Linux资源限制测试通过。 |
| Final唯一性 | Artifact/Message/event/receipt原子幂等与E2E唯一final通过。 |
| 恢复/no-replay | multi-waiting、approval、MRTR、remote、失租、late-result与真实approval resume通过。 |
| 可观测性 | durable/transient事件、完整指标、低基数label和安全审计引用测试通过。 |
| API/Frontend兼容 | API 436项、Frontend 307项、SSE/history/interrupt及empty-edge graph通过。 |
| 可访问性 | Frontend focus、keyboard、semantic status与refresh announcement回归包含在307项中并通过。 |
| 可维护性 | 单一AgentLoop/Invocation/Outcome职责、旧runtime和physical合同零生产引用、文档authority closed。 |

## 9. AL-P7 验收与边界

| 验收 | 状态 | 证据 |
|---|---|---|
| AL-P7-01 | pass | 三backend仓库外备份、实际隔离restore及Sidecar Version/Compatibility readiness。 |
| AL-P7-02～03 | pass | TaskEdge及DAG-only字段在三backend/proto/Rust中物理删除。 |
| AL-P7-04～06 | pass | Agent数据digest/行数保持、API固定投影、真实timeout与三backend parity通过。 |
| AL-P7-07 | pass | Canonical Backend、Frontend和全部required Rust gates通过。 |
| AL-P7-08 | pass | Active docs/index/evidence validator闭合。 |
| AL-P7-09 | pass | 真实MCP discovery/Selector/Tool/approval resume/Parser/Artifact/final证据闭合。 |
| AL-P7-10 | pass | 仅声明`main`本地受控开发完成，未部署或修改`prod`，不承诺旧DAG Task兼容。 |

## 10. Rollback 与保留项

任一backend停在partial prefix时，禁止继续apply或启动混合schema binary，只能使用r4 backup set按
Sidecar -> PostgreSQL -> SQLite顺序执行`restore-all`，并以正常revert恢复对应Phase 6代码。`restore-all`的三backend顺序、
部分失败不前进receipt、完成后完整恢复和exact retry均有operator回归。本轮真实destructive apply一次成功，因此没有对已成功
迁移的开发数据执行rollback。r4和此前r3持久备份继续保留，直到用户明确结束rollback窗口；完成P7-C不自动删除备份。

P7-C创建的七个隔离PostgreSQL测试数据库和MCP smoke临时volume已删除，均为可重建测试产物、无需恢复。专用PostgreSQL容器、
真实迁移source/restore目标和持久备份仍保留。根`docker_cmd.md`最终再次验证为存在、ignored、untracked，全程未读取内容；
`prod`未修改。
