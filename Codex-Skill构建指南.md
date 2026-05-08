# Codex Skill 构建指南（适配本系统）

- **适用对象**：使用 Oh-my-codex / Codex 的 `skill-creator` 创建 Skill，并希望这些 Skill 能被本项目后端 `main_agent.respond` 使用的开发者。
- **适配范围**：本系统的 Codex Skill 兼容层，而不是完整 Codex 本地 runtime。
- **当前实现入口**：`src/integrations/codex_skills/`、`src/capabilities/main_agent/`、`src/api/runtime.py`。
- **更新时间**：2026-05-08

## 1. 一句话结论

本系统可以加载 Codex 风格的 `SKILL.md`，按用户问题匹配 Skill，并把匹配到的 Skill 正文注入主代理 prompt；也可以受控执行 Skill manifest 明确声明的 Python 脚本，并把 JSON 输出注入主代理 prompt。

但本系统**不是完整 Codex runtime**：不会自动读取 Skill 的 `references/`，不会执行 Markdown 代码块，不支持 shell 脚本 / MCP / plugin runtime，也不会给脚本继承完整本机环境变量。

## 2. Skill 放在哪里

默认 runtime 会扫描：

```text
<项目根目录>/skill/**/SKILL.md
~/.codex/skills/**/SKILL.md
```

测试或定制 runtime 可通过 `build_api_runtime(..., skill_roots=[...])` 或 `skill_catalog=...` 显式注入。

推荐目录：

```text
skill/my-skill/
  SKILL.md
  scripts/
    optional_auto_run.py
```

说明：
- `SKILL.md` 是唯一必需文件。
- `scripts/` 可选，仅当需要确定性处理或结构化预处理时使用。
- `references/`、`assets/` 可以作为 Codex 人工构建过程的辅助资源，但本系统后端不会自动加载它们给 LLM。

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
| `description` | 强烈建议 | 参与匹配打分；应写清楚“什么时候使用”。 |
| `triggers` | 强烈建议 | 按子串命中，分数最高；中文 Skill 必须尽量列出自然触发表达。 |
| `inputs` | 可选 | 会被解析；顶层 `inputs.required` 当前不阻塞主代理执行，主要作为契约说明。 |
| `outputs` | 脚本 Skill 建议 | 顶层 `outputs.required` 会用于校验脚本 JSON 输出。 |
| `parameters` / `input_parameters` | 脚本 Skill 建议 | 声明主代理在执行自动脚本前解析的业务参数；系统先做确定性解析，仍缺少文本型标量参数时才让 LLM 生成候选 JSON，最终只有通过系统校验的值会作为脚本 stdin 顶层字段注入。 |
| `scripts` | 可选 | 只支持声明式 Python 脚本；`auto_run: true` 时自动执行。 |
| 其他顶层字段 | 可选 | 会进入 manifest metadata，但当前主代理不依赖。 |

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

## 6. 脚本型 Skill

### 6.1 支持范围

当前 `SkillScriptRunner` 只支持：

- `runtime: python`
- JSON stdin
- JSON object stdout
- `auto_run: true` 或 `run_by_default: true`
- 相对路径脚本，且必须位于 Skill 包目录内
- timeout、stdout、stderr 上限

不支持：

- shell / bash / node / arbitrary command runtime
- `runtime: r` / `runtime: R` 直接声明（当前请使用 Python wrapper 调用 Rscript）
- Markdown 代码块自动执行
- 绝对路径
- `..` 路径逃逸
- symlink 脚本
- 执行时安装依赖
- 继承完整环境变量或 secret
- 交互式 stdin


### 6.2 运行环境与依赖口径

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
| `Rscript` | `R 4.6.0` | 执行包内 `.R` 脚本 |
| `jsonlite` | `2.0.0` | R 脚本 JSON stdin/stdout |

R Skill 约束：
- 不要在 Skill 执行时安装 R 包。
- 不要依赖 RStudio、GUI R app 或交互式输入。
- `.R` 文件必须放在 Skill 包目录内，例如 `scripts/analyze.R`。
- Python wrapper 必须用包内相对路径定位 `.R` 文件，不能要求主代理读取本地路径。
- Python wrapper 应从受控候选路径查找 `Rscript`；找不到时输出清晰错误。
- Python wrapper 调用 Rscript 时必须显式传入最小可用 `PATH`，否则 R 进程可能找不到系统工具或 Rscript。
- `.R` 脚本 stdout 必须是 JSON object，stderr 只作为诊断。

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


### 6.3 脚本收到什么输入

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

### 6.4 脚本必须输出什么

stdout 必须是 JSON object，例如：

```json
{"answer": "脚本处理结果", "facts": ["..."]}
```

如果 `outputs.required` 或 `scripts[].outputs.required` 声明了必需字段，stdout JSON 必须包含这些字段，否则脚本结果会被视为失败，不会注入主代理 prompt。

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

### 6.5 脚本型 Skill 示例

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

## 7. Oh-my-codex `skill-creator` 提示词模板

把下面这段作为创建 Skill 的约束交给 Oh-my-codex `skill-creator`：

```text
请创建一个适配 multi_agent_framework 后端的 Codex Skill。必须遵守：
1. Skill 包只能依赖 SKILL.md；可选 scripts/ 下的 Python 脚本。
2. SKILL.md frontmatter 必须包含 name、description；中文任务必须包含高质量 triggers。
3. 本系统只会把 SKILL.md body 注入 LLM，不会自动读取 references/ 或 assets/。
4. 如需脚本，只能声明 scripts[].runtime=python，path 必须是包内相对路径，不能使用绝对路径、..、symlink、shell、node 或任意命令。
5. 自动脚本必须设置 auto_run: true，stdin 为 JSON object，至少包含 query、uploaded_artifacts、metadata；如需业务参数，必须用 parameters/input_parameters 声明可解析字段，LLM 只会在缺参时生成候选并由系统校验后注入，不要依赖主代理口头承诺传参；stdout 必须是 JSON object。若需要下载文件，写入 MAF_SKILL_OUTPUT_DIR 并用 output_files 声明；若需要 R 语言逻辑，不要声明 runtime:r；请创建 runtime:python 的 wrapper 调用包内 .R 脚本和 Rscript。
6. 如果声明 outputs.required 或 scripts[].outputs.required，脚本 stdout 必须包含这些字段。
7. 不要创建 README、安装指南、CHANGELOG 等额外说明文件；除非脚本需要，不要创建 references/ 或 assets/。
8. Skill 正文保持精简，写 Use when、Workflow、Output、Boundaries；不要放 secret、内网地址、数据库密码或要求模型读取本地路径。
9. 输出最终文件树和每个文件内容。
```

## 8. 构建检查清单

交付 Skill 前逐项检查：

- [ ] `SKILL.md` 存在，且以 YAML frontmatter 开头和闭合。
- [ ] `name` 非空，稳定且不和已有 Skill 重名。
- [ ] `description` 说明“什么时候使用”，不是泛泛描述。
- [ ] 中文 Skill 有明确 `triggers`。
- [ ] body 非空，且是主代理可直接遵循的操作说明。
- [ ] 没有把 secret、完整数据库连接串、API key 写进 Skill。
- [ ] 如果有脚本，`runtime` 是 `python`。
- [ ] 如果有脚本，`path` 是包内相对路径，不包含 `..`。
- [ ] 如果有脚本，stdout 是 JSON object。
- [ ] 如果声明了 required 输出，脚本真实输出包含这些字段。
- [ ] 如果脚本产出下载文件，文件写入 `MAF_SKILL_OUTPUT_DIR`，stdout `output_files[].path` 使用 `outputs/` 下相对路径，不使用绝对路径、`..`、symlink、hardlink 或目录，且不声明平台默认拒绝的源压缩包。
- [ ] 输出文件扩展名属于平台 allowlist；如声明 `mime_type`，MIME 与扩展名匹配。
- [ ] 如果 Skill 只产出固定文件类型，已用 `outputs.files` 声明更严格的扩展名 / MIME 约束，且该声明只收紧、不放宽平台默认规则。
- [ ] 如果脚本需要业务参数，已在 `parameters` / `input_parameters` 中列出所有可接受字段、类型、required、默认值、aliases 或 patterns，并在脚本内保留最终校验。
- [ ] 没有默认值且脚本必须依赖的业务参数已声明 `required: true`；有默认值的业务参数声明为非必填并写明 `default`，脚本默认值与 manifest 一致。
- [ ] 枚举型参数已用 `enum` 列出所有可接受值，脚本会拒绝不在枚举范围内的值。
- [ ] 不依赖执行时安装新包。
- [ ] 不依赖 cwd 指向 Skill 目录。
- [ ] 如果使用 R，Skill 仍通过 Python wrapper 暴露；`.R` 文件在包内，且 R stdout 是 JSON object。

## 9. 验证方法

### 9.1 验证 parser 能读取 Skill

```bash
python - <<'PY'
from pathlib import Path
from src.integrations.codex_skills import parse_skill_file

manifest = parse_skill_file(Path('skill/my-skill/SKILL.md'))
print(manifest.name)
print(manifest.triggers)
print([script.name for script in manifest.scripts])
PY
```

### 9.2 验证 catalog 能发现并匹配 Skill

```bash
python - <<'PY'
from src.integrations.codex_skills import SkillCatalog, match_skills

catalog = SkillCatalog.from_roots(['skill'])
matches = match_skills('帮我写周报', catalog)
print([(m.manifest.name, m.score, m.reason) for m in matches])
PY
```

### 9.3 验证脚本能通过受控 runner

```bash
python - <<'PY'
import asyncio
from pathlib import Path
from src.integrations.codex_skills import SkillScriptRunner, parse_skill_file

manifest = parse_skill_file(Path('skill/my-skill/SKILL.md'))
script = manifest.scripts[0]
result = asyncio.run(SkillScriptRunner().run(manifest, script, {'query': '测试输入'}))
print(result)
PY
```

### 9.4 运行现有回归

```bash
python -m unittest discover -s tests/integrations/codex_skills -p 'test_*.py'
python -m unittest discover -s tests/capabilities/main_agent -p 'test_*.py'
```

## 10. 常见错误

| 错误 | 结果 | 修正 |
|---|---|---|
| 只有 description，没有 triggers | 中文意图可能匹配不到 | 增加用户自然表达触发词 |
| `SKILL.md` 没有正文 | parser 拒绝 | 至少写清 workflow / output / boundaries |
| 脚本输出普通文本 | runner 拒绝 | 输出 JSON object |
| 脚本 path 写绝对路径 | runner 拒绝 | 改为包内相对路径 |
| 脚本依赖 cwd 读取文件 | 找不到文件 | 用 `Path(__file__).parent` |
| 写了 shell 脚本 | runner 拒绝 | 改写为 Python |
| 直接声明 `runtime: r` | runner 拒绝 | 使用 Python wrapper 调用包内 `.R` 脚本 |
| 把详细资料放 references/ | 后端 LLM 看不到 | 把必要内容压缩进 SKILL.md body，或用脚本读取并输出摘要 |
| 输出字段和 `outputs.required` 不一致 | 脚本结果失败 | 对齐 required 字段 |

## 11. 和标准 Codex Skill 的差异

| 能力 | 标准 Codex / Oh-my-codex | 本系统后端当前支持 |
|---|---|---|
| `SKILL.md` frontmatter | 支持 | 支持 `name` / `description` / `triggers` 等 |
| `SKILL.md` body | 触发后加载 | 匹配后注入主代理 prompt |
| `agents/openai.yaml` | UI metadata | 当前后端不读取 |
| `references/` | Codex 可按需读取 | 当前后端不自动读取 |
| `assets/` | Codex 可用于产物 | 当前后端不自动使用 |
| Python 脚本 | 可作为资源 | 仅 manifest 声明且 auto_run 时受控执行 |
| Shell / 任意命令 | Codex 环境可能可执行 | 不支持 |
| R 脚本 | Codex 可通过本地命令自行运行 | 当前通过 `runtime: python` wrapper 调用 `Rscript`，不支持直接 `runtime: r` |
| MCP / plugin runtime | Codex 插件可提供 | 不支持 |
| 本地文件访问 | Codex agent 可读 workspace | 主代理 LLM 不具备；脚本只可读自己包内可定位资源 |

## 12. 给 Skill creator 的推荐默认策略

- 首选 prompt-only Skill：简单、稳定、最符合当前主代理注入方式。
- 只有在需要确定性解析、统计、格式转换时才加 Python 脚本。
- Skill body 控制在 200 行以内；超过时优先删减，而不是拆 references，因为后端不会自动加载 references。
- triggers 比 description 更重要；每个 Skill 至少写 3 个真实用户会说的触发表达。
- 脚本只输出事实和结构化中间结果，最终措辞交给主代理 LLM。
