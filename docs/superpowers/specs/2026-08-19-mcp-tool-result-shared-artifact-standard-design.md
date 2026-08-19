# MCP Tool原始返回复用公共Artifact标准设计

## 状态

- 日期：2026-08-19
- 分支：`main`
- 状态：设计已确认，尚未实施
- 用户决策：MCP产物标准直接复用Skill最终进入的公共Artifact标准；成功Call展示完整原始返回，失败或取消Call只展示脱敏错误码
- 范围：用户级`mcp.dispatch`成功Tool Call的durable result投影、补偿与公共Artifact展示

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
`artifact_owned / artifact_promoted`。当前缺口不是Artifact基础设施，而是成功Call提交后从未触发该桥接。

## 核心原则

```text
Skill output_files ─> Skill可信采集 ─┐
                                   ├─> 公共Artifact标准/API/下载/前端卡片
MCP durable result ─> MCP可信投影 ─┘
```

“复用Skill产物标准”指复用最终公开合同和交付链，不指把远端MCP响应伪装成Skill sandbox文件。

## 目标

1. 每个成功且具有authoritative durable result的MCP Tool Call自动生成一个独立原始返回Artifact。
2. MCP Artifact与Skill Artifact使用相同公开字段、Task Artifact API、鉴权下载和前端文件卡片。
3. 普通调用、approval恢复、remote Task、startup recovery使用同一投影合同。
4. promotion失败不改变已经提交的Call/Task终态，不重放Tool网络调用，并可在重启后幂等补投。
5. 当前仍为`retained`且文件未过期的历史成功结果自动补投。
6. 完整Tool原始返回不进入LLM prompt、Node dependency output、event、audit或v2 resume envelope。

## 非目标

- 不把失败或取消Call的远端原始错误体公开给用户。
- 不在对话正文内联完整JSON，不新增JSON查看器或MCP专属前端组件。
- 不改变Selector、Tool参数、approval、Gateway调用、terminal candidate、receipt或no-replay语义。
- 不把多个Call合并为ZIP，也不使用Skill“同会话新产物覆盖旧产物”的规则。
- 不恢复已经被24小时GC删除、缺少authoritative receipt或身份不完整的历史结果。
- 不把Artifact或完整原始返回反向注入Main Agent；现有最多20,000字符的execution-only有界
  projection保持不变。

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
`outputs_dir`或本地脚本信任边界。MCP继续只通过
`MCPDurableResultLifecycleManager.promote_to_artifact()`进入公共Artifact链。该方法必须：

1. 读取lifecycle和authoritative Call；
2. 校验Call为`completed`且receipt与`result_ref`一致；
3. 使用snapshot authority复验owner、Task、Node、Call、size、SHA和store kind；
4. 以no-clobber方式复制到`LocalArtifactFileStore`；
5. 保存公共Artifact；
6. CAS把lifecycle从`retained`切换为`artifact_owned`。

## 正常数据流

```text
Tool返回
→ durable result完整落盘
→ terminal candidate密封
→ Call + receipt + lifecycle原子提交
→ Call authoritative completed
→ promote_to_artifact(result_ref)
→ Artifact保存并标记artifact_owned
→ Coordinator继续原有branch/finalizer
→ Task Artifact API返回公共文件卡片
```

promotion必须发生在terminal commit成功之后、当前执行离开Coordinator之前。它不能进入数据库
terminal事务，也不能在Call authority提交前创建用户可下载文件。

一个branch中的每个成功Call都独立promotion；后续Selector继续执行不会覆盖前一个Artifact。
这里的Call指durable `mcp_call_record`业务Call。OCR job workflow内部的poll/ack属于同一个已批准
Call的受控步骤，不单独生成Artifact；该Call的Artifact保存workflow提交的authoritative最终结果。

## approval、remote与恢复

- **approval恢复**：恢复后的Call仍在普通terminal commit后立即promotion。
- **remote Task**：recovery worker提交terminal receipt后调用同一promotion入口，再触发continuation；
  Artifact失败不得重放remote Tool或阻断既有receipt authority。
- **startup/post-ready**：批量扫描`retained` lifecycle，只对同时具有completed Call、matching receipt和
  可验证snapshot的结果补投；每批最多1,000条，使用keyset分页。
- **历史补投**：当前未过期的`retained / dispatch_resolved`结果使用相同批处理；已删除文件、orphan、
  identity不足或receipt冲突一律不猜测恢复。

补投只执行本地SQL和文件I/O，网络调用增量必须为0。

## 失败与补偿

Tool业务终态优先于Artifact投影：

- terminal commit失败：沿用现有unknown/no-replay或fail-closed路径，不创建Artifact；
- promotion失败：Call/receipt/Task保持原终态，lifecycle保持`retained`，不删除durable result；
- 记录闭合的`mcp.result_artifact_failed`，只包含`reason_code`和`error_type`，不含业务ID或原始内容；
- 当前Task仍可完成，前端不伪造Artifact卡片；
- post-ready reconciler下一轮或重启后重试promotion；
- 并发promotion使用确定性Artifact ID、no-clobber文件写入和lifecycle CAS，精确重复返回同一Artifact；
- 已有Artifact但lifecycle未切换时，先逐字节复验文件再完成CAS；
- Artifact存在但身份、size或SHA不一致时fail closed，不覆盖文件。

失败/取消Call没有completed durable result，不创建原始返回Artifact；前端继续显示现有脱敏错误码。

## 生命周期与删除

- `retained`结果继续受24小时durable GC约束，直到promotion成功；
- promotion成功后转为`artifact_owned`，原durable文件由Artifact接管，不再由durable GC删除；
- MCP Artifact不参与Skill同会话supersede；所有成功Call按Task/Call独立保留；
- conversation强删除、Artifact下载鉴权和公共文件清理继续复用现有Artifact合同；
- 回滚promotion触发器时，已经生成的公共Artifact仍有效，不反向删除或降级lifecycle。

## API与前端

不新增MCP专属API或组件：

- `GET /api/v1/tasks/{task_id}/artifacts`返回每个成功Call对应的公共file Artifact；
- `GET /api/v1/artifacts/{artifact_id}/download`继续做owner鉴权、`nosniff`和attachment下载；
- 历史会话恢复继续通过公共Artifact投影显示文件卡片；
- 前端复用现有`parseFileArtifactDisplays`与文件卡片，显示文件名、大小、摘要和下载按钮；
- 页面不解析或内联完整不可信JSON。

Main Agent仍只接收现有最多20,000字符的有界execution projection。Artifact公开描述和完整原始字节
不作为新的prompt输入，也不要求模型在正文生成裸下载链接。

## 安全不变量

1. 只有completed Call、matching receipt和可验证durable snapshot能生成Artifact。
2. Artifact字节必须与terminal receipt绑定的size和SHA一致。
3. Artifact投影、重试和历史补投不得发起MCP网络调用。
4. 完整raw result和Artifact字节不得进入event、audit、prompt、Node output payload或v2 envelope；
   既有有界execution projection不变。
5. API不得公开内部storage/result/Server/Call authority。
6. 跨用户Task查询和下载继续返回404。
7. 失败/取消远端错误体不得被Artifact化。
8. 64 MiB durable result上限和公共Artifact文件安全检查保持不变。

## 测试与验收

### 单元与集成

- completed result生成确定性文件名、JSON MIME、size、SHA和summary；
- 一个Call一个Artifact，三个成功Call得到三个独立Artifact；
- failed/cancelled/unknown/no-call不生成Artifact；
- receipt、Call、lifecycle或snapshot身份冲突fail closed；
- 重复promotion返回同一Artifact且不重复文件；
- 文件写成功/CAS前崩溃可在重启后逐字节复验并完成；
- terminal commit成功/promotion失败时Call和Task不回滚、不重放网络；
- approval、remote terminal和startup recovery使用同一合同；
- retained历史结果分页补投，deleted/orphan/identity不足跳过或安全拒绝；
- Artifact接管后durable GC不删除文件；
- Skill现有输出、覆盖、ZIP和下载测试保持不变。

### API与前端

- Task Artifact API对N个成功Call返回N个file Artifact；
- `storage_ref`为空且download URL可用；
- 文件内容与原始durable result逐字节一致；
- 跨用户下载404；
- 前端复用文件卡片，无MCP专属解析；
- history恢复仍显示全部MCP文件卡片；
- event、audit、prompt和v2 envelope泄漏扫描不出现完整raw result或Artifact字节。

### 真实smoke

使用用户2,326,771-byte PNG和OCR MCP：

- Task、两个Node、Call、intent、outbox、branch和receipt全部completed；
- 只有1个`start_parse_job`业务Call；
- Task Artifact API包含1个`01-start_parse_job-result.json`；
- 下载大小约50 KiB，SHA与receipt/lifecycle一致；
- 下载内容包含OCR完整原文，而Main Agent正文仍可保持有界摘要；
- v2 envelope不含Base64、Tool参数或Tool结果；
- 重启后Artifact仍可下载，网络调用数不增加。

## 兼容性与发布

- 不新增Storage表或修改terminal aggregate schema；允许新增只读分页查询以枚举可投影lifecycle。
- 不修改Skill公开Artifact合同、现有前端卡片或下载路由。
- 部署后先运行post-ready历史补投，再执行全新MCP smoke；补投不阻塞Ready。
- 本设计只授权`main`开发与本地验证，不授权`prod`部署。

## 回滚

1. 停止新的promotion触发和post-ready补投。
2. 保留已经生成的Artifact与`artifact_owned` lifecycle，不删除文件、不降级数据库状态。
3. 未投影的`retained`结果恢复原24小时GC语义。
4. 不回滚Call、receipt、Task、Node、intent、outbox或no-replay证据。
