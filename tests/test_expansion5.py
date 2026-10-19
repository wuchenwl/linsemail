from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from mailbox_clients import Client, ProviderError, parse_tmail_parts
from check_tasklet_inboxes import content_candidates
from summarize_tasklet_test import observations


class Expansion5Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.client = Client(self.folder)

    def anonymmail(self):
        self.client.config = {"provider": "anonymmail", "address": "owned@example.invalid", "csrf": "old"}

    def tempmailbox(self, expired=False):
        mailbox = {"id": "own-id", "address": "owned@example.invalid",
                   "expiresAt": (datetime.now(timezone.utc) + timedelta(hours=-1 if expired else 1)).isoformat()}
        self.client.config = {"provider": "tempmailbox_net", "address": mailbox["address"], "mailbox": mailbox}
        return mailbox

    def test_anonymmail_expired_csrf_refreshes_once_with_same_cookie_and_address(self):
        self.anonymmail()
        self.client.json = Mock(side_effect=[ProviderError("upstream_http_error", "expired csrf", 403),
                                            {"hash": "new", "name": "csrf_test_name"},
                                            {"owned@example.invalid": {"emails": []}}])
        self.assertEqual(self.client.list_messages(), [])
        calls = self.client.json.call_args_list
        self.assertEqual([call.args[0] for call in calls], ["POST", "GET", "POST"])
        self.assertEqual(calls[0].kwargs["form"], calls[2].kwargs["form"])
        self.assertEqual(calls[2].kwargs["headers_extra"]["X-CSRF-TOKEN"], "new")

    def test_anonymmail_repeated_forbidden_is_reported_without_looping_or_clearing(self):
        self.anonymmail()
        self.client.json = Mock(side_effect=[ProviderError("upstream_http_error", "forbidden", 403),
                                            {"hash": "new"}, ProviderError("upstream_http_error", "forbidden", 403)])
        with self.assertRaises(ProviderError):
            self.client.list_messages()
        self.assertEqual(self.client.json.call_count, 3)
        self.assertFalse(any("clearInbox" in call.args[1] for call in self.client.json.call_args_list))

    def test_anonymmail_missing_original_address_is_not_an_empty_inbox(self):
        self.anonymmail()
        self.client.json = Mock(return_value={"other@example.invalid": {"emails": []}})
        with self.assertRaises(ProviderError) as error:
            self.client.list_messages()
        self.assertEqual(error.exception.code, "mailbox_expired_or_changed")

    def test_anonymmail_body_is_cached_and_reading_never_deletes_upstream_mail(self):
        self.anonymmail()
        self.client.json = Mock(return_value={"owned@example.invalid": {"emails": [{"token": "message-id", "subject": "fixture", "body": "<p>own body</p>"}]}})
        self.client.list_messages()
        self.assertEqual(self.client.get_message("message-id")["html"], "<p>own body</p>")
        self.assertEqual(self.client.json.call_count, 1)
        with self.assertRaises(ProviderError):
            self.client.get_message("another-message")
        self.assertEqual(self.client.json.call_count, 1)

    def test_tempmailbox_session_reassignment_stops_before_reading_messages(self):
        self.tempmailbox()
        self.client.json = Mock(return_value={"mailbox": {"id": "other-id", "address": "other@example.invalid"}})
        with self.assertRaises(ProviderError) as error:
            self.client.list_messages()
        self.assertEqual(error.exception.code, "mailbox_expired_or_changed")
        self.assertEqual(self.client.json.call_count, 1)

    def test_tempmailbox_expiry_does_not_allocate_a_replacement(self):
        self.tempmailbox(expired=True)
        self.client.json = Mock()
        with self.assertRaises(ProviderError) as error:
            self.client.list_messages()
        self.assertEqual(error.exception.code, "mailbox_expired")
        self.client.json.assert_not_called()

    def test_mailforspam_wrong_mailbox_missing_list_and_partial_page_fail(self):
        self.client.config = {"provider": "mailforspam", "address": "owned@example.invalid"}
        for body in ({"mailbox": "other@example.invalid", "total_emails": 0, "emails": []},
                     {"mailbox": "owned@example.invalid", "total_emails": 0},
                     {"mailbox": "owned@example.invalid", "total_emails": 101, "emails": []}):
            self.client.json = Mock(return_value=body)
            with self.assertRaises(ProviderError):
                self.client.list_messages()

    def test_mailforspam_404_is_retained_as_error_instead_of_assuming_empty(self):
        self.client.config = {"provider": "mailforspam", "address": "owned@example.invalid"}
        self.client.json = Mock(side_effect=ProviderError("upstream_http_error", "missing", 404))
        with self.assertRaises(ProviderError) as error:
            self.client.list_messages()
        self.assertEqual(error.exception.status, 404)

    def test_mailforspam_detail_identity_must_match_listed_id(self):
        self.client.config = {"provider": "mailforspam", "address": "owned@example.invalid"}
        self.client.json = Mock(side_effect=[{"mailbox": "owned@example.invalid", "total_emails": 1, "emails": [{"id": "own-id", "sender": "fixture@example.invalid"}]},
                                            {"id": "other-id", "body_text": "wrong message"}])
        self.client.list_messages()
        with self.assertRaises(ProviderError):
            self.client.get_message("own-id")

    def test_linshiyou_native_framing_preserves_html_and_stable_message_identity(self):
        raw = '<li>header</li><-----TMAILCHOPPER-----><div>Tasklet fixture</div><-----TMAILNEXTMAIL----->'
        first, second = parse_tmail_parts(raw), parse_tmail_parts(raw)
        self.assertEqual(first, second)
        self.assertEqual(first[0]["html"], "<div>Tasklet fixture</div>")
        self.assertIsNone(first[0]["subject"])
        self.assertEqual(len(first[0]["id"]), 71)

    def test_linshiyou_only_literal_empty_response_is_an_empty_inbox(self):
        self.assertEqual(parse_tmail_parts(""), [])
        for raw, code in ((" \n", "unexpected_response"), ("DIE", "mailbox_expired"), ("<html>Challenge</html>", "unexpected_response"),
                          ("header<-----TMAILCHOPPER----->", "unexpected_response")):
            with self.assertRaises(ProviderError) as error:
                parse_tmail_parts(raw)
            self.assertEqual(error.exception.code, code)

    def test_linshiyou_different_address_does_not_read_or_replace_original(self):
        self.client.config = {"provider": "linshiyou", "address": "owned@example.invalid"}
        self.client.request = Mock(return_value="other@example.invalid")
        with self.assertRaises(ProviderError) as error:
            self.client.list_messages()
        self.assertEqual(error.exception.code, "mailbox_expired_or_changed")
        self.assertEqual(self.client.request.call_count, 1)
        self.assertEqual(self.client.config["address"], "owned@example.invalid")

    def test_body_only_tasklet_candidate_is_identified_for_review(self):
        own = {"subject": None, "html": "<p>Tasklet verification fixture</p>"}
        metadata, bodies = content_candidates([own, {"subject": "Welcome"}])
        self.assertEqual(metadata, [])
        self.assertEqual(bodies, [own])

    def test_summary_retains_expiry_and_body_only_candidates_without_false_metadata(self):
        for number, raw in enumerate(("DIE", "header<-----TMAILCHOPPER-----><p>Tasklet fixture</p>")):
            record = {"url": "https://linshiyou.com/api/mail", "http_status": 200,
                      "checked_at": f"2026-10-18T07:00:0{number}+00:00", "stdout": raw}
            (self.folder / f"response-{number}.local.json").write_text(json.dumps(record), encoding="utf-8")
        rows = observations(self.folder, "linshiyou")
        self.assertEqual(rows[0]["error"], "mailbox_expired")
        self.assertIsNone(rows[0]["message_count"])
        self.assertEqual(rows[1]["message_count"], 1)
        self.assertFalse(rows[1]["tasklet_metadata_candidate"])
        self.assertTrue(rows[1]["tasklet_body_candidate"])


if __name__ == "__main__":
    unittest.main()
