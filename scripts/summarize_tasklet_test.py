"""Summarize dedicated Tasklet inbox observations without publishing credentials."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mailbox_clients import LIVEWIRE_BASES, MailRows, WebMailRows, wire_value, parse_tmail_parts, parse_harakiri_page, ProviderError


def merge_check_errors(checks, records):
    """Keep application failures even when expiry validation made no HTTP request."""
    output = list(checks)
    seen = set()
    for record, path in records:
        if not record.get("error") or not record.get("checked_at"):
            continue
        key = (record["checked_at"], record["error"])
        if key in seen:
            continue
        seen.add(key)
        output.append({"checked_at": record["checked_at"], "http_status": record.get("http_status"),
                       "message_count": None, "tasklet_metadata_candidate": False, "error": record["error"],
                       "path": str(path), "kind": "application_check"})
    return sorted(output, key=lambda item: datetime.fromisoformat(item["checked_at"]))


def merge_application_checks(checks, records):
    """Use validated client results when a Livewire delta omits unchanged messages."""
    output = merge_check_errors(checks, records)
    seen = set()
    for record, path in records:
        if record.get("error") or record.get("http_status") != 200 or type(record.get("message_count")) is not int:
            continue
        end = datetime.fromisoformat(record["checked_at"])
        start = datetime.fromisoformat(record.get("started_at", record["checked_at"]))
        key = record["checked_at"]
        if key in seen:
            continue
        seen.add(key)
        if any(start <= datetime.fromisoformat(item["checked_at"]) <= end and item.get("message_count") is not None and not item.get("error") for item in checks):
            continue
        output.append({"checked_at": record["checked_at"], "http_status": 200, "message_count": record["message_count"],
            "tasklet_metadata_candidate": record.get("tasklet_metadata_candidates", 0) > 0, "error": None,
            "tasklet_body_candidate": record.get("tasklet_body_candidates", 0) > 0,
            "path": str(path), "kind": "validated_application_check"})
    return sorted(output, key=lambda item: datetime.fromisoformat(item["checked_at"]))


def observations(folder, provider):
    output = []
    for path in sorted(folder.glob("*.local.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            continue
        command = record.get("command", "")
        url = record.get("url", command.removeprefix("GET ") if isinstance(command, str) else "")
        endpoint = urlsplit(url).path
        params = parse_qs(urlsplit(url).query)
        payload = record.get("request_payload") or {}
        query = payload.get("query", "") if isinstance(payload, dict) else ""
        livewire_fetch = provider in LIVEWIRE_BASES and isinstance(payload, dict) and (
            any(update.get("payload", {}).get("event") == "fetchMessages" for update in payload.get("updates", [])) or
            any(call.get("method") == "__dispatch" and call.get("params", [None])[0] == "fetchMessages"
                for component in payload.get("components", []) for call in component.get("calls", [])))
        is_list = ((provider in ("best_tempmail", "mailtm") and endpoint.endswith("/messages")) or
                   (provider == "grabmail" and endpoint == "/api/v1/mailbox") or
                   (provider in ("fakemail", "minuteinbox") and endpoint == "/index/refresh") or
                   (provider == "tempmail_plus" and endpoint == "/api/mails") or
                   (provider == "byom" and endpoint.startswith("/mails/")) or
                   (provider == "guerrillamail" and endpoint == "/ajax.php" and params.get("f", [None])[0] in ("check_email", "get_email_list")) or
                   (provider == "10minutemail_net" and endpoint == "/mailbox.ajax.php") or
                   (provider == "tempmail_lol" and endpoint == "/v2/inbox") or
                   (provider == "maildrop" and endpoint == "/graphql" and "inbox(" in query) or
                   (provider == "dropmail" and endpoint.startswith("/api/graphql/") and "session(" in query and "mails" in query) or
                   (provider == "duckmail" and endpoint == "/messages") or
                   (provider == "moakt" and endpoint == "/en/inbox" and record.get("method") == "GET") or
                   (provider == "mohmal" and endpoint == "/en/refresh") or
                   (provider == "inboxkitten" and endpoint == "/api/v1/mail/list") or
                   (provider == "awamail" and endpoint == "/welcome/get_emails") or
                   (provider == "tempmail_ing" and endpoint.startswith("/api/emails/")) or
                   (provider == "tempmailto" and endpoint == "/get_messages") or
                   (provider == "10minutemail_one" and re.fullmatch(r"/api/v1/mailbox/[^/]+", endpoint)) or
                   (provider == "22do" and endpoint == "/action/mailbox/message") or
                   (livewire_fetch and endpoint in ("/livewire/message/frontend.app", "/livewire/update")) or
                   (provider == "emailtemp_org" and endpoint == "/messages") or
                   (provider == "tempmail100" and endpoint == "/web/emails") or
                   (provider == "anonymmail" and endpoint == "/api/get") or
                   (provider == "tempmailbox_net" and endpoint == "/api/messages") or
                   (provider == "mailforspam" and re.fullmatch(r"/api/mailboxes/[^/]+/emails", endpoint)) or
                   (provider == "linshiyou" and endpoint == "/api/mail") or
                   (provider == "harakirimail" and endpoint.startswith("/api/v1/inbox/")) or
                   (provider == "tempemail_co" and endpoint == "/home/api_list"))
        if not is_list or not record.get("checked_at"):
            continue
        status = record.get("http_status", record.get("status"))
        count, possible_tasklet, body_candidate, api_error = None, False, False, None
        if status == 200:
            try:
                raw = record.get("body", record.get("stdout", "{}")).lstrip("\ufeff")
                if provider == "byom":
                    match = re.fullmatch(r"\s*linsemail\((.*)\);?\s*", raw, re.S)
                    raw = match.group(1) if match else "null"
                if provider == "linshiyou":
                    body = {"messages": parse_tmail_parts(raw)}
                elif provider == "10minutemail_net":
                    parser = MailRows()
                    parser.feed(raw)
                    body = {"messages": parser.items}
                    if not re.search(r'<table\b[^>]*\bid=["\x27]maillist["\x27]', raw):
                        api_error = "mailbox_expired_or_unexpected_html"
                elif provider in ("moakt", "mohmal"):
                    parser = WebMailRows(provider)
                    parser.feed(raw)
                    body = {"messages": parser.items}
                    if parser.unparsed_rows:
                        api_error = "unrecognized_message_rows"
                    config = json.loads((folder / "inbox.local.json").read_text(encoding="utf-8"))
                    if config["address"] not in raw or (provider == "moakt" and 'id="email_message_list"' not in raw) or (provider == "mohmal" and 'id="inbox-table"' not in raw):
                        api_error = "mailbox_expired_or_unexpected_html"
                else:
                    body = json.loads(raw)
                if provider == "harakirimail":
                    config = json.loads((folder / "inbox.local.json").read_text(encoding="utf-8"))
                    items, total = parse_harakiri_page(body, config["prefix"], int(params.get("skip", ["0"])[0]))
                    if int(params.get("skip", ["0"])[0]) != 0 or total != len(items):
                        output.append({"checked_at": record["checked_at"], "http_status": status, "message_count": None,
                            "tasklet_metadata_candidate": False, "tasklet_body_candidate": False, "error": None,
                            "kind": "partial_paginated_list", "path": str(path.resolve())})
                        continue
                elif provider == "tempemail_co":
                    config = json.loads((folder / "inbox.local.json").read_text(encoding="utf-8"))
                    items = body.get("mails")
                    if body.get("result") is not True:
                        api_error = "upstream_api_error"
                    elif body.get("address") != config["address"]:
                        api_error = "mailbox_expired_or_changed"
                elif provider == "anonymmail":
                    config = json.loads((folder / "inbox.local.json").read_text(encoding="utf-8"))
                    items = body.get(config["address"], {}).get("emails")
                    if body.get("error") or not isinstance(body.get(config["address"]), dict):
                        api_error = "mailbox_expired_or_changed"
                elif provider in ("tempmailbox_net", "linshiyou"):
                    items = body.get("messages")
                elif provider == "mailforspam":
                    config = json.loads((folder / "inbox.local.json").read_text(encoding="utf-8"))
                    items = body.get("emails")
                    if body.get("mailbox") != config["address"]:
                        api_error = "mailbox_expired_or_changed"
                    if not isinstance(items, list) or type(body.get("total_emails")) is not int or body["total_emails"] != len(items):
                        api_error = "unexpected_response"
                elif provider in LIVEWIRE_BASES:
                    data = (wire_value(json.loads(body["components"][0]["snapshot"])["data"]) if provider == "spambox_xyz" else body.get("serverMemo", {}).get("data", {}))
                    if "messages" not in data:
                        output.append({"checked_at": record["checked_at"], "http_status": status, "message_count": None,
                            "tasklet_metadata_candidate": False, "error": None, "kind": "partial_component_update", "path": str(path.resolve())})
                        continue
                    items = data["messages"]
                    if data.get("error"):
                        api_error = "upstream_api_error"
                elif provider in ("best_tempmail", "grabmail", "emailtemp_org"):
                    items = body["messages"]
                    if provider == "emailtemp_org":
                        config = json.loads((folder / "inbox.local.json").read_text(encoding="utf-8"))
                        if body.get("mailbox") != config["address"]:
                            api_error = "mailbox_expired_or_changed"
                elif provider == "mailtm":
                    items = body["hydra:member"]
                elif provider == "tempmail_plus":
                    items = body["mail_list"] if body.get("result") else None
                elif provider == "guerrillamail":
                    items = body["list"]
                elif provider == "10minutemail_net":
                    items = body["messages"]
                elif provider == "tempmail_lol":
                    items = body.get("emails") or []
                    if body.get("expired"):
                        api_error = "mailbox_expired"
                elif provider == "maildrop":
                    items = body["data"]["inbox"]
                elif provider == "dropmail":
                    session = body.get("data", {}).get("session")
                    items = session["mails"] if session else []
                    if not session:
                        api_error = "mailbox_expired_or_graphql_error"
                elif provider == "duckmail":
                    items = body["hydra:member"]
                elif provider in ("moakt", "mohmal"):
                    items = body["messages"]
                elif provider == "inboxkitten":
                    items = [{"subject": item.get("message", {}).get("headers", {}).get("subject"), "from": item.get("sender")} for item in body]
                elif provider == "awamail":
                    items = body.get("data", {}).get("emails") if body.get("success") is True else None
                    if items is None:
                        api_error = "upstream_api_error"
                elif provider == "tempmail_ing":
                    items = body.get("emails") if body.get("success") is True else None
                    if items is None:
                        api_error = "upstream_api_error"
                elif provider == "tempmailto":
                    config = json.loads((folder / "inbox.local.json").read_text(encoding="utf-8"))
                    items = body.get("messages")
                    if body.get("status") is not True:
                        api_error = "upstream_api_error"
                    elif body.get("mailbox") != config["address"]:
                        api_error = "mailbox_expired_or_changed"
                elif provider == "22do":
                    items = [] if body.get("data") is None else body["data"]
                    if body.get("status") is not True or "data" not in body:
                        api_error = "upstream_api_error"
                elif provider == "tempmail100":
                    items = body.get("data", {}).get("list")
                    if "list" in body.get("data", {}) and items is None and body["data"].get("total") == 0:
                        items = []
                    if body.get("code") != 0 or not isinstance(items, list) or body["data"].get("total") != len(items):
                        api_error = "upstream_api_error"
                else:
                    items = [] if body == 0 else body
                if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                    raise ValueError("List response missing")
                count = None if api_error else len(items)
                metadata = [{k: item[k] for k in ("subject", "from", "from_mail", "od", "predmet", "mail_subject", "mail_from", "headerSubject", "fromAddr", "headerfrom") if k in item} for item in items]
                possible_tasklet = "tasklet" in json.dumps(metadata, ensure_ascii=False).lower()
                body_candidate = "tasklet" in json.dumps([{k: item[k] for k in ("html", "text", "body") if k in item} for item in items], ensure_ascii=False).lower()
            except ProviderError as exc:
                api_error = exc.code
            except (ValueError, TypeError, KeyError, AttributeError):
                api_error = "unexpected_response"
        output.append({"checked_at": record["checked_at"], "http_status": status, "message_count": count,
                       "tasklet_metadata_candidate": possible_tasklet, "error": api_error, "path": str(path.resolve())})
        output[-1]["tasklet_body_candidate"] = body_candidate
    return sorted(output, key=lambda item: datetime.fromisoformat(item["checked_at"]))


def main():
    attempts, manifest = [], []
    latest_path = ROOT / "research/tasklet/latest-checks.json"
    latest_checks = {row["attempt"]: row for row in json.loads(latest_path.read_text(encoding="utf-8"))} if latest_path.exists() else {}
    for folder in sorted((ROOT / "research/tasklet").glob("attempt-*")):
        meta_path = folder / "attempt.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        inbox_dir = Path(meta.get("inbox_dir", folder))
        inbox_path = inbox_dir / "inbox.local.json"
        if not inbox_path.exists():
            continue
        inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
        provider = meta.get("provider", inbox.get("provider", "best_tempmail"))
        checks = observations(inbox_dir, provider)
        history_path = folder / "check-history.jsonl"
        records = [(json.loads(line), history_path) for line in history_path.read_text(encoding="utf-8").splitlines() if line] if history_path.exists() else []
        if folder.name in latest_checks:
            records.append((latest_checks[folder.name], latest_path))
        checks = merge_application_checks(checks, records)
        successful = [item for item in checks if item["http_status"] == 200 and item["message_count"] is not None and not item.get("error")]
        if not successful:
            raise RuntimeError(folder.name + ": no parsed successful inbox observation")
        latest, first = successful[-1], successful[0]
        dom_paths = [Path(meta["dom_path"])] if meta.get("dom_path") else list(folder.glob("*.dom.txt"))
        ui_ack = any("Check your email" in p.read_text(encoding="utf-8") and inbox["address"] in p.read_text(encoding="utf-8") for p in dom_paths)
        api_ack = False
        auth_requested_at, auth_expires_at = None, None
        auth_dir = Path(meta["auth_request_dir"]) if meta.get("auth_request_dir") else None
        if auth_dir:
            state = json.loads((auth_dir / "tasklet.local.json").read_text(encoding="utf-8"))
            api_ack = state.get("email") == inbox["address"] and state.get("status") in ("verification_pending", "authenticated")
            auth_requested_at, auth_expires_at = state.get("last_requested_at"), state.get("expires_at")
        needs_review = any(item["tasklet_metadata_candidate"] or item.get("tasklet_body_candidate") for item in checks)
        review_path = folder / "review.json"
        review = json.loads(review_path.read_text(encoding="utf-8")) if review_path.exists() else {}
        verification_received = review.get("verification_received") is True
        registration_succeeded = review.get("registration_succeeded") is True
        if verification_received or registration_succeeded:
            evidence = [Path(p) for p in review.get("evidence_paths", [])]
            if not evidence or not all(p.is_file() for p in evidence):
                raise RuntimeError(folder.name + ": reviewed success lacks evidence files")
        if registration_succeeded and not verification_received:
            raise RuntimeError(folder.name + ": registration requires observed verification evidence")
        status = ("registered" if registration_succeeded else "verification_received" if verification_received else
                  "message_review_required" if needs_review else "inbox_check_failed" if checks[-1]["http_status"] != 200 or checks[-1].get("error") else "verification_not_observed")
        attempts.append({"id": folder.name, "provider": provider, "status": status, "email_accepted": None,
                         "ui_send_acknowledged": ui_ack if ui_ack else None, "api_request_acknowledged": api_ack if auth_dir else None,
                         "verification_received": verification_received, "registration_succeeded": registration_succeeded,
                         "checked_at": checks[-1]["checked_at"], "message_checks": len(successful),
                         "auth_requested_at": auth_requested_at, "auth_expires_at": auth_expires_at,
                         "minutes_after_auth_request": round((datetime.fromisoformat(checks[-1]["checked_at"]) - datetime.fromisoformat(auth_requested_at)).total_seconds() / 60, 2) if auth_requested_at else None,
                         "observation_interval_minutes": round((datetime.fromisoformat(latest["checked_at"]) - datetime.fromisoformat(first["checked_at"])).total_seconds() / 60, 2),
                         "latest_http_status": checks[-1]["http_status"], "latest_error": checks[-1].get("error"),
                         "last_observed_message_count": latest["message_count"],
                         "evidence_dir": str(folder.resolve()), "inbox_evidence_dir": str(inbox_dir.resolve()), "observations": checks,
                         "interpretation": "已执行专用邮箱测试；发信确认与邮件投递分别记录。服务欢迎信不计为 Tasklet 验证信。未收到验证信不能据此判断域名被封。"})
        for evidence_folder in dict.fromkeys([folder, inbox_dir] + ([auth_dir] if auth_dir else [])):
            for path in sorted(evidence_folder.iterdir()):
                if path.is_file():
                    raw = path.read_bytes()
                    manifest.append({"path": str(path.resolve()), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    result = {"target": "https://tasklet.ai/login", "updated_at": datetime.now(timezone.utc).isoformat(), "attempts": attempts,
              "successful_registrations": sum(a["registration_succeeded"] for a in attempts),
              "scope": "仅本项目创建的专用测试邮箱；页面确认、API 确认、实际验证信和完成注册分别记录。"}
    destination = ROOT / "research/tasklet/results.json"
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (destination.parent / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = ["# Tasklet 注册实测", "", f"记录时间：{result['updated_at']}。目标：{result['target']}。", "",
              "官网 Sign up 指向 /login。现在可以使用 Tasklet 的真实 HTTP 登录接口发信，无需反复点击浏览器；接口详情见 tasklet-api.md。", "",
              "|来源|收件检查次数|最近 HTTP|最后成功邮件数|最近错误|距 API 发信分钟|网页确认|API 确认|验证信|注册完成|", "|---|---:|---:|---:|---|---:|---|---|---|---|"]
    for a in attempts:
        report.append(f"|{a['provider']}|{a['message_checks']}|{a['latest_http_status']}|{a['last_observed_message_count']}|{a['latest_error'] or ''}|{a['minutes_after_auth_request']}|{a['ui_send_acknowledged']}|{a['api_request_acknowledged']}|{a['verification_received']}|{a['registration_succeeded']}|")
    report += ["", "FakeMail、GuerrillaMail、10MinuteMail.net、MinuteInbox 的欢迎邮件不作为 Tasklet 验证信；已读取正文。其余观测以原始收件响应为准。Maildrop 官方说明首次投递可能因灰名单延迟约 15 分钟。最后成功邮件数保留历史观测；最近错误以应用检查为准，不能把过期箱或解析失败算作当前空箱。", "",
               "页面显示已发送或 API 返回 expiresAt 仅为请求确认；email_accepted 保留 null。真实验证信尚未观察到时，不执行验证码猜测，也不标记注册成功。", "",
               "原始 HTTP 输入、响应、Cookie、邮箱凭证保存在各目录的 *.local.json / *.local.cookies。公开摘要仅包含状态、次数与证据路径。manifest.json 记录文件哈希。", "",
               "汇总命令：`python -X utf8 scripts/summarize_tasklet_test.py`。", ""]
    (ROOT / "docs/tasklet-test.md").write_text("\n".join(report), encoding="utf-8")
    print(json.dumps({"result": str(destination), "attempts": len(attempts), "successful_registrations": result["successful_registrations"]}))


if __name__ == "__main__":
    main()
