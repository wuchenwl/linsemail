"""Web contracts from public assets plus session and cross-mailbox regressions."""
import json
from urllib.parse import parse_qs

import httpx
import pytest

from backend.web_clients import WEB_IDS, WebClient
from backend.tests.test_console import ctx
from mailbox_clients import ProviderError


def mailnesia(address, rows=""):
    return f'<h1 class="emails"><a href="mailto:{address}">Mail for</a></h1><table class="email"><tbody>{rows}</tbody></table>'


def tempail(address, count=0, row=""):
    return f'''<input id="eposta_adres" value="{address}"><script>var oturum="session-one";var posta_adet="0";</script>
    <div id="epostalar"><ul>{row or '<li><div class="eposta-bekleniyor">Waiting for emails</div></li>'}</ul><script>posta_adet="{count}";</script></div>'''


@pytest.mark.parametrize("provider", sorted(WEB_IDS))
def test_web_create_reopen_list_and_read_owned_message(provider, tmp_path, monkeypatch):
    calls, created = [], False
    address = "allocated@provider.example"
    def handler(request):
        nonlocal created, address
        calls.append((request.method, str(request.url)))
        path = request.url.path
        headers = {"Set-Cookie": "session=fixture-cookie; Path=/; Secure; HttpOnly"}
        if provider == "tempmailo":
            if request.method == "GET" and path == "/":
                return httpx.Response(200, text='<input value="fixture-csrf" name="__RequestVerificationToken">', headers=headers)
            assert "session=fixture-cookie" in request.headers["cookie"]
            assert request.headers["RequestVerificationToken"] == "fixture-csrf"
            if path == "/changemail":
                return httpx.Response(200, json=address)
            assert json.loads(request.content) == {"mail": address}
            return httpx.Response(200, json=[{"from":"sender@example.test", "subject":"Owned", "date":"2026-10-19", "text":"Your code: 654321"}])
        if provider == "disposablemail":
            if path == "/":
                return httpx.Response(200, text='const CSRF="fixture-csrf"', headers=headers)
            assert "session=fixture-cookie" in request.headers["cookie"] if request.url.host != "www.tempmailaddress.com" else True
            if path == "/index/index":
                assert request.url.params["csrf_token"] == "fixture-csrf"
                return httpx.Response(200, json={"email":address})
            if path == "/index/refresh":
                return httpx.Response(200, json=[{"id":1,"od":"sender@example.test","predmet":"Owned"}])
            if path == "/index/email":
                assert parse_qs(request.content.decode()) == {"id":["1"]}
                return httpx.Response(200, json={"id":"1","od":"sender@example.test","predmet":"Owned","telo":"<p>Your code: 654321</p>"})
            pytest.fail("The current body is returned by the session-scoped JSON endpoint")
        if provider == "mailcatch":
            if "/api/list/" in path:
                prefix=path.rsplit("/",1)[-1]
                address=prefix+"@mailcatch.com"
                return httpx.Response(200, text=f'<div class="inbox-header">{prefix}<small>@mailcatch.com</small></div><ul class="email-list"><li class="email-item" data-email-id="42" data-subject="Owned" data-sender="sender@example.test"></li></ul>')
            return httpx.Response(200, text='<div class="email-content"><iframe srcdoc="&lt;p&gt;Your code: 654321&lt;/p&gt;"></iframe></div>')
        if provider == "mailnesia":
            if "p" in request.url.params:
                return httpx.Response(200, text="")
            if "/42" in path:
                assert request.url.params["noheadernofooter"] == "ajax"
                return httpx.Response(200, text='<div class="emails"><div class="pill-content"><div id="text_plain_42">Your code: 654321</div></div></div>')
            address=path.rsplit("/",1)[-1]+"@mailnesia.com"
            assert len(address.split("@")[0]) <= 30
            return httpx.Response(200, text=mailnesia(address,'<tr id="42"><td>2026-10-19</td><td>sender@example.test</td><td>dedicated</td><td>Owned</td><td></td></tr>'))
        if request.method == "POST":
            form=parse_qs(request.content.decode())
            assert form == {"oturum":["session-one"],"veri[0]":["body-key"],"veri[1]":["42"]}
            return httpx.Response(200, text='<div>Your code: 654321</div>')
        row='<li id="mail_42" onclick="mail_oku(42,\'body-key\')"><div class="gonderen">sender@example.test</div><div class="baslik">Owned</div></li>'
        return httpx.Response(200, text=tempail(address,1,row), headers=headers)
    native=httpx.Client
    monkeypatch.setattr("backend.web_clients.httpx.Client", lambda **kw:native(transport=httpx.MockTransport(handler),**kw))
    client=WebClient(tmp_path)
    client.create(provider)
    assert client.config["address"] == address
    client=WebClient(tmp_path)
    listing=client.list_messages()
    assert len(listing)==1
    body=client.get_message(listing[0]["id"])
    assert "654321" in (body.get("text") or body.get("html"))
    count=len(calls)
    with pytest.raises(ProviderError) as error:
        client.get_message("foreign-id/../../other")
    assert error.value.code=="message_not_in_mailbox" and len(calls)==count
    with pytest.raises(ProviderError) as error:
        client.create(provider)
    assert error.value.code=="existing_mailbox" and len(calls)==count


@pytest.mark.parametrize("provider,source,code",[
    ("mailcatch",'<div class="inbox-header">other@mailcatch.com</div><ul class="email-list"></ul>',"mailbox_identity_changed"),
    ("mailcatch",'<h1>Maintenance</h1>',"unexpected_response"),
    ("mailcatch",'<div class="inbox-header">owned@mailcatch.com</div>',"unexpected_response"),
    ("mailnesia",mailnesia("other@mailnesia.com"),"mailbox_identity_changed"),
    ("mailnesia",'<h1>Invalid characters entered!</h1>',"unexpected_response"),
    ("tempail",tempail("other@provider.example"),"mailbox_identity_changed"),
    ("tempail",tempail("owned@provider.example",1),"incomplete_mailbox_observation"),
    ("tempmailo",'{"error":"expired"}',"unexpected_response"),
    ("tempmailo",'[{"from":"sender","subject":"missing body"}]',"unexpected_response"),
    ("disposablemail",'{"error":"expired"}',"unexpected_response"),
    ("disposablemail",'[{"subject":"missing id"}]',"unexpected_response"),
])
def test_errors_and_changed_identity_never_become_empty_inboxes(provider,source,code,tmp_path,monkeypatch):
    client=WebClient(tmp_path)
    domain="mailcatch.com" if provider=="mailcatch" else "mailnesia.com" if provider=="mailnesia" else "provider.example"
    client.config={"provider":provider,"address":"owned@"+domain,"csrf":"fixture","session":"session-one"}
    monkeypatch.setattr(client,"request",lambda *a,**kw:source)
    with pytest.raises(ProviderError) as error:
        client.list_messages()
    assert error.value.code==code
    assert not (tmp_path/"messages.local.json").exists()


@pytest.mark.parametrize("provider,source",[
    ("mailcatch",'<div class="inbox-header">owned@mailcatch.com</div><ul class="email-list"></ul>'),
    ("mailnesia",mailnesia("owned@mailnesia.com")),
    ("tempmailo",'[]'),
    ("disposablemail",'0'),
    ("tempail",tempail("owned@provider.example")),
])
def test_explicit_empty_mailbox_is_accepted(provider,source,tmp_path,monkeypatch):
    client=WebClient(tmp_path)
    client.config={"provider":provider,"address":"owned@"+("mailcatch.com" if provider=="mailcatch" else "mailnesia.com" if provider=="mailnesia" else "provider.example"),"csrf":"fixture","session":"session-one"}
    monkeypatch.setattr(client,"request",lambda *a,**kw:source)
    assert client.list_messages()==[]


@pytest.mark.parametrize("status,body,code",[
    (403,"Forbidden","upstream_access_denied"),
    (429,"Too many requests","rate_limited"),
    (304,"","upstream_http_error"),
    (200,'<title>Just a moment</title>',"upstream_challenge"),
    (200,"a"*2_000_001,"response_too_large"),
], ids=["forbidden", "rate-limit", "not-modified", "challenge", "too-large"])
def test_transport_errors_are_explicit(status,body,code,tmp_path,monkeypatch):
    native=httpx.Client
    monkeypatch.setattr("backend.web_clients.httpx.Client",lambda **kw:native(transport=httpx.MockTransport(lambda req:httpx.Response(status,text=body)),**kw))
    with pytest.raises(ProviderError) as error:
        WebClient(tmp_path).request("GET","https://mailcatch.com/api/list/owned")
    assert error.value.code==code


def test_tempmailo_content_identity_survives_list_reordering(tmp_path,monkeypatch):
    client=WebClient(tmp_path)
    client.config={"provider":"tempmailo","address":"dedicated@example.test","csrf":"fixture"}
    messages=[{"text":"A","subject":"first"},{"text":"B","subject":"second"}]
    monkeypatch.setattr(client,"json",lambda *a,**kw:messages)
    first=client.list_messages()
    messages.reverse()
    second=client.list_messages()
    assert first[0]["id"]==second[1]["id"]


def test_lost_cookie_session_fails_before_network_request(tmp_path,monkeypatch):
    client=WebClient(tmp_path)
    client.config={"provider":"disposablemail","address":"dedicated@example.test","session_cookies":{"www.disposablemail.com|/|PHPSESSID":"original-hash"}}
    def unexpected(*a,**kw):
        pytest.fail("A lost session must not allocate or access another mailbox")
    monkeypatch.setattr("backend.web_clients.httpx.Client",unexpected)
    with pytest.raises(ProviderError) as error:
        client.list_messages()
    assert error.value.code=="mailbox_session_lost"


def test_mailnesia_collects_all_pages_and_refuses_wrong_detail(tmp_path,monkeypatch):
    client=WebClient(tmp_path)
    client.config={"provider":"mailnesia","address":"dedicated@mailnesia.com"}
    def row(mid):
        return f'<tr id="{mid}"><td><time datetime="2026-10-19T00:00:00Z"></time></td><td>sender</td><td>dedicated</td><td>Owned</td><td></td></tr>'
    def request(method,url,**kw):
        if "p=1" in url:return row("2")
        if "p=2" in url:return ""
        if "/1?" in url:return '<div id="text_plain_999">Wrong message</div>'
        return mailnesia(client.config["address"],row("1"))
    monkeypatch.setattr(client,"request",request)
    assert [r["id"] for r in client.list_messages()]==["1","2"]
    with pytest.raises(ProviderError) as error:
        client.get_message("1")
    assert error.value.code=="unexpected_response"


def test_web_session_survives_worker_leases_and_remains_encrypted(ctx,monkeypatch):
    from sqlalchemy import select
    from backend.models import Mailbox, ProviderBudget, now
    from backend.tests.test_console import post
    native=httpx.Client
    def handler(request):
        if request.url.path=="/changemail":
            assert request.headers["RequestVerificationToken"]=="web-csrf-secret"
            assert "session=web-cookie-secret" in request.headers["cookie"]
            return httpx.Response(200,text="dedicated@provider.example")
        if request.method=="GET":
            return httpx.Response(200,text='<input name="__RequestVerificationToken" value="web-csrf-secret">',headers={"Set-Cookie":"session=web-cookie-secret; Path=/; Secure; HttpOnly"})
        assert json.loads(request.content)=={"mail":"dedicated@provider.example"}
        return httpx.Response(200,json=[{"from":"test@sender.example","subject":"owned fixture","text":"Your code: 112233"}])
    monkeypatch.setattr("backend.web_clients.httpx.Client",lambda **kw:native(transport=httpx.MockTransport(handler),**kw))
    operation=post(ctx,"/mailboxes",{"provider_id":"tempmailo"}).json()
    ctx.worker.tick()
    mailbox_id=operation["resource_id"]
    with ctx.db.session.begin() as session:
        record=session.get(Mailbox,mailbox_id)
        assert record.address=="dedicated@provider.example"
        assert "web-cookie-secret" not in record.vault_blob and "web-csrf-secret" not in record.vault_blob
        assert "cookies.local.cookies" in ctx.db.decrypt(record.vault_blob)
        session.get(ProviderBudget,"tempmailo").next_allowed_at=now()
    ctx.worker.tick()
    response=ctx.client.get(f"/api/v1/mailboxes/{mailbox_id}/messages",headers=ctx.headers)
    messages=response.json()["items"]
    assert len(messages)==1
    assert "web-cookie-secret" not in response.text and "web-csrf-secret" not in response.text
    detail=ctx.client.get(f"/api/v1/mailboxes/{mailbox_id}/messages/{messages[0]['id']}",headers=ctx.headers).json()
    assert detail["extractions"][0]["value"]=="112233"
