# MCP Tool原始返回复用公共Artifact标准实施计划

## 状态与依据

- 日期：2026-08-19
- 分支：`main`
- 状态：计划已通过`document-perfectization`并完成Checkpoint A/B、restart修复、自动门禁和本地历史补投；外部OCR/真实PG保留为明确缺口
- 设计依据：`2026-08-19-mcp-tool-result-shared-artifact-standard-design.md`
- 设计信心门：十轮一致性复审，99/100，Pass with recorded assumptions
- 计划信心门：九轮审计/修订，99/100，Pass with recorded assumptions
- 范围：实现成功MCP业务Call的完整原始返回Artifact投影、历史补投、状态可见性和验证；不改变Tool执行或业务选择语义

本计划中的“原始返回”只指Adapter/Gateway规范化后、由completed receipt绑定的authoritative durable result
完整字节；不包含HTTP headers、SSE frame、SDK对象、credential、网络诊断或失败Call远端错误体。

### 实际执行记录（2026-08-19）

| 检查点 | 提交 | 结果 |
|---|---|---|
| Checkpoint A | `fe45624` | projector、closed event/fold、Storage keyset/summary/精确删除CAS完成，未装配writer |
| Checkpoint B | `712d216` | ordinary/approval/remote/reconciler、Task/Message API、SSE reducer、MessageBubble Alert一次性激活 |
| Restart修复 | `07bde65` | startup validator与writer的`completed`无Call、`stopped_after_call`合法终态恢复对称 |
| 文档checkpoint | 本文最终提交 | 记录自动门禁、本地补投、已知失败与外部证据缺口 |

实际验证结果：

- 聚焦后端93项、restart/storage相关70项、前端全量304项、TypeScript production build通过；
- core 46、storage 363（6项环境skip）、lifecycle 25、orchestration 181、main-agent capability 65、
  MCP capability 14、observability 34通过；integrations 640中639通过，唯一失败是既有shadow错误文案断言；
- API全量476项曾出现1个本功能filtered-reader异常传播，修复后其精确回归与本功能套件通过；其余6项Skill/API失败
  和cancel-late E2E失败已在Checkpoint A快照复现；`tests/capabilities/skill_tool`为0测试，`skill/sql-query`目录不存在；
- 本地开发卷50条候选中31条安全补投、31/31文件校验与公共HTTP下载通过；19条缺Call authority保持retained；
- 全新用户PNG OCR因缺少具体外发目的地授权未执行，真实PostgreSQL DSN未验证，`prod`未部署。

## 1. 实施参与者与交付价值

| 角色或系统 | 实施责任与交付 |
|---|---|
| MCP integration / runtime | 在所有completed terminal authority之后调用同一projector，并保证失败不回滚、不重放 |
| Storage（SQLite/PostgreSQL） | 提供等价keyset查询、单行删除CAS和不误删业务结果的bulk GC |
| API / frontend | 复用公共Artifact API和文件卡片，并恢复按Call隔离的闭合投影状态 |
| 测试与运维 | 证明历史补投先于删除、目标盘容量安全、重启幂等、零网络重放和真实OCR完整下载 |
| 最终用户 | 每个成功MCP业务Call得到独立、完整、可下载的JSON Artifact；失败仍只显示安全错误码 |

## 2. 完成声明

只有同时满足以下条件才可宣称`main`仓库实现完成：

1. 每个具有authoritative durable result的completed `mcp_call_record`最终且仅闭合为一种结果：正常路径恰有
   1个公共file Artifact，或在原24小时期限到达后形成闭合permanent failure；用户事件存储失败时至少写入
   不含业务标识的本地观测，任何缺失不得在系统层面静默；
2. Artifact字节、size和SHA与terminal receipt一致，文件名只由Call sequence和可信Tool name派生；
3. ordinary completed terminal commit、approval恢复、remote terminal和reconciler共用同一个projector；
   ordinary failed terminal commit明确不调用；
4. failed、cancelled、unknown、无Call或receipt冲突不创建原始返回Artifact；
5. 投影失败不回滚Call/receipt/Task，不发MCP网络请求，并在原24小时期限内补偿；
6. pre-ready不claim`retained / dispatch_resolved`；到期业务结果仅在当前投影返回permanent后按
   `result_ref + revision`单行CAS删除，bulk GC永久排除该类行；
7. promotion后durable源最终为`deleted`，公共Artifact仍可下载；
8. 多Call闭合状态按`safe_call_ref`隔离，Task刷新/history恢复后仍显示延迟或永久失败；
9. 完整raw result不进入prompt、event、audit、Node output或v2 envelope；
10. 自动追踪矩阵、前端回归、真实OCR smoke和最终diff审查通过，且`prod`未修改或部署。

真实PostgreSQL DSN不属于本地仓库完成的隐含前置：若未提供，必须明确记录“真实PG未验证”，不能用SQLite或
DDL contract测试冒充；未来`prod`发布仍必须补齐真实PG证据，并且不在本计划授权范围内。

## 3. 严格范围与checkpoint规则

预计允许新增或修改：

- `src/integrations/mcp/result_artifact_projection.py`（新增canonical projector、事件合同与状态fold）
- `src/integrations/mcp/durable_result_lifecycle.py`
- `src/integrations/mcp/dispatch_coordinator.py`
- `src/integrations/mcp/audit.py`
- `src/api/runtime.py`
- `src/api/dto.py`
- `src/api/routes/tasks.py`
- `src/api/routes/conversations.py`
- `src/core/contracts.py`
- `src/storage/sqlite/repositories.py`
- `src/storage/postgres/repositories.py`
- `frontend/src/api/taskEvents.ts`
- `frontend/src/api/types.ts`
- `frontend/src/domain/taskEvents.ts`
- `frontend/src/components/MCPRuntimeStatus.tsx`
- `frontend/src/App.tsx`
- 对应既有测试、相关目录`AGENTS.md`、本设计/计划、`docs/AGENTS.md`和`CHANGELOG.md`

明确禁止修改：

- MCP Selector、Tool参数、approval政策、Gateway网络调用、OCR job poll/ack业务语义；
- terminal candidate、receipt、pending payload或v2 resume envelope schema；
- Skill `output_files`采集、ZIP、supersede和manifest信任边界；
- 公共Artifact下载路由、公开`ArtifactResponse`合同或新增MCP专属Artifact组件；
- PostgreSQL/SQLite表、列、约束或index schema；
- `prod`配置、镜像、数据库或外部`ocr_mcp`服务。

开始每个Phase前记录`git status --short`。保留现有`.omx/plans/`删除和根`AGENTS.md`修改，不暂存、
不恢复、不覆盖。

Checkpoint只用于代码审查和回滚，不是可部署里程碑：

- Checkpoint A只包含未装配的纯合同/Storage/projector，不能启动服务验证新功能；
- Checkpoint B必须同时包含安全GC、ordinary/remote装配、Task API和前端闭环；A与B之间禁止重建、重启或部署服务；
- 只有Checkpoint B的完整自动门禁通过后才允许本地服务重启和真实smoke；
- 每个checkpoint只暂存计划内文件，不包含用户原有工作树改动。

## 4. Phase 0：基线、红测试与可执行命令

### 4.1 基线命令

开始实施前运行并保存精确结果：

```bash
conda run -n multi_agent python -m unittest \
  tests.integrations.mcp.test_durable_result_lifecycle \
  tests.storage.test_mcp_dispatch_aggregate_repository \
  tests.api.test_user_mcp_aggregate_recovery_startup \
  tests.api.test_user_mcp_runtime_wiring

(cd frontend && npm test -- --run \
  src/domain/taskEvents.test.ts \
  src/components/MCPRuntimeStatus.test.tsx \
  src/App.test.tsx)
```

既有失败必须记录测试名、错误和与当前diff无关的证据，不得为清零无关失败扩大范围。

### 4.2 后端红测试

先增加失败测试，至少覆盖：

- projector拒绝非completed Call、缺失或不匹配receipt、lifecycle和snapshot身份漂移；
- Call sequence/Tool name只能从Storage读取，调用方不能伪造filename/summary；
- 精确重复和并发调用只得到同一Artifact；
- destination已有相同文件时复验后补CAS，不同文件fail closed且不覆盖；
- 目标Artifact文件系统空间低于`result_size + low_watermark`返回deferred；
- 三个成功Call生成三个Artifact，失败/取消Call为0；
- `reconcile_untracked()`外层只调用一次并依赖其现有内部完整分页；
- 两页以上`updated_at/result_ref`历史候选稳定遍历；
- pre-ready不claim业务结果；bulk GC不选择`dispatch_resolved`；
- 到期失败只允许精确result/revision单行CAS删除，CAS竞争不误删其他状态；
- 并发补齐一个游标之前的已到期lifecycle时，另一实例bulk GC仍无权删除它。

### 4.3 API与前端红测试

- projection事件exact schema、合法组合和额外键拒绝；
- 状态fold只允许`absent → deferred → ready|permanent_failure`；同status不同reason按闭合优先级归并，
  ready与permanent双terminal fork fail closed；
- 多Call状态互不清除，迟到deferred不覆盖终态；
- Task Summary最多20项/120个合法事件、按safe ref稳定排序，非法、fork或第121条事件fail closed；
- ready事件不调用当前`loadArtifacts()`，不提前关闭SSE或完成Task；
- completed/current Task恢复从Task Summary恢复闭合提醒；普通历史assistant从MessageResponse恢复，再沿用公共Artifact加载；
- raw Call ID、result ref、storage key、远端错误体不进入event/API/audit；
- projection事件与观测sink失败不改变Call/Task/Artifact authority。

红测试只证明合同缺失，不提交临时失败状态。

## 5. Phase 1：纯合同、Storage authority与projector（不生产装配）

### 5.1 Storage查询与删除authority

在`StoragePort`、SQLite和PostgreSQL增加等价合同：

1. `list_projectable_mcp_durable_result_lifecycles(...)`
   - 只枚举`status=retained AND reason=dispatch_resolved`；
   - 复用现有`status + updated_at + result_ref`索引，以`(updated_at, result_ref)`升序keyset；
   - cursor两个字段必须同时存在或同时为空；`limit`范围`1..1000`；
   - 返回lifecycle authority，不接受调用方owner/Task过滤代替内部身份校验。
2. `claim_mcp_dispatch_result_deletion(result_ref, expected_revision, now)`
   - 只接受`retained / dispatch_resolved`且`eligible_at <= now`的精确行；
   - revision匹配才转`deleting`，否则返回`None`；
   - 不接受列表、通配符或仅按时间范围claim。
3. 现有`claim_mcp_durable_result_deletions()`
   - 只允许`artifact_owned / artifact_promoted`和`retained / orphan`；
   - 永久排除`retained / dispatch_resolved`。
4. `summarize_mcp_durable_result_backfill(now)`
   - 只返回按closed status/reason汇总的行数、到期业务结果数和总size bytes；
   - 不返回或记录owner、Task、Node、Call/result ref、文件名或路径；
   - SQLite/PostgreSQL必须返回等价聚合结果。

SQLite/PostgreSQL共享合同测试覆盖空页、cursor shape、同`updated_at`稳定翻页、超过两页、状态并发变化、
单行CAS竞争和bulk排除。不得增加schema/index migration。

### 5.2 Canonical事件和状态fold

新增`result_artifact_projection.py`，集中定义：

- `MCPResultArtifactProjectionStatus`和`ReasonCode`闭合枚举；
- exact payload builder/parser；
- 合法status/reason/artifact_count组合；
- 每个safe Call的canonical fold：`absent → deferred → ready|permanent_failure`；
- 同status合法reason采用固定优先级：ready为`promoted > already_promoted`，deferred为
  `projection_failed > capacity_unavailable`，permanent为`source_expired > projection_failed`；
- 只有同Call同时出现ready和permanent才是`authority_conflict`；不得按created_at/event ID猜测；
- Task Summary fold：最多20个safe Call、120个合法status/reason事件，按safe ref排序；任一非法/fork/超限时
  整段安全投影为空并返回闭合诊断码；
- 确定性event ID由Artifact ID、status和reason_code派生。
- EventRecord时间必须来自稳定authority：ready=`Artifact.created_at`，deferred=`Call.terminal_at`并以
  lifecycle.created_at兜底，retained到期permanent=`lifecycle.eligible_at`，已deleted的source_expired=
  `lifecycle.deleted_at`；同ID重试不得使用当前clock。

后端Task Summary和前端必须通过等价测试向量证明同一fold结果；不得各自发明“latest event”规则。

### 5.3 Projector合同

实现`MCPResultArtifactProjector.project_completed_result(result_ref, source)`：

- `source`只允许`immediate | reconciler`，用于低基数观测；
- 从lifecycle读取owner/Task/Node/Call，再读取authoritative Call和matching receipt；
- 从Call生成`sanitize_download_filename(f"{call_sequence:02d}-{tool_name}-result.json")`及最多200字符summary；
- runtime从现有`user_mcp_capacity_values[1]`注入`artifact_disk_low_watermark_bytes`；
- projector注入`free_bytes(path)`测试hook，并且只检查`LocalArtifactFileStore.root_dir`所在目标文件系统；
- 内部调用现有`promote_to_artifact()`完成1 MiB worker-thread复制、逐字节验证、Artifact保存和lifecycle CAS；
- 注入已有audit reference signer生成`safe_call_ref`，调用方看不到raw Call authority；
- 注入frontend event sink和低基数projection observer；observer只接收status、reason、source和elapsed_ms，
  不接收owner、Task、Server、Tool、Call/result ref或raw内容；
- 不吞`asyncio.CancelledError`；其他Exception必须收敛为闭合结果，不得逃逸到terminal业务路径。

闭合分类必须同时使用结果和当前lifecycle时间：

| 条件 | 返回 |
|---|---|
| Artifact已验证存在或本次promotion成功 | `ready/promoted|already_promoted/1` |
| 任意非成功且`now < eligible_at` | `deferred/capacity_unavailable|projection_failed/0` |
| 非成功且`now >= eligible_at`，源仍存在但无法投影 | `permanent_failure/projection_failed/0` |
| lifecycle/source已被另一authority删除且无Artifact | `permanent_failure/source_expired/0` |

ready/permanent是终态，不能再转换为另一终态。事件写失败时，deferred由下一轮确定性重试；ready以Artifact API
为权威；permanent提示为best-effort，不得为了补写提示无限延长24小时源保留。

单元测试必须证明相同status/reason重试生成逐字段相同的EventRecord，并分别覆盖SQLite merge和Sidecar
idempotency路径；不同reason生成不同event ID。

现有`promote_to_artifact(filename, summary)`保留为低层内部方法和测试入口，但生产调用只能经过projector。

### Checkpoint A

Phase 1全部定向测试、SQLite/PG contract测试及`git diff --check`通过后提交：

```text
feat(mcp): define result artifact projection authority
```

该checkpoint没有runtime/Coordinator装配，不得重启或部署来宣称功能可用。

## 6. Phase 2：安全reconciler、GC与历史API（仍不启用生产writer）

### 6.1 Lifecycle单轮顺序

在`MCPDurableResultLifecycleManager`增加canonical单轮入口：

1. `repair_incomplete(limit=1000)`循环至不足一批；
2. 调用一次`reconcile_untracked(limit=1000)`；该现有方法内部已经分页到文件枚举结束；
3. 使用`(updated_at, result_ref)`keyset扫描全部projectable候选，每页最多1,000并yield；
4. 对每个候选调用projector：ready转artifact-owned；deferred保留；到期permanent后立即调用精确单行claim；
5. 精确claim成功才调用现有安全文件删除；CAS丢失则重读，artifact-owned/deleted视为他人收敛，其他状态留到下一轮；
6. 扫描后bulk清理artifact-owned和orphan，每批1,000；bulk API不可能返回dispatch-resolved。

文件digest/identity异常时仍沿用现有fail-closed删除行为：不得静默unlink未知文件；lifecycle保持可诊断中断态并告警，
该安全异常可以超过普通24小时清理目标，不能为满足期限而绕过身份校验。

### 6.2 Pre-ready与60秒任务实现

- pre-ready的`_repair_mcp_terminal_candidate_lifecycle()`继续修复已有`deleting`；
- terminal candidate恢复后只做untracked reconcile，不调用旧bulk `run_once()`；
- 实现独立`_run_mcp_result_artifact_reconciler_forever()`，立即一轮后每60秒运行；
- 同一实例只允许一个单轮执行；60秒从上一轮结束后计算，不允许周期重叠或积压并发scan；
- 普通Exception记录闭合观测并在60秒后重试；done callback保存
  `_mcp_result_artifact_reconciler_error`，复用现有post-ready error模式，不改变Ready authority；
- shutdown cancel并await；不复用一次性的`_mcp_post_ready_recovery_task`；
- 本Phase只实现和测试方法，暂不在`ApiRuntime.start()`启动，也不向Coordinator/remote注入projector。

### 6.3 Task与Message历史投影

- 新增`extra='forbid'`的typed Pydantic projection response model，字段使用闭合Literal/整数约束；
- `TaskSummaryResponse`增加可选、`max_length=20`的`mcp_result_artifact_projections`列表，不使用无约束dict；
- `_build_task_summary()`通过现有filtered Task event reader只取projection事件，固定`limit=121`；返回121条
  即判超限，避免无界读取损坏历史；
- 使用Phase 1 canonical fold，最多20个Call并稳定排序；
- owner鉴权继续由现有Task route先完成；
- 旧Task、无事件、非法/fork/超限均返回空可选字段，不暴露内部诊断；
- DTO和route不新增endpoint，不改变公共Artifact合同。

同一typed字段还要加入assistant `MessageResponse`：

- `list_conversation_messages`在现有conversation owner鉴权后，收集completed assistant的有界Task ID集合；
- 对唯一Task ID以最大并发8调用同一canonical helper派生投影，与现有`artifacts_by_task_id`一起装配历史响应，
  不允许为每条重复message重复查询；
- 不把状态复制到持久Message metadata，不增加表或Message writer副作用；
- 非assistant、无Task、未完成message返回缺省空字段；
- API测试覆盖任意非current历史Task、跨用户404、多个assistant Task和非法事件fail closed。

故障注入覆盖snapshot hold与GC竞争、写文件后崩溃、Artifact保存后CAS崩溃、两实例同result、游标后插入到期行、
低空间到期、deferred事件确定性重试、permanent提示失败不延长保留，以及shutdown中断。

## 7. Phase 3：ordinary、approval、remote与runtime一次性激活

本Phase必须在同一工作树中完成所有生产装配，不允许中途重启服务。

### 7.1 Coordinator

向`MCPDispatchCoordinator`注入窄async projector callable，在ordinary completed terminal commit成功后、
继续Selector/branch/finalizer之前调用：

- callable只接收`result_ref`并固定`source=immediate`；
- projection结果不改写commit结果；异常已由projector闭合；
- approval恢复复用ordinary路径，不新增专用分支；
- failed/cancelled/unknown terminal不调用；
- 同文件另一处failed `commit_mcp_call_terminal()`只能保留失败authority与零projector调用，不是第二个正向接入点；
- 投影期间不持有terminal数据库事务或MCP网络lease。

### 7.2 Remote terminal

`commit_remote_task_result`在completed Call/receipt/lifecycle提交成功后调用同一projector，再允许continuation：

- projector失败不撤销receipt、不阻断continuation；
- result_ref为空或非completed终态不投影；
- remote Tool或`tasks/get`调用计数不因投影变化；
- restart/retry只读取本地authority，网络调用增量为0。

### 7.3 Runtime装配与观测

在同一Phase完成：

- 构造projector并注入Storage、artifact store、snapshot authority、signer、event sink、observer、clock、
  `user_mcp_capacity_values[1]`和目标盘`free_bytes`；
- 向Coordinator与remote committer注入同一实例；
- `ApiRuntime.start()`在aggregate pre-ready全部通过后、返回前只`create_task`启动reconciler，不await首轮扫描；
  因此首轮属于post-ready后台工作，不扩大启动阻塞门；
- shutdown和done callback装配；
- projection frontend event为`EventVisibility.FRONTEND`，MCP audit继续只保留闭合字段；
- status/reason计数从持久projection audit事件聚合；observer记录`projection_latency_ms`和
  `immediate|reconciler`来源到既有安全观测sink，不新增rollout metric schema或高基数label；
- 观测sink失败不改变Artifact或Task authority，但必须写固定、无业务ID的应用日志码
  `mcp_result_artifact_projection_observation_failed`，避免permanent缺失在系统层面完全静默。

后端Green Gate必须覆盖ordinary completed正向、ordinary failed零调用、approval、remote、startup、周期、
shutdown、事件/audit泄漏和网络调用计数。

## 8. Phase 4：前端闭环与Checkpoint B

### 8.1 前端实现

- 命名SSE列表加入`mcp.result_artifact_projection`；
- `TaskEventState.mcp`增加最多20项、按safe ref索引的状态map；
- `frontend/src/api/types.ts`使用闭合union定义Task Summary projection，不使用`Record<string, unknown>`；
- `MessageResponse`和前端`ConversationMessage`增加同一typed projection字段，`AssistantMessagePatch`允许安全更新；
- exact parser拒绝额外键、非法组合和空/非字符串safe ref；
- 实现与后端共享测试向量等价的canonical fold；ready/permanent双terminal fork不显示猜测状态；
- `MCPRuntimeStatus`复用现有Alert显示deferred/permanent聚合提醒；
- ready只清除对应Call提醒，不调用`loadArtifacts()`；
- projection事件加入frontend exact validator和known event集合；同一事件在Task进入terminal后若仍在已打开SSE中
  到达，继续按闭合状态消费，但不得改变Task phase；SSE关闭后的更新由current Task Summary或普通历史
  MessageResponse恢复；
- Task完成仍由现有Artifact API加载文件；current Task恢复使用Task Summary，普通历史消息使用MessageResponse；
- `loadArtifacts()`清空`currentTaskId`前，把当前fold结果复制到对应assistant message；运行中只显示
  `MCPRuntimeStatus`，完成后由MessageBubble中的现有Ant Design Alert持续显示，避免重复提醒；
- `messageFromHistory()`从assistant MessageResponse恢复同一字段，所以非current历史Task也可见；
- 扩展现有`mergeHistoryWithLiveFallbackNotices()`（可重命名为通用notice merge），在history响应缺失或暂时落后时
  按taskId保留live projection notice；后端返回更新状态时以canonical fold合并，不用旧live状态覆盖新终态；
- Alert使用现有可访问语义并覆盖`role`/可读文本测试，不增加只有颜色才能辨认的状态；
- 不新增JSON viewer、MCP Artifact card、轮询器或无限SSE。

### 8.2 前后端完整checkpoint

执行后端Phase 1～4定向套件、frontend三份定向测试、typecheck、build和`git diff --check`后提交：

```text
feat(mcp): deliver complete tool results as artifacts
```

Checkpoint B必须同时包含安全GC、runtime激活、API和frontend；只有此checkpoint完整通过后才允许本地服务重启。

## 9. 需求追踪矩阵

| 设计需求 | 实施Phase | 必须证据 |
|---|---|---|
| FR-01, FR-02, NFR-01, NFR-02 | Phase 1 | projector身份、文件名、字节/SHA、幂等、正常ready与到期permanent二选一测试 |
| FR-03, FR-04 | Phase 3 | ordinary completed正向、failed零调用、approval、remote相同实例与失败隔离测试 |
| FR-05, FR-09, NFR-08, NFR-09 | Phase 1～3 | 单行删除CAS、bulk排除、游标并发、snapshot hold、周期/shutdown故障注入 |
| FR-06, G-06, NFR-04, NFR-06 | Phase 1、3、4 | exact event/fold、audit/API/prompt/Node/v2泄漏扫描 |
| FR-07, NFR-05 | Phase 2、4 | Task Summary当前恢复、MessageResponse任意历史assistant、owner隔离、公共Artifact API/下载与MessageBubble提醒 |
| FR-08 | Phase 0、1、3 | failed/cancelled/unknown/no-call零Artifact测试 |
| FR-10, NFR-03 | Phase 2、3、5 | backfill/restart前后MCP网络计数为0 |
| NFR-07, NFR-10 | Phase 1、2 | 64 MiB、1 MiB分块、目标盘low-watermark、到期精确删除测试 |
| 兼容性 | Phase 5 | Skill ZIP/supersede、下载、现有MCP approval/recovery与frontend回归 |

## 10. Phase 5：完整验证、发布前计数与真实OCR smoke

### 10.1 自动验证顺序

1. Phase 1～4新增定向测试；
2. `conda run -n multi_agent python -m unittest discover -s tests/integrations/mcp -p 'test_*.py'`；
3. `conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_*.py'`；
4. 相关API模块后，再运行`conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'`；
5. `conda run -n multi_agent python -m compileall -q src tests`；
6. `(cd frontend && npm test -- --run)`；
7. `(cd frontend && npm run typecheck)`；
8. `(cd frontend && npm run build)`；
9. Skill output Artifact、ZIP、supersede、API下载和现有frontend Artifact回归；
10. `git diff --check`并对照实施前status审查最终diff。

任何未运行或环境skip不得写成通过。真实PG DSN可用时追加对应集成测试；不可用时记录精确缺口。

### 10.2 历史补投预检

本地服务重启前只读统计并记录低基数计数，不输出result/Call/Task/owner ID：

- `retained / dispatch_resolved`总数；
- 已到`eligible_at`数量；
- `artifact_owned`、`orphan`和`deleting`数量；
- 预计总字节数的安全聚合值。

统计只用于估算首轮时长和核对补投结果，不改变路由、生命周期或Ready门禁。实际历史量和多Call分布仍是
上线前才能取得的有界假设。

### 10.3 真实OCR smoke

仅在Checkpoint B完整验证后，于`main`本地开发环境执行：

1. 使用用户2,326,771-byte PNG创建全新OCR MCP Task；
2. 如出现Tool approval，等待正常批准，不绕过Grant/Interrupt；
3. 等待Task完成并只读核对Task、Node、Call、branch、intent、outbox和receipt；
4. 确认只有1个`start_parse_job`业务Call；
5. Task Artifact API包含`01-start_parse_job-result.json`；
6. 下载字节、size和SHA与durable receipt一致，内容包含完整OCR原文；
7. Main Agent正文仍为有界摘要，event/audit/v2 envelope不含raw result或Base64；
8. 等待合法reconciler周期，确认durable源为`deleted`而Artifact仍可下载；
9. 重启后history恢复仍显示Artifact，MCP网络调用数不增加；
10. 对比预检与补投后安全计数，解释所有未投影/永久失败类别。

smoke失败时保留安全证据，不自动重放Tool调用，不修改外部OCR服务。

## 11. 文档、最终checkpoint与回滚

实施完成后同步设计/计划状态、实际测试数量、checkpoint、真实Task证据、相关目录`AGENTS.md`、
`docs/AGENTS.md`和`CHANGELOG.md`。未完成的真实PG或`prod`证据必须保留为缺口。

最终文档checkpoint：

```text
docs(mcp): record result artifact rollout evidence
```

回滚顺序：

1. 停止本地新MCP提交并等待当前Call收敛；
2. 禁止创建新业务result后，保留projector和安全reconciler运行，直到
   `retained / dispatch_resolved = 0`且`deleting = 0`；期间仍按精确CAS/Artifact规则收敛；
3. 若身份/digest损坏导致无法安全删除或promotion，回滚标记Blocked并要求operator处理；不得为清零而恢复旧bulk路径；
4. drain证据通过后，停止60秒reconciler和immediate projector，再回滚writer/UI；
5. bulk排除`dispatch_resolved`和pre-ready禁删保护必须保留到drain完成所在版本，不能提前回滚；
6. 保留公共Artifact reader、projection事件reader和已生成Artifact；
7. 不降级已经`artifact_owned`或`deleted`的lifecycle，不尝试重建durable源；
8. 不删除或回滚Task、Node、Call、receipt、intent、outbox、Interrupt或no-replay证据；
9. 不使用`git reset --hard`、工作树清理或会影响用户现有改动的操作。

## 12. document-perfectization复审记录

### 第1轮：初稿审计

- Blocking：0。
- Major 1：Checkpoint A在安全GC和事件合同完成前启用ordinary/remote projector，阶段性重启会形成错误半成品。
- Major 2：generic bulk deletion仍可在多实例/并发untracked场景删除未获投影机会的业务结果。
- Major 3：事件/history按“最新时间”归并，没有canonical单调fold和双terminal冲突处理。
- Major 4：误把内部已完整分页的`reconcile_untracked()`当单页循环，并为keyset选择了无现成索引的`eligible_at`。
- Major 5：设计要求的状态/延迟观测、可执行测试命令、真实PG证据边界和需求追踪没有形成实施门禁。
- 结论：不通过；用户已预先授权本轮技术性修订。

### 第2轮：修订后完整评分

| 类别 | 得分 | 扣分 |
|---|---:|---|
| 目标、范围、用户与干系人价值 | 15/15 | 无 |
| 功能需求 | 20/20 | 无 |
| 非功能需求 | 10/10 | 无 |
| 验收标准与可测试性 | 15/15 | 无 |
| 边界情况与失败模式 | 10/10 | 无 |
| 依赖与实施可行性 | 10/10 | 无；已锁定现有index、low-watermark注入和不改schema边界 |
| 测试、发布、迁移与回滚 | 10/10 | 无 |
| 风险、假设、追踪与一致性 | 9/10 | Minor：真实历史候选量和多Call分布只能在实施前预检取得；影响首轮耗时估算，不影响删除authority或正确性；按Phase 5安全计数、分页与结果分类跟进 |
| **总分** | **99/100** | **1个有界Minor** |

- Blocking：0；Major：0；Minor：1。
- 复审新增1个Major：原回滚顺序先停止reconciler，却又保留bulk排除，会导致未投影业务结果永久滞留；
  若恢复旧bulk则重新引入数据丢失风险。
- 结论：未通过，继续修订。

### 第3轮：回滚修订后复审

- 回滚改为drain-first：停止新提交后保留安全projector/reconciler，直到业务retained和deleting均为0；
  身份损坏无法收敛时明确Blocked，禁止恢复旧bulk绕过。
- 复审新增1个Major：完成声明绝对要求每个completed Call必有Artifact，却又允许低容量或损坏authority在
  24小时后permanent failure，两个验收真值互相矛盾。
- 结论：未通过，继续修订。

### 第4轮：完成语义修订后复审

- 完成声明和FR-01统一为闭合二选一：正常容量/完整authority必须ready并有唯一Artifact；只有达到原期限仍
  无法安全投影才允许permanent failure，任何缺失不得静默。
- 复审新增1个Major：事件ID只绑定Artifact ID+status，同status的reason变化会用相同ID写不同payload，
  造成持久化identity conflict。
- 结论：未通过，继续修订。

### 第5轮：事件ID修订后复审

- event ID增加reason_code；同status原因使用固定优先级归并，只有ready/permanent并存才是fork；Task Summary
  以121条有界filtered query检测超过20 Call/120合法事件的损坏历史。
- 复审新增1个Major：相同event ID重试若使用当前clock，会在SQLite merge改写created_at，并可能与Sidecar
  idempotency payload冲突。
- 结论：未通过，继续修订。

### 第6轮：完整事件幂等修订后复审

- created_at改由稳定authority重建：ready取Artifact时间、deferred取Call terminal/lifecycle创建时间、
  retained到期permanent取eligible_at、deleted source_expired取deleted_at；相同ID重试必须逐字段相同。
- 复审发现1个Major：原规则仍把所有permanent时间写成eligible_at，但现有deleted row会清空该字段，
  source_expired无法重建稳定EventRecord。
- 结论：未通过，继续修订。

### 第7轮：deleted时间authority修订后复审

- retained到期permanent继续使用eligible_at，已deleted且无Artifact的source_expired改用deleted_at；全部事件
  状态现在都有可重建的稳定时间authority。
- 复审发现1个Major：计划把Coordinator两处terminal commit都描述成completed正向接入，但源码中一处是
  failed commit，只能作为零projector调用的负向门禁。
- 结论：未通过，继续修订。

### 第8轮：接入点修订后复审

- ordinary正向接入收敛为唯一completed commit；failed commit明确零调用；approval复用completed分支，remote
  保持独立相同projector入口。
- 复审发现1个Major：`MCPRuntimeStatus`只在currentTaskId存在时渲染，而Task完成会清空该ID；普通历史对话也
  不保证请求Task Summary，因此原history提醒会消失。
- 结论：未通过，继续修订。

### 第9轮：历史消息可见性修订后最终评分

- Task Summary保留current Task恢复；assistant MessageResponse增加同一只读闭合投影，覆盖任意历史Task；
  terminal前状态复制到前端assistant message，完成后由MessageBubble Alert持续显示。
- 复核Phase顺序、Storage deletion authority、事件fold/时间、测试命令、observability、回滚和真实PG边界后，
  无新增Blocking或Major。

完整重评分：

| 类别 | 得分 | 扣分 |
|---|---:|---|
| 目标、范围、用户与干系人价值 | 15/15 | 无 |
| 功能需求 | 20/20 | 无 |
| 非功能需求 | 10/10 | 无 |
| 验收标准与可测试性 | 15/15 | 无 |
| 边界情况与失败模式 | 10/10 | 无 |
| 依赖与实施可行性 | 10/10 | 无；已锁定现有index、low-watermark注入和不改schema边界 |
| 测试、发布、迁移与回滚 | 10/10 | 无；drain-first回滚已闭合 |
| 风险、假设、追踪与一致性 | 9/10 | Minor：真实历史候选量和多Call分布只能在实施前预检取得；影响首轮耗时估算，不影响删除authority或正确性；按Phase 5安全计数、分页、单飞周期和结果分类跟进 |
| **总分** | **99/100** | **1个有界Minor** |

- Blocking：0；Major：0；Minor：1。

- 最终结论：**Pass with recorded assumptions**，通过95分信心门。
