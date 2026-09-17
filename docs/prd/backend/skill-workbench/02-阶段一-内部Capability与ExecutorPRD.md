# 阶段一：Workbench 内部 Capability 与 Executor PRD

- **编号**：后端 PRD 22-Phase 1
- **日期**：2026-06-25
- **状态**：待实施
- **上游依赖**：阶段零 Workbench policy / output contract
- **下游阶段**：阶段二 Runtime Workbench Loop、阶段三脱敏门禁
- **目标模块**：`src/capabilities/workbench/`、`src/api/runtime.py`、`CapabilityRegistry`、`InstanceRegistry`、`CapabilityExecutor`、`tests/capabilities/workbench/`

## 1. 阶段目标

把 Workbench 从“策略决策”推进到“可执行的内部平台 capability”，但仍不要求接入 Skill plan：

1. 注册 `workbench.*` descriptors，全部 `public=False`。
2. 提供本地 internal instance 和 `WorkbenchExecutor`。
3. 每个 executor 只消费安全摘要、metadata 和 contract 摘要，不读取完整文件或完整 rows。
4. 所有输出都通过 `WorkbenchOutputContractV1` 和敏感字段 sanitizer。
5. consumer contract tests 证明 Workbench 不出现在 public capability list、planner prompt 或 public payload policy。

## 2. 范围

### In scope

- 新建 `src/capabilities/workbench/`。
- descriptors：`workbench.data_profile`、`workbench.schema_match`、`workbench.preflight_validate`、`workbench.domain_validate`、`workbench.artifact_inspect`、`workbench.report_verify`。
- executor 返回 `CapabilityExecutionResult.output_payload`。
- executor 默认不生成用户可下载 artifact。
- 输出 required 字段和禁止字段验证。
- timeout / digest size 上限的最小实现或配置接入。
- runtime wiring：descriptor 注册、internal execution instance、CompositeExecutor 顺序、scheduler 可选中。

### Out of scope

- 不自动插入 Workbench nodes。
- 不接 finalizer prompt dependency context。
- 不实现 runtime replanner。
- 不扩展 Skill contract `quality_workbench`。
- 不把内部诊断暴露为 public API。

## 3. Descriptor 要求

每个 descriptor 必须满足：

| 字段 | 要求 |
| --- | --- |
| `capability_id` | `workbench.<stage>` |
| `public` | `False` |
| `kind` | `workbench` |
| `source` | `builtin` |
| planner payload policy | 不注册 |
| instance | 只支持 internal 本地执行 |

`CapabilityRegistry.list(public_only=True)` 不得返回任何 `workbench.*`。Planner prompt 和 LLM runtime replanner prompt 不得包含 `workbench.*`。

## 4. Executor 行为

Workbench executor 是平台层 digest / validation，不是业务算法：

| Stage | 输入边界 | 输出边界 |
| --- | --- | --- |
| `data_profile` | artifact metadata、input schema 摘要、上游 output 摘要 | 数据规模、文件/文本类型、字段候选、缺失摘要 |
| `schema_match` | schema id、contract 摘要、data profile | matched / missing / ambiguous 字段、置信度、需补信息 |
| `preflight_validate` | Skill contract 摘要、selected schema 元信息、output contract 元信息、artifact metadata、resource policy、platform policy | metadata-only 可执行性、阻断原因、warning |
| `domain_validate` | output digest、domain policy、request intent 摘要 | domain warnings、blocking errors、边界说明 |
| `artifact_inspect` | artifact metadata、output contract | artifact 完整性、缺失项、类型摘要 |
| `report_verify` | Skill output digest、artifact inspect、domain validate | report completeness、finalizer highlights / caveats |

禁止行为：

- 不读取完整文件内容、完整 rows、schema DDL、SQL、storage key、本地路径或最终 resolved input payload。
- 不创建前端可展示 artifact。
- 不把 handler、runtime、entrypoint、路径、storage ref 写入 output。
- 不调用 LLM。

## 5. 输出契约

每个 executor 必须返回：

```json
{
  "schema_version": "workbench.output.v1",
  "workbench_kind": "artifact_inspect",
  "target_capability_id": "skill.<id>",
  "target_node_id": "task:skill_execute",
  "summary": "短摘要",
  "satisfaction": {
    "satisfied": true,
    "reason_code": "verified | warning | missing_required_output | artifact_missing | domain_boundary",
    "replan_recommended": false
  },
  "highlights": [],
  "caveats": [],
  "structured_content": {
    "safe_digest": {},
    "blocking": false,
    "confidence": "low | medium | high"
  }
}
```

`summary` 和 `safe_digest` 必须有大小上限；超限时 fail closed 或裁剪并记录 caveat，不能静默输出超大 digest。

## 6. 测试计划

| 测试 | 断言 |
| --- | --- |
| internal descriptors | `workbench.*` descriptor `public=False`、`kind=workbench`、`source=builtin`。 |
| public list isolation | public capability list 不返回 `workbench.*`。 |
| planner isolation | planner prompt 不包含 `workbench.*`。 |
| executor contract | 每个 stage 输出 required 字段完整，`workbench_kind` 与 capability 对齐。 |
| forbidden field sanitizer | executor 输出含禁止字段时失败或剔除。 |
| no frontend artifact | executor 不创建用户可下载 artifact。 |
| metadata-only inspection | `artifact_inspect` 只消费 artifact metadata，不读取文件原文。 |
| preflight metadata-only | `preflight_validate` 不读取最终 resolved input、完整文件、rows、storage key 或本地路径。 |

推荐命令：

```bash
python -m pytest tests/capabilities/workbench/
python -m pytest tests/api/test_route_contract.py -k capabilities
python -m pytest tests/orchestration/ -k planner
```

## 7. 阶段验收

- `workbench.*` 已可内部执行，但对 public API 和 planner 不可见。
- 所有 stage 输出满足 `WorkbenchOutputContractV1`。
- 禁止字段和 digest size 上限有自动化测试。
- 本阶段完成后仍不改变现有 Skill plan；DAG 接入留到阶段二。
