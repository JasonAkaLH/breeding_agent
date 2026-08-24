# 全仓业务代码渐进式架构清理设计

- **日期**：2026-08-24
- **状态**：设计章节已获用户逐项批准；document-perfectization 第 3 轮审计与修订完成，待第 4 轮独立复审
- **审查轮次**：3 次完整审计、3 次授权修订
- **适用分支**：`main`
- **适用仓库**：`breeding_agent`
- **目标**：在可验证地保持现有功能、公开合同和副作用顺序不变的前提下，分阶段清理全仓业务代码中的单体模块、复制实现、无效抽象、死代码和错误的职责边界
- **实施方式**：P0～P8 系列计划；每个计划继续拆成可独立验证、提交和回滚的检查点
- **生产边界**：不部署或修改 `prod`，不执行 schema/data migration，不访问或输出 `docker_cmd.md`

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
- Ruff 复杂度扫描确认存在大量热点，但该扫描只作为审计入口，不能作为自动改写依据；P0 必须记录工具版本、命令、范围和诊断快照后才能比较后续变化。

六个并行只读审计工作流覆盖了 API/Core、Auth/Lifecycle/State/Storage、Orchestration/Capabilities、Integrations、Frontend、Native/Scripts。审计确认：仓库同时存在高置信的死代码和复制实现，也存在大量看似重复、实际承载协议版本、安全 authority、错误码、事务、锁、幂等、恢复和兼容语义的代码。直接“大一统”重写无法满足功能不变要求。

本设计选择架构优先方案，但采用稳定门面和渐进接管，不做 big-bang rewrite：

```text
原调用方 -> 原公开模块/类型 -> 新的内部职责模块
```

原模块先作为兼容门面保留，新模块一次只接管一个职责。每个检查点全绿后才进入下一检查点；任何可观察行为漂移都回滚当前检查点。

### 1.1 用户、利益相关方与价值

本项目不增加用户可见功能，直接价值来自降低后续修改的回归面，同时保持现有产品行为稳定。

| 参与者 | 关注点 | 本设计提供的价值 |
|---|---|---|
| 最终用户 | 对话、附件、长任务、Interrupt、MCP、历史与恢复不能丢失、重复或退化 | 结构迁移期间保持 API、SSE、UI、Tool 调用和恢复行为不变 |
| Backend/Agent 实施者 | 巨型 Runtime、Coordinator 和跨层依赖难以局部修改 | 每个职责有唯一 owner、窄 port 和可独立回滚检查点 |
| Storage/PostgreSQL 维护者 | 事务、锁、CAS、override 和 schema 注册容易在搬迁中漂移 | 机器可读 owner manifest、真实 PostgreSQL parity 与零目标 skip 门禁 |
| Frontend 维护者 | `App.tsx` 多状态源、订阅和 timer 耦合 | 明确 App、Hook、Reducer、Component 的唯一 state/effect ownership |
| Rust/Runtime 维护者 | crate root 类型、Proto、PyO3、contract artifact 与平台构建路径敏感 | 保留 canonical type/module path，并为 Ubuntu/manylinux 建立 required gate |
| 安全、可靠性与运维 reviewer | no-replay、credential、raw result、取消、清理和回滚边界必须可审计 | 编号 NFR、failure matrix、side-effect trace 和风险登记 |
| Code reviewer/发布负责人 | 大型 diff 难审、失败后难定位 | 每个检查点只迁移一个职责，独立测试、commit 与 revert |

用户价值不以 LOC、文件数、lint 数字或抽象数量衡量；只有在行为证据闭合、职责唯一且后续变更面可被局部审查时才成立。

## 2. 现状行为基线

设计前只读基线绑定以下不可变 revision：

- `baseline_commit=c8da6ccdf89eed5851cb5a79385cf583560a3c93`；
- `baseline_tree=df3d2a21c8cee01990518999285894e1a57e24a2`；
- 当时 `origin/main=d39d3ddfaeab564bd927cbdba42f8192f5b95357`；
- 基线于 2026-08-24 在本地开发环境运行；其后的本设计、自审与 document-perfectization commits 只修改文档、索引和 CHANGELOG，不改变业务代码。

只读基线结果如下：

- 当前分支为开发分支 `main`，基线运行前工作树干净；
- Backend canonical 域通过：Core 42、Storage 400、Lifecycle 37、Integrations `Ran 704 / OK / skipped=2`、Agent Skills 209、Orchestration 102、Capabilities 45、API 436、E2E 7、Observability 39、Scripts 62、Deployment 3；Integrations 的 2 个 skip 是 Linux-only Result Parser RLIMIT 与 malicious-regex timeout gate，macOS 结果不构成这两项通过证据；
- Frontend 21 个测试文件、307 项测试通过，`npm run typecheck` 与 `npm run build` 通过；
- Rust workspace tests、`cargo fmt --check`、Clippy `-D warnings` 通过；
- 本地 Git-ignored 的 `skill/sql-query` 不存在，对应外部 Skill 测试记为 N/A，不属于本仓业务代码完成证据；
- 本轮基线未重新运行真实 PostgreSQL profile，因此 P5 的 PostgreSQL 高风险检查点必须重新取得真实 DSN 证据；
- Linux-only Result Parser、manylinux wheel smoke、真实外部 MCP 和生产观察窗不是普通 macOS 本地门禁；其中本设计列为 required 的 Result Parser、Ubuntu/manylinux 与受控真实 MCP profile 缺失时，必须按状态机记录验证缺口且不得宣称项目完成。

这些数字是设计起点，原始 console 输出没有作为 checked-in artifact 保存，因此不是未来检查点可以复用的通过证据。P0 必须基于实施起点重新生成可审计 manifest、gate result 和 digest；每个受影响检查点也必须基于当时的代码重新运行对应门禁。

## 3. 范围

### 3.1 范围内

- `src/**` 中的业务 Python；
- `frontend/src/**` 中的非测试 TypeScript、React 与 CSS 业务源码；
- `native/crates/**/src/**` 中的非测试 Rust 业务源码；
- `scripts/**` 中的运维、迁移、证据与验证逻辑；
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

## 4. 备选方案与决策

### 4.1 方案 A：安全等价小批次

只处理已明确证明的死代码、复制逻辑和无效抽象。风险最低，但不能充分拆解 `ApiRuntime`、Storage repository、Frontend App 和 Rust `lib.rs` 等结构性单体。

### 4.2 方案 B：架构优先渐进拆分（采用）

为每个单体保留稳定门面，通过一系列独立检查点逐项迁移职责。先搬家，再抽象；先锁行为，再删除重复。该方案能改善边界，同时保持每一步可验证、可回滚。

### 4.3 方案 C：一次性架构重写

理论上可最快减少文件体积，但会同时改变导入、装配、事务、状态和恢复路径，无法证明功能不变，因此拒绝。

## 5. 功能需求

下列需求是 P0～P8 和后续 implementation plan 的规范性输入。为避免与统一 Agent Loop 的既有 `FR-1～FR-26` 混淆，本文两位编号 `FR-01～FR-17` 等价于 cleanup-local `CLN-FR-01～CLN-FR-17`；引用统一权威时明确写“统一 Agent Loop FR”。计划章节中的文件名可以在不改变 owner 和验收合同的前提下细化，但不得弱化这些需求。

| ID | 必须满足的需求 | 主责计划 | 完成证据摘要 |
|---|---|---|---|
| FR-01 | P0 必须枚举全部受跟踪业务源码；每个路径有一个 `primary_owner_plan`，必要时另列不拥有该文件的 `authorized_touch_plans`。P0 初始状态允许 `planned_change`，项目完成时该状态必须为零；路径最终状态只能是 `changed` 或 `reviewed_no_change`，rename/delete 以 action+tombstone 表达。`deferred_out_of_scope` 只属于已审行为 finding，`blocked_external` 只属于具名 gate，均不得替代源码路径审计 | P0/P8 | inventory 无遗漏、无重复 primary owner、无未授权 touch、最终 path status 仅两类、`planned_change == 0` |
| FR-02 | 旧公开 import、签名、默认值、对象/模块身份、错误合同、Proto/PyO3/Frontend facade 必须保持，除非另有明确破坏性批准 | P0～P8 | public-contract manifest 前后 exact comparison |
| FR-03 | 每个有状态或跨层职责迁移前必须先在旧实现上建立可执行 characterization；replacement test 必须先绿，才能移除只锁文件布局的旧测试 | P0/各计划 | trace、golden、focused test 在迁移前后相同 |
| FR-04 | 每个检查点只允许一个业务实现和一个副作用执行路径；不得双跑真实 Tool、数据库写、上传、删除、provider 或消息发送 | 全部 | call-count/side-effect trace 与基线完全相同 |
| FR-05 | P0 必须生成编译期 import edge inventory；P8 时所有 forbidden edge 必须清零，或记录为带唯一 owner、理由和退出条件的显式兼容 exception | P0/P2～P8 | AST edge gate + exception registry |
| FR-06 | Core storage 子协议必须让新消费者依赖窄 port，同时保持 `src.core.contracts.StoragePort` 的公开 aggregate 定义、三路径对象 identity 和运行时协议行为 | P1 | 259 方法唯一归属、签名一致、aggregate identity 不变 |
| FR-07 | Orchestration/Capabilities 迁移必须继承统一 Agent Loop 权威，保持 invocation、waiting、multi-waiting、resume、lease 和 final 顺序 | P2 | Agent trace/fault matrix、权威 FR 映射 |
| FR-08 | Integrations 迁移必须保持 Agent Skills 双通道隐私、MCP Gateway 安全顺序、Result Parser 两阶段失败、Coordinator no-replay 和历史零网络重投 | P3 | P3 安全/失败矩阵全部通过 |
| FR-09 | `ApiRuntime` 必须收敛为稳定 facade 与装配入口；文件、会话、interrupt、lifecycle/recovery、factory 只能各有一个实现 owner | P4 | runtime owner manifest、startup/shutdown trace、API 全量门禁 |
| FR-10 | Storage/State/Lifecycle 必须按领域切片且保持 session、事务、SQL、锁、CAS、metadata、公开 facade identity、既有基类相对顺序和 PostgreSQL override owner；MRO 只能出现 P0 manifest 逐项批准的 mixin 插入 | P5 | SQLite/PG parity、exact MRO/owner delta manifest、DDL digest、零目标 skip |
| FR-11 | Frontend 必须形成 Contracts、Domain、Hooks、Components、App 的单向依赖和唯一 state/effect owner，且 DOM/a11y/SSE/附件/MCP 行为不变 | P6 | P6 checkpoint matrix、测试发现数不低于 P0 manifest、typecheck/build |
| FR-12 | Rust 必须保留 root/non-root canonical type path、contract artifact、Proto/PyO3 与平台合同；scripts 必须保留 CLI、权限模板、mode 和输出合同 | P7 | type-name/compile fixture、byte diff、平台 gate、CLI trace |
| FR-13 | Grounded fallback 必须按符号记录证据；masking fallback 必须保持旧行为并登记风险，不得在结构迁移中顺手修复 | P0/P8 | fallback registry 有 source/test/owner/exit criterion |
| FR-14 | 每条 required gate 必须记录 ran/pass/fail/skip；`Ran 0 tests`、目标 profile skip、缺 required platform 或未保存证据都不得计为通过 | 全部 | gate-result artifact 与已存在的 `tested_commit` 及其 clean disposable checkout 绑定 |
| FR-15 | 只有全部 mandatory structural deliverables 和 required external/platform gate 闭合时才能声明项目 `complete`；独立计划可以越过有边界的外部阻断继续，但不得掩盖总体状态 | 全部 | 计划状态机与最终 completion matrix |
| FR-16 | 每个检查点必须从已记录 start commit 和工作树 inventory 开始；主责文件无未授权重叠修改，无关用户 diff 原样保留；required gate 在 `tested_commit` 的 disposable clean checkout 运行，不能要求原工作树丢弃无关 diff；业务实现 commit 独立，允许紧随一个只含 evidence/index 的 commit，并有 pre/post-commit 回滚步骤 | 全部 | start/tested commit、原工作树 preservation digest、clean checkout、implementation/evidence commit、rollback record |
| FR-17 | 已知 UI、fallback、观测、安全 primitive、client-version、artifact validation 和资源泄漏等行为问题不属于本轮结构迁移 | P8 | deferred finding registry；业务行为无混改 |

## 6. 非功能需求

| ID | 类别 | 可判定要求 | 主责计划与证据 |
|---|---|---|---|
| NFR-COMP-01 | 兼容性 | 公开 Python/Rust/TS 符号的 import、签名、默认值、`__module__`/`__qualname__`、关键 `type_name`、对象 identity、Proto/PyO3 path 和 JSON schema 必须与 P0 manifest 相同 | P0/全部；public-contract manifest、compile/import/pickle smoke |
| NFR-REL-01 | 可靠性 | 确定性调用次数、写入顺序、事务数、锁序、CAS、lease、timer/retry 次数必须 exact；wall-clock 只按既有 timeout/budget 验证，不要求逐毫秒相同 | P0/P2～P7；side-effect trace 与 fault tests |
| NFR-REL-02 | No-replay | 任一 may-have-dispatched 或不确定副作用在 restart/retry 时第二次外部 Tool 调用必须为 0；现有 17 个 MCP fault boundary 集合不得减少 | P3；fault-injection matrix、network delta |
| NFR-SEC-01 | Gateway 安全 | scope bootstrap/retry 必须保持 `lease/admission → endpoint revalidate/observe → credential decrypt → exact ValidatedEndpoint client → initialize/discover`，本 attempt 在 endpoint/admission 拒绝时下游 credential/client/network 为 0；已打开 scope 的 call 必须保持现有 `safety admission → scope → authorization → accepting#1 → descriptor/args → registration/on_created → assignment guard → accepting#2 → task safety → 已有 adapter Tool send`，guard 失败只要求 Tool send 为 0，不得为每个 call 重做 DNS/credential/client | P3；分阶段 Gateway security tests、bootstrap/call trace |
| NFR-SEC-02 | Authority 隔离 | Temporary Result、Pending Action、Projection、CP7 Candidate、Credential/Master Key、historical raw authority 的 key/AAD/cap/mode/identity/exception 不得合并 | P3/P5；authority-specific tests 与 import gate |
| NFR-PRIV-01 | 隐私 | prompt/LLM/public event/audit/metric/frontend public projection 不得包含 Tool args、credential、raw MCP result、附件正文或 execution-only artifact 字段；authority-bound execution request metadata 维持现有例外且不得外泄；历史重投不得获得网络/client/credential 依赖 | P3/P6；分通道 forbidden-key/taint snapshots、zero-network test |
| NFR-PERF-01 | 性能/资源 | 不得新增 LLM、DNS、credential、network、DB/API round trip、subscription/timer、worker spawn 或完整 raw-copy 次数；queue/cap/timeout/size 常量保持 exact，除非另有行为变更批准 | P0/P3/P5/P6；spy/counter、frontend call trace、constant manifest |
| NFR-OBS-01 | 观测 | durable event、metric label/cardinality、bucket、error/result category、八条 safety red line、gap 与 cleanup failure 语义保持；observer 的 fail-open/fail-closed 角色不变 | P2/P3/P4；event/metric golden 与 observability tests |
| NFR-A11Y-01 | 可访问性 | 前端根元素、wrapper、class、role、accessible name、`aria-live`、focus、scroll、portal 和 welcome mount/key 不变；不得引入新 a11y 依赖 | P6；Testing Library 与 DOM/class snapshot |
| NFR-PORT-01 | 平台 | macOS 是开发门禁；Ubuntu 22.04 是 Result Parser Linux-only RLIMIT/regex、Rust release 与 manylinux required gate。required remote gate 未运行时相关 P3/P7 检查点只能是 `pending_platform` | P0/P3/P7；CI/remote gate artifact |
| NFR-MAINT-01 | 可维护性 | 每个业务职责必须有唯一 owner；旧 facade 只能保留 constructor、assembly、compat forwarding 或白名单 transaction runner，不得保留第二套业务 method body | P1～P8；owner manifest 与 AST facade gate |
| NFR-ROLL-01 | 可回滚性 | 未提交失败不得使用 reset/checkout；使用审阅过的反向 patch 恢复本检查点文件。已提交检查点使用逆序 `git revert`；不得影响无关用户改动或 `docker_cmd.md` | 全部；rollback record、status/ignore checks |

“对象 identity”只指同一 revision/进程内多个兼容 import/alias 必须以 `is` 指向同一 canonical object（例如三条 `StoragePort` 路径），不比较两个进程或两个 Git revision 的内存地址。跨 revision 的 public function/method compatibility 以 import path、module/qualname、raw+resolved annotation、signature、async、descriptor defining owner 和明确 alias关系比较。

## 7. 总体设计原则

### 7.1 稳定门面

- Python 旧 import、类名、函数签名、默认值和公开导出继续可用；
- Python 公开类型如受 `__module__`、pickle、repr 或 introspection 影响，优先保留定义原位；
- Rust public struct/enum 继续定义在原 canonical module；root-defined public free function 的 body 只有在 root 保留签名一致的薄 wrapper 时才可下沉，避免 function-item def path 因裸 `pub use` 漂移；
- Frontend 旧 `api/types.ts`、`domain/artifacts.ts`、`domain/taskEvents.ts` 继续 re-export 或保留公开 facade；
- Storage 的三条 `StoragePort` 导入路径必须继续指向同一个 aggregate protocol 对象。

### 7.2 先搬家，再抽象

一个检查点只做一种变更：

1. 原样移动职责；
2. 验证行为与顺序；
3. 在后续独立检查点收敛重复；
4. 最后删除已确证私有且无引用的旧实现。

不得在迁移时同时改命名、错误策略、fallback、数据结构或可见行为。

### 7.3 单一 authority

- Agent Loop 是 AgentRun、AgentItem、continuation identity 和调用顺序的唯一 authority；
- MCP Coordinator 是 durable aggregate、approval、continuation、terminal 和 no-replay 的唯一顺序控制者；
- MCP Gateway 是 endpoint、credential、client、外部 I/O 和 isolated parsing 的唯一 authority；
- Storage/State 是 session、事务、SQL、锁、CAS 和持久化的唯一 authority；
- Lifecycle 是 interrupt、cancel、mailbox 与 conversation guard 规则的唯一 authority；
- API 只负责装配、HTTP/SSE、DTO 和公开投影；
- Frontend reducer 先消费事件，controller 才执行终态副作用。

### 7.4 禁止双跑副作用

不得通过同时执行新旧实现来比较真实副作用，尤其禁止双跑 MCP Tool、上传、删除、发送消息、数据库写入或外部 provider 调用。等价性通过 characterization、fake/trace、digest、状态快照和单实现执行证明。

### 7.5 不新增框架

本轮不引入 Service Locator、动态 `__getattr__` facade、通用事件框架、Frontend 状态库、Context 架构或新的生产依赖。只使用仓库现有语言、库和装配模式。

## 8. 行为锁

“功能不变”按五层合同验证。

### 8.1 API 与类型合同

- import path、`__all__`、函数/方法签名、默认值和 keyword-only 形状；
- Python `__module__`、pickle round-trip 和公开对象 identity；
- Rust root exports、canonical type path、Cargo feature 与 public method；
- exception class、error code、retriable 分类、message 和安全 metadata keys。

### 8.2 数据合同

- DTO/dataclass 字段顺序、default、`slots/frozen/repr`；
- JSON key、排序、separator、UTF-8、digest 和 byte limit；
- durable event type、payload、event id 与顺序；
- checked-in Rust contract artifact 和 export binary 输出；
- ORM table/column/index/constraint、metadata identity 与 SQL 结果；
- MCP 五版 decoder、checkpoint、projection 和 parser revision。

### 8.3 副作用合同

- storage write、flush、rollback、commit 与 transaction owner；
- row/advisory lock、CAS、claim、lease 和 idempotency 顺序；
- Tool reservation、may-have-dispatched、唯一外部调用、terminal commit；
- interrupt、slot、continuation、visible message 和 outcome commit；
- startup reconciliation、Ready、post-ready remote work 与 shutdown；
- Frontend upload、rollback、history refresh、optimistic message、SSE、interrupt、cancel 和 artifact completion。

### 8.4 Frontend 合同

- DOM 层级、className、文案、ARIA、Popover/Drawer 挂载位置；
- focus、scroll、keyboard、random welcome mount/key；
- reducer 对 ignored/conflict/late/unknown event 的 state identity；
- generation、conversation、task、assistant scope guards。

### 8.5 兼容合同

- legacy transport、协议 adapter 和五版 Result Parser 不合并；
- Python 兼容 alias 和 facade 不在无外部调用证据时删除；
- Proto、PyO3 module/function、JSON envelope 和 checked-in contract 路径不变；
- grounded fallback 原样保留；masking fallback 在结构迁移中也先保持旧行为。

## 9. 目标依赖与运行流

以下箭头分开表达编译期 import 和运行时副作用，禁止把两者混成一条层级链。

### 9.1 编译期 import DAG

```text
API assembly
  -> Orchestration / Lifecycle / Capability APIs
  -> Core storage/read/event ports
  -> concrete Auth/Lifecycle/Storage/State/Orchestration/Capability/Integration/Sidecar implementations, only in the composition root to construct and inject them

Orchestration
  -> Capability APIs
  -> narrow Lifecycle / Storage ports

Capabilities
  -> Integrations
  -> narrow Lifecycle / Storage ports

Integrations
  -> external protocol/client libraries
  -> narrow Storage ports where durable authority is required

Storage / State implementations
  -> Core contracts and shared SQLAlchemy metadata

API projection / SSE
  -> Core read/event ports
```

Storage 不拥有 SSE，SSE 也不是 Storage 的下游实现层。`src/api/runtime.py` 及 P4 `runtime_components/factory.py` 是唯一 composition root，可以为 constructor、backend selection、one-time configuration/registration 与注入而导入所需 Auth/Lifecycle/Storage/State/Orchestration/Capability/Integration/Sidecar concrete；其他 API route/projection/runtime component 只能依赖公开 service API 或窄 port。Composition root 不得绕过 service/port 调用 repository 或 external-I/O 业务方法。P0 把当前 `runtime.py` 尚未迁出的 concrete import/call 逐项标为 P4-owned temporary compatibility exception；P4 每个检查点收缩 allowlist，最终只保留构造/装配 symbol。AST import allowlist、constructor trace 与 business-method call gate 共同固定边界。

### 9.2 运行时副作用流

```text
request -> API adapter -> AgentLoopOrchestrator -> Capability adapter
  -> Integration / external I/O when required

API / Orchestration / Capability / Integration
  -> narrow Storage or Lifecycle port
  -> Storage/State implementation

durable Event/Task/Agent state
  -> API projection / SSE
  -> Frontend reducer / presentation
```

该运行流不授予高层绕过端口写具体 backend 的权限，也不允许 Storage 反向调用 API、Selector 或外部 Tool。

后端层级目标：

| 层 | 应拥有 | 不应拥有 |
|---|---|---|
| Core | 稳定共享模型、证据 primitive、窄协议 | capability 专属流程、外部 I/O |
| Lifecycle | interrupt/cancel/mailbox/guard 规则 | SQL、MCP transport、Agent sample |
| Storage | session、SQL、事务、锁、CAS、文件持久化、Runtime Sidecar persistence client、backend parity、共享 SQLAlchemy metadata | API/SSE、Tool 选择、非持久化的 Tool/provider 外部 I/O |
| State | schema policy、queue/read-store、health/cutover 与 runtime state service | API/SSE、Capability 业务实现 |
| Orchestration | AgentRun/Item、continuation、catalog、调用顺序 | Skill schema、具体 slot 持久化 |
| Capabilities | Skill/MCP 执行入口与领域 adapter | API 装配、外部 credential store |
| Integrations | 协议、client、Gateway、parser、安全 authority | AgentRun 状态机、API route |
| API | runtime 装配、HTTP/SSE、DTO、公开投影 | repository 与 slot 业务实现 |

允许的 Storage/State/Lifecycle 边界必须在 P0 inventory 中逐项登记：

| 来源 | 目标允许边 | P5 目标 |
|---|---|---|
| State schema/runtime | shared SQLAlchemy metadata | metadata 由 `src/storage/sqlalchemy` 唯一注册，State 不依赖 SQLite 专属模块 |
| PostgreSQL bootstrap | State schema/reconciler policy | 保留窄 assembly edge，禁止 repository method 依赖 State service |
| Storage repository | pure Core/Lifecycle contract | 只允许无 I/O 的状态/错误合同；不得依赖 Lifecycle service |
| Lifecycle service | narrow writer/store port | 移除对 concrete Sidecar facade/shadow 的依赖，由装配注入 |

P0 必须生成现有反向 edge inventory；P2～P7 每移除一条 edge 都更新 owner。P8 时 forbidden edge 必须为零，显式兼容 exception 必须有 owner、理由和退出条件。

### 9.3 Frontend import DAG

```text
App shell -> API client assembly / Domain / Controller Hooks / Feature Controllers / Presentational Components
Controller Hooks -> API transport ports / Domain / Wire Contracts
Feature Controllers -> API transport / Domain / Presentational Components
API task-event transport -> Domain task-event validation -> Wire Contracts
Presentational Components -> Domain view models / Wire Contracts
```

Domain 必须不导入 React component、Hook 或 API client；API task-event transport 只允许依赖无副作用的 Domain closed-event validator，Domain 不得反向依赖 API。Presentational Components 必须不导入 Controller Hooks 或 API。现有 `MCPSettingsPanel` 明确归类为 feature controller：可以持有该面板局部 state/effect 并调用 MCP settings API，但不得持有 App task runtime、跨会话 state 或导入 App Hook；本轮不为追求“纯组件”强行迁移它。

### 9.4 Rust import DAG

```text
runtime config / serve
  -> gRPC bridge
    -> service
      -> kernel + backend adapter
        -> private validation / crate-private contract helpers
    -> transport / Proto codec
      -> Proto modules + crate-root public contract types

each PyO3 module/function
  -> its existing P0-mapped contract / policy / kernel facade
  -> its existing JSON envelope codec

crate-root public non-PyO3 wrappers
  -> their P0-mapped private implementation

all child implementations
  -> crate-root public contract types (type-only compatibility leaf)
```

gRPC bridge 可以分别调用 service 与 transport codec；service/kernel/backend 不得反向依赖 Proto/transport codec。PyO3 不强制经过 service：Skill bridge 继续直达 Skill contract/policy facade，Core/Lifecycle bridge 继续直达对应 contract/transition kernel，Safety bridge 继续直达既有 sanitizer kernels；P0 对每个导出记录 exact target/call count，禁止为对称性增加 service 或 I/O。crate-root public contract type 是为保持 canonical path 的 type-only 例外，不得反向调用 child implementation；root public wrapper 可以单向委托已记录的 private implementation。禁止按箭头反向构造 config/service，也禁止因拆文件形成行为 cycle。

## 10. 系列计划总览

| 计划 | 主目标 | 风险级别 |
|---|---|---|
| P0 | 冻结公开合同、行为 trace 与环境门禁 | 低 |
| P1 | Core `StoragePort` 子协议、证据 primitive 与基础依赖方向 | 低 |
| P2 | Orchestration/Capabilities 所有权、continuation、Skill authority、Prompt | 中到高；continuation/slot/API wiring 为高风险 |
| P3 | Agent Skills、Result Parser、Gateway、Coordinator | 中到高 |
| P4 | `ApiRuntime` 内部组件与 factory | 中到高 |
| P5 | Storage/State/Lifecycle 与 SQLite/PostgreSQL parity | 高 |
| P6 | Frontend App、reducers、controllers | 中到高 |
| P7 | Rust 大型模块与 scripts | 纯 impl 搬迁为中；SQLite/Sandbox/gRPC/PyO3/operator 为高 |
| P8 | 已确证死代码/重复收尾与全仓最终证明 | 低到高，按项分开 |

每个计划在进入实现前生成基于当时 HEAD 的详细实施计划，避免预先写出随后失真的实现步骤。

### 10.1 Deliverable/gate 状态与依赖解锁

状态落在具名 deliverable 或 gate ID 上，不用一个 scalar 同时概括本地、PostgreSQL、MCP 和 Ubuntu 结果。每个 plan artifact 包含 `local_status`、`deliverables[]`、`required_external_gates[]` 与 `blocking_gate_ids[]`；只有 local deliverables 与全部 required gates 都 complete 时，plan 才能报告 `complete`。

| 状态 | 含义 | 是否可解锁依赖该 ID 的后续 |
|---|---|---|
| `pending` | 尚未开始 | 否 |
| `in_progress` | 当前检查点正在实现或验证 | 否 |
| `complete` | 该具名 deliverable/gate 的全部 mandatory evidence 闭合 | 是 |
| `bounded_deferred` | 仅明确非 mandatory、out-of-scope 行为修复延期，已记录 exact scope、owner、风险和重入条件 | 不作为 mandatory dependency；不得承载外部证据缺口 |
| `blocked_external` | required PostgreSQL、远端服务或权限不可用 | 不解锁依赖该证据的计划；独立计划可继续 |
| `pending_platform` | required Ubuntu/Linux/manylinux gate 未运行 | 不得把相关 Result Parser 或 Rust 检查点标为 complete；独立计划可继续 |
| `failed` | 行为、合同或门禁漂移 | 否；必须回滚当前检查点 |

解锁按具名 gate 而不是编号或 plan scalar：P1～P4、P6 与 P7 纯机械检查点依赖 `P0.local-contract`；P3 Slot facade 依赖 `P2.interrupt-authority`，P4 interrupt adapter 同时依赖 `P2.interrupt-authority` 与 `P3.slot-domain-facade`；P4 MCP wiring 其余检查点依赖 `P3.local-integrations`，本地闭合后产出 `P4.local-mcp-wiring`，不要求等待 P3 external gates；`P3.real-mcp` 依赖这两个 local gate并在最后一个相关 implementation commit 后运行。P3 Result Parser 收口额外依赖 `P3.ubuntu-result-parser`；P5 各高风险切片依赖 11.6 表中对应的 8 个稳定 `P0.pg.*` leaf；P7 各检查点按 18.5 选择三个稳定 `P7.ubuntu.*` leaf。一个 plan 可以同时拥有 `local_status=complete`、某个 `P0.pg.*=blocked_external`、`P3/P7.ubuntu.*=pending_platform`，报告必须逐 gate 展示全部 blocker，不能用优先级覆盖或把局部完成写成 plan complete。无依赖的 P6/P7/P3 检查点可以继续，但项目级状态仍不得标为 `complete`。

`bounded_deferred` 只适用于明确非范围行为修复，不得用于外部 gate，也不得用于跳过 `ApiRuntime`、Storage、Frontend App、MCP Gateway/Coordinator 或 Rust 核心结构目标。任何 mandatory structural deliverable 未完成时，最终只能报告已完成的具名 deliverable 与全部 blocker。

## 11. P0：行为与兼容基线冻结

P0 只增加测试与证据，不修改业务实现。

### 11.1 机器可读证据

P0 必须创建 `docs/checkpoint/progressive-architecture-cleanup/`，至少包含：

- `business-source-inventory.json`：路径、语言、`primary_owner_plan`、`authorized_touch_plans`、状态、finding IDs、证据命令；
- `public-contract-manifest.json`：import、signature、default、async、module/type/object identity；
- `dependency-edge-inventory.json`：现有 edge、允许/禁止分类、primary owner/authorized touch plan、退出条件；
- `storage-port-ownership.json`：259 个方法到唯一子协议的映射；
- `postgres-override-ownership.json`：MRO、effective defining class/module、override set、transaction runner；
- `side-effect-traces/*.json`：Agent、Gateway、Coordinator、API lifecycle、Storage、Frontend runtime 的确定性 trace；
- `gate-results/*.json`：command、`start_commit`、`tested_commit`、ran/pass/fail/skip、duration、platform、artifact digest；
- `finding-register.md`：稳定 finding ID、分类、owner、状态、风险、证据和退出条件。

JSON artifact 必须包含 `schema_version`、`baseline_commit`、`baseline_tree`、生成命令和 `canonical_sha256`。摘要算法固定为：移除顶层 `canonical_sha256` 字段后，使用锁定 Python 版本的 `json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)`，再 UTF-8 encode 并计算 SHA-256；输入 number 类型不得先做字符串化/float 归一，NaN/Infinity 必须拒绝。校验器必须重算，禁止把摘要字段本身纳入摘要。不得保存 credential、DSN、用户正文、Tool 参数、raw result、附件正文或绝对敏感路径。

Gate 只绑定一个已经存在的 `tested_commit`，并在该 commit 的 disposable clean checkout 中运行，不得要求原共享工作树 clean，也不得要求 artifact 写入承载自身的 evidence commit SHA。检查点先创建业务实现 commit；若原工作树仍有无关用户 diff，保留它们并记录 status/path 与 tracked-diff bytes digest，然后用 `mktemp -d` + 从当前 repository object database 进行 `git clone --no-local --no-checkout` + 在新 clone 内 `git switch --detach <tested_commit>` 创建隔离 checkout。禁止用 stash/reset/checkout 原工作树，也不向原仓库注册 secondary worktree；Gate 前要求 clone 的 HEAD/tree exact 且 tracked status 为空。Conda/toolchain 可复用宿主环境，Node/remote cache 等非源码依赖只能按 P0 allowlist 只读接入并记录版本/digest。Ignored secret、runtime 数据和 `docker_cmd.md` 不在 Git object 中，禁止复制进 clone。

Gate 期间 disposable clone 禁止任何 agent/user并行写；Gate 后要求其无非预期 tracked diff，先完成 evidence 脱敏/摘要复制，再仅删除经 realpath 验证位于本次 `mktemp` root 的 clone；cleanup 失败写 gate failure，不得删除未知目录。原工作树的 status/diff digest 前后若不同，必须登记为 external/user drift、确认不与 checkpoint-owned path 重叠并保留最新内容，不能据此修改或覆盖用户文件；无外部 drift 时 digest 必须一致。`docker_cmd.md` metadata/ignore/tracking 状态始终必须一致。随后允许在原工作树创建一个只含 evidence、AGENTS/CHANGELOG/index 的 evidence commit；若只有测试/证据变化而无业务实现，`tested_commit` 指向该测试 commit。这样既隔离实际被测代码和无关用户 diff，也避免 commit SHA 或文件 digest 自引用。

业务源码 inventory 的原始全集必须用 NUL 分隔命令生成：

```bash
git ls-files -z -- src frontend/src native/crates scripts
```

生成器随后在程序内按扩展名和 test/spec/`#[cfg(test)]` 规则分类，禁止用 `'src/**/*.py'`、`'frontend/src/**/*'`、`'native/crates/**/src/**/*.rs'` 之类 Git pathspec 当全集，因为它们会漏掉目录根层文件。原始全集中的每个 path 都必须进入 `included_business_source` 或带 exact reason 的 `excluded_tracked_dependency`；独立 set-difference gate 必须证明 `raw_tracked == included ∪ excluded`、交集为空、unaccounted 为零，并记录各分类 count。业务范围至少覆盖：

- `src/` 下全部 `.py`，包括 `src/` 根层文件；
- `frontend/src/` 下全部 `.ts`、`.tsx`、`.css`，包括 `App.tsx`、`main.tsx`、`styles.css` 与根层声明文件，test/spec 文件只进入有理由的 excluded set；
- `native/crates/` 各 crate `src/` 下全部 `.rs`，包括根层 `lib.rs`/adapter 文件；文件纳入审计但 `#[cfg(test)]` block 不作为清理目标；
- 受跟踪的 `scripts/**`，其中权限 SQL、保护 shell 和 executable mode 可以标 `reviewed_no_change`，不能从 inventory 消失。

每个路径必须有且只有一个 `primary_owner_plan`。跨计划装配接线等必要修改写入 `authorized_touch_plans`，每项包含计划、exact symbol/lineage、允许动作、前置 gate 和退出条件；secondary touch 不取得文件所有权。初始 inventory 可标 `planned_change`，P8 必须将其清零；最终路径只能标 `changed` 或 `reviewed_no_change`。明确非范围的行为问题另写 finding，外部缺口另写 `blocking_gate_id`；二者不得改变 path status 或使 mandatory structural work 看似闭合。`api/runtime.py` 等多计划交接文件必须显式记录 P2/P3 wiring touch 与 P4 primary ownership。Checked-in contract JSON、Proto、Cargo/Node/Python lock/config 属验证依赖，不作为普通清理目标，但其差异必须进入 gate result。

Inventory 是随检查点演进的 ledger，不是只含 P0 路径的静态 snapshot。P1～P8 每个检查点都重新运行 raw tracked-set 命令，并为变动记录 `baseline_path`、`current_path`、`action`（`created|modified|renamed|deleted|reviewed`）、primary owner、authorized touch 与 parent finding。删除路径保留 tombstone，rename 同时关联 old/new path，新文件不得因不在 P0 snapshot 而逃逸。P8 必须对当前 raw tracked set、历史 tombstones 和 inventory 做双向 set-difference，三者无未解释路径。

P0 还要解析本次范围内各层 `AGENTS.md` 中形如仓库相对路径的反引号项，生成 `agents-index-audit.json`，逐项记录 exists/tracked/ignored/intentional-external；命令、环境变量和通配说明不得误判为路径。已知 `scripts/AGENTS.md` 引用但当前不存在的 `scripts/stage_user_mcp_cp7_inputs.py` 必须进入 finding register，不能据此臆造入口；只有对应目录索引被修正或明确标为 intentional historical reference 后该 finding 才闭合。

### 11.2 Python 公开面与 trace

- snapshot `src.api`、`src.capabilities.main_agent`、`src.orchestration.agent_loop`、`src.integrations`、`src.integrations.mcp`、`src.integrations.agent_skills`、Result Parser service/worker 的公开导出；
- 冻结 `ApiRuntime`、`build_api_runtime` 签名、默认值、属性与 patch seam；
- 冻结旧 `src.integrations.codex_skills` alias、`MCPGateway`、`UserMCPDispatchCoordinator`、adapter、worker entry/checkpoint 的 import、signature、`__module__`、pickle/spawn；
- 冻结 `src.capabilities.main_agent.__all__`，以及 `PromptEnvelopeMode`、`MainAgentPromptResolution`、`resolve_main_agent_prompt_envelope_mode`、`build_main_agent_rendered_prompt`、`build_main_agent_rendered_messages`、`build_main_agent_prompt_envelope`、`resolve_main_agent_prompt_for_mode`、`resolve_main_agent_trim_max_tokens`、`prompt_envelope_audit_payload` 的直接模块路径、签名与类型/对象身份；
- 冻结 `StoragePort` 259 个方法的名称、async 属性、签名/default、class object、`__module__`、`__qualname__`、三路径 `is`、MRO 和 runtime-checkable `isinstance`；
- 对公开 constructor/function/method 同时冻结 raw `__annotations__`、`inspect.signature` 和 `typing.get_type_hints(..., include_extras=True)` 的解析类型/object identity，防止 postponed annotation 或同名 alias 掩盖协议变化；`MCPTaskPresenceService.storage` 当前 `Any | None` 必须原样记录，不得为了对称性改成窄 port；
- 冻结公开导出的 `SQLiteCollaborationRepository`、`SQLiteStateRepository`、`SQLiteStorage`、`PostgreSQLStorage` 的 class identity/MRO、public descriptor defining class/module/qualname/signature/async、effective implementation、patch seam；记录 `SQLiteStorage._run` 在同一 Session 内依次构造 State/Collaboration repository、执行 callback、commit 的精确关系；
- 冻结关键 Python 类型的 module identity 与 pickle；
- 为 Agent waiting/continuation、MCP Coordinator/Gateway、startup/shutdown、SQLite `_run` cancellation 和 PostgreSQL transaction owner 建立 side-effect trace。

时间、UUID、event ID、idempotency key 等可观察字段必须通过注入固定 clock/generator 得到 exact trace，不得通过删除字段来“归一化”漂移。只有纯 wall-clock duration 可以按 manifest 中逐字段记录的理由排除 exact comparison。

### 11.3 先替换 implementation-shape tests

已知以下测试包含 `inspect.getsource` 或同类文件布局断言：

- `tests/storage/test_user_mcp_postgres_schema_contract.py`；
- `tests/storage/test_user_mcp_cp7_postgres_integration.py`；
- `tests/storage/test_postgres_conversation_delete.py`；
- `tests/storage/test_sqlite_task_repository.py`；
- `tests/storage/test_sqlite_interrupt_repository.py`；
- `tests/lifecycle/test_rust_lifecycle_contract.py`；
- `tests/storage/test_rust_runtime_sidecar_contract.py`。

该手工清单不是全集。P0 必须 AST 扫描全部受跟踪 `tests/**/*.py`，识别 `inspect.getsource`、读取生产 `.py` 后做 substring/regex/AST 的断言、`__code__`/descriptor owner 与 class-body 布局检查，生成 `source-shape-test-inventory.json`：测试 path/symbol、目标生产 symbol、断言性质、安全价值、主责计划、replacement 与删除条件，unclassified 为 0。

P0 必须先在旧实现上增加更强的 effective-method、SQL/lock/transaction trace 与结果断言并运行通过，随后才可删除仅要求方法位于同一 class body 的断言。针对 `CancellationService` 的 Rust/Lifecycle source assertions 要先锁 raw/resolved annotation、off/shadow/enforce、cancellation-token write call/顺序和实际 private implementation，再迁移断言，避免下沉后误通过或误失败。仍有安全价值的 method-level source/AST 断言可以保留，但必须跟随 effective defining method，不能假定 mixin 前的文件布局。该步骤是行为锁迁移，不是测试代码清理。

### 11.4 Frontend

- 锁定 upload + keep-open interrupt；
- 锁定旧 generation/conversation 的异步响应不可写入新 scope；
- 锁定 CP7 unknown/late-result、cancel reconcile 和 artifact completion；
- 冻结 `WAITING_INPUT_CHECK_DELAY_MS=8000`、graph fallback `maxAttempts=30`、`waitingInputCheckDelayMs` 测试注入行为、`WAITING_INTERRUPT_RETRY_DELAY_MS=250`、`WAITING_INTERRUPT_MAX_RETRIES=6`、`EVENT_STREAM_RECONNECT_DELAY_MS=1000`、`CANCEL_RECONCILE_DELAY_MS=250`、`CANCEL_RECONCILE_MAX_ATTEMPTS=10`；
- 在 React StrictMode 下锁定 fetch、subscription、timer 和 cleanup 次数；
- 不为现存 UI bug 增加修复性断言。

### 11.5 Rust

- 冻结 root imports、public signatures、serde contract、error strings 和 Cargo dependencies；
- 冻结 root-defined public type 的 `std::any::type_name`，并冻结 `RuntimeSidecarSqliteAdapter` 的 `sqlite_adapter` canonical module path；
- 枚举 root-defined public free functions，以可实例化 representative function item 的 `std::any::type_name_of_val`/compile fixture 冻结 def path；对 public `async fn` 还要构造但不执行 representative future 并冻结其 `type_name_of_val`。body 下沉时保留物理定义在 root 的签名一致 thin wrapper，不允许仅 `pub use child::function`；
- 对 public type/function 另存 outer attribute/qualifier token manifest：至少 `#[must_use]`（含 message）、`#[deprecated]`、`#[cfg]`/`cfg_attr`、`pub` visibility、`async|const|unsafe|extern ABI`、generics/where；root wrapper 必须复制 exact。外部 compile fixture 使用 `#![deny(unused_must_use)]`，防止 wrapper 漏属性而普通 import/type-name仍通过；
- byte-level 比较下表 6 份 checked-in contract 与各自 Cargo export binary 输出；Cargo bin name 使用 hyphen，不能把 source stem 的 underscore 当作 `--bin` 名；
- 建立外部-crate root-import compile fixture，冻结 `pb::{common,runtime,skill}::v1` 与 PyO3 module/function/JSON envelope；逐 PyO3 export 记录 direct canonical target、是否经过 service、调用/I/O次数，禁止跨 crate 套用统一 bridge 链；
- 建立 operation-by-operation 内存/SQLite parity matrix：task submit/get/list/active、Agent commit/get/list/final、node、artifact、event、lease、cancel、bundle。success 对每个 operation 必须覆盖；exact retry、conflict、CAS 和 invalid idempotency 只适用于签名/合同实际支持该语义的 mutation，纯 read 或无相应 key/version 的 operation 必须逐项标 `N/A` 并给 signature/contract reason，禁止把 N/A 伪装为 pass；
- parity 比较 domain record、typed error code/category/retriable、安全 metadata 规定子集和最终 state；SQLite reopen/durability/sqlite_error metadata 只能作为逐项 backend-only 断言，不得使用通用 allowlist；
- 测试 adapter 只存在于 tests，不新增生产 backend trait。

| Producer crate | Cargo `--bin` name | Bin source | Checked-in artifact |
|---|---|---|---|
| `maf_core_types` | `export-core-contract` | `native/crates/maf_core_types/src/bin/export_core_contract.rs` | `src/core/rust_contracts/core_contract.json` |
| `maf_lifecycle` | `export-lifecycle-contract` | `native/crates/maf_lifecycle/src/bin/export_lifecycle_contract.rs` | `src/lifecycle/rust_contracts/lifecycle_contract.json` |
| `maf_runtime_store` | `export-runtime-sidecar-contract` | `native/crates/maf_runtime_store/src/bin/export_runtime_sidecar_contract.rs` | `src/storage/rust_contracts/runtime_sidecar_contract.json` |
| `maf_skill_runtime` | `export-skill-runtime-contract` | `native/crates/maf_skill_runtime/src/bin/export_skill_runtime_contract.rs` | `src/integrations/agent_skills/rust_contracts/skill_runtime_contract.json` |
| `maf_audit_sanitizer` | `export-safety-contract` | `native/crates/maf_audit_sanitizer/src/bin/export_safety_contract.rs` | `src/integrations/rust_contracts/safety_contract.json` |
| `maf_mcp_runtime` | `export-mcp-runtime-contract` | `native/crates/maf_mcp_runtime/src/bin/export_mcp_runtime_contract.rs` | `src/integrations/mcp/rust_contracts/mcp_runtime_contract.json` |

### 11.6 PostgreSQL required profile

P0 必须新增可实际执行的普通 StoragePort PostgreSQL parity suite，以及真正执行 owner guard、claim、terminal mutation 的 CP7 suite。至少覆盖：

- auth generation/rotate/clear CAS；
- Task/TaskNode CAS；
- mailbox、interrupt 与 event ordering；
- owner guard 首次接管、claim takeover、terminal commit；
- rollout API 与 permissions 的角色隔离；
- legacy migration 专用 role；
- conversation delete 并发；
- fresh bootstrap、metadata/DDL drift 与事务 rollback。

稳定 leaf gate 与 P5 解锁映射如下，后续 artifact/`blocking_gate_ids` 禁止用通配符替代：

| Gate ID | 必须覆盖 | 解锁切片 |
|---|---|---|
| `P0.pg.storage-parity` | auth generation/rotate/clear、Task/TaskNode CAS、mailbox/interrupt/event ordering | P5 Auth、Task/Lifecycle shared→PG |
| `P0.pg.cp7-authority` | owner guard 首次接管、claim takeover、terminal mutation/commit | P5 MCP dispatch/CP7/Remote Task |
| `P0.pg.rollout-permissions` | rollout API、app/snapshot/evaluator/operator role isolation | P5 MCP rollout/observability |
| `P0.pg.legacy-migration` | dedicated migration role、first apply/exact replay/conflict、durable record | P5 legacy compatibility runners |
| `P0.pg.conversation-delete` | concurrent delete、guard/rollback/final state | P5 Conversation |
| `P0.pg.bootstrap-ddl` | fresh bootstrap、single metadata、table/index/constraint/DDL digest drift | P5 SQLAlchemy base/mapper registration |
| `P0.pg.mvcc-concurrency` | competing claim/CAS/lock winner与旧 snapshot可见性 | P5 owner/claim/remote high-risk paths |
| `P0.pg.transaction-rollback` | 每个受影响 runner 的 exception point、all-or-zero、session/commit/rollback | P5 所有 PG transaction-owner 迁移 |

每个 profile 必须记录准确 env 名、命令、预期测试数和 `skipped == 0`。测试必须使用模块独立、可删除、明确非生产的数据库/schema/role；仅检查 DSN 字符串存在不构成 live evidence。P5 高风险切片只有在对应 profile 真正执行通过后才能开始。

### 11.7 P0 本地与外部门禁分层

P0 本地 manifest/import/trace/SQLite/Frontend 门禁完成后，可解锁不依赖外部环境的 P1～P4、P6 与 P7 纯机械检查点。PostgreSQL profile 未运行时状态为 `blocked_external`，只阻断 P5 对应切片；Ubuntu 未运行时状态为 `pending_platform`，阻断 P3 Result Parser worker-supervisor 收口、P7 对应高风险检查点和项目最终完成，但不阻断 P3 其他独立检查点。

## 12. P1：Core 协议基础层

P1 只拆 Core 中跨层共享的协议与无副作用证据 primitive，不迁移领域 helper 或有状态控制器。各业务域的纯 helper 与兼容门面仍由 P2～P7 的唯一主责计划处理，避免两个计划同时拥有同一迁移。

### 12.1 StoragePort 子协议

新增 `src/core/storage_ports/`：

- `auth.py`
- `conversation.py`
- `lifecycle.py`
- `mcp_config.py`
- `mcp_dispatch.py`
- `mcp_remote_task.py`
- `mcp_rollout.py`
- `security.py`

这些模块只定义跨层 persistence Protocol，不得包含 MCP routing/execution、默认策略、I/O、实现 helper 或 capability workflow。`security.py` 唯一拥有 master-key validation 两个方法，259 个方法必须在 ownership manifest 中恰好归属一次。

公开 aggregate `StoragePort` 必须继续物理定义在 `src/core/contracts.py`；`src.storage.interfaces.StoragePort` 与 `src.storage.StoragePort` 继续原样导出该对象，不得从新 `composite.py` re-export。P1 第一阶段只让新内部消费者使用窄子协议，公开 aggregate 的 259 个方法声明保持原位。

本轮禁止让公开 aggregate 继承新子协议，因为该动作必然改变其 MRO/descriptor owner。aggregate 的 259 个声明镜像原位保留，由 ownership/signature test 校验其与窄协议 union 一致；声明镜像只描述 persistence contract，不是第二套业务实现。若未来要改变 aggregate 继承关系，属于本轮之外的独立兼容性变更，必须另行批准。

Core 作为该 aggregate 的 canonical owner 是当前跨模块 contract 边界的显式决定。Core 只拥有 persistence shape，不拥有 MCP 或 Capability 业务流程；将 canonical owner 迁出 Core 不属于本轮范围。

### 12.2 唯一计划所有权

- capability event 与 prompt helper 归 P2；
- Agent Skills、MCP 与 Result Parser helper 归 P3；
- API bootstrap/trust/registry/event helper 归 P4；
- SQLAlchemy mapper、repository support 与 storage helper 归 P5；
- Frontend wire contract 与 domain helper 归 P6；
- Rust 常量、crate-private validation 与 scripts helper 归 P7。

所有计划都遵守同一抽取规则：helper 只能承载完全相同的纯逻辑，调用方仍负责抛出各自原异常。

### 12.3 P1 验收

- 三条公开 import 使用 `is` 指向 `src.core.contracts.StoragePort`；
- `StoragePort.__module__ == "src.core.contracts"` 且 `__qualname__ == "StoragePort"`；
- 子协议 method union 恰为 259，无缺失、重复或无 owner 方法；
- 所有签名、default、keyword-only、async 属性与基线一致；
- `isinstance(SQLiteStorage(...), StoragePort)` 保持；
- 新消费者只依赖所需窄协议，AST import gate 不新增反向 edge；
- `tests/core/test_contracts.py`、`tests/storage/test_sqlite_bootstrap.py` 和 Storage 全量通过。

## 13. P2：Orchestration 与 Capabilities

### 13.1 规范性权威

P2 只做结构等价迁移，不重新解释统一 Agent Loop。若本设计、后续 implementation plan、源码注释或局部测试发生冲突，以 `docs/prd/backend/unified-agent-loop/README.md`、总纲及 Phase 2/3/4 PRD 为准。唯一已确认的例外是 13.6 `BEHAVIOR-ORCH-PARALLEL-001`：它明确记录当前代码与架构理想的既有偏差，本轮因“功能不变”只允许保留并延期，不得把它反写成统一权威的新规范。

P2 必须持续满足统一权威的 FR-1、FR-8～FR-10、FR-13、FR-15～FR-18、FR-20、FR-23～FR-26。`AgentToolCatalogBuilder`、`CapabilityInvocationPolicy` 与 Runner 唯一负责 capability 可见性、model payload filtering、schema 校验和 effective payload；policy-filtered payload 是 Invocation Kernel 的输入。`CapabilityInvocationService` 唯一负责 route handoff、instance selection、execution metadata/request、executor 调用、late-result 与 semantic classification。

TaskNode 当前是两个有序写阶段而不是单写 owner：`AgentTaskInvocationCommitPort` 先做 TaskNode/Event/Interrupt/visible-question projection；随后 `AgentAtomicWriter.commit_agent_call_outcome` 在 AgentItem/outcome/waiting set/Run revision 同一事务中再次写最终 Node status/output refs。两次写都必须保留，后者不是可删除的重复。`AgentAtomicWriter` 仍是 Agent outcome 原子集合的唯一 authority；projection port 不得自行提交 AgentItem/waiting/revision。Skill adapter 不是 waiting 顺序 authority。

### 13.2 必需检查点

1. 锁 Agent item/event/interrupt/continuation 的完整顺序；
2. 仅将当前供 MCP Tool 使用的 `main_agent.helpers.make_event` 原样迁到 `src/capabilities/events.py`，旧路径 forwarding；不得替换 event-id material 不同的 `SkillExecutor._make_event`；
3. 收敛 MCP selector/router 中仅 generator/repair loop 完全相同的私有流程，不合并 prompt payload、parser、异常或 purpose；
4. 将 invocation-local continuation locator builder 与临时 handoff cache 移到 `agent_loop/continuation.py`；该 cache 不是 durable authority，重启时允许为空；
5. 定义 `InterruptAuthorityPort`，先用 adapter 包裹旧实现；
6. 将 Skill slot interrupt authority 移到 `capabilities/skill_tool/slot_interrupt_authority.py` 并由 API 装配注入；
7. 令 Main Agent prompt wrapper 委托通用 prompt profile/envelope，同时保留原公开与直接模块 import；
8. 最后删除 P0 明确判定为私有、无 import/spawn/registration 作用且零引用的 dead code。

候选新文件名可以在 implementation plan 中调整，但以上 owner、顺序与验收不是可选建议。

### 13.3 `InterruptAuthorityPort` 合同

```python
@dataclass(frozen=True, slots=True)
class InterruptAuthorityResult:
    interrupt: Interrupt
    lifecycle_event_payload: Mapping[str, Any]

class InterruptAuthorityPort(Protocol):
    async def persist(
        self,
        interrupt: Interrupt,
        *,
        now: datetime,
    ) -> InterruptAuthorityResult: ...
```

合同必须满足：

- 每个 interrupt 最多调用一次；
- 非 slot interrupt 原样返回，event payload 为空；
- slot event payload 来自持久化后的 carrier 投影；
- `_agent_continuation` 逐字段保留；
- port 异常按原类型传播，不得变成 no-op、空结果或另一错误；
- 保持现有部分写入与异常边界，不顺手改成新事务；
- production assembly 缺少实现时 fail closed，不得静默跳过；
- Slot authority 只持久化 SlotCollection/SlotEvent 并返回转换后的 Interrupt；Orchestration 继续唯一负责外层 `save_interrupt`、visible message、node event 与 Agent outcome。

### 13.4 Invocation 到 `WAITING_FOR_INPUT`

以下 trace 必须 exact：

1. sample、tool-call 与 reserved-result 已持久化；
2. 计算 policy/effective payload；
3. ownership 校验；
4. selected-route/capability authority 校验；
5. instance selection；
6. TaskNode CAS 为 `RUNNING`；
7. `node.started`；
8. 构造 authoritative execution metadata；
9. executor 恰好调用一次；
10. 再次 ownership 校验并执行 late-result/cancel 分类；
11. capability events；
12. 按现有 `save_task_node` 路径持久化 `WAITING_FOR_INPUT`，不得引入新 CAS；
13. bind interrupt；
14. 构造 locator 并写 invocation-local cache；
15. 调用 Slot authority，持久化 slot events；
16. `_agent_continuation` 穿过 slot carrier；
17. save interrupt；
18. 持久化 visible assistant question；
19. `node.waiting_for_input`；
20. `commit_agent_call_outcome(WAITING_FOR_INPUT)` 在单一 CAS/事务内提交 Agent result/outcome、再次写 TaskNode status/output refs、更新 waiting set 与 Run revision；
21. 每个 outcome 按 call ordinal 单独执行上述原子提交；当前 wave 全部 outcome 正常提交完成后，若 waiting set 非空才释放 lease。

Route rejection 必须发生在 instance selection 和 executor 之前；任何抽象不得把 `node.started` 或外部副作用前移。

### 13.5 `WAITING_FOR_DEPENDENCY`

必须保持：

```text
capability events
-> TaskNode WAITING_FOR_DEPENDENCY
-> safe_remote_task_ref
-> conversation owner
-> authority digest
-> continuation locator
-> publish remote binding
-> node.waiting_for_dependency
-> Agent outcome commit
-> wave outcome ordering
-> lease release
```

Remote binding 未成功发布时不得把 outcome 伪装为可恢复 waiting，也不得重发外部 Tool。

### 13.6 Multi-waiting 与 Resume

- 当前 parallel wave 在 `_execute_wave()` 正常返回时必须全部执行完，并按 call ordinal 提交全部 outcome；
- 并发 call 只保证各 call 内 causal order 与 Agent outcome ordinal；不得虚构同一 wave 不同 call 的全局 event 完成顺序；
- 同一 wave 可以有多个 waiting call，后续 wave 不得启动；
- waiting 集合非空时 model-call spy 必须为 0；
- 回答一个 waiting call 只能移除该 call，remaining 非空时继续 waiting；
- resume 顺序必须为 `identity validate → reacquire same Run lease → reload waiting/durable authority → continue capability-specific authority → validate resolution + reload identity/state → one atomic CAS(continuation item + original result/outcome + TaskNode final projection + waiting-set branch + Run revision) → commit 后 ack`；
- resolution 再次返回 waiting 时保留 exact call、result 继续 reserved，随后释放 lease；resolution terminal 但仍有其他 waiting 时只移除 exact call并释放 lease；最后一个 waiting terminal 时移除 exact call并在同一 lease 下恢复 remaining wave，batch closed 后才允许同一 model binding 再 sample；
- commit 成功但 ack 失败的 duplicate delivery 必须从 committed result 收敛，只重试 ack，不再次 resolve capability；
- 新构造、空 handoff cache 的 continuation service 必须仍能从 Interrupt/slot carrier、MCP aggregate/remote binding 和 Agent outcome 恢复原 Run/call/model binding；cold locator reconstruction 本身不得从内存 cache 猜 locator或调用 resolver/executor，但收到新的有效 answer/approval/remote authority 后按下列 resume-kind 调用合同执行。

Resume-kind 调用次数必须分开锁定：

| 场景 | Resolver/executor/external call 合同 |
|---|---|
| 仅 cold locator/cache reconstruction，尚无新 authority delivery | resolver 0、executor 0、network 0 |
| committed duplicate、terminal repair、unknown no-replay | resolver/executor 0；只允许幂等 ack/投影修复 |
| 新接受的 Skill missing-input answer | 继续原 capability authority恰好 1 次；不得创建新 call/sample |
| MCP approval | 批准前 Tool send 0；首次有效批准后原 Tool 最多 1 次；duplicate approval Tool 0 |
| remote completion/dependency | 只 adopt/poll/commit既有 remote authority；`tools/call` 重投 0 |

Parallel exception 路径冻结当前实现并登记 `BEHAVIOR-ORCH-PARALLEL-001`：普通 `asyncio.gather` 的首个异常向上传播，不取消已启动 sibling；sibling 可能完成既有外部/TaskNode/Event projection，但 `wave_results` 未返回，因此该 wave 的 Agent outcome commit loop 一项也不执行，已发生的外部副作用不回滚。异常收口必须按现有入口分开保持，不得伪造一个全局 failure owner：

| 入口 | 当前收口合同 |
|---|---|
| `_schedule_execution → _run_execution` 的正常/兼容执行 | `_run_execution` 捕获 `Exception`，经 `_mark_task_failed(execution_crash)` 调用 Agent fail CAS，收敛 Task/Run 并写 `task.failed`；较晚 sibling 按现有 ownership recheck 决定后续 projection |
| HTTP Interrupt/Slot answer 直接走 `_resume_agent_interrupt → AgentRunRecoveryCoordinator` | continuation 内的异常同步向 answer request 传播；该路径当前不调 `_mark_task_failed`，不得在结构迁移中暗自改成通用 terminalizer |
| `start() → _recover_agent_runs` 或启动阶段 MCP dispatch recovery | 异常向 `start()` 传播并阻止 ready，不经 `_mark_task_failed` |
| post-ready `_reconcile_mcp_dispatch_recovery` background task | 异常保存到 `_mcp_post_ready_recovery_error`，不经通用 Task failure path |

P0 barrier fixture 必须分别让 sibling 在入口收口前/后越过副作用点，对上表四类入口各自锁定 exception target、HTTP/startup/background outcome、reserved results、Node/Event、Agent outcome count=0、Task/Run/claim/waiting/lease 实际终态与 event 顺序；只有第一行断言 `execution_crash`/`task.failed`。该行为与统一架构的 fatal sibling-cancel 理想存在既有偏差，本轮按功能不变登记延期；不得换成 TaskGroup、`return_exceptions=True`、显式 sibling cancel、all-settle，也不得把后三类入口新接到 `_mark_task_failed`。若迁移无法原样保持，P2 该检查点阻断。

### 13.7 P2/P3/P4 交接

- P2 只允许在 `api/runtime.py` 修改新 service 的 import、构造和注入行，不抽取 API helper；
- P3 必须保留 `SkillSlotInterruptAuthority` 消费的 Slot carrier facade/re-export，并重跑 P2 slot authority tests；P3 不修改 `InterruptAuthorityPort` 或 Agent waiting 顺序；
- P4 以 P2 完成后的 wiring 为冻结输入；P4 的 `runtime_components/interrupts.py` 只拥有认证、request/runtime adapter、依赖注入、调度和公开响应投影，不复制 Slot authority、Agent waiting 或 durable resume 逻辑；
- capability missing-input 的初始 carrier→SlotCollection bootstrap/ref 只由 P2 `SkillSlotInterruptAuthority` 完成；
- Slot carrier codec、纯 transition/validation/question domain 由 P3 Agent Skills stable facade 拥有；
- HTTP answer/cancel/recovery 调度由 P4 API interrupt adapter 调用上述 facade；
- SlotCollection/SlotEvent CRUD、CAS 与事务由 Storage port/P5 implementation 拥有；
- `file_selection_runtime.py` 的普通 file/sheet-selection Interrupt 继续由 API file-selection domain 处理，不经过 `InterruptAuthorityPort`，也不被误称为 SlotCollection transition。

交接 gate：

| Gate ID | 闭合条件 | 直接解锁 |
|---|---|---|
| `P2.interrupt-authority` | port/adapter、initial slot bootstrap、continuation preservation、P2 fault matrix 全绿 | P3 slot facade；P4 interrupt adapter |
| `P3.slot-domain-facade` | carrier codec/transition/validation/question facade 与 P2 consumer compatibility 全绿 | P4 interrupt adapter |
| `P3.local-integrations` | P3 本地 Agent Skills/Parser/Gateway/Coordinator contracts 全绿，不含 external/platform gates | P4 MCP assembly/wiring |
| `P4.interrupt-adapter` | 同时消费前两 gate，HTTP answer/cancel/recovery trace exact，无重复 authority | P4 interrupt/lifecycle后续检查点 |

Parser/Gateway/Coordinator 等与 Slot 无关的 P3 检查点只依赖 `P0.local-contract`，不得因上述交接被无谓串行化。

### 13.8 Prompt 兼容

`MainAgentPromptResolution` 必须继续定义在原模块；generic resolution 必须映射回原类型，不能直接替换类型 identity。四种 mode 的 prompt/messages、异常、template id/version、audit keys/value、LLM projection 和 shadow render fallback 必须 golden-equal。

P0 冻结的 `src.capabilities.main_agent.__all__` 与 11.2 明列的直接模块符号只有在 P0 inventory 明确标为私有且无仓外兼容承诺时才可删除；否则保留薄 wrapper。`main_agent.helpers.make_event` 若被 manifest 判为兼容符号，旧模块必须保留签名一致、在旧模块物理定义的 forwarding wrapper，以保持 `__module__`；只有判为私有时才允许裸 alias/re-export。

### 13.9 P2 验收与故障注入

| 验收项 | 必须结果 | 主要证据 |
|---|---|---|
| route reject | 不选择 instance，executor 0 次 | `tests/orchestration/test_agent_invocation.py` |
| capability execute | 每个 call 恰好 1 次 | invocation trace |
| waiting trace | 13.4/13.5 逐项 exact | Agent invocation/continuation tests |
| multi-waiting | outcomes 按 ordinal；关闭一个时 model 0 次 | Agent loop/continuation tests |
| restart/resume kinds | 空 cache locator reconstruction 0 调用；新 Skill answer/MCP approval/remote/duplicate 按 13.6 表 exact | lifecycle recovery/API continuation call-count tests |
| slot carrier | continuation 完整，duplicate answer 不重复 slot event/message/outcome | Skill slot/API interrupt tests |
| prompt | 四 mode golden equality，原类型与 import 保留 | prompt envelope/profile/main-agent tests |
| boundary | `task_projection.py` 不再 import `missing_input_interrupt` 或调用 `slot_collection_*` | AST import gate |

必须覆盖的 fault：slot collection 已写但 interrupt 未写、interrupt 已写但 visible message 未写、locator 已构造但 authority persist 报错、remote binding publish 失败、outcome commit 失败、parallel invocation 异常、restart/duplicate delivery，以及 resume 的 waiting→waiting/terminal+others/last-terminal/commit后ack失败。P0 先生成 `side-effect-traces/p2-fault-state-matrix.json`；每个中断点记录 projection-port Node 写与 atomic-writer Node 重写各自的 before/after/call count，并记录 AgentRun status/revision/waiting IDs、SlotCollection revision/events、Interrupt、visible message、node event、reserved/committed result item、lease owner/token/release/terminal clear、ack 以及 resolver/executor/external call count。该 artifact 未生成前不得进入 continuation/slot 搬迁。任何 fault 都不得新增 capability 或外部 Tool 调用；remote binding publish 失败允许保留现有 TaskNode 部分状态但不得产生 waiting outcome，outcome commit 失败不得释放 waiting lease。

## 14. P3：Integrations

### 14.1 Agent Skills

目标依赖方向：

```text
contract/schema/value/slot_contract
  -> resolution + slot_state
    -> missing-input presentation + execution
      -> public facade
```

检查点：

- 抽 `slot_contract.py`、`llm_slot_json.py`、`file_selection_validation.py`；
- 从 `execution.py` 搬出 v2 LLM slot resolution；
- 从 `slot_state.py` 搬出 prompt/extraction/coercion；
- 从 `missing_input_interrupt.py` 搬出 presentation 与 question payload；
- 保留公开 dataclass/service 的定义和 module identity；
- schema-load masking fallback 原样保留，并在 P0 锁定其 diagnostic、是否继续执行和最终结果；该 finding 进入风险表，不在 P3 改成 fail closed。

Agent Skills 必须保持两个信息通道：

- prompt-safe channel 只允许受限摘要与 public profile；
- authority-bound execution-only channel 可以在现有 `CapabilityExecutionRequest.metadata` 中持有 raw `content`、`content_base64`、`storage_key`，供 executor 构造脚本上下文；canonical key 是 `skill_artifacts`，但兼容读取顺序必须保持 `skill_artifacts or uploaded_artifacts or artifacts`，随后才按现有规则 sanitize，若结果为空才使用 `fallback_artifact_context`。该 transport metadata 不是 public/safe metadata，字段不得进入 slot/input/question prompt、LLM payload、public result metadata、diagnostic、event、audit、metric 或日志。

P0/P3 必须分别对 execution request metadata 与 prompt/public channels 做 sentinel/taint snapshot：前者证明既有 raw artifact 可达 executor且调用次数不变，后者证明相同 sentinel 不可达。Execution matrix 必须覆盖 `skill_artifacts` 优先、legacy raw `uploaded_artifacts`、legacy `artifacts`、空/falsey key 向下一 key fallback、truthy 但 sanitize 后为空时只落到 `fallback_artifact_context` 的现有语义，以及 safe-metadata 对三 key 的 raw-field 净化。另覆盖 input resolution v1/v2、slot state、public profile、result/event/audit/metric，禁止用删除 legacy/execution transport 的方式让隐私测试通过。

### 14.2 Result Parser

- 五个 `decoder_YYYY_MM_DD.py`、registry、worker entry、checkpoint 和 parser revision 保持原位；
- 仅从 service 抽 `worker_supervisor.py`，承载 spawn/pipe/timeout/terminate/kill/IPC；
- 首个 worker message timeout、非 Mapping、`worker_error` 或缺少 `checkpoint` key 是 `_run_worker` 内的 hard failure；checkpoint 的 semantic validation 不得前移，它只在第二消息处理、process join/terminate 收口和 gate release后由 `_validate_worker_checkpoint` 执行，invalid semantic checkpoint 此时 hard fail；不得伪造 successful outcome；
- 任一已验证 checkpoint（`succeeded`、`tool_error` 或 `malformed`）后的第二阶段 timeout、IPC、两次 send 之间 worker 退出、projection envelope 或 projection-store failure，必须保留该 checkpoint，只设置 `projection_error=projection_failed`；
- parser observer 是非 authority：observer 抛普通 `Exception` 时按现实现吞掉且不改变 outcome；`CancelledError` 或其他 `BaseException` 不得被 broadened catch 吞掉，继续传播并可能使已构造 outcome 不返回；两类 trace 都必须锁定；
- 父进程仍只接收不超过 4 KiB checkpoint 和 bounded projection，不读取 raw；
- gate 必须保持 1 active、8 queued、每 owner 2；queue wait 30 秒、worker wall 10 秒、mapping 64 KiB、raw 64 MiB、checkpoint 4 KiB、projection envelope 192 KiB、worker address space 512 MiB；
- cleanup 按现实现分阶段保持 exact：首消息 timeout 为 `terminate → join(2s) → kill(if still alive) → join(2s)` 并 hard fail；首消息前的其他异常/取消只在仍存活时 `terminate → join(2s)` 后原异常传播；已收到 checkpoint key、仍等待第二消息时若 timeout/EOF/`MCPResultWorkerError`，只在仍存活时 `terminate → join(2s)` 并把第二阶段降为 `projection_failed`，随后才 semantic-validate checkpoint；同阶段若 coroutine 被 cancel/抛其他 `BaseException`，必须丢弃 checkpoint、传播原异常，且只 `terminate → join(2s)`；正常/soft-projection 路径最终 `join(2s)`，仍存活时再 `terminate → join(2s)`。后 4 条路径不得顺手新增 kill；event-loop heartbeat 与 gate release次数保持；
- worker entry、checkpoint type、spawn/pickle module identity 原位；
- 不把 decoder 合并成配置驱动的大一统 parser。

证据至少覆盖 `tests/integrations/mcp/test_result_parser_worker.py` 中 checkpoint、projection-store failure、timeout/heartbeat、cancellation、gate、Linux resource limit 与 malicious regex cases。P0 必须新增 barrier-controlled process fixtures，确定性覆盖 first-send 前 cancel、first-send 后/second-send 前 cancel、checkpoint key 存在但 semantic invalid、second EOF 后 worker 已退出/仍存活；逐例断言 outcome/exception、checkpoint 是否保留、terminate/join/kill 顺序、gate release 和 observer/projection-store 调用次数。当前只 `sleep(0.001)` 后取消并检查 gate 的用例不是阶段证明。

Result Parser `worker_supervisor.py` 检查点还要求 P0 在 `.github/workflows/rust-quality.yml` 新增独立 job ID `result-parser-linux-gate`：`runs-on: ubuntu-22.04`，`actions/checkout@v4`，`actions/setup-python@v5` 固定 Python `3.13.13`，随后 `python -m pip install -r requirements.txt`。Workflow 的 pull_request/push paths 必须加入 Result Parser service/worker/projection store、`tests/integrations/mcp/test_result_parser_worker.py`、`requirements.txt` 和 workflow自身；不得假设现有 Rust job 中已存在名为 `multi_agent` 的 Conda env。Job 执行：

```bash
python -m unittest \
  tests.integrations.mcp.test_result_parser_worker.MCPResultParserWorkerTest.test_linux_worker_enforces_512_mib_and_parses_64_mib_boundary \
  tests.integrations.mcp.test_result_parser_worker.MCPResultParserWorkerTest.test_linux_malicious_schema_regex_is_terminated_by_wall_timeout
python -m unittest discover -s tests/integrations/mcp -p 'test_*.py'
```

Targeted profile 的 expected count 恰为 2、`ran=2`、`failed=0`、`skipped=0`；MCP integration full suite 也必须记录 discovered/ran/skip，以上两项不得 skip。Job 上传 `progressive-cleanup-result-parser-linux` artifact，记录 workflow/job ID、Git commit/tree、runner image/architecture/kernel、Python/pip version、`requirements.txt` SHA、target/full counts、RLIMIT before/after、timeout 与 process cleanup trace，不保存 raw 64 MiB payload。只有 artifact commit 等于 `tested_commit` 且 job success 才闭合 `P3.ubuntu-result-parser`；runner/job/artifact 缺失时该 Result Parser 子检查点和 P3 总体为 `pending_platform`。P3 其他独立检查点可继续，但不得用 macOS skip 或既有 wheel job冒充通过。

### 14.3 MCP Gateway 状态所有权

先抽无状态：

- `gateway_catalog.py`
- `gateway_metrics.py`

再抽有状态：

- `GatewayScopeRuntime`：open/bootstrap/renew/close/invalidate；
- `GatewayCallRuntime`：execute/normalize/parse/cancel/continue。

`MCPGateway` 保持公开 facade。候选两个 runtime 必须共同引用一个 `_GatewayState`，禁止各 runtime 镜像或复制 map/lock。P0 先生成逐 field/read-write-site synchronization manifest：全局 `_scopes`、`_opening`、`_opening_owners`、shadow/metric maps 与 `_lock` 仍由 Gateway state 拥有；`_ScopeState.calls`、`terminal_calls` 以及 gateway `_terminal_call_tasks` 保持现有单 event-loop owner 和锁外访问；`_ScopeState.accepting_calls` 与 `closing` 是 mixed-domain barrier，既有 `_lock` 内 open/reuse 检查及 close 时 `accepting_calls=False` + `closing=True` 原子写必须保留，同时其 call/cleanup 路径的锁外访问也不得移动。不得把 mixed field 简化成纯锁内或纯锁外，也不得把任何 state 复制到两个 runtime。

Linearization point 与 P0 trace 保持：open/close/invalidate 的全局 state registration/removal 按当前 lock site 收敛；普通 call 的 `_require_scope`、call registration/removal、terminal bookkeeping 与 metric read 保持当前 event-loop 顺序和锁上下文。cancel/gather/client close 等可等待操作在锁外对已捕获对象执行；完成后按原顺序重新进入 lock 收敛。不得在持锁区新增网络、credential、process wait 或 observer I/O。每个迁移检查点比较每个字段的 read/write site、lock-held boolean、同一临界区共同写、await 边界与 cancellation trace。

`close_task`、`invalidate_server` 与 open/call/shadow 的竞态必须继续满足 single-flight、late-open barrier、queued/opening/active cancellation 和 cleanup failure 可观测合同。

### 14.4 Gateway 安全顺序

Gateway 独占 endpoint revalidation、credential read、adapter/client、external I/O 和 isolated parsing。安全序列分成两个不得合并的阶段。

Scope bootstrap/discovery 的每次 retry 必须重新绑定单一 `ValidatedEndpoint`，并保持：

```text
lease/admission
-> endpoint revalidate + security observation
-> credential decrypt/read
-> exact ValidatedEndpoint client construction
-> initialize/discover
```

本 bootstrap attempt 在 admission 或 endpoint 拒绝时，后续 credential/client/network 次数为 0；readonly shadow client 必须使用传入 endpoint 且不新增 DNS。

已打开 scope 的普通 call 复用已初始化的 adapter，保持：

```text
safety admission
-> require existing scope
-> authorization guard
-> accepting-calls barrier #1
-> descriptor lookup + argument validation
-> call registration / active gauge / call task creation
-> optional on_created callback
-> start_allowed
-> task-call assignment guard
-> accepting-calls barrier #2
-> authoritative task load + task/shadow safety validation
-> result sink + descriptor recheck
-> existing adapter Tool send
```

两次 accepting check 是 `on_created`/task creation 与 close race 之间的 barrier，不得合并、重排或删去。各 guard/callback 失败必须保持现有 error class/code、call task cancel/gather、state.calls/terminal bookkeeping、gauge 和 sink cleanup trace，且 Tool send/network delta 为 0；bootstrap 已经发生的 credential/client 次数不回溯为 0，且不得在每个 call 重做 endpoint、DNS、credential 或 client construction。验证分别绑定 Gateway single-flight/bootstrap retry/endpoint/no-DNS 与 call authorization/argument/on_created/assignment/task safety/two-close-barrier/cleanup failure tests。

### 14.5 MCP Coordinator 与 no-replay

最高风险、最后处理：

- 先在原类内将 dispatch 与 call-tool 切成明确 phase methods；
- 再迁入 provisional `dispatch_preparation.py`、`dispatch_continuation.py`、`dispatch_terminal.py`；
- Coordinator 始终是唯一顺序控制者；
- reservation、may-have-dispatched、unknown convergence 和 no-replay 不得复制到 collaborator、Gateway 或 recovery；
- 每个 crash point 必须证明零二次 Tool 调用。

这些模块是 Coordinator 私有有状态 phase，不是可复用 pure helper：preparation 会 arm intent、写 audit、resolve/claim outbox，terminal 会写 durable aggregate。它们只能由同一 Coordinator 实例按固定 phase 调用，Gateway、recovery 和 API 禁止直接导入。若 call trace 无法证明一致，方法保留在原类中而不迁文件。

`tests/integrations/mcp/test_mcp_dispatch_fault_injection.py` 的 17 个 closed boundary 集合和逐 boundary durable proof 不得减少。P0/P3 必须让每个 fixture 同时输出 machine-readable `expected_network_delta`、spy 取得的 `actual_network_delta` 与 `restart_network_delta`，逐边界要求 actual exact-equal expected 且 restart 为 0；当前只断言 expected 属于 `{0,1}` 不是执行证据。另须保持：

- approval/MRTR 从原 action/payload 恢复，不 rerun Selector 或重新审批；
- remote Task 只 adopt + poll，不重发 tools/call；
- unknown side effect 永不 replay；
- OCR job 为 `start → poll* → persist final → best-effort ack`，terminal failure 不 ack；
- durable result、candidate、receipt、archive/delete 任一 fault 不产生第二次外部副作用。

### 14.6 Historical reprojection

- 按 Call ref keyset page=1000；
- held durable source 优先，回收后才使用 identity-bound internal managed copy；
- 全程零网络，不得获得 Gateway/client/credential/endpoint 依赖；
- raw 不进入父进程日志、异常、metric 或公共 projection；
- source deleted、tamper、missing authority 的现有 safe-hide/failure 语义不变。

P0/P3 必须为 historical service/reconciler 增加 fail-fast network spy：Gateway、client factory、credential loader、endpoint revalidator 或 transport 任一被解析/调用即测试失败，并以 AST/import gate 禁止 historical 模块依赖这些 symbol。当前测试中未连接真实依赖的常量 `network_calls = 0` 不能作为零网络证据。

### 14.7 Observability 与资源

- Gateway/Coordinator metric label、cardinality、bucket、result/error category 和写入顺序保持；不得把两者当前略有差异的 `_metric_error_category` 顺手统一；
- 八条 safety red line、gap 和 cleanup failure 继续可观测，generic metrics helper 不得吞掉；
- Result Parser observer 继续非 authority/fail-open；
- metric/audit 不得含 Tool args、credential、raw result 或附件正文；
- 每次 Agent Skill resolution 的 LLM 调用数与 prompt bytes不得增加；Gateway discovery、DNS、credential、client、network、DB round trip不得增加；每次 parse 最多一个 worker spawn且不得额外完整复制 raw。

### 14.8 禁止合并的安全 authority

- Temporary Result Store；
- Pending Action Payload Store；
- Projection Store；
- CP7 terminal candidate；
- credential/master-key domains；
- historical raw resolver/managed copy。

它们的 key、AAD、size cap、mode、identity、no-clobber、exception 和 paired snapshot 均为独立合同。

### 14.9 P3 验收

| 领域 | 必须结果 | 主要证据 |
|---|---|---|
| Agent Skills | prompt/public 与 authority-bound execution metadata 分离，sentinel transport 保留且 public forbidden keys 为 0 | Agent Skills artifact/input/slot/public-profile taint tests |
| Result Parser | first-key/semantic-validation阶段、pre/post-checkpoint cancel、process cleanup、gate/size/timeout、spawn identity exact；Ubuntu 两项零 skip | Result Parser barrier worker/service tests + `P3.ubuntu-result-parser` |
| Gateway state | 单 state owner、mixed/lock/event-loop field site exact、single-flight、close/invalidate barrier | Gateway field/await trace + concurrency/cleanup tests |
| Gateway security | bootstrap reject 后下游 credential/client/network 0；call两次accepting及各guard reject后 Tool send 0；retry exact endpoint | 分阶段 Gateway endpoint/security/call tests |
| Coordinator | 17 fault boundaries，actual=expected且 restart Tool=0 | dispatch fault-injection machine-readable traces |
| Job workflow | start/poll/persist/ack 顺序 exact，terminal failure no ack | job workflow tests |
| Historical | page=1000、source priority、fail-fast spy/forbidden import证明零网络、raw 不泄露 | historical reprojection tests/import gate |
| Observability | labels/cardinality/red-lines/cleanup 语义不变 | gateway/coordinator/rollout observability tests |
| Controlled real MCP | 5 场景、Server count exact、restart second Tool=0、redaction/cleanup/revoke 全绿 | `P3.real-mcp` artifact |

## 15. P4：API Runtime

`src.api.runtime.ApiRuntime`、`build_api_runtime`、`src.api.__init__` 导出及测试 patch seam 保持稳定。

必需检查点（新文件名为 provisional，owner 与顺序为强制）：

1. `runtime_components/bootstrap.py`、`artifact_trust.py`、`registry.py`、`event_helpers.py`；
2. `runtime_components/files.py` 与文件/附件 runtime mixin；
3. `runtime_components/conversations.py`、`task_history.py`；
4. `runtime_components/interrupts.py`；
5. `runtime_components/lifecycle.py`，整块迁移 startup/recovery/shutdown；
6. `runtime_components/service_core.py` 与 `factory.py`；
7. P4 业务组件只消费 P1 已稳定的 StoragePort facade，不在 P4 再修改该协议；composition root 仅按 9.1 例外构造具体 backend；
8. 完成旧 runtime 私有 helper re-export、patch seam 与 import smoke 的兼容收口。

### 15.1 关键限制

- env/config 不能在 import 时提前读取；
- helper 仍在原调用位置执行；
- factory 通过显式 `runtime_cls` 与 factory 参数保留 monkeypatch seam；
- `build_api_runtime` 公开 53 参数暂不改成新 config object；
- runtime holder 仍在完整构造后赋值；
- master-key sentinel、aggregate reconciliation、dispatch recovery、Agent recovery、Ready 和 post-ready remote task 的顺序不变；
- shutdown 的 quiesce、cancel/gather、CP7 close、service close 和 engine dispose 顺序不变。

`ApiRuntime` 类声明必须继续物理位于 `src/api/runtime.py`，保持 `__module__`、构造签名、实例属性名和测试直接访问的 private seam。`build_api_runtime` wrapper 保持 53 参数及原 import path；factory body 可以迁移，但 wrapper 必须在调用时显式传入当前 facade 中可被 monkeypatch 的 `RuntimeSidecarGrpcClient`、`ApiRuntime` 和其他 factory seam，禁止新模块在 import 时静态捕获替代对象。

### 15.2 Interrupt 与文件职责

| 职责 | 唯一 owner |
|---|---|
| HTTP/auth/request DTO、conversation/file scope、公开 response | API interrupt/file runtime adapter |
| 普通文件/附件消歧、sheet selection Interrupt 与调度 | API `file_selection_runtime` 与纯 file-selection domain helper；不经过 `InterruptAuthorityPort` |
| capability missing-input 初始 carrier→SlotCollection bootstrap/ref | P2 `SkillSlotInterruptAuthority` |
| Slot carrier codec、纯 transition/validation/question domain | P3 Agent Skills stable facade |
| HTTP answer/cancel/recovery 对 Slot facade 的调度 | P4 API interrupt adapter |
| SlotCollection/SlotEvent CRUD、CAS 与事务 | Storage port / P5 implementation |
| Agent waiting/outcome/visible question 顺序 | Orchestration projection-port预写 + `AgentAtomicWriter` outcome事务内Node重写，按 13.1 双阶段分责 |
| durable resume/recovery identity | Lifecycle/Agent recovery authority |

P4 的 `runtime_components/interrupts.py` 只能搬迁 API adapter 与装配代码，不得再实现 slot authority、Agent waiting 或 recovery。回答路径中调用 port 的次数、外层 InterruptAnswer/message/event persistence owner 和 resume scheduling 必须由 P0 trace 明确后原样搬迁。

### 15.3 P4 验收

- `from src.api import ApiRuntime, build_api_runtime, create_app` 与所有 P0 private seam import 成功；
- `ApiRuntime.__module__ == "src.api.runtime"`，签名/属性 manifest exact；
- patch `src.api.runtime.RuntimeSidecarGrpcClient` 仍只构造预期 client；
- env/config 读取、engine/storage、master-key、registry、orchestrator、holder 赋值顺序 exact；
- startup 十阶段 reconciliation、Ready、post-ready remote work 与 shutdown trace exact；
- P2/P3 interrupt/slot/recovery ownership无重复实现；
- API focused、API 全量、Lifecycle/Orchestration/Integrations 受影响套件通过；
- `src/api/runtime.py` facade 仅保留 class declaration、assembly/wrapper、compat re-export 与尚未迁移检查点的唯一实现，不出现第二套 method body。

以上本地项全绿后产出 `P4.local-mcp-wiring`；它只解锁真实 MCP profile，不等于 P4 plan complete。P4 plan complete 还要求 `P3.real-mcp` 的 `relevant_source_digest` 与当前 P3/P4 MCP wiring 相同；P4 任一相关修改后旧 evidence 自动失效并必须重跑。

## 16. P5：Storage、State 与 Lifecycle

核心原则：唯一 session/transaction owner 不变，方法体先原样迁移。

### 16.1 共享 SQLAlchemy 基础

新增：

- `src/storage/sqlalchemy/base.py`
- `src/storage/sqlalchemy/mappers/`
- `src/storage/sqlalchemy/repository_support.py`

先移动纯类型、row mapper、规范化和校验 helper。SQLite/PostgreSQL 继续 re-export 旧路径；PostgreSQL 不再依赖 SQLite 私有下划线 helper。

`src/storage/sqlalchemy/base.py` 是 ORM metadata 的 canonical owner；authoritative model-registration 模块必须显式导入且只注册一次。必须保持：

- `RuntimeBase.metadata is SQLiteBase.metadata`；
- SQLite 与 PostgreSQL 使用同一 metadata object；
- table/column/index/constraint set 与 canonical DDL digest 前后相同；
- compatibility import 不产生 duplicate table definition；
- State runtime schema 只依赖 shared metadata，不再依赖 SQLite 专属 base/models。

### 16.2 领域切片顺序

1. Auth 与 Conversation；
2. Task/Lifecycle projection，并给 Lifecycle 服务引入窄 port；
3. MCP config 与 owner authority；
4. MCP rollout/observability；
5. MCP dispatch、CP7、durable result；
6. Remote Task 与最终 assembly。

每个切片可以提供只承载 private implementation 的 repository/facade mixin；公开导出的 `SQLiteCollaborationRepository`、`SQLiteStateRepository`、`SQLiteStorage`、`PostgreSQLStorage` 的旧路径、class object、`__module__` 与 `__qualname__` 继续作为 assembly/兼容 facade。所有既有 public method 必须在其基线 descriptor defining class body 保留签名/raw+resolved annotation/async/descriptor owner/module/qualname exact 的薄 forwarding wrapper，mixin 只定义新 private implementation 名；基线本就继承的方法继续由原 defining class 提供，禁止在子类新覆盖，也禁止把 public method 直接下沉后只靠继承/re-export。Wrapper→private impl 调用次数、异常、patch seam 与 `super()` trace exact，wrapper 不是第二套业务实现。一个检查点只能移动 repository 或 facade 中的一层，禁止同一检查点同时改变两层 MRO/owner。

P0 必须冻结上述四个 class 的 MRO、public descriptor owner、public wrapper→effective private implementation、PostgreSQL override set 和 `super()` target。每个切片在实现前列出 exact expected post-checkpoint MRO：只允许在指定位置插入该切片的 private mixin，所有既有基类相对顺序与 public descriptor owner不变；只批准 private implementation relocation 与每个受影响 `super()` target，其余 MRO/owner delta 必须为零。`SQLiteCollaborationRepository` 的 event/mailbox/interrupt/slot/checkpoint 方法及其与 `_run` 共享 Session/单 commit 关系必须逐项映射。每迁移一个领域都更新 owner manifest；任何 PostgreSQL 专用方法意外落回 SQLite/shared owner 时立即失败。

### 16.3 事务、Session 与锁不变量

- `SQLiteStorage._run` 继续负责 session、`BEGIN IMMEDIATE`、commit、shield 和 cancellation wait；
- `_run` 每次继续以同一 Session 构造一个 `SQLiteStateRepository` 与一个 `SQLiteCollaborationRepository`，callback 参数顺序、共享 identity、flush/commit/exception/cancel 边界不得改变；不得把 Collaboration event/mailbox/interrupt/slot/checkpoint 操作拆到第二 transaction；
- repository 内现有 flush 与 CAS 失败 rollback 原样保留；
- P0 必须为每个 PostgreSQL override 记录 session factory、transaction count、lock acquisition order、flush count、commit/rollback branch、exception point 和 effective inherited/overridden owner；
- PG CP7、rollout、conversation delete、`create_user_mcp_servers_atomic`、`reserve_mcp_call`、remote-task claim、health/scope expiry、owner-server lock、legacy migration 等 runner 各自继续拥有当前 session/commit；
- Lifecycle 一次业务操作中的多次 storage 调用不得顺手合并成单事务；
- CP7 owner→server→intent→outbox→pending→branch→call→receipt→projection→candidate→durable→task→node→interrupt→answer→grant 锁顺序不得重排；
- 冻结 PostgreSQL override 集，禁止专用方法意外退化为 inherited generic path；
- SQLite `_run` 被取消时必须继续等待后台线程完成既有 transaction，再传播 `CancelledError`；不得留下并行 commit 或提前 rollback。

### 16.4 Lifecycle 与 State 边界

公开 Lifecycle/Auth service facade 的 constructor、参数名、annotation、default 与 patch seam 保持 P0 manifest exact，包括现有 `storage: StoragePort` 与 `runtime_sidecar_client=`。新私有 implementation/helper 才依赖 P1 的窄 store/writer port；公开 facade 在内部只把需要的窄能力传给它。Conversation guard、Mailbox、MCP presence、Interrupt、Cancellation、Auth token 分别拥有独立 Protocol。Cancellation facade 保留 `runtime_sidecar_client` keyword，并在内部/assembly 包装为 `CancellationTokenWriter`；不得改名、删除或增加调用次数。公开 aggregate 参数可保留但 AST/method-usage gate 要证明各私有业务组件只使用其领域子集。

`src/state/service.py`、PostgreSQL queue/read-store、health/cutover 均纳入 FR-01 审计；只有发现有证据的职责混合或复制时才拆分，不能为了对称性做推测性重写。State schema policy 与 Storage bootstrap 的窄 assembly edge 按 9.1 import DAG 与 9.2 runtime flow inventory 管理。

### 16.5 PostgreSQL 停止条件

没有真实 integration/permissions/并发 DSN 证据时，P5 可以停在纯协议、mapper 和 SQLite/shared 检查点，但不得进入或宣称完成 rollout、CP7、remote-task 和最终 PG parity。

P5 高风险 profile 必须使用 11.6 的独立非生产数据库并达到目标模块零 skip。仅 source-order test 或 DSN 字符串断言不是 parity evidence。缺证据时对应 11.6 stable leaf gate 为 `blocked_external`，P5 local deliverables 与独立 P6/P7 可以继续，但 P5 plan 和项目不得 complete。

### 16.6 P5 验收

| 验收项 | 必须结果 |
|---|---|
| Metadata | 单一 metadata object；table/DDL digest exact；零 duplicate registration |
| MRO/owner | 四个公开 Storage/Collaboration facade identity 与 public descriptor owner exact；只出现预先批准的 private mixin/implementation/`super()` delta；既有基类相对顺序不变；PG override 不退化 |
| SQLite transaction | `_run`、BEGIN/commit/rollback/cancel trace exact |
| Collaboration repository | 30 个 event/mailbox/interrupt/slot/checkpoint 方法有 owner map；State+Collaboration 同 Session/单 callback/单 commit exact |
| PostgreSQL transaction | session/lock/flush/commit/rollback trace exact |
| Lifecycle ports | 公开 constructor/annotation/keyword exact；私有 implementation 不导入 concrete storage 且只用窄 port；调用/事务次数不变 |
| Source-shape replacement | 新 behavior/trace tests 先在旧实现绿，旧布局断言才删除 |
| PG parity | 对应真实 profile ran>0、failed=0、target skipped=0 |
| Facade | 旧路径仅 assembly/compat/白名单 runner，无第二套 method body |

## 17. P6：Frontend

必需检查点（文件名 provisional，依赖与 ownership 强制）：

1. `contracts/taskEvents.ts`、`artifacts.ts`、`capabilities.ts`；
2. `domain/interrupts.ts`、`conversationMessages.ts`、`uploads.ts`；
3. 搬出 Message、Artifact、Files、Sidebar 与受控 `LoginView` 纯展示组件；现有 `LoginPage` 保持 feature controller；
4. 拆 Artifact validation/data-query/MCP/file projector，保留 facade；
5. 拆 task-event validation、ledger 和 model，保留公开导出；
6. 拆 MCP 子 reducer，主 reducer 仍控制分派顺序；
7. `useConversationAttachments`；
8. `useConversationTaskRuntime`，只接管 accepted-task 后生命周期。

### 17.1 State 与副作用所有权

| Owner | 唯一持有的 state/ref/effect | 不得持有 |
|---|---|---|
| `App` | auth token/session、conversation、generation、messages、`pendingAssistantPatches` buffer 与 apply/decorate seam、composer、model、command menus、optimistic turn、首次普通/Slash/MCP pre-accept `api.submitMessage` 与 accepted handoff、跨 Hook command coordination、drawer/drag/input ref、composer eligibility | accepted-task SSE subscription、runtime retry timers、附件 API busy state 的镜像 |
| `useConversationAttachments` | draft attachments、saved/pending uploads、uploading、deleting IDs；upload/delete/reload/rollback/commit API effect | conversation generation、messages、task runtime、drawer/drag UI |
| `useConversationTaskRuntime` | accepted-task 之后的 task state 与同步 ref、current task/assistant、pending interrupt、Interrupt answer API、MCP approval/control API 与 busy state、SSE subscription、reconnect/interrupt/cancel timers、presentation mode、restored IDs | 首次普通/Slash/MCP submit、auth、composer、messages/message patch buffer、optimistic user turn、附件状态/rollback |
| Feature controllers | `LoginPage` 的 username/submitting/error 与 login API effect；`MCPSettingsPanel` 的面板局部 state/effect 与 settings API | App task runtime、跨会话 state、App controller Hook |
| Domain reducers | 纯 state transition、validation、ledger/fingerprint | fetch、timer、React ref、DOM |
| Presentational components | 局部展开/收起等纯 UI state | API、subscription、controller Hook |

`App` 与 Hook 只通过显式 command/port 协作：

- Attachment commands：`addDraft/removeDraft/reloadSaved/uploadSnapshot/rollbackSnapshot/resetSnapshot/commitSnapshot/deleteSaved`；
- Task runtime commands：`attachAcceptedTask`/`restoreTask`、`answerInterrupt`、`decideMCPApproval`、`continueMCPCall`、`cancelMCPCall`、`requestTaskCancel`、`disposeScope`；`attachAcceptedTask` 只接收 App 已取得的 task/assistant/generation/conversation binding，绝不再次提交首次消息；
- App ports：`patchAssistant`、`reloadHistory`、`refreshConversationHistory`、`showNotice`、`isScopeCurrent`。`patchAssistant` 必须封装“message 已存在则更新；尚未出现则写入 App-owned pending buffer”，App 在 optimistic/restored message 插入时唯一消费并删除该 patch；Hook 不读取或复制 buffer。

Interrupt answer API、keep-open/resume 与 task artifact completion 由 Task Runtime Hook 唯一执行；Hook 通过 `patchAssistant` 修改由 App 持有的 message，不得复制 message store。附件 Hook 唯一执行 upload/delete/rollback API；App 只决定何时发 command。

Interrupt answer 的跨 Hook 协议必须在 F8 implementation plan 前冻结为 closed discriminated outcome，至少区分 `resumed`、`keep_open`、`rejected` 与 `stale_scope`，并携带原 task/interrupt/generation binding；不得用 exception/nullable object 猜分支。App 是唯一协调者：先向 Attachment Hook 取得 upload snapshot，再创建既有 optimistic turn，然后调用 Runtime Hook 的 `answerInterrupt` 恰好一次；根据 outcome，App 只调用 outcome 指定的 Attachment `commitSnapshot`，或按现有顺序调用 `rollbackSnapshot → resetSnapshot`，`stale_scope` 保持现有不补偿语义。Runtime Hook 唯一应用 task/interrupt/subscription/busy state，App 不直接写这些 state。P0 trace 必须逐 outcome 锁定 optimistic message、API、attachment disposition、task state/notice、subscribe 的实际先后；若实现需要两阶段 `settleInterruptOutcome` 或 callback，opaque token 必须 one-shot，禁止 App/Hook 双 rollback、双 reset、双 subscribe 或在 API 失败后遗漏补偿。

### 17.2 App 最终所有权

`App` 继续拥有：

- auth token/session 与登录成功后的装配；`LoginPage` feature controller 继续拥有登录表单/API effect，并只通过 `onLogin` 交回结果；
- conversation、generation、messages 与 pending assistant patch buffer；
- composer、model 与 slash/MCP command menu；
- optimistic user/assistant message creation；
- drawer/drag/input refs 与 composer eligibility。

F3 先把现有 Login markup 原样迁入受控 `LoginView`（props 至少含 username、submitting、error、change/submit callbacks），`LoginPage` 继续持有当前 form state、trim/canSubmit、`api.login`、错误文案和 finally 时序。`LoginPage` 与 `MCPSettingsPanel` 都是明确的 feature-controller 例外，不得在本轮伪称纯展示或顺手把 API ownership 改到别处。

### 17.3 附件时序

1. 多附件串行上传；
2. 上传前标记 uploading；
3. 中途失败回滚已上传文件；
4. submit 失败 rollback 并恢复草稿；
5. history reload 在上传后、optimistic message 前；
6. uploading 状态由 finally 清理。

Partial rollback 失败时必须保持当前 draft/saved-list 与用户 notice 行为；不得为了“更干净”吞掉失败或清空更多状态。Conversation 删除中的多个 unfinished task 继续按当前顺序取消，部分失败按当前 error/continue 语义处理。

### 17.4 Task runtime 时序

- 新订阅前关闭旧订阅；
- 每个异步写入检查 generation/conversation/task/assistant；
- waiting event 先经过 reducer，再加载 interrupt；
- interrupt 展示完成后才关闭 SSE；
- clarification keep-open 不清错 pending interrupt；
- unknown/late-result 不提前结束订阅；
- terminal artifact 加载后才清 runtime。

还必须保持：

- waiting-input graph fallback delay 8000ms、最多 30 次，并保留 `waitingInputCheckDelayMs` 测试注入；`WAITING_INTERRUPT_RETRY_DELAY_MS=250`、最多 6 次；SSE reconnect 1000ms；cancel reconcile 250ms、最多 10 次；
- StrictMode effect setup/cleanup 不新增重复 fetch、subscription 或 timer；
- predecessor pending/event-sync-error 时保持当前 resync/reconnect 决策；
- Interrupt 选择优先级保持 payload ID → node ID → 首个 open interrupt；
- MCP unknown、availability failure、late result 的订阅关闭条件分别按现有 reducer/runtime 行为；
- artifact 加载失败后的 terminal cleanup 与 notice 保持；
- Message/Artifact 拆分继续遵守 Markdown、MathJax 与 MCP strict typed projection 安全约束。

### 17.5 DOM 与可访问性验收

- 纯组件搬迁检查点不得修改 `frontend/src/styles.css`；
- 不新增/删除 wrapper，保持 root element、className、role、accessible name 和 Testing Library query 结果；
- 锁定 `aria-live`、Modal/Drawer focus、Popover portal、展开控件关联；
- 锁定 composer autofocus、conversation auto-scroll、welcome mount/key 和随机文案生成时机；
- 当前键盘行为按原样保存，不在结构检查点修复 MCP 菜单方向键；
- 使用现有 Testing Library/Vitest，不新增 a11y dependency。

### 17.6 P6 检查点矩阵

| CP | 迁移 | 最低 focused evidence |
|---|---|---|
| F1 | Wire Contracts | `api/client.test.ts`、`api/taskEvents.test.ts`、`contracts/taskEvents.test.ts`、`contracts/artifacts.test.ts`、`contracts/capabilities.test.ts`、typecheck |
| F2 | Interrupt/message/upload pure domain | `domain/interrupts.test.ts`、`domain/conversationMessages.test.ts`、`domain/uploads.test.ts`、`App.test.tsx` |
| F3 | Presentational views + preserved feature controllers | `components/MessageView.test.tsx`、`components/ArtifactView.test.tsx`、`components/FilesView.test.tsx`、`components/SidebarView.test.tsx`、`components/LoginView.test.tsx`、`components/MCPSettingsPanel.test.tsx`、`styles.test.ts`、`App.test.tsx` DOM/a11y |
| F4 | Artifact projectors | `domain/artifacts.test.ts`、`App.test.tsx` 的 artifact/history/MCP-result restore cases |
| F5 | Task-event validation/ledger/model | `api/taskEvents.test.ts`、`domain/taskEvents.test.ts` |
| F6 | MCP subreducer/presentation | `domain/taskEvents.test.ts`、`components/MCPRuntimeStatus.test.tsx`、`components/MCPApprovalDialog.test.tsx`、`App.test.tsx` |
| F7 | Attachment Hook | `hooks/useConversationAttachments.test.tsx` + `App.test.tsx` 的 upload/rollback/delete/interrupt-upload/StrictMode cases |
| F8 | Task Runtime Hook | `hooks/useConversationTaskRuntime.test.tsx` + `App.test.tsx` 的首次 normal/Slash/MCP accepted handoff（submit=1、Hook submit=0）、interrupt-upload submit failure 的 rollback/reset/notice 顺序、resumed/keep-open/stale outcomes、approval、continue/cancel MCP call success/failure/busy trace、restore/replay/SSE reconnect/cancel/late-result/artifact completion/StrictMode cases |

每个 CP 还必须运行全部 Frontend tests，发现数不得低于 P0 manifest，并运行 typecheck/build。F3 必须证明 `styles.css` 内容零 diff；F7/F8 必须比较 API/subscription/timer call trace。

### 17.7 明确延期的前端问题

- MCP 命令菜单方向键；
- Artifact ID 语义推断；
- localStorage 异常差异；
- API 文本错误体 fallback；
- upload refresh 失败清空状态；
- 任何文案、视觉、DOM、ARIA、焦点或滚动调整。

## 18. P7：Native 与 Scripts

### 18.1 Public type 与 contract owner

- 当前物理定义在 crate root 的 public struct/enum 必须继续定义在 root，禁止移入 `api.rs` 后 re-export；
- `RuntimeSidecarSqliteAdapter` 当前定义在 `sqlite_adapter`，必须保持该 canonical module path；可以改为 `sqlite_adapter/mod.rs` + children，但不能移到 root 或改模块名；
- 禁止新建跨 crate common contract 合并外形相似的公开 error/policy/serde type；
- P0 的 `type_name` snapshot、external-crate root-import fixture、serde/contract byte diff 是每个 P7 检查点的前置与后置门禁。

| Checked-in aggregate artifact | Serialization producer |
|---|---|
| Core model/schema contract | `maf_core_types` |
| Lifecycle interrupt/cancel contract | `maf_lifecycle` |
| Runtime schema/features/resource-limit contract | `maf_runtime_store` |
| Skill contract/policy/sandbox runtime | `maf_skill_runtime` |
| Safety aggregate snapshot | `maf_audit_sanitizer` |
| MCP contract/adapter/sanitizer/registries | `maf_mcp_runtime` |

`maf_runtime_sidecar` 继续拥有 Runtime transport/kernel/gRPC/serve 实现，相关 PyO3 crate 只拥有稳定 bridge；它们不是上表 aggregate artifact 的当然字段 authority。

Serialization producer 不自动成为每个 JSON field 的 runtime enforcement owner。P0 必须生成 `rust-contract-field-ownership.json`，逐 JSON pointer 记录 artifact/serializer symbol、实际 enforcement crate/symbol、literal mirror/parity test 和允许依赖边。例如 Safety 的 `db_row_limit/db_column_limit/db_result_bytes` 由 `maf_data_access` 常量/校验执行，artifact/auth/audit字段分别由其既有 kernel 执行；`maf_audit_sanitizer` 只序列化 aggregate snapshot。Runtime contract 的 `queue_size` 镜像 `maf_task_dispatcher`，event payload/replay limits 镜像 `maf_event_log`，而 artifact producer 仍是 `maf_runtime_store`。

共享常量只在既有依赖方向允许时由 consumer 单向引用 enforcement owner。若 aggregate serializer 当前故意不依赖 enforcement crate，literal mirror 是 byte-compat snapshot，不是本轮“重复代码”目标；使用外部 compile/parity fixture证明值相同，禁止为了去重给 `maf_audit_sanitizer`/`maf_runtime_store` 新增或倒置生产 Cargo 依赖。每个 field 的运行时校验继续留在原 owner，6 份 artifact bytes 与 Cargo dependency manifest 均保持 exact。

### 18.2 Runtime Sidecar

public type declaration 保持 crate root，逐步抽：

- `validation.rs`
- `codec.rs`
- `kernel.rs`
- `service.rs`
- `grpc.rs`
- `config.rs`
- `serve.rs`

`sqlite_adapter` 模块名保持，在内部按 schema、agent state、tasks、nodes、artifacts、events、leases、control、rows 拆分。此阶段不引入生产 backend trait。

纯 validation/codec/impl 搬迁为中风险；SQLite domain split、gRPC/service 和 backend parity 为高风险，必须逐域运行 runtime-sidecar crate tests、Clippy 和 P0 parity，不得一次搬完整文件。

### 18.3 Skill Runtime

拆为 contract、policy、sandbox/process、sandbox/stdio、service、grpc、codec、serve。Sandbox limits 使用单一常量源，但值不变。stdio partial-output、kill/wait、env clear 和 path guard 顺序不变。

Sandbox process/stdio、gRPC 与 PyO3 packaging 是高风险检查点；macOS 本地通过不等于 release gate 通过。

### 18.4 MCP Runtime

拆为 contract、errors、official SDK、shadow、JSON-RPC、sanitizer、registries。不得顺手接通测试/预备 registry，也不修 client-version、fingerprint 或 sanitizer 行为。

### 18.5 平台与 packaging gate

每个受影响 Rust 检查点必须证明：

- `Cargo.toml`、`Cargo.lock`、Proto、`build.rs` 无非预期语义 diff；
- 11.5 明列的 6 份 export JSON 与各自 producer byte-identical，比较原始 bytes/SHA 而非 parse 后 pretty print；
- `pb::{common,runtime,skill}::v1` path 不变；
- root `skill_runtime_contract_json`、`skill_policy_validate_json` 不变；
- PyO3 module/function、abi3-py313 与 JSON envelope 不变；
- `cfg` 属性原样搬迁。

支持矩阵为 macOS 开发门禁 + Ubuntu 22.04 release 门禁。Ubuntu full rust-quality 和受影响 manylinux build/import smoke 是 required remote gate；无法运行时检查点状态为 `pending_platform`，不能标 complete。非支持平台的 `cfg(not(unix))` 可以 N/A，但必须记录且不能改其源码语义。

P7 Ubuntu stable leaf gates：

| Gate ID | Existing workflow job | 适用范围 / required result |
|---|---|---|
| `P7.ubuntu.rust-quality` | `.github/workflows/rust-quality.yml#rust-quality` | 任一 Rust business/contract/Cargo change；完整 fmt/clippy/test/nextest/audit/deny/coverage、fuzz compile、release binary/provenance步骤 success |
| `P7.ubuntu.rust-fuzz-smoke` | `#rust-fuzz-smoke` | 修改被现有 fuzz target 覆盖的 validation/parser/sanitizer/policy 或 fuzz harness；bounded targets 与 locked metadata 全 success |
| `P7.ubuntu.pyo3-wheel-smoke` | `#skill-runtime-pyo3-wheel-smoke` | 修改任一 Skill、Core/Lifecycle、Safety PyO3 bridge/contract/packaging；三 wheel manylinux_2_35 build、install、import/contract smoke 全 success |

P0/P7 为每个 leaf 保存 job/run ID、workflow commit、conclusion、step set、artifact/provenance digest 与 `relevant_source_digest`。Digest path set至少包含该 job trigger 的业务/contract/Cargo/workflow/requirements/quality scripts；任一 relevant path 后续变化使旧 evidence 失效并必须在最新 `tested_commit` 重跑。P7 implementation plan 只能从上表选择 required leaf，不能生成临时 `P7.ubuntu.<profile>` 名；不适用 leaf 必须以 path/fuzz/PyO3 owner map证明 N/A。

### 18.6 Scripts

P7 对 engine create/dispose 只做 audit-first：P0 finding 必须逐 symbol 记录 create、role/argument validation、ownership transfer/return、success/error/cancel dispose、patch seam 和 call trace。只有某一对路径在所有这些点完全等价时，才能原样抽成既有 helper；否则各自标 `reviewed_no_change`。不得新增、提前或延后异常路径 dispose。已识别的 pre-validation engine leak 属行为/资源 bug，进入 P8 延期 finding，不能借结构迁移修复。

`scripts/prd_evidence.py` 已有 `collect_gate_results`、`finish_release_gate_result`、`run_evidence_cli`，不得再建第二套 ordered gate helper。P0 先列 remaining duplicated symbol pairs 及 gate order、guarded/candidate/pending/error code、mapping validation、stdout/stderr/exit trace；完全等价者复用现有 helper，语义不同（包括 PRD07 startup loop）则保留原实现或加只适配其差异的薄 adapter。无 fully equivalent duplicate 时该 finding 标 `reviewed_no_change`。不得统一所有 CLI、删除 deprecated 参数或改变 stdout/stderr、exit code、env name、role 与错误文本。

`scripts/postgres/*.sql` 权限模板、`check_docker_cmd_policy.sh`、shebang 与 executable mode 只审计不重构，除非存在已批准的具体 finding。每个受影响 CLI 必须从 repo root 运行其 help/success/failure tests，并检查 workflow/runbook/path 引用；新增 helper 若改变 CI path trigger，必须同步 trigger。

### 18.7 P7 验收

| 子检查点 | 风险 | Required gate |
|---|---|---|
| Runtime validation/codec/impl 搬迁 | 中 | crate test + fmt + clippy + type-name/contract diff + `P7.ubuntu.rust-quality` |
| SQLite adapter domain split | 高 | parity matrix + reopen + runtime-sidecar full tests + `P7.ubuntu.rust-quality` |
| Skill contract/policy | 中 | contract bytes + skill tests + PyO3 check + `P7.ubuntu.rust-quality`；涉及bridge再加 `pyo3-wheel-smoke` |
| Sandbox process/stdio/gRPC | 高 | process/fault tests + `rust-quality` + `pyo3-wheel-smoke`，若fuzz-covered再加 `rust-fuzz-smoke` |
| MCP Runtime split | 中到高 | MCP crate tests + contract/SDK conformance + `P7.ubuntu.rust-quality`，若fuzz-covered再加 `rust-fuzz-smoke` |
| PRD evidence reuse audit | 低 | existing-helper inventory + symbol-pair equivalence + ordered gate/CLI output trace；无等价 pair 时 `reviewed_no_change` |
| Operator/migration/storage scripts | 中到高 | role/env/engine/SQL/exit trace + isolated environment |

Python compile gate必须包含 `src scripts tests`；P7 还检查 `git diff --summary` 以捕获 file mode 变化。

## 19. P8：收尾清理与延期审计

P8 只删除或收敛满足以下条件的内容：

- 私有；
- 仓库内零引用；
- 不属于文档化公开 API；
- 不参与 import side effect、registration、pickle/spawn；
- 有定向测试或静态证据；
- 删除不会改变异常、日志、事件、计时或副作用。

已审计候选包括未使用 import/local、断链旧 parser、不可达 fallback literal、完全相同 pure helper、无效半成品对象等。每项必须使用稳定 finding ID（如 `SLOP-API-001`），记录 path/symbol、分类、owner、当前状态、证据命令、风险、处理/延期原因和退出条件。具体清单必须在 P8 基于当时 HEAD 重新验证，不能直接引用设计期行号执行。

`src.core.models` 中公开类的物理迁移也只允许在 P8 单独评估。只有 module identity、pickle、repr、type hints、Rust contract 和旧 import 均可证明不变时才迁移；否则保留公开类定义原位，只拆其私有 helper 与 validation。

以下问题不属于功能不变重构，继续形成单独延期报告：

- 吞异常或伪装为空结果的 masking fallback；
- fixed-zero observability；
- Runtime/MCP client-version 声明与校验不一致；
- artifact field/idempotency 验证缺口；
- 手写 HMAC/constant-time compare 替换；
- Artifact ID 跨层语义；
- Frontend 键盘与错误可见性 bug；
- scripts pre-validation engine leak；
- 安全 authority 或 crate 边界重构。

这些项只有在用户明确批准行为变化并先补合同后，才能进入独立任务。

P8 结束时，FR-01 inventory 中每个业务源码路径和 finding 必须闭合；结构性 mandatory finding 不得用 `bounded_deferred` 跳过。明确非范围的行为问题可以登记延期而不阻断结构清理，但最终报告必须逐项列出。

## 20. Fallback 处理规则

### 20.1 Grounded fallback

保留满足以下条件的 fallback：

- 保护明确的兼容或 fail-safe 边界；
- 有原因、状态或证据；
- 主路径和 fallback 均有覆盖；
- 不隐藏 authority 或安全失败。

符号级已确认清单：

| Finding | Primary/trigger/fallback | 分类与精确证据 | Owner / exit criterion |
|---|---|---|---|
| `FALLBACK-API-001` MCP artifact projection | `src.api.artifact_responses.artifact_response` 在无 projection metadata 时 safe-hide；有 ref 但 store missing 时 `projection_missing`；load/hash/envelope/model invalid 时 `projection_invalid`；均不读取或下载 raw | Grounded 的 safe-hide 证据：`tests/api/test_conversation_messages_artifacts.py::ConversationMessagesArtifactRestoreAPITest::test_mcp_result_file_is_safe_hidden_and_cannot_be_downloaded`；P0 补 store-missing、tampered hash/envelope/model-invalid 各分支与 download=404 characterization | P4 保持；只有另行批准 artifact 行为/安全合同并有 raw non-disclosure replacement tests 才可退出 |
| `FALLBACK-MCP-001` adapter explicit downgrade | `src.integrations.mcp.adapter_2026.safe_auto_downgrade_version` 仅由显式 unsupported evidence 触发到批准版本 | Grounded；`tests/integrations/mcp/test_2026_07_28_adapter.py::MCP20260728AdapterTests::test_safe_auto_downgrade_requires_explicit_unsupported_evidence` | P3 保持；只有版本矩阵与 downgrade policy 独立变更获批才退出 |
| `FALLBACK-SCRIPT-001` legacy audit export | `scripts.migrate_legacy_mcp_config.run` durable apply 成功后 `_write_audit` 失败，返回 `failed_non_authoritative` | Grounded；`tests/scripts/test_migrate_legacy_mcp_config.py::LegacyMCPMigrationCommandTests::test_builtin_apply_persists_config_and_writes_secret_safe_audit` | P7 保持；只有 audit authority/CLI exit contract 独立变更获批才退出 |
| `FALLBACK-CORE-001` Core Rust contract `off` | `src.core.rust_contract._load_core_contract_for_mode` 显式 `off` 读取 checked-in contract | `off` 是主模式，不是 fallback；现有 artifact test 为间接证据，P0 新增显式 `MAF_RUST_CORE_MODE=off` characterization并断言 PyO3 import/call=0 | P7 保持；只有部署 mode 产品决策变化才退出 |
| `FALLBACK-CORE-002` Core Rust shadow | 同一 symbol 在 PyO3 module missing、entry/load/parse `RustCoreContractError` 或 compatibility mismatch 时回 checked-in contract且不暴露 fault evidence | Masking/observability gap；现有 `CorePyo3FacadeTest` 覆盖 module absent/mismatch；P0 补 missing entry、invalid JSON/non-object/load error各分支 | P7/P8 保持并延期；只有单独行为设计补 diagnostic/rollout contract 后退出 |
| `FALLBACK-LIFECYCLE-001` Lifecycle Rust shadow | `src.lifecycle.rust_contract._load_lifecycle_contract_for_mode` 在 PyO3 module missing/load error/mismatch 时回 checked-in contract | Masking/observability gap；`tests/lifecycle/test_lifecycle_pyo3_facade.py::LifecyclePyo3FacadeTest::test_shadow_keeps_checked_in_artifact_on_lifecycle_contract_mismatch`；P0 补 module-missing characterization | P5/P7/P8 保持并延期；只有单独行为设计补 fault evidence 后退出 |

其他 component 的 shadow/off/enforce 必须逐符号审查，不得从 mode 名称推导全部 fallback grounded。Enforce fail-closed 行为原样保留。

### 20.2 Masking fallback

结构迁移阶段保持原行为并记录，不顺手修复。只有“不可能分支”可在证明不可达后删除；会改变错误传播、用户可见状态、重试或安全策略的 fallback 必须另立行为变更设计。

首批风险登记还必须包括 Agent Skills schema-load fallback、MCP presence `return_exceptions=True` 后丢弃 cancel failure、hidden `codex_skills` alias、fixed-zero observability、client-version mismatch 和外部 PG/Linux/MCP evidence gap。

## 21. 验收与追踪

### 21.1 FR 到证据

| FR | Owner | Required acceptance/evidence |
|---|---|---|
| FR-01 | P0/P8 | raw tracked set 全覆盖；每路径唯一 primary owner、authorized touches 可审计；new/renamed/deleted tombstone 双向闭合；P8 `planned_change=0` 且最终 path status 只含 `changed|reviewed_no_change` |
| FR-02 | P0/全部 | `public-contract-manifest.json` 前后 exact；API patch seam、StoragePort identity、Rust type path、Frontend facade smoke |
| FR-03 | P0/各计划 | replacement characterization 在旧实现先绿；迁移后同 trace/golden；source-shape test 仅在 replacement 通过后移除 |
| FR-04 | 全部 | side-effect trace 中外部/DB/worker/message call count 与基线 exact；无新旧双实现并行 |
| FR-05 | P0/P8 | AST edge inventory；forbidden edge 为零或 exception 有 owner/退出条件 |
| FR-06 | P1 | 12.3 全部通过；Core/Storage tests；259 方法无缺失/重复 |
| FR-07 | P2 | 13.9 matrix 与 fault 全绿；统一 Agent Loop 权威 FR 映射无漂移 |
| FR-08 | P3 | 14.9 matrix、17 fault boundary、Gateway/Parser/Skills/privacy/history tests、Ubuntu Parser 与 `P3.real-mcp` 全绿 |
| FR-09 | P4 | 15.3 全部通过；API/startup/shutdown/interrupt focused 与全量门禁 |
| FR-10 | P5 | 16.6 全部通过；SQLite/PG owner、metadata、transaction、parity artifact；目标 profile 零 skip |
| FR-11 | P6 | 17.6 八检查点、DOM/a11y、API/subscription/timer trace、Frontend 全量/typecheck/build |
| FR-12 | P7 | 18.7 matrix、6 份 contract bytes、public type/function/future identity compile fixture、CLI trace、Ubuntu/manylinux gate |
| FR-13 | P0/P8 | fallback/finding registry 每项具 symbol、source、test、owner、preserve reason、exit criterion |
| FR-14 | 全部 | 每命令 gate JSON 记录 ran/pass/fail/skip；目标 profile ran>0、skip=0；required platform 有 artifact |
| FR-15 | 全部 | 状态机满足 10.1；mandatory 未完成时总体不得 complete |
| FR-16 | 全部 | clean start/tested commit、reviewed diff、独立 implementation/test commit + 可选 evidence-only commit、rollback record、AGENTS/CHANGELOG decision |
| FR-17 | P8 | deferred behavior finding 与结构 diff 分离；无行为修复混入结构 commit |

### 21.2 计划退出条件

| 计划 | Mandatory exit | 不满足时状态 |
|---|---|---|
| P0 | 本地 manifests/trace/characterization 可重现；外部 profile 测试代码和命令已定义 | 本地缺口 `failed`；PG `blocked_external`；Ubuntu `pending_platform` |
| P1 | Aggregate identity/259-method ownership/窄 port consumer 全绿 | `failed` |
| P2 | 13.9 与统一 Agent Loop 权威回归全绿 | `failed` |
| P3 | 14.9、安全/NFR/no-replay 本地 gate 全绿；Result Parser Ubuntu profile 与 `P3.real-mcp` 实际通过后才可 plan complete | 本地缺口 `failed`；Linux 为 `pending_platform`；真实 MCP 为 `blocked_external` |
| P4 | Runtime facade、startup/shutdown、interrupt owner 和 API gates 全绿；当前 relevant-source digest 有新鲜 `P3.real-mcp` evidence | 本地缺口 `failed`；真实 MCP 缺失为 `blocked_external` |
| P5 | Shared/SQLite 可分步 complete；最终 P5 还要求所有受影响真实 PG profile 零 skip | `blocked_external` 不能冒充 complete |
| P6 | F1～F8、DOM/a11y、全量/typecheck/build 全绿 | `failed` |
| P7 | 本地门禁与 required Ubuntu/manylinux gate 全绿 | 未远端验证为 `pending_platform` |
| P8 | Inventory/finding/forbidden-edge/facade gates 闭合；全仓最终证明通过 | 任一 mandatory 缺口则总体非 complete |

任何计划的 module/file 提议可以在 implementation plan 中因最新 HEAD 调整，但 FR、NFR、owner、trace 和退出条件只能通过新的用户批准设计修改。

## 22. 验证矩阵

### 22.1 每个检查点

```text
characterization coverage
-> focused tests
-> module suite
-> cross-layer suite
-> lint/typecheck/build/contract diff
-> git diff --check
-> final diff review
-> implementation/test commit
-> gates on that clean tested commit
-> optional evidence-only commit
```

每一步的 command、结果、test count、skip、platform、`start_commit`、`tested_commit`、disposable checkout identity/cleanup 与 artifact digest 必须写入 `docs/checkpoint/progressive-architecture-cleanup/gate-results/`。Gate 必须按 11.1 在 `tested_commit` 的 clean disposable checkout 上运行，原工作树可保留已登记的无关用户 diff；随后 evidence-only commit 不得修改业务或测试源码。`contract diff` 必须列出比较文件及 SHA；`final diff review` 必须记录 reviewer、scope 与结论，不能只写一句“已检查”。

### 22.2 Backend canonical

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

### 22.3 Frontend

```bash
cd frontend
npm test -- --run
npm run typecheck
npm run build
```

Frontend 全量 test files/tests 发现数不得低于 P0 manifest；新增 characterization 会提高基线。测试减少必须有逐项获批的删除映射。

### 22.4 Rust

```bash
conda run -n multi_agent python scripts/run_rust_quality_gates.py --run --only cargo_fmt
conda run -n multi_agent python scripts/run_rust_quality_gates.py --run --only cargo_clippy
conda run -n multi_agent python scripts/run_rust_quality_gates.py --run --only cargo_test
```

受影响检查点按可用性增加 deny、audit、coverage、fuzz compile 和平台 wheel smoke。缺少 required tool 或平台不能标为通过。

### 22.5 Skip、lint 与差异判定

- `Ran 0 tests`、命令不存在、non-zero exit 都是失败；
- 高风险 profile 和被修改模块在其适用平台要求目标 skip=0；
- 本地平台声明 skip 只有两种合法处理：未改对应代码且该平台确实 N/A，可记录理由；或由具名 required remote gate 覆盖，此时本地 suite 只能记为 green-with-declared-skip，相关 deliverable 保持 `pending_platform`，直到远端适用平台 `skipped=0`。不得把本地 skip 计为 required pass；
- `tests/e2e/test_controlled_real_mcp_gate.py` 在普通 Backend/E2E discover 中必须注册 5 个 test；`CLEANUP_REAL_MCP_RUN!=1` 时只允许 closed skip reason `P3.real-mcp:not_requested`，该 skip 映射 `P3.real-mcp=blocked_external` 而不是 pass。专用 profile 设为 1 后，缺 env/control capability 必须 fail 而非 skip，并要求 5/5 ran、0 skip；
- Python correctness gate 对所有 touched Python 文件运行 `ruff check <files> --select E4,E7,E9,F`；P8 最终运行 `ruff check src scripts --select E4,E7,E9,F` 并要求 PASS；
- `C901,PLR0911,PLR0912,PLR0913,PLR0915` 是诊断快照，不要求历史非零结果立即清零，但受影响 scope 不得新增未解释 hotspot；
- `git diff --check` 必须 PASS；`git diff --summary` 用于发现 file mode/rename；
- contract JSON 比较 raw bytes 与 SHA-256，不能只比较 parse 后对象；
- wall-clock 波动不单独判失败；确定性 timeout constant、调用次数、顺序和资源 cap 必须 exact。

### 22.6 受控真实 MCP gate

Gate ID 为 `P3.real-mcp`，前置必须是 `P3.local-integrations=complete` 与 `P4.local-mcp-wiring=complete`。P3 负责测试 harness/Gateway/Coordinator/Parser 证据，P4 负责 runtime wiring 证据。只要本轮修改 Gateway、Coordinator、Result Parser 或 MCP runtime wiring，该 gate 就不允许 N/A；缺 Server、网络或授权时为 `blocked_external`。P0 必须新增 `tests/e2e/test_controlled_real_mcp_gate.py`，固定执行命令为：

```bash
CLEANUP_REAL_MCP_RUN=1 \
conda run -n multi_agent python -m unittest tests.e2e.test_controlled_real_mcp_gate
```

Required env 名固定为：`CLEANUP_REAL_MCP_ENDPOINT`、`CLEANUP_REAL_MCP_PROTOCOL_VERSION`、`CLEANUP_REAL_MCP_CONTROL_ENDPOINT`、`CLEANUP_REAL_MCP_CONTROL_HOST`、`CLEANUP_REAL_MCP_CONTROL_SPKI_SHA256`、`CLEANUP_REAL_MCP_CONTROL_TOKEN`、`CLEANUP_REAL_MCP_ORDINARY_TOOL`、`CLEANUP_REAL_MCP_APPROVAL_TOOL`、`CLEANUP_REAL_MCP_ARTIFACT_TOOL`、`CLEANUP_REAL_MCP_RESPONSE_LOSS_TOOL`。artifact 只记录 env 是否存在和非敏感 digest，不记录值、endpoint、token 或 credential。

Control protocol 固定为 `maf.cleanup.real_mcp.control.v1`。在发送 `CONTROL_TOKEN` 前，harness 必须先验证 control URL 为无 userinfo/fragment 的 HTTPS、host 与 `CONTROL_HOST` exact、系统 TLS chain/hostname 有效、peer SPKI SHA-256 与 pin exact，且全程禁用 redirect；随后无认证读取同 origin `GET /.well-known/breeding-agent-mcp-cleanup-gate/v1/manifest`。任何 endpoint/TLS/pin/manifest 拒绝发生时 control/MCP credential send 次数必须为 0。Manifest canonical JSON 必须包含：

- `schema=maf.cleanup.real_mcp.control_manifest.v1`、`environment=testing`、`destructive=false`、profile revision/expiry；
- MCP endpoint digest、supported protocol versions，以及 control paths 固定相对值 `POST /v1/cleanup-runs`、`POST /v1/cleanup-runs/{run_ref}/faults/response-loss`、`GET /v1/cleanup-runs/{run_ref}/counters`、`POST /v1/cleanup-runs/{run_ref}/cleanup`；
- capabilities exact set：`issue_short_lived_credential`、`run_scoped_counter`、`response_loss_after_commit`、`artifact_cleanup`、`counter_reset`、`credential_revoke`；
- 四个 Tool 的 name、canonical safe `input_template`/digest、允许替换的 `run_ref|scenario_ref|fault_token` placeholders、canonical expected result template/digest、output schema/digest、typed projection/artifact schema，以及 closed `approval_policy`（仅 approval Tool 为 `platform_per_call`）和 `response_behavior`（仅 loss Tool 为 `drop_after_commit`）。

Manifest Tool names 必须与 env exact，协议/MCP endpoint 必须通过生产 `ValidatedEndpoint` policy 并与 env exact。Harness 禁止从任意 Tool schema猜参数，只能对 manifest 的安全 canonical template 做声明过的 placeholder 替换，并在调用前重算 input/output-schema digest。

Control JSON/HTTP envelope 固定如下：所有 response 带 `schema` 与 `control_revision`；error 使用 `schema=maf.cleanup.real_mcp.control_error.v1`、closed `code`、`retriable` boolean、safe `request_ref`，不得回显 token/credential/input。Manifest=200；create run 首次=201、相同 `client_run_id`+`Idempotency-Key` exact replay=200且返回同一 run binding；fault-token 首次=201/exact replay=200；counter/cleanup=200。401/403 不重试，409 表示 identity/idempotency conflict，410 表示 expired run，429/503 只允许 GET counter 有界重试且不能重试 Tool/control mutation；其他 status/error/schema 一律 fail。Cleanup 因协议定义幂等，可在 transport response-loss 后以同 key最多重试一次，但仍按最终 closed action验证。

通过同 origin Bearer-authenticated `POST /v1/cleanup-runs` 创建 run，request schema 为 `maf.cleanup.real_mcp.create_run.v1`，只含随机 `client_run_id`、owner ref digest、protocol 与 requested TTL；response `maf.cleanup.real_mcp.run.v1` 必须返回 opaque `run_ref`、expires-at、短期 Bearer MCP credential、counter nonce 和与 manifest 匹配的 endpoint，初始四类 counter 全为 0。Credential 只注入 disposable owner/server 的现有 encrypted credential store，header 固定 `Authorization: Bearer <secret>`；不得放入 Tool args、文件、evidence或进程 argv/env二次传播。

每个 Tool 场景生成唯一 `attempt_ref` 与 call idempotency key，canonical template只替换 `run_ref`、`attempt_ref` 及 loss 场景的 `fault_token`。Response-loss 前用 request schema `maf.cleanup.real_mcp.arm_fault.v1` 取得绑定 `run_ref + attempt_ref + call/idempotency digest` 的一次性 token。Counters response `maf.cleanup.real_mcp.counters.v1` 每项必须返回 attempt ref digest、Tool name、call/idempotency digest、execution count、committed flag、result digest 和 counter nonce，禁止只给全局累计值。Cleanup request/response 分别为 `maf.cleanup.real_mcp.cleanup.v1`/`cleanup_result.v1`，必须幂等地删除本 run 外部 artifact、把 counters清零并撤销 credential，返回 `artifact_cleanup|counter_reset|credential_revoke` 三项 closed result；任何 path/schema/binding/TTL/nonce/digest 不匹配都 fail closed。

Suite 必须恰好运行 5 个场景，`ran=5`、`failed=0`、`skipped=0`：

1. owner-bound disposable Task/Server scope 完成真实 discovery；
2. ordinary allowlisted Tool 使用 manifest canonical input，返回的 result/output-schema/typed projection digest exact，Server run+scenario counter delta 恰为 1；
3. harness 在本地 authority 中把 manifest 标记的 Tool 配置为 per-call approval，approval→resume 使用原 Call/Run binding，批准前 counter 0、批准后 result digest exact 且 counter 恰为 1；
4. Artifact Tool 的 Result Parser checkpoint/outcome、typed projection schema/digest与公共 Artifact contract exact，evidence 只保存 schema/digest 而非 raw；
5. 专用 response-loss Tool 使用 one-shot fault token，在 Server 以同一 call/idempotency correlation 记账后断开；重建本地 Coordinator/runtime 并从同一临时 durable store 恢复，restart 前该 scenario counter 为 1、restart 后第二次 Tool network delta 为 0。

四个 Tool 必须由 control manifest 明确 allowlist，且为 deterministic、idempotent、非破坏、无下游外部副作用的 gate-only Tool；response-loss 只能使用专门实现“commit counter then drop response”的测试 Tool，禁止拿真实写业务 Tool 模拟。每场景最长 60 秒，suite 最长 300 秒，超时为 fail。

Harness 使用全新随机 owner/task/call scope 与模块独立临时 storage，不访问 `prod`、不复活旧失败 Task、不复用上次 credential/counter。finally 必须关闭 scope/client/runtime，删除本地 Task/Server 配置和临时 artifact，调用 control endpoint 清除 counter/外部 artifact 并撤销短期 credential；任何 cleanup/revoke 失败使 gate 失败且该 run 不可复用。失败 evidence 只保存 run-ref digest、未闭合 action code 与 operator cleanup ticket/action，禁止保存 control token/credential；人工清理成功也只能闭合 cleanup，不得把该失败 run 改记为业务场景 PASS，必须新建 run 重跑。

Evidence `gate-results/controlled-real-mcp.json` 必须按 11.1 canonicalization 记录 `tested_commit`、`relevant_source_digest`、protocol/version/adapter、Server ref digest、5 个场景 ID、脱敏 Task/Call ref digest、每阶段 counter before/after、restart network delta、projection/artifact schema+digest、timeout、redaction scan 与 cleanup/revoke result。`relevant_source_digest` 对 inventory 中标记 `real_mcp_evidence_scope` 的 P3 Gateway/Coordinator/Parser/Agent Skills adapter、P4 MCP assembly/runtime wiring、相关 capability/orchestration adapter 及 gate harness，以 `path NUL git-blob-sha LF` 排序计算；P0 保存 exact path set。任一路径后续 created/modified/renamed/deleted 都立即使旧 artifact 失效，必须在包含最后相关改动的最新 clean `tested_commit` 重跑；无关 docs/其他模块 commit 只有 digest 不变时才不失效。P4 exit 与项目 completion 都必须现场重算该 digest。日志、event、metric、diagnostic、exception 和公共 artifact 对 sentinel Tool args、credential 与 raw result 的 forbidden scan 必须为零。fake/local unit tests 仍是前置门禁，但不能代替此 profile。

## 23. Edge cases 与 failure modes

| ID | 场景 | 必须行为 | Owner/证据 |
|---|---|---|---|
| EDGE-01 | 公开类型移入子模块 | 不得仅靠 re-export 改变 module/type identity；不满足则保留定义原位 | P0/P1/P7 manifests |
| EDGE-02 | 新模块产生 import cycle/eager side effect | import smoke 与 AST DAG 失败，当前检查点回滚 | P1～P7 |
| EDGE-03 | 旧 test 锁 class/file layout | replacement behavior/trace test 先在旧实现绿，再移除纯布局断言 | P0/P5 |
| EDGE-04 | 固定 clock/UUID 后 event/idempotency 漂移 | exact failure，不允许通过删除字段归一化 | P0/各计划 |
| EDGE-05 | Slot 已写、Interrupt/Message/Outcome 未写 | 保持现有部分写入与恢复分类，不新增重复 event/message/capability | P2 fault matrix |
| EDGE-06 | Tool may-have-dispatched 后 crash/retry | unknown/aborted/no-replay 按原合同，第二次 Tool 调用 0 | P3 17-boundary matrix |
| EDGE-07 | Result Parser checkpoint 后 projection 失败 | 保留 authoritative checkpoint/outcome，仅 projection_failed | P3 parser tests |
| EDGE-08 | Gateway open/call 与 close/invalidate 并发 | 单 state owner；全局 lock 与 per-scope event-loop domain 不变；late open 被 barrier 阻止；锁外 cleanup 顺序不变 | P3 Gateway tests/field trace |
| EDGE-09 | SQLite `_run` coroutine 被取消 | 后台 transaction 完成后再传播取消，不并行 commit | P0/P5 trace |
| EDGE-10 | PostgreSQL 专用 override 在 mixin 后丢失 | owner/MRO gate 失败并回滚，不允许 inherited generic path | P5 manifest |
| EDGE-11 | ORM compatibility import 重复注册 | metadata identity/table digest gate 失败 | P5 schema tests |
| EDGE-12 | Required PG/Ubuntu/manylinux/controlled MCP control service 不可用 | 标具名 `blocked_external`/`pending_platform`；普通 discover 的注册 skip 不算通过；独立计划可继续，总体不 complete | 状态机/gate artifact |
| EDGE-13 | React StrictMode 重挂 effect | fetch/subscription/timer 次数与 cleanup exact，不得双订阅 | P6 F7/F8 tests |
| EDGE-14 | Partial upload rollback 或 conversation cancel 部分失败 | 保持旧 draft/saved-list、notice、顺序和错误语义 | P6 App/Hook tests |
| EDGE-15 | Terminal artifact 加载失败或 late/unknown MCP event | 按现有 reducer/runtime 关闭或保持订阅，不提前清 state | P6 runtime tests |
| EDGE-16 | Script helper/path/mode 迁移 | shebang、executable bit、role/env、stdout/stderr、exit code 与 workflow trigger 不变 | P7 CLI tests/diff summary |
| EDGE-17 | Targeted discover 发现 0 test 或目标 skip | gate 失败，不得用聚合 suite 的其他 PASS 掩盖 | 全部 gate result |
| EDGE-18 | 测试数据库 cleanup 失败 | 记录 inventory 与 operator action，停止复用该 DB，不得连接生产/通用 DSN | P0/P5 rollback record |
| EDGE-19 | Result Parser first-send后取消或 semantic-invalid checkpoint | post-checkpoint cancel 丢弃checkpoint并传播；semantic validation只在process/gate收口后hard fail；process trace exact | P3 barrier fixtures |
| EDGE-20 | Gateway close 横跨 call registration/on_created | mixed `accepting_calls/closing` 临界区和两次 accepting barrier exact；Tool send 0或1按actual trace | P3 field/call matrix |
| EDGE-21 | Control token发送前 endpoint/TLS/pin失败 | credential send 0、redirect 0、MCP call 0；gate fail | `P3.real-mcp` control protocol |
| EDGE-22 | Parallel wave 一个 sibling 抛异常 | 保留当前 gather不取消已启动 sibling、零 wave Agent outcome；只有 scheduled execution 走 `execution_crash` terminal CAS，HTTP continuation/startup recovery/post-ready recovery 分别保持 13.6 的现有收口 | P2 四入口 barrier fault matrix |
| EDGE-23 | 真实 MCP cleanup/revoke 部分失败 | run不可复用，记录脱敏operator action；清理后也必须新run重跑全部场景 | controlled MCP evidence |

## 24. 依赖、风险与假设

### 24.1 依赖与权威

| 依赖 | 用途 | 约束 |
|---|---|---|
| `docs/prd/backend/unified-agent-loop/` | Agent execution/recovery 唯一 authority | P2/P4 不得重新解释 |
| `src/**/AGENTS.md`、`frontend/AGENTS.md`、`native/AGENTS.md`、`scripts/AGENTS.md` | 模块职责与门禁 | 每个检查点重新读取并同步索引 |
| Conda `multi_agent` + `requirements.txt` | Python runtime/tests | P0 记录版本；不新增生产依赖 |
| Frontend `package.json` lock/install | Vitest/TypeScript/Vite | 不升级依赖；全量/typecheck/build required |
| Rust toolchain 1.95 与 quality tools | Native gates | macOS dev + Ubuntu 22.04 release；缺 required tool 不算 PASS |
| GitHub Actions `result-parser-linux-gate` | P3 Linux RLIMIT/regex required profile | Ubuntu 22.04 + Python 3.13.13 + pinned requirements；commit-bound artifact、target/full零skip |
| PostgreSQL 17 isolated profiles | P5 parity/permissions/concurrency | 明确非生产、模块独立、零目标 skip、可清理 |
| Controlled MCP Server + `maf.cleanup.real_mcp.control.v1` API | P3/P4 discovery/call/approval/parser/no-replay真实门禁 | pinned HTTPS control、non-prod/run-isolated、canonical fixtures/counters、credential revoke/cleanup；缺失为 `blocked_external` |
| Git `main` development branch | 检查点与回滚 | 不切 `prod`，不 reset/checkout 用户数据 |
| Git-ignored external `skill/` | 外部 Project Skill | 不属于本仓清理；仅相关接口回归，不伪报外部 bundle 测试 |

### 24.2 风险登记

| Risk ID | 风险 | 缓解/退出条件 | 状态 |
|---|---|---|---|
| R-01 | 仓外消费者直接 import 未导出私有符号 | P0 import inventory + 兼容 wrapper；无证据时不删 | active |
| R-02 | Core/Storage/State/Lifecycle 新循环依赖 | AST edge manifest；每 CP import smoke；forbidden edge 失败 | active |
| R-03 | Source-shape tests 阻止安全 mixin 拆分 | 先建更强行为/trace tests，再迁移布局断言 | active |
| R-04 | PostgreSQL parity profile 不完整或误用共享/生产 DB | 11.6 独立 live suites、DB inventory/cleanup、零 skip | blocked_external until run |
| R-05 | Result Parser Linux-only 与 Rust/manylinux path 未在 macOS 覆盖 | P3/P7 required remote gate；未跑为 pending_platform | pending_platform |
| R-06 | Coordinator/Gateway 拆分破坏 mixed-lock/two-accepting barrier或产生二次外部调用 | field-site lock matrix + exact call trace + 17-boundary actual/restart delta | active |
| R-07 | Result Parser worker 边界因 module move/pickle、semantic validation前移或post-checkpoint cancel漂移 | worker entry/type 原位 + barrier fixtures + staged cleanup/cancel tests + Ubuntu gate | active |
| R-08 | Frontend Hook effect identity 导致双订阅/timer | StrictMode call trace、唯一 owner、F7/F8 focused tests | active |
| R-09 | DOM wrapper/class/focus 漂移但单元测试未察觉 | F3 DOM/a11y contract；styles zero diff；必要时现有浏览器 smoke | active |
| R-10 | Compatibility facade 永久堆积第二套实现 | AST facade allowlist；P8 owner/inventory closure | active |
| R-11 | Masking fallback 在搬迁中被误修或进一步隐藏 | symbol-level registry；结构 commit 禁止行为变更 | active |
| R-12 | 公开 Python/Rust model 物理迁移改变 identity | P8 单独评估；不满足 manifest 即保留原位 | active |
| R-13 | Implementation plan 因前序修改失真 | 每个计划只在进入前基于当前 HEAD 生成 | mitigated |
| R-14 | scripts SQL/role/mode/CLI 合同因通用 helper 漂移 | audit-only 边界、CLI trace、diff summary、role tests | active |
| R-15 | 无关用户改动在长任务中被覆盖 | 每 CP clean/dirty inventory，只编辑主责文件，保留无关 diff | active |
| R-16 | 受控真实 MCP control protocol/Server/授权不可用，或 cleanup/revoke 失败 | pinned HTTPS/SPKI + v1 manifest/run API/canonical fixture；失败 artifact 保留 run-ref digest 与外部 operator cleanup action，不复用 run；缺失为 blocked_external | blocked_external until run |
| R-17 | 当前 parallel gather异常不取消 sibling，且四类入口的异常收口不同，与统一架构理想偏差 | `BEHAVIOR-ORCH-PARALLEL-001` 四入口 barrier trace；结构迁移保持 13.6 的 entry-specific failure/零 wave outcome 语义，行为修复另立设计 | bounded_deferred behavior |

### 24.3 假设与无阻断开放项

- `main` 仍是开发环境权威，`prod` 不在范围；
- 本设计不要求 schema/data migration，真实 PG 仅用于隔离验证；
- 现有 public/private import 只在 P0 manifest 后分类，无法盘点的仓外使用通过 facade 保守兼容；
- 当前测试是行为起点但不是充分证明，P0 会补 trace、parity 与 external profile；
- PostgreSQL DSN 和 Ubuntu runner 的实际可用时间未知，该未知已由状态机和停止条件界定，不需要改变产品方向；
- 本轮没有定价、用户政策、合规姿态或 rollout 产品决策，未发现需要用户额外选择的 material business decision。

## 25. 停止与回滚

以下任一情况立即停止当前检查点：

- import、signature、type/module identity 漂移；
- event/payload/digest/error text 漂移；
- SQL、lock、CAS、commit/rollback 或 external call 次数漂移；
- Frontend DOM/ARIA/focus/scroll 漂移；
- Rust contract/proto/PyO3/Cargo contract 漂移；
- 需要 schema/data migration 才能继续；
- 高风险 PostgreSQL 检查点缺少真实 profile；
- 必须通过改变旧行为才能让测试通过。

每个检查点开始前记录 start commit、`git status --short` 和 checkpoint-owned 文件差异；这些文件必须无未授权重叠修改，无关用户 diff 则逐项登记并保留。

检查点尚未提交而失败时，只能用经审阅的反向 `apply_patch` 逐文件撤销本检查点改动；禁止 `git reset`、`git checkout --` 或覆盖无关用户文件。检查点已提交后使用逆序 `git revert`：若有 evidence-only commit，先 revert evidence，再 revert implementation/test commit。每个检查点恰好一个业务 implementation/test commit，允许紧随一个只含 evidence/index 的 commit；不得 squash 成巨型提交，也不得把第二个业务改动藏进 evidence commit。

真实 PostgreSQL gate 还必须记录专用 database/schema/role 的 before/after inventory、清理结果和失败时 operator action。清理失败的测试数据库不得复用；不得使用生产或未明确隔离的通用 DSN。计划内无生产 schema/data migration，但验证环境 cleanup 仍是 required gate。

任何回滚都不得影响 `docker_cmd.md`；检查点前后验证其存在、权限不高于 `0600`、被忽略且未跟踪，不读取正文。

## 26. 文档与 Git

- 每个计划开始前生成基于当前 HEAD 的详细 implementation plan；
- 每个结构检查点同步检查对应 `AGENTS.md` 与 `CHANGELOG.md`；
- 模块职责、入口或目录变化时更新索引；
- 每个检查点有独立 implementation/test commit；需要 checked-in gate artifact 时紧随 evidence-only commit；
- 依赖前置必须 `complete`；与 `bounded_deferred`/外部阻断无依赖的计划可按 10.1 继续；
- 只读审计可以并行；主工作树写入任务只在文件边界互不重叠时并行，最终由主代理统一集成。任何 evidence-bound gate 使用的 disposable checkout 禁止并行写，验证完成并移除后才可复用该并发槽。

## 27. 完成标准

项目级清理只有同时满足以下条件才完成：

- P0～P8 的 mandatory structural deliverable 均为 `complete`；行为修复类 `deferred_out_of_scope` 不阻断，但必须登记；
- 任何 required PostgreSQL、Ubuntu/manylinux 或受控真实 MCP gate 为 `blocked_external`/`pending_platform` 时，总体不得标 `complete`；
- FR-01 inventory 中每个当前/历史业务源码路径均有唯一 primary owner、可审计 touch/action，最终 status 仅 `changed|reviewed_no_change` 且 `planned_change=0`；
- FR-01～FR-17、NFR 矩阵和 21.2 计划退出条件全部有绑定 `tested_commit` 的 evidence；`P3.real-mcp` relevant-source digest 与最终相关源码集合现场重算一致；
- 各旧公开 facade 只承担兼容与装配，不保留第二套业务实现；
- P0 forbidden dependency edges 已清零；兼容 exception 必须有唯一 owner、理由和退出条件；
- 已识别的完全复制实现已合并，语义不同的相似实现有清晰所有权；
- 结构变化已同步 AGENTS/CHANGELOG；
- Backend、Frontend、Rust 相关全量门禁通过；
- PostgreSQL、Ubuntu/manylinux、受控真实 MCP 等 required gate 实际运行通过；非适用项必须有明确 N/A 依据；
- `docker_cmd.md` 始终存在、被忽略、未被跟踪且未被读取；
- 最终报告按 AI Slop Cleaner 格式记录范围、行为锁、简化、fallback、changed files、checks、风险和延期项。

如果 P3、P5、P7 或真实 MCP required gate 仍被外部条件阻断，可以报告其他独立 deliverable/计划完成，但总标题必须写明项目级全部 `blocking_gate_ids` 与 `blocked_external`/`pending_platform`，不得使用“全仓清理完成”。

代码行数、文件数量或 lint 数字不是单独完成条件。职责单一、依赖清晰、重复实现消失且行为证据闭合，才是本计划的成功标准。
