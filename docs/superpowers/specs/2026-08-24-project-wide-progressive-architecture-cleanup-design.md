# 全仓业务代码渐进式架构清理设计

- **日期**：2026-08-24
- **状态**：设计章节已获用户逐项批准；scope reset精简复审修订已完成，待新固定基线独立信心门复审
- **适用分支**：`main`
- **适用仓库**：`breeding_agent`
- **目标**：在可验证地保持现有功能、公开合同和副作用顺序不变的前提下，分阶段清理全仓业务代码中的单体模块、复制实现、无效抽象、死代码和错误的职责边界
- **实施方式**：P0～P8 系列计划；每个计划继续拆成可独立验证、提交和回滚的检查点
- **生产边界**：不部署或修改 `prod`，不执行 schema/data migration，不访问或输出 `docker_cmd.md`
- **设计边界**：总设计只规定职责 owner、不可变行为、阶段依赖和退出条件；具体文件拆分、测试符号、CI job、artifact 格式与命令编排在进入各计划时基于最新 HEAD 决定

## 1. 背景与结论

仓库在统一 Agent Loop、用户级 MCP、Result Parser、Rust Sidecar 和 PostgreSQL 状态平台连续演进后，已经形成较完整的行为与安全测试，但业务实现中出现了明显的结构性负担：

- `src/storage/sqlite/repositories.py` 约 16,895 行；
- `src/api/runtime.py` 约 13,878 行；
- `frontend/src/App.tsx` 约 3,814 行；
- `src/integrations/mcp/dispatch_coordinator.py` 约 3,510 行；
- `src/integrations/mcp/gateway.py` 约 2,382 行；
- `native/crates/maf_runtime_sidecar/src/lib.rs` 约 4,008 行；
- `src/core/contracts.py` 中单个 `StoragePort` 约 1,580 行、259 个异步方法；
- SQLite 同步 repository 约 252 个方法，异步 storage facade 约 265 个方法；
- Ruff 对后端与脚本给出 666 个复杂度信号，但这些信号只能作为审计入口，不能作为自动改写依据。

全仓只读审计确认：仓库同时存在高置信的死代码和复制实现，也存在大量看似重复、实际承载协议版本、安全 authority、错误码、事务、锁、幂等、恢复和兼容语义的代码。直接“大一统”重写无法满足功能不变要求。

本设计选择架构优先方案，但采用稳定门面和渐进接管，不做 big-bang rewrite：

```text
原调用方 -> 原公开模块/类型 -> 新的内部职责模块
```

原模块先作为兼容门面保留，新模块一次只接管一个职责。每个检查点全绿后才进入下一检查点；任何可观察行为漂移都回滚当前检查点。

### 1.1 用户、利益相关方与价值

本项目不增加用户可见功能。价值来自降低巨型模块和复制实现造成的回归面，同时保持现有产品行为稳定。

| 参与者 | 需要保持的结果 | 本设计提供的价值 |
|---|---|---|
| 最终用户 | 对话、附件、Interrupt、MCP、历史、长任务和恢复不丢失、不重复、不退化 | 每次只迁移一个职责，并以原行为锁验收 |
| Backend/Frontend/Native 实施者 | 修改一个职责时不必理解整个巨型模块 | 稳定 facade、唯一 owner、清晰依赖方向 |
| Storage 与可靠性维护者 | 事务、锁、CAS、取消、no-replay 和恢复顺序不漂移 | 高风险路径先 characterization，受影响平台真实验证 |
| Code reviewer | diff 可审、失败可定位、用户改动不被覆盖 | 小检查点、独立提交、明确停止与回滚 |

本项目不以 LOC、文件数、lint 数或抽象数量衡量成功；只有业务职责更清晰、重复实现收敛且受影响行为锁通过才算完成。

## 2. 现状行为基线

设计前只读基线绑定：

- `baseline_commit=c8da6ccdf89eed5851cb5a79385cf583560a3c93`；
- `baseline_tree=df3d2a21c8cee01990518999285894e1a57e24a2`；
- 分支为开发权威 `main`；其后的设计审查提交未修改业务代码。

基线结果如下：

- 基线采集时工作树干净；后续实施以各检查点记录的当前 `main` commit 为准，不依赖易变的 `origin/main` ahead/behind 数字；
- Backend canonical 域通过：Core 42、Storage 400、Lifecycle 37、Integrations 704（另有 2 项 Linux-only skip）、Agent Skills 209、Orchestration 102、Capabilities 45、API 436、E2E 7、Observability 39、Scripts 62、Deployment 3；
- Frontend 21 个测试文件（`frontend/src/` 20 个、`frontend/scripts/` 1 个）、307 项测试通过，`npm run typecheck` 与 `npm run build` 通过；
- Rust workspace tests、`cargo fmt --check`、Clippy `-D warnings` 通过；
- 本地 Git-ignored 的 `skill/sql-query` 不存在，对应外部 Skill 测试记为 N/A，不属于本仓业务代码完成证据；
- 本轮基线未重新运行真实 PostgreSQL profile，因此 P5 的 PostgreSQL 高风险检查点必须重新取得真实 DSN 证据；
- Linux-only Result Parser、manylinux wheel、真实 PostgreSQL 和真实外部 MCP 只在检查点实际触及对应平台语义时成为该切片的必需门禁；环境不可用时延期该切片并准确记录，不阻塞无关业务模块。

这些数字只是设计起点。P0 在实施开始 commit 重新发现 inventory、测试数量和公开合同；每个受影响检查点基于当时代码重新运行对应门禁，不能复用旧 PASS。

## 3. 范围

### 3.1 范围内

- `src/**` 中的业务 Python；
- `frontend/src/**` 中的非测试 TypeScript、React 与 CSS，以及被 package lifecycle 实际调用的 `frontend/scripts/**` 运行/构建逻辑；
- `native/crates/**/src/**` 中的非测试 Rust 与各 crate `build.rs`；
- `scripts/**` 中的业务、运维与迁移逻辑；验证/quality脚本只做兼容审计和必要适配，不作为普通去重目标；
- 为锁定旧行为而新增或定向调整的最小 characterization coverage；
- 因模块入口或职责索引变化而必须同步的 `AGENTS.md`、`CHANGELOG.md` 和设计/计划文档。

### 3.2 不作为清理目标

- `tests/**`、`frontend/src/**/*.test.*`、Rust `#[cfg(test)]` 与 fuzz targets；
- `docs/**` 的一般性文字清理；
- Docker、CI、部署配置和依赖升级，除非某个结构检查点无法在不更新路径的情况下完成；
- Git-ignored 的外部 `skill/`；
- `runtime/`、`target/`、`dist/`、`__pycache__/`、`frontend/public/vendor/` 等运行或生成物；
- 现有业务 bug、错误策略、安全策略、协议策略或用户可见行为修复；
- `prod` 部署、生产数据、生产凭据或生产观察窗。

### 3.3 测试代码边界

测试仅作为行为锁，不纳入去重、风格或架构清理。只有现有覆盖不足以证明结构迁移等价时，才新增最小 characterization test。不得借此重写 fixture 体系、统一测试 helper 或修改本应被锁定的旧行为。

P0 不从上述几条手写 glob 直接推导“全部业务代码”。它从完整 tracked code/config universe 开始，至少覆盖 `src/`、完整 `frontend/`、完整 `native/`、`scripts/`、`tests/`、`.github/` 及根级语言/依赖/构建配置，并把每项简单分类为 `business_source|test|contract_or_build_dependency|explicit_out_of_scope`，记录理由且 `unclassified=0`。`native/proto/**`、Cargo/Node/Python manifest/lock、checked-in contract与workflow属于contract/build/validation dependency：不作普通清理，但其变化必须进入受影响合同与构建检查。

## 4. 备选方案与决策

### 4.1 方案 A：安全等价小批次

只处理已明确证明的死代码、复制逻辑和无效抽象。风险最低，但不能充分拆解 `ApiRuntime`、Storage repository、Frontend App 和 Rust `lib.rs` 等结构性单体。

### 4.2 方案 B：架构优先渐进拆分（采用）

为每个单体保留稳定门面，通过一系列独立检查点逐项迁移职责。先搬家，再抽象；先锁行为，再删除重复。该方案能改善边界，同时保持每一步可验证、可回滚。

### 4.3 方案 C：一次性架构重写

理论上可最快减少文件体积，但会同时改变导入、装配、事务、状态和恢复路径，无法证明功能不变，因此拒绝。

## 5. 总体设计原则

### 5.1 稳定门面

- Python 旧 import、类名、函数签名、默认值和公开导出继续可用；
- Python 公开类型如受 `__module__`、pickle、repr 或 introspection 影响，优先保留定义原位；
- 当前定义在crate root的Rust public type、free function与`async fn`物理定义继续留在root；body可下沉到private helper，但只能由root同签名thin wrapper委托，不能仅以`pub use`替代。受影响item的`async|const|unsafe|extern ABI`、visibility、generics/where、`cfg/cfg_attr`、`deprecated`与`must_use`保持；
- Frontend 旧 `api/types.ts`、`domain/artifacts.ts`、`domain/taskEvents.ts` 继续 re-export 或保留公开 facade；
- `src.core.contracts.StoragePort`、`src.core.StoragePort`、`src.storage.interfaces.StoragePort`、`src.storage.StoragePort`四条公开路径必须以`is`指向同一个aggregate protocol对象。

### 5.2 先搬家，再抽象

一个检查点只做一种变更：

1. 原样移动职责；
2. 验证行为与顺序；
3. 在后续独立检查点收敛重复；
4. 最后删除已确证私有且无引用的旧实现。

不得在迁移时同时改命名、错误策略、fallback、数据结构或可见行为。

### 5.3 单一 authority

- Agent Loop 是 AgentRun、AgentItem、continuation identity 和调用顺序的唯一 authority；
- MCP Coordinator 是 durable aggregate、approval、continuation、terminal 和 no-replay 的唯一顺序控制者；
- MCP Gateway 是 endpoint、credential、client、外部 I/O 和 isolated parsing 的唯一 authority；
- Storage/State 是 session、事务、SQL、锁、CAS 和持久化的唯一 authority；
- Lifecycle 是 interrupt、cancel、mailbox 与 conversation guard 规则的唯一 authority；
- API 只负责装配、HTTP/SSE、DTO 和公开投影；
- Frontend reducer 先消费事件，controller 才执行终态副作用。

### 5.4 禁止双跑副作用

不得通过同时执行新旧实现来比较真实副作用，尤其禁止双跑 MCP Tool、上传、删除、发送消息、数据库写入或外部 provider 调用。等价性通过 characterization、fake/trace、digest、状态快照和单实现执行证明。

### 5.5 不新增框架

本轮不引入 Service Locator、动态 `__getattr__` facade、通用事件框架、Frontend 状态库、Context 架构或新的生产依赖。只使用仓库现有语言、库和装配模式。

### 5.6 功能需求

| ID | 必须实现的结果 |
|---|---|
| FR-01 | P0 对完整tracked code/config universe分类且`unclassified=0`；每个business path有唯一所属计划，最终状态只能是`changed|reviewed_no_change`，不得用抽样代替“所有业务代码”审查 |
| FR-02 | 公开 import、签名、类型/module identity、HTTP/SSE、序列化、Proto、PyO3、Frontend facade 与 CLI 合同前后不变 |
| FR-03 | 高风险职责在旧实现上先有最小 characterization；迁移后相同输入得到相同结果、异常和可观察顺序 |
| FR-04 | 新旧实现不得同时执行真实副作用；数据库、Tool、上传、消息、worker 与外部调用次数和先后不变 |
| FR-05 | 每个状态和副作用只有一个 owner；目标依赖方向无新增反向依赖或隐式 Service Locator |
| FR-06 | P1 保持四条`StoragePort`公开路径identity，建立无catch-all的窄persistence port映射和Cancellation边界；P2～P5采用最窄port |
| FR-07 | P2 保持 Agent Loop、waiting/resume、continuation、interrupt 与 lease 的现有 authority 和阶段顺序 |
| FR-08 | P3 保持 Agent Skills、Result Parser、Gateway、Coordinator、Historical 的独立安全边界、隐私和 no-replay |
| FR-09 | P4 保持 `ApiRuntime`/factory/patch seam，API 仅装配且 startup→ready→post-ready→shutdown 顺序不变 |
| FR-10 | P5 保持 Storage/State/Lifecycle 的 session、transaction、lock、CAS、metadata 与 PostgreSQL 专用语义 |
| FR-11 | P6 保持 Frontend state/effect owner、异步 scope guard、附件/Task 时序及 DOM/a11y 合同 |
| FR-12 | P7保持Rust public/contract/Cargo/PyO3/Proto与Scripts CLI/SQL/receipt/exit合同；四个workstream独立关闭 |
| FR-13 | P8 只删除已证明私有、零引用、无注册/import/pickle/spawn 副作用的 dead/duplicate code |
| FR-14 | Grounded 与 masking fallback 在结构迁移中保持现状；已知行为 bug 只登记，不借重构修复 |
| FR-15 | 每个检查点只迁移一个职责，运行受影响门禁、审阅 diff、独立提交并可单独回滚 |
| FR-16 | 最终无新旧双实现、无未解释反向依赖；平台或外部验证缺口准确标记，不伪报完成 |

### 5.7 非功能需求

| ID | 约束 |
|---|---|
| NFR-COMPAT | 公开对象 identity、wire/data bytes、错误分类与 legacy adapter 保持兼容 |
| NFR-RELIABILITY | no-replay、idempotency、cancel/cleanup、lease、恢复与终态收敛不因拆分改变 |
| NFR-DATA | 不做 schema/data migration；transaction/session/lock owner 与 commit/rollback 边界保持 |
| NFR-SECURITY | 安全 authority 不合并；外部测试只使用明确隔离的非生产环境，不向日志或证据写凭据、Tool 参数、raw result 或用户正文 |
| NFR-PERF | 不新增 LLM、网络、DB round trip、worker、subscription、timer 或完整 raw-copy 次数 |
| NFR-UX | Frontend 文案、DOM、ARIA、focus、scroll、portal、timer 和可见 loading/error 时序不变 |
| NFR-TEST | 测试只作行为锁；目标测试 0 项、非零失败或目标平台 skip 都不能算通过 |
| NFR-REVIEW | 变更小而可审、保留用户无关改动；不新增通用 gate/evidence 框架或生产依赖 |

## 6. 行为锁

“功能不变”按五层合同验证。

### 6.1 API 与类型合同

- import path、`__all__`、函数/方法签名、默认值和 keyword-only 形状；
- Python `__module__`、pickle round-trip 和公开对象 identity；
- Rust root exports、canonical type path、Cargo feature 与 public method；
- exception class、error code、retriable 分类、message 和安全 metadata keys。

### 6.2 数据合同

- DTO/dataclass 字段顺序、default、`slots/frozen/repr`；
- JSON key、排序、separator、UTF-8、digest 和 byte limit；
- durable event type、payload、event id 与顺序；
- checked-in Rust contract artifact 和 export binary 输出；
- ORM table/column/index/constraint、metadata identity 与 SQL 结果；
- MCP 五版 decoder、checkpoint、projection 和 parser revision。

### 6.3 副作用合同

- storage write、flush、rollback、commit 与 transaction owner；
- row/advisory lock、CAS、claim、lease 和 idempotency 顺序；
- Tool reservation、may-have-dispatched、唯一外部调用、terminal commit；
- interrupt、slot、continuation、visible message 和 outcome commit；
- startup reconciliation、Ready、post-ready remote work 与 shutdown；
- Frontend upload、rollback、history refresh、optimistic message、SSE、interrupt、cancel 和 artifact completion。

### 6.4 Frontend 合同

- DOM 层级、className、文案、ARIA、Popover/Drawer 挂载位置；
- focus、scroll、keyboard、random welcome mount/key；
- reducer 对 ignored/conflict/late/unknown event 的 state identity；
- generation、conversation、task、assistant scope guards。

### 6.5 兼容合同

- legacy transport、协议 adapter 和五版 Result Parser 不合并；
- Python 兼容 alias 和 facade 不在无外部调用证据时删除；
- Proto、PyO3 module/function、JSON envelope 和 checked-in contract 路径不变；
- grounded fallback 原样保留；masking fallback 在结构迁移中也先保持旧行为。

### 6.6 已知现状偏差：锁定而不修复

下列行为可能不理想，但已存在且会被本次结构迁移触及。对应计划必须先用确定性 barrier/fault characterization 锁住当前 outcome、异常和副作用；修复需另立行为变更任务。

| Finding ID | 当前必须保持的边界 | Owner plan |
|---|---|---|
| `BEHAVIOR-ORCH-PARALLEL-001` | parallel capability wave 的 sibling cancel/异常收口按各现有入口保持，不统一成新 gather 策略 | P2 |
| `BEHAVIOR-ORCH-LEASE-001` | heartbeat 旋转与 Invocation 持有旧 lease token 的现状保持，结构迁移不顺手改 token 传播 | P2 |
| `BEHAVIOR-ORCH-TERMINAL-CONTINUATION-001` | terminal status、active sample、result identity 与 ack callback 的现有非对称矩阵保持 | P2 |
| `BEHAVIOR-ORCH-AUTHORITY-SNAPSHOT-001` | 各 resume 入口在 Agent lease 前读取/claim authority 的当前边界和锁序保持 | P2/P4 |
| `BEHAVIOR-ORCH-TASKNODE-PREPROJECTION-001` | TaskNode 第一阶段可先可见、Agent outcome 第二阶段失败时不做新增补偿 | P2 |
| `BEHAVIOR-PARSER-CLEANUP-CANCEL-001` | Result Parser cleanup 中的可取消 join/terminate/kill 阶段按当前路径保持，不新增 shield/finally | P3 |
| `BEHAVIOR-API-LIFECYCLE-001` | startup 部分失败和 shutdown 首错阻断后续 cleanup 的现状保持 | P4 |

## 7. 目标依赖方向

### 7.1 编译期/import方向

下图中`A -> B`表示A可以import/依赖B；禁止新增反向依赖。基线中已存在的反向边只有在P0列出exact symbols、唯一owner、兼容理由、禁止扩张和退出条件后，才能作为bounded compatibility edge保留；完成条件是“无未解释反向边”，不是假设基线已为零。

```text
API composition/routes -> Orchestration -> Capabilities -> Integrations
each consumer -> Core contracts / narrow Lifecycle and Storage ports
Lifecycle implementation -> Core contracts / narrow Storage ports
Storage implementation -> Core contracts
```

Storage/Lifecycle实现不得import API/SSE、Tool选择或Agent sample；API不得import具体repository或取得slot/transaction authority。

已知MCP Dispatch bounded edge：P2 Capabilities继续拥有`src.capabilities.mcp_dispatch`公开executor、`MCPDispatchOutcome`、selector/router及其models/contracts；P3 Integrations拥有concrete Coordinator、Gateway、transport、`selector_context`与shadow-compare integration glue。P3当前对上述P2公开对象的imports为保持module/object identity的有限例外；P0冻结exact symbol set，P2/P3不得新增跨边状态或I/O。P8可保留有理由的兼容例外，若要消除则另立获批计划并先定义public facade迁移，不能在本次结构检查点临场移动类型。

后端层级目标：

| 层 | 应拥有 | 不应拥有 |
|---|---|---|
| Core | 稳定共享模型、证据 primitive、窄协议 | capability 专属流程、外部 I/O |
| Lifecycle | interrupt/cancel/mailbox/guard 规则 | SQL、MCP transport、Agent sample |
| Storage/State | session、SQL、事务、锁、CAS、backend parity | API/SSE、Tool 选择 |
| Orchestration | AgentRun/Item、continuation、catalog、调用顺序 | Skill schema、具体 slot 持久化 |
| Capabilities | Skill/MCP 执行入口与领域 adapter | API 装配、外部 credential store |
| Integrations | 协议、client、Gateway、parser、安全 authority | AgentRun 状态机、API route |
| API | runtime 装配、HTTP/SSE、DTO、公开投影 | repository 与 slot 业务实现 |

Frontend import方向：

```text
App shell -> Controller Hooks -> API client / Domain -> Wire Contracts
App shell -> Presentational Components -> Domain / Wire Contracts
```

Controller Hooks与Presentational Components是App下的兄弟消费者；组件不得反向拥有App/controller state、API或subscription。

Rust import/call方向：

```text
crate-root public wrappers -> config/serve / service
config/serve -> codec/gRPC -> service
service -> kernel + backend adapter -> private contract/validation
```

### 7.2 运行时事件与数据流

下图箭头只表示控制/数据传播，不表示import：

```text
Orchestration / Capability / Integration
  -> Storage transaction + durable event/projection
    -> API public projection / SSE
      -> Frontend reducer
        -> Controller terminal effects
```

Runtime flow不能用来证明compile dependency；Storage写入event/projection不等于Storage依赖API/SSE。

## 8. 系列计划总览

| 计划 | 主目标 | 风险级别 |
|---|---|---|
| P0 | 全业务源码 inventory、公开合同与高风险行为锁 | 低 |
| P1 | Core persistence子协议、StoragePort兼容与Cancellation边界 | 低 |
| P2 | Orchestration/Capabilities所有权、continuation、Interrupt边界与Prompt | 中 |
| P3 | Agent Skills、Result Parser、Gateway、Coordinator | 中到高 |
| P4 | `ApiRuntime` 内部组件与 factory | 中到高 |
| P5 | Storage/State/Lifecycle 与 SQLite/PostgreSQL parity | 高 |
| P6 | Frontend App、reducers、controllers | 中到高 |
| P7 | Runtime Sidecar、Skill Runtime、MCP Runtime、Operational Scripts四个独立workstream | 中 |
| P8 | 已确证死代码/重复收尾与全仓最终证明 | 低到高，按项分开 |

结构工作按 `P0 → P1 → … → P8` 渐进推进。每个计划在进入实现前基于当时 HEAD 生成详细实施计划，文件名、测试符号、命令和平台 job 到那时才冻结，避免预先制造失真的实现约束。

计划状态只使用：

- `pending`：前置未完成；
- `active`：当前正在实施；
- `local_complete`：本地结构与门禁完成，但明确列出不影响后续模块的条件性平台切片；
- `platform_pending` / `external_pending`：本计划实际触及的平台语义尚未验证，该切片不得宣称完成；
- `complete`：本计划全部适用切片闭合；
- `failed`：行为或合同漂移，当前检查点已停止。

平台缺口不允许被写成 PASS，也不应阻塞不依赖该平台的后续业务模块。例如 P5 的 PostgreSQL 高风险切片可延期，同时继续 P6 Frontend；最终项目完成声明仍须逐项列出未闭合切片。

## 9. P0：行为与兼容基线冻结

P0 只增加测试与证据，不修改业务实现。

### 9.1 Python

- snapshot `src.api`、`src.capabilities.main_agent`、`src.orchestration.agent_loop` 等公开导出；
- 冻结 `ApiRuntime`、`build_api_runtime` 签名、默认值、属性与 patch seam；
- 冻结 `StoragePort` 259 个方法的名称、async 属性和 `inspect.signature`；
- 冻结关键 Python 类型的 module identity 与 pickle；
- 为 Agent waiting/continuation、MCP Coordinator/Gateway、startup/shutdown 建立 side-effect trace。

### 9.2 Frontend

- 锁定 upload + keep-open interrupt；
- 锁定旧 generation/conversation 的异步响应不可写入新 scope；
- 锁定 CP7 unknown/late-result、cancel reconcile 和 artifact completion；
- 不为现存 UI bug 增加修复性断言。

### 9.3 Rust

- 冻结 root imports、public signatures、serde contract、error strings 和 Cargo dependencies；
- 枚举受影响crate当前root-defined public type/free/async fn及outer attributes；冻结其canonical def/function-item/future identity，具体compile/type-name fixture下沉P7实施计划；
- byte-level 比较 Core、Lifecycle、Runtime Sidecar、Skill Runtime、Safety、MCP Runtime 六份 checked-in contract 与现有 export binary 输出；
- 建立内存/SQLite 同 fixture parity，SQLite reopen/durability 保持 backend-only 断言。

### 9.4 PostgreSQL

P5 前明确真实测试 profile：auth CAS、AgentRun/AgentItem/lease/atomic outcome、Task/Node CAS、mailbox、interrupt、event order、owner guard、claim takeover、rollout role separation、legacy migration role、conversation delete 并发、fresh bootstrap 和 drift rollback。

### 9.5 P0 最小交付物与证据边界

P0 只建立完成本次清理所需的最小账本，不建设新的通用 gate/evidence 平台：

- 完整tracked code/config分类表，以及business source inventory的path、语言、所属计划、finding IDs、最终`changed|reviewed_no_change`；
- 受影响公开面的兼容快照：import/signature/identity、HTTP/SSE、wire/data、Rust/Frontend/CLI contract；
- 高风险职责的最小 side-effect/transaction/lock/cancel characterization；
- 当前依赖方向与唯一 owner map；
- finding register：`exact_duplicate|structural_candidate|reviewed_no_change|deferred_behavior`，每项有理由和退出条件；
- 简洁 gate record：被测 commit、scope、命令或 CI run、工作目录、平台、ran/fail/skip、结论或未运行原因。

Artifact 格式由 P0 实施计划选择最少可维护形式；不得新增通用 launcher、typed argv、artifact-root protocol、证据自散列平台或为了验证证据系统而形成第二套大型测试框架。使用现有测试命令和 workflow，在正确模块目录运行；外部 gate 使用明确非生产、隔离配置，输出不得包含凭据、Tool 参数、raw result 或用户正文。

P0 退出时必须证明 inventory 无遗漏、所有计划 owner 唯一、公开合同可比较、即将进入 P1 的行为锁已在旧实现上通过。P0 不修改业务实现。

## 10. P1：无状态基础层

P1只建立Core/shared persistence contract与Cancellation边界，不迁移有状态控制器，也不提前接管P2～P7的领域私有helper。

### 10.1 StoragePort 子协议

将259个persistence方法逐一映射到真实窄域和后续采用计划，至少覆盖：

- auth、credential/security/master-key；
- conversation与collaboration；
- task/node/mailbox/interrupt/cancel；
- artifact/event/projection/checkpoint；
- MCP config/dispatch/CP7/durable result/remote task/rollout。

`aggregate compatibility`只是一层公开facade，不是方法ownership bucket；无法归入窄域的方法必须有具体兼容理由，不能进入catch-all。具体模块名由P1实施计划决定。上述四条公开路径继续re-export同一canonical对象；259个方法无缺失、重复或签名变化。

P1输出一张port→owner-plan→consumer handoff：P2～P5在迁移各自业务职责时把新增和内部消费者改到最窄port；公开签名、明确compat seam可继续引用aggregate。P1允许port定义在采用计划前短暂存在，但P8/project exit必须证明无未解释的生产内部aggregate consumer。

### 10.2 Cancellation writer 边界

Cancellation Sidecar writer 是独立的 non-aggregate port，不塞入 259-method `StoragePort` union。旧 AgentRun 与 legacy 路径仍分别经过各自 admission；off/shadow/enforce、client 缺失、错误、SQL/Sidecar 调用次数和先后保持当前 characterization。尤其 enforce/no-client 必须保留当前 exact error，且下游写入为零。具体 Protocol 形状与 test double 在 P1 实施计划中确定。

### 10.3 P1 退出条件

四条公开`StoragePort`路径identity相同；259个方法在窄域映射中恰好一次且签名不变；Cancellation writer未污染aggregate；Core/Storage/Lifecycle contract tests通过；handoff表为每个窄port指定实际采用计划。Capability、Skills、MCP、API、Frontend与Rust私有helper分别留在P2、P3、P4、P6、P7，只有其owner计划能抽取和接线。

## 11. P2：Orchestration 与 Capabilities

P2 的目标职责是：

- Agent Loop 继续唯一拥有 AgentRun/Item、Invocation、lease、waiting 与 continuation 顺序；
- 只合并 event material、generator/repair control loop 完全相同的私有 helper，协议 parser 仍独立；
- invocation-local continuation locator cache只有一个owner，但它不是durable authority；restart/cache miss必须从Interrupt carrier、Agent result与remote binding等现有durable carriers重建；
- 通用 `InterruptAuthorityPort` 只定义边界，Skill slot、MCP approval 与 API 通过薄 adapter 交接，不复制状态；
- Main Agent prompt wrapper 可委托通用 profile/envelope，但公开 import、输入和输出不变。

具体模块名、迁移批次和 adapter 类型由 P2 实施计划决定；dead code 统一留到 P8。

### 11.1 Agent waiting 顺序

实现计划先从旧代码生成 exact trace；总设计固定以下阶段边界：

1. sample/tool-call/reserved-result 持久化并校验 ownership；
2. TaskNode 进入 `RUNNING` 并发布 started；
3. capability 执行，随后再次校验 ownership 并提交 capability events；
4. TaskNode 进入 waiting，绑定 interrupt/slot/continuation authority；
5. 保存 interrupt，发布可见问题与 waiting event，再提交 call outcome；
6. 更新 waiting set 并释放 lease。

Dependency waiting与terminal continuation不得把不同状态的异常、ack、result identity或lease边界统一成新快路径。任何抽象不得跨越或重排这些阶段；6.6的五项Orchestration现状偏差必须由P2 characterization覆盖。

### 11.2 Multi-waiting 与 resume

- 一次answer只移除被回答call；其他waiting call及其identity保持；
- remaining waiting非空时不启动model（model调用=0），也不恢复后续wave；
- 全部waiting关闭后才恢复remaining waves，保持同一Run/model binding且不重放已执行Tool/Capability；
- atomic outcome提交后才ack；response loss/restart沿同一durable identity收敛，不能用新call掩盖旧状态。

### 11.3 跨计划 owner 交接

| Owner | 唯一职责 |
|---|---|
| P2 Orchestration | initial missing-input bootstrap、Agent/Interrupt协调、waiting/resume顺序 |
| P3 Integrations | Slot carrier、transition与question domain；不取得AgentRun authority |
| P2/P3 MCP Dispatch | P2拥有public executor/outcome/selector-router contracts；P3拥有concrete Coordinator/Gateway/transport；现有P3→P2 imports只按7.1 bounded edge保留 |
| P4 API | HTTP answer/cancel/recovery adapter；只装配和投影 |
| P5 Storage | Slot/Interrupt durable CRUD与CAS；不决定resume顺序 |
| P2/P5 Agent persistence | P2拥有AgentRun/AgentItem/lease/atomic-outcome语义与ports；P5拥有SQLite/PostgreSQL repository、Session/SQL/CAS实现 |
| API file-selection | 保持独立业务路径，不错误复用Slot authority port |
| Lifecycle | durable Agent recovery identity与conversation/task guard |

P2退出条件：公开Orchestration/Capability imports不变；waiting/resume/terminal与multi-waiting focused trace前后相同；Tool/LLM/Storage调用次数不变；locator cache可从durable carriers重建；不存在第二套interrupt/continuation authority；MCP Dispatch bounded symbol set未扩张。

## 12. P3：Integrations

### 12.1 Agent Skills

目标依赖方向：

```text
public facade
  -> missing-input presentation + execution
    -> resolution + slot_state
      -> contract/schema/value/slot_contract
```

合同/schema/value、resolution/slot state、missing-input presentation/execution 与 public facade 分层，但具体模块名由 P3 实施计划决定。公开 dataclass/service 的定义与module identity保持；execution-only raw metadata仍只在执行authority内可达，prompt/public event/log不得获得Tool参数、附件正文或raw result；schema-load masking fallback原样保留。

Legacy artifact context兼容顺序固定为`skill_artifacts → uploaded_artifacts → artifacts`的truthy选择，且必须“先选择key、后sanitize”：选中的truthy值若sanitize后为空，直接使用`fallback_artifact_context`，不能继续尝试后续key。P3实施计划为三key、falsey下探与truthy-but-sanitizes-empty补最小characterization。

### 12.2 Result Parser

- 五个 `decoder_YYYY_MM_DD.py`、registry、worker entry、checkpoint和parser revision保持原位，不合并成配置驱动的大一统parser；
- service可把process supervision作为一个职责搬出，但spawn/pickle identity、first/second message阶段、timeout、terminate/join/kill、gate release与projection soft-failure语义保持；
- 父进程仍只接收合同上限内的checkpoint和bounded projection，不读取raw；
- `BEHAVIOR-PARSER-CLEANUP-CANCEL-001`用barrier锁定，不在结构迁移中修复。

只有检查点触及worker、resource limit、regex timeout或cleanup路径时，Ubuntu Result Parser门禁才是该切片required：运行现有两个Linux-only目标测试和相关MCP integration suite，目标skip为0。Job、Python版本、命令、artifact上传等细节由P3实施计划复用现有workflow，不在总设计建设新runner平台。

### 12.3 MCP Gateway

`MCPGateway`保持公开facade和单一共享state。catalog/metrics等无状态逻辑可先搬；scope lifecycle与call lifecycle可作为两个内部职责，但不得复制map/lock或把mixed lock/event-loop字段改成单域。Gateway继续独占endpoint revalidation、credential read、adapter/client、external I/O和isolated parsing；bootstrap安全顺序、call的两次accepting检查、registration callback与Tool send先后按旧trace保持。

### 12.4 MCP Coordinator

最高风险、最后处理。先在原类内把preparation、dispatch、continuation、terminal收敛成明确phase，再决定是否搬入私有collaborator。Coordinator始终是唯一顺序控制者；reservation、may-have-dispatched、unknown convergence和no-replay不得复制到Gateway、recovery或helper。现有17个operation fault boundary用确定性本地adapter spy和durable restart覆盖：原始Tool/job-start第二次调用为0，合法poll/get/ack按operation单独计数。

### 12.5 禁止合并的安全 authority

- Temporary Result Store；
- Pending Action Payload Store；
- Projection Store；
- CP7 terminal candidate；
- credential/master-key domains；
- historical raw resolver/managed copy。

它们的 key、AAD、size cap、mode、identity、no-clobber、exception 和 paired snapshot 均为独立合同。

### 12.6 Historical 与条件性真实烟测

Historical reprojection保持raw/managed source优先级、分页边界和零网络/零credential/client依赖；历史重投不能借重构重新联系外部Server。

只有检查点实际修改MCP transport、adapter或runtime wiring，且已有隔离non-prod endpoint时，才运行薄真实烟测：discovery、普通调用、approval/resume、artifact parse与cleanup。没有现成环境时标记该切片`external_pending`，不得为本清理任务新建control service、fault-token/counter协议或SPKI测试平台；no-replay的阻断权威仍是本地确定性fault/restart测试。

P3退出条件：五个domain公开合同不变；Skills隐私、Parser阶段、Gateway state/security、Coordinator no-replay与Historical zero-network focused tests前后相同；不存在合并后的安全authority或第二套external I/O owner；7.1 MCP Dispatch bounded edge的imports/identity未扩张且无未解释reverse edge；适用的Linux/真实烟测已通过或准确标记pending。

## 13. P4：API Runtime

`src.api.runtime.ApiRuntime`、`build_api_runtime`、`src.api.__init__` 导出及测试 patch seam 保持稳定。

内部按 bootstrap/factory、files/attachments、conversation/history、interrupt、lifecycle 五类职责渐进接管；具体模块、mixin或composition由P4实施计划决定。配合P1改用窄port，但routes/runtime只获得实际需要的方法。公开model类只有在module/pickle合同可证明时才物理迁移，否则声明原位。

### 13.1 关键限制

- env/config 不能在 import 时提前读取；
- helper 仍在原调用位置执行；
- factory 保留现有显式 class/parameter monkeypatch seam；
- `build_api_runtime` 公开签名暂不改成新 config object；
- runtime holder 仍在完整构造后赋值；
- master-key sentinel、aggregate reconciliation、dispatch recovery、Agent recovery、Ready 和 post-ready remote task 的顺序不变；
- shutdown 的 quiesce、cancel/gather、CP7 close、service close 和 engine dispose 顺序不变。

`BEHAVIOR-API-LIFECYCLE-001`与`BEHAVIOR-ORCH-AUTHORITY-SNAPSHOT-001`在搬迁lifecycle/interrupt前先characterize。P4退出条件：API/OpenAPI/SSE与runtime公开面无diff；startup/shutdown/interrupt focused trace相同；API没有复制Agent recovery、Slot或file-selection业务authority；API及受影响cross-layer suite通过。

## 14. P5：Storage、State 与 Lifecycle

核心原则：唯一 session/transaction owner 不变，方法体先原样迁移。

### 14.1 共享 SQLAlchemy 基础

先移动纯类型、metadata/base、row mapper、规范化和校验helper，建立SQLite/PostgreSQL共享的显式基础；具体模块布局由P5实施计划决定。SQLite/PostgreSQL继续re-export旧路径，shared metadata identity保持；PostgreSQL不再依赖SQLite私有下划线helper，但其专用override不得退化成generic inherited path。

### 14.2 领域切片顺序

1. Auth 与 Conversation；
2. AgentRun/AgentItem/lease/atomic-outcome durable repository；
3. Task/Lifecycle projection，并给 Lifecycle 服务引入窄 port；
4. MCP config 与 owner authority；
5. MCP rollout/observability；
6. MCP dispatch、CP7、durable result；
7. Remote Task 与最终 assembly。

每个切片只迁移一个domain owner；使用mixin、module或composition由实施计划依据当前MRO和patch seam选择。公开`SQLiteAgentRepository`、`PostgreSQLAgentRepository`、`SQLiteStateRepository`、`SQLiteCollaborationRepository`、`SQLiteStorage`、`PostgreSQLStorage`旧路径及现有Agent repository subclass/MRO继续作为assembly/facade，不保留第二套方法体。

### 14.3 事务与锁不变量

- `SQLiteStorage._run` 继续负责 session、`BEGIN IMMEDIATE`、commit、shield 和 cancellation wait；
- `_run`用同一个Session构造StateRepository与CollaborationRepository，并保持一次callback/commit边界；不得因拆分形成第二Session或第二事务；
- Agent durable repository继续拥有独立Session、SQLite`BEGIN IMMEDIATE`、CAS、commit/rollback与shield边界，不得并入`SQLiteStorage._run`或State+Collaboration事务；
- repository 内现有 flush 与 CAS 失败 rollback 原样保留；
- PG CP7、rollout、conversation delete runner 各自继续拥有 session/commit；
- Lifecycle 一次业务操作中的多次 storage 调用不得顺手合并成单事务；
- CP7 owner→server→intent→outbox→pending→branch→call→receipt→projection→candidate→durable→task→node→interrupt→answer→grant 锁顺序不得重排；
- 冻结 PostgreSQL override 集，禁止专用方法意外退化为 inherited generic path。

### 14.4 State、Lifecycle 与 Sidecar 边界

State只拥有运行状态投影，Lifecycle只拥有task/node/mailbox/interrupt/cancel/conversation guard规则；两者通过窄port访问Storage，不取得session或SQL。Sidecar off/shadow/enforce仍按各domain现有采样点、SQL/Sidecar调用次数、错误、CAS/idempotency与先后执行，不抽成掩盖差异的大一统writer。Cancellation writer沿用P1独立边界。

### 14.5 PostgreSQL 停止条件

P5实施计划在迁移每个domain前列出受影响的inherited/overridden PostgreSQL有效operation及对应真实integration/permissions/concurrency测试，遗漏operation则不启动该切片。没有隔离真实DSN证据时，P5可以完成不依赖PG的协议、mapper、SQLite/shared切片，但不得迁移或宣称完成对应rollout、CP7、remote-task与PG parity切片。

P5退出条件：公开facade/MRO/metadata合同不变；每个domain只有一个repository/storage owner；Agent repository import/MRO、SQLite/PostgreSQL parity、lease/CAS/final/fault与其他transaction/lock/cancel trace前后相同；受影响SQLite/Sidecar/PG测试通过且目标skip为0，或明确列出尚未开始的`external_pending` PostgreSQL切片。

## 15. P6：Frontend

P6按wire contract、纯domain/reducer、controller hook、presentational component、App shell分层。具体文件和批次由P6实施计划决定。Reducer/projector保持纯函数，组件不直接拥有API/subscription。Attachment controller唯一拥有draft/saved/pending upload、uploading/deleting state与附件API的upload/delete/reload/rollback/commit。Task Runtime controller唯一拥有accepted-task state、current task/assistant IDs、pending interrupt、presentation/busy mode、Interrupt answer API与closed outcome分类，以及后续SSE/timer/MCP/cancel/artifact effects；它只能通过App port patch message，不能持有message store，也不能在accepted handoff后再次submit。

### 15.1 App 最终所有权

`App` 继续拥有：

- auth；
- conversation selection与restore generation；
- messages store与`pendingAssistantPatches` buffer；
- composer；
- model；
- slash/MCP command menu；
- optimistic user/assistant turn；
- normal/Slash/MCP首次submit、accepted-task handoff与Attachment/Task controller跨域协调。

Interrupt answer时，App只取得attachment snapshot、创建现有optimistic turn、调用Task Runtime command恰好一次并按returned outcome协调Attachment disposition；App不直接执行第二类answer API，也不直接写Task Runtime state。Task Runtime只在`resumed` outcome为新assistant建立subscription。

### 15.2 附件时序

1. 多附件串行上传；
2. 上传前标记 uploading；
3. 中途失败回滚已上传文件；
4. submit 失败 rollback 并恢复草稿；
5. history reload 在上传后、optimistic message 前；
6. uploading 状态由 finally 清理。

### 15.3 Task runtime 时序

- 新订阅前关闭旧订阅；
- 每个异步写入检查 generation/conversation/task/assistant；
- waiting event 先经过 reducer，再加载 interrupt；
- interrupt 展示完成后才关闭 SSE；
- clarification keep-open 不清错 pending interrupt；
- unknown/late-result 不提前结束订阅；
- terminal artifact 加载后才清 runtime。

Interrupt answer outcome必须按下表锁定；App是唯一跨Attachment/Task controller coordinator，禁止双rollback/reset/subscribe：

| Outcome | answer-submit API delta / new-assistant SSE subscription delta | Optimistic turn与task/notice | Attachment disposition | Pending interrupt |
|---|---|---|---|---|
| pre-upload failure | `0 / 0` | 不创建turn，不启动task | 按共享upload helper处理failed/draft/rollback | 不变 |
| stale scope after API | `1 / 0` | 已创建旧scope turn；不得写当前scope、不得新增notice | no compensation/delete/reset | no current-scope write |
| keep-open | `1 / 0` | clarification assistant完成，task回到waiting | commit已上传draft | retain/refresh |
| resumed | `1 / 1` | assistant绑定resumed task并继续running | commit/mark sent | clear |
| API rejected | `1 / 0` | task标failed并显示既有notice；不订阅 | rollback uploaded + reset draft | retain/not cleared |

两列delta均指单次answer attempt相对调用前的增量；upload/delete/history/listInterrupts API与MCP approval后的重订阅不计入这两列，分别由Attachment/Task trace锁定。Pre-upload失败时，失败附件保留`failed/待重试`、同批其他附件回到draft；已上传项rollback delete失败时按现状移除对应draft、refresh saved list并显示部分保存notice。上述调用和disposition不借hook拆分统一；P6实施计划为rejected与stale补最小characterization。

### 15.4 DOM 与可访问性

拆展示组件时保持wrapper层级、className、role/name、ARIA、focus、scroll、portal容器、random welcome mount/key和`styles.css`行为。Login提交资格仍由controller计算，View只投影；StrictMode下subscription/timer setup与cleanup次数不增加。

### 15.5 明确延期的前端问题

- MCP 命令菜单方向键；
- Artifact ID 语义推断；
- localStorage 异常差异；
- API 文本错误体 fallback；
- upload refresh 失败清空状态；
- 任何文案、视觉、DOM、ARIA、焦点或滚动调整。

P6退出条件：App/Attachment/Task Runtime/reducer/component owner唯一，Interrupt answer API只由Task Runtime执行；附件与Task trace、Interrupt delta/disposition、stale guards、DOM/a11y focused tests前后相同；Frontend全量测试、typecheck和build通过；不存在第二套message、answer-submit、subscription或timer owner。

## 16. P7：Native 与 Scripts

P7分成四个可独立完成的workstream：Runtime Sidecar、Skill Runtime、MCP Runtime、Operational Scripts。某一workstream的manylinux、fuzz或PostgreSQL条件不得阻塞不依赖它的另一个workstream。现有验证工具脚本不是普通清理目标，只在业务路径变化使其失效时做最小适配。

### 16.1 Runtime Sidecar

当前root-defined public type/free/async fn按5.1物理留root；private validation、codec、kernel、service、gRPC、config/serve职责渐进拆分，具体文件由P7实施计划决定。`RuntimeSidecarSqliteAdapter`的canonical `sqlite_adapter` path保持，内部可按schema、agent state、tasks、nodes、artifacts、events、leases、control、rows逐域拆分；不引入生产backend trait。受影响domain保持memory/SQLite parity、reopen、CAS、transaction和error行为。

### 16.2 Skill Runtime

按contract/policy、sandbox process/stdio、service/gRPC、codec/serve职责拆分。Sandbox limits可使用单一常量源但值不变；env clear、path guard、spawn、partial-output、kill/wait与错误顺序保持。PyO3 module/function/signature和JSON envelope不变。

### 16.3 MCP Runtime

按contract/error、official SDK/shadow、JSON-RPC、sanitizer、registry职责拆分。不得顺手接通测试/预备registry，也不修client-version、fingerprint或sanitizer行为；Proto path、public function/type identity、Cargo feature/dependency方向与checked-in contract bytes保持。

### 16.4 Scripts

Operational Scripts只按已登记finding处理完全等价的单文件重复和engine lifecycle。CLI flag/help、env、role、stdout/stderr、error text、exit code、SQL权限、shebang/mode、apply/restore/receipt与cleanup顺序保持；pre-validation engine leak等行为bug延期。已有`prd_evidence.py` helper只在调用方所有guard/order/result完全等价时复用，不统一所有CLI或删除deprecated参数。验证/证据脚本仅适配被移动的业务入口，不作为去重对象。

Unified schema migration的非直觉顺序是设计级合同：apply为`SQLite mutation → PostgreSQL locked mutation → Sidecar data mutation → Sidecar semantic probe`，receipt依次为`restore_verified → applying_sqlite → sqlite_applied → applying_postgres → postgres_applied → applying_sidecar → sidecar_applied → verified → completed`；restore-all为`Sidecar data restore → PostgreSQL pg_restore → SQLite restore → Sidecar semantic probe → restored`，完成前不新增backend中间receipt。P0/P7在旧实现冻结每步调用次数、fault prefix/receipt与最终semantic digest，迁移前后exact；不得把restore改成apply顺序。

### 16.5 平台与退出条件

- 所有touched Rust先通过本地fmt、Clippy和workspace tests；
- 修改Ubuntu专属runtime/packaging路径时使用现有rust-quality job；
- 修改fuzz覆盖的parser/validation/sanitizer/policy时，现有fuzz job运行全部适用target；当前漏掉的`mcp_runtime_protocol`在对应实施计划中补齐，不新建通用runner；
- 修改PyO3 bridge/contract/packaging时，现有manylinux job完成三wheel build/install/import smoke；
- Scripts运行受影响CLI的help/success/failure与对应integration tests；涉及真实PG的迁移脚本使用隔离non-prod profile。

P7每个workstream以public/contract manifest前后相同、focused/full tests和适用平台gate通过为退出条件；平台不可用时仅该切片`platform_pending`。具体job、命令、target和artifact由P7实施计划记录。

## 17. P8：收尾清理与延期审计

P8 只删除或收敛满足以下条件的内容：

- 私有；
- 仓库内零引用；
- 不属于文档化公开 API；
- 不参与 import side effect、registration、pickle/spawn；
- 有定向测试或静态证据；
- 删除不会改变异常、日志、事件、计时或副作用。

已审计候选包括未使用 import/local、断链旧 parser、不可达 fallback literal、完全相同 pure helper、无效半成品对象等。具体清单必须在 P8 基于当时 HEAD 重新验证，不能直接引用设计期行号执行。

P8不重新开启公开Core class物理迁移或新的跨crate/跨层架构工作。它还负责最终inventory闭合：每个当前业务路径为`changed|reviewed_no_change`，所有exact duplicate/structural finding有结论，不存在新旧双实现、未解释反向依赖或孤儿兼容facade，并在最终业务commit上重跑相关Backend/Frontend/Rust全量门禁。

以下问题不属于功能不变重构，继续形成单独延期报告：

- 吞异常或伪装为空结果的 masking fallback；
- fixed-zero observability；
- Runtime/MCP client-version 声明与校验不一致；
- artifact field/idempotency 验证缺口；
- 手写 HMAC/constant-time compare 替换；
- Artifact ID 跨层语义；
- Frontend 键盘与错误可见性 bug；
- 安全 authority 或 crate 边界重构。

这些项只有在用户明确批准行为变化并先补合同后，才能进入独立任务。

## 18. Fallback 处理规则

### 18.1 Grounded fallback

保留满足以下条件的 fallback：

- 保护明确的兼容或 fail-safe 边界；
- 有原因、状态或证据；
- 主路径和 fallback 均有覆盖；
- 不隐藏 authority 或安全失败。

已确认例子包括 checked-in Rust contract 的 off/shadow fallback、MCP safe-hide、显式 unsupported evidence 下的 adapter downgrade、非权威 audit export 失败标记。

### 18.2 Masking fallback

结构迁移阶段保持原行为并记录，不顺手修复。只有“不可能分支”可在证明不可达后删除；会改变错误传播、用户可见状态、重试或安全策略的 fallback 必须另立行为变更设计。

## 19. 验证矩阵

### 19.1 每个检查点

```text
characterization coverage
-> focused tests
-> module suite
-> cross-layer suite
-> lint/typecheck/build/contract diff
-> git diff --check
-> final diff review
-> independent commit
```

### 19.2 Backend canonical

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

### 19.3 Frontend

```bash
cd frontend
npm test -- --run
npm run typecheck
npm run build
```

### 19.4 Rust

```bash
conda run -n multi_agent python scripts/run_rust_quality_gates.py --run --only cargo_fmt
conda run -n multi_agent python scripts/run_rust_quality_gates.py --run --only cargo_clippy
conda run -n multi_agent python scripts/run_rust_quality_gates.py --run --only cargo_test
```

受影响检查点按可用性增加 deny、audit、coverage、fuzz compile 和平台 wheel smoke。缺少 required tool 或平台不能标为通过。

### 19.5 需求到验收的追踪

| 需求 | Owner | 最小验收 |
|---|---|---|
| FR-01 | P0/P8 | tracked universe分类`unclassified=0`；business inventory前后set检查；最终每路径`changed|reviewed_no_change` |
| FR-02～FR-04 | 全部 | 公开contract diff为零；迁移前后focused characterization与副作用trace相同 |
| FR-05 | P0/P1～P8 | owner/dependency review；无第二owner、无新增/未解释反向依赖；bounded edge exact set不扩张 |
| FR-06 | P1/P2～P5 | 四路径StoragePort identity、259-method窄域/consumer handoff、Cancellation writer；最终无未解释internal aggregate consumer |
| FR-07 | P2 | waiting/resume/terminal/五项known-behavior barrier与Orchestration/Capabilities suites |
| FR-08 | P2/P3 | MCP Dispatch public/concrete owner与bounded identity edge；Skills/Parser/Gateway/Coordinator/Historical focused tests；适用Linux/真实烟测 |
| FR-09 | P4 | API/OpenAPI/SSE/runtime seam、lifecycle/interrupt trace与API/cross-layer suites |
| FR-10 | P2/P5 | Agent repository独立session/MRO/parity、State+Collaboration同Session、其他facade/metadata/transaction/lock/CAS/cancel trace与适用SQLite/Sidecar/PG suites |
| FR-11 | P6 | App/Attachment/Task owner、附件/Task/Interrupt outcome、DOM/a11y trace与Frontend全量/typecheck/build |
| FR-12 | P7 | Rust public/byte contracts、Scripts CLI/SQL/receipt trace与各workstream适用平台gate |
| FR-13～FR-14 | P8 | 每个删除/合并finding有零引用、行为证据；fallback与延期问题无行为diff |
| FR-15～FR-16 | 全部/P8 | 小提交、diff review/rollback记录；最终无双实现、缺口状态准确 |
| 全部NFR | 全部 | 受影响test/trace/contract通过；无新增资源/round trip/secret exposure；用户diff保留 |

每个gate record至少绑定被测commit、scope、工作目录、命令或CI run、平台、ran/fail/skip和结论。相关业务代码或测试变化后必须重跑对应gate；无需为此实现自定义digest、canonical argv或artifact writer。计划exit运行其完整领域门禁，项目exit再在最终业务commit运行所有受影响全量门禁。

## 20. Edge cases、依赖、风险与假设

### 20.1 跨域 failure modes

| 类别 | 失败示例 | 处理 |
|---|---|---|
| Public identity | import、module、pickle、Rust def/future path或patch seam因搬迁改变 | 声明留原位；Rust root item用物理root wrapper；contract diff不为零即停 |
| Similar-not-equal | 两段代码形似但event ID、error、lock或fallback不同 | 不合并，标`reviewed_no_change`并记录差异 |
| Duplicate side effect | 新旧实现都写DB、发Tool、订阅或启动worker | 禁止双跑；call-count trace发现即回滚 |
| Transaction/concurrency | session、commit、CAS、lock或lease边界被helper跨越 | 方法体先原样迁移；fault/barrier trace不一致即停 |
| Cancel/cleanup | await位置变化吞掉取消、提前cleanup或新增补偿 | 保持当前阶段；已知偏差引用6.6，行为修复延期 |
| Async stale UI | 旧generation/conversation/task结果写入新scope | 每个异步写入保留scope guard并覆盖late-result测试 |
| Platform-only | macOS通过但Linux resource、PG dialect或manylinux失败 | 仅对应切片pending；不得用本地PASS替代目标平台 |
| External isolation | smoke误连生产、遗留credential或临时资源 | 连接前确认non-prod/隔离scope；cleanup失败显式报告 |
| Dead-code false positive | registration/import/pickle/spawn或仓外public consumer未被静态引用发现 | 保守保留facade；P8需动态/contract证据后才删 |
| User worktree | 长任务覆盖无关用户diff或保护文件 | 每检查点确认owned paths；不stash/reset用户工作，不读取受保护正文 |

### 20.2 关键依赖

| 依赖 | 用途 | 约束 |
|---|---|---|
| `docs/prd/backend/unified-agent-loop/` | Agent execution/recovery语义权威 | P2/P4只做结构迁移，不重新解释waiting/resume/atomic outcome |
| 当前`main`与适用`AGENTS.md` | 实施权威与模块规则 | 每个计划开始时重读；职责/入口变化同步tracked索引 |
| 现有Backend/Frontend/Rust tests | 行为锁与回归 | tests不是清理目标；只补最小characterization |
| 现有CI/workflow | Ubuntu、fuzz、manylinux | 优先复用；只在业务变化使其失效时最小修改 |
| 隔离PostgreSQL/外部MCP | 平台与真实adapter烟测 | non-prod、可清理；仅实际触及对应语义时required |
| Git小提交 | review与rollback | 不squash成巨型结构提交；不切`prod` |

### 20.3 风险登记

| Risk | 缓解与退出条件 |
|---|---|
| 仓外消费者依赖未发现的公开面 | P0公开contract与兼容facade；无证据时不删 |
| 新模块形成循环依赖或第二owner | 目标DAG、import review、P8双实现检查 |
| 既有MCP Dispatch反向兼容edge在重构中扩张 | P0冻结exact symbols；P2/P3 owner明确；禁止新增跨边state/I/O |
| 状态/并发/no-replay因抽象漂移 | 旧实现先做focused barrier/fault trace；一次迁移一个owner |
| Agent durable repository被误并入Storage `_run` | 独立public/MRO/session/CAS合同与SQLite/PG parity/fault trace |
| 高风险平台暂不可用 | 延期对应切片并继续无关模块；完成声明列pending |
| 安全authority或raw数据被错误合并 | P3独立authority/隐私测试；public/log sentinel为零 |
| Frontend拆分改变可见DOM或effect identity | DOM/a11y/StrictMode与异步scope测试 |
| Rust物理搬迁改变canonical identity | public声明留root、byte contract与compile tests |
| 大任务再次演化成基础设施项目 | 设计边界与NFR-REVIEW；新runner/framework需独立用户授权 |

### 20.4 假设

- `main`继续是开发权威，`prod`不在本任务范围；
- 不需要schema/data migration；如某切片必须migration才能继续，该切片停止并另立任务；
- 现有测试是行为起点但不是充分证明，高风险搬迁按需补characterization；
- 外部PG/MCP、Ubuntu/manylinux可用时间未知，已由pending状态界定，不需要扩大本任务建设新平台；
- 具体模块名和检查点在进入各计划时可按最新HEAD调整，但FR/NFR、owner与不可变行为不能静默改变。

## 21. 停止与回滚

以下任一情况立即停止当前检查点：

- import、signature、type/module identity 漂移；
- event/payload/digest/error text 漂移；
- SQL、lock、CAS、commit/rollback 或 external call 次数漂移；
- Frontend DOM/ARIA/focus/scroll 漂移；
- Rust contract/proto/PyO3/Cargo contract 漂移；
- 需要 schema/data migration 才能继续；
- 高风险 PostgreSQL 检查点缺少真实 profile；
- 必须通过改变旧行为才能让测试通过。

每个检查点一个commit，不squash成巨型提交。未提交的owned changes用`apply_patch`反向修改；已提交检查点按逆序`git revert`，不使用破坏性reset/checkout用户文件。外部临时资源必须cleanup；cleanup失败报告剩余资源和operator action，不把该gate记为PASS。根目录`docker_cmd.md`只核验存在/ignore/tracking/mode metadata，始终禁止读取正文、移动、删除、跟踪或提交。

## 22. 文档与 Git

- 每个计划开始前生成基于当前 HEAD 的详细 implementation plan；
- 每个结构检查点同步检查对应 `AGENTS.md` 与 `CHANGELOG.md`；
- 模块职责、入口或目录变化时更新索引；
- 每个检查点独立 commit；
- 当前计划达到`local_complete|complete`后才激活下一计划；条件性pending切片按第8节规则不阻塞无关模块；
- 只读审计可以并行，写入任务只在文件边界互不重叠时并行，最终由主代理统一集成和验证。

## 23. 完成标准

项目级清理只有同时满足以下条件才完成：

- P0～P8所有mandatory local结构目标完成；行为修复类finding可以延期，但不得用延期掩盖未做的业务审查；
- tracked code/config分类`unclassified=0`，每个业务路径为`changed|reviewed_no_change`，无悬空finding；
- FR-01～FR-16与全部NFR均有对应验收结果，公开contract和关键behavior trace无漂移；
- 各旧公开 facade 只承担兼容与装配，不保留第二套业务实现；
- 目标依赖方向无新增或未解释反向import；7.1 bounded compatibility edge未扩张；
- 已识别的完全复制实现已合并，语义不同的相似实现有清晰所有权；
- 结构变化已同步 AGENTS/CHANGELOG；
- Backend、Frontend、Rust 相关全量门禁通过；
- 实际触及的PostgreSQL、Linux Result Parser、manylinux、fuzz或真实MCP切片已通过目标平台门禁；未触及或环境不可用项准确记录为N/A/pending，不伪报PASS；
- `docker_cmd.md` 始终存在、被忽略、未被跟踪且未被读取；
- 最终报告按 AI Slop Cleaner 格式记录范围、行为锁、简化、fallback、changed files、checks、风险和延期项。

若仍有`platform_pending|external_pending`，可以准确报告“本地结构清理完成、列出的外部切片待验证”，但不得宣称“全仓端到端完全验证”。

代码行数、文件数量或 lint 数字不是单独完成条件。职责单一、依赖清晰、重复实现消失且行为证据闭合，才是本计划的成功标准。
