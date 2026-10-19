import base64
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime, parseaddr
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import tempfile

from mailbox_clients import Client, ProviderError
from .registry import OFFICIAL_FIELDS, PROVIDERS, WEB_IDS, provider_info


class TextOnly(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "iframe"):
            self.skip += 1
        if tag in ("p", "br", "div", "li", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "iframe") and self.skip:
            self.skip -= 1

    def handle_data(self, value):
        if not self.skip:
            self.parts.append(value)


def to_text(html):
    parser = TextOnly()
    parser.feed(html[:1_000_000])
    return re.sub(r"\n{3,}", "\n\n", "".join(parser.parts)).strip()


def parse_time(value):
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / (1000 if value > 1e12 else 1), timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(str(value))
        except (ValueError, TypeError):
            return None
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)


def normalize(item):
    if not isinstance(item, dict):
        raise ProviderError("unexpected_response", "Message was not an object")
    identity = Client.message_id(item)
    text = item.get("text", item.get("body_text"))
    html = item.get("html", item.get("body_html"))
    if isinstance(html, list):
        html = "\n".join(value for value in html if isinstance(value, str))
    if text is None and isinstance(html, str):
        text = to_text(html)
    sender = item.get("from", item.get("from_mail", item.get("od")))
    if isinstance(sender, dict):
        sender = sender.get("address", sender.get("email"))
    sender = str(sender)[:512] if sender is not None else None
    address = parseaddr(sender or "")[1]
    subject = item.get("subject", item.get("predmet"))
    strength = "weak" if item.get("identity_strength") == "weak" else "strong"
    if not identity:
        strength = "weak"
        identity = "sha256-" + hashlib.sha256(json.dumps({"from": sender, "subject": subject, "text": text, "html": html}, sort_keys=True).encode()).hexdigest()
    return {"upstream_identity": identity[:512], "identity_strength": strength, "from_address": sender,
            "from_domain": address.rpartition("@")[2].lower() or None, "subject": str(subject)[:20000] if subject is not None else None,
            "received_at": parse_time(item.get("received_at", item.get("created_at", item.get("date", item.get("kdy"))))),
            "text_body": str(text)[:1_000_000] if text is not None else None}


def extract(text):
    if text is None:
        return [], "unavailable"
    results = []
    for match in re.finditer(r"(?:验证码|校验码|verification\s+code|one[- ]time\s+(?:code|password)|your\s+code|OTP)\s*(?:is|为|是|[:：])?\s*[:：]?\s*([0-9]{4,8})(?![0-9])", text[:100000], re.I):
        value = match.group(1)
        if not any(item["type"] == "otp" and item["value"] == value for item in results):
            results.append({"type": "otp", "value": value, "source_field": "text_body", "source_span": {"start": match.start(1), "end": match.end(1)}, "rule_id": "contextual-numeric-v1", "rule_strength": "contextual"})
    for match in re.finditer(r"https?://[^\s<>\"']+", text[:100000]):
        value = match.group(0).rstrip(".,，。)")
        if any(word in value.lower() for word in ("verify", "magic", "confirm", "signin")):
            results.append({"type": "magic_link", "value": value, "source_field": "text_body", "source_span": {"start": match.start(), "end": match.start()+len(value)}, "rule_id": "link-context-v1", "rule_strength": "weak"})
    return results[:20], "none" if not results else "unique" if len(results) == 1 else "ambiguous"


class LegacyBridge:
    """Run legacy clients in short-lived private directories; encrypt only session state."""
    def __init__(self, database):
        self.db = database

    def run(self, mailbox, action, upstream_id=None):
        with tempfile.TemporaryDirectory(prefix="linsemail-") as scratch:
            folder = Path(scratch)
            if mailbox.vault_blob:
                saved = self.db.decrypt(mailbox.vault_blob)
                for name, content in saved.items():
                    if name in ("inbox.local.json", "cookies.local.cookies", "messages.local.json"):
                        (folder / name).write_bytes(base64.b64decode(content))
            from .expansion_clients import EXPANSION_FIELDS, ExpansionClient
            if mailbox.provider_id in EXPANSION_FIELDS:
                client = ExpansionClient(folder)
            elif mailbox.provider_id in WEB_IDS:
                from .web_clients import WebClient
                client = WebClient(folder)
            elif mailbox.provider_id in OFFICIAL_FIELDS:
                from .official_clients import OfficialClient
                client = OfficialClient(folder)
            else:
                client = Client(folder)
            if action == "create":
                from .models import ProviderConfiguration
                with self.db.session() as session:
                    config = session.get(ProviderConfiguration, mailbox.provider_id)
                    credentials = self.db.decrypt(config.vault_blob) if config and config.vault_blob else {}
                client.config["credentials"] = credentials
                client.create(mailbox.provider_id)
                # create() consumes address_prefix from the encrypted queued session.
                output = {"address": client.config["address"], "expires_at": parse_time(client.config.get("expires_at"))}
            else:
                if client.config.get("address") != mailbox.address:
                    raise ProviderError("mailbox_identity_changed", "Original mailbox identity did not match")
                if action == "list":
                    output = [normalize(item) for item in client.list_messages()]
                else:
                    # get_message requires the same mailbox's previously observed list IDs.
                    body = client.get_message(upstream_id)
                    body = body.get("message", body) if isinstance(body, dict) else body
                    output = normalize({**body, "id": upstream_id})
                if client.config.get("address") != mailbox.address:
                    raise ProviderError("mailbox_identity_changed", "Upstream changed the original mailbox")
            saved = {name: base64.b64encode((folder / name).read_bytes()).decode() for name in
                     ("inbox.local.json", "cookies.local.cookies", "messages.local.json") if (folder / name).exists()}
            return output, self.db.encrypt(saved)
