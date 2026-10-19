import { AutomationPanel } from './AutomationPanel';
import { setLocale, t, useLocale, type Locale } from './i18n';
import { useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { useInfiniteQuery, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  ArrowDownLeft, ArrowLeft, ArrowRight, Check, ChevronDown, ChevronRight, Circle,
  Clock3, Code2, Copy, ExternalLink, Github, Inbox, Layers3, LoaderCircle, LogOut,
  Mail, Moon, Plus, Radio, RefreshCw, Search, ShieldCheck, Sun, Terminal, Trash2, X,
} from 'lucide-react';
import {
  api, ApiError, formatTime, remaining, setCsrf, write,
  type Detail, type Mailbox, type Message, type Operation, type Page, type Provider, type ProviderPage, type Session, type WaitJob,
} from './api';

const stateNames: Record<string, string> = {
  active: '接收中', creating: '创建中', failed: '创建失败', expired: '已到期', deleting: '删除中', deleted: '已删除',
  waiting: '等待来信', matched: '已匹配', cancelled: '已取消', timed_out: '已超时', mailbox_expired: '邮箱已到期',
};
const accessNames: Record<string, string> = {
  owned_domain: '本机接收', session_cookie: '会话邮箱', mailbox_token: '令牌邮箱', public_address: '公开邮箱',
  public_address_with_session: '公开地址 · 会话', session_cookie_and_mailbox_id: '会话邮箱',
  public_address_with_visitor_token: '公开地址 · 访问令牌', api_key: 'API 密钥', api_token: '会话令牌',
  bearer_key: 'API 密钥', api_key_namespace: '密钥 · 命名空间', unknown: '待确认',
};
const sourceStates: Record<string, string> = {
  enabled: '已接入', credential_required: '待配置', adapter_required: '待适配',
  deployment_required: '需自建部署', alias_only: '转发别名',
};
type View = 'inbox' | 'waits' | 'providers' | 'automation' | 'api';
const tabs: { id: View; label: string; icon: typeof Inbox }[] = [
  { id: 'inbox', label: '收件工作台', icon: Inbox }, { id: 'waits', label: '取件任务', icon: Clock3 },
  { id: 'automation', label: '自动化', icon: Radio },
  { id: 'providers', label: '邮箱来源', icon: Layers3 }, { id: 'api', label: 'API', icon: Code2 },
];

const PROJECT_REPOSITORY = 'https://github.com/wuchenwl/linsemail';

function RepositoryLink({ credit = false }: { credit?: boolean }) {
  return <a className={credit ? 'repository-credit' : 'icon-button'} href={PROJECT_REPOSITORY}
    target="_blank" rel="noopener noreferrer" title="GitHub · wuchenwl/linsemail" aria-label="GitHub · wuchenwl/linsemail">
    <Github size={credit ? 13 : 18} aria-hidden="true" />{credit && <span>wuchenwl/linsemail</span>}
  </a>;
}

function LanguageSwitch() {
  const locale = useLocale();
  return <label className="language-switch"><span className="sr-only">Language / 语言</span>
    <select aria-label="Language / 语言" value={locale} onChange={event => setLocale(event.target.value as Locale)}>
      <option value="zh-CN">简体中文</option><option value="en">English</option>
    </select><ChevronDown size={12} aria-hidden="true" /></label>;
}

function Glyph({ small = false }: { small?: boolean }) {
  return <span className={'glyph' + (small ? ' small' : '')}><Mail size={small ? 17 : 21} strokeWidth={1.6} /></span>;
}
function Empty({ icon: Icon = Inbox, title, text, children }: {
  icon?: typeof Inbox; title: string; text: string; children?: ReactNode;
}) {
  return <div className="empty"><span className="empty-icon"><Icon size={27} strokeWidth={1.3} /></span>
    <h3>{t(title)}</h3><p>{t(text)}</p>{children}</div>;
}
function Modal({ title, caption, children, close }: { title: string; caption: string; children: ReactNode; close: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { ref.current?.showModal(); return () => ref.current?.close(); }, []);
  return <dialog ref={ref} className="modal" onCancel={event => { event.preventDefault(); close(); }}>
    <button className="icon-button modal-close" onClick={close} aria-label={t("关闭")}><X size={19} /></button>
    <p className="eyebrow">LINSEMAIL / WORKSPACE</p><h2>{t(title)}</h2><p className="modal-caption">{t(caption)}</p>{children}
  </dialog>;
}
function CreateForm({ providers, initialProvider, pending, close, submit }: {
  providers: Provider[]; initialProvider: string; pending: boolean; close: () => void; submit: (data: object) => void;
}) {
  const [provider, setProvider] = useState(initialProvider);
  const [prefix, setPrefix] = useState('');
  function handle(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    submit({ ...(provider ? { provider_id: provider } : {}), name: data.get('name'), address_prefix: customPrefix ? prefix.trim() : '', ttl_seconds: Number(data.get('ttl')), labels: [] });
  }
  const source = providers.find(item => item.id === provider);
  const customPrefix = !provider || source?.capabilities.custom_prefix === 'supported';
  return <Modal title={t("一个新的收件地址。")} caption={t("选择来源、设置保留时间，其余交给工作台。")} close={close}>
    <form onSubmit={handle}>
      <label>{t("邮箱名称")}<span>{t("便于识别，可选")}</span><input name="name" maxLength={120} placeholder={t("例如：产品测试")} autoFocus /></label>
      <label>{t("邮箱来源")}<div className="select-wrap"><select value={provider} onChange={event => setProvider(event.target.value)}>
        <option value="">{t('自动选择（智能路由）')}</option>
        {providers.map(item => <option key={item.id} value={item.id} disabled={item.admission_state !== 'enabled'}>{t(item.name)}{item.admission_state !== 'enabled' ? ' · ' + t(sourceStates[item.admission_state]) : ''}</option>)}
      </select><ChevronDown size={16} /></div></label>
      <div className="source-note"><ShieldCheck size={17} /><p>{(!provider ? t('根据近期健康度与延迟选择可用的外部来源；无样本时按冷启动策略选择。') : t(source?.description)) || t("正在加载来源…")}</p></div>
      <label>{t("邮箱前缀")}<span>{t("可选，留空随机生成")}</span><input name="address_prefix" value={prefix}
        onChange={event => setPrefix(event.target.value)} maxLength={48} pattern="[A-Za-z0-9]([A-Za-z0-9]|-){0,47}"
        disabled={!customPrefix} placeholder={customPrefix ? t("例如：my-test-") : t("此来源使用随机地址")} autoCapitalize="none" spellCheck={false}
        aria-describedby="prefix-help" /></label>
      <p className="fine-print" id="prefix-help">{customPrefix
        ? t("支持字母、数字和连字符，首位须为字母或数字。填写后追加 16 位随机字符；留空则全部随机。")
        : t("该来源暂不支持自定义前缀，地址由来源随机分配。")}</p>
      <label>{t("保留时间")}<div className="select-wrap"><select name="ttl" defaultValue="3600">
        <option value="900">{t("15 分钟")}</option><option value="3600">{t("1 小时")}</option><option value="21600">{t("6 小时")}</option><option value="86400">{t("24 小时")}</option>
      </select><ChevronDown size={16} /></div></label>
      <p className="fine-print">{t("到期后自动停止取件，本地邮件在 15 分钟宽限期后清理。第三方来源的可用期可能更短。")}</p>
      <div className="modal-actions"><button type="button" className="button secondary" onClick={close} disabled={pending}>{t("取消")}</button>
        <button className="button primary" disabled={pending || (!!provider && source?.admission_state !== 'enabled')}>{pending ? <LoaderCircle size={16} className="spin" /> : <Plus size={16} />}{t("创建邮箱")}</button></div>
    </form>
  </Modal>;
}
function ProviderSettings({ source, pending, close, save, clear }: {
  source: Provider; pending: boolean; close: () => void;
  save: (credentials: Record<string, string>) => void; clear: () => void;
}) {
  function handle(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    save(Object.fromEntries(source.credential_fields.map(field => [field.name, String(data.get(field.name) || '').trim()]).filter(([, value]) => value)));
  }
  return <Modal title={t(source.name)} caption={t("连接你的服务账户。密钥加密保存在本机，保存后不再展示。")} close={close}>
    <form onSubmit={handle} autoComplete="off">
      {source.credential_fields.map((field, index) => <label key={field.name}>{t(field.label)}<input name={field.name}
        type={field.secret ? 'password' : 'text'} required={field.required !== false} autoFocus={index === 0} maxLength={4096} autoComplete="off" spellCheck={false} /></label>)}
      {source.documentation[0] && <a className="text-button" href={source.documentation[0]} target="_blank" rel="noreferrer">{t("查看官方接口文档")}<ExternalLink size={13} /></a>}
      <p className="fine-print">{t("保存配置后即可尝试创建邮箱。创建和取件结果会更新来源状态。移除配置会停止新建；已有邮箱保留创建时的会话。")}</p>
      <div className="modal-actions">{source.configured && <button type="button" className="text-button" disabled={pending} onClick={clear}>{t("移除配置")}</button>}
        <button type="button" className="button secondary" onClick={close} disabled={pending}>{t("取消")}</button>
        <button className="button primary" disabled={pending}>{pending && <LoaderCircle size={15} className="spin" />}{t("保存配置")}</button></div>
    </form>
  </Modal>;
}
function WaitForm({ box, pending, close, submit }: { box: Mailbox; pending: boolean; close: () => void; submit: (data: object) => void }) {
  function handle(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const predicate: Record<string, string> = {};
    if (String(data.get('domain')).trim()) predicate.from_domain = String(data.get('domain')).trim();
    if (String(data.get('subject')).trim()) predicate.subject_contains = String(data.get('subject')).trim();
    submit({ timeout_seconds: Number(data.get('timeout')), predicate });
  }
  return <Modal title={t("等一封符合条件的邮件。")} caption={box.address || t("当前邮箱")} close={close}>
    <form onSubmit={handle}>
      <label>{t("发件域名")}<span>{t("可选，精确匹配")}</span><input name="domain" placeholder="example.com" autoFocus maxLength={253} /></label>
      <label>{t("主题包含")}<span>{t("可选")}</span><input name="subject" placeholder={t("例如：验证码")} maxLength={200} /></label>
      <label>{t("最长等待")}<div className="select-wrap"><select name="timeout" defaultValue="120">
        <option value="30">{t("30 秒")}</option><option value="120">{t("2 分钟")}</option><option value="300">{t("5 分钟")}</option>
      </select><ChevronDown size={16} /></div></label>
      <p className="fine-print">{t("从任务创建时的收件序号开始匹配新邮件；已有邮件不会误触发任务。")}</p>
      <div className="modal-actions"><button type="button" className="button secondary" onClick={close}>{t("取消")}</button>
        <button className="button primary" disabled={pending}>{pending ? <LoaderCircle className="spin" size={16} /> : <Radio size={16} />}{t("开始等待")}</button></div>
    </form>
  </Modal>;
}

export default function App() {
  useLocale();
  const query = useQueryClient();
  const [theme, setTheme] = useState(() => localStorage.getItem('linsemail-theme') || 'light');
  const [error, setError] = useState('');
  const session = useQuery({
    queryKey: ['session'], queryFn: async () => {
      try { const value = await api<Session>('/session'); setCsrf(value.csrf_token); return value; }
      catch (err) { if (err instanceof ApiError && err.status === 401) return null; throw err; }
    }, retry: false,
  });
  useEffect(() => { document.documentElement.dataset.theme = theme; localStorage.setItem('linsemail-theme', theme); }, [theme]);
  async function login() {
    try {
      const value = await write<Session>('/session'); setCsrf(value.csrf_token);
      query.setQueryData(['session'], value); setError('');
    } catch (err) { setError((err as Error).message); }
  }
  const toggle = () => setTheme(value => value === 'light' ? 'dark' : 'light');
  if (session.isPending) return <main className="boot"><Glyph /><LoaderCircle size={20} className="spin" /><span>{t("连接工作区…")}</span></main>;
  if (!session.data) return <main className="login">
    <div className="login-top"><LanguageSwitch /><a className="brand" href="/"><Glyph /><span>linsemail</span></a><div className="login-tools"><RepositoryLink /><button className="icon-button" onClick={toggle} aria-label={t("切换配色")}>{theme === 'light' ? <Moon size={18} /> : <Sun size={18} />}</button></div></div>
    <div className="login-content"><span className="badge"><span className="status-dot" />{t("本地工作区 · 私有会话")}</span>
      <h1>{t("来信，")}<br />{t("尽在掌握。")}</h1><p>{t("临时邮箱、邮件与取件任务。")}<br />{t("一个安静、清晰的工作台。")}</p>
      <button className="button primary large" onClick={login}>{t("进入本地工作区")}<ArrowRight size={18} /></button>
      <p className="login-note">{error || session.error?.message || t("仅限本机访问。邮箱内容不会写入浏览器本地存储。")}</p></div>
    <div className="login-bottom"><span>LINSEMAIL / MAILBOX CONSOLE</span><RepositoryLink credit /></div>
  </main>;
  return <Console session={session.data} theme={theme} toggle={toggle} logout={async () => {
    await api('/session', { method: 'DELETE' }); setCsrf('');
    query.setQueryData(['session'], null);
    query.removeQueries({ predicate: item => item.queryKey[0] !== 'session' });
  }} />;
}

function Console({ session, theme, toggle, logout }: { session: Session; theme: string; toggle: () => void; logout: () => Promise<void> }) {
  const query = useQueryClient();
  const [view, setView] = useState<View>('inbox');
  const [boxId, setBoxId] = useState<string | null>(null);
  const [messageId, setMessageId] = useState<string | null>(null);
  const [mobile, setMobile] = useState<'boxes' | 'messages' | 'reader'>('boxes');
  const [boxSearch, setBoxSearch] = useState('');
  const [messageSearch, setMessageSearch] = useState('');
  const [modal, setModal] = useState<'create' | 'wait' | 'delete' | null>(null);
  const [pending, setPending] = useState('');
  const [notice, setNotice] = useState<{ text: string; error?: boolean } | null>(null);
  const [live, setLive] = useState(false);
  const [issuedToken, setIssuedToken] = useState('');
  const [sourceSearch, setSourceSearch] = useState('');
  const [sourceFilter, setSourceFilter] = useState('all');
  const [createProvider, setCreateProvider] = useState('');
  const [configureSource, setConfigureSource] = useState<Provider | null>(null);
  const [, tick] = useState(0);
  const boxesQuery = useInfiniteQuery({
    queryKey: ['mailboxes'], queryFn: ({ pageParam }) => api<Page<Mailbox>>('/mailboxes?limit=50' + (pageParam ? '&cursor=' + encodeURIComponent(pageParam) : '')),
    initialPageParam: null as string | null, getNextPageParam: page => page.next_cursor, refetchInterval: 6000,
  });
  const boxes = useMemo(() => boxesQuery.data?.pages.flatMap(page => page.items).filter(box => box.state !== 'deleted') || [], [boxesQuery.data]);
  const box = boxes.find(item => item.id === boxId);
  const active = box?.state === 'active' && new Date(box.expires_at).getTime() > Date.now();
  const providers = useQuery({ queryKey: ['providers'], queryFn: () => api<ProviderPage>('/providers'), refetchInterval: 15000 });
  const health = useQuery({ queryKey: ['health'], queryFn: () => api<{ worker_online: boolean; smtp_port: number }>('/health'), refetchInterval: 8000 });
  const messagesQuery = useInfiniteQuery({
    queryKey: ['messages', boxId],
    queryFn: ({ pageParam }) => api<Page<Message>>('/mailboxes/' + boxId + '/messages?limit=50' + (pageParam ? '&cursor=' + encodeURIComponent(pageParam) : '')),
    initialPageParam: null as string | null, getNextPageParam: page => page.next_cursor,
    enabled: !!boxId && !!active, refetchInterval: 12000,
  });
  const messages = useMemo(() => messagesQuery.data?.pages.flatMap(page => page.items) || [], [messagesQuery.data]);
  const detail = useQuery({
    queryKey: ['message', boxId, messageId], queryFn: () => api<Detail>('/mailboxes/' + boxId + '/messages/' + messageId),
    enabled: !!boxId && !!messageId && !!active,
  });
  const waits = useQuery({ queryKey: ['waits'], queryFn: () => api<Page<WaitJob>>('/waits?limit=100'), refetchInterval: 2000 });
  const waiting = waits.data?.items.filter(item => item.state === 'waiting') || [];
  function refresh() {
    for (const key of ['mailboxes', 'messages', 'message', 'waits']) void query.invalidateQueries({ queryKey: [key] });
  }
  useEffect(() => { const interval = setInterval(() => tick(value => value + 1), 30000); return () => clearInterval(interval); }, []);
  useEffect(() => { if (notice) { const timer = setTimeout(() => setNotice(null), notice.error ? 9000 : 3500); return () => clearTimeout(timer); } }, [notice]);
  useEffect(() => {
    const stream = new EventSource('/api/v1/events');
    stream.onopen = () => setLive(true);
    stream.onerror = () => setLive(false);
    const listener = () => refresh();
    for (const type of ['message.received', 'message.updated', 'operation.completed', 'wait.matched', 'wait.terminated', 'mailbox.expired']) stream.addEventListener(type, listener);
    return () => stream.close();
  }, []);
  useEffect(() => { if (!boxId && boxes.length) setBoxId(boxes[0].id); }, [boxes, boxId]);
  useEffect(() => { setMessageId(null); setMessageSearch(''); setIssuedToken(''); }, [boxId]);
  useEffect(() => { if (!messageId && messages.length) setMessageId(messages[0].id); }, [messages, messageId]);
  useEffect(() => { if (boxesQuery.error) setNotice({ text: boxesQuery.error.message, error: true }); }, [boxesQuery.error]);
  async function perform(name: string, action: () => Promise<unknown>, success?: string) {
    if (pending) return;
    setPending(name);
    try { await action(); if (success) setNotice({ text: success }); }
    catch (err) { setNotice({ text: (err as Error).message, error: true }); }
    finally { setPending(''); refresh(); }
  }
  async function finish(operation: Operation) {
    for (let attempt = 0; attempt < 90; attempt++) {
      const current = attempt === 0 ? operation : await api<Operation>('/operations/' + operation.id);
      if (current.state === 'succeeded') return current;
      if (['failed', 'uncertain'].includes(current.state)) throw new Error(current.error?.detail || t("操作未成功完成，请查看邮箱状态。"));
      await new Promise(resolve => setTimeout(resolve, 500));
    }
    throw new Error(t("操作仍在队列中，稍后会自动更新。"));
  }
  async function copy(value: string) {
    try { await navigator.clipboard.writeText(value); setNotice({ text: t("已复制到剪贴板") }); }
    catch { setNotice({ text: t("剪贴板不可用，请手动选择并复制。"), error: true }); }
  }
  function chooseBox(id: string) { setBoxId(id); setMobile('messages'); }
  const visibleBoxes = boxes.filter(item => (item.name + ' ' + item.address + ' ' + item.provider_id).toLowerCase().includes(boxSearch.toLowerCase()));
  const visibleMessages = messages.filter(item => ((item.subject || '') + ' ' + (item.from_address || '')).toLowerCase().includes(messageSearch.toLowerCase()));
  const providerName = (id: string) => t(providers.data?.items.find(item => item.id === id)?.name) || id;
  const visibleProviders = providers.data?.items.filter(item => {
    const matches = (item.name + ' ' + item.id + ' ' + (item.homepage || '')).toLowerCase().includes(sourceSearch.toLowerCase());
    return matches && (sourceFilter === 'all' || (sourceFilter === 'pending' ? !item.adapter_implemented : item.admission_state === sourceFilter));
  }) || [];
  const snippet = [
    "curl -H 'Authorization: Bearer MAILBOX_TOKEN' \\",
    "  'http://127.0.0.1:8100/api/v1/mailboxes/" + (box?.id || 'MAILBOX_ID') + "/messages'",
    '',
    t("# 等待一封新邮件"),
    "curl -X POST -H 'Authorization: Bearer MAILBOX_TOKEN' \\",
    "  -H 'Content-Type: application/json' -H 'Idempotency-Key: UNIQUE_KEY' \\",
    t("  -d '{\"timeout_seconds\":120,\"predicate\":{\"subject_contains\":\"verification\"}}' \\"),
    "  'http://127.0.0.1:8100/api/v1/mailboxes/" + (box?.id || 'MAILBOX_ID') + "/waits'",
  ].join('\n');
  return <div className="app">
    <header className="topbar">
      <a className="brand" href="/" aria-label={t("Linsemail 首页")}><Glyph /><span>linsemail</span><span className="brand-divider" /></a>
      <span className="workspace-name">{t(session.workspace_name)}<ChevronDown size={12} /></span>
      <nav aria-label={t("主导航")}>{tabs.map(({ id, label, icon: Icon }) => <button key={id} className={view === id ? 'active' : ''} onClick={() => setView(id)}>
        <Icon size={16} strokeWidth={1.6} />{t(label)}{id === 'waits' && waiting.length > 0 && <span className="nav-count">{waiting.length}</span>}
      </button>)}</nav>
      <div className="header-actions"><LanguageSwitch /><RepositoryLink /><span className="local-pill"><span className="status-dot" />LOCAL</span>
        <button className="icon-button" title={t("切换明暗配色")} aria-label={t("切换明暗配色")} onClick={toggle}>{theme === 'light' ? <Moon size={17} /> : <Sun size={17} />}</button>
        <button className="avatar" onClick={() => void perform('logout', logout)} title={t("退出工作区")} aria-label={t("退出工作区")}><LogOut size={16} /></button>
      </div>
    </header>
    <main className="main">
      <section className="page-heading">
        <div><p className="eyebrow">YOUR MAIL, IN FOCUS</p><h1>{view === 'inbox' ? t("收件工作台") : view === 'waits' ? t("取件任务") : view === 'providers' ? t("邮箱来源") : t("统一收件与接口集成")}<span>.</span></h1>
          <p className="heading-copy">{view === 'inbox' ? t("管理每一个地址，接住每一封来信。") : view === 'waits' ? t("设好条件，让来信主动找到你。") : view === 'providers' ? t("不同的来源，同一种清晰的收件体验。") : t("从创建邮箱，到等待来信，全部通过 API 完成。")}</p></div>
        <div className="heading-actions"><span className="worker-status"><span className={'status-dot' + (!health.data?.worker_online ? ' hollow' : '')} />{health.data?.worker_online ? t("取件服务运行中") : health.isPending ? t("连接服务中") : t("取件服务离线")}</span>
          <button className="button primary" onClick={() => setModal('create')}><Plus size={16} />{t("新建邮箱")}</button></div>
      </section>
      {view === 'inbox' && <>
        <div className="workbench-meta"><span><span className="status-dot" />{boxes.filter(item => item.state === 'active').length} {t("个使用中")}<span className="meta-sep">/</span>{boxes.reduce((sum, item) => sum + item.message_count, 0)} {t("封本地邮件")}</span><span>{live ? t("实时事件已连接") : t("定时同步中")}<Radio size={13} /></span></div>
        <section className={'workbench mobile-' + mobile} aria-label={t("三栏收件工作台")}>
          <aside className="mailbox-pane">
            <div className="pane-heading"><h2>{t("我的邮箱")}<span>{boxes.length.toString().padStart(2, '0')}</span></h2><button className="icon-button compact" onClick={() => setModal('create')} aria-label={t("添加邮箱")}><Plus size={17} /></button></div>
            <div className="search"><Search size={15} /><input aria-label={t("搜索邮箱")} value={boxSearch} onChange={event => setBoxSearch(event.target.value)} placeholder={t("搜索邮箱…")} /><span>⌕</span></div>
            <div className="mailbox-list">{boxesQuery.isPending ? <Empty title={t("正在读取邮箱")} text={t("连接你的工作区…")} /> : !visibleBoxes.length ? <Empty icon={Mail} title={boxSearch ? t("没有匹配的邮箱") : t("从一个地址开始")} text={boxSearch ? t("试试其他关键词。") : t("创建一个临时邮箱，开始接收邮件。")} /> : visibleBoxes.map(item => <button key={item.id} className={'mailbox-item ' + (boxId === item.id ? 'selected' : '')} onClick={() => chooseBox(item.id)}>
              <div className="mailbox-row"><span className="box-symbol"><Inbox size={17} strokeWidth={1.5} /></span><strong>{item.name || providerName(item.provider_id)}</strong><span className="message-count">{item.message_count}</span></div>
              <div className="mailbox-address">{item.address || (item.state === 'creating' ? t("正在分配地址…") : t("地址不可用"))}</div>
              <div className="mailbox-bottom"><span><span className={'status-dot' + (item.state !== 'active' ? ' hollow' : '')} />{t(stateNames[item.state]) || item.state}</span><span><Clock3 size={11} />{remaining(item.expires_at)}</span></div>
            </button>)}
              {boxesQuery.hasNextPage && <button className="text-button load-more" onClick={() => void boxesQuery.fetchNextPage()}>{t("加载更多邮箱")}</button>}
            </div>
            <div className="pane-foot"><ShieldCheck size={14} /><span>{t("按时清理，轻装收件。")}</span></div>
          </aside>
          <section className="message-pane" aria-label={t("邮件列表")}>
            <div className="pane-heading message-heading"><button className="icon-button mobile-back" onClick={() => setMobile('boxes')} aria-label={t("返回邮箱")}><ArrowLeft size={17} /></button>
              <div><h2>{box?.name || (box ? providerName(box.provider_id) : t("收件箱"))}</h2><span className="selected-address">{box?.address || t("选择左侧邮箱")}</span></div>
              <button className="icon-button compact" disabled={!box?.address} onClick={() => box?.address && void copy(box.address)} title={t("复制邮箱地址")} aria-label={t("复制邮箱地址")}><Copy size={15} /></button></div>
            <div className="message-toolbar"><span>{t("全部邮件")}<b>{box?.message_count || 0}</b></span><div className="search inline"><Search size={14} /><input aria-label={t("搜索已加载邮件")} value={messageSearch} onChange={event => setMessageSearch(event.target.value)} placeholder={t("搜索")} /></div></div>
            <div className="message-list">{!box ? <Empty title={t("邮箱就绪，来信可期")} text={t("选择或创建一个邮箱。")} /> : !active ? <Empty icon={Clock3} title={t(stateNames[box.state]) || t("此邮箱暂不可用")} text={box.sync.last_error?.detail || t("邮箱可用后，邮件将在这里出现。")} /> : messagesQuery.isPending ? <Empty title={t("读取收件箱…")} text={t("正在加载本地邮件。")} /> : messagesQuery.error ? <Empty title={t("暂时无法读取")} text={messagesQuery.error.message} /> : !visibleMessages.length ? <Empty icon={Mail} title={messageSearch ? t("没有匹配的邮件") : t("等待第一封来信")} text={messageSearch ? t("仅搜索当前已加载的主题和发件人。") : t("新邮件会自动出现在这里，无需反复刷新。")} /> : visibleMessages.map(item => <button key={item.id} className={'message-item ' + (messageId === item.id ? 'selected' : '')} onClick={() => { setMessageId(item.id); setMobile('reader'); }}>
              <div className="sender-row"><span className="sender-initial">{(item.from_domain || '?')[0].toUpperCase()}</span><strong>{item.from_domain || item.from_address || t("未知发件人")}</strong><time>{formatTime(item.received_at || item.first_seen_at)}</time></div>
              <h3>{item.subject || t("(无主题)")}</h3><p>{item.from_address || t("来源未提供发件人")}</p>
              <div className="message-tags"><span>{item.body_state === 'ready' ? t("正文已就绪") : t("等待正文")}</span><small>#{item.ingest_seq.toString().padStart(3, '0')}</small><ChevronRight size={13} /></div>
            </button>)}
              {messagesQuery.hasNextPage && <button className="text-button load-more" onClick={() => void messagesQuery.fetchNextPage()} disabled={messagesQuery.isFetchingNextPage}>{t("加载更早的邮件")}</button>}
            </div>
            <div className="pane-foot sync-foot"><span><span className={'status-dot' + (box?.sync.freshness !== 'fresh' ? ' hollow' : '')} />{box?.sync.state === 'backoff' ? t("取件退避中") : box?.sync.state === 'blocked' ? t("取件已暂停") : t("上次同步 ") + formatTime(box?.sync.last_success_at)}</span>
              <button className="icon-button compact" aria-label={t("立即同步")} title={t("立即同步")} disabled={!active || !!pending} onClick={() => void perform('sync', async () => { await finish(await write<Operation>('/mailboxes/' + boxId + '/syncs')); }, t("收件箱已同步"))}><RefreshCw size={14} className={pending === 'sync' ? 'spin' : ''} /></button></div>
          </section>
          <section className="reader-pane" aria-label={t("邮件阅读器")}>
            <div className="reader-toolbar"><button className="text-button mobile-back" onClick={() => setMobile('messages')}><ArrowLeft size={15} />{t("邮件列表")}</button><span className="reader-label"><Mail size={14} />{t("邮件详情")}</span><div>
              <button className="icon-button" title={t("创建等待任务")} aria-label={t("创建等待任务")} disabled={!active} onClick={() => setModal('wait')}><Clock3 size={17} /></button>
              <button className="icon-button" title={t("移除当前邮箱")} aria-label={t("移除当前邮箱")} disabled={!box || ['deleted', 'deleting'].includes(box.state)} onClick={() => setModal('delete')}><Trash2 size={16} /></button></div></div>
            <div className="reader-content">{!active || !messageId ? <Empty icon={ArrowDownLeft} title={t("给重要来信，留一个位置。")} text={t("选中一封邮件，阅读正文与取件结果。")} /> : detail.isPending ? <Empty title={t("打开邮件…")} text={t("正在读取本地正文。")} /> : detail.error ? <Empty title={t("邮件暂不可读")} text={detail.error.message} /> : detail.data && <>
              <div className="reader-kicker"><span className="badge subtle">INBOX</span><span>{t("收件序号 #")}{detail.data.ingest_seq.toString().padStart(3, '0')}</span></div>
              <h2 className="mail-subject">{detail.data.subject || t("(无主题)")}</h2>
              <div className="sender-detail"><span className="sender-avatar">{(detail.data.from_domain || '?')[0].toUpperCase()}</span><div><strong>{detail.data.from_address || t("未知发件人")}</strong><span>{t("发给")}{box?.address}</span></div><time>{formatTime(detail.data.received_at || detail.data.first_seen_at, true)}</time></div>
              {(detail.data.extractions || []).length > 0 && <div className="extraction-card"><div className="extraction-heading"><ShieldCheck size={15} /><strong>{detail.data.extraction_state === 'ambiguous' ? t("检测到多个候选，请核对正文") : t("从正文中提取")}</strong><span>{t("可追溯")}</span></div>
                {detail.data.extractions.map((candidate, index) => <div className="extraction" key={index}><div><span>{candidate.type === 'otp' ? t("验证码") : t("验证链接候选")}</span><code className={candidate.type === 'otp' ? 'otp' : 'link-value'}>{candidate.value}</code><small>{t("正文字符")}{candidate.source_span.start}–{candidate.source_span.end} · {candidate.rule_id}</small></div>
                  <button className="copy-button" onClick={() => void copy(candidate.value)} aria-label={candidate.type === 'otp' ? t("复制验证码") : t("复制链接")}><Copy size={14} /><span>{t("复制")}</span></button></div>)}
              </div>}
              <div className="mail-body">{detail.data.text_body !== null ? detail.data.text_body : <div className="body-unavailable"><p>{t("来源暂时只返回了邮件摘要。")}</p><button className="button secondary" disabled={!!pending} onClick={() => void perform('body', async () => { await finish(await write<Operation>('/mailboxes/' + boxId + '/messages/' + messageId + '/fetches')); })}><RefreshCw size={14} />{t("获取正文")}</button></div>}</div>
              <div className="mail-provenance"><ShieldCheck size={14} /><p>{t("纯文本安全阅读 · 外部图片与脚本不会加载")}<br /><span>{providerName(box!.provider_id)} {t("· 本地收件")}{formatTime(detail.data.first_seen_at, true)}</span></p></div>
            </>}</div>
            <div className="reader-foot"><span>{box ? t(accessNames[box.mailbox_access]) || box.mailbox_access : t("你的下一封来信，已经有了位置。")}</span><button className="text-button" disabled={!active} onClick={() => setModal('wait')}>{t("等待新邮件")}<ArrowRight size={13} /></button></div>
          </section>
        </section>
      </>}
      {view === 'providers' && <section className="sources-section">
        <div className="source-summary" aria-label={t("来源统计")}>
          <div><strong>{providers.data?.summary.external_total ?? '—'}</strong><span>{t("外部来源 · 另有 1 个本机测试箱")}</span></div>
          <div><strong>{providers.data?.summary.ready ?? '—'}</strong><span>{t("可尝试创建")}</span></div>
          <div><strong>{providers.data?.summary.credential_required ?? '—'}</strong><span>{t("待配置")}</span></div>
          <div><strong>{providers.data?.summary.pending ?? '—'}</strong><span>{t("待适配 / 部署")}</span></div>
        </div>
        <div className="source-controls"><div className="source-filters" role="group" aria-label={t("筛选来源")}>
          {[['all', t("全部")], ['enabled', t("已接入")], ['credential_required', t("待配置")], ['pending', t("待适配")]].map(([value, label]) => <button
            key={value} className={sourceFilter === value ? 'selected' : ''} onClick={() => setSourceFilter(value)} aria-pressed={sourceFilter === value}>{t(label)}</button>)}
        </div><label className="search source-search"><Search size={15} /><input aria-label={t("搜索邮箱来源")} placeholder={t("名称、域名或来源 ID")} value={sourceSearch} onChange={event => setSourceSearch(event.target.value)} /></label></div>
        <p className="source-count">{t("显示")}{visibleProviders.length} {t("个来源 · 已接入表示具备适配器；最近实测单独显示。")}</p>
        {providers.error ? <Empty title={t("来源加载失败")} text={providers.error.message} /> : !visibleProviders.length && !providers.isLoading ? <Empty icon={Search} title={t("没有匹配的来源")} text={t("试试其他名称，或切换筛选条件。")} /> : <div className="provider-grid">{visibleProviders.map((item, index) => <article className="provider-card" key={item.id} data-provider-id={item.id}>
          <div className="provider-number">{String(index + 1).padStart(2, '0')}<span className={'badge ' + (item.admission_state === 'enabled' ? '' : 'subtle')}>{item.id === 'local' ? t("本机") : t(sourceStates[item.admission_state]) || item.admission_state}</span></div>
          <span className="provider-symbol"><Layers3 size={24} strokeWidth={1.2} /></span>
          <h2>{t(item.name)}</h2><p>{t(item.description)}</p>
          <dl><div><dt>{t("访问方式")}</dt><dd>{t(accessNames[item.mailbox_access]) || t("来源账户")}</dd></div>
            <div><dt>{t("取件方式")}</dt><dd>{item.id === 'local' ? t("SMTP → 本地入库") : item.adapter_implemented ? t("Worker 自动轮询") : t("待接通")}</dd></div>
            <div><dt>{t("最近实测")}</dt><dd>{item.last_check ? (item.last_check.status === 'ok' ? t("通过 · ") : t("失败 · ")) + formatTime(item.last_check.checked_at) : item.id === 'local' ? t("本机接收") : t("尚未实测")}</dd></div></dl>
          <p className="fine-print">{item.last_check?.status === 'failed' ? t("最近错误：") + item.last_check.code : t(item.blocked_reason) || (item.id === 'local' ? t("仅接收发往本机的测试邮件。") : t("来源可用性以实时观测为准。"))}</p>
          <dl className="provider-metrics"><div><dt>{t('请求成功率')}</dt><dd>{item.intelligence?.request_success_rate == null ? '—' : Math.round(item.intelligence.request_success_rate * 100) + '%'} <small>({item.intelligence?.request_samples || 0})</small></dd></div><div><dt>{t('收件成功率')}</dt><dd>{item.intelligence?.delivery_success_rate == null ? '—' : Math.round(item.intelligence.delivery_success_rate * 100) + '%'} <small>({item.intelligence?.delivery_samples || 0})</small></dd></div><div><dt>{t('延迟 P95')}</dt><dd>{item.intelligence?.latency_p95_ms == null ? '—' : item.intelligence.latency_p95_ms + ' ms'}</dd></div></dl>
          <p className="fine-print">{t('近 24 小时观测；无样本显示横线。收件率按已结束等待任务统计。')}</p>
          <button className="text-button" disabled={!!pending || item.admission_state !== 'enabled'} onClick={() => void perform('probe', async () => { await write('/providers/' + item.id + '/probes'); }, t('来源探测已排队'))}>{t('测试健康度')}<Radio size={13} /></button>
          <div className="source-links">{item.homepage && <a href={item.homepage} target="_blank" rel="noreferrer">{t("官方网站")}<ExternalLink size={12} /></a>}{item.credential_fields.length > 0 && <button onClick={() => setConfigureSource(item)}>{item.configured ? t("修改配置") : t("配置来源")}</button>}</div>
          <button className="text-button" disabled={item.admission_state !== 'enabled'} onClick={() => { setCreateProvider(item.id); setModal('create'); }}>{item.admission_state === 'enabled' ? t("创建邮箱") : t(sourceStates[item.admission_state])}<ArrowRight size={14} /></button>
        </article>)}</div>}
      </section>}
      {view === 'waits' && <section className="task-panel"><div className="section-heading"><div><h2>{t("新邮件，自动匹配。")}</h2><p>{t("按发件域名和主题等待，命中后返回明确的邮件 ID。")}</p></div><button className="button secondary" disabled={!active} onClick={() => setModal('wait')}><Plus size={15} />{t("新建任务")}</button></div>
        {waits.error ? <Empty title={t("任务读取失败")} text={waits.error.message} /> : !waits.data?.items.length ? <Empty icon={Radio} title={t("还没有等待任务")} text={t("选中一个使用中的邮箱，开始等待下一封来信。")} /> : <div className="task-list">{waits.data.items.map(item => <article className="task-row" key={item.id}><span className="task-symbol">{item.state === 'matched' ? <Check size={19} /> : item.state === 'waiting' ? <Radio size={19} /> : <Clock3 size={19} />}</span><div className="task-description"><strong>{boxes.find(box => box.id === item.mailbox_id)?.name || item.mailbox_id}</strong><p>{Object.values(item.predicate).join(' · ') || t("任意新邮件")} <span>{t("从序号")}{item.after_seq} {t("开始")}</span></p></div><span className="badge">{t(stateNames[item.state]) || item.state}</span><time>{t("截止")}{formatTime(item.deadline_at, true)}</time>
          {item.state === 'waiting' && <button className="text-button" onClick={() => void perform('cancel', async () => { await api('/waits/' + item.id, { method: 'DELETE' }); })}>{t("取消")}</button>}
          {item.matched_message_id && <button className="text-button" onClick={() => { setBoxId(item.mailbox_id); setTimeout(() => setMessageId(item.matched_message_id), 0); setView('inbox'); setMobile('reader'); }}>{t("查看邮件")}<ArrowRight size={13} /></button>}
        </article>)}</div>}<p className="panel-note">{t("展示最近 100 个任务。任务持久保存在服务端，关闭页面不会中断等待；到期与取消均有明确状态。")}</p>
      </section>}
      {view === 'automation' && <AutomationPanel providers={providers.data?.items || []} />}
      {view === 'api' && <section className="api-grid"><article className="api-intro"><span className="provider-symbol"><Terminal size={25} strokeWidth={1.3} /></span><h2>{t("让收件成为")}<br />{t("工作流的一部分。")}</h2><p>{t("统一 JSON 接口，持久化等待任务，实时事件。浏览器与自动化共享同一份邮件状态。")}</p><a className="button secondary" href="/api/docs" target="_blank" rel="noreferrer">{t("打开接口文档")}<ExternalLink size={14} /></a><div className="token-section"><h3>{t("邮箱访问令牌")}</h3><p>{t("仅授权当前选中的邮箱，1 小时后失效。令牌仅在签发后展示，请妥善保存。")}</p><div className="token-box-name"><Mail size={14} />{box?.address || t("请先选择一个使用中的邮箱")}</div>
        {issuedToken ? <div className="token-result"><code>{issuedToken}</code><button className="icon-button" aria-label={t("复制访问令牌")} onClick={() => void copy(issuedToken)}><Copy size={15} /></button></div> : <button className="text-button" disabled={!active || !!pending} onClick={() => void perform('token', async () => {
          const result = await write<{ access_token: string }>('/mailboxes/' + boxId + '/tokens', { scopes: ['mailbox:read', 'messages:read', 'messages:fetch', 'waits:write', 'events:read'], ttl_seconds: 3600 }); setIssuedToken(result.access_token);
        })}>{t("生成当前邮箱令牌")}<ArrowRight size={14} /></button>}</div></article>
        <article className="code-panel"><div className="code-heading"><span><Circle size={7} fill="currentColor" />API / v1</span><button className="text-button" onClick={() => void copy(snippet)}><Copy size={13} />{t("复制示例")}</button></div><pre>{snippet}</pre><div className="code-notes"><p><Check size={14} />{t("写入请求支持幂等键")}</p><p><Check size={14} />{t("邮箱令牌按箱授权")}</p><p><Check size={14} />{t("错误使用 application/problem+json")}</p></div></article>
      </section>}
      <footer className="page-footer"><span><Glyph small />LINSEMAIL <span className="footer-slash">/</span><RepositoryLink credit /></span><span>{t("本地工作区")}<span className="footer-dot">·</span>v0.4.0</span></footer>
    </main>
    {notice && <div className={'toast' + (notice.error ? ' error' : '')} role="status">{notice.error ? <Circle size={15} /> : <Check size={15} />}{t(notice.text)}<button className="icon-button compact" aria-label={t("关闭提示")} onClick={() => setNotice(null)}><X size={14} /></button></div>}
    {configureSource && <ProviderSettings source={configureSource} pending={pending === 'configure'} close={() => setConfigureSource(null)} save={credentials => void perform('configure', async () => {
      await write('/providers/' + configureSource.id + '/configuration', { credentials }, 'PUT');
      await query.invalidateQueries({ queryKey: ['providers'] }); setConfigureSource(null);
    }, t("来源配置已加密保存"))} clear={() => void perform('configure', async () => {
      await api('/providers/' + configureSource.id + '/configuration', { method: 'DELETE' });
      await query.invalidateQueries({ queryKey: ['providers'] }); setConfigureSource(null);
    }, t("已移除来源配置"))} />}
    {modal === 'create' && <CreateForm providers={providers.data?.items || []} initialProvider={createProvider} pending={pending === 'create'} close={() => { setModal(null); setCreateProvider(''); }} submit={data => void perform('create', async () => {
      const operation = await write<Operation>('/mailboxes', data); setBoxId(operation.resource_id); setView('inbox'); setMobile('messages'); setModal(null); refresh(); await finish(operation);
    }, t("新邮箱已就绪"))} />}
    {modal === 'wait' && box && <WaitForm box={box} pending={pending === 'wait'} close={() => setModal(null)} submit={data => void perform('wait', async () => { await write<WaitJob>('/mailboxes/' + box.id + '/waits', data); setModal(null); setView('waits'); }, t("等待任务已创建"))} />}
    {modal === 'delete' && box && <Modal title={t("移除这个邮箱？")} caption={box.address || box.name} close={() => setModal(null)}><p className="delete-copy">{t("停止取件并清理本地邮件，同时撤销该邮箱的访问令牌。第三方来源上的副本仍由其保留策略决定。")}</p><div className="modal-actions"><button className="button secondary" onClick={() => setModal(null)}>{t("保留邮箱")}</button><button className="button primary" disabled={!!pending} onClick={() => void perform('delete', async () => { await finish(await api<Operation>('/mailboxes/' + box.id, { method: 'DELETE' })); setModal(null); setBoxId(null); setMessageId(null); }, t("邮箱已移除"))}><Trash2 size={14} />{t("确认移除")}</button></div></Modal>}
  </div>;
}
