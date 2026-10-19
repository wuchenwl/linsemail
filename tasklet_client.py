"""Tasklet email authentication client derived from its public web frontend."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from uuid import uuid4

from mailbox_clients import Client, ProviderError

API_ORIGIN = "https://api.tasklet.ai"
BUNDLE_BUILD = "7941"


class TaskletClient:
    def __init__(self, folder):
        self.transport = Client(folder)
        self.path = self.transport.folder / "tasklet.local.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def save(self):
        self.path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def post(self, path, payload, *, authenticated=False):
        headers = {"tasklet-client": "web", "tasklet-bundle-build": BUNDLE_BUILD,
                   "Origin": "https://tasklet.ai", "Referer": "https://tasklet.ai/"}
        if authenticated:
            token = self.state.get("session_token")
            if not token:
                raise ProviderError("not_authenticated", "A verified Tasklet session is required")
            headers["Authorization"] = "Bearer " + token
        # Fixed first-party origin and fixed call sites; no arbitrary proxy URLs.
        body = self.transport.json("POST", API_ORIGIN + path, payload=payload, headers_extra=headers)
        if not isinstance(body, dict):
            raise ProviderError("unexpected_response", "Tasklet returned a non-object response")
        return body

    def request_link(self, email):
        email = email.strip()
        if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            raise ProviderError("invalid_email", "Provide one email address")
        if self.state and self.state.get("email") != email:
            raise ProviderError("existing_request", "Use a separate state directory for a different address")
        if self.state.get("session_token"):
            raise ProviderError("already_authenticated", "Existing session preserved")
        if not self.state:
            self.state = {"email": email, "magic_link_secret": str(uuid4()),
                          "created_at": datetime.now(timezone.utc).isoformat(), "status": "request_created"}
            self.save()
        body = self.post("/api/auth/magic-link/request", {"email": email, "magicLinkSecret": self.state["magic_link_secret"]})
        if "expiresAt" not in body:
            raise ProviderError("unexpected_response", "Tasklet response lacks expiresAt")
        self.state.update(status="verification_pending", expires_at=body["expiresAt"],
                          last_requested_at=datetime.now(timezone.utc).isoformat())
        self.save()
        return self.status()

    def verify(self, *, pin=None, magic_link_token=None):
        if not self.state.get("magic_link_secret"):
            raise ProviderError("no_auth_request", "Request a sign-in email first")
        if self.state.get("session_token"):
            raise ProviderError("already_authenticated", "Existing session preserved")
        if (pin is None) == (magic_link_token is None):
            raise ProviderError("invalid_verification", "Supply exactly one PIN or email-link token")
        if magic_link_token is not None:
            if not isinstance(magic_link_token, str) or not 1 <= len(magic_link_token) <= 4096:
                raise ProviderError("invalid_verification", "Invalid link token")
            pin = self.post("/api/auth/magic-link/verify", {"token": magic_link_token}).get("pin")
        if not isinstance(pin, str) or not re.fullmatch(r"[0-9]{6}", pin):
            raise ProviderError("invalid_verification", "PIN must be six digits")
        body = self.post("/api/signIn", {"type": "magic_link", "magicLinkSecret": self.state["magic_link_secret"],
                                          "pin": pin, "allowDuplicate": False, "doNotTrack": True, "sessionIds": None})
        if body.get("type") == "existing_account":
            self.state.update(status="existing_account", existing_providers=body.get("existingProviders", []))
        elif body.get("type") == "success" and body.get("userId") and body.get("sessionToken"):
            self.state.update(status="authenticated", user_id=body["userId"], session_token=body["sessionToken"],
                              is_new_user=body.get("isNewUser", False), authenticated_at=datetime.now(timezone.utc).isoformat())
        else:
            raise ProviderError("unexpected_response", "Unknown sign-in response")
        self.save()
        return self.status()

    def status(self):
        # This reports locally saved state. A session check is a separate upstream request.
        return {"status": self.state.get("status", "not_started"), "email": self.state.get("email"),
                "expires_at": self.state.get("expires_at"), "authenticated": bool(self.state.get("session_token")),
                "user_id": self.state.get("user_id"), "source": "local_state"}

    def check_session(self):
        body = self.post("/api/profile", None, authenticated=True)
        if not body.get("userId") or body["userId"] != self.state.get("user_id"):
            raise ProviderError("unexpected_response", "Profile does not match the saved session")
        self.state["session_checked_at"] = datetime.now(timezone.utc).isoformat()
        self.save()
        return {"authenticated": True, "checked_at": self.state["session_checked_at"],
                "profile": {key: body[key] for key in ("userId", "name", "email", "organizations") if key in body}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("request", "resend", "status", "verify", "session"))
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--email")
    parser.add_argument("--pin")
    parser.add_argument("--link-token")
    args = parser.parse_args()
    client = TaskletClient(args.state_dir)
    try:
        if args.action == "request":
            if not args.email:
                parser.error("request requires --email")
            result = client.request_link(args.email)
        elif args.action == "resend":
            result = client.request_link(client.state.get("email", ""))
        elif args.action == "verify":
            result = client.verify(pin=args.pin, magic_link_token=args.link_token)
        elif args.action == "session":
            result = client.check_session()
        else:
            result = client.status()
        print(json.dumps(result, ensure_ascii=False))
    except ProviderError as error:
        print(json.dumps({"error": error.code, "upstream_status": error.status}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
