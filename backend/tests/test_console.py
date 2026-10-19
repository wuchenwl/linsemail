"""Integration tests against isolated schemas in the local PostgreSQL instance."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from email.message import EmailMessage
import hashlib
import json
import smtplib
import socket
import threading
import time
from types import SimpleNamespace
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url

from backend.api import create_app
from backend.config import Settings
from backend.db import ALL_SCOPES, Database
from backend.models import Credential, Event, Idempotency, Mailbox, Message, Operation, SyncJob, WaitJob, Workspace, now
from backend.providers import extract, normalize, to_text
from backend.service import Service, upsert_messages, workspace_lock
from backend.smtp import start_smtp
from backend.worker import Worker


@pytest.fixture
def ctx(tmp_path):
    settings=Settings.load()
    schema="test_"+uuid.uuid4().hex
    admin=create_engine(settings.database_url)
    with admin.begin() as connection:
        connection.execute(text("CREATE SCHEMA "+schema))
    url=make_url(settings.database_url).update_query_dict({"options":"-csearch_path="+schema})
    database=Database(Settings(tmp_path,url.render_as_string(hide_password=False)))
    database.initialize()
    credential=json.loads((tmp_path/"admin.local.json").read_text())["access_token"]
    client=TestClient(create_app(database.settings,database))
    with client:
        yield SimpleNamespace(db=database,client=client,service=Service(database),worker=Worker(database),
                              headers={"Authorization":"Bearer "+credential},token=credential)
    database.close()
    with admin.begin() as connection:
        connection.execute(text("DROP SCHEMA "+schema+" CASCADE"))
    admin.dispose()


def post(ctx,path,payload=None,key=None,headers=None):
    return ctx.client.post("/api/v1"+path,json=payload,headers=(headers or ctx.headers)|{"Idempotency-Key":key or str(uuid.uuid4())})


def box(ctx):
    response=post(ctx,"/mailboxes",{"provider_id":"local","name":"integration","ttl_seconds":3600})
    assert response.status_code==202,response.text
    ctx.worker.tick()
    result=ctx.client.get("/api/v1/mailboxes/"+response.json()["resource_id"],headers=ctx.headers)
    assert result.json()["state"]=="active",result.text
    return result.json()


def ingest(ctx,mailbox,item):
    with ctx.db.session.begin() as session:
        workspace=workspace_lock(session,"ws_local")
        return upsert_messages(session,workspace,session.get(Mailbox,mailbox["id"]),[normalize(item)])


def test_real_smtp_persists_and_matches_wait(ctx):
    mailbox=box(ctx)
    waiting=post(ctx,"/mailboxes/"+mailbox["id"]+"/waits",{"predicate":{"from_domain":"sender.example","subject_contains":"验收"},"timeout_seconds":120}).json()
    with socket.socket() as socket_:
        socket_.bind(("127.0.0.1",0))
        ctx.db.settings.smtp_port=socket_.getsockname()[1]
    receiver=start_smtp(ctx.db)
    message=EmailMessage()
    message["From"]="Linsemail QA <qa@sender.example>"
    message["To"]=mailbox["address"]
    message["Subject"]="验收来信"
    message.set_content("Your verification code is 581204\n此邮件通过本机 SMTP 真实投递。")
    raw=message.as_bytes()
    try:
        with smtplib.SMTP("127.0.0.1",ctx.db.settings.smtp_port,timeout=5) as smtp:
            assert smtp.sendmail("qa@sender.example",[mailbox["address"]],raw)=={}
            assert smtp.sendmail("qa@sender.example",[mailbox["address"]],raw)=={}
            with pytest.raises(smtplib.SMTPRecipientsRefused):
                smtp.sendmail("qa@sender.example",["nobody@external.example"],raw)
    finally:
        receiver.stop()
    ctx.worker.tick()
    page=ctx.client.get("/api/v1/mailboxes/"+mailbox["id"]+"/messages",headers=ctx.headers).json()
    assert len(page["items"])==1
    detail=ctx.service.get_message(ctx.service.principal(ctx.token),mailbox["id"],page["items"][0]["id"])
    assert detail["extractions"][0]["value"]=="581204"
    result=ctx.service.get_wait(ctx.service.principal(ctx.token),waiting["id"])
    assert result["state"]=="matched" and result["matched_message_id"]==detail["id"]


def test_concurrent_creation_is_idempotent_and_conflicts(ctx):
    payload={"provider_id":"local","name":"same","ttl_seconds":3600}
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(lambda _:post(ctx,"/mailboxes",payload,"one-key-1234"),range(2)))
    assert all(r.status_code==202 for r in responses)
    assert responses[0].json()==responses[1].json()
    with ctx.db.session() as session:
        assert session.scalar(select(func.count()).select_from(Mailbox))==1
    conflict=post(ctx,"/mailboxes",payload|{"name":"different"},"one-key-1234")
    assert conflict.status_code==409
    assert conflict.json()["code"]=="idempotency_conflict"


def test_cookie_csrf_origin_and_strict_validation(ctx):
    ctx.client.post("/api/v1/session")
    assert ctx.client.post("/api/v1/mailboxes",json={"provider_id":"local"}).status_code==403
    session=ctx.client.get("/api/v1/session").json()
    headers={"X-CSRF-Token":session["csrf_token"],"Idempotency-Key":"csrf-valid-123"}
    assert ctx.client.post("/api/v1/mailboxes",json={"provider_id":"local","surprise":True},headers=headers).status_code==422
    assert ctx.client.post("/api/v1/session",headers={"Origin":"https://untrusted.example"}).status_code==403
    assert ctx.client.get("/api/v1/mailboxes?limti=5",headers=ctx.headers).status_code==422
    assert ctx.client.get("/api/v1/mailboxes?limit=5&limit=9",headers=ctx.headers).status_code==422
    assert ctx.client.get("/api/v1/health",headers={"Host":"untrusted.example"}).status_code==400


def test_mailbox_tokens_and_cross_workspace_do_not_leak(ctx):
    first,second=box(ctx),box(ctx)
    result=post(ctx,"/mailboxes/"+first["id"]+"/tokens",{"scopes":["mailbox:read","messages:read","waits:write"],"ttl_seconds":3600}).json()
    headers={"Authorization":"Bearer "+result["access_token"]}
    assert ctx.client.get("/api/v1/mailboxes/"+first["id"],headers=headers).status_code==200
    assert ctx.client.get("/api/v1/mailboxes/"+second["id"],headers=headers).status_code==404
    assert ctx.client.get("/api/v1/mailboxes",headers=headers).status_code==403
    assert post(ctx,"/mailboxes/"+first["id"]+"/syncs",headers=headers).status_code==403
    secret="separate-workspace-token"
    with ctx.db.session.begin() as session:
        session.add(Workspace(id="ws_other",name="other"));session.flush()
        session.add(Credential(id="other",workspace_id="ws_other",token_hash=hashlib.sha256(secret.encode()).hexdigest(),kind="workspace",scopes=ALL_SCOPES))
    assert ctx.client.get("/api/v1/mailboxes/"+first["id"],headers={"Authorization":"Bearer "+secret}).status_code==404
    assert ctx.client.get("/api/v1/mailboxes",headers={"Authorization":"Bearer "+secret}).json()["items"]==[]
    ctx.client.delete("/api/v1/mailboxes/"+first["id"]+"/tokens/"+result["id"],headers=ctx.headers)
    assert ctx.client.get("/api/v1/mailboxes/"+first["id"],headers=headers).status_code==401


def test_token_replay_is_encrypted_and_expires(ctx):
    mailbox=box(ctx);path="/mailboxes/"+mailbox["id"]+"/tokens"
    payload={"scopes":["messages:read"],"ttl_seconds":60}
    first=post(ctx,path,payload,"encrypted-key-123").json()
    assert post(ctx,path,payload,"encrypted-key-123").json()==first
    with ctx.db.session.begin() as session:
        record=session.scalar(select(Idempotency).where(Idempotency.route.endswith("/tokens")))
        assert first["access_token"] not in json.dumps(record.response)
        session.get(Credential,first["id"]).expires_at=now()-timedelta(seconds=1)
    assert ctx.client.get("/api/v1/mailboxes/"+mailbox["id"]+"/messages",headers={"Authorization":"Bearer "+first["access_token"]}).status_code==401


def test_wait_uses_arrival_sequence_and_persists(ctx):
    mailbox=box(ctx)
    ingest(ctx,mailbox,{"id":"old","from":"qa@sender.example","subject":"code","text":"Your code: 123456"})
    waiting=post(ctx,"/mailboxes/"+mailbox["id"]+"/waits",{"predicate":{"subject_contains":"code"}}).json()
    assert waiting["after_seq"]==1 and waiting["state"]=="waiting"
    ingest(ctx,mailbox,{"id":"late","from":"qa@sender.example","subject":"code","date":"2000-01-01T00:00:00Z","text":"Your code: 654321"})
    reopened=Database(ctx.db.settings)
    try:
        principal=Service(reopened).principal(ctx.token)
        result=Service(reopened).get_wait(principal,waiting["id"])
        assert result["state"]=="matched"
        assert Service(reopened).get_message(principal,mailbox["id"],result["matched_message_id"])["text_body"]=="Your code: 654321"
    finally:
        reopened.close()


def test_explicit_history_wait_and_cancellation(ctx):
    mailbox=box(ctx)
    ingest(ctx,mailbox,{"id":"old","subject":"match","text":"old"})
    historical=post(ctx,"/mailboxes/"+mailbox["id"]+"/waits",{"after_seq":0,"predicate":{"subject_contains":"match"}}).json()
    assert historical["state"]=="matched"
    assert ctx.client.delete("/api/v1/waits/"+historical["id"],headers=ctx.headers).status_code==409
    pending=post(ctx,"/mailboxes/"+mailbox["id"]+"/waits",{}).json()
    assert ctx.client.delete("/api/v1/waits/"+pending["id"],headers=ctx.headers).json()["state"]=="cancelled"
    ingest(ctx,mailbox,{"id":"new","subject":"match","text":"new"})
    assert ctx.client.get("/api/v1/waits/"+pending["id"],headers=ctx.headers).json()["state"]=="cancelled"


def test_wait_timeout_and_ttl_halt_reads_and_cleanup(ctx):
    mailbox=box(ctx)
    ingest(ctx,mailbox,{"id":"test","text":"will be cleaned"})
    pending=post(ctx,"/mailboxes/"+mailbox["id"]+"/waits",{}).json()
    with ctx.db.session.begin() as session:
        session.get(WaitJob,pending["id"]).deadline_at=now()-timedelta(seconds=1)
    ctx.worker.maintain()
    assert ctx.client.get("/api/v1/waits/"+pending["id"],headers=ctx.headers).json()["state"]=="timed_out"
    with ctx.db.session.begin() as session:
        session.get(Mailbox,mailbox["id"]).expires_at=now()-timedelta(minutes=16)
    ctx.worker.maintain()
    assert ctx.client.get("/api/v1/mailboxes/"+mailbox["id"]+"/messages",headers=ctx.headers).status_code==410
    assert ctx.worker.claim_sync() is None
    with ctx.db.session() as session:
        assert session.get(Mailbox,mailbox["id"]).state=="expired"
        assert session.scalar(select(func.count()).select_from(Message))==0


def test_refresh_coalesces_and_cannot_switch_blocked_identity(ctx):
    mailbox=box(ctx);path="/mailboxes/"+mailbox["id"]+"/syncs"
    with ThreadPoolExecutor(max_workers=3) as pool:
        responses=list(pool.map(lambda _:post(ctx,path),range(3)))
    assert len({r.json()["id"] for r in responses})==1
    ctx.worker.tick()
    assert ctx.client.get("/api/v1/operations/"+responses[0].json()["id"],headers=ctx.headers).json()["state"]=="succeeded"
    with ctx.db.session.begin() as session:
        session.get(Mailbox,mailbox["id"]).sync_state="blocked"
    assert post(ctx,path).status_code==409


def test_stale_worker_cannot_commit_after_lease_replaced(ctx):
    mailbox=box(ctx)
    post(ctx,"/mailboxes/"+mailbox["id"]+"/syncs")
    task=ctx.worker.claim_sync()
    assert task is not None
    with ctx.db.session.begin() as session:
        job=session.get(SyncJob,mailbox["id"])
        job.lease_epoch+=1;job.lease_owner="new-owner"
    ctx.worker.execute_sync(task)
    with ctx.db.session() as session:
        assert session.get(SyncJob,mailbox["id"]).lease_owner=="new-owner"
        operation=session.scalar(select(Operation).where(Operation.type=="sync_mailbox"))
        assert operation.state=="queued"


def test_interrupted_creation_is_not_retried(ctx):
    operation=post(ctx,"/mailboxes",{"provider_id":"local"}).json()
    task=ctx.worker.claim_operation()
    assert task is not None
    with ctx.db.session.begin() as session:
        session.get(Operation,operation["id"]).lease_until=now()-timedelta(seconds=1)
        session.get(SyncJob,operation["resource_id"]).lease_until=now()-timedelta(seconds=1)
    ctx.worker.maintain()
    ctx.worker.execute_operation(task)
    assert ctx.worker.claim_operation() is None
    result=ctx.client.get("/api/v1/operations/"+operation["id"],headers=ctx.headers).json()
    assert result["state"]=="uncertain"
    assert ctx.client.get("/api/v1/mailboxes/"+operation["resource_id"],headers=ctx.headers).json()["address"] is None


def test_message_pagination_cursor_scope_and_dedup(ctx):
    mailbox,other=box(ctx),box(ctx)
    for index in range(3):
        ingest(ctx,mailbox,{"id":str(index),"subject":str(index),"text":"test","date":"2000-01-01T00:00:00Z"})
    ingest(ctx,mailbox,{"id":"1","subject":"1","text":"test"})
    first=ctx.client.get("/api/v1/mailboxes/"+mailbox["id"]+"/messages?limit=2",headers=ctx.headers).json()
    assert [row["ingest_seq"] for row in first["items"]]==[3,2]
    second=ctx.client.get("/api/v1/mailboxes/"+mailbox["id"]+"/messages",params={"cursor":first["next_cursor"]},headers=ctx.headers).json()
    assert [row["ingest_seq"] for row in second["items"]]==[1] and not second["has_more"]
    bad=ctx.client.get("/api/v1/mailboxes/"+other["id"]+"/messages",params={"cursor":first["next_cursor"]},headers=ctx.headers)
    assert bad.status_code==400


def test_delete_cleans_local_content_and_revokes_tokens(ctx):
    mailbox=box(ctx)
    ingest(ctx,mailbox,{"id":"test","text":"to delete"})
    token=post(ctx,"/mailboxes/"+mailbox["id"]+"/tokens",{"scopes":["messages:read"]}).json()
    pending=post(ctx,"/mailboxes/"+mailbox["id"]+"/waits",{}).json()
    operation=ctx.client.delete("/api/v1/mailboxes/"+mailbox["id"],headers=ctx.headers).json()
    assert operation["state"]=="queued"
    ctx.worker.tick()
    assert ctx.client.get("/api/v1/mailboxes",headers=ctx.headers).json()["items"]==[]
    with ctx.db.session() as session:
        assert session.scalar(select(func.count()).select_from(Message))==0
        assert session.get(WaitJob,pending["id"]).state=="cancelled"
        assert session.get(Credential,token["id"]).revoked


def test_html_is_text_only_and_extractions_are_traceable():
    value=to_text('<h1>Hello</h1><script>alert("bad")</script><img src="https://tracker.example/x"><p>Your code: 123456</p>')
    assert "alert" not in value and "tracker" not in value
    candidates,state=extract(value)
    assert state=="unique"
    span=candidates[0]["source_span"]
    assert value[span["start"]:span["end"]]=="123456"
    assert extract("Your code: 123456\nOTP: 654321")[1]=="ambiguous"
    assert extract("order 123456")[1]=="none"


def test_slow_provider_does_not_block_local_work(ctx,monkeypatch):
    entered,release=threading.Event(),threading.Event()
    def delayed_provider(mailbox,action,upstream_id=None):
        if action=="create":
            entered.set()
            assert release.wait(10)
            return {"address":"test@controlled.example","expires_at":None},None
        return [],None
    monkeypatch.setattr(ctx.worker.bridge,"run",delayed_provider)
    post(ctx,"/mailboxes",{"provider_id":"guerrillamail","name":"controlled-delay"})
    runner=threading.Thread(target=ctx.worker.run,daemon=True)
    runner.start()
    try:
        assert entered.wait(5)
        operation=post(ctx,"/mailboxes",{"provider_id":"local"}).json()
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            result=ctx.client.get("/api/v1/operations/"+operation["id"],headers=ctx.headers).json()
            if result["state"]=="succeeded":
                break
            time.sleep(.1)
        assert result["state"]=="succeeded"
        assert ctx.client.get("/api/v1/health").json()["worker_online"]
    finally:
        release.set();ctx.worker.stop_event.set();runner.join(5)
    assert not runner.is_alive()


@pytest.mark.parametrize('requested', ['my-test-', '', '   ', None, 'A' * 48])
def test_address_prefix_survives_queued_worker_reopen(ctx, monkeypatch, requested):
    import base64
    import re
    from mailbox_clients import Client
    observed = []
    def upstream(self, method, url, **kwargs):
        observed.append(url)
        if url.endswith('/domains'):
            return {'public': ['grabmail.io']}
        assert '/mailbox?' in url
        return {'messages': []}
    monkeypatch.setattr(Client, 'json', upstream)
    payload = {'provider_id': 'grabmail', 'address_prefix': requested}
    operation = post(ctx, '/mailboxes', payload).json()
    with ctx.db.session() as session:
        queued = session.get(Mailbox, operation['resource_id'])
        if (requested or '').strip():
            assert requested not in queued.vault_blob
            saved = ctx.db.decrypt(queued.vault_blob)
            assert json.loads(base64.b64decode(saved['inbox.local.json']))['address_prefix'] == requested
    # Execute after re-creating the worker, not through an in-memory request variable.
    worker = Worker(ctx.db)
    task = worker.claim_operation()
    assert task and task[0].id == operation['id']
    worker.execute_operation(task)
    mailbox = ctx.client.get('/api/v1/mailboxes/' + operation['resource_id'], headers=ctx.headers).json()
    assert mailbox['state'] == 'active', mailbox
    prefix = (requested or '').strip()
    local = mailbox['address'].split('@')[0]
    assert local.startswith(prefix) and re.fullmatch('[a-f0-9]{16}', local[len(prefix):])
    assert len(local) <= 64
    with ctx.db.session() as session:
        reopened = session.get(Mailbox, mailbox['id'])
        assert worker.bridge.run(reopened, 'list')[0] == []
        assert reopened.address == mailbox['address']
    from urllib.parse import parse_qs, urlsplit
    assert parse_qs(urlsplit(observed[-1]).query)['address'] == [mailbox['address']]


def test_address_prefix_idempotency_and_provider_capability(ctx):
    payload = {'provider_id': 'grabmail', 'address_prefix': 'custom-'}
    first = post(ctx, '/mailboxes', payload, key='prefix-idempotency')
    assert first.status_code == 202
    assert post(ctx, '/mailboxes', payload, key='prefix-idempotency').json() == first.json()
    assert post(ctx, '/mailboxes', payload | {'address_prefix': 'other-'}, key='prefix-idempotency').status_code == 409
    for provider in ['local', 'guerrillamail']:
        response = post(ctx, '/mailboxes', payload | {'provider_id': provider})
        assert response.status_code == 422
        assert response.json()['code'] == 'custom_prefix_unsupported'
    providers = ctx.client.get('/api/v1/providers', headers=ctx.headers).json()['items']
    assert next(p for p in providers if p['id'] == 'grabmail')['capabilities']['custom_prefix'] == 'supported'
    for invalid in ['a@b', 'a' * 49, 'a/b', 123]:
        response = post(ctx, '/mailboxes', payload | {'address_prefix': invalid})
        assert response.status_code == 422
