# MCP 五版本 Result 解析实施计划

## 状态

- 日期：2026-08-20
- 分支：`main`
- 设计依据：`2026-08-20-mcp-versioned-result-parsing-design.md`
- 状态：九检查点仓库实现完成；真实PostgreSQL与production rollout待外部执行
- 策略：保留既有安全解析和typed projection合同，删除独立Parser模式参数及旁路分支

## 固定合同

- 五个独立 Decoder 精确覆盖 `2024-11-05`、`2025-03-26`、`2025-06-18`、`2025-11-25`、`2026-07-28`。
- 原始 Result 永远只作为内部 authority；公共 API、前端、prompt、event 和 audit 禁止 raw fallback。
- `MCPCallRecord` 冻结 output schema；terminal 原子固化 result source 与 validated checkpoint digest。
- user/agent projection 使用同一 parsed model，分别执行预算、脱敏和 URI/media policy。
- 启用用户级MCP时Result Parser固定always-on；不存在live safe-hide/shadow旁路，故障处置不得恢复raw展示。

## 执行记录

- Checkpoint 1 已完成：`09f8559 fix(api): safe-hide MCP raw result artifacts`。
- Checkpoint 2 已完成：`38fc16c feat(mcp): persist result parser authority`。
- Checkpoint 3 已完成：`2cd2a41 feat(mcp): add versioned result decoders`。
- Checkpoint 4 已完成：`7f7ca1e feat(mcp): isolate result parsing workers`；backend Linux容器43项全通过，含512 MiB `RLIMIT_AS`、64 MiB raw边界和恶意regex终止。
- Checkpoint 5 已完成：`b98ca52 feat(mcp): enforce parsed result terminal gate`；199项terminal/recovery/runtime聚焦回归通过。
- Checkpoint 6 已完成：`1a879cf feat(mcp): publish bounded result projections`；published projection经Artifact expected-storage-ref CAS绑定，Selector删除raw refs并按最新优先/Call顺序消费有界agent projection，Remote continuation在begin前加载投影且metadata不含raw，OCR改为窄invoker注入，legacy executor改用统一Decoder/projector。
- Checkpoint 7 已完成：`5f5e16d feat(frontend): render typed MCP business results`；API只从复验后的published projection返回strict typed业务视图，前端删除Artifact ID/raw识别并展示structured/preview/text/empty/unavailable闭合卡片，可访问展开控件和显式截断提示已覆盖。
- Checkpoint 8 已完成实现与最终验证：completed Call按ref每页1000条keyset本地扫描，raw resolver优先held durable、回收后读取identity-bound managed copy，source-deleted零网络补投通过；新增闭合Parser count/latency指标与shadow安全摘要，24小时缺projection continuation收敛failed/no-replay，SQLite/PostgreSQL metric constraint执行additive替换；OCR内部start/poll/ack与legacy executor均进入隔离Result Service。独立检查点提交主题为`feat(mcp): reconcile parsed results and finalize rollout gates`。
- Checkpoint 9 已完成：删除`MAF_USER_MCP_RESULT_PARSER_MODE`、`MCPResultParserMode`和Gateway/runtime分支；ordinary、approval、workflow与remote task统一使用强制解析语义，历史`safe_hide`仅作为不可用原因保留。聚焦后端83项、完整MCP 528项中的527项通过（唯一失败为既有shadow manifest错误文案断言）、前端143项/typecheck/build通过。

## 开发检查点

1. **Safe-hide floor**：公共 MCP Artifact 改为 typed unavailable view，`storage_ref=""`，下载保持 404。
2. **Schema/source authority**：descriptor/Call/receipt/candidate v3、SQLite/PostgreSQL additive migration和双读写测试。
3. **Parser contract**：统一模型、strict JSON、五版本 Registry/Decoder、schema校验与projection纯函数。
4. **Isolated worker/store**：1 active、8 queued、2 per owner、30秒queue、10秒worker、Linux 512 MiB；精确discard与task-private projection store。
5. **Terminal gate**：ordinary、approval、MRTR、2025 tasks/result、2026 tasks/get、restart recovery统一进入Result Service。
6. **Projection consumers**：Selector、remote continuation、OCR start/poll/ack与legacy executor只消费有界agent projection。
7. **Typed API/frontend**：strict `MCPBusinessResultView`、业务结果卡、可访问性、live/history一致与无下载旁路。
8. **History/rollout**：本地零网络重投影、keyset/CAS补投、闭合指标、shadow/enforce门禁和safe-hide回滚。
9. **Always-on收敛**：移除Parser模式配置与旁路；live结果始终解析，malformed确定性失败，projection发布失败仍closed unavailable。

每个检查点先补失败测试，再实现和运行聚焦回归，最后创建独立 Git commit。任何检查点失败不得通过放宽authority、重放Tool或恢复raw展示绕过。

## 最终门禁

- 五版本conformance、Gateway/remote/restart等价、streamed `isError=true`、64 MiB资源与恶意schema门禁通过。
- SQLite和真实PostgreSQL migration/terminal transaction通过；缺DSN的skip不算通过。
- Task/Conversation API、前端unit/typecheck/build和泄漏扫描通过。
- Linux容器验证512 MiB address-space hard cap；非Linux结果不替代该证据。
- source-deleted历史raw copy可零网络重投影；失败则保持safe-hide。
- `docker_cmd.md`仍存在、被忽略且未被Git跟踪；不修改或部署`prod`。

## 自主审查结论

本计划经三轮 `document-perfectization`，最终97/100、0 Blocking、0 Major，结论为 **Pass with recorded assumptions**。Linux `RLIMIT_AS`与source-deleted历史零网络重投影已取得仓库证据；真实PostgreSQL DSN仍是唯一未取得的外部门禁，skip不得记作通过。

## 最终验证记录

- Linux backend容器Result Parser/worker/store 43项通过，覆盖64 KiB/64 MiB边界、512 MiB `RLIMIT_AS`与恶意regex终止。
- 最终方案聚焦联测93项通过；新增历史补投、OCR逐步解析、legacy隔离解析、continuation 24小时收敛、SQLite/PostgreSQL schema plan和typed API均通过。
- 前端`artifacts/App` 143项、typecheck与production build通过。
- 分层回归：core 46、storage 370（6项外部skip）、lifecycle 25、orchestration 181、main-agent capability 65、MCP capability 15、observability 35均通过；skill-tool目录当前无可发现unittest。
- integrations 682项中681项通过、2项外部skip，唯一失败是实施前已存在的shadow manifest错误文案断言差异；API 481项中的7个既有Skill/任务时序失败和e2e 2项中的1个late-result audit超时均不经过本Result方案路径，已作为基线缺口保留，未在本任务中修改。
- `MAF_POSTGRES_TEST_DSN`、`CP7_POSTGRES_VALIDATION_DSN`、`MAF_POSTGRES_ROLLOUT_INTEGRATION_TEST_DSN`与`MAF_POSTGRES_ROLLOUT_PERMISSIONS_TEST_DSN`均未配置；真实PostgreSQL migration/transaction gate未运行，不记为通过，也因此不允许进入production parser enforce。
