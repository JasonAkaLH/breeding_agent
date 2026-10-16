# 文献知识库运行与验收交接

## 存储与启动

知识库复用主应用的 SQLAlchemy session factory，新增 `knowledge_library`、`knowledge_document`、`knowledge_import`、`knowledge_library_grant`、`conversation_knowledge_selection` 和 `knowledge_query_scope` 六张表。SQLite 沿用 additive bootstrap；PostgreSQL manifest 为 `maf.postgresql_fresh_runtime_schema.v12`，沿用 schema reconciliation，不重建既有任务表。

正文默认保存在运行数据库路径所在目录的 `knowledge/`，可由 `build_api_runtime(knowledge_store_path=...)` 显式指定。该目录必须持久化；多 worker 使用同一数据库和同一共享持久目录。文件根和正文保持私有权限，不放入 Git、镜像构建上下文或公开静态目录。默认 `runtime/` 已被 Git 与 Docker 排除；自定义目录也须满足这些约束。

部署前在隔离数据库副本验证 additive bootstrap、重复启动幂等、既有业务数据不变；PostgreSQL 应用角色需具备既有 schema bootstrap 所要求的权限及新增表的读写权限。数据库和知识库目录需要一致的受控备份。本次没有对开发或生产数据库执行升级，也没有发布镜像。

ApiRuntime 管理知识库处理服务的启动和关闭。导入提取使用现有主 Agent 模型，不创建新模型客户端体系；处理状态、claim、审核字段、发布进度及清理引用均持久化。重启扫描未完成工作，claim 到期后可重新领取。长文本分段覆盖全文后汇总；失败可重试或人工完善后确认。

## 最小验证

1. 从“用户账户设置 → 文献知识库”建库，上传 Markdown，查看原文与提取字段；未确认前 Agent 索引无该文献。
2. 修改并确认后，索引出现已审核条目；下载正文 SHA-256 与上传原始字节一致。
3. 向已有用户共享：接收方可读、下载和问答，不能看草稿、维护文献或继续共享；撤权后新读取失败。
4. 选择指定库并立即发送，确认保存完成后才提交问题；保存失败保留输入并阻止发送。运行中修改只影响下一问题。
5. 查看实际工具结果顺序与模型请求，确认先读索引再读正文；长文跟随连续游标，压缩请求也包含实际正文。
6. 替换正文并注入索引或清理失败：操作显示未完成、旧引用不可读；恢复后仅当前正文存在，历史聊天答案保持不变。

自动验收命令与实际结果见[实施验证记录](../superpowers/specs/2026-10-16-literature-knowledge-implementation-evidence.md)。真实 PostgreSQL 并发需要 `MAF_POSTGRES_KNOWLEDGE_TEST_DSN` 或现有隔离测试 DSN；不得使用应用/生产数据库代替。

真实文献模型评测使用 `scripts/evaluate_knowledge.py --help` 查看清单格式，并提供已授权 Markdown 文献和问题集；先校验清单，显式 `--live` 才执行。记录实际选库、选文献、完整覆盖、引用、延迟及模型调用/token；引用语义还需对照正文人工核验。合成文献和替身测试不作为真实质量结果。

## 发布与回退边界

后端新增管理/选库接口和三个原生知识库工具，前端应配套发布；保留现有 MCP/Skill、消息与 SSE 合同。先完成隔离环境检查，再按既有发布流程授权部署。

回退前停止知识库写入与处理，收敛活动的知识库 AgentRun，防止旧程序恢复其无法识别的引用。先在隔离副本确认旧程序与新增表、PostgreSQL manifest、任务历史兼容；不兼容时采用前向修复。普通代码回退不删除新增表或文献目录。

备份只用于受控灾难恢复，不提供历史正文功能。恢复可能带回已清除的正文时，须单独确认恢复点和数据影响；不能作为普通功能回退。任何回退或恢复均不复核、改写或删除既有聊天答案。

License Requirement：复用现有依赖，无新增第三方依赖或许可变化。
