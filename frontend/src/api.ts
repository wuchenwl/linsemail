import { getLocale, problemText, t } from './i18n';

export interface Session { workspace_id: string; workspace_name: string; csrf_token: string; local_mode: boolean }
export interface Provider {
  intelligence?: { request_success_rate: number | null; delivery_success_rate: number | null; latency_p95_ms: number | null; request_samples: number; delivery_samples: number; health: string };
  id: string; name: string; description: string; mailbox_access: string; admission_state: string; transport: string[];
  homepage: string | null; blocked_reason: string | null; adapter_implemented: boolean; configured: boolean;
  capabilities: Record<string, string>; documentation: string[];
  credential_fields: { name: string; label: string; secret: boolean; required?: boolean }[];
  last_check: { checked_at: string; status: string; action: string; code: string | null; create?: boolean; list?: boolean; read_text?: boolean } | null;
}
export interface ProviderPage extends Page<Provider> {
  summary: { external_total: number; adapter_total: number; ready: number; credential_required: number; pending: number; local: number };
}
export interface Mailbox {
  id: string; provider_id: string; address: string | null; name: string; state: string;
  mailbox_access: string; created_at: string; expires_at: string; last_ingest_seq: number;
  message_count: number; version: number; labels: string[];
  sync: { state: string; last_success_at: string | null; freshness: string; last_error: { detail: string } | null };
}
export interface Message {
  id: string; mailbox_id: string; from_address: string | null; from_domain: string | null;
  subject: string | null; received_at: string | null; first_seen_at: string; ingest_seq: number; body_state: string;
  version: number;
}
export interface Extraction {
  type: string; value: string; rule_id: string; source_span: { start: number; end: number };
}
export interface Detail extends Message {
  text_body: string | null; extractions: Extraction[]; extraction_state: string;
}
export interface WaitJob {
  id: string; mailbox_id: string; state: string; after_seq: number; deadline_at: string;
  predicate: Record<string, string>; matched_message_id: string | null; created_at: string; version: number;
}
export interface Operation { id: string; state: string; type: string; resource_id: string; error: { detail: string } | null }
export interface Page<T> { items: T[]; has_more: boolean; next_cursor: string | null }
let csrfToken = '';
export function setCsrf(value: string) { csrfToken = value; }
export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) { super(message); }
}
export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body) headers.set('Content-Type', 'application/json');
  if (options.method && !['GET', 'HEAD'].includes(options.method)) headers.set('X-CSRF-Token', csrfToken);
  const response = await fetch('/api/v1' + path, { credentials: 'same-origin', ...options, headers });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new ApiError(response.status, error.code || 'request_failed', problemText(error.code || 'request_failed', error.detail));
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}
export function write<T>(path: string, body?: unknown, method = 'POST'): Promise<T> {
  return api<T>(path, { method, headers: { 'Idempotency-Key': crypto.randomUUID() }, body: body === undefined ? undefined : JSON.stringify(body) });
}
export function formatTime(value?: string | null, full = false) {
  if (!value) return t('尚未取件');
  return new Intl.DateTimeFormat(getLocale() === 'en' ? 'en-GB' : 'zh-CN', full
    ? { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }
    : { hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(value));
}
export function remaining(value?: string) {
  if (!value) return '—';
  const minutes = Math.ceil((new Date(value).getTime() - Date.now()) / 60000);
  if (minutes <= 0) return t('已到期');
  if (getLocale() === 'en') return minutes < 60 ? minutes + ' min' : Math.floor(minutes / 60) + ' h ' + minutes % 60 + ' min';
  if (minutes < 60) return minutes + ' 分钟';
  return Math.floor(minutes / 60) + ' 小时 ' + minutes % 60 + ' 分';
}
