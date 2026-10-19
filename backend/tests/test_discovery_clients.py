"""Receive contracts from saved first-party docs and Kmail's current web assets."""
from html import escape
import json

import httpx
import pytest

from backend.discovery_clients import DISCOVERY_FIELDS
from backend.expansion_clients import ExpansionClient
from mailbox_clients import ProviderError


def fixture_transport(provider, calls, state):
    state['address'] = 'dedicated@receiver.example'

    def handler(request):
        calls.append(request)
        path, address = request.url.path, state['address']
        if provider == 'hissline':
            assert request.headers['X-API-Key'] == 'fixture-key'
            assert request.headers['X-Agent-ID'] == 'agent1'
        elif provider not in {'kmail', 'tempmail_ai', 'temp_mail_ai'}:
            assert request.headers['Authorization'] == 'Bearer fixture-key'
        if state.get('denied'):
            return httpx.Response(401, json={'error': 'invalid credential'})
        row = {'id': 'm1', 'from': 'test@sender.example', 'subject': 'Owned contract inbox'}
        body = None if state.get('missing_body') else 'Your code: 654321'
        recipient = 'foreign@receiver.example' if state.get('foreign') else address
        if provider == 'kmail':
            if path == '/api/refresh':
                if len(calls) > 1:
                    assert 'kmail_session=owned' in request.headers['Cookie']
                return httpx.Response(200, json={'email': address, 'mails': [{**row, 'email': row['from']}], 'stats': {}},
                                      headers={'Set-Cookie': 'kmail_session=owned; Path=/; Secure; HttpOnly'})
            assert path == '/mail/m1'
            markup = ('<html><div><div><strong>To:</strong> ' + recipient + '</div></div>'
                      '<iframe id="email-content-iframe"' +
                      (' srcdoc="' + escape('<p>' + body + '</p>', quote=True) + '"' if body else '') +
                      '></iframe></html>')
            return httpx.Response(200, text=markup)
        if provider == 'freecustom':
            if path.endswith('/domains'):
                result = {'success': True, 'count': 1, 'data': [{'domain': 'receiver.example', 'tier': 'free'}]}
            elif request.method == 'POST':
                data = json.loads(request.content)
                assert set(data) == {'inbox'} and data['inbox'].startswith('ls')
                state['address'] = data['inbox']
                result = {'success': True, 'data': {'inbox': state['address'], 'registered_at': '2026-10-19T08:00:00Z'}}
            elif path.endswith('/messages'):
                assert path == '/v1/inboxes/' + address + '/messages'
                result = {'success': True, 'data': {'inbox': address, 'messages': [row], 'count': 1, 'has_more': False}}
            else:
                assert path == '/v1/inboxes/' + address + '/messages/m1'
                result = {'success': True, 'data': {**row, 'to': recipient, 'text': body, 'html': None}}
        elif provider == 'ninemail':
            if request.method == 'POST':
                assert path == '/api/v1/inboxes' and json.loads(request.content) == {'ttl_minutes': 60}
                result = {'data': {'uuid': 'box1', 'address': address, 'expires_at': '2026-10-19T09:00:00Z'}}
            elif path.endswith('/messages'):
                assert path == '/api/v1/inboxes/box1/messages'
                result = {'data': [{'uuid': 'm1', 'subject': row['subject']}], 'meta': {'next_cursor': None}}
            else:
                assert path == '/api/v1/messages/m1'
                result = {'data': {'uuid': 'm1', 'to_addresses': [{'email': recipient}], 'text_body': body, 'html_body': None}}
        elif provider == 'tempmail_ai':
            if request.method == 'POST':
                prefix = json.loads(request.content)['local_part']
                state['address'] = prefix + '@receiver.example'
                result = {'code': 0, 'data': {'local_part': prefix, 'domain': 'receiver.example',
                                            'address': state['address'], 'expires_at': 1792400400}}
            elif path.endswith('/inbox'):
                prefix = address.split('@')[0]
                assert request.url.params['local_part'] == prefix
                result = {'code': 0, 'data': {'session': {'local_part': prefix, 'address': recipient},
                                            'mails': [{**row, 'from_addr': row['from']}]}}
            else:
                assert path.endswith('/mail/m1') and request.url.params['local_part'] == address.split('@')[0]
                result = {'code': 0, 'data': {'id': 'm1', 'text_body': body, 'html': None}}
        elif provider == 'temp_mail_ai':
            if request.method == 'POST':
                assert path == '/api/mailbox/random'
                result = {'success': True, 'email': address}
            elif path.endswith('/messages'):
                assert path == '/api/mailbox/' + address + '/messages'
                result = {'success': True, 'messages': [row]}
            else:
                assert path == '/api/mailbox/' + address + '/message/m1'
                result = {'success': True, 'message': {**row, 'to': recipient, 'text': body, 'html': None}}
        elif provider == 'mailsink':
            if request.method == 'POST':
                assert path == '/v1/inboxes' and json.loads(request.content)['local_part'].startswith('ls')
                result = {'id': 'box1', 'address': address, 'expires_at': '2026-10-19T09:00:00Z'}
            elif path.endswith('/messages'):
                assert path == '/v1/inboxes/box1/messages'
                result = {'messages': [] if state.get('foreign') else [{**row, 'from_address': row['from']}]}
            else:
                assert path == '/v1/messages/m1'
                result = {**row, 'content': {'text': body}}
        else:
            resource = {'id': 'box1', 'type': 'email_address',
                        'attributes': {'email': recipient, 'domain_id': 42, 'expires_at': None}}
            if request.method == 'POST':
                assert path == '/api/v1/email_addresses/random' and json.loads(request.content) == {'domain_id': '42'}
                result = {'data': resource}
            elif path.endswith('/box1'):
                result = {'data': resource}
            elif path.endswith('/inbound_emails'):
                assert path == '/api/v1/email_addresses/box1/inbound_emails'
                result = {'data': [{'id': 'm1', 'type': 'inbound_email', 'attributes':
                                   {'from_email': row['from'], 'subject': row['subject'], 'received_at': '2026-10-19T08:00:00Z'}}]}
            else:
                assert path == '/api/v1/email_addresses/box1/inbound_emails/m1'
                result = {'data': {'id': 'm1', 'type': 'inbound_email', 'attributes': {'text_body': body, 'html_body': None}}}
        return httpx.Response(200, json=result)
    return handler


def configured(tmp_path):
    client = ExpansionClient(tmp_path)
    client.config['credentials'] = {'api_key': 'fixture-key', 'agent_id': 'agent1', 'domain_id': '42'}
    return client


def install_fixture(provider, monkeypatch):
    calls, state = [], {}
    native = httpx.Client
    handler = fixture_transport(provider, calls, state)
    monkeypatch.setattr('backend.web_clients.httpx.Client',
                        lambda **kwargs: native(transport=httpx.MockTransport(handler), **kwargs))
    return calls, state


@pytest.mark.parametrize('provider', DISCOVERY_FIELDS)
def test_nonempty_receive_reopens_original_mailbox_and_blocks_unobserved_ids(provider, tmp_path, monkeypatch):
    calls, state = install_fixture(provider, monkeypatch)
    client = configured(tmp_path)
    client.create(provider)
    client = ExpansionClient(tmp_path)
    assert client.config['address'] == state['address']
    assert [row['id'] for row in client.list_messages()] == ['m1']
    message = client.get_message('m1')
    assert 'Your code: 654321' in (message.get('text') or message.get('html'))
    count = len(calls)
    with pytest.raises(ProviderError) as error:
        client.get_message('never-observed')
    assert error.value.code == 'message_not_in_mailbox' and len(calls) == count
    assert len([r for r in calls if r.method == 'POST']) == (0 if provider == 'kmail' else 1)


@pytest.mark.parametrize('provider', DISCOVERY_FIELDS)
def test_foreign_recipient_and_missing_body_are_rejected(provider, tmp_path, monkeypatch):
    _, state = install_fixture(provider, monkeypatch)
    client = configured(tmp_path)
    client.create(provider)
    state['foreign'] = True
    with pytest.raises(ProviderError) as error:
        client.get_message('m1')
    assert error.value.code == ('message_not_in_mailbox' if provider == 'mailsink' else 'mailbox_identity_changed')
    state['foreign'], state['missing_body'] = False, True
    client.list_messages()
    with pytest.raises(ProviderError) as error:
        client.get_message('m1')
    assert error.value.code == 'unexpected_response'


@pytest.mark.parametrize('provider', DISCOVERY_FIELDS)
def test_unauthorized_requests_do_not_create_empty_mailboxes(provider, tmp_path, monkeypatch):
    _, state = install_fixture(provider, monkeypatch)
    state['denied'] = True
    with pytest.raises(ProviderError) as error:
        configured(tmp_path).create(provider)
    assert error.value.code == 'upstream_access_denied'
    assert not (tmp_path / 'messages.local.json').exists()


def test_kmail_lost_cookie_never_refreshes_into_another_mailbox(tmp_path, monkeypatch):
    calls, _ = install_fixture('kmail', monkeypatch)
    client = configured(tmp_path)
    client.create('kmail')
    client.cookies.clear()
    count = len(calls)
    with pytest.raises(ProviderError) as error:
        client.list_messages()
    assert error.value.code == 'mailbox_session_lost' and len(calls) == count


def test_kmail_missing_session_cannot_be_admitted(tmp_path, monkeypatch):
    client = configured(tmp_path)
    monkeypatch.setattr(client, 'api', lambda *a, **kw: {'email': 'owned@receiver.example', 'mails': []})
    with pytest.raises(ProviderError) as error:
        client.create('kmail')
    assert error.value.code == 'session_initialization_failed'


@pytest.mark.parametrize('reply', [
    {'code': 1, 'data': {'mails': []}},
    {'code': False, 'data': {'mails': []}},
    {'code': 0, 'data': {'session': {'local_part': 'owned', 'address': 'other@receiver.example'}, 'mails': []}},
])
def test_tempmail_ai_errors_and_foreign_sessions_do_not_appear_empty(reply, tmp_path, monkeypatch):
    client = configured(tmp_path)
    client.config.update(provider='tempmail_ai', address='owned@receiver.example', inbox_id='owned')
    monkeypatch.setattr(client, 'api', lambda *a, **kw: reply)
    with pytest.raises(ProviderError):
        client.list_messages()
    assert not (tmp_path / 'messages.local.json').exists()


@pytest.mark.parametrize('provider', ['tempmail_ai', 'mailsink'])
def test_global_detail_not_called_for_message_removed_from_original_inbox(provider, tmp_path, monkeypatch):
    calls, state = install_fixture(provider, monkeypatch)
    client = configured(tmp_path)
    client.create(provider)
    if provider == 'mailsink':
        state['foreign'] = True
    else:
        original = client.api
        def api(method, url, **kw):
            result = original(method, url, **kw)
            if url.endswith('/inbox'):
                result['data']['mails'] = []
            return result
        monkeypatch.setattr(client, 'api', api)
    with pytest.raises(ProviderError) as error:
        client.get_message('m1')
    assert error.value.code == 'message_not_in_mailbox'
    assert not any('/mail/m1' in r.url.path or '/messages/m1' in r.url.path for r in calls)


@pytest.mark.parametrize('provider', ['freecustom', 'ninemail'])
def test_cursor_pagination_collects_all_and_failed_followup_preserves_previous_cache(provider, tmp_path, monkeypatch):
    client = configured(tmp_path)
    client.config.update(provider=provider, address='owned@receiver.example', inbox_id='box1')
    cursors = []

    def api(*a, params=None, **kw):
        cursor = params.get('before' if provider == 'freecustom' else 'cursor')
        cursors.append(cursor)
        mid = 'm2' if cursor else 'm1'
        if provider == 'freecustom':
            return {'success': True, 'data': {'inbox': client.config['address'], 'messages': [{'id': mid}],
                                             'count': 1, 'has_more': cursor is None}}
        return {'data': [{'uuid': mid}], 'meta': {'next_cursor': None if cursor else 'page2'}}

    monkeypatch.setattr(client, 'api', api)
    assert [row['id'] for row in client.list_messages()] == ['m1', 'm2']
    assert cursors == [None, 'm1' if provider == 'freecustom' else 'page2']
    previous = (tmp_path / 'messages.local.json').read_bytes()
    calls = []

    def interrupted(*a, **kw):
        calls.append(1)
        if len(calls) == 2:
            raise ProviderError('upstream_network_error', 'Interrupted followup')
        return ({'success': True, 'data': {'inbox': client.config['address'], 'messages': [{'id': 'new'}], 'count': 1, 'has_more': True}}
                if provider == 'freecustom' else {'data': [{'uuid': 'new'}], 'meta': {'next_cursor': 'next'}})

    monkeypatch.setattr(client, 'api', interrupted)
    with pytest.raises(ProviderError):
        client.list_messages()
    assert (tmp_path / 'messages.local.json').read_bytes() == previous


@pytest.mark.parametrize('provider,reply', [
    ('kmail', {'email': 'foreign@receiver.example', 'mails': []}),
    ('freecustom', {'success': True, 'data': {'inbox': 'owned@receiver.example', 'messages': [], 'count': 1, 'has_more': False}}),
    ('freecustom', {'success': True, 'data': {'inbox': 'owned@receiver.example', 'messages': [], 'count': 0}}),
    ('freecustom', {'success': True, 'data': {'inbox': 'owned@receiver.example', 'messages': [], 'count': 0, 'has_more': True}}),
    ('freecustom', {'success': True, 'data': {'inbox': 'owned@receiver.example', 'messages': [{'id': 'loop'}], 'count': 1, 'has_more': True}}),
    ('ninemail', {'data': [], 'meta': {}}),
    ('ninemail', {'data': [], 'meta': {'next_cursor': 'next'}}),
    ('ninemail', {'data': [{'uuid': 'loop'}], 'meta': {'next_cursor': 'next'}}),
    ('ninemail', {'data': [], 'meta': {'next_cursor': False}}),
])
def test_identity_count_and_continuation_failures_do_not_replace_saved_observation(provider, reply, tmp_path, monkeypatch):
    client = configured(tmp_path)
    client.config.update(provider=provider, address='owned@receiver.example', inbox_id='box1')
    client.cache([{'id': 'original'}])
    previous = (tmp_path / 'messages.local.json').read_bytes()
    monkeypatch.setattr(client, 'api', lambda *a, **kw: reply)
    with pytest.raises(ProviderError):
        client.list_messages()
    assert (tmp_path / 'messages.local.json').read_bytes() == previous


@pytest.mark.parametrize('change', ['id', 'email', 'domain_id', 'type', 'capacity'])
def test_hissline_scopes_original_mailbox_and_reports_documented_capacity(change, tmp_path, monkeypatch):
    client = configured(tmp_path)
    client.config.update(provider='hissline', address='owned@receiver.example', inbox_id='box1')

    def api(method, url, **kw):
        if url.endswith('/inbound_emails'):
            return {'data': [{'id': index, 'type': 'inbound_email', 'attributes': {}} for index in range(100)]}
        item = {'id': 'box1', 'type': 'email_address', 'attributes': {'email': 'owned@receiver.example', 'domain_id': 42}}
        if change in {'id', 'type'}:
            item[change] = 'foreign'
        elif change != 'capacity':
            item['attributes'][change] = 'foreign'
        return {'data': item}

    monkeypatch.setattr(client, 'api', api)
    with pytest.raises(ProviderError) as error:
        client.list_messages()
    assert error.value.code == ('incomplete_mailbox_observation' if change == 'capacity' else
                                'unexpected_response' if change == 'type' else 'mailbox_identity_changed')
    assert not (tmp_path / 'messages.local.json').exists()
