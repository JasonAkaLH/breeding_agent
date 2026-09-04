# 主 Agent 统一 Tool Result 50k Token 预算 Frontend 实施计划

## 状态与依据

- 日期：2026-09-04
- 目标分支：`main`
- 基线：`main@4d21bfbf`
- 状态：`implemented_verified_not_published`
- 设计依据：`2026-09-04-unified-tool-result-50k-token-budget-design.md`
- 已完成前置：Backend Checkpoint A～F和backend-dev `0.1.35`镜像发布

### 实际执行记录

- Checkpoint 0/A：新增超过旧20,000字符与80,000 bytes的structured、text、supplemental和Backend
  truncated preview红测，删除Frontend MCP业务长度guard；同消息多Result均保持ready且末尾sentinel完整；
- Checkpoint B：`failureMessage()`新增`model_unavailable`固定文案；closed Agent event validator同时接受旧
  `agent.run.failed`无code历史shape和新Backend带安全code shape；
- Checkpoint C：Frontend指南、当前API文档、API更新日志与developer-doc断言切到Backend唯一50,000-token
  authority；
- Checkpoint D：聚焦Frontend 3文件211项、Frontend全量24文件350项、相关Backend API/文档25项通过；
  typecheck和production build通过。构建主JS保留typed schema、`projection_invalid`与专用错误文案，
  `2e4`/`8e4`及旧MCP业务budget guard均零命中；
- 当前未构建或推送Frontend镜像，未更新`docker_cmd.md`，未部署或修改数据库/历史结果。

本计划只闭合此前明确延期的Frontend范围。Backend是50,000-token业务预算的唯一权威：每个Tool
Result完整返回并完成解析、校验和脱敏后，由Backend按Agent Run绑定模型恰好调用一次Provider
`POST /tokenization`；Frontend不调用Tokenization、不估算Token，也不再使用字符数或UTF-8 bytes
限制业务结果。Backend返回合法typed业务视图后，Frontend有多少展示多少。

## 1. 完成声明

只有同时满足以下条件，才可宣称Frontend切换完成：

1. `frontend/src/domain/artifacts.ts`不再定义或调用MCP业务视图的20,000-code-point、80,000-byte
   预算；不以新的字符、byte、数组项数、DOM长度或Token估算替代；
2. 合法`maf.mcp.business_result_view.v1`不论序列化长度均保持`availability=ready`，structured、text、
   supplemental texts和content metadata完整进入现有业务卡片；
3. Frontend仍严格校验闭合字段集合、schema、outcome、availability、primary discriminated union、
   JSON finite value和metadata结构；非法DTO继续`projection_invalid`，不得读取raw `storage_ref`兜底；
4. `projection_truncated`和primary `truncated`只忠实显示Backend的50,000-token裁剪事实，Frontend不再
   二次裁剪；
5. `model_unavailable`在`agent.run.failed`和`task.failed`实时/历史折叠路径统一显示固定中文文案，
   不暴露Provider endpoint、HTTP状态、响应正文或Tokenization细节；
6. 不修改Backend业务代码、Projection revision、数据库schema/data、历史Artifact或外部MCP/Skill；
7. 聚焦测试、Frontend全量、typecheck、production build、静态产物合同和最终diff门禁全部通过。

固定错误文案：

> 模型服务暂时不可用，无法完成本次请求，请稍后重试。

## 2. 严格范围

预计修改：

- `frontend/src/domain/artifacts.ts`
- `frontend/src/domain/artifacts.test.ts`
- `frontend/src/domain/taskEvents.ts`
- `frontend/src/domain/taskEvents.test.ts`
- `frontend/src/App.test.tsx`（只有真实渲染红测证明需要时）
- `frontend/AGENTS.md`
- `docs/api/api-doc.html`
- `docs/api/API更新日志.md`
- `tests/api/test_developer_docs.py`
- 本计划、原50k实施账本、`docs/AGENTS.md`和`CHANGELOG.md`

实际实施只修改红测或引用搜索证明必要的文件。明确禁止：

- 在浏览器调用Provider `/tokenization`，向浏览器下发Provider凭据，或引入本地Tokenizer；
- 增加分页、抽样、虚拟列表、下载raw、延迟补读或“展开后再取完整结果”；
- 修改业务卡片的现有折叠交互、结构化JSON格式、media/resource展示或公开DTO schema；
- 删除与Tool Result无关的限制，例如delegated Skill instruction 20,000-code-point输入合同、单公式资源
  限制、AgentItem 131,072-byte承载合同或MCP raw 64 MiB安全上限；
- 修改既有历史Tool Result、执行重投影、调用远端Tool、运行数据库migration或进入`prod`。

## 3. Checkpoint 0：基线与红测试

### 3.1 基线

```bash
cd frontend
npm run test:unit -- --run src/domain/artifacts.test.ts src/domain/taskEvents.test.ts
npm run typecheck
npm run build
cd ..
git diff --check
```

开始前确认工作树除用户既有`?? test.json`外无未说明改动；全过程不读取、修改或暂存该文件。

### 3.2 红测试

先增加当前实现必然失败的测试：

1. `structured`业务视图序列化字符数大于20,000但bytes小于80,000，末尾sentinel必须保留，结果仍为
   `ready`；
2. 中文/emoji `text`业务视图同时超过20,000字符和80,000 bytes，首尾sentinel均保留；
3. 大型`supplemental_texts`继续完整保留，不使用隐藏的总量门槛；
4. `projection_truncated=true`的大型Backend预览仍完整保留，并继续显示已有截断提示；
5. 同一assistant消息包含多个大型MCP Result时逐卡完整保留，不施加Frontend聚合预算；
6. 未知字段、错误schema、非法primary、NaN/Infinity和非法metadata继续安全降级为
   `projection_invalid`；任何断言均不得从`storage_ref`恢复内容；
7. `agent.run.failed`与`task.failed`携带`code=model_unavailable`时均得到固定专用文案，事件先后顺序
   不得被通用错误覆盖。

测试数据使用确定性字符串，不复制开发数据库中的真实业务正文。

## 4. Checkpoint A：删除Frontend业务长度门槛

在`frontend/src/domain/artifacts.ts`做最小修改：

1. 删除`MCP_MAX_CODE_POINTS`；
2. 删除`MCP_MAX_UTF8_BYTES`；
3. 删除`withinMCPViewBudget()`；
4. 从`parseMCPBusinessResultView()`入口条件中删除该函数调用；
5. 保持其余closed schema/type校验逐行语义不变。

Frontend不新增50,000-token常量。50,000-token只属于Backend业务预算；浏览器没有绑定模型的Provider
authority，也不应重复验证Backend已经裁剪和标记的结果。

完成后运行：

```bash
cd frontend
npm run test:unit -- --run src/domain/artifacts.test.ts
npm run typecheck
```

## 5. Checkpoint B：`model_unavailable`专用提示

在`frontend/src/domain/taskEvents.ts`的唯一`failureMessage()`映射中加入
`code === 'model_unavailable'`分支。不得在`App.tsx`、SSE client或MCP卡片分别复制映射。

测试至少覆盖：

- `agent.run.failed`首次设置专用错误；
- 随后的`task.failed`保持同一专用错误；
- 单独收到`task.failed`也显示同一文案；
- 其他未知错误继续使用原通用文案。

完成后运行：

```bash
cd frontend
npm run test:unit -- --run src/domain/taskEvents.test.ts src/App.test.tsx
npm run typecheck
```

若`App.test.tsx`无需改动且现有回归已经覆盖事件到UI的链路，不为满足文件清单强行修改。

## 6. Checkpoint C：文档与静态合同

1. `frontend/AGENTS.md`把“MCP非法/超预算DTO安全降级”改为“MCP非法DTO安全降级”；
2. 当前API文档删除Frontend或公共MCP业务视图20,000字符/80,000-byte合同，明确Backend按绑定模型
   50,000 tokens裁剪，Frontend不做长度校验；
3. `API更新日志.md`追加本次新合同，不改写历史条目；
4. 更新`tests/api/test_developer_docs.py`的当前文档断言；
5. 更新原50k实施账本、`docs/AGENTS.md`和`CHANGELOG.md`的准确状态。旧设计/实施记录中描述当时行为的
   历史文字保持不变。

静态扫描必须证明Frontend生产代码不存在MCP业务长度门槛：

```bash
rg -n 'MCP_MAX_CODE_POINTS|MCP_MAX_UTF8_BYTES|withinMCPViewBudget' frontend/src
```

期望零命中。不能用对全仓`20_000`零命中作为验收，因为仓库仍有与Tool Result无关的合法限制。

## 7. Checkpoint D：完整验证

按以下顺序运行并读取结果：

```bash
cd frontend
npm run test:unit -- --run src/domain/artifacts.test.ts src/domain/taskEvents.test.ts src/App.test.tsx
npm run test:unit -- --run
npm run typecheck
npm run build
cd ..
conda run -n multi_agent python -m unittest \
  tests.api.test_user_mcp_dto \
  tests.api.test_conversation_messages_artifacts \
  tests.api.test_developer_docs
git diff --check
```

检查`frontend/dist`生成的主JS：

- 不再包含MCP业务视图的`2e4`/`8e4`成对预算guard；
- 仍包含`projection_invalid`结构校验和`model_unavailable`专用中文文案；
- root Dockerfile `frontend` target的`/seedpilot/`路径不变。

最终审查每一行diff均能追溯到“Frontend不做业务长度校验”或已批准的
`model_unavailable`提示；无Backend、Rust、schema或外部Skill源码变化。

## 8. Checkpoint E：提交、镜像和发布边界

自动门禁全部通过后创建一个范围清晰的Frontend commit并推送`origin main`。随后：

1. 只读检查下一个`breeding-agent-frontend-dev`候选tag未占用；按现有序列预计为`0.1.29`，若已占用
   则停止并先修订账本，不覆盖已有tag；
2. 使用根`Dockerfile`的`frontend` target构建并推送`linux/amd64`镜像，不重建backend或
   runtime-sidecar；
3. 远端回拉并验证OCI digest、architecture、`nginx -t`、`/seedpilot/` health和静态bundle合同；
4. 在仓库外建立权限不高于`0600`的`docker_cmd.md`备份，只更新开发Frontend tag；不改端口、网络、
   volume、alias、凭据、Backend tag或任何`prod`命令；
5. 验证`docker_cmd.md`仍存在、权限不高于`0600`、Git-ignored且未被跟踪，不输出其中敏感内容。

镜像发布和`docker_cmd.md`更新不等于部署。停止/替换开发容器必须由用户另行明确授权。

## 9. 开发环境部署后验收

部署新Frontend后执行只读/用户可见验收：

1. 当前已发布合法v2 Projection的`conv-web-51208e548d5588`无需改数据库或重建数据；刷新后其
   27,458字符业务视图应保持`ready`并能展开到末尾，不再显示`projection_invalid`；
2. 新建一个小于50,000 tokens但超过旧20,000字符门槛的Tool Result，Frontend完整展示且Backend
   truncation flag为false；
3. 新建一个超过50,000 tokens的Tool Result，只允许Backend标记并裁到50,000 tokens；Frontend完整
   展示该Backend Projection和截断提示，不得二次缩短；
4. 同轮多个Tool Result逐Call验收，每张卡片完整展示各自Backend Projection；
5. 模拟/受控触发`model_unavailable`时，实时失败和刷新后历史状态均显示固定专用文案；
6. 远端静态主JS不再包含旧MCP `2e4`/`8e4`预算guard。

验收只创建后续新Task；不修改历史Tool Result、Artifact、Projection或数据库记录。

## 10. 回滚

- 代码回滚只撤销Frontend commit；Backend `0.1.35`、数据库和Projection v2不回滚；
- 镜像回滚恢复前一Frontend digest/tag；旧Frontend会把大型合法view重新安全显示为
  `projection_invalid`，但不得读取raw补偿；
- 无schema/data migration，因此不需要数据库回滚；
- 回滚后记录端到端50k UI目标再次变为未完成，不能宣称业务结果已损坏。
