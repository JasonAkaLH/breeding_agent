# OCR MCP 可信附件工作流实施计划

## 实施状态（2026-08-19）

- Phase 0～5代码和文档已完成；外部`ocr_mcp`修改前文件备份位于仓库外权限`0600`目录。
- 自动证据：定向106项、storage 362项、恢复/API 55项、ocr_mcp 57项通过；integrations 632项仅有既有shadow manifest错误文案断言失败。
- Phase 6本地backend重建与用户报纸PNG真实smoke尚未完成，因此当前不宣称最终验收通过或远端OCR源码已部署。

## 1. 完成声明

只有同时满足以下条件才可宣称完成：

- 用户报纸PNG在新Task中经`$OCR服务`取得非空OCR结果；
- approval前携带正文的外部请求数为0；
- start只执行一次，workflow有界poll并完成ack或记录安全cleanup告警；
- 标准`isError=true`形成失败Call/receipt且不重复；
- Task/Node/branch/intent/outbox/Call全部终态一致；
- v2 envelope仍小于4 KiB且不含Base64/Tool I/O；
- breeding_agent定向/相关回归与ocr_mcp pytest通过；
- 未部署的远端OCR源码变更明确记录为缺口，不伪装成生产完成。

## 2. Phase 0：失败回归与纯组件

### 2.1 可信附件物化

新增：

- `src/integrations/mcp/attachment_materialization.py`
- `tests/integrations/mcp/test_attachment_materialization.py`

先写失败测试覆盖：

1. exact OCR catalog + 单一PNG附件生成严格base64 source；
2. 物化后SHA、decoded size、MIME和safe basename一致；
3. 2.33 MB真实测试图片可物化且canonical参数小于32 MiB；
4. 超10 MiB、多附件、删除/空正文、SHA漂移、MIME不支持fail closed；
5. 非OCR catalog、automatic binding和无附件不改写参数；
6. 已物化参数恢复时幂等，不重新选择附件。

实现最小纯函数与closed error codes，不读取网络或credential。

### 2.2 OCR workflow runner

新增：

- `src/integrations/mcp/job_workflows.py`
- `tests/integrations/mcp/test_job_workflows.py`

先写fake adapter测试：

- start queued -> get running -> get succeeded -> ack；
- start/get `isError=true`；
- failed/cancelled/expired/gone；
- RESULT_NOT_READY；
- timeout、取消后cancel_parse_job、ack失败保留成功结果；
- 不接受catalog/schema漂移或未知workflow kind；
- 输出只返回最终get payload，不返回job控制响应。

runner只接收已初始化adapter、exact catalog、批准后的arguments、sleep/clock与结果sink，不读取LLM或SQL。

## 3. Phase 1：Gateway与错误语义

修改：

- `src/integrations/mcp/gateway.py`
- `src/integrations/mcp/gateway_models.py`（仅在需要closed workflow enum时）
- `tests/integrations/mcp/test_user_mcp_gateway.py`

步骤：

1. `call_tool`接受可选closed workflow kind，普通调用默认行为不变。
2. workflow在同一个Gateway call_ref、TaskCallGuard、scope与result sink内运行。
3. 任意raw Tool result在写sink前检查`isError`；true时abort sink并抛确定性remote Tool error。
4. workflow成功仅把最终OCR payload写入durable result；intermediate job控制payload不落业务artifact。
5. workflow取消时best-effort cancel remote job，无法确认则保持现有unknown/no-replay。
6. metrics按一个逻辑业务Call计数；audit不记录job ID、filename或payload。

验证普通Call、MRTR、2025/2026 Tasks和streaming结果测试不回归。

## 4. Phase 2：Coordinator审批与终结

修改：

- `src/integrations/mcp/dispatch_coordinator.py`
- `src/integrations/mcp/selector_context.py`（仅增加closed workflow识别需要的安全字段时）
- `tests/integrations/mcp/test_dispatch_coordinator.py`
- `tests/api/test_user_mcp_recovery_startup.py`

步骤：

1. explicit binding时保留当前消息active附件对象，同时继续只给Selector安全摘要。
2. Selector选中exact OCR start tool后，在fingerprint前调用materializer。
3. approval prompt/pending action/admission/call全部绑定同一物化参数SHA。
4. approval恢复直接读取加密pending payload，不运行Selector、不重新读取附件正文。
5. 调用Gateway时传`ocr_async_job_v1`；成功后直接finish branch，不再进入下一Selector step。
6. remote Tool error转成`mcp_tool_error`并提交failed candidate/receipt/finalizer，禁止重复。
7. `_maintain_dispatch_claim`进入即renew；Selector与长workflow均维持claim。
8. generic异常不得只落`task.failed`；aggregate finalizer必须先完成或由startup确定性收敛。

## 5. Phase 3：SQLite/PostgreSQL finalizer CAS

修改：

- `src/storage/sqlite/repositories.py`
- `src/storage/postgres/repositories.py`
- `tests/storage/test_mcp_dispatch_aggregate_repository.py`
- 相关真实PostgreSQL测试（有DSN才计通过）

测试与实现：

1. 短Call结束也至少续租一次；
2. finalizer持有精确owner/token/revision但lease刚过期时可做terminal CAS；
3. 新worker claim后revision/token变化，旧workerfinalize必须冲突；
4. active/nonterminal Call仍阻止普通失败终结；
5. 已有terminal receipt的failed workflow收敛Node/Task/outbox/intent/branch；
6. startup可修复历史Task failed + Node running + outbox active，不发网络调用。

SQLite/PostgreSQL保持等价合同；不新增表或非必要migration。

## 6. Phase 4：ocr_mcp严格source合同

外部checkout：`/Users/yinpeihai/Code_workspace/ocr_mcp`（当前无Git metadata）。

修改：

- `src/ocr_mcp/schemas.py`
- `src/ocr_mcp/server.py`（仅schema/error返回所需）
- `tests/test_mcp_contract.py`
- `tests/test_sources_and_backend.py`
- `README.md`与`AGENTS.md`索引说明（职责变化时）

先备份将修改文件到仓库外0600目录。先写失败测试，再把`source`改为discriminated union，覆盖五种合法source、
缺type、`file|file_path`、额外字段和错配variant。保持错误以标准`isError=true`返回，不改变现有工具名、
自定义job流程或2025-11-25 wire。

## 7. Phase 5：文档、回归与checkpoint

同步：

- 设计/实施计划状态与最终证据；
- `docs/AGENTS.md` Future Work；
- `src/integrations/AGENTS.md`与`tests/AGENTS.md`入口；
- `CHANGELOG.md`从“仅设计”更新为实际实现/验证状态。

验证顺序：

1. 新纯组件测试；
2. Gateway/Coordinator/aggregate storage/startup定向测试；
3. MCP integrations、storage、API相关套件；
4. compileall与`git diff --check`；
5. ocr_mcp完整pytest；
6. 审阅两checkout最终diff；
7. breeding_agent创建实现checkpoint；ocr_mcp因无Git只交付文件级diff/备份位置。

## 8. Phase 6：真实服务验收

1. 重建`breeding-agent-backend:local`，保留当前持久化master key与真实volume。
2. 重启backend并确认Ready；不重启无关服务。
3. 使用用户提供的PNG创建新Task并绑定新配置的OCR Server。
4. 若需要人工Tool approval，等待用户在前端批准；不得绕过。
5. 观察Task至终态并只读核对：
   - 调用次数；
   - Call/receipt/candidate；
   - workflow result与artifact；
   - Task/Node/branch/intent/outbox；
   - envelope size/forbidden字段；
   - backend/frontend健康。
6. 若真实远端仍返回业务错误，保留完整安全错误码并区分本地修复与远端部署缺口，不重复Tool调用。

## 9. 回滚

- 代码回滚前停止新MCP提交并等待/收敛active workflow。
- 不删除用户附件、对话、Task、receipt或no-replay证据。
- 不回滚v2 envelope或master key。
- OCR源码回退只恢复本次备份的明确文件；不覆盖外部checkout其他用户修改。
