"""Owned fixtures exercise saved first-party contracts, not live availability."""
import json

import httpx
import pytest

from backend.expansion_clients import ExpansionClient
from backend.platform_clients import PLATFORM_FIELDS
from mailbox_clients import ProviderError


def fixture(provider, monkeypatch):
    if provider in {'tempsmail', 'zemail', 'nulz', 'cybertemp', 'runmail', 'zumasia'}:
        return extra_fixture(provider, monkeypatch)
    state = {'address': 'owned@receiver.example'}
    calls = []

    def handle(req):
        calls.append((req.method, req.url.path))
        p, path = provider, req.url.path
        if state.get('denied'):
            return httpx.Response(401, json={'error': 'denied'})
        if PLATFORM_FIELDS[p]:
            assert req.headers.get('X-API-Key' if p in {'tempmailer', 'inboxical', 'evilmail'} else 'Authorization') == (
                'fixture-key' if p in {'tempmailer', 'inboxical', 'evilmail'} else 'Bearer fixture-key')
        if path.endswith('/signup'):
            assert req.method == 'POST' and 'Authorization' not in req.headers
            return httpx.Response(201, json={'id': 'account1', 'token': 'owned-token'})
        if p == 'lobstermail':
            assert req.headers['Authorization'] == 'Bearer owned-token'
        address = 'foreign@receiver.example' if state.get('foreign') else state['address']
        box = {'id': 'box1', 'address': address, 'email': address, 'email_address': address,
               'status': 'active', 'isActive': True}
        row = {'id': 'm1', 'uid': 1, 'inboxId': 'box1', 'email_id': 'box1', 'inbox_id': 'box1',
               'inbox': address, 'to': [address], 'to_addrs': [address], 'to_email': address,
               'inbox_email_address': address, 'subject': 'Owned fixture', 'from': 'sender@test.example',
               'fromAddress': 'sender@test.example', 'from_addr': 'sender@test.example',
               'from_email': 'sender@test.example'}
        body = None if state.get('missing_body') else 'Your code: 730219'
        detail = {**row, **{k: body for k in ['text', 'bodyText', 'body_text', 'text_body', 'body']}}
        if state.get('wrong_detail'):
            detail.update(id='foreign', uid=999, inboxId='foreign', email_id='foreign', inbox_id='foreign', inbox='foreign')
        rows = [] if state.get('empty') else [detail if p in {'dustmail', 'destroy_network', 'inboxical', 'lobstermail'} else row]
        if state.get('cap'):
            rows = [{**detail, 'id': str(i), 'uid': i + 1} for i in range(100)]
        if req.method == 'POST':
            payload = json.loads(req.content) if req.content else None
            if p in {'dustmail', 'tempmailer', 'tempmaillab', 'evilmail', 'lobstermail'}:
                assert payload == {}
            if p == 'postmx':
                assert payload['lifecycle_mode'] == 'temporary' and payload['message_analysis']['mode'] == 'disabled'
            if p == 'evilmail':
                value = {'email': address, 'sessionToken': 'owned-session'}
            else:
                value = box
            result = {'success': True, 'data': value} if p in {'dustmail', 'evilmail', 'tempmaillab'} else {'inbox': value} if p == 'tempmailer' else {'success': True, 'inbox': value} if p == 'postmx' else value
        elif p == 'evilmail':
            if path.endswith('/ext/temp-email'):
                assert req.url.params['token'] == 'owned-session'
                value = {'email': address}
            elif '/ext/message/' in path:
                assert req.url.params['token'] == 'owned-session'
                value = detail
            else:
                assert path.endswith('/temp-email/owned-session')
                value = {'messages': rows}
            result = {'status': 'success', 'data': value}
        elif path.endswith('/box1'):
            result = {'success': True, 'data': {'inbox': box, 'emails': rows}, 'meta': {'email_count': len(rows)}} if p == 'dustmail' else {'data': box} if p == 'tempmaillab' else {'inbox': box} if p == 'tempmailer' else box
        elif path.endswith(('/messages', '/emails')):
            if p == 'destroy_network':
                result = rows
            elif p == 'tempmailer':
                assert req.url.params['limit'] == '100'
                result = {'messages': rows}
            elif p == 'inboxical':
                result = {'messages': rows, 'total': len(rows), 'page': int(req.url.params['page']), 'limit': 100}
                if state.get('truncated'):
                    result['total'] += 1
                if state.get('two_pages'):
                    page = int(req.url.params['page'])
                    result.update(messages=[{**row, 'id': 'm'+str(page)}], total=2, limit=1)
            elif p == 'lobstermail':
                result = {'data': rows}
                if state.get('loop'):
                    result['cursor'] = 'repeat'
                if state.get('two_pages'):
                    second = bool(req.url.params.get('cursor'))
                    result = {'data': [{**detail, 'id': 'm2' if second else 'm1'}], **({} if second else {'cursor': 'next'})}
            else:
                page = {'has_more': False, 'next_cursor': None}
                if state.get('loop'):
                    page.update(has_more=True, next_cursor='repeat')
                if state.get('truncated'):
                    page = {'has_more': True}
                if state.get('two_pages'):
                    second = bool(req.url.params.get('cursor'))
                    rows = [{**row, 'id': 'm2' if second else 'm1'}]
                    page = {'has_more': not second, 'next_cursor': None if second else 'next'}
                result = {'data' if p == 'tempmaillab' else 'messages': rows,
                          'pagination' if p == 'tempmaillab' else 'page_info': page, 'success': True}
        else:
            result = {'data': detail} if p == 'tempmaillab' else {'message': detail, 'success': True} if p in {'tempmailer', 'postmx'} else detail
        if state.get('malformed') and req.method == 'GET':
            result = {}
        return httpx.Response(200, json=result)

    native = httpx.Client
    monkeypatch.setattr('backend.web_clients.httpx.Client', lambda **kw: native(transport=httpx.MockTransport(handle), **kw))
    return calls, state


def configured(path):
    c = ExpansionClient(path)
    c.config['credentials'] = {'api_key': 'fixture-key'}
    return c


@pytest.mark.parametrize('provider', PLATFORM_FIELDS)
def test_create_reopen_read_and_unknown_id_boundary(provider, tmp_path, monkeypatch):
    calls, state = fixture(provider, monkeypatch)
    c = configured(tmp_path)
    c.create(provider)
    c = ExpansionClient(tmp_path)
    rows = c.list_messages()
    assert len(rows) == 1 and rows[0]['from']
    assert '730219' in str(c.get_message(rows[0]['id']))
    count = len(calls)
    with pytest.raises(ProviderError):
        c.get_message('never-observed')
    assert len(calls) == count
    state['empty'] = True
    with pytest.raises(ProviderError):
        c.get_message(rows[0]['id'])
    assert c.list_messages() == []
    assert sum(method == 'POST' for method, _ in calls) == (0 if provider in {'cybertemp', 'zumasia'} else 2 if provider == 'lobstermail' else 1)


@pytest.mark.parametrize('provider', PLATFORM_FIELDS)
def test_identity_denial_malformed_and_missing_body(provider, tmp_path, monkeypatch):
    _, state = fixture(provider, monkeypatch)
    c = configured(tmp_path)
    c.create(provider)
    mid = c.list_messages()[0]['id']
    state['missing_body'] = True
    with pytest.raises(ProviderError):
        c.get_message(mid)
    state['missing_body'] = False
    state['foreign'] = True
    with pytest.raises(ProviderError):
        c.list_messages()
    state['foreign'] = False
    for mode in ['denied', 'malformed']:
        state[mode] = True
        with pytest.raises(ProviderError):
            c.list_messages()
        state[mode] = False


@pytest.mark.parametrize('provider', [p for p in PLATFORM_FIELDS if p not in {'dustmail', 'tempsmail', 'cybertemp'}])
def test_detail_identity_is_checked(provider, tmp_path, monkeypatch):
    _, state = fixture(provider, monkeypatch)
    c = configured(tmp_path)
    c.create(provider)
    mid = c.list_messages()[0]['id']
    state['wrong_detail'] = True
    with pytest.raises(ProviderError):
        c.get_message(mid)


@pytest.mark.parametrize('provider', ['dustmail', 'tempmailer'])
def test_capped_snapshot_is_not_complete(provider, tmp_path, monkeypatch):
    _, state = fixture(provider, monkeypatch)
    c = configured(tmp_path)
    c.create(provider)
    state['cap'] = True
    with pytest.raises(ProviderError) as error:
        c.list_messages()
    assert error.value.code == 'incomplete_mailbox_observation'


@pytest.mark.parametrize('provider', ['tempmaillab', 'postmx', 'inboxical', 'lobstermail'])
def test_complete_second_page_and_broken_continuation(provider, tmp_path, monkeypatch):
    calls, state = fixture(provider, monkeypatch)
    c = configured(tmp_path)
    c.create(provider)
    state['two_pages'] = True
    assert [r['id'] for r in c.list_messages()] == ['m1', 'm2']
    state['two_pages'] = False
    state['truncated' if provider == 'inboxical' else 'loop'] = True
    start = len(calls)
    with pytest.raises(ProviderError) as error:
        c.list_messages()
    assert error.value.code == 'incomplete_mailbox_observation' and len(calls) - start <= 3


def extra_fixture(p, monkeypatch):
    state, calls = {'address': 'owned@receiver.example'}, []

    def handle(req):
        calls.append((req.method, req.url.path))
        if state.get('denied'):
            return httpx.Response(401, json={'error': 'denied'})
        path = req.url.path
        if p != 'tempsmail':
            assert req.headers['Authorization'] == ('ApiKey ' if p == 'nulz' else 'Bearer ') + 'fixture-key'
        if p == 'cybertemp':
            assert req.headers['X-API-KEY'] == 'fixture-key'
        if p == 'zemail':
            assert req.headers['Zemail-Version'] == '2026-04-23'
        if p == 'zumasia' and '/inboxes/' in path:
            state['address'] = path.split('/inboxes/')[1].split('/')[0]
        if p == 'cybertemp' and path.endswith('/getMail'):
            state['address'] = req.url.params['email']
        address = 'foreign@receiver.example' if state.get('foreign') else state['address']
        box = {'id': 123, 'address': address, 'active': True}
        text = None if state.get('missing_body') else 'Your code: 730219'
        row = {'id': 'm1', 'from': 'sender@test.example', 'sender_email': 'sender@test.example',
               'from_addr': 'sender@test.example', 'from_email': 'sender@test.example', 'fromAddr': 'sender@test.example',
               'to_addr': address, 'alias_id': '123', 'to': address,
               'receivedAt': 1792400400000, 'subject': 'Owned fixture'}
        detail = {**row, **{k: text for k in ['body_text', 'text', 'content', 'textBody']}}
        rows = [] if state.get('empty') else [detail if p in {'tempsmail', 'cybertemp'} else row]
        if req.method == 'POST':
            if p == 'tempsmail':
                assert path == '/api/extension/emails/fixture-key'
                result = {'status': True, 'data': {'email': address, 'secure_key': 'owned-session'}}
            elif p == 'zemail':
                assert path == '/api/mailboxes' and json.loads(req.content) == {'type': 'random'}
                result = {'data': box}
            elif p == 'nulz':
                assert path == '/v1/aliases' and json.loads(req.content) == {}
                result = box
            else:
                assert p == 'runmail' and json.loads(req.content) == {'type': 'temp', 'ttl': 3600}
                result = {'success': True, 'data': {'uuid': '123', 'email': address, 'access_token': 'owned-session'}}
        elif path.endswith('/getDomains'):
            result = {'domains': ['receiver.example']}
        elif p == 'tempsmail':
            assert req.headers['x-secret-token'] == 'owned-session'
            assert path == '/api/extension/messages/fixture-key/' + state['address']
            result = {'status': True, 'mailbox': address, 'messages': rows}
        elif path.endswith('/123'):
            result = {'data': box} if p == 'zemail' else {'success': True, 'data': {'uuid': '123', 'email': address}} if p == 'runmail' else box
        elif path.endswith(('/messages', '/emails', '/getMail')):
            if p == 'runmail':
                assert req.headers['X-Mailbox-Token'] == 'owned-session'
            if p == 'zemail':
                page = int(req.url.params['page'])
                result = {'data': rows, 'has_more': False, 'next_cursor': None}
                if state.get('two_pages'):
                    result.update(data=[{**row, 'id': 'm'+str(page)}], has_more=page == 1)
                if state.get('broken'):
                    result['has_more'] = True
            elif p == 'zumasia':
                skip = int(req.url.params['skip'])
                result = {'address': address, 'messages': rows, 'limit': 50, 'skip': skip}
                if state.get('two_pages'):
                    result.update(messages=[{**row, 'id': 'm'+str(skip+1)}] if skip < 2 else [], limit=1)
                if state.get('broken'):
                    result['skip'] = -1
            elif p == 'cybertemp':
                offset = int(req.url.params['offset'])
                result = {'emails': rows}
                if state.get('two_pages'):
                    result = {'emails': [{**detail, 'id': 'm'+str(i)} for i in range(50)] if offset == 0 else [{**detail, 'id': 'm50'}]}
                if state.get('broken'):
                    result = {'emails': [detail]*50}
            else:
                offset = int(req.url.params['offset'])
                result = {'messages': rows, 'total': len(rows)}
                if state.get('two_pages'):
                    result = {'messages': [{**row, 'id': 'm'+str(offset+1)}], 'total': 2}
                if state.get('broken'):
                    result['total'] += 1
                if p == 'runmail':
                    result = {'success': True, 'data': result}
        else:
            if state.get('wrong_detail'):
                detail['id'] = 'foreign'
            result = {'data': detail} if p == 'zemail' else {'success': True, 'data': detail} if p == 'runmail' else detail
        if state.get('malformed'):
            result = {}
        return httpx.Response(200, json=result)

    native = httpx.Client
    monkeypatch.setattr('backend.web_clients.httpx.Client', lambda **kw: native(transport=httpx.MockTransport(handle), **kw))
    return calls, state


@pytest.mark.parametrize('provider', ['zemail', 'nulz', 'cybertemp', 'runmail', 'zumasia'])
def test_extra_pagination_complete_and_fail_closed(provider, tmp_path, monkeypatch):
    _, state = fixture(provider, monkeypatch)
    c = configured(tmp_path)
    c.create(provider)
    state['two_pages'] = True
    rows = c.list_messages()
    assert len(rows) == (51 if provider == 'cybertemp' else 2)
    state['two_pages'], state['broken'] = False, True
    with pytest.raises(ProviderError) as error:
        c.list_messages()
    assert error.value.code == 'incomplete_mailbox_observation'
