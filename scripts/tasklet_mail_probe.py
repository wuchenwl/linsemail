"""Read only inboxes created for this Tasklet test; keep credentials local."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import secrets
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


def request(attempt, label, method, url, payload=None, token=None):
    checked_at = datetime.now(timezone.utc)
    headers = {"Accept": "application/ld+json, application/json", "User-Agent": "linsemail-research/1.0"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    status, raw, error = None, "", ""
    try:
        try:
            response = urlopen(Request(url, data=data, headers=headers, method=method), timeout=30)
        except HTTPError as exc:
            response = exc
        with response:
            status = response.status
            raw = response.read().decode("utf-8", errors="replace")
    except (OSError, URLError) as exc:
        error = str(exc)
    record = {"checked_at": checked_at.isoformat(), "method": method, "url": url,
              "request_body": payload, "authorization": "Bearer from inbox.local.json" if token else None,
              "http_status": status, "stdout": raw, "stderr": error,
              "exit_status": 0 if status is not None and 200 <= status < 300 else 1,
              "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest()}
    path = attempt / (checked_at.strftime("%Y%m%dT%H%M%S.%fZ-") + label + ".local.json")
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if record["exit_status"]:
        raise RuntimeError(f"{label}: HTTP {status}; {error}; evidence={path}")
    return json.loads(raw), path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("create-mailtm", "check", "read"))
    parser.add_argument("--attempt", type=Path, required=True)
    parser.add_argument("--provider", choices=("best_tempmail", "mailtm"))
    parser.add_argument("--message-id")
    args = parser.parse_args()
    attempt = args.attempt.resolve()
    attempt.mkdir(parents=True, exist_ok=True)
    config_path = attempt / "inbox.local.json"
    if args.action == "create-mailtm":
        if config_path.exists():
            raise SystemExit("Existing test inbox preserved; use check instead.")
        domains, _ = request(attempt, "domains", "GET", "https://api.mail.tm/domains")
        domain_items = domains if isinstance(domains, list) else domains.get("hydra:member", [])
        domain = next(item["domain"] for item in domain_items if item.get("isActive") and not item.get("isPrivate"))
        config = {"provider": "mailtm", "address": "ls-tasklet-" + secrets.token_hex(6) + "@" + domain,
                  "password": secrets.token_urlsafe(24), "created_at": datetime.now(timezone.utc).isoformat()}
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        account, account_path = request(attempt, "create-inbox", "POST", "https://api.mail.tm/accounts",
                                        {"address": config["address"], "password": config["password"]})
        config["account_id"] = account["id"]
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        auth, auth_path = request(attempt, "token", "POST", "https://api.mail.tm/token",
                                  {"address": config["address"], "password": config["password"]})
        config["token"] = auth["token"]
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"provider": "mailtm", "address": config["address"], "created": True,
                          "account_evidence": str(account_path), "token_evidence": str(auth_path)}))
        return
    config = json.loads(config_path.read_text(encoding="utf-8"))
    provider = args.provider or config.get("provider")
    if provider == "best_tempmail":
        url = "https://api.best-tempmail.com/v1/inboxes/" + quote(config["address"], safe="") + "/messages"
        token = None
    elif provider == "mailtm":
        url, token = "https://api.mail.tm/messages", config["token"]
    else:
        raise SystemExit("Specify the provider for this existing test inbox.")
    if args.action == "read":
        if not args.message_id:
            parser.error("read requires --message-id")
        url += "/" + quote(args.message_id, safe="")
    body, path = request(attempt, args.action, "GET", url, token=token)
    if args.action == "check":
        items = body.get("messages", []) if provider == "best_tempmail" else body.get("hydra:member", [])
        summary = [{key: item[key] for key in ("id", "subject", "from", "createdAt", "received_at") if key in item}
                   for item in items]
        print(json.dumps({"provider": provider, "address": config["address"], "http_status": 200,
                          "message_count": len(items), "messages": summary, "evidence": str(path)}, ensure_ascii=False))
    else:
        print(json.dumps({"provider": provider, "http_status": 200, "message_keys": list(body), "evidence": str(path)}))


if __name__ == "__main__":
    main()
