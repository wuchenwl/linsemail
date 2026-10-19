# 邮箱前缀

创建邮箱时可选填 `address_prefix`。它与页面上的邮箱名称 `name` 无关。

```http
POST /api/v1/mailboxes
Authorization: Bearer <TOKEN>
Idempotency-Key: <UNIQUE_KEY>
Content-Type: application/json

{"provider_id":"grabmail","address_prefix":"my-test-","ttl_seconds":3600}
```

返回 202 后，按 operation/resource_id 查询创建结果。例如 `my-test-<16位随机十六进制字符>@grabmail.io`。
省略字段、空字符串、null 或纯空格均随机生成，不再自动添加 `ls-tasklet-`。
填写 `ls-tasklet-` 仍可获得原来的前缀格式。前缀首位为英文字母或数字，后续允许英文字母、数字和连字符，最长48位；前后空白自动去除。

从 `GET /api/v1/providers` 的 `capabilities.custom_prefix` 查询支持情况。
目前支持 GrabMail、TempMail.plus、BYOM、Maildrop、InboxKitten（5家）；其他来源传入非空前缀会返回422 `custom_prefix_unsupported`，不会静默忽略。
随机后缀始终保留，减少同名前缀撞箱；公开邮箱仍应避免接收敏感内容。
创建参数随已有加密会话存储，worker重开后仍可使用；无需更改数据库结构。
已有邮箱地址和读信身份保持不变，不会因新建规则而重新生成。
