# 阶段五：灰度发布、审计与回归门禁 PRD

- **编号**：后端 PRD 21-Phase 5
- **日期**：2026-06-19
- **状态**：待实施
- **前置阶段**：阶段四 selector 消歧、interrupt 与绑定
- **目标模块**：runtime feature flags、audit sink、API/frontend docs、release checks、rollout/rollback config

## 1. 阶段目标

完成 `file_upload` history 与智能选择能力的发布收口：补齐审计事件、`shadow` / `enforce_narrow` / `enforce_guarded_multi` 配置、guarded multi-select 后续灰度策略、端到端验收矩阵、文档索引和回滚口径，确保能力可灰度、可诊断、可回滚。

## 2. 范围

### In scope

- 标准化所有 conversation file audit 事件与安全 payload。
- 定义 selector `disabled | shadow | enforce_narrow | enforce_guarded_multi` 等 rollout 模式。
- 明确 guarded multi-select 的启用门禁与默认关闭策略。
- 补齐端到端测试、前端 typecheck、API contract、Skill parser / builder 回归。
- 更新用户 / 开发文档、PRD 索引、API 文档和 release notes。
- 定义 rollback 行为：关闭 selector 强制模式后保留 file_upload history 展示，不影响上传和 conversation file context 主路径。

### Out of scope

- 不默认放量 guarded multi-select。
- 不引入跨会话文件索引、向量检索或新公开 API。
- 不要求 backfill 历史上传 file_upload message。

## 3. 审计事件

新增或保留 audit-only 事件：

```text
conversation_file.file_upload_message_upserted
conversation_file.file_upload_message_marked_deleted
conversation_file.file_upload_index_repair_required
conversation_file.file_selector_invoked
conversation_file.file_selector_decision_recorded
conversation_file.file_selector_invalid_output
conversation_file.file_selector_clarification_requested
conversation_file.file_selector_resumed_from_interrupt
conversation_file.file_selector_auto_bound
```

事件只保存结构化摘要：

- task_id / conversation_id / node_id；
- upload_id / selected_upload_ids；
- selector 触发原因和 `requirement_profile` 摘要；
- candidate 数量、candidate upload_id 列表或 hash、安全元数据摘要；
- decision / confidence / reason_code；
- 是否进入澄清、澄清候选数量；
- 降级或 repair 原因。

事件不得保存完整 LLM prompt、文件正文、`content_base64`、`storage_key`、本地路径、secret 或 provider raw prompt。

## 4. Rollout 模式

| 模式 | 行为 | 用途 |
| --- | --- | --- |
| `disabled` | 不调用 selector；保留 conversation file context 默认可用。 | 紧急回滚 / 默认安全路径 |
| `shadow` | 计算 profile、candidate、would-decision，只写 audit，不改变执行。 | 阶段三观测 |
| `enforce_narrow` | 仅 required file、明确文件指代、同名多候选、recent usage continuation、upload_id exact 等 narrow 场景生效。 | 阶段四默认模式 |
| `enforce_guarded_multi` | 在 narrow 基础上允许满足 allow_multiple / 明确比较合并意图的 select_many 自动绑定。 | 后续灰度，默认关闭 |

运行时配置必须只接受上述四个 mode；旧值 `enforce` 不保留兼容 alias，避免误解为普通文件上下文场景也会强制 selector。若部署环境仍配置旧值，启动校验或配置解析必须 fail closed 到 `disabled` 并记录配置错误。

`upload_id exact` 在发布门禁中固定为当前生成规则 `upl-` + 12 位十六进制字符；正文识别正则为 `(?<![A-Za-z0-9_-])upl-[0-9a-fA-F]{12}(?![A-Za-z0-9_-])`。

## 5. Guarded multi-select 门禁

`select_many` 自动绑定默认关闭。开启 `enforce_guarded_multi` 前必须满足：

1. `FileRequirementProfile.allow_multiple=true` 或用户明确要求比较 / 合并多个文件。
2. 所有候选完整参与判断，不能基于截断 shortlist 自动绑定。
3. 所有 selected ids 都 active、同 conversation/user、权限校验通过。
4. 下游 Skill /主代理路径明确支持多文件输入。
5. 有单独测试覆盖自动多选、澄清确认、超限、deleted 混入、sheet selection 组合。
6. audit 能区分 `multi_select_auto_bound` 与 `multi_select_confirmed_by_user`。

## 6. 回滚策略

- 关闭 selector 强制模式后，系统必须恢复到 conversation file context 默认可用路径。
- `file_upload` schema 字段和历史消息可以继续保留并展示，不影响上传 / 执行主路径。
- 若 file_upload history 写入异常，可临时关闭上传强一致扩展，但不得返回“resource 成功、history/index 失败”的不一致成功；必须走失败或 repair。
- deleted 文件不可复用约束不可因 selector 回滚而失效。
- `conversation_file_index` repair marker pending 时，rollback / disabled 模式仍必须以 DB resources 为事实源；不得重新信任旧 `index.md`。
- rollback 文档必须说明哪些 audit 事件会停止产生，哪些历史展示继续保留。

## 7. Release gate 验收矩阵

| 领域 | 必过验证 |
| --- | --- |
| Repository / migration | Message 兼容、upsert/delete 幂等、allowlist、安全投影。 |
| Upload / delete | 原始文件、resource、file_upload、index 强一致；失败补偿；repair marker。 |
| API / frontend | history 返回 file_upload；前端卡片展示 active/pending/failed/deleted；隐藏 internal system。 |
| Prompt / memory | file_upload 渲染为历史事件；deleted 不可复用；无路径/正文/base64。 |
| Selector | trigger、post-processing、低置信澄清、exact upload_id、recent usage、sheet selection、deleted 排除。 |
| Skill / builder | `file_selection` 最终字段 parser、旧字段拒绝、builder 模板/checklist/指南更新。 |
| Audit / observability | 所有事件字段脱敏，reason_code 稳定，shadow / enforce_narrow / enforce_guarded_multi 模式可诊断。 |
| Rollback | disabled 模式恢复 conversation context；file_upload history 展示不破坏主路径。 |

## 8. 测试计划

完整 release gate 推荐命令：

```bash
python -m pytest tests/api/test_uploads.py
python -m pytest tests/api/test_conversation_file_selection.py
python -m pytest tests/api/test_pending_skill_context.py
python -m pytest tests/integrations/agent_skills/test_artifact_context.py
python -m pytest tests/integrations/agent_skills/test_input_schema_parser.py
python -m pytest tests/api/test_route_contract.py
cd frontend && npm test -- --run
cd frontend && npm run typecheck
```

新增发布级场景：

- selector 从 `shadow` 切到 `enforce_narrow` 前后，普通问答行为不变。
- `disabled` 回滚后，不再写 selector attachment，但 active conversation file context 仍可用。
- audit payload 静态扫描确认不含路径、`storage_key`、正文、base64、secret。
- deleted 文件在任意 rollout 模式下都不可进入执行路径。
- `enforce` 旧配置值不被接受；合法模式仅为 `disabled | shadow | enforce_narrow | enforce_guarded_multi`。
- repair pending 时 selector / rollback 均不基于旧 `index.md` 自动选择。
- guarded multi-select 默认关闭；开启时需要独立灰度测试全绿。

## 9. 文档与索引

发布前必须同步：

- `docs/prd/README.md` 后端 PRD 入口。
- `docs/prd/backend/00-主代理框架PRD.md` 专题索引。
- `docs/AGENTS.md` Future Work 状态。
- API 文档 / API 更新日志中的 history response 与上传删除行为。
- 前端开发文档中的 file_upload 卡片与 natural-language interrupt 行为。
- `CHANGELOG.md` 当天记录，注明 License Requirement：无新增依赖/许可变更，除非实际实现阶段引入依赖。

## 10. 阶段验收

- selector 能被安全灰度并可一键回滚到 disabled。
- audit 事件足以解释每次自动选择、澄清、恢复、失败和 repair。
- 全量 release gate 通过或明确记录无法运行的验证缺口。
- 文档和索引指向阶段化 PRD 入口，不再只依赖单一长篇 PRD。
