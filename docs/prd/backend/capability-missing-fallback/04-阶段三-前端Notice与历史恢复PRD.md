# 阶段三：前端 Notice 与历史恢复 PRD

- **Status**：Ready for implementation
- **Date**：2026-06-25
- **Parent PRD**：`docs/prd/backend/23-能力缺失LLMFallback披露PRD.md`
- **Depends On**：阶段二后端 full fallback 闭环
- **Phase Goal**：前端消费后端事件和 history metadata，在 Workbench 状态区与 assistant 气泡顶部展示结构化 `CapabilityFallbackNotice`，并保证刷新历史后可恢复。

## 1. In Scope

1. SSE / task event reducer 识别 `capability.missing_fallback`。
2. 前端状态模型：
   - `TaskEventState` 或等价运行态模型必须新增 `capabilityFallbackNotice` / 等价字段，用于保存 SSE 收到的 fallback notice state；
   - `ConversationMessage` 或等价消息模型必须能承载 assistant fallback notice，或能承载经安全解析后的 fallback metadata；
   - 不得把 raw backend metadata 全量透传到展示层。
3. 新增专用 `CapabilityFallbackNotice`：
   - 不引入通用 `severity` / `level` 字段；
   - 视觉按 warning 样式；
   - 展示 full / partial、缺失能力摘要、fallback 内容范围；
   - partial 字段可先兼容解析，完整 partial 行为由阶段四后端提供。
4. assistant 气泡顶部渲染 notice。
5. `messageFromHistory` / history restore 从 `message.metadata.capability_missing_fallback` 安全解析并恢复 notice：
   - 只接受父 PRD 定义的安全字段；
   - 字段缺失或类型异常时按“不显示 notice，但正文照常展示”降级；
   - 不把未知 metadata 字段转为 UI 文案。
6. Workbench：
   - 收到 fallback event 时展示能力缺失降级提示；
   - task completed 后正常停止；
   - `loading_artifacts` 不算 active，不显示停止按钮。

## 2. Out of Scope

- 前端不得自行判断能力是否缺失。
- 不新增全局 severity / level 体系。
- 不修改后端 fallback 判定。
- 不生成 artifact 下载 UI。

## 3. Functional Requirements

| 编号 | 要求 | 验收 |
| --- | --- | --- |
| P3-R1 | SSE reducer 识别 `capability.missing_fallback`。 | taskEvents 测试。 |
| P3-R2 | `TaskEventState` / 运行态模型保存 fallback notice，不全量透传 raw metadata。 | taskEvents 类型与 reducer 测试。 |
| P3-R3 | Workbench 状态区显示 fallback notice。 | App/component 测试。 |
| P3-R4 | assistant 气泡顶部显示 `CapabilityFallbackNotice`。 | component 测试。 |
| P3-R5 | history metadata 可安全恢复 notice。 | messageFromHistory/App 测试。 |
| P3-R6 | 旧历史无 metadata 或 metadata 类型异常时不报错、不显示 notice。 | history 兼容测试。 |
| P3-R7 | `loading_artifacts` 不触发 active/停止按钮。 | App/task state 测试。 |

## 4. Display Copy

- full：`未调用匹配能力：能力库中没有匹配的可执行 Skill/能力，本次由通用 LLM 生成回答。`
- partial：`部分能力缺失：已调用部分能力；以下范围由通用 LLM 补充生成。`

正文中的披露文字仍由后端保留；前端 notice 是结构化提示，不得删除或隐藏正文披露。

## 5. Edge Cases

| 场景 | 期望 |
| --- | --- |
| SSE event 收到但 final metadata 缺失 | 本轮可保留运行态 notice；刷新后消失视为后端 bug，测试应覆盖。 |
| final history reload 返回 metadata | 以 metadata 为准恢复 notice。 |
| metadata 缺字段或字段类型异常 | 不显示 notice，不报错，正文照常展示。 |
| 旧前端不识别 event | 后端正文披露仍可让用户知道事实。 |

## 6. 测试计划

```bash
cd frontend && npm test -- --run
cd frontend && npm run typecheck
```

如修改 API 类型或示例，也应运行相关后端 API contract 测试。

## 7. 完成标准

- fallback 任务运行中和完成后均有结构化 notice。
- 刷新页面或重新打开会话后，notice 从 history metadata 恢复。
- 旧消息和异常 metadata 不破坏渲染。
