# MCP auto与显式绑定路由等价性实施计划

## 状态与依据

- 日期：2026-08-19
- 分支：`main`
- 状态：计划已制定，尚未实施
- 设计依据：`2026-08-19-mcp-auto-explicit-route-equivalence-design.md`
- 设计checkpoint：`0570572`
- 范围：只新增Orchestration route handoff、修改其唯一调用点并增加测试；不修改MCP执行链

## 1. 完成声明

只有同时满足以下条件才可宣称开发完成：

1. auto与explicit选中同一Server时，Executor收到相同`input_payload`和功能性MCP metadata；
2. auto route只能使用当前`available_mcp_servers`，显式/恢复route只能使用可信固定Server ID；
3. allowlist外Server、固定ID冲突或无效固定ID在scheduler、Executor和网络调用前失败；
4. canonical投影删除Node中的`mcp_dispatch_server_id`、`forced_by_mcp_command`和`mcp_command`，
   只把`mcp_binding_mode=explicit_command`作为selected-server执行合同；
5. malformed payload继续返回现有`mcp_dispatch_payload_invalid`，不得改变错误码或调用Coordinator；
6. approval、startup v2、legacy v1和remote continuation不重推断auto/explicit来源；
7. 两个MCP Node的既有Plan保持两个Node及原有边，每个Node独立校验和归一化；
8. 用户PNG的全新auto OCR Task只产生1个`start_parse_job`业务Call并返回非空正文；
9. v2 envelope继续不含Base64、Tool参数或Tool结果；
10. 最终业务源码diff仅包含`src/orchestration/mcp_route_handoff.py`和
    `src/orchestration/service.py`。

## 2. 严格文件边界

允许新增或修改：

- `src/orchestration/mcp_route_handoff.py`
- `src/orchestration/service.py`
- `tests/orchestration/test_mcp_route_handoff.py`
- `tests/orchestration/test_mcp_route_handoff_service.py`
- `tests/orchestration/test_mcp_dispatch_resume_v2.py`
- 必要的既有Orchestration/API测试文件，仅用于增加回归断言
- 本设计、实施计划、`src/orchestration/AGENTS.md`、`docs/AGENTS.md`和`CHANGELOG.md`

明确禁止修改：

- `src/api/runtime.py`
- `src/capabilities/mcp_dispatch/`
- `src/integrations/mcp/`
- `src/storage/`
- frontend业务代码
- v2 envelope、pending payload、Coordinator、Gateway、OCR workflow或多MCP DAG实现

开始实施前记录当前`git status --short`，保留现有`.omx/`删除和根`AGENTS.md`修改，不暂存、不恢复、
不覆盖这些用户变更。每个checkpoint只暂存本计划列出的明确文件。

## 3. Phase 0：基线与失败测试

### 3.1 基线验证

先运行并保存结果：

```text
tests.orchestration.test_workflow_router
tests.orchestration.test_mcp_dispatch_resume_v2
tests.orchestration.test_fake_capability_flow
tests.capabilities.mcp_dispatch.test_selector_router_executor
tests.api.test_mcp_server_soft_binding
tests.api.test_user_mcp_recovery_startup
```

基线失败必须区分为既有失败或本轮回归；不得为通过测试而修改禁止范围源码。

### 3.2 先写route handoff红测试

新增`tests/orchestration/test_mcp_route_handoff.py`，在实现前覆盖：

1. 非MCP capability逐字段不变；
2. malformed MCP payload逐字段不变且无route rejection；
3. pinned ID与payload匹配时通过；
4. pinned ID优先于allowlist，冲突时拒绝；
5. pinned字段存在但为`None`、非字符串、空白字符串时拒绝且不能降级allowlist；
6. 无pinned ID时，payload Server必须属于非空allowlist；
7. payload和pinned ID只按`.strip()`值比较，原payload不变；
8. allowlist外、空allowlist返回闭合`mcp_selected_route_not_authorized`；
9. 成功投影主动删除三个route-only Node字段并设置`explicit_command`；
10. 输入payload和Node metadata不被原地修改；
11. helper不读取Storage、Event、clock、网络或用户内容。

红测试只证明当前缺少合同，不提交临时失败状态。

## 4. Phase 1：纯route handoff合同

新增`src/orchestration/mcp_route_handoff.py`，只实现以下最小单元：

- 闭合常量`mcp_selected_route_not_authorized`；
- 用于区分“pinned字段缺失”和“字段存在但非法”的私有sentinel；
- frozen result对象，只包含`normalized_node_metadata`和可空`rejection_code`；
- `normalize_selected_mcp_route(...)`纯函数。

实现顺序：

1. exact payload判断复用Executor现有语义：keys必须恰为`server_id`，值必须是strip后非空字符串；
2. non-MCP或malformed payload原样返回，不抢占现有Validator/Executor错误；
3. pinned存在时只允许与payload strip值相同；非法或冲突直接拒绝；
4. pinned缺失时在调用方提供的`frozenset[str]`中做成员校验；
5. authority通过后浅拷贝Node metadata，删除三个route-only字段并覆盖binding mode；
6. 不创建audit事件，不解释auto/explicit来源，不读取附件或Task内容。

复杂度约束解释为：helper只处理有界Node metadata并做集合成员判断；不得遍历附件、消息、Tool结果或
其他可增长业务数据。完成后运行Phase 0新增单测和`git diff --check`。

### Checkpoint A

只有纯helper测试全部通过后才进入Service接入。Checkpoint A允许与Phase 2合并提交，不单独提交红测。

## 5. Phase 2：Orchestration唯一接入点

修改`src/orchestration/service.py`的`_execute_node`，不得修改其他业务模块。

### 5.1 authority输入投影

在scheduler选择之前：

1. 从`request.metadata`按key是否存在提取raw pinned ID；
2. 从`request.available_mcp_servers`投影strip后的非空Server ID为`frozenset`；
3. 调用纯route handoff；
4. helper成功时用`dataclasses.replace`生成局部canonical `WorkflowNodePlan`，原Plan不改写；
5. 后续继续走现有`_execution_metadata`和Task assignment authority；因为canonical Node不再包含
   route-only字段，请求级固定ID和显式命令会被既有system metadata规则移除。

### 5.2 authority拒绝收敛

当helper返回`mcp_selected_route_not_authorized`：

1. 不调用scheduler或Executor；
2. 以传入`task_node.status`为expected status执行CAS到FAILED，写`finished_at`；
3. CAS成功后记录只含安全error code的`node.failed`，不记录Server ID、payload或用户文字；
4. 返回failed Node和空output，让现有completion policy收敛Task；
5. CAS失败时重新读取Node，绝不覆盖取消、终态或并发worker的新状态，也不在本worker执行网络调用；
6. 不创建MCP intent/outbox/Call，因此无需新增Storage finalizer。

新增`tests/orchestration/test_mcp_route_handoff_service.py`：

- auto allowlist成功，FakeExecutor收到`explicit_command`；
- explicit pinned成功，Executor输入与auto精确等价；
- allowlist外和pinned冲突时scheduler、Executor调用数均为0；
- 拒绝后required Node和Task均FAILED，无READY/RUNNING残留；
- CAS取消竞争不覆盖CANCELLED Node/Task；
- malformed payload仍进入现有Executor错误路径；
- 非MCP Node行为不变；
- 两个不同Server的MCP Node保留Node数、依赖边和各自Server ID。

### Checkpoint B

定向测试通过后创建实现checkpoint：

```text
fix(mcp): unify selected server route handoff
```

只暂存两份业务源码、Orchestration测试及必要文档，不包含任何既有用户改动。

## 6. Phase 3：恢复与兼容回归

### 6.1 v2恢复

扩充`tests/orchestration/test_mcp_dispatch_resume_v2.py`：

- v2输入仍由envelope/intent固定Server重建；
- Executor看到`mcp_binding_mode=explicit_command`；
- Executor metadata不含`mcp_dispatch_server_id`或显式命令字段；
- dependency projection、attachment snapshot和envelope SHA断言保持原样；
- envelope仍不含metadata、actual Tool I/O或Base64。

### 6.2 approval、legacy与continuation

以回归测试证明而不修改对应源码：

- explicit与auto approval恢复均使用request中的固定Server ID；
- legacy v1 reader行为不变；
- remote continuation没有来源marker时，pending MCP Node通过当前available profiles校验；
- intent/envelope/payload Server冲突在既有恢复authority或route handoff处零网络失败；
- open Interrupt、取消Task和`may_have_dispatched` no-replay行为不变。

### 6.3 prompt-injection边界

复用并补足现有测试：

- API继续剥离用户提供的`mcp_dispatch_server_id`、`mcp_binding_mode`、
  `forced_by_mcp_command`和`mcp_command`；
- Planner payload只能含`server_id`，Planner metadata不能写system route字段；
- Planner即使输出格式合法但不在当前profiles中的Server ID，也会在route handoff被拒绝；
- Coordinator现有owner/available校验测试继续通过，证明第二层authority未被绕过。

本Phase只允许修改测试；若测试要求修改API、Coordinator、Storage或恢复Provider，立即停止并回到设计，
不得扩大实现范围。

## 7. Phase 4：验证、文档与真实smoke

### 7.1 自动验证顺序

1. 新helper和Service接入测试；
2. 完整`tests/orchestration`；
3. MCP dispatch capability测试；
4. MCP integrations测试，证明执行链无源码改动且行为兼容；
5. soft binding、approval、startup recovery相关API测试；
6. 后端`compileall`和相关完整回归；
7. `git diff --check`；
8. 对照实施前status审查最终diff，只暂存计划允许文件。

任何既有失败必须记录测试名、错误和与本轮diff无关的证据；不得把未运行测试写成通过。

### 7.2 本地服务与真实OCR smoke

1. 停止新提交并等待当前本地执行handle收敛；
2. 使用现有本地配置重建并重启backend/frontend，不修改生产环境；
3. 确认backend Ready、frontend可访问及OCR Server仍available；
4. 使用用户提供的2,326,771-byte PNG创建全新auto Task“提取一下图片中的文字”；
5. 如出现Tool approval，等待用户正常批准，不绕过Interrupt/Answer/Grant；
6. 等待Task终态并只读核对：
   - Planner选择的Server属于当次profiles；
   - 仅1个`start_parse_job`业务Call；
   - durable OCR result和最终正文非空；
   - Task、Node、branch、intent、outbox、receipt终态一致；
   - v2 envelope不含Base64、Tool参数或Tool结果；
   - auto没有显式binding badge。

本地smoke失败时保留安全错误码和证据，不自动重放Tool调用，也不修改外部`ocr_mcp`仓库。

### 7.3 文档收敛

实现完成后同步：

- 本设计和实施计划的完成状态、测试数量与真实Task证据；
- `src/orchestration/AGENTS.md`新增route handoff入口说明；
- `docs/AGENTS.md` Future Work；
- `CHANGELOG.md`从“计划/设计”更新为实际实现和验证结果；
- 若目录职责未变化，不修改其他`AGENTS.md`。

真实smoke证据另建文档checkpoint：

```text
docs(mcp): record auto route equivalence smoke
```

## 8. 回滚

- 回滚前停止新MCP提交并等待当前Node收敛；
- 只回滚route handoff、Service唯一调用点及其测试，不回滚数据库或v2 envelope；
- 不删除Task、Interrupt、intent、outbox、Call、receipt或no-replay证据；
- 回滚后auto恢复旧`automatic`执行metadata，explicit保持现有路径；
- 不使用`git reset --hard`、工作树清理或任何可能影响现有用户改动的操作。
