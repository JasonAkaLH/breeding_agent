# COMPLETED Task 与 Recoverable Run 直接收敛实施计划

依据：`2026-09-04-completed-task-recoverable-run-direct-convergence-design.md`

状态：`published_pending_deploy`
日期：2026-09-04
目标分支：`main`

## 目标与边界

把已批准的生命周期authority规则落地：`Task=COMPLETED + recoverable AgentRun`直接、原子、幂等收敛为
`Run=COMPLETED`，不创建或改写任何Message、AgentItem、TaskNode、Artifact、Event、FinalReceipt或final
projection。FAILED/CANCELLED既有收敛保持不变；identity、CAS和未知组合继续fail closed。

不修改数据库schema、公开API、Frontend、MCP parser/projection、外部Skill或`prod`。不直接修改开发库，
不执行部署。范围外未跟踪`test.json`保持未读取、未修改、未暂存。

## Checkpoint 0：红测锁定

- 扩展API recovery测试，证明COMPLETED不再抛
  `agent_startup_terminal_task_has_recoverable_run`，且prepared/Skill loader零调用；
- 增加SQLite repository合同测试，锁定Run字段转换、Task原样和所有成功业务对象零新增/零改写；
- 增加Runtime Sidecar repository测试，锁定既有`CommitAgentState` operation、稳定幂等key、零projection；
- 保留FAILED/CANCELLED、identity/CAS冲突和普通`commit_final`回归。

## Checkpoint A：Repository 原子操作

- `AgentAtomicWriter`新增单一completed-from-terminal-task方法；
- SQLite在同一事务锁定Run与Task，要求Task已COMPLETED、Run为recoverable、revision/claim精确匹配；
- 写Run为COMPLETED，清waiting/claim/lease，revision递增，写同一时间和稳定reason code；
- PostgreSQL继承共享实现；重复调用只接受完全一致的既定终态。

## Checkpoint B：Runtime Sidecar 适配

- Python Sidecar repository复用现有`CommitAgentState` wire，使用独立operation和稳定幂等key；
- 携带原样COMPLETED Task，items/nodes/artifacts/final projection均为空；
- 证明现有non-`commit_final`校验可承载，不修改Rust/proto/schema；
- repository行为合同与SQLite/PostgreSQL一致。

## Checkpoint C：Runtime 接线

- AgentLoopOrchestrator新增窄入口，负责读取Run、调用writer、释放进程内context；
- `_converge_terminal_task_recoverable_run()`为COMPLETED增加第三个同名终态分支；
- 在任何Conversation、Message、prepared authority或Skill lookup前完成；
- 写后回读Task/Run均须COMPLETED，再best-effort清理current-task指针。

## Checkpoint D：部署 Audit

- 受保护`docker_cmd.md`在仓库外建立`0600`备份；
- pre/post audit新增`terminal_completed_task_recoverable_run`分项，并纳入terminalizable总数；
- pre允许该项存在，post要求归零；不输出Task ID、DSN或业务正文；
- 保持旧writer退出、数据库强制只读、900秒health和frontend延迟恢复门禁。

## Checkpoint E：验证、发布与账本

- 运行聚焦API/Storage/Sidecar测试及API、Storage、Orchestration、E2E相关分层回归；
- 运行compileall、Ruff、Rust相关门禁、audit镜像内语法/import/classifier和`git diff --check`；
- 代码检查点后构建并推送新backend-dev `0.1.33`，验证远端`linux/amd64`、attestation、镜像无
  `/app/config.yaml`和新收敛入口；
- 受保护`docker_cmd.md`仅把backend/audit候选统一更新到`0.1.33`；
- 更新设计状态、实施计划、`docs/AGENTS.md`和`CHANGELOG.md`。Frontend、Runtime Sidecar镜像、部署和
  `prod`保持不变。

## 完成声明

只有代码、三repository合同、runtime接线、audit、自动门禁、远端镜像和非敏感账本全部闭合，才标记
`published_pending_deploy`。真实开发hard cut与数据库收敛需用户执行部署命令后另行验收。

## 实施账本

- Checkpoint 0/A～C：`5caac314`完成红测、三repository合同、原子writer和Runtime前置接线；
- Checkpoint D：受保护`docker_cmd.md`已新增`terminal_completed_task_recoverable_run`，pre计为可收敛、post要求
  归零，并保持只读审计、旧writer证明、900秒health和frontend延迟恢复门禁；
- Checkpoint E：聚焦85项、API 649项、Storage 573项（14 skip）、Orchestration 200项、E2E 12项、compileall、
  变更面Ruff、Rust fmt/clippy/cargo test/nextest（198项）、cargo-audit和diff-check通过；全仓Ruff仅保留12个
  与本次无关的既存测试unused-import错误；
- backend-dev `0.1.33`已发布，远端OCI index digest为
  `sha256:97d9885c9684f2ff8eb57bf8e0c94bb5409ee9b9d18e5e0192b97b0ade3c191e`，`linux/amd64`
  manifest为`sha256:0f69e6a87f913a836093aa1e9f2d51916c425e1e27b59694d929e366ba6298ca`，attestation为
  `sha256:a095c9643525944ec1f9fae73e9fdbdde44978282c5a3819245d62a904481ad2`；远端digest重拉smoke通过；
- 未执行开发部署、pre/post真实数据库审计或直接数据修改；Frontend、Sidecar镜像、schema、外部Skill、`prod`和
  未跟踪`test.json`均未改。

License Requirement：复用现有Python、SQLAlchemy、Agent repository、Runtime Sidecar
`CommitAgentState`、unittest、Rust测试链、Dockerfile与Docker Buildx；无新增依赖、第三方代码或许可变化。
