# 阶段四：Selector 消歧、Interrupt 与绑定 PRD

- **编号**：后端 PRD 21-Phase 4
- **日期**：2026-06-19
- **状态**：待实施
- **前置阶段**：阶段三文件需求画像与 selector shadow
- **目标模块**：`src/api/file_selection.py`、`src/api/file_selection_runtime.py`、task attachment repository、interrupt resume、sheet selection 链路

## 1. 阶段目标

在 `enforce_narrow` 范围内启用文件 selector：当用户明确引用文件、下游 required file、同名 / 多候选需要缩窄、continuation 需要 recent usage 或 interrupt answer 需要恢复时，平台可以安全选择、澄清或提示缺文件，并把最终选择写入 task attachment provenance。

## 2. 范围

### In scope

- 构造 `ConversationFileCandidate`，只包含 active resource 的 prompt-safe 字段和 recent usage。
- 支持正文 / interrupt answer 中的 `upload_id` exact-token extraction。
- 支持 LLM selector 或等价语义选择输出 `FileSelectionDecision`。
- 服务端后处理、confidence 门槛、active candidate 校验、`select_many` 默认确认策略。
- `file_selection_ambiguous` interrupt 与自然语言恢复。
- 选中文件写 task attachment，并复用 `resolve_uploads_for_message()` 或等价权限校验。
- 选中文件需要 sheet selection 时链式进入 `sheet_selection_required`。

### Out of scope

- 不做全文内容检索、embedding、RAG 或跨会话文件选择。
- 不默认开启自动 `select_many` 绑定。
- 不新增前端点选组件；仍使用自然语言 interrupt。

## 3. ConversationFileCandidate

候选必须从 active `ConversationFileResource` 构造，只能包含 prompt-safe 字段：

```json
{
  "upload_id": "upl-1a2b3c4d5e6f",
  "filename": "materials.csv",
  "original_filename": "materials.csv",
  "normalized_filename": "materials.csv",
  "file_type": "csv",
  "content_type": "text/csv",
  "size_bytes": 12345,
  "sha256_short": "abcdef123456",
  "description_summary": "...",
  "preview": "仅限安全截断后的 metadata preview",
  "created_at": "2026-06-18T10:00:00",
  "selected_sheet": "Sheet1",
  "requires_sheet_selection": false,
  "recent_usage": {
    "usage_count": 2,
    "last_used_task_id": "task_xxx",
    "last_used_at": "2026-06-18T10:30:00",
    "last_source_kind": "file_selector",
    "selected_sheet": "Sheet1"
  }
}
```

`recent_usage` 必须来自 conversation 范围内的 task input attachments 或等价 usage provenance，不得只用上传历史推断。

## 4. FileSelectionDecision

selector 输出结构化决策：

```json
{
  "decision": "select_one | select_many | ambiguous | no_file_needed | no_usable_file",
  "selected_upload_ids": ["upl-1a2b3c4d5e6f"],
  "confidence": 0.91,
  "reason_code": "single_candidate | filename_match | recent_usage | ambiguous_candidates | no_files_in_conversation | metadata_insufficient"
}
```

稳定 reason_code：

| reason_code | 含义 | 用户可见策略 |
| --- | --- | --- |
| `no_files_in_conversation` | 当前会话没有可用文件 | 提示用户上传文件或明确文件来源 |
| `all_candidates_invalid` | 候选不存在、过期、越权或状态不可用 | 提示文件不可用，必要时重新上传 |
| `file_type_mismatch` | 文件类型不满足当前需求 | 说明需要的文件类型 |
| `metadata_insufficient` | metadata / summary / preview 不足以安全判断 | 请求用户补充说明或指定 upload_id |
| `ambiguous_candidates` | 多个候选均可能正确 | 打开 `file_selection_ambiguous` interrupt |
| `needs_sheet_selection` | 已选表格还需 sheet selection | 链式进入 `sheet_selection_required` |
| `llm_selector_failed` | LLM 调用失败、返回格式错误或无法解析 | 降级澄清或缺文件提示 |
| `recent_usage` | 根据最近实际使用文件定位 | 可自动选择或用于候选解释 |
| `explicit_upload_id` | 前端 metadata 或用户正文 / interrupt answer 精确给出 upload_id | metadata 走提交前显式绑定；正文 / answer 走 selector provenance 路径 |

## 5. 服务端后处理与安全校验

LLM / selector 输出后必须服务端验证：

1. `selected_upload_ids` 必须属于本次 active candidate 列表。
2. candidate 必须仍属于当前 conversation / user，且 status 未 deleted。
3. `confidence` 必须在 0 到 1。
4. `decision=select_one` 时必须恰好一个合法 id。
5. `decision=select_many` 时必须多个合法 id，且 `FileRequirementProfile.allow_multiple=true` 或用户明确要求比较 / 合并多个文件；否则转 `ambiguous`。
6. `enforce_narrow` 默认只自动绑定高置信 `select_one`；`select_many` 默认转入澄清确认，除非显式开启 `enforce_guarded_multi` 且满足 allow_multiple / 明确比较合并意图。
7. 低于置信阈值（建议 `<0.75`）转 `ambiguous`。
8. 候选集合未完整参与判断时，即使生成 shortlist，也不得自动绑定；只能进入澄清并提示用户缩小描述或选择候选。
9. JSON parse 失败、schema invalid、选择不存在文件时降级为 `ambiguous` 或 `no_usable_file`，并写入标准 reason_code。
10. 选中文件若 `requires_sheet_selection=true`，必须链式进入 `sheet_selection_required`；sheet 选择完成后再恢复原任务并绑定 attachment。
11. 最终仍调用 `resolve_uploads_for_message()` 或等价权限校验做 fail-closed 校验。

## 6. 用户正文中的 upload_id 精准选择

用户可以在普通 chat message 或 interrupt answer 中直接发送 `upload_id` 来精准选择文件。该能力是自然语言选择的一部分，不要求前端转换为 `metadata.upload_ids`。

处理规则：

1. 后端在调用 LLM selector 前，必须先做 `upload_id` exact-token extraction，只接受当前系统生成格式的完整 token：`upl-` + 12 位十六进制字符。匹配正则为 `(?<![A-Za-z0-9_-])upl-[0-9a-fA-F]{12}(?![A-Za-z0-9_-])`，大小写不敏感；不支持 `upl_...`、`upload_...`、substring、编辑距离匹配或前缀补全。
2. 若正文中恰好一个 `upload_id` 命中当前 conversation / user 的 active resource，且本轮语义需要文件或平台需要写 task-level provenance，则以 `reason_code=explicit_upload_id` 生成高置信 `select_one`，再走权限校验、sheet selection 和 task attachment 绑定。
3. 若正文中出现多个有效 `upload_id`，且用户明确要求比较、合并或 `allow_multiple=true`，可进入 `select_many` 后处理；默认仍按 `enforce_guarded_multi` 门禁决定是否可自动绑定，否则需要澄清确认。
4. 若正文中的 `upload_id` 不存在、越权、已删除或不属于当前 conversation，不能交给 LLM 猜测，也不能静默忽略；应打开不可用文件澄清或返回用户可见说明。
5. 正文 `upload_id` 与结构化 `metadata.upload_ids` 的失败语义不同：`metadata.upload_ids` 仍在提交前 HTTP 400 fail-closed；正文 `upload_id` 是聊天内容，通常在 message/task 创建后通过 interrupt / 可见说明恢复。
6. 审计事件必须记录 `reason_code=explicit_upload_id`、命中的 selected_upload_ids 和失败原因摘要，但不得记录文件正文或敏感路径。

## 7. Interrupt 与恢复

### 7.1 file_selection_ambiguous

当 selector 返回 `ambiguous`、`metadata_insufficient`、`multi_select_requires_confirmation` 或低置信时，打开现有 interrupt：

- `reason_code = file_selection_ambiguous`
- `required_fields._file_selection` 包含 profile、candidate 摘要、reason_code、允许上传替换文件标记。
- `question` 使用自然语言列出候选，不要求前端新增点选 UI。

候选展示至少包含：序号、filename / original_filename、upload_id、uploaded_at / created_at、description_summary、selected_sheet / requires_sheet_selection、recent usage（如有）。

### 7.2 interrupt answer 恢复

用户可通过以下方式回答：

- 直接回复一个或多个有效 `upload_id`；
- 回复“第一份 / 第二个 / 用 120 行那个”等自然语言；
- 重新上传文件并说“用这个”；
- 明确说“不用文件”。

恢复规则：

1. 解析用户答案，得到 selected upload_ids 或 no-file decision。
2. 新上传 replacement file 必须先走 uploads API 校验和 file_upload history 写入，再成为 candidate。
3. selected upload_ids 必须重新做 conversation/user/status 权限校验。
4. 选中文件写入 task_input_attachment，`source_kind=file_selector` 或 `interrupt_answer_upload`。
5. 若选中文件需 sheet selection，先进入 `sheet_selection_required`，完成后恢复原 task。
6. 恢复事件必须记录 selected ids、reason_code、source，不记录完整文件正文或 prompt。

## 8. `enforce_narrow` 范围

阶段四只允许对以下场景执行 `enforce_narrow`：

- required file profile；
- 明确文件指代；
- 同名 / 多候选且下游只接受单文件；
- recent usage continuation；
- interrupt answer 恢复；
- 用户正文出现 `upload_id`。

普通问答、探索性总结、无 required profile 且 conversation file context 已可满足的场景，不应为了写 provenance 强制 selector。

但 active conversation file context 不得短路上述 `enforce_narrow` 场景：当请求已经落入 required file、明确单文件指代、同名/多候选缩窄、recent usage continuation、interrupt answer 恢复或正文 `upload_id` 时，即使当前会话 active 文件已经注入执行 metadata，也必须继续 selector / deterministic selection 判定，并最终写 task attachment provenance、打开澄清或返回不可用文件说明。

## 9. 测试计划

| 测试 | 断言 |
| --- | --- |
| 单文件 required | 自动 select_one，写 task attachment。 |
| 单文件普通问答 | 保留 context，不写 attachment。 |
| 多个同名 materials.csv | 打开 `file_selection_ambiguous`。 |
| 全部文件总结 | 可使用 conversation context，不中断。 |
| recent usage | 先用 A、再上传 B、说“继续用刚才那个”选择 A。 |
| 正文 upload_id | `upl-[0-9a-fA-F]{12}` 完整 token active id 精准选择；未知、越权、deleted id 澄清且不交 LLM 猜测；嵌入更长 token 时不命中。 |
| selector invalid JSON | 降级 ambiguous / no_usable_file 并记录 reason_code。 |
| low confidence | 不自动绑定，进入 interrupt。 |
| select_many 默认 | 默认澄清确认，不自动多绑定。 |
| sheet selection | 选中多 sheet 文件后链式进入 `sheet_selection_required`。 |
| deleted 文件 | 不进入 candidates、binding 或 manifest。 |
| interrupt resume | upload_id / 序号 / 新上传文件均可恢复原 task。 |

推荐命令：

```bash
python -m pytest tests/api/test_conversation_file_selection.py
python -m pytest tests/api/test_pending_skill_context.py
python -m pytest tests/integrations/agent_skills/test_artifact_context.py
```

## 10. 阶段验收

- selector `enforce_narrow` 场景能安全自动选择、澄清或提示缺文件。
- 所有最终绑定都写入 task attachment provenance 并通过权限 / 状态校验。
- 多候选、低置信、selector 异常、deleted / 越权 / 未知 id 均 fail closed。
- 用户可只用聊天自然语言完成文件消歧，不需要新增前端控件。
