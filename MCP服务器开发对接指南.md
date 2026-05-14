# MCP 服务器开发对接指南

这份文档给开发 MCP Server 的同事阅读。目标是让你的服务能被本项目顺利发现、调用和校验。

一句话：**请实现一个 HTTP 接口，接收 JSON-RPC 2.0 请求，按 MCP `2025-11-25` 协议返回工具列表和工具调用结果。**

---

## 1. 我们当前支持什么

当前本项目作为 **MCP Client** 调用你的 **MCP Server**。

我们当前支持：

| 项 | 当前要求 |
|---|---|
| MCP 协议版本 | `2025-11-25` |
| 消息格式 | JSON-RPC 2.0 |
| 传输方式 | Streamable HTTP，也就是 HTTP `POST` JSON |
| 必须支持的方法 | `initialize`、`notifications/initialized`、`tools/list`、`tools/call` |
| 工具输入 schema | JSON Schema Draft 2020-12 或 Draft-07 |
| 工具输出 | `content` 和可选 `structuredContent` |

当前不作为正式接入方式：

- `stdio` 启动本地进程；
- 交互式 OAuth；
- WebSocket；
- 自定义非 JSON-RPC 协议。

---

## 2. 你需要提供一个 HTTP endpoint

例如：

```text
https://mcp.example.com/mcp
```

本项目会向这个地址发 `POST` 请求。

请求头大致如下：

```http
Content-Type: application/json
Accept: application/json, text/event-stream
MCP-Protocol-Version: 2025-11-25
```

如果你的服务器需要鉴权，可以使用：

```http
Authorization: Bearer <token>
```

或者使用我们配置好的 API Key Header。不要把 token、密钥、数据库连接串写进响应、日志或工具输出里。

---

## 3. 第一步：初始化 initialize

客户端首次连接时，会先调用 `initialize`。

### 请求示例

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "initialize",
  "params": {
    "protocolVersion": "2025-11-25",
    "capabilities": {},
    "clientInfo": {
      "name": "multi_agent_framework",
      "version": "1"
    }
  }
}
```

### 正确响应

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "protocolVersion": "2025-11-25",
    "capabilities": {
      "tools": {}
    },
    "serverInfo": {
      "name": "customer-mcp-server",
      "version": "1.0.0"
    }
  }
}
```

注意：

- `jsonrpc` 必须是 `"2.0"`。
- 响应里的 `id` 必须和请求里的 `id` 一样。
- `protocolVersion` 必须返回 `"2025-11-25"`。
- 如果返回其他版本，本项目会拒绝连接。

初始化成功后，客户端会发送一条通知。

### 通知示例

```json
{
  "jsonrpc": "2.0",
  "method": "notifications/initialized",
  "params": {}
}
```

这条消息没有 `id`，说明它不需要业务结果。你的服务器可以返回 HTTP `202`、`204` 或空响应。

---

## 4. 第二步：返回工具列表 tools/list

初始化后，本项目会调用 `tools/list` 来发现你的服务器提供哪些工具。

### 请求示例

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/list",
  "params": {}
}
```

### 响应示例

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "tools": [
      {
        "name": "search_customer",
        "description": "按关键词查询客户信息。",
        "inputSchema": {
          "type": "object",
          "properties": {
            "keyword": {
              "type": "string",
              "description": "客户名称、简称或编号关键词"
            }
          },
          "required": ["keyword"],
          "additionalProperties": false
        },
        "outputSchema": {
          "type": "object",
          "properties": {
            "customers": {
              "type": "array",
              "items": {
                "type": "object",
                "properties": {
                  "id": {"type": "string"},
                  "name": {"type": "string"}
                }
              }
            }
          }
        }
      }
    ]
  }
}
```

如果工具很多，可以分页：

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "tools": [
      {
        "name": "search_customer",
        "description": "按关键词查询客户信息。",
        "inputSchema": {
          "type": "object",
          "properties": {
            "keyword": {"type": "string"}
          }
        }
      }
    ],
    "nextCursor": "page-2"
  }
}
```

客户端会继续请求：

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "tools/list",
  "params": {
    "cursor": "page-2"
  }
}
```

### 工具列表的要求

每个工具至少要有：

```json
{
  "name": "工具的稳定英文名",
  "description": "给人看的工具说明",
  "inputSchema": {
    "type": "object",
    "properties": {}
  }
}
```

建议：

- `name` 使用稳定英文名，例如 `search_customer`、`get_order_status`。
- 不要频繁改工具名；工具名变化会影响调用配置。
- `description` 写清楚工具做什么，不要写内部实现细节。
- `inputSchema` 尽量严格，能写 `required` 就写 `required`。
- 如果输出有稳定结构，提供 `outputSchema`。

---

## 5. 第三步：执行工具 tools/call

当系统决定调用某个 MCP 工具时，会发送 `tools/call`。

### 请求示例

```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "method": "tools/call",
  "params": {
    "name": "search_customer",
    "arguments": {
      "keyword": "Acme"
    }
  }
}
```

### 成功响应示例

```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "找到 2 个客户。"
      }
    ],
    "structuredContent": {
      "customers": [
        {
          "id": "cust_001",
          "name": "Acme Corp"
        },
        {
          "id": "cust_002",
          "name": "Acme China"
        }
      ]
    },
    "isError": false
  }
}
```

字段说明：

| 字段 | 是否必需 | 说明 |
|---|---|---|
| `content` | 建议必填 | 给模型和用户看的内容，最常用是 `type: text` |
| `structuredContent` | 可选 | 机器可读的结构化结果；如果提供了 `outputSchema`，会按 schema 校验 |
| `isError` | 可选 | `false` 表示工具执行成功；`true` 表示工具本身执行失败 |

### 工具执行失败，但协议正常

如果请求格式正确，只是业务上没查到或执行失败，可以返回 `isError: true`：

```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "没有找到符合条件的客户。"
      }
    ],
    "structuredContent": {
      "customers": []
    },
    "isError": true
  }
}
```

---

## 6. 协议错误怎么返回

如果是协议层或服务器层错误，用 JSON-RPC 的 `error` 字段。

### 错误响应示例

```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "error": {
    "code": -32000,
    "message": "上游服务暂时不可用",
    "data": {
      "retryAfterMs": 1000
    }
  }
}
```

常见情况：

| 场景 | 建议做法 |
|---|---|
| JSON 格式不对 | 返回 JSON-RPC error |
| method 不支持 | 返回 `-32601` |
| 参数不合法 | 返回 JSON-RPC error，说明哪个字段不合法 |
| 上游服务超时 | 返回 JSON-RPC error，不要返回半截业务结果 |
| 业务没查到 | 优先用 `result` + `isError: true` 或空结果 |

注意：错误信息里不要放 secret、token、数据库连接串、内部真实路径、完整 SQL、完整外部 URL。

---

## 7. HTTP 响应要求

推荐：

| 情况 | HTTP 状态码 | Body |
|---|---|---|
| JSON-RPC 正常响应 | `200` | JSON-RPC response |
| notification 已接受 | `202` 或 `204` | 可为空 |
| 未授权 | `401` 或 `403` | 可带 `WWW-Authenticate` |
| 服务端异常 | `500` 或 `503` | JSON-RPC error 或清晰错误 |

`Content-Type` 可以是：

```http
application/json
```

如果你要用 SSE，也可以返回：

```http
text/event-stream
```

SSE 的 `data:` 里仍然应该是 JSON-RPC JSON，例如：

```text
data: {"jsonrpc":"2.0","id":4,"result":{"content":[{"type":"text","text":"完成"}],"isError":false}}

```

---

## 8. 安全和输出边界

请遵守这些规则：

1. 不要在任何响应里返回密钥、token、数据库连接串、内部文件路径。
2. 不要把外部网页、文档、用户上传内容包装成系统指令。
3. 工具输出尽量短，长内容请摘要或分页。
4. 结构化结果尽量返回稳定字段，不要一次一个形状。
5. 对危险操作要谨慎。当前本项目公开接入的通用 MCP 工具默认只接受只读工具。
6. 如果工具有副作用，例如写数据库、发邮件、删文件，需要单独评审，不要混在普通只读工具里。

---

## 9. 和本项目联调时，你需要提供什么

请提供以下信息：

```yaml
server_id: customer_service
endpoint: https://mcp.example.com/mcp
protocol_version: 2025-11-25
transport: streamable_http

tools:
  - tool_name: search_customer
    description: 按关键词查询客户信息
    input_fields:
      - keyword
    output_shape: customers[]
    risk_level: read_only
```

如果需要鉴权，请额外说明：

```yaml
auth:
  type: bearer_env 或 api_key_env
  header_name: 如果是 API Key，需要说明 Header 名称
  token_owner: token 由哪一方提供和轮换
```

不要把真实 token 写在文档、邮件、聊天记录或代码里。

---

## 10. 最小可用对话流程

完整流程如下：

```text
1. 客户端 -> 服务器：initialize
2. 服务器 -> 客户端：返回 protocolVersion、capabilities、serverInfo
3. 客户端 -> 服务器：notifications/initialized
4. 客户端 -> 服务器：tools/list
5. 服务器 -> 客户端：返回 tools
6. 客户端 -> 服务器：tools/call
7. 服务器 -> 客户端：返回 tool result
```

只要你的 MCP Server 正确支持上面流程，就可以进入基础联调。

---

## 11. 联调检查清单

交付前请逐项确认：

- [ ] HTTP endpoint 可以访问。
- [ ] 只接受 `POST` JSON-RPC 请求。
- [ ] `MCP-Protocol-Version` 支持 `2025-11-25`。
- [ ] `initialize` 返回同样的 `protocolVersion`。
- [ ] `notifications/initialized` 能正常处理。
- [ ] `tools/list` 返回工具列表。
- [ ] 每个工具都有稳定的 `name`、`description`、`inputSchema`。
- [ ] `tools/call` 能按 `name` 和 `arguments` 执行。
- [ ] 响应 `id` 和请求 `id` 一致。
- [ ] 成功响应使用 `result`，协议错误使用 `error`。
- [ ] 工具输出不包含 secret、token、连接串、内部路径。
- [ ] 只读工具标注清楚；有副作用的工具单独说明。
- [ ] 超时、上游失败、参数错误都有清晰错误响应。

---

## 12. 一个最小请求 / 响应样例

### 请求

```json
{
  "jsonrpc": "2.0",
  "id": 10,
  "method": "tools/call",
  "params": {
    "name": "ping",
    "arguments": {
      "message": "hello"
    }
  }
}
```

### 响应

```json
{
  "jsonrpc": "2.0",
  "id": 10,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "pong: hello"
      }
    ],
    "structuredContent": {
      "reply": "pong: hello"
    },
    "isError": false
  }
}
```

这就是我们当前项目能消费的 MCP 工具调用主体格式。
