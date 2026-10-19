"""Real PostgreSQL recovery, HTTP webhook and MCP protocol integration coverage."""
from datetime import timedelta
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import socket
import subprocess
import sys
import threading
import uuid

import pytest
from sqlalchemy import func, select
import uvicorn

from backend.tests.test_console import ctx, post, box, ingest
from backend.automation import Automation
from backend.intelligence import observe, snapshot
from backend.models import AutomationTask, Credential, Mailbox, Operation, ProviderObservation, WaitJob, WebhookDelivery, now
from backend.service import DomainError, Service
from backend.worker import Worker
from backend.webhooks import target


def due(ctx):
    with ctx.db.session.begin() as session:
        for row in session.scalars(select(AutomationTask)):
            row.next_due_at=now()-timedelta(seconds=1)
        for row in session.scalars(select(WebhookDelivery)):
            row.next_due_at=now()-timedelta(seconds=1)


def test_intelligence_separates_requests_delivery_and_cancelled_waits(ctx):
    empty=snapshot(ctx.db,['mailtm'])['mailtm']
    assert empty['health']=='unknown' and empty['delivery_success_rate'] is None
    with ctx.db.session.begin() as session:
        for latency in (10,20,30,40,100):
            observe(session,'mailtm','list',True,latency)
    metric=snapshot(ctx.db,['mailtm'])['mailtm']
    assert metric['request_success_rate']==1 and metric['latency_p95_ms']==100
    mailbox=box(ctx)
    waiting=post(ctx,'/mailboxes/'+mailbox['id']+'/waits',{'predicate':{'subject_contains':'receipt'}}).json()
    ingest(ctx,mailbox,{'id':'receipt','from':'sender@example.com','subject':'receipt','text':'OTP 123456'})
    assert ctx.service.get_wait(ctx.service.principal(ctx.token),waiting['id'])['state']=='matched'
    cancelled=post(ctx,'/mailboxes/'+mailbox['id']+'/waits',{}).json()
    ctx.client.delete('/api/v1/waits/'+cancelled['id'],headers=ctx.headers)
    metric=snapshot(ctx.db,['local'])['local']
    assert metric['delivery_samples']==1 and metric['delivery_success_rate']==1


def test_smart_routing_replay_preserves_selection_and_circuit_opens(ctx,monkeypatch):
    service=ctx.client.app.state.service
    original=service.list_providers
    monkeypatch.setattr(service,'list_providers',lambda p:[x for x in original(p) if x['id'] in ('local','mailtm','grabmail','agentmail')])
    with ctx.db.session.begin() as session:
        for _ in range(3):
            observe(session,'grabmail','create',False,800,'unavailable')
        for _ in range(6):
            observe(session,'mailtm','list',True,10)
    preview=ctx.client.get('/api/v1/routing/preview',headers=ctx.headers).json()
    assert preview['selected_provider_id']=='mailtm'
    assert [x['provider_id'] for x in preview['candidates']]==['mailtm']
    first=post(ctx,'/mailboxes',{},'auto-route-test-key')
    assert first.status_code==202,first.text
    with ctx.db.session.begin() as session:
        assert session.get(Mailbox,first.json()['resource_id']).provider_id=='mailtm'
        for _ in range(3):
            observe(session,'mailtm','list',False,100,'offline')
    assert post(ctx,'/mailboxes',{},'auto-route-test-key').json()==first.json()
    assert post(ctx,'/mailboxes',{}).status_code==503
    assert snapshot(ctx.db,['mailtm'])['mailtm']['health']=='cooldown'


def test_durable_workflow_recovers_restart_and_matches(ctx):
    response=post(ctx,'/workflows',{'provider_id':'local','predicate':{'subject_contains':'workflow'}} ,'workflow-idempotency')
    assert response.status_code==202,response.text
    task_id=response.json()['id']
    assert post(ctx,'/workflows',{'provider_id':'local','predicate':{'subject_contains':'workflow'}},'workflow-idempotency').json()==response.json()
    first=ctx.worker.automation.claim()
    ctx.worker.automation.execute(first)
    with ctx.db.session() as session:
        saved=session.get(AutomationTask,task_id)
        box_id=saved.result['mailbox_id']
    restarted=Worker(ctx.db)
    for _ in range(3):
        due(ctx);restarted.tick()
    mailbox=ctx.service.get_mailbox(ctx.service.principal(ctx.token),box_id)
    ingest(ctx,mailbox,{'id':'workflow-mail','from':'qa@example.com','subject':'workflow','text':'Your code: 654321'})
    due(ctx);Worker(ctx.db).tick()
    task=ctx.client.get('/api/v1/tasks/'+task_id,headers=ctx.headers).json()
    assert task['state']=='matched' and task['result']['matched_message_id']
    with ctx.db.session() as session:
        assert session.scalar(select(func.count()).select_from(Mailbox))==1
    assert ctx.client.get('/api/v1/tasks/'+task_id).status_code==401


def test_task_cancellation_timeout_and_stale_lease(ctx):
    first=post(ctx,'/workflows',{'provider_id':'local'}).json()
    claimed=ctx.worker.automation.claim()
    ctx.client.delete('/api/v1/tasks/'+first['id'],headers=ctx.headers)
    ctx.worker.automation.execute(claimed)
    assert ctx.client.get('/api/v1/tasks/'+first['id'],headers=ctx.headers).json()['state']=='cancelled'
    with ctx.db.session() as session:
        assert session.scalar(select(func.count()).select_from(Mailbox))==0
    second=post(ctx,'/workflows',{'provider_id':'local'}).json()
    with ctx.db.session.begin() as session:
        session.get(AutomationTask,second['id']).deadline_at=now()-timedelta(seconds=1)
    due(ctx);ctx.worker.tick()
    assert ctx.client.get('/api/v1/tasks/'+second['id'],headers=ctx.headers).json()['state']=='timed_out'


def test_probe_scheduler_is_bounded_and_manual_probe_runs(ctx):
    ctx.worker.automation.schedule_probe();ctx.worker.automation.schedule_probe()
    with ctx.db.session() as session:
        scheduled=session.scalars(select(AutomationTask)).all()
        assert len(scheduled)==1 and scheduled[0].payload['provider_id']!='local'
    # Cancel the scheduled external probe; this test exercises actual local SMTP capture only.
    ctx.client.delete('/api/v1/tasks/'+scheduled[0].id,headers=ctx.headers)
    response=post(ctx,'/providers/local/probes',None)
    assert response.status_code==202,response.text
    for _ in range(5):
        due(ctx);ctx.worker.tick()
    result=ctx.client.get('/api/v1/tasks/'+response.json()['id'],headers=ctx.headers).json()
    assert result['state']=='succeeded' and result['result']['delivery_tested'] is False


def test_real_webhook_signed_delivery_retries_without_body_leak(ctx):
    ctx.db.settings.webhook_allow_loopback=True
    received=[]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body=self.rfile.read(int(self.headers['Content-Length']))
            received.append((dict(self.headers),body))
            self.send_response(500 if len(received)==1 else 204);self.end_headers()
        def log_message(self,*args): pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        payload={'url':f'http://127.0.0.1:{server.server_port}/hook','events':['message.received']}
        response=post(ctx,'/webhooks',payload,'webhook-idempotent-key')
        assert response.status_code==201,response.text
        sub=response.json()
        assert post(ctx,'/webhooks',payload,'webhook-idempotent-key').json()==sub
        mailbox=box(ctx)
        ingest(ctx,mailbox,{'id':'wh-mail','subject':'private','text':'PRIVATE_MAIL_CONTENT'})
        delivery=ctx.worker.webhooks.claim();ctx.worker.webhooks.execute(delivery)
        with ctx.db.session() as session:
            assert session.get(WebhookDelivery,delivery.id).state=='pending'
        due(ctx);Worker(ctx.db).webhooks.execute(ctx.worker.webhooks.claim())
        assert len(received)==2
        for headers,body in received:
            values=dict(x.split('=',1) for x in headers['X-Linsemail-Signature'].split(','))
            expected=hmac.new(sub['secret'].encode(),values['t'].encode()+b'.'+body,hashlib.sha256).hexdigest()
            assert hmac.compare_digest(expected,values['v1'])
            assert b'PRIVATE_MAIL_CONTENT' not in body
        assert received[0][0]['X-Linsemail-Event-ID']==received[1][0]['X-Linsemail-Event-ID']
        assert ctx.client.get('/api/v1/webhook-deliveries',headers=ctx.headers).json()['items'][0]['state']=='succeeded'
        assert 'secret' not in ctx.client.get('/api/v1/webhooks',headers=ctx.headers).json()['items'][0]
        assert ctx.client.delete('/api/v1/webhooks/'+sub['id'],headers=ctx.headers).json()['active'] is False
        with ctx.db.engine.connect() as connection:
            # The issued secret is encrypted at rest, including idempotency replay metadata.
            from sqlalchemy import text
            assert sub['secret'] not in str(connection.execute(text('SELECT response FROM idempotency')).all())
    finally:
        server.shutdown();server.server_close();thread.join(2)


@pytest.mark.parametrize('url',['http://127.0.0.1/hook','https://127.0.0.1/hook','https://169.254.169.254/metadata',
    'https://10.0.0.1/hook','https://user:pass@example.com/hook','file:///tmp/file'])
def test_webhook_rejects_private_targets(url):
    with pytest.raises(DomainError): target(url)


def test_mcp_stdio_calls_real_local_api(ctx):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(ctx.client.app,host='127.0.0.1',port=port,log_level='error',lifespan='off'))
    thread=threading.Thread(target=server.run,daemon=True);thread.start()
    import time
    deadline=time.monotonic()+5
    while not server.started and time.monotonic()<deadline: time.sleep(.02)
    assert server.started
    requests=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18'}},
        {'jsonrpc':'2.0','method':'notifications/initialized'},
        {'jsonrpc':'2.0','id':2,'method':'tools/list'},
        {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'create_workflow','arguments':{'provider_id':'local','idempotency_key':'mcp-e2e-workflow'}}},
        {'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'provider_intelligence'}},
        {'jsonrpc':'2.0','id':5,'method':'missing'}]
    env=dict(os.environ,LINSEMAIL_API_URL=f'http://127.0.0.1:{port}/api/v1',LINSEMAIL_API_TOKEN=ctx.token)
    try:
        result=subprocess.run([sys.executable,'-m','backend.mcp'],input='\n'.join(json.dumps(x) for x in requests)+'\n',
            capture_output=True,text=True,env=env,timeout=20)
        assert result.returncode==0,result.stderr
        replies=[json.loads(line) for line in result.stdout.splitlines()]
        assert len(replies)==5
        assert replies[0]['result']['serverInfo']['name']=='linsemail'
        assert len(replies[1]['result']['tools'])==11
        task=replies[2]['result']['structuredContent']
        assert task['kind']=='workflow' and not replies[2]['result']['isError']
        assert replies[3]['result']['structuredContent']['items']
        assert replies[4]['error']['code']==-32601
    finally:
        server.should_exit=True;thread.join(5)


def test_new_resources_enforce_workspace_and_scope(ctx):
    from backend.db import ALL_SCOPES
    from backend.models import Workspace
    task=post(ctx,'/workflows',{'provider_id':'local'}).json()
    mailbox=box(ctx)
    issued=post(ctx,'/mailboxes/'+mailbox['id']+'/tokens',{'scopes':['mailbox:read']}).json()
    limited={'Authorization':'Bearer '+issued['access_token']}
    assert ctx.client.get('/api/v1/tasks/'+task['id'],headers=limited).status_code==403
    token='test-other-'+uuid.uuid4().hex
    with ctx.db.session.begin() as session:
        session.add(Workspace(id='ws_other',name='Other'));session.flush()
        session.add(Credential(id='other_key',workspace_id='ws_other',kind='workspace',scopes=ALL_SCOPES,token_hash=hashlib.sha256(token.encode()).hexdigest()))
    other={'Authorization':'Bearer '+token}
    assert ctx.client.get('/api/v1/tasks/'+task['id'],headers=other).status_code==404
    assert ctx.client.get('/api/v1/tasks',headers=other).json()['items']==[]
    for path in ('/tasks','/webhooks','/webhook-deliveries','/routing/preview','/providers/intelligence'):
        assert ctx.client.get('/api/v1'+path).status_code==401


def test_step_replay_after_crash_does_not_duplicate_mailbox(ctx):
    task=post(ctx,'/workflows',{'provider_id':'local'}).json()
    with ctx.db.session() as session:
        row=session.get(AutomationTask,task['id'])
        payload={k:row.payload[k] for k in ('provider_id','name','address_prefix','ttl_seconds','labels') if k in row.payload}
    principal=ctx.service.principal(ctx.token)
    original=ctx.service.create_mailbox(principal,payload,task['id']+':create')
    restarted=Worker(ctx.db)
    restarted.automation.execute(restarted.automation.claim())
    with ctx.db.session() as session:
        saved=session.get(AutomationTask,task['id'])
        assert saved.result['operation_id']==original['id']
        assert session.scalar(select(func.count()).select_from(Mailbox))==1


def test_workflow_total_deadline_records_failure_and_preserves_prior_match(ctx):
    for should_match in (False,True):
        task=post(ctx,'/workflows',{'provider_id':'local','predicate':{'subject_contains':'deadline'}}).json()
        for _ in range(4):
            due(ctx);ctx.worker.tick()
        with ctx.db.session() as session:
            row=session.get(AutomationTask,task['id']);box_id=row.result['mailbox_id'];wait_id=row.result['wait_id']
        if should_match:
            mailbox=ctx.service.get_mailbox(ctx.service.principal(ctx.token),box_id)
            ingest(ctx,mailbox,{'id':'deadline-message','subject':'deadline','text':'hello'})
        with ctx.db.session.begin() as session:
            session.get(AutomationTask,task['id']).deadline_at=now()-timedelta(seconds=1)
        due(ctx);ctx.worker.automation.execute(ctx.worker.automation.claim())
        result=ctx.client.get('/api/v1/tasks/'+task['id'],headers=ctx.headers).json()
        assert result['state']==('matched' if should_match else 'timed_out')
    metric=snapshot(ctx.db,['local'])['local']
    assert metric['delivery_samples']==2 and metric['delivery_success_rate']==.5


def test_webhook_fences_stale_worker_and_bounds_retries(ctx,monkeypatch):
    ctx.db.settings.webhook_allow_loopback=True
    sub=post(ctx,'/webhooks',{'url':'http://127.0.0.1:12345/hook','events':['message.received']}).json()
    mailbox=box(ctx)
    ingest(ctx,mailbox,{'id':'lease-message','text':'body'})
    old=ctx.worker.webhooks.claim()
    with ctx.db.session.begin() as session:
        session.get(WebhookDelivery,old.id).lease_until=now()-timedelta(seconds=1)
    fresh=Worker(ctx.db).webhooks.claim()
    sent=[]
    monkeypatch.setattr('backend.webhooks.send',lambda *args:sent.append(args) or 204)
    ctx.worker.webhooks.execute(fresh);ctx.worker.webhooks.execute(old)
    assert len(sent)==1
    ingest(ctx,mailbox,{'id':'retry-message','text':'body'})
    with ctx.db.session.begin() as session:
        row=session.scalar(select(WebhookDelivery).where(WebhookDelivery.state=='pending'));row.attempts=5
    monkeypatch.setattr('backend.webhooks.send',lambda *args:503)
    final=ctx.worker.webhooks.claim();ctx.worker.webhooks.execute(final)
    with ctx.db.session() as session:
        assert session.get(WebhookDelivery,final.id).state=='failed'
        assert session.get(WebhookDelivery,final.id).attempts==6
