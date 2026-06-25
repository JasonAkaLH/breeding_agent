# 阶段四：Contract 质量策略与健康诊断 PRD

- **编号**：后端 PRD 22-Phase 4
- **日期**：2026-06-25
- **状态**：待实施
- **上游依赖**：阶段零至阶段三全部完成
- **目标模块**：`src/integrations/agent_skills/contract.py`、`src/integrations/agent_skills/skill_capabilities.py`、Skill runtime diagnostics、Skill builder / 文档、`tests/integrations/agent_skills/`

## 1. 阶段目标

在 runtime Workbench loop 稳定后，把平台静态策略迁移为 Skill contract 可选声明，并补齐健康诊断：

1. Skill contract 可选增加 `quality_workbench`。
2. Contract parser / dataclass / diagnostics 统一支持该字段。
3. `skill_capabilities.py` 将非法 Workbench 策略转换成 `SkillCapabilityDiagnostic`。
4. Health / diagnostics 能说明 Workbench 策略命中、非法字段、预算和降级原因。
5. Skill builder / 文档告诉维护者如何声明 Workbench stage、digest 需求和预算。

## 2. 范围

### In scope

- 新增 `SkillQualityWorkbenchContract` dataclass。
- `contract.py` parser 支持 `quality_workbench`。
- unknown field、invalid stage、invalid budget、invalid finalizer mode 诊断。
- `skill_capabilities.py` 将非法 `quality_workbench` 转成 `SkillCapabilityDiagnostic`。
- capability health payload 的 internal / audit-only 诊断。
- Skill builder / Skill 构建指南同步新增 optional `quality_workbench` 说明。

### Out of scope

- 不把 diagnostics 变成新的 public API，除非后续 PRD 明确要求。
- 不强制所有 Skill 立即声明 `quality_workbench`。
- 不把 Workbench 变成具体业务质量算法。
- 不改变阶段二 runtime loop 的 public/internal 安全边界。

## 3. Contract 扩展

Phase 4 可选支持：

```yaml
quality_workbench:
  enabled: true
  domain_kind: generic
  stages: [schema_match, artifact_inspect, report_verify]
  finalizer_digest_mode: when_finalizer_exists
  max_replans: 1
  max_dynamic_nodes: 3
```

建议 dataclass：

```python
class SkillQualityWorkbenchContract:
    enabled: bool = False
    domain_kind: str = "generic"
    stages: tuple[str, ...] = ()
    finalizer_digest_mode: str = "none"
    max_replans: int = 0
    max_dynamic_nodes: int = 0
```

## 4. Parser 与 diagnostics 规则

1. Contract 策略优先，平台静态策略作为兼容 fallback。
2. 非法 stage、非法预算、非法 finalizer mode 产生 `SkillCapabilityDiagnostic`。
3. 未声明 `quality_workbench` 的 Skill 不产生错误。
4. 默认不因非法 `quality_workbench` 破坏内置 Workbench capability 注册。
5. 对该 Skill 的 Workbench 行为按平台策略 fail closed 或禁用 Workbench；不得静默启用不合法 stage。
6. diagnostics 不得包含 raw contract body、path、handler、runtime、entrypoint、secret。

## 5. Skill 维护者文档要求

文档必须说明：

- `quality_workbench` 是 optional。
- stage 只允许平台定义的通用 stage。
- `domain_kind` 只用于选择通用质量策略，不得把具体业务算法写进主框架。
- `max_replans/max_dynamic_nodes` 是预算上限，不保证一定追加节点。
- Workbench digest 只承载验证摘要，不替代 Skill 自己的业务测试。

## 6. 测试计划

| 测试 | 断言 |
| --- | --- |
| contract dataclass parse | 合法 `quality_workbench` 解析为 `SkillQualityWorkbenchContract`。 |
| optional absent | 未声明字段的 Skill 保持兼容。 |
| invalid stage diagnostics | 非法 stage 产生 diagnostic，不静默忽略。 |
| invalid budget diagnostics | 负数或非整数预算产生 diagnostic。 |
| invalid finalizer mode diagnostics | 非法 finalizer mode 产生 diagnostic。 |
| capability diagnostics bridge | `skill_capabilities.py` 输出 `SkillCapabilityDiagnostic`。 |
| fallback policy | 无 contract 字段时仍可走平台静态策略 fallback。 |
| docs updated | Skill builder / 构建指南包含 optional `quality_workbench` 示例和边界。 |

推荐命令：

```bash
python -m pytest tests/integrations/agent_skills/ -k "contract or capability"
python -m pytest tests/orchestration/ -k "workbench"
```

## 7. 阶段验收

- `quality_workbench` 为 optional，合法配置可进入 Workbench policy。
- 非法配置有 diagnostics，不强制破坏现有 Skill 注册。
- Skill 维护者文档说明如何声明 stage、digest 需求和预算。
- Contract 策略与 runtime Workbench loop 的 public/internal、安全、answer mode 和预算边界一致。
