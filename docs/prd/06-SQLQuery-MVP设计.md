# SQLQuery-MVP设计

> 来源：从 `docs/主代理框架PRD.md` 拆分而来，承载 SQLQuery 的数据库接入前提、MVP 任务 DAG、Schema Context Builder 与验收闭环。

## 10.4 当前已知数据库接入参考
仓库当前已有 `src/mysql_engine.py`，其中提供了基于 SQLAlchemy `create_engine` + `QueuePool` 的 MySQL 访问示例，可作为一期 SQLQuery 数据源接入参考。

基于当前仓库事实，需要强调：
- 该示例可作为数据库连通性与连接池参数参考。
- 主代理框架核心业务逻辑仍必须保持 async 边界。
- 正式执行链路中，数据库访问不得直接阻塞事件循环；应通过明确的异步执行边界进行封装。
- 数据库连接信息在正式实现中应通过配置或密钥管理注入，不应以硬编码形式作为长期方案。
- 一期 SQLQuery MVP 默认只允许只读查询，不允许写入、DDL 或高风险 SQL。
- 当前 `src/mysql_engine.py` 只提供数据库连接示例，本身并不构成“只读保证”；正式实现必须引入独立的只读执行约束。
- 正式执行时应优先使用数据库层面的只读账号 / 最小权限账号，而不是仅依赖 SQL 文本校验。
- 当前 SQLQuery 执行链路中仅配置一个 MySQL 账号：`chatu:chatu123`。
- 该账号已确认是只读账号，因此一期不设计多账号切换与权限编排机制。
- 一期仍保留 SQL Guard，作为数据库只读权限之外的第二层保护。

## 10.5 SQLQuery MVP 任务 DAG 细化草案
### 10.5.1 首阶段建议 Capability 拆分
一期 SQLQuery MVP 建议至少拆成以下 capability：
- `sql_query.intent_route`：识别用户是否为数据库查询意图，并提取查询目标
- `sql_query.schema_context_prepare`：获取可用 schema、表、字段、必要约束信息
- `sql_query.sql_generate`：基于自然语言与 schema 生成 SQL 草案
- `sql_query.sql_guard`：校验 SQL 是否只读、是否单语句、是否命中禁用模式
- `sql_query.sql_execute_readonly`：执行只读 SQL 并返回结构化结果
- `sql_query.result_summarize`：将 SQL 结果整理为用户可读回复

其中：
- `sql_query.sql_guard`、`sql_query.sql_execute_readonly` 建议为**专用任务型 Agent / 执行器**
- `sql_query.sql_generate` 可由外部 LLM 支撑
- `sql_query.result_summarize` 可由外部 LLM 或轻量 summarizer 执行

### 10.5.2 首阶段标准 DAG
建议一期标准链路默认生成如下主干 DAG：

1. **节点 A：意图识别与任务路由**
   - capability：`sql_query.intent_route`
   - 作用：判断是否进入 SQLQuery 链路，抽取查询主题、时间范围、维度、指标
   - criticality：`required`

2. **节点 B：Schema 上下文准备**
   - capability：`sql_query.schema_context_prepare`
   - 作用：收集本次任务允许访问的库、表、字段、示例映射、字段说明
   - criticality：`required`
   - 依赖：A

3. **节点 C：SQL 生成**
   - capability：`sql_query.sql_generate`
   - 作用：生成候选 SQL 与生成说明
   - criticality：`required`
   - 依赖：A、B

4. **节点 D：SQL 安全校验**
   - capability：`sql_query.sql_guard`
   - 作用：检查只读约束、语法风险、是否多语句、是否越权访问
   - criticality：`required`
   - 依赖：C

5. **节点 E：SQL 执行**
   - capability：`sql_query.sql_execute_readonly`
   - 作用：执行通过校验的 SQL，返回结果集、行数、执行摘要
   - criticality：`required`
   - 依赖：D

6. **节点 F：结果汇总**
   - capability：`sql_query.result_summarize`
   - 作用：将查询结果转换为用户可读答复
   - criticality：`required`
   - 依赖：E

### 10.5.3 动态扩展节点
在混合型 DAG 模型下，一期允许有限动态扩展，但必须受控。SQLQuery 场景建议只允许以下类型的扩展：
- **B1：Schema 补充节点**：当表/字段信息不足时，补充额外 schema 上下文
- **C1：SQL 重新生成节点**：当 guard 发现 SQL 不安全但可修复时，触发一次受限重生成
- **F1：结果二次解释节点**：当结果集结构复杂时，允许补充一个轻量解释节点

不允许无限循环重生成；一期建议 `sql_generate -> sql_guard` 最多只允许 1 次修复型回路。

### 10.5.4 节点策略建议
| 节点 | criticality | retry | timeout 建议 | 说明 |
|---|---|---|---|---|
| A 意图识别 | required | 0-1 次 | 10s | 失败则任务终止 |
| B Schema 准备 | required | 1 次 | 15s | 可对数据库元信息查询做有限重试 |
| C SQL 生成 | required | 1 次 | 30s | 外部 LLM 超时可重试一次 |
| D SQL Guard | required | 0 次 | 5s | 校验失败不直接重试执行，优先进入一次修复回路 |
| E SQL 执行 | required | 0-1 次 | 60s | 只对可判定的瞬时数据库错误允许有限重试 |
| F 结果汇总 | required | 1 次 | 20s | 汇总失败可回退为原始表格摘要 |

### 10.5.5 SQL 安全边界（首阶段建议）
一期 SQLQuery MVP 建议采用严格白名单策略：
- 仅允许只读查询
- 默认允许：`SELECT`、只读 `WITH ... SELECT`
- 默认拒绝：
  - `INSERT`
  - `UPDATE`
  - `DELETE`
  - `REPLACE`
  - `CREATE / ALTER / DROP / TRUNCATE`
  - 多语句执行
  - 文件导入导出相关语句
  - 显式锁表语句
  - 高风险系统表访问
- 如无法确认安全性，默认拒绝执行并返回审计事件
- 一期不提供人工覆盖、白名单豁免或管理员绕过执行路径

### 10.5.5.1 严格只读防线（必须项）
SQLQuery Capability 在一期必须被定义为**严格只读能力**，不能通过任何执行路径写入数据库。建议采用至少四层防线：

1. **Capability 合约层**
   - `sql_query` 相关 capability 明确定义为 `read_only_query_only`
   - 不存在“查询失败后自动切换为写入型 SQL”的回退路径

2. **SQL Guard 层**
   - 所有 SQL 在进入执行节点前必须经过 `sql_query.sql_guard`
   - 只允许 `SELECT` 与只读 `WITH ... SELECT`
   - 任何无法确定是否安全的 SQL，一律拒绝执行
   - 多语句、DDL、DML、锁表、导入导出语句一律拦截

3. **执行适配层**
   - `sql_query.sql_execute_readonly` 只能接收 guard 已通过的 SQL
   - 执行器本身不得提供绕过 guard 的直接执行入口
   - 若执行前发现 SQL 未携带 guard 通过标记，必须拒绝执行

4. **数据库权限层**
   - 正式运行必须优先使用只读数据库账号或等价最小权限账号
   - 即使上层 guard 失效，也应由数据库权限阻止写入成功

### 10.5.5.2 拒绝策略
对于任何疑似写入、结构变更或高风险操作，一期默认执行以下策略：
- 不执行 SQL
- 输出 `sql_query.sql_guard_blocked` 事件
- 写入 JSONL 审计日志
- 返回明确的能力边界说明：当前 SQLQuery Capability 仅支持只读查询

### 10.5.6 关键产物建议
SQLQuery 链路建议至少沉淀以下 artifact：
- `intent_summary`：本次查询意图摘要
- `schema_context_snapshot`：本次生成 SQL 使用的 schema 摘要
- `generated_sql`：生成后的 SQL 文本
- `guard_report`：SQL 校验结果
- `query_result_preview`：结果预览
- `result_summary`：面向用户的最终汇总文本

### 10.5.7 SQLQuery 专项事件建议
除通用事件外，一期建议补充以下专项事件：
- `sql_query.intent_detected`
- `sql_query.schema_prepared`
- `sql_query.sql_generated`
- `sql_query.sql_guard_passed`
- `sql_query.sql_guard_blocked`
- `sql_query.write_blocked`
- `sql_query.query_executed`
- `sql_query.result_summarized`

### 10.5.8 SQLQuery 失败边界建议
一期建议按以下边界处理失败：
- **Schema 不足**：允许一次补充上下文后重试 SQL 生成
- **SQL 不安全**：不执行，优先进入一次修复型重生成；仍失败则终止任务，并记录阻断审计事件
- **疑似写入 / DDL / 高风险语句**：直接阻断，不进入执行阶段，不允许自动放行
- **数据库瞬时连接错误**：允许有限重试
- **SQL 执行语义错误**：记录审计并终止当前任务
- **结果汇总失败**：允许降级输出结构化原始结果摘要

## 10.6 Schema Context Builder 规则草案
### 10.6.1 目标
Schema Context Builder 负责在 `intent_route` 完成后，根据路由结果、schema profile、用户问题和任务约束，生成一份**最小必要 schema 上下文**提供给 `sql_query.sql_generate` 节点，而不是把整库 schema 原样发送给 LLM。

### 10.6.2 输入
Builder 一期建议接收以下输入：
- `route_id`
- `schema_profile_id`
- 用户原始问题
- 意图识别结果（作物类型、品种名、QTN、基因名、时间范围、指标、维度）
- `routing_rules.yaml`
- `schema_metadata.yaml`
- 当前 SQL policy profile

### 10.6.3 输出
Builder 输出建议至少包含：
- `selected_tables`
- `selected_columns`
- `join_hints`
- `route_description`
- `business_constraints`
- `sql_constraints`
- `context_summary`

该输出会作为 `schema_context_snapshot` artifact 持久化，并作为 `sql_query.sql_generate` 的核心输入之一。

### 10.6.4 一级裁剪：按业务路线裁剪
第一层必须按 `route_id` 裁剪：
- `approval_variety_db` 只允许使用对应 profile 下的审定品种库表
- `genotype_db` 只允许使用基因型数据库相关表
- 若路由未确定，则不生成 SQL，上游先走澄清流程

### 10.6.5 二级裁剪：按子域特征裁剪
在路线内部继续做更细裁剪：
- **审定品种库路线**：优先根据作物识别结果，仅选择该作物对应单表；作物未识别时，不直接放开全部五张表，优先触发澄清
- **基因型数据库路线**：
  - 查品种基础信息时优先 `variety`
  - 查位点 / 基因信息时优先 `qtn`
  - 查品种-位点基因型时组合 `variety_genotype + variety + qtn`
  - 查籼粳成分比例时优先 `rice_comp + variety`

### 10.6.6 三级裁剪：按字段暴露规则裁剪
- 仅选择 `expose_to_llm: true` 的字段进入上下文
- 主键、自增 ID 等对生成 SQL 无帮助的字段可不进入默认上下文
- 大文本字段仅在用户问题与其直接相关时才加入上下文
- 同一张表进入上下文的字段数应受 `max_columns_per_table` 限制

### 10.6.7 Join Hint 规则
- 仅在多表查询确有必要时注入 join hints
- Join hint 必须来自 `schema_metadata.yaml` 的白名单，不允许 LLM自由猜测隐藏关联
- 一期默认优先提供显式 join 对：
  - `variety_genotype.variety_id -> variety.variety_id`
  - `variety_genotype.qtn_id -> qtn.qtn_id`
  - `rice_comp.variety_id -> variety.variety_id`
  - `rice_varieties.ref_var_id -> variety.variety_id`

### 10.6.8 业务约束注入
Builder 输出中应同时注入业务约束，而不只给表字段：
- 当前只支持两条业务路线：审定品种库、基因型数据库
- 审定品种库当前只支持五种作物
- 超出当前支持范围的问题应触发澄清或拒答，而不是强行生成 SQL
- SQLQuery Capability 只支持只读查询

### 10.6.9 SQL 约束注入
Builder 在发给 LLM 的上下文中需要明确附带：
- 仅允许只读 SQL
- 仅允许单语句
- 非聚合查询默认需要 LIMIT
- 不允许访问路由白名单外的表
- 不允许访问系统 schema

### 10.6.10 失败策略
- 路由不明确：返回 `context_build_blocked`，要求上游澄清
- 找不到匹配表：返回 `context_build_blocked`，不进入 SQL 生成
- 可用字段不足：允许一次 schema 补充节点扩展
- join 关系不明确：不猜测 join，优先降级为单表查询或触发澄清

### 10.6.11 建议事件
一期建议补充以下事件：
- `sql_query.context_build_started`
- `sql_query.context_build_completed`
- `sql_query.context_build_blocked`
- `sql_query.context_schema_trimmed`

### 10.6.12 路线级示例
#### 审定品种库路线示例
用户问题：`查询近五年水稻审定品种有哪些`

建议上下文：
- route：`approval_variety_db`
- selected_tables：`rice_varieties`
- selected_columns：`year`, `crop_name`, `variety_name`, `approval_num`, `applicant`, `breeder`
- business_constraints：当前为审定品种库、水稻子域

#### 基因型数据库路线示例
用户问题：`查询品种XX在QTN12位点上的基因型`

建议上下文：
- route：`genotype_db`
- selected_tables：`variety`, `variety_genotype`, `qtn`
- selected_columns：
  - `variety.variety_name`
  - `variety.variety_id`
  - `variety_genotype.variety_id`
  - `variety_genotype.qtn_id`
  - `variety_genotype.genotype`
  - `qtn.qtn_id`
  - `qtn.qtn_seq`
- join_hints：`variety_genotype.variety_id = variety.variety_id`, `variety_genotype.qtn_id = qtn.qtn_id`

### 10.6.13 评分维护策略
Schema Context Builder 的评分体系不应演化成一张难以维护的大型“人工评分表”。一期建议采用：

> **固定评分公式 + 少量可维护配置项**

即：
- 评分逻辑主体固化在代码中
- 人工主要维护少量高价值配置，而不是维护成百上千条问题到表/字段的打分映射

### 10.6.14 不建议采用的大评分表模式
不建议维护如下模式：
- 每类问题模式单独配置表分数
- 每个问法维护一组表级固定分值
- 每次业务变化都需要手工调整大量历史评分配置

原因：
- 用户问法天然多样，问题模式无法稳定穷举
- 维护成本极高
- 一旦 schema 或业务范围变化，规则容易失控

### 10.6.15 推荐维护内容
一期建议真正维护的是以下几类轻量配置：

1. **路由词典**
   - 由 `routing_rules.yaml` 维护
   - 决定问题更接近哪条业务路线

2. **字段别名字典**
   - 维护用户自然语言与字段语义之间的映射
   - 例如“育种者”映射到 `breeder`，“适种区域”映射到 `suitable_area`

3. **表级偏置规则**
   - 仅在少量明确业务场景下提升某些表的优先级
   - 例如在审定品种库路线下命中“玉米”时提升 `corn_varieties`
   - 在基因型数据库路线下命中“QTN”时提升 `qtn` 与 `variety_genotype`

4. **少量可调权重**
   - 例如字段命中权重、表描述命中权重、join 可达性权重
   - 这类权重应保持少量、稳定，不宜频繁人工调参

### 10.6.16 推荐评分来源
Builder 一期建议将评分来源限制在以下几类：
- route 命中
- crop / 子域命中
- 表名命中
- 表描述命中
- 字段名命中
- 字段描述命中
- 字段别名命中
- join hint 可达性
- 表级业务偏置

这意味着评分体系本质上是：
- **结构性分**：来自 schema profile、白名单、`expose_to_llm` 等静态元数据
- **语义提示分**：来自业务词典与字段别名
- **业务偏置分**：来自少量手工维护的偏置规则

### 10.6.17 维护原则
- 优先补充词典、别名和偏置规则，不优先去调复杂权重
- 优先通过真实问例和错误案例来修正规则，而不是预先枚举所有问题模式
- 对高频错例进行增量修正，避免一次性大规模改动评分体系
- 评分规则改动应保留变更理由，必要时补充测试样例或样例集

### 10.6.18 人工维护与协作方式
长期维护中，业务知识主要由产品 / 业务侧提供，系统规则整理和更新可由开发协作完成：
- 业务侧负责提供：字段语义、常见问法、路由错例、结果期望
- 开发侧负责更新：路由词典、字段别名、表偏置规则、Builder 评分实现

也就是说，Builder 评分维护不应依赖“维护一张巨大分数表”，而应依赖：
- 业务知识沉淀
- 真实问例迭代
- 少量规则与偏置更新

### 10.6.19 后续可演进方向
若后续业务规模扩大，可逐步演进到：
- 独立字段别名字典配置文件
- 独立表级偏置规则配置文件
- 基于真实问例的评估样例集
- 更细粒度的评分观测与调优工具

但一期不建议将这些全部前置实现。

### 10.6.20 缓存策略
Schema Context Builder 的缓存策略一期确定为：**B / 只缓存配置加载结果**。

具体含义：
- `routing_rules.yaml`、`schema_metadata.yaml`、`sql_guard_rules.yaml` 的加载结果可在进程内缓存
- 每次请求仍重新执行 schema context 裁剪逻辑
- 一期不依赖最终 `schema context` 结果缓存作为正确性前提
- 后续可在接口层预留结果缓存能力，但不作为一期必需项

### 10.6.21 一期缓存边界
一期建议缓存的对象仅包括：
- 路由规则配置
- schema 元数据配置
- SQL guard 规则配置

一期不建议默认缓存的对象包括：
- 最终 `selected_tables`
- 最终 `selected_columns`
- 最终 `context_summary`
- 针对具体用户问题生成的完整 prompt 输入

### 10.6.22 缓存失效策略
一期建议使用简单、可解释的缓存失效策略：
- 进程启动时加载配置
- 当配置文件版本号变化或文件内容变化时重新加载
- 不依赖复杂的分布式缓存失效广播
- 本地开发阶段可通过重启服务或显式 reload 触发刷新

### 10.6.23 选择该缓存策略的原因
采用“只缓存配置加载结果”而不缓存最终裁剪结果，主要是为了：
- 保持实现简单
- 避免 schema context 结果缓存引入额外失效逻辑
- 保证每次请求都在当前 route / hints / 问题语义下重新裁剪，减少误命中风险
- 在一期阶段优先保证正确性与可解释性，而不是过早追求极致性能

### 10.6.24 字段别名字典落地形式
字段别名字典的落地形式当前确定为：**A / 直接纳入 `schema_metadata.yaml`**。

即：
- 字段别名不额外拆分独立文件
- 由 `schema_metadata.yaml` 统一承载 schema 元数据与字段语义别名
- Builder 在加载 schema metadata 时即可同时获得字段描述、字段暴露规则与字段别名信息

### 10.6.25 字段别名字典建议结构
在 `schema_metadata.yaml` 中，建议为字段增加诸如以下结构：

```yaml
columns:
  breeder:
    sql_type: varchar(200)
    description: 育种者
    expose_to_llm: true
    aliases:
      - 育种者
      - 选育者
      - 选育单位
```

若某些别名更适合表级表达，也可在表级补充轻量别名字段，但一期以字段级别名为主。

### 10.6.26 选择该落地方式的原因
采用字段别名字典内嵌于 `schema_metadata.yaml`，主要考虑：
- 一期配置文件数量尽量少
- 字段语义与字段元数据放在同一位置，维护时更直观
- 当前项目 schema 规模仍在可控范围内，尚未大到必须拆分多份配置

### 10.6.27 后续演进边界
虽然一期将字段别名字典直接放入 `schema_metadata.yaml`，但后续若出现以下情况，可再拆分独立文件：
- 字段别名字典增长过快
- 多人频繁协作维护，导致 schema metadata 变得过大
- 需要对别名字典进行独立版本管理或审核流程

也就是说，一期采用 A，并不阻断后续演进到独立 `field_aliases.yaml` 的可能性。

## 14. MVP 验收闭环
### 14.1 MVP 核心目标
证明主代理框架已经具备“接收对话消息 → 生成 SQLQuery 任务 DAG → 调度多个 capability → 调用外部 LLM 与本地数据库访问适配层 → 持久化状态与记忆 → 流式回传 → 支持硬停止”的完整闭环。

### 14.2 MVP 验收场景一：SQLQuery 标准异步执行闭环
一个用户在已有 `conversation_id` 下发起一条自然语言数据查询请求，例如“统计最近 30 天按地区分组的订单数”，主代理需要：
1. 接收消息并在短时间内返回 `task_id`
2. 读取会话历史记忆与当前会话上下文
3. 生成一个最小可运行 DAG
4. 至少调度 4 类节点：
   - 任务理解 / 路由节点
   - Schema / 查询上下文准备节点
   - SQL 生成与安全校验节点
   - SQL 执行与结果汇总节点
5. 通过外部 LLM 生成 SQL，并通过数据库访问适配层访问 MySQL
6. 一期只允许执行只读 SQL；对非只读语句必须在校验阶段拦截
7. 将生成 SQL、校验结果、执行摘要、节点状态、任务状态、关键产物、最终消息持久化
8. 通过实时事件流向前端回传执行进度
9. 最终返回助手结果，并将任务置为 `completed`

### 14.3 MVP 验收场景二：硬停止闭环
在任务运行过程中，用户发起“停止处理”请求，系统需要：
1. 立即接受取消请求并返回 `cancelling`
2. 阻止新节点启动
3. 终止当前 task context，并向相关运行中节点传播取消
4. 将未启动节点标记为取消阻断
5. 释放本地调度与资源占位
6. 输出结构化取消事件与 JSONL 审计日志
7. 将任务按状态机规则收敛到 `cancelled` 或 `cancellation_partial`
8. 后续同一会话可继续发起新任务，且不复用被取消任务产物

### 14.4 MVP 最低验收标准
- 消息提交接口可稳定返回 `task_id`
- 同一会话串行约束生效
- 任务图可查询
- 至少 1 个外部 LLM 调用节点与 1 个 MySQL 执行节点可被主代理成功调度
- SQLQuery 链路可完成“自然语言 → SQL → 只读校验 → 执行 → 汇总输出”
- 非只读 SQL 能被明确拦截并记录审计事件
- 事件流可稳定输出关键状态变化
- JSONL 日志可按 `conversation_id / task_id` 检索
- Task Context Termination 链路可成功跑通
