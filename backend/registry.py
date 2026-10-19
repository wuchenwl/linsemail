"""Provider inventory and executable capabilities share one registry."""
import json
from pathlib import Path
from mailbox_clients import CUSTOM_PREFIX_PROVIDERS

from .web_clients import WEB_IDS
from .expansion_clients import EXPANSION_FIELDS, EXPANSION_OPTIONAL_FIELDS

ROOT = Path(__file__).resolve().parents[1]
CATALOG = json.loads((ROOT / "data/providers.json").read_text(encoding="utf-8"))["providers"]
LEGACY_IDS = frozenset(item["id"] for item in CATALOG if item["integration"].get("adapter_implemented"))
HYDRA_BASES = {"mailtm": "https://api.mail.tm", "mailgw": "https://api.mail.gw", "tempemail_cc": "https://tempemail.cc/api", "tempmail_email": "https://api.mail.tm"}

# Only protocols actually implemented in official_clients are advertised here.
OFFICIAL_FIELDS = {
    **{key: [] for key in HYDRA_BASES},
    "burner_kiwi": [], "best_tempmail": [],
    "temp_mail_io": ["api_key"], "agentmail": ["api_key"],
    "mailsac": ["api_key"], "mailosaur": ["api_key", "server_id"],
    "testmail": ["api_key", "namespace"], "mailisk": ["api_key", "namespace"],
}
FIELD_LABELS = {"api_key": "API 密钥", "server_id": "服务器 ID", "namespace": "命名空间", "domain": "收件域名", "team_id": "团队 ID", "folder_id": "收件文件夹 ID", "base_url": "部署地址", "site_password": "站点访问密码（可选）", "agent_id": "代理账户 ID", "domain_id": "已分配域名 ID"}


def _entry(item):
    identity = item["id"]
    api = item.get("api", {})
    legacy = identity in LEGACY_IDS
    implemented = legacy or identity in OFFICIAL_FIELDS or identity in WEB_IDS or identity in EXPANSION_FIELDS
    fields = EXPANSION_FIELDS.get(identity, OFFICIAL_FIELDS.get(identity, []))
    optional_fields = EXPANSION_OPTIONAL_FIELDS.get(identity, [])
    previous = item.get("integration", {}).get("status")
    if implemented:
        state = "credential_required" if fields else "enabled"
        reason = "填写该服务的密钥后，可通过统一 API 创建和取件。" if fields else "已接入创建、列取和正文读取；可用性以最近实测为准。"
        if identity == "tempmail_email":
            reason = "该网页使用 Mail.tm 收件接口，与 Mail.tm 共用收件能力。"
        if identity == 'mailcare':
            reason = '已接入 MailCare 托管收件 API；配置密钥、团队 ID 和已分配收件域名后使用。'
        if identity == 'ninemail':
            reason = '已接入 9Mail 私测版 API；需运营方签发的专属 Bearer Token，目前没有公开自助密钥。'
        if identity == 'hissline':
            reason = '已接入 Hissline（mailhook.co）；需账户密钥、代理账户 ID 和已分配域名 ID。'
        if identity in {'cloudflare_temp_email', 'synox_disposable_mailbox'}:
            reason = '已实现自部署收件接口；需填写自己的部署地址与收件域名，尚未进行部署实测。'
    elif previous == "alias_only":
        state, reason = "alias_only", "该服务提供转发别名，需要绑定收件邮箱，不能直接作为临时收件箱。"
    elif previous == "deployment_required" or "self-hosted" in item["name"]:
        state, reason = "deployment_required", "需要部署自己的邮件服务及域名，再提供收件接口。"
    else:
        state = "adapter_required"
        reason = "尚未接通取件协议。" + ("该服务还需要账户或 API 密钥。" if api.get("account_required") else "")
    access = (item.get("web_interface") or {}).get("mailbox_access") or api.get("auth", "unknown")
    if identity in {"maildrop", "mailcatch", "mailnesia", "restmail", "emailfake", "generator_email", "mail_temp_com"} or access == "none":
        access = "public_address"
    if access in ("mailbox_token", "token", "X-Burner-Key", "free_token"):
        access = "mailbox_token"
    if identity in HYDRA_BASES or identity == 'tempmail_cc':
        access = "mailbox_token"
    if identity == "mailsac":
        access = "public_address"
    return {"id": identity, "name": item["name"], "homepage": item["homepage"],
            "description": reason, "mailbox_access": access, "source_kind": "aggregator",
            "admission_state": state, "blocked_reason": None if state == "enabled" else reason,
            "adapter_implemented": implemented, "adapter_version": "console-0.4" if identity in EXPANSION_FIELDS else "console-0.3" if identity in WEB_IDS else "console-0.2" if implemented else None,
            "transport": ["poll"] if implemented else [],
            "capabilities": {key: "supported" if implemented else "unsupported" for key in ("create", "list", "read_text")} |
                            {key: "unsupported" for key in ("read_html", "delete", "extend")} |
                            {'custom_prefix': 'supported' if identity in CUSTOM_PREFIX_PROVIDERS else 'unsupported'},
            "credential_fields": [{"name": field, "label": FIELD_LABELS[field], "secret": field in {'api_key', 'site_password'}, "required": field in fields} for field in fields + optional_fields],
            "configured": not fields, "last_check": None,
            "documentation": api.get("documentation", []), "interface": "web_session" if identity in WEB_IDS or identity in {"10minutemail", "crazymailing", "maildax"} else "legacy" if legacy else "official_api" if implemented else "unconnected"}


LOCAL = {"id": "local", "name": "本机测试箱", "homepage": None,
         "mailbox_access": "owned_domain", "description": "通过本机 SMTP 1025 端口接收测试邮件，不接收公网来信。",
         "source_kind": "qa_capture", "admission_state": "enabled", "blocked_reason": None,
         "adapter_implemented": True, "adapter_version": "console-0.2", "transport": ["smtp"],
         "capabilities": {"create": "supported", "list": "supported", "read_text": "supported", "read_html": "unsupported", "delete": "unsupported", "extend": "unsupported", "custom_prefix": "unsupported"},
         "credential_fields": [], "configured": True, "last_check": None, "documentation": [], "interface": "local"}
PROVIDERS = [LOCAL] + [_entry(item) for item in CATALOG]
if len({item["id"] for item in PROVIDERS}) != len(PROVIDERS):
    raise RuntimeError("Duplicate provider ID in catalog")


def provider_info(provider_id):
    return next((item.copy() for item in PROVIDERS if item["id"] == provider_id), None)
