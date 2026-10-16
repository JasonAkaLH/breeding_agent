# 文献知识库实施验证记录

- 日期：2026-10-16。
- 变更前基线：`main@6db96e1`；既有失败在该 HEAD 的隔离源码副本复现。
- 需求来源：[PRD 24](../../prd/backend/24-文献知识库原生CapabilityPRD.md)。
- 实施依据：[完整实施计划](2026-10-16-literature-knowledge-implementation-plan.md)。
- 当前结论：知识库仓库实现已有确定性自动验收证据；真实 PostgreSQL 和真实育种文献模型评测仍缺环境证据，不能据此宣布 AC-27 或整个 PRD 验收完成。
- 本记录不含文献全文、问题与回答正文、模型完整请求、凭据或本地部署文件内容；没有执行部署或生产数据操作。

## 1. 已执行的知识库链路验证

命令：

```bash
conda run -n multi_agent python -m unittest tests.e2e.test_knowledge_scope_recovery tests.e2e.test_knowledge_flow tests.scripts.test_evaluate_knowledge
conda run -n multi_agent ruff check scripts/evaluate_knowledge.py tests/e2e/test_knowledge_flow.py tests/e2e/test_knowledge_scope_recovery.py tests/scripts/test_evaluate_knowledge.py
```

结果：10 项测试通过；上述文件 Ruff 通过。测试使用可重复的合成文献和模型替身，不将其等同于真实模型的引用质量评测。

- 实际管理 API 完成上传、提取、人工确认和带 BOM/CRLF 原始字节下载，下载 SHA-256 与上传一致。
- 模型替身接收真实 `AgentModelRequest`，根据实际工具结果驱动 `list_libraries → read_index → read_document`，取得索引之外的前段和末段实验细节；最终引用使用工具返回的书目信息。
- 长索引和超过 120,000 字符的正文触发多页读取，逐页核对起止位置、连续性、总长度、末段和游标；模型实际请求及上下文计数输入均包含正文。
- 降低该测试模型的上下文窗口，实际触发闭合历史压缩；压缩模型实际请求包含前段独有事实，压缩后综合回答仍使用该事实及正确书目。没有仅检查持久化小引用或最终文字。
- 工具结果的持久化 `AgentItem` 只包含引用，保持 131,072-byte 合同；正文段落不出现在工具结果或审计日志，不生成文献工具 Artifact。最终普通回答沿用原有最终文本 Artifact 行为。
- 删除正文后下载不可用，已经输出的聊天答案和引用保持逐字段不变。
- 使用既有已编译 Rust Sidecar，经过真实协议握手，在 `off/shadow/enforce` 三模式完成准入、改变选库、重启实际 Sidecar 进程和 Python 存储、重放同一消息、从 canonical `Task.root_message_id` 恢复原范围；下一问题采用新选择，`enforce` 没有 SQL Task 投影。
- 三模式测试只在独立临时 Sidecar SQLite 中准备 finalization fixture，沿用现有客户端集成测试模式；它不是生产迁移、签封或切换演练。

## 2. 需求与验收证据定位

下表为证据入口，须结合第 3 节实际执行结果与第 4 节缺口阅读。列出的测试覆盖代码行为，不自动证明真实模型行为或未运行的平台检查。

| 验收 | 实现或自动验收入口 | 证据边界 |
|---|---|---|
| AC-01 | 前端账户设置入口、知识库管理组件；`test_knowledge_results` catalog 用例 | 前端结果见第 3 节；三个 Agent 工具均只读。 |
| AC-02 | `test_knowledge_routes`；`test_knowledge_repository` 会话删除独立性 | 库元信息和持久范围；文献库不随会话删除。 |
| AC-03 | `test_extraction` 全分段/front matter 优先；`test_knowledge_flow` 首尾细节 | 模型替身证明全文进入提取路径；真实育种模型简述质量待评测。 |
| AC-04 | `test_library_service` 人工审核；`test_knowledge_routes`；端到端发布门禁 | 未审核不对 reader/Agent 发布。 |
| AC-05 | `test_library_service`/`test_knowledge_routes` 批量结果；管理 UI 测试 | 各项成功和失败独立保留。 |
| AC-06 | `test_knowledge_files` 原始字节；`test_knowledge_flow` 下载 | BOM、换行不归一化，下载校验真实原始 SHA-256。 |
| AC-07 | `test_knowledge_repository` 同库跨草稿/文献并发判重；service 批量用例 | 同库原始字节身份受后端事务约束。 |
| AC-08 | `test_library_service` 不同原始字节；repository 判重用例 | 不依赖标题或 DOI 判等。 |
| AC-09 | service 跨库重复与替换；file store 跨库独立性 | 一个库删除、替换不影响其他库副本。 |
| AC-10 | service 替换/清理失败恢复/指针切换后退出；repository CAS | 仅当前正文可读，旧正文待清理不假报成功。 |
| AC-11 | service 删除失败重启恢复；file store 删除标记/临时文件用例 | 禁止迟到上传复活，保留可恢复状态。 |
| AC-12 | service 索引失败与修复；repository/file store 索引修订并发 | 物理索引原子发布，旧修订不能迟到覆盖。 |
| AC-13 | service/API 既有用户共享与幂等 | 不创建虚拟用户。 |
| AC-14 | `test_knowledge_routes` owner/reader 路径；管理 UI 权限测试 | reader 不可读取草稿、共享名单或执行维护。 |
| AC-15 | service 授权；`test_knowledge_results` 实时 ACL；API 直接访问 | 撤权后不复用旧成功正文。 |
| AC-16 | `test_access` 主体解析器替身；API 拒绝未知主体 | 只预留授权接口，不建团队功能。 |
| AC-17 | `test_knowledge_selection`；前端选库测试 | 会话持久偏好、首次提问前保存、显式自动模式。 |
| AC-18 | `test_knowledge_selection`；`test_knowledge_scope_recovery` 三模式真实 Sidecar | 已固定范围不从新偏好扩容；恢复以 canonical root message 取得。 |
| AC-19 | service/API 部分及全部失效用例；前端失效提示 | 无静默扩大到其他库。 |
| AC-20 | `test_knowledge_results` catalog/策略/执行/投影；观测回归 | 独立 knowledge 类型，不借 Skill/MCP 包装。 |
| AC-21 | `test_knowledge_flow` 实际模型请求顺序 | 确定性替身通过；真实模型质量仍待第 4 节评测。 |
| AC-22 | `test_knowledge_results` UTF-8 连续分页；长索引/正文端到端 | 真实请求有正文，按展开内容计数。 |
| AC-23 | `test_knowledge_results` 版本变化/旧游标/旧引用 | 不混合不同 hash 内容，不恢复旧文件。 |
| AC-24 | `test_knowledge_flow` 书目、引用与通用补充；知识库提示词 | 真实模型引用语义仍须问题集评测及人工核验。 |
| AC-25 | `test_knowledge_flow` 删除前后历史逐字段一致；service 清理 | 答案为普通聊天文本，无运行时文献关联。 |
| AC-26 | service 提取失败、人工完善、丢弃中提取、并发确认、发布中断 | 持久化状态及 CAS；真实 PostgreSQL 并发检查待环境。 |
| AC-27 | 第 3 节分层回归、第 4 节缺口 | 未整体关闭，不能将 skip 或基线失败记作通过。 |
| AC-28 | `test_knowledge_results` 新调用跳过旧复用、授权与版本重验 | 同参新读取得当前内容，旧成功不绕过 ACL。 |
| AC-29 | `test_knowledge_flow` 实际必然压缩；`test_knowledge_results` 压缩请求与计数 | 替身验证真实控制和请求链；真实模型前段事实保留仍待评测。 |

FR-01 至 FR-18 和 NFR-01 至 NFR-05 仍按实施计划第 5 节映射；本次未删减或延后已确认需求。NFR-01 至 NFR-04 有仓库自动验收入口，NFR-05 与 AC-27 的真实环境部分仍未闭合。

## 3. 分层回归结果

| 检查 | 实际结果 | 说明 |
|---|---|---|
| 核心合同 `tests/core` discover | 54 项，3 失败 | 三项完全相同的失败已在变更前 HEAD 的隔离源码副本复现，为现有 StoragePort exact snapshot 与 `list_skill_recovery_candidate_task_ids` 的差异；未扩大范围修复。 |
| 生命周期 `tests/lifecycle` discover | 48 项通过 | 普通 lifecycle 回归。 |
| 编排 `tests/orchestration` discover | 207 项通过 | 含新增 9 项 knowledge 结果/上下文/压缩/重读测试。 |
| 能力 `tests/capabilities` discover | 22 项通过 | 含 knowledge 域测试和状态日志隐私回归；确保子目录参与 discover。 |
| 观测 `tests/observability` discover | 41 项通过 | 既有观测合同。 |
| 集成 `tests/integrations` discover | 835 项，1 失败、2 跳过 | 唯一失败为已有 MCP transport family 测试；已在变更前 HEAD 的隔离源码副本复现同一失败。两个 Linux 进程限制/正则终止 gate 在 macOS 跳过。 |
| 端到端 `tests/e2e` discover | 18 项通过 | 最终模型提示词调整后再次全量运行，9.654 秒通过；覆盖知识库、既有普通聊天、Skill、MCP 与取消路径。 |
| 既有真实 Sidecar gRPC 客户端模块 | 18 项通过 | 含真实 SQLite 往返、近 49 MiB submission、Unix socket 与 mTLS；不重复编译 binary。 |
| 既有真实 Sidecar Agent repository 模块 | 6 项通过 | 实际 Sidecar Agent 权威。 |
| PostgreSQL admission/preparation/conversation memory 三模块 | 0 项执行，3 个模块跳过 | 未配置模块专用/回退隔离 DSN。 |
| 新增知识库 PostgreSQL 模块 | 模块跳过 | 实际报告 `maf_postgres_knowledge_test_dsn_not_configured`；未把继承的并发合同记为已运行。 |
| 前端完整测试 | 27 个文件、374 项通过 | 完整运行后的小范围修改另以管理面板 9 项、选库 hook 6 项、client 5 项和 App 知识库/interrupt/竞态子集 27 项重验，均通过。 |
| 前端类型、构建及差异检查 | 通过 | `typecheck`、`build` 和差异空白检查；未执行部署。 |
| 选库名称最终补验 | 1 项通过 | 选库摘要展示已选库名称，且延迟保存期间 Enter 不会提前提交；过滤运行的其余用例不算执行通过。 |
| API 聚焦合同回归 | 27 项通过 | 管理路由/选库、runtime public surface、路由参数约束和 capability 列表；另已修复小模型窗口 fixture 的预算回归，对应测试及 knowledge 9 项共 10 项通过。 |
| API 最终完整回归 `tests/api` discover | 672 项通过 | 109.201 秒，新增管理/选库合同及原有 API 均通过。 |
| 存储最终完整回归 `tests/storage` discover | 共 587 项，16 跳过，其余通过 | 46.535 秒；PostgreSQL 等外部环境 skip 保留，不当作已执行。 |

Sidecar 集成测试只将现有测试的 `_ensure_*_binary` helper 替换为返回本地已编译 debug binary，保留真实进程、真实协议和兼容握手，没有 mock RPC，也没有另起 Cargo 门禁。完整集成执行使用的两个 debug binary 均存在。没有修改 Rust 合同。

另以 `-v` 单独运行知识库 PostgreSQL 模块和两个 Linux 专用用例，实际分别得到 `maf_postgres_knowledge_test_dsn_not_configured`、`Linux RLIMIT gate` 和 `Linux terminable regex gate` 的 skip 原因。

基线失败复现：将变更前 HEAD 的 `src/` 与 `tests/` 提取到独立临时目录，在相同 `multi_agent` 解释器中执行相同核心合同用例及 `tests.integrations.test_mcp_client.MCPClientTests.test_client_rejects_negotiated_version_incompatible_with_transport_family`，保留相同断言失败。基线复现不等于全量门禁通过。

## 4. 实际环境缺口与重跑方式

1. 当前没有模块专用 PostgreSQL 测试 DSN，也没有回退 `MAF_POSTGRES_TEST_DSN`。新增 `tests/storage/test_knowledge_postgres_integration.py` 及既有相关 PostgreSQL 并发、schema/权限和事务用例需要在明确隔离的测试库重跑，不能使用应用或生产数据库代替。
2. 没有提供获准用于评测的真实育种 Markdown 文献集合与预期证据问题集。没有执行真实 provider 文献问答，也没有承诺选库准确率、引用准确率或性能。根目录存在本地模型配置文件只说明文件存在，不等于真实评测已经完成。
3. `scripts/evaluate_knowledge.py` 提供显式配置和 corpus 清单入口：默认只校验清单，只有 `--live` 才在临时本地 SQLite/文献根中提取、确认和问答。报告只记录安全 ID、实际选库与选文献、完整阅读、事实匹配、引用标号和书目标题检查、延迟、调用量及模型返回的 token 可用性；不输出全文、问题、答案、请求、配置或宿主路径。文本提取适配层不返回 usage 时明确记为不可得。真实答案的引用语义及未支持结论仍须人工核验。
4. 真实文献清单应覆盖每库几百篇、多个主题、相近标题、缺失字段、跨库重复、长文和空库。问题提供预期文献及正文事实，至少一个问题要求实际压缩。没有满足这些条件时，单个 smoke 不能替代 NFR-05。

评测清单为 JSON，包含 `libraries[{id,name,description,documents[{id,path}]}]` 和 `questions[{id,text,library_ids,expected_document_ids,expected_facts,require_compaction}]`。路径相对于清单目录，不能越出该目录。ID 使用字母、数字、下划线或连字符，报告只使用这些 ID。

```bash
conda run -n multi_agent python scripts/evaluate_knowledge.py --config /explicit/authorized/llm.yaml --corpus /explicit/authorized/corpus/manifest.json
conda run -n multi_agent python scripts/evaluate_knowledge.py --config /explicit/authorized/llm.yaml --corpus /explicit/authorized/corpus/manifest.json --live
```

以上为重跑格式示例，并非已执行命令或有效本地路径；不读取受保护部署文件寻找凭据。

License Requirement：本记录、测试与评测脚本复用现有依赖，无新增第三方代码或许可变化。
