# OCR MCP 可信附件工作流与聚合终态修复设计

## 状态

- 日期：2026-08-19
- 适用范围：`main` 分支 user-scoped MCP、外部 `ocr_mcp` source schema、真实 OCR 人工验收
- 决策：采用可信本地附件物化 + 单一逻辑 OCR workflow；不把 OCR 特例塞进恢复信封
- 用户授权：自主设计、实施、复审与验证，直到本地修复完成
- 实施状态：主仓代码、外部`ocr_mcp`严格source schema、自动回归和用户报纸PNG本地真实smoke均已完成；严格schema源码尚未部署到远端OCR endpoint

## 背景与故障证据

最新真实 OCR Task 已成功完成 Server discovery 与 Tool approval，但随后连续 16 次调用
`start_parse_job`。每次 Tool 返回完全相同的 232-byte payload：

```json
{
  "content": [{"type": "text", "text": "INVALID_ARGUMENT: Unsupported source type: None"}],
  "isError": true,
  "structuredContent": {
    "error": {"code": "INVALID_ARGUMENT", "message": "Unsupported source type: None"}
  }
}
```

持久化 action 显示 Selector 只生成了 `source.file_path` 或 `source.file`。OCR MCP 实际只接受
带判别字段的 `base64|shared_volume|url|upload_id|file_uri` source。现有 direct MCP Selector
只看到 basename、MIME 与 size，既没有文件正文，也没有可信远端 handle，因此无法自行构造
合法 source。

同时确认三个独立缺陷：

1. Gateway 把任意 Mapping 结果归一化为 `completed`，忽略标准 MCP `isError=true`。
2. completed result只以 opaque ref进入 Selector；自定义`start/get/ack` job不能靠通用LLM可靠编排。
3. dispatch claim renewer进入后先等待5秒；短Call从不续租，连续多步在30秒后失去finalize authority，
   最终留下Task=`failed`、Node=`running`、outbox=`active`。

旧 OCR Skill 已提供经过测试的正确业务顺序：解析 execution-only artifact、建立合法 source、
`start_parse_job -> get_parse_job -> ack_parse_job`，并将最终OCR正文转成展示artifact。本设计复用
其行为合同，不复制Skill脚本或本地配置。

## 目标

1. `$OCR服务`可对当前消息唯一图片/PDF附件完成真实OCR并返回最终结果。
2. LLM只接收附件安全摘要；附件正文不进入prompt、event、audit或v2恢复信封。
3. 用户批准前不产生任何携带附件正文的外部请求。
4. `isError=true`必须形成失败Call/receipt和单次Task失败，禁止重复相同调用。
5. dispatch claim在短Call、多步Selector和长OCR执行期间持续有效。
6. 任意异常最终收敛Task、Node、branch、intent与outbox，不留下running/active残留。
7. OCR MCP `tools/list`明确发布合法source判别联合，错误字段在出网前被拒绝。

## 非目标

- 不修改64 KiB v2恢复信封合同，不把Tool参数、结果、Base64或附件正文放入信封。
- 不迁移到MCP `2026-07-28`；本轮保持真实服务使用的`2025-11-25`。
- 不实现大于10 MiB文件的companion `/uploads`恢复协议。
- 不把任意第三方MCP Server自动识别成OCR，不依赖显示名称或路由描述猜测能力。
- 不复活或重放已经失败、`may_have_dispatched=true`的旧Task。
- 不修改OCR Skill用户合同或把其本地token/config复制到breeding_agent。

## 方案比较

### A. 审批前调用 `/uploads`

最接近旧Skill，但文件会在用户批准精确Tool action前外传；拒绝。

### B. 审批后 `/uploads`，再改写 `upload_id`

避免提前外传，但会使批准参数指纹、pending payload和实际网络参数不同；若要正确恢复，必须新增
delivery表、上传side-effect状态和materialized-argument authority。范围过大，本轮不选。

### C. 可信本地物化 `base64` + 单一逻辑OCR workflow（采用）

在LLM输出后、fingerprint和approval action封存前，从TaskInputAttachment的持久化source读取正文，
构造OCR MCP已支持的base64 source。实际参数只进入既有32 MiB AES-GCM pending payload；用户批准后，
Gateway在一个逻辑Call内确定性执行start/poll/ack并把最终get结果写入durable result。该方案复用现有
approval、CP7 candidate/receipt、no-replay和64 KiB envelope边界，能覆盖当前2.33 MB图片。

## 架构

### 1. Attachment materializer

新增无网络副作用的纯组件，只在以下闭合条件全部满足时启用：

- explicit MCP binding；
- 当前消息恰有一个active TaskInputAttachment；
- Tool catalog同时包含`start_parse_job|get_parse_job|ack_parse_job|cancel_parse_job`；
- `start_parse_job` input schema声明`source.type=base64`变体；
- 附件为PNG/JPEG/PDF，解码后大小不超过10 MiB；
- source payload正文、size、MIME与SHA和TaskInputAttachment快照一致。

组件忽略Selector提供的`source.file|source.file_path`，构造：

```json
{
  "source": {
    "type": "base64",
    "data": "<execution-only>",
    "mime_type": "image/png",
    "filename": "safe-basename",
    "sha256": "<verified sha256>"
  },
  "result_format": "both",
  "return_markdown": true
}
```

物化发生在fingerprint、pending action、approval与call admission之前，因此批准内容和实际网络参数
完全一致。恢复只读取已加密的pending payload，不重新调用Selector或重新选择附件。

### 2. OCR workflow runner

Gateway仅对上述闭合catalog启用`ocr_async_job_v1` runner。一个逻辑MCP Call内部执行：

```text
start_parse_job(base64 source)
  -> queued/running: get_parse_job(include_result=true, result_format=both), bounded poll
  -> succeeded: retain exact successful get result
  -> ack_parse_job(result_receipt), bounded best-effort cleanup
  -> durable result -> CP7 candidate -> terminal receipt
```

- start/get任一`isError=true`立即抛出确定性remote tool error。
- failed/cancelled/expired/gone为终态失败。
- poll间隔2秒、总时限3600秒；取消时若已有job_id，best-effort调用`cancel_parse_job`。
- ack失败不删除已经durable保存的成功结果，但记录低基数cleanup error。
- 整个workflow只有一个Call budget、一个approval action和一个terminal receipt。
- start已发出后若进程崩溃而无可信终态，沿用unknown/no-replay，不自动重发。

### 3. MCP错误语义

Gateway在写durable result前识别`isError=true`并中止sink，转换为`mcp_tool_error`。Coordinator将其作为
已知终态失败提交candidate/receipt，再由统一finalizer收敛aggregate。失败arguments fingerprint进入
Selector禁重集合；同一Task不得再次发出相同调用。

### 4. Claim与finalizer

- `_maintain_dispatch_claim`启动时立即renew，再进入5秒周期，而不是先等待。
- 每次Selector前主动renew，长Selector期间维持heartbeat。
- finalizer允许持有精确owner/token/revision但刚过期的原worker做terminal CAS；若已有新claim，token或
  revision必然变化并冲突。过期本身不能把已经有可信receipts的aggregate永久留在active。
- generic exception路径必须返回safe capability error或完成terminal writer，不只写Task.failed。

### 5. OCR MCP schema

`StartParseJobRequest.source`改为以`type`判别的Pydantic union：Base64Source、SharedVolumeSource、
UrlSource、UploadIdSource与FileUriSource。`tools/list`因此发布`oneOf`/`discriminator`和各变体required字段；
`file`、`file_path`、缺少type和未知嵌套字段在进入JobManager前失败。

服务继续使用标准MCP Tool execution error：业务校验错误返回`isError=true`。本轮不把自定义OCR job
冒充官方MCP Tasks，也不改变当前远端部署协议版本。

## 数据与隐私边界

| 数据 | LLM prompt | v2 envelope | SQL控制面 | 加密pending文件 | MCP网络 |
|---|---:|---:|---:|---:|---:|
| basename/MIME/size | 是 | 否 | 是 | 是 | 是 |
| attachment ID | 否 | 是 | 是 | 间接绑定 | 否 |
| Base64正文 | 否 | 否 | 既有source authority | 是 | 用户批准后 |
| Tool结果正文 | 否 | 否 | 只存ref/SHA/size | 否 | 接收后写durable store |
| token/headers | 否 | 否 | 仅密文credential | 否 | transport注入 |

审计只记录workflow kind、大小桶、阶段、closed result/error code和耗时，不记录文件名、attachment ID、
job ID、receipt、参数、结果、URL或credential。

## 兼容与回滚

- 非OCR Server、无附件Task、automatic binding和普通Tool继续现有Coordinator路径。
- OCR Skill保持不变，可作为独立参考实现。
- 回滚breeding_agent前必须停止新提交并确认没有active OCR workflow；旧失败Task不重放。
- ocr_mcp schema收紧对合法调用兼容，只拒绝此前本就无法执行的错误source。
- 外部`ocr_mcp` checkout当前无Git metadata；修改前创建0600仓库外备份，交付时列出精确diff与测试。

## 验收

1. materializer单测：真实2.33 MB PNG、SHA/MIME/size、10 MiB边界、多附件、删除/漂移、未知catalog。
2. Gateway单测：start->running->succeeded->ack；isError；terminal job失败；timeout；cancel；ack失败。
3. Coordinator回归：审批前零网络、审批后一次逻辑Call、v2 envelope小于4 KiB且无Base64、失败不重复。
4. SQLite/PostgreSQL聚合测试：短Call也renew；过期旧claim精确terminal CAS；新claim阻止stale finalize。
5. startup/API回归：Task/Node/intent/outbox/branch终态一致，无running/active残留。
6. ocr_mcp schema与contract测试：合法五种source；缺type、file/file_path、额外字段拒绝。
7. 相关backend套件、OCR MCP pytest、`git diff --check`与compile检查。
8. 重建本地backend，用用户提供的报纸PNG创建新`$OCR服务`Task；审批后断言：
   - `start_parse_job`只调用一次；
   - Task成功且OCR结果非空；
   - outbox/intent/Node/branch/Call全部终态；
   - 无`execution_crash`、无重复Call、无信封超限。

## 记录的限制

- 当前直接附件桥接只覆盖单附件且解码后不超过10 MiB；大文件需要后续单独设计可恢复companion upload。
- 逻辑workflow崩溃后安全失败而不恢复远端自定义job；要实现继续轮询，应让OCR Server采用官方MCP Tasks
  或新增durable custom-job binding，不能在本轮静默重发start。
- 本地修改OCR源码不等于远端`175.6.25.109`已部署；真实smoke若仍使用旧远端schema，breeding_agent
  workflow保持兼容，但schema收紧的生产证据需单独部署取得。
