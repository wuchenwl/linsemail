# Reproducible screenshots · 可复现截图

These PNGs capture the actual Linsemail frontend with intercepted, synthetic API fixtures. No real mailbox session, provider credential, private message or access token is displayed. They demonstrate the interface, not delivery or uptime. Both README languages reference the corresponding locale’s images.

这些 PNG 来自真实前端渲染，API 使用拦截后的合成演示数据。截图不含真实邮箱会话、来源密钥、私人邮件或可用令牌，也不作为投递成功或服务在线率的证明。两种语言的介绍文档分别引用对应界面。

With the frontend built and served locally, install Playwright in a development environment and run:

```bash
python -m pip install playwright
python -m playwright install chromium
python scripts/screenshot-console.py
```

For an already installed Chromium-based browser, supply `--browser-path PATH`. The script renders welcome, inbox, wait tasks, providers, API, creation form and mobile reader in both languages; verifies switching, persistence and responsive layout; and writes a synthetic-data screenshot manifest. The browser does not send live creation requests to any provider.
