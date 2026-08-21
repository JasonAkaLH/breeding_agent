# Phase 7：破坏性 Schema 删除与最终门禁 PRD

- **日期**：2026-08-22
- **状态**：pending
- **父总纲**：`00-统一同模型AgentLoop总纲PRD.md`
- **上游**：Phase 6必须`cutover_complete`
- **主责需求**：FR-25
- **最终复验**：FR-1～FR-26全部集成证明，不改变原主责归属
- **主责NFR**：无新增主责；负责源设计全部NFR最终集成证明
- **目标结果**：在仓库外备份/恢复演练后删除DAG physical storage/proto合同，完成三backend、后端、Frontend、Rust、文档和受控真实MCP最终门禁。

## 1. 目标与价值

Phase 6已经删除可执行DAG控制面，但旧TaskEdge和DAG-only字段仍增加schema、proto和维护负担，也可能诱导未来代码
重新读取。Phase 7在不可逆边界前验证恢复能力，再物理删除旧合同，使最终仓库只有Agent invocation ledger。

## 2. 进入门禁

- Phase 6状态`cutover_complete`且运行时零DAG读取；
- 全部Agent自动/显式/waiting/recovery/cancel/final路径通过；
- 待删除schema/proto inventory冻结；
- SQLite/PostgreSQL/Runtime Sidecar备份与恢复命令已评审；
- 当前分支是`main`，目标是受控开发环境，不涉及`prod`；
- 根目录`docker_cmd.md`存在性不通过读取内容验证，且任何操作不得影响该文件。

### 2.1 当前证据与受影响模块

| 锚点 | 当前事实 | 本阶段影响 |
|---|---|---|
| `src/core/models.py` | Task含root_node_id，TaskNode含DAG policy字段，TaskEdge存在 | 删除physical core contract，保留invocation字段 |
| `src/storage/sqlite/`、`src/storage/postgres/` | 表、row、repository和manifest保存DAG字段/Edge | 执行受控destructive migration和permission更新 |
| `native/proto/maf/runtime/v1/runtime.proto`、`maf_core_types`、`maf_runtime_sidecar` | Proto/Rust model/SQLite adapter仍声明DAG字段 | 同版本删除并更新contract vectors/client/facade |
| `src/api/dto.py`、Frontend graph types | 客户端仍要求criticality/dependency字段 | DTO固定投影，storage不再保存 |
| storage/Rust/API/Frontend migration tests | 保护当前schema和graph兼容 | 更新为删除/restore/parity和empty-edge证明 |

## 3. 备份与恢复硬门禁

Migration前必须在仓库外生成权限不高于`0600`的备份并验证：

- SQLite canonical数据库完整备份；
- PostgreSQL测试/开发数据库schema与data备份；
- Runtime Sidecar持久化数据库/文件备份；
- 对应commit SHA、schema version、digest、created time和恢复命令；
- 在隔离受控开发目标实际执行restore并运行readiness/contract smoke。

只检查备份文件存在、只打印restore命令、只做dry-run或未验证可读都不构成通过。备份不得放入仓库、Git object、
artifact公开下载或日志；不得包含/触碰`docker_cmd.md`。

## 4. 物理删除范围

### 4.1 Task/TaskNode

删除：

- `Task.root_node_id`；
- `TaskNode.criticality`；
- `TaskNode.dependency_type`；
- `TaskNode.retry_policy`；
- `TaskNode.timeout_policy`；
- `TaskNode.resource_class`。

保留真实调用属性：Capability identity、assigned instance、status、input/output refs、timestamps和Artifact/Event关联。
Capability内部Skill sandbox、MCP call/remote和Provider timeout继续有效，不新增统一外层timeout。

### 4.2 TaskEdge

删除TaskEdge model、table、repository、StoragePort、proto、Rust type、write/read/list path和dependency scheduling引用。
Migration/history tests可保留表名但必须隔离，业务源码不得import。

### 4.3 跨backend

同步更新：

- Python core contracts/models/enums；
- SQLite models/bootstrap/repository/migration；
- PostgreSQL models/schema manifest/reconciler/permissions；
- Runtime Sidecar proto、Rust models/SQLite adapter/gRPC/Python client/facade；
- Rust/Python contract JSON和golden vectors；
- API DTO固定兼容projection；
- Frontend types/tests。

## 5. API兼容

`/graph`继续返回：

```json
{
  "nodes": [
    {
      "node_id": "opaque-invocation-node-id",
      "criticality": "required",
      "dependency_type": "hard"
    }
  ],
  "edges": []
}
```

固定字段由DTO层派生，不来自storage。Route不得调用TaskEdge repository。后续breaking API重命名`/calls`不在本阶段。

## 6. Migration合同

- Migration只面向已确认不需要恢复的旧DAG Task；不推断/转换WorkflowPlan为AgentItems；
- 新Agent Task和AgentRun/Item/Artifact/Event必须完整保留；
- SQLite/PostgreSQL/Rust migration顺序和schema version必须成对；
- migration failure保持pre-migration数据可由备份恢复，不启动不匹配binary；
- downgrade不写反向schema猜测；回滚只能恢复备份和对应Phase 6代码；
- PostgreSQL使用受控角色/锁/事务，permission drift fail closed；
- Sidecar proto兼容边界与binary版本一起切换，不运行混合schema binary。

## 7. 最终静态删除证明

业务源码不得再运行时引用：

- `WorkflowPlan`/`WorkflowNodePlan`；
- `RuntimeReplanner`、Soft Skill/Main Agent replanner；
- `max_replans`/`max_dynamic_nodes`；
- workflow provider/router/expander/validator；
- `main_agent.respond`作为finalizer；
- `mcp_remote_task_continuation_plan`；
- `planner.reasoning_delta`、`soft_skill.reasoning_delta`旧生产/消费路径；
- TaskEdge storage/proto/dependency scheduling；
- DAG-only Task/TaskNode字段；
- planner LLM config/wiring和DAG Prompt措辞。

历史docs/migrations/tests引用必须带明确historical/migration语义，且不能被生产源码import或主索引列为当前入口。

## 8. 功能需求与验收

| ID | Requirement | Acceptance |
|---|---|---|
| AL-P7-01 | 三类仓库外备份实际可恢复。 | 隔离restore后readiness和contract tests通过。 |
| AL-P7-02 | TaskEdge在三backend和proto中物理删除。 | Schema/proto/static scans为零业务引用。 |
| AL-P7-03 | DAG-only Task/TaskNode字段物理删除。 | Python/Rust/DB contracts不再声明，DTO仅固定投影。 |
| AL-P7-04 | Agent数据和外部API保持。 | Existing Agent Task/history/artifact/graph migration tests。 |
| AL-P7-05 | Capability内部真实timeout保持。 | Skill/MCP/Provider timeout回归通过。 |
| AL-P7-06 | 三backend Agent storage parity最终通过。 | SQLite、真实PG、Sidecar/Rust conformance/fault tests。 |
| AL-P7-07 | 全部后端/Frontend/Rust门禁通过。 | 无skip required gate。 |
| AL-P7-08 | Active docs/index不再指导DAG实现。 | PRD inventory全部闭合且AGENTS/CHANGELOG同步。 |
| AL-P7-09 | 受控真实MCP smoke完成或有书面waiver。 | Evidence绑定当前commit/config；否则blocked。 |
| AL-P7-10 | 不宣称`prod`部署或旧Task兼容。 | Final evidence明确main/local边界。 |

## 9. 完整测试与验证

### 9.1 Backend

运行`compileall`和全部tests分层：core、storage、lifecycle、integrations、orchestration/Agent、capabilities、API、e2e、
observability及相关Skill外部兼容测试。任何skip必须分类；真实PG required tests不得skip。

### 9.2 PostgreSQL

使用真实测试DSN运行schema manifest、reconciler、permission、migration、transaction、lease/concurrency和restore tests。
缺DSN保持`blocked`。

### 9.3 Rust

从`scripts/run_rust_quality_gates.py`运行受影响的fmt、clippy、workspace test、deny/contract等required gates。缺工具或使用
`--skip-unavailable`不能记为通过。非默认wheel smoke仅在对应ABI/wheel发布合同受影响时要求。

### 9.4 Frontend

```bash
cd frontend
npm test -- --run
npm run typecheck
npm run build
```

### 9.5 真实MCP

在明确授权、隔离配置下验证discovery、Selector、ordinary Tool、approval或waiting恢复、Result Parser、Artifact和final
answer。Evidence不得包含credential/raw result。缺授权或配置时Phase 7保持`blocked`，除非用户明确批准书面waiver。

## 10. 失败模式

- Backup/restore校验失败：不得执行migration；
- 任一backend schema不一致：不得启动post-migration binary；
- Static scan发现生产读取：回到Phase 6/7修复，不加兼容field；
- Migration中断：停止发布，恢复pre-migration backup和Phase 6代码；
- Required test skip/缺工具/缺DSN：blocked；
- 真实MCP smoke失败：forward fix或回退Phase 6，不伪报complete；
- Active docs仍声明DAG：blocked；
- `docker_cmd.md`意外缺失：先从仓库外安全备份恢复并验证ignored/untracked，再继续。

## 11. 文档与状态收口

- 本目录README改为`complete`并记录每阶段commit/evidence；
- `docs/prd/README.md`和`backend/00`把Agent Loop列为当前基线；
- 受影响旧PRD标记rewrite/superseded/historical；
- 更新src/tests/frontend/native/docs的`AGENTS.md`职责索引；
- CHANGELOG记录删除范围、验证、外部证据和License Requirement；
- 不把本地/CI/测试PG证据称为`prod`。

## 12. 回滚

Phase 7后只允许：

1. 同时恢复Phase 7前SQLite/PostgreSQL/Sidecar备份与Phase 6代码；或
2. 保持新schema并forward fix。

禁止只回退代码、只恢复单backend、自动重建DAG字段、从AgentItems反向猜测WorkflowPlan或恢复旧DAG Task。

## 13. 完成标准

AL-P7-01～10全部通过；FR-1～FR-26和全部NFR最终集成证明闭合；三backend、Backend、Frontend、Rust、static/docs和
真实MCP门禁无未批准缺口；工作树/提交范围可审查。满足后目录状态才能标记`complete`。
