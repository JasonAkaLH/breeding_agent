# 全仓业务代码渐进式架构清理 P3 实施计划

## 1. 状态与硬边界

- 日期：2026-08-25
- 分支：`main`
- 状态：`active`
- P3 start commit：`5d7a3606498349ba8338743a7afa26fdefe7078c`
- P3 start tree：`253f97e30d26a2cf87eda974cd5285e7323c3573`
- P3 start tracked set：1049

P3只处理`src/integrations/**`的三类确证问题：Agent Skills纯解析helper复制、MCP附件元数据纯helper复制、Integrations persistence边界仍依赖aggregate或`Any`。P3不拆Gateway/Coordinator/Parser控制流，不迁移P2 Agent authority、P4 wiring或P5 adapters，不修改transport、外部I/O、schema/data、错误、时序、公开合同、`prod`或P0 deferred behavior。

## 2. ai-slop-cleaner finding register

| Finding | 分类 | 证据 | P3处置 |
|---|---|---|---|
| `P3-SKILL-STRING-TUPLE-001` | exact duplication | `contract.py`、`input_schema.py`、`slot_state.py`三份4-statement body完全相同 | 移入单一私有`agent_skills/_parsing.py`，原模块import alias保持调用名 |
| `P3-SKILL-JSON-OBJECT-001` | exact duplication | `execution.py`、`input_resolution.py`、`missing_input_interrupt.py`三份5-statement body完全相同 | 同一私有module复用；保留JSONDecodeError、首尾brace fallback与Mapping gate |
| `P3-MCP-ATTACHMENT-001` | exact duplication | Coordinator与Selector各复制basename/content-type/UTF-8 truncate三个pure helper | 移入单一私有`mcp/_attachment_metadata.py`；两端import alias，输出逐fixture exact |
| `P3-PERSISTENCE-BOUNDARY-001` | boundary violation | 6个P3类显式依赖aggregate `StoragePort`，另有多个`storage: Any`或重复本地method Protocol | 使用P1窄ports和无direct-method本地组合Protocol；执行代码零修改 |
| `P3-RUNTIME-RETAIN-001` | reviewed_no_change | Skill/MCP runtime retain/release body相同，但revision类型、refresh、eviction owner不同 | 不建立跨domain基类；P8前只有证明完整revision语义一致才可复用 |
| `P3-SAFETY-REPORT-001` | reviewed_no_change | Gateway/Coordinator `_report_safety_violation` body相同但各自持有独立detector map与I/O owner | 不抽跨owner helper |
| `P3-MINUTE-BUCKET-001` | reviewed_no_change | Observability/Safety body相同但public/private signature default不同 | 不改变签名或强行统一 |
| `P3-CONTROL-COMPLEXITY-001` | reviewed_no_change | Integrations有90个C901；Coordinator 74/55、Gateway/Parser/secure stores均跨高风险阶段 | 不为指标拆分；只在对应行为锁与平台证据充分的独立后续计划处理 |
| `P3-PARSER-CLEANUP-001` | deferred behavior | cleanup join/terminate/kill可取消 | P3不触及Parser service/worker/cleanup |

P3 AST三语句以上exact duplicate共10组；除上述两类已证实同owner pure helper外，其余短cleaner、runtime state或安全owner差异均不合并。

## 3. Checkpoints

### Checkpoint A：计划与基线

Agent Skills parser/input/slot、Coordinator/fault、Gateway、Selector、durable/CP7 lifecycle、Historical/artifact projection、audit/health/config共168项focused baseline必须先绿。

提交：`docs(cleanup): plan P3 integration boundaries`

### Checkpoint B：Agent Skills解析helper复用

新增`src/integrations/agent_skills/_parsing.py`，只包含：

- `string_tuple(value)`：严格保持None/string/list|tuple|set/other行为、顺序与strip；
- `load_json_object(text)`：严格保持empty、JSON error、首尾brace fallback、non-object error位置与message。

六个原模块使用private alias，删除六份本地body；不修改其它相似但非exact helper。新增直接测试覆盖输入/异常矩阵并证明六个alias指向各自唯一canonical function。

提交：`refactor(skills): reuse parsing helpers`

### Checkpoint C：MCP附件metadata helper复用

新增`src/integrations/mcp/_attachment_metadata.py`，只拥有basename、content type与UTF-8 bounded truncate。Coordinator/Selector保留原private调用名作为import alias，删除六份本地body。测试覆盖路径分隔、control chars、空值、255-byte边界、多字节截断、invalid content type，并证明两端alias identity相同。

不得移动`_mcp_message_attachments`、budget、authority validation或Selector/Coordinator phase。

提交：`refactor(mcp): reuse attachment metadata helpers`

### Checkpoint D：P3采用P1窄Ports

仅改annotation/Protocol declaration，不改调用。覆盖当前已知storage consumers：

- direct：CP7 safety、CP7 terminal lifecycle、MCP observability、health、user config/client、legacy migration apply、shadow compare；
- local composites：Dispatch Coordinator、Gateway、Selector Context、Durable Result lifecycle、Historical reprojection、Result Artifact projection、MCP audit；
- 复用P1 bases重写现有Credential recovery与Remote Task recovery本地Protocol，删除重复method declarations。

新增直接contract test，证明：显式文件不再import aggregate或以`Any`标注storage；组合Protocol无direct async method且继承surface精确；constructor/public function除annotation外签名不变。P4 composition仍传同一concrete storage对象。

提交：`refactor(integrations): adopt narrow persistence ports`

### Checkpoint E：全量门禁与终态handoff

逐域运行Backend canonical；Frontend/Rust、Linux Parser、真实外部MCP均因生产语义未触及记为N/A而非PASS。复跑P0的17 MCP fault boundaries、Gateway/Coordinator exact trace、Historical zero-network。最终生产diff只允许上述Integrations文件和两个新private helper。

同步本计划、`docs/AGENTS.md`、`src/integrations/AGENTS.md`与`CHANGELOG.md`，冻结P4/P5 handoff。

提交：`docs(cleanup): close P3 integration boundaries`

## 4. 必须保持的合同

- Agent Skills schema/value/error/fallback与`skill_artifacts → uploaded_artifacts → artifacts`truthy选择顺序不变；
- 五版本Parser、spawn/pickle、timeout/cleanup/projection保持原位；
- Gateway共享state、endpoint/credential/bootstrap、accepting guards、registration callback和唯一Tool send不变；
- Coordinator reservation→may-have-dispatched→Tool→terminal/no-replay及17 fault boundary count/order不变；
- Temporary Result、Pending Action、Projection、CP7 candidate、credential/master-key、historical managed copy保持独立authority；
- Historical keyset page=1000、held→managed source顺序、network/client/credential调用=0；
- P3→P2 MCP Dispatch imports与functional call sites/kinds/counts/identity delta=0。

## 5. 停止条件

若helper复用改变任一异常类型/message、byte边界、schema/fallback；若port annotation需要修改P4/P5实现；若Gateway/Coordinator/Parser/secure store控制流产生diff；若真实I/O、Tool调用或authority owner改变；立即停止该改动并保留已绿检查点。P3不以减少C901、文件行数或抽象数量作为完成条件。
