# 阶段四 PRD —— 工具信息分层与能力公开档案安全

- **日期**：2026-05-29
- **状态**：待实施
- **父实施计划**：`docs/orchestration/大语言模型提示词信封与缓存友好上下文组装实施计划.md`
- **所属专题**：大语言模型提示词信封
- **范围**：工具规则、工具公开档案、工具输入 schema、工具结果 segment、Skill public profile 复用、内部结构泄漏防回归
- **非范围**：不改变 Skill 执行器 contract；不改变 artifact 下载 API；不修复 Skill resume artifact 继承

## 1. 问题陈述

工具规则、Skill 用法、输入 schema、执行结果和普通历史具有不同的可信度与安全边界。当前主代理 Skill match 仍可能把 `manifest.body` 注入 prompt，带来脚本路径、handler、runtime 细节泄漏风险。阶段四要把工具信息拆成独立 segment，并复用现有 `build_public_skill_profile` 脱敏能力。

## 2. 目标

1. 建立工具信息四层 segment：stable tool rules、selected public tool profiles、tool input schema、required tool results and artifacts。
2. 主代理 Skill match 禁止直接注入 `match.manifest.body`。
3. 复用并扩展 `src/integrations/codex_skills/public_profile.py::build_public_skill_profile`。
4. tool result segment 保留平台 `download_url`、missing、error、diagnostics 等关键事实。
5. Skill input resolver 不把 `entrypoint` 等内部入口名作为默认公开字段。
6. 增加安全扫描测试，防止脚本路径、handler、runtime、DSN、token 进入 prompt/audit。

## 3. 非目标

- 不让 LLM 读取完整 `SKILL.md` body。
- 不暴露 Skill 内部代码结构。
- 不把 output file 本地路径给前端或 LLM 当下载链接。
- 不改变 existing artifact storage / download authorization。

## 4. 功能需求

| ID | Requirement | Acceptance |
| --- | --- | --- |
| P4-FR-1 | 主代理 Skill profile 必须使用 public profile。 | string 模式 prompt 中含 capability_id/display_name/description/public_usage，但不含 `manifest.body` 内部内容。 |
| P4-FR-2 | 必须复用现有 sanitizer。 | 代码路径调用或共享 `build_public_skill_profile`；不重复实现不一致 allowlist。 |
| P4-FR-3 | tool schema segment 必须只含用户可见参数契约。 | 参数名、类型、必填、aliases、accepted formats、missing input 标准可见；entrypoint/handler/path 不可见。 |
| P4-FR-4 | tool result segment 必须保留下载事实。 | 只有存在平台 `/api/v1/artifacts/.../download` 的 `download_url` 时，finalizer 才能声称可下载。 |
| P4-FR-5 | artifact raw content 不得进入 prompt。 | 测试包含 raw/content/path/storage_ref 的 artifact context，渲染后不出现敏感字段。 |

## 5. 非功能需求

- **Security**：内部实现字段默认拒绝，不依赖 LLM 自律。
- **User value**：公开档案要足够回答“如何构建数据、字段值应该是什么、示例如何填写”。
- **Compatibility**：现有 `/skill` 软绑定答疑继续可用，且不暴露内部结构。

## 6. 实施计划

1. 扩展 public profile 测试，覆盖 main agent prompt 使用场景。
2. 将 `prompt_builder.py` 中 `match.manifest.body` 替换为 public profile segment。
3. 为 tool input schema 建立安全投影函数，供主代理和 resolver profile 复用。
4. 将 dependency / script results / artifact context 归入 tool result segment，并保留文件下载硬约束。
5. 增加 prompt/audit 敏感词扫描测试。

## 7. 验收标准

- `conda run -n multi_agent python -m unittest tests.integrations.codex_skills.test_public_skill_profile` 通过。
- `/skill` prompt safety test 通过。
- 主代理 Skill match prompt 不含脚本路径、handler、runtime、sidecar、config、DSN、token、secret。
- finalizer 文件下载硬约束仍有测试覆盖。
- License Requirement：无依赖/许可变更，未触发 cargo-deny 风险。

## 8. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| profile 过度脱敏导致答疑不可用。 | `public_usage` 必须包含输入格式、参数含义、示例、输出说明；测试覆盖用户可见用法回答。 |
| 重复 sanitizer 行为不一致。 | 明确复用 `build_public_skill_profile`，只扩展一处 allowlist。 |
| tool result 过大。 | 关键事实 required，明细压缩或采样；audit 记录裁剪。 |
