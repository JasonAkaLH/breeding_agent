# 18-04 LLM Provider Fallback 策略 PRD

- **项目**：breeding_agent
- **范围**：Planner provider fallback、主代理 provider failure 策略、可选 backup provider chain
- **文档状态**：分步 PRD（继承总纲 PRD 18）
- **日期**：2026-05-24
- **上游总纲**：`docs/prd/backend/18-失败自检恢复与Fallback控制层PRD.md`

## 1. 背景

Planner provider failure 当前保持 fail closed；主代理 LLM failure 当前返回结构化错误和审计事件。LLM fallback 不同于普通 retry，它可能改变任务路线、模型、输出质量、成本和用户信任，因此必须显式配置、显式审计、显式 metadata。

## 2. 目标

- 保持 provider fallback 默认关闭。
- Planner provider failure 可选降级为 `main_agent_only`，但仅在配置启用时。
- Planner invalid output、非法能力、显式 Skill 失败、权限失败不得 fallback。
- 主代理 provider failure 默认展示更明确“模型服务暂时不可用”文案。
- 可选 backup provider chain 必须有审计、metadata 和成本边界。

## 3. 非目标

- 不默认启用任何 provider failover。
- 不把模板化错误提示作为正常 assistant 成功消息写入历史。
- 不让 planner provider failure fallback 到业务 Skill 自动路线。
- 不对 prompt 构造错误、schema 错误、权限错误做 failover。

## 4. Planner provider fallback

配置：

```yaml
planner:
  provider_failure_fallback: disabled | main_agent_only
```

默认：`disabled`。

### disabled

Planner provider failure 仍返回 planning failed。

### main_agent_only

仅 provider / network / timeout failure 可触发。触发后：

- 使用确定性主代理单节点计划。
- task metadata 写入 `planner_source=fallback`、`planner_fallback_reason=provider_failed`、`original_planner_error_type`。
- 写 audit-only 事件 `planner.provider_fallback`。

禁止触发：

- planner 输出非法。
- planner 选择非法能力。
- 用户显式指定 Skill 失败。
- 认证或权限失败。

## 5. 主代理 provider failure

默认策略：

- 返回结构化失败。
- 前端展示：`模型服务暂时不可用，请稍后重试。`
- 如果节点 retry 开启，可由节点保护壳重试一次。
- 不生成伪成功业务回答。

## 6. 可选 backup provider chain

配置：

```yaml
main_agent:
  provider_fallback:
    mode: disabled | failover
    chain:
      - primary
      - backup_a
```

默认：`disabled`。

启用 failover 后：

- 仅 provider/network/timeout transient 可切换 backup。
- 成功后写 metadata：`response_provider`、`provider_fallback_used=true`、`provider_fallback_reason=primary_failed`。
- 写 audit-only 事件 `main_agent.provider_fallback`。
- 审计不得包含 prompt 原文、API key、base_url、secret。

## 7. 成本边界

- 未配置 backup provider 时，可考虑 main-agent 节点 `max_attempts=1 或 2`。
- 配置 backup provider 时，推荐 provider chain = primary + 1 backup，节点 `max_attempts=1`。
- 禁止出现不受控组合，例如 3 providers * 3 attempts。

## 8. 用户可见语义

| 场景 | 用户表现 | 内部证据 |
|---|---|---|
| Planner fallback 到 main_agent_only | 用户可不感知 | task metadata + audit |
| 主代理 backup provider 成功 | 用户可不感知 provider 名称 | response metadata + audit |
| 主代理所有 provider 失败 | 明确模型服务不可用 | structured failure + audit |

## 9. 验收标准

- planner disabled 走主代理。
- planner provider failure fallback disabled -> planning failed。
- planner provider failure fallback enabled -> main agent only。
- planner invalid output 不 fallback。
- planner 选择非法能力不 fallback。
- explicit Skill failure 不 fallback。
- main agent provider failure 显示模型服务不可用。
- main agent provider failure + 节点 retry 开启时可重试。
- backup provider 成功记录 metadata。
- backup provider 失败返回结构化失败。
- provider fallback 审计脱敏。
- retry 与 provider chain 不组合爆炸。

## 10. 测试要求

- `tests/orchestration` 覆盖 planner provider failure 与 invalid output 差异。
- `tests/api` 覆盖用户可见错误文案。
- `tests/capabilities/main_agent` 覆盖 provider failure、backup success、backup failure。
- fake LLM runtime 必须支持 provider failure、invalid output、timeout、backup success。

## 11. 风险与缓解

| 风险 | 缓解 |
|---|---|
| fallback 改变任务意图 | Planner 只允许 fallback 到 main_agent_only，不自动选业务 Skill |
| 成本失控 | provider chain 和 node retry 有组合上限 |
| 用户误以为是主路径结果 | metadata + audit 标记 fallback 来源 |
| 低质量模板回答污染历史 | 模板只作为错误提示，不作为成功 assistant message |
