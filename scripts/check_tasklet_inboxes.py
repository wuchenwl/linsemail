"""Check all recorded dedicated inboxes once; never create or resend."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mailbox_clients import Client, ProviderError
from tasklet_mail_probe import request as legacy_request


def content_candidates(items):
    metadata, bodies = [], []
    for item in items:
        if "tasklet" in json.dumps({k: item[k] for k in ("subject", "from", "from_mail", "od", "predmet") if k in item}, ensure_ascii=False).lower():
            metadata.append(item)
        elif "tasklet" in json.dumps({k: item[k] for k in ("html", "text", "body") if k in item}, ensure_ascii=False).lower():
            bodies.append(item)
    return metadata, bodies


def main():
    results = []
    for attempt in sorted((ROOT / "research/tasklet").glob("attempt-*")):
        meta_path = attempt / "attempt.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        inbox_dir = Path(meta.get("inbox_dir", attempt))
        config = json.loads((inbox_dir / "inbox.local.json").read_text(encoding="utf-8"))
        provider = meta.get("provider", config.get("provider", "best_tempmail"))
        result = {"attempt": attempt.name, "provider": provider, "started_at": datetime.now(timezone.utc).isoformat()}
        try:
            if provider in ("best_tempmail", "mailtm"):
                url = ("https://api.best-tempmail.com/v1/inboxes/" + quote(config["address"], safe="") + "/messages" if provider == "best_tempmail" else "https://api.mail.tm/messages")
                body, _ = legacy_request(inbox_dir, "check", "GET", url, token=config.get("token"))
                items = body["messages"] if provider == "best_tempmail" else body["hydra:member"]
                client = None
            else:
                client = Client(inbox_dir)
                items = client.list_messages()
            candidates, body_candidates = content_candidates(items)
            result.update(http_status=200, message_count=len(items), tasklet_metadata_candidates=len(candidates),
                          tasklet_body_candidates=len(body_candidates))
            for item in candidates + body_candidates:
                message_id = Client.message_id(item)
                if client:
                    client.get_message(message_id)
                else:
                    legacy_request(inbox_dir, "read", "GET", url + "/" + quote(message_id, safe=""), token=config.get("token"))
        except (ProviderError, RuntimeError) as exc:
            result.update(error=exc.code if isinstance(exc, ProviderError) else "mailbox_request_failed",
                          http_status=exc.status if isinstance(exc, ProviderError) else None)
        result["checked_at"] = datetime.now(timezone.utc).isoformat()
        with (attempt / "check-history.jsonl").open("a", encoding="utf-8") as history:
            history.write(json.dumps(result, ensure_ascii=False) + "\n")
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    (ROOT / "research/tasklet/latest-checks.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
