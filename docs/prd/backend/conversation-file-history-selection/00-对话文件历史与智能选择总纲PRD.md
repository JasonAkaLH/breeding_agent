# 对话文件历史与智能选择总纲 PRD

- **编号**：后端 PRD 21 分步总纲
- **日期**：2026-06-19
- **状态**：设计已确认，待按阶段实施
- **父兼容入口**：`docs/prd/backend/21-对话文件历史与智能选择PRD.md`
- **目标模块**：`src/core/models.py`、`src/storage/`、`src/api/runtime.py`、`src/api/dto.py`、`src/api/file_selection.py`、`src/api/file_selection_runtime.py`、`src/capabilities/main_agent/`、`src/integrations/agent_skills/`、`frontend/`

## 1. 背景与问题

对话文件本地资源系统已经让上传文件成为 conversation-scoped 本地资源，Skill runtime 可通过 `resource_manifest.json` 和 `files[].mount_path` 读取真实文件副本。当前系统也支持在无显式 `metadata.upload_ids` 时，把当前 conversation 的 active 文件作为上下文提供给主代理和 Skill runtime。

仍需统一解决：

1. 文件上传成功没有稳定进入 conversation history，后续轮次难以回答“哪个文件什么时候进入这个对话”。
2. conversation 文件池、历史上传事件与本轮实际使用 provenance 容易混淆。
3. 多文件 / 同名文件需要聊天式消歧，低置信时不得强行选择。
4. 未来 Skill 文件需求必须来自 machine-readable contract/schema，不能硬编码 Skill 名称。

### 1.1 已确认实施口径

- **active context 是基线，不是 selector bypass**：无显式 `metadata.upload_ids` 时，active conversation 文件仍可默认作为上下文候选注入；但 required file、明确单文件指代、同名/多候选缩窄、recent usage continuation、interrupt answer 恢复或正文 `upload_id` 精准选择必须进入 selector / deterministic selection 判定，并写 task attachment provenance 或打开澄清。
- **`file_upload` 存在现有 message 表**：不新增独立 `conversation_file_history` 表；通过扩展 `Message` / `message` / `MessageResponse` 的 `message_type`、`metadata`、`updated_at` 支持文件上传历史。
- **`index.md` repair marker 必须持久化**：`index.md` 只是 DB 投影；重写失败后必须写 DB durable repair marker，audit event 不能替代 marker。repair 采用当场重试一次、后台退避重试、下次访问懒修复三层触发。
- **rollout mode 使用显式阶段名**：selector 配置为 `disabled | shadow | enforce_narrow | enforce_guarded_multi`；旧 `enforce` 不作为兼容 alias。
- **正文 upload_id 精准匹配系统生成规则**：当前上传 ID 生成格式为 `upl-` + 12 位十六进制字符；自然语言 exact-token extraction 只识别完整 token 正则 `(?<![A-Za-z0-9_-])upl-[0-9a-fA-F]{12}(?![A-Za-z0-9_-])`，再做服务端权限 / 状态校验。

## 2. 统一契约

```text
ConversationFileResource = active/deleted 权限与可用性事实源
file_upload message       = conversation history 中的上传事件快照和展示入口
task_input_attachment     = 某个 task 实际绑定 / 选择 / 使用过哪个文件的 provenance
selector decision         = 需要缩窄或消歧时，从 active 文件池中选择本轮有效文件
```

## 3. 产品目标

1. 上传成功后 conversation history 立即出现 `message_type=file_upload` 的结构化历史片段。
2. 用户可通过自然语言或直接发送 `upload_id` 引用 conversation 文件；低置信时进入聊天式澄清。
3. active conversation 文件默认可作为上下文候选，但 task-level attachment 只记录本轮实际 provenance。
4. deleted 文件保留为历史事实，但在 API、前端、prompt、selector、binding 和 Skill manifest 中都不可复用。
5. LLM、前端、selector、audit 只接收安全元数据，不接收文件正文、本地路径、`storage_key`、`content` 或 `content_base64`。
6. 保持既有 chat message、interrupt answer、uploads 和 `metadata.upload_ids` 语义，不新增公开 API。
7. 新增 / 迁移 Skill 通过 contract/schema 的 `file_selection` 最终字段声明文件需求，平台归一化为 `FileRequirementProfile`；不保留旧字段 alias 或 legacy type 推断。
8. 上传历史写入、selector 触发、自动选择、歧义中断、恢复选择、删除标记和 repair 都留下结构化事件或状态。

## 4. 非目标

- 不改变上传文件的权限、大小、类型校验规则。
- 不新增公开上传 API、历史 API endpoint、前端文件选择控件或向量检索服务。
- 不做 RAG；第一版不引入 embedding、chunk、vector store 或跨会话索引。
- 不把真实文件路径、`storage_key`、原始文件内容、`content_base64` 暴露给前端、主代理 prompt、selector LLM 或 audit payload。
- 不把 deleted 文件重新挂载给 Skill。
- 第一阶段不 backfill 旧文件的 `file_upload` 历史消息；旧 active resources 仍可通过文件池和 selector 使用。
- 不按现有 Skill 名称硬编码文件选择策略。

## 5. 核心不变量

1. **文件身份永远是 `upload_id`**：文件名、上传时间、摘要、preview 和 recent usage 只用于定位和解释，不作为权限或绑定事实源。
2. **`ConversationFileResource` 是可用性事实源**：selector 候选、Skill manifest、conversation 文件池和删除状态必须以 resource 表为准。
3. **`file_upload` message 是历史事实，不是可用性事实源**：它用于排序、展示、记忆和用户引用理解；不能凭历史消息伪造 active 文件。
4. **`task_input_attachment` 是本轮实际使用 provenance**：recent usage 必须优先来自 task attachment / selector binding / interrupt answer / sheet selection，而不是仅根据上传时间推断。
5. **显式 `metadata.upload_ids` 优先**：如果前端 / 用户本轮显式绑定文件，沿用既有校验与绑定流程，不被 selector 吞掉或降级。
6. **正文中的 `upload_id` 是精准选择提示**：用户在普通消息或 interrupt answer 中直接发送 / 粘贴 `upload_id` 时，平台必须先做服务端精确匹配和权限校验。
7. **文件派生文本不可信**：`filename`、`description_summary`、preview、OCR/PDF 摘要、sheet 名和列名全部按 untrusted user/file data 处理。
8. **selector 只看元数据**：第一版 selector 不读取文件正文；后端只做权限、状态、候选范围、schema 和安全后处理校验。
9. **歧义走聊天 interrupt**：用户通过普通自然语言回答 upload_id、序号、文件描述或重新上传文件；不要求新增前端点选组件。
10. **active context 不短路 required/narrow selector**：conversation file context 可服务普通问答和“全部文件”总结，但不得让 required file、明确单文件指代、同名/多候选缩窄、recent usage continuation 或正文 `upload_id` 绕过 provenance 绑定 / 澄清判定。
11. **`index.md` repair pending 时禁止信旧投影**：只允许从 DB active resources 构造候选；repair 完成前不得用旧 `index.md` 判断文件仍可用。

## 6. 阶段拆分

| 阶段 | 主题 | 可独立完成的结果 |
| --- | --- | --- |
| 阶段零 | 数据模型与 repository 基线 | `Message` 兼容字段、file_upload projection、repository upsert/delete 契约、public allowlist 与安全投影测试。 |
| 阶段一 | 上传删除强一致与历史展示 | 上传 / 摘要回填 / 删除流程写入并维护 `file_upload` history；定义 DB repair marker 与三层 repair 触发；历史 API 和前端卡片可展示 active/deleted 文件。 |
| 阶段二 | 会话文件上下文与 memory 安全 | active conversation file context 与 task attachment provenance 分离；memory 渲染 file_upload 历史并强约束 deleted 不可用。 |
| 阶段三 | 文件需求画像与 selector shadow | 从 metadata、Skill contract/schema、用户 query 归一化文件需求；selector 只 shadow 记录 would-select 和 reason_code。 |
| 阶段四 | selector 消歧、interrupt 与绑定 | 实施候选、recent usage、upload_id 精确选择、selector 后处理、`file_selection_ambiguous` 恢复与 task attachment 绑定。 |
| 阶段五 | 灰度发布、审计与回归门禁 | audit 事件、guarded multi-select 后续灰度、rollout/rollback、端到端验收和文档收口。 |

## 7. 全局验收矩阵

| ID | 验收项 | 主要阶段 |
| --- | --- | --- |
| AC-001 | 上传成功后，conversation history 立即出现 `message_type=file_upload` 文件片段。 | 阶段一 |
| AC-002 | 文件片段只包含 allowlist 元数据，不包含路径 / storage_key / 原文 / base64。 | 阶段零 / 一 |
| AC-003 | 摘要 ready / failed 时更新同一条 file_upload message，不新增重复消息，`created_at` 不变。 | 阶段一 |
| AC-004 | 历史 API 只返回 public allowlist message types：`chat` 与 `file_upload`。 | 阶段零 / 一 |
| AC-005 | 前端历史恢复展示 file_upload 卡片，并继续隐藏其他非 allowlist system message。 | 阶段一 |
| AC-006 | LLM 历史上下文能看到 file_upload 事件，且该事件被标记为不可信历史文件数据，不是系统指令。 | 阶段二 |
| AC-007 | deleted 文件在 prompt 与前端卡片中明确标记为不可复用。 | 阶段一 / 二 |
| AC-008 | deleted 文件不会进入 selector、binding、conversation active context 或 Skill manifest。 | 阶段二 / 四 |
| AC-009 | 上传成功必须包含原始文件、DB resource、file_upload message 和最新 `index.md`；任一失败都不返回成功。 | 阶段一 |
| AC-010 | 删除时 `index.md` 重写失败会记录 repair marker，并在 repair 完成前禁止基于旧 index 自动文件选择。 | 阶段一 / 五 |
| AC-011 | 用户无需点选文件，也可通过自然语言让平台定位或澄清会话文件。 | 阶段四 |
| AC-012 | 多候选或低置信时，平台通过 `file_selection_ambiguous` interrupt 提供自然语言候选，不强猜。 | 阶段四 |
| AC-013 | 自动选择只在合法、单一、高置信且候选完整参与判断时发生。 | 阶段四 |
| AC-014 | 所有最终绑定都复用权限校验与 task attachment provenance 路径。 | 阶段四 |
| AC-015 | 显式 `metadata.upload_ids` 保持既有 HTTP 400 fail-closed 语义，不被 selector 吞掉。 | 阶段二 / 四 |
| AC-016 | 新增或迁移 Skill 必须通过 `file_selection` 最终字段声明文件需求，selector 不硬编码 Skill 名，且不接受旧字段 alias / legacy type 推断。 | 阶段三 |
| AC-017 | 第一阶段不 backfill 旧文件；旧 active resources 仍可通过文件池和 selector 使用。 | 阶段一 / 四 |
| AC-018 | 用户可在普通消息或 interrupt answer 中直接发送 `upload_id` 精准选择 active 文件；未知、越权或 deleted id 不被猜测或静默忽略。 | 阶段四 |
| AC-019 | selector rollout mode 只接受 `disabled`、`shadow`、`enforce_narrow`、`enforce_guarded_multi`；旧 `enforce` 不作为兼容 alias。 | 阶段五 |

## 8. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| file_upload 历史被误当作可用文件事实源 | 所有候选、binding、manifest 均从 `ConversationFileResource` 查询 active 状态；测试覆盖 deleted history 不可复用。 |
| conversation 文件池默认可用与 selector 绑定语义混淆 | 文档和测试区分 file context、effective_upload_ids、task attachment 和 history；普通无 selector 路径不得写 attachment。 |
| LLM 误选文件 | selector 仅看元数据；低置信转 ambiguous；服务端校验 upload_id、status、conversation/user 和 confidence。 |
| 同名同结构文件难区分 | 澄清候选展示 upload_id、上传时间、摘要、sheet 和 recent usage。 |
| 文件候选太多导致 prompt 过大 | 可压缩、分批或 shortlist；只要完整候选未参与判断，就不得自动绑定。 |
| 文件正文泄漏 | 统一 allowlist projection；安全测试锁定禁止字段。 |
| 删除时 index 与 DB 状态短暂不一致 | durable repair marker；repair 完成前禁止基于旧 index 自动选择。 |
| 未来 Skill 未声明文件需求 | builder 模板和 checklist 强制写 `file_selection` 最终字段；缺失或使用旧字段时作为契约错误处理，不做 legacy type 推断。 |

## 9. 全局测试入口

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
