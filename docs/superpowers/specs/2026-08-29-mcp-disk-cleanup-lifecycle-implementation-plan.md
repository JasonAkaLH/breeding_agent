# MCP 磁盘清理生命周期最小实施计划

依据：`2026-08-29-mcp-disk-cleanup-lifecycle-design.md`
设计基线：`main@bb0ecddb`及其已批准document-perfectization修订
状态：`implemented_automated`；document-perfectization第二轮`100/100 Pass`
目标分支：`main`

## 1. 完成声明与边界

本计划只闭合两个已确认的生产接线缺口：

1. pending action payload在完整durable terminal evidence后best-effort精确删除，遗漏或崩溃残留在24小时后收敛；
2. 现有临时结果orphan janitor从startup单次运行改为startup后立即一轮、此后每小时一轮。

完成必须同时满足：waiting approval、approved、recoverable、没有严格terminal MRTR continuation证据的
input-required及任何证据不完整/冲突的payload不会被误删；即时Call、Remote Task、startup terminal candidate
恢复和unknown no-replay四类完整终态均尝试精确删除；后台cleanup任一分支失败不影响请求、另一清理分支或
下一轮。

严格排除schema/migration、配置项、metric、audit schema、通用janitor框架、durable raw result reconciler、
managed Artifact、Frontend、Rust Sidecar、外部MCP Server、Skill、镜像、部署及`prod`。

每个Checkpoint按“聚焦红测 → 最小生产修改 → 相关回归 → diff审查 → 独立commit”推进；前一Checkpoint
未green不得进入下一Checkpoint。

## 2. Checkpoint A：fail-closed pending payload保护集合

### 2.1 先写红测

修改：

- `tests/storage/test_mcp_dispatch_aggregate_repository.py`
- `tests/core/test_contracts.py`或现有公开Storage contract快照测试（仅在新增方法要求同步时）
- 新增聚焦的`tests/storage/test_mcp_pending_action_cleanup_postgres_integration.py`

红测必须锁定同一truth table：

1. `proposed`、`waiting_approval`、`approved`始终返回为受保护ref；
2. `consumed`且直接Call为`reserved`、`active`、`remote_pending`或其他可恢复非终态时受保护；
3. `consumed`且Call为`completed`、`failed`、`cancelled`，但缺少同一Call的完整receipt时受保护；
4. `consumed`且Call为`unknown`，但缺少同一Call的unknown projection时受保护；
5. action绑定的原Call为`input_required`时，没有严格匹配的terminal MRTR continuation，或continuation仍为
   可恢复非终态、缺receipt/unknown projection时受保护；
6. MRTR continuation通过`continuation_of_call_ref`严格指向原Call，且owner、Task、Node、Server、Tool、参数摘要、
   配置版本、安全版本和input schema全部一致并具有完整终态证据时，才允许对应action退出保护集合；
7. action缺Call、action/Call/continuation/receipt/projection的identity不一致、状态未知时
   继续受保护；
8. 只有`denied`、`invalidated`，以及通过直接Call或严格MRTR链绑定完整ordinary receipt/unknown projection的
   `consumed` action退出保护集合；
9. SQLite与PostgreSQL返回相同的opaque ref集合，不返回参数、路径、owner或正文。

PostgreSQL测试使用模块独立DSN，允许本地未配置时明确skip，但不得用静态源码检查替代真实语义测试。

### 2.2 最小实现

生产修改限定为：

- `src/core/contracts.py`
- `src/storage/sqlite/repositories.py`
- `src/storage/postgres/repositories.py`仅在共享实现无法直接复用时修改

具体步骤：

1. 在现有`MCPDispatchStoragePort`增加一个无参数窄查询，固定返回受保护的`tuple[str, ...]`；
2. 在共享SQLAlchemy repository读取action、直接关联Call、通过`continuation_of_call_ref`关联的MRTR continuation、
   receipt和projection，按“默认保护、明确证据才退出保护”分类；不得以inner join丢弃关联缺失行；
3. ordinary evidence要求直接Call或严格MRTR链与receipt identity完整一致，unknown evidence要求直接Call或严格
   MRTR链与unknown projection identity完整一致；任何未知值或不一致都留在保护集合；
4. 查询保持只读，不锁业务行、不修改状态、不新增索引、表、列或migration；
5. PostgreSQL优先继承共享dialect-neutral实现；只有红测证明需要时才增加窄override。

### 2.3 门禁与提交

```bash
conda run -n multi_agent python -m unittest \
  tests.storage.test_mcp_dispatch_aggregate_repository \
  tests.core.test_contracts
conda run -n multi_agent python -m unittest \
  tests.storage.test_mcp_pending_action_cleanup_postgres_integration
conda run -n multi_agent ruff check \
  src/core/contracts.py \
  src/storage/sqlite/repositories.py \
  src/storage/postgres/repositories.py \
  tests/storage/test_mcp_dispatch_aggregate_repository.py \
  tests/storage/test_mcp_pending_action_cleanup_postgres_integration.py
git diff --check
```

Implementation commit：`fix(storage): protect live MCP pending payloads`

## 3. Checkpoint B：四类终态后的精确删除

### 3.1 先写红测

修改现有测试：

- `tests/integrations/mcp/test_pending_action_payloads.py`
- `tests/integrations/mcp/test_dispatch_coordinator.py`
- `tests/api/test_user_mcp_aggregate_recovery_startup.py`
- `tests/api/test_user_mcp_recovery_startup.py`

红测必须证明：

1. 即时Call和MRTR continuation的`completed`、`failed`、`cancelled` receipt提交后调用一次精确删除；
2. 即时Call、MRTR continuation或startup aggregate收敛为`unknown`且projection完整后调用一次精确删除；
3. Remote Task terminal commit与startup terminal candidate恢复commit后，即使目标Call是通过
   `continuation_of_call_ref`关联action且原始snapshot已不存在，也会先
   按持久化action identity重新`open_validated()`取得当前snapshot，再精确删除；
4. `input_required`、receipt/projection提交前以及action/Call/evidence绑定缺失或冲突时不调用删除；
5. terminal commit成功后发生的open、revalidate、unlink或日志异常不得回滚terminal状态、改变返回结果或重放
   外部调用；文件留给周期清理；
6. 精确删除只使用既有`MCPPendingActionPayloadDeletionEvidence`授权，不增加按路径、年龄或宽松状态删除。

### 3.2 最小实现

生产修改限定为：

- `src/integrations/mcp/pending_action_payloads.py`
- `src/integrations/mcp/dispatch_coordinator.py`
- `src/api/runtime.py`

具体步骤：

1. 把现有pending action→payload identity构造收敛到`pending_action_payloads.py`的单一窄helper，Coordinator与
   Runtime复用；不增加service、registry或新生命周期框架；
2. Coordinator在已持有原始snapshot的即时ordinary terminal和unknown projection成功持久化后，构造严格
   evidence并best-effort调用`delete_with_terminal_evidence()`；MRTR continuation必须先严格验证当前Call到原
   Call及action的既有引用链，不修改原Call的`input_required`历史状态；
3. Runtime为Remote Task terminal commit、startup candidate恢复commit和startup unknown no-replay收敛增加
   同一个private best-effort路径：先尝试当前Call的直接`pending_action_id`；不存在时只允许沿
   `continuation_of_call_ref`加载原Call及其action。验证receipt/projection绑定后重建identity，
   `open_validated()`取得当前snapshot，再调用现有精确删除；
4. startup unknown收敛接口不返回Call ID，因此只枚举当前intent的owner、Task与Node内Call并稳定排序；逐项严格
   验证直接action或MRTR链，只处理存在同Call projection的记录。不得扫描其他Task/Node，不得把同批仅被标记为
   `unknown`但没有projection的Call当作删除证据；
5. 无pending action且无严格MRTR链、identity冲突或文件验证失败时直接保留，不猜测其他关联；
6. cleanup错误只写固定低敏日志，不含路径、payload ref、action ID、Call ID、用户或正文。

### 3.3 门禁与提交

```bash
conda run -n multi_agent python -m unittest \
  tests.integrations.mcp.test_pending_action_payloads \
  tests.integrations.mcp.test_dispatch_coordinator \
  tests.api.test_user_mcp_aggregate_recovery_startup \
  tests.api.test_user_mcp_recovery_startup
conda run -n multi_agent ruff check \
  src/integrations/mcp/pending_action_payloads.py \
  src/integrations/mcp/dispatch_coordinator.py \
  src/api/runtime.py \
  tests/integrations/mcp/test_pending_action_payloads.py \
  tests/integrations/mcp/test_dispatch_coordinator.py \
  tests/api/test_user_mcp_aggregate_recovery_startup.py \
  tests/api/test_user_mcp_recovery_startup.py
git diff --check
```

Implementation commit：`fix(mcp): delete terminal pending payloads`

## 4. Checkpoint C：唯一周期cleanup Task

### 4.1 先写红测

修改：

- `tests/api/test_user_mcp_runtime_wiring.py`
- `tests/api/test_user_mcp_aggregate_recovery_startup.py`
- `tests/integrations/mcp/test_user_mcp_temporary_results.py`仅补当前合同缺失的周期并发回归

红测锁定：

1. startup recovery、MCP authority reconciliation及其他可等待启动步骤全部完成后，只把cleanup Task作为
   `start()`返回前最后一个动作创建；此前任一早期或后段步骤失败时handle保持`None`且不运行清理；
2. Task进入后立即运行首轮，然后才调用可注入sleep seam等待3600秒；
3. 临时结果cleanup每轮使用当轮`active_task_keys()`；active、未满1小时、带manifest目录保持不变，旧无manifest
   orphan删除；
4. pending cleanup每轮先取得保护集合，再以timezone-aware当前时间调用既有24小时`cleanup_orphans()`；保护查询
   失败时不得调用pending文件删除；
5. 临时结果、保护查询和pending文件cleanup分别隔离异常；一项失败不阻止另一项，Task不退出且下一轮继续；
6. shutdown准确cancel并`gather(..., return_exceptions=True)`等待同一Task，随后清空handle；
7. cleanup不触发LLM、MCP网络、外部Server、Artifact reconciler或其他后台任务。

### 4.2 最小实现

生产修改只涉及`src/api/runtime.py`：

1. 增加一个初始化为`None`的`_mcp_disk_cleanup_task`和一个固定默认`asyncio.sleep` seam；
2. 删除现有startup单次临时janitor调用；在其余可等待启动步骤全部成功后，把创建唯一forever loop作为
   `start()`的最后一个动作，确保之后没有可失败的startup操作；loop首轮立即执行，轮末等待3600秒；
3. 每轮按“临时结果cleanup → 保护集合查询及pending cleanup”执行，各段独立捕获异常并记录固定低敏日志；
4. 只有janitor、result store和pending payload store均按各自需要存在时才执行对应分支；不增加环境变量；
5. shutdown沿现有后台Task模式取消、等待并置空handle。

不得修改`MCPTemporaryResultJanitor`或`MCPPendingActionPayloadStore.cleanup_orphans()`的年龄、文件身份、安全校验
和删除算法，除非聚焦红测证明现有合同与批准设计直接冲突；出现这种情况先停在当前Checkpoint重新审查，不顺手
扩展实现。

### 4.3 门禁与提交

```bash
conda run -n multi_agent python -m unittest \
  tests.api.test_user_mcp_runtime_wiring \
  tests.api.test_user_mcp_aggregate_recovery_startup \
  tests.integrations.mcp.test_user_mcp_temporary_results
conda run -n multi_agent ruff check \
  src/api/runtime.py \
  tests/api/test_user_mcp_runtime_wiring.py \
  tests/api/test_user_mcp_aggregate_recovery_startup.py \
  tests/integrations/mcp/test_user_mcp_temporary_results.py
git diff --check
```

Implementation commit：`fix(runtime): schedule MCP disk cleanup`

## 5. Checkpoint D：相关全量门禁与文档闭合

依次执行：

```bash
conda run -n multi_agent python -m compileall -q src tests
conda run -n multi_agent python -m unittest discover -s tests/core -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent ruff check src tests
git diff --check
```

真实PostgreSQL聚焦模块必须在配置独立DSN的环境再跑一次并记录零skip结果。本计划不要求Frontend、Rust、镜像、
部署或真实外部MCP调用，因为生产改动不进入这些边界。

最终检查：

1. diff中的每一行都能追溯到保护查询、终态精确删除、周期Task或其测试；
2. 无表、列、migration、环境变量、metric、audit schema、依赖或许可变化；
3. `docker_cmd.md`仍存在、仍被忽略、未被读取、跟踪或修改；
4. 更新设计/计划状态、`docs/AGENTS.md`和`CHANGELOG.md`，记录自动门禁及PostgreSQL实际执行或明确skip；
5. 不构建、不push、不部署镜像，不修改`prod`。

Final commit：`docs(mcp): close disk cleanup lifecycle fix`

### 5.1 实施证据（2026-08-29）

- Checkpoint A：`293661eb fix(storage): protect live MCP pending payloads`；聚焦Core/SQLite 46项通过，新增真实
  PostgreSQL模块本机因未配置`MAF_POSTGRES_MCP_CLEANUP_TEST_DSN`或`MAF_POSTGRES_TEST_DSN`明确skip 1项；
- Checkpoint B：`b662339e fix(mcp): delete terminal pending payloads`；四类终态、直接Call、严格MRTR continuation、
  startup unknown同Call projection及文件实际删除相关73项通过；
- Checkpoint C：`aff896a1 fix(runtime): schedule MCP disk cleanup`；唯一Task、startup最后创建、立即首轮、3600秒
  周期、异常隔离和shutdown相关58项通过；
- Checkpoint D：`compileall`通过；Core 54项通过；Storage 554项通过、14项环境性skip；Integrations 764项通过、
  2项环境性skip；API 615项通过；`git diff --check`通过；
- 变更面Ruff通过。全仓`ruff check src tests`仍报告32个既存测试告警，均不在本次生产改动中，本轮未扩散清理；
- 未新增schema、migration、配置、metric、依赖或许可变化；未运行Frontend、Rust、镜像、部署、真实外部MCP或
  `prod`。真实PostgreSQL聚焦模块仍须在提供独立DSN的环境执行零skip门禁，因此状态不升级为`complete_local`。

## 6. 回滚

按C→B→A逆序回退独立implementation commits。回滚不删除文件、不修改数据库、不需要migration；回退后只恢复
当前startup单次临时orphan清理和pending payload保留行为。已经由新代码安全删除的终态/orphan文件无需恢复，
durable receipt、projection、Task、Call、Artifact及其他authority不受影响。

License Requirement：复用现有Python、SQLAlchemy、SQLite/PostgreSQL、pending payload加密store、临时结果janitor
和ApiRuntime后台Task模式；无新增依赖或许可变化。
