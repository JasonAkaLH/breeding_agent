# 阶段零：测试基线与旧行为锁定 PRD

- **父总纲**：`docs/prd/backend/table-upload-normalization/00-表格上传编码兼容与表头规范化总纲PRD.md`
- **状态**：待实施
- **实施类型**：tests-only / 文档校验；不得修改生产 runtime
- **适用范围**：`tests/api/`、`tests/capabilities/main_agent/`、`tests/integrations/agent_skills/`、前端类型测试可先补期望但不得默认污染主回归

## 1. 目标

阶段零必须先把当前行为与目标风险锁定为可验证基线，避免后续实现时误伤现有上传、权限、prompt-safe 和 Skill artifact 通道。

## 2. 本阶段范围

### 2.1 In scope

1. 锁定现有 UTF-8 CSV / JSON 上传、预览、列表、删除、权限隔离、`sha256` 基于原始 bytes 的行为。
2. 新增 CSV BOM + 外层引号、非 UTF-8 编码、空 / 重复表头、宽表摘要上限、多 sheet Excel 目标行为测试。
3. 锁定 `uploaded_artifacts` 不包含完整内容、`skill_artifacts` 可作为执行专用内容通道的隔离关系。
4. 锁定 `application/vnd.ms-excel` + CSV 后缀或无 Excel magic bytes 时仍按 CSV 兼容的期望。
5. 补充 API / runtime 目标测试骨架时，必须让尚未实现的目标测试以明确 skip / expected failure / 独立测试名表达，不能让默认全量回归长期红。

### 2.2 Out of scope

1. 不新增 `table_upload_normalizer`。
2. 不修改 `src/api/upload_store.py`、`src/api/runtime.py` 或执行器生产代码。
3. 不新增 `openpyxl` / `xlrd`。
4. 不修改 `skill/**`。
5. 不改前端生产 UI。

## 3. 功能需求

| ID | 需求 |
|---|---|
| P0-FR-01 | 当前 CSV happy path 的 preview columns、row_count、`skill_artifacts[].content` 必须被测试锁定。 |
| P0-FR-02 | 当前 JSON object / array preview 行为必须被测试锁定。 |
| P0-FR-03 | 当前 image / PDF base64 执行专用 artifact 与 prompt-safe 摘要隔离必须被测试锁定。 |
| P0-FR-04 | owner scoped upload、unknown upload delete no-op、oversized 与 unsupported file 错误必须被测试锁定。 |
| P0-FR-05 | 目标测试必须覆盖 BOM + 外层引号表头应规范化为 `ped_id`，并验证旧实现会失败或被标记为待实现。 |
| P0-FR-06 | 目标测试必须覆盖 prompt-safe 摘要不含 `content` / `content_base64` / 原始行数据。 |
| P0-FR-07 | 目标测试必须覆盖多 sheet Excel 未选择 sheet 时不得产生可执行 content。 |

## 4. 验收标准

| ID | 验收标准 |
|---|---|
| P0-AC-01 | `tests.api.test_uploads` 中现有 CSV / JSON / image / PDF / 权限 / 删除行为仍能单独运行。 |
| P0-AC-02 | 新增目标测试名称清楚表达未来行为；未实现目标不得让默认 `discover -s tests/api` 持续失败。 |
| P0-AC-03 | no-raw 断言覆盖 prompt-safe `uploaded_artifacts` 与主代理 prompt 构造路径。 |
| P0-AC-04 | 测试中没有真实敏感配置、真实文件内容日志或对 `skill/**` 的修改。 |

## 5. 回归命令

```bash
conda run -n multi_agent python -m unittest tests.api.test_uploads
conda run -n multi_agent python -m unittest tests.api.test_pending_skill_context
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
```

## 6. 完成门禁

- 生产代码 diff 为空或仅限必要测试 seam 文档注释。
- 默认 API 回归无新增红测试。
- License Requirement：无依赖 / 许可变更，未触发 cargo-deny 风险。
