"""Mailbox clients reconstructed from public frontend calls and official docs."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from html import unescape
from html.parser import HTMLParser
from http.cookiejar import LWPCookieJar
import json
from pathlib import Path
import re
import secrets
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener

_NO_PAYLOAD = object()

LIVEWIRE_BASES = {"mailtemp_net": "https://mailtemp.net", "mailtemp_uk": "https://mailtemp.uk", "spambox_xyz": "https://spambox.xyz"}


class LivewirePage(HTMLParser):
    """Read signed server state as data; preserve Livewire 3 snapshot bytes."""
    def __init__(self):
        super().__init__()
        self.v2, self.v3, self.links, self.csrf = {}, {}, [], None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        try:
            if "wire:initial-data" in attrs:
                item = json.loads(attrs["wire:initial-data"])
                self.v2[item["fingerprint"]["name"]] = item
            if "wire:snapshot" in attrs:
                item = json.loads(attrs["wire:snapshot"])
                self.v3[item["memo"]["name"]] = attrs["wire:snapshot"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderError("unexpected_response", "Malformed server component state") from exc
        if tag == "meta" and attrs.get("name") == "csrf-token":
            self.csrf = attrs.get("content")
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])


def wire_value(value):
    """Decode PHP array wrappers only; unknown serialized objects remain errors."""
    if isinstance(value, list) and len(value) == 2 and isinstance(value[1], dict) and "s" in value[1]:
        if value[1]["s"] != "arr" or not isinstance(value[0], (list, dict)):
            raise ProviderError("unexpected_response", "Unsupported server-side value type")
        return wire_value(value[0])
    if isinstance(value, dict):
        return {key: wire_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [wire_value(item) for item in value]
    return value


def parse_tmail_parts(raw):
    """Parse LinShiYou's observed header/body delimiter protocol without running HTML."""
    if raw == "":
        return []
    if raw.strip() == "DIE":
        raise ProviderError("mailbox_expired", "The original TMail session expired")
    items = []
    for fragment in raw.split("<-----TMAILNEXTMAIL----->"):
        if not fragment.strip():
            continue
        parts = fragment.split("<-----TMAILCHOPPER----->")
        if len(parts) != 2 or not all(part.strip() for part in parts):
            raise ProviderError("unexpected_response", "Unrecognized TMail message framing")
        items.append({"id": "sha256-" + hashlib.sha256(fragment.encode()).hexdigest(),
                      "subject": None, "from": None, "html": parts[1]})
    if not items:
        raise ProviderError("unexpected_response", "Nonempty TMail response contained no message frames")
    return items


def parse_harakiri_page(body, prefix, skip):
    """Validate one observed page; callers must still assemble the complete inbox."""
    if not isinstance(body, dict) or body.get("inbox") != prefix:
        raise ProviderError("mailbox_expired_or_changed", "Response belongs to another inbox")
    items = body.get("emails")
    if (any(type(body.get(key)) is not int for key in ("skip", "limit", "count", "total_count")) or
            body["skip"] != skip or body["limit"] <= 0 or body["total_count"] < 0 or
            not isinstance(items, list) or body["count"] != len(items) or
            len(items) > body["limit"] or skip + len(items) > body["total_count"]):
        raise ProviderError("unexpected_response", "Mailbox pagination is inconsistent")
    if any(not isinstance(item, dict) or not isinstance(item.get("_id"), str) or not item["_id"] for item in items):
        raise ProviderError("unexpected_response", "Message identity missing")
    return [{**item, "id": item["_id"], "received_at": item.get("received")} for item in items], body["total_count"]


class MailRows(HTMLParser):
    """Parse the public 10MinuteMail table without running its scripts."""
    def __init__(self):
        super().__init__()
        self.items, self.cells, self.cell, self.mid = [], [], None, None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tr":
            self.cells, self.mid = [], None
        elif tag == "td":
            self.cell = []
        elif tag == "a" and self.cell is not None:
            target = urlsplit(attrs.get("href", ""))
            if target.path == "readmail.html":
                self.mid = parse_qs(target.query).get("mid", [None])[0]

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag == "td" and self.cell is not None:
            self.cells.append("".join(self.cell).strip())
            self.cell = None
        elif tag == "tr" and self.mid and len(self.cells) >= 3:
            self.items.append({"id": self.mid, "from": self.cells[0], "subject": self.cells[1], "date": self.cells[2]})


class MailFragment(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.depth, self.parts = 0, []

    def handle_starttag(self, tag, attrs):
        if not self.depth:
            if tag == "div" and "mailinhtml" in dict(attrs).get("class", "").split():
                self.depth = 1
            return
        self.parts.append(self.get_starttag_text())
        if tag not in ("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"):
            self.depth += 1

    def handle_startendtag(self, tag, attrs):
        if self.depth:
            self.parts.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if self.depth:
            self.depth -= 1
            if self.depth:
                self.parts.append("</" + tag + ">")

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)

    def handle_entityref(self, name):
        self.handle_data("&" + name + ";")

    def handle_charref(self, name):
        self.handle_data("&#" + name + ";")


class WebMailRows(HTMLParser):
    """Read message rows from saved, server-rendered inbox layouts."""
    def __init__(self, provider):
        super().__init__()
        self.provider, self.items, self.unparsed_rows = provider, [], 0
        self.cells, self.cell, self.mid, self.path = [], None, None, None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tr":
            self.cells, self.cell, self.path = [], None, None
            self.mid = attrs.get("data-msg-id") if self.provider == "mohmal" else None
        elif tag == "td":
            self.cell = []
        elif tag == "a" and self.provider == "moakt" and self.cell is not None:
            path = attrs.get("href", "")
            match = re.fullmatch(r"/en/inbox/([A-Za-z0-9_-]+)", path)
            if match and match.group(1) not in ("compose", "logout", "extend", "change", "delete"):
                self.mid, self.path = match.group(1), path

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag == "td" and self.cell is not None:
            self.cells.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr":
            cells = self.cells
            if self.mid and self.provider == "mohmal" and len(cells) >= 3:
                self.items.append({"id": self.mid, "subject": cells[0], "date": cells[1], "from": cells[2]})
            elif self.mid and self.provider == "moakt" and self.path and len(cells) >= 2:
                self.items.append({"id": self.mid, "subject": cells[0], "from": cells[1], "_path": self.path})
            elif len(cells) >= (3 if self.provider == "mohmal" else 2):
                self.unparsed_rows += 1
            self.mid = None


class ProviderError(RuntimeError):
    def __init__(self, code, message, status=None):
        super().__init__(message)
        self.code, self.status = code, status


CUSTOM_PREFIX_PROVIDERS = frozenset({'grabmail', 'tempmail_plus', 'byom', 'maildrop', 'inboxkitten'})


def normalize_address_prefix(value):
    """A user prefix plus 16 random hex characters fits a 64-byte local part."""
    if value is None:
        return ''
    if not isinstance(value, str):
        raise ProviderError('invalid_address_prefix', '邮箱前缀必须为字符串。')
    value = value.strip()
    if value and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,47}', value):
        raise ProviderError('invalid_address_prefix', '邮箱前缀限 1–48 位英文字母、数字和连字符，首位须为字母或数字。')
    return value


class Client:
    def __init__(self, folder):
        self.folder = Path(folder).resolve()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.config_path = self.folder / "inbox.local.json"
        self.cookies = LWPCookieJar(str(self.folder / "cookies.local.cookies"))
        if Path(self.cookies.filename).exists():
            self.cookies.load(ignore_discard=True, ignore_expires=False)
        self.opener = build_opener(HTTPCookieProcessor(self.cookies))
        self.config = json.loads(self.config_path.read_text(encoding="utf-8")) if self.config_path.exists() else {}

    def save(self):
        self.config_path.write_text(json.dumps(self.config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.cookies.save(ignore_discard=True, ignore_expires=False)

    def request(self, method, url, *, payload=_NO_PAYLOAD, form=None, raw_body=None, xhr=False, referer=None, max_bytes=2_000_000, headers_extra=None):
        if sum((payload is not _NO_PAYLOAD, form is not None, raw_body is not None)) > 1:
            raise ValueError("Only one request body representation is allowed")
        headers = {"User-Agent": "Mozilla/5.0 (compatible; LinsemailClient/0.2)", "Accept": "application/json,text/html;q=0.9,*/*;q=0.8"}
        headers.update(headers_extra or {})
        data = None
        if payload is not _NO_PAYLOAD:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif form is not None:
            data = urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        elif raw_body is not None:
            if not isinstance(raw_body, bytes):
                raise TypeError("raw_body must contain bytes")
            data = raw_body
            headers.setdefault("Content-Type", "text/plain; charset=UTF-8")
        if xhr:
            headers["X-Requested-With"] = "XMLHttpRequest"
        if referer:
            headers["Referer"] = referer
        stamp = datetime.now(timezone.utc)
        status, raw, error, content_type = None, b"", "", ""
        try:
            try:
                response = self.opener.open(Request(url, data=data, headers=headers, method=method), timeout=25)
            except HTTPError as exc:
                response = exc
            with response:
                status = response.status
                raw = response.read(max_bytes + 1)
                content_type = response.headers.get("Content-Type", "")
                if len(raw) > max_bytes:
                    error = f"Response exceeds {max_bytes} byte client limit"
        except (URLError, OSError) as exc:
            error = str(exc)
        text = raw.decode("utf-8", errors="replace")
        record = {"checked_at": stamp.isoformat(), "method": method, "url": url,
                  "request_payload": None if payload is _NO_PAYLOAD else payload, "request_form": form, "http_status": status,
                  "request_raw_body": raw_body.decode("utf-8") if raw_body is not None else None,
                  "json_payload_present": payload is not _NO_PAYLOAD,
                  "request_headers": {key: ("Bearer [local credential]" if key.lower() == "authorization" else value) for key, value in headers.items()},
                  "content_type": content_type, "stdout": text, "stderr": error,
                  "exit_status": 0 if status is not None and 200 <= status < 300 and not error else 1,
                  "sha256": hashlib.sha256(raw).hexdigest()}
        path = self.folder / (stamp.strftime("%Y%m%dT%H%M%S.%fZ") + "-http.local.json")
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.cookies.save(ignore_discard=True, ignore_expires=False)
        if record["exit_status"]:
            raise ProviderError("upstream_http_error", f"HTTP {status}; evidence={path}; {error}", status)
        return text

    def json(self, method, url, **kwargs):
        raw = self.request(method, url, **kwargs)
        try:
            return json.loads(raw.lstrip("\ufeff"))
        except ValueError as exc:
            raise ProviderError("unexpected_response", "Expected JSON; got a different upstream response") from exc

    def graphql(self, url, query, variables=None):
        body = self.json("POST", url, payload={"query": query, "variables": variables or {}})
        if body.get("errors") or not isinstance(body.get("data"), dict):
            raise ProviderError("upstream_graphql_error", "GraphQL did not return successful data")
        return body["data"]

    def guerrilla(self, function, **values):
        params = {"f": function, **values}
        if self.config.get("sid_token"):
            params["sid_token"] = self.config["sid_token"]
        body = self.json("GET", "https://api.guerrillamail.com/ajax.php?" + urlencode(params))
        if body.get("sid_token"):
            self.config["sid_token"] = body["sid_token"]
        return body

    @staticmethod
    def tenminute_one_page(page):
        """Decode Nuxt's public JSON payload without evaluating remote JavaScript."""
        match = re.search(r'<script[^>]+id="__NUXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
        try:
            data = json.loads(match.group(1))
            root = data[data[0][1]]
            state = data[root["state"]]
            state = data[state[1]]
            domains = [data[index] for index in data[state["$semailDomains"]]]
            token = data[root["mailServiceToken"]]
            if not isinstance(token, str) or not token or not domains or any(not re.fullmatch(r"[a-z0-9.-]+", domain) for domain in domains):
                raise ValueError("Missing public token/domains")
        except (AttributeError, ValueError, TypeError, KeyError, IndexError) as exc:
            raise ProviderError("unexpected_response", "Public Nuxt mailbox configuration missing") from exc
        return domains, token

    def tenminute_one_headers(self):
        return {"Authorization": "Bearer " + self.config["token"],
                "X-Request-ID": secrets.token_hex(16), "X-Timestamp": str(int(time.time()))}

    def twentytwo_token(self):
        if self.config.get("token") and float(self.config.get("token_expires_at", 0)) > time.time():
            return self.config["token"]
        body = self.json("POST", "https://22.do/action/mailbox/applyToken",
                         payload={"email": self.config["address"], "uuid": self.config["client_id"]}, referer="https://22.do/")
        if not isinstance(body, dict) or body.get("status") is not True or not isinstance(body.get("data"), dict) or not body["data"].get("token"):
            raise ProviderError("token_not_granted", "Mailbox token not granted; interactive challenges are not automated")
        self.config.update(token=body["data"]["token"], token_expires_at=body["data"]["expireTime"])
        self.save()
        return self.config["token"]

    def livewire_call(self, method, params, *, event=False):
        base = LIVEWIRE_BASES[self.config["provider"]]
        headers = {"X-Livewire": "true"}
        if self.config["wire_version"] == 2:
            component = self.config["wire_component"]
            name = component["fingerprint"]["name"]
            if name not in ("frontend.actions", "frontend.app"):
                raise ProviderError("unexpected_response", "Unexpected mailbox component")
            payload = {"fingerprint": component["fingerprint"], "serverMemo": component["serverMemo"],
                       "updates": [{"type": "fireEvent" if event else "callMethod", "payload": {
                           "id": secrets.token_hex(8), "event" if event else "method": method, "params": params}}]}
            headers["X-CSRF-TOKEN"] = self.config["csrf"]
            body = self.json("POST", base + "/livewire/message/" + name, payload=payload, headers_extra=headers, referer=base + "/")
            if not isinstance(body, dict) or not isinstance(body.get("serverMemo"), dict):
                raise ProviderError("unexpected_response", "Mailbox component update missing")
            update = body["serverMemo"]
            if update.get("errors"):
                raise ProviderError("upstream_validation_error", "Mailbox action requires additional validation")
            if "data" in update and not isinstance(update["data"], dict):
                raise ProviderError("unexpected_response", "Mailbox component data is not an object")
            old_data = component["serverMemo"].get("data", {})
            component["serverMemo"].update(update)
            component["serverMemo"]["data"] = {**old_data, **update.get("data", {})}
            effects = body.get("effects", {})
        else:
            call = {"path": "", "method": "__dispatch" if event else method,
                    "params": [method, params] if event else params}
            body = self.json("POST", base + "/livewire/update", payload={"_token": self.config["csrf"],
                "components": [{"snapshot": self.config["wire_component"], "updates": {}, "calls": [call]}]},
                headers_extra=headers, referer=base + "/")
            try:
                previous = json.loads(self.config["wire_component"])
                returned = body["components"][0]
                snapshot = returned["snapshot"]
                parsed = json.loads(snapshot)
                if any(parsed["memo"][key] != previous["memo"][key] for key in ("name", "id")):
                    raise ValueError("Mailbox component identity changed")
            except (ValueError, TypeError, KeyError, IndexError) as exc:
                raise ProviderError("unexpected_response", "Mailbox snapshot update missing") from exc
            if parsed["memo"].get("errors"):
                raise ProviderError("upstream_validation_error", "Mailbox action requires additional validation")
            self.config["wire_component"] = snapshot
            effects = returned.get("effects", {})
        rendered = effects.get("html")
        if rendered is not None and not isinstance(rendered, str):
            raise ProviderError("unexpected_response", "Mailbox rendering is not HTML")
        self.config["wire_last_html"] = rendered or ""
        self.save()
        return effects

    def livewire_data(self):
        if self.config["wire_version"] == 2:
            return self.config["wire_component"]["serverMemo"]["data"]
        return wire_value(json.loads(self.config["wire_component"])["data"])

    def livewire_create(self, provider):
        base = LIVEWIRE_BASES[provider]
        page = self.request("GET", base + "/")
        parser = LivewirePage()
        parser.feed(page)
        version = 3 if provider == "spambox_xyz" else 2
        csrf = parser.csrf if version == 3 else (re.search(r"window\.livewire_token\s*=\s*['\"]([^'\"]+)", page) or [None, None])[1]
        if not csrf:
            raise ProviderError("session_initialization_failed", "Mailbox CSRF missing")
        self.config.update(csrf=csrf, wire_version=version)
        try:
            if provider != "mailtemp_net":
                self.config["wire_component"] = parser.v3["frontend.actions"] if version == 3 else parser.v2["frontend.actions"]
                effects = self.livewire_call("random", [])
                target = urljoin(base + "/", effects.get("redirect", ""))
                if not effects.get("redirect") or urlsplit(target).netloc != urlsplit(base).netloc:
                    raise ProviderError("session_initialization_failed", "Random mailbox was not granted a valid redirect")
                page = self.request("GET", target)
                parser = LivewirePage()
                parser.feed(page)
                if version == 3:
                    self.config["csrf"] = parser.csrf
                else:
                    self.config["csrf"] = re.search(r"window\.livewire_token\s*=\s*['\"]([^'\"]+)", page).group(1)
            self.config["wire_component"] = parser.v3["frontend.app"] if version == 3 else parser.v2["frontend.app"]
            identity = re.search(r"const email\s*=\s*['\"]([^'\"]+@[^'\"]+)", page)
            address = identity.group(1) if identity else self.livewire_data().get("email")
            if not isinstance(address, str) or "@" not in address or not self.config.get("csrf"):
                raise ValueError("Mailbox identity missing")
            self.config["address"] = address
        except (KeyError, ValueError, AttributeError, TypeError) as exc:
            raise ProviderError("session_initialization_failed", "Signed mailbox component or identity missing") from exc
        self.livewire_call("syncEmail", {"email": address} if version == 3 else [address], event=True)

    def anonymmail_post(self, path, form):
        def post():
            return self.json("POST", "https://anonymmail.net" + path, form=form, xhr=True,
                             headers_extra={"X-CSRF-TOKEN": self.config["csrf"]}, referer="https://anonymmail.net/")
        try:
            return post()
        except ProviderError as exc:
            if exc.status != 403:
                raise
        # The inspected frontend refreshes an expired CSRF once, using the same session.
        body = self.json("GET", "https://anonymmail.net/api/csrf", xhr=True, referer="https://anonymmail.net/")
        if not isinstance(body, dict) or not isinstance(body.get("hash"), str) or not body["hash"]:
            raise ProviderError("session_initialization_failed", "CSRF refresh was not granted")
        self.config["csrf"] = body["hash"]
        self.save()
        return post()

    def create(self, provider):
        if self.config.get("address"):
            raise ProviderError("existing_mailbox", "Existing mailbox preserved; use list_messages")
        requested_prefix = normalize_address_prefix(self.config.get('address_prefix'))
        if requested_prefix and provider not in CUSTOM_PREFIX_PROVIDERS:
            raise ProviderError('custom_prefix_unsupported', '该来源暂不支持自定义邮箱前缀。')
        self.config = {"provider": provider, "created_at": datetime.now(timezone.utc).isoformat()}
        if requested_prefix:
            self.config['address_prefix'] = requested_prefix
        prefix = requested_prefix + secrets.token_hex(8)
        if provider == "grabmail":
            domains = self.json("GET", "https://grabmail.io/api/v1/domains")
            self.config["address"] = prefix + "@" + domains["public"][0]
        elif provider == "maildax":
            body = self.json("POST", "https://api2.maildax.com/api/email", payload={})
            account = body.get("data", body)
            if not isinstance(account, dict) or not account.get("email") or not account.get("secret"):
                raise ProviderError("unexpected_response", "Mailbox email/secret missing from create response")
            self.config.update(address=account["email"], secret=account["secret"])
        elif provider == "fakemail":
            html = self.request("GET", "https://www.fakemail.net/")
            csrf = re.search(r'\bCSRF\s*=\s*["\x27]([^"\x27]+)', html)
            if not csrf:
                raise ProviderError("session_initialization_failed", "Frontend CSRF value was not present")
            body = self.json("GET", "https://www.fakemail.net/index/index?" + urlencode({"csrf_token": csrf.group(1)}), xhr=True, referer="https://www.fakemail.net/")
            if not isinstance(body, dict) or not body.get("email"):
                raise ProviderError("unexpected_response", "Create response did not contain an email address")
            self.config["address"] = body["email"]
        elif provider == "tempmail_plus":
            html = self.request("GET", "https://tempmail.plus/")
            domains = re.findall(r'<button type="button" class="dropdown-item">([^<]+)</button>', html)
            if not domains:
                raise ProviderError("unexpected_response", "Frontend domain list missing")
            self.config.update(address=prefix + "@" + domains[0], epin="")
        elif provider == "byom":
            self.config["address"] = prefix + "@byom.de"
        elif provider == "guerrillamail":
            body = self.guerrilla("get_email_address", lang="en")
            self.config["address"] = body["email_addr"]
        elif provider == "tempmail_lol":
            body = self.json("POST", "https://api.tempmail.lol/v2/inbox/create", payload={"domain": None, "prefix": None})
            self.config.update(address=body["address"], token=body["token"])
        elif provider == "maildrop":
            self.config["address"] = prefix + "@maildrop.cc"
        elif provider == "dropmail":
            token = self.json("POST", "https://dropmail.me/api/token/generate", payload={"type": "af", "lifetime": "1h"})["token"]
            self.config["api_token"] = token
            body = self.graphql("https://dropmail.me/api/graphql/" + token,
                                "mutation { introduceSession { id expiresAt addresses { address } } }")["introduceSession"]
            self.config.update(address=body["addresses"][0]["address"], session_id=body["id"], expires_at=body["expiresAt"])
        elif provider == "10minutemail_net":
            page = self.request("GET", "https://10minutemail.net/")
            match = re.search(r'id="fe_text"[^>]+value="([^"<>]+@[^"<>]+)"', page)
            if not match:
                raise ProviderError("session_initialization_failed", "Mailbox address not found in public page")
            self.config["address"] = match.group(1)
        elif provider == "duckmail":
            body = self.json("GET", "https://api.duckmail.sbs/domains")
            domains = [item["domain"] for item in body["hydra:member"] if item.get("isActive", True) and not item.get("isPrivate", False)]
            if not domains:
                raise ProviderError("unexpected_response", "No public active domain")
            address, password = prefix + "@" + domains[0], secrets.token_urlsafe(24)
            self.json("POST", "https://api.duckmail.sbs/accounts", payload={"address": address, "password": password})
            body = self.json("POST", "https://api.duckmail.sbs/token", payload={"address": address, "password": password})
            self.config.update(address=address, password=password, token=body["token"])
        elif provider == "moakt":
            page = self.request("GET", "https://www.moakt.com/")
            domains = re.findall(r'<option value="([A-Za-z0-9.-]+)">', page)
            if not domains:
                raise ProviderError("unexpected_response", "Public domain selector missing")
            page = self.request("POST", "https://www.moakt.com/en/inbox", form={"domain": domains[0], "username": prefix,
                                "setemail": "Create", "preferred_domain": domains[0]}, referer="https://www.moakt.com/")
            address = re.search(r'id="email-address"[^>]*>\s*([^<]+@[^<]+)\s*</div>', page)
            if not address:
                raise ProviderError("session_initialization_failed", "Created address missing from inbox page")
            self.config["address"] = unescape(address.group(1)).strip()
        elif provider == "inboxkitten":
            self.config["address"] = prefix + "@inboxkitten.com"
        elif provider == "mohmal":
            page = self.request("GET", "https://www.mohmal.com/en/create/random")
            address = re.search(r'data-email="([^"<>]+@[^"<>]+)"', page)
            if not address:
                raise ProviderError("session_initialization_failed", "Created address missing from inbox page")
            self.config["address"] = unescape(address.group(1))
        elif provider == "awamail":
            page = self.request("GET", "https://awamail.com/?lang=zh")
            address = re.search(r'id="email-input"[^>]*\bvalue="([^"<>]+@[^"<>]+)"', page)
            if not address:
                raise ProviderError("session_initialization_failed", "Session mailbox missing from page")
            self.config["address"] = unescape(address.group(1)).strip()
        elif provider == "tempmail_ing":
            body = self.json("POST", "https://api.tempmail.ing/api/generate", payload={"duration": 60})
            if body.get("success") is not True or not isinstance(body.get("email"), dict):
                raise ProviderError("unexpected_response", "Mailbox generation failed")
            self.config.update(address=body["email"]["address"], expires_at=body["email"]["expiresAt"])
        elif provider == "minuteinbox":
            page = self.request("GET", "https://www.minuteinbox.com/")
            match = re.search(r'\bCSRF\s*=\s*["\x27]([^"\x27]+)', page)
            if not match:
                raise ProviderError("session_initialization_failed", "Frontend CSRF missing")
            self.config["csrf"] = match.group(1)
            body = self.json("GET", "https://www.minuteinbox.com/index/index?" + urlencode({"csrf_token": self.config["csrf"]}),
                             xhr=True, referer="https://www.minuteinbox.com/")
            if not isinstance(body, dict) or not body.get("email"):
                raise ProviderError("unexpected_response", "Mailbox address missing")
            self.config["address"] = body["email"]
        elif provider == "tempmailto":
            page = self.request("GET", "https://tempmailto.com/")
            match = re.search(r'<meta\b[^>]*name="csrf-token"[^>]*content="([^"<>]+)"', page)
            if not match:
                raise ProviderError("session_initialization_failed", "Frontend CSRF missing")
            self.config["csrf"] = unescape(match.group(1))
            body = self.json("POST", "https://tempmailto.com/get_messages", payload={"_token": self.config["csrf"]},
                             xhr=True, referer="https://tempmailto.com/")
            if not isinstance(body, dict) or body.get("status") is not True or not isinstance(body.get("messages"), list) or not body.get("mailbox"):
                raise ProviderError("session_initialization_failed", "Public session mailbox not granted")
            self.config["address"] = body["mailbox"]
        elif provider == "10minutemail_one":
            page = self.request("GET", "https://10minutemail.one/")
            domains, token = self.tenminute_one_page(page)
            self.config.update(address=prefix + "@" + domains[0], token=token)
        elif provider == "22do":
            page = self.request("GET", "https://22.do/")
            match = re.search(r'\bconst domains\s*=\s*(\[[^;]+\])', page)
            try:
                domains = json.loads(match.group(1))
                domain = domains[0]
                if not re.fullmatch(r"[a-z0-9.-]+", domain) or domain in ("gmail.com", "googlemail.com", "hotmail.com", "outlook.com"):
                    raise ValueError("Expected provider's own public domain")
            except (AttributeError, ValueError, IndexError, TypeError) as exc:
                raise ProviderError("unexpected_response", "Public domain list missing") from exc
            address = "lstasklet" + secrets.token_hex(8) + "@" + domain
            body = self.json("POST", "https://22.do/action/mailbox/login", payload={"email": address, "language": "en-US"}, referer="https://22.do/")
            if not isinstance(body, dict) or body.get("status") is not True:
                raise ProviderError("session_initialization_failed", "Dedicated mailbox not granted")
            self.config.update(address=address, client_id=secrets.token_hex(16))
        elif provider == "anonymmail":
            page = self.request("GET", "https://anonymmail.net/")
            identity = re.search(r'<b\b[^>]*class="[^"]*\bemail\b[^"]*"[^>]*>\s*([^<]+@[^<]+)\s*</b>', page)
            csrf = re.search(r'name="csrf_test_name"\s+value="([^"]+)"', page)
            if not identity or not csrf:
                raise ProviderError("session_initialization_failed", "Dedicated address or CSRF missing")
            address = unescape(identity.group(1)).strip()
            if not re.fullmatch(r"[^\s@]+@[^\s@]+", address) or address.rsplit("@", 1)[1].lower() in ("gmail.com", "outlook.com", "hotmail.com"):
                raise ProviderError("session_initialization_failed", "Expected a provider-owned temporary address")
            self.config.update(address=address, csrf=csrf.group(1))
        elif provider == "tempmailbox_net":
            body = self.json("POST", "https://tempmailbox.net/api/mailbox", payload={}, referer="https://tempmailbox.net/")
            mailbox = body.get("mailbox") if isinstance(body, dict) else None
            if not isinstance(mailbox, dict) or any(not isinstance(mailbox.get(key), str) or not mailbox[key] for key in ("id", "address", "expiresAt")):
                raise ProviderError("session_initialization_failed", "Dedicated mailbox identity missing")
            self.config.update(address=mailbox["address"], mailbox=mailbox)
        elif provider == "mailforspam":
            self.config["address"] = "ls" + secrets.token_hex(8) + "@mailforspam.com"
        elif provider == "linshiyou":
            address = "lstasklet" + secrets.token_hex(10) + "@linshiyou.com"
            returned = self.request("GET", "https://linshiyou.com/api/user?" + urlencode({"user": address}), xhr=True, referer="https://linshiyou.com/").strip()
            if returned != address:
                raise ProviderError("mailbox_identity_not_granted", "The requested dedicated address was not returned")
            self.config["address"] = address
        elif provider == "harakirimail":
            prefix = "lstasklet" + secrets.token_hex(10)
            self.config.update(address=prefix + "@harakirimail.com", prefix=prefix)
        elif provider == "tempemail_co":
            body = self.json("GET", "https://tempemail.co/home/api_init")
            if not isinstance(body, dict) or not isinstance(body.get("domains"), list) or "tempemail.co" not in body["domains"]:
                raise ProviderError("session_initialization_failed", "Provider-owned domain was not offered")
            address = "lstasklet" + secrets.token_hex(10) + "@tempemail.co"
            self.config["address"] = address
            self.save()
            body = self.json("GET", "https://tempemail.co/home/user?" + urlencode({"user": address}))
            if not isinstance(body, dict) or body.get("address") != address:
                raise ProviderError("mailbox_identity_not_granted", "Requested dedicated address was not granted")
        elif provider in LIVEWIRE_BASES:
            self.livewire_create(provider)
        elif provider == "emailtemp_org":
            page = self.request("GET", "https://emailtemp.org/")
            parser = LivewirePage()
            parser.feed(page)
            if not parser.csrf:
                raise ProviderError("session_initialization_failed", "Mailbox CSRF missing")
            body = self.json("POST", "https://emailtemp.org/messages", form={"_token": parser.csrf, "captcha": ""}, xhr=True, referer="https://emailtemp.org/")
            if not isinstance(body, dict) or not body.get("mailbox") or not isinstance(body.get("messages"), list):
                raise ProviderError("session_initialization_failed", "Public session mailbox not granted")
            self.config.update(address=body["mailbox"], csrf=parser.csrf)
        elif provider == "tempmail100":
            body = self.json("POST", "https://tempmail100.com/init", xhr=True, referer="https://tempmail100.com/")
            if not isinstance(body, dict) or body.get("code") != 0 or not isinstance(body.get("data"), dict) or not body["data"].get("token"):
                raise ProviderError("session_initialization_failed", "Visitor mailbox token not granted")
            self.config.update(token=body["data"]["token"], address=body["data"].get("address"))
            if not self.config["address"]:
                self.save()  # Keep the issued token if generation fails; no automatic repeated create.
                body = self.json("POST", "https://tempmail100.com/web/generate", xhr=True,
                                 headers_extra={"Authorization": self.config["token"]}, referer="https://tempmail100.com/")
                if not isinstance(body, dict) or body.get("code") != 0 or not body.get("data", {}).get("address"):
                    raise ProviderError("session_initialization_failed", "Dedicated mailbox not generated")
                self.config["address"] = body["data"]["address"]
        else:
            raise ProviderError("unsupported_provider", provider)
        self.save()
        self.list_messages()
        return {"provider": provider, "address": self.config["address"]}

    def list_messages(self):
        provider = self.config.get("provider")
        address = self.config["address"]
        if provider == "grabmail":
            body = self.json("GET", "https://grabmail.io/api/v1/mailbox?" + urlencode({"address": address}))
            items = body.get("messages")
        elif provider == "maildax":
            body = self.json("GET", "https://api2.maildax.com/api/email/mails?" + urlencode({"email": address, "secret": self.config["secret"]}))
            items = body.get("data")
        elif provider == "fakemail":
            body = self.json("GET", "https://www.fakemail.net/index/refresh", xhr=True, referer="https://www.fakemail.net/")
            items = [] if body == 0 else body
        elif provider == "tempmail_plus":
            body = self.json("GET", "https://tempmail.plus/api/mails?" + urlencode({"email": address, "limit": 20, "epin": self.config["epin"]}), xhr=True, referer="https://tempmail.plus/")
            if not body.get("result"):
                raise ProviderError("upstream_api_error", str(body.get("err", "Inbox request failed")))
            items = body.get("mail_list")
        elif provider == "byom":
            prefix = address.split("@", 1)[0]
            raw = self.request("GET", "https://api.byom.de/mails/" + quote(prefix, safe="") + "?alt=json-in-script&callback=linsemail")
            match = re.fullmatch(r"\s*linsemail\((.*)\);?\s*", raw, re.S)
            if not match:
                raise ProviderError("unexpected_response", "Expected a JSONP response")
            try:
                items = json.loads(match.group(1))
            except ValueError as exc:
                raise ProviderError("unexpected_response", "Invalid JSONP payload") from exc
        elif provider == "guerrillamail":
            items = self.guerrilla("get_email_list", offset=0).get("list")
            if isinstance(items, list):
                items = [{**item, "id": str(item["mail_id"]), "from": item.get("mail_from"),
                          "subject": item.get("mail_subject"), "date": item.get("mail_date")} for item in items]
        elif provider == "tempmail_lol":
            body = self.json("GET", "https://api.tempmail.lol/v2/inbox?" + urlencode({"token": self.config["token"]}))
            if body.get("expired"):
                raise ProviderError("mailbox_expired", "Mailbox token expired")
            items = body.get("emails") or []
            if isinstance(items, list):
                items = [{**item, "id": str(item.get("id") or hashlib.sha256(json.dumps(item, sort_keys=True, ensure_ascii=False).encode()).hexdigest())} for item in items]
        elif provider == "maildrop":
            items = self.graphql("https://api.maildrop.cc/graphql",
                                 "query Inbox($mailbox: String!) { inbox(mailbox:$mailbox) { id headerfrom subject date } }",
                                 {"mailbox": address.split("@", 1)[0]}).get("inbox")
            if isinstance(items, list):
                items = [{**item, "from": item.get("headerfrom")} for item in items]
        elif provider == "dropmail":
            session = self.graphql("https://dropmail.me/api/graphql/" + self.config["api_token"],
                                   "query Inbox($id: ID!) { session(id:$id) { id expiresAt mails { id fromAddr headerSubject text html receivedAt } } }",
                                   {"id": self.config["session_id"]}).get("session")
            if not session:
                raise ProviderError("mailbox_expired", "DropMail session expired")
            self.config["expires_at"] = session["expiresAt"]
            items = [{**item, "from": item.get("fromAddr"), "subject": item.get("headerSubject"),
                      "received_at": item.get("receivedAt")} for item in session["mails"]]
        elif provider == "10minutemail_net":
            page = self.request("GET", "https://10minutemail.net/mailbox.ajax.php", xhr=True, referer="https://10minutemail.net/")
            if not re.search(r'<table\b[^>]*\bid=["\x27]maillist["\x27]', page):
                raise ProviderError("mailbox_expired", "Expected inbox table; session may have expired")
            parser = MailRows()
            parser.feed(page)
            items = parser.items
        elif provider == "duckmail":
            body = self.json("GET", "https://api.duckmail.sbs/messages", headers_extra={"Authorization": "Bearer " + self.config["token"]})
            items = body.get("hydra:member")
            if isinstance(items, list):
                items = [{**item, "received_at": item.get("createdAt")} for item in items]
        elif provider in ("moakt", "mohmal"):
            url = {"moakt": "https://www.moakt.com/en/inbox", "mohmal": "https://www.mohmal.com/en/refresh"}[provider]
            page = self.request("GET", url)
            if provider == "moakt":
                identity = re.search(r'id="email-address"[^>]*>\s*([^<]+)\s*</div>', page)
                valid = identity and unescape(identity.group(1)).strip() == address and 'id="email_message_list"' in page
            elif provider == "mohmal":
                identity = re.search(r'data-email="([^"<>]+@[^"<>]+)"', page)
                valid = identity and unescape(identity.group(1)) == address and 'id="inbox-table"' in page
            if not valid:
                raise ProviderError("mailbox_expired_or_unexpected_html", "Expected this mailbox's inbox and identity")
            parser = WebMailRows(provider)
            parser.feed(page)
            if parser.unparsed_rows:
                raise ProviderError("unexpected_response", "Unrecognized message rows; refusing to report an empty inbox")
            items = parser.items
        elif provider == "inboxkitten":
            body = self.json("GET", "https://inboxkitten.com/api/v1/mail/list?" + urlencode({"recipient": address.split("@", 1)[0]}))
            items = body
            if isinstance(items, list):
                items = [{**item, "id": str(item["storage"]["key"]), "from": item.get("sender"),
                          "subject": item.get("message", {}).get("headers", {}).get("subject"), "date": item.get("timestamp")} for item in items]
        elif provider == "awamail":
            body = self.json("GET", "https://awamail.com/welcome/get_emails", xhr=True, referer="https://awamail.com/")
            if body.get("success") is not True:
                raise ProviderError("upstream_api_error", "Mailbox list failed")
            items = body.get("data", {}).get("emails")
            if isinstance(items, list):
                items = [{**item, "from": item.get("from_address"), "text": item.get("text_content"), "html": item.get("html_content")} for item in items]
        elif provider == "tempmail_ing":
            if self.config.get("expires_at") and datetime.fromisoformat(self.config["expires_at"].replace("Z", "+00:00")) <= datetime.now(timezone.utc):
                raise ProviderError("mailbox_expired", "Allocated address has expired")
            body = self.json("GET", "https://api.tempmail.ing/api/emails/" + quote(address, safe=""))
            if body.get("success") is not True:
                raise ProviderError("upstream_api_error", "Mailbox list failed")
            items = body.get("emails")
            if isinstance(items, list):
                items = [{**item, "from": item.get("from_address")} for item in items]
        elif provider == "minuteinbox":
            identity = self.json("GET", "https://www.minuteinbox.com/index/index?" + urlencode({"csrf_token": self.config["csrf"]}),
                                 xhr=True, referer="https://www.minuteinbox.com/")
            if not isinstance(identity, dict) or identity.get("email") != address:
                raise ProviderError("mailbox_expired_or_changed", "Session no longer belongs to the recorded mailbox")
            body = self.json("GET", "https://www.minuteinbox.com/index/refresh", xhr=True, referer="https://www.minuteinbox.com/")
            items = [] if type(body) is int and body == 0 else body
        elif provider == "tempmailto":
            body = self.json("POST", "https://tempmailto.com/get_messages", payload={"_token": self.config["csrf"]},
                             xhr=True, referer="https://tempmailto.com/")
            if not isinstance(body, dict) or body.get("status") is not True:
                raise ProviderError("upstream_api_error", "Public session mailbox not available")
            if body.get("mailbox") != address:
                raise ProviderError("mailbox_expired_or_changed", "Returned mailbox differs from recorded address")
            items = body.get("messages")
            if isinstance(items, list):
                items = [{**item, "from": item.get("from_email", item.get("from"))} for item in items]
        elif provider == "10minutemail_one":
            items = self.json("GET", "https://web.10minutemail.one/api/v1/mailbox/" + quote(address, safe=""),
                              headers_extra=self.tenminute_one_headers())
        elif provider == "22do":
            body = self.json("POST", "https://22.do/action/mailbox/message", payload={"email": address, "lastime": 0},
                             headers_extra={"Authorization": "Bearer " + self.twentytwo_token()}, referer="https://22.do/")
            if not isinstance(body, dict) or body.get("status") is not True or "data" not in body:
                raise ProviderError("upstream_api_error", "Mailbox list not granted")
            # The observed public frontend explicitly uses null when no messages exist.
            items = [] if body["data"] is None else body["data"]
            if isinstance(items, list):
                if any(not isinstance(item, dict) or not item.get("messageId") for item in items):
                    raise ProviderError("unexpected_response", "Message identifiers missing")
                items = [{**item, "id": str(item["messageId"]), "date": item.get("time")} for item in items]
        elif provider == "anonymmail":
            body = self.anonymmail_post("/api/get", {"email": address})
            if not isinstance(body, dict) or body.get("error") or not isinstance(body.get(address), dict):
                raise ProviderError("mailbox_expired_or_changed", "Original mailbox was not granted")
            items = body[address].get("emails")
            if isinstance(items, list):
                if any(not isinstance(item, dict) or not isinstance(item.get("token"), str) or not item["token"] for item in items):
                    raise ProviderError("unexpected_response", "Message token missing")
                items = [{**item, "id": item["token"], "html": item.get("body")} for item in items]
        elif provider == "tempmailbox_net":
            mailbox = self.config["mailbox"]
            try:
                expires = datetime.fromisoformat(mailbox["expiresAt"].replace("Z", "+00:00"))
                if expires.tzinfo is None:
                    raise ValueError("Timezone missing")
            except (KeyError, ValueError, TypeError, AttributeError) as exc:
                raise ProviderError("unexpected_response", "Mailbox expiry is invalid") from exc
            if datetime.now(timezone.utc) >= expires:
                raise ProviderError("mailbox_expired", "Original mailbox expired; no replacement requested")
            current = self.json("GET", "https://tempmailbox.net/api/mailbox", referer="https://tempmailbox.net/")
            value = current.get("mailbox") if isinstance(current, dict) else None
            if not isinstance(value, dict) or value.get("id") != mailbox["id"] or value.get("address") != address:
                raise ProviderError("mailbox_expired_or_changed", "Session mailbox changed")
            body = self.json("GET", "https://tempmailbox.net/api/messages?" + urlencode({"mailbox": mailbox["id"]}), referer="https://tempmailbox.net/")
            items = body.get("messages") if isinstance(body, dict) else None
        elif provider == "mailforspam":
            body = self.json("GET", "https://api.mailforspam.com/api/mailboxes/" + quote(address, safe="") +
                             "/emails?page=1&page_size=100&sort_by=date&sort_order=desc", referer="https://mailforspam.com/")
            if not isinstance(body, dict) or body.get("mailbox") != address:
                raise ProviderError("mailbox_expired_or_changed", "Response belongs to a different mailbox")
            items = body.get("emails")
            if not isinstance(items, list) or type(body.get("total_emails")) is not int or body["total_emails"] != len(items):
                raise ProviderError("unexpected_response", "Message list is missing or incomplete")
            if any(not isinstance(item, dict) or not item.get("id") for item in items):
                raise ProviderError("unexpected_response", "Message ID missing")
            items = [{**item, "from": item.get("sender")} for item in items]
        elif provider == "linshiyou":
            # Select the same generated address, as the public frontend does on reload.
            returned = self.request("GET", "https://linshiyou.com/api/user?" + urlencode({"user": address}), xhr=True, referer="https://linshiyou.com/").strip()
            if returned != address:
                raise ProviderError("mailbox_expired_or_changed", "Original address was not preserved")
            items = parse_tmail_parts(self.request("GET", "https://linshiyou.com/api/mail", xhr=True, referer="https://linshiyou.com/"))
        elif provider == "harakirimail":
            prefix = self.config["prefix"]
            if address != prefix + "@harakirimail.com":
                raise ProviderError("mailbox_identity_not_granted", "Stored mailbox identities disagree")
            items, total, skip = [], None, 0
            while True:
                body = self.json("GET", "https://harakirimail.com/api/v1/inbox/" + quote(prefix, safe="") + "?" + urlencode({"skip": skip}))
                page, observed_total = parse_harakiri_page(body, prefix, skip)
                if total is not None and total != observed_total:
                    raise ProviderError("incomplete_mailbox_observation", "Mailbox changed during pagination; retry the same mailbox")
                total = observed_total
                if total > 200:
                    raise ProviderError("incomplete_mailbox_observation", "Mailbox exceeds the 200-message client limit")
                items.extend(page)
                if len({item["id"] for item in items}) != len(items):
                    raise ProviderError("unexpected_response", "Repeated message identity across pages")
                if len(items) == total:
                    break
                if not page:
                    raise ProviderError("incomplete_mailbox_observation", "Empty page before declared end of inbox")
                skip += len(page)
        elif provider == "tempemail_co":
            body = self.json("GET", "https://tempemail.co/home/api_list")
            if not isinstance(body, dict) or body.get("result") is not True:
                raise ProviderError("upstream_api_error", "Inbox list was not granted")
            if body.get("address") != address:
                raise ProviderError("mailbox_expired_or_changed", "Original session mailbox changed")
            items = body.get("mails")
            if (not isinstance(items, list) or any(not isinstance(item, dict) or type(item.get("id")) not in (str, int) or str(item["id"]) == "" for item in items) or
                    len({str(item["id"]) for item in items}) != len(items)):
                raise ProviderError("unexpected_response", "Message list or identities missing")
            items = [{**item, "id": str(item["id"]), "from": item.get("fromAddress", item.get("from"))} for item in items]
        elif provider in LIVEWIRE_BASES:
            self.livewire_call("fetchMessages", {} if self.config["wire_version"] == 3 else [], event=True)
            data = self.livewire_data()
            if data.get("email") != address:
                raise ProviderError("mailbox_expired_or_changed", "Mailbox identity changed")
            if data.get("error"):
                raise ProviderError("upstream_api_error", "Mailbox reports an upstream error")
            items = data.get("messages")
            if isinstance(items, list):
                parser = LivewirePage()
                parser.feed(self.config.get("wire_last_html", ""))
                normalized = []
                for item in items:
                    if not isinstance(item, dict) or not self.message_id(item):
                        raise ProviderError("unexpected_response", "Message ID missing from mailbox snapshot")
                    item = dict(item)
                    for link in parser.links:
                        target = urljoin(LIVEWIRE_BASES[provider] + "/", link)
                        parts = urlsplit(target)
                        if parts.netloc == urlsplit(LIVEWIRE_BASES[provider]).netloc and unquote(parts.path.rstrip("/").rsplit("/", 1)[-1]) == self.message_id(item):
                            item["_read_url"] = target
                            break
                    normalized.append(item)
                items = normalized
        elif provider == "emailtemp_org":
            body = self.json("POST", "https://emailtemp.org/messages", form={"_token": self.config["csrf"], "captcha": ""}, xhr=True, referer="https://emailtemp.org/")
            if not isinstance(body, dict) or body.get("mailbox") != address:
                raise ProviderError("mailbox_expired_or_changed", "Mailbox identity changed or was not granted")
            items = body.get("messages")
            if isinstance(items, list):
                items = [{**item, "from": item.get("from_email", item.get("from"))} for item in items]
        elif provider == "tempmail100":
            body = self.json("GET", "https://tempmail100.com/web/emails", xhr=True,
                             headers_extra={"Authorization": self.config["token"]}, referer="https://tempmail100.com/")
            if not isinstance(body, dict) or body.get("code") != 0 or not isinstance(body.get("data"), dict):
                raise ProviderError("upstream_api_error", "Mailbox list not granted")
            items = body["data"].get("list")
            # The public frontend and observed empty inbox both use null at total=0.
            if "list" in body["data"] and items is None and body["data"].get("total") == 0:
                items = []
            if not isinstance(items, list) or body["data"].get("total") != len(items):
                raise ProviderError("unexpected_response", "Mailbox list and declared total disagree")
            if any(not isinstance(item, dict) or not item.get("uuid") for item in items):
                raise ProviderError("unexpected_response", "Message UUID missing")
            items = [{**item, "id": str(item["uuid"]), "from": item.get("fromAddress"), "date": item.get("timestamp")} for item in items]
        else:
            raise ProviderError("unsupported_provider", provider)
        if not isinstance(items, list):
            raise ProviderError("unexpected_response", "Inbox response did not contain a message list")
        if any(not isinstance(item, dict) for item in items):
            raise ProviderError("unexpected_response", "Invalid message entry")
        self.config["message_ids"] = [self.message_id(item) for item in items if self.message_id(item)]
        (self.folder / "messages.local.json").write_text(json.dumps(items, ensure_ascii=False) + "\n", encoding="utf-8")
        self.save()
        return items

    @staticmethod
    def message_id(item):
        return str(item.get("id", item.get("_id", item.get("mail_id", ""))))

    def get_message(self, message_id):
        message_id = str(message_id)
        if message_id not in self.config.get("message_ids", []):
            raise ProviderError("message_not_listed", "Read the message list first; message does not belong to this inbox")
        provider = self.config["provider"]
        if provider in ("harakirimail", "tempemail_co"):
            cached = json.loads((self.folder / "messages.local.json").read_text(encoding="utf-8"))
            item = next(item for item in cached if self.message_id(item) == message_id)
            if provider == "harakirimail":
                return {**item, "html": self.request("GET", "https://harakirimail.com/email/" + quote(message_id, safe=""))}
            body = self.json("GET", "https://tempemail.co/home/api_read?" + urlencode({"id": message_id}))
            detail = body.get("mail") if isinstance(body, dict) else None
            if (not isinstance(body, dict) or body.get("result") is not True or not isinstance(detail, dict) or
                    str(detail.get("id")) != message_id):
                raise ProviderError("unexpected_response", "Message detail identity changed or read was not granted")
            if not isinstance(detail.get("html"), str) and not isinstance(detail.get("text"), str):
                raise ProviderError("message_body_not_available", "No message body returned")
            return {**item, **detail, "id": message_id, "from": detail.get("fromAddress", detail.get("from"))}
        if provider in ("anonymmail", "tempmailbox_net", "mailforspam", "linshiyou"):
            cached = json.loads((self.folder / "messages.local.json").read_text(encoding="utf-8"))
            item = next(item for item in cached if self.message_id(item) == message_id)
            if provider in ("anonymmail", "linshiyou"):
                if not isinstance(item.get("html"), str):
                    raise ProviderError("message_body_not_available", "No message body was returned")
                return item
            if provider == "tempmailbox_net":
                return {**item, "html": self.request("GET", "https://tempmailbox.net/api/message/" + quote(message_id, safe="") + "/body", referer="https://tempmailbox.net/")}
            body = self.json("GET", "https://api.mailforspam.com/api/mailboxes/" + quote(self.config["address"], safe="") +
                             "/emails/" + quote(message_id, safe=""), referer="https://mailforspam.com/")
            if not isinstance(body, dict) or str(body.get("id")) != message_id:
                raise ProviderError("unexpected_response", "Message detail identity changed")
            return {**item, **body, "from": body.get("sender"), "html": body.get("body_html"), "text": body.get("body_text")}
        if provider in LIVEWIRE_BASES or provider in ("emailtemp_org", "tempmail100"):
            cached = json.loads((self.folder / "messages.local.json").read_text(encoding="utf-8"))
            item = next(item for item in cached if self.message_id(item) == message_id)
            if provider in LIVEWIRE_BASES:
                if isinstance(item.get("html"), str) or isinstance(item.get("text"), str):
                    return item
                target = item.get("_read_url")
                if not target or urlsplit(target).netloc != urlsplit(LIVEWIRE_BASES[provider]).netloc:
                    raise ProviderError("message_body_not_available", "No message body or read link was supplied by this inbox")
            elif provider == "emailtemp_org":
                target = "https://emailtemp.org/view/" + quote(message_id, safe="")
            else:
                target = "https://tempmail100.com/detail/" + quote(message_id, safe="")
            return {**item, "html": self.request("GET", target)}
        if provider in ("minuteinbox", "tempmailto", "10minutemail_one", "22do"):
            cached = json.loads((self.folder / "messages.local.json").read_text(encoding="utf-8"))
            item = next(item for item in cached if self.message_id(item) == message_id)
            if provider == "minuteinbox":
                metadata = self.json("POST", "https://www.minuteinbox.com/index/email", form={"id": message_id},
                                     xhr=True, referer="https://www.minuteinbox.com/")
                page = self.request("GET", "https://www.minuteinbox.com/email/id/" + quote(message_id, safe=""))
                return {**item, **metadata, "id": message_id, "html": page}
            if provider == "10minutemail_one":
                body = self.json("GET", "https://web.10minutemail.one/api/v1/mailbox/" + quote(self.config["address"], safe="") + "/" + quote(message_id, safe=""),
                                 headers_extra=self.tenminute_one_headers())
                if not isinstance(body, dict):
                    raise ProviderError("unexpected_response", "Message object missing")
                content = body.get("body")
                if not isinstance(content, dict):
                    raise ProviderError("unexpected_response", "Message body object missing")
                return {**item, **body, "html": content.get("html"), "text": content.get("text")}
            base = "https://tempmailto.com/view/" if provider == "tempmailto" else "https://22.do/content/"
            page = self.request("GET", base + quote(message_id, safe=""))
            return {**item, "html": page}
        if provider in ("moakt", "mohmal", "inboxkitten", "awamail", "tempmail_ing"):
            cached = json.loads((self.folder / "messages.local.json").read_text(encoding="utf-8"))
            item = next(item for item in cached if self.message_id(item) == message_id)
            if provider == "tempmail_ing":
                return item  # The frontend displays the full message from the list response.
            if provider == "awamail":
                body = self.json("GET", "https://awamail.com/welcome/get_email/" + quote(message_id, safe=""))
                if body.get("success") is not True or not isinstance(body.get("data"), dict):
                    raise ProviderError("unexpected_response", "Message detail missing")
                value = body["data"]
                return {**value, "from": value.get("from_address"), "text": value.get("text_content"), "html": value.get("html_content")}
            if provider == "inboxkitten":
                query = urlencode({"region": item["storage"]["region"], "key": item["storage"]["key"]})
                metadata = self.json("GET", "https://inboxkitten.com/api/v1/mail/getKey?" + query)
                page = self.request("GET", "https://inboxkitten.com/api/v1/mail/getHtml?" + query)
                return {**item, "html": page, "text": metadata.get("body-plain")}
            if provider == "moakt":
                path = item.get("_path", "")
                if not re.fullmatch(r"/en/inbox/[A-Za-z0-9_-]+", path):
                    raise ProviderError("unexpected_response", "Message path not present in inbox")
                url = "https://www.moakt.com" + path
            else:
                url = "https://www.mohmal.com/en/message/" + quote(message_id, safe="")
            page = self.request("GET", url)
            return {**item, "html": page}
        if provider == "grabmail":
            return self.json("GET", "https://grabmail.io/api/v1/message/" + quote(message_id, safe="") + "?" + urlencode({"mailbox": self.config["address"]}))
        if provider == "maildax":
            return self.json("GET", "https://api2.maildax.com/api/email/mail/" + quote(message_id, safe="") + "?" + urlencode({"secret": self.config["secret"]}))
        if provider == "fakemail":
            metadata = self.json("POST", "https://www.fakemail.net/index/email", form={"id": message_id}, xhr=True, referer="https://www.fakemail.net/")
            # The public frontend uses this iframe URL after its metadata call.
            html = self.request("GET", "https://www.tempmailaddress.com/email/id/" + quote(message_id, safe=""), referer="https://www.fakemail.net/")
            return {**metadata, "id": message_id, "html": html}
        if provider == "tempmail_plus":
            body = self.json("GET", "https://tempmail.plus/api/mails/" + quote(message_id, safe="") + "?" + urlencode({"email": self.config["address"], "epin": self.config["epin"]}), xhr=True, referer="https://tempmail.plus/")
            if not isinstance(body, dict) or not body.get("result"):
                raise ProviderError("upstream_api_error", "Message request failed")
            return body
        if provider == "byom":
            items = json.loads((self.folder / "messages.local.json").read_text(encoding="utf-8"))
            return next(item for item in items if self.message_id(item) == message_id)
        if provider == "guerrillamail":
            body = self.guerrilla("fetch_email", email_id=message_id)
            self.save()
            return {**body, "id": message_id, "subject": body.get("mail_subject"), "from": body.get("mail_from"),
                    "html": body.get("mail_body"), "date": body.get("mail_date")}
        if provider in ("tempmail_lol", "dropmail"):
            items = json.loads((self.folder / "messages.local.json").read_text(encoding="utf-8"))
            return next(item for item in items if self.message_id(item) == message_id)
        if provider == "maildrop":
            body = self.graphql("https://api.maildrop.cc/graphql",
                                "query Message($mailbox: String!, $id: String!) { message(mailbox:$mailbox, id:$id) { id headerfrom subject date data html } }",
                                {"mailbox": self.config["address"].split("@", 1)[0], "id": message_id}).get("message")
            if not isinstance(body, dict):
                raise ProviderError("unexpected_response", "Message no longer present")
            return {**body, "from": body.get("headerfrom"), "text": body.get("data")}
        if provider == "10minutemail_net":
            page = self.request("GET", "https://10minutemail.net/readmail.html?" + urlencode({"mid": message_id}), referer="https://10minutemail.net/")
            parser = MailFragment()
            parser.feed(page)
            if not parser.parts:
                raise ProviderError("unexpected_response", "Message content missing")
            items = json.loads((self.folder / "messages.local.json").read_text(encoding="utf-8"))
            metadata = next(item for item in items if self.message_id(item) == message_id)
            return {**metadata, "html": "".join(parser.parts)}
        if provider == "duckmail":
            body = self.json("GET", "https://api.duckmail.sbs/messages/" + quote(message_id, safe=""),
                             headers_extra={"Authorization": "Bearer " + self.config["token"]})
            if isinstance(body.get("html"), list):
                body["html"] = "\n".join(body["html"])
            return {**body, "received_at": body.get("createdAt")}
        raise ProviderError("unsupported_provider", provider)
