import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from mailbox_clients import Client, ProviderError, parse_harakiri_page
from summarize_tasklet_test import observations


def page(prefix="own", skip=0, total=0, ids=(), limit=10):
    return {"inbox": prefix, "skip": skip, "limit": limit, "count": len(ids),
            "total_count": total, "emails": [{"_id": str(i), "subject": "fixture"} for i in ids]}


class Expansion6Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.client = Client(self.folder)

    def harakiri(self):
        self.client.config = {"provider": "harakirimail", "prefix": "own", "address": "own@harakirimail.com"}

    def tempemail(self):
        self.client.config = {"provider": "tempemail_co", "address": "own@tempemail.co"}

    def test_harakiri_identity_mismatch_is_not_an_empty_box(self):
        with self.assertRaises(ProviderError) as error:
            parse_harakiri_page(page(prefix="other"), "own", 0)
        self.assertEqual(error.exception.code, "mailbox_expired_or_changed")

    def test_harakiri_inconsistent_counts_and_missing_list_are_errors(self):
        for key, value in (("count", True), ("count", 1), ("skip", 1), ("limit", 0), ("emails", None), ("total_count", -1)):
            with self.subTest(key=key, value=value):
                data = page()
                data[key] = value
                with self.assertRaises(ProviderError):
                    parse_harakiri_page(data, "own", 0)

    def test_harakiri_assembles_all_pages_before_publishing_message_ids(self):
        self.harakiri()
        self.client.json = Mock(side_effect=[page(total=11, ids=range(10)), page(skip=10, total=11, ids=(10,))])
        items = self.client.list_messages()
        self.assertEqual([item["id"] for item in items], [str(i) for i in range(11)])
        self.assertTrue(self.client.json.call_args_list[1].args[1].endswith("?skip=10"))

    def test_harakiri_changing_total_during_pagination_does_not_publish_partial_result(self):
        self.harakiri()
        self.client.json = Mock(side_effect=[page(total=11, ids=range(10)), page(skip=10, total=12, ids=(10, 11))])
        with self.assertRaises(ProviderError) as error:
            self.client.list_messages()
        self.assertEqual(error.exception.code, "incomplete_mailbox_observation")
        self.assertNotIn("message_ids", self.client.config)

    def test_harakiri_empty_page_before_total_is_incomplete(self):
        self.harakiri()
        self.client.json = Mock(return_value=page(total=1))
        with self.assertRaises(ProviderError) as error:
            self.client.list_messages()
        self.assertEqual(error.exception.code, "incomplete_mailbox_observation")

    def test_harakiri_duplicate_message_across_pages_is_rejected(self):
        self.harakiri()
        self.client.json = Mock(side_effect=[page(total=11, ids=range(10)), page(skip=10, total=11, ids=(0,))])
        with self.assertRaises(ProviderError):
            self.client.list_messages()

    def test_harakiri_large_mailbox_is_reported_as_incomplete(self):
        self.harakiri()
        self.client.json = Mock(return_value=page(total=201, ids=range(10)))
        with self.assertRaises(ProviderError) as error:
            self.client.list_messages()
        self.assertEqual(error.exception.code, "incomplete_mailbox_observation")
        self.assertEqual(self.client.json.call_count, 1)

    def test_unlisted_body_reads_make_no_network_request(self):
        for provider in ("harakirimail", "tempemail_co"):
            self.client.config = {"provider": provider, "message_ids": ["own-id"]}
            self.client.request = Mock()
            with self.assertRaises(ProviderError) as error:
                self.client.get_message("another-id")
            self.assertEqual(error.exception.code, "message_not_listed")
            self.client.request.assert_not_called()

    def test_tempemail_unoffered_domain_stops_before_creating_an_address(self):
        self.client.json = Mock(return_value={"domains": ["gmail.com"]})
        with self.assertRaises(ProviderError):
            self.client.create("tempemail_co")
        self.assertEqual(self.client.json.call_count, 1)

    def test_tempemail_session_reassignment_does_not_replace_original_address(self):
        self.tempemail()
        self.client.json = Mock(return_value={"result": True, "address": "another@tempemail.co", "mails": []})
        with self.assertRaises(ProviderError) as error:
            self.client.list_messages()
        self.assertEqual(error.exception.code, "mailbox_expired_or_changed")
        self.assertEqual(self.client.config["address"], "own@tempemail.co")

    def test_tempemail_failed_missing_or_duplicate_list_is_not_empty(self):
        self.tempemail()
        for body in ({"result": False, "address": "own@tempemail.co", "mails": []},
                     {"result": True, "address": "own@tempemail.co"},
                     {"result": True, "address": "own@tempemail.co", "mails": [{"id": 1}, {"id": "1"}]}):
            self.client.json = Mock(return_value=body)
            with self.assertRaises(ProviderError):
                self.client.list_messages()

    def test_tempemail_body_must_match_listed_id(self):
        self.tempemail()
        self.client.json = Mock(side_effect=[{"result": True, "address": "own@tempemail.co", "mails": [{"id": 7}]},
                                            {"result": True, "mail": {"id": 8, "html": "wrong"}}])
        self.client.list_messages()
        with self.assertRaises(ProviderError):
            self.client.get_message("7")

    def test_tempemail_listed_body_preserves_html_and_sender(self):
        self.tempemail()
        self.client.json = Mock(side_effect=[{"result": True, "address": "own@tempemail.co", "mails": [{"id": 7}]},
                                            {"result": True, "mail": {"id": 7, "html": "<p>fixture</p>", "fromAddress": "sender@example.invalid"}}])
        self.client.list_messages()
        detail = self.client.get_message("7")
        self.assertEqual(detail["html"], "<p>fixture</p>")
        self.assertEqual(detail["from"], "sender@example.invalid")

    def test_summary_does_not_promote_one_page_to_complete_inbox(self):
        self.harakiri()
        self.client.save()
        for i, body in enumerate((page(total=11, ids=range(10)), page(prefix="another"))):
            record = {"url": "https://harakirimail.com/api/v1/inbox/own?skip=0", "http_status": 200,
                      "checked_at": f"2026-10-18T10:00:0{i}+00:00", "stdout": json.dumps(body)}
            (self.folder / f"response-{i}.local.json").write_text(json.dumps(record), encoding="utf-8")
        rows = observations(self.folder, "harakirimail")
        self.assertIsNone(rows[0]["message_count"])
        self.assertEqual(rows[0]["kind"], "partial_paginated_list")
        self.assertEqual(rows[1]["error"], "mailbox_expired_or_changed")

    def test_raw_socket_body_cannot_ambiguously_override_json_or_form(self):
        for kwargs in ({"payload": {}, "raw_body": b"40"}, {"form": {}, "raw_body": b"40"}):
            with self.assertRaises(ValueError):
                self.client.request("POST", "https://example.invalid", **kwargs)


if __name__ == "__main__":
    unittest.main()
