# 失败自检、恢复与 Fallback 控制层分步 PRD

本目录承接总纲 PRD：`docs/prd/backend/18-失败自检恢复与Fallback控制层PRD.md`。

总纲 PRD 定义统一目标、边界、fail-closed 原则、总体验收矩阵；本目录将实施范围拆成可独立计划、开发、测试和验收的步骤 PRD。

## 分步 PRD 索引

| 顺序 | PRD | 范围 | 实施优先级 |
|---|---|---|---|
| 1 | `18-01-节点执行保护壳PRD.md` | 节点异常归一、retry、timeout、attempt 审计、取消中停止 retry、node.failed 接入 replan | P0 |
| 2 | `18-02-前端恢复体验PRD.md` | SSE 自动重连、replay 去重、artifact retry、upload warning、文件类 Skill 缺附件 fail early 前后端契约 | P1 |
| 3 | `18-03-审计与Sidecar可靠性PRD.md` | audit sink 失败隔离、审计故障诊断、RuntimeSidecar enforce bounded retry、shadow/enforce 边界 | P1 |
| 4 | `18-04-LLMProviderFallback策略PRD.md` | Planner provider fallback、主代理 provider failure 友好错误、可选 backup provider chain、成本边界 | P2 |
| 5 | `18-05-端到端验收与RolloutPRD.md` | 总体验收矩阵、分阶段 rollout、回滚策略、e2e 场景、release gate | Release gate |

## 执行规则

- 不得绕过总纲 PRD 的 fail-closed 原则。
- 每份步骤 PRD 可以独立进入实施计划，但必须保持与其他步骤的事件、状态、配置和测试契约一致。
- 默认配置必须保持当前系统行为：节点 `max_attempts=1`、planner fallback disabled、main-agent provider fallback disabled。
- 涉及 Rust 依赖、Cargo.lock、native/deny.toml 或供应链策略时，必须按仓库 License Requirement 运行 cargo-deny 相关检查。
