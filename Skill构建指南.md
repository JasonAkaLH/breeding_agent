# Skill 构建指南（适配本系统）

- **适用对象**：创建可被本项目后端 `main_agent.respond` / `SkillExecutor` 使用的 Skill 的开发者。
- **适配范围**：本系统的 Skill 兼容层，而不是通用本地 agent runtime。
- **当前实现入口**：项目 Skill 兼容层、`src/capabilities/main_agent/`、`src/capabilities/skill_tool/`、`src/api/runtime.py`。
- **更新时间**：2026-05-18

## 1. 一句话结论

本系统可以加载项目约定格式的 `SKILL.md`，并支持三类后端执行形态：

1. **instruction-only Skill**：匹配后把 Skill 正文注入 `main_agent.respond`。
2. **`python_subprocess` Skill**：受控执行 manifest 声明的 Python 脚本，并把 JSON 输出注入主代理。
3. **`platform_service` Skill**：仅限项目信任 Skill，通过 runtime 预注册 / allowlist 的 handler 绑定受控服务。

Rust 不是第四种 execution mode；Rust 只能作为 Skill-owned runtime 的内部实现，放在 `skill/<skill-name>/native/`，并通过本指南允许的 PyO3 wheel、native binary 或 sidecar adapter 接回 `platform_service` / 受控 handler contract。

但本系统**不是通用本地 agent runtime**：不会自动读取 Skill 的 `references/`，不会执行 Markdown 代码块，不支持 shell 脚本 / plugin runtime，不会在运行时 `cargo build` / 下载 Rust 依赖 / 执行任意 native binary，也不会给脚本继承完整本机环境变量或 secret。

## 2. Skill 放在哪里

默认 runtime 会扫描：

```text
<项目根目录>/skill/**/SKILL.md
```

测试或定制 runtime 可通过 `build_api_runtime(..., skill_roots=[...])` 或 `skill_catalog=...` 显式注入。

推荐目录：

```text
skill/my-skill/
  SKILL.md
  scripts/
    optional_auto_run.py
  native/                # 可选；仅项目级 trusted Rust Skill 使用
    Cargo.toml
    crates/
```

说明：
- `SKILL.md` 是唯一必需文件。
- `scripts/` 可选，仅当需要确定性处理或结构化预处理时使用。
- `native/` 可选，仅允许项目级 trusted Skill 放置 Rust source / adapter；普通用户级 Skill 不得要求后端自动编译或执行 Rust。
- `references/`、`assets/` 可以作为人工构建过程的辅助资源，但本系统后端不会自动加载它们给 LLM。

## 3. 兼容字段

### 3.1 `SKILL.md` 基本形态

```markdown
---
name: report-writer
description: 生成周报、月报和项目汇报材料
triggers:
  - 周报
  - 月报
  - 汇报材料
---

# Report Writer

当用户要求写周报、月报或项目汇报材料时：
1. 先识别时间范围、受众和素材是否足够。
2. 如果素材足够，按“摘要 / 进展 / 风险 / 下周计划”输出。
3. 如果缺少关键信息，只问一个最重要的问题。
```

### 3.2 Frontmatter 字段支持表

| 字段 | 是否建议 | 当前系统行为 |
|---|---:|---|
| `name` | 必填 | 解析必需；也参与匹配打分。 |
| `display_name` | 必填 | 用户可见名称；应使用简短、稳定、可读的中文或中英混合名称，不参与 capability id 生成。 |
| `description` | 强烈建议 | 参与匹配打分；应写清楚“什么时候使用”。 |
| `triggers` | 强烈建议 | 按子串命中，分数最高；中文 Skill 必须尽量列出自然触发表达。 |
| `capability_id` | 公开 Skill 必填 | 公开到 capability pool 的稳定 ID；项目 Skill 使用 `skill.*`，例如 `skill.report_writer`。 |
| `inputs` | 可选 | 会被解析；顶层 `inputs.required` 当前不阻塞主代理执行，主要作为契约说明。 |
| `outputs` | 脚本 / 服务 Skill 建议 | 顶层 `outputs.required` / `scripts[].outputs.required` 是执行校验契约：用于判断脚本 / handler 输出是否满足声明；不表示这些字段会原样注入主代理 prompt。 |
| `parameters` / `input_parameters` | 脚本 / 服务 Skill 建议 | 声明执行前需要解析的业务参数；系统先做确定性解析，仍缺少文本型标量参数时才让 LLM 生成候选 JSON，最终只有通过系统校验的值会作为入参注入。 |
| `scripts` | 可选 | 只支持声明式 Python 脚本；`auto_run: true` 时自动执行。 |
| `execution.mode` | 执行型 Skill 建议 | 支持 `delegated_main_agent`、`python_subprocess`、`platform_service`。 |
| `execution.handler` | platform-service 必填 | runtime 预注册 / allowlist 的 handler key；不能作为动态 import 路径。 |
| `execution.answer_mode` | 执行型 Skill 建议 | 支持 `direct`、`requires_finalizer`、`none`；platform-service 必须显式声明。`requires_finalizer` 表示执行结果先进入安全归一化的 dependency context，再由主代理汇总，而不是把原始 payload 整包塞进 prompt。 |
| `execution.services` | platform-service 可选 | 仅能列出该 Skill allowlist 允许的服务名；普通脚本不能绑定服务。 |
| `execution.trust_scope` | platform-service 建议 | 标记信任边界；当前项目内服务型 Skill 使用 `project`。 |
| `x_runtime.rust` | Rust Skill 可选 | 仅作为约定 metadata；不会触发自动编译或自动执行，必须由 platform handler / runtime allowlist 显式支持。 |
| 其他顶层字段 | 可选 | 会进入 manifest metadata；除非 runtime 显式支持，否则不产生执行能力。 |

## 4. 匹配规则怎么写才有效

当前 matcher 是轻量规则，不是语义向量召回：

1. 用户问题包含 `triggers` 中任一字符串：强命中。
2. 用户问题包含 `name`：中等命中。
3. 用户问题包含 `description` 按空格 / 逗号切出来的 token：弱命中。
4. 最多返回 3 个匹配 Skill，按分数排序。

因此，Skill creator 应遵守：

- 中文 Skill 必须写 `triggers`，不要只依赖 `description`。
- triggers 写用户会说的话，而不是内部模块名。
- 一个 trigger 不要太泛，例如不要只写“分析”“生成”“查询”。
- 同一类意图可写 3-8 个高质量触发表达。

示例：

```yaml
triggers:
  - 写周报
  - 生成周报
  - 本周工作总结
  - 项目汇报
```

## 5. Skill 正文怎么写

本系统会把匹配到的 Skill body 原文注入 `main_agent.respond` prompt，所以正文应该是短、清晰、可执行的指令。

推荐结构：

```markdown
# <Skill Name>

## Use when
- 用户明确要求 ...

## Workflow
1. ...
2. ...
3. ...

## Output
- 使用中文。
- 输出 Markdown。
- 固定包含：...

## Boundaries
- 不要 ...
- 如果缺少 ...，只问一个关键问题。
```

正文原则：
- 保持精简，优先写本系统主代理必须知道的流程、边界和输出格式。
- 不要在正文里放大量参考资料；本系统不会按需打开 `references/`。
- 不要要求模型读取本地文件路径，主代理 LLM 没有本地文件访问能力。
- 不要放 API key、数据库密码、内网地址等敏感信息。
- 如果需要确定性处理，写脚本并通过 `scripts` 显式声明。

## 6. 平台服务型 Skill

`platform_service` 用于把项目内受控业务服务包装成公开 `skill.*` capability。它不是普通脚本模式，也不是 native capability：编排层只看见 Skill capability，handler 与服务由 API runtime 显式注册并 allowlist。

约束：

- service-bound Skill 必须使用 `execution.mode: platform_service`。
- `python_subprocess` 不能绑定受控 DB、内部 LLM、secret、完整环境变量或任意平台服务。
- `execution.handler` 是稳定 handler key；`trust_scope: project` 的公开项目 Skill 可额外声明相对 `handler_module`，系统只在 public skill root 内受控加载该文件。
- `execution.services` 只能请求该 Skill allowlist 内的服务；缺失或越权时 fail closed。
- 需要主代理汇总结构化结果时，使用 `answer_mode: requires_finalizer`。

项目级服务型 Skill 示例：

```yaml
name: report-service
capability_id: skill.report_service
description: 通过项目级 platform-service 生成结构化报告草稿和受控 artifact
execution:
  mode: platform_service
  trust_scope: project
  handler: skill.report_service.platform_handler
  handler_module: runtime/report_service/platform_handler.py
  handler_factory: build_handler
  answer_mode: requires_finalizer
  services:
    - llm.non_stream
    - artifact_writer
    - progress_events
```

服务型 Skill 仅以 `skill.*` capability 公开；planner/public capability、测试入口与 handler key 均应使用当前 Skill contract。框架不会为某个 Skill 额外增加 API route、capability kind、前端协议或 orchestration 特判。


## 7. Rust 型 Skill runtime 接入限制

Rust 只能作为 Skill-owned runtime 的内部实现，不能成为新的公开 capability 类型，也不能绕过 `platform_service` / service allowlist / artifact-event-audit contract。

核心原则：**Skill 来兼容框架，不是框架兼容 Skill。**框架定义可接受的 Rust 形态、目录、构建、运行和审计边界；Skill 作者必须按这些边界构建 Skill。若 Skill 需要框架新增专属 route、专属 executor、专属前端协议或专属 secret 注入，则该 Skill 不符合接入要求。

### 7.1 目录与所有权

项目级 Rust Skill 使用固定目录：

```text
skill/<skill-name>/
  SKILL.md
  runtime/
    <skill_python_adapter>/
      platform_handler.py
  native/
    Cargo.toml
    crates/
      <skill_name>_core/
      <skill_name>_pyo3/       # 可选 adapter
      <skill_name>_cli/        # 可选 adapter
      <skill_name>_sidecar/    # 可选 adapter
```

约束：

- `native/` 只属于该 Skill bundle；移除 Skill 时必须能整体移除 native source、build artifact、sidecar config 与 capability 注册。
- 业务规则必须集中在 shared Rust core crate；PyO3 / CLI / sidecar adapter 只能做协议转换，不得复制业务逻辑。
- 普通用户级 Skill 不允许携带需要后端编译、安装或执行的 Rust runtime。

### 7.2 可接受 Rust 形态

| 形态 | 允许场景 | 接入方式 | 必须满足 |
|---|---|---|---|
| PyO3 wheel | 低延迟库型 pure kernel、进程内校验 / 转换 | Python platform handler import 已构建 wheel/module | wheel 由 CI/部署预构建；禁止 runtime `cargo build`；panic 必须映射为 Python 错误 |
| native binary | 一次性离线执行、受控 subprocess、CLI 调试 | platform handler 调用固定 allowlist binary，使用 JSON stdin/stdout 或 typed protocol | binary 路径固定在 Skill bundle/build artifact；有 timeout、size limit、退出码映射和审计 |
| sidecar service | 长连接池、高并发、强隔离、资源限额、崩溃隔离 | runtime 注册 service endpoint/client，platform handler 只通过 allowlist service 调用 | health/readiness、版本协议、shutdown、tracing/metrics、端口/config 由平台管理；仅内部可访问 |

禁止形态：

- 在 Skill 执行时运行 `cargo build`、`cargo run`、`rustc` 或下载 crates。
- 让 `python_subprocess` 直接执行任意 Rust binary。
- 让普通 Skill 自行开放端口、启动常驻进程或读取 secret。
- 让 Skill sidecar 对公网、前端、用户、普通 Skill 或外部系统直接暴露。
- 通过 `SKILL.md`、用户输入、LLM 输出或外部 tool output 指定任意 sidecar 地址、端口、socket path 或 service name。
- 通过 `SKILL.md` 声明任意本地路径、绝对路径 binary、动态库路径或外部下载 URL。
- 要求框架为该 Skill 新增专属 executor、专属 route、专属 capability id 规则或前端组件。

Skill-owned sidecar 如被 runtime allowlist 接纳，只能通过 Unix domain socket、loopback、同 Pod / 内部网络、私有服务发现或 mTLS 内网访问；endpoint 必须来自部署配置 / runtime allowlist。跨主机访问必须由平台提供 mTLS 或等价服务身份校验，且 health / readiness / metrics / debug endpoint 也不得对公网开放。

Skill-owned sidecar 还必须接受框架 resource limit 基线：max concurrent executions 默认 4、上限 `min(8, cpu)`；per-skill concurrent 为 2；queue size 为 64；queue wait 上限 10s；默认执行 timeout 60s；hard timeout 300s；stdout / stderr 各 1MB；单次 structured result 4MB；输出 artifact 默认上限 32MB，超出必须走 artifact policy；cancel grace 为 5s 后清理进程树。Skill 不得通过 manifest 或 adapter 请求无界队列、无界 stream、无界输出或无 deadline 执行。

Skill-owned Rust adapter 不得在 `SKILL.md`、`x_runtime.rust`、脚本参数、LLM 输出或外部 tool output 中携带 secret value、token、mTLS key、数据库连接串、provider key、session / HMAC key、sidecar endpoint 或任意本地路径。需要 secret 的项目级 Skill 必须通过 runtime allowlist / platform service 获取受控服务；Skill 只能声明非敏感 metadata、contract_version、package / adapter 名称或 secret reference id，不能声明 secret value。secret rotation 由部署系统和 runtime 控制，Skill 不能自行 reload secret 或读取 secret 文件。

Rust Skill 产物供应链规则冻结：PyO3 wheel、native binary、sidecar image / binary 必须由 CI 或部署流水线预构建，具备 artifact id、version、checksum、SBOM、Cargo.lock digest、contract_version、bundle revision 与 provenance 记录；runtime 只能加载 allowlist 中校验通过的 artifact。Skill 执行路径不得编译 Rust、下载 crates、替换 wheel / binary / image、加载任意本地动态库或连接未登记 sidecar。

Rust Skill 性能与运维规则冻结：上线前必须提供 shared core / adapter benchmark，覆盖关键输入规模、P50/P95/P99、CPU、memory、输出大小与 sidecar queue / timeout 行为；sidecar adapter 必须具备 health/readiness/version、dashboard、alert、drain / restart / rollback、artifact quarantine、secret / identity failure 演练证据。没有 provenance、benchmark、runbook 或演练证据的 Rust Skill 不得作为项目级交付 Skill 接入。

### 7.3 Manifest 建议元数据

`x_runtime.rust` 是建议 metadata，不会自动赋予执行能力。只有当 platform handler、runtime allowlist、构建产物和部署配置全部就绪时，才允许使用对应 adapter。

```yaml
x_runtime:
  rust:
    adapter: pyo3        # pyo3 | binary | sidecar
    core_crate: report_service_core
    package: report_service_pyo3
    contract_version: 1
    artifact_ref: report_service_pyo3@1.0.0   # 可选；非路径，必须由 runtime allowlist 解析
    artifact_sha256: "<sha256>"             # 可选；不得替代平台 allowlist 校验
    provenance_ref: ci://report-service/1.0.0  # 可选；不得包含 secret 或本机路径
```

同时必须使用 `platform_service`：

```yaml
execution:
  mode: platform_service
  trust_scope: project
  handler: skill.report_service.platform_handler
  handler_module: runtime/report_service/platform_handler.py
  handler_factory: build_handler
  services:
    - artifact_writer
    - progress_events
```

### 7.4 构建、测试与审计

Rust 型 Skill 上线前必须提供：

- Rust core unit tests。
- adapter contract tests。
- Python platform handler integration tests。
- golden tests：同一输入在 PyO3 / binary / sidecar 可比场景下输出一致。
- `cargo fmt --check`、`cargo clippy -- -D warnings`、`cargo test`。
- 构建产物来源说明：wheel / binary / sidecar image 不得在业务请求路径中临时生成。
- 供应链证据：artifact id、version、sha256、SBOM、Cargo.lock digest、contract_version、bundle revision 与 provenance。
- benchmark 证据：shared core、PyO3 / binary / sidecar adapter 的关键路径 P50/P95/P99、CPU、memory、输出大小与 sidecar queue / timeout 行为。
- 运维证据：sidecar adapter 的 health/readiness/version、dashboard、alert、drain / restart / rollback、artifact quarantine、secret / identity failure 演练。
- 审计说明：不读取未授权环境变量、secret、本地路径，不输出真实文件路径、完整 prompt 或敏感配置。

## 8. 脚本型 Skill

### 8.1 支持范围

当前 `SkillScriptRunner` 只支持：

- `runtime: python`
- JSON stdin
- JSON object stdout
- `auto_run: true` 或 `run_by_default: true`
- 相对路径脚本，且必须位于 Skill 包目录内
- timeout、stdout、stderr 上限

不支持：

- shell / bash / node / arbitrary command runtime
- `runtime: rust` / `runtime: native` 直接声明
- `runtime: r` / `runtime: R` 直接声明（当前请使用 Python wrapper 调用 Rscript）
- Markdown 代码块自动执行
- 绝对路径
- `..` 路径逃逸
- symlink 脚本
- 执行时安装依赖、编译 Rust 或下载 native artifact
- 继承完整环境变量或 secret
- 交互式 stdin


### 8.2 运行环境与依赖口径

脚本运行在公司后端统一 Python 运行环境中：

- Skill 作者不要假定个人电脑、个人虚拟环境或某台机器上的包可用。
- 可用第三方依赖以后端项目正式依赖快照和部署环境为准。
- Skill 包执行时不会安装 Skill 包自带的依赖声明。
- 新脚本优先使用 Python 标准库；确需新增依赖时，必须先走后端依赖评审，更新项目正式依赖快照和部署环境后，再允许 Skill 上线。


#### 当前后端正式依赖快照

以下清单同步自仓库根目录 `requirements.txt`，用于告诉 Skill 作者脚本当前可以依赖哪些第三方包。该清单是共享项目口径，不代表任何个人机器环境。

维护规则：
- Skill 脚本可以优先使用标准库；如需第三方包，应只使用下表或后端部署环境明确提供的包。
- 不要在 Skill 包内声明或安装额外依赖。
- 如果新增、删除或升级后端依赖，必须同步更新 `requirements.txt` 和本节清单。
- 即使包可用，也不要在脚本中读取未授权文件、环境变量、secret 或执行外部生产副作用。

| Package | Version |
|---|---|
| `a2a-sdk` | `1.0.1` |
| `annotated-doc` | `0.0.4` |
| `annotated-types` | `0.7.0` |
| `anyio` | `4.13.0` |
| `attrs` | `26.1.0` |
| `Automat` | `25.4.16` |
| `certifi` | `2026.2.25` |
| `cffi` | `2.0.0` |
| `charset-normalizer` | `3.4.7` |
| `click` | `8.3.2` |
| `constantly` | `23.10.4` |
| `cryptography` | `46.0.7` |
| `cssselect` | `1.4.0` |
| `defusedxml` | `0.7.1` |
| `distro` | `1.9.0` |
| `fastapi` | `0.136.0` |
| `filelock` | `3.29.0` |
| `google-api-core` | `2.30.3` |
| `google-auth` | `2.49.2` |
| `googleapis-common-protos` | `1.74.0` |
| `h11` | `0.16.0` |
| `httpcore` | `1.0.9` |
| `httpx` | `0.28.1` |
| `httpx-sse` | `0.4.3` |
| `hyperlink` | `21.0.0` |
| `idna` | `3.12` |
| `Incremental` | `24.11.0` |
| `itemadapter` | `0.13.1` |
| `itemloaders` | `1.4.0` |
| `jiter` | `0.14.0` |
| `jmespath` | `1.1.0` |
| `json-rpc` | `1.15.0` |
| `jsonschema` | `4.26.0` |
| `jsonschema-specifications` | `2025.9.1` |
| `lxml` | `6.1.0` |
| `mcp` | `1.27.0` |
| `numpy` | `2.4.4` |
| `openai` | `2.32.0` |
| `packaging` | `26.0` |
| `pandas` | `3.0.2` |
| `parsel` | `1.11.0` |
| `Protego` | `0.6.0` |
| `proto-plus` | `1.27.2` |
| `protobuf` | `7.34.1` |
| `pyasn1` | `0.6.3` |
| `pyasn1_modules` | `0.4.2` |
| `pycparser` | `3.0` |
| `pydantic` | `2.13.3` |
| `pydantic-settings` | `2.14.0` |
| `pydantic_core` | `2.46.3` |
| `PyDispatcher` | `2.0.7` |
| `PyJWT` | `2.12.1` |
| `PyMySQL` | `1.1.2` |
| `pyOpenSSL` | `26.0.0` |
| `python-dateutil` | `2.9.0.post0` |
| `python-dotenv` | `1.2.2` |
| `python-multipart` | `0.0.26` |
| `PyYAML` | `6.0.3` |
| `queuelib` | `1.9.0` |
| `referencing` | `0.37.0` |
| `regex` | `2026.4.4` |
| `requests` | `2.33.1` |
| `requests-file` | `3.0.1` |
| `rpds-py` | `0.30.0` |
| `Scrapy` | `2.15.0` |
| `service-identity` | `24.2.0` |
| `setuptools` | `82.0.1` |
| `six` | `1.17.0` |
| `sniffio` | `1.3.1` |
| `SQLAlchemy` | `2.0.49` |
| `sse-starlette` | `3.3.4` |
| `starlette` | `1.0.0` |
| `tabulate` | `0.10.0` |
| `tiktoken` | `0.12.0` |
| `tldextract` | `5.3.1` |
| `tqdm` | `4.67.3` |
| `Twisted` | `25.5.0` |
| `typing-inspection` | `0.4.2` |
| `typing_extensions` | `4.15.0` |
| `urllib3` | `2.6.3` |
| `uvicorn` | `0.45.0` |
| `w3lib` | `2.4.1` |
| `wheel` | `0.46.3` |
| `zope.interface` | `8.3` |


#### R 辅助运行时（当前已验证）

当前后端 `SkillScriptRunner` 仍只原生执行 `runtime: python`。如果 Skill 需要使用 R 语言逻辑，必须采用 **Python wrapper + Rscript** 模式：

1. `SKILL.md` 的 `scripts[].runtime` 仍写 `python`。
2. Python wrapper 从 stdin 读取本系统传入的 JSON。
3. Python wrapper 调用 Skill 包内的 `.R` 脚本。
4. `.R` 脚本使用 `jsonlite` 读取 stdin / 写出 stdout JSON。
5. Python wrapper 校验并透传 R 脚本输出的 JSON object。

当前已验证的 R 附加依赖口径：

| Runtime / Package | 当前验证版本 | 用途 |
|---|---:|---|
| `Rscript` | Docker backend：Ubuntu 22.04 `r-base-core`（当前 apt 版本 4.1.x） | 执行包内 `.R` 脚本 |
| `jsonlite` | Docker backend：Ubuntu 22.04 `r-cran-jsonlite`（当前 apt 版本 1.7.x） | R 脚本 JSON stdin/stdout |
| UTF-8 locale | Docker backend：`LANG=C.UTF-8`、`LC_ALL=C.UTF-8` | 解析包含中文字符串的 `.R` 源码 |

R Skill 约束：
- 不要在 Skill 执行时安装 R 包。
- Docker backend 必须在 `Dockerfile` 中安装 `locales`、`r-base-core`、`r-cran-jsonlite`，并设置 `LANG=C.UTF-8` / `LC_ALL=C.UTF-8`；不能只依赖本机开发环境已有 R 包。
- 不要依赖 RStudio、GUI R app 或交互式输入。
- `.R` 文件必须放在 Skill 包目录内，例如 `scripts/analyze.R`。
- Python wrapper 必须用包内相对路径定位 `.R` 文件，不能要求主代理读取本地路径。
- Python wrapper 应从受控候选路径查找 `Rscript`；找不到时输出清晰错误。
- Python wrapper 调用 Rscript 时必须显式传入最小可用 `PATH` 和 UTF-8 locale（至少 `LANG`、`LC_ALL`、`LC_CTYPE`），否则 R 进程可能找不到系统工具 / Rscript，或在 Docker 的非 UTF-8 环境中把中文字符串解析成 `unexpected INCOMPLETE_STRING`。
- `.R` 脚本 stdout 必须是 JSON object，stderr 只作为诊断。
- 新增或修改 R-backed Skill 时，必须补充/维护 Dockerfile 与 wrapper 环境回归测试，至少覆盖 `r-cran-jsonlite`、UTF-8 locale 和 R subprocess env 不被裁掉。

推荐 Python wrapper 中使用的 `Rscript` 查找方式：

```python
from pathlib import Path
import shutil


def find_rscript() -> str:
    for candidate in (
        shutil.which("Rscript"),
        "/usr/local/bin/Rscript",
        "/opt/homebrew/bin/Rscript",
        "/Library/Frameworks/R.framework/Resources/bin/Rscript",
    ):
        if candidate and Path(candidate).exists():
            return str(candidate)
    raise RuntimeError("Rscript is not available in the backend runtime")
```

R wrapper Skill 的目录建议：

```text
skill/mini-rcbd/
  SKILL.md
  scripts/
    run_rcbd.py
    rcbd_analysis.R
```

`SKILL.md` 中仍声明 Python wrapper：

```yaml
scripts:
  - name: run_rcbd
    path: scripts/run_rcbd.py
    runtime: python
    auto_run: true
    timeout_seconds: 30
    inputs:
      required:
        - query
    outputs:
      required:
        - answer
```

Python wrapper 示例骨架：

```python
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def find_rscript() -> str:
    candidates = (
        "/usr/local/bin/Rscript",
        "/opt/homebrew/bin/Rscript",
        "/Library/Frameworks/R.framework/Resources/bin/Rscript",
    )
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    raise RuntimeError("Rscript is not available in the backend runtime")


payload = json.load(sys.stdin)
r_script = Path(__file__).parent / "rcbd_analysis.R"
process = subprocess.run(
    [find_rscript(), str(r_script)],
    input=json.dumps(payload, ensure_ascii=False),
    text=True,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    timeout=30,
    check=False,
    env={"PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
)
if process.returncode != 0:
    raise RuntimeError(process.stderr[-1000:])
result = json.loads(process.stdout)
if not isinstance(result, dict):
    raise RuntimeError("R script stdout must be a JSON object")
print(json.dumps(result, ensure_ascii=False))
```

R 脚本示例骨架：

```r
library(jsonlite)

input_text <- paste(readLines("stdin", warn = FALSE), collapse = "\n")
payload <- fromJSON(input_text, simplifyVector = FALSE)

query <- if (!is.null(payload$query)) payload$query else ""
result <- list(
  answer = paste0("R received query: ", query)
)
cat(toJSON(result, auto_unbox = TRUE, null = "null"))
```


### 8.3 脚本收到什么输入

自动执行脚本的 stdin 是 JSON object：

```json
{
  "query": "用户当前问题",
  "blocks": 2,
  "uploaded_artifacts": [
    {
      "upload_id": "...",
      "filename": "materials.csv",
      "file_type": "csv",
      "content": "ped_id,design_check\nA,0\n",
      "preview": {
        "row_count": 1,
        "columns": ["ped_id", "design_check"]
      }
    }
  ],
  "metadata": {
    "请求 metadata 中的非敏感字段": "..."
  }
}
```

注意：
- 当用户通过前端上传 JSON / CSV 并在消息中引用 `upload_ids` 时，自动脚本收到的 `uploaded_artifacts` 会包含原始 `content`，用于确定性处理文件内容。
- 主代理 prompt 中的“上传文件上下文”仍只注入脱敏摘要，不包含原始 `content`；不要把脚本收到原文误认为 LLM prompt 也能直接读取完整文件。
- 如果 Skill 在 frontmatter 声明了 `parameters`，主代理会在执行自动脚本前解析参数，并把成功解析的值作为 stdin 顶层字段注入；例如 RCBD Skill 可声明 `blocks`，使“2次重复”解析成 `"blocks": 2`。
- Skill 自动脚本可接受的所有业务参数都必须在 `parameters` / `input_parameters` 中列出；即使某个参数由脚本默认值补齐，也要声明出来，避免主代理、LLM 补槽、审计和脚本维护者各自理解一套隐式契约。
- 没有默认值且脚本执行必须依赖的参数必须声明 `required: true`；有默认值的参数应声明为非必填（`required: false` 或省略 `required`），并写明 `default`，脚本内部默认值必须与 manifest 保持一致。当前 resolver 不会自动把 `default` 注入 stdin，脚本应在字段缺失时应用同一默认值。
- 枚举型参数必须用 `enum` 列出全部可接受值；如用户可能使用中文 / 英文 / 缩写表达，应同时在 `aliases`、`patterns` 或 Skill 正文中说明映射关系。当前 resolver 主要按参数名、类型和 source 做系统校验，不会替脚本强制枚举校验；脚本仍必须拒绝不在枚举范围内的值。
- 参数解析顺序固定为：显式 payload / 上传 artifact / 安全 metadata / 当前问题与安全最近用户消息的正则解析 / LLM 缺参补槽。前面步骤已解析出的值不会被 LLM 覆盖。
- LLM 补槽只在仍缺少 `string`、`integer`、`number` 等文本型标量参数时触发；它只能返回候选 JSON，系统会按 manifest 声明的参数名、类型、source 和 required 规则再次校验。LLM 不能直接启动脚本，也不能把口头承诺变成入参。
- `artifact` / `file` / `data` 参数只能由真实上传或上传摘要满足，LLM 不得虚构文件或数据参数。
- 自动脚本不会收到完整 `conversation_memory`、`history_summary`、`recent_messages` 或 `resolved_user_message` 原文；需要跨轮继承的值必须先经 `parameters` 解析成结构化字段。
- 如果没有通过受控上传入口引用文件，`uploaded_artifacts` 可能只有脱敏 metadata，不保证包含 `content`。
- 脚本 `cwd` 是临时隔离目录，不是仓库根目录，也不是 Skill 源目录。
- 如需读取 Skill 包内资源，请用 `Path(__file__).parent` 定位。

参数声明示例：

```yaml
parameters:
  blocks:
    type: integer
    required: true
    aliases: [blocks, 区组数, 区组, 重复数, 重复, reps, replications]
    patterns:
      - '(?:blocks?|区组数|区组|重复数|重复|reps?|replications?)\s*[:：=]?\s*(\d+)'
      - '(\d+)\s*(?:个|次)?(?:区组|重复|rep|reps|blocks?)'
  material_data:
    type: artifact
    required: true
    source: artifact
  analysis_mode:
    type: string
    required: false
    default: anova
    enum: [anova, layout_only, summary_only]
    aliases: [分析模式, 输出模式]
```

`source: artifact` 表示该必填参数由 `uploaded_artifacts` 是否存在来满足；脚本仍通过 `uploaded_artifacts` 读取实际文件内容，`material_data` 顶层字段只是可审计的可用性标记。
`analysis_mode` 是带默认值的可选枚举参数；如果 stdin 中没有该字段，脚本应使用 `anova`，并拒绝不在 `enum` 列表中的其他值。

### 8.4 脚本必须输出什么

stdout 必须是 JSON object，例如：

```json
{"answer": "脚本处理结果", "facts": ["..."]}
```

建议始终输出一个短的人类可读摘要字段：`answer`、`response_text` 或 `summary`。当前 Skill executor 会在执行边界把 `answer` / `summary` 归一化为 `response_text`，供 `requires_finalizer` 的主代理 finalizer 从 dependency context 中读取；如果已经输出非空 `response_text`，则不会被 `answer` 覆盖。

如果 `outputs.required` 或 `scripts[].outputs.required` 声明了必需字段，stdout JSON 必须包含这些字段，否则脚本结果会被视为失败，无法进入正常回答链路。注意：required-output 校验只回答“输出是否合格”，不回答“哪些字段会进入 prompt”。字段存在、通过 required 校验，也不等于原始 payload 会整包注入主代理 prompt。

三层契约必须分清：

1. **required-output 执行校验契约**：`outputs.required` / `scripts[].outputs.required` 校验脚本或服务是否产出必需字段；缺字段会导致执行失败或不进入正常回答链路。
2. **main-agent dependency context 注入契约**：`requires_finalizer` 只读取归一化、脱敏、allowlist 后的 dependency context；`answer` / `summary` 会先归一化为 `response_text`，表格类字段也受 allowlist 与 token budget 限制。
3. **`output_files` artifact 契约**：可下载文件由 artifact 管线收集、校验、打包和替换；文件描述与文件正文不保证原样进入 finalizer prompt。

推荐输出字段矩阵：

| 用途 | 推荐字段 | 是否可进入 finalizer prompt |
|---|---|---|
| 人类可读摘要 | `answer` / `response_text` / `summary` | 是；`answer` / `summary` 会归一化为 `response_text` 后进入，已有非空 `response_text` 优先。 |
| 表格结果 | `columns` / `rows` / `row_count` / `preview_row_count` | 是；受现有 allowlist 与 token budget 限制，不应输出无界大表。 |
| 错误 / 缺参 | `ok: false` + `answer` + 可选短 `error` | 是；`answer` 会归一化为 `response_text`，`ok: false` 会标记为 `is_error` 供主代理识别。 |
| 文件产物 | `output_files` | 由 artifact 管线处理；不保证原样进入 prompt，文件正文不会作为最终回答内容注入。 |
| 领域大对象 | `parameters` / `out_design` / fieldbook 全量 | 否；不要依赖整包注入，如需汇总请另行生成短 `answer` / `summary` / `response_text`。 |

如果脚本需要产出可下载文件，应写入运行时提供的 `MAF_SKILL_OUTPUT_DIR`，并在 stdout JSON 中声明 `output_files`：

```json
{
  "answer": "处理完成，已生成文件。",
  "output_files": [
    {
      "path": "outputs/result.html",
      "filename": "result.html",
      "mime_type": "text/html",
      "label": "结果文件",
      "summary": "可下载后在本地查看。"
    }
  ]
}
```

文件产出边界：

- `path` 必须是 `outputs/` 下的相对路径，不能是绝对路径、`..`、symlink、hardlink 或目录。
- 当前平台默认允许的输出扩展名为：`.txt`、`.md`、`.json`、`.csv`、`.tsv`、`.html`、`.pdf`、`.xlsx`、`.png`、`.jpg`、`.jpeg`。
- 如果声明 `mime_type`，必须与文件扩展名匹配；例如 `.html` 只能声明 `text/html`，不能把 `result.html` 声明为 `application/json`。
- 脚本可声明多个合法输出文件；平台会自动打包成 1 个 zip 下载文件。
- 同一会话只保留当前 1 个 Skill 输出文件；新输出会替换旧输出。
- HTML v1 只下载不站内预览；文件正文不会进入主代理 prompt。
- Skill 直接声明 `.zip` / `.tar` / `.gz` 源文件默认不允许；多文件 zip 由平台生成。
- 文件收集、保存或旧文件替换失败不会让主代理整体崩溃；平台会拒绝本次文件产物并记录诊断，主回答仍应基于脚本结构化输出说明文件未保存。

如果 Skill 只会产出少数固定文件类型，建议在 manifest 的 `outputs.files` 中声明更严格的扩展名 / MIME 约束。该声明只能在平台默认 allowlist 基础上收紧，不能放宽全局拒绝项：

```yaml
outputs:
  required:
    - answer
  files:
    - extensions: [.html]
      mime_types: [text/html]
```

### 8.5 脚本型 Skill 示例

目录：

```text
skill/keyword-counter/
  SKILL.md
  scripts/count_keywords.py
```

`SKILL.md`：

```markdown
---
name: keyword-counter
description: 统计用户文本中的关键词出现次数，并给出简短解释
triggers:
  - 统计关键词
  - 关键词次数
outputs:
  required:
    - answer
scripts:
  - name: count_keywords
    path: scripts/count_keywords.py
    runtime: python
    auto_run: true
    timeout_seconds: 3
    inputs:
      required:
        - query
    outputs:
      required:
        - answer
---

# Keyword Counter

当用户要求统计关键词时，优先使用脚本输出。回答时：
- 先给出统计结果；
- 再用一句话说明统计口径；
- 不要编造脚本没有返回的数据。
```

`scripts/count_keywords.py`：

```python
from __future__ import annotations

import json
import re
import sys

payload = json.load(sys.stdin)
query = str(payload.get("query") or "")
words = re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]", query)

result = {
    "answer": f"共识别 {len(words)} 个粗粒度词元。",
    "token_count": len(words),
}
print(json.dumps(result, ensure_ascii=False))
```

## 9. Skill 创建提示词模板

把下面这段作为创建 Skill 的约束：

```text
请创建一个适配 breeding_agent 后端的 Skill。必须遵守：
1. Skill 包只能依赖 SKILL.md；可选 scripts/ 下的 Python 脚本；只有项目级 trusted Skill 才能在 native/ 放 Rust runtime。
2. SKILL.md frontmatter 必须包含 name、display_name、description；中文任务必须包含高质量 triggers。
3. 本系统只会把 SKILL.md body 注入 LLM，不会自动读取 references/ 或 assets/。
4. 如需脚本，只能声明 scripts[].runtime=python，path 必须是包内相对路径，不能使用绝对路径、..、symlink、shell、node、runtime:rust 或任意命令。
5. 自动脚本必须设置 auto_run: true，stdin 为 JSON object，至少包含 query、uploaded_artifacts、metadata；如需业务参数，必须用 parameters/input_parameters 声明可解析字段，LLM 只会在缺参时生成候选并由系统校验后注入，不要依赖主代理口头承诺传参；stdout 必须是 JSON object。若需要下载文件，写入 MAF_SKILL_OUTPUT_DIR 并用 output_files 声明；若需要 R 语言逻辑，不要声明 runtime:r；请创建 runtime:python 的 wrapper 调用包内 .R 脚本和 Rscript。
6. 只有项目信任、runtime 已注册 handler/service allowlist 的 Skill 才能声明 execution.mode=platform_service；不要把 platform handler 写成动态 import 路径，不要让 python_subprocess 绑定服务。Rust 只能作为 platform_service 背后的受控实现，不能要求框架自动编译或执行。
7. 如果声明 outputs.required 或 scripts[].outputs.required，脚本 stdout 必须包含这些字段；这只是执行校验契约，不代表字段会原样注入 prompt。推荐输出 answer / response_text / summary，其中 answer / summary 会在 finalizer 使用前归一化为 response_text。
8. 不要创建 README、安装指南、CHANGELOG 等额外说明文件；除非脚本需要，不要创建 references/ 或 assets/。
9. Skill 正文保持精简，写 Use when、Workflow、Output、Boundaries；不要放 secret、内网地址、数据库密码或要求模型读取本地路径。
10. 输出最终文件树和每个文件内容。
```

## 10. 构建检查清单

交付 Skill 前逐项检查：

- [ ] `SKILL.md` 存在，且以 YAML frontmatter 开头和闭合。
- [ ] `name` 非空，稳定且不和已有 Skill 重名。
- [ ] `display_name` 非空，适合作为用户界面 / 进度展示名称。
- [ ] `description` 说明“什么时候使用”，不是泛泛描述。
- [ ] 中文 Skill 有明确 `triggers`。
- [ ] body 非空，且是主代理可直接遵循的操作说明。
- [ ] 没有把 secret、完整数据库连接串、API key 写进 Skill。
- [ ] 如果声明 `platform_service`，`capability_id` 使用 `skill.*`，`execution.handler` 是 runtime 预注册 handler key，`answer_mode` 已显式声明。
- [ ] 如果声明 `platform_service`，`execution.services` 只列出 allowlist 允许的服务；没有把 DB、LLM、secret 暴露给普通脚本。
- [ ] 如果有脚本，`runtime` 是 `python`。
- [ ] 如果有脚本，`path` 是包内相对路径，不包含 `..`。
- [ ] 如果有脚本，stdout 是 JSON object。
- [ ] 如果声明了 required 输出，脚本真实输出包含这些字段。
- [ ] 已区分 required-output 校验、main-agent dependency context 注入和 `output_files` artifact 管线；没有假设原始 payload 会整包进入 prompt。
- [ ] 执行型 Skill 至少输出一个短摘要字段：`answer` / `response_text` / `summary`；需要 finalizer 汇总时知道 `answer` / `summary` 会归一化为 `response_text`。
- [ ] 如果脚本产出下载文件，文件写入 `MAF_SKILL_OUTPUT_DIR`，stdout `output_files[].path` 使用 `outputs/` 下相对路径，不使用绝对路径、`..`、symlink、hardlink 或目录，且不声明平台默认拒绝的源压缩包。
- [ ] 输出文件扩展名属于平台 allowlist；如声明 `mime_type`，MIME 与扩展名匹配。
- [ ] 如果 Skill 只产出固定文件类型，已用 `outputs.files` 声明更严格的扩展名 / MIME 约束，且该声明只收紧、不放宽平台默认规则。
- [ ] 如果脚本需要业务参数，已在 `parameters` / `input_parameters` 中列出所有可接受字段、类型、required、默认值、aliases 或 patterns，并在脚本内保留最终校验。
- [ ] 没有默认值且脚本必须依赖的业务参数已声明 `required: true`；有默认值的业务参数声明为非必填并写明 `default`，脚本默认值与 manifest 一致。
- [ ] 枚举型参数已用 `enum` 列出所有可接受值，脚本会拒绝不在枚举范围内的值。
- [ ] 不依赖执行时安装新包。
- [ ] 不依赖 cwd 指向 Skill 目录。
- [ ] 如果使用 R，Skill 仍通过 Python wrapper 暴露；`.R` 文件在包内，且 R stdout 是 JSON object。
- [ ] 如果使用 Rust，Rust source 只位于 `skill/<skill-name>/native/`；没有要求运行时 `cargo build` / `cargo run` / 下载 crates。
- [ ] Rust Skill 使用 shared core + adapter 模式；PyO3 / binary / sidecar 没有复制业务逻辑。
- [ ] Rust adapter 只能由 `platform_service` handler 或 allowlist service 调用，不能由普通 `python_subprocess` 任意执行。
- [ ] Rust sidecar adapter 如存在，endpoint 来自部署配置 / runtime allowlist，未在 `SKILL.md` 中声明任意公网地址、端口、socket path 或 service name。
- [ ] Rust sidecar adapter 如存在，接受框架并发、队列、timeout、stdout/stderr、result size、artifact size、cancel grace 限制；没有声明无界 stream / queue / output。
- [ ] Rust Skill 未在 `SKILL.md`、`x_runtime.rust`、脚本参数或 adapter metadata 中写入 secret value、token、mTLS key、数据库连接串、provider key、session / HMAC key 或任意本地 secret 路径。
- [ ] Rust wheel / binary / sidecar image 由 CI / 部署流水线预构建；业务请求路径不会运行 `cargo build`、下载 crates、替换 artifact 或加载任意本地动态库。
- [ ] Rust artifact 有 artifact id、version、sha256、SBOM、Cargo.lock digest、contract_version、bundle revision 与 provenance，并被 runtime allowlist 接纳。
- [ ] Rust shared core 与所有 adapter 有 contract tests、golden tests 与 benchmark 证据；P50/P95/P99、CPU、memory、输出大小和 sidecar queue / timeout 行为可审查。
- [ ] Rust sidecar adapter（如存在）有 health/readiness/version、dashboard、alert、drain / restart / rollback、artifact quarantine、secret / identity failure 演练证据。
- [ ] Rust Skill 移除后，主体框架不会残留该 Skill 的 PyO3 module、binary、sidecar endpoint、artifact allowlist、capability 注册或专属前端 / API 分支。

## 11. 验证方法

### 11.1 最小静态检查

- 确认 `SKILL.md` 以 YAML frontmatter 开头并闭合。
- 确认 `name`、`description`、中文 `triggers`、`execution`、`parameters`、`scripts` 与 `outputs.required` 符合本指南。
- 确认 `SKILL.md` 中不写产品名、secret、内网地址、绝对本地路径或个人环境路径。

### 11.2 脚本本地 smoke

对脚本型 Skill，使用仓库统一 Python 环境直接给 wrapper 传入最小 JSON stdin，检查 stdout 是 JSON object，并至少包含 `answer` / `response_text` / `summary` 之一。

### 11.3 项目回归

- 新增或修改 Skill 后，应补对应 integration 回归测试，覆盖 manifest 解析、匹配、缺参返回、成功执行和 output files 安全声明。
- 执行与该 Skill 直接相关的 integration 测试；如触及主代理、API 或前端契约，再追加相应分层测试。
- 如果修改 Rust、依赖或供应链策略，按仓库 License Requirement 运行许可门禁。

## 12. 常见错误

| 错误 | 结果 | 修正 |
|---|---|---|
| 只有 description，没有 triggers | 中文意图可能匹配不到 | 增加用户自然表达触发词 |
| `SKILL.md` 没有正文 | parser 拒绝 | 至少写清 workflow / output / boundaries |
| 脚本输出普通文本 | runner 拒绝 | 输出 JSON object |
| 脚本 path 写绝对路径 | runner 拒绝 | 改为包内相对路径 |
| 脚本依赖 cwd 读取文件 | 找不到文件 | 用 `Path(__file__).parent` |
| 写了 shell 脚本 | runner 拒绝 | 改写为 Python |
| 声明 `runtime: rust` | runner 拒绝 | 改为 `platform_service` + `native/` + 受控 Rust adapter |
| 在请求路径中 `cargo build` | runner / 评审拒绝 | 在 CI/部署阶段预构建 wheel/binary/sidecar |
| 直接声明 `runtime: r` | runner 拒绝 | 使用 Python wrapper 调用包内 `.R` 脚本 |
| 把详细资料放 references/ | 后端 LLM 看不到 | 把必要内容压缩进 SKILL.md body，或用脚本读取并输出摘要 |
| 输出字段和 `outputs.required` 不一致 | 脚本结果失败 | 对齐 required 字段 |
| 以为 `outputs.required` 字段会原样进入主代理 prompt | finalizer 看不到预期内容 | 输出短 `answer` / `summary` / `response_text`，并依赖归一化后的 dependency context |
| 把 `output_files` 当作 prompt 内容 | finalizer 不会读取文件正文 | 用 `output_files` 交付下载产物，同时用 `answer` / `summary` 描述文件内容 |

## 13. 和通用本地 Skill runtime 的差异

| 能力 | 通用本地 Skill runtime | 本系统后端当前支持 |
|---|---|---|
| `SKILL.md` frontmatter | 支持 | 支持 `name` / `description` / `triggers` 等 |
| `SKILL.md` body | 触发后加载 | 匹配后注入主代理 prompt |
| `agents/openai.yaml` | UI metadata | 当前后端不读取 |
| `references/` | 可能按需读取 | 当前后端不自动读取 |
| `assets/` | 可能用于产物 | 当前后端不自动使用 |
| Python 脚本 | 可作为资源 | 仅 manifest 声明且 auto_run 时受控执行 |
| Shell / 任意命令 | 某些本地环境可能可执行 | 不支持 |
| Rust runtime | 某些本地环境可自行编译/运行 | 仅项目级 trusted Skill 可通过 `native/` + platform-service + allowlist adapter 接入；不自动编译或执行 |
| R 脚本 | 某些本地环境可通过本地命令运行 | 当前通过 `runtime: python` wrapper 调用 `Rscript`，不支持直接 `runtime: r` |
| MCP / plugin runtime | 插件可提供 | 不支持 |
| 本地文件访问 | 本地 agent 可能可读 workspace | 主代理 LLM 不具备；脚本只可读自己包内可定位资源 |

## 14. 给 Skill creator 的推荐默认策略

- 首选 prompt-only Skill：简单、稳定、最符合当前主代理注入方式。
- 只有在需要确定性解析、统计、格式转换时才加 Python 脚本。
- 只有项目级 trusted Skill 且确需性能、隔离或类型安全时才引入 Rust；Rust 必须适配框架 contract。
- Skill body 控制在 200 行以内；超过时优先删减，而不是拆 references，因为后端不会自动加载 references。
- triggers 比 description 更重要；每个 Skill 至少写 3 个真实用户会说的触发表达。
- 脚本只输出事实和结构化中间结果，最终措辞交给主代理 LLM。
