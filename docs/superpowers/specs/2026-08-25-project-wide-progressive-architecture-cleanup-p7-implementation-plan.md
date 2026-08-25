# 全仓业务代码渐进式架构清理 P7 实施计划

## 1. 状态与硬边界

- 日期：2026-08-25
- 分支：`main`
- 状态：`active`
- P7 start commit：`a5c9318aa6281091f8cdbe06e594b163e0b37984`
- P7 start tree：`f87b61c2d0c0f883b8b2700e174a02154cad4366`
- P7 start tracked set：1081
- P7 start path-list SHA-256：`fc319a6f9fd6c2802c99957399b977a6c756dbe155c8f87d682503e69464363b`

P7只处理总设计已分配的四个workstream：Runtime Sidecar、Skill Runtime、MCP Runtime、Operational Scripts。Rust crate-root public declarations/free functions、Proto、checked-in contract、Cargo production dependency/feature方向、PyO3 facade、CLI/SQL/receipt、backend transaction与错误时序均为冻结合同。测试/验证脚本只为迁移行为锁或既定MCP fuzz gap做最小适配，不作为清理对象。

## 2. 基线与 finding register

基线已通过：Rust/Python contract与migration focused 97项、Scripts 63项、Rust fmt、workspace all-target/all-feature Clippy `-D warnings`、workspace 147 tests。Scripts三语句以上exact function-body duplicate为0；Ruff只剩4个Scripts C901，不以复杂度信号强拆安全CLI。

| Finding | 分类 | P7处置 |
|---|---|---|
| `P7-RS-DUP-001` | exact duplicate | SQLite adapter两份`validate_expected_status`/`write_failed`复用root private canonical owner；错误与事务体不变 |
| `P7-RS-CODEC-001` | structural candidate | 只把约350行private protobuf codec原样迁入`codec.rs`；public records/kernel/service/gRPC/serve与SQLite transaction原位 |
| `P7-SKILL-STDIO-001` | structural candidate | portable bounded stdio private helpers原样迁入`stdio.rs`；process-group/env/spawn/kill/wait orchestration原位 |
| `P7-SKILL-SERVICE-001` | structural candidate | Service/gRPC impl与private codec按职责迁入private modules；root public types/free async serve functions保持物理定义 |
| `P7-MCP-GATE-001` | gate gap | 新增独立`mcp_runtime_protocol` local/Ubuntu 30秒fuzz gate；fuzz lock对齐production `rmcp 1.7.0`；增加root/Proto/Cargo/bytes合同锁 |
| `P7-MCP-BOUNDARY-001` | structural candidate | contract/error、official SDK/shadow、JSON-RPC、sanitizer、registry分批迁入private modules；root public declarations/free functions保持 |
| `P7-SCRIPTS-ENGINE-001` | structural candidate | `migrate_mcp_dispatch_aggregate.py`同文件复用DSN guard、PG engine lifecycle和rejected JSON emitter；环境读取与异常/exit顺序不变 |
| `P7-SCRIPTS-CROSS-001` | reviewed_no_change | 其它CLI/engine/printer/JSON/SQL相似点的role、guard、error、partial-apply或生命周期不同；不建跨脚本框架 |
| `P7-RS-BACKEND-001` | reviewed_no_change | memory/SQLite相似实现是双backend parity；不引入production backend trait，不拆atomic transaction/CAS/schema |
| `P7-SKILL-POLICY-001` | reviewed_no_change | public contract/policy free functions保持root完整实现；不以thin wrapper或新常量表改写行为 |
| `P7-DEFERRED-001` | deferred behavior | Sidecar Agent lease/recovery-list/cancel、Skill child cleanup/locking、MCP version/fingerprint/sanitizer、Scripts pre-validation engine leak均不修复 |

## 3. Checkpoints

### Checkpoint A：审计、计划与基线

四workstream独立只读审计；冻结216个Rust crate-root public items及三份workstream contract：

- Runtime Sidecar：69项，semantic public-shape `3fdbccdbbd3eb608a38507f28071252f26176894cf5a7d5e3f5bbfce1fc2f6d2`；
- Skill Runtime：60项，semantic public-shape `bfceb1f9284a48b26cde6d4fe8d518e82846405058de0f74f866fac777bc7a3f`；
- MCP Runtime：87项，semantic public-shape `1b47306f86d1bc8f512aac2c545c1f59d18fa4157c9c723ba07733586b15d829`；
- contract bytes：Sidecar `f1bce693...c40ee6`、Skill `bafaa94a...f17c`、MCP `ab27de5f...0233`；
- Proto bytes：Runtime `1201f380...7742f`、MCP `f8951ffc...afbc`、Common `35d7c12e...2a89`。

提交：`docs(cleanup): plan P7 native and scripts boundaries`

### Checkpoint B：先闭合MCP可执行合同门

- 新增MCP root public compile/type-name/constant/function fixture，锁39 const、7 enum、27 struct、14 free functions及关键associated methods；
- 在现有Python合同测试锁Common/MCP Proto、checked-in JSON、main stdout、production Cargo feature/dependency和双lock `rmcp=1.7.0`；
- fuzz manifest直接pin仅测试图使用的`rmcp = "=1.7.0"`并机械更新`native/fuzz/Cargo.lock`，不改production manifest/lock；
- `run_rust_quality_gates.py`新增独立`mcp_runtime_protocol_fuzz_smoke`，workflow与测试加入Ubuntu 30秒实际执行；不替代原Skill fuzz gate。

运行MCP Rust/Python focused、locked fuzz compile与本机30秒MCP fuzz。提交：`test(native): lock MCP runtime refactor contracts`

### Checkpoint C：Runtime Sidecar private去重与codec

先让SQLite adapter导入root canonical `validate_expected_status`、`write_failed`，删除两份exact body；再把private protobuf conversion函数原样迁入`codec.rs`并由root/gRPC导入。不得移动public declaration/free function、Grpc trait、kernel/service、serve/config、SQLite method/transaction、schema/CAS/lease/control。

迁移codec逐function AST/text等价；运行Sidecar/Runtime Store Rust、Python Agent/contract/gRPC focused、fmt与crate Clippy。提交分为：

- `refactor(sidecar): reuse validation error helpers`
- `refactor(sidecar): isolate protobuf codec`

### Checkpoint D：Skill Runtime private stdio与service边界

- `stdio.rs`只承接portable LimitedReader state、stdin writer、bounded drain/deadline/snapshot/response helper；函数体原样；
- `service.rs`承接`SkillSandboxService` impl，`grpc.rs`承接Grpc impl/tonic trait与protobuf codec；root public structs及三个async serve functions仍物理留root；
- process manager的path/env/process-group/spawn/kill/wait/error优先级保持root原顺序，不改policy/contract/PyO3。

移动前补最小existing-gap assertions：空`requested_services` fallback、失败handshake不改变readiness及stdio partial/错误优先级仅在现有测试确有缺口时增加。运行Skill Runtime/PyO3 compile、Python contract/PyO3、fmt与crate Clippy。此切片不修改PyO3 bridge/contract/packaging或platform-specific process-group代码，manylinux/Ubuntu sandbox为N/A。

提交分为：

- `refactor(skill-runtime): isolate bounded stdio`
- `refactor(skill-runtime): isolate service and grpc adapters`

### Checkpoint E：MCP Runtime private boundaries

按依赖顺序独立迁移：

1. `contract.rs`/`error.rs`；
2. `json_rpc.rs`；
3. `sanitizer.rs`；
4. `registry.rs`；
5. `official_sdk.rs`。

所有root public declarations/free functions与outer attrs原位；public function可委托唯一private implementation，不得用re-export改变canonical definition。Private module不得反调root wrapper。JSON-RPC检查顺序、sanitizer state/count/caps、registry失败状态、official SDK FNV fingerprint/版本/shadow/redaction/error均逐body或fixture等价；不接通预备registry、不新增gRPC/build.rs、不改`main.rs`、Proto、Cargo production graph或checked-in contract。

每批运行MCP crate tests/Clippy/contract bytes；JSON-RPC与sanitizer批次额外运行locked compile和30秒MCP fuzz。可按风险合并为不超过3个清晰提交，但不能把行为修改混入。

### Checkpoint F：Operational Scripts单文件复用

先在`test_migrate_mcp_dispatch_aggregate.py`锁report/apply success/error的engine create/dispose count、invalid DSN/backend时零create、stdout reason与exit 2/3、环境读取顺序。随后只在该脚本增加private DSN loader、PG engine context manager与rejected JSON emitter；report仍先copy env再读DSN，apply仍先读DSN再copy env；authority conflict捕获仍早于migration error。

其它10个operational/SQL business paths全部`reviewed_no_change`。运行该模块、Scripts 63+新增项和真实PG N/A说明；SQL未改时不要求PG profile。提交：`refactor(scripts): reuse MCP aggregate engine lifecycle`

### Checkpoint G：P7全量门禁与handoff

必须通过：

- Python 7-module Rust contract/migration suite、Scripts full；
- Rust workspace fmt、all-target/all-feature Clippy、workspace tests；
- MCP locked fuzz compile与本机30秒实际fuzz；
- 三workstream checked-in contract/Proto/Cargo/public surface零漂移；
- touched crate focused与Python bridge/integration；
- `git diff --check`、tracked universe、file mode、dependency/license和`docker_cmd.md`保护检查。

nextest/coverage/audit/deny/release artifact等现有平台gate按availability运行并准确登记；未触及的manylinux、真实PG、Frontend、Backend业务、外部MCP与`prod`不得冒充PASS。同步本计划、`native/AGENTS.md`、`scripts/AGENTS.md`、`docs/AGENTS.md`与`CHANGELOG.md`。

提交：`docs(cleanup): close P7 native and scripts boundaries`

## 4. 停止与回滚

若root public item/path/signature/attrs变化、checked-in/Proto bytes变化、production Cargo graph变化、function/codec迁移不能等价、测试显示error/order/call-count/transaction变化、实际fuzz失败、需要修改PyO3/Proto/SQL/production wiring或修deferred behavior，则停止该候选并保留前一绿色检查点。

每个checkpoint独立commit，逆序revert。P7不删除公开预备registry/deprecated CLI，不清理dead code；只有P8基于P7终态HEAD重新证明后可处置私有零引用项。
