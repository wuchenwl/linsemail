from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import Catalog
from mail_api import ADAPTERS, MailboxStore, make_handler
from mailbox_clients import Client, ProviderError


class FakeClient:
    calls = []

    def __init__(self, folder):
        self.folder = Path(folder)
        self.path = self.folder / "inbox.local.json"
        self.config = json.loads(self.path.read_text()) if self.path.exists() else {}

    def create(self, provider):
        self.config = {"provider": provider, "address": self.folder.name + "@example.invalid", "secret": "upstream-secret"}
        self.path.write_text(json.dumps(self.config))

    def list_messages(self):
        self.calls.append(self.folder.name)
        if self.config["provider"] == "fakemail":
            raise ProviderError("upstream_http_error", "evidence=C:/private/token-secret.json", 502)
        return [{"mail_id": 42, "subject": "fixture", "from_mail": "sender@example.invalid", "secret": "upstream-secret"}]

    def get_message(self, message_id):
        if message_id != "42":
            raise ProviderError("message_not_listed", "not owned")
        return {"mail_id": 42, "subject": "fixture", "text": "body", "secret": "upstream-secret"}


class MailboxApiTests(unittest.TestCase):
    def setUp(self):
        FakeClient.calls = []
        self.temp = tempfile.TemporaryDirectory()
        self.store = MailboxStore(self.temp.name, client_factory=FakeClient)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Catalog(), self.store))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.temp.cleanup()

    def request(self, path, *, method="GET", body=None, token=None, raw=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        data = raw if raw is not None else json.dumps(body).encode() if body is not None else None
        try:
            response = urlopen(Request(self.base + path, data=data, headers=headers, method=method), timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read())

    def create(self, provider="byom"):
        status, body = self.request("/api/mailboxes", method="POST", body={"provider": provider})
        self.assertEqual(status, 201)
        return body

    def test_tokens_are_bound_to_one_mailbox_before_upstream_access(self):
        first, second = self.create(), self.create()
        for token in (None, "wrong", second["access_token"]):
            self.assertEqual(self.request(first["messages_url"], token=token)[0], 401)
        self.assertEqual(FakeClient.calls, [])
        status, result = self.request(first["messages_url"], token=first["access_token"])
        self.assertEqual(status, 200)
        self.assertEqual(result["items"][0]["id"], "42")
        self.assertNotIn("secret", json.dumps(result))
        access = (Path(self.temp.name) / first["id"] / "access.local.json").read_text()
        self.assertNotIn(first["access_token"], access)

    def test_create_rejects_arbitrary_addresses_urls_and_invalid_json(self):
        for body in ({"provider": "byom", "address": "someone@byom.de"}, {"url": "http://localhost"}, [], {"provider": []}, {"provider": "maildax"}):
            self.assertEqual(self.request("/api/mailboxes", method="POST", body=body)[0], 400)
        self.assertEqual(self.request("/api/mailboxes", method="POST", raw=b"{")[0], 400)
        self.assertEqual(self.request("/api/mailboxes", method="POST", raw=b"x" * 1025)[0], 413)

    def test_upstream_errors_do_not_disclose_paths_or_secrets(self):
        inbox = self.create("fakemail")
        status, result = self.request(inbox["messages_url"], token=inbox["access_token"])
        self.assertEqual((status, result), (502, {"error": "upstream_http_error", "upstream_status": 502}))

    def test_detail_and_unlisted_messages(self):
        inbox = self.create()
        path = inbox["messages_url"]
        token = inbox["access_token"]
        self.assertEqual(self.request(path + "/999", token=token)[0], 404)
        status, result = self.request(path + "/42", token=token)
        self.assertEqual((status, result["text"]), (200, "body"))
        self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(self.request(path + "?email=other", token=token)[0], 400)
        self.assertEqual(self.request(path + "/..%2F..", token=token)[0], 400)

    def test_catalog_and_capabilities_are_served_together(self):
        expected = json.loads((ROOT / "tests/catalog_expectations.json").read_text(encoding="utf-8"))
        self.assertEqual(self.request("/api/providers")[1]["total"], expected["provider_count"])
        self.assertEqual({x["id"] for x in self.request("/api/mailbox-providers")[1]["items"]}, set(ADAPTERS))

    def test_opaque_graphql_message_ids_can_contain_encoded_slashes(self):
        class OpaqueClient(FakeClient):
            def list_messages(self):
                return [{"id": "TWFpbDqc/Mg==", "subject": "fixture"}]

            def get_message(self, message_id):
                if message_id != "TWFpbDqc/Mg==":
                    raise ProviderError("message_not_listed", "not owned")
                return {"id": message_id, "text": "fixture body"}
        self.store.client_factory = OpaqueClient
        inbox = self.create("dropmail")
        path = inbox["messages_url"] + "/" + quote("TWFpbDqc/Mg==", safe="")
        status, result = self.request(path, token=inbox["access_token"])
        self.assertEqual((status, result["id"], result["text"]), (200, "TWFpbDqc/Mg==", "fixture body"))


class FrontendClientTests(unittest.TestCase):
    def test_guerrilla_uses_full_inbox_and_persists_rotated_session(self):
        with tempfile.TemporaryDirectory() as folder:
            client = Client(folder)
            client.config = {"provider": "guerrillamail", "address": "fixture@guerrillamailblock.com", "sid_token": "old"}
            client.json = Mock(side_effect=[{"list": [{"mail_id": 1, "mail_from": "welcome", "mail_subject": "Welcome"}], "sid_token": "new"},
                                           {"mail_id": 1, "mail_subject": "Welcome", "mail_body": "<p>welcome</p>"}])
            self.assertEqual(client.list_messages()[0]["subject"], "Welcome")
            self.assertIn("f=get_email_list", client.json.call_args.args[1])
            self.assertEqual(client.get_message("1")["html"], "<p>welcome</p>")
            self.assertIn("sid_token=new", client.json.call_args.args[1])
            self.assertEqual(Client(folder).config["sid_token"], "new")

    def test_graphql_http_success_with_errors_is_not_an_empty_inbox(self):
        with tempfile.TemporaryDirectory() as folder:
            client = Client(folder)
            client.config = {"provider": "maildrop", "address": "fixture@maildrop.cc"}
            client.json = Mock(return_value={"data": None, "errors": [{"message": "rate limit"}]})
            with self.assertRaises(ProviderError) as error:
                client.list_messages()
            self.assertEqual(error.exception.code, "upstream_graphql_error")
            self.assertFalse((Path(folder) / "messages.local.json").exists())

    def test_lol_stable_ids_survive_reordering_and_expiry_is_distinct(self):
        with tempfile.TemporaryDirectory() as folder:
            client = Client(folder)
            client.config = {"provider": "tempmail_lol", "address": "fixture@example.invalid", "token": "fixture"}
            first = {"from": "a", "subject": "first", "body": "body", "date": 1}
            second = {"from": "b", "subject": "second", "body": "other", "date": 2}
            client.json = Mock(side_effect=[{"expired": False, "emails": [first, second]}, {"expired": False, "emails": [second, first]}, {"expired": True, "emails": None}])
            message_id = client.list_messages()[0]["id"]
            client.list_messages()
            self.assertEqual(client.get_message(message_id)["body"], "body")
            with self.assertRaises(ProviderError) as error:
                client.list_messages()
            self.assertEqual(error.exception.code, "mailbox_expired")

    def test_dropmail_free_token_lifetime_and_session_message_ownership(self):
        with tempfile.TemporaryDirectory() as folder:
            client = Client(folder)
            client.json = Mock(side_effect=[{"token": "af_fixture"}, {"data": {"introduceSession": {"id": "session", "expiresAt": "future", "addresses": [{"address": "fixture@example.invalid"}]}}},
                                           {"data": {"session": {"id": "session", "expiresAt": "future", "mails": [{"id": "opaque/id", "fromAddr": "sender", "headerSubject": "fixture", "text": "body"}]}}}])
            client.create("dropmail")
            self.assertEqual(client.json.call_args_list[0].kwargs["payload"], {"type": "af", "lifetime": "1h"})
            self.assertEqual(client.get_message("opaque/id")["subject"], "fixture")
            with self.assertRaises(ProviderError):
                client.get_message("other")
            self.assertEqual(client.json.call_count, 3)

    def test_10minute_html_rows_and_body_are_scoped_to_own_inbox(self):
        with tempfile.TemporaryDirectory() as folder:
            client = Client(folder)
            client.config = {"provider": "10minutemail_net", "address": "fixture@example.invalid"}
            listing = '<table id="maillist"><tr><td><a href="readmail.html?mid=welcome">sender</a></td><td><a href="readmail.html?mid=welcome">Hi &amp; welcome</a></td><td>now</td></tr></table>'
            detail = '<div>navigation</div><div class="mailinhtml"><img src="welcome.png"><div><p>Hello<br>friend &amp; guest</p></div></div><div>footer</div>'
            client.request = Mock(side_effect=[listing, detail])
            self.assertEqual(len(client.list_messages()), 1)
            body = client.get_message("welcome")
            self.assertEqual(body["subject"], "Hi & welcome")
            self.assertIn("friend &amp; guest", body["html"])
            self.assertNotIn("footer", body["html"])
            self.assertNotIn("navigation", body["html"])

    def test_plus_mail_id_can_be_read_and_other_ids_cannot(self):
        with tempfile.TemporaryDirectory() as folder:
            client = Client(folder)
            client.config = {"provider": "tempmail_plus", "address": "fixture@mailto.plus", "epin": ""}
            client.json = Mock(side_effect=[{"result": True, "mail_list": [{"mail_id": 123}]}, {"result": True, "mail_id": 123, "text": "fixture"}])
            client.list_messages()
            self.assertEqual(client.config["message_ids"], ["123"])
            self.assertEqual(client.get_message("123")["text"], "fixture")
            self.assertIn("/api/mails/123?email=fixture%40mailto.plus", client.json.call_args.args[1])
            with self.assertRaises(ProviderError):
                client.get_message("124")
            self.assertEqual(client.json.call_count, 2)

    def test_byom_parses_jsonp_as_data_and_reads_own_cached_message(self):
        with tempfile.TemporaryDirectory() as folder:
            client = Client(folder)
            client.config = {"provider": "byom", "address": "fixture@byom.de"}
            client.request = Mock(return_value='linsemail([{"id":"abc","text":"body","html":"<p>body</p>"}]);')
            client.list_messages()
            self.assertEqual(client.get_message("abc")["text"], "body")
            client.request.return_value = 'linsemail([]); process.exit()'
            with self.assertRaises(ProviderError):
                client.list_messages()


if __name__ == "__main__":
    unittest.main()
