# Multi-Agent Framework

一个面向云端业务场景的自研 Multi-Agent 框架骨架项目。

## 项目目标

- **不依赖 LangChain、LangGraph、AutoGen 等成品 Agent 框架**。
- **以 Python 为主**，后续允许把高性能热点下沉到 C++。
- **面向服务端运行**，而不是围绕 CLI 体验来设计。
- **保留强可扩展性**，方便后续接入模型适配器、工具系统、工作流编排、记忆层和观测能力。

## 当前初始化内容

- 服务端项目骨架（FastAPI 作为接入层）
- 统一配置管理
- Agent 协议与注册中心
- 最小编排器（Coordinator）
- 示例 Echo Agent
- 基础测试、类型检查与 lint 配置
- Docker 运行文件
- 架构说明文档
- 预留 `native/` 目录，供未来 C++ 模块接入

## 目录结构

```text
.
├── .env.example
├── .gitignore
├── .editorconfig
├── Dockerfile
├── README.md
├── docs/
│   └── architecture.md
├── native/
│   └── README.md
├── pyproject.toml
├── src/
│   └── multi_agent_framework/
│       ├── __init__.py
│       ├── app.py
│       ├── bootstrap.py
│       ├── config.py
│       ├── agents/
│       ├── api/
│       ├── core/
│       ├── infra/
│       └── orchestration/
└── tests/
```

## 设计原则

1. **服务优先**：核心能力通过服务层暴露，而不是围绕命令行脚本组织。
2. **协议优先**：先稳定 Agent contract、上下文、消息与编排边界。
3. **分层清晰**：接入层、编排层、Agent 层、基础设施层、原生扩展层解耦。
4. **异步优先**：后续多 Agent 并发、超时控制、事件流处理都以 async 为核心。
5. **逐步演进**：先搭骨架，再补模型适配、工具运行时、状态机、记忆层、追踪系统。

## 本地开发

```bash
python -m pip install -e ".[dev]"
uvicorn multi_agent_framework.app:app --host 0.0.0.0 --port 8000 --reload
```

## 质量检查

```bash
ruff check src tests
mypy src tests
pytest
```

## 当前接口

- `GET /api/v1/healthz`：健康检查
- `GET /api/v1/agents`：查看当前已注册 Agent
- `POST /api/v1/workflows/execute`：执行一个 Agent 请求

示例请求：

```json
{
  "agent_name": "echo",
  "messages": [
    {"role": "user", "content": "hello"}
  ]
}
```

## 下一步建议

1. 增加模型适配层（OpenAI / 私有模型网关 / 内部推理服务）
2. 设计工具调用协议与沙箱执行模型
3. 设计 session、memory、state store
4. 引入 DAG / 状态机式工作流编排
5. 增加 tracing、metrics、审计日志
6. 识别性能热点，再决定哪些模块下沉 C++
