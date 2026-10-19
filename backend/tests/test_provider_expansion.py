"""Catalog admission, credential isolation and real request/response contracts."""
import json

import httpx
import pytest
from sqlalchemy import func, select

from backend.models import Mailbox, ProviderBudget, ProviderConfiguration, SyncJob, now
from backend.official_clients import OfficialClient
from backend.registry import HYDRA_BASES, OFFICIAL_FIELDS
from backend.tests.test_console import ctx, post, box
from mailbox_clients import ProviderError


def test_inventory_admission_and_no_phantom_mailboxes(ctx):
    response = ctx.client.get("/api/v1/providers", headers=ctx.headers)
    assert response.status_code == 200
    result = response.json()
    assert result["summary"] == {"external_total": 127, "adapter_total": 97, "ready": 61, "credential_required": 36, "pending": 30, "local": 1}
    assert len(result["items"]) == 128
    assert len({item["id"] for item in result["items"]}) == 128
    for provider in result["items"]:
        detail = ctx.client.get("/api/v1/providers/" + provider["id"], headers=ctx.headers)
        assert detail.json() == provider
        if provider["admission_state"] != "enabled":
            rejected = post(ctx, "/mailboxes", {"provider_id": provider["id"]})
            assert rejected.status_code == 409, rejected.text
            assert rejected.json()["code"] == "provider_" + provider["admission_state"]
    with ctx.db.session() as session:
        assert session.scalar(select(func.count()).select_from(Mailbox)) == 0
    assert ctx.client.get("/api/v1/providers/does-not-exist", headers=ctx.headers).status_code == 404


def test_destroy_polling_budget_is_shared_and_at_least_one_minute(ctx):
    from datetime import timedelta
    from backend.service import aware
    with ctx.db.session.begin() as session:
        budget = session.get(ProviderBudget, 'destroy_network')
        assert budget is not None
        stamp = now()
        budget.next_allowed_at = stamp - timedelta(seconds=1)
        assert ctx.worker._budget(session, 'destroy_network')
        assert aware(budget.next_allowed_at) >= stamp + timedelta(seconds=60)
        assert not ctx.worker._budget(session, 'destroy_network')


def test_credentials_are_encrypted_redacted_and_scope_protected(ctx):
    secret = "provider-test-secret-do-not-return"
    path = "/api/v1/providers/agentmail/configuration"
    response = ctx.client.put(path, headers=ctx.headers, json={"credentials": {"api_key": secret}})
    assert response.status_code == 200 and response.json()["admission_state"] == "enabled"
    assert secret not in response.text
    assert secret not in ctx.client.get("/api/v1/providers", headers=ctx.headers).text
    with ctx.db.session() as session:
        stored = session.get(ProviderConfiguration, "agentmail").vault_blob
        assert secret not in stored and ctx.db.decrypt(stored) == {"api_key": secret}
    for invalid in ({"api_key": ""}, {"api_key": secret, "extra": "ignored?"}, {"api_key": "x\r\ny"}):
        assert ctx.client.put(path, headers=ctx.headers, json={"credentials": invalid}).status_code == 422
    assert ctx.client.put("/api/v1/providers/mailosaur/configuration", headers=ctx.headers,
                          json={"credentials": {"api_key": secret, "server_id": "../other"}}).status_code == 422
    mailbox = box(ctx)
    issued = post(ctx, "/mailboxes/" + mailbox["id"] + "/tokens", {"scopes": ["mailbox:read"]}).json()
    limited = {"Authorization": "Bearer " + issued["access_token"]}
    assert ctx.client.get("/api/v1/providers", headers=limited).status_code == 403
    assert ctx.client.put(path, headers=limited, json={"credentials": {"api_key": secret}}).status_code == 403
    removed = ctx.client.delete(path, headers=ctx.headers)
    assert removed.status_code == 200 and removed.json()["admission_state"] == "credential_required"


def test_keyed_adapter_runs_through_worker_and_retains_original_session(ctx, monkeypatch):
    secret = "controlled-provider-key"
    address, inbox = "dedicated@agentmail.to", "dedicated@agentmail.to"
    calls = []
    def request(client, method, url, **kwargs):
        calls.append((method, url))
        assert client.config["credentials"]["api_key"] == secret
        if method == "POST":
            return {"inbox_id": inbox, "email": address}
        if url.endswith("/messages"):
            return {"messages": [{"message_id": "message-1", "from": "test@sender.example", "subject": "owned fixture"}]}
        return {"message_id": "message-1", "from": "test@sender.example", "text": "Your code: 778899"}
    monkeypatch.setattr(OfficialClient, "request", request)
    path = "/api/v1/providers/agentmail/configuration"
    ctx.client.put(path, headers=ctx.headers, json={"credentials": {"api_key": secret}}).raise_for_status()
    operation = post(ctx, "/mailboxes", {"provider_id": "agentmail"}).json()
    ctx.worker.tick()
    mailbox_id = operation["resource_id"]
    assert ctx.service.get_mailbox(ctx.service.principal(ctx.token), mailbox_id)["address"] == address
    with ctx.db.session.begin() as session:
        session.get(ProviderBudget, "agentmail").next_allowed_at = now()
    ctx.worker.tick()
    messages = ctx.client.get("/api/v1/mailboxes/" + mailbox_id + "/messages", headers=ctx.headers).json()["items"]
    assert len(messages) == 1
    # Removing global configuration must not silently recreate an existing inbox.
    ctx.client.delete(path, headers=ctx.headers).raise_for_status()
    fetch = post(ctx, "/mailboxes/" + mailbox_id + "/messages/" + messages[0]["id"] + "/fetches").json()
    with ctx.db.session.begin() as session:
        session.get(ProviderBudget, "agentmail").next_allowed_at = now()
    ctx.worker.tick()
    assert ctx.client.get("/api/v1/operations/" + fetch["id"], headers=ctx.headers).json()["state"] == "succeeded"
    detail = ctx.client.get("/api/v1/mailboxes/" + mailbox_id + "/messages/" + messages[0]["id"], headers=ctx.headers).json()
    assert detail["extractions"][0]["value"] == "778899"
    assert sum(method == "POST" for method, _ in calls) == 1


@pytest.mark.parametrize("provider", list(OFFICIAL_FIELDS))
def test_documented_http_contracts_create_list_read(provider, tmp_path, monkeypatch):
    key = "fixture-api-key"
    address = "fixture@provider.example"
    received = []
    def handler(request):
        nonlocal address
        received.append((request.method, request.url.path))
        path = request.url.path
        if provider in HYDRA_BASES:
            if path.endswith("/domains"):
                body = {"hydra:member": [{"domain": "provider.example", "isActive": True}]}
            elif path.endswith("/accounts"):
                address = json.loads(request.content)["address"]
                body = {"id": "account-id", "address": address}
            elif path.endswith("/token"):
                body = {"token": "mailbox-secret"}
            else:
                assert request.headers["Authorization"] == "Bearer mailbox-secret"
                body = {"hydra:member": [{"id": "m1", "subject": "fixture"}]} if path.endswith("/messages") else {"id": "m1", "text": "Your code: 123456", "html": ["<p>fixture</p>"]}
        elif provider == "burner_kiwi":
            if path.endswith("/inbox"):
                body = {"success": True, "result": {"email": {"address": address, "id": "inbox-id"}, "token": "burner-secret"}}
            else:
                assert request.headers["X-Burner-Key"] == "burner-secret"
                body = {"success": True, "result": [{"id": "m1", "body_plain": "Your code: 123456"}]} if path.endswith("/messages") else {"success": True, "result": {"address": address}}
        elif provider == "best_tempmail":
            body = {"address": address} if request.method == "POST" else {"messages": [{"id": "m1", "text": "Your code: 123456"}]} if path.endswith("/messages") else {"id": "m1", "text": "Your code: 123456"}
        elif provider == "temp_mail_io":
            assert request.headers["X-API-Key"] == key
            body = {"email": address} if request.method == "POST" else {"messages": [{"id": "m1"}]} if path.endswith("/messages") else {"id": "m1", "body_text": "Your code: 123456"}
        elif provider == "agentmail":
            assert request.headers["Authorization"] == "Bearer " + key
            body = {"email": address, "inbox_id": "inbox-id"} if request.method == "POST" else {"messages": [{"message_id": "m1"}]} if path.endswith("/messages") else {"message_id": "m1", "text": "Your code: 123456"}
        elif provider == "mailsac":
            assert request.headers["Mailsac-Key"] == key
            if "/text/" in path:
                return httpx.Response(200, text="Your code: 123456")
            body = [{"_id": "m1", "subject": "fixture"}]
        elif provider == "mailosaur":
            assert request.headers["Authorization"].startswith("Basic ")
            address = client.config["address"]
            body = {"items": [{"id": "m1", "to": [{"email": address}]}, {"id": "other", "to": [{"email": "other@server.mailosaur.net"}]}]} if path.endswith("/search") else {"id": "m1", "text": {"body": "Your code: 123456"}}
        elif provider == "testmail":
            assert request.url.params["apikey"] == key
            assert request.url.params["tag"] == client.config["tag"]
            body = {"result": "success", "count": 1, "emails": [{"id": "m1", "text": "Your code: 123456"}]}
        else:
            assert request.headers["X-Api-Key"] == key
            address = client.config["address"]
            assert request.url.params["to_addr_prefix"] == address
            body = {"total_count": 1, "data": [{"id": "m1", "to": [{"address": address}], "text": "Your code: 123456"}]}
        return httpx.Response(200, json=body)
    native_client = httpx.Client
    monkeypatch.setattr("backend.official_clients.httpx.Client", lambda **kwargs: native_client(transport=httpx.MockTransport(handler), **kwargs))
    client = OfficialClient(tmp_path)
    client.config["credentials"] = {"api_key": key, "namespace": "fixture", "server_id": "server"}
    client.create(provider)
    client = OfficialClient(tmp_path)  # Reopen the saved session, just like a worker lease.
    listing = client.list_messages()
    assert len(listing) == 1 and listing[0]["id"] == "m1"
    assert client.get_message("m1")["text"] == "Your code: 123456"
    before = len(received)
    with pytest.raises(ProviderError, match="not observed"):
        client.get_message("foreign-id")
    assert len(received) == before


def test_upstream_failures_are_not_empty_inboxes(tmp_path, monkeypatch):
    client = OfficialClient(tmp_path)
    client.config = {"provider": "agentmail", "address": "test@agentmail.to", "inbox_id": "test", "credentials": {"api_key": "secret"}}
    monkeypatch.setattr(client, "request", lambda *args, **kwargs: {"messages": None})
    with pytest.raises(ProviderError) as error:
        client.list_messages()
    assert error.value.code == "unexpected_response"
    monkeypatch.setattr(client, "request", lambda *args, **kwargs: {"messages": [], "next_page_token": "more"})
    with pytest.raises(ProviderError) as error:
        client.list_messages()
    assert error.value.code == "incomplete_mailbox_observation"
