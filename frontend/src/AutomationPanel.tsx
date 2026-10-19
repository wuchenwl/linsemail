import { useState, type FormEvent } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowRight, Copy, Radio, Terminal, Trash2 } from 'lucide-react';
import { api, write, formatTime, type Provider } from './api';
import { t } from './i18n';

type Task = { id: string; kind: string; state: string; result: Record<string, string>; error: { detail: string } | null; created_at: string };
type Hook = { id: string; url: string; active: boolean; events: string[] };
type Delivery = { id: string; state: string; attempts: number; status_code: number | null; error_code: string | null };
const terminal = new Set(['succeeded', 'matched', 'failed', 'timed_out', 'cancelled']);
const states: Record<string, string> = { queued: '排队中', running: '执行中', waiting: '等待来信', matched: '已匹配', succeeded: '已完成', failed: '失败', timed_out: '已超时', cancelled: '已取消', pending: '等待重试' };

export function AutomationPanel({ providers }: { providers: Provider[] }) {
  const query = useQueryClient();
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [secret, setSecret] = useState('');
  const tasks = useQuery({ queryKey: ['agent-tasks'], queryFn: () => api<{ items: Task[] }>('/tasks'), refetchInterval: 3000 });
  const hooks = useQuery({ queryKey: ['webhooks'], queryFn: () => api<{ items: Hook[] }>('/webhooks') });
  const deliveries = useQuery({ queryKey: ['deliveries'], queryFn: () => api<{ items: Delivery[] }>('/webhook-deliveries'), refetchInterval: 5000 });
  async function perform(action: () => Promise<unknown>) {
    if (busy) return;
    setBusy(true); setError('');
    try { await action(); await Promise.all(['agent-tasks', 'webhooks', 'deliveries'].map(key => query.invalidateQueries({ queryKey: [key] }))); }
    catch (err) { setError((err as Error).message); }
    finally { setBusy(false); }
  }
  function workflow(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = event.currentTarget, data = new FormData(form), provider = String(data.get('provider') || '');
    const predicate = Object.fromEntries(['from_domain', 'subject_contains', 'text_contains'].map(key => [key, String(data.get(key) || '').trim()]).filter(([, value]) => value));
    void perform(async () => { await write('/workflows', { ...(provider ? { provider_id: provider } : {}), name: String(data.get('name') || ''), predicate, timeout_seconds: Number(data.get('timeout')) }); form.reset(); });
  }
  function webhook(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = event.currentTarget, data = new FormData(form);
    void perform(async () => { const result = await write<{ secret: string }>('/webhooks', { url: String(data.get('url')), events: [String(data.get('event'))] }); setSecret(result.secret); form.reset(); });
  }
  return <section className="automation-panel">
    <div className="automation-intro"><Radio size={22} /><div><h2>{t('从收件，到可恢复的自动化。')}</h2><p>{t('一次请求完成创建与匹配。关闭页面后，任务仍由 Worker 持续执行。')}</p></div></div>
    {(error || tasks.error || hooks.error || deliveries.error) && <p className="automation-error" role="alert">{error || tasks.error?.message || hooks.error?.message || deliveries.error?.message}</p>}
    <div className="automation-columns">
      <article className="automation-card"><p className="eyebrow">PERSISTENT WORKFLOW</p><h3>{t('新建组合任务')}</h3>
        <form onSubmit={workflow}><label>{t('邮箱名称')}<input name="name" maxLength={120} /></label>
          <label>{t('邮箱来源')}<select name="provider"><option value="">{t('自动选择（智能路由）')}</option>{providers.filter(x => x.admission_state === 'enabled').map(x => <option key={x.id} value={x.id}>{t(x.name)}</option>)}</select></label>
          <label>{t('发件域名')}<input name="from_domain" maxLength={253} placeholder="example.com" /></label>
          <label>{t('主题包含')}<input name="subject_contains" maxLength={200} /></label><label>{t('正文包含')}<input name="text_contains" maxLength={200} /></label>
          <label>{t('等待时限')}<select name="timeout" defaultValue="120"><option value="60">60 s</option><option value="120">120 s</option><option value="300">300 s</option></select></label>
          <button className="button primary" disabled={busy}>{t('启动工作流')}<ArrowRight size={14} /></button>
        </form></article>
      <article className="automation-card"><p className="eyebrow">SIGNED EVENTS / WEBHOOK</p><h3>{t('事件推送')}</h3><p>{t('签名事件支持持久重试。接收端按事件 ID 去重，再通过 API 读取邮件。')}</p>
        <form onSubmit={webhook}><label>{t('接收地址')}<input type="url" name="url" required maxLength={2048} placeholder="https://example.com/hooks/mail" /></label>
          <label>{t('订阅事件')}<select name="event"><option value="wait.matched">wait.matched</option><option value="message.received">message.received</option><option value="operation.completed">operation.completed</option><option value="mailbox.expired">mailbox.expired</option></select></label>
          <button className="button secondary" disabled={busy}>{t('添加 Webhook')}</button></form>
        {secret && <div className="automation-secret"><p>{t('请保存签名密钥，列表中不会再次展示。')}</p><code>{secret}</code><button className="text-button" onClick={() => void navigator.clipboard.writeText(secret).catch(() => setError(t('复制失败')))}><Copy size={13} />{t('复制')}</button><button className="text-button" onClick={() => setSecret('')}>{t('关闭')}</button></div>}
        <div className="automation-hooks">{hooks.data?.items.filter(x => x.active).map(hook => <div key={hook.id}><span><strong>{hook.url}</strong><small>{hook.events.join(', ')}</small></span><button className="icon-button" aria-label={t('移除 Webhook')} disabled={busy} onClick={() => void perform(() => api('/webhooks/' + hook.id, { method: 'DELETE' }))}><Trash2 size={15} /></button></div>)}</div>
        <p className="fine-print">{t('默认仅允许公网 HTTPS。本机回调须显式启用回环测试配置。')}</p>
      </article>
    </div>
    <article className="automation-card"><div className="section-heading"><div><p className="eyebrow">DURABLE TASKS</p><h3>{t('组合任务与来源探测')}</h3></div><span>{tasks.data?.items.length || 0}</span></div>
      {!tasks.data?.items.length && <p>{t('还没有组合任务。创建邮箱并等待邮件，从这里开始。')}</p>}
      <div className="automation-task-list">{tasks.data?.items.map(task => <div className="automation-task" key={task.id}><div><strong>{task.result.address || task.id}</strong><small>{task.kind} · {formatTime(task.created_at, true)}</small>{task.error && <p>{task.error.detail}</p>}{task.result.matched_message_id && <code>{task.result.matched_message_id}</code>}</div><span className="badge">{t(states[task.state]) || task.state}</span>{!terminal.has(task.state) && <button className="text-button" disabled={busy} onClick={() => void perform(() => api('/tasks/' + task.id, { method: 'DELETE' }))}>{t('取消')}</button>}</div>)}</div>
    </article>
    <div className="automation-columns"><article className="automation-card"><p className="eyebrow">MCP / STDIO</p><h3><Terminal size={17} />{t('MCP 工具接入')}</h3><p>{t('在项目目录运行 MCP 服务，复用本机工作区鉴权和同一套 API。')}</p><pre>python -m backend.mcp</pre><p>{t('支持来源查询、智能建箱、邮件读取、组合任务与任务取消。')}</p><a className="text-button" href="https://github.com/wuchenwl/linsemail/blob/main/docs/agent.zh-CN.md" target="_blank" rel="noreferrer">{t('查看接入指南')}<ArrowRight size={13} /></a></article>
      <article className="automation-card"><p className="eyebrow">WEBHOOK OUTBOX</p><h3>{t('最近投递')}</h3>{!deliveries.data?.items.length && <p>{t('订阅事件发生后，这里会显示签名推送与重试结果。')}</p>}{deliveries.data?.items.slice(0, 10).map(item => <div className="automation-delivery" key={item.id}><code>{item.id.slice(-12)}</code><span>{t(states[item.state]) || item.state}</span><span>{item.status_code || item.error_code || '—'} · {item.attempts}</span></div>)}</article></div>
  </section>;
}
