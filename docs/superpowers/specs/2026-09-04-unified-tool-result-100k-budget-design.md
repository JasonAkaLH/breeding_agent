# 主 Agent统一Tool Result 100k预算设计

状态：已批准，待书面复核

目标分支：`main`；不涉及`prod`部署

## 背景

当前Tool Result不存在单一全局字符预算。MCP Result Parser、MCP carrier、MCP Selector、
Legacy Skill和delegated Skill各自存在20,000字符限制；普通Skill v2则优先受128 KiB
AgentItem限制，超限后可通过private transient stage在模型请求时恢复完整结果。真实开发库
证据显示，一次34,148-byte MCP结果已完整持久化，但先被Parser标记
`source_truncated=true`，再被carrier缩短到17,161字符并标记
`carrier_truncated=true`。

## 目标

所有新产生并提供给主Agent、MCP业务卡片或MCP Selector的MCP/Skill Tool Result使用同一
语义预算：

- 每次Tool Call只产生一个逻辑Tool Result；
- 每个Tool Result最多提供100,000个业务内容Unicode code points；
- 不足100,000时完整提供，超过时截断并显式标记；
- schema、状态、SHA、调用标识和其他envelope metadata不计入业务字符预算；
- 同轮多个Tool Result分别拥有100,000字符上限，但仍受模型90%总上下文预算约束。

## 非目标

- 不修改、补投、重建或CAS更新任何历史Tool Result、Artifact、receipt或projection；
- 不重新调用历史Tool；
- 不公开MCP私有raw结果或取消64 MiB raw上限；
- 不扩大PostgreSQL AgentItem的128 KiB持久化上限；
- 不修改数据库schema，不执行数据迁移；
- 不部署`prod`，不修改外部MCP Server或外部Skill源码。

## 统一预算合同

新增单一backend pure policy owner，至少定义：

- `TOOL_RESULT_BUSINESS_MAX_CODE_POINTS = 100_000`；
- 单view编码安全上限1 MiB；
- 同时承载user/agent view的MCP private projection envelope上限2 MiB；
- 稳定截断标记、code-point计数和确定性公平分配helper。

字符预算只统计业务内容字符串，不统计closed envelope。编码上限只作为资源与攻击面门禁，
不得在正常100,000字符Unicode内容之前先行截断；转义密集内容超过编码门禁时仍须显式标记，
禁止静默丢失。

Frontend保留同值常量用于DTO defensive validation，并用跨Backend/Frontend contract fixture防止
100,000值漂移；运行时不新增配置开关。

## Capability安全投影

### MCP

版本化Result Parser继续先完成协议解码、`isError`、output schema、敏感内容清洗和raw SHA
校验，再分别生成最多100,000业务字符的user view与agent projection。user view供前端业务
卡片使用；agent projection供主Agent和Selector使用。三者不得读取或公开raw backend。

`source_truncated`只表示原始安全业务投影超过100,000字符；不再由旧20,000限制触发。

### 普通与Legacy Skill

普通Skill继续优先尝试完整inline。超过128 KiB AgentItem时复用既有private transient stage，
durable Tool Result只保存identity-bound receipt；Context Builder在模型请求前复验并注入最多
100,000字符。Legacy Skill的Artifact-backed路径继续保留Artifact authority，但主Agent视图使用
同一100,000字符policy，不回退到旧20,000 preview策略。

### Delegated Skill

delegated instruction也属于Tool Result，业务instruction body上限提升到100,000字符。超过
AgentItem inline容量时复用private stage/receipt，不扩大数据库行；pinned bundle revision、
profile digest和instruction SHA校验保持不变。

## Private result与单Result身份

保持`1 Tool Call -> 1 Tool Result`和`source_call_item_id`唯一约束。大结果不会拆成多个
AgentItem或伪造多个provider Tool message。

inline安全envelope不超过128 KiB时原样持久化；超过时持久化小型closed receipt。模型请求时，
Context Builder仅在owner/Run/Task/Call/result item/revision/SHA全部匹配后把receipt替换为单个逻辑
Tool Result。MCP复用durable Projection Store；Skill/delegated Skill复用既有transient result
store。仅在现有store无法表达所需binding时扩展窄contract，不新建重复存储系统。

resolver失败时在provider调用前fail closed，不回退读取未验证raw、不重新执行Tool，也不把内部
path、storage key或projection ref发送给模型。

## 同轮多Result与总上下文

Context Builder沿用AgentRun固定模型窗口90%总预算。处理顺序固定为：

1. 生成包含完整当前Tool wave的真实candidate并使用绑定模型token counter预检；
2. 超限时先按现有closed规则压缩旧历史，不压缩当前user正文；
3. 仍超限时，对当前wave使用确定性water-filling：短结果优先完整保留，长结果共享剩余额度；
4. 通过实际token counter对共同字符cap二分，直到完整candidate可放入总预算；
5. 只在本次model request中设置`carrier_truncated=true`，不改写durable Tool Result；
6. 即使最小closed结果集合仍无法放入时，返回既有fatal context错误，不删除某个Result、
   不重放Tool。

结果顺序继续按durable call/item sequence。公平分配不允许因capability类型、文本语言或先后顺序
饿死其中一个Result。

## MCP业务卡片与Selector

前端业务卡片和MCP Selector不再拥有独立20,000字符业务限制。它们与主Agent共享100,000
字符policy：不足即完整提供，超过才截断。前端仍只读取typed business view；Selector仍只读取
identity-bound projection且执行零网络历史恢复。

Selector自己的模型上下文窗口仍是硬上限；超限时复用同一确定性公平分配策略，而不是恢复独立
固定20,000 cap。

## 新旧revision隔离

新writer使用新的Tool Result budget/projection revision。旧revision保持原样只读：

- API和历史读取继续按旧合同展示现存内容；
- historical reprojector将旧revision分类为retired/read-only；
- 不加载raw重建100,000 projection；
- 不删除旧projection、不写unavailable reason、不修改Artifact metadata；
- 新旧reader可以共存，但只有新writer生成100,000合同。

revision升级不得要求数据库schema变化。部署门禁只验证新Task使用新revision，不扫描或修改历史
业务行。

## 安全与失败语义

- MCP协议/schema/`isError`/敏感字段/URL清洗规则保持不变；
- raw MCP结果继续保持private authority和64 MiB上限；
- user/agent/selector projection任一identity、SHA、revision或size校验失败均fail closed；
- 前端投影无效继续显示closed unavailable，不显示raw fallback；
- Selector投影无效不得猜测历史内容；
- staging发布或解析失败不得产生部分Tool Result、重复Call或静默成功；
- 观测只记录capability类别、source/carrier truncation和低敏计数，不记录正文或内部引用。

## 验证

### Pure boundary

- ASCII、中文、emoji和转义密集内容覆盖99,999、100,000、100,001边界；
- metadata不计入业务字符预算；
- 1 MiB view与2 MiB envelope门禁不会提前截断正常Unicode内容；
- source/carrier标记与实际截断严格一致。

### Capability

- MCP main Agent、业务卡片和Selector对同一新结果得到一致100,000语义上限；
- 普通Skill、Legacy Skill和delegated Skill使用同一上限；
- 100,000中文字符通过private receipt注入，durable AgentItem仍小于128 KiB；
- raw、path、storage ref和凭据泄漏扫描为零。

### Context

- 同轮多个长短混合Result执行确定性water-filling，全部保留且顺序稳定；
- 先压缩旧历史，再缩当前wave；
- provider candidate由真实token counter证明不超过Run 90%预算；
- `1 Call -> 1 Result`、唯一约束和provider Tool配对保持。

### Compatibility

- 冻结旧revision fixture逐字节保持；
- 历史扫描为零reprojection、零raw读取、零Artifact CAS和零Tool网络调用；
- SQLite、PostgreSQL和Runtime Sidecar现有Agent contract不需要schema/proto修改；
- 运行相关Backend、Frontend、Context preflight、MCP、Skill和E2E门禁。

## 回滚

回滚只停止新100,000 writer并恢复旧writer版本。新revision结果继续由兼容reader安全读取或按closed
unavailable处理；不得降级解析为旧revision、不得重写历史、不得公开raw。
