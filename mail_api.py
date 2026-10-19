"""Unified local mailbox API. Run: python mail_api.py --port 8000."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import shutil
import threading
from urllib.parse import unquote, urlsplit

from app import Catalog, ROOT, make_handler as make_catalog_handler
from mailbox_clients import Client, ProviderError
from tasklet_client import TaskletClient


ADAPTERS = {
    "grabmail": {"interface": "official_api", "mailbox_access": "public_address"},
    "fakemail": {"interface": "public_frontend", "mailbox_access": "session_cookie"},
    "tempmail_plus": {"interface": "public_frontend", "mailbox_access": "public_address"},
    "byom": {"interface": "public_frontend", "mailbox_access": "public_address"},
    "guerrillamail": {"interface": "official_api", "mailbox_access": "session_cookie", "body_read_live_verified": True},
    "tempmail_lol": {"interface": "official_api", "mailbox_access": "mailbox_token"},
    "maildrop": {"interface": "official_api", "mailbox_access": "public_address"},
    "dropmail": {"interface": "official_api", "mailbox_access": "api_token"},
    "10minutemail_net": {"interface": "public_frontend", "mailbox_access": "session_cookie", "body_read_live_verified": True},
    "duckmail": {"interface": "public_frontend", "mailbox_access": "mailbox_token"},
    "moakt": {"interface": "public_frontend", "mailbox_access": "session_cookie"},
    "inboxkitten": {"interface": "public_frontend", "mailbox_access": "public_address"},
    "mohmal": {"interface": "public_frontend", "mailbox_access": "session_cookie"},
    "awamail": {"interface": "public_frontend", "mailbox_access": "session_cookie"},
    "tempmail_ing": {"interface": "public_frontend", "mailbox_access": "public_address"},
    "minuteinbox": {"interface": "public_frontend", "mailbox_access": "session_cookie", "body_read_live_verified": True},
    "tempmailto": {"interface": "public_frontend", "mailbox_access": "session_cookie"},
    "10minutemail_one": {"interface": "public_frontend", "mailbox_access": "public_address_with_visitor_token"},
    "22do": {"interface": "public_frontend", "mailbox_access": "mailbox_token"},
    "mailtemp_net": {"interface": "public_frontend", "mailbox_access": "session_cookie"},
    "spambox_xyz": {"interface": "public_frontend", "mailbox_access": "session_cookie"},
    "mailtemp_uk": {"interface": "public_frontend", "mailbox_access": "session_cookie"},
    "emailtemp_org": {"interface": "public_frontend", "mailbox_access": "session_cookie"},
    "tempmail100": {"interface": "public_frontend", "mailbox_access": "mailbox_token"},
    "anonymmail": {"interface": "public_frontend", "mailbox_access": "session_cookie"},
    "tempmailbox_net": {"interface": "public_frontend", "mailbox_access": "session_cookie_and_mailbox_id"},
    "mailforspam": {"interface": "public_frontend", "mailbox_access": "public_address"},
    "linshiyou": {"interface": "public_frontend", "mailbox_access": "public_address_with_session"},
    "harakirimail": {"interface": "public_frontend", "mailbox_access": "public_address"},
    "tempemail_co": {"interface": "public_frontend", "mailbox_access": "public_address_with_session"},
}


class ApiError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


def normalize(item, *, detail=False):
    """Return a stable response without forwarding upstream secrets or action markup."""
    sender = item.get("from", item.get("from_mail", item.get("od")))
    value = {"id": Client.message_id(item), "subject": item.get("subject", item.get("predmet")),
             "from": sender, "received_at": item.get("received_at", item.get("created_at", item.get("date", item.get("kdy"))))}
    if detail:
        value.update(text=item.get("text", item.get("body", item.get("body_text"))),
                     html=item.get("html", item.get("body_html")))
    return value


class MailboxStore:
    def __init__(self, root=None, client_factory=Client):
        self.root = Path(root or ROOT / "research/mailboxes").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.client_factory = client_factory
        self._guard, self._locks = threading.Lock(), {}

    def lock(self, mailbox_id):
        with self._guard:
            return self._locks.setdefault(mailbox_id, threading.Lock())

    def _issue(self, folder, config):
        token = secrets.token_urlsafe(32)
        access = {"token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                  "created_at": datetime.now(timezone.utc).isoformat()}
        (folder / "access.local.json").write_text(json.dumps(access) + "\n", encoding="utf-8")
        return {"id": folder.name, "provider": config["provider"], "address": config["address"],
                "access_token": token, "token_type": "Bearer",
                "messages_url": f"/api/mailboxes/{folder.name}/messages"}

    def create(self, provider):
        if provider not in ADAPTERS:
            raise ApiError(400, "unsupported_provider")
        folder = self.root / secrets.token_hex(16)
        folder.mkdir()
        client = self.client_factory(folder)
        client.create(provider)
        return self._issue(folder, client.config)

    def import_mailbox(self, source):
        """Local-only reuse of a previously created test inbox; never an HTTP route."""
        source = Path(source).resolve()
        config = json.loads((source / "inbox.local.json").read_text(encoding="utf-8"))
        if config.get("provider") not in ADAPTERS or not config.get("address"):
            raise ValueError("Unsupported existing test inbox")
        folder = self.root / secrets.token_hex(16)
        folder.mkdir()
        for name in ("inbox.local.json", "cookies.local.cookies", "messages.local.json"):
            if (source / name).is_file():
                shutil.copyfile(source / name, folder / name)
        (folder / "origin.local.json").write_text(json.dumps({"source": str(source)}) + "\n", encoding="utf-8")
        return self._issue(folder, config)

    def authorize(self, mailbox_id, authorization):
        if not re.fullmatch(r"[0-9a-f]{32}", mailbox_id):
            raise ApiError(401, "unauthorized")
        if not authorization.startswith("Bearer ") or len(authorization) > 256:
            raise ApiError(401, "unauthorized")
        folder = self.root / mailbox_id
        try:
            access = json.loads((folder / "access.local.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ApiError(401, "unauthorized") from None
        supplied = hashlib.sha256(authorization[7:].encode()).hexdigest()
        if not secrets.compare_digest(supplied, access["token_sha256"]):
            raise ApiError(401, "unauthorized")
        return folder


class TaskletStore(MailboxStore):
    def __init__(self, root, client_factory=TaskletClient):
        super().__init__(root, client_factory=client_factory)

    def create(self, email):
        folder = self.root / secrets.token_hex(16)
        folder.mkdir()
        client = self.client_factory(folder)
        status = client.request_link(email)
        issued = self._issue(folder, {"provider": "tasklet", "address": email})
        return {"id": issued["id"], "access_token": issued["access_token"], "token_type": "Bearer", **status}


def make_handler(catalog, store, tasklet_store=None):
    tasklet_store = tasklet_store or TaskletStore(store.root / "tasklet-auth")

    class Handler(make_catalog_handler(catalog)):
        server_version = "Linsemail/0.2"

        def log_message(self, format, *args):
            # URLs and bearer tokens are never logged by this handler.
            pass

        def fail(self, error, head=False):
            if isinstance(error, ApiError):
                return self.respond(error.status, {"error": error.code}, head)
            status = (404 if error.code == "message_not_listed" else
                      400 if error.code in ("invalid_email", "invalid_verification") else
                      409 if error.code in ("not_authenticated", "already_authenticated", "no_auth_request", "existing_request") else
                      429 if error.status == 429 else 502)
            return self.respond(status, {"error": error.code, "upstream_status": error.status}, head)

        def do_GET(self, head=False):
            target = urlsplit(self.path)
            path = target.path.rstrip("/") or "/"
            if path.startswith("/api/tasklet"):
                try:
                    if target.query:
                        raise ApiError(400, "query_not_supported")
                    if path == "/api/tasklet":
                        return self.respond(200, {"interface": "public_frontend", "auth_request_live_verified": True,
                                                  "sign_in_live_verified": False, "session_live_verified": False,
                                                  "endpoints": ["/api/tasklet/auth/request", "/api/tasklet/auth/{id}",
                                                                "/api/tasklet/auth/{id}/resend", "/api/tasklet/auth/{id}/verify",
                                                                "/api/tasklet/auth/{id}/session"]}, head)
                    match = re.fullmatch(r"/api/tasklet/auth/([0-9a-f]{32})", path)
                    if not match:
                        raise ApiError(404, "not_found")
                    request_id = match.group(1)
                    folder = tasklet_store.authorize(request_id, self.headers.get("Authorization", ""))
                    with tasklet_store.lock(request_id):
                        result = tasklet_store.client_factory(folder).status()
                    return self.respond(200, {"id": request_id, **result}, head)
                except (ApiError, ProviderError) as error:
                    return self.fail(error, head)
                except (ValueError, KeyError, TypeError, OSError):
                    return self.respond(502, {"error": "invalid_upstream_state"}, head)
            if path in ("/", "/health", "/api/mailbox-providers") and not target.query:
                if path == "/":
                    result = {"service": "linsemail", "stage": "mailbox_api", "mailbox_adapters_implemented": len(ADAPTERS),
                              "endpoints": ["/api/providers", "/api/mailbox-providers", "/api/mailboxes", "/api/mailboxes/{id}/messages", "/openapi.json"]}
                elif path == "/health":
                    result = {"status": "ok", "providers": len(catalog.by_id), "mailbox_adapters": len(ADAPTERS)}
                else:
                    result = {"items": [{"id": key, **value, "operations": ["create", "list_messages", "get_message"],
                                         "body_read_live_verified": value.get("body_read_live_verified", key == "fakemail")} for key, value in ADAPTERS.items()]}
                return self.respond(200, result, head)
            if not path.startswith("/api/mailboxes"):
                return super().do_GET(head=head)
            try:
                if target.query:
                    raise ApiError(400, "query_not_supported")
                match = re.fullmatch(r"/api/mailboxes/([0-9a-f]{32})/messages(?:/([^/]{1,256}))?", path)
                if not match:
                    raise ApiError(404, "not_found")
                mailbox_id, encoded_id = match.groups()
                folder = store.authorize(mailbox_id, self.headers.get("Authorization", ""))
                with store.lock(mailbox_id):
                    client = store.client_factory(folder)
                    if encoded_id is None:
                        items = client.list_messages()
                        result = {"mailbox_id": mailbox_id, "items": [normalize(item) for item in items], "count": len(items)}
                    else:
                        message_id = unquote(encoded_id)
                        if any(ord(c) < 32 for c in message_id) or any(part in (".", "..") for part in message_id.split("/")):
                            raise ApiError(400, "invalid_message_id")
                        body = client.get_message(message_id)
                        if not isinstance(body, dict):
                            raise ProviderError("unexpected_response", "Invalid message response")
                        # Some official APIs wrap the detail in a message object.
                        body = body.get("message", body)
                        result = normalize(body, detail=True)
                        result["id"] = message_id
                return self.respond(200, result, head)
            except (ApiError, ProviderError) as error:
                return self.fail(error, head)
            except (ValueError, KeyError, TypeError, OSError, StopIteration):
                return self.respond(502, {"error": "invalid_upstream_state"}, head)

        def do_POST(self):
            self.close_connection = True
            try:
                path = self.path.rstrip("/")
                action_match = re.fullmatch(r"/api/tasklet/auth/([0-9a-f]{32})/(resend|verify|session)", path)
                if path not in ("/api/mailboxes", "/api/tasklet/auth/request") and not action_match:
                    raise ApiError(404, "not_found")
                if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                    raise ApiError(415, "json_required")
                if self.headers.get("Transfer-Encoding"):
                    raise ApiError(400, "unsupported_transfer_encoding")
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    raise ApiError(400, "invalid_content_length") from None
                if not 1 <= size <= 1024:
                    raise ApiError(413, "invalid_body_size")
                self.connection.settimeout(30)
                try:
                    data = json.loads(self.rfile.read(size))
                except (ValueError, UnicodeError):
                    raise ApiError(400, "invalid_json") from None
                if path == "/api/mailboxes":
                    if not isinstance(data, dict) or set(data) != {"provider"} or not isinstance(data["provider"], str):
                        raise ApiError(400, "provider_only_required")
                    return self.respond(201, store.create(data["provider"]))
                if path == "/api/tasklet/auth/request":
                    if not isinstance(data, dict) or set(data) != {"email"} or not isinstance(data["email"], str):
                        raise ApiError(400, "email_only_required")
                    return self.respond(202, tasklet_store.create(data["email"]))
                request_id, action = action_match.groups()
                folder = tasklet_store.authorize(request_id, self.headers.get("Authorization", ""))
                with tasklet_store.lock(request_id):
                    client = tasklet_store.client_factory(folder)
                    if action == "verify":
                        if not isinstance(data, dict) or set(data) not in ({"pin"}, {"magic_link_token"}):
                            raise ApiError(400, "pin_or_link_token_required")
                        result = client.verify(**data)
                    else:
                        if data != {}:
                            raise ApiError(400, "empty_object_required")
                        result = client.request_link(client.state.get("email", "")) if action == "resend" else client.check_session()
                return self.respond(200, {"id": request_id, **result})
            except (ApiError, ProviderError) as error:
                return self.fail(error)
            except (ValueError, KeyError, TypeError, OSError):
                return self.respond(502, {"error": "invalid_upstream_state"})

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1",))
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(Catalog(), MailboxStore(args.data_dir)))
    print(f"Linsemail mailbox API: http://{args.host}:{server.server_port} ({len(ADAPTERS)} adapters)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
