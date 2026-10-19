from html import escape
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from mailbox_clients import Client, LivewirePage, ProviderError, wire_value
from summarize_tasklet_test import merge_application_checks, observations


class Expansion4Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.client = Client(self.temp.name)

    def v2(self):
        self.client.config = {"provider": "mailtemp_net", "address": "owned@example.invalid", "csrf": "fixture",
            "wire_version": 2, "wire_component": {"fingerprint": {"name": "frontend.app", "id": "component"},
            "serverMemo": {"data": {"email": "owned@example.invalid", "messages": [], "error": "", "initial": True}, "checksum": "old"}}}

    def v3(self):
        snapshot = '{ "data": {"email":"owned@example.invalid","messages":[[],{"s":"arr"}],"error":""}, "memo":{"name":"frontend.app","id":"component","errors":[]}, "checksum":"old" }'
        self.client.config = {"provider": "spambox_xyz", "address": "owned@example.invalid", "csrf": "fixture",
                              "wire_version": 3, "wire_component": snapshot}
        return snapshot

    def test_html_decoder_preserves_literal_signed_snapshot_without_evaluation(self):
        raw = self.v3()
        parser = LivewirePage()
        parser.feed('<script>throw new Error("do not execute")</script><div wire:snapshot="' + escape(raw, quote=True) + '"></div>')
        self.assertEqual(parser.v3["frontend.app"], raw)

    def test_v2_partial_updates_preserve_data_needed_by_next_signed_request(self):
        self.v2()
        self.client.json = Mock(return_value={"serverMemo": {"data": {"initial": False}, "checksum": "new"}, "effects": {}})
        self.client.livewire_call("fetchMessages", [], event=True)
        memo = self.client.config["wire_component"]["serverMemo"]
        self.assertEqual(memo["checksum"], "new")
        self.assertEqual(memo["data"]["email"], "owned@example.invalid")
        self.assertFalse(memo["data"]["initial"])
        self.assertEqual(self.client.json.call_args.kwargs["payload"]["updates"][0]["payload"]["event"], "fetchMessages")

    def test_v3_dispatch_forwards_exact_snapshot_and_keeps_returned_snapshot(self):
        raw = self.v3()
        updated = raw.replace('"old"', '"new"')
        self.client.json = Mock(return_value={"components": [{"snapshot": updated, "effects": {}}]})
        self.client.livewire_call("fetchMessages", {}, event=True)
        component = self.client.json.call_args.kwargs["payload"]["components"][0]
        self.assertEqual(component["snapshot"], raw)
        self.assertEqual(component["calls"][0]["params"], ["fetchMessages", {}])
        self.assertEqual(self.client.config["wire_component"], updated)

    def test_v2_checksum_only_refresh_keeps_original_identity_and_messages(self):
        self.v2()
        self.client.json = Mock(return_value={"serverMemo": {"checksum": "fresh"}, "effects": {"html": None}})
        self.assertEqual(self.client.list_messages(), [])
        memo = self.client.config["wire_component"]["serverMemo"]
        self.assertEqual(memo["checksum"], "fresh")
        self.assertEqual(memo["data"]["email"], "owned@example.invalid")
        self.assertEqual(memo["data"]["messages"], [])

    def test_replaced_or_invalid_v3_state_does_not_overwrite_own_snapshot(self):
        raw = self.v3()
        for updated in ('{}', raw.replace('"component"', '"other"')):
            self.client.json = Mock(return_value={"components": [{"snapshot": updated}]})
            with self.assertRaises(ProviderError):
                self.client.livewire_call("fetchMessages", {}, event=True)
            self.assertEqual(self.client.config["wire_component"], raw)

    def test_validation_and_changed_email_are_errors_instead_of_empty_inboxes(self):
        for response, expected in (({"serverMemo": {"errors": {"captcha": ["required"]}}}, "upstream_validation_error"),
            ({"serverMemo": {"data": {"email": "different@example.invalid", "messages": []}}}, "mailbox_expired_or_changed"),
            ({"serverMemo": {"data": {"error": "IMAP unavailable", "messages": []}}}, "upstream_api_error")):
            self.v2()
            self.client.json = Mock(return_value=response)
            with self.assertRaises(ProviderError) as error:
                self.client.list_messages()
            self.assertEqual(error.exception.code, expected)
            self.assertEqual(self.client.config["address"], "owned@example.invalid")

    def test_php_array_wrappers_are_decoded_and_unknown_types_fail(self):
        self.assertEqual(wire_value([[{"id": "1", "parts": [[], {"s": "arr"}]}], {"s": "arr"}]), [{"id": "1", "parts": []}])
        with self.assertRaises(ProviderError):
            wire_value([{}, {"s": "unrecognized"}])

    def test_only_same_message_links_on_provider_host_can_be_read(self):
        self.v2()
        self.client.json = Mock(return_value={"serverMemo": {"data": {"messages": [{"id": "42", "subject": "fixture"}]}},
            "effects": {"html": '<a href="https://other.invalid/view/42">bad</a><a href="/view/other">wrong message</a><a href="/view/42">own</a>'}})
        items = self.client.list_messages()
        self.assertEqual(items[0]["_read_url"], "https://mailtemp.net/view/42")
        self.client.request = Mock(return_value="<p>own body</p>")
        with self.assertRaises(ProviderError):
            self.client.get_message("other")
        self.client.request.assert_not_called()
        self.assertEqual(self.client.get_message("42")["html"], "<p>own body</p>")

    def test_tempmail100_uuid_mapping_and_total_mismatch(self):
        self.client.config = {"provider": "tempmail100", "address": "owned@example.invalid", "token": "fixture"}
        self.client.json = Mock(return_value={"code": 0, "data": {"total": 1, "list": [{"uuid": "message", "subject": "fixture", "fromAddress": "sender@example.invalid"}]}})
        self.assertEqual(self.client.list_messages()[0]["id"], "message")
        self.client.json = Mock(return_value={"code": 0, "data": {"total": 1, "list": []}})
        with self.assertRaises(ProviderError) as error:
            self.client.list_messages()
        self.assertEqual(error.exception.code, "unexpected_response")

    def test_emailtemp_reassignment_is_not_replacement_of_original_address(self):
        self.client.config = {"provider": "emailtemp_org", "address": "owned@example.invalid", "csrf": "fixture"}
        self.client.json = Mock(return_value={"mailbox": "new@example.invalid", "messages": []})
        with self.assertRaises(ProviderError) as error:
            self.client.list_messages()
        self.assertEqual(error.exception.code, "mailbox_expired_or_changed")

    def test_tempmail100_null_list_is_empty_only_with_explicit_success_and_zero_total(self):
        self.client.config = {"provider": "tempmail100", "address": "owned@example.invalid", "token": "fixture"}
        self.client.json = Mock(return_value={"code": 0, "data": {"total": 0, "list": None}})
        self.assertEqual(self.client.list_messages(), [])
        for response in ({"code": 0, "data": {"total": 1, "list": None}},
                         {"code": 0, "data": {"list": None}},
                         {"code": 0, "data": {"total": 0}},
                         {"code": 1, "data": {"total": 0, "list": None}}):
            self.client.json = Mock(return_value=response)
            with self.assertRaises(ProviderError):
                self.client.list_messages()

    def test_incremental_response_requires_validated_complete_client_observation(self):
        delta = {"checked_at": "2026-10-18T07:00:01+00:00", "http_status": 200, "message_count": None, "error": None}
        result = {"started_at": "2026-10-18T07:00:00+00:00", "checked_at": "2026-10-18T07:00:02+00:00",
                  "http_status": 200, "message_count": 0, "tasklet_metadata_candidates": 0}
        merged = merge_application_checks([delta], [(result, "history"), (result, "latest")])
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[-1]["message_count"], 0)
        self.assertEqual(merged[-1]["kind"], "validated_application_check")

    def test_complete_raw_response_and_application_check_are_not_counted_twice(self):
        raw = {"checked_at": "2026-10-18T07:00:01+00:00", "http_status": 200, "message_count": 1, "error": None}
        result = {"started_at": "2026-10-18T07:00:00+00:00", "checked_at": "2026-10-18T07:00:02+00:00",
                  "http_status": 200, "message_count": 1}
        self.assertEqual(merge_application_checks([raw], [(result, "history")]), [raw])

    def test_summary_distinguishes_null_empty_inbox_from_missing_or_inconsistent_list(self):
        folder = Path(self.temp.name)
        for number, data in enumerate(({"total": 0, "list": None}, {"total": 1, "list": None}, {"total": 0})):
            record = {"url": "https://tempmail100.com/web/emails", "http_status": 200,
                      "checked_at": f"2026-10-18T07:00:0{number}+00:00", "stdout": json.dumps({"code": 0, "data": data})}
            (folder / f"response-{number}.local.json").write_text(json.dumps(record), encoding="utf-8")
        results = observations(folder, "tempmail100")
        self.assertEqual(results[0]["message_count"], 0)
        self.assertIsNone(results[0]["error"])
        self.assertTrue(all(row["error"] and row["message_count"] is None for row in results[1:]))


if __name__ == "__main__":
    unittest.main()
