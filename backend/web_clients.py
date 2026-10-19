"""Receive-only web protocols with persistent, encrypted mailbox sessions.

Frontend assets are protocol references, never executable code. Only dedicated
random inboxes created by this client can be read; detail IDs must be observed.
"""
import hashlib
from html import escape
from html.parser import HTMLParser
import json
import re
import secrets
from urllib.parse import quote, urlencode, urljoin, urlsplit

import httpx

from mailbox_clients import Client, ProviderError

WEB_IDS = frozenset({"tempmailo", "mailcatch", "mailnesia", "disposablemail", "tempail"})
VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())


class Element:
    def __init__(self, tag="", attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def find(self, tag=None, *, cls=None, identity=None):
        return [node for node in self.walk() if (tag is None or node.tag == tag)
                and (cls is None or cls in node.attrs.get("class", "").split())
                and (identity is None or node.attrs.get("id") == identity)]

    def walk(self):
        for child in self.children:
            if isinstance(child, Element):
                yield child
                yield from child.walk()

    def text(self):
        if self.tag in ("script", "style", "noscript"):
            return ""
        return "".join(child.text() if isinstance(child, Element) else child for child in self.children)

    def html(self):
        attrs = "".join(" " + key + '="' + escape(value or "", quote=True) + '"' for key, value in self.attrs.items())
        inner = "".join(child.html() if isinstance(child, Element) else escape(child) for child in self.children)
        return "<" + self.tag + attrs + ">" + ("" if self.tag in VOID else inner + "</" + self.tag + ">")


class Page(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.root = Element()
        self.stack = [self.root]
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        node = Element(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in VOID:
            if len(self.stack) > 160:
                raise ProviderError("unexpected_response", "HTML nesting exceeded the receive limit")
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for offset in range(len(self.stack)-1, 0, -1):
            if self.stack[offset].tag == tag:
                del self.stack[offset:]
                break

    def handle_data(self, value):
        self.stack[-1].children.append(value)


def one(nodes, description):
    if len(nodes) != 1:
        raise ProviderError("unexpected_response", description + " was not uniquely present")
    return nodes[0]


def match(pattern, source, description):
    value = re.search(pattern, source, re.S)
    if value is None:
        raise ProviderError("unexpected_response", description + " was missing")
    return value


def array(value):
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ProviderError("unexpected_response", "Expected an explicit message array")
    return value


def identity(value):
    if not isinstance(value, (str, int)) or isinstance(value, bool) or not str(value) or len(str(value)) > 512:
        raise ProviderError("unexpected_response", "Message identity missing or invalid")
    return str(value)


def mailnesia_rows(container):
    items = []
    for row in container.find("tr"):
        cells = row.find("td")
        if not cells:
            continue
        if len(cells) != 5:
            raise ProviderError("unexpected_response", "Unrecognized mailbox row")
        mid = identity(row.attrs.get("id"))
        if not re.fullmatch(r"[0-9]+", mid):
            raise ProviderError("unexpected_response", "Invalid mailbox message ID")
        times = cells[0].find("time")
        items.append({"id": mid, "from": cells[1].text().strip(), "subject": cells[3].text().strip(),
                      "date": times[0].attrs.get("datetime") if times else cells[0].text().strip()})
    return items


class WebClient(Client):
    def check_session(self):
        expected = self.config.get("session_cookies", {})
        current = {"|".join((cookie.domain, cookie.path, cookie.name)): hashlib.sha256(cookie.value.encode()).hexdigest()
                   for cookie in self.cookies if not cookie.is_expired()}
        if any(current.get(key) != value for key, value in expected.items()):
            raise ProviderError("mailbox_session_lost", "Original mailbox cookie session expired or changed")

    def request(self, method, url, *, payload=None, form=None, xhr=False, referer=None, headers_extra=None, allow_no_content=False, timeout=20, same_origin_redirects=0):
        self.check_session()
        headers = {"User-Agent": "Linsemail/0.3", "Accept": "application/json,text/html;q=0.9", **(headers_extra or {})}
        if xhr:
            headers["X-Requested-With"] = "XMLHttpRequest"
        if referer:
            headers["Referer"] = referer
        try:
            with httpx.Client(cookies=self.cookies, timeout=timeout, follow_redirects=False) as client:
                with client.stream(method, url, headers=headers, json=payload, data=form) as response:
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 2_000_000:
                            raise ProviderError("response_too_large", "Upstream response exceeded the receive limit")
                    status = response.status_code
                    location = response.headers.get('location')
        except httpx.HTTPError as exc:
            raise ProviderError("upstream_network_error", "Upstream request failed") from exc
        self.cookies.save(ignore_discard=True, ignore_expires=False)
        self.check_session()
        if status in (301, 302, 303, 307, 308) and same_origin_redirects and method == 'GET' and location:
            target = urljoin(url, location)
            current, following = urlsplit(url), urlsplit(target)
            if (current.scheme, current.netloc) != (following.scheme, following.netloc) or following.username or following.password:
                raise ProviderError('upstream_redirect_rejected', 'Mailbox redirect left its original origin')
            return self.request(method, target, xhr=xhr, referer=referer, headers_extra=headers_extra,
                                allow_no_content=allow_no_content, timeout=timeout, same_origin_redirects=same_origin_redirects-1)
        if not 200 <= status < 300:
            code = "rate_limited" if status == 429 else "upstream_access_denied" if status in (401, 403) else "upstream_http_error"
            raise ProviderError(code, "Upstream request failed", status)
        if status == 204 and allow_no_content:
            return None
        source = raw.decode("utf-8", errors="replace")
        if re.search(r"(?i)(?:<title>Just a moment|cf-chl-|id=[\"']challenge-form)", source):
            raise ProviderError("upstream_challenge", "Upstream requires an interactive challenge")
        return source

    def cache(self, items):
        if len({item["id"] for item in items}) != len(items):
            raise ProviderError("unexpected_response", "Duplicate upstream message IDs")
        (self.folder / "messages.local.json").write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        self.save()
        return items

    def create(self, provider):
        if provider not in WEB_IDS:
            raise ProviderError("unsupported_provider", "Web adapter is not implemented")
        if self.config.get("address"):
            raise ProviderError("existing_mailbox", "Existing mailbox preserved")
        self.config["provider"] = provider
        # Mailnesia caps the local part at 30 characters; keep 96 random bits.
        prefix = "ls" + secrets.token_hex(12)
        if provider == "tempmailo":
            source = self.request("GET", "https://tempmailo.com/")
            inputs = Page(source).root.find("input")
            csrf = one([node for node in inputs if node.attrs.get("name") == "__RequestVerificationToken"], "CSRF field")
            token = csrf.attrs.get("value")
            if not token:
                raise ProviderError("session_initialization_failed", "CSRF token missing")
            self.config["csrf"] = token
            allocated = self.request("GET", "https://tempmailo.com/changemail", headers_extra={"RequestVerificationToken": token}).strip()
            # The current endpoint returns a JSON string; older frontend builds
            # treated a plain address as text. Neither is an arbitrary object.
            if allocated.startswith('"'):
                try:
                    allocated = json.loads(allocated)
                except ValueError as exc:
                    raise ProviderError("unexpected_response", "Malformed allocated address") from exc
            self.config["address"] = allocated
        elif provider in {"mailcatch", "mailnesia"}:
            self.config["address"] = prefix + "@" + ("mailcatch.com" if provider == "mailcatch" else "mailnesia.com")
        elif provider == "disposablemail":
            source = self.request("GET", "https://www.disposablemail.com/")
            csrf = match(r'\bCSRF\s*=\s*["\x27]([^"\x27]+)', source, "Frontend CSRF").group(1)
            result = self.json("GET", "https://www.disposablemail.com/index/index?" + urlencode({"csrf_token": csrf}), xhr=True, referer="https://www.disposablemail.com/")
            if not isinstance(result, dict):
                raise ProviderError("unexpected_response", "Inbox creation was not an object")
            self.config["address"] = result.get("email")
        else:
            source = self.request("GET", "https://tempail.com/en/")
            address = one(Page(source).root.find("input", identity="eposta_adres"), "Mailbox address")
            self.config.update(address=address.attrs.get("value"), session=match(r'\boturum="([^"]+)"', source, "Mailbox session").group(1))
        if not isinstance(self.config.get("address"), str) or not re.fullmatch(r"[A-Za-z0-9._+%-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", self.config["address"]):
            raise ProviderError("unexpected_response", "No valid mailbox address was allocated")
        if provider in {"disposablemail", "tempail", "tempmailo"}:
            cookies = {"|".join((cookie.domain, cookie.path, cookie.name)): hashlib.sha256(cookie.value.encode()).hexdigest()
                       for cookie in self.cookies if not cookie.is_expired()}
            if not cookies:
                raise ProviderError("session_initialization_failed", "Mailbox cookie session missing")
            self.config["session_cookies"] = cookies
        # Creation is accepted only after observing this dedicated inbox.
        self.list_messages()
        return self.config

    def list_messages(self):
        provider, address = self.config["provider"], self.config["address"]
        prefix = address.split("@", 1)[0]
        if provider == "tempmailo":
            body = self.json("POST", "https://tempmailo.com/", payload={"mail": address}, headers_extra={"RequestVerificationToken": self.config["csrf"]})
            items = []
            for row in array(body):
                # This frontend has no durable message ID: use content identity,
                # never its unstable display index, and keep the complete body.
                if not any(isinstance(row.get(key), str) for key in ("text", "html")):
                    raise ProviderError("unexpected_response", "Full message body missing")
                mid = "content-" + hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                items.append({**row, "id": mid, "identity_strength": "weak"})
        elif provider == "mailcatch":
            source = self.request("GET", "https://mailcatch.com/api/list/" + quote(prefix, safe=""))
            page = Page(source).root
            header = one(page.find(cls="inbox-header"), "Inbox header")
            if re.sub(r"\s+", "", header.text()) != address:
                raise ProviderError("mailbox_identity_changed", "Inbox response belongs to another address")
            listing = one(page.find("ul", cls="email-list"), "Message list")
            items = [{"id": identity(node.attrs.get("data-email-id")), "from": node.attrs.get("data-sender"),
                      "subject": node.attrs.get("data-subject"), "received_at": node.attrs.get("data-timestamp")}
                     for node in listing.find(cls="email-item")]
        elif provider == "mailnesia":
            source = self.request("GET", "https://mailnesia.com/mailbox/" + quote(prefix, safe=""))
            page = Page(source).root
            heading = one(page.find("h1", cls="emails"), "Mailbox heading")
            if not any(node.attrs.get("href") == "mailto:" + address for node in heading.find("a")):
                raise ProviderError("mailbox_identity_changed", "Inbox response belongs to another address")
            listing = one(page.find("table", cls="email"), "Message table")
            items = mailnesia_rows(listing)
            # The initial page is page 0. Public source returns only row
            # fragments for p=1,... and an empty body at the end.
            if items:
                for number in range(1, 6):
                    fragment = self.request("GET", "https://mailnesia.com/mailbox/" + quote(prefix, safe="") + "?" + urlencode({"noheadernofooter": "autopager", "p": number}))
                    if not fragment.strip():
                        break
                    more = mailnesia_rows(Page(fragment).root)
                    if not more:
                        raise ProviderError("unexpected_response", "Unrecognized mailbox continuation")
                    items.extend(more)
                else:
                    raise ProviderError("incomplete_mailbox_observation", "Mailbox exceeded the pagination limit")
        elif provider == "disposablemail":
            # This read-only lookup confirms the original cookie session. It must
            # never call /index/index, which can silently allocate a new inbox.
            body = self.json("GET", "https://www.disposablemail.com/index/refresh", xhr=True, referer="https://www.disposablemail.com/")
            # The frontend explicitly uses JSON numeric 0 for an empty list.
            items = [] if type(body) is int and body == 0 else [{**row, "id": identity(row.get("id"))} for row in array(body)]
            for row in items:
                row.update({"from": row.get("od"), "subject": row.get("predmet"), "date": row.get("kdy")})
        elif provider == "tempail":
            # A full page supplies an explicit list and current identity. The
            # AJAX delta's 304 response alone cannot prove an empty mailbox.
            source = self.request("GET", "https://tempail.com/en/")
            page = Page(source).root
            current = one(page.find("input", identity="eposta_adres"), "Mailbox address").attrs.get("value")
            session = match(r'\boturum="([^"]+)"', source, "Mailbox session").group(1)
            if current != address or session != self.config["session"]:
                raise ProviderError("mailbox_identity_changed", "Original mailbox session expired or changed")
            listing = one(page.find(identity="epostalar"), "Message list")
            counts = re.findall(r'\bposta_adet="(\d+)"', source)
            if not counts:
                raise ProviderError("unexpected_response", "Mailbox count missing")
            count = int(counts[-1])
            items = []
            for row in listing.find("li"):
                if not row.attrs.get("id", "").startswith("mail_"):
                    continue
                handlers = [node.attrs.get("onclick", "") for node in [row, *row.walk()]]
                call = match(r"mail_oku\(['\"]?([A-Za-z0-9_-]+)['\"]?\s*,\s*['\"]([A-Za-z0-9_-]+)['\"]\)", " ".join(handlers), "Message read parameters")
                mid, read_key = call.groups()
                if row.attrs["id"] != "mail_" + mid:
                    raise ProviderError("unexpected_response", "Message row and read parameters differ")
                items.append({"id": mid, "read_key": read_key,
                              "from": one(row.find(cls="gonderen"), "Sender").text().strip(),
                              "subject": one(row.find(cls="baslik"), "Subject").text().strip()})
            if len(items) != count:
                raise ProviderError("incomplete_mailbox_observation", "Mailbox count and parsed rows differ")
            if not items and not listing.find(cls="eposta-bekleniyor"):
                raise ProviderError("unexpected_response", "Empty inbox marker missing")
        else:
            raise ProviderError("unsupported_provider", "Web adapter is not implemented")
        return self.cache(items)

    def get_message(self, message_id):
        path = self.folder / "messages.local.json"
        items = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        item = next((row for row in items if row["id"] == message_id), None)
        if item is None:
            raise ProviderError("message_not_in_mailbox", "Message was not observed in this mailbox")
        provider, mid = self.config["provider"], quote(message_id, safe="")
        prefix = quote(self.config["address"].split("@", 1)[0], safe="")
        if provider == "tempmailo":
            return item
        if provider == "disposablemail":
            metadata = self.json("POST", "https://www.disposablemail.com/index/email", form={"id": message_id}, xhr=True, referer="https://www.disposablemail.com/")
            if not isinstance(metadata, dict) or identity(metadata.get("id")) != message_id:
                raise ProviderError("mailbox_identity_changed", "Message detail identity changed")
            if not isinstance(metadata.get("telo"), str):
                raise ProviderError("unexpected_response", "Message body missing")
            return {**item, "from": metadata.get("od"), "subject": metadata.get("predmet"), "html": metadata["telo"]}
        if provider == "mailcatch":
            source = self.request("GET", "https://mailcatch.com/api/data/" + prefix + "/" + mid)
            page = Page(source).root
            content = one(page.find(cls="email-content"), "Message content")
            frames = content.find("iframe")
            if frames:
                frame = one(frames, "Message frame")
                if "srcdoc" not in frame.attrs:
                    raise ProviderError("unexpected_response", "Message frame has no inline body")
                source = frame.attrs["srcdoc"]
            else:
                source = content.html()
            return {**item, "html": source}
        if provider == "mailnesia":
            source = self.request("GET", "https://mailnesia.com/mailbox/" + prefix + "/" + mid + "?noheadernofooter=ajax")
            page = Page(source).root
            bodies = page.find(identity="text_plain_" + message_id) or page.find(identity="text_html_" + message_id)
            return {**item, "html": one(bodies, "Message body with original ID").html()}
        else:
            source = self.request("POST", "https://tempail.com/en/api/oku/", form={"oturum": self.config["session"], "veri[0]": item["read_key"], "veri[1]": message_id}, xhr=True, referer="https://tempail.com/en/")
        if not source.strip() or re.search(r"(?i)(?:<title>|alert-message error|not found|session expired|window.location\s*=)", source):
            raise ProviderError("unexpected_response", "Upstream did not return a message fragment")
        return {**item, "html": source}
