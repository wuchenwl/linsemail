from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mailbox_clients import Client, ProviderError, WebMailRows


class AdditionalFrontendTests(unittest.TestCase):
    def test_moakt_rows_keep_read_links_and_ignore_controls_and_external_links(self):
        parser = WebMailRows("moakt")
        parser.feed('<table><tr><td><a href="/en/inbox/compose">Compose</a></td><td>control</td></tr>'
                    '<tr><td><a href="https://other.invalid/en/inbox/abc">other</a></td><td>other</td></tr>'
                    '<tr><td><a href="/en/inbox/a81b9">Sign <b>in</b></a></td><td>Tasklet</td><td>delete</td></tr></table>')
        self.assertEqual(parser.items, [{"id": "a81b9", "subject": "Sign in", "from": "Tasklet", "_path": "/en/inbox/a81b9"}])

    def test_expired_html_session_cannot_silently_switch_address(self):
        for provider, html in (("moakt", '<div id="email-address">new@example.invalid</div><div id="email_message_list"></div>'),
                               ("mohmal", '<div data-email="new@example.invalid"></div><table id="inbox-table"></table>')):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as folder:
                client = Client(folder)
                client.config = {"provider": provider, "address": "original@example.invalid"}
                client.request = Mock(return_value=html)
                with self.assertRaises(ProviderError) as error:
                    client.list_messages()
                self.assertEqual(error.exception.code, "mailbox_expired_or_unexpected_html")
                self.assertEqual(client.config["address"], "original@example.invalid")
                self.assertFalse((Path(folder) / "messages.local.json").exists())

    def test_mohmal_rows_follow_data_message_ids_not_control_links(self):
        parser = WebMailRows("mohmal")
        parser.feed('<table><tr class="no-messages"><td>none</td></tr><tr data-msg-id="42">'
                    '<td><b>Sign in</b></td><td>now</td><td>Tasklet</td></tr></table>')
        self.assertEqual(parser.items, [{"id": "42", "subject": "Sign in", "date": "now", "from": "Tasklet"}])

    def test_ing_sends_numeric_minutes_and_does_not_query_expired_addresses(self):
        with tempfile.TemporaryDirectory() as folder:
            client = Client(folder)
            expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            client.json = Mock(side_effect=[{"success": True, "email": {"address": "fixture@example.invalid", "expiresAt": expiry}},
                                           {"success": True, "emails": [{"id": 7, "from_address": "Tasklet", "subject": "Sign in", "text": "fixture PIN"}]}])
            client.create("tempmail_ing")
            self.assertEqual(client.json.call_args_list[0].kwargs["payload"], {"duration": 60})
            self.assertEqual(client.get_message("7")["text"], "fixture PIN")
            client.config["expires_at"] = "2000-01-01T00:00:00Z"
            with self.assertRaises(ProviderError) as error:
                client.list_messages()
            self.assertEqual(error.exception.code, "mailbox_expired")
            self.assertEqual(client.json.call_count, 2)

    def test_awamail_failed_json_is_not_a_successful_empty_inbox(self):
        with tempfile.TemporaryDirectory() as folder:
            client = Client(folder)
            client.config = {"provider": "awamail", "address": "fixture@example.invalid"}
            client.json = Mock(return_value={"success": False, "error": "expired"})
            with self.assertRaises(ProviderError):
                client.list_messages()
            self.assertFalse((Path(folder) / "messages.local.json").exists())

    def test_kitten_storage_locator_can_only_come_from_same_inbox_list(self):
        with tempfile.TemporaryDirectory() as folder:
            client = Client(folder)
            client.config = {"provider": "inboxkitten", "address": "fixture@inboxkitten.com"}
            client.json = Mock(side_effect=[[{"storage": {"region": "us", "key": "opaque/key"}, "sender": "Tasklet", "message": {"headers": {"subject": "Sign in"}}}], {}])
            client.request = Mock(return_value="<p>fixture</p>")
            self.assertEqual(client.list_messages()[0]["subject"], "Sign in")
            with self.assertRaises(ProviderError):
                client.get_message("not-listed")
            self.assertEqual(client.get_message("opaque/key")["html"], "<p>fixture</p>")
            self.assertIn("key=opaque%2Fkey", client.request.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
