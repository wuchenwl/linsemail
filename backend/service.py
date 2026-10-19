from datetime import timedelta, timezone
import base64
import hashlib
import json
import secrets

from cryptography.fernet import InvalidToken
from sqlalchemy import func, select

from .db import ALL_SCOPES
from .models import Credential, Event, Idempotency, Mailbox, Message, Operation, SyncJob, WaitJob, Workspace, now, uid
from .providers import PROVIDERS, extract, provider_info
from mailbox_clients import normalize_address_prefix, ProviderError
import re


def aware(value):
    return value.replace(tzinfo=value.tzinfo or timezone.utc) if value else None


def iso(value):
    return aware(value).isoformat() if value else None


class DomainError(Exception):
    def __init__(self, status, code, detail):
        self.status, self.code, self.detail = status, code, detail
        super().__init__(detail)


def problem(status, code, detail):
    return {"type": "urn:linsemail:problem:" + code, "title": detail, "detail": detail,
            "status": status, "code": code, "request_id": uid("req")}


def mailbox_dict(row, count=0):
    return {"id": row.id, "provider_id": row.provider_id, "address": row.address, "name": row.name,
            "state": row.state, "mailbox_access": row.mailbox_access, "labels": row.labels,
            "created_at": iso(row.created_at), "expires_at": iso(row.expires_at), "provider_expires_at": iso(row.provider_expires_at),
            "last_ingest_seq": row.last_ingest_seq, "version": row.version, "message_count": count,
            "sync": {"state": row.sync_state, "last_success_at": iso(row.last_success_at),
                     "freshness": "fresh" if row.last_success_at and row.sync_state not in ("backoff", "blocked") and (now()-aware(row.last_success_at)).total_seconds()<60 else "stale" if row.last_success_at else "unknown",
                     "last_error": row.last_error, "list_completeness": "complete" if row.last_success_at else "unknown"}}


def operation_dict(row):
    return {key: getattr(row, key) for key in ("id", "type", "state", "resource_id", "error")} | {"created_at": iso(row.created_at), "updated_at": iso(row.updated_at)}


def wait_dict(row):
    return {key: getattr(row, key) for key in ("id", "mailbox_id", "state", "after_seq", "predicate", "matched_message_id", "version", "error")} | {"created_at": iso(row.created_at), "deadline_at": iso(row.deadline_at)}


def message_dict(row, detail=False):
    value = {key: getattr(row, key) for key in ("id", "mailbox_id", "from_address", "from_domain", "subject", "ingest_seq", "body_state", "identity_strength", "version")}
    value.update(received_at=iso(row.received_at), first_seen_at=iso(row.first_seen_at))
    if detail:
        value.update(text_body=row.text_body, sanitized_html=None, sanitization_version=None,
                     extractions=row.extractions, extraction_state=row.extraction_state)
    return value


def workspace_lock(session, workspace_id):
    return session.scalar(select(Workspace).where(Workspace.id == workspace_id).with_for_update())


def emit(session, workspace, event_type, mailbox_id=None, message_id=None, wait_id=None, version=1):
    workspace.event_seq += 1
    event = Event(id=uid("evt"), workspace_id=workspace.id, seq=workspace.event_seq, type=event_type,
                  mailbox_id=mailbox_id, message_id=message_id, wait_id=wait_id, resource_version=version, occurred_at=now())
    session.add(event)
    if wait_id and event_type in ('wait.matched','wait.terminated'):
        wait=session.get(WaitJob,wait_id)
        if wait and wait.state in ('matched','timed_out','expired'):
            from .intelligence import observe
            mailbox=session.get(Mailbox,mailbox_id)
            observe(session,mailbox.provider_id,'delivery',wait.state=='matched',
                    (now()-aware(wait.created_at)).total_seconds()*1000,identity=event.id)
    from .webhooks import enqueue
    enqueue(session,event)
    return event


def matches(message, predicate):
    if predicate.get("from_domain") and (message.from_domain or "").lower() != predicate["from_domain"].lower():
        return False
    if predicate.get("subject_contains") and predicate["subject_contains"].casefold() not in (message.subject or "").casefold():
        return False
    if predicate.get("text_contains") and (message.text_body is None or predicate["text_contains"].casefold() not in message.text_body.casefold()):
        return False
    return True


def match_waits(session, workspace, mailbox, messages):
    jobs = session.scalars(select(WaitJob).where(WaitJob.mailbox_id == mailbox.id, WaitJob.state == "waiting").with_for_update()).all()
    stamp = now()
    for job in jobs:
        if aware(job.deadline_at) <= stamp:
            continue
        candidate = next((message for message in sorted(messages, key=lambda item: item.ingest_seq)
                          if message.ingest_seq > job.after_seq and matches(message, job.predicate)), None)
        if candidate:
            job.state, job.matched_message_id = "matched", candidate.id
            job.version += 1
            emit(session, workspace, "wait.matched", mailbox.id, candidate.id, job.id, job.version)


def upsert_messages(session, workspace, mailbox, items):
    changed = []
    for item in items:
        existing = session.scalar(select(Message).where(Message.mailbox_id == mailbox.id, Message.upstream_identity == item["upstream_identity"]))
        if not existing:
            mailbox.last_ingest_seq += 1
            existing = Message(id=uid("msg"), workspace_id=mailbox.workspace_id, mailbox_id=mailbox.id,
                               ingest_seq=mailbox.last_ingest_seq, **item)
            existing.version = 1
            session.add(existing)
            kind = "message.received"
        elif item.get("text_body") is not None and item["text_body"] != existing.text_body:
            for key, value in item.items():
                if value is not None:
                    setattr(existing, key, value)
            existing.version += 1
            kind = "message.updated"
        else:
            continue
        existing.body_state = "ready" if existing.text_body is not None else "metadata_only"
        existing.extractions, existing.extraction_state = extract(existing.text_body)
        session.flush()
        emit(session, workspace, kind, mailbox.id, existing.id, version=existing.version)
        changed.append(existing)
    session.flush()
    if changed:
        match_waits(session, workspace, mailbox, changed)
    return changed


class Service:
    def __init__(self, database):
        self.db = database

    def principal(self, token):
        if not token or len(token) > 256:
            raise DomainError(401, "invalid_credentials", "请进入工作区或提供有效令牌。")
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.db.session() as session:
            row = session.scalar(select(Credential).where(Credential.token_hash == digest, Credential.revoked.is_(False)))
            if not row or (row.expires_at and aware(row.expires_at) <= now()):
                raise DomainError(401, "invalid_credentials", "凭据无效或已过期。")
            return {"id": row.id, "workspace_id": row.workspace_id, "kind": row.kind, "mailbox_id": row.mailbox_id,
                    "scopes": row.scopes, "csrf_token": row.csrf_token}

    def local_login(self):
        token, csrf = "lm_session_" + secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self.db.session.begin() as session:
            session.add(Credential(id=uid("sess"), workspace_id="ws_local", token_hash=hashlib.sha256(token.encode()).hexdigest(),
                                   kind="session", scopes=ALL_SCOPES, csrf_token=csrf, expires_at=now()+timedelta(hours=12)))
        return token, csrf

    @staticmethod
    def require(principal, scope, mailbox_id=None, workspace_only=False):
        if scope not in principal["scopes"]:
            raise DomainError(403, "insufficient_scope", "此凭据没有执行该操作的权限。")
        if workspace_only and principal["kind"] == "mailbox":
            raise DomainError(403, "insufficient_scope", "此操作需要工作区凭据。")
        if principal["kind"] == "mailbox" and principal["mailbox_id"] != mailbox_id:
            raise DomainError(404, "resource_not_found", "资源不存在。")

    @staticmethod
    def box(session, principal, mailbox_id, active=False):
        row = session.get(Mailbox, mailbox_id)
        if not row or row.workspace_id != principal["workspace_id"] or (principal["kind"] == "mailbox" and principal["mailbox_id"] != mailbox_id):
            raise DomainError(404, "resource_not_found", "邮箱不存在。")
        if active and (row.state != "active" or aware(row.expires_at) <= now()):
            raise DomainError(410 if row.state in ("active", "expired") else 409, "mailbox_unavailable", "此邮箱已过期或暂时不可取件。")
        return row

    def encode_cursor(self, workspace_id, scope, value):
        return self.db.encrypt({"workspace": workspace_id, "scope": scope, "value": value, "issued": now().timestamp()})

    def decode_cursor(self, principal, scope, cursor):
        if not cursor:
            return None
        try:
            value = self.db.decrypt(cursor)
            if value["workspace"] != principal["workspace_id"] or value["scope"] != scope:
                raise ValueError()
            if now().timestamp()-value["issued"] > 86400:
                raise DomainError(410, "cursor_expired", "游标已过期，请重新读取。")
            return value["value"]
        except (InvalidToken, ValueError, KeyError, TypeError, json.JSONDecodeError):
            raise DomainError(400, "invalid_cursor", "游标无效或不属于当前列表。") from None

    def idempotent(self, principal, route, key, payload, callback):
        if not 8 <= len(key or "") <= 128:
            raise DomainError(400, "idempotency_key_required", "此写操作需要 8–128 字符的 Idempotency-Key。")
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        request_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        with self.db.session.begin() as session:
            workspace = workspace_lock(session, principal["workspace_id"])
            saved = session.scalar(select(Idempotency).where(Idempotency.workspace_id == workspace.id, Idempotency.route == route, Idempotency.key_hash == key_hash))
            if saved:
                if saved.request_hash != request_hash:
                    raise DomainError(409, "idempotency_conflict", "相同幂等键已用于不同请求。")
                return saved.response
            response = callback(session, workspace)
            session.add(Idempotency(id=uid("idem"), workspace_id=workspace.id, route=route, key_hash=key_hash, request_hash=request_hash, response=response))
            return response

    def create_mailbox(self, principal, payload, key):
        self.require(principal, "mailbox:write", workspace_only=True)
        try:
            prefix = normalize_address_prefix(payload.get('address_prefix'))
        except ProviderError as exc:
            raise DomainError(422, exc.code, str(exc)) from None
        def create(session, workspace):
            provider_id=payload.get('provider_id')
            if provider_id is None:
                from .intelligence import choose
                provider_id=choose(self,principal,prefix)
            provider = provider_info(provider_id)
            if not provider:
                raise DomainError(422, "unsupported_provider", "该来源未接入当前工作台。")
            provider = self.get_provider(principal, provider["id"])
            if prefix and provider['capabilities'].get('custom_prefix') != 'supported':
                raise DomainError(422, 'custom_prefix_unsupported', '该来源暂不支持自定义邮箱前缀，请留空以使用上游随机地址。')
            if provider["admission_state"] != "enabled":
                raise DomainError(409, "provider_" + provider["admission_state"], provider["blocked_reason"] or "该来源暂不可创建邮箱。")
            count = session.scalar(select(func.count()).select_from(Mailbox).where(Mailbox.workspace_id == workspace.id, Mailbox.state.in_(["creating", "active"])))
            if count >= 100:
                raise DomainError(429, "workspace_limit", "当前工作区最多保留 100 个活跃邮箱。")
            stamp = now()
            box = Mailbox(id=uid("mb"), workspace_id=workspace.id, provider_id=provider["id"], name=payload.get("name") or "新建邮箱",
                          mailbox_access=provider["mailbox_access"], labels=payload.get("labels", []), created_at=stamp,
                          expires_at=stamp+timedelta(seconds=payload.get("ttl_seconds", 3600)))
            if prefix:
                # Reuse encrypted session storage so the queued worker can reopen this request.
                seed = base64.b64encode(json.dumps({'address_prefix': prefix}).encode('utf-8')).decode('ascii')
                box.vault_blob = self.db.encrypt({'inbox.local.json': seed})
            session.add(box); session.flush()
            session.add(SyncJob(mailbox_id=box.id, next_due_at=stamp))
            operation = Operation(id=uid("op"), workspace_id=workspace.id, type="create_mailbox", state="queued", resource_id=box.id, created_at=stamp, updated_at=stamp)
            session.add(operation); session.flush()
            return operation_dict(operation)
        return self.idempotent(principal, "POST/mailboxes", key, payload, create)

    def list_providers(self, principal):
        self.require(principal, "providers:read", workspace_only=True)
        from .models import ProviderConfiguration
        with self.db.session() as session:
            configs = {row.provider_id: row for row in session.scalars(select(ProviderConfiguration))}
        result = []
        for provider in PROVIDERS:
            item = dict(provider)
            config = configs.get(item["id"])
            credentials = self.db.decrypt(config.vault_blob) if config and config.vault_blob else {}
            item["configured"] = all(credentials.get(field["name"]) for field in item["credential_fields"] if field.get('required', True))
            if item["admission_state"] == "credential_required" and item["configured"]:
                item["admission_state"], item["blocked_reason"] = "enabled", None
            item["last_check"] = config.last_check if config else None
            result.append(item)
        return result

    def get_provider(self, principal, provider_id):
        item = next((item for item in self.list_providers(principal) if item["id"] == provider_id), None)
        if item is None:
            raise DomainError(404, "provider_not_found", "来源不存在。")
        from .intelligence import snapshot
        item['intelligence']=snapshot(self.db,[provider_id])[provider_id]
        return item

    def configure_provider(self, principal, provider_id, credentials):
        self.require(principal, "mailbox:write", workspace_only=True)
        provider = self.get_provider(principal, provider_id)
        fields = {item["name"] for item in provider["credential_fields"]}
        required = {item['name'] for item in provider['credential_fields'] if item.get('required', True)}
        if not fields or not provider["adapter_implemented"]:
            raise DomainError(409, "provider_configuration_unsupported", "该来源没有可配置的已实现适配器。")
        if not required.issubset(credentials) or not set(credentials).issubset(fields) or any(not isinstance(value, str) or not value.strip() or len(value) > 4096 or "\n" in value or "\r" in value for value in credentials.values()):
            raise DomainError(422, "invalid_provider_configuration", "请完整填写来源要求的字段。")
        for field in ("namespace", "server_id"):
            if field in credentials and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,62}", credentials[field]):
                raise DomainError(422, "invalid_provider_configuration", "命名空间与服务器 ID 只能使用字母、数字和连字符。")
        for field in ('team_id', 'folder_id'):
            if field in credentials and not re.fullmatch(r'[0-9]{1,20}', credentials[field]):
                raise DomainError(422, 'invalid_provider_configuration', '团队和文件夹 ID 必须为数字。')
        if "domain" in credentials:
            from .expansion_clients import domain
            from mailbox_clients import ProviderError
            try:
                domain(credentials["domain"])
            except ProviderError:
                raise DomainError(422, "invalid_provider_configuration", "请填写有效的收件域名。")
        if 'base_url' in credentials:
            from .expansion_clients import deployment_url
            from mailbox_clients import ProviderError
            try:
                deployment_url(credentials['base_url'])
            except ProviderError:
                raise DomainError(422, 'invalid_provider_configuration', '请填写不含账号、查询串或片段的 HTTP(S) 部署地址。')
        from .models import ProviderConfiguration
        with self.db.session.begin() as session:
            workspace_lock(session, principal["workspace_id"])
            config = session.get(ProviderConfiguration, provider_id)
            if not config:
                config = ProviderConfiguration(provider_id=provider_id)
                session.add(config)
            config.vault_blob = self.db.encrypt(credentials)
            config.last_check = None
            config.updated_at = now()
        return self.get_provider(principal, provider_id)

    def clear_provider_configuration(self, principal, provider_id):
        self.require(principal, "mailbox:write", workspace_only=True)
        self.get_provider(principal, provider_id)
        from .models import ProviderConfiguration
        with self.db.session.begin() as session:
            workspace_lock(session, principal["workspace_id"])
            config = session.get(ProviderConfiguration, provider_id)
            if config:
                config.vault_blob, config.last_check = None, None
        return self.get_provider(principal, provider_id)

    def list_mailboxes(self, principal, cursor=None, limit=50):
        self.require(principal, "mailbox:read", workspace_only=True)
        offset = self.decode_cursor(principal, "mailboxes", cursor)
        with self.db.session() as session:
            query = select(Mailbox).where(Mailbox.workspace_id == principal["workspace_id"], Mailbox.state != "deleted")
            if offset:
                stamp, row_id = offset
                from sqlalchemy import or_, and_
                from .providers import parse_time
                query = query.where(or_(Mailbox.created_at < parse_time(stamp), and_(Mailbox.created_at == parse_time(stamp), Mailbox.id < row_id)))
            rows = session.scalars(query.order_by(Mailbox.created_at.desc(), Mailbox.id.desc()).limit(limit+1)).all()
            more = len(rows)>limit; rows=rows[:limit]
            counts = dict(session.execute(select(Message.mailbox_id, func.count()).where(Message.workspace_id==principal["workspace_id"]).group_by(Message.mailbox_id)).all())
            return {"items": [mailbox_dict(row, counts.get(row.id, 0)) for row in rows], "has_more": more,
                    "next_cursor": self.encode_cursor(principal["workspace_id"], "mailboxes", [iso(rows[-1].created_at), rows[-1].id]) if more else None}

    def get_mailbox(self, principal, mailbox_id):
        self.require(principal, "mailbox:read", mailbox_id)
        with self.db.session() as session:
            box = self.box(session, principal, mailbox_id)
            count = session.scalar(select(func.count()).select_from(Message).where(Message.mailbox_id == box.id))
            return mailbox_dict(box, count)

    def list_messages(self, principal, mailbox_id, cursor=None, limit=50):
        self.require(principal, "messages:read", mailbox_id)
        offset = self.decode_cursor(principal, "messages/"+mailbox_id, cursor)
        with self.db.session() as session:
            box = self.box(session, principal, mailbox_id, active=True)
            query = select(Message).where(Message.mailbox_id == box.id)
            if offset is not None:
                query = query.where(Message.ingest_seq < offset)
            rows = session.scalars(query.order_by(Message.ingest_seq.desc()).limit(limit+1)).all()
            more=len(rows)>limit;rows=rows[:limit]
            return {"mailbox_id": box.id, "items": [message_dict(row) for row in rows], "sync": mailbox_dict(box)["sync"], "has_more": more,
                    "next_cursor": self.encode_cursor(principal["workspace_id"], "messages/"+mailbox_id, rows[-1].ingest_seq) if more else None}

    def get_message(self, principal, mailbox_id, message_id):
        self.require(principal, "messages:read", mailbox_id)
        with self.db.session() as session:
            self.box(session, principal, mailbox_id, active=True)
            row = session.get(Message, message_id)
            if not row or row.mailbox_id != mailbox_id:
                raise DomainError(404, "resource_not_found", "邮件不存在。")
            return message_dict(row, True)

    def queue_operation(self, principal, mailbox_id, kind, message_id=None):
        self.require(principal, "messages:fetch" if kind=="fetch_body" else "mailbox:write", mailbox_id, workspace_only=kind!="fetch_body")
        with self.db.session.begin() as session:
            workspace_lock(session, principal["workspace_id"])
            box = self.box(session, principal, mailbox_id, active=kind!="delete_mailbox")
            if message_id:
                message = session.get(Message, message_id)
                if not message or message.mailbox_id != box.id:
                    raise DomainError(404, "resource_not_found", "邮件不存在。")
            existing = session.scalar(select(Operation).where(Operation.resource_id==box.id, Operation.type==kind,
                                                             Operation.message_id==message_id, Operation.state.in_(["queued","running"])))
            if existing:
                return operation_dict(existing)
            if kind == "delete_mailbox":
                previous = session.scalar(select(Operation).where(Operation.resource_id==box.id,Operation.type==kind).order_by(Operation.created_at.desc()))
                if box.state in ("deleted", "deleting") and previous:
                    return operation_dict(previous)
                box.state="deleting";box.version+=1
            if kind=="sync_mailbox":
                if box.sync_state=="blocked":
                    raise DomainError(409,"mailbox_blocked","此邮箱的上游身份校验未通过，取件已暂停。")
                job=session.get(SyncJob,box.id);job.next_due_at=now()
                if box.sync_state not in ("syncing", "backoff"):
                    box.sync_state="scheduled"
            stamp=now()
            row=Operation(id=uid("op"),workspace_id=box.workspace_id,type=kind,state="queued",resource_id=box.id,message_id=message_id,created_at=stamp,updated_at=stamp)
            session.add(row);session.flush()
            return operation_dict(row)

    def get_operation(self, principal, operation_id):
        with self.db.session() as session:
            row=session.get(Operation,operation_id)
            if not row or row.workspace_id!=principal["workspace_id"]:
                raise DomainError(404,"resource_not_found","操作不存在。")
            self.require(principal,"messages:fetch" if row.type=="fetch_body" else "mailbox:read",row.resource_id,workspace_only=row.type!="fetch_body")
            return operation_dict(row)

    def create_wait(self, principal, mailbox_id, payload, key):
        self.require(principal,"waits:write",mailbox_id)
        def create(session, workspace):
            box=self.box(session,principal,mailbox_id,active=True)
            waiting=session.scalar(select(func.count()).select_from(WaitJob).where(WaitJob.mailbox_id==box.id,WaitJob.state=="waiting"))
            if waiting>=20:
                raise DomainError(429,"wait_limit","每个邮箱最多同时等待 20 个任务。")
            row=WaitJob(id=uid("wait"),workspace_id=workspace.id,mailbox_id=box.id,state="waiting",
                        after_seq=payload.get("after_seq",box.last_ingest_seq),predicate=payload.get("predicate",{}),
                        created_at=now(),deadline_at=min(now()+timedelta(seconds=payload.get("timeout_seconds",120)),aware(box.expires_at)),version=1)
            session.add(row);session.flush()
            candidates=session.scalars(select(Message).where(Message.mailbox_id==box.id,Message.ingest_seq>row.after_seq).order_by(Message.ingest_seq)).all()
            match_waits(session,workspace,box,candidates)
            return wait_dict(row)
        return self.idempotent(principal,"POST/mailboxes/"+mailbox_id+"/waits",key,payload,create)

    def get_wait(self, principal, wait_id, cancel=False):
        with self.db.session.begin() as session:
            workspace=workspace_lock(session,principal["workspace_id"]) if cancel else None
            row=session.get(WaitJob,wait_id)
            if not row or row.workspace_id!=principal["workspace_id"]:
                raise DomainError(404,"resource_not_found","等待任务不存在。")
            self.require(principal,"waits:write",row.mailbox_id)
            if cancel:
                if row.state not in ("waiting","cancelled"):
                    raise DomainError(409,"wait_already_terminal","任务已结束，无法取消。")
                if row.state=="waiting":
                    row.state="cancelled";row.version+=1
                    emit(session,workspace,"wait.terminated",row.mailbox_id,wait_id=row.id,version=row.version)
            return wait_dict(row)

    def list_waits(self, principal, cursor=None, limit=50):
        self.require(principal,"waits:write",workspace_only=True)
        offset=self.decode_cursor(principal,"waits",cursor)
        with self.db.session() as session:
            query=select(WaitJob).where(WaitJob.workspace_id==principal["workspace_id"])
            if offset:
                from sqlalchemy import and_, or_
                from .providers import parse_time
                stamp,row_id=offset
                query=query.where(or_(WaitJob.created_at<parse_time(stamp),and_(WaitJob.created_at==parse_time(stamp),WaitJob.id<row_id)))
            rows=session.scalars(query.order_by(WaitJob.created_at.desc(),WaitJob.id.desc()).limit(limit+1)).all()
            more=len(rows)>limit;rows=rows[:limit]
            return {"items":[wait_dict(row) for row in rows],"has_more":more,
                    "next_cursor":self.encode_cursor(principal["workspace_id"],"waits",[iso(rows[-1].created_at),rows[-1].id]) if more else None}

    def issue_token(self, principal, mailbox_id, payload, key):
        self.require(principal,"tokens:write",mailbox_id,workspace_only=True)
        def issue(session,workspace):
            box=self.box(session,principal,mailbox_id,active=True)
            token="lm_box_"+secrets.token_urlsafe(32)
            row=Credential(id=uid("key"),workspace_id=workspace.id,token_hash=hashlib.sha256(token.encode()).hexdigest(),
                           kind="mailbox",mailbox_id=box.id,scopes=payload["scopes"],created_at=now(),
                           expires_at=min(now()+timedelta(seconds=payload.get("ttl_seconds",3600)),aware(box.expires_at)))
            session.add(row);session.flush()
            # Idempotency records never store a raw secret.
            return {"encrypted_response":self.db.encrypt({"id":row.id,"mailbox_id":box.id,"scopes":row.scopes,"created_at":iso(row.created_at),
                    "expires_at":iso(row.expires_at),"revoked_at":None,"access_token":token,"token_type":"Bearer"})}
        return self.db.decrypt(self.idempotent(principal,"POST/mailboxes/"+mailbox_id+"/tokens",key,payload,issue)["encrypted_response"])
