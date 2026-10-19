# API 指南

[English](api.md) · [项目介绍](../README.zh-CN.md)

基础地址：`http://127.0.0.1:8100/api/v1`。交互文档：`/api/docs`。精确的输入、输出与约束以运行时 `/api/v1/openapi.json` 为准。界面切换语言只影响展示文案，机器字段、枚举与 ID 保持稳定。后端错误说明可能为中文，调用程序应判断 `code`，而不是匹配翻译文本。

## 鉴权与写入

脚本使用 `Authorization: Bearer WORKSPACE_TOKEN`，令牌由本机生成，保存在 `.runtime/admin.local.json`。浏览器使用会话 Cookie，写入同时需要 `X-CSRF-Token`；本机会话入口仅面向本地环境。

`POST /mailboxes/{mailbox_id}/tokens` 可签发仅限一个邮箱的读取、正文获取、等待与事件权限。界面签发一小时令牌，且仅在生成后展示一次；不会暴露来源平台密钥。

创建邮箱与持久等待任务需要 8–128 字符的 `Idempotency-Key`。同一路由复用相同键与请求体会返回已记录结果；相同键搭配不同请求体返回 409 `idempotency_conflict`。上游创建状态不确定时，不应自动换键重试，以免产生额外邮箱。

## 资源地图

| 操作 | 方法与路径 |
| --- | --- |
| 健康状态与 worker 心跳 | `GET /health` |
| 来源与能力元数据 | `GET /providers` |
| 加密来源配置 | `PUT /providers/{provider_id}/configuration` |
| 创建、列出、查看邮箱 | `POST /mailboxes`、`GET /mailboxes`、`GET /mailboxes/{mailbox_id}` |
| 排队清理本地邮箱 | `DELETE /mailboxes/{mailbox_id}` |
| 排队立即同步 | `POST /mailboxes/{mailbox_id}/syncs` |
| 列信、读取正文 | `GET /mailboxes/{mailbox_id}/messages`、`GET /mailboxes/{mailbox_id}/messages/{message_id}` |
| 排队补取正文 | `POST /mailboxes/{mailbox_id}/messages/{message_id}/fetches` |
| 查看异步操作 | `GET /operations/{operation_id}` |
| 创建、查看、取消等待任务 | `POST /mailboxes/{mailbox_id}/waits`、`GET /waits/{wait_id}`、`DELETE /waits/{wait_id}` |
| 工作区、单邮箱事件 | `GET /events`、`GET /mailboxes/{mailbox_id}/events` |

## 先创建，再观察结果

```json
{
  "provider_id": "grabmail",
  "name": "Product QA",
  "address_prefix": "qa-",
  "ttl_seconds": 3600,
  "labels": ["staging"]
}
```

`ttl_seconds` 为 300–86400。`address_prefix` 可省略；null、空字符串与纯空格均表示随机生成。自定义前缀最多 48 位，只含英文字母、数字和连字符，首位须为字母或数字；始终追加 16 位随机十六进制字符。先查看 `capabilities.custom_prefix`，不支持的来源返回 422 `custom_prefix_unsupported`。

创建返回 202 操作资源，包含 `id`、`state`、`resource_id`。轮询 `GET /operations/OPERATION_ID`，直至 `succeeded`、`failed` 或 `uncertain`；再通过 `GET /mailboxes/MAILBOX_ID` 获取地址。`uncertain` 不能简单理解为上游一定未创建成功。

## 列信与读取

列表使用绑定资源的不透明游标，返回 `items`、`has_more`、`next_cursor`。不能把游标挪给另一个邮箱或列表。消息包含 `ingest_seq`、`body_state`、身份强度与本地首次观测时间；邮箱同步信息单独报告新鲜度、错误和列表完整性。

详情提供纯文本正文、提取候选及其来源字符区间。只有摘要的邮件可能需要显式调用 `/fetches` 补取正文。上游正文作为内容展示，不执行其中 HTML。

## 等待新来信

```json
{
  "timeout_seconds": 120,
  "predicate": {
    "from_domain": "example.com",
    "subject_contains": "verification"
  }
}
```

超时为 5–300 秒。可选 `after_seq` 指定起始水位，省略时使用当前邮箱序号。发件域名精确匹配，主题与正文使用包含条件；多个条件同时满足才命中。水位之前的旧邮件不会误触发任务。

创建返回 201 持久等待资源。`GET /waits/WAIT_ID` 支持通过 `timeout_seconds` 做有界长轮询；命中后提供 `matched_message_id`。其他终态包括 `cancelled`、`timed_out`、`mailbox_expired`。DELETE 取消任务，关闭浏览器不会取消任务。

## 事件与错误

SSE 接口提供资源变更通知，使用事件 ID 支持重连；游标语义以 OpenAPI 为准。收到事件后仍以资源读取结果为准。排队操作与等待任务分别具有自己的 ID 与终态。

错误采用 `application/problem+json`，包含 `status`、`code`、`detail`、`request_id`。常见错误有 `validation_failed`、`provider_credential_required`、`custom_prefix_unsupported`、`mailbox_expired`、`idempotency_conflict`、`workspace_limit`。上游错误会保留在同步或操作状态中。

## 本机测试箱

使用 `provider_id: "local"` 创建邮箱，待状态活跃后，将测试邮件发送至分配的 `@local.linsemail.test` 地址，SMTP 为 `127.0.0.1:1025`。监听器只接收活跃的本机收件人；该能力不会配置公网 MX 或公网 SMTP 投递。

## 智能路由、组合任务与事件集成

建箱的 `provider_id` 可省略。`GET /routing/preview` 返回候选与分数；`GET /providers/intelligence` 返回健康度、P95 延迟与等待结果样本。手动探测 `POST /providers/PROVIDER/probes` 返回 202 和任务 ID。

`POST /workflows` 接收建箱字段、`predicate` 和 5–300 秒总时限；`GET /tasks`、`GET /tasks/TASK_ID` 和 `DELETE /tasks/TASK_ID` 负责查询与取消。探测与工作流均需要工作区凭据。

`POST /webhooks` 用幂等键创建签名事件订阅；`GET /webhooks` 查询，`DELETE /webhooks/WEBHOOK_ID` 停用，`GET /webhook-deliveries` 查看最近结果。订阅事件和签名校验见 [开发工具接入指南](agent.zh-CN.md)。

新增资源列表目前最多返回 100 条；来源指标单独注明窗口与样本上限。现有邮箱/邮件接口的分页契约保持不变。MCP 通过同一 HTTP API 工作，不赋予额外权限。
