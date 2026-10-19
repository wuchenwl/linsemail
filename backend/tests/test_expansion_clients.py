"""Nonempty receive contracts, session boundaries, and hostile upstream replies."""
import json

import httpx
import pytest

from backend.expansion_clients import EXPANSION_FIELDS, HTML_BASES, ExpansionClient, raw_message
from backend.discovery_clients import DISCOVERY_FIELDS
from backend.continuation_clients import CONTINUATION_FIELDS
from backend.platform_clients import PLATFORM_FIELDS
from backend.providers import normalize
from backend.tests.test_console import ctx
from mailbox_clients import ProviderError


def transport(provider, calls, *, broken=None):
    state = {"address": "dedicated@receiver.example"}
    def handler(request):
        calls.append(request)
        path = request.url.path
        address = state["address"]
        row = {"id": "m1", "from": "test@sender.example", "subject": "Dedicated contract inbox"}
        detail = {**row, "text": "Your code: 654321"}
        key = request.headers.get("X-API-Key")
        if provider in {"chat_tempmail", "smailpro", "mailslurp", "temporaryemail_ai"}:
            assert key == "fixture-key"
        elif provider == "mailtd" and not path.endswith("/domains"):
            assert request.headers["Authorization"] == "Bearer fixture-key"
        elif provider == "mailinator":
            assert request.headers["Authorization"] == "fixture-key"
            assert "*" not in path
        elif provider == "inboxes":
            assert request.headers["apikey"] == "fixture-key"
        elif provider == 'mailcare':
            assert request.headers['Authorization'] == 'Bearer fixture-key'
        elif provider == 'mailtrap_inbound':
            assert request.headers['Api-Token'] == 'fixture-key'
        if provider == "chat_tempmail":
            if path.endswith("/domains"):
                result = {"domains": ["receiver.example"]}
            elif path.endswith("/generate"):
                data = json.loads(request.content)
                assert data["expiryTime"] == 3600000
                state["address"] = data["name"] + "@" + data["domain"]
                result = {"id": "inbox1", "email": state["address"]}
            elif path.endswith("/inbox1"):
                result = {"messages": [{**row, "received_at": 1700000000000}], "total": 1}
            else:
                result = {**row, "emailId": "inbox1", "to_address": address, "content": detail["text"]}
        elif provider == "smailpro":
            if path.endswith("/create"):
                assert request.url.params["email"] == "random@random.com"
                result = {"email": address, "expired_at": 1700003000, "action": "created"}
            elif path.endswith("/inbox"):
                result = {"messages": [{"mid": "m1", "textTo": address, "textFrom": row["from"], "textSubject": row["subject"], "textDate": "2026-10-19T08:00:00Z"}]}
            else:
                assert request.url.params["email"] == address and request.url.params["mid"] == "m1"
                result = {"body": "<p>" + detail["text"] + "</p>"}
        elif provider == "mailtd":
            if path.endswith("/domains"):
                result = {"domains": [{"id": "domain1", "domain": "receiver.example", "default": True}]}
            elif path.endswith("/accounts"):
                data = json.loads(request.content)
                state["address"] = data["address"]
                assert len(data["password"]) >= 24
                result = {"id": "inbox1", "address": state["address"]}
            elif path.endswith("/messages"):
                result = {"messages": [row], "page": int(request.url.params["page"])}
            else:
                result = {**row, "address": address, "text_body": detail["text"], "html_body": None}
        elif provider == "mailslurp":
            if path.endswith("/inboxes"):
                assert request.url.params["expiresIn"] == "3600000"
                result = {"id": "inbox1", "emailAddress": address}
            elif path.endswith("/paginated"):
                result = {"content": [row], "number": int(request.url.params["page"]), "last": True}
            else:
                result = {**row, "inboxId": "inbox1", "body": detail["text"], "isHTML": False}
        elif provider == "inboxes":
            if path.endswith("/domains"):
                result = [{"qdn": "receiver.example"}]
            elif request.method == "POST":
                state["address"] = path.rsplit("/", 1)[1]
                result = {"created": True}
            elif "/inboxes/" in path:
                result = [{**row, "uid": "m1", "to": address}]
            else:
                result = {**detail, "uid": "m1", "to": address}
        elif provider == "mailinator":
            if "/messages/" not in path:
                state["address"] = path.rsplit("/", 1)[1] + "@receiver.example"
                result = {"msgs": [{**row, "to": state["address"].split("@")[0], "time": 1700000000000}]}
            else:
                result = {"text": detail["text"]} if path.endswith("/text") else detail
        elif provider == "maildax":
            result = {"data": {"email": address, "secret": "mailbox-secret"}} if request.method == "POST" else {"data": [{**row, "_id": "m1"}]} if path.endswith("/mails") else {"data": {**detail, "_id": "m1"}}
            if request.method != "POST":
                assert request.url.params["secret"] == "mailbox-secret"
        elif provider == "crazymailing":
            if path.endswith("/mailbox"):
                result = {"mailbox": {"id": "inbox1", "address": address, "expiresAt": "2026-10-19T09:00:00Z"}}
            elif path.endswith("/messages"):
                assert request.url.params["mailbox"] == "inbox1"
                result = {"messages": [row]}
            else:
                assert "session=owned" in request.headers["Cookie"]
                return httpx.Response(200, text="<p>" + detail["text"] + "</p>")
        elif provider == 'temporaryemail_ai':
            if path == '/temp-mail':
                data=json.loads(request.content)
                assert data['suffix'].startswith('ls') and data['acceptAttachments'] is False
                result={'email':address,'token':'mailbox-token','expiresAtUtc':'2026-10-20T08:00:00Z'}
            elif '/token/' in path:
                result={'id':'inbox1','virtualEmail':address,'expiresAt':'2026-10-20T08:00:00Z'}
            else:
                result={'messages':[{**row,'to':address,'body':'<p>'+detail['text']+'</p>','receivedAtUtc':'2026-10-19T08:00:00Z'}]}
        elif provider == 'cloudflare_temp_email':
            if path.endswith('/new_address'):
                data = json.loads(request.content)
                state['address'] = data['name'] + '@' + data['domain']
                result = {'address': state['address'], 'jwt': 'mailbox-jwt'}
            else:
                assert request.headers['Authorization'] == 'Bearer mailbox-jwt'
                raw = 'From: test@sender.example\r\nTo: '+address+'\r\nSubject: Fixture\r\n\r\nYour code: 654321'
                message = {**row, 'address': address, 'raw': raw}
                result = {'address': address} if path.endswith('/settings') else {'results': [message], 'count': 1} if path.endswith('/mails') else message
        elif provider == '1secmail':
            params = request.url.params
            if params['action'] == 'genRandomMailbox':
                result = [address]
            else:
                assert params['login'] + '@' + params['domain'] == address
                result = [row] if params['action'] == 'getMessages' else {**row, 'textBody': detail['text'], 'htmlBody': None}
        elif provider == 'mailcare':
            if '/emails/' not in path:
                assert request.url.params['inbox'].split('@')[1] == 'receiver.example'
                state['address'] = request.url.params['inbox']
                result = {'data': [{**row, 'inbox': {'email': state['address']}, 'sender': {'email': row['from']}}],
                          'meta': {'current_page': 1, 'last_page': 1}}
            elif request.headers['Accept'] == 'message/rfc2822':
                return httpx.Response(200, text='From: test@sender.example\r\nTo: '+address+'\r\nSubject: Fixture\r\n\r\nYour code: 654321')
            else:
                result = {'data': {**row, 'inbox': {'email': address}}}
        elif provider == 'mailtrap_inbound':
            if request.method == 'POST':
                assert path == '/api/inbound/folders/42/inboxes'
                assert set(json.loads(request.content)) == {'name'}
                result = {'id': 123, 'address': address, 'domain_id': 1}
            elif path.endswith('/messages'):
                result = {'data': [{**row, 'inbox_id': 123, 'to': [address]}], 'total_count': 1, 'last_id': None}
            else:
                result = {**row, 'inbox_id': 123, 'text_body': detail['text']}
        elif provider == "internxt":
            if path.endswith('/create-email'):
                result={'address':address,'token':'inbox-token'}
            else:
                assert request.url.params['email']==address and request.url.params['token']=='inbox-token'
                result=[row] if path.endswith('/get-inbox') else {**row,'body':detail['text']}
        elif provider == "emailnator":
            if path.endswith('/generate-email'):
                assert json.loads(request.content) == {'ids': [1]}
                result = {'status': 'success', 'email': address, 'type': 'domain', 'email_type_id': 1}
            elif path.endswith('/message-list'):
                assert json.loads(request.content) == {'email': address, 'limit': 20}
                result = {'status': 'success', 'messages': [row], 'message_count': 1, 'message_limit': 20}
            else:
                result = {**row, 'content': '<p>' + detail['text'] + '</p>', 'locked': False}
        else:
            if path == "/":
                return httpx.Response(200, text="Dedicated page", headers={"Set-Cookie": "session=owned; Path=/; Secure; HttpOnly"})
            result = {"address": address} if path.endswith("/address") else [{**row, "bodyPlainText": detail["text"], "bodyHtmlContent": None, "sender": row["from"]}]
        if broken:
            result = broken(request, result)
        return httpx.Response(200, json=result, headers={"Set-Cookie": "session=owned; Path=/; Secure; HttpOnly"} if provider=="crazymailing" and request.method=="POST" else {})
    return handler


@pytest.mark.parametrize("provider", [p for p in EXPANSION_FIELDS if p not in HTML_BASES and p not in DISCOVERY_FIELDS and p not in CONTINUATION_FIELDS and p not in PLATFORM_FIELDS and p not in {"mailcx", "restmail", "synox_disposable_mailbox", "tempmail_cc"}])
def test_saved_receive_contracts_and_foreign_id_rejection(provider, tmp_path, monkeypatch):
    calls = []
    native = httpx.Client
    handler = transport(provider, calls)
    monkeypatch.setattr("backend.web_clients.httpx.Client", lambda **kw: native(transport=httpx.MockTransport(handler), **kw))
    client = ExpansionClient(tmp_path)
    client.config["credentials"] = {"api_key": "fixture-key", "domain": "receiver.example", "team_id": "21", "folder_id": "42", "base_url": "https://deployed.example"}
    client.create(provider)
    address = client.config["address"]
    client = ExpansionClient(tmp_path)
    assert client.config["address"] == address
    rows = client.list_messages()
    assert [r["id"] for r in rows] == ["m1"]
    assert normalize(client.get_message("m1"))["text_body"].strip() == "Your code: 654321"
    before = len(calls)
    with pytest.raises(ProviderError) as error:
        client.get_message("unobserved-id")
    assert error.value.code == "message_not_in_mailbox" and len(calls) == before
    allocations = [r for r in calls if r.method == 'POST' and not r.url.path.endswith('/message-list')]
    assert len(allocations) <= 1


@pytest.mark.parametrize("provider", [p for p, fields in EXPANSION_FIELDS.items() if fields])
def test_keyed_sources_require_configuration_before_network(provider, tmp_path, monkeypatch):
    client = ExpansionClient(tmp_path)
    monkeypatch.setattr(client, "request", lambda *a, **k: pytest.fail("Unexpected unauthenticated request"))
    with pytest.raises(ProviderError) as error:
        client.create(provider)
    assert error.value.code == "provider_credentials_required"


@pytest.mark.parametrize("provider", ["crazymailing", "10minutemail"])
def test_cookie_loss_never_allocates_a_replacement(provider, tmp_path, monkeypatch):
    calls=[]
    native=httpx.Client
    handler=transport(provider,calls)
    monkeypatch.setattr("backend.web_clients.httpx.Client", lambda **kw:native(transport=httpx.MockTransport(handler),**kw))
    client=ExpansionClient(tmp_path)
    client.create(provider)
    client.cookies.clear()
    before=len(calls)
    with pytest.raises(ProviderError) as error:
        client.list_messages()
    assert error.value.code=="mailbox_session_lost" and len(calls)==before


@pytest.mark.parametrize("provider,reply", [
    ("chat_tempmail", {"messages": None}),
    ("mailslurp", {"content": [], "number": 0}),
    ("mailtd", {"messages": [], "page": 2}),
    ("inboxes", [{"uid": "m1", "to": "someoneelse@receiver.example"}]),
    ("smailpro", {"messages": [{"mid": "m1", "textTo": "someoneelse@receiver.example"}]}),
    ("crazymailing", {"mailbox": {"id": "new-inbox", "address": "new@receiver.example"}}),
    ("maildax", {"error": "expired", "data": []}),
    ("10minutemail", {"address": "new@receiver.example"}),
    ("emailnator", {"status": "error", "messages": [], "message_count": 0}),
    ("emailnator", {"status": "success", "messages": [], "message_count": 1}),
    ('mailtrap_inbound', {'data': [], 'total_count': 1, 'last_id': None}),
    ('mailcare', {'data': [], 'meta': {'current_page': 2, 'last_page': 2}}),
    ('cloudflare_temp_email', {'address': 'foreign@receiver.example'}),
    ('temporaryemail_ai', {'id':'inbox1', 'virtualEmail':'other@receiver.example'}),
])
def test_errors_and_identity_changes_are_not_empty_inboxes(provider, reply, tmp_path, monkeypatch):
    client=ExpansionClient(tmp_path)
    client.config={"provider":provider,"address":"original@receiver.example","inbox_id":"inbox1","secret":"saved","token":"fixture-token","credentials":{"api_key":"fixture-key", "base_url":"https://deployed.example", "team_id":"21"}}
    monkeypatch.setattr(client,"api",lambda *a,**k:reply)
    with pytest.raises(ProviderError):client.list_messages()
    assert not (tmp_path/"messages.local.json").exists()


def test_cursor_pagination_collects_all_and_rejects_loops(tmp_path, monkeypatch):
    client=ExpansionClient(tmp_path)
    client.config={"provider":"chat_tempmail","address":"owned@receiver.example","inbox_id":"inbox1","credentials":{"api_key":"fixture-key"}}
    calls=[]
    def api(*a,**k):
        calls.append(k.get("params"))
        return {"messages":[{"id":"m2"}],"total":2} if k.get("params") else {"messages":[{"id":"m1"}],"nextCursor":"page2","total":2}
    monkeypatch.setattr(client,"api",api)
    assert [r['id'] for r in client.list_messages()]==['m1','m2']
    assert calls==[None,{"cursor":"page2"}]
    previous=(tmp_path/'messages.local.json').read_bytes()
    monkeypatch.setattr(client,"api",lambda *a,**k:{"messages":[],"nextCursor":"loop"})
    with pytest.raises(ProviderError) as error:client.list_messages()
    assert error.value.code=='incomplete_mailbox_observation'
    assert (tmp_path/'messages.local.json').read_bytes()==previous


def test_mailinator_domain_configuration_is_validated_and_redacted(ctx):
    path='/api/v1/providers/mailinator/configuration'
    for receiving_domain in ['../../wrong', 'https://example.com','bad..example.com','domain with space']:
        r=ctx.client.put(path,headers=ctx.headers,json={'credentials':{'api_key':'fixture-key','domain':receiving_domain}})
        assert r.status_code==422
    r=ctx.client.put(path,headers=ctx.headers,json={'credentials':{'api_key':'fixture-key','domain':'owned.example'}})
    assert r.status_code==200 and r.json()['admission_state']=='enabled'
    assert 'fixture-key' not in r.text


@pytest.mark.parametrize('provider', list(HTML_BASES))
def test_html_inboxes_use_only_the_random_address_and_read_mime(provider,tmp_path,monkeypatch):
    calls=[]
    native=httpx.Client
    expected=None
    def handler(request):
        nonlocal expected
        calls.append(request)
        path=request.url.path
        if path=='/':
            return httpx.Response(200,text='<input id="domainName2" value="receiver.example">')
        parts=path.strip('/').split('/')
        if path == '/box/':
            assert expected is not None and 'inbox_ctx=' in request.headers['Cookie']
            user, host = expected.split('@')
            parts = [host, user]
        assert parts[0]=='receiver.example' and parts[1].startswith('ls') and len(parts[1])==24
        address=parts[1]+'@'+parts[0]
        if expected is None:expected=address
        assert address==expected
        if len(parts)==2 and path != '/box/':
            return httpx.Response(302, headers={'Location': '/box/#tab_ctx=' + parts[0] + '%2F' + parts[1]})
        if len(parts)==3:
            assert parts[2]=='m1' and request.url.params['src']=='m1'
            return httpx.Response(200,text='From: test@sender.example\r\nTo: '+address+'\r\nSubject: Fixture\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nYour code: 654321')
        markup='<input id="userName" value="'+parts[1]+'"><input id="domainName2" value="'+parts[0]+'"><script>window.SITE_DATA={num_mess:1};</script><div id="email-table"><div class="list-group-item" onclick="loadInboxClientSide(\'/'+parts[0]+'/'+parts[1]+'/m1\')"><div class="from_div_45g45gg">test@sender.example</div><div class="subj_div_45g45gg">Fixture</div><div class="time_div_45g45gg">2026-10-19T08:00:00Z</div></div></div>'
        return httpx.Response(200,text=markup)
    monkeypatch.setattr('backend.web_clients.httpx.Client',lambda **kw:native(transport=httpx.MockTransport(handler),**kw))
    client=ExpansionClient(tmp_path);client.create(provider)
    client=ExpansionClient(tmp_path)
    assert len(client.list_messages())==1
    assert client.get_message('m1')['text']=='Your code: 654321'
    before=len(calls)
    with pytest.raises(ProviderError):client.get_message('other')
    assert len(calls)==before


@pytest.mark.parametrize('source',[
    '<html>CAPTCHA required</html>',
    'From: sender@example.com\r\nSubject: Test\r\nTo: foreign@receiver.example\r\n\r\nwrong body',
])
def test_raw_mail_checks_format_and_recipient(source):
    with pytest.raises(ProviderError):raw_message(source,'owned@receiver.example')


def test_restmail_content_identity_and_nonempty_body(tmp_path,monkeypatch):
    client=ExpansionClient(tmp_path)
    state={}
    def api(*a,**kw):
        address=client.config['address']
        return [{'to':[{'address':address}],'from':[{'address':'test@sender.example'}],'headers':{'date':'2026-10-19T08:00:00Z'},'subject':'Fixture','text':'Your code: 654321'}]
    monkeypatch.setattr(client,'api',api)
    client.create('restmail');rows=client.list_messages()
    item=client.get_message(rows[0]['id'])
    assert normalize(item)['identity_strength']=='weak'
    assert item['from']=='test@sender.example' and item['text']=='Your code: 654321'
    first=rows[0]['id'];assert client.list_messages()[0]['id']==first
    monkeypatch.setattr(client,'api',lambda *a,**k:[{'to':[{'address':'other@receiver.example'}],'text':'wrong'}])
    with pytest.raises(ProviderError):client.list_messages()


def test_mailcx_204_long_poll_cursor_and_mime(tmp_path,monkeypatch):
    native=httpx.Client
    calls=[];address=None;poll=0
    def handler(request):
        nonlocal address,poll
        calls.append(request)
        assert request.headers['x-api-token']=='fixture-key'
        if '/inbox/' in request.url.path:
            address=request.url.path.rsplit('/',1)[1];poll+=1
            if poll!=2:
                return httpx.Response(204)
            assert 'since' not in request.url.params
            return httpx.Response(200,json={'emails':[{'id':'m1','from_email':'test@sender.example','subject':'Fixture','created_at':'2026-10-19T08:00:00Z'}],'next_since':'cursor1'})
        return httpx.Response(200,text='From: test@sender.example\r\nTo: '+address+'\r\nSubject: Fixture\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nYour code: 654321')
    monkeypatch.setattr('backend.web_clients.httpx.Client',lambda **kw:native(transport=httpx.MockTransport(handler),**kw))
    client=ExpansionClient(tmp_path);client.config['credentials']={'api_key':'fixture-key','domain':'receiver.example'}
    client.create('mailcx');assert client.list_messages()[0]['id']=='m1'
    client=ExpansionClient(tmp_path)
    assert client.list_messages()[0]['id']=='m1'
    assert calls[-1].url.params['since']=='cursor1'
    assert client.get_message('m1')['text']=='Your code: 654321'
    monkeypatch.setattr(client,'request',lambda *a,**k:'')
    with pytest.raises(ProviderError):client.api('GET','https://api.mail.cx/v1/inbox/owned',allow_no_content=True)


def test_selfhost_html_list_and_mime_stay_in_dedicated_mailbox(tmp_path, monkeypatch):
    native = httpx.Client
    calls = []
    def handler(request):
        calls.append(request)
        query = str(request.url.query, 'ascii')
        if request.url.params.get('action') == 'download_email':
            address = request.url.params['address']
            assert request.url.params['email_id'] == '1'
            return httpx.Response(200, text='From: test@sender.example\r\nTo: '+address+'\r\nSubject: Fixture\r\n\r\nYour code: 654321')
        assert query.endswith('@receiver.example') and query.startswith('ls')
        markup = '<span id="my-address">'+query+'</span><div id="email-list"><a class="email-list-item" href="#mail-box-1"><h6 class="list-group-item-heading"><span class="text-muted">test@sender.example</span><small title="2026-10-19T08:00:00Z">today</small></h6><p class="list-group-item-text">Fixture</p></a><div id="mail-box-1"></div></div>'
        return httpx.Response(200, text=markup)
    monkeypatch.setattr('backend.web_clients.httpx.Client', lambda **kw: native(transport=httpx.MockTransport(handler), **kw))
    client = ExpansionClient(tmp_path)
    client.config['credentials']={'base_url':'https://deployed.example','domain':'receiver.example'}
    client.create('synox_disposable_mailbox')
    client = ExpansionClient(tmp_path)
    assert client.list_messages()[0]['id'] == '1'
    assert normalize(client.get_message('1'))['text_body'] == 'Your code: 654321'
    before = len(calls)
    with pytest.raises(ProviderError): client.get_message('2')
    assert before == len(calls)


def test_html_redirect_never_leaks_cookie_or_key_to_another_origin(tmp_path, monkeypatch):
    native = httpx.Client
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={'Location':'https://other.example/stolen'})
    monkeypatch.setattr('backend.web_clients.httpx.Client', lambda **kw: native(transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(ProviderError) as error:
        ExpansionClient(tmp_path).request('GET','https://owned.example/',headers_extra={'X-API-Key':'fixture-key'},same_origin_redirects=2)
    assert error.value.code == 'upstream_redirect_rejected' and len(calls) == 1


def test_optional_site_password_configuration_and_url_validation(ctx):
    path = '/api/v1/providers/cloudflare_temp_email/configuration'
    for value in ['ftp://owned.example', 'https://user:pass@owned.example', 'https://owned.example?token=secret', 'https://owned.example/#mail', 'https://bad host', 'https://owned.example:99999']:
        r = ctx.client.put(path,headers=ctx.headers,json={'credentials':{'base_url':value,'domain':'receiver.example'}})
        assert r.status_code == 422
    r = ctx.client.put(path,headers=ctx.headers,json={'credentials':{'base_url':'https://deployed.example','domain':'receiver.example'}})
    assert r.status_code == 200 and r.json()['admission_state'] == 'enabled'
    fields = {f['name']: f for f in r.json()['credential_fields']}
    assert fields['site_password']['required'] is False and fields['site_password']['secret'] is True
    r = ctx.client.put(path,headers=ctx.headers,json={'credentials':{'base_url':'https://deployed.example','domain':'receiver.example','site_password':'hidden-fixture-password'}})
    assert r.status_code == 200 and 'hidden-fixture-password' not in r.text


@pytest.mark.parametrize('provider, response', [
    ('mailtrap_inbound', {'data': [], 'total_count': 1, 'last_id': 'loop'}),
    ('mailcare', {'data': [], 'meta': {'current_page': 1, 'last_page': 20}}),
])
def test_new_keyed_pagination_cannot_turn_truncation_into_empty(provider,response,tmp_path,monkeypatch):
    client = ExpansionClient(tmp_path)
    client.config={'provider':provider,'address':'owned@receiver.example','inbox_id':'123','credentials':{'api_key':'fixture-key','team_id':'21'}}
    monkeypatch.setattr(client,'api',lambda *a,**k:response)
    with pytest.raises(ProviderError) as error:client.list_messages()
    assert error.value.code == 'incomplete_mailbox_observation'
    assert not (tmp_path/'messages.local.json').exists()
