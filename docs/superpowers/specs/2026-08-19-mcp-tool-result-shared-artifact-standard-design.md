# MCP Tool原始返回复用公共Artifact标准设计

## 状态

- 日期：2026-08-19
- 分支：`main`
- 状态：设计已确认并通过document-perfectization复审；`main`仓库实现、本地开发卷补投和公共下载验收已完成
- 用户决策：MCP产物标准直接复用Skill最终进入的公共Artifact标准；成功Call展示完整原始返回，失败或取消Call只展示脱敏错误码
- 范围：用户级`mcp.dispatch`成功Tool Call的durable result投影、补偿与公共Artifact展示
- 信心门：99/100，无Blocking或Major问题

## 实施结果（2026-08-19）

- Checkpoint A：`fe45624 feat(mcp): define result artifact projection authority`；
- Checkpoint B：`712d216 feat(mcp): publish complete tool results as artifacts`；
- restart authority对称性修复：`07bde65 fix(mcp): accept valid resolved restart states`；
- 自动门禁：前端全量304项和production build通过；本功能聚焦后端93项、restart/storage相关70项通过；
- 本地开发卷预检为50条`retained / dispatch_resolved`、0条到期、310,962 bytes；首轮补投后31条生成
  公共Artifact并由`artifact_owned → deleting → deleted`删除源，31/31文件的size和SHA与lifecycle/receipt一致，
  事件全部为`ready/promoted`且raw authority泄漏扫描为0；
- 其余19条均缺少Call authority但仍有receipt，不满足本设计的完整identity条件，保持retained且不猜测恢复；
- 公共Task Artifact列表与download均返回200，下载字节和SHA精确匹配；backend、frontend与runtime-sidecar健康；
- 全量后端仍有实施前已存在的1项shadow错误文案、6项Skill/API和1项cancel-late E2E失败；已用Checkpoint A
  源码快照复现，不计为本设计通过；`skill/sql-query`本地兼容目录不存在，真实PostgreSQL未运行；
- 全新2,326,771-byte用户PNG OCR需要把具体文件发送到具体外部MCP目的地；当前没有这项新增外发授权，因此未执行。
  `prod`未部署。

## 用户、干系人与受影响系统

| 角色或系统 | 需要与影响 |
|---|---|
| 最终用户 | 每个成功MCP业务Call都能通过现有文件卡片下载完整、未截断的原始返回；失败时只看到安全错误码 |
| Main Agent | 仍只消费最多20,000字符的execution-only有界projection，不读取Artifact完整内容 |
| MCP Coordinator / recovery worker | terminal commit之后调用同一个投影入口；投影失败不得改变业务终态或重放网络调用 |
| Storage / durable lifecycle | 保证authoritative result、Artifact copy与源文件GC之间的身份校验、幂等和竞态安全 |
| API / frontend | 复用公共Artifact API、下载鉴权和文件卡片；扩展闭合事件reducer、Task/Message历史投影和既有Alert提醒 |
| 运维与发布 | 先补投仍可验证的历史结果，再做全新OCR smoke；不扩大Ready阻塞面，不授权`prod`部署 |

## 问题证据

最新成功OCR Task `task-9517dd6d620c`证明执行结果和用户产物当前脱节：

- `start_parse_job`只有1个业务Call并以`completed`终态提交；
- terminal receipt绑定一个50,114-byte `durable_content_addressed`原始结果；
- `mcp_durable_result_lifecycle`保持`retained / dispatch_resolved`，计划24小时后进入GC；
- `mcp.dispatch` Node的`output_refs`为空，Task没有由MCP Node生产的Artifact；
- 用户只看到Main Agent生成的554字符中间文本Artifact，无法下载完整Tool原始返回；
- 后续询问“你把原文的全文本给我”时，模型仍只能基于有界projection生成截断文字，不能读取完整durable result。

仓库已经存在`MCPDurableResultLifecycleManager.promote_to_artifact()`，它能校验durable snapshot，
复制到公共`LocalArtifactFileStore`，创建`ArtifactType.FILE`并把lifecycle切换为
`artifact_owned / artifact_promoted`。现有测试同时证明`artifact_owned`只是“Artifact副本已接管”的可删除源状态：
durable janitor随后删除原始data/manifest并把lifecycle推进到`deleted`，公共Artifact文件继续存在。
当前缺口不是Artifact基础设施，而是成功Call提交后没有生产调用方，且回填、GC和前端状态尚未形成闭环。

## 核心原则

```text
Skill output_files ─> Skill可信采集 ─┐
                                   ├─> 公共Artifact标准/API/下载/前端卡片
MCP durable result ─> MCP可信投影 ─┘
```

“复用Skill产物标准”指复用最终公开合同和交付链，不指把远端MCP响应伪装成Skill sandbox文件。

本文“原始返回”精确定义为：协议Adapter/Gateway完成安全规范化后、与completed terminal receipt绑定并写入
authoritative durable result的完整字节。它不是HTTP headers、SSE frame、SDK内部对象、credential、
网络诊断或失败Call的远端错误体；投影不得绕过现有Adapter直接捕获transport wire。

## 目标与成功指标

| ID | 目标 | 可验证成功指标 |
|---|---|---|
| G-01 | 成功Call产出完整原始返回 | 正常容量与完整authority下，每个completed业务Call恰有1个匹配receipt的可下载file Artifact；无法在原24小时内安全投影的Call必须闭合为permanent failure，并至少产生安全用户事件或低基数本地观测，禁止系统静默缺失 |
| G-02 | 复用公共Artifact标准 | MCP与Skill使用相同ArtifactResponse、Task Artifact API、下载路由和前端文件卡片 |
| G-03 | 覆盖全部提交路径 | 普通调用、approval恢复、remote Task和startup/post-ready recovery调用同一projector合同 |
| G-04 | 失败隔离与no-replay | 投影失败不回滚Call/receipt/Task、不触发MCP网络调用，24小时有效期内可幂等补投 |
| G-05 | 回填当前可恢复历史 | 部署时对仍有源文件、completed Call和matching receipt的`retained / dispatch_resolved`结果执行补投 |
| G-06 | 控制面零原文泄漏 | 完整raw result不进入prompt、Node output、event、audit或v2 resume envelope |

## 非目标

- 不把失败或取消Call的远端原始错误体公开给用户。
- 不在对话正文内联完整JSON，不新增JSON查看器或MCP专属Artifact组件。
- 不改变Selector、Tool参数、approval、Gateway调用、terminal candidate、receipt或no-replay语义。
- 不把多个Call合并为ZIP，也不使用Skill“同会话新产物覆盖旧产物”的规则。
- 不恢复已经被24小时GC删除、缺少authoritative receipt或身份不完整的历史结果。
- 不把Artifact或完整原始返回反向注入Main Agent；现有最多20,000字符的execution-only有界projection保持不变。
- 不保证因磁盘容量不足且超过既有24小时durable保留期的源结果可恢复；此时保持现有有界存储策略并给出安全告警。

## 功能需求

| ID | 需求 |
|---|---|
| FR-01 | 每个具有authoritative durable result的completed业务Call最终必须且只能闭合为：一个独立`ArtifactType.FILE`，或到期后的安全permanent failure；正常容量/完整authority路径只允许前者，permanent用户事件存储失败时至少必须有本地安全观测 |
| FR-02 | Artifact ID、文件名、Task、Node、Tool、size和SHA只能从已提交authority派生，调用方不得传入可伪造展示元数据 |
| FR-03 | 普通、approval、remote与reconciler使用同一个`MCPResultArtifactProjector.project_completed_result(result_ref)`入口 |
| FR-04 | projector在terminal commit之后、branch continuation或Task terminal事件之前尽力执行；失败不得改变已提交业务终态 |
| FR-05 | pre-ready阶段不得删除尚未尝试投影的`retained / dispatch_resolved`结果；post-ready对每个到期业务结果只有在本轮投影返回permanent后才可按result_ref+revision精确claim删除，bulk GC不得选择该类结果 |
| FR-06 | projection事件只能携带闭合状态和原因码；不得携带raw result、result ref、Server/Call authority或远端错误体 |
| FR-07 | API和前端复用公共file Artifact链；即时或恢复加载时都能显示已有Artifact |
| FR-08 | failed、cancelled、unknown、无Call或无matching receipt的结果不得创建原始返回Artifact |
| FR-09 | promotion成功后的durable源文件按现有`artifact_owned → deleting → deleted`生命周期回收，Artifact副本独立保留 |
| FR-10 | 历史补投只读本地SQL与文件，不重放或补发任何MCP网络请求 |

## 公共Artifact合同

每个成功Call生成一个`ArtifactType.FILE`：

| 字段 | 合同 |
|---|---|
| `artifact_id` | 继续由`mcp_durable_result_artifact_id(result_ref)`确定性生成 |
| `task_id` | durable lifecycle中的Task |
| `producer_node_id` | durable lifecycle中的MCP Node |
| `filename` | `{call_sequence:02d}-{sanitized_tool_name}-result.json` |
| `mime_type` | `application/json` |
| `size_bytes` | durable snapshot精确字节数 |
| `sha256` | durable snapshot内容SHA-256 |
| `summary` | `MCP Tool原始返回：{tool_name}`，最多200字符 |
| `download_url` | 复用`/api/v1/artifacts/{artifact_id}/download` |
| `source_kind` | private storage metadata中的`mcp_result` |
| `retention_status` | `active` |

公开`ArtifactResponse.storage_ref`继续为空。`storage_key`、`result_ref`、owner、Server ID、endpoint、
credential、Call authority和内部文件路径不得进入API或前端。

原始字节按durable result原样复制，不重新序列化、不截断、不改变字段顺序。文件名只使用数据库中已经
提交的Call sequence和经过公共文件名sanitizer处理的Tool name。

## 可信采集边界

### Skill

`SkillOutputArtifactManager`继续负责：

- 读取受控sandbox `outputs_dir`；
- 按Skill manifest校验声明的`output_files`；
- 多文件时打包；
- 使用Skill专属覆盖/清理语义。

### MCP

MCP不得调用`SkillOutputArtifactManager.process_script_output()`，因为MCP没有Skill manifest、
`outputs_dir`或本地脚本信任边界。新增窄入口`MCPResultArtifactProjector`，内部委托现有
`MCPDurableResultLifecycleManager.promote_to_artifact()`进入公共Artifact链。

`project_completed_result(result_ref)`只接收opaque `result_ref`，所有用户可见元数据必须由Storage重新读取：

1. 读取lifecycle、authoritative Call和terminal receipt；
2. 校验Call为`completed`且receipt、Call和lifecycle绑定同一个`result_ref`；
3. 使用snapshot authority复验owner、Task、Node、Call、size、SHA和store kind；
4. 从authoritative Call读取sequence和Tool name并生成安全文件名；
5. 以no-clobber方式复制到`LocalArtifactFileStore`；
6. 保存公共Artifact；
7. CAS把lifecycle从`retained`切换为`artifact_owned`；
8. 返回闭合结果`ready | deferred | permanent_failure`，不得抛出足以回滚terminal authority的业务异常。

projector以窄async callable注入Coordinator和remote terminal committer；调用方不得直接拼装Artifact，
也不得把Server、Tool、owner、size或SHA作为可信参数传入。

## 统一接入点与时序

### 普通与approval路径

`src/integrations/mcp/dispatch_coordinator.py`中completed分支的terminal commit成功后调用projector；同文件另一处
`commit_mcp_call_terminal()`属于failed分支，必须明确不调用projector：

```text
Tool返回
→ durable result完整落盘
→ terminal candidate密封
→ Call + receipt + lifecycle原子提交
→ Call authoritative completed
→ projector.project_completed_result(result_ref)
→ Coordinator继续branch/finalizer
→ Task terminal事件
→ Task Artifact API返回公共文件卡片
```

approval恢复最终重新进入相同ordinary terminal commit分支，不增加第二套投影逻辑。projection不能进入
terminal数据库事务，也不能在Call authority提交前创建用户可下载文件。它在Task terminal事件前执行，
因此正常成功时现有`loadArtifacts()`会在Task完成后一次取得Artifact。

### remote路径

`src/api/runtime.py`中的remote terminal committer在Call/receipt/lifecycle提交成功后调用同一projector，
然后才触发continuation。投影失败不撤销receipt、不阻断continuation、不重发remote Tool；后续reconciler
只根据已提交authority重试本地投影。

### 一个业务Call一个Artifact

一个branch中的每个成功Call都独立promotion；后续Selector继续执行不会覆盖前一个Artifact。这里的Call指
durable `mcp_call_record`业务Call。OCR job workflow内部的poll/ack属于同一个已批准Call的受控步骤，
不单独生成Artifact；该Call的Artifact保存workflow提交的authoritative最终结果。

## startup、历史补投与并发合同

现状只有启动恢复中的一次`result_manager.run_once()`，没有可承载补偿的周期durable janitor。实施时必须：

- pre-ready的`_repair_mcp_terminal_candidate_lifecycle()`只完成已经处于`deleting`的中断态修复；
- terminal candidate恢复结束后只执行`reconcile_untracked()`补齐缺失lifecycle，不再在pre-ready调用会claim
  `retained`删除的`run_once()`；
- Ready之后启动独立、受supervisor和shutdown管理的`mcp-result-artifact-reconciler`任务；立即运行一轮，
  此后每60秒运行一轮；普通Exception记录闭合指标并在60秒后重试，任务意外退出必须由done callback标记runtime
  专用error状态并通过既有audit/日志路径告警，不得静默退出；
- SQLite/PostgreSQL多实例可以同时运行该任务，依靠snapshot hold、确定性Artifact ID和Storage CAS收敛，
  不引入单实例leader authority。

每轮reconciler按以下固定顺序运行，因位于post-ready后台而不阻塞Ready：

1. 完成已有`deleting`等中断态修复，避免旧GC半完成状态悬挂；
2. 调用一次现有`reconcile_untracked(limit=1000)`；该方法内部已经按result ref分页到文件枚举结束，先为
   可验证但尚无SQL lifecycle的durable文件补齐记录，不得在外层按返回总数重复全量调用；
3. 复用现有`status + updated_at + result_ref`索引，以`(updated_at, result_ref)`keyset分页枚举
   `retained / dispatch_resolved`候选，每批最多1,000条；
4. 对每个候选调用同一个projector；未到`eligible_at`的任何失败统一返回deferred并保留，成功转为
   `artifact_owned`；已到期且仍失败时返回permanent，随后以该行刚读取的`result_ref + revision`执行单行CAS
   claim/delete。未经本轮projector返回permanent的`dispatch_resolved`行不得删除；
5. 每页后`await asyncio.sleep(0)`让出event loop，直到本轮keyset扫描结束；
6. 扫描结束后bulk deletion只允许选择`artifact_owned`及`retained / orphan`，不得选择
   `retained / dispatch_resolved`；删除每批最多1,000条直至不足一批；
7. 等待下一次60秒周期。shutdown必须cancel并await该任务。

固定顺序与单行deletion authority确保部署后的每个历史业务结果在删除前至少获得一次可证明的投影尝试，
不会被原pre-ready `run_once()`、另一实例或并发补齐的旧lifecycle抢先GC。扫描期间新写入的结果由普通即时
projector处理；未落入当前keyset页的结果最迟下一轮补偿，且bulk GC无权删除它。

并发和竞态处理：

- projector获取snapshot authority/hold后再复制；GC不得删除有活动snapshot hold的源文件；
- hold释放或revision变化导致CAS失败时，重读lifecycle和Artifact；如果文件身份、size和SHA完全一致，
  只补做CAS，否则fail closed且不覆盖；
- 确定性Artifact ID、no-clobber保存与lifecycle CAS共同保证普通路径、remote worker和reconciler并发时只产生一个Artifact；
- 如果另一执行者已经完成promotion或源GC，当前执行者返回`ready/already_promoted`或
  `permanent_failure/source_expired`，不得重新请求远端；
- `retained / dispatch_resolved`删除必须由当前候选的精确revision CAS触发；CAS丢失时重读状态，
  `artifact_owned/deleted`视为其他worker已收敛，其他状态留待下一轮，不得降级为bulk delete；
- 历史结果已删除文件、orphan、identity不足或receipt冲突时一律不猜测恢复。

历史补投和所有重试的MCP网络调用增量必须为0。

## 生命周期、容量与删除

权威生命周期如下：

```text
retained / dispatch_resolved
  ├─ projection成功 → artifact_owned / artifact_promoted
  │                    → deleting → deleted（只删除durable data/manifest）
  │                    └──────────── 公共Artifact文件保持active
  └─ projection未成功 → retained，至原24小时eligible_at后由既有GC删除
```

- `artifact_owned`不是永久保留durable源文件，而是“公共Artifact副本已验证，可立即回收源”的状态；
- durable GC删除的是原始data/manifest，不得删除`source_kind=mcp_result`的公共Artifact文件；
- 复制期间最多临时占用“一份源 + 一份Artifact”，promotion完成后源文件进入既有GC；
- 单结果继续受64 MiB上限约束，复制必须采用现有1 MiB分块/worker thread文件路径，内存复杂度为O(1)，
  不得在event loop一次性读取完整结果；
- 开始复制前复用既有MCP临时磁盘low-watermark配置值，但必须对Artifact目标目录所在文件系统检查，要求
  可用空间至少为`result_size + low_watermark`；result源与Artifact目录配置在不同文件系统时不得检查错盘；
- 空间不足时返回`deferred/capacity_unavailable`，保持`retained`并在原24小时期限内由周期janitor重试；
- 未到原`eligible_at`时，容量、I/O、身份或文件冲突等非成功结果都只能记录deferred并安全重试；到期后仍
  失败才记录`permanent_failure/(projection_failed|source_expired)`，并只允许当前行的精确CAS删除源；
  不无限延长保留、不绕过容量门禁，也不允许generic bulk GC删除该类结果；
- MCP Artifact不参与Skill同会话supersede；所有成功Call按Task/Call独立保留；
- conversation强删除、Artifact下载鉴权和公共文件清理继续复用现有Artifact合同。

## 失败、补偿与用户可见性

Tool业务终态优先于Artifact投影：

- terminal commit失败：沿用现有unknown/no-replay或fail-closed路径，不创建Artifact；
- promotion失败：Call/receipt/Task保持原终态，lifecycle保持`retained`，不删除未到期durable result；
- 当前Task仍可完成，前端不伪造Artifact卡片；
- post-ready reconciler在原24小时有效期内重试promotion；
- 已有Artifact但lifecycle未切换时，先逐字节复验文件再完成CAS；
- Artifact存在但身份、size或SHA不一致时fail closed，不覆盖文件；
- failed/cancelled Call没有completed authoritative result，不创建原始返回Artifact，继续显示现有脱敏错误码。

使用一个闭合事件`mcp.result_artifact_projection`。payload必须精确包含以下字段，不允许额外键：

| 字段 | 值 |
|---|---|
| `schema` | `maf.user_mcp.result_artifact_projection.v1` |
| `safe_call_ref` | 使用既有MCP audit reference signer生成的不可逆Call引用 |
| `status` | `ready | deferred | permanent_failure` |
| `reason_code` | `promoted | already_promoted | capacity_unavailable | projection_failed | source_expired` |
| `artifact_count` | `0 | 1` |

合法组合只有：`ready/(promoted|already_promoted)/1`、
`deferred/(capacity_unavailable|projection_failed)/0`、
`permanent_failure/(projection_failed|source_expired)/0`。事件ID由确定性Artifact ID、status与reason_code派生；
相同status/reason重试写同一事件，reason或status变化写新事件。Projector内部使用已有signer生成
`safe_call_ref`，Coordinator/remote调用方
看不到raw Call authority。Reducer以`safe_call_ref`维护最多20个Call状态，并使用唯一canonical fold：
`absent → deferred → ready | permanent_failure`；同status的多个合法reason按闭合优先级归并：ready优先
`promoted`，deferred优先`projection_failed`，permanent优先`source_expired`。同Call同时出现ready和permanent
才视为authority conflict并fail closed，不按created_at或event ID猜测先后。一个Call的ready不得清除另一
Call的告警，迟到的deferred不得覆盖同Call终态。事件持久化失败
不得回滚Artifact或业务终态：deferred结果仍为`retained`，下一轮会用
同一确定性ID重试；ready结果以Artifact API为权威，不因提示事件失败删除Artifact；permanent failure提示为
best-effort，事件存储故障不得借此突破原24小时源保留上限，必须另记低基数本地观测。

确定性事件必须连`created_at`也可重建：ready使用Artifact `created_at`，deferred使用authoritative Call
`terminal_at`（缺失时使用lifecycle `created_at`），仍为retained的到期permanent使用`eligible_at`，已经
deleted且无Artifact的`source_expired`使用`deleted_at`。禁止重试时使用当前clock改写同event ID的时间；
SQLite merge与Sidecar idempotency都必须收到逐字段相同的EventRecord。

Task/Node关联继续使用事件envelope；payload不得包含`result_ref`、Artifact storage信息、Call/Server ID、
Tool返回、远端错误体或自由文本异常。MCP audit allowlist只增加`schema`、`reason_code`和`artifact_count`，
与既有`safe_call_ref`、`status`共同保留相同闭合字段。

为覆盖已完成Task刷新后的状态，既有`TaskSummaryResponse`增加可选
`mcp_result_artifact_projections`，最多20项；每项复用上述5个闭合字段。后端从Task事件中按
`safe_call_ref`执行与前端相同的canonical fold并按safe ref稳定排序，不以时间戳决定authority；一个Call最多
6个合法status/reason事件，因此filtered reader固定读取至多121条，第121条即判超限，不新增表或MCP专属
endpoint。字段缺失表示旧Task或无投影事件，前端保持兼容；任何额外键、非法组合、状态fork、超过20个Call
或超过120个事件均fail closed为不展示状态并记录闭合观测，而不是显示自由文本。

普通历史对话并不保证completed Task仍是`current_task_id`，因此`ConversationMessagesResponse.messages[]`中的
assistant `MessageResponse`也必须增加相同可选投影。`list_conversation_messages`在现有owner鉴权后按assistant
message的Task集合有界派生canonical fold，不写入Message metadata或新增Storage schema；旧字段缺失继续兼容。
Task Summary负责当前Task恢复，MessageResponse负责任意completed历史消息，二者必须复用同一后端helper和
测试向量。

前端只扩展现有`TaskEventState.mcp` reducer和`MCPRuntimeStatus`的Ant Design Alert：

- `ready`：清除既有projection警告，但不得提前调用当前`loadArtifacts()`，因为该函数会把Task置完成并关闭SSE；
- `deferred`：显示“工具调用已完成，完整结果文件正在生成，可稍后刷新”；
- `permanent_failure`：显示“工具调用已完成，但完整结果文件未能保留”，不展示内部原因；
- 多Call时只要任一Call为deferred或permanent就保留对应聚合提醒，ready Call不遮蔽失败Call；
- projector正常在Task terminal事件前完成，现有Task完成加载即可显示卡片；
- `loadArtifacts()`清空`currentTaskId`前必须把当前聚合状态复制到对应assistant message；MessageBubble复用
  Ant Design Alert持续显示deferred/permanent，运行中的`MCPRuntimeStatus`不与message notice重复渲染；
- 如果补投发生在Task SSE关闭后，当前Task恢复读取Task Summary，普通历史消息读取MessageResponse，然后沿用
  公共Artifact加载；已成功补投的Artifact届时出现，仍失败的Call继续显示提醒；不保持无限SSE或新增轮询器。

## API与前端Artifact交付

不新增MCP专属Artifact API或组件：

- `GET /api/v1/tasks/{task_id}/artifacts`返回每个成功Call对应的公共file Artifact；
- `GET /api/v1/artifacts/{artifact_id}/download`继续做owner鉴权、`nosniff`和attachment下载；
- 历史会话恢复继续通过公共Artifact投影显示文件卡片；
- 前端复用现有`parseFileArtifactDisplays`与文件卡片，显示文件名、大小、摘要和下载按钮；
- 页面不解析或内联完整不可信JSON。

Main Agent仍只接收现有最多20,000字符的有界execution projection。Artifact公开描述和完整原始字节
不作为新的prompt输入，也不要求模型在正文生成裸下载链接。

## 非功能需求与安全不变量

| ID | 需求与门禁 |
|---|---|
| NFR-01 | 只有completed Call、matching receipt和可验证durable snapshot能生成Artifact |
| NFR-02 | Artifact字节必须与terminal receipt绑定的size和SHA一致 |
| NFR-03 | Artifact投影、重试和历史补投的MCP网络调用增量为0 |
| NFR-04 | 完整raw result和Artifact字节不得进入event、audit、prompt、Node output payload或v2 envelope；既有有界projection不变 |
| NFR-05 | API不得公开内部storage/result/Server/Call authority；跨用户Task查询和下载继续返回404 |
| NFR-06 | 失败/取消远端错误体不得被Artifact化 |
| NFR-07 | 64 MiB结果以内采用1 MiB分块和worker thread复制，常量内存，不阻塞event loop |
| NFR-08 | 并发普通/remote/reconciler投影精确幂等；崩溃恢复不覆盖不一致文件 |
| NFR-09 | 补投不阻塞服务Ready；单批不超过1,000并使用keyset分页 |
| NFR-10 | 容量不足不突破low-watermark或无限延长24小时durable保留 |

## 验收与追踪矩阵

| 需求 | 验收测试 | 证据 |
|---|---|---|
| FR-01, FR-02, NFR-01, NFR-02 | completed result生成确定性ID/文件名/MIME/size/SHA/summary；身份或receipt冲突fail closed | manager/projector单元测试 |
| FR-01, FR-09 | 三个成功Call得到三个独立Artifact；promotion后durable源删除而Artifact仍可下载 | lifecycle集成测试 |
| FR-03, FR-04 | ordinary completed commit、approval恢复、remote terminal调用同一fake projector；ordinary failed commit明确不调用；投影失败不回滚、不重放 | Coordinator/runtime集成测试 |
| FR-05, FR-10, NFR-03, NFR-09 | pre-ready不claim业务结果；untracked单次内部分页；`updated_at/result_ref`两批以上回填；到期失败只精确CAS删除；bulk GC拒绝dispatch_resolved；60秒周期与shutdown；网络调用计数保持0 | startup/post-ready集成测试 |
| FR-05, FR-09, NFR-08 | snapshot hold期间GC不删除；文件写成功/CAS前崩溃和并发promotion可恢复为单Artifact | 故障注入与并发测试 |
| FR-06, G-06, NFR-04 | event/audit/prompt/Node output/v2 envelope泄漏扫描不出现raw result、内部ref或远端错误体 | 安全回归测试 |
| FR-07, NFR-05 | Task Artifact API返回N个file Artifact，`storage_ref`为空，下载可用，跨用户404 | API集成测试 |
| FR-07 | ready不提前完成Task；Task Summary覆盖当前恢复、MessageResponse覆盖任意历史assistant；terminal前复制notice；MessageBubble持续Alert；多Call/fold/fork闭合 | API/frontend parser/reducer/App测试 |
| FR-08, NFR-06 | failed/cancelled/unknown/no-call不生成Artifact且只显示脱敏错误码 | 后端与前端回归测试 |
| NFR-07, NFR-10 | 64 MiB输入常量内存分块复制；低空间返回deferred，到期后GC且有永久告警 | 容量/性能测试 |
| 兼容性 | Skill现有输出、覆盖、ZIP、下载和前端卡片测试保持不变 | Skill/API/frontend回归 |

## 真实OCR smoke

使用用户2,326,771-byte PNG和OCR MCP：

- Task、两个Node、Call、intent、outbox、branch和receipt全部completed；
- 只有1个`start_parse_job`业务Call；
- Task Artifact API包含1个`01-start_parse_job-result.json`；
- 下载大小约50 KiB，SHA与receipt/lifecycle一致；
- 下载内容包含OCR完整原文，而Main Agent正文仍可保持有界摘要；
- v2 envelope不含Base64、Tool参数或Tool结果；
- durable源最终进入`deleted`，Artifact仍可下载；
- 重启与history恢复后Artifact仍显示，MCP网络调用数不增加。

## 实施影响面

| 模块 | 预期最小改动 |
|---|---|
| `src/integrations/mcp/result_artifact_projection.py` | 新增canonical projector、闭合事件builder/parser、状态fold、目标盘容量检查与安全观测合同 |
| `src/integrations/mcp/durable_result_lifecycle.py` | 保留低层promotion并增加历史候选投影、到期业务结果单行CAS删除和安全bulk GC编排 |
| `src/integrations/mcp/dispatch_coordinator.py` | ordinary completed terminal commit后调用窄projector；failed terminal commit增加零调用回归 |
| `src/api/runtime.py` | 装配projector；remote terminal接入；移除pre-ready源GC；管理60秒post-ready reconciler及shutdown |
| `src/core/contracts.py`、`src/storage/sqlite/`与`src/storage/postgres/` | 增加复用现有`updated_at`索引的只读keyset查询及精确单行deletion claim；bulk claim排除dispatch_resolved |
| `src/integrations/mcp/audit.py` | 允许projection事件的3个新增闭合字段，继续拒绝secret-like和额外业务内容 |
| `src/api/dto.py`、`src/api/routes/tasks.py`与`src/api/routes/conversations.py` | Task Summary和assistant MessageResponse增加相同闭合projection列表，复用有界安全fold helper |
| `frontend/src/api/taskEvents.ts` | 把projection事件加入命名SSE监听列表 |
| `frontend/src/api/types.ts`与`frontend/src/App.tsx` | 解析Task/Message projection，terminal前持久到前端assistant message并由MessageBubble Alert显示 |
| `frontend/src/domain/taskEvents.ts` | 解析并归并闭合projection状态 |
| `frontend/src/components/MCPRuntimeStatus.tsx` | 使用现有Alert展示deferred/permanent状态 |
| `tests/`与`frontend/src/**/*.test.*` | 按追踪矩阵增加回归、故障注入、安全泄漏和前端恢复覆盖 |

不新增Storage表或修改terminal aggregate schema；若现有生命周期字段不足以支持分页，允许增加只读查询，
不引入新的持久业务authority。

## 风险、假设与缓解

| 类型 | 内容 | 影响 | 缓解或验证 |
|---|---|---|---|
| 已证实风险 | 当前pre-ready和generic bulk deletion都会claim已到期`retained / dispatch_resolved`；并发补齐lifecycle时仅靠“先扫描后GC”仍可能删除未尝试结果 | 历史原文不可恢复 | pre-ready禁删；业务结果到期只允许projector返回permanent后的result_ref+revision单行CAS；bulk GC永久排除dispatch_resolved |
| 已证实风险 | `artifact_owned`会被现有janitor选择并删除durable源 | 错误实现会误以为源永久存在 | Artifact逐字节验证与CAS成功后才进入该状态；验收“源deleted、Artifact可下载” |
| 已证实风险 | 当前`loadArtifacts()`会完成Task并关闭SSE | ready事件若提前调用会截断后续Call/事件 | ready只更新状态；仍由`task.completed`或history路径加载Artifact |
| 已证实风险 | Artifact目录可配置为与result源不同文件系统 | 检查源盘空间不能保证目标复制成功 | 对`LocalArtifactFileStore.root_dir`所在文件系统执行`result_size + low_watermark`检查 |
| 有界假设 | 既有MCP audit reference signer可作为前端`safe_call_ref`来源 | 决定projector与runtime装配的窄依赖接口 | 实施计划锁定signer注入；安全测试证明不可逆且不出现raw Call ID |
| 有界假设 | 实际历史候选量和多Call分布尚无完整生产统计 | 影响回填耗时估算，不影响正确性合同 | 每批1,000、逐页yield、60秒周期；发布前记录候选计数并用三Call合成Task验收 |
| 回滚风险 | 已promotion的durable源可能已deleted | 关闭功能后不能还原原始双份存储 | 回滚只停止新writer/reconciler，保留公共Artifact reader和已生成文件，不降级lifecycle |

没有未决产品方向、用户政策、合规或发布授权问题；`prod`继续明确不在本设计授权范围内。

## 兼容性、发布与观测

- 不修改Skill公开Artifact合同、现有前端文件卡片或下载路由。
- 部署后post-ready先按固定顺序尝试历史补投，再执行全新MCP smoke；补投不阻塞Ready。
- 指标至少区分`promoted`、`already_promoted`、`deferred_capacity`、`permanent_failure`、
  `source_expired`和`projection_latency_ms`，标签不得包含用户、Server、Tool返回或内部ref。
- 发布门禁：自动追踪矩阵通过；真实OCR smoke通过；未出现raw-result泄漏；源GC后Artifact仍可下载。
- 本设计只授权`main`开发与本地验证，不授权`prod`部署、构建或数据迁移。

## 回滚

1. 先停止新MCP业务提交并等待活动Call收敛，不能先停止安全reconciler。
2. 保留projector/reconciler和bulk排除保护，直到`retained / dispatch_resolved = 0`且`deleting = 0`；
   每个遗留结果继续按Artifact或到期精确CAS删除规则收敛。
3. 身份/digest损坏导致无法安全promotion或删除时，回滚必须Blocked并交给operator；不得恢复旧generic bulk绕过。
4. drain证据通过后才停止projector/reconciler并回滚新writer/UI；已生成Artifact继续有效，已删除源不得重建。
5. 保留公共Artifact reader与闭合projection事件reader；不回滚Call、receipt、Task、Node、intent、outbox或
   no-replay证据。

## document-perfectization复审记录

### 第1轮：修订前

- 发现4个Major：错误的`artifact_owned`保留语义、接入点不闭合、回填/GC竞态与容量未定义、异步失败前端不可见。
- 发现2个Minor：缺少干系人/受影响系统，缺少需求到测试证据的追踪矩阵。
- 结论：未通过；用户已授权直接修订。

### 第2轮：首次修订后

- 正确对齐`artifact_owned → deleting → deleted`源生命周期，Artifact文件独立保留。
- 明确唯一projector、四条调用路径、terminal之前尽力投影和no-replay失败边界。
- 明确修复→补投→GC顺序、snapshot hold/CAS竞态、1,000条keyset分页、64 MiB/1 MiB/low-watermark容量合同。
- 使用闭合事件与既有`MCPRuntimeStatus`/Artifact加载链完成即时、延迟和history恢复可见性。
- 增加干系人、FR/NFR、追踪矩阵、发布指标和真实smoke门禁。
- 复审发现2个Major：误认为已有周期janitor；ready事件若复用当前`loadArtifacts()`会提前完成Task并关闭SSE。
- 复审同时发现不存在的事件协议文件引用，以及多Call状态互相覆盖和已完成Task不重放SSE的history缺口。
- 结论：未通过，继续按用户授权修订。

### 第3轮：最终复审

- 增加独立60秒post-ready reconciler，pre-ready不再claim未投影`retained`源；锁定
  repair/untracked→完整keyset投影→GC顺序和shutdown/supervision。
- ready事件不提前加载Artifact；闭合事件以`safe_call_ref`按最多20个Call独立收敛，Task Summary提供history恢复投影。
- 修正实际影响文件，补入MCP audit、SSE命名监听、Task DTO/route、frontend type/App恢复和目标文件系统容量检查。

完整评分：

| 类别 | 得分 | 扣分 |
|---|---:|---|
| 目标、范围、用户与干系人价值 | 15/15 | 无 |
| 功能需求 | 20/20 | 无 |
| 非功能需求 | 10/10 | 无 |
| 验收标准与可测试性 | 15/15 | 无 |
| 边界情况与失败模式 | 10/10 | 无 |
| 依赖与实施可行性 | 10/10 | 无；实施计划已锁定从现有容量配置注入low-watermark并检查Artifact目标文件系统 |
| 测试、发布、迁移与回滚 | 10/10 | 无 |
| 风险、假设、追踪与一致性 | 9/10 | Minor：真实历史候选量和多Call分布没有完整生产统计；只影响回填耗时估算，已用1,000条keyset、逐页yield、60秒周期与发布前计数约束 |
| **总分** | **99/100** | **1个有界Minor** |

- Blocking：0；Major：0；Minor：1。
- 结论：**Pass with recorded assumptions**，通过95分信心门。

### 第4轮：实施计划一致性复审

- 发现并修正1个Major：多实例或并发`reconcile_untracked`可在keyset扫描游标之后新增已到期行，原“完整扫描后
  bulk GC”仍可能删除未获投影机会的`dispatch_resolved`结果。
- 删除authority改为逐行闭合：未到期失败只能deferred；到期permanent后才以当前revision精确CAS删除；
  generic bulk GC永久排除业务结果，只清理artifact-owned和orphan。
- keyset改为复用现有`status + updated_at + result_ref`索引；Task Summary与前端统一canonical状态fold，
  不再依赖时间戳选择“最新”事件。
- 实施计划同时锁定low-watermark的现有配置来源、目标盘free-bytes注入与测试hook，消除原依赖接口Minor。
- 修订后完整评分为99/100；Blocking 0，Major 0，剩余1个有界Minor是实施前尚无真实历史量统计，结论保持
  **Pass with recorded assumptions**。

### 第5轮：完成语义一致性复审

- 发现并修正1个Major：绝对“每个completed Call必有Artifact”与已批准的到期permanent failure边界冲突。
- G-01和FR-01统一为闭合二选一：正常容量/完整authority只允许唯一Artifact；只有达到原期限仍无法安全投影
  才允许permanent failure，任何缺失不得静默。
- 重评分仍为99/100；Blocking 0，Major 0，唯一Minor仍为真实历史量需实施前预检，最终结论不变。

### 第6轮：事件幂等一致性复审

- 发现并修正1个Major：同status不同reason原本会复用同一event ID并写入冲突payload。
- event ID改为绑定Artifact ID、status和reason；同status原因以固定优先级fold，ready/permanent并存才判fork；
  history读取以121条硬限检测超过20 Call/120合法事件的异常。
- 重评分仍为99/100；Blocking 0，Major 0，唯一Minor与最终结论不变。

### 第7轮：EventRecord精确幂等复审

- 发现并修正1个Major：确定性event ID若搭配重试时的当前时间，SQLite merge会改写历史，Sidecar也可能拒绝
  同idempotency key的不同payload。
- created_at改为稳定authority派生，要求同ID重试逐字段相同；重评分仍为99/100，最终结论不变。

### 第8轮：deleted时间authority复审

- 发现并修正1个Major：deleted lifecycle会清空eligible_at，source_expired不能用它重建事件时间。
- retained到期permanent使用eligible_at，deleted source_expired使用deleted_at；重评分仍为99/100，最终结论不变。

### 第9轮：Coordinator接入点事实复审

- 发现并修正1个Major：两处terminal commit并非两个completed入口，其中一处是failed commit。
- ordinary只在唯一completed commit后调用projector；failed commit锁定零调用，approval仍复用completed分支；
  重评分仍为99/100，最终结论不变。

### 第10轮：历史消息可见性复审

- 发现并修正1个Major：Task完成后currentTaskId清空会卸载MCPRuntimeStatus，且普通历史消息不一定读取Task Summary。
- assistant MessageResponse增加相同闭合投影，terminal前同步到前端message，MessageBubble持续显示Alert；
  重评分仍为99/100，最终结论不变。
