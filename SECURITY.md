# Security policy · 安全策略

Linsemail currently targets a local workstation. The console binds to loopback and checks request host, browser origin and session CSRF tokens. Provider credentials and mailbox session blobs are encrypted at rest; the encryption key and database both remain in the runtime directory. Anyone able to read that directory may obtain both, so filesystem access is part of the trust boundary.

当前版本面向本机工作站。服务默认监听回环地址，并校验 Host、浏览器来源与会话 CSRF。来源凭证和邮箱会话加密保存，但密钥与数据库均位于运行目录；本机文件访问权限属于安全边界。

## Reporting a vulnerability

Use the repository’s **Security → Report a vulnerability** feature if enabled. Do not put credentials, inbox contents, exploit details or working account tokens in a public issue. If private reporting is unavailable, open a minimal issue requesting a private contact without disclosing the vulnerability. Maintainers should enable GitHub private vulnerability reporting before public release.

优先使用仓库 **Security → Report a vulnerability** 私密报告入口。若入口未开启，请仅提交请求私密联系的简短 issue，不公开漏洞细节、邮箱正文或可用凭证。发布者应在开源时启用 GitHub 私密漏洞报告。

Include the affected version, reproducible steps using synthetic data, impact and a suggested fix if available. This project has no promised response-time SLA. Security fixes initially target the latest release.

请提供受影响版本、使用合成数据的复现步骤与影响范围。项目不承诺响应 SLA；安全修复首先面向最新版本。

## Operational boundaries

- Mailbox creation and deletion are local state transitions; upstream retention is controlled by each provider.
- Temporary public inboxes do not imply exclusive ownership of an address.
- The reader displays plain text and does not load remote message scripts or images.
- Provider URLs and self-hosted deployments must be controlled by the operator. The local mode is not a hardened multi-tenant public hosting configuration.
- Never publish `.runtime/`, `.env`, `*.local.json`, `*.local.cookies`, research captures or source archives containing them.
