"""Receive-only adapters for documented APIs; no credentials or bodies are logged."""
import base64
import json
from pathlib import Path
import re
import secrets
from urllib.parse import quote

import httpx

from mailbox_clients import Client, ProviderError
from .registry import HYDRA_BASES, OFFICIAL_FIELDS


def required(value, key):
    result = value.get(key) if isinstance(value, dict) else None
    if not isinstance(result, str) or not result:
        raise ProviderError("unexpected_response", "Required upstream field missing: " + key)
    return result


def rows(value, key=None):
    result = value.get(key) if key and isinstance(value, dict) else value
    if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
        raise ProviderError("unexpected_response", "Expected an explicit message array")
    return result


def message(value):
    result = dict(value)
    identity = value.get("id", value.get("message_id"))
    if identity is None:
        raise ProviderError("unexpected_response", "Message identity missing")
    result["id"] = str(identity)
    sender = value.get("from", value.get("from_address", value.get("sender")))
    if isinstance(sender, list):
        sender = sender[0] if sender else None
    result["from"] = sender
    result["received_at"] = value.get("createdAt", value.get("received_date", value.get("received", value.get("timestamp", value.get("received_at")))))
    result["text"] = value.get("text", value.get("body_text", value.get("body_plain")))
    result["html"] = value.get("html", value.get("body_html"))
    for field in ("text", "html"):
        if isinstance(result[field], dict):
            result[field] = result[field].get("body")
        if isinstance(result[field], list):
            result[field] = "\n".join(item for item in result[field] if isinstance(item, str))
    return result


class OfficialClient:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.config_path = self.folder / "inbox.local.json"
        self.config = json.loads(self.config_path.read_text(encoding="utf-8")) if self.config_path.exists() else {}

    def save(self):
        self.config_path.write_text(json.dumps(self.config) + "\n", encoding="utf-8")

    def request(self, method, url, *, headers=None, payload=None, params=None, plain=False):
        try:
            with httpx.Client(timeout=20, follow_redirects=False) as client:
                with client.stream(method, url, headers={"Accept": "application/json", "User-Agent": "Linsemail/0.2", **(headers or {})},
                                   json=payload, params=params) as response:
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        content.extend(chunk)
                        if len(content) > 2_000_000:
                            raise ProviderError("response_too_large", "Upstream response exceeded the receive limit")
                    status = response.status_code
        except httpx.HTTPError as exc:
            raise ProviderError("upstream_network_error", "Upstream request failed") from exc
        if not 200 <= status < 300:
            code = "credentials_rejected" if status in (401, 403) else "rate_limited" if status == 429 else "upstream_http_error"
            raise ProviderError(code, "Upstream request failed", status)
        if plain:
            return content.decode("utf-8", errors="replace")
        try:
            return json.loads(content)
        except ValueError as exc:
            raise ProviderError("unexpected_response", "Expected JSON from upstream") from exc

    def headers(self):
        provider = self.config["provider"]
        key = self.config.get("credentials", {}).get("api_key", "")
        if provider in HYDRA_BASES:
            return {"Authorization": "Bearer " + self.config["token"]}
        if provider == "burner_kiwi":
            return {"X-Burner-Key": self.config["token"]}
        if provider == "agentmail":
            return {"Authorization": "Bearer " + key}
        if provider == "mailosaur":
            return {"Authorization": "Basic " + base64.b64encode((key + ":").encode()).decode()}
        return {"Mailsac-Key" if provider == "mailsac" else "X-API-Key": key} if key else {}

    def create(self, provider):
        if provider not in OFFICIAL_FIELDS:
            raise ProviderError("unsupported_provider", "Adapter is not implemented")
        credentials = self.config.get("credentials", {})
        if any(not credentials.get(field) for field in OFFICIAL_FIELDS[provider]):
            raise ProviderError("provider_credentials_required", "Configure provider credentials first")
        self.config.update(provider=provider)
        prefix = "linsemail-" + secrets.token_hex(10)
        if provider in HYDRA_BASES:
            base = HYDRA_BASES[provider]
            body = self.request("GET", base + "/domains")
            domains = rows(body, "hydra:member") if isinstance(body, dict) else rows(body)
            domain = next((item.get("domain") for item in domains if item.get("isActive", True) and not item.get("isPrivate")), None)
            if not isinstance(domain, str) or not re.fullmatch(r"[A-Za-z0-9.-]+", domain):
                raise ProviderError("no_public_domain", "No active public domain")
            address, password = prefix + "@" + domain, secrets.token_urlsafe(24)
            account = self.request("POST", base + "/accounts", payload={"address": address, "password": password})
            if required(account, "address").lower() != address.lower():
                raise ProviderError("mailbox_identity_changed", "Created mailbox did not match")
            auth = self.request("POST", base + "/token", payload={"address": address, "password": password})
            self.config.update(address=address, token=required(auth, "token"))
        elif provider == "burner_kiwi":
            body = self.request("GET", "https://burner.kiwi/api/v1/inbox")
            if body.get("success") is not True or not isinstance(body.get("result"), dict):
                raise ProviderError("unexpected_response", "Inbox creation was not acknowledged")
            result = body["result"]
            email = result.get("email", {})
            self.config.update(address=required(email, "address"), inbox_id=required(email, "id"), token=required(result, "token"), expires_at=email.get("ttl"))
        elif provider == "best_tempmail":
            body = self.request("POST", "https://api.best-tempmail.com/v1/inboxes", payload={})
            self.config.update(address=required(body, "address"), expires_at=body.get("expires_at"))
        elif provider == "temp_mail_io":
            body = self.request("POST", "https://api.temp-mail.io/v1/emails", payload={}, headers=self.headers())
            self.config["address"] = required(body, "email")
        elif provider == "agentmail":
            body = self.request("POST", "https://api.agentmail.to/v0/inboxes", payload={}, headers=self.headers())
            self.config.update(address=required(body, "email"), inbox_id=required(body, "inbox_id"))
        elif provider == "mailsac":
            self.config["address"] = prefix + "@mailsac.com"
            self.list_messages()  # Verify the supplied key can read this dedicated address.
        elif provider == "mailosaur":
            server = credentials["server_id"]
            self.config["address"] = prefix + "." + server + "@" + server + ".mailosaur.net"
            self.list_messages()
        elif provider in ("testmail", "mailisk"):
            namespace = credentials["namespace"]
            self.config["tag"] = prefix
            self.config["address"] = namespace + "." + prefix + "@inbox.testmail.app" if provider == "testmail" else prefix + "@" + namespace + ".mailisk.net"
            self.list_messages()
        if not re.fullmatch(r"[^\s<>@]+@[A-Za-z0-9.-]+", self.config.get("address", "")):
            raise ProviderError("unexpected_response", "Upstream did not return a valid mailbox address")
        self.save()

    def list_messages(self):
        provider, address = self.config["provider"], self.config["address"]
        headers, credentials = self.headers(), self.config.get("credentials", {})
        if provider in HYDRA_BASES:
            body = self.request("GET", HYDRA_BASES[provider] + "/messages", headers=headers)
            items = rows(body, "hydra:member") if isinstance(body, dict) else rows(body)
            if isinstance(body, dict) and body.get("hydra:view", {}).get("hydra:next"):
                raise ProviderError("incomplete_mailbox_observation", "Upstream mailbox requires pagination")
        elif provider == "burner_kiwi":
            base = "https://burner.kiwi/api/v1/inbox/" + quote(self.config["inbox_id"], safe="")
            identity = self.request("GET", base, headers=headers)
            if identity.get("success") is not True or identity.get("result", {}).get("address") != address:
                raise ProviderError("mailbox_identity_changed", "Original inbox identity not confirmed")
            body = self.request("GET", base + "/messages", headers=headers)
            if body.get("success") is not True:
                raise ProviderError("upstream_api_error", "Inbox listing was not acknowledged")
            items = rows(body, "result")
        elif provider == "best_tempmail":
            items = rows(self.request("GET", "https://api.best-tempmail.com/v1/inboxes/" + quote(address, safe="") + "/messages"), "messages")
        elif provider == "temp_mail_io":
            body = self.request("GET", "https://api.temp-mail.io/v1/emails/" + quote(address, safe="") + "/messages", headers=headers)
            items = rows(body, "messages") if isinstance(body, dict) else rows(body)
        elif provider == "agentmail":
            body = self.request("GET", "https://api.agentmail.to/v0/inboxes/" + quote(self.config["inbox_id"], safe="") + "/messages", headers=headers, params={"limit": 100})
            items = rows(body, "messages")
            if body.get("next_page_token"):
                raise ProviderError("incomplete_mailbox_observation", "Upstream mailbox requires pagination")
        elif provider == "mailsac":
            items = rows(self.request("GET", "https://mailsac.com/api/addresses/" + quote(address, safe="") + "/messages", headers=headers))
            items = [{**item, "id": item.get("_id"), "received_at": item.get("received")} for item in items]
        elif provider == "mailosaur":
            body = self.request("POST", "https://mailosaur.com/api/messages/search", headers=headers,
                                params={"server": credentials["server_id"]}, payload={"sentTo": address})
            items = rows(body, "items")
            items = [item for item in items if any(target.get("email", "").lower() == address.lower() for target in item.get("to", []))]
        elif provider == "testmail":
            body = self.request("GET", "https://api.testmail.app/api/json", params={"apikey": credentials["api_key"], "namespace": credentials["namespace"], "tag": self.config["tag"], "limit": 100})
            if body.get("result") != "success":
                raise ProviderError("upstream_api_error", "Namespace listing was not acknowledged")
            items = rows(body, "emails")
            if body.get("count", len(items)) > len(items):
                raise ProviderError("incomplete_mailbox_observation", "Upstream mailbox requires pagination")
        elif provider == "mailisk":
            body = self.request("GET", "https://api.mailisk.com/api/emails/" + quote(credentials["namespace"], safe="") + "/inbox", headers=headers,
                                params={"to_addr_prefix": address, "limit": 20, "offset": 0, "wait": "false"})
            items = rows(body, "data")
            if body.get("total_count", len(items)) > len(items):
                raise ProviderError("incomplete_mailbox_observation", "Upstream mailbox requires pagination")
            items = [item for item in items if any(target.get("address", "").lower() == address.lower() for target in item.get("to", []))]
        else:
            raise ProviderError("unsupported_provider", "Adapter is not implemented")
        items = [message(item) for item in items]
        if len({item["id"] for item in items}) != len(items):
            raise ProviderError("unexpected_response", "Duplicate upstream message IDs")
        (self.folder / "messages.local.json").write_text(json.dumps(items), encoding="utf-8")
        self.save()
        return items

    def get_message(self, message_id):
        cached_path = self.folder / "messages.local.json"
        cached = json.loads(cached_path.read_text(encoding="utf-8")) if cached_path.exists() else []
        cached_item = next((item for item in cached if item["id"] == message_id), None)
        if cached_item is None:
            raise ProviderError("message_not_in_mailbox", "Message was not observed in this mailbox")
        provider, headers = self.config["provider"], self.headers()
        identity, address = quote(message_id, safe=""), quote(self.config["address"], safe="")
        if provider in HYDRA_BASES:
            body = self.request("GET", HYDRA_BASES[provider] + "/messages/" + identity, headers=headers)
        elif provider == "temp_mail_io":
            body = self.request("GET", "https://api.temp-mail.io/v1/messages/" + identity, headers=headers)
        elif provider == "agentmail":
            body = self.request("GET", "https://api.agentmail.to/v0/inboxes/" + quote(self.config["inbox_id"], safe="") + "/messages/" + identity, headers=headers)
        elif provider == "mailsac":
            return {**cached_item, "text": self.request("GET", "https://mailsac.com/api/text/" + address + "/" + identity, headers=headers, plain=True)}
        elif provider == "mailosaur":
            body = self.request("GET", "https://mailosaur.com/api/messages/" + identity, headers=headers)
        elif provider == "best_tempmail":
            body = self.request("GET", "https://api.best-tempmail.com/v1/inboxes/" + address + "/messages/" + identity)
        else:
            return cached_item  # These protocols include complete bodies in their list.
        result = message(body)
        if result["id"] != message_id:
            raise ProviderError("mailbox_identity_changed", "Message detail identity changed")
        return result
