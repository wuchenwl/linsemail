"""Transactional outbox with signed, bounded, DNS-pinned deliveries."""
from datetime import timedelta
import hashlib
import hmac
import http.client
import ipaddress
import json
import secrets
import socket
import time
from urllib.parse import urlsplit
from sqlalchemy import or_, select
from .models import WebhookSubscription, WebhookDelivery, now, uid
from .service import DomainError, aware, iso, workspace_lock

EVENTS = frozenset({'message.received','message.updated','wait.matched','wait.terminated',
                   'mailbox.expired','operation.completed'})


def target(url, allow_loopback=False, resolve=True):
    try:
        parts=urlsplit(url)
        if len(url)>2048 or any(ord(c)<32 for c in url) or parts.scheme not in ('https','http') or not parts.hostname or parts.username or parts.password or parts.fragment:
            raise ValueError()
        port=parts.port or (443 if parts.scheme=='https' else 80)
        if not resolve:
            return parts, port, []
        addresses=list(dict.fromkeys(x[4][0] for x in socket.getaddrinfo(parts.hostname,port,type=socket.SOCK_STREAM)))
        if not addresses:
            raise ValueError()
        ips=[ipaddress.ip_address(x) for x in addresses]
        if any(not (ip.is_global or (allow_loopback and ip.is_loopback)) for ip in ips):
            raise ValueError()
        if parts.scheme=='http' and not (allow_loopback and all(ip.is_loopback for ip in ips)):
            raise ValueError()
        return parts,port,addresses
    except (ValueError, OSError):
        raise DomainError(422,'invalid_webhook_url','Webhook 须使用公网 HTTPS；回环 HTTP 仅在明确启用本机测试配置后可用。') from None


def send(url, body, headers, allow_loopback=False):
    parts,port,addresses=target(url,allow_loopback)
    address=addresses[0]
    class PinnedHTTPS(http.client.HTTPSConnection):
        def connect(self):
            self.sock=socket.create_connection((address,port),timeout=self.timeout)
            self.sock=self._context.wrap_socket(self.sock,server_hostname=self.host)
    class PinnedHTTP(http.client.HTTPConnection):
        def connect(self):
            self.sock=socket.create_connection((address,port),timeout=self.timeout)
    cls= PinnedHTTPS if parts.scheme=='https' else PinnedHTTP
    connection=cls(parts.hostname,port,timeout=5)
    try:
        connection.request('POST',(parts.path or '/')+('?' + parts.query if parts.query else ''),body,headers)
        response=connection.getresponse()
        response.read(4096)
        return response.status
    finally:
        connection.close()


def subscription_dict(row):
    return {'id':row.id,'url':row.url,'events':row.events,'active':row.active,'created_at':iso(row.created_at)}


def enqueue(session,event):
    # Called inside the same transaction as the event, never after commit.
    for sub in session.scalars(select(WebhookSubscription).where(
            WebhookSubscription.workspace_id==event.workspace_id,WebhookSubscription.active.is_(True))):
        if event.type not in sub.events:
            continue
        session.add(WebhookDelivery(id=uid('whd'),subscription_id=sub.id,event_id=event.id,
            payload={'id':event.id,'type':event.type,'workspace_id':event.workspace_id,
                     'seq':event.seq,'mailbox_id':event.mailbox_id,'message_id':event.message_id,
                     'wait_id':event.wait_id,'resource_version':event.resource_version,'occurred_at':iso(event.occurred_at or now())}))


class Webhooks:
    def __init__(self,service):
        self.service=service
        self.db=service.db

    def create(self,principal,payload,key):
        self.service.require(principal,'mailbox:write',workspace_only=True)
        target(payload['url'],self.db.settings.webhook_allow_loopback)
        if not payload['events'] or not set(payload['events'])<=EVENTS:
            raise DomainError(422,'invalid_webhook_events','Webhook 事件类型不受支持。')
        def create(session,workspace):
            count=len(session.scalars(select(WebhookSubscription.id).where(WebhookSubscription.workspace_id==workspace.id,WebhookSubscription.active.is_(True))).all())
            if count>=20:
                raise DomainError(429,'webhook_limit','工作区最多启用 20 个 Webhook。')
            row=WebhookSubscription(id=uid('wh'),workspace_id=workspace.id,url=payload['url'],
                events=sorted(set(payload['events'])),vault_blob=self.db.encrypt({'secret':secrets.token_urlsafe(32)}))
            session.add(row);session.flush()
            return subscription_dict(row)
        result=self.service.idempotent(principal,'POST/webhooks',key,payload,create)
        with self.db.session() as session:
            row=session.get(WebhookSubscription,result['id'])
            return result|{'secret':self.db.decrypt(row.vault_blob)['secret']}

    def list(self,principal):
        self.service.require(principal,'events:read',workspace_only=True)
        with self.db.session() as session:
            return {'items':[subscription_dict(x) for x in session.scalars(select(WebhookSubscription).where(
                WebhookSubscription.workspace_id==principal['workspace_id']).order_by(WebhookSubscription.created_at.desc()).limit(100))]}

    def remove(self,principal,subscription_id):
        self.service.require(principal,'mailbox:write',workspace_only=True)
        with self.db.session.begin() as session:
            workspace_lock(session,principal['workspace_id'])
            row=session.get(WebhookSubscription,subscription_id)
            if not row or row.workspace_id!=principal['workspace_id']:
                raise DomainError(404,'resource_not_found','Webhook 不存在。')
            row.active=False
            return subscription_dict(row)

    def deliveries(self,principal):
        self.service.require(principal,'events:read',workspace_only=True)
        with self.db.session() as session:
            rows=session.scalars(select(WebhookDelivery).join(WebhookSubscription).where(
                WebhookSubscription.workspace_id==principal['workspace_id']).order_by(WebhookDelivery.created_at.desc()).limit(100)).all()
            return {'items':[{k:getattr(x,k) for k in ('id','subscription_id','event_id','state','attempts','status_code','error_code')}
                |{'next_due_at':iso(x.next_due_at),'created_at':iso(x.created_at)} for x in rows]}

    def claim(self):
        with self.db.session.begin() as session:
            row=session.scalar(select(WebhookDelivery).where(WebhookDelivery.state.in_(['pending','running']),
                WebhookDelivery.next_due_at<=now(),or_(WebhookDelivery.lease_until.is_(None),WebhookDelivery.lease_until<now()))
                .order_by(WebhookDelivery.next_due_at).with_for_update(skip_locked=True).limit(1))
            if not row:
                return None
            if row.attempts>=6:
                row.state='failed';row.error_code='attempts_exhausted';row.lease_until=None
                return None
            row.state='running';row.attempts+=1;row.lease_epoch+=1;row.lease_until=now()+timedelta(seconds=30)
            session.flush()
            return row

    def execute(self,row):
        with self.db.session() as session:
            current=session.get(WebhookDelivery,row.id)
            if current.state!='running' or current.lease_epoch!=row.lease_epoch or not current.lease_until or aware(current.lease_until)<=now():
                return
            subscription=session.get(WebhookSubscription,row.subscription_id)
        status,error=None,None
        if not subscription.active:
            error='subscription_disabled'
        else:
            body=json.dumps(row.payload,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
            stamp=str(int(time.time()))
            signature=hmac.new(self.db.decrypt(subscription.vault_blob)['secret'].encode(),stamp.encode()+b'.'+body,hashlib.sha256).hexdigest()
            headers={'Content-Type':'application/json','User-Agent':'Linsemail/0.4.0',
                     'X-Linsemail-Event-ID':row.event_id,'X-Linsemail-Delivery-ID':row.id,
                     'X-Linsemail-Signature':f't={stamp},v1={signature}'}
            try:
                status=send(subscription.url,body,headers,self.db.settings.webhook_allow_loopback)
                if not 200<=status<300:
                    error='http_status'
            except Exception as exc:
                error=getattr(exc,'code','delivery_failed')
        with self.db.session.begin() as session:
            current=session.get(WebhookDelivery,row.id)
            if current.lease_epoch!=row.lease_epoch or current.state!='running' or not current.lease_until or aware(current.lease_until)<=now():
                return
            current.status_code=status;current.error_code=error;current.lease_until=None
            current.state='succeeded' if error is None else 'failed' if row.attempts>=6 or error=='subscription_disabled' else 'pending'
            current.next_due_at=now()+timedelta(seconds=min(900,5*2**min(row.attempts,8)))
