# 旧版 API 第三方调用兼容层实现计划

状态：`planned_not_implemented`。本次交付仅为实现计划，不代表兼容服务已经实现、测试或部署。

## 1. 已确认目标与基线

- 旧 API 基线：`1e48266b9e874f95be8087032c0dbf8018363377`，对应用户指定的 7 月 2 日无环境后缀 `0.1.23` 后端。
- 新版本审计基线：`main@88726f7a2aa5a9d9ed576ca71aecc84cb8e554b2`。实际接入哪个后端部署，由部署配置指定；不能把本地 main 等同于生产服务器正在运行的版本。
- Web 只使用新版前端；旧版网页不部署，也不作为兼容测试对象。兼容层只供调用旧版 HTTP/SSE API 的第三方程序使用。
- 现有仓库 `docker-compose.yml` 将新前端映射为宿主机 `51999:80`。外部入口继续使用 **51999**；第三方调用基址为 `/seedpilot/compat/`，旧 `/api/v1/*` 操作对应 `:51999/seedpilot/compat/api/v1/*`。
- 兼容 FastAPI 镜像只在容器网络内监听 8000，不发布 51998 或其他新宿主机端口。新版 MCP 设置继续使用新版 Web/API 入口。
- 新前端、新后端、Runtime Sidecar 的业务代码与镜像保持原样，不修改数据库结构和业务数据。为在 51999 增加路由，必须调整该端口的入口代理与容器端口发布配置。
- 当前工作分支是 main。本计划不切换分支，不操作生产，也不读取或改写 `docker_cmd/` 受保护文件。

成功标准是：旧版公开 API 操作在兼容路径上保持约定的请求、响应、错误和 SSE 语义；实际第三方程序需要的旧业务流程逐项通过验收。新版 Web 及新版 API 继续通过原路径工作。接口返回 202 只是准入成功，不能作为业务兼容完成的证据。

## 2. 架构与交付边界

```text
第三方程序 ── :51999/seedpilot/compat/api/v1/* ── 51999 入口代理 ── 兼容 FastAPI:8000 ── 新版后端
新版 Web   ── :51999/seedpilot/* ────────────── 51999 入口代理 ── 原新前端镜像:80 ── 新版后端
```

采用独立 HTTP 服务与独立入口代理。入口代理先匹配 `/seedpilot/compat/`，将请求转给兼容 FastAPI；其余路径交给原新前端镜像处理。入口代理占用原宿主机 51999，新前端容器只在内部网络提供端口 80。这会改变部署编排，前端和后端业务镜像不重建。

### 2.1 对外接口与网络

1. 独立 FastAPI 镜像内的 Uvicorn 监听 `0.0.0.0:8000`，Dockerfile 声明 `EXPOSE 8000`。兼容容器只加入内部网络，不配置宿主机 `ports`。
2. 第三方程序的兼容 API 基址为 `/seedpilot/compat`。旧接口路径 `/api/v1/...` 拼接后为 `/seedpilot/compat/api/v1/...`；SSE、上传和下载沿用同一前缀。
3. 入口代理用优先级高于 `/seedpilot/` 的 `/seedpilot/compat/` 路由，精确去掉该前缀，将其余 `/api/v1/...` 送给兼容 FastAPI。兼容应用只处理规范的 `/api/v1/*`，不在不同层重复改写前缀。
4. 入口代理将其他 `/seedpilot/*` 原样转给新前端容器，保留它对新后端的现有 API、静态资源、文档及 SSE 转发行为。新增 MCP 设置仍通过新前端入口访问新版后端。
5. 第三方程序需把原请求基址改为 `/seedpilot/compat`；兼容服务不为其提供网页静态资源。若调用者自行拼接完整 URL，按固定路径前缀逐项核对。
6. 第三方程序按普通 HTTP 客户端调用，不新增浏览器专用 CORS 逻辑。入口继续沿用原 51999 的 HTTP/TLS 配置。
7. 上游地址由运营者配置，客户端不能通过 URL、请求头或请求体选择任意上游。上游 URL 不得指回入口代理形成循环。

计划中的独立兼容 Dockerfile 使用容器内部端口：

```dockerfile
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
```

入口代理的核心路由采用如下 Nginx 语义；完整配置仍需保留新前端原有 Host、转发头、连接和静态页面行为：

```nginx
location ^~ /seedpilot/compat/ {
    proxy_pass http://legacy-compat:8000/;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 3900s;
}
location / {
    proxy_pass http://frontend:80;
}
```

前一个 `proxy_pass` 带尾部 `/`，将 `/seedpilot/compat/api/v1/...` 映射为内部 `/api/v1/...`。兼容容器与入口代理加入可到达彼此的网络；兼容容器还需可到达 `LEGACY_UPSTREAM_BASE_URL` 指向的新版后端。只有入口代理发布宿主机 `51999:80`，原新前端容器取消宿主机 51999 绑定。`EXPOSE` 只是镜像元数据，不发布宿主机端口。

### 2.2 运行参数

| 参数 | 用途与规则 |
|---|---|
| `LEGACY_UPSTREAM_BASE_URL` | 必填，新版后端可达的固定 origin，不包含 `/api/v1`；不写入凭据，不默认猜测开发或生产地址。 |
| `LEGACY_MODEL_EFFORT_MAP_FILE` | 按目标模型目录生成并审阅的显式强度映射；映射规则固定在一次兼容服务发布内。 |

上游地址、现有第三方调用样例和目标模型/Skill 清单是未来接入时的环境输入。本计划不填入推测的生产地址或凭据，也不需要为编写计划取得这些访问权限。

### 2.3 独立性与状态

- 使用 Python、FastAPI、httpx、uvicorn，版本优先沿用仓库现有锁定版本；兼容服务仅安装自身需要的依赖。
- 通过公开 HTTP API 访问新版后端，不导入 `src/api/runtime.py`，不直接连接数据库、Sidecar、模型服务或 MCP Server。
- 首期不增加数据库、Redis、后台任务队列或第二套任务状态机。转换状态限于当前请求/SSE 连接；重连从上游公开 Task、Interrupt、Artifact、History 恢复。
- 用户、会话、任务、附件、授权和消息身份以新版后端为准。兼容服务不签发替代 token，不保存 MCP 凭据，不为越权资源补读存储。
- 默认不自动重试有副作用的 HTTP 请求，不把上游尚未确认的消息或授权答复提前伪造为 202。

## 3. 必须实现的转换合同

### 3.1 请求、能力与模型

| 输入或场景 | 兼容规则 | 验收要点 |
|---|---|---|
| 旧 `main_agent.respond`，没有 Skill 绑定 | 转为 `routing_mode=auto`、`capability_id=null`；保留正文、会话、附件和消息 ID。 | 普通聊天完成，旧 capability 不进入新后端。 |
| 旧 `main_agent.respond` + `metadata.soft_skill_binding.capability_id` | 验证 Skill 当前可见且可用，转为 `hint + skill.*`；只删除已完成转换的旧绑定元数据。 | 任务确实携带所选 Skill hint，不只是避免报错。 |
| 已清空旧主 Agent ID，但仍携带 `soft_skill_binding` | 同样提取真实 Skill，生成 `hint + skill.*`；若与已有顶层 Skill 选择冲突则拒绝。 | 兼容局部修改过的旧请求，不静默丢弃 Skill 选择。 |
| `auto` 携带非空 capability | 依旧版运行时对该 capability 的精确语义规范化；无法确认语义时返回明确错误，不一律清空 capability。 | 不丢失调用者明确的能力选择，不把未知 ID 变成普通聊天。 |
| 已经是合法新版请求形状 | 保留语义，不做二次降级。 | 相同规范化输入产生相同上游请求。 |
| 模型目录、已缓存模型 ID | 将新版目录投影为旧 API 的 value/label 合同，以新版可用目录为准；模型失效时报错。 | 不将未知模型悄悄替换为默认模型。 |
| 显式 `minimal/high/max` 与 thinking 参数 | 同名且合法的参数原样使用；不支持的强度只按显式模型映射转换。thinking 状态必须保持。 | 第三方实际使用的模型和强度组合逐项通过。 |
| 模型不支持调用方提交的 thinking 状态 | 返回明确的参数错误；新版模型仍可供新版 Web 使用。 | 不暗中启用 thinking 或改换模型。 |
| 同一 `client_message_id` 重试 | 保留原 ID，规范化规则在该兼容版本内确定；冲突继续返回错误。 | 相同请求重试不重复执行，不同正文不能覆盖旧消息。 |
| 上传与文件选择 | 保留 multipart、upload_ids 和旧客户端实际发送的有效补参字段。 | 上传正文不转成 JSON/Base64，不无故重传附件。 |
| 旧版可接受的 file selector/metadata 别名 | 对 `file_intent`、`accepted_file_types`、`needs_file`、`requires_file` 等按旧 DTO/运行时逐字段核对，再映射为新版相应字段。 | 已发布旧 API 的合法输入仍可用，未知字段不被猜测。 |

模型强度转换是发布配置的一部分：实施时先枚举目标目录，为缺少同名强度的组合给出明确映射表和差异说明，再启用该模型。不得用随模型目录变化的“自动取最高/默认值”改变一次重试的请求内容。

第三方程序的实际调用样例用于确定优先级，旧版公开合同用于确定兼容范围。不能用历史网页的请求子集代替完整的旧 API 合同。

### 3.2 HTTP 响应、最终回答与业务产物

1. 按旧 OpenAPI 的响应字段、类型、状态码和错误结构投影，逐操作判断新版新增字段是否会影响严格 schema 客户端。
2. Task、Conversation、Message、Upload 的真实 ID 保持不变，便于第三方程序与新版 Web 访问同一份数据。
3. 最终答案只使用新版正式 final Artifact 或该 Task 对应的正式 assistant history；不能取第一个 text 作为答案。
4. 只有旧合同要求的 producer/Artifact 语义才做响应投影；Artifact ID 和下载身份保持真实可验证。不得为取悦某个网页 parser 伪造不可下载的 ID，也不回写上游。
5. 文件下载经兼容入口转发，保留文件名、类型、状态码和真实下载鉴权。只改写已知上游或相对下载 URL，禁止从任意远端 URL 拉取内容。
6. 原有 Skill 的查询表格、OCR 文本和生成文件按旧 API 数据形状验收，不能用普通回答存在代替结构化结果。
7. 新 `mcp_result` 只读取公开 `mcp_business_result` 及投影状态；仅在可以保留旧 API 语义时转换。deferred、永久不可用、执行状态未知要有可判别状态，不伪装成功空结果，不开放 raw MCP result。
8. 实时结果与后续历史查询使用一致投影，避免同一 Task 得到两种矛盾结果。
9. graph 返回真实活动节点与空 edges；不生成虚假的 DAG 依赖关系、关键路径或调度结论。依赖旧 DAG 推断的第三方业务需单独识别为能力缺口。

### 3.3 SSE 事件与恢复

兼容 SSE 是独立的事件消费与投影模块，不能用字符串全局替换实现。

| 新版事实 | 旧入口输出 |
|---|---|
| `agent.run.completed` | 旧 `task.completed`；必要时先发送真实最终文本的 `main_agent.output_delta/output_final`。 |
| `agent.run.failed` | 旧 `task.failed`，保留失败状态和可公开错误信息。 |
| `agent.run.cancelled` | 旧 `task.cancelled`，不得改成完成。 |
| `agent.run.waiting` | 先确认当前 open Interrupt，再生成旧 `node.waiting_for_input` 和一致的 interrupt_id/node_id。 |
| `agent.run.resumed` | 使用旧客户端可识别的恢复事件；只针对当前 Task 和真实恢复。 |
| 已兼容的 Skill/节点进度 | 保留旧公共事件合同中需要的字段，验证第三方事件消费者可解析。 |
| `auth.invalidated` | 保持登录失效语义，终止流与上游订阅。 |

实现约束：

- 同时生成正确的 SSE named event 和 JSON envelope.event_type，保持 task_id、conversation_id、node_id 等关联一致。
- 衍生 event_id 由上游事件身份和转换种类确定，同一事件重新投影不能生成全新随机 ID。
- 当前上游 SSE 路由没有暴露可直接使用的 Last-Event-ID 恢复合同；不能假设把该请求头转发就完成断点续传。
- 对历史事件回放、重复终态、已回答的 Interrupt 和新连接快照分别处理。新连接不能因回放旧 waiting 事件重新弹出已结束的补参问题。
- 使用完整 SSE 帧解析，处理跨 chunk UTF-8、多行 data、CRLF、心跳、断连和慢消费者；有界缓冲并保留背压，不一次性读完整个流。
- 上游完成与结果展示分开判断：最终结果尚不可读时，不生成虚构文本；通过公开只读接口做有界补查。若仍不可读，保留真实任务终态并提供结果暂不可用提示，不把已完成任务改成执行失败。
- 上游没有最终文本逐 token 公共流时，可在结果可读后发送一次完整 delta，再发终态；不得人为切片冒充实时模型输出。
- `agent.reasoning_delta/reset` 具有样本撤回语义。旧 API 消费者若假定 reasoning 只增不减，不能把可撤回内容直接转成旧增量；只有公开合同能证明内容已稳定时才发送。不得读取内部审计流或把瞬时 reasoning 存库重放。
- 下游断开时关闭对应上游 SSE；退出、token 失效、服务重启不能留下无限后台订阅。

### 3.4 Interrupt、MCP 授权与在线状态

旧版 API 已支持的 Skill 文本补参、附件补交和工作表选择必须按原请求/响应合同验收完整闭环。

MCP 设置留在新版 Web/API。第三方旧请求仍可能使新后端触发 MCP，兼容层必须如实暴露运行中的等待状态：

1. GET Interrupt 保留真实 interrupt_id、reason_code、问题及所需字段，不能把 MCP 授权伪装成普通 Skill 文本补参。
2. 旧程序明确提交了可核验的结构化 MCP 授权值时，使用当前用户身份校验 Interrupt 归属与状态，再映射为 `metadata.mcp_tool_approval=allow_once|always_allow|deny`。自由文本“允许/继续”不能推断成授权。
3. 旧版 API 没有表达能力的 MCP 输入表单和逐 Tool 授权返回明确的待处理 Task/Interrupt 信息，由调用方使用新版 API 或新版 Web 完成；不自动批准或猜测字段。
4. 用户主动取消 Task 时转发真实取消接口；授权拒绝、断开 SSE 和取消远端任务保持各自语义。
5. 保留同步补参 POST 的真实结果，不提前返回假成功。上游 SSE 订阅仅随真实第三方连接/请求存续；请求结束或断开即释放。
6. 新后端 MCP 在线服务默认离线宽限为 300 秒。第三方断开 SSE 后的取消属于实际后端行为；兼容层不能在调用者离开后无限保活。
7. 测试连续 Interrupt、明确授权、自由文本拒绝、同步 POST 期间再次等待和离线超时。无法由旧公共 API 表达的新功能记入交接限制，不记为旧功能已兼容。

### 3.5 鉴权、错误与资源隔离

- 登录、刷新、退出使用新版公共接口；Bearer token 透传。旧 token 失效时重新登录，不从失效 token 推导可信身份。
- 所有 Task、Artifact、Interrupt 的补查也必须使用请求者的 token；不能使用共享管理员 token。
- 401/403、真正的会话忙、消息身份冲突、参数错误、资源缺失和上游不可用保留真实含义；响应正文保留可追踪错误码。
- 按旧 API 的状态码与错误体合同处理 400/401/403/404/409 等；同为 409 的会话忙、消息身份冲突和能力不可用不能被统一误报。已受理后发生错误时保留可机读的真实原因，不能将错误改成 200。
- 本地校验失败与上游已经受理后连接中断分别记录；后者不能自动重新提交并创建第二个任务。
- 只记录路由、状态、耗时和错误类别等低敏诊断，不记录 Authorization、正文、上传内容或 MCP 凭据。

## 4. 拟新增文件

以下仅为未来实施位置，本次不创建这些代码或配置文件。

```text
compat/legacy_api/
  AGENTS.md               # 独立服务边界
  README.md               # 配置、启动、第三方接入与限制
  app.py                  # 旧 API 路由、生命周期、healthz
  config.py               # 固定上游与模型映射校验
  upstream.py             # HTTP 转发、鉴权头、上传下载流
  request_mapping.py      # 聊天、Skill、模型与旧请求规范化
  response_mapping.py     # Task、History、Artifact、错误投影
  sse.py                  # SSE 解析、转换、重连状态与背压
  interrupts.py           # 补参、明确授权、当前 Interrupt 校验
  requirements.txt        # 最小依赖与固定版本
  Dockerfile              # 独立构建，只 COPY 兼容服务；内部 EXPOSE 8000
  edge-nginx.conf         # 51999 入口的 /seedpilot/compat/ 优先路由
  compose.yaml            # 入口代理发布 51999；兼容服务仅内部网络可达
tests/compat/
  fixtures/               # 脱敏的旧 API/新 API wire fixtures 与来源说明
  test_legacy_contract.py
  test_legacy_requests.py
  test_legacy_responses.py
  test_legacy_sse.py
  test_legacy_interrupts.py
  test_legacy_proxy.py
docs/runbooks/legacy-api-compatibility.md
```

按实际实现可合并很小的模块，不为每个 endpoint 创建独立框架。新服务构建上下文限定在 `compat/legacy_api/`，不复用现有 Backend Docker target，不把旧源码、测试、部署凭据打进运行镜像。更新新增目录与测试入口对应 AGENTS 索引及 CHANGELOG；现有业务文件保持不变。

## 5. 实施顺序与每阶段完成条件

### P0：固定合同与功能清单

- 从两个固定提交提取 OpenAPI、公开 HTTP 路由、请求校验、响应序列化、SSE 事件和 Interrupt 合同。
- 将旧版 22 个公开 HTTP 操作逐项标记为透传、请求转换、响应转换或 SSE 转换，确认 `/seedpilot/compat/` 前缀覆盖完整。
- 收集实际第三方调用样例并脱敏，覆盖登录、会话、历史、普通消息、能力/Skill 选择、模型参数、文件、补参、取消、产物与 SSE。不能取得真实调用样例时，以旧版后端公共合同形成完整 fixture，并将真实第三方联调列为发布门禁。
- 按目标可用模型与 Skill 列出能力依赖；缺失业务实现不能算协议兼容器缺一条映射就可解决。
- 先建立已知失败样例：`main_agent.respond`、Skill hint 丢失、旧 selector 字段拒绝、新终态不识别、多 text 误选及 MCP 等待。

完成条件：旧 API 合同差异逐操作归档，预期失败可复现；每个旧操作有合同 fixture，实际第三方联调的输入与可用性已明确。

### P1：独立服务与透明代理

- 新建独立应用、HTTP client 生命周期、配置校验、方法/路径白名单和 healthz。
- 新建 51999 入口代理配置，让 `/seedpilot/compat/` 优先命中内部兼容服务，其余请求原样转发给新前端镜像；新前端镜像保持原样，仅改部署端口发布。
- 打通登录、会话、文件上传/下载的透明转发；移除逐跳头，改写正文后重新计算长度/编码相关响应头。
- 大文件、SSE 和普通 JSON 使用不同的读取路径；上传/下载保持有界内存与连接清理。
- 本地编排只增加入口代理与兼容容器，不重建现有 Backend/Frontend 镜像；检查同一宿主机只有入口代理绑定 51999。

完成条件：`/seedpilot/compat/api/v1/*` 与 `/seedpilot/api/v1/*` 分流准确，鉴权和用户隔离保持，流式上传下载内容一致；停止适配器不影响新版入口。

### P2：消息准入与 Skill/模型转换

- 实现第 3.1 节的确定性映射，保留所有业务身份与附件引用。
- 接入模型目录和显式强度映射；验证关闭 thinking、缓存模型失效和 Skill 不存在。
- 加入相同 ID 精确重试、冲突请求、同文不同 ID、并发提交测试；不使用内容哈希去重合法的重复发言。

完成条件：旧 API 的合法普通消息、能力选择与 Skill 绑定提交到正确新版执行入口；相同消息重试不重复执行，参数变化不被隐式吞掉。

### P3：结果、历史与错误投影

- 实现旧响应 schema、最终文本身份、结构化结果、文件 URL 和下载字节流的合同投影。
- 对 MCP 公开业务视图与不可用状态做可机读投影，保持 raw result 隔离。
- 统一实时查询与历史记录转换，建立固定错误映射表。

完成条件：按旧响应 schema 解析的第三方消费者可识别真实 final、表格、OCR 和文件；后续历史查询一致，损坏/过期/不可用不伪装成功。

### P4：SSE 与状态恢复

- 实现 named event/envelope 双层转换、稳定事件身份、等待/恢复/三种终态映射。
- 补齐上游历史回放、旧 waiting 去除、最终结果晚于事件可读、断线与慢消费者。
- 运行旧 SSE 合同消费者与真实 HTTP 流测试，不能只对转换后的 JSON 做断言。

完成条件：第三方事件消费者可识别完成/失败/取消；无重复答案、陈旧补参、跨用户事件和遗留订阅。

### P5：补参及 MCP 运行中交互

- 打通原有文本、文件和工作表补参。
- 保留旧 API 可表达的补参合同；对 MCP 新授权/输入提供可机读的待处理信息，由调用方使用新版 API/Web 完成。
- 验证同步答复期间的上游订阅生命周期、连续 Interrupt、授权精确重试、token 失效和离线宽限。
- 若协议包装无法保持某项旧业务流程，形成具体失败证据与影响范围，不修改新版来掩盖问题。

完成条件：旧 API 的原有补参业务闭环；新 MCP 交互明确交接；没有自动授权、重复 Tool 副作用或后台无限保活。

### P6：第三方合同与新版 Web 对照验收、独立打包

- 用旧 OpenAPI 合同测试和实际第三方程序调用兼容路径，逐项跑完整功能矩阵。
- 用当前新前端通过原路径访问同一个隔离新版后端，验证普通业务与 MCP 设置/授权/结果流程。
- 以两个用户验证会话、附件、Task、Interrupt 的权限边界；以同一用户验证第三方入口与新版 Web 数据一致性。
- 构建独立兼容镜像与入口配置，检查兼容容器只有内部端口 8000、宿主机仅 51999；测试代理重启、兼容容器停止和新入口恢复。
- 输出兼容范围、明确限制、镜像 digest、上游源码/镜像版本和测试结果。实际后端与审计基线不一致时重跑合同与业务验收。

完成条件：下表全部旧 API 必需用例通过，实际第三方联调通过或明确记录未完成的外部验收；禁止把 mock 后端通过标记为真实部署通过。

### P7：后续部署与回退手册

- 部署前核对目标环境、实际上游版本、51999 入口代理归属、网络连通、现有 TLS、第三方调用基址和模型/Skill 配置。
- 兼容服务只在内部网络启动；把 51999 入口代理的 `/seedpilot/compat/` 路由接到它，其余路径继续到新前端镜像。
- 发布后运行旧入口 smoke 和新入口对照，记录错误类别、流结束状态、连接释放和延迟。
- 回退优先恢复上一兼容镜像；首次发布失败则撤回 `/seedpilot/compat/` 路由并停止兼容容器，51999 的新版 Web 路由保持可用。直接连接不兼容的新后端不能称为恢复旧 API 功能。
- 停止适配器不会回滚上游已受理的任务；维护前检查在途任务，MCP SSE 断开仍受新版离线规则约束。

本阶段是未来操作计划。本次不启动服务、不推送镜像、不切流、不更改生产配置。

## 6. 验收矩阵

| 类别 | 必须覆盖的证据 |
|---|---|
| 旧请求合同 | 22 个旧操作；`/seedpilot/compat/api/v1/*` 路由；普通 auto；旧主 Agent ID；Skill 绑定；旧 metadata 别名；未知 ID。 |
| 模型 | 第三方实际提交的 thinking/effort 组合；映射缺失；模型下线；相同请求重试。 |
| 身份与并发 | token 失效/退出；A 用户访问 B 用户资源；同 ID 同正文；同 ID 不同正文；同文不同 ID；会话忙。 |
| 上传下载 | 原有支持的文件类型；multipart；取消/中断上传；文件名；过期/损坏文件；下载大小与哈希一致。 |
| SSE | named event；UTF-8 分片/多行；心跳；重复/回放；等待→恢复；完成/失败/取消；断连/刷新/重启；背压。 |
| 结果 | 单 final；中间 text 排在 final 前；查询表格；OCR；生成文件；公开 MCP 视图与不可用状态；历史一致。 |
| 补参与授权 | 旧文本/附件/工作表补参；明确结构化授权值；含糊答复拒绝；新版 MCP 待处理信息；离线超时。 |
| 独立部署 | 51999 两路径分流；兼容容器无宿主机端口；现有 TLS；上游故障；兼容服务停止/回滚；新版入口仍工作。 |
| 新版对照 | 新版 Web 的普通对话、Skill、MCP 设置/授权/结果、历史与下载仍走 `/seedpilot/*`，不经过兼容服务。 |

测试层级：纯转换单元测试 → httpx/ASGI 隔离集成 → 旧 OpenAPI 合同 fixture 与实际第三方调用 → 真正 HTTP SSE 流 → 新版 Web 原路径对照。涉及 Runtime/Sidecar 的真实上游场景使用既有测试部署，不在兼容服务内复制执行逻辑。

## 7. 已知限制与发布判定

以下内容必须写入交付说明，不能通过协议包装宣称恢复：

- 旧 DAG 的真实依赖执行语义；已删除的模型、Skill 或后端业务能力。
- 上游不存在的实时 token 输出；可撤回逐样本 reasoning 无法转换成只增不减的旧事件合同。
- 旧 API 无法表达的 MCP 设置、逐 Tool 授权与复杂输入表单；第三方需使用新版 API 或新版 Web 处理这些新功能。
- 旧密钥签发 token 的自动延续、旧运行中 DAG Task 的自动复活，以及服务器尚未完成的数据库/配置迁移。

上述 API 语义差异与旧业务失败分别记账。P0 形成真实旧 API 合同清单，P6 逐项判断；若旧版合法请求被兼容层拒绝且没有明确可行的替代路径，整体不能标记为旧 API 兼容完成。

## 8. 当前代码依据与本次验证范围

- 请求入口和 routing 校验：`src/api/dto.py`、`src/api/routes/conversations.py`、`src/api/runtime.py`。
- 新旧请求合同：两个固定提交中的 `src/api/dto.py`、能力注册与公开 OpenAPI。
- SSE、Task 与 Artifact API：`src/api/routes/tasks.py`、`src/api/sse.py`、`src/api/agent_projection.py`。
- MCP 在线行为：`src/lifecycle/mcp_presence.py`。
- 51999 路由与部署基线：当前 `docker/nginx.conf`、`docker-compose.yml`；实施时调整入口编排，保持新前端业务镜像不变。

本次仅做源码核对、计划一致性检查和文档 diff 检查。计划中的服务、测试、镜像和服务器验收均尚未执行；此前审计结果不能替代未来兼容层实现的验证。
