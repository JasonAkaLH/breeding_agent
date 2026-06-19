# 对话文件历史与智能选择分步 PRD 索引

- **日期**：2026-06-19
- **状态**：阶段零、阶段一、阶段二已实施；阶段三至阶段五待实施
- **父兼容入口**：`docs/prd/backend/21-对话文件历史与智能选择PRD.md`
- **关联 PRD**：`docs/prd/backend/20-对话文件本地资源文件系统PRD.md`、`docs/prd/backend/skill-contract-progressive-disclosure/README.md`、`docs/prd/backend/table-upload-normalization/README.md`
- **总目标**：把 conversation-scoped 上传文件升级为可排序历史、可安全注入上下文、可按任务 provenance 绑定、可在低置信时聊天式澄清的统一文件上下文系统。

## 目录级置信标准

本目录是一组可逐阶段实施、验证和回滚的 PRD，而不是互不相干的文档集合。实施和评审时必须同时满足：

1. **事实源清晰**：`ConversationFileResource` 始终是 active/deleted、权限与可用性的事实源；`file_upload` message 只做历史快照和展示入口。
2. **上下文与绑定分离**：conversation file context 可以默认暴露 active 文件；task-level attachment 只记录本轮显式上传、selector、interrupt answer、sheet selection 等实际 provenance。
3. **安全投影优先**：前端、LLM、selector、audit 只能接收 prompt-safe / frontend-safe 元数据，不得暴露路径、`storage_key`、正文、base64、secret 或 provider raw payload。
4. **低置信不猜测**：同名、多候选、候选信息不足、LLM selector 异常或候选集合未完整参与判断时，必须澄清或缺文件提示，不得静默绑定。
5. **测试先行**：每个阶段都必须先补能失败描述目标行为的测试，再实现生产路径；阶段完成前必须跑对应阶段回归命令。
6. **可灰度回滚**：selector 从 `shadow` 到 `enforce_narrow` / `enforce_guarded_multi` 分阶段开启；关闭 selector 强制模式后必须回到当前 conversation file context 默认可用路径。
7. **active context 不短路 selector**：普通“总结全部/当前会话文件”可只依赖 conversation file context 且不写 task attachment；但 required file、明确单文件指代、同名/多候选缩窄、recent usage continuation 或正文 `upload_id` 精准选择必须进入 selector / deterministic selection 判定，并写 provenance 或打开澄清。
8. **repair marker 持久化**：`index.md` 是 DB 的投影；重写失败必须有 DB durable repair marker 记录，audit/log 不能替代 marker。修复完成前不得信任旧 `index.md` 做自动选择依据。

## 拆分原则

1. 父兼容入口保留合并后的完整语义与历史背景；本目录负责把实施拆成可独立计划、开发、验收、回滚的阶段 PRD。
2. 阶段 PRD 默认继承总纲的不变量、禁止字段、安全边界和验收口径，只记录本阶段新增或收窄的范围。
3. Schema / repository / public history 基线先于上传强一致；历史和 prompt 安全先于 selector；selector shadow 先于 `enforce_narrow`；发布门禁最后收口。
4. 所有阶段均不得新增公开上传 API、历史 API endpoint、前端点选控件、RAG / embedding / vector store 或跨会话文件索引。

## 阶段文件

| 阶段 | PRD | 目标 | 实施优先级 |
| --- | --- | --- | --- |
| 总纲 | `00-对话文件历史与智能选择总纲PRD.md` | 统一目标、非目标、术语、不变量、阶段依赖、总体验收矩阵与风险控制。 | Umbrella |
| 阶段零 | `01-阶段零-数据模型与Repository基线PRD.md` | 扩展 Message / DTO 兼容字段、建立 file_upload repository 契约、public allowlist 与安全投影测试。 | P0 |
| 阶段一 | `02-阶段一-上传删除强一致与历史展示PRD.md` | 上传成功写入 file_upload history，摘要回填幂等更新，删除标记历史，历史 API / 前端卡片展示。 | P1 |
| 阶段二 | `03-阶段二-会话文件上下文与Memory安全PRD.md` | 明确 conversation file context、file_upload memory 渲染、deleted 不可复用约束、无 selector 时不写 task attachment。 | P1 |
| 阶段三 | `04-阶段三-文件需求画像与SelectorShadowPRD.md` | 从 Skill contract/schema、metadata、用户 query 归一化 `FileRequirementProfile`，建立 trigger detector 与 selector shadow 审计。 | P2 |
| 阶段四 | `05-阶段四-Selector消歧Interrupt与绑定PRD.md` | 实施候选构造、recent usage、upload_id 精准选择、selector 后处理、`file_selection_ambiguous` interrupt/resume 与 task attachment 绑定。 | P3 |
| 阶段五 | `06-阶段五-灰度发布审计与回归门禁PRD.md` | 完成 audit 事件、guarded multi-select 策略、rollout/rollback、端到端验收、文档和 release gate。 | Release gate |

## 跨阶段不变量

- 文件身份永远是 `upload_id`；文件名、摘要、preview、上传时间和 recent usage 只用于解释与定位。
- deleted 文件可保留在历史中，但不得进入 active context、selector candidates、binding、Skill manifest 或执行 workspace。
- `message_type=file_upload` 是唯一允许返回给前端和 memory 的 public system message 类型；不得泛化暴露其他 `role=system` 消息。
- `metadata.upload_ids` 是提交前显式绑定契约，保持 HTTP 400 fail-closed；用户正文 / interrupt answer 中的 `upload_id` 是聊天内容，走 selector provenance 路径。
- LLM selector 第一版只看元数据；最终选择必须经服务端 active candidate、conversation/user/status、confidence、schema 和权限校验。
- `select_many` 自动绑定默认关闭；只有后续 guarded multi-select 灰度开启且满足 allow_multiple / 明确比较合并意图时才允许自动绑定多文件。
- `index.md` 不是权限事实源；若 index repair pending，不得基于旧 index 自动选择文件。
- 当前自动识别的正文 `upload_id` 只匹配系统生成格式 `upl-` + 12 位十六进制字符，且必须是完整 token；substring 命中不得作为精准选择。

## 推荐阶段执行顺序

```text
阶段零 数据模型 / repository / allowlist
  -> 阶段一 上传删除强一致 / 历史 API / 前端卡片
  -> 阶段二 conversation context / memory / deleted 安全
  -> 阶段三 FileRequirementProfile / contract / selector shadow
  -> 阶段四 selector `enforce_narrow` / interrupt / attachment provenance
  -> 阶段五 audit / guarded rollout / release gate
```

## 总体验证命令入口

各阶段 PRD 给出更细命令。完整 release gate 至少覆盖：

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
