"""Snapshot boundaries, nonempty bodies, recovery failures and reboot isolation."""
import json
import time

import httpx
import pytest

from backend.expansion_clients import ExpansionClient
from mailbox_clients import ProviderError


def socket_server(monkeypatch, *, fault=None, empty=False):
    native=httpx.Client
    requests=[]
    queues={}
    state={'generation':'boot-a','token':'private-recovery','address':'dedicated@tempmail.cc','connections':0,'claims':0}
    def event(*args):return '42'+json.dumps(list(args),separators=(',',':'))
    def handler(request):
        requests.append(request)
        sid=request.url.params.get('sid')
        if sid is None:
            state['connections']+=1;sid='sid-'+str(state['connections']);queues[sid]=[]
            return httpx.Response(200,text='0'+json.dumps({'sid':sid,'pingInterval':25000,'pingTimeout':20000}))
        if request.method=='GET':
            assert queues[sid], 'Client must stop at the acknowledged snapshot boundary'
            return httpx.Response(200,text=queues[sid].pop(0))
        packet=request.content.decode()
        if packet=='40':queues[sid].append('40{"sid":"namespace1"}')
        elif packet.startswith('42'):
            value=json.loads(packet[2:]);state['claims']+=1
            creating=value[0]=='request shortid'
            assert value[0] in ('request shortid','recover')
            if not creating:assert value[1]==state['token']
            if not creating and fault=='expired':
                queues[sid].append(event('recovery rejected',{'message':'expired'}))
            else:
                boot='boot-b' if fault=='reboot' and not creating else state['generation']
                address='foreign' if fault=='identity' and not creating else 'dedicated'
                mailbox={'_id':1,'headers':{'from':'test@sender.example','to':state['address'],'subject':'Fixture','date':'2026-10-19T08:00:00Z'},'recipients':[state['address']],'text':'Your code: 654321','html':None}
                if fault=='foreign_message' and not creating:mailbox['recipients']=['other@tempmail.cc']
                rows=[] if empty else [mailbox]
                if fault=='limit' and not creating:rows=[{**mailbox,'_id':i+1} for i in range(20)]
                packets=[event('boot',{'id':boot})]
                if rows:packets.append(event('mail history',rows))
                packets.append(event('shortid',address))
                if not (fault=='missing_boundary' and not creating):
                    packets.append(event('recovery',{'token':state['token'],'expiresAt':int(time.time()*1000)+1800000}))
                # Split one claim across polls, including Engine.IO heartbeat.
                queues[sid].extend(['2',packets[0],'\x1e'.join(packets[1:])])
                if fault=='missing_boundary' and not creating:queues[sid].extend(['6']*20)
        elif packet=='3':pass
        else:assert packet=='41\x1e1'
        return httpx.Response(200,text='ok')
    monkeypatch.setattr('backend.socket_clients.httpx.Client',lambda **kw:native(transport=httpx.MockTransport(handler),**kw))
    return state,requests


def test_nonempty_receive_reconnect_and_cached_body(tmp_path,monkeypatch):
    state,requests=socket_server(monkeypatch)
    client=ExpansionClient(tmp_path);client.create('tempmail_cc')
    assert client.config['address']==state['address']
    assert client.config['recovery_token']==state['token']
    client=ExpansionClient(tmp_path)
    messages=client.list_messages();assert [m['id'] for m in messages]==['boot-a:1']
    before=len(requests)
    assert client.get_message('boot-a:1')['text']=='Your code: 654321'
    with pytest.raises(ProviderError) as error:client.get_message('boot-b:1')
    assert error.value.code=='message_not_in_mailbox' and len(requests)==before
    claims=[json.loads(r.content[2:]) for r in requests if r.method=='POST' and r.content.startswith(b'42')]
    assert [c[0] for c in claims]==['request shortid','recover']
    assert sum(r.content==b'41\x1e1' for r in requests)==2


def test_acknowledged_empty_snapshot_is_not_a_poll_timeout(tmp_path,monkeypatch):
    state,requests=socket_server(monkeypatch,empty=True)
    client=ExpansionClient(tmp_path);client.create('tempmail_cc')
    assert client.list_messages()==[] and state['claims']==2


@pytest.mark.parametrize('fault,code',[
    ('expired','mailbox_session_lost'),('reboot','mailbox_generation_changed'),
    ('identity','mailbox_identity_changed'),('foreign_message','mailbox_identity_changed'),
    ('limit','incomplete_mailbox_observation'),('missing_boundary','incomplete_mailbox_observation'),
])
def test_invalid_recovery_never_replaces_or_overwrites_mailbox(fault,code,tmp_path,monkeypatch):
    state,requests=socket_server(monkeypatch,fault=fault)
    client=ExpansionClient(tmp_path);client.create('tempmail_cc')
    config=(tmp_path/'inbox.local.json').read_bytes();messages=(tmp_path/'messages.local.json').read_bytes()
    client=ExpansionClient(tmp_path)
    with pytest.raises(ProviderError) as error:client.list_messages()
    assert error.value.code==code
    assert (tmp_path/'inbox.local.json').read_bytes()==config and (tmp_path/'messages.local.json').read_bytes()==messages
    assert state['claims']==2


def test_malformed_handshake_and_http_error_are_not_empty(tmp_path,monkeypatch):
    native=httpx.Client
    for reply in [httpx.Response(403),httpx.Response(200,text='<html>Challenge</html>'),httpx.Response(200,text='0{}')]:
        monkeypatch.setattr('backend.socket_clients.httpx.Client',lambda **kw:native(transport=httpx.MockTransport(lambda request:reply),**kw))
        with pytest.raises(ProviderError):ExpansionClient(tmp_path).create('tempmail_cc')
        assert not (tmp_path/'inbox.local.json').exists()
