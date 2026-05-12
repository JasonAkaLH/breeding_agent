# Capability 接入指南

本文回答一个具体问题：**后续要加入新的 capability 要怎么做，都要在哪里加入什么？**

当前框架的原则是：新增 capability 必须适配主代理编排标准，而不是让 orchestration 内核反向适配某个具体业务能力。SQLQuery 只是首个宏能力示例；后续 capability 应通过同一套 registry、executor、workflow provider、planner payload allowlist 接入。

---

## 1. 先判断 capability 类型

新增前先把能力归入以下两类之一：

| 类型 | 说明 | 例子 | 是否需要 macro provider |
|---|---|---|---|
| 单节点 public capability | LLM Planner 可以直接规划该 capability，执行时就是一个节点 | 未来的 `report.generate`、`file.analyze` | 通常不需要 |
| public macro capability + internal 子工作流 | 对外只暴露一个高层 capability，内部展开成多个固定节点 | 当前 `sql_query.query` 展开为 intent route / schema / generate / guard / execute / result filtering | 需要 |

判断规则：
- 如果用户只需要一个原子能力，且该能力的输入输出边界清晰，用单节点 public capability。
- 如果能力内部有安全边界、固定 DAG、多个步骤或不应暴露给 LLM Planner 的实现节点，用 public macro capability。
- Internal capability 必须 `public=False`，不能让 LLM Planner 直接选择。

---

## 2. 代码目录应该加在哪里

新增 capability 默认放在：

```text
src/capabilities/<capability_name>/
```

建议最小结构：

```text
src/capabilities/<capability_name>/
  __init__.py
  workflow.py          # descriptor / payload policy / workflow provider；实例 builder 可放这里或 executor.py
  executor.py          # ExecutorPort 适配，分发到具体 CapabilityContract
  <node_or_logic>.py   # 具体业务节点，可按能力拆分
```

如果是复杂宏能力，可以按 SQLQuery 的方式拆成多个节点文件：

```text
src/capabilities/sql_query/
  workflow.py
  executor.py
  intent_route.py
  schema_context_prepare.py
  sql_generate.py
  sql_guard.py
  sql_execute_readonly.py
  result_filtering.py
```

---

## 3. `workflow.py` / capability 包必须声明什么

`workflow.py` 通常是 capability 接入主框架的第一入口，至少声明：

1. public capability descriptor；
2. 如有内部节点，声明 internal descriptors；
3. planner payload allowlist；
4. workflow provider。

capability 包还必须导出 local execution instance builder。推荐把 builder 放在 `workflow.py`，但不是强制要求；例如当前 SQLQuery 的 `build_local_sql_query_instance()` 放在 `executor.py`，再由 `src/capabilities/sql_query/__init__.py` 统一导出。runtime 装配只依赖 capability 包的导出契约，不依赖 builder 的具体文件位置。

示例：

```python
from src.orchestration.models import (
    CapabilityDescriptor,
    ExecutionInstance,
    InstanceState,
    OrchestrationRequest,
    WorkflowNodePlan,
    WorkflowPlan,
)
from src.orchestration.planner_payload_policy import CapabilityPayloadPolicy

REPORT_PUBLIC_CAPABILITY_DESCRIPTORS = (
    CapabilityDescriptor(
        capability_id="report.generate",
        name="Report Generator",
        description="Generate a structured business report from the user request and upstream capability outputs.",
        public=True,
    ),
)

REPORT_PLANNER_PAYLOAD_POLICIES = {
    "report.generate": CapabilityPayloadPolicy(
        planner_allowed_fields=("format", "max_sections"),
        system_payload_factory=lambda request: {
            "topic": request.user_message,
        },
    ),
}

class ReportWorkflowProvider:
    def build_plan(self, request: OrchestrationRequest) -> WorkflowPlan:
        node_id = f"{request.task_id}:report.generate"
        return WorkflowPlan(
            task_id=request.task_id,
            nodes=(
                WorkflowNodePlan(
                    node_id=node_id,
                    capability_id="report.generate",
                    input_payload={"topic": request.user_message},
                    retry_policy={"max_attempts": 1},
                    timeout_policy={"seconds": 60},
                ),
            ),
            metadata={"route": "report"},
        )


def build_local_report_instance(*, instance_id: str = "inst-report-local") -> ExecutionInstance:
    return ExecutionInstance(
        instance_id=instance_id,
        supported_capabilities=("report.generate",),
        state=InstanceState.ONLINE,
        load_score=0,
    )
```

### Payload allowlist 规则

LLM Planner 只能决定拓扑和少量明确允许的结构化字段。执行前所有 planner `input_payload` 都会经过 per-capability allowlist：

- `planner_allowed_fields`：允许从 LLM Planner 输出进入执行图的字段；
- `system_payload_factory`：从可信 `OrchestrationRequest` 生成系统字段；
- 系统字段优先级高于 planner 字段；
- 未配置 policy 的 capability 默认 fail-closed，planner payload 会被全部丢弃。

因此，**不要在 `LLMWorkflowProvider` 里为新 capability 写 payload 特判**。新 capability 的 payload 策略应该跟随 descriptor 一起注册进 `CapabilityRegistry`。

---

## 4. `executor.py` 要做什么

每个 capability executor 要实现 `ExecutorPort`，并把 capability id 分发到具体 `CapabilityContract`。

示例：

```python
from src.core.contracts import (
    CapabilityContract,
    CapabilityExecutionRequest,
    CapabilityExecutionResult,
    ExecutorPort,
)

class ReportGenerateCapability(CapabilityContract):
    capability_id = "report.generate"
    version = "1"
    description = "Generate a structured report."

    async def execute(self, request: CapabilityExecutionRequest) -> CapabilityExecutionResult:
        topic = str(request.input_payload.get("topic") or "")
        # 这里写业务逻辑；I/O 调用优先 async。
        return CapabilityExecutionResult(
            capability_id=request.capability_id,
            task_id=request.task_id,
            node_id=request.node_id,
            output_payload={"summary": f"Report for {topic}"},
            artifacts=(),
            events=(),
        )

class ReportExecutor(ExecutorPort):
    def __init__(self) -> None:
        self._capabilities: dict[str, CapabilityContract] = {
            "report.generate": ReportGenerateCapability(),
        }

    def supports(self, capability_id: str) -> bool:
        return capability_id in self._capabilities

    async def execute(self, request: CapabilityExecutionRequest) -> CapabilityExecutionResult:
        capability = self._capabilities.get(request.capability_id)
        if capability is None:
            raise ValueError(f"Unsupported report capability_id: {request.capability_id}")
        return await capability.execute(request)
```

如果 capability 会访问外部系统，依赖应该通过 executor 构造参数注入，方便测试里传 fake adapter / fake generator；不要在测试默认路径访问真实外部服务。

---

## 5. `__init__.py` 要导出什么

`src/capabilities/<capability_name>/__init__.py` 应导出 runtime 装配需要的对象：

```python
from .executor import ReportExecutor, ReportGenerateCapability
from .workflow import (
    REPORT_PLANNER_PAYLOAD_POLICIES,
    REPORT_PUBLIC_CAPABILITY_DESCRIPTORS,
    ReportWorkflowProvider,
    build_local_report_instance,
)

__all__ = [
    "REPORT_PLANNER_PAYLOAD_POLICIES",
    "REPORT_PUBLIC_CAPABILITY_DESCRIPTORS",
    "ReportExecutor",
    "ReportGenerateCapability",
    "ReportWorkflowProvider",
    "build_local_report_instance",
]
```

---

## 6. `src/api/runtime.py` 要在哪里接入

当前真实后端 runtime 在 `build_api_runtime()` 中集中装配 capability。新增 capability 后通常要改这里。

### 6.1 导入 capability 对象

在 `src/api/runtime.py` 顶部导入：

```python
from src.capabilities.report import (
    REPORT_PLANNER_PAYLOAD_POLICIES,
    REPORT_PUBLIC_CAPABILITY_DESCRIPTORS,
    ReportExecutor,
    ReportWorkflowProvider,
    build_local_report_instance,
)
```

如有 internal descriptors，也一起导入。

### 6.2 注册 descriptors 和 payload policy

在 `build_api_runtime()` 创建 `CapabilityRegistry()` 后注册：

```python
_register_capability_descriptors(
    capability_registry,
    REPORT_PUBLIC_CAPABILITY_DESCRIPTORS,
    planner_payload_policies=REPORT_PLANNER_PAYLOAD_POLICIES,
)
```

如果有 internal descriptors：

```python
_register_capability_descriptors(capability_registry, REPORT_INTERNAL_CAPABILITY_DESCRIPTORS)
```

注意：
- public descriptors 会出现在 LLM Planner 的 public capability 清单里；
- internal descriptors 只用于系统内部展开后的 validator / scheduler / executor，不会暴露给 LLM Planner；
- allowlist 跟 registry 绑定，后续 Planner 会动态读取。

### 6.3 注册 execution instance

在 `InstanceRegistry()` 后注册：

```python
instance_registry.register(build_local_report_instance())
```

### 6.4 加入 CompositeExecutor

在 `CompositeExecutor([...])` 中加入：

```python
CompositeExecutor(
    [
        MainAgentExecutor(...),
        SQLQueryExecutor(...),
        ReportExecutor(...),
    ]
)
```

### 6.5 如果是 macro capability，加入 macro provider

如果 public capability 会展开内部 DAG，需要加入 macro provider map：

```python
report_workflow_provider = ReportWorkflowProvider()

default_workflow_provider = LLMWorkflowProvider(
    capability_registry=capability_registry,
    fallback_provider=auto_workflow_provider,
    macro_providers={
        "sql_query.query": sql_query_workflow_provider,
        "report.generate": report_workflow_provider,
    },
    ...
)
```

如果 deterministic fallback 也要用它，`AutoWorkflowProvider` 的 `macro_providers` 也要同步传入。

如果它是单节点 public capability，则无需 macro provider；Planner 输出的节点会直接进入执行图，由 scheduler + executor 执行。

### 6.6 显式 capability 路由是否要改

默认产品路径应继续使用 `capability_id=None`，让 `LLMWorkflowProvider` / `AutoWorkflowProvider` 自动规划，并在需要时由 `main_agent.respond` 收束最终回答。

如果新 capability 还要支持调试或兼容用的显式 `capability_id` 调用，需要检查 `src/orchestration/workflow_router.py`：

- 当前 `main_agent` / `main_agent.*` 会路由到 `MainAgentWorkflowProvider`；
- 当前 `sql_query` / `sql_query.query` 会直接路由到 `SQLQueryWorkflowProvider`；
- 显式 `sql_query.query` 只运行 SQLQuery 内部六节点，不会自动追加 `main_agent.respond`；
- 如果未来显式调用某个 capability 也必须生成主代理最终回答，应让对应 provider / router 返回含 `main_agent.respond` 的高层 DAG，并补 `tests/orchestration/test_workflow_router.py` 与 API 测试。

---

## 7. LLM Planner 要不要改

一般不需要改 `src/orchestration/llm_workflow_provider.py`。

LLM Planner 当前会从 `CapabilityRegistry.list(public_only=True)` 动态读取 public capability 清单，并从 `CapabilityRegistry.planner_payload_policies()` 动态读取 payload allowlist。也就是说：

- 新 capability 只要注册为 `public=True`，Planner prompt 就能看到；
- 新 capability 只要注册了 `CapabilityPayloadPolicy`，Planner prompt 就会看到允许的 payload 字段；
- 执行前统一由 `PlannerPayloadPolicy` 过滤 payload。

通常只需要把 capability 的 `description` 写清楚，让 planner 知道什么时候选它。

不应该做的事：
- 不要在 `LLMWorkflowProvider` 中新增 `if capability_id == "xxx"` 的 payload 特判；
- 不要把 internal capability 暴露给 Planner；
- 不要让 Planner 生成敏感字段、账号字段、连接信息或原始 SQL 等高风险 payload。

只有当“所有 capability 都共享的新规划规则”发生变化时，才考虑改 `src/orchestration/planner_contract.py`。如果只是某个业务能力的选择提示，优先通过 descriptor description、测试样例、必要时 deterministic fallback 实现。

---

## 8. AutoWorkflowProvider 什么时候要改

`src/orchestration/auto_workflow_provider.py` 是 LLM Planner 不可用或输出非法时的确定性 fallback。

新增 capability 后是否要改它，取决于产品要求：

- 如果新能力必须在 planner 失败时也能自动触发，需要在 `AutoWorkflowProvider` 增加明确、可测试的启发式规则；
- 如果新能力只依赖 LLM Planner 自动选择，可以不改；planner 失败时会回退到当前默认路径；
- 启发式规则应该基于用户意图和领域关键词，不要依赖前端手动 capability 选择。

如果改了 `AutoWorkflowProvider`，必须新增 `tests/orchestration/test_auto_workflow_provider.py` 覆盖：

- 命中新 capability；
- 普通问题不误触发；
- 与 SQLQuery 等既有能力不冲突。

---

## 9. 主代理最终收束与上游结果契约

SQLQuery 接入主代理的标准不是“主代理直接调用 SQLQuery 内部节点”，而是 **public DAG + macro expansion + dependency context**：

1. 高层 DAG 先选择数据能力，例如 `sql_query.query`；
2. `WorkflowExpander` 把 public macro 展开成内部固定 workflow；
3. 下游 `main_agent.respond` 依赖 macro tail，例如 SQLQuery 的 `sql_query.result_filtering`；
4. `OrchestrationService` 把已完成依赖节点的 `output_payload` 作为 `dependency_outputs` 传给主代理；
5. `src/capabilities/main_agent/prompt_builder.py` 对 dependency output 做 allowlist 脱敏后注入主代理 prompt。

因此，新增 capability 如果需要由主代理最终汇总，应确认：

- 初始 planner / fallback / runtime replan 输出的高层 DAG 中，`main_agent.respond` 依赖该 capability 的尾节点；
- capability 的 `output_payload` 包含适合主代理消费的安全字段，例如 `summary`、`response_text`、`row_count`、`columns`、`rows`、`highlights`、`caveats` 等；
- 如果需要新增主代理可见字段，必须同步更新 `build_dependency_context()` / `_sanitize_dependency_output()` 的 allowlist，并补主代理 prompt 或 API 集成测试；
- 不要把原始 SQL、连接信息、guard token、完整大表 rows、完整 prompt、文件路径或 secret 直接放进主代理 dependency context；
- 如果 capability 已产出前端专用 artifact，仍建议提供一个简短 `summary` 或结构化摘要，避免主代理只能看到 artifact id 而无法回答。

SQLQuery 的验证参考：

- `tests/orchestration/test_workflow_expander.py`：macro tail 会重连到下游主代理；
- `tests/orchestration/test_llm_workflow_provider.py`：Planner 缺主代理时会补 finalizer，主代理未依赖数据能力时会重连；
- `tests/api/test_main_agent_llm.py`：默认数据库问题最终 prompt 包含“上游能力结果上下文”。

---

## 10. 运行时重编排接入

如果 capability 的节点输出可能出现“当前结果不满足，但系统内可补足”的情况，可以接入运行时重编排；否则不要为了形式引入 replanner。

当前有两层可参考：

- `src/capabilities/main_agent/runtime_replanner.py`：通用主代理 LLM 观察 / 重排 advisor，只读取脱敏后的 node output，并只允许输出 public capability 高层 DAG；
- `src/capabilities/sql_query/runtime_replanner.py`：SQLQuery capability 自己持有领域规则，识别多子问题拆分，并返回多个 public `sql_query.query` 分支 + `main_agent.respond` 汇总。

新增 capability 如需专属 runtime replanner，应遵守：

- replanner 放在 capability 包内，业务语义不要写进 `src/orchestration/` 通用层；
- `build_replan()` 只返回 public capability 组成的 revised plan，再由 `WorkflowExpander` 展开 macro；
- 必须尊重 `max_replans` / `max_dynamic_nodes`，避免无限重排；
- observation / prompt 只使用必要的脱敏输出字段，禁止泄露 SQL、secret、完整 rows、完整 prompt 或外部系统凭据；
- 在 `src/api/runtime.py` 的 `CompositeRuntimeReplanner([...])` 中显式装配；
- 补 `tests/orchestration/test_runtime_*.py`，覆盖触发、不触发、预算、非法 revised plan 被拒绝、最终仍由主代理汇总等路径。

---

## 11. API / 前端是否要改

### 后端 API

通常不需要新增 API。默认对话入口应继续让用户不选 capability，由主代理自动规划。

只有以下情况才需要改 API DTO / routes：
- 新 capability 需要上传文件、特殊 metadata 或新的控制参数；
- 新 capability 产生新的前端可见事件类型；
- 新 capability 需要新的 artifact 查询或下载接口。

### 前端

前端默认不应该恢复“让用户手动选择 capability”的产品形态。

只有当新 capability 有特殊展示结果时，才需要在 `frontend/` 增加：

- API type / event reducer 支持；
- artifact parser；
- 结果卡片组件；
- 对应 Vitest / React Testing Library 测试。

如果只是最终由 `main_agent.respond` 汇总成自然语言回答，前端可以不改。

---

## 12. 测试要补哪些

新增 capability 至少补以下测试面：

| 测试层 | 建议路径 | 必测内容 |
|---|---|---|
| capability 单元测试 | `tests/capabilities/<capability_name>/` | 输入 payload、业务分支、错误/clarify/fallback、安全边界 |
| workflow / planner 测试 | `tests/orchestration/` | descriptor public/internal、macro expansion、payload allowlist、Planner 可选择该 capability、主代理 finalizer / 依赖重连 |
| runtime replan 测试 | `tests/orchestration/test_runtime_*.py` | 只有接入 runtime replanner 时需要；覆盖触发、不触发、预算与 public-only revised DAG |
| API 集成测试 | `tests/api/` | 默认提交消息后能规划并执行；planner 失败时 fallback 行为合理；需要主代理汇总时 prompt 能看到上游结果上下文 |
| e2e / observability | `tests/e2e/`、`tests/observability/` | 关键 happy path、事件和 audit 输出 |
| 前端测试 | `frontend/src/**/*.test.ts(x)` | 只有新增 UI / artifact 展示时需要 |

新增或修改运行时行为时，优先 TDD：先补失败测试，再改实现。

当前常用回归命令：

```bash
conda run -n multi_agent python -m unittest discover -s tests/orchestration -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/capabilities/<capability_name> -p 'test_*.py'
conda run -n multi_agent python -m unittest discover -s tests/api -p 'test_*.py'
```

如果涉及前端展示：

```bash
cd frontend
npm test -- --run
npm run build
```

---

## 13. 文档要同步哪里

新增 capability 时至少同步：

1. `CHANGELOG.md`：记录能力目的、接入点、验证结果；
2. `docs/prd/backend/`：如果是新业务能力或新架构边界，补 PRD 或专题说明；
3. 本指南：如果接入流程发生变化，更新本文件；
4. `README.md` / `AGENTS.md`：只有新增标准命令、目录职责或长期规则时才改。

---

## 14. 最小接入检查清单

新增 capability 完成前，逐项确认：

- [ ] 已放在 `src/capabilities/<capability_name>/`；
- [ ] 已声明 public descriptor；
- [ ] internal 节点如存在，均 `public=False`；
- [ ] 已声明 `CapabilityPayloadPolicy`，或者明确接受默认 fail-closed；
- [ ] 已实现 `ExecutorPort` 和具体 `CapabilityContract`；
- [ ] 已在 `src/api/runtime.py` 注册 descriptor / payload policy / execution instance / executor；
- [ ] macro capability 已加入 `LLMWorkflowProvider` 的 `macro_providers`；
- [ ] 如果支持显式 `capability_id` 调用，已确认 `WorkflowRouter` 语义并补测试；
- [ ] 需要 deterministic fallback 时，已更新 `AutoWorkflowProvider`；
- [ ] 如果默认自动路径要由主代理汇总，已确保 `main_agent.respond` 依赖能力尾节点；
- [ ] 如果主代理需要消费新字段，已更新 dependency context allowlist 并补测试；
- [ ] 如果接入 runtime replanner，已保证 public-only revised DAG、预算边界和 runtime replan 测试；
- [ ] LLM Planner 不需要任何 capability-specific payload 特判；
- [ ] 已补 capability / orchestration / API 测试；
- [ ] 已更新 changelog 和相关 docs；
- [ ] 已跑过对应分层回归。

---

## 15. 当前两个 capability 的参考位置

| 能力 | 参考文件 |
|---|---|
| 主代理 `main_agent.respond` | `src/capabilities/main_agent/workflow.py`、`src/capabilities/main_agent/executor.py` |
| SQLQuery public macro `sql_query.query` | `src/capabilities/sql_query/workflow.py`、`src/capabilities/sql_query/executor.py` |
| Planner payload allowlist 通用实现 | `src/orchestration/planner_payload_policy.py` |
| Capability registry policy 挂载点 | `src/orchestration/registry.py` |
| LLM Planner provider | `src/orchestration/llm_workflow_provider.py` |
| Planner prompt / output parser | `src/orchestration/planner_contract.py` |
| Macro expansion | `src/orchestration/workflow_expander.py` |
| 显式 capability 路由 | `src/orchestration/workflow_router.py` |
| 主代理上游结果注入 | `src/capabilities/main_agent/prompt_builder.py` |
| Runtime replanner 参考 | `src/capabilities/main_agent/runtime_replanner.py`、`src/capabilities/sql_query/runtime_replanner.py` |
| Runtime 装配 | `src/api/runtime.py` |
