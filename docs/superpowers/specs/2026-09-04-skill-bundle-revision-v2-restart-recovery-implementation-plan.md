# Skill Bundle Revision v2 与旧任务终态化实施计划

依据：`2026-09-03-skill-bundle-revision-v2-restart-recovery-design.md`

## 状态

`published_pending_deploy`

计划基线为 `main@ad0f00db`。设计已经过12轮审查/修订，以98/100、0 Blocking、0 Major、2 Minor通过
完整信心门。仓库实现已由`36ce1a9c`、`f13fef39`、`97139173`、`9ab0fe4d`和`9253b299`
闭合；backend-dev `0.1.31`已基于`main@414afa2d`构建并推送，受保护`docker_cmd.md`仅同步backend
标签。尚未修改远端数据库、部署开发环境或触及`prod`。

范围外未跟踪文件`test.json`必须保持未读取、未修改、未暂存。

## 实施结果（2026-09-04）

- `36ce1a9c`：稳定full-SHA v2 revision、typed retired/invalid/unavailable分类，以及普通、delegated、
  Slot、retain的exact revision解析；
- `f13fef39`：SQLite、PostgreSQL继承实现与Runtime Sidecar共用的terminal Run+Task create，以及
  orchestrator窄初始化入口；Sidecar继续只用既有`CommitAgentState(operation="create_run")`；
- `97139173`：128条稳定分页候选、MCP前startup eligibility prepass、通用prepared authority只读加载、
  pending三kind terminal handoff、ack-last和terminal Task Answer/Slot liveness gate；
- `9ab0fe4d`、`9253b299`：删除剩余resume active fallback，exact校验handed-off Interrupt/Node/no-server
  intent，并让正常pre-Agent Interrupt在handoff时retain冻结revision。
- backend-dev `0.1.31`：远端OCI index digest为
  `sha256:3a90a5da0b5b9ce86987e82b3f49093cbc35b238e296ce96453078d81937ea25`，包含`linux/amd64`
  manifest `sha256:942ac5133bfc1a62cfd48d1648bf9eaf04130be47910a066e8b4e214889a0ea7`及attestation；
  按远端digest重拉后确认镜像不含`/app/config.yaml`，revision v2/retired分类和关键API runtime import
  smoke通过。

最终自动证据：Storage 569项通过（14项环境skip）、Orchestration 199项通过、API 646项通过、E2E 12项
通过、Skill capability聚焦11项通过、Agent Skills聚焦216项通过（13项平台/环境skip）；compileall、变更面
Ruff、关键package import、active-fallback静态扫描和`git diff --check`通过。Integrations全量运行801项时仅
保留仓库既有且与本变更无关的`test_client_rejects_negotiated_version_incompatible_with_transport_family`
稳定失败，另有2项环境skip；本轮未修改该MCP transport-family合同。

本轮没有新增目录职责或变更模块归属，根`AGENTS.md`无需修改；`docs/AGENTS.md`和`CHANGELOG.md`已同步。
发布版本升级为backend-dev `0.1.31`，受保护`docker_cmd.md`仅同步该backend标签并继续保持`0600`、
Git-ignored/untracked。未修改数据库schema/data、Rust/proto、Frontend、外部Skill、部署或`prod`；远端旧Task
实际终态化与开发环境hard cut仍需独立授权。

## 完成声明

只有以下条件全部满足，才可声明本地仓库实施完成：

1. 新写Skill revision严格为`skillrev-v2-<64 lowercase hex>`；同一fingerprint跨独立runtime state得到
   同一revision，v1至少6位counter格式、null/blank、unknown和missing-v2得到不同typed分类；
2. active v2只在新submission preparation冻结时选择；`bundle_for_revision()`、普通Skill executor、
   delegated activator、Slot manifest、retain和Agent recovery均不允许旧Task以空值或异常回退到active；
3. startup在MCP aggregate/recovery前终态化所有已有recoverable legacy AgentRun，以及已handed-off、
   无AgentRun的非终态legacy submission Task；SQL候选只作索引并回读当前Task/Agent authority；
4. `ACCEPTED/PLANNING/RUNNING` legacy Task收敛为`FAILED`，`CANCELLING`沿现有合同收敛为
   `CANCELLED`；单个可终态化旧任务不阻断其他v2 Run和应用启动；
5. pending prepared legacy/invalid/unavailable submission在route/memory/selector物化前进入terminal
   handoff；三种planned kind均使用原kind和确定性identity，Task终态后ack-last，故障重试不重复对象或副作用；
6. snapshot缺失、digest/schema/relationship损坏、handed-off agent_run缺Run、identity drift和terminal
   writer失败均保持fail closed，不猜测、不fallback、不写部分authority；
7. terminal Task的Answer、approval、显式chat replay和Slot隐式恢复在任何mutation/调度前拒绝；
   `list_interrupts()`只返回已有持久化Interrupt，历史读取不访问Skill runtime；
8. 旧Conversation仍可读；其下一条新消息清理terminal current pointer并创建显式绑定当前v2的新Task/Run；
9. SQLite、PostgreSQL继承实现和Runtime Sidecar Agent repository合同一致；Sidecar只复用已有
   `CommitAgentState` wire，不修改Rust/proto/schema；
10. 聚焦测试、Backend相关分层回归、compileall、Ruff、package import和`git diff --check`通过；最终
    diff不含Frontend、MCP parser/projection、外部Skill、部署文件、`docker_cmd.md`或`prod`。

## Checkpoint 0：基线与红测矩阵

### 目标

先锁定旧实现的实际缺口，保证后续修改只解决本设计。

### 红测范围

- `tests/integrations/agent_skills/test_skill_runtime_state.py`
- `tests/capabilities/skill_tool/test_executor.py`及现有普通Skill executor测试
- `tests/api/test_submission_admission_recovery.py`
- `tests/api/test_submission_admission_runtime_startup.py`
- `tests/api/test_agent_loop_runtime.py`、Interrupt/Slot相关现有测试
- `tests/storage/test_agent_storage_sqlite.py`
- `tests/storage/test_runtime_sidecar_agent_repository.py`
- `tests/storage/test_agent_repository_contract.py`

### 必须先失败的断言

1. 两个独立`SkillRuntimeState`产生不同计数器revision；
2. v1 exact key、null/blank和missing revision仍可能取得active或已有bundle；
3. 普通/delegated Skill和Slot manifest仍存在active fallback；
4. startup在MCP recovery前没有Skill eligibility prepass；
5. pending legacy submission仍进入正常materialization；
6. handed-off initial Interrupt无AgentRun时不会被枚举和终态化；
7. terminal Task仍可进入Answer/approval或Slot read-recovery mutation；
8. 三Agent backend缺少“直接创建terminal Run+Task”的exact operation。

若新增断言以外已有测试先失败，先记录并证明与本功能无关；不得修改生产代码掩盖基线问题。

## Checkpoint A：稳定 revision v2 与唯一执行分类

### 修改范围

- `src/integrations/agent_skills/skill_runtime_state.py`
- `src/capabilities/skill_tool/executor.py`
- `src/api/runtime.py`中的delegated activator、Slot manifest、retain和prepared recovery入口
- 对应Integrations、Capabilities、API测试

### 实现

1. 删除Skill revision进程计数器；`_fingerprint_digest()`返回完整64位SHA-256，`_make_bundle()`生成
   `skillrev-v2-<digest>`。
2. 增加唯一private classifier和typed error，closed分类为：
   - `retired`：null、blank、`^skillrev-[0-9]{6,}-[0-9a-f]{12}$`；
   - `v2`：`^skillrev-v2-[0-9a-f]{64}$`；
   - `invalid`：其他格式；
   - `unavailable`：格式合法v2但`_bundles`无exact key。
3. `bundle_for_revision()`成为exact执行resolver：null/blank/v1/invalid/missing-v2均抛带stable
   safe error code的typed异常；需要当前catalog的管理路径显式使用`active_bundle`。
4. `retain_revision()`、`activate_revision()`、`catalog_for_revision()`和
   `skill_name_for_capability()`沿同一exact resolver，不复制正则或错误映射。
5. 普通`SkillToolExecutor._resolve_skill()`和`activate_delegated_skill()`要求metadata显式携带
   revision，并分别返回retired/invalid/unavailable safe error；不得再统一成
   `skill_bundle_revision_missing`。
6. `_manifest_for_slot_collection()`删除广义异常→active catalog fallback；`_retain_task_skill_revision()`
   删除metadata缺失→active fallback；`_recover_agent_run()`删除prepared缺失→active binding。
7. 新submission builder继续显式读取当前`active_revision`并写入prepared authority；不得影响MCP
   revision或active Skill reload管理路径。

### 验证

- 同fingerprint跨两个state相同；内容变化revision变化、恢复内容revision恢复；
- 注入可命中的v1 key仍返回retired；
- null/blank、v1、unknown、missing-v2四类调用次数证明active bundle未被读取；
- 普通/delegated/Slot/retain/resume得到一致typed结果；
- 原动态刷新、retain/release和active registry测试保持通过。

建议检查点：

`test(skill): lock revision v2 execution boundary`
`fix(skill): require exact revision v2`

## Checkpoint B：三 Agent repository 的 terminal create

### 修改范围

- `src/orchestration/agent_loop/repository.py`
- `src/orchestration/agent_loop/orchestrator.py`
- `src/storage/sqlite/agent_repository.py`
- `src/storage/postgres/agent_repository.py`（继承合同验证，不复制实现）
- `src/storage/runtime_sidecar_agent_repository.py`
- repository contract、SQLite和Sidecar测试

### 实现

1. 在窄Agent writer合同增加`create_terminal_run`，输入包含确定性`AgentRun`、目标
   `FAILED/CANCELLED`和safe reason；只允许Task当前非终态且目标与Task状态矩阵匹配。
2. SQL实现用一次现有事务：
   - 锁定Task并复验immutable identity/current status；
   - 若Run不存在，直接插入terminal Run并同事务更新Task；
   - 若Run存在，只允许所有binding、reason、status和terminal identity exact replay；
   - 不创建AgentItem、Node、Event或Artifact。
3. Runtime Sidecar实现复用现有`CommitAgentState(operation="create_run")` wire，一次提交terminal
   Run和Task；response lost后先读取existing Run并exact验证，不增加Rust/proto operation。
4. orchestrator暴露窄`initialize_terminal_run`，只复用现有binding factory构造Run，不调用
   `commit_agent_user_message`、初始化Event或`run_initialized`。
5. PostgreSQL继续继承共享SQLAlchemy事务实现；三backend public async surface/signature合同同步更新。
6. `CANCELLING`只允许terminal target`CANCELLED`；其他非终态只允许`FAILED`。

### 验证

- 三backend首次写入与exact replay结果一致；
- 不同binding/reason/status/idempotency identity冲突；
- fault injection证明SQL事务零部分Run/Task；
- Sidecar trace只有existing`CommitAgentState`，无新RPC/proto；
- user/activation item和started Event数量均为0。

建议检查点：

`test(agent): lock terminal run initialization`
`feat(agent): add terminal run initialization`

## Checkpoint C：startup eligibility prepass 与 handed-off Task

### 修改范围

- `src/core/contracts.py`或等价窄local Protocol
- `src/storage/sqlite/repositories.py`
- `src/storage/postgres/repositories.py`（共享实现）
- `src/api/runtime.py`
- `src/api/submission_admission.py`中的prepared只读validator复用
- Startup、Storage、PostgreSQL相关测试

### 实现

1. 增加private SQL candidate reader，以`task_id`稳定游标、固定`limit=128`分页读取
   `ACCEPTED/PLANNING/RUNNING/CANCELLING` Task；SQL中的AgentRun存在性只用于缩小候选，不作为
   canonical authority。
2. 每个pre-Agent候选必须重新通过当前`TaskStoragePort`读取Task，并通过已选择Agent repository读取
   Run；已终态、identity漂移或已有Run时分别skip/fail closed/交给Run路径。
3. 抽取只读prepared authority loader，支持验证所有planned handoff kind的preparation
   record、prepared bytes/digest、schema/relationship、closed receipt、owner/Task/Message identity，
   不重算route/memory/selector。
4. 在`ApiRuntime.start()`中把`_reconcile_skill_recovery_eligibility()`放到
   `coordinator.project_pending()`之后、`_admit_mcp_rollout_instance()`和任何MCP aggregate/recovery之前。
5. Run路径：
   - prepared loader返回None视为retired；
   - v1/null/blank、unknown、missing-v2分别映射retired/invalid/unavailable；
   - Task非CANCELLING调用existing fail writer，CANCELLING调用existing cancel writer；
   - 成功终态化一个Run后继续下一个；writer/authority错误阻断startup。
6. handed-off无Run路径：
   - 只接受exact`interrupt`或`no_server_intent`；
   - `agent_run`无Run、snapshot损坏和identity漂移阻断startup；
   - legacy/invalid/unavailable initial Interrupt只用当前Task CAS完成FAILED/CANCELLED，保留Interrupt/Node；
   - no-server非CANCELLING复用existing exact convergence，CANCELLING只完成Task取消；
   - exact v2保持现状。
7. terminal后best-effort CAS清理`conversation.current_task_id`；失败不撤销terminal authority。
8. MCP recovery读取terminal legacy Task时必须沿既有status gate跳过；pending admission仍无Agent/MCP
   authority，留给Checkpoint D。

### 验证

- 顺序spy证明prepass先于所有MCP startup stage；
- 128/129/多页候选无遗漏、重复或游标漂移；
- SQL候选状态与Sidecar authority不同时以authority为准；
- 四Task状态、三revision错误和两handoff kind矩阵闭合；
- legacy Task终态后MCP outbox/recovery调用数为0；
- 第一条legacy失败后第二条exact v2正常恢复；
- corrupt authority和writer failure阻断startup。

建议检查点：

`test(skill): lock startup retirement matrix`
`feat(skill): retire legacy tasks before recovery`

## Checkpoint D：pending terminal handoff

### 修改范围

- `src/api/submission_admission.py`
- `src/api/runtime.py`的terminal handoff callbacks
- `tests/api/test_submission_admission_recovery.py`
- `tests/api/test_submission_admission_runtime_startup.py`
- 相关Agent repository测试

### 实现

1. Coordinator在`_validated_prepared_snapshot()`和closed receipt校验后、任何
   `materialize_route_decision/memory_context/selector_decision`及normal handoff callback前调用唯一
   Skill revision classifier。
2. exact v2保持当前路径；retired/invalid/unavailable进入新增
   `materialize_terminal_handoff(record, prepared, receipt, safe_error_code)` callback。
3. `agent_run`：
   - 构造prepared中的model binding；
   - 调Checkpoint B terminal initializer一次创建terminal Run+Task；
   - 不创建activation/user item，不sampling；
   - 返回`agent-run:<task_id>`。
4. `interrupt`：
   - 从validated prepared+closed selector receipt构造原确定性identity；
   - 幂等创建terminal Node/Interrupt，Interrupt状态为CANCELLED，不持久化可回答问题消息；
   - 按Task状态完成FAILED/CANCELLED。
5. `no_server_intent`：
   - 使用原`mcp-no-server`确定性identity；
   - 非CANCELLING复用existing terminal no-server convergence；
   - CANCELLING创建/读取安全历史intent并完成Task取消，不执行MCP。
6. terminal materialization后调用现有`_validate_handoff()`并ack`handed_off`；ack必须是最后一步。
   每个步骤response lost都通过exact read/replay收敛。
7. terminal agent_run后的wakeup明确no-op；normal callback、route/memory/selector materialization、
   Agent sampling、Skill/MCP调用均为0。
8. prepared snapshot本身缺失/损坏时保持现有错误和claim retry，不调用terminal callback、不ack。

### 验证

- 三planned kind × FAILED/CANCELLED × 三revision错误；
- exact v2路径调用序列与旧基线一致；
- materialize前、terminal对象后、Task终态后、ack前/后fault matrix；
- 每次重跑对象/identity/status唯一，无重复Event或业务副作用；
- snapshot corrupt全矩阵fail closed。

建议检查点：

`test(submission): lock legacy terminal handoff`
`feat(submission): retire pending legacy handoffs`

## Checkpoint E：终态 liveness 与历史只读边界

### 修改范围

- `src/api/runtime.py`
- Interrupt、approval、Slot、conversation history和message submission测试

### 实现

1. `_answer_interrupt_unlocked()`读取Task后立即拒绝COMPLETED/FAILED/CANCELLED；该门位于读取pending
   approval、保存Answer/Grant、reserve Message和任何continuation之前。
2. 显式chat replay继续统一进入同一answer门；不得建立第二套判定。
3. `_recover_missing_v2_slot_interrupts()`读取Task后对terminal status立即return，早于
   `list_slot_collections_for_task`、任何save、`_schedule_v2_slot_resume`和manifest lookup。
4. `list_interrupts()`对terminal Task只返回既有Interrupt；原安全可见消息历史投影行为保持不变。
5. Task终态后遗留OPEN Interrupt/pending approval仅作历史证据；所有mutation调用数为0。
6. 新消息提交沿既有admission self-heal清理terminal current pointer，并显式冻结当前v2到新prepared
   authority；旧prepared、Message、AgentItem、Artifact不修改。

### 验证

- direct answer、approval allow/deny、MRTR、remote input、显式chat replay都在首个mutation前拒绝；
- ready/waiting SlotCollection的terminal Task执行`list_interrupts()`时零write/zero schedule/zero Skill lookup；
- 旧conversation/message/task/artifact可读；
- 同旧Conversation的新消息创建新Task/Run并只含v2；
- 新Conversation提交基线不变。

建议检查点：

`test(api): lock terminal legacy liveness`
`fix(api): block legacy continuation after terminal`

## Checkpoint F：文档、全量门禁与本地完成

### 文档

- 更新本实施计划状态和实际检查点commit；
- 同步`docs/AGENTS.md`和`CHANGELOG.md`；
- 检查本次目录/职责变化是否需要更新根`AGENTS.md`；若无变化，记录无需修改；
- 不修改`docker_cmd.md`、版本号、镜像或部署文档。

### 聚焦门禁

```bash
conda run -n multi_agent python -m unittest   tests.integrations.agent_skills.test_skill_runtime_state   tests.capabilities.skill_tool.test_executor   tests.api.test_submission_admission_recovery   tests.api.test_submission_admission_runtime_startup   tests.storage.test_agent_storage_sqlite   tests.storage.test_runtime_sidecar_agent_repository   tests.storage.test_agent_repository_contract
```

按实际受影响文件补充Interrupt、Slot、conversation history、message submission、PostgreSQL继承和
Agent orchestration聚焦模块。

### 相关分层门禁

```bash
conda run -n multi_agent python -m compileall -q src tests
conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/skill_tool -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/e2e -p 'test_*.py'
```

另运行变更文件Ruff、关键package import、敏感revision/path输出扫描、`git diff --check`和最终
`git status --short`。任何环境skip或既有失败必须逐项记录，不得宣称通过未运行门禁。

### 授权边界与当前状态

最初自动门禁完成时状态上限为`implemented_automated`。用户随后已授权backend镜像构建/推送和
`docker_cmd.md`标签更新，因此当前状态为`published_pending_deploy`。远端数据库只读统计、旧Task实际
终态化、开发环境hard cut、readiness和真实旧/新Conversation smoke仍需独立授权。

License Requirement：复用现有Python、SQLAlchemy、SHA-256、Agent repository、Submission Admission、
Runtime Sidecar既有`CommitAgentState` wire和unittest；不新增依赖、第三方代码或许可变化。
