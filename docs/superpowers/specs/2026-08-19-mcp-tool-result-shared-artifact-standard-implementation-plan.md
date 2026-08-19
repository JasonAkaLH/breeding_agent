# MCP Tool原始返回复用公共Artifact标准实施计划

## 状态与依据

- 日期：2026-08-19
- 分支：`main`
- 状态：待实施
- 设计依据：`2026-08-19-mcp-tool-result-shared-artifact-standard-design.md`
- 设计信心门：`document-perfectization`三轮复审，98/100，Pass with recorded assumptions
- 范围：只实现成功MCP业务Call的完整原始返回Artifact投影、历史补投、状态可见性和验证；不改Tool执行或业务选择语义

本计划中的“原始返回”只指Adapter/Gateway规范化后、由completed receipt绑定的authoritative durable result
完整字节；不包含HTTP headers、SSE frame、SDK对象、credential、网络诊断或失败Call远端错误体。

## 1. 完成声明

只有同时满足以下条件才可宣称仓库实现完成：

1. 每个具有authoritative durable result的completed `mcp_call_record`恰有1个公共file Artifact；
2. Artifact字节、size和SHA与terminal receipt一致，文件名由Call sequence和安全Tool name确定；
3. ordinary两处terminal commit、approval恢复、remote terminal和reconciler共用同一个projector；
4. failed、cancelled、unknown、无Call或receipt冲突不创建原始返回Artifact；
5. 投影失败不回滚Call/receipt/Task，不发MCP网络请求，并在24小时期限内补偿；
6. pre-ready不claim尚未投影的`retained`源；post-ready严格执行untracked→完整投影扫描→GC；
7. promotion后durable源最终为`deleted`，公共Artifact仍可下载；
8. 多Call闭合状态按`safe_call_ref`隔离，Task刷新/history恢复后仍能显示延迟或永久失败；
9. 完整raw result不进入prompt、event、audit、Node output或v2 envelope；
10. 自动追踪矩阵、前端回归、真实OCR smoke和最终diff审查通过，且`prod`未修改或部署。

## 2. 严格范围与工作树保护

预计允许修改：

- `src/integrations/mcp/durable_result_lifecycle.py`
- `src/integrations/mcp/dispatch_coordinator.py`
- `src/integrations/mcp/audit.py`
- `src/api/runtime.py`
- `src/api/dto.py`
- `src/api/routes/tasks.py`
- `src/core/contracts.py`
- `src/storage/sqlite/repositories.py`
- `src/storage/postgres/repositories.py`
- `frontend/src/api/taskEvents.ts`
- `frontend/src/api/types.ts`
- `frontend/src/domain/taskEvents.ts`
- `frontend/src/components/MCPRuntimeStatus.tsx`
- `frontend/src/App.tsx`
- 对应既有测试、相关`AGENTS.md`、本设计/计划、`docs/AGENTS.md`和`CHANGELOG.md`

明确禁止修改：

- MCP Selector、Tool参数、approval政策、Gateway网络调用、OCR job poll/ack业务语义；
- terminal candidate、receipt、pending payload或v2 resume envelope schema；
- Skill `output_files`采集、ZIP、supersede和manifest信任边界；
- 公共Artifact下载路由、公开`ArtifactResponse`合同或新增MCP专属Artifact组件；
- `prod`配置、镜像、数据库或外部`ocr_mcp`服务。

开始每个Phase前记录`git status --short`。保留现有`.omx/plans/`删除和根`AGENTS.md`修改，不暂存、
不恢复、不覆盖。每个checkpoint只暂存本计划明确列出的本轮文件。

## 3. Phase 0：基线与红测试

### 3.1 基线证据

先运行最小既有基线并保存结果：

```text
tests.integrations.mcp.test_durable_result_lifecycle
tests.storage.test_mcp_dispatch_aggregate_repository
tests.api.test_user_mcp_aggregate_recovery_startup
tests.api.test_user_mcp_runtime_wiring
frontend/src/domain/taskEvents.test.ts
frontend/src/components/MCPRuntimeStatus.test.tsx
frontend/src/App.test.tsx中的Artifact/history相关用例
```

既有失败必须记录测试名和与当前diff无关的证据，不得为清零无关失败扩大范围。

### 3.2 先写失败测试

后端红测试至少覆盖：

- projector拒绝非completed Call、缺失或不匹配receipt、lifecycle和snapshot身份漂移；
- Call sequence/Tool name只能从Storage读取，调用方不能伪造filename/summary；
- 精确重复和并发调用只得到同一Artifact；
- destination已有相同文件时复验后补CAS，不同文件fail closed且不覆盖；
- 目标文件系统空间低于`result_size + low_watermark`返回deferred；
- 三个成功Call生成三个Artifact，失败/取消Call为0；
- pre-ready恢复不再claim due retained删除；
- 两页以上历史候选先全部尝试投影，再执行任何retained GC。

前端/API红测试至少覆盖：

- projection事件精确schema、合法组合和额外键拒绝；
- 相同Call的deferred不能覆盖ready/permanent，多Call状态互不清除；
- Task Summary最多20项、按safe ref稳定排序，非法或超限fail closed；
- ready事件不触发当前`loadArtifacts()`、不提前关闭SSE或完成Task；
- completed/history恢复先恢复闭合提醒，再沿用公共Artifact加载；
- raw Call ID、result ref、storage key、远端错误体不进入event/API/audit。

红测试只证明缺失合同，不提交临时失败checkpoint。

## 4. Phase 1：Storage查询与canonical projector

### 4.1 只读候选查询

在`StoragePort`、SQLite和PostgreSQL增加等价的只读分页查询：

- 只枚举`status=retained AND reason=dispatch_resolved`；
- keyset固定为`(eligible_at, result_ref)`，排序升序；
- `limit`范围`1..1000`；
- 返回lifecycle authority，不接受owner/Task等调用方过滤来替代内部验证；
- SQLite/PostgreSQL测试覆盖空页、边界、同eligible_at稳定翻页、并发状态变化和超过两页。

不得新增表、列、index或新业务authority；若查询计划证明现有index不足，先记录证据并回到设计审查，
不能静默添加migration。

### 4.2 Projector合同

在`durable_result_lifecycle.py`增加：

- frozen闭合结果：`status`、`reason_code`、可空Artifact，不含raw authority；
- `MCPResultArtifactProjector.project_completed_result(result_ref)`；
- Storage读取Call/receipt/lifecycle并做完整身份验证；
- 从Call生成`{sequence:02d}-{safe_tool_name}-result.json`和最多200字符summary；
- 对`LocalArtifactFileStore.root_dir`所在文件系统做目标容量检查；
- 内部调用现有`promote_to_artifact()`完成1 MiB分块复制、逐字节验证、Artifact保存和lifecycle CAS；
- 使用已有audit reference signer生成`safe_call_ref`，通过注入的Event sink写闭合状态；
- Exception分类为deferred或permanent，但绝不吞掉`CancelledError`，也不回滚terminal authority。

事件写失败时，deferred结果由下一轮确定性重试；ready以Artifact API为权威；permanent提示为best-effort，
不得为了补写提示无限延长24小时源保留，需记录低基数本地观测。

现有`promote_to_artifact(filename, summary)`可以保留为低层内部方法和测试入口，但生产调用只能经过projector。
不得把Call/receipt校验复制到Coordinator或runtime。

### Phase 1 Green Gate

运行projector、durable lifecycle和SQLite/PostgreSQL repository定向测试以及`git diff --check`。只有身份、
幂等、容量、文件冲突和源GC测试全部通过后才进入生产接入。

## 5. Phase 2：ordinary、approval与remote生产接入

### 5.1 Coordinator

向`MCPDispatchCoordinator`注入一个窄async projector callable，在两处ordinary completed terminal commit
返回成功后立即调用，随后继续现有Selector/branch/finalizer：

- projection成功或失败都不能重写commit结果；
- callable只接收`result_ref`；
- approval恢复复用ordinary路径，不新增专用分支；
- failed/cancelled/unknown terminal不调用projector；
- 投影期间不持有terminal数据库事务或MCP网络lease。

测试用fake projector锁定两处调用次数、顺序、异常隔离、approval恢复和多Call行为。

### 5.2 Remote terminal

在`src/api/runtime.py::commit_remote_task_result`成功提交completed Call/receipt/lifecycle后调用同一projector，
然后才允许continuation admission：

- projector失败不撤销receipt、不阻断continuation；
- remote Tool或`tasks/get`调用次数不因补投变化；
- result_ref为空或非completed终态不投影；
- restart/retry只读取本地authority，禁止重发网络。

### Checkpoint A

Phase 1～2定向测试通过后创建小范围checkpoint：

```text
feat(mcp): project completed tool results to artifacts
```

只暂存projector、Storage合同/实现、Coordinator/runtime接入、相关测试和必要索引文档。

## 6. Phase 3：post-ready补投、GC与supervision

### 6.1 Manager单轮顺序

把生产单轮入口重构为固定顺序：

1. `repair_incomplete(limit=1000)`直至不足一批；
2. `reconcile_untracked(limit=1000)`直至不足一批；
3. 用keyset完整扫描本轮全部可投影candidate，每页最多1,000并yield；
4. 对每个candidate调用同一projector；
5. 扫描结束后才claim/delete due `retained`和`artifact_owned`，每批最多1,000直至不足一批。

pre-ready aggregate recovery中的原`result_manager.run_once()`改为只执行untracked reconcile，不得claim
retained删除。已有`deleting`修复仍保留，因为该状态已跨过旧版本deletion authority。

### 6.2 60秒后台任务

`ApiRuntime`增加`_mcp_result_artifact_reconciler_task`：

- Ready后立即启动一轮，之后每60秒运行；
- 普通Exception记录低基数结果并等待下一周期；
- done callback把异常保存到独立`_mcp_result_artifact_reconciler_error`并通过既有audit/日志路径告警；
- shutdown cancel并await；
- 多实例不选leader，依靠snapshot hold/no-clobber/CAS/skip-locked收敛；
- 不复用一次性的`_mcp_post_ready_recovery_task`名称或生命周期。

故障注入覆盖：snapshot hold与GC竞争、写文件后崩溃、Artifact保存后CAS崩溃、两实例同result、低空间到期、
deferred事件写失败后的确定性重试、permanent提示失败不延长保留，以及shutdown中断。

### Checkpoint B

startup/post-ready、lifecycle、storage并发测试通过后提交：

```text
feat(mcp): reconcile result artifacts before durable gc
```

## 7. Phase 4：闭合事件、Task Summary与前端状态

### 7.1 后端事件与历史投影

实现`mcp.result_artifact_projection` exact payload：

- `schema=maf.user_mcp.result_artifact_projection.v1`；
- `safe_call_ref`；
- `status`、`reason_code`、`artifact_count`合法组合；
- 确定性event ID基于Artifact ID和status；
- Event envelope承载Task/Node，payload不含raw Call/Server/result/storage/error。

更新MCP audit allowlist，只增加`schema`、`reason_code`、`artifact_count`，复用已有`safe_call_ref`和`status`。
Task Summary增加最多20项的`mcp_result_artifact_projections`，从Task事件按safe ref归并最新合法状态；不新增表
或endpoint。后端对非法/超限历史事件返回空投影并记录闭合观测，不把自由文本异常发送前端。

### 7.2 前端

- 命名SSE列表加入projection事件；
- `TaskEventState.mcp`增加按safe ref的有界状态map；
- exact parser拒绝额外键、非法组合和非字符串safe ref；
- 同Call状态单调收敛，多Call分别保留；
- `MCPRuntimeStatus`复用现有Alert显示deferred/permanent聚合提醒；
- ready只清除对应Call提醒，不调用`loadArtifacts()`；
- Task完成仍由现有Artifact API加载文件；history恢复从Task Summary恢复状态后加载Artifact。

不得新增JSON viewer、MCP Artifact card、轮询器或无限SSE。

### Checkpoint C

API DTO/route、audit、frontend parser/reducer/component/App测试通过后提交：

```text
feat(mcp): surface result artifact projection state
```

## 8. Phase 5：完整验证与真实OCR smoke

### 8.1 自动验证顺序

1. projector/lifecycle/storage定向测试；
2. Coordinator、remote、startup/post-ready定向测试；
3. Task API、audit与泄漏扫描测试；
4. frontend相关Vitest、`npm run typecheck`和`npm run build`；
5. `conda run -n multi_agent python -m compileall -q src tests`；
6. `tests/integrations/mcp`、`tests/storage`、`tests/api`相关完整套件；
7. Skill output Artifact、ZIP、supersede、API下载和现有frontend Artifact回归；
8. `git diff --check`并对照实施前status审查最终diff。

任何未运行或因环境跳过的测试不得写成通过。真实PostgreSQL DSN未配置时，SQLite通过不能替代PG证据。

### 8.2 真实OCR smoke

仅在`main`本地开发环境执行：

1. 使用用户2,326,771-byte PNG创建全新OCR MCP Task；
2. 如出现Tool approval，等待正常批准，不绕过Grant/Interrupt；
3. 等待Task完成并只读核对Task、Node、Call、branch、intent、outbox和receipt；
4. 确认只有1个`start_parse_job`业务Call；
5. Task Artifact API包含`01-start_parse_job-result.json`；
6. 下载字节、size和SHA与durable receipt一致，内容包含完整OCR原文；
7. Main Agent正文仍为有界摘要，event/audit/v2 envelope不含raw result或Base64；
8. 等待或触发合法janitor周期，确认durable源为`deleted`而Artifact仍可下载；
9. 重启后history恢复仍显示Artifact，MCP网络调用计数不增加。

smoke失败时保留安全证据，不自动重放Tool调用，不修改外部OCR服务。

## 9. 文档、索引与最终checkpoint

实施完成后同步：

- 本设计与计划状态、实际测试数量、checkpoint和真实Task证据；
- 受影响目录的`AGENTS.md`入口；
- `docs/AGENTS.md` Future Work；
- `CHANGELOG.md`从“待实施”更新为实际完成与验证范围；
- 未完成的真实PostgreSQL或`prod`发布证据明确保留为缺口。

最终文档checkpoint：

```text
docs(mcp): record result artifact rollout evidence
```

## 10. 回滚

- 回滚前停止新MCP提交并等待当前Call收敛；
- 先停止60秒reconciler和新的projector调用，再回滚writer；
- 保留公共Artifact reader、既有projection事件reader和已生成Artifact；
- 不降级已经`artifact_owned`或`deleted`的lifecycle，不尝试重建durable源；
- 未投影`retained`结果继续原24小时GC；
- 不删除或回滚Task、Node、Call、receipt、intent、outbox、Interrupt或no-replay证据；
- 不使用`git reset --hard`、工作树清理或会影响现有用户改动的操作。
