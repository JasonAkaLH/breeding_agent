# MCP 五版本 Result 解析实施计划

## 状态

- 日期：2026-08-20
- 分支：`main`
- 设计依据：`2026-08-20-mcp-versioned-result-parsing-design.md`
- 状态：执行中
- 策略：safe-hide 先行，随后依次落地 authority、Decoder、隔离 worker、terminal gate、typed projection、历史补投和 rollout

## 固定合同

- 五个独立 Decoder 精确覆盖 `2024-11-05`、`2025-03-26`、`2025-06-18`、`2025-11-25`、`2026-07-28`。
- 原始 Result 永远只作为内部 authority；公共 API、前端、prompt、event 和 audit 禁止 raw fallback。
- `MCPCallRecord` 冻结 output schema；terminal 原子固化 result source 与 validated checkpoint digest。
- user/agent projection 使用同一 parsed model，分别执行预算、脱敏和 URI/media policy。
- rollout 只允许 `safe_hide | shadow | enforce`；缺失或未知配置按 `safe_hide`，回滚不得恢复 raw 展示。

## 开发检查点

1. **Safe-hide floor**：公共 MCP Artifact 改为 typed unavailable view，`storage_ref=""`，下载保持 404。
2. **Schema/source authority**：descriptor/Call/receipt/candidate v3、SQLite/PostgreSQL additive migration和双读写测试。
3. **Parser contract**：统一模型、strict JSON、五版本 Registry/Decoder、schema校验与projection纯函数。
4. **Isolated worker/store**：1 active、8 queued、2 per owner、30秒queue、10秒worker、Linux 512 MiB；精确discard与task-private projection store。
5. **Terminal gate**：ordinary、approval、MRTR、2025 tasks/result、2026 tasks/get、restart recovery统一进入Result Service。
6. **Projection consumers**：Selector、remote continuation、OCR start/poll/ack与legacy executor只消费有界agent projection。
7. **Typed API/frontend**：strict `MCPBusinessResultView`、业务结果卡、可访问性、live/history一致与无下载旁路。
8. **History/rollout**：本地零网络重投影、keyset/CAS补投、闭合指标、shadow/enforce门禁和safe-hide回滚。

每个检查点先补失败测试，再实现和运行聚焦回归，最后创建独立 Git commit。任何检查点失败不得通过放宽authority、重放Tool或恢复raw展示绕过。

## 最终门禁

- 五版本conformance、Gateway/remote/restart等价、streamed `isError=true`、64 MiB资源与恶意schema门禁通过。
- SQLite和真实PostgreSQL migration/terminal transaction通过；缺DSN的skip不算通过。
- Task/Conversation API、前端unit/typecheck/build和泄漏扫描通过。
- Linux容器验证512 MiB address-space hard cap；非Linux结果不替代该证据。
- source-deleted历史raw copy可零网络重投影；失败则保持safe-hide。
- `docker_cmd.md`仍存在、被忽略且未被Git跟踪；不修改或部署`prod`。

## 自主审查结论

本计划经三轮 `document-perfectization`，最终97/100、0 Blocking、0 Major，结论为 **Pass with recorded assumptions**。剩余三项实施期门禁是Linux `RLIMIT_AS`、真实PostgreSQL和source-deleted历史重投影证据。
