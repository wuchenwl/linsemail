"""Dedicated receive protocols verified from first-party sources and owned fixtures."""
import hashlib
import json
import re
import time
from urllib.parse import parse_qs, quote, urlsplit

from mailbox_clients import ProviderError
from .web_clients import Element, Page, array, identity, one

CONTINUATION_FIELDS = {'temp_mail24': [], 'noemail': [], 'nukemail': [], 'xelio': [],
                       'tmailor': ['api_key'], 'tempmailgrab': ['api_key']}
BASES = {'temp_mail24': 'https://temp-mail24.com', 'noemail': 'https://noemail.cc',
         'nukemail': 'https://nukemail.app', 'xelio': 'https://api.xelio.me',
         'tmailor': 'https://open-api.tmailor.com', 'tempmailgrab': 'https://tempmailgrab.com/api/v1'}


def pin_session(client):
    cookies = {'|'.join((c.domain, c.path, c.name)): hashlib.sha256(c.value.encode()).hexdigest()
               for c in client.cookies if not c.is_expired()}
    if not cookies:
        raise ProviderError('session_initialization_failed', 'Original mailbox cookie session missing')
    client.config['session_cookies'] = cookies


def data(value):
    from .expansion_clients import obj
    value = obj(value)
    if value.get('success') is not True:
        raise ProviderError('upstream_api_error', 'Upstream did not acknowledge the operation')
    return value.get('data')


def wp(client, action, **fields):
    source = client.request('POST', BASES['temp_mail24'] + '/wp-admin/admin-ajax.php',
                            form={'action': 'pikulinpw_temp_mail_' + action, **fields})
    try:
        return data(json.loads(source))
    except (ValueError, TypeError):
        raise ProviderError('unexpected_response', 'Expected acknowledged mailbox JSON') from None


def solve_pow(challenge):
    from .expansion_clients import field
    bits, count = challenge.get('bits'), challenge.get('solutions')
    if type(bits) is not int or not 1 <= bits <= 22 or type(count) is not int or not 1 <= count <= 16:
        raise ProviderError('unsupported_proof_of_work', 'Advertised work exceeds the receive budget')
    seed, found, deadline = field(challenge, 'challenge'), [], time.monotonic() + 10
    for nonce in range(2_000_000):
        digest = hashlib.sha256((seed + str(nonce)).encode()).digest()
        if int.from_bytes(digest[:4], 'big') >> (32 - bits) == 0:
            found.append(str(nonce))
            if len(found) == count:
                return ','.join(found)
        if nonce % 4096 == 0 and time.monotonic() >= deadline:
            break
    raise ProviderError('proof_of_work_budget_exceeded', 'Advertised work did not complete within the receive budget')


def create_mailbox(client, provider, prefix):
    from .expansion_clients import domain, email, field, obj, same
    base = BASES[provider]
    if provider == 'temp_mail24':
        allocated = obj(data(client.api('POST', base + '/wp-content/plugins/pikulinpw-temp-mail/bootstrap.php')))
        client.config['address'] = email(field(allocated, 'email'))
        pin_session(client)
        client.config['nonce'] = field(obj(wp(client, 'nonce')), 'nonce')
    elif provider == 'noemail':
        root = Page(client.request('GET', base + '/')).root
        choices = one(root.find('select', identity='domain'), 'Receiving domains').find('option')
        domains = [domain(node.attrs['value']) for node in choices if node.attrs.get('value')]
        if not domains:
            raise ProviderError('no_public_domain', 'No public receiving domain was supplied')
        client.config['address'] = email(prefix + '@' + domains[0])
    elif provider == 'nukemail':
        domains = array(obj(client.api('GET', base + '/api/domains')).get('domains'))
        eligible = [row for row in domains if row.get('is_premium_only') is False]
        if not eligible:
            raise ProviderError('no_public_domain', 'No public receiving domain was supplied')
        selected = domain(field(eligible[0], 'domain'))
        challenge = obj(client.api('GET', base + '/api/pow/challenge'))
        allocated = obj(client.api('POST', base + '/api/inbox/create', payload={
            'address': prefix, 'domain': selected, 'pow_id': field(challenge, 'id'), 'pow_nonce': solve_pow(challenge)}))
        address = email(field(allocated, 'email'))
        same(address, prefix + '@' + selected)
        entry = obj(allocated.get('address'))
        same(field(entry, 'address') + '@' + field(entry, 'domain'), address)
        same(field(entry, 'token'), field(allocated, 'token'))
        client.config.update(address=address, token=field(allocated, 'token'), inbox_id=identity(entry.get('id')))
        pin_session(client)
    elif provider == 'xelio':
        allocated = obj(data(client.api('POST', base + '/api/emails', payload={'username': prefix})))
        address = email(field(allocated, 'address'))
        same(address.split('@')[0], prefix)
        client.config.update(address=address, token=field(allocated, 'access_token'))
    elif provider == 'tmailor':
        allocated = obj(client.api('POST', base + '/v1/mailboxes', payload={},
                                  headers={**auth(client), 'Idempotency-Key': prefix}))
        client.config.update(address=email(field(allocated, 'email')), token=field(allocated, 'token'))
    elif provider == 'tempmailgrab':
        allocated = obj(client.api('POST', base + '/inbox', payload={'prefix': prefix}, headers=auth(client)))
        client.config.update(address=email(field(allocated, 'address')), inbox_id=field(allocated, 'id'),
                             expires_at=allocated.get('expires_at'))


def auth(client):
    provider = client.config['provider']
    if provider == 'xelio':
        from .expansion_clients import field
        return {'X-Mailbox-Token': field(client.config, 'token')}
    if provider in {'tmailor', 'tempmailgrab'}:
        return {'Authorization': 'Bearer ' + client.config['credentials']['api_key']}
    return {}


def lines(node):
    if not isinstance(node, Element):
        return node
    if node.tag == 'br':
        return '\n'
    return ''.join(lines(child) for child in node.children)


def noemail_rows(source, address):
    from .expansion_clients import same
    root = Page(source).root
    same(one([n for n in root.find('input') if n.attrs.get('type') == 'email'], 'Mailbox identity').attrs.get('value'), address)
    items = []
    for card in root.find(cls='card'):
        headers = card.find(cls='card-header')
        if not headers:
            continue
        if headers[0].text().strip().startswith('Subject: Ad'):
            continue
        first = next((n for n in headers[0].children if isinstance(n, Element) and n.tag == 'div'), None)
        if first is None:
            raise ProviderError('unexpected_response', 'Mailbox card header missing')
        header = lines(first)
        if not header.strip().startswith('To:'):
            if header.strip().startswith('Subject: Ad'):
                continue
            raise ProviderError('unexpected_response', 'Unrecognized mailbox card')
        values = {}
        for label in ['To', 'From', 'Subject', 'Received']:
            match = re.search(r'(?:^|\n)\s*' + label + r':\s*([^\n]*)', header)
            if not match:
                raise ProviderError('unexpected_response', 'Mailbox message header incomplete')
            values[label] = match[1].strip()
        same(values['To'], address)
        ids = [re.search(r'Report abuse \(ID ([0-9]+)\)', parse_qs(urlsplit(n.attrs.get('href', '')).query).get('subject', [''])[0])
               for n in card.find('a') if n.attrs.get('href', '').startswith('mailto:')]
        ids = [m[1] for m in ids if m]
        mid = identity(one(ids, 'Mailbox message ID'))
        body = one(card.find(cls='card-text'), 'Message body')
        subject = one(card.find(cls='card-title'), 'Message subject').text().strip()
        same(subject, values['Subject'])
        items.append({'id': mid, 'from': values['From'], 'subject': subject,
                      'date': values['Received'], 'text': body.text().strip(), 'html': body.html()})
    if not items and 'No emails received yet.' not in root.text():
        raise ProviderError('unexpected_response', 'Explicit empty inbox or message cards missing')
    return items


def list_messages(client):
    from .expansion_clients import field, obj, raw_message, same
    provider, address = client.config['provider'], client.config['address']
    base = BASES[provider]
    if provider == 'noemail':
        return noemail_rows(client.request('GET', base + '/' + quote(address, safe='')), address)
    if provider == 'temp_mail24':
        current = obj(data(client.api('POST', base + '/wp-content/plugins/pikulinpw-temp-mail/bootstrap.php')))
        same(field(current, 'email'), address)
        response = obj(wp(client, 'inbox', nonce=field(client.config, 'nonce')))
        markup = response.get('html')
        if not isinstance(markup, str):
            raise ProviderError('unexpected_response', 'Mailbox list HTML missing')
        if not markup.strip():
            return []
        root, items = Page(markup).root, []
        for row in root.find('li'):
            link = one(row.find('a'), 'Message link')
            url = urlsplit(link.attrs.get('href', ''))
            if url.scheme != 'https' or url.netloc != 'temp-mail24.com' or url.path != '/':
                raise ProviderError('unexpected_response', 'Message link left the receive endpoint')
            mid = one(parse_qs(url.query).get('tm', []), 'Message ID')
            if not re.fullmatch(r'[0-9]+', mid):
                raise ProviderError('unexpected_response', 'Invalid message ID')
            items.append({'id': mid, 'from': one(row.find(cls='email'), 'Message sender').text().strip(),
                          'subject': one(row.find(cls='subject'), 'Message subject').text().strip()})
        if not items:
            raise ProviderError('unexpected_response', 'Nonempty inbox has no recognized rows')
        return items
    if provider == 'nukemail':
        response = obj(client.api('GET', base + '/api/inbox'))
        same(field(response, 'token'), field(client.config, 'token'))
        if response.get('state') != 'active':
            raise ProviderError('mailbox_expired', 'Original mailbox is no longer active')
        entries = array(response.get('addresses'))
        if len(entries) != 1:
            raise ProviderError('mailbox_identity_changed', 'Original single-address inbox changed')
        entry = one([e for e in entries if str(e.get('id')) == client.config['inbox_id']], 'Original mailbox entry')
        same(field(entry, 'address') + '@' + field(entry, 'domain'), address)
        same(field(entry, 'token'), field(client.config, 'token'))
        return [{**row, 'id': identity(row.get('id')), 'from': row.get('sender'), 'date': row.get('received_at'),
                 'text': row.get('body_text'), 'html': row.get('body_html')} for row in array(response.get('messages'))]
    if provider == 'xelio':
        rows = array(data(client.api('GET', base + '/api/emails/' + quote(address, safe=''), headers=auth(client))))
        result = []
        for row in rows:
            same(field(row, 'recipients'), address)
            result.append({**row, **raw_message(field(row, 'data'), address), 'id': identity(row.get('id'))})
        return result
    if provider == 'tmailor':
        info = obj(client.api('POST', base + '/v1/mailbox/info', headers=auth(client), payload={'token': field(client.config, 'token')}))
        same(field(info, 'email'), address)
        same(field(info, 'token'), client.config['token'])
        items, seen, cursor, total = [], set(), None, None
        for _ in range(20):
            page = obj(client.api('POST', base + '/v1/mailbox/messages', headers=auth(client),
                                 payload={'token': client.config['token'], 'limit': 100, **({'cursor': cursor} if cursor else {})}))
            rows = array(page.get('items'))
            if type(page.get('total')) is not int or page['total'] < 0 or (total is not None and page['total'] != total):
                raise ProviderError('incomplete_mailbox_observation', 'Message count changed during pagination')
            total = page['total']
            for row in rows:
                same(field(row, 'to'), address)
                items.append({**row, 'id': identity(row.get('id')), 'from': field(obj(row.get('from')), 'address'), 'date': row.get('received_at')})
            if 'next_cursor' not in page:
                raise ProviderError('incomplete_mailbox_observation', 'Missing continuation metadata')
            cursor = page['next_cursor']
            if cursor is None:
                if len(items) != total:
                    raise ProviderError('incomplete_mailbox_observation', 'Message count and parsed rows differ')
                return items
            if not isinstance(cursor, str) or not cursor or cursor in seen or not rows:
                raise ProviderError('incomplete_mailbox_observation', 'Invalid or repeated continuation cursor')
            seen.add(cursor)
        raise ProviderError('incomplete_mailbox_observation', 'Inbox exceeded the observation page limit')
    response = obj(client.api('GET', base + '/inbox/' + quote(client.config['inbox_id'], safe='') + '/messages', headers=auth(client)))
    return [{**row, 'id': identity(row.get('id')), 'from': row.get('sender'), 'date': row.get('created_at')}
            for row in array(response.get('messages'))]


def read_message(client, item):
    from .expansion_clients import body_present, field, obj, raw_message, same
    provider, address, mid = client.config['provider'], client.config['address'], item['id']
    current = next((r for r in client.list_messages() if r['id'] == mid), None)
    if current is None:
        raise ProviderError('message_not_in_mailbox', 'Message is no longer in the original mailbox')
    if provider in {'noemail', 'nukemail'}:
        return body_present(current)
    base = BASES[provider]
    if provider == 'temp_mail24':
        response = obj(wp(client, 'message', nonce=field(client.config, 'nonce'), message_id=mid))
        root = Page(field(response, 'html')).root
        frame = one(root.find('iframe', cls='message-frame'), 'Message body frame')
        same(one(root.find(cls='message-subject'), 'Message subject').text().strip(), 'Subject: ' + current['subject'])
        return body_present({**current, 'html': frame.attrs.get('srcdoc')})
    if provider == 'xelio':
        row = obj(data(client.api('GET', base + '/api/emails/' + quote(address, safe='') + '/' + quote(mid, safe=''), headers=auth(client))))
        same(identity(row.get('id')), mid)
        same(field(row, 'recipients'), address)
        return {**current, **raw_message(field(row, 'data'), address)}
    if provider == 'tmailor':
        row = obj(client.api('POST', base + '/v1/mailbox/message', headers=auth(client),
                             payload={'token': field(client.config, 'token'), 'message_id': mid}))
        same(identity(row.get('id')), mid)
        same(field(row, 'to'), address)
        if row.get('body_available') is not True or row.get('seen') is not True:
            raise ProviderError('unexpected_response', 'Message body was not acknowledged')
        return body_present({**current, 'text': row.get('text'), 'html': row.get('html')})
    row = obj(client.api('GET', base + '/inbox/' + quote(client.config['inbox_id'], safe='') + '/messages/' + quote(mid, safe=''), headers=auth(client)))
    same(identity(row.get('id')), mid)
    return body_present({**current, 'text': row.get('text_body'), 'html': row.get('html_body')})
