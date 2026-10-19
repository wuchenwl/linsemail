"""Nonempty protocols, owned mailbox identity, bounded work and paging failures."""
from html import escape
import json
from urllib.parse import parse_qs

import httpx
import pytest

from backend.continuation_clients import CONTINUATION_FIELDS, solve_pow
from backend.expansion_clients import ExpansionClient
from mailbox_clients import ProviderError


def fixture(provider, monkeypatch):
    state = {'address': 'dedicated@receiver.example'}
    calls = []
    native = httpx.Client

    def handle(req):
        calls.append(req)
        path = req.url.path
        address = state['address']
        recipient = 'foreign@receiver.example' if state.get('foreign') else address
        body = None if state.get('missing_body') else 'Your code: 840217'
        rows = [] if state.get('empty') else [{'id': 'm1', 'subject': 'Owned fixture', 'sender': 'test@sender.example'}]
        if state.get('denied'):
            return httpx.Response(401, json={'error': 'denied'})
        if provider in {'tmailor', 'tempmailgrab'}:
            assert req.headers['Authorization'] == 'Bearer fixture-key'
        if provider == 'noemail':
            if path == '/':
                return httpx.Response(200, text='<select id="domain"><option value="receiver.example">domain</option></select>')
            state['address'] = path[1:]
            address = state['address']
            recipient = 'foreign@receiver.example' if state.get('foreign') else address
            markup = '<input type="email" value="' + address + '"><div class="card"><div class="card-header">Subject: Ad</div></div>'
            if state.get('empty'):
                markup += '<div>No emails received yet.</div>'
            else:
                markup += ('<div class="card"><div class="card-header"><div>To: ' + recipient +
                           '<br>From: test@sender.example<br>Subject: Owned fixture<br>Received: 2026-10-19</div>'
                           '<a href="mailto:staff@receiver.example?subject=Report%20abuse%20(ID%201)">Report</a></div>'
                           '<div class="card-body"><h5 class="card-title">Owned fixture</h5>' +
                           ('<p class="card-text">' + body + '</p>' if body else '') + '</div></div>')
            return httpx.Response(200, text='malformed' if state.get('malformed') else markup)
        if provider == 'temp_mail24':
            if path.endswith('bootstrap.php'):
                result = {'success': True, 'data': {'email': recipient}}
            else:
                assert 'owned_session=fixture' in req.headers['Cookie']
                form = parse_qs(req.content.decode())
                action = form['action'][0].rsplit('_', 1)[-1]
                if action == 'nonce':
                    result = {'success': True, 'data': {'nonce': 'fixture-nonce'}}
                elif action == 'inbox':
                    assert form['nonce'] == ['fixture-nonce']
                    markup = '' if state.get('empty') else ('<li><a href="https://temp-mail24.com/?tm=1">'
                              '<span class="email">test@sender.example</span><span class="subject">Owned fixture</span></a></li>')
                    result = {'success': True, 'data': {'html': markup}}
                else:
                    assert action == 'message' and form['message_id'] == ['1']
                    result = {'success': True, 'data': {'html': '<div class="message-subject">Subject: Owned fixture</div>'
                              '<iframe class="message-frame"' + (' srcdoc="' + escape(body) + '"' if body else '') + '></iframe>'}}
            if state.get('malformed'):
                result = {'success': False, 'data': {}}
            return httpx.Response(200, json=result, headers={'Set-Cookie': 'owned_session=fixture; Path=/; Secure'})
        if provider == 'nukemail':
            if path == '/api/domains':
                result = {'domains': [{'domain': 'receiver.example', 'is_premium_only': False}]}
            elif path.endswith('/challenge'):
                result = {'id': 'challenge', 'challenge': 'fixture', 'bits': 1, 'solutions': 1}
            elif path.endswith('/create'):
                sent = json.loads(req.content)
                assert sent['pow_id'] == 'challenge' and sent['pow_nonce']
                state['address'] = sent['address'] + '@' + sent['domain']
                result = {'email': state['address'], 'token': 'mailbox-token', 'address': {
                    'id': 'box1', 'address': sent['address'], 'domain': sent['domain'], 'token': 'mailbox-token'}}
            else:
                assert path == '/api/inbox' and 'owned_session=fixture' in req.headers['Cookie']
                result = {'token': 'mailbox-token', 'state': 'active', 'addresses': [{
                    'id': 'box1', 'address': recipient.split('@')[0], 'domain': recipient.split('@')[1], 'token': 'mailbox-token'}],
                    'messages': [{**r, 'sender': 'test@sender.example', 'body_text': body, 'body_html': None} for r in rows]}
                if state.get('additional_address'):
                    result['addresses'].append({'id': 'foreign', 'address': 'other', 'domain': 'receiver.example', 'token': 'mailbox-token'})
            if state.get('malformed'):
                result = {'token': 'mailbox-token', 'state': 'active', 'addresses': []}
            return httpx.Response(200, json=result, headers={'Set-Cookie': 'owned_session=fixture; Path=/; Secure'})
        if provider == 'xelio':
            if req.method == 'POST':
                state['address'] = json.loads(req.content)['username'] + '@receiver.example'
                result = {'address': state['address'], 'access_token': 'mailbox-token'}
            else:
                assert req.headers['X-Mailbox-Token'] == 'mailbox-token'
                raw = 'From: test@sender.example\nTo: ' + recipient + '\nSubject: Owned fixture\n\nYour code: 840217'
                row = {'id': 'm1', 'recipients': recipient, 'sender': 'test@sender.example', 'data': raw}
                if state.get('missing_body'):
                    row['data'] = ''
                result = ([] if state.get('empty') else [row]) if path.endswith(address) else row
            return httpx.Response(200, json={'success': not state.get('malformed', False), 'data': result})
        if provider == 'tmailor':
            sent = json.loads(req.content)
            if path == '/v1/mailboxes':
                assert sent == {} and req.headers['Idempotency-Key'].startswith('ls')
                result = {'email': address, 'token': 'mailbox-token'}
            else:
                assert sent['token'] == 'mailbox-token' and 'mailbox-token' not in str(req.url)
                if path.endswith('/info'):
                    result = {'email': recipient, 'token': 'mailbox-token'}
                elif path.endswith('/messages'):
                    result = {'items': [{**r, 'from': {'address': 'test@sender.example'}, 'to': recipient} for r in rows],
                              'total': len(rows), 'next_cursor': None}
                    if state.get('loop'):
                        result.update(total=999, next_cursor='repeat')
                    elif state.get('truncated'):
                        result['total'] = len(rows) + 1
                else:
                    result = {'id': 'm1', 'to': recipient, 'seen': True, 'body_available': True, 'text': body, 'html': None}
            if state.get('malformed'):
                result = {}
            return httpx.Response(200, json=result)
        if req.method == 'POST':
            assert path.endswith('/inbox') and json.loads(req.content)['prefix'].startswith('ls')
            result = {'id': 'box1', 'address': address, 'expires_at': 1792400400}
        elif path.endswith('/messages'):
            assert path == '/api/v1/inbox/box1/messages'
            result = {'messages': rows}
        else:
            assert path == '/api/v1/inbox/box1/messages/m1'
            result = {'id': 'm1', 'text_body': body, 'html_body': None}
        if state.get('malformed'):
            result = {}
        return httpx.Response(200, json=result)

    monkeypatch.setattr('backend.web_clients.httpx.Client', lambda **kw: native(transport=httpx.MockTransport(handle), **kw))
    return calls, state


def configured(folder):
    client = ExpansionClient(folder)
    client.config['credentials'] = {'api_key': 'fixture-key'}
    return client


@pytest.mark.parametrize('provider', CONTINUATION_FIELDS)
def test_receive_reconnect_and_unobserved_id_boundary(provider, tmp_path, monkeypatch):
    calls, state = fixture(provider, monkeypatch)
    client = configured(tmp_path)
    client.create(provider)
    client = ExpansionClient(tmp_path)
    assert client.config['address'] == state['address']
    rows = client.list_messages()
    assert len(rows) == 1
    assert rows[0]['from'] and 'sender.example' in rows[0]['from']
    assert '840217' in str(client.get_message(rows[0]['id']))
    count = len(calls)
    with pytest.raises(ProviderError) as error:
        client.get_message('never-observed')
    assert error.value.code == 'message_not_in_mailbox' and len(calls) == count
    state['empty'] = True
    assert client.list_messages() == []
    with pytest.raises(ProviderError):
        client.get_message(rows[0]['id'])


@pytest.mark.parametrize('provider', CONTINUATION_FIELDS)
def test_missing_body_and_denial_are_not_empty_inboxes(provider, tmp_path, monkeypatch):
    _, state = fixture(provider, monkeypatch)
    client = configured(tmp_path)
    client.create(provider)
    mid = client.list_messages()[0]['id']
    state['missing_body'] = True
    with pytest.raises(ProviderError):
        client.get_message(mid)
    state['missing_body'], state['denied'] = False, True
    with pytest.raises(ProviderError) as error:
        client.list_messages()
    assert error.value.code == 'upstream_access_denied'


@pytest.mark.parametrize('provider', CONTINUATION_FIELDS)
def test_malformed_list_cannot_become_empty(provider, tmp_path, monkeypatch):
    _, state = fixture(provider, monkeypatch)
    client = configured(tmp_path)
    client.create(provider)
    state['malformed'] = True
    with pytest.raises(ProviderError):
        client.list_messages()


@pytest.mark.parametrize('provider', ['temp_mail24', 'noemail', 'nukemail', 'xelio', 'tmailor'])
def test_original_recipient_cannot_change(provider, tmp_path, monkeypatch):
    _, state = fixture(provider, monkeypatch)
    client = configured(tmp_path)
    client.create(provider)
    state['foreign'] = True
    with pytest.raises(ProviderError) as error:
        client.list_messages()
    assert error.value.code == 'mailbox_identity_changed'


@pytest.mark.parametrize('provider', ['temp_mail24', 'nukemail'])
def test_lost_session_fails_before_network(provider, tmp_path, monkeypatch):
    calls, _ = fixture(provider, monkeypatch)
    client = configured(tmp_path)
    client.create(provider)
    count = len(calls)
    client.cookies.clear()
    with pytest.raises(ProviderError) as error:
        client.list_messages()
    assert error.value.code == 'mailbox_session_lost' and len(calls) == count


@pytest.mark.parametrize('mode', ['loop', 'truncated'])
def test_tmailor_pagination_must_complete(mode, tmp_path, monkeypatch):
    calls, state = fixture('tmailor', monkeypatch)
    client = configured(tmp_path)
    client.create('tmailor')
    state[mode] = True
    start = len(calls)
    with pytest.raises(ProviderError) as error:
        client.list_messages()
    assert error.value.code == 'incomplete_mailbox_observation' and len(calls) - start <= 3


@pytest.mark.parametrize('bits,count', [(False, 1), (23, 1), (1, 17), (0, 1)])
def test_pow_rejects_unbounded_work_before_hashing(bits, count):
    with pytest.raises(ProviderError) as error:
        solve_pow({'bits': bits, 'solutions': count, 'challenge': 'fixture'})
    assert error.value.code == 'unsupported_proof_of_work'


def test_pow_deadline_is_enforced(monkeypatch):
    moments = iter([0, 11])
    monkeypatch.setattr('backend.continuation_clients.time.monotonic', lambda: next(moments))
    with pytest.raises(ProviderError) as error:
        solve_pow({'bits': 22, 'solutions': 16, 'challenge': 'fixture'})
    assert error.value.code == 'proof_of_work_budget_exceeded'


def test_nukemail_added_address_cannot_expand_receive_scope(tmp_path, monkeypatch):
    _, state = fixture('nukemail', monkeypatch)
    client = configured(tmp_path)
    client.create('nukemail')
    state['additional_address'] = True
    with pytest.raises(ProviderError) as error:
        client.list_messages()
    assert error.value.code == 'mailbox_identity_changed'
