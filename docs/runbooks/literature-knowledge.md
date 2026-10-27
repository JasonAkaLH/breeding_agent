# 文献知识库运行与验收交接

## 存储与启动

知识库复用主应用的 SQLAlchemy session factory，新增 `knowledge_library`、`knowledge_document`、`knowledge_import`、`knowledge_library_grant`、`conversation_knowledge_selection` 和 `knowledge_query_scope` 六张表。SQLite 沿用 additive bootstrap；PostgreSQL manifest 为 `maf.postgresql_fresh_runtime_schema.v13`，沿用 schema reconciliation，不重建既有任务表。

原始 PDF、原始 Markdown、PDF 提取的 Markdown 正文和 `index.md` 都保存为本地文件，**不把文件二进制或解析全文写入 SQLite/PostgreSQL**。SQL 中的 `body_ref` / `parsed_body_ref` 仅为相对文件引用，`sha256` / `parsed_body_sha256` 仅为校验值；SQL 继续承担元数据、授权、claim、审核与清理状态。Agent 根据文件索引选文献，在实际模型/压缩请求构造时从文件读取正文；持久工具结果只保存小引用。

正文默认保存在运行数据库路径所在目录的 `knowledge/`，可由 `build_api_runtime(knowledge_store_path=...)` 显式指定。该目录必须持久化；多 worker 使用同一数据库和同一共享持久目录。文件根和正文保持私有权限，不放入 Git、镜像构建上下文或公开静态目录。默认 `runtime/` 已被 Git 与 Docker 排除；自定义目录也须满足这些约束。

部署前在隔离数据库副本验证 additive bootstrap、重复启动幂等、既有业务数据不变；PostgreSQL 应用角色需具备既有 schema bootstrap 所要求的权限及新增表的读写权限。数据库和知识库目录需要一致的受控备份。本次没有对开发或生产数据库执行升级，也没有发布镜像。

ApiRuntime 管理知识库处理服务的启动和关闭。导入提取使用现有主 Agent 模型，不创建新模型客户端体系；处理状态、claim、审核字段、发布进度及清理引用均持久化。重启扫描未完成工作，claim 到期后可重新领取。长文本分段覆盖全文后汇总；失败可重试或人工完善后确认。

## 最小验证

1. 从“用户账户设置 → 文献知识库”建库，混合上传 Markdown/PDF，查看提取正文及字段；PDF 只处理文字层，非空页不可提取则整篇失败，正文未就绪不能编辑审核字段或确认。未确认前 Agent 索引无该文献。
2. 修改并确认后，索引出现已审核条目。`/source` 下载原件并校验原件 SHA；`/content` 始终为 Markdown，PDF 使用解析正文 SHA、原 Markdown 保留 BOM/CRLF 原始字节。两者都认证、no-store；已发布/丢弃草稿不能回读。
3. 向已有用户共享：接收方可读、下载和问答，不能看草稿、维护文献或继续共享；撤权后新读取失败。
4. 选择指定库并立即发送，确认保存完成后才提交问题；保存失败保留输入并阻止发送。运行中修改只影响下一问题。
5. 查看实际工具结果顺序与模型请求，确认先读索引再读正文；长文跟随连续游标，压缩请求也包含实际正文。
6. 替换正文并注入索引或清理失败：操作显示未完成、旧引用不可读；恢复后仅当前正文存在，历史聊天答案保持不变。

自动验收命令与实际结果见[双格式实施证据](../superpowers/specs/2026-10-27-literature-knowledge-pdf-implementation-evidence.md)；原 Markdown 历史证据单独保留。真实 PostgreSQL 并发需要 `MAF_POSTGRES_KNOWLEDGE_TEST_DSN` 或现有隔离测试 DSN；不得使用应用/生产数据库代替。

本次按用户确认，以 SQLite 实测为验收基线；PostgreSQL 仅执行 v13 四个 nullable TEXT 列的静态 reconciliation 合同，未连接实际 PostgreSQL。

PDF 解析固定 pypdf==6.21.2（BSD-3-Clause），plain 文字模式与默认容错读取；保留页序/页界，不重建表格、标题或公式。当前内部上限：原件 20 MiB、2000 页、单内容流 16 MiB、累计内容流 128 MiB、输出/提取文字计数 20 MiB、Form 深度 16。超过上限失败，不截断；上游解压也有自身上限，这些不是独立进程的硬内存限额。复杂排版顺序、伪文字层质量和图片内容仍需对照原件审核。

真实文献模型评测使用 `scripts/evaluate_knowledge.py --help` 查看清单格式，并提供已授权 Markdown/PDF 文献和问题集；先校验清单，显式 `--live` 才执行。记录实际选库、选文献、完整覆盖、引用、延迟及模型调用/token；引用语义还需对照正文人工核验。合成文献和替身测试不作为真实质量结果。

实际文献读取后，Agent 请求会加入仅存内存的连续阅读进度与当前书目。可读文献须按游标读完整个文字正文；最终答案提交前检查正文数字引用、书目与缺字段，最多纠正两次。`knowledge_answer_read_incomplete` / `knowledge_answer_citation_invalid` 表示未通过该检查，无效候选不会发布。授权失效或版本变化须重新读取，不能继续引用旧正文。

上下文预检、压缩准备、模型、工具和最终发布阶段均保持租约；阶段入口先续租，内部 TTL/3 心跳，避免连续短阶段重置计时而失租。元数据结构失败只重提取一次，provider 故障不重试。排查时使用 closed 状态与调用计数，不开启正文或凭据日志。

评测示例（仅对已授权语料/端点）：

```bash
conda run -n multi_agent python scripts/evaluate_knowledge.py --config config.yaml --corpus /path/to/authorized/manifest.json
conda run -n multi_agent python scripts/evaluate_knowledge.py --config config.yaml --corpus /path/to/authorized/manifest.json --context-window 16000 --live
```

`--context-window` 只修改内存配置，原文件不变；报告包含失败/纠正次数和 `citation_format_valid`，仍需人工对照正文检查引用语义。问题集声明必需压缩时，窗口过大而未发生压缩也算验收未通过。300 项受控干扰索引和 300 篇真实 PDF 存储检查分别记录，均不宣称是 300 篇真实模型质量评测。

## 发布与回退边界

后端新增管理/选库接口和三个原生知识库工具，前端应配套发布；保留现有 MCP/Skill、消息与 SSE 合同。先完成隔离环境检查，再按既有发布流程授权部署。

回退前停止知识库写入与处理，收敛活动的知识库 AgentRun，防止旧程序恢复其无法识别的引用。旧 writer 会把 PDF 原件误当 Markdown，不可直接回退继续写入；优先前向修复，不删除 nullable 解析列或任一正文文件。先在隔离副本确认旧程序与新增表、PostgreSQL manifest、任务历史兼容；不兼容时采用前向修复。普通代码回退不删除新增表或文献目录。

备份只用于受控灾难恢复，不提供历史正文功能。恢复可能带回已清除的正文时，须单独确认恢复点和数据影响；不能作为普通功能回退。任何回退或恢复均不复核、改写或删除既有聊天答案。

License Requirement：PDF 使用 pypdf==6.21.2（BSD-3-Clause）；本轮稳定性与引用修复复用现有依赖，无新增许可变化。
