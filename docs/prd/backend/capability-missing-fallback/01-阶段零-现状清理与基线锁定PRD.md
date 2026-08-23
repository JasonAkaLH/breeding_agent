# 阶段零：现状清理与基线锁定 PRD

> **Phase 6 authority notice（2026-08-23）**：本文中的旧任务编排名词仅保留为历史设计或兼容语境，不再描述当前执行控制面。当前任务入口、Tool调用、补充输入、恢复、取消和最终输出以 `docs/prd/backend/unified-agent-loop/` 为唯一authority；不得据本文恢复旧控制面或读取旧Task。

- **Status**：Ready for implementation
- **Date**：2026-06-25
- **Parent PRD**：`docs/prd/backend/23-能力缺失LLMFallback披露PRD.md`
- **Phase Goal**：在新增 fallback contract 前，整理或回滚与父 PRD 冲突的草稿方向，并用回归测试锁定现有普通 main agent、Workbench 停止和 history 行为。

## 1. 背景

父 PRD 第 19 节指出当前工作区存在未提交草稿改动，其中包括 hard-fail `required_skill_missing` 和 executor 正则判断执行意图。这些方向与目标语义冲突：能力缺失应走 LLM fallback 并 completed，而不是直接失败；能力缺失主判断应属于 Planner/Replanner，而不是 Executor 词表或正则。

## 2. In Scope

1. 检查并清理与父 PRD 冲突的草稿实现：
   - hard-fail `required_skill_missing`；
   - executor 中面向通用业务意图的正则/关键词分类；
   - 会导致 `main_agent.respond` 被伪装成已执行缺失能力的 prompt 或文案。
2. 保留与父 PRD 兼容的既有方向：
   - 主代理身份 prompt 不硬编码具体能力清单；
   - `loading_artifacts` 不算 active；
   - 已有 metadata 字段、history API 和前端类型基础。
3. 添加或补齐基线回归：
   - 普通 `main_agent.respond` 请求能完成；
   - 普通闲聊/解释不会带 fallback metadata；
   - Workbench 在 completed 后停止；
   - history metadata 缺失时前端/后端容忍。

## 3. Out of Scope

- 不扩展 Planner JSON schema。
- 不新增 `capability.missing_fallback` 事件。
- 不实现前端 `CapabilityFallbackNotice`。
- 不实现 partial fallback。

## 4. 功能要求

| 编号 | 要求 | 验收 |
| --- | --- | --- |
| P0-R1 | 冲突草稿逻辑不得让能力缺失 hard-fail 成常规路径。 | 代码检查 + 相关测试不再断言 hard-fail 为目标语义。 |
| P0-R2 | Executor 不得保留通用意图正则分类作为能力缺失主判断。 | executor 单测或代码检查。 |
| P0-R3 | 普通 LLM 请求继续使用普通 `main_agent.respond`。 | main_agent / workflow 回归。 |
| P0-R4 | history metadata 缺失时兼容旧消息。 | API 或前端 history 测试。 |

## 5. 测试计划

建议最小命令：

```bash
python -m pytest tests/capabilities/main_agent/
python -m pytest tests/api/ -k "conversation or task"
cd frontend && npm test -- --run
```

若只做文档/草稿整理，至少记录未运行生产测试的原因，并用 `git diff` 确认没有引入新的功能 contract。

## 6. 完成标准

- 与父 PRD 冲突的草稿方向已清理或标注为待替换，不再作为后续阶段依赖。
- 普通 main agent 和历史恢复基线有测试或明确验证证据。
- 可以安全进入阶段一 schema / metadata contract 实施。
