# MCP 首次生产启用实施计划

依据：[已批准设计](2026-09-18-mcp-first-production-enablement-design.md)。状态：`implementing`。

## 顺序与交付

1. **证据合同和兼容性**：新增部署初始化 source/producer 与独立 `first_enablement` payload，复用现有 evidence 封装、摘要和记录转换。先补充新组合接受/拒绝及旧 payload 摘要不变的测试；普通阶段观察、批准和激活入口继续拒绝首次证据。
2. **数据库约束**：扩展 evidence CHECK 约束的精确组合，更新 schema manifest；交付仅修改这些 CHECK 的事务 SQL。对已有约束升级、重复执行、旧记录兼容和非法组合拒绝进行真实 PostgreSQL 验证；public 表仍为 66。
3. **一次性事务入口**：新增 PostgreSQL 管理函数，检查真实 superuser、预期数据库、目标 MCP 配置、schema/权限及首次空状态。使用数据库级事务锁和固定顺序表锁，在同一事务内写 gate scope/evidence/approval/activation；精确重复执行返回原结果，差异、部分状态和后续 activation 拒绝。
4. **管理命令和常驻边界**：提供只读预检与显式 apply，管理员 DSN 仅从独立环境变量读取，错误只输出安全 code；验证生产配置、主密钥和 Sidecar 信任。常驻 Backend 拒绝该管理员变量。Dockerfile 仅交付固定脚本及 SQL 文件。
5. **回归和故障测试**：在本机新建独立 PostgreSQL 容器，覆盖旧业务数据保留、权限拒绝、非空 MCP 数据、已有记录、配置/发布不匹配、重试、并发、三步写入故障回滚、正常实例租约与安全 blocker；完成相关现有 evidence/rollout/API/schema 测试及变更面静态检查。
6. **源码与镜像**：分阶段提交；将同一业务改动同步 main/prod，保留主工作区已有文档修改及所有受保护部署文件。构建、检查和发布新的 Backend 候选，保留原候选 digest；与已发布 Sidecar 再做兼容和信任检查。
7. **服务器恢复库演练材料**：更新使用固定新工件的恢复库结构演练及首次初始化命令；正式库操作继续由用户逐项执行。恢复库实际输出确认之前，不宣称生产准入或迁移完成。

## 实现位置

- `src/integrations/mcp/rollout_evidence.py`、`observability.py` 与现有 evidence 解析入口：新增类型、校验和历史兼容。
- `src/storage/sqlalchemy_models.py`、`src/state/postgres/runtime_schema.py`：CHECK 与 schema manifest。
- `src/storage/postgres/mcp_first_enablement.py`：独立、显式调用的管理员事务；不进入普通启动路径。
- `scripts/initialize_mcp_first_production.py`、`scripts/postgres/user_mcp_first_enablement_constraints.sql`：运维入口及兼容 DDL。
- `src/api/runtime.py`、`Dockerfile`：拒绝管理员配置进入常驻进程，交付指定运维文件。
- 对应 `tests/integrations/mcp/`、`tests/storage/`、`tests/scripts/`、`tests/api/` 测试与目录索引。

## 验收记录规则

每个阶段记录实际命令和结果；没有实际执行的 PostgreSQL、镜像或服务器检查标记为待执行。测试使用独立临时实例，不复用或停止已有 PostgreSQL 服务。初始化脚本不得执行 schema DDL、删除旧数据、伪造观察证据或调用外部 MCP/LLM 服务。

License Requirement：复用现有依赖，无新增依赖或许可例外。

## 本地实现记录

- 已完成独立 first_enablement payload、原摘要兼容、管理员事务入口、显式 CHECK SQL、常驻管理员凭据拒绝及 Docker 固定文件交付。
- PostgreSQL 17.10 独立测试实例：10 项新增集成测试及 20 项原 rollout 真实角色测试全部通过，无 skip；覆盖保留历史会话/Task、只读预检、重复执行、旧约束升级、错误角色/数据库、并发与写入故障回滚、app 租约和安全阻断。
- evidence/脚本/ledger/schema/API 相关首轮 91 项回归通过；新增 CLI 与 closed payload 专项 8 项通过。最终复核与镜像检查继续进行，服务器恢复库和正式库尚未执行新入口。
