from pathlib import Path
import hashlib
import json
import secrets

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from .models import Base, Credential, ProviderBudget, Workspace

ALL_SCOPES = ["providers:read", "mailbox:read", "mailbox:write", "messages:read", "messages:fetch", "waits:write", "events:read", "tokens:write"]


class Database:
    def __init__(self, settings):
        self.settings = settings
        options = {"connect_args": {"check_same_thread": False}} if settings.database_url.startswith("sqlite") else {"pool_pre_ping": True}
        self.engine = create_engine(settings.database_url, **options)
        self.session = sessionmaker(self.engine, expire_on_commit=False)
        key_path = settings.runtime / "vault.key.local"
        if not key_path.exists():
            try:
                with key_path.open("xb") as stream:
                    stream.write(Fernet.generate_key())
            except FileExistsError:
                pass
        self.cipher = Fernet(key_path.read_bytes().strip())

    def initialize(self):
        Base.metadata.create_all(self.engine)
        with self.session.begin() as session:
            if not session.get(Workspace, "ws_local"):
                session.add(Workspace(id="ws_local", name="本地工作区"))
                session.flush()
            from .registry import PROVIDERS
            for provider in PROVIDERS:
                if provider["adapter_implemented"] and not session.get(ProviderBudget, provider["id"]):
                    session.add(ProviderBudget(provider_id=provider["id"]))
            # Only the API/launcher initializes an admin key. Never overwrite a previous key.
            key_file = self.settings.runtime / "admin.local.json"
            existing = session.scalar(select(Credential).where(Credential.id == "key_local_admin"))
            if not existing:
                token = "lm_" + secrets.token_urlsafe(40)
                session.add(Credential(id="key_local_admin", workspace_id="ws_local", token_hash=hashlib.sha256(token.encode()).hexdigest(), kind="workspace", scopes=ALL_SCOPES))
                key_file.write_text(json.dumps({"access_token": token, "token_type": "Bearer", "base_url": f"http://127.0.0.1:{self.settings.api_port}/api/v1"}) + "\n", encoding="utf-8")

    def encrypt(self, value):
        return self.cipher.encrypt(json.dumps(value).encode()).decode()

    def decrypt(self, value):
        return json.loads(self.cipher.decrypt(value.encode()).decode())

    def close(self):
        self.engine.dispose()
