# Prompt Envelope P4 — 测试规格：工具信息分层与公开档案安全

- 日期：2026-05-29
- 目标 PRD：`docs/prd/backend/prompt-envelope/05-阶段四-工具信息分层与能力公开档案安全PRD.md`
- 对应计划：`.omx/plans/prd-20260529-prompt-envelope-p4-tool-profile-safety.md`

## 1. 测试原则

1. 先锁安全失败面：任何 `manifest.body`、脚本路径、handler、runtime、sidecar、config、DSN、token、secret、artifact raw content/path/storage_ref 进入 prompt/audit 都失败。
2. 同时锁用户价值：public profile 必须保留 capability/display/description/public_usage、参数、输入格式、字段示例、输出说明。
3. PromptEnvelope string 模式必须实际发送 envelope，不能继续用 P2 guard fallback 掩盖 Skill match。
4. 下载事实必须可证明：只有 platform `download_url` 能支持 finalizer 声称下载，不能暴露本地路径或 `outputs/...`。

## 2. Test Matrix

| ID | 文件 | 场景 | 断言 |
| --- | --- | --- | --- |
| T1 | `tests.integrations.agent_skills.test_public_skill_profile` | public profile 读取 public_usage / parameters / inputs / outputs | 有用户可见字段；无 scripts/Rscript/wrapper/handler/runtime/path/token/secret |
| T2 | `tests.capabilities.main_agent.test_conversation_memory_prompt` | legacy/off main_agent prompt + SkillMatch | prompt 包含 public profile，不含 `manifest.body` 内部文本 |
| T3 | `tests.capabilities.main_agent.test_conversation_memory_prompt` | string rendered prompt + SkillMatch | segment names 包含 `selected_public_tool_profiles`、`tool_input_schema`；顺序符合 security role；无内部泄漏 |
| T4 | `tests.capabilities.main_agent.test_conversation_memory_prompt` | tool result/artifact sanitization | 保留 `/api/v1/artifacts/.../download`、missing/error/diagnostics；删除 entrypoint/local path/storage_ref/raw content |
| T5 | `tests.capabilities.main_agent.test_main_agent_workflow_and_executor` | `MAF_PROMPT_ENVELOPE_MODE=string` + auto Skill match | prompt 为 envelope，audit `effective_mode=string`，payload 无用户原文和内部 Skill body |
| T6 | `tests.capabilities.main_agent.test_main_agent_workflow_and_executor` | matched Skill prompt | 保留 `matched_skills` output_payload；prompt 只含 public profile/use case，不含 body-only 私有指令 |
| T7 | 现有 `/skill` 软绑定测试 | usage question answer | decision/answer prompt 继续包含公开 profile 并不暴露脚本路径/handler |
| T8 | 现有 finalizer/download 相关测试 | output_files download_url | 只有 platform download_url 可进入 prompt；sandbox/file/local outputs 不作为下载链接 |

## 3. 目标命令

```bash
conda run -n multi_agent python -m unittest tests.integrations.agent_skills.test_public_skill_profile
conda run -n multi_agent python -m unittest tests.capabilities.main_agent.test_conversation_memory_prompt
conda run -n multi_agent python -m unittest tests.capabilities.main_agent.test_main_agent_workflow_and_executor
```

## 4. 回归命令

```bash
conda run -n multi_agent python -m unittest discover -s tests/capabilities/main_agent -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/integrations/agent_skills -p 'test_*.py'
python -m compileall src/capabilities/main_agent src/integrations/agent_skills
```

## 5. 安全扫描清单

在 prompt 字符串和 audit payload 字符串上至少扫描：

- `runtime:`, `python_subprocess`, `handler`, `handler_module`, `handler_factory`
- `scripts/`, `.py`, `Rscript`, `wrapper`
- `sidecar`, `platform_service`, `config.yaml`
- `mysql://`, `postgresql://`, `dsn`, `api_key`, `token`, `secret`, `password`
- `storage_ref`, `local_path`, 内部 `file_path` / 本地路径值、`outputs/`, `sandbox:/mnt/data`（仅允许在下载硬约束说明中作为禁止示例出现，不能作为生成文件事实；用户可见的 OCR `file_path` 参数名不属于内部路径泄漏）
- artifact raw content sentinel（例如 `raw,csv,body`）

## 6. 通过标准

- T1-T8 均通过。
- 目标命令和回归命令 exit code 为 0。
- Architect verification 认可 public profile/tool schema/result segment 边界。
- Deslop 后重新运行同一回归仍通过。
- License Requirement：无依赖/许可变更，未触发 cargo-deny 风险。
