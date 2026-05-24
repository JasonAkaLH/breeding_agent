# 18-05 端到端验收与 Rollout PRD

- **项目**：breeding_agent
- **范围**：失败自检、恢复与 Fallback 控制层整体 release gate、回归矩阵、灰度与回滚
- **文档状态**：分步 PRD（继承总纲 PRD 18）
- **日期**：2026-05-24
- **上游总纲**：`docs/prd/backend/18-失败自检恢复与Fallback控制层PRD.md`

## 1. 背景

18-01 至 18-04 分别覆盖后端执行保护、前端恢复、审计/sidecar 可靠性和 LLM provider fallback 策略。本 PRD 定义整体交付验收、rollout 顺序、回滚方式和 release gate，防止分步实现后出现事件契约不一致或默认策略偏离总纲。

## 2. 目标

- 汇总 18-01 至 18-04 的端到端验收矩阵。
- 定义分阶段 rollout 和回滚方式。
- 明确 release gate 与文档同步要求。
- 确保默认配置保持当前行为和 fail-closed 边界。

## 3. 非目标

- 不新增新的功能范围。
- 不替代各步骤 PRD 的详细实现验收。
- 不放宽安全、权限、schema、contract、enforce 失败边界。

## 4. Rollout 阶段

| 阶段 | 前置 | 行为 | 回滚方式 |
|---|---|---|---|
| Phase 1：后端保护壳 | 18-01 实现和测试通过 | 默认 `max_attempts=1`，只改变异常归一和审计；不打开额外 retry | 关闭新增 retry 分支，保留外层 task failed 收束 |
| Phase 2：前端恢复体验 | 18-02 实现和测试通过 | 打开 SSE reconnect、artifact retry、upload warning | 前端 feature flag 或配置回退到一次性状态查询 |
| Phase 3：audit / sidecar 可靠性 | 18-03 实现和测试通过 | audit sink failure 隔离；sidecar enforce 接入 bounded retry | sidecar retry helper 回 disabled；audit 隔离可保留 |
| Phase 4：LLM fallback 策略 | 18-04 实现和测试通过 | 默认只优化失败文案；provider fallback 仍 disabled | 配置保持 disabled |
| Phase 5：选择性启用 provider fallback | 运维确认 provider、成本、审计 | 仅按配置打开 planner/main-agent fallback | 配置回 disabled |

## 5. 端到端验收矩阵

| 场景 | 期望结果 | 覆盖 PRD |
|---|---|---|
| 能力抛异常 | node failed，任务可按 replan 预算继续或失败 | 18-01 |
| 可重试错误一次失败一次成功 | attempt 审计完整，最终 node completed | 18-01 |
| retry backoff 中取消任务 | 不再发起新 attempt，取消流程接管 | 18-01 |
| SSE 断开时任务仍运行 | 前端 reconnecting，重连后 replay 去重 | 18-02 |
| artifact 首次加载失败 | 显示 retry，二次成功替换气泡 | 18-02 |
| 普通任务附件缺失 | upload warning 可见，任务继续 | 18-02 |
| 文件类 Skill 附件缺失 | fail early，不创建长任务 | 18-02 |
| audit sink 抛异常 | SSE 仍投递，记录降噪诊断 | 18-03 |
| sidecar enforce transient | bounded retry 成功或耗尽 fail closed | 18-03 |
| planner provider failure fallback disabled | planning failed | 18-04 |
| planner provider failure fallback enabled | main_agent_only，metadata/audit 标记 | 18-04 |
| main-agent provider failure | 用户看到模型服务不可用 | 18-04 |
| backup provider 成功 | response metadata/audit 标记 fallback | 18-04 |

## 6. 全量测试要求

实施完成后，至少运行或说明无法运行的验证：

```bash
conda run -n multi_agent python -m unittest discover -s tests/core -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/storage -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/lifecycle -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/integrations -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/main_agent -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/e2e -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/observability -p 'test_*.py'
cd frontend && npm test -- --run && npm run build
```

涉及 Rust 依赖、`Cargo.lock`、`native/deny.toml` 或供应链策略时，必须额外运行 License Requirement 要求的 cargo-deny 相关检查。

## 7. 文档同步要求

每个阶段完成时必须更新：

- 对应分步 PRD 的状态或实现证据。
- 总纲 PRD 如有契约变化。
- `CHANGELOG.md`。
- 如新增配置项，更新相关运行配置说明或 README。
- 如新增前端用户文案，确保中文文案与测试一致。

## 8. Release gate

全部阶段进入完成态前必须满足：

- 所有默认配置保持 fail-closed 和现有行为兼容。
- 所有新增事件 / metadata / audit 字段脱敏。
- 所有前端恢复状态不污染正式历史正文。
- provider fallback 未经显式配置不得启用。
- sidecar enforce 最终失败不得回退 Python。
- 所有 key acceptance tests 通过或有明确环境不可用说明。

## 9. 回滚原则

- 回滚优先走配置关闭，而不是删除状态数据。
- 已持久化的新增事件必须向后兼容；旧前端或旧任务读取时不得报错。
- provider fallback 和 Skill retry 均可独立关闭。
- sidecar retry 关闭后仍保持 enforce fail-closed。

## 10. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 分阶段实现导致契约不一致 | 以本 PRD 的端到端验收矩阵作为 release gate |
| 默认策略误开启 fallback | 配置默认 disabled，测试覆盖缺配置行为 |
| 回滚后旧任务无法显示 | 新事件和状态必须向后兼容 |
| 测试矩阵过重 | 每阶段先跑对应分层测试，release 前跑全量矩阵 |
