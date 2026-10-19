# Contributing · 参与贡献

[GitHub repository / 项目仓库](https://github.com/wuchenwl/linsemail) · [Issues / 问题反馈](https://github.com/wuchenwl/linsemail/issues) · [Pull requests / 贡献代码](https://github.com/wuchenwl/linsemail/pulls)

English and Chinese contributions are welcome. Start with a focused issue describing the behavior you want to change and how it can be verified.

欢迎使用中文或英文参与。请先说明具体问题、预期行为与验证方式；一次 PR 聚焦一个完整改进。

## Development

1. Follow the [quick start](README.en.md#quick-start), then stop the console before changing service configuration.
2. Run `npm ci --prefix frontend` and install `backend/requirements.lock.txt` in a virtual environment.
3. Start the API and worker; run `npm run dev --prefix frontend` for the Vite development server.
4. Run `python -m pytest backend/tests tests -q`, `node scripts/test-i18n.cjs` and `npm run build --prefix frontend`.

Integration tests need PostgreSQL and create disposable schemas. Use a dedicated development database whose user may create and drop schemas. Tests use controlled upstream responses; do not turn them into unbounded live-provider probes.

集成测试需要 PostgreSQL，并创建、删除隔离 schema。请使用专用开发数据库。协议测试通过受控响应复验契约，不应在 CI 中反复调用外部服务。

## Provider adapters

- Describe the official documentation or public protocol behind the implementation.
- Cover create, list, non-empty body retrieval, original-mailbox reconnection, pagination and upstream errors.
- Preserve mailbox identity. A body ID must first be observed in that mailbox’s list.
- Mark credentials, deployment requirements and shared infrastructure accurately. A new domain or brand alias is not a new receiving infrastructure.
- Keep implementation, reproducible tests and any live-test evidence separate. An adapter is not proof of delivery or third-party signup compatibility.

新来源请同时提供完整取件契约、非空正文与异常边界测试；明确密钥需求、部署条件和共用后台。不要用域名数量替代适配器数量，也不要把接口成功等同于真实投递成功。

## Interface and documentation

Keep the monochrome visual system, keyboard access and responsive three-pane layout. Add interface copy to `frontend/src/i18n.ts` and update both README languages when behavior changes. Leave addresses, user labels and message bodies in their original language. Screenshot instructions live in [docs/screenshots/README.md](docs/screenshots/README.md).

界面新增文案须同步中英文词典；用户标签、邮箱地址和邮件正文不自动翻译。行为变更请同步两份介绍文档，并按截图说明更新演示图。

## Pull requests

Explain the problem, resulting behavior, relevant verification and remaining limits. Do not commit runtime data, provider keys, tokens, cookies, inbox messages, local database files or downloaded frontend bundles. Keep screenshots synthetic and reproducible. Contributions are provided under the project’s [MIT license](LICENSE).
