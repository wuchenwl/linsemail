"""Receive adapters checked against saved public documentation and web assets.

Only newly allocated/random dedicated inboxes are used. An upstream error is
never an empty inbox, and message bodies require previously observed IDs.
"""
from email import policy
from email.parser import Parser
from email.utils import getaddresses
from http.cookiejar import Cookie
import hashlib
import ipaddress
import json
import re
import secrets
from urllib.parse import quote, urlencode, urlsplit

from mailbox_clients import ProviderError
from .web_clients import WebClient, Page, array, identity, one, match
from .discovery_clients import DISCOVERY_FIELDS, create_mailbox as create_discovered, list_messages as list_discovered, read_message as read_discovered
from .continuation_clients import CONTINUATION_FIELDS, create_mailbox as create_continued, list_messages as list_continued, read_message as read_continued
from .platform_clients import PLATFORM_FIELDS, create_mailbox as create_platform, list_messages as list_platform, read_message as read_platform


HTML_BASES = {"emailfake": "https://emailfake.com", "generator_email": "https://generator.email", "mail_temp_com": "https://mail-temp.com"}
EXPANSION_FIELDS = {
    "chat_tempmail": ["api_key"], "smailpro": ["api_key"],
    "mailtd": ["api_key"], "mailslurp": ["api_key"],
    "mailinator": ["api_key", "domain"], "inboxes": ["api_key"],
    "maildax": [], "10minutemail": [], "crazymailing": [],
    "restmail": [], "mailcx": ["api_key", "domain"],
    "internxt": [],
    "emailnator": [],
    "1secmail": [],
    "mailcare": ["api_key", "team_id", "domain"],
    "mailtrap_inbound": ["api_key", "folder_id"],
    "cloudflare_temp_email": ['base_url', 'domain'],
    "synox_disposable_mailbox": ['base_url', 'domain'],
    "tempmail_cc": [],
    "temporaryemail_ai": ['api_key'],
    **DISCOVERY_FIELDS,
    **CONTINUATION_FIELDS,
    **PLATFORM_FIELDS,
    **{key: [] for key in HTML_BASES},
}
EXPANSION_OPTIONAL_FIELDS = {'cloudflare_temp_email': ['site_password']}


def obj(value):
    if not isinstance(value, dict):
        raise ProviderError("unexpected_response", "Expected an upstream object")
    if value.get("error") or value.get("success") is False:
        raise ProviderError("upstream_api_error", "Upstream did not acknowledge the operation")
    return value


def field(value, key):
    result = obj(value).get(key)
    if not isinstance(result, str) or not result or len(result) > 4096:
        raise ProviderError("unexpected_response", "Required upstream field missing: " + key)
    return result


def domain(value):
    if not isinstance(value, str) or len(value) > 253 or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}", value) or ".." in value:
        raise ProviderError("unexpected_response", "Invalid receiving domain")
    return value.lower()


def email(value):
    if not isinstance(value, str) or len(value) > 320 or not re.fullmatch(r"[A-Za-z0-9._+%-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}", value):
        raise ProviderError("unexpected_response", "Invalid allocated mailbox address")
    domain(value.split("@", 1)[1])
    return value


def deployment_url(value):
    try:
        parts = urlsplit(value)
        if parts.scheme not in ('https', 'http') or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or parts.port == 0 or any(c.isspace() for c in value):
            raise ValueError()
        if parts.hostname != 'localhost':
            try:
                ipaddress.ip_address(parts.hostname)
            except ValueError:
                domain(parts.hostname)
    except (ValueError, TypeError, ProviderError):
        raise ProviderError('unexpected_response', 'Invalid deployment base URL') from None
    return value.rstrip('/')


def same(actual, expected):
    if not isinstance(actual, str) or actual.lower() != expected.lower():
        raise ProviderError("mailbox_identity_changed", "Original mailbox or message identity changed")


def body_present(item):
    if not any(isinstance(item.get(key), str) for key in ("text", "html")):
        raise ProviderError("unexpected_response", "Message body missing")
    return item


def raw_message(source, address):
    parsed = Parser(policy=policy.default).parsestr(source)
    if not parsed.get("From") or not parsed.get("Subject"):
        raise ProviderError("unexpected_response", "Expected a complete RFC 5322 message")
    recipients = [v.lower() for _, v in getaddresses(parsed.get_all("To", []) + parsed.get_all("Cc", []) + parsed.get_all("Delivered-To", []))]
    if address.lower() not in recipients:
        raise ProviderError("mailbox_identity_changed", "Raw message belongs to another mailbox")
    part = parsed.get_body(preferencelist=("plain", "html"))
    if part is None:
        raise ProviderError("unexpected_response", "Raw message has no readable MIME part")
    return {"from": str(parsed["From"]), "subject": str(parsed["Subject"]), "date": str(parsed.get("Date", "")),
            "html" if part.get_content_type() == "text/html" else "text": part.get_content()}


class ExpansionClient(WebClient):
    def api(self, method, url, *, params=None, payload=None, headers=None, allow_no_content=False):
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params)
        source = self.request(method, url, payload=payload, headers_extra=headers, allow_no_content=allow_no_content,
                              timeout=35 if allow_no_content else 20)
        if source is None and allow_no_content:
            return None
        try:
            return json.loads(source)
        except (ValueError, TypeError) as exc:
            raise ProviderError("unexpected_response", "Expected JSON from upstream") from exc

    def auth(self):
        provider = self.config["provider"]
        if provider == 'cloudflare_temp_email':
            result = {}
            password = self.config.get('credentials', {}).get('site_password')
            if password:
                result['x-custom-auth'] = password
            if self.config.get('token'):
                result['Authorization'] = 'Bearer ' + self.config['token']
            return result
        key = self.config.get("credentials", {}).get("api_key")
        if not key:
            return {}
        name = {"mailtd": "Authorization", "mailcare": "Authorization", "mailinator": "Authorization", "mailtrap_inbound": "Api-Token", "inboxes": "apikey", "mailcx": "x-api-token"}.get(provider, "X-API-Key")
        headers = {name: "Bearer " + key if provider in {"mailtd", "mailcare"} else key}
        if provider == 'mailcare':
            headers['Accept'] = 'application/vnd.mailcare.v1+json'
        return headers

    def create(self, provider):
        if provider not in EXPANSION_FIELDS:
            raise ProviderError("unsupported_provider", "Adapter is not implemented")
        if self.config.get("address"):
            raise ProviderError("existing_mailbox", "Existing mailbox preserved")
        credentials = self.config.get("credentials", {})
        if any(not credentials.get(key) for key in EXPANSION_FIELDS[provider]):
            raise ProviderError("provider_credentials_required", "Configure provider credentials first")
        self.config["provider"] = provider
        headers = self.auth()
        prefix = "ls" + secrets.token_hex(12)
        if provider in PLATFORM_FIELDS:
            create_platform(self, provider, prefix)
            self.list_messages()
            return self.config
        if provider in CONTINUATION_FIELDS:
            create_continued(self, provider, prefix)
            self.list_messages()
            return self.config
        if provider in DISCOVERY_FIELDS:
            create_discovered(self, provider, prefix)
            self.list_messages()
            return self.config
        if provider == 'tempmail_cc':
            from .socket_clients import snapshot
            allocated, rows = snapshot(self.config, create=True)
            self.config.update(allocated)
            self.cache(rows)
            return self.config
        if provider in HTML_BASES:
            source = self.request("GET", HTML_BASES[provider] + "/")
            root = Page(source).root
            selected = domain(one(root.find("input", identity="domainName2"), "Receiving domain").attrs.get("value"))
            self.config["address"] = prefix[:24] + "@" + selected
            mapping = re.search(r'cookie_names\s*:\s*\{[^}]*["\x27]inbox_ctx["\x27]\s*:\s*["\x27]([A-Za-z0-9_.-]+)', source)
            self.config['context_cookie'] = mapping[1] if mapping else 'inbox_ctx'
        elif provider == '1secmail':
            allocated = self.api('GET', 'https://www.1secmail.com/api/v1/', params={'action': 'genRandomMailbox', 'count': 1})
            if not isinstance(allocated, list) or len(allocated) != 1:
                raise ProviderError('unexpected_response', 'Expected one allocated address')
            self.config['address'] = email(allocated[0])
        elif provider == 'temporaryemail_ai':
            allocated = obj(self.api('POST', 'https://api.temporaryemail.ai/temp-mail', headers=headers,
                                     payload={'suffix': prefix, 'acceptAttachments': False}))
            self.config.update(address=email(field(allocated, 'email')), token=field(allocated, 'token'),
                               expires_at=field(allocated, 'expiresAtUtc'))
        elif provider == 'mailcare':
            self.config['address'] = prefix + '@' + domain(credentials['domain'])
        elif provider == 'mailtrap_inbound':
            allocated = obj(self.api('POST', 'https://mailtrap.io/api/inbound/folders/' + quote(credentials['folder_id'], safe='') + '/inboxes',
                                     payload={'name': prefix}, headers=headers))
            self.config.update(address=field(allocated, 'address'), inbox_id=identity(allocated.get('id')))
        elif provider == 'cloudflare_temp_email':
            base = deployment_url(credentials['base_url'])
            selected = domain(credentials['domain'])
            allocated = obj(self.api('POST', base + '/api/new_address', headers=headers, payload={'name': prefix, 'domain': selected}))
            address = email(field(allocated, 'address'))
            same(address.split('@', 1)[1], selected)
            self.config.update(address=address, token=field(allocated, 'jwt'))
        elif provider == 'synox_disposable_mailbox':
            deployment_url(credentials['base_url'])
            self.config['address'] = prefix + '@' + domain(credentials['domain'])
        elif provider == "internxt":
            allocated = obj(self.api("GET", "https://internxt.com/api/temp-mail/create-email"))
            self.config.update(address=field(allocated, "address"), token=field(allocated, "token"))
        elif provider == "emailnator":
            allocated = obj(self.api("POST", "https://www.emailnator.com/api/generate-email", payload={"ids": [1]}))
            self.emailnator_ok(allocated)
            address = email(field(allocated, "email"))
            # Request only custom disposable domains. Shared Gmail, plus/dot
            # aliases and Outlook pools are deliberately excluded.
            if address.rsplit("@", 1)[1].lower() in {"gmail.com", "googlemail.com", "hotmail.com", "outlook.com", "live.com"}:
                raise ProviderError("unexpected_response", "Shared email pools are unsupported")
            self.config["address"] = address
        elif provider == "restmail":
            self.config["address"] = prefix + "@restmail.net"
        elif provider == "mailcx":
            self.config["address"] = prefix + "@" + domain(credentials["domain"])
        elif provider == "chat_tempmail":
            domains = obj(self.api("GET", "https://chat-tempmail.com/api/email/domains", headers=headers)).get("domains")
            if not isinstance(domains, list) or not domains:
                raise ProviderError("no_public_domain", "No receiving domain was supplied")
            selected = domain(domains[0])
            allocated = obj(self.api("POST", "https://chat-tempmail.com/api/emails/generate", headers=headers,
                                     payload={"name": prefix, "domain": selected, "expiryTime": 3600000}))
            same(field(allocated, "email"), prefix + "@" + selected)
            self.config.update(address=allocated["email"], inbox_id=field(allocated, "id"))
        elif provider == "smailpro":
            # Use Sonjj's independent generic mailbox service, never a shared
            # Gmail/Outlook pool or a pre-existing address.
            allocated = obj(self.api("GET", "https://app.sonjj.com/v1/temp_email/create", headers=headers,
                                     params={"email": "random@random.com", "expiry_minutes": 60}))
            self.config.update(address=field(allocated, "email"), expires_at=allocated.get("expired_at"))
        elif provider == "mailtd":
            domains = array(obj(self.api("GET", "https://api.mail.td/api/domains")).get("domains"))
            if not domains:
                raise ProviderError("no_public_domain", "No receiving domain was supplied")
            address = prefix + "@" + domain(field(domains[0], "domain"))
            allocated = obj(self.api("POST", "https://api.mail.td/api/accounts", headers=headers,
                                     payload={"address": address, "password": secrets.token_urlsafe(24)}))
            same(field(allocated, "address"), address)
            self.config.update(address=address, inbox_id=field(allocated, "id"))
        elif provider == "mailslurp":
            allocated = obj(self.api("POST", "https://api.mailslurp.com/inboxes", headers=headers,
                                     params={"name": prefix, "expiresIn": 3600000}))
            self.config.update(address=field(allocated, "emailAddress"), inbox_id=field(allocated, "id"))
        elif provider == "inboxes":
            domains = array(self.api("GET", "https://inboxes.com/api/v3/domains", headers=headers))
            if not domains:
                raise ProviderError("no_public_domain", "No receiving domain was supplied")
            self.config["address"] = prefix + "@" + domain(field(domains[0], "qdn"))
            allocated = obj(self.api("POST", self.inbox_path(), headers=headers))
            if allocated.get("ok") is not True and allocated.get("created") is not True:
                raise ProviderError("unexpected_response", "Inbox activation was not acknowledged")
        elif provider == "mailinator":
            self.config["address"] = prefix + "@" + domain(credentials["domain"])
        elif provider == "maildax":
            allocated = obj(obj(self.api("POST", "https://api2.maildax.com/api/email", payload={})).get("data"))
            self.config.update(address=field(allocated, "email"), secret=field(allocated, "secret"))
        elif provider == "crazymailing":
            allocated = obj(obj(self.api("POST", "https://crazymailing.com/api/mailbox", payload={})).get("mailbox"))
            self.config.update(address=field(allocated, "address"), inbox_id=field(allocated, "id"), expires_at=allocated.get("expiresAt"))
        elif provider == "10minutemail":
            self.request("GET", "https://10minutemail.com/")
            allocated = obj(self.api("GET", "https://10minutemail.com/session/address"))
            self.config["address"] = field(allocated, "address")
        email(self.config.get("address"))
        if provider in {"crazymailing", "10minutemail"}:
            cookies = {"|".join((c.domain, c.path, c.name)): hashlib.sha256(c.value.encode()).hexdigest()
                       for c in self.cookies if not c.is_expired()}
            if not cookies:
                raise ProviderError("session_initialization_failed", "Mailbox cookie session missing")
            self.config["session_cookies"] = cookies
        # Verify credentials/session against this dedicated address once.
        self.list_messages()
        return self.config

    def inbox_path(self):
        provider = self.config["provider"]
        mid = quote(self.config.get("inbox_id", ""), safe="")
        if provider == "chat_tempmail":
            return "https://chat-tempmail.com/api/emails/" + mid
        if provider == "mailtd":
            return "https://api.mail.td/api/accounts/" + mid + "/messages"
        if provider == "inboxes":
            return "https://inboxes.com/api/v3/inboxes/" + quote(self.config["address"], safe="")
        if provider == "mailinator":
            prefix, receiving_domain = self.config["address"].split("@", 1)
            return "https://api.mailinator.com/api/v2/domains/" + quote(receiving_domain, safe="") + "/inboxes/" + quote(prefix, safe="")
        if provider == "mailslurp":
            return "https://api.mailslurp.com/inboxes/" + mid + "/emails/paginated"
        if provider == 'mailcare':
            return 'https://mailcare.io/api/teams/' + quote(self.config['credentials']['team_id'], safe='') + '/emails'
        if provider == 'mailtrap_inbound':
            return 'https://mailtrap.io/api/inbound/inboxes/' + mid + '/messages'
        raise ProviderError("unsupported_provider", "No scoped inbox endpoint")

    def list_messages(self):
        provider, address = self.config["provider"], self.config["address"]
        headers, items = self.auth(), []
        if provider in PLATFORM_FIELDS:
            items = list_platform(self)
        elif provider in CONTINUATION_FIELDS:
            items = list_continued(self)
        elif provider in DISCOVERY_FIELDS:
            items = list_discovered(self)
        elif provider == 'tempmail_cc':
            from .socket_clients import snapshot
            current, items = snapshot(self.config)
            self.config.update(current)
        elif provider in HTML_BASES:
            items = self.html_listing()
        elif provider == '1secmail':
            prefix, selected = address.split('@', 1)
            items = array(self.api('GET', 'https://www.1secmail.com/api/v1/', params={'action': 'getMessages', 'login': prefix, 'domain': selected}))
        elif provider == 'temporaryemail_ai':
            current = obj(self.api('GET', 'https://api.temporaryemail.ai/temp-mail/token/' + quote(self.config['token'], safe=''), headers=headers))
            same(field(current, 'virtualEmail'), address)
            current_id = identity(current.get('id'))
            if self.config.get('inbox_id') is not None:
                same(current_id, self.config['inbox_id'])
            response = obj(self.api('GET', 'https://api.temporaryemail.ai/temp-mail/' + quote(address, safe='') + '/messages', headers=headers))
            for row in array(response.get('messages')):
                same(field(row, 'to'), address)
                items.append(body_present({**row, 'id': identity(row.get('id')), 'html': row.get('body'), 'received_at': row.get('receivedAtUtc')}))
            self.config['inbox_id'] = current_id
        elif provider == 'synox_disposable_mailbox':
            base = deployment_url(self.config['credentials']['base_url'])
            root = Page(self.request('GET', base + '/?' + quote(address, safe='@.'))).root
            same(one(root.find(identity='my-address'), 'Mailbox address').text().strip(), address)
            container = one(root.find(identity='email-list'), 'Explicit mailbox list')
            for row in container.find('a', cls='email-list-item'):
                mid = match(r'^#mail-box-([0-9]+)$', row.attrs.get('href', ''), 'Message link')[1]
                heading = one(row.find('h6', cls='list-group-item-heading'), 'Sender heading')
                items.append({'id': mid, 'from': one(heading.find('span', cls='text-muted'), 'Sender').text().strip(),
                              'date': one(heading.find('small'), 'Message timestamp').attrs.get('title'),
                              'subject': one(row.find('p', cls='list-group-item-text'), 'Subject').text().strip()})
            bodies = [node for node in container.walk() if node.attrs.get('id', '').startswith('mail-box-')]
            if {node.attrs['id'][9:] for node in bodies} != {row['id'] for row in items}:
                raise ProviderError('unexpected_response', 'Mailbox headers and bodies differ')
        elif provider == 'cloudflare_temp_email':
            base = deployment_url(self.config['credentials']['base_url'])
            current = obj(self.api('GET', base + '/api/settings', headers=headers))
            same(field(current, 'address'), address)
            total = None
            for page in range(10):
                response = obj(self.api('GET', base + '/api/mails', headers=headers, params={'offset': page*100, 'limit': 100}))
                if total is None:
                    total = response.get('count')
                    if type(total) is not int or total < 0:
                        raise ProviderError('incomplete_mailbox_observation', 'Mailbox count missing')
                rows = array(response.get('results'))
                for row in rows:
                    same(field(row, 'address'), address)
                    items.append({**row, **raw_message(field(row, 'raw'), address), 'id': identity(row.get('id'))})
                if len(items) == total:
                    break
                if not rows or len(items) > total:
                    raise ProviderError('incomplete_mailbox_observation', 'Mailbox count and pages differ')
            else:
                raise ProviderError('incomplete_mailbox_observation', 'Mailbox exceeded the page limit')
        elif provider == 'mailcare':
            for page in range(1, 11):
                response = obj(self.api('GET', self.inbox_path(), headers=headers, params={'inbox': address, 'page': page, 'limit': 100}))
                meta = obj(response.get('meta'))
                if meta.get('current_page') != page or type(meta.get('last_page')) is not int or meta['last_page'] < page:
                    raise ProviderError('incomplete_mailbox_observation', 'Missing or inconsistent page metadata')
                for row in array(response.get('data')):
                    same(field(obj(row.get('inbox')), 'email'), address)
                    items.append({**row, 'from': obj(row.get('sender')).get('email')})
                if meta['last_page'] == page:
                    break
            else:
                raise ProviderError('incomplete_mailbox_observation', 'Mailbox exceeded the page limit')
        elif provider == 'mailtrap_inbound':
            cursor, seen, total = None, set(), None
            for _ in range(10):
                response = obj(self.api('GET', self.inbox_path(), headers=headers, params={'last_id': cursor} if cursor else None))
                count = response.get('total_count')
                if type(count) is not int or count < 0 or total is not None and count != total or 'last_id' not in response:
                    raise ProviderError('incomplete_mailbox_observation', 'Missing or changing mailbox count')
                total = count
                for row in array(response.get('data')):
                    same(identity(row.get('inbox_id')), self.config['inbox_id'])
                    items.append(row)
                cursor = response['last_id']
                if cursor is None:
                    if total != len(items):
                        raise ProviderError('incomplete_mailbox_observation', 'Message count and pages differ')
                    break
                if not isinstance(cursor, str) or not cursor or cursor in seen:
                    raise ProviderError('incomplete_mailbox_observation', 'Repeated or invalid cursor')
                seen.add(cursor)
            else:
                raise ProviderError('incomplete_mailbox_observation', 'Mailbox exceeded the page limit')
        elif provider == "internxt":
            items = array(self.api("GET", "https://internxt.com/api/temp-mail/get-inbox", params={"email": address, "token": self.config["token"]}))
        elif provider == "emailnator":
            response = obj(self.api("POST", "https://www.emailnator.com/api/message-list", payload={"email": address, "limit": 20}))
            self.emailnator_ok(response)
            listed = array(response.get("messages"))
            limit = response.get('message_limit')
            if response.get("message_count") != len(listed) or type(limit) is not int or limit < 1 or len(listed) >= limit:
                raise ProviderError("incomplete_mailbox_observation", "Upstream message limit or inconsistent count")
            for row in listed:
                mid = identity(row.get("id", row.get("messageID")))
                if mid != "ADSVPN":
                    items.append({**row, "id": mid, "received_at": row.get("timestamp")})
        elif provider == "restmail":
            response = array(self.api("GET", "https://restmail.net/mail/" + quote(address.split("@", 1)[0], safe="")))
            for row in response:
                targets = array(row.get("to"))
                if not any(target.get("address", "").lower() == address.lower() for target in targets):
                    raise ProviderError("mailbox_identity_changed", "Mailbox response contains another recipient")
                body_present(row)
                senders = array(row.get("from"))
                sender = senders[0].get("address") if senders else None
                mid = "content-" + hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                items.append({**row, "id": mid, "identity_strength": "weak", "from": sender, "date": obj(row.get("headers")).get("date")})
        elif provider == "mailcx":
            # Each request is an incremental long-poll. Persist both the cursor
            # and complete observed-ID cache across worker leases.
            path = self.folder / "messages.local.json"
            previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
            cursor = self.config.get("next_since")
            response = self.api("GET", "https://api.mail.cx/v1/inbox/" + quote(address, safe=""), headers=headers,
                                params={"limit": 50, **({"since": cursor} if cursor else {})}, allow_no_content=True)
            if response is not None:
                response = obj(response)
                fresh = array(response.get("emails"))
                if len(fresh) >= 50:
                    raise ProviderError("incomplete_mailbox_observation", "Mailbox reached the upstream result limit")
                next_cursor = field(response, "next_since")
                if fresh and next_cursor == cursor:
                    raise ProviderError("incomplete_mailbox_observation", "Upstream cursor did not advance")
                by_id = {row["id"]: row for row in previous}
                if len({identity(row.get("id")) for row in fresh}) != len(fresh):
                    raise ProviderError("unexpected_response", "Duplicate upstream message IDs")
                for row in fresh:
                    by_id[identity(row.get("id"))] = {**row, "id": identity(row.get("id")), "from": row.get("from_email")}
                items = list(by_id.values())
                if len(items) > 1000:
                    raise ProviderError("incomplete_mailbox_observation", "Mailbox cache exceeded the observation limit")
                self.config["next_since"] = next_cursor
            else:
                items = previous
        elif provider == "chat_tempmail":
            cursor = None
            seen = set()
            for _ in range(10):
                response = obj(self.api("GET", self.inbox_path(), headers=headers, params={"cursor": cursor} if cursor else None))
                items.extend(array(response.get("messages")))
                cursor = response.get("nextCursor")
                if not cursor:
                    break
                if not isinstance(cursor, str) or cursor in seen:
                    raise ProviderError("incomplete_mailbox_observation", "Invalid or repeated continuation cursor")
                seen.add(cursor)
            else:
                raise ProviderError("incomplete_mailbox_observation", "Mailbox exceeded the page limit")
            items = [{**row, "received_at": row.get("received_at", 0) / 1000 if isinstance(row.get("received_at"), (int, float)) else row.get("received_at")} for row in items]
        elif provider == "smailpro":
            response = obj(self.api("GET", "https://app.sonjj.com/v1/temp_email/inbox", headers=headers, params={"email": address}))
            items = []
            for row in array(response.get("messages")):
                same(field(row, "textTo"), address)
                items.append({**row, "id": identity(row.get("mid")), "from": row.get("textFrom"), "subject": row.get("textSubject"), "date": row.get("textDate")})
        elif provider == "mailtd":
            # A page contains at most 30; a short page is the documented end.
            for page in range(1, 11):
                response = obj(self.api("GET", self.inbox_path(), headers=headers, params={"page": page}))
                if response.get("page") != page:
                    raise ProviderError("incomplete_mailbox_observation", "Upstream page identity changed")
                more = array(response.get("messages"))
                items.extend(more)
                if len(more) < 30:
                    break
            else:
                raise ProviderError("incomplete_mailbox_observation", "Mailbox exceeded the page limit")
        elif provider == "mailslurp":
            for page in range(10):
                response = obj(self.api("GET", self.inbox_path(), headers=headers, params={"page": page, "size": 100, "sort": "ASC"}))
                if response.get("number") != page or not isinstance(response.get("last"), bool):
                    raise ProviderError("incomplete_mailbox_observation", "Missing or inconsistent page metadata")
                items.extend(array(response.get("content")))
                if response["last"]:
                    break
            else:
                raise ProviderError("incomplete_mailbox_observation", "Mailbox exceeded the page limit")
        elif provider == "inboxes":
            for row in array(self.api("GET", self.inbox_path(), headers=headers)):
                same(field(row, "to"), address)
                items.append({**row, "id": identity(row.get("uid"))})
        elif provider == "mailinator":
            # Restrict every query to the dedicated local part, never wildcard.
            for page in range(10):
                response = obj(self.api("GET", self.inbox_path(), headers=headers, params={"skip": page * 99, "limit": 99, "sort": "ascending"}))
                more = array(response.get("msgs"))
                for row in more:
                    target = field(row, "to")
                    same(target if "@" in target else target + "@" + address.split("@", 1)[1], address)
                items.extend(more)
                if len(more) < 99:
                    break
            else:
                raise ProviderError("incomplete_mailbox_observation", "Mailbox exceeded the page limit")
            items = [{**row, "received_at": row.get("time", 0) / 1000 if isinstance(row.get("time"), (int, float)) else row.get("time")} for row in items]
        elif provider == "maildax":
            response = obj(self.api("GET", "https://api2.maildax.com/api/email/mails", params={"email": address, "secret": self.config["secret"]}))
            items = [{**row, "id": identity(row.get("_id"))} for row in array(response.get("data"))]
        elif provider == "crazymailing":
            current = obj(obj(self.api("GET", "https://crazymailing.com/api/mailbox")).get("mailbox"))
            same(field(current, "id"), self.config["inbox_id"])
            same(field(current, "address"), address)
            response = obj(self.api("GET", "https://crazymailing.com/api/messages", params={"mailbox": self.config["inbox_id"]}))
            items = array(response.get("messages"))
        elif provider == "10minutemail":
            current = obj(self.api("GET", "https://10minutemail.com/session/address"))
            same(field(current, "address"), address)
            items = [{**row, "from": row.get("sender"), "date": row.get("sentDateFormatted"), "text": row.get("bodyPlainText"), "html": row.get("bodyHtmlContent")}
                     for row in array(self.api("GET", "https://10minutemail.com/messages/messagesAfter/0"))]
            for row in items:
                body_present(row)
        else:
            raise ProviderError("unsupported_provider", "Adapter is not implemented")
        normalized = []
        for row in items:
            obj(row)
            normalized.append({**row, "id": identity(row.get("id")), "received_at": row.get("received_at", row.get("created_at", row.get("createdAt", row.get("date"))))})
        return self.cache(normalized)

    @staticmethod
    def emailnator_ok(response):
        if response.get("status") != "success":
            raise ProviderError("upstream_api_error", "Emailnator did not acknowledge the operation")

    def html_path(self):
        prefix, receiving_domain = self.config["address"].split("@", 1)
        return HTML_BASES[self.config["provider"]] + "/" + quote(receiving_domain, safe="") + "/" + quote(prefix, safe="")

    def html_listing(self):
        self.html_context('')
        source = self.request("GET", self.html_path(), same_origin_redirects=2)
        root = Page(source).root
        prefix, receiving_domain = self.config["address"].split("@", 1)
        same(one(root.find("input", identity="userName"), "Mailbox name").attrs.get("value"), prefix)
        same(one(root.find("input", identity="domainName2"), "Mailbox domain").attrs.get("value"), receiving_domain)
        count = int(match(r'\bnum_mess\s*:\s*(\d+)', source, "Explicit message count").group(1))
        table = one(root.find(identity="email-table"), "Explicit inbox list")
        items = []
        for row in table.find(cls="list-group-item"):
            call = match(r"loadInboxClientSide\(['\"](/?[A-Za-z0-9._%+@/-]+)['\"]\)", row.attrs.get("onclick", ""), "Scoped message link")
            path = call.group(1).lstrip("/").split("/")
            if len(path) != 3 or path[0].lower() != receiving_domain.lower() or path[1].lower() != prefix.lower():
                raise ProviderError("mailbox_identity_changed", "Message link left the original inbox")
            mid = identity(path[2])
            items.append({"id": mid, "from": one(row.find(cls="from_div_45g45gg"), "Sender").text().strip(),
                          "subject": one(row.find(cls="subj_div_45g45gg"), "Subject").text().strip(),
                          "date": one(row.find(cls="time_div_45g45gg"), "Message time").text().strip()})
        if len(items) != count:
            raise ProviderError("incomplete_mailbox_observation", "Message count and parsed rows differ")
        return items

    def html_context(self, message_id):
        prefix, selected = self.config['address'].split('@', 1)
        host = HTML_BASES[self.config['provider']].split('//', 1)[1]
        value = quote(selected + '/' + prefix + '/' + message_id, safe='')
        self.cookies.set_cookie(Cookie(0, self.config['context_cookie'], value, None, False, '.'+host, True, True,
                                      '/', True, True, None, True, None, None, {}, False))

    def get_message(self, message_id):
        path = self.folder / "messages.local.json"
        items = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        item = next((row for row in items if row["id"] == message_id), None)
        if item is None:
            raise ProviderError("message_not_in_mailbox", "Message was not observed in this mailbox")
        provider, mid, headers = self.config["provider"], quote(message_id, safe=""), self.auth()
        if provider in PLATFORM_FIELDS:
            return read_platform(self, item)
        if provider in CONTINUATION_FIELDS:
            return read_continued(self, item)
        if provider in DISCOVERY_FIELDS:
            return read_discovered(self, item)
        if provider in HTML_BASES:
            self.html_context(message_id)
            source = self.request("GET", self.html_path() + "/" + mid + "?" + urlencode({"src": message_id}), same_origin_redirects=2)
            return {**item, **raw_message(source, self.config["address"])}
        if provider == '1secmail':
            prefix, selected = self.config['address'].split('@', 1)
            response = obj(self.api('GET', 'https://www.1secmail.com/api/v1/', params={'action': 'readMessage', 'login': prefix, 'domain': selected, 'id': message_id}))
            same(identity(response.get('id')), message_id)
            return body_present({**item, **response, 'text': response.get('textBody'), 'html': response.get('htmlBody')})
        if provider == 'mailcare':
            url = self.inbox_path() + '/' + mid
            response = obj(obj(self.api('GET', url, headers=headers)).get('data'))
            same(identity(response.get('id')), message_id)
            same(field(obj(response.get('inbox')), 'email'), self.config['address'])
            # A raw MIME response validates the recipient as well as the body.
            source = self.request('GET', url, headers_extra={**headers, 'Accept': 'message/rfc2822'})
            return {**item, **raw_message(source, self.config['address'])}
        if provider == 'mailtrap_inbound':
            response = obj(self.api('GET', self.inbox_path() + '/' + mid, headers=headers))
            same(identity(response.get('id')), message_id)
            same(identity(response.get('inbox_id')), self.config['inbox_id'])
            return body_present({**item, **response, 'text': response.get('text_body'), 'html': response.get('html_body')})
        if provider == 'synox_disposable_mailbox':
            base = deployment_url(self.config['credentials']['base_url'])
            source = self.request('GET', base + '/?' + urlencode({'action': 'download_email', 'email_id': message_id, 'address': self.config['address']}))
            return {**item, **raw_message(source, self.config['address'])}
        if provider == 'cloudflare_temp_email':
            base = deployment_url(self.config['credentials']['base_url'])
            response = obj(self.api('GET', base + '/api/mail/' + mid, headers=headers))
            same(identity(response.get('id')), message_id)
            same(field(response, 'address'), self.config['address'])
            return {**item, **raw_message(field(response, 'raw'), self.config['address'])}
        if provider in {"restmail", "tempmail_cc", "temporaryemail_ai"}:
            return body_present(item)
        if provider == "emailnator":
            response = obj(self.api("GET", "https://www.emailnator.com/api/message/" + mid))
            if response.get("locked") or not isinstance(response.get("content"), str):
                raise ProviderError("upstream_access_denied", "Message is locked or body is unavailable")
            for key in ("subject", "from"):
                if item.get(key) is not None and response.get(key) != item[key]:
                    raise ProviderError("mailbox_identity_changed", "Message metadata changed")
            return {**item, "html": response["content"]}
        if provider == "internxt":
            response = obj(self.api("GET", "https://internxt.com/api/temp-mail/get-message",
                                    params={"email": self.config["address"], "token": self.config["token"], "messageId": message_id}))
            same(identity(response.get("id")), message_id)
            return body_present({**item, **response, "text": response.get("body")})
        if provider == "mailcx":
            source = self.request("GET", "https://api.mail.cx/v1/email/" + mid + "/raw", headers_extra=headers)
            return {**item, **raw_message(source, self.config["address"])}
        if provider == "10minutemail":
            self.check_session()
            return body_present(item)
        if provider == "smailpro":
            response = obj(self.api("GET", "https://app.sonjj.com/v1/temp_email/message", headers=headers,
                                    params={"email": self.config["address"], "mid": message_id}))
            return {**item, "html": field(response, "body")}
        if provider == "chat_tempmail":
            response = obj(self.api("GET", self.inbox_path() + "/" + mid, headers=headers))
            same(field(response, "emailId"), self.config["inbox_id"])
            if response.get("to_address") is not None:
                same(response["to_address"], self.config["address"])
            result = {**item, **response, "text": response.get("content")}
        elif provider == "mailtd":
            response = obj(self.api("GET", self.inbox_path() + "/" + mid, headers=headers))
            same(field(response, "address"), self.config["address"])
            result = {**item, **response, "text": response.get("text_body"), "html": response.get("html_body")}
        elif provider == "mailslurp":
            response = obj(self.api("GET", "https://api.mailslurp.com/emails/" + mid, headers=headers))
            same(field(response, "inboxId"), self.config["inbox_id"])
            if not isinstance(response.get("isHTML"), bool):
                raise ProviderError("unexpected_response", "Mail body format missing")
            result = {**item, **response, "html" if response["isHTML"] else "text": response.get("body")}
        elif provider == "inboxes":
            response = obj(self.api("GET", "https://inboxes.com/api/v3/messages/" + mid, headers=headers))
            same(field(response, "to"), self.config["address"])
            result = {**item, **response, "id": identity(response.get("uid"))}
        elif provider == "mailinator":
            response = obj(self.api("GET", self.inbox_path() + "/messages/" + mid, headers=headers))
            same(identity(response.get("id")), message_id)
            # Use the separately documented text endpoint; parts have no stable
            # declared MIME schema in the current OpenAPI specification.
            text = obj(self.api("GET", "https://api.mailinator.com/api/v2/domains/" + quote(self.config["address"].split("@", 1)[1], safe="") + "/messages/" + mid + "/text", headers=headers))
            if not isinstance(text.get("text"), str):
                raise ProviderError("unexpected_response", "Message text missing")
            result = {**item, **response, "text": text["text"]}
        elif provider == "maildax":
            response = obj(obj(self.api("GET", "https://api2.maildax.com/api/email/mail/" + mid, params={"secret": self.config["secret"]})).get("data"))
            result = {**item, **response, "id": identity(response.get("_id"))}
        elif provider == "crazymailing":
            # The frontend exposes complete bodies in an HTML frame endpoint.
            self.check_session()
            return {**item, "html": self.request("GET", "https://crazymailing.com/api/message/" + mid + "/body")}
        else:
            raise ProviderError("unsupported_provider", "Adapter is not implemented")
        same(identity(result.get("id")), message_id)
        return body_present(result)
