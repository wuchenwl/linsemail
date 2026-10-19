import { useSyncExternalStore } from 'react';

export type Locale = 'zh-CN' | 'en';
const storageKey = 'linsemail-language';
function initialLocale(): Locale {
  try { return localStorage.getItem(storageKey) === 'en' ? 'en' : 'zh-CN'; }
  catch { return 'zh-CN'; }
}
let locale: Locale = initialLocale();
const listeners = new Set<() => void>();
const subscribe = (listener: () => void) => { listeners.add(listener); return () => listeners.delete(listener); };
export const getLocale = () => locale;
function updateDocument() {
  if (typeof document !== 'undefined') {
    document.documentElement.lang = locale;
    document.title = locale === 'en' ? 'Linsemail · Mailbox workspace' : 'Linsemail · 收件工作台';
  }
}
export function setLocale(next: Locale) {
  if (next !== 'en' && next !== 'zh-CN') return;
  locale = next;
  try { localStorage.setItem(storageKey, next); } catch { /* Session-only preference when storage is unavailable. */ }
  updateDocument();
  listeners.forEach(listener => listener());
}
export function useLocale() { return useSyncExternalStore(subscribe, getLocale, getLocale); }
if (typeof window !== 'undefined') window.addEventListener('storage', event => {
  if (event.key === storageKey) {
    locale = event.newValue === 'en' ? 'en' : 'zh-CN';
    updateDocument(); listeners.forEach(listener => listener());
  }
});
updateDocument();

export const english: Record<string, string> = {
  '正文包含': 'Body contains',
  "自动化": "Automation",
  "自动选择（智能路由）": "Automatic (smart routing)",
  "根据近期健康度与延迟选择可用的外部来源；无样本时按冷启动策略选择。": "Choose an eligible external provider using recent health and latency. New providers use a cold-start policy.",
  "请求成功率": "Request success",
  "收件成功率": "Delivery success",
  "延迟 P95": "P95 latency",
  "近 24 小时观测；无样本显示横线。收件率按已结束等待任务统计。": "Observations over 24 hours. A dash means no samples; delivery rate uses completed wait tasks.",
  "来源探测已排队": "Health probe queued",
  "测试健康度": "Probe health",
  "排队中": "Queued",
  "执行中": "Running",
  "已完成": "Completed",
  "失败": "Failed",
  "等待重试": "Retry pending",
  "从收件，到可恢复的自动化。": "From inbox to recoverable automation.",
  "一次请求完成创建与匹配。关闭页面后，任务仍由 Worker 持续执行。": "One request creates a mailbox and waits for a match. The worker continues after the page closes.",
  "新建组合任务": "New workflow",
  "等待时限": "Timeout",
  "启动工作流": "Start workflow",
  "事件推送": "Event delivery",
  "签名事件支持持久重试。接收端按事件 ID 去重，再通过 API 读取邮件。": "Signed events support persistent retries. Deduplicate by event ID, then read messages through the API.",
  "接收地址": "Receiver URL",
  "订阅事件": "Subscribe to event",
  "添加 Webhook": "Add webhook",
  "请保存签名密钥，列表中不会再次展示。": "Save this signing secret. It is not shown in the subscription list.",
  "复制失败": "Copy failed",
  "移除 Webhook": "Remove webhook",
  "默认仅允许公网 HTTPS。本机回调须显式启用回环测试配置。": "Public HTTPS only by default. Local callbacks require the explicit loopback test setting.",
  "组合任务与来源探测": "Workflows and health probes",
  "还没有组合任务。创建邮箱并等待邮件，从这里开始。": "No workflows yet. Start here to create a mailbox and wait for a message.",
  "MCP 工具接入": "MCP tool integration",
  "在项目目录运行 MCP 服务，复用本机工作区鉴权和同一套 API。": "Run the MCP server in the project directory. It shares local workspace authorization and the same API.",
  "支持来源查询、智能建箱、邮件读取、组合任务与任务取消。": "Query providers, route mailboxes, read messages, run workflows and cancel tasks.",
  "查看接入指南": "Read the integration guide",
  "最近投递": "Recent deliveries",
  "订阅事件发生后，这里会显示签名推送与重试结果。": "Signed deliveries and retry results appear here when subscribed events occur.",
  '接收中': 'Receiving', '创建中': 'Creating', '创建失败': 'Creation failed', '已到期': 'Expired',
  '删除中': 'Removing', '已删除': 'Removed', '等待来信': 'Waiting', '已匹配': 'Matched',
  '已取消': 'Cancelled', '已超时': 'Timed out', '邮箱已到期': 'Mailbox expired',
  '本机接收': 'Local capture', '会话邮箱': 'Session mailbox', '令牌邮箱': 'Token mailbox',
  '公开邮箱': 'Public mailbox', '公开地址 · 会话': 'Public · session', '公开地址 · 访问令牌': 'Public · visitor token',
  'API 密钥': 'API key', '会话令牌': 'Session token', '密钥 · 命名空间': 'Key · namespace', '待确认': 'Unconfirmed',
  '已接入': 'Integrated', '待配置': 'Needs setup', '待适配': 'Not integrated', '需自建部署': 'Self-hosting required', '转发别名': 'Forwarding alias',
  '收件工作台': 'Inbox', '取件任务': 'Wait tasks', '邮箱来源': 'Providers', '关闭': 'Close',
  '一个新的收件地址。': 'A fresh address.', '选择来源、设置保留时间，其余交给工作台。': 'Choose a provider, set a lifetime, and let the workspace handle the rest.',
  '邮箱名称': 'Mailbox name', '便于识别，可选': 'Optional display name', '例如：产品测试': 'e.g. Product testing',
  '正在加载来源…': 'Loading providers…', '邮箱前缀': 'Address prefix', '可选，留空随机生成': 'Optional; blank means random',
  '例如：my-test-': 'e.g. my-test-', '此来源使用随机地址': 'This provider assigns a random address',
  '支持字母、数字和连字符，首位须为字母或数字。填写后追加 16 位随机字符；留空则全部随机。': 'Use letters, digits and hyphens; start with a letter or digit. We append 16 random characters. Leave blank for a fully random address.',
  '该来源暂不支持自定义前缀，地址由来源随机分配。': 'Custom prefixes are unavailable for this provider. The provider assigns the address.',
  '保留时间': 'Lifetime', '15 分钟': '15 minutes', '1 小时': '1 hour', '6 小时': '6 hours', '24 小时': '24 hours',
  '到期后自动停止取件，本地邮件在 15 分钟宽限期后清理。第三方来源的可用期可能更短。': 'Pickup stops at expiry. Local messages are cleared after a 15-minute grace period. Upstream mailboxes may expire sooner.',
  '取消': 'Cancel', '创建邮箱': 'Create mailbox',
  '连接你的服务账户。密钥加密保存在本机，保存后不再展示。': 'Connect your provider account. Credentials are encrypted locally and are not displayed after saving.',
  '查看官方接口文档': 'Official API documentation',
  '保存配置后即可尝试创建邮箱。创建和取件结果会更新来源状态。移除配置会停止新建；已有邮箱保留创建时的会话。': 'Save credentials to try mailbox creation. Creation and pickup update provider status. Removing credentials stops new mailboxes; existing sessions are retained.',
  '移除配置': 'Remove credentials', '保存配置': 'Save credentials', '等一封符合条件的邮件。': 'Wait for the right message.',
  '当前邮箱': 'Current mailbox', '发件域名': 'Sender domain', '可选，精确匹配': 'Optional, exact match',
  '主题包含': 'Subject contains', '可选': 'Optional', '例如：验证码': 'e.g. verification', '最长等待': 'Timeout',
  '30 秒': '30 seconds', '2 分钟': '2 minutes', '5 分钟': '5 minutes',
  '从任务创建时的收件序号开始匹配新邮件；已有邮件不会误触发任务。': 'Match messages arriving after this task is created. Existing messages cannot trigger it.',
  '开始等待': 'Start waiting', '连接工作区…': 'Connecting to workspace…', '切换配色': 'Toggle theme',
  '本地工作区 · 私有会话': 'Local workspace · Private session', '来信，': 'Your inbox.', '尽在掌握。': 'In focus.',
  '临时邮箱、邮件与取件任务。': 'Temporary mailboxes, messages and wait tasks.', '一个安静、清晰的工作台。': 'One quiet, focused workspace.',
  '进入本地工作区': 'Enter workspace', '仅限本机访问。邮箱内容不会写入浏览器本地存储。': 'Local access only. Mail content is never saved to browser local storage.',
  '操作未成功完成，请查看邮箱状态。': 'The operation did not complete. Check the mailbox status.',
  '操作仍在队列中，稍后会自动更新。': 'The operation is still queued. Its status will update automatically.',
  '已复制到剪贴板': 'Copied to clipboard', '剪贴板不可用，请手动选择并复制。': 'Clipboard is unavailable. Select and copy the text manually.',
  '# 等待一封新邮件': '# Wait for a new message', 'Linsemail 首页': 'Linsemail home', '主导航': 'Main navigation',
  '切换明暗配色': 'Toggle light and dark theme', '退出工作区': 'Sign out', '统一收件与接口集成': 'Unified inbox and integrations',
  '管理每一个地址，接住每一封来信。': 'Every address organized. Every message in reach.',
  '设好条件，让来信主动找到你。': 'Set your conditions. Let the right message find you.',
  '不同的来源，同一种清晰的收件体验。': 'Different providers. One consistent inbox.',
  '从创建邮箱，到等待来信，全部通过 API 完成。': 'From creating an address to waiting for mail. All through one API.',
  '取件服务运行中': 'Worker online', '连接服务中': 'Connecting', '取件服务离线': 'Worker offline', '新建邮箱': 'New mailbox',
  '个使用中': ' active', '封本地邮件': ' local messages', '实时事件已连接': 'Live events connected', '定时同步中': 'Periodic sync',
  '三栏收件工作台': 'Three-pane inbox', '我的邮箱': 'Mailboxes', '添加邮箱': 'Add mailbox', '搜索邮箱': 'Search mailboxes',
  '搜索邮箱…': 'Search mailboxes…', '正在读取邮箱': 'Loading mailboxes', '连接你的工作区…': 'Connecting to your workspace…',
  '没有匹配的邮箱': 'No matching mailboxes', '从一个地址开始': 'Start with an address', '试试其他关键词。': 'Try a different search.',
  '创建一个临时邮箱，开始接收邮件。': 'Create a temporary mailbox to start receiving messages.', '正在分配地址…': 'Assigning an address…',
  '地址不可用': 'Address unavailable', '加载更多邮箱': 'Load more mailboxes', '按时清理，轻装收件。': 'A lighter inbox, by design.',
  '邮件列表': 'Message list', '返回邮箱': 'Back to mailboxes', '收件箱': 'Inbox', '选择左侧邮箱': 'Select a mailbox',
  '复制邮箱地址': 'Copy email address', '全部邮件': 'All messages', '搜索已加载邮件': 'Search loaded messages', '搜索': 'Search',
  '邮箱就绪，来信可期': 'Ready for incoming mail', '选择或创建一个邮箱。': 'Select or create a mailbox.', '此邮箱暂不可用': 'Mailbox unavailable',
  '邮箱可用后，邮件将在这里出现。': 'Messages appear here once the mailbox is ready.', '读取收件箱…': 'Loading inbox…',
  '正在加载本地邮件。': 'Loading locally stored messages.', '暂时无法读取': 'Unable to load messages', '没有匹配的邮件': 'No matching messages',
  '等待第一封来信': 'Waiting for the first message', '仅搜索当前已加载的主题和发件人。': 'Search covers loaded subjects and senders only.',
  '新邮件会自动出现在这里，无需反复刷新。': 'New messages appear automatically. No repeated refresh needed.',
  '未知发件人': 'Unknown sender', '(无主题)': '(No subject)', '来源未提供发件人': 'Sender not provided',
  '正文已就绪': 'Body ready', '等待正文': 'Awaiting body', '加载更早的邮件': 'Load earlier messages',
  '取件退避中': 'Pickup backing off', '取件已暂停': 'Pickup paused', '上次同步': 'Last sync ', '立即同步': 'Sync now', '收件箱已同步': 'Inbox synced',
  '邮件阅读器': 'Message reader', '邮件详情': 'Message details', '创建等待任务': 'Create wait task', '移除当前邮箱': 'Remove mailbox',
  '给重要来信，留一个位置。': 'Make room for the next message.', '选中一封邮件，阅读正文与取件结果。': 'Select a message to read its content and extraction results.',
  '打开邮件…': 'Opening message…', '正在读取本地正文。': 'Loading the stored message body.', '邮件暂不可读': 'Message unavailable',
  '收件序号 #': 'Message sequence #', '发给': 'To ', '检测到多个候选，请核对正文': 'Multiple candidates; check the message',
  '从正文中提取': 'Extracted from message', '可追溯': 'Traceable', '验证码': 'Verification code', '验证链接候选': 'Verification link candidate',
  '正文字符': 'Body characters ', '复制验证码': 'Copy verification code', '复制链接': 'Copy link', '复制': 'Copy',
  '来源暂时只返回了邮件摘要。': 'The provider has returned only a message summary.', '获取正文': 'Fetch body',
  '纯文本安全阅读 · 外部图片与脚本不会加载': 'Plain-text reading · External images and scripts are not loaded', '· 本地收件': '· Captured locally ',
  '你的下一封来信，已经有了位置。': 'A place for your next message.', '等待新邮件': 'Wait for new mail', '来源统计': 'Provider summary',
  '外部来源 · 另有 1 个本机测试箱': 'External providers · plus local capture', '可尝试创建': 'Ready to try', '待适配 / 部署': 'Not integrated / self-hosted',
  '筛选来源': 'Filter providers', '全部': 'All', '搜索邮箱来源': 'Search providers', '名称、域名或来源 ID': 'Name, domain or provider ID',
  '显示': 'Showing ', '个来源 · 已接入表示具备适配器；最近实测单独显示。': ' providers · Integrated means an adapter exists; live results are shown separately.',
  '来源加载失败': 'Unable to load providers', '没有匹配的来源': 'No matching providers', '试试其他名称，或切换筛选条件。': 'Try another name or filter.',
  '本机': 'Local', '访问方式': 'Access', '来源账户': 'Provider account', '取件方式': 'Pickup', 'SMTP → 本地入库': 'SMTP → local storage',
  'Worker 自动轮询': 'Worker polling', '待接通': 'Not connected', '最近实测': 'Latest check', '通过 ·': 'Passed ·', '失败 ·': 'Failed ·',
  '尚未实测': 'Not live-tested', '最近错误：': 'Latest error: ', '仅接收发往本机的测试邮件。': 'Captures messages sent to the local SMTP listener only.',
  '来源可用性以实时观测为准。': 'Provider availability is based on live observations.',
  '官方网站': 'Website', '修改配置': 'Edit credentials', '配置来源': 'Configure provider', '新邮件，自动匹配。': 'New messages. Automatically matched.',
  '按发件域名和主题等待，命中后返回明确的邮件 ID。': 'Wait by sender domain and subject. Get a message ID when a match arrives.',
  '新建任务': 'New task', '任务读取失败': 'Unable to load tasks', '还没有等待任务': 'No wait tasks yet',
  '选中一个使用中的邮箱，开始等待下一封来信。': 'Select an active mailbox and start waiting for the next message.',
  '任意新邮件': 'Any new message', '从序号': 'After sequence ', '开始': ' onward', '截止': 'Due ', '查看邮件': 'View message',
  '展示最近 100 个任务。任务持久保存在服务端，关闭页面不会中断等待；到期与取消均有明确状态。': 'Showing the latest 100 tasks. Tasks persist on the server and keep running when this page closes. Expiry and cancellation have explicit states.',
  '让收件成为': 'Make incoming mail', '工作流的一部分。': 'part of your workflow.',
  '统一 JSON 接口，持久化等待任务，实时事件。浏览器与自动化共享同一份邮件状态。': 'One JSON API, persistent wait tasks and live events. Your browser and automation share the same mailbox state.',
  '打开接口文档': 'Open API docs', '邮箱访问令牌': 'Mailbox access token',
  '仅授权当前选中的邮箱，1 小时后失效。令牌仅在签发后展示，请妥善保存。': 'Access is limited to the selected mailbox and expires after one hour. The token is shown only once.',
  '请先选择一个使用中的邮箱': 'Select an active mailbox first', '复制访问令牌': 'Copy access token',
  '生成当前邮箱令牌': 'Generate mailbox token', '复制示例': 'Copy example', '写入请求支持幂等键': 'Idempotency keys for writes',
  '邮箱令牌按箱授权': 'Tokens scoped to one mailbox', '错误使用 application/problem+json': 'Errors use application/problem+json',
  '本地工作区': 'Local workspace', '本机测试箱': 'Local capture', '关闭提示': 'Dismiss notification',
  '来源配置已加密保存': 'Provider credentials saved and encrypted', '已移除来源配置': 'Provider credentials removed',
  '新邮箱已就绪': 'Your mailbox is ready', '等待任务已创建': 'Wait task created', '移除这个邮箱？': 'Remove this mailbox?',
  '停止取件并清理本地邮件，同时撤销该邮箱的访问令牌。第三方来源上的副本仍由其保留策略决定。': 'Stop pickup, clear local messages and revoke mailbox tokens. Upstream copies follow the provider’s retention policy.',
  '保留邮箱': 'Keep mailbox', '邮箱已移除': 'Mailbox removed', '确认移除': 'Remove mailbox',
  '服务器 ID': 'Server ID', '命名空间': 'Namespace', '收件域名': 'Receiving domain', '团队 ID': 'Team ID',
  '收件文件夹 ID': 'Inbox folder ID', '部署地址': 'Deployment URL', '站点访问密码（可选）': 'Site password (optional)',
  '代理账户 ID': 'Agent account ID', '已分配域名 ID': 'Assigned domain ID',
  '填写该服务的密钥后，可通过统一 API 创建和取件。': 'Configure your provider credentials to create mailboxes and receive messages through the unified API.',
  '已接入创建、列取和正文读取；可用性以最近实测为准。': 'Creation, message listing and body retrieval are integrated. Availability depends on the latest live check.',
  '该网页使用 Mail.tm 收件接口，与 Mail.tm 共用收件能力。': 'This website uses the Mail.tm API and shares its receiving infrastructure.',
  '已接入 MailCare 托管收件 API；配置密钥、团队 ID 和已分配收件域名后使用。': 'MailCare hosted inbox API. Configure an API key, team ID and assigned receiving domain.',
  '已接入 9Mail 私测版 API；需运营方签发的专属 Bearer Token，目前没有公开自助密钥。': '9Mail private-beta API. Requires a provider-issued Bearer token; public self-service keys are unavailable.',
  '已接入 Hissline（mailhook.co）；需账户密钥、代理账户 ID 和已分配域名 ID。': 'Hissline (mailhook.co) integration. Requires an account key, agent account ID and assigned domain ID.',
  '已实现自部署收件接口；需填写自己的部署地址与收件域名，尚未进行部署实测。': 'Self-hosted receiving adapter. Configure your own deployment URL and domain; deployment has not been live-tested.',
  '该服务提供转发别名，需要绑定收件邮箱，不能直接作为临时收件箱。': 'Forwarding aliases require a destination mailbox and cannot serve as standalone inboxes.',
  '需要部署自己的邮件服务及域名，再提供收件接口。': 'Deploy your own mail service and domain to provide a receiving endpoint.',
  '尚未接通取件协议。': 'The receiving protocol is not integrated.',
  '尚未接通取件协议。该服务还需要账户或 API 密钥。': 'The receiving protocol is not integrated. A provider account or API key is also required.',
  '通过本机 SMTP 1025 端口接收测试邮件，不接收公网来信。': 'Capture test messages on local SMTP port 1025. This is not a public mail server.',
  '尚未取件': 'Not synced yet',
};

/** Translate interface copy only. Addresses, message bodies and user labels remain intact. */
export function t(message?: string | null): string {
  if (!message) return '';
  if (locale === 'zh-CN') return message;
  const key = message.trim();
  const translated = english[key];
  return translated === undefined ? message : message.slice(0, message.indexOf(key)) + translated + message.slice(message.indexOf(key) + key.length);
}

export function problemText(code: string, detail?: string): string {
  if (locale === 'zh-CN') return detail || '请求失败 (' + code + ')';
  const translated = t(detail);
  if (translated && !/[\u3400-\u9fff]/.test(translated)) return translated;
  const messages: Record<string, string> = {
    custom_prefix_unsupported: 'This provider does not support custom prefixes. Leave the prefix blank.',
    validation_failed: 'Some request fields are invalid. Check the input and try again.',
    idempotency_conflict: 'This idempotency key was already used for a different request.',
    workspace_limit: 'This workspace already has 100 active mailboxes.',
    mailbox_expired: 'This mailbox has expired.', csrf_required: 'Reload the page to refresh your session.',
    provider_unavailable: 'The provider is unavailable. Your original mailbox has been preserved.',
    request_failed: 'The request could not be completed.',
  };
  return messages[code] || 'Operation could not be completed (' + code + ').';
}
