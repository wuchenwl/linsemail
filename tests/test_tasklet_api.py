from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import Catalog
from mail_api import MailboxStore, TaskletStore, make_handler
from mailbox_clients import ProviderError
from tasklet_client import API_ORIGIN, TaskletClient


class TaskletClientTests(unittest.TestCase):
    def test_request_and_resend_reuse_secret_across_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            first = TaskletClient(folder)
            first.post = Mock(return_value={"expiresAt": 2000000000000})
            first.request_link("fixture@example.invalid")
            secret = first.post.call_args.args[1]["magicLinkSecret"]
            second = TaskletClient(folder)
            second.post = Mock(return_value={"expiresAt": 2000000000001})
            second.request_link("fixture@example.invalid")
            self.assertEqual(second.post.call_args.args[1]["magicLinkSecret"], secret)
            self.assertNotIn(secret, json.dumps(second.status()))
            with self.assertRaises(ProviderError):
                second.request_link("different@example.invalid")

    def test_pin_signin_and_session_do_not_disclose_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            client = TaskletClient(folder)
            client.state = {"email": "fixture@example.invalid", "magic_link_secret": "fixture-secret"}
            client.post = Mock(return_value={"type": "success", "userId": "user-fixture", "sessionToken": "private-session"})
            result = client.verify(pin="123456")
            self.assertTrue(result["authenticated"])
            self.assertNotIn("private-session", json.dumps(result))
            self.assertEqual(client.post.call_args.args[0], "/api/signIn")
            self.assertFalse(client.post.call_args.args[1]["allowDuplicate"])
            restored = TaskletClient(folder)
            self.assertEqual(restored.state["session_token"], "private-session")
            restored.transport.json = Mock(return_value={"userId": "user-fixture", "sessionToken": "private-session"})
            result = restored.check_session()
            self.assertEqual(restored.transport.json.call_args.args[1], API_ORIGIN + "/api/profile")
            self.assertIsNone(restored.transport.json.call_args.kwargs["payload"])
            self.assertEqual(restored.transport.json.call_args.kwargs["headers_extra"]["Authorization"], "Bearer private-session")
            self.assertNotIn("private-session", json.dumps(result))

    def test_email_link_token_is_resolved_before_signin(self):
        with tempfile.TemporaryDirectory() as folder:
            client = TaskletClient(folder)
            client.state = {"email": "fixture@example.invalid", "magic_link_secret": "fixture-secret"}
            client.post = Mock(side_effect=[{"pin": "123456"}, {"type": "existing_account", "existingProviders": ["google"]}])
            self.assertEqual(client.verify(magic_link_token="fixture-token")["status"], "existing_account")
            self.assertEqual(client.post.call_args_list[0].args, ("/api/auth/magic-link/verify", {"token": "fixture-token"}))
            self.assertEqual(client.post.call_args_list[1].args[1]["pin"], "123456")
            self.assertNotIn("session_token", client.state)

    def test_invalid_verification_and_pending_session_make_no_network_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            client = TaskletClient(folder)
            client.state = {"magic_link_secret": "fixture-secret"}
            client.transport.json = Mock()
            for kwargs in ({}, {"pin": "12345"}, {"pin": 123456}, {"pin": "123456", "magic_link_token": "token"}):
                with self.assertRaises(ProviderError):
                    client.verify(**kwargs)
            with self.assertRaises(ProviderError):
                client.check_session()
            client.transport.json.assert_not_called()


class TaskletHttpTests(unittest.TestCase):
    def test_http_routes_authenticate_before_any_tasklet_action(self):
        with tempfile.TemporaryDirectory() as folder:
            client = Mock()
            client.state = {"email": "fixture@example.invalid"}
            client.request_link.return_value = {"status": "verification_pending", "authenticated": False}
            client.status.return_value = {"status": "verification_pending", "authenticated": False}
            client.verify.return_value = {"status": "authenticated", "authenticated": True}
            tasklet = TaskletStore(Path(folder) / "tasklet", client_factory=lambda _: client)
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Catalog(), MailboxStore(Path(folder) / "mail"), tasklet))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"

            def request(path, body=None, token=None):
                headers = {"Content-Type": "application/json"}
                if token:
                    headers["Authorization"] = "Bearer " + token
                try:
                    response = urlopen(Request(base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers), timeout=5)
                except HTTPError as exc:
                    response = exc
                with response:
                    return response.status, json.loads(response.read())

            try:
                status, issued = request("/api/tasklet/auth/request", {"email": "fixture@example.invalid"})
                self.assertEqual(status, 202)
                path = "/api/tasklet/auth/" + issued["id"]
                token = issued["access_token"]
                self.assertEqual(request(path, token=token)[0], 200)
                self.assertEqual(request(path + "/verify", {"pin": "123456"}, "wrong")[0], 401)
                client.verify.assert_not_called()
                self.assertEqual(request(path + "/resend", {}, token)[0], 200)
                self.assertEqual(client.request_link.call_count, 2)
                self.assertEqual(request(path + "/verify", {"pin": "123456", "allowDuplicate": True}, token)[0], 400)
                self.assertEqual(request(path + "/verify", {"pin": "123456"}, token)[0], 200)
                client.verify.assert_called_once_with(pin="123456")
                self.assertEqual(request("/api/tasklet/auth/request", {"email": "fixture@example.invalid", "url": "http://localhost"})[0], 400)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(5)


if __name__ == "__main__":
    unittest.main()
