# 能力缺失 LLM Fallback 披露分步 PRD 索引

- **日期**：2026-06-25
- **状态**：已从父 PRD 拆分；待实施
- **父兼容入口**：`docs/prd/backend/23-能力缺失LLMFallback披露PRD.md`
- **关联 PRD**：`docs/prd/backend/12-Skill一等Capability能力池PRD.md`、`docs/prd/backend/15-SkillExecutor实现需求PRD.md`、`docs/prd/backend/18-失败自检恢复与Fallback控制层PRD.md`、`docs/prd/backend/skill-workbench/README.md`、`docs/prd/backend/prompt-envelope/README.md`
- **总目标**：当用户请求需要业务 Skill、MCP 或内置 capability 但当前 public capability 库无匹配业务能力时，系统必须以 `completed` 停止任务，通过 LLM fallback 给出可复制文本或建议，并在正文、事件、history metadata 与前端 notice 中如实披露未调用匹配能力。

## 目录级置信标准与发布门禁

本目录是一组可逐阶段实施、验证和回滚的 PRD。以下标准是全局不变量和发布门禁；阶段零 / 阶段一尚未实现完整 fallback 链路时，必须保证不引入与这些不变量冲突的实现，也不得宣称后续阶段能力已经完成。

1. **事实披露不可丢失**：只要 `capability_missing_fallback.disclosure_required=true`，assistant 正文必须由后端保证披露前缀；阶段二完成后，运行时事件和 assistant message metadata 也必须保留结构化证据；阶段三完成后，前端 notice 和 history restore 必须能展示同一事实。
2. **任务终态守恒**：能力缺失 fallback 是成功完成的降级回答，任务终态必须是 `completed`；不得新增 `completed_with_warning`，也不得把能力缺失误标为系统失败。
3. **普通 LLM 请求不被污染**：闲聊、解释、总结、改写、翻译、头脑风暴和用户明确要求“建议/草案/思路”的请求必须继续走普通 `main_agent.respond`，不显示 fallback notice。
4. **Planner/Replanner 主路径**：是否能力缺失由 Planner/Replanner 基于用户原文、effective question、历史上下文和 public capability 列表判断；Executor 只做强制 Skill/MCP 缺失、metadata 补传和 prompt 注入等弱兜底，不做通用意图正则分类。
5. **产物边界不越权**：fallback 可输出 Markdown/CSV/HTML 代码块、布局草案或步骤，但不得生成用户可下载平台 artifact、下载按钮或声称文件已生成。
6. **metadata 最小化**：assistant history metadata 只保存精简 `capability_missing_fallback`，不得保存完整历史正文、完整 prompt、handler、runtime、source path、sandbox、secret、文件内容或内部路径。
7. **事件审计一致**：`capability.missing_fallback` 使用 `EventVisibility.FRONTEND` 单条事件，同时可作为审计证据查询；同一 task 默认只发送一次。
8. **可阶段回滚**：每个阶段都必须提供最小验收测试；前一阶段未通过时不得进入依赖它的阶段。

## 拆分原则

1. 父 PRD 保留完整产品语义、术语、边界、数据结构和总体验收标准；本目录只拆分可执行、可验收的实施阶段。
2. 阶段 PRD 默认继承父 PRD 的禁止项：不新增终态、不引入全局 severity/level、不为 fallback 生成平台文件 artifact、不伪装调用缺失能力、不硬编码具体能力清单。
3. 每个阶段文件都必须是交付级代码设计：明确最终目标范围内的 contract、边界、失败模式和测试门禁；不得以缩减版或临时实现为验收口径。
4. 先清理与父 PRD 冲突的草稿改动，再建立 plan metadata contract；随后完成后端 full fallback 闭环，再接前端 notice/history；最后补 partial fallback、Replanner 和审计硬化。
5. Phase 2 和 Phase 3 可在同一开发窗口内顺序合并，但验证必须分层：后端 completed / metadata / event 先通过，前端 runtime/history notice 再放量。

## 阶段文件

| 阶段 | PRD | 目标 | 实施优先级 |
| --- | --- | --- | --- |
| 总纲 | `00-能力缺失LLMFallback披露总纲PRD.md` | 目录内总纲摘录：统一不变量、阶段依赖、全局验收与回滚门禁。 | Umbrella |
| 阶段零 | `01-阶段零-现状清理与基线锁定PRD.md` | 整理/回滚与父 PRD 冲突的 hard-fail、executor 正则意图判断等草稿方向，并锁定普通 main agent、Workbench 停止和 history 基线。 | P0 |
| 阶段一 | `02-阶段一-PlanMetadata契约PRD.md` | 扩展 Planner schema、repair prompt、parser 和 plan/node metadata 传播，使 `capability_missing_fallback` 成为合法、收敛、可测试的 plan contract。 | P1 |
| 阶段二 | `03-阶段二-后端FullFallback闭环PRD.md` | 完成 full fallback 后端交付闭环：Planner 区分普通 respond 与 fallback respond，Runtime 事件、MainAgent prompt、正文后处理、history metadata 和 artifact 禁止边界闭环。 | P1 |
| 阶段三 | `04-阶段三-前端Notice与历史恢复PRD.md` | 前端识别 `capability.missing_fallback`，在 Workbench 和 assistant 气泡展示 `CapabilityFallbackNotice`，并从 history metadata 恢复。 | P2 |
| 阶段四 | `05-阶段四-PartialFallback与Replanner审计PRD.md` | 支持部分能力已执行后的 partial fallback、Replanner 后发现能力缺失、事件去重和审计查询一致性。 | P3 |

## 推荐阶段执行顺序

```text
阶段零 现状清理 / 基线测试
  -> 阶段一 Planner schema / parser / metadata contract
  -> 阶段二 后端 full fallback completed 闭环
  -> 阶段三 前端运行态 notice / history 恢复
  -> 阶段四 partial fallback / Replanner / 审计硬化
```

阶段二是后端 full fallback 的交付门槛：无匹配业务能力时任务必须 completed、正文必须披露、不得生成下载 artifact。阶段四是复杂 DAG 和审计硬化门槛；在阶段四前，可以明确不支持 partial fallback 自动规划，但不得错误声称 partial 场景已完整实现。

## 总体验证命令入口

各阶段 PRD 给出更细命令。完整 release gate 至少覆盖：

```bash
python -m pytest tests/orchestration/ -k "planner or replanner or workflow"
python -m pytest tests/capabilities/main_agent/
python -m pytest tests/api/ -k "task or event or conversation or artifact"
cd frontend && npm test -- --run
cd frontend && npm run typecheck
```

若只新增或调整文档，以链接、目录索引、父 PRD 一致性和 `CHANGELOG.md` 同步为验收；若触及生产代码，必须运行该阶段列出的最小测试并记录未覆盖风险。
