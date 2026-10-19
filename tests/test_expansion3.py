from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from mailbox_clients import Client, ProviderError
from summarize_tasklet_test import merge_check_errors, observations


class Expansion3Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.client = Client(self.folder)

    def test_nuxt_payload_is_decoded_as_data_and_missing_state_is_rejected(self):
        payload = [["ShallowReactive", 1], {"state": 2, "mailServiceToken": 6}, ["Reactive", 3],
                   {"$semailDomains": 4}, [5], "example.invalid", "public-visitor-token"]
        page = '<script type="application/json" id="__NUXT_DATA__">' + json.dumps(payload) + '</script>'
        self.assertEqual(Client.tenminute_one_page(page), (["example.invalid"], "public-visitor-token"))
        for invalid in ("<html>challenge</html>", page.replace('"example.invalid"', '"https://other.invalid/path"')):
            with self.assertRaises(ProviderError):
                Client.tenminute_one_page(invalid)

    def test_minuteinbox_detects_session_reassignment_before_listing(self):
        self.client.config = {"provider": "minuteinbox", "address": "original@example.invalid", "csrf": "fixture"}
        self.client.json = Mock(return_value={"email": "new@example.invalid"})
        with self.assertRaises(ProviderError) as error:
            self.client.list_messages()
        self.assertEqual(error.exception.code, "mailbox_expired_or_changed")
        self.assertEqual(self.client.json.call_count, 1)
        self.assertEqual(self.client.config["address"], "original@example.invalid")

    def test_tempmailto_does_not_replace_recorded_mailbox_or_ignore_failure(self):
        self.client.config = {"provider": "tempmailto", "address": "original@example.invalid", "csrf": "fixture"}
        for response, expected in (({"status": True, "mailbox": "new@example.invalid", "messages": []}, "mailbox_expired_or_changed"),
                                   ({"status": False, "messages": []}, "upstream_api_error")):
            self.client.json = Mock(return_value=response)
            with self.assertRaises(ProviderError) as error:
                self.client.list_messages()
            self.assertEqual(error.exception.code, expected)

    def test_22do_only_explicit_success_null_means_empty(self):
        self.client.config = {"provider": "22do", "address": "dedicated@example.invalid", "token": "fixture", "token_expires_at": 9999999999}
        self.client.json = Mock(return_value={"status": True, "data": None})
        self.assertEqual(self.client.list_messages(), [])
        for response in ({"status": False, "data": None}, {"status": True}, {"status": True, "data": {}}, {"status": True, "data": [{}]}):
            self.client.json = Mock(return_value=response)
            with self.assertRaises(ProviderError):
                self.client.list_messages()

    def test_22do_message_ids_come_from_list_and_unlisted_reads_make_no_request(self):
        self.client.config = {"provider": "22do", "address": "dedicated@example.invalid", "token": "fixture", "token_expires_at": 9999999999}
        self.client.json = Mock(return_value={"status": True, "data": [{"messageId": "a/b", "subject": "fixture", "time": 10}]})
        items = self.client.list_messages()
        self.assertEqual(items[0]["id"], "a/b")
        self.client.request = Mock(return_value="body")
        with self.assertRaises(ProviderError):
            self.client.get_message("other")
        self.client.request.assert_not_called()
        self.assertEqual(self.client.get_message("a/b")["html"], "body")
        self.assertTrue(self.client.request.call_args.args[1].endswith("a%2Fb"))

    def test_10one_nested_message_body_is_normalized(self):
        self.client.config = {"provider": "10minutemail_one", "address": "dedicated@example.invalid", "token": "fixture", "message_ids": ["42"]}
        (self.folder / "messages.local.json").write_text(json.dumps([{"id": "42", "subject": "fixture"}]), encoding="utf-8")
        self.client.json = Mock(return_value={"id": "42", "body": {"html": "<p>fixture</p>", "text": "fixture"}})
        result = self.client.get_message("42")
        self.assertEqual((result["html"], result["text"]), ("<p>fixture</p>", "fixture"))

    def test_local_expiry_overrides_older_success_without_changing_historical_count(self):
        old = {"checked_at": "2026-10-18T06:00:00+00:00", "http_status": 200, "message_count": 0, "error": None}
        expired = {"checked_at": "2026-10-18T06:30:00+00:00", "http_status": None, "error": "mailbox_expired"}
        checks = merge_check_errors([old], [(expired, "history"), (expired, "latest")])
        self.assertEqual(len(checks), 2)
        self.assertEqual(checks[-1]["error"], "mailbox_expired")
        self.assertIsNone(checks[-1]["message_count"])
        self.assertEqual(checks[0]["message_count"], 0)

    def test_invalid_json_http_200_is_retained_as_failed_observation(self):
        record = {"checked_at": "2026-10-18T06:30:00+00:00", "method": "GET",
                  "url": "https://www.fakemail.net/index/refresh", "http_status": 200, "stdout": "<html>error</html>"}
        (self.folder / "invalid.local.json").write_text(json.dumps(record), encoding="utf-8")
        checks = observations(self.folder, "fakemail")
        self.assertEqual(checks[0]["error"], "unexpected_response")
        self.assertIsNone(checks[0]["message_count"])


if __name__ == "__main__":
    unittest.main()
