# MCP 工具授权框生命周期硬伤最小修复实施计划

依据：`2026-09-01-mcp-approval-dialog-lifecycle-hard-defect-repair-design.md`

初始设计提交：`2b05c065`；首轮硬伤修订提交：`6d0de294`；后续设计/计划硬伤修订以本文档提交历史为准

状态：`published_pending_deploy`

自动化实施证据（2026-09-01）：

- 生产代码严格限于第1节列出的3个文件，测试严格限于对应3个既有文件；未修改依赖、API/DTO、schema、
  migration、Runtime Sidecar、Rust、Skill或`prod`；
- 后端聚焦门禁分别通过20项API recovery/continuation测试与71项aggregate/coordinator测试；
- 前端聚焦门禁通过3个文件193项测试，typecheck与production build通过；
- 受影响范围门禁通过`tests/api`全量628项、Frontend全量24个文件347项、compileall、变更面Ruff、
  typecheck、production build和`git diff --check`；无测试失败或skip，仅保留既有Frontend大chunk提示；
- 实施源码commit `36f853a06d263c6ddfbd8670e4b8cba32f13e6c5`已非强制推送，GitHub与Gitee
  `refs/heads/main`均只读核验为该commit；
- backend-dev `0.1.29`与frontend-dev `0.1.28`在构建前均确认tag不存在；已从上述clean commit构建并
  推送，OCI index digest分别为
  `sha256:a3c0d46470e9c9033d09d01273b0afee2f1b76d14c9c55ceaefd19d8376c6915`和
  `sha256:5ce9bb47927b560c9f5892eb91ce89b927787fd7185cd13168f96929cf4c85fa`，均含
  `linux/amd64`及attestation；backend无内置配置/.env/运行数据且严格配置bootstrap、隔离healthy与
  `/api-doc`通过，frontend `nginx -t`、修复静态标记、隔离healthy与`/seedpilot/`通过；
- `docker_cmd.md`已在仓库外创建`0600`备份，只完成4处backend dev tag与2处frontend dev tag精确替换，
  文件继续为`0600`、Git-ignored/untracked；Runtime Sidecar tag未改；
- 开发服务器部署、真实MCP/Chrome和`biobin_dev`只读smoke尚未执行，因此不得标记为
  `deployed_pending_smoke`或`complete_dev`。

目标分支：`main`

目标环境：main 开发环境；不涉及 `prod`

## 1. 完成声明与范围

完成必须同时满足：

1. MCP approval authority首次转换后，在Agent continuation结束前持久化并发布安全的
   `mcp.tool_approval_decided`；
2. authority与Event之间发生部分失败时，exact answer replay补齐同一个Event且不重复Answer、Grant、
   Tool调用或账本记录；
3. 同一approval Interrupt的UI重试复用同一消息身份，并经真实chat submission入口命中exact replay；
4. 用户点击授权后立即恢复原Task SSE，授权框不等待完整Tool/Agent执行才关闭；
5. 旧授权POST返回不能覆盖下一次MCP/普通Interrupt，也不能复活已进入终态的Task；
6. Event replay和HTTP状态对账的所有Task/Agent终态都清除`mcp.approval`；
7. 实施commit已非强制推送到现有两个源码远端，两个`main`均指向同一commit后才构建镜像；
8. 新代码以`biobin_user`连接`biobin_dev`完成真实审批smoke，backend监听`31888`、frontend监听
   `31999`，其他开发容器、`breeding-agent-net`和`prod`保持不变。

生产代码只修改：

- `src/api/runtime.py`
- `frontend/src/App.tsx`
- `frontend/src/domain/taskEvents.ts`

测试只修改：

- `tests/api/test_user_mcp_recovery_startup.py`
- `frontend/src/App.test.tsx`
- `frontend/src/domain/taskEvents.test.ts`

不新增依赖、API、DTO、数据库schema、migration、队列、配置或feature flag；不修改MCP Coordinator、
Gateway、协议、Tool参数、远端OCR、Agent continuation/lease/recovery、Runtime Sidecar、Rust、Skill、
历史Task/Event/Grant或`prod`。

开发发布对固定为：

- backend-dev：`0.1.29`，由当前`0.1.28`升级；
- frontend-dev：`0.1.28`，由当前`0.1.27`升级；
- runtime-sidecar-dev：保持当前`0.1.27`，不构建、不重启。

## 2. Checkpoint A：基线与红测

### 2.1 只读基线

实施前确认：

- 当前分支为`main`，工作树只包含本任务预期改动；
- 根`docker_cmd.md`存在、权限不高于`0600`、仍被Git忽略且未被跟踪；
- 设计列出的三个生产文件和三个测试文件与`6d0de294`一致；
- 当前前端测试仍证明“POST返回后才重订阅”，终态helper仍保留旧approval；
- 当前后端授权路径没有Runtime级`mcp.tool_approval_decided` exact append。

不读取或输出`docker_cmd.md`中的密码、Token、DSN或其他敏感值。

### 2.2 后端红测

在`tests/api/test_user_mcp_recovery_startup.py`复用现有SQLite runtime和approval fixture，先增加：

1. `allow_once`/`always_allow`的resume由可控Event阻塞时，Interrupt已经answered，
   `mcp.tool_approval_decided`已经持久化，而Tool尚未执行；
2. `deny`不进入resume，但返回前已经持久化`decision=deny`；
3. 决定Event只含`interrupt_id`、64位`safe_call_ref`和`decision`，不含arguments、Endpoint、credential、
   authorization、server/tool原始标识或fingerprint；
4. 首次决定Event append被注入一次失败后，authority保持成功；通过真实`submit_chat_message`入口以相同
   `client_message_id`和answer重试时，exact append补齐一条Event，Answer/Grant/Tool调用均不重复；
5. 首次请求与exact replay的Event ID、payload和`created_at`完全一致，不触发
   `runtime_store_idempotency_conflict`。

旧实现必须因没有Runtime decided Event、没有exact补写而失败。红测阶段不修改生产代码、不提交。

### 2.3 前端红测

在`frontend/src/App.test.tsx`和`frontend/src/domain/taskEvents.test.ts`增加或收紧：

- 将approval对应的第二次`submitMessage`保持为未完成Promise；点击按钮后，在Promise resolve前必须出现
  第二条Task订阅；
- 在POST未返回时注入decided Event，授权框立即关闭，后续Tool Event仍被消费；
- 在旧POST未返回时注入下一次MCP approval或普通Interrupt，再resolve旧POST；新waiting authority保持；
- 在旧POST未返回时注入Task/Agent终态并完成结果收敛，再resolve旧POST；Task不恢复running、不重建
  current Task或SSE；
- approval POST失败时弹框仍pending、按钮恢复，但提前建立的第二条订阅保留；相应旧断言从1条订阅改为
  2条；
- 同一Interrupt失败后重试必须提交完全相同的`clientMessageId`，新的Interrupt必须使用不同ID；
- 六类Task/Agent终态Event、`markTaskCompleted()`和`markTaskFailed()`均清除approval；
- SSE丢失后仅靠`getTask()`对账completed、failed、cancelled或既有MCP terminal projection时，页面不再
  显示授权框。

旧实现必须分别暴露“订阅建立过晚”“旧response覆盖新状态”和“终态helper保留approval”。

## 3. Checkpoint B：后端决定Event exact append

只修改`src/api/runtime.py`。

### 3.1 唯一最小helper

在`ApiRuntime`内部增加私有helper，不新建模块：

1. Event ID只由版本化event kind、`interrupt_id`和`interrupt_answer_id`经现有SHA-256/canonical JSON
   模式派生；
2. `safe_call_ref`使用pending action的`approval_fingerprint`与既有
   `mcp-approval-call-reference-v1` context生成；
3. Event `created_at`固定使用accepted answer持久化的`created_at`，不能在重试时重新取当前时间；
4. payload严格为`interrupt_id`、`safe_call_ref`、`decision`；
5. 使用`storage.append_event_exact()`；仅`duplicate=false`时调用现有Event broker发布。

不得把Server/Tool显示名补入Runtime Event；前端继续从required Event的previous approval复用显示值。

### 3.2 两个调用点

- 首次`accept_mcp_tool_approval()`返回`accepted`或`denied_finalized`后，立即调用helper；然后才记录
  `node.ready_to_resume`、进入Agent resume或返回deny终态；
- 找到`exact_answer`时，在`continuation_receipt`提前返回之前调用同一个helper。已有Event只得到
  duplicate，不发布第二条；缺失Event被补齐。此路径不再次调用approval authority、Grant writer或Tool。

`conflict`、`invalidated`和非MCP Interrupt不得写decided Event。

### 3.3 通过标准

- 红测全部转绿；
- `allow_once`、`always_allow`、`deny`均有正确decision；
- accepted resume被阻塞时Event已经可从storage读取；
- 注入首次append失败后的重试只有1个accepted Answer、0或1个既有scope Grant、1个Runtime decided
  Event和最多1次实际Tool执行；
- Event序列化泄漏扫描为0。

建议检查点提交：`fix(api): persist MCP approval decisions before resume`。

## 4. Checkpoint C：终态投影清理

先修改`frontend/src/domain/taskEvents.ts`：

- 六类Task/Agent终态Event fold都把`mcp.approval`设为`null`；
- `markTaskCompleted()`和`markTaskFailed()`执行相同清理；
- 不清空MCP calls、result artifact projection、execution unknown/resolution或其他终态证据；
- 非终态Event、approval decided和普通waiting行为不变。

再修改`frontend/src/App.tsx`的既有终态入口：

- `markTaskCancelledState()`清除approval；
- `reconcileTerminalTaskStatus()`的MCP terminal projection早返回分支立即清除approval；
- completed/failed/cancelled继续复用已收紧的helper或取消收敛函数；
- 不改变Artifact加载、历史消息刷新、current Task清理或失败/取消文案。

通过标准：纯reducer测试覆盖六类Event和两个helper；App测试覆盖无终态SSE的四类HTTP对账路径。

## 5. Checkpoint D：授权提交时序与旧response guard

只在`frontend/src/App.tsx`的`handleMCPApprovalDecision()`内做手术式修改：

1. 捕获approval、`interruptId`、conversation、Task、assistant和generation快照；
2. 以`mcp-approval-answer-v1-${interruptId}`生成稳定`clientMessageId`；不得包含decision，不得每次点击
   调用`makeClientId()`；同一Interrupt的重试复用该ID，不同Interrupt自然隔离；
3. 设置现有submitting状态后，立即对原Task调用`subscribeToTask()`，再调用`api.submitMessage()`；
4. POST返回后先验证generation/conversation以及当前Task/assistant仍与快照一致；
5. `loading_artifacts`、cancelling、completed、failed、cancelled均视为已收敛，旧response只进入`finally`；
6. 当前phase为waiting且当前pending approval不是原`interruptId`时，视为新的MCP/普通Interrupt，旧
   response不得清除pending interrupt、assistant prompt、approval或订阅；
7. durable decided Event已把原approval设为非pending时，同Task response不再重复修改投影或订阅；
8. 只有原approval仍为同一pending Interrupt时，成功response才作为Event尚未到达的本地兜底，将它
   设为非pending并恢复running；
9. response Task ID与原Task不同时，只有上述快照仍为当前authority才执行既有Task切换和订阅；同Task
   不建立第三条订阅；
10. catch继续显示现有错误且不乐观关闭approval；finally继续清除submitting。

若首次请求已经完成authority转换但客户端只收到失败，同一decision重试必须命中相同message identity并
补写Event；用户改选另一decision时必须保留现有409 conflict，不能创建第二个Answer或覆盖authority。

不得抽取通用SSE/Modal框架，不修改API client、Dialog组件或全局Task状态架构。

通过标准：所有deferred Promise竞争测试转绿，连续Tool approval无需刷新，终态和普通Interrupt不被旧
response覆盖。

建议前端检查点提交：`fix(frontend): close MCP approval lifecycle races`。

## 6. Checkpoint E：自动门禁与范围审计

### 6.1 聚焦门禁

```bash
conda run -n multi_agent python -m unittest \
  tests.api.test_user_mcp_recovery_startup \
  tests.api.test_agent_continuation
conda run -n multi_agent python -m unittest \
  tests.storage.test_mcp_dispatch_aggregate_repository \
  tests.integrations.mcp.test_dispatch_coordinator

npm --prefix frontend test -- --run \
  src/App.test.tsx \
  src/domain/taskEvents.test.ts \
  src/components/MCPApprovalDialog.test.tsx
npm --prefix frontend run typecheck
npm --prefix frontend run build
```

### 6.2 受影响范围回归

```bash
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m compileall -q src tests
conda run -n multi_agent ruff check \
  src/api/runtime.py \
  tests/api/test_user_mcp_recovery_startup.py

npm --prefix frontend test -- --run
npm --prefix frontend run typecheck
npm --prefix frontend run build

git diff --check
```

所有命令都从仓库根目录执行。若完整API或Frontend出现失败，必须先用未修改基线复现后才能标记为既有
失败；不能把skip或未运行写成通过。

### 6.3 最终diff门禁

功能diff只能包含第1节列出的3个生产文件和3个测试文件。模块职责和目录结构不变，因此
`src/api/AGENTS.md`、`frontend/AGENTS.md`原则上不改；实施结束仍必须检查其索引是否受影响。

Rust/Runtime Sidecar代码没有变化，且复用仓库已经在正式路径使用的`append_event_exact()`合同，因此本次
不运行Rust全量门禁、不重建Sidecar；真实开发smoke必须证明现有部署合同可用。

## 7. Checkpoint F：文档、镜像与发布前预检

### 7.1 状态与账本

自动门禁通过后：

- 更新本计划和设计状态，但不提前写入未执行的部署/smoke证据；
- 更新`CHANGELOG.md`记录准确测试数量、已知失败和范围审计；
- 检查`docs/AGENTS.md`索引；
- 提交仓库实现后，以非强制`git push origin main`推送到`origin`现有的GitHub和Gitee push URL；
- 分别只读验证两个远端`refs/heads/main`都等于本地实施commit；任一推送或验证失败立即停止；
- 只有双远端源码检查点闭合后才构建镜像，保证发布artifact可由远端源码恢复。

### 7.2 镜像构建与验证

使用根`Dockerfile`的既有targets构建`linux/amd64`：

- `backend` -> `breeding-agent-backend-dev:0.1.29`；
- `frontend` -> `breeding-agent-frontend-dev:0.1.28`。

构建前以只读manifest检查确认两个目标tag尚未被占用；任一tag已存在且不是本次clean commit的同一
artifact时禁止覆盖，先停止并修订计划、账本和受保护命令中的版本。不得临时改tag后绕过文档。

不构建`runtime-sidecar` target。推送前验证：

- backend镜像不包含`/app/config.yaml`、凭据或本地runtime数据；
- 隔离backend完成package import、strict config bootstrap和`/api-doc` smoke；
- frontend执行`nginx -t`，`/seedpilot/`返回成功且静态产物包含本次前端修复；
- 两个远端OCI manifest都包含`linux/amd64`，记录不可变digest；
- BuildKit警告逐条区分既有启发式警告与真实失败。

### 7.3 `docker_cmd.md`保护

编辑前必须在仓库外创建权限不高于`0600`的备份。只允许把开发backend tag更新到`0.1.29`、frontend
tag更新到`0.1.28`；网络、端口、alias、volume、secret、Skill挂载、数据库和所有`prod`命令保持不变。

编辑后确认文件仍存在、权限不高于`0600`、Git-ignored/untracked且未进入Git对象。不得输出文件中的
敏感内容。

## 8. Checkpoint G：开发服务器成对部署

服务器命令由用户在现有root FinalShell会话中逐条执行；Codex不直接操作FinalShell。每一步都先检查，
再给下一条命令。

### 8.1 部署前只读核验

必须确认：

- 当前backend恰为`breeding-agent-backend-dev:0.1.28`、端口`31888`、网络
  `breeding-agent-dev-net`、alias `backend`且healthy；
- 当前frontend恰为`breeding-agent-frontend-dev:0.1.27`、端口`31999`且healthy；
- `breeding-agent-net`中的prod容器和全部其他容器状态已记录；
- 新backend使用现有环境、只读volume和开发网络预检，`SELECT current_database(), current_user`严格断言
  `database=biobin_dev user=biobin_user`；任何`biobin_db`、数据库名/用户不一致或strict bootstrap失败都
  立即停止；
- 本次无schema或手工数据变更，不运行DDL、migration、restore或数据库清理。

### 8.2 最小替换顺序

1. pull并核对两个新镜像digest/architecture；
2. 只停止并重命名旧`breeding-agent-backend-dev`为明确的`0.1.28` rollback容器；
3. 按受保护命令以原网络、alias、volume、环境和`31888:8000`启动backend `0.1.29`；
4. 等待backend healthy并验证`http://127.0.0.1:31888/api-doc`；
5. 只停止并重命名旧`breeding-agent-frontend-dev`为明确的`0.1.27` rollback容器；
6. 以原网络、volume和`31999:80`启动frontend `0.1.28`；
7. 等待frontend healthy并验证`http://127.0.0.1:31999/seedpilot/`；
8. 重新列出dev/prod两个网络的容器、镜像、health和端口，确认非目标容器前后不变。

绝对禁止`docker compose down`、批量stop/restart、network删除/重建、volume删除、`docker system prune`或
任何会影响PostgreSQL、OCR、breedcore、breedstat2、imgpu、Runtime Sidecar和prod容器的命令。

backend或frontend任一启动/health失败时不继续smoke；按第10节成对回滚。

## 9. Checkpoint H：真实MCP验收

在Chrome现有开发登录态打开`http://175.6.25.109:31999/seedpilot/`，使用全新conversation和现有OCR
MCP。选择尚无Grant的Tool；若所有目标Tool已有Grant，只能在用户明确同意后删除该目标Tool的单条Grant，
不得批量清空其他授权。

### 9.1 用户可见行为

1. 触发第一个MCP Tool approval，点击`仅允许一次`；
2. 授权框在decided Event到达后关闭，不能等OCR/Agent最终完成才关闭；
3. 页面继续显示Tool执行状态；若随后出现第二个Tool approval，必须无需刷新即可显示且不被第一个POST
   的迟到response关闭；
4. Task最终完成并显示回答；
5. 刷新页面，授权框不再出现；
6. 再执行一次`deny`可由自动测试作为必需证据，真实远端不额外触发无价值调用。

### 9.2 `biobin_dev`只读证据

只查询安全标识、状态、计数和时间：

- required Event后存在对应的唯一Runtime exact decided Event；
- decided发生在Tool/Agent terminal之前；
- Interrupt为answered，accepted Answer唯一，pending action最终consumed或deny终态正确；
- `always_allow`未参与本次smoke；若另行测试，其Grant仍是既有per-Tool scope且唯一；
- Task/AgentRun终态一致，无lock、`execution_crash`或重复Tool调用；
- Event payload泄漏扫描为0。

查询前再次断言`current_database()='biobin_dev'`且`current_user='biobin_user'`。不得查询或输出用户消息
正文、Tool arguments、Endpoint、Header、Token、DSN或原始fingerprint；不得连接、查询或修改
`biobin_db`。

### 9.3 发布完成条件

只有自动门禁、两个镜像digest、开发容器health、真实approval时序、刷新后无旧弹框、数据库安全证据和
其他容器不变全部闭合，才把计划标记为`complete_dev`。否则准确记录为`implemented_local`、
`published_pending_deploy`或`deployed_pending_smoke`。

## 10. 回滚与停止条件

### 10.1 成对回滚

出现以下任一情况立即停止并回滚backend/frontend整对：

- 新backend未以`biobin_user`连接`biobin_dev`、strict bootstrap或health失败；
- 新frontend health失败或不能访问backend；
- approval仍永久pending、下一授权被旧response关闭、Task终态被复活；
- 出现重复Tool调用、Grant扩大、Event泄漏、`execution_crash`；
- 任一非目标容器状态、网络或端口发生变化。

回滚只停止新backend/frontend，恢复保留的backend `0.1.28`和frontend `0.1.27` rollback容器，并分别验证
`31888`、`31999`和health。不得回滚数据库，因为本次没有schema/data cutover；真实smoke产生的合法Task、
Answer、Event和MCP结果保留，不手工删除。

### 10.2 保留窗口

旧rollback容器保留到用户确认开发验收结束。之后如需删除，只精确删除这两个已退出rollback容器，并在
执行前再次取得用户确认；不做通用容器或镜像清理。

## 11. 验收映射

| 设计成功标准 | 自动证据 | 开发环境证据 |
| --- | --- | --- |
| decided早于完整continuation | 后端阻塞resume测试 | Event时间早于Tool/Agent terminal |
| exact replay无重复副作用 | 稳定client ID、真实chat入口append失败注入与exact retry | 唯一Answer/Event/Tool计数 |
| 点击后立即恢复SSE | deferred POST前订阅断言 | 授权框不等待OCR结束 |
| 旧POST不覆盖新Interrupt/终态 | 两类deferred竞争测试 | 连续approval与最终完成 |
| 所有终态清除approval | reducer/helper/HTTP对账测试 | 完成后刷新无弹框 |
| 安全与范围不回归 | payload扫描、双远端commit、diff门禁 | `biobin_dev/biobin_user`身份与其他容器不变 |

## 12. 建议提交序列

1. `fix(api): persist MCP approval decisions before resume`
2. `fix(frontend): close MCP approval lifecycle races`
3. `docs(mcp): record approval lifecycle verification`

红测不单独提交；每个功能提交前运行对应聚焦门禁。镜像只从自动门禁通过后的clean commit构建。

License Requirement：复用现有Python、FastAPI、Interrupt authority、durable Event exact append、React、
Task SSE、Vitest、unittest和Docker targets；无新增依赖或许可变化。
