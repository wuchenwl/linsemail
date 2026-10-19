"""Receive-only platform protocols; first-party contracts are saved in research.

Each session is pinned to its allocated inbox. Capped snapshots fail explicitly
at the cap; cursors and counts must prove that a paginated list is complete.
"""
from urllib.parse import quote

from mailbox_clients import ProviderError
from .web_clients import array, identity

PLATFORM_FIELDS = {p: ['api_key'] for p in
                   ['dustmail', 'tempmailer', 'tempmaillab', 'inboxical', 'evilmail', 'postmx',
                    'tempsmail', 'zemail', 'nulz', 'cybertemp', 'runmail', 'zumasia']}
PLATFORM_FIELDS.update(destroy_network=[], lobstermail=[])
BASES = {'dustmail': 'https://dustmail.net/api/v1',
         'tempmailer': 'https://tempmailer-backend.onrender.com/v1',
         'tempmaillab': 'https://api.tempmaillab.com/v1',
         'inboxical': 'https://api.inboxical.com/api',
         'evilmail': 'https://evilmail.pro/api', 'postmx': 'https://api.postmx.co/v1',
         'destroy_network': 'https://destroy.network/api',
         'lobstermail': 'https://api.lobstermail.ai/v1',
         'tempsmail': 'https://tempsmail.org/api', 'zemail': 'https://zemail.me/api',
         'nulz': 'https://v1.nulz.lol/v1', 'cybertemp': 'https://api.cybertemp.xyz',
         'runmail': 'https://run-mail.com/api/runmail/v1', 'zumasia': 'https://zumasia.com/api/v1'}


def helpers():
    from . import expansion_clients
    return expansion_clients


def auth(client):
    p = client.config['provider']
    if p == 'lobstermail':
        return {'Authorization': 'Bearer ' + helpers().field(client.config, 'token')}
    if not PLATFORM_FIELDS[p]:
        return {}
    key = client.config['credentials']['api_key']
    if p == 'runmail':
        return {'Authorization': 'Bearer ' + key, **({'X-Mailbox-Token': client.config['token']} if client.config.get('token') else {})}
    if p == 'zemail':
        return {'Authorization': 'Bearer ' + key, 'Zemail-Version': '2026-04-23'}
    if p == 'nulz':
        return {'Authorization': 'ApiKey ' + key}
    if p == 'cybertemp':
        return {'X-API-KEY': key, 'Authorization': 'Bearer ' + key}
    if p == 'tempsmail':
        return {'x-secret-token': client.config['token']} if client.config.get('token') else {}
    return {'X-API-Key': key} if p in {'tempmailer', 'inboxical', 'evilmail'} else {'Authorization': 'Bearer ' + key}


def call(client, method, path, **kw):
    return client.api(method, BASES[client.config['provider']] + path, headers=auth(client), **kw)


def unwrap(value):
    h = helpers()
    return h.obj(h.obj(value).get('data'))


def acknowledged(value):
    h = helpers()
    if h.obj(value).get('success') is not True:
        raise ProviderError('upstream_api_error', 'Upstream acknowledgement missing')
    return value


def create_mailbox(client, provider, prefix):
    h = helpers()
    if provider == 'zumasia':
        # Documented catch-all local parts; no invented create endpoint.
        client.config['address'] = h.email(prefix + '@zumasia.com')
        return
    if provider == 'cybertemp':
        values = h.obj(call(client, 'GET', '/getDomains')).get('domains')
        if not isinstance(values, list) or not values:
            raise ProviderError('no_public_domain', 'No explicit receiving domain list')
        client.config['address'] = h.email(prefix + '@' + h.domain(values[0]))
        return
    if provider == 'tempsmail':
        value = h.obj(call(client, 'POST', '/extension/emails/' + quote(client.config['credentials']['api_key'], safe='')))
        if value.get('status') is not True:
            raise ProviderError('upstream_api_error', 'Mailbox creation was not acknowledged')
        box = h.obj(value.get('data'))
        client.config.update(address=h.email(h.field(box, 'email')), token=h.field(box, 'secure_key'))
        return
    if provider == 'runmail':
        box = unwrap(acknowledged(call(client, 'POST', '/mailboxes', payload={'type': 'temp', 'ttl': 3600})))
        client.config.update(address=h.email(h.field(box, 'email')), inbox_id=h.field(box, 'uuid'), token=h.field(box, 'access_token'))
        return
    if provider == 'zemail':
        box = unwrap(call(client, 'POST', '/mailboxes', payload={'type': 'random'}))
    elif provider == 'nulz':
        box = h.obj(call(client, 'POST', '/aliases', payload={}))
    elif provider == 'lobstermail':
        # Persist the normal signup token before allocating; no external identity.
        if not client.config.get('token'):
            signup = h.obj(client.api('POST', BASES[provider] + '/signup'))
            client.config['token'] = h.field(signup, 'token')
            client.save()
        box = h.obj(call(client, 'POST', '/inboxes', payload={}))
    elif provider == 'dustmail':
        box = unwrap(acknowledged(call(client, 'POST', '/inbox', payload={})))
    elif provider == 'tempmailer':
        box = h.obj(h.obj(call(client, 'POST', '/inboxes', payload={})).get('inbox'))
    elif provider == 'tempmaillab':
        box = unwrap(client.api('POST', BASES[provider] + '/emails', payload={},
                               headers={**auth(client), 'Idempotency-Key': prefix}))
    elif provider == 'inboxical':
        box = h.obj(call(client, 'POST', '/inboxes', payload={'name': prefix}))
    elif provider == 'evilmail':
        box = unwrap(call(client, 'POST', '/ext/temp-email', payload={}))
        client.config.update(address=h.email(h.field(box, 'email')), token=h.field(box, 'sessionToken'))
        return
    elif provider == 'postmx':
        box = h.obj(acknowledged(client.api('POST', BASES[provider] + '/inboxes',
                     headers={**auth(client), 'Idempotency-Key': prefix}, payload={
                         'label': prefix, 'lifecycle_mode': 'temporary', 'ttl_minutes': 60,
                         'message_analysis': {'mode': 'disabled', 'recipients': []}})).get('inbox'))
    else:
        box = h.obj(call(client, 'POST', '/inbox'))
    address_key = 'email' if provider == 'dustmail' else 'email_address' if provider in {'inboxical', 'postmx'} else 'address'
    client.config.update(address=h.email(h.field(box, address_key)), inbox_id=identity(box.get('id')))


def inbox_path(client):
    p = client.config['provider']
    kind = 'inbox' if p in {'dustmail', 'destroy_network'} else 'emails' if p == 'tempmaillab' else 'mailboxes' if p in {'zemail', 'runmail'} else 'aliases' if p == 'nulz' else 'inboxes'
    return '/' + kind + '/' + quote(client.config['inbox_id'], safe='')


def check_box(client, box, address_key='address'):
    h = helpers()
    box = h.obj(box)
    h.same(identity(box.get('id')), client.config['inbox_id'])
    h.same(h.field(box, address_key), client.config['address'])
    if box.get('status') == 'expired' or box.get('isActive') is False or box.get('active') is False:
        raise ProviderError('mailbox_expired', 'Original inbox is no longer active')


def normalize_row(client, row):
    h = helpers()
    row = h.obj(row)
    p, address = client.config['provider'], client.config['address']
    if p in {'destroy_network', 'lobstermail', 'tempmaillab', 'inboxical', 'postmx'}:
        parent = {'destroy_network': 'inboxId', 'lobstermail': 'inboxId', 'tempmaillab': 'email_id',
                  'inboxical': 'inbox_id', 'postmx': 'inbox_id'}[p]
        h.same(h.field(row, parent), client.config['inbox_id'])
    if p == 'tempmailer':
        # The public schema only says string; bind its observed representation
        # to one of the two allocation identities, never infer arbitrary values.
        parent = h.field(row, 'inbox')
        if parent.lower() not in {address.lower(), client.config['inbox_id'].lower()}:
            raise ProviderError('mailbox_identity_changed', 'Message left the original inbox')
    if p == 'postmx':
        h.same(h.field(row, 'to_email'), address)
        h.same(h.field(row, 'inbox_email_address'), address)
    if p == 'nulz':
        h.same(h.field(row, 'to_addr'), address)
        if 'alias_id' in row:
            h.same(h.field(row, 'alias_id'), client.config['inbox_id'])
    if p == 'runmail':
        h.same(h.field(row, 'to'), address)
    if p == 'cybertemp' and 'to' in row:
        h.same(h.field(row, 'to'), address)
    if p in {'tempmaillab', 'lobstermail', 'inboxical'}:
        recipients = row.get('to_addrs' if p == 'inboxical' else 'to')
        if not isinstance(recipients, list) or any(not isinstance(v, str) for v in recipients):
            raise ProviderError('unexpected_response', 'Expected a recipient string array')
        if address.lower() not in [v.lower() for v in recipients]:
            raise ProviderError('mailbox_identity_changed', 'Message recipient left the original inbox')
    sender = {'destroy_network': 'fromAddress', 'inboxical': 'from_addr', 'nulz': 'from_addr', 'zumasia': 'fromAddr',
              'tempsmail': 'from_email', 'zemail': 'sender_email', 'postmx': 'from_email'}.get(p, 'from')
    text = 'bodyText' if p in {'destroy_network', 'tempmailer'} else 'body_text' if p in {'inboxical', 'zemail', 'nulz'} else 'text_body' if p == 'postmx' else 'body' if p == 'lobstermail' else 'text'
    html = 'bodyHtml' if p in {'destroy_network', 'tempmailer'} else 'body_html' if p in {'inboxical', 'zemail', 'nulz'} else 'html_body' if p == 'postmx' else 'content' if p == 'tempsmail' else 'html'
    if p == 'cybertemp':
        text = 'body_text' if 'body_text' in row else 'text'
        html = 'body_html' if 'body_html' in row else 'html'
    if p == 'zumasia':
        text, html = 'textBody', 'htmlBody'
    date = row.get('received_at', row.get('receivedAt', row.get('created_at', row.get('date'))))
    if p == 'zumasia':
        if type(date) not in {int, float}:
            raise ProviderError('unexpected_response', 'Expected Unix epoch milliseconds')
        date /= 1000
    return {**row, 'id': identity(row.get('uid') if p == 'evilmail' else row.get('id')),
            'from': row.get(sender), 'text': row.get(text), 'html': row.get(html),
            'date': date}


def incomplete():
    raise ProviderError('incomplete_mailbox_observation', 'Snapshot cap, inconsistent count or invalid continuation metadata')


def list_messages(client):
    h = helpers()
    p, address = client.config['provider'], client.config['address']
    if p == 'zumasia':
        items, seen, skip = [], set(), 0
        for _ in range(20):
            response = h.obj(call(client, 'GET', '/inboxes/' + quote(address, safe='') + '/messages', params={'limit': 50, 'skip': skip}))
            h.same(h.field(response, 'address'), address)
            limit = response.get('limit')
            if type(limit) is not int or not 1 <= limit <= 50 or type(response.get('skip')) is not int or response['skip'] != skip:
                incomplete()
            rows = array(response.get('messages'))
            if len(rows) > limit:
                incomplete()
            for row in rows:
                item = normalize_row(client, row)
                if item['id'] in seen:
                    incomplete()
                seen.add(item['id'])
                items.append(item)
            if len(rows) < limit:
                return items
            skip += len(rows)
        incomplete()
    if p == 'tempsmail':
        response = h.obj(call(client, 'GET', '/extension/messages/' + quote(client.config['credentials']['api_key'], safe='')
                             + '/' + quote(address, safe='')))
        if response.get('status') is not True:
            raise ProviderError('upstream_api_error', 'Mailbox observation was not acknowledged')
        h.same(h.field(response, 'mailbox'), address)
        return [normalize_row(client, row) for row in array(response.get('messages'))]
    if p == 'cybertemp':
        items, seen = [], set()
        for page in range(20):
            response = call(client, 'GET', '/getMail', params={'email': address, 'limit': 50, 'offset': page * 50})
            rows = array(response if isinstance(response, list) else h.obj(response).get(
                'messages' if 'messages' in h.obj(response) else 'emails'))
            if len(rows) > 50:
                incomplete()
            for row in rows:
                item = normalize_row(client, row)
                if item['id'] in seen:
                    incomplete()
                seen.add(item['id'])
                items.append(item)
            if len(rows) < 50:
                return items
        incomplete()
    if p == 'evilmail':
        token = h.field(client.config, 'token')
        current = unwrap(call(client, 'GET', '/ext/temp-email', params={'token': token}))
        h.same(h.field(current, 'email'), address)
        response = unwrap(call(client, 'GET', '/temp-email/' + quote(token, safe='')))
        return [normalize_row(client, r) for r in array(response.get('messages'))]
    path = inbox_path(client)
    if p == 'dustmail':
        response = acknowledged(call(client, 'GET', path))
        value = unwrap(response)
        check_box(client, value.get('inbox'), 'email')
        rows = array(value.get('emails'))
        count = h.obj(response.get('meta')).get('email_count')
        if type(count) is not int or count != len(rows) or len(rows) >= 100:
            incomplete()
        return [normalize_row(client, r) for r in rows]
    if p != 'postmx':
        box = call(client, 'GET', path)
        if p == 'tempmailer':
            box = h.obj(box).get('inbox')
        elif p in {'tempmaillab', 'zemail'}:
            box = unwrap(box)
        if p == 'runmail':
            box = unwrap(acknowledged(box))
            h.same(h.field(box, 'uuid'), client.config['inbox_id'])
            h.same(h.field(box, 'email'), address)
        else:
            check_box(client, box, 'email_address' if p == 'inboxical' else 'address')
    suffix = '/emails' if p in {'lobstermail', 'zemail'} else '/messages'
    if p in {'destroy_network', 'tempmailer'}:
        response = call(client, 'GET', path + suffix, **({'params': {'limit': 100}} if p == 'tempmailer' else {}))
        rows = array(h.obj(response).get('messages')) if p == 'tempmailer' else array(response)
        if p == 'tempmailer' and len(rows) >= 100:
            incomplete()
        return [normalize_row(client, r) for r in rows]
    items, seen_cursors, seen_ids, cursor, total = [], set(), set(), None, None
    for page in range(1, 21):
        params = {'limit': 100}
        if p in {'inboxical', 'zemail'}:
            params['page'] = page
        elif p in {'nulz', 'runmail'}:
            params.update(limit=50, offset=len(items))
        elif cursor is not None:
            params['cursor'] = cursor
        if p == 'lobstermail':
            params['direction'] = 'inbound'
        response = h.obj(call(client, 'GET', path + suffix, params=params))
        if p == 'runmail':
            response = unwrap(acknowledged(response))
        if p == 'postmx':
            acknowledged(response)
        key = 'data' if p in {'tempmaillab', 'lobstermail', 'zemail'} else 'messages'
        rows = array(response.get(key))
        for row in rows:
            item = normalize_row(client, row)
            if item['id'] in seen_ids:
                incomplete()
            seen_ids.add(item['id'])
            items.append(item)
        if p in {'nulz', 'runmail'}:
            count = response.get('total')
            if type(count) is not int or count < 0 or (total is not None and total != count):
                incomplete()
            total = count
            if len(items) == total:
                return items
            if not rows or len(rows) > 50 or len(items) > total:
                incomplete()
            continue
        if p == 'inboxical':
            count, limit = response.get('total'), response.get('limit')
            if (type(count) is not int or count < 0 or (total is not None and count != total)
                    or type(limit) is not int or not 1 <= limit <= 100 or response.get('page') != page):
                incomplete()
            total = count
            if len(items) == total:
                return items
            if not rows or len(items) > total or len(rows) != limit:
                incomplete()
            continue
        if p == 'zemail':
            more, next_cursor = response.get('has_more'), response.get('next_cursor')
            if type(more) is not bool or (more and not rows):
                incomplete()
            if not more:
                return items
            continue  # Official SDK paginates by `page`, not by cursor.
        if p == 'lobstermail':
            next_cursor = response.get('cursor')
            more = next_cursor is not None
        else:
            pagination = h.obj(response.get('pagination' if p == 'tempmaillab' else 'page_info'))
            more, next_cursor = pagination.get('has_more'), pagination.get('next_cursor')
            if type(more) is not bool or 'next_cursor' not in pagination:
                incomplete()
        if not more:
            if next_cursor is not None:
                incomplete()
            return items
        if not rows or not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
            incomplete()
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    incomplete()


def read_message(client, item):
    h = helpers()
    p, mid = client.config['provider'], item['id']
    current = next((r for r in client.list_messages() if r['id'] == mid), None)
    if current is None:
        raise ProviderError('message_not_in_mailbox', 'Message is no longer in the original inbox')
    if p in {'dustmail', 'tempsmail', 'cybertemp'}:
        return h.body_present(current)
    if p == 'evilmail':
        value = unwrap(call(client, 'GET', '/ext/message/' + quote(mid, safe=''), params={'token': client.config['token']}))
    else:
        scoped = p in {'destroy_network', 'lobstermail', 'zemail', 'runmail'}
        path = inbox_path(client) + ('/emails/' if p in {'lobstermail', 'zemail'} else '/messages/') if scoped else '/inboxes/messages/' if p == 'inboxical' else '/messages/'
        value = call(client, 'GET', path + quote(mid, safe=''))
        if p == 'runmail':
            value = unwrap(acknowledged(value))
        elif p in {'tempmaillab', 'zemail'}:
            value = unwrap(value)
        elif p == 'tempmailer':
            value = h.obj(value).get('message')
        elif p == 'postmx':
            value = acknowledged(value).get('message')
    result = normalize_row(client, value)
    h.same(result['id'], mid)
    return h.body_present(result)
