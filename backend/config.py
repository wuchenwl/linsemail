from dataclasses import dataclass
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Settings:
    runtime: Path
    database_url: str
    local_session: bool = True
    smtp_port: int = 1025
    api_port: int = 8100
    provider_probes: bool = True
    provider_probe_interval: int = 300
    webhook_allow_loopback: bool = False

    @classmethod
    def load(cls):
        runtime = Path(os.environ.get("LINSEMAIL_RUNTIME", str(ROOT / ".runtime"))).resolve()
        runtime.mkdir(parents=True, exist_ok=True)
        database_url = os.environ.get("LINSEMAIL_DATABASE_URL")
        connection_file = runtime / "database.local.json"
        if not database_url and connection_file.exists():
            database_url = json.loads(connection_file.read_text(encoding="utf-8"))["url"]
        if not database_url:
            raise RuntimeError("Database not configured. Run scripts/start-console.ps1 first.")
        return cls(runtime, database_url, os.environ.get("LINSEMAIL_LOCAL_SESSION", "1") == "1",
                   int(os.environ.get("LINSEMAIL_SMTP_PORT", "1025")), int(os.environ.get("LINSEMAIL_PORT", "8100")),
                   os.environ.get("LINSEMAIL_PROVIDER_PROBES", "1") == "1",
                   max(60, int(os.environ.get("LINSEMAIL_PROVIDER_PROBE_INTERVAL", "300"))),
                   os.environ.get("LINSEMAIL_WEBHOOK_ALLOW_LOOPBACK", "0") == "1")
