# 全仓业务代码渐进式架构清理设计

- **日期**：2026-08-24
- **状态**：设计章节已获用户逐项批准；书面设计待最终审阅
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
- Ruff 对后端与脚本给出 666 个复杂度信号，但这些信号只能作为审计入口，不能作为自动改写依据。

六个并行只读审计工作流覆盖了 API/Core、Auth/Lifecycle/State/Storage、Orchestration/Capabilities、Integrations、Frontend、Native/Scripts。审计确认：仓库同时存在高置信的死代码和复制实现，也存在大量看似重复、实际承载协议版本、安全 authority、错误码、事务、锁、幂等、恢复和兼容语义的代码。直接“大一统”重写无法满足功能不变要求。

本设计选择架构优先方案，但采用稳定门面和渐进接管，不做 big-bang rewrite：

```text
原调用方 -> 原公开模块/类型 -> 新的内部职责模块
```

原模块先作为兼容门面保留，新模块一次只接管一个职责。每个检查点全绿后才进入下一检查点；任何可观察行为漂移都回滚当前检查点。

## 2. 现状行为基线

设计前只读基线如下：

- 当前分支为开发分支 `main`，工作树干净，HEAD 相对 `origin/main` 领先两个既有提交；
- Backend canonical 域通过：Core 42、Storage 400、Lifecycle 37、Integrations 704、Agent Skills 209、Orchestration 102、Capabilities 45、API 436、E2E 7、Observability 39、Scripts 62、Deployment 3；
- Frontend 21 个测试文件、307 项测试通过，`npm run typecheck` 与 `npm run build` 通过；
- Rust workspace tests、`cargo fmt --check`、Clippy `-D warnings` 通过；
- 本地 Git-ignored 的 `skill/sql-query` 不存在，对应外部 Skill 测试记为 N/A，不属于本仓业务代码完成证据；
- 本轮基线未重新运行真实 PostgreSQL profile，因此 P5 的 PostgreSQL 高风险检查点必须重新取得真实 DSN 证据；
- Linux-only manylinux wheel smoke、真实外部 MCP 和生产观察窗不是普通本地重构门禁，缺失时必须准确记录验证缺口。

这些数字是设计起点，不是未来检查点可以复用的通过证据。每个受影响检查点必须基于当时的代码重新运行对应门禁。

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

## 5. 总体设计原则

### 5.1 稳定门面

- Python 旧 import、类名、函数签名、默认值和公开导出继续可用；
- Python 公开类型如受 `__module__`、pickle、repr 或 introspection 影响，优先保留定义原位；
- Rust public struct/enum 优先继续定义在 crate root，仅移动 impl、private helper 和 free function；
- Frontend 旧 `api/types.ts`、`domain/artifacts.ts`、`domain/taskEvents.ts` 继续 re-export 或保留公开 facade；
- Storage 的三条 `StoragePort` 导入路径必须继续指向同一个 aggregate protocol 对象。

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

## 7. 目标依赖方向

```text
API assembly/routes
  -> Orchestration Agent Loop
    -> Capability
      -> Integration / external I/O
        -> Storage authority
          -> Event / Projection / SSE
```

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

Frontend 目标依赖方向：

```text
Wire Contracts
  -> Domain validation/projection/reducer
    -> Controller Hooks
      -> Presentational Components
        -> App shell
```

Rust 目标依赖方向：

```text
crate-root public declarations
  -> private contract/validation
    -> kernel + backend adapter
      -> service
        -> codec/gRPC
          -> config/serve
```

## 8. 系列计划总览

| 计划 | 主目标 | 风险级别 |
|---|---|---|
| P0 | 冻结公开合同、行为 trace 与环境门禁 | 低 |
| P1 | 无状态 helper、wire contract、StoragePort 子协议与基础依赖方向 | 低 |
| P2 | Orchestration/Capabilities 所有权、continuation、Skill authority、Prompt | 中 |
| P3 | Agent Skills、Result Parser、Gateway、Coordinator | 中到高 |
| P4 | `ApiRuntime` 内部组件与 factory | 中到高 |
| P5 | Storage/State/Lifecycle 与 SQLite/PostgreSQL parity | 高 |
| P6 | Frontend App、reducers、controllers | 中到高 |
| P7 | Rust 大型模块与 scripts | 中 |
| P8 | 已确证死代码/重复收尾与全仓最终证明 | 低到高，按项分开 |

每个计划在进入实现前生成基于当时 HEAD 的详细实施计划。当前计划全绿后才激活下一计划，避免预先写出随后失真的实现步骤。

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
- byte-level 比较 runtime/skill/MCP 三份 checked-in contract；
- 建立内存/SQLite 同 fixture parity，SQLite reopen/durability 保持 backend-only 断言。

### 9.4 PostgreSQL

P5 前明确真实测试 profile：auth CAS、Task/Node CAS、mailbox、interrupt、event order、owner guard、claim takeover、rollout role separation、legacy migration role、conversation delete 并发、fresh bootstrap 和 drift rollback。

## 10. P1：无状态基础层

P1 只抽纯 helper、类型和协议，不迁移有状态控制器。

### 10.1 StoragePort 子协议

新增 `src/core/storage_ports/`：

- `auth.py`
- `conversation.py`
- `lifecycle.py`
- `mcp_config.py`
- `mcp_dispatch.py`
- `mcp_remote_task.py`
- `mcp_rollout.py`
- `composite.py`

`src.core.contracts.StoragePort` 与 `src.storage.interfaces.StoragePort` re-export 同一个 composite 类型。方法签名逐项原样迁移，不先修改生产消费者注解。

### 10.2 共享纯 helper

候选包括：

- capability event helper，但不合并 event-id material 不同的 Skill event；
- Agent Skills slot JSON、file-selection validation 和 slot contract；
- MCP attachment display sanitizer；
- API bootstrap/trust/registry/event helper；
- Frontend wire contracts；
- Rust 单一常量源和 crate-private validation helper。

任何 helper 只能承载完全相同的纯逻辑。调用方仍负责抛出各自原异常。

## 11. P2：Orchestration 与 Capabilities

P2 建议拆为以下检查点：

1. 锁 Agent item/event/interrupt/continuation 的完整顺序；
2. 将通用 capability event helper 移到 `src/capabilities/events.py`，旧路径 forwarding；
3. 收敛 MCP selector/router 中仅 generator/repair loop 相同的私有流程，不合并 parser；
4. 将 continuation locator 内存 authority 移到 `agent_loop/continuation.py`；
5. 定义通用 `InterruptAuthorityPort`，先用 adapter 包裹旧实现；
6. 将 Skill slot interrupt authority 移到 `capabilities/skill_tool/slot_interrupt_authority.py` 并由 API 注入；
7. 令 Main Agent prompt wrapper 委托通用 prompt profile/envelope，保留所有公开 import；
8. 最后删除已确证私有且无引用的 orchestration/capability dead code。

### 11.1 Agent waiting 顺序

必须保持：

1. sample/tool-call/reserved-result 已持久化；
2. ownership 校验；
3. TaskNode CAS `RUNNING`；
4. `node.started`；
5. capability 执行；
6. 再次 ownership 校验；
7. capability events；
8. TaskNode `WAITING_FOR_INPUT`；
9. bind interrupt；
10. continuation locator；
11. slot authority 与 slot events；
12. continuation 穿过 carrier；
13. save interrupt；
14. visible assistant question；
15. `node.waiting_for_input`；
16. call outcome `WAITING_FOR_INPUT`；
17. 更新 waiting calls 并释放 lease。

任何抽象不得重排以上步骤。

## 12. P3：Integrations

### 12.1 Agent Skills

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
- schema-load masking fallback 原样保留。

### 12.2 Result Parser

- 五个 `decoder_YYYY_MM_DD.py`、registry、worker entry、checkpoint 和 parser revision 保持原位；
- 仅从 service 抽 `worker_supervisor.py`，承载 spawn/pipe/timeout/terminate/kill/IPC；
- 父进程仍只接收不超过合同上限的 checkpoint 和 bounded projection；
- 不把 decoder 合并成配置驱动的大一统 parser。

### 12.3 MCP Gateway

先抽无状态：

- `gateway_catalog.py`
- `gateway_metrics.py`

再抽有状态：

- `GatewayScopeRuntime`：open/bootstrap/renew/close/invalidate；
- `GatewayCallRuntime`：execute/normalize/parse/cancel/continue。

`MCPGateway` 保持公开 facade。Gateway 独占 endpoint revalidation、credential read、adapter/client、external I/O 和 isolated parsing。

### 12.4 MCP Coordinator

最高风险、最后处理：

- 先在原类内将 dispatch 与 call-tool 切成明确 phase methods；
- 再迁入 `dispatch_preparation.py`、`dispatch_continuation.py`、`dispatch_terminal.py`；
- Coordinator 始终是唯一顺序控制者；
- reservation、may-have-dispatched、unknown convergence 和 no-replay 不得复制到 collaborator、Gateway 或 recovery；
- 每个 crash point 必须证明零二次 Tool 调用。

### 12.5 禁止合并的安全 authority

- Temporary Result Store；
- Pending Action Payload Store；
- Projection Store；
- CP7 terminal candidate；
- credential/master-key domains；
- historical raw resolver/managed copy。

它们的 key、AAD、size cap、mode、identity、no-clobber、exception 和 paired snapshot 均为独立合同。

## 13. P4：API Runtime

`src.api.runtime.ApiRuntime`、`build_api_runtime`、`src.api.__init__` 导出及测试 patch seam 保持稳定。

建议检查点：

1. `runtime_components/bootstrap.py`、`artifact_trust.py`、`registry.py`、`event_helpers.py`；
2. `runtime_components/files.py` 与文件/附件 runtime mixin；
3. `runtime_components/conversations.py`、`task_history.py`；
4. `runtime_components/interrupts.py`；
5. `runtime_components/lifecycle.py`，整块迁移 startup/recovery/shutdown；
6. `runtime_components/service_core.py` 与 `factory.py`；
7. 配合 P1 完成 StoragePort facade；
8. 公开 model 类物理迁移仅在 module/pickle 合同可证明时执行，否则保留声明原位。

### 13.1 关键限制

- env/config 不能在 import 时提前读取；
- helper 仍在原调用位置执行；
- factory 通过显式 `runtime_cls` 与 factory 参数保留 monkeypatch seam；
- `build_api_runtime` 公开 53 参数暂不改成新 config object；
- runtime holder 仍在完整构造后赋值；
- master-key sentinel、aggregate reconciliation、dispatch recovery、Agent recovery、Ready 和 post-ready remote task 的顺序不变；
- shutdown 的 quiesce、cancel/gather、CP7 close、service close 和 engine dispose 顺序不变。

## 14. P5：Storage、State 与 Lifecycle

核心原则：唯一 session/transaction owner 不变，方法体先原样迁移。

### 14.1 共享 SQLAlchemy 基础

新增：

- `src/storage/sqlalchemy/base.py`
- `src/storage/sqlalchemy/mappers/`
- `src/storage/sqlalchemy/repository_support.py`

先移动纯类型、row mapper、规范化和校验 helper。SQLite/PostgreSQL 继续 re-export 旧路径；PostgreSQL 不再依赖 SQLite 私有下划线 helper。

### 14.2 领域切片顺序

1. Auth 与 Conversation；
2. Task/Lifecycle projection，并给 Lifecycle 服务引入窄 port；
3. MCP config 与 owner authority；
4. MCP rollout/observability；
5. MCP dispatch、CP7、durable result；
6. Remote Task 与最终 assembly。

每个切片分别提供 repository-method mixin 与 facade-method mixin。`SQLiteStateRepository`、`SQLiteStorage`、`PostgreSQLStorage` 的旧路径继续作为 assembly。

### 14.3 事务与锁不变量

- `SQLiteStorage._run` 继续负责 session、`BEGIN IMMEDIATE`、commit、shield 和 cancellation wait；
- repository 内现有 flush 与 CAS 失败 rollback 原样保留；
- PG CP7、rollout、conversation delete runner 各自继续拥有 session/commit；
- Lifecycle 一次业务操作中的多次 storage 调用不得顺手合并成单事务；
- CP7 owner→server→intent→outbox→pending→branch→call→receipt→projection→candidate→durable→task→node→interrupt→answer→grant 锁顺序不得重排；
- 冻结 PostgreSQL override 集，禁止专用方法意外退化为 inherited generic path。

### 14.4 PostgreSQL 停止条件

没有真实 integration/permissions/并发 DSN 证据时，P5 可以停在纯协议、mapper 和 SQLite/shared 检查点，但不得进入或宣称完成 rollout、CP7、remote-task 和最终 PG parity。

## 15. P6：Frontend

建议检查点：

1. `contracts/taskEvents.ts`、`artifacts.ts`、`capabilities.ts`；
2. `domain/interrupts.ts`、`conversationMessages.ts`、`uploads.ts`；
3. 搬出 Message、Artifact、Files、Sidebar、Login 纯展示组件；
4. 拆 Artifact validation/data-query/MCP/file projector，保留 facade；
5. 拆 task-event validation、ledger 和 model，保留公开导出；
6. 拆 MCP 子 reducer，主 reducer 仍控制分派顺序；
7. `useConversationAttachments`；
8. `useConversationTaskRuntime`，只接管 accepted-task 后生命周期。

### 15.1 App 最终所有权

`App` 继续拥有：

- auth；
- conversation；
- composer；
- model；
- slash/MCP command menu；
- optimistic user/assistant message creation。

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

### 15.4 明确延期的前端问题

- MCP 命令菜单方向键；
- Artifact ID 语义推断；
- localStorage 异常差异；
- API 文本错误体 fallback；
- upload refresh 失败清空状态；
- 任何文案、视觉、DOM、ARIA、焦点或滚动调整。

## 16. P7：Native 与 Scripts

### 16.1 Runtime Sidecar

public type declaration 保持 crate root，逐步抽：

- `validation.rs`
- `codec.rs`
- `kernel.rs`
- `service.rs`
- `grpc.rs`
- `config.rs`
- `serve.rs`

`sqlite_adapter` 模块名保持，在内部按 schema、agent state、tasks、nodes、artifacts、events、leases、control、rows 拆分。此阶段不引入生产 backend trait。

### 16.2 Skill Runtime

拆为 contract、policy、sandbox/process、sandbox/stdio、service、grpc、codec、serve。Sandbox limits 使用单一常量源，但值不变。stdio partial-output、kill/wait、env clear 和 path guard 顺序不变。

### 16.3 MCP Runtime

拆为 contract、errors、official SDK、shadow、JSON-RPC、sanitizer、registries。不得顺手接通测试/预备 registry，也不修 client-version、fingerprint 或 sanitizer 行为。

### 16.4 Scripts

先处理单文件重复和 engine 生命周期，再在 `scripts/prd_evidence.py` 建窄的 ordered gate helper。不得统一所有 CLI、删除 deprecated 参数或改变 stdout/stderr、exit code、env name、role 与错误文本。

## 17. P8：收尾清理与延期审计

P8 只删除或收敛满足以下条件的内容：

- 私有；
- 仓库内零引用；
- 不属于文档化公开 API；
- 不参与 import side effect、registration、pickle/spawn；
- 有定向测试或静态证据；
- 删除不会改变异常、日志、事件、计时或副作用。

已审计候选包括未使用 import/local、断链旧 parser、不可达 fallback literal、完全相同 pure helper、无效半成品对象等。具体清单必须在 P8 基于当时 HEAD 重新验证，不能直接引用设计期行号执行。

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
conda run -n multi_agent python -m compileall -q src tests
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

## 20. 停止与回滚

以下任一情况立即停止当前检查点：

- import、signature、type/module identity 漂移；
- event/payload/digest/error text 漂移；
- SQL、lock、CAS、commit/rollback 或 external call 次数漂移；
- Frontend DOM/ARIA/focus/scroll 漂移；
- Rust contract/proto/PyO3/Cargo contract 漂移；
- 需要 schema/data migration 才能继续；
- 高风险 PostgreSQL 检查点缺少真实 profile；
- 必须通过改变旧行为才能让测试通过。

每个检查点一个 commit，不 squash 成巨型提交。计划内无 schema/data migration，代码回滚使用按检查点逆序 `git revert`。不得使用破坏性 reset 或影响 `docker_cmd.md` 的工作树操作。

## 21. 文档与 Git

- 每个计划开始前生成基于当前 HEAD 的详细 implementation plan；
- 每个结构检查点同步检查对应 `AGENTS.md` 与 `CHANGELOG.md`；
- 模块职责、入口或目录变化时更新索引；
- 每个检查点独立 commit；
- 当前计划全绿后才激活下一计划；
- 只读审计可以并行，写入任务只在文件边界互不重叠时并行，最终由主代理统一集成和验证。

## 22. 完成标准

项目级清理只有同时满足以下条件才完成：

- P0～P8 已执行或明确以证据延期；
- 各旧公开 facade 只承担兼容与装配，不保留第二套业务实现；
- 目标依赖方向无新增反向 import；
- 已识别的完全复制实现已合并，语义不同的相似实现有清晰所有权；
- 结构变化已同步 AGENTS/CHANGELOG；
- Backend、Frontend、Rust 相关全量门禁通过；
- PostgreSQL、Linux wheel、真实 MCP 等未运行项准确记录，不伪报；
- `docker_cmd.md` 始终存在、被忽略、未被跟踪且未被读取；
- 最终报告按 AI Slop Cleaner 格式记录范围、行为锁、简化、fallback、changed files、checks、风险和延期项。

代码行数、文件数量或 lint 数字不是单独完成条件。职责单一、依赖清晰、重复实现消失且行为证据闭合，才是本计划的成功标准。
