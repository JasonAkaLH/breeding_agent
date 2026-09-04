# 主 Agent 统一 Tool Result 100k 字符预算设计

状态：用户已批准设计原则，待书面复核

目标分支：`main`；不涉及 `prod` 部署

## 背景

当前 Tool Result 没有单一全局字符预算。MCP Result Parser、Agent carrier、MCP
Selector、API DTO、Frontend、Legacy Skill 和 delegated Skill 分别存在 20,000 字符或
80,000 UTF-8 bytes 限制。开发库中的一次 MCP 调用已经证明：34,148-byte 原始结果完整落盘，
但业务正文先后被 Parser 和 carrier 的 20,000 字符预算截短；80,000-byte 条件虽然该次没有触发，
却会让中文、emoji 等多字节内容比 ASCII 更早被截断。

本次只统一后续 Tool Result 的业务正文预算。既有历史结果不补投、不重建、不迁移。

## 已批准目标

- 所有新产生并提供给主 Agent、MCP 业务卡片或 MCP Selector 的 MCP / Skill Tool Result，
  每个业务调用最多保留 100,000 个 Unicode code points；
- 100,000 字符以内完整保留，超过才截断，并沿用明确的 truncation 标记；
- 删除所有用于裁剪业务内容的 80,000-byte 判断；中文、emoji 和 ASCII 只按字符数处理；
- 一次业务 Tool Call 对应一个逻辑 Tool Result，不拆成两条 AgentItem 或两条 provider
  Tool message；
- 同轮有多个 Tool Result 时，每个结果先独立应用 100,000 字符上限，再受对应模型的实际
  token 上下文预算约束；
- 只影响新写入结果，不修改历史数据。

## 非目标

- 不删除 MCP raw result 的 64 MiB 输入安全上限；
- 不删除数据库 AgentItem、Projection Store、临时文件、sandbox、RPC 或进程内存的基础设施
  安全上限；
- 不公开 MCP 私有 raw result、内部路径、storage ref、凭据或未清洗字段；
- 不修改数据库 schema，不执行历史数据迁移或历史重投影；
- 不修改外部 MCP Server、外部 Skill 源码或 `prod`。

## 方案选择

采用最小的“统一字符 policy + 删除业务 byte budget”方案，不引入新的 projection revision、
数据库表或并行存储系统。

不采用以下方案：

- 只把 20,000 改成 100,000、保留 80,000 bytes：多字节文本仍会提前截断；
- 删除所有 byte 上限：会连带取消 raw、文件、数据库和进程安全门禁，超出本次目标；
- 为本次改动新建完整 projection/staging 架构：现有 Projection Store 和 transient stage 已能承载
  引用式结果，只需补齐必要的通用绑定。

## 统一字符合同

Backend 增加一个 shared pure policy owner，定义：

```text
TOOL_RESULT_BUSINESS_MAX_CODE_POINTS = 100_000
```

业务字符的计数对象是清洗后实际交给消费者的业务值：

- 文本结果按 Python `len(str)` / JavaScript `Array.from(str).length` 计数；
- structured JSON 按 `ensure_ascii=False` 的 canonical JSON 文本计数，JSON key、数值和结构符号均
  计入，因为它们会实际显示或发送给模型；
- supplemental text 和可展示 metadata 的文本值计入；
- schema、状态、调用 ID、SHA、size、projection revision、truncation flag 等 closed envelope
  metadata 不计入业务正文预算。

Backend 是数值和裁剪语义的权威。Frontend 保留同值 defensive constant，并由合同测试锁定
100,000，运行时不新增配置开关。

这里的旧 20,000 和新 100,000 始终表示 Unicode 字符数，不表示 token 数，也不通过 token
counter 决定单个 Result 的业务上限。token counter 只处理应用字符上限后的完整模型请求是否落入
上下文窗口。

## 删除 80,000-byte 业务裁剪

以下路径删除 80,000 UTF-8 bytes 条件及对应按 bytes 截断逻辑：

- MCP Result Parser 的 user view 与 agent projection；
- Agent Tool Result carrier / model view；
- MCP Selector 历史结果 bundle；
- API DTO 的 MCP business view 校验；
- Frontend MCP business view 校验；
- delegated Skill instruction result。

这些路径只按 100,000 业务字符判断是否截断。不得以序列化字节数、语言或 UTF-8 宽度改变
业务正文长度。

## 基础设施安全上限

基础设施 byte 上限只负责拒绝异常载荷或决定 inline / reference 承载方式，不得裁剪业务正文：

- MCP raw result 继续使用现有 64 MiB 上限；
- AgentItem 继续使用现有 128 KiB 行载荷上限；
- Projection Store、临时结果文件、RPC 和 sandbox 继续有容量门禁。

实现必须用最坏 UTF-8 编码和固定 envelope 开销证明：基础设施正常承载路径能容纳一个已按
100,000 字符裁剪的结果。若 AgentItem 无法 inline，则写入 identity-bound receipt，模型请求时从
既有 private store 复验并恢复同一个逻辑结果。MCP 优先复用 Projection Store；普通/delegated
Skill 优先复用现有 transient result store。必要的 store 容量调整属于基础设施容量，不是第二套
业务预算；容量不足必须 fail closed，不能再次按 bytes 截短正文。

private payload 必须先完整 stage/publish，再提交引用它的 AgentItem。发布失败不得提交 receipt；
AgentItem 已提交后，重试必须复用同一 identity-bound 内容。resolver 复验 owner、Task、Run、Call、
result item、revision 和 SHA；任一不符都在 provider 调用前 fail closed，不回退 raw，也不重放 Tool。

## Capability 行为

### MCP

Result Parser 继续先完成协议解码、`isError`、output schema、敏感内容清洗和 raw SHA 校验，再生成
最多 100,000 业务字符的 user view 与 agent projection。`source_truncated` 只表示安全业务投影超过
100,000 字符。

前端 MCP 业务卡片不再有独立 20,000/80,000 限制，直接展示已验证的 100,000 字符业务 view。
同一个 `mcp.dispatch` 内的每个 completed MCP business Call 保持各自一条 result entry 和各自
100,000 字符上限；顶层仍是与该 Agent Tool Call 配对的一条 closed bundle。

### Skill

普通 Skill、Legacy Skill 和 delegated Skill 都使用同一 100,000 字符 policy。能放入 AgentItem 的
结果继续 inline；不能放入时使用既有 Artifact / transient receipt 路径。delegated instruction
超过 100,000 字符时按统一 policy 截断并标记，不能再因 80,000 bytes 提前失败。普通 Skill 的
transient resolver 和 Legacy Skill 的 Artifact resolver 都必须在注入模型前应用同一个字符 policy，
不得把未裁剪 raw 绕过统一入口直接送给模型。

### MCP Selector

Selector 不再使用独立的 20,000 字符 / 80,000-byte 历史结果总预算。每个已验证 historical MCP
result entry 先按统一 100,000 字符 policy 提供，再由 Selector 自己的模型 token 上下文预算决定
本次请求可注入多少内容。Runtime 必须把 Selector 实际使用的 model edition、context window 和
项目现有 token counter 注入 Selector candidate builder；没有可信 model budget 时 fail closed，
不回退字符或 byte 估算。Selector 继续零网络恢复，只读 identity-bound projection。

## 同轮多结果与 token 上下文

主 Agent 和 Selector 都复用 `src/integrations/token_counter.py` 的现有计数链，不以 UTF-8 bytes
估算 token。配置可用时，该计数器按绑定 model edition 批量调用模型 Provider 的
`POST {base_url}/tokenization`，缓存并汇总每段返回的 `total_tokens`；Provider tokenization 失败时
沿用现有配置决定是否回退本地 `tiktoken`，本次不修改 fallback policy。主 Agent 沿用 AgentRun
固定模型窗口的 90% total-context budget：

1. 构建包含当前 Tool wave 的真实 candidate；
2. 先按现有 closed 规则压缩旧历史，不压缩当前 user 正文；
3. 若仍超限，对当前 wave 的 Tool Result 做确定性公平字符分配；
4. 使用上述现有 token counter 验证 candidate，直到落入预算；
5. 只在本次模型请求标记 `carrier_truncated=true`，不改写 durable 100,000 字符结果；
6. 最小 closed result 集合仍放不下时返回既有 fatal context error，不丢弃某个 Result，也不重放
   Tool。

公平分配保持 result 顺序，短结果优先完整保留，长结果共享剩余 token 空间。一次业务 Call 始终只
对应一个逻辑 Result；裁剪不会拆分消息。

## 兼容、发布与回滚

- 不创建新 projection revision；现有 revision reader 同时接受旧的短结果和新的最长 100,000 字符
  结果；
- 旧结果逐字节保持原样，不读取 raw 重建，不修改 Artifact，不调用远端 Tool；
- rollout 只验证新结果不再出现 80,000-byte 提前截断，并验证 100,000/100,001 字符边界；
- 回滚恢复旧 writer/reader 即可。旧版本可能把新的大 view 安全降级为 unavailable，但不得读取 raw
  或改写历史；
- 不进行数据库 schema/data migration。

单张 MCP 业务卡片最多展示 100,000 业务字符，Frontend 沿用现有展开/折叠卡片直接渲染，不新增
懒加载、分页或二次摘要。API 可以返回该完整 typed view；这是用户明确选择的产品行为。多个历史
Artifact 同时返回造成的响应总量继续由既有 API/HTTP 基础设施门禁处理，但不得在单卡内部恢复
20,000 字符或 80,000-byte 业务裁剪。

## 验证与验收

### 字符边界

- ASCII、中文、emoji、组合字符和转义密集 structured JSON 覆盖 99,999、100,000、100,001；
- 相同 code-point 数的 ASCII、中文和 emoji 得到相同裁剪位置；
- 100,000 字符上限不随 Provider tokenization 返回值变化；
- 业务路径中不存在 80,000-byte 判断或 byte-based truncate helper；
- envelope metadata 不占业务正文 100,000 字符额度。

### 端到端结果

- 新 MCP result 在主 Agent、业务卡片和 Selector 中均遵守统一字符 policy；
- 普通、Legacy、delegated Skill 均遵守统一字符 policy；
- 100,000 中文字符可通过 inline 或 identity-bound receipt 成为一个逻辑 Tool Result，AgentItem
  仍不超过 128 KiB；
- 同轮多个结果在实际 token 预算内确定性收敛，保持顺序且不拆消息；
- 主 Agent 与 Selector 的 token 预算测试覆盖 Provider `/tokenization` 成功、缓存命中和现有配置下
  的 `tiktoken` fallback；
- raw、path、storage ref 和凭据泄漏扫描为零。

### 历史与安全

- 历史 fixture 逐字节不变；
- 零历史 reproject、零 raw 补读、零 Artifact CAS、零数据库迁移、零 Tool 网络重放；
- Projection Store / transient receipt 发布、重试、resolver identity drift 和缺文件均 fail closed；
- Backend 聚焦测试、Frontend artifact 测试、Context preflight、MCP、Skill、API 和 E2E 门禁通过。
