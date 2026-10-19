"""Receive contracts found in public service docs and current first-party assets."""
from email.utils import getaddresses
import hashlib
from urllib.parse import quote

from mailbox_clients import ProviderError
from .web_clients import Page, array, identity, one

DISCOVERY_FIELDS = {
    'kmail': [],
    'freecustom': ['api_key'],
    'ninemail': ['api_key'],
    'hissline': ['api_key', 'agent_id', 'domain_id'],
    'tempmail_ai': [],
    'temp_mail_ai': [],
    'mailsink': ['api_key'],
}
BASES = {'kmail': 'https://kmail.pw', 'freecustom': 'https://api2.freecustom.email/v1',
         'ninemail': 'https://9mail.xyz/api/v1', 'hissline': 'https://app.mailhook.co/api/v1',
         'tempmail_ai': 'https://www.tempmail.ai/api/common-api/v1/temp-mail',
         'temp_mail_ai': 'https://temp-mail.ai/api', 'mailsink': 'https://api.mailsink.dev/v1'}


def auth(client):
    provider = client.config['provider']
    credentials = client.config.get('credentials', {})
    if provider in {'kmail', 'tempmail_ai', 'temp_mail_ai'}:
        return {}
    if provider == 'hissline':
        return {'X-API-Key': credentials['api_key'], 'X-Agent-ID': credentials['agent_id']}
    return {'Authorization': 'Bearer ' + credentials['api_key']}


def acknowledged(value):
    from .expansion_clients import obj
    value = obj(value)
    if value.get('success') is not True:
        raise ProviderError('upstream_api_error', 'Upstream did not acknowledge the operation')
    return value


def tempmail_ai_data(value):
    from .expansion_clients import obj
    value = obj(value)
    if type(value.get('code')) is not int or value['code'] != 0:
        raise ProviderError('upstream_api_error', 'Upstream did not acknowledge the operation')
    return obj(value.get('data'))


def hissline_identity(client):
    from .expansion_clients import field, obj, same
    resource = obj(obj(client.api('GET', BASES['hissline'] + '/email_addresses/'
                                 + quote(client.config['inbox_id'], safe=''), headers=auth(client))).get('data'))
    if resource.get('type') != 'email_address':
        raise ProviderError('unexpected_response', 'Expected an email-address resource')
    same(identity(resource.get('id')), client.config['inbox_id'])
    attributes = obj(resource.get('attributes'))
    same(field(attributes, 'email'), client.config['address'])
    same(identity(attributes.get('domain_id')), client.config['credentials']['domain_id'])


def create_mailbox(client, provider, prefix):
    from .expansion_clients import domain, email, field, obj, same
    base, headers = BASES[provider], auth(client)
    if provider == 'kmail':
        current = obj(client.api('GET', base + '/api/refresh'))
        client.config['address'] = email(field(current, 'email'))
        array(current.get('mails'))
        cookies = {'|'.join((c.domain, c.path, c.name)): hashlib.sha256(c.value.encode()).hexdigest()
                   for c in client.cookies if not c.is_expired()}
        if not cookies:
            raise ProviderError('session_initialization_failed', 'Mailbox cookie session missing')
        client.config['session_cookies'] = cookies
    elif provider == 'freecustom':
        domains = array(acknowledged(client.api('GET', base + '/domains', headers=headers)).get('data'))
        if not domains:
            raise ProviderError('no_public_domain', 'No receiving domain was supplied')
        address = prefix + '@' + domain(field(domains[0], 'domain'))
        result = acknowledged(client.api('POST', base + '/inboxes', headers=headers, payload={'inbox': address}))
        allocated = obj(result['data']) if 'data' in result else result
        same(field(allocated, 'inbox'), address)
        client.config['address'] = address
    elif provider == 'ninemail':
        allocated = obj(obj(client.api('POST', base + '/inboxes', headers=headers,
                                      payload={'ttl_minutes': 60})).get('data'))
        client.config.update(address=email(field(allocated, 'address')), inbox_id=field(allocated, 'uuid'),
                             expires_at=field(allocated, 'expires_at'))
    elif provider == 'hissline':
        credentials = client.config['credentials']
        allocated = obj(obj(client.api('POST', base + '/email_addresses/random', headers=headers,
                                      payload={'domain_id': credentials['domain_id']})).get('data'))
        if allocated.get('type') != 'email_address':
            raise ProviderError('unexpected_response', 'Expected an email-address resource')
        attributes = obj(allocated.get('attributes'))
        same(identity(attributes.get('domain_id')), credentials['domain_id'])
        client.config.update(address=email(field(attributes, 'email')), inbox_id=identity(allocated.get('id')),
                             expires_at=attributes.get('expires_at'))
    elif provider == 'tempmail_ai':
        allocated = tempmail_ai_data(client.api('POST', base + '/session', payload={'local_part': prefix}))
        address = email(field(allocated, 'address'))
        same(field(allocated, 'local_part'), prefix)
        same(address, prefix + '@' + domain(field(allocated, 'domain')))
        client.config.update(address=address, inbox_id=prefix, expires_at=allocated.get('expires_at'))
    elif provider == 'temp_mail_ai':
        allocated = acknowledged(client.api('POST', base + '/mailbox/random'))
        client.config['address'] = email(field(allocated, 'email'))
    elif provider == 'mailsink':
        allocated = obj(client.api('POST', base + '/inboxes', headers=headers, payload={'local_part': prefix}))
        client.config.update(address=email(field(allocated, 'address')), inbox_id=field(allocated, 'id'),
                             expires_at=allocated.get('expires_at'))


def list_messages(client):
    from .expansion_clients import field, obj, same
    provider, address = client.config['provider'], client.config['address']
    base, headers = BASES[provider], auth(client)
    if provider == 'kmail':
        current = obj(client.api('GET', base + '/api/refresh'))
        same(field(current, 'email'), address)
        return [{**row, 'id': identity(row.get('id')), 'from': row.get('email')}
                for row in array(current.get('mails'))]
    if provider == 'tempmail_ai':
        current = tempmail_ai_data(client.api('GET', base + '/inbox', params={'local_part': client.config['inbox_id']}))
        session = obj(current.get('session'))
        same(field(session, 'local_part'), client.config['inbox_id'])
        same(field(session, 'address'), address)
        return [{**row, 'id': identity(row.get('id')), 'from': row.get('from_addr')}
                for row in array(current.get('mails'))]
    if provider == 'temp_mail_ai':
        response = acknowledged(client.api('GET', base + '/mailbox/' + quote(address, safe='') + '/messages'))
        return [{**row, 'id': identity(row.get('id'))} for row in array(response.get('messages'))]
    if provider == 'mailsink':
        response = obj(client.api('GET', base + '/inboxes/' + quote(client.config['inbox_id'], safe='')
                                  + '/messages', headers=headers))
        return [{**row, 'id': identity(row.get('id')), 'from': row.get('from_address')}
                for row in array(response.get('messages'))]
    if provider == 'hissline':
        hissline_identity(client)
        response = obj(client.api('GET', base + '/email_addresses/' + quote(client.config['inbox_id'], safe='')
                                  + '/inbound_emails', headers=headers, params={'limit': 100}))
        rows = array(response.get('data'))
        if len(rows) >= 100:
            raise ProviderError('incomplete_mailbox_observation', 'Inbox reached the documented result limit')
        items = []
        for row in rows:
            if row.get('type') != 'inbound_email':
                raise ProviderError('unexpected_response', 'Expected an inbound-email resource')
            attributes = obj(row.get('attributes'))
            items.append({'id': identity(row.get('id')), 'from': attributes.get('from_email'),
                          'subject': attributes.get('subject'), 'received_at': attributes.get('received_at')})
        return items
    items, seen, cursor = [], set(), None
    for _ in range(10):
        if provider == 'freecustom':
            response = acknowledged(client.api('GET', base + '/inboxes/' + quote(address, safe='') + '/messages',
                                              headers=headers, params={'limit': 100, **({'before': cursor} if cursor else {})}))
            data = obj(response.get('data'))
            same(field(data, 'inbox'), address)
            rows = array(data.get('messages'))
            if type(data.get('count')) is not int or data['count'] != len(rows):
                raise ProviderError('incomplete_mailbox_observation', 'Message count does not match the page')
            if not isinstance(data.get('has_more'), bool):
                raise ProviderError('incomplete_mailbox_observation', 'Missing continuation metadata')
            next_cursor = identity(rows[-1].get('id')) if data['has_more'] and rows else None
            if data['has_more'] and not rows:
                raise ProviderError('incomplete_mailbox_observation', 'Continuation page was empty')
        else:
            response = obj(client.api('GET', base + '/inboxes/' + quote(client.config['inbox_id'], safe='')
                                      + '/messages', headers=headers,
                                      params={'per_page': 25, **({'cursor': cursor} if cursor else {})}))
            rows = array(response.get('data'))
            meta = obj(response.get('meta'))
            if 'next_cursor' not in meta:
                raise ProviderError('incomplete_mailbox_observation', 'Missing continuation metadata')
            next_cursor = meta['next_cursor']
            if next_cursor is not None and (not isinstance(next_cursor, str) or not next_cursor):
                raise ProviderError('incomplete_mailbox_observation', 'Invalid continuation cursor')
            if next_cursor is not None and not rows:
                raise ProviderError('incomplete_mailbox_observation', 'Continuation page was empty')
            rows = [{**row, 'id': identity(row.get('uuid'))} for row in rows]
        items.extend({**row, 'id': identity(row.get('id'))} for row in rows)
        if next_cursor is None:
            return items
        if next_cursor in seen:
            raise ProviderError('incomplete_mailbox_observation', 'Repeated continuation cursor')
        seen.add(next_cursor)
        cursor = next_cursor
    raise ProviderError('incomplete_mailbox_observation', 'Inbox exceeded the observation page limit')


def read_message(client, item):
    from .expansion_clients import body_present, field, obj, same
    provider, address, message_id = client.config['provider'], client.config['address'], item['id']
    base, headers = BASES[provider], auth(client)
    mid = quote(message_id, safe='')
    if provider == 'kmail':
        current = client.list_messages()
        if not any(row['id'] == message_id for row in current):
            raise ProviderError('message_not_in_mailbox', 'Message is no longer in the original mailbox')
        source = client.request('GET', base + '/mail/' + mid)
        root = Page(source).root
        recipients = [node.text().split(':', 1)[1].strip() for node in root.find('div')
                      if any(getattr(child, 'tag', None) == 'strong' and child.text().strip() == 'To:'
                             for child in node.children)]
        if not recipients or any(value.lower() != address.lower() for value in recipients):
            raise ProviderError('mailbox_identity_changed', 'Message recipient changed')
        frame = one(root.find('iframe', identity='email-content-iframe'), 'Message body frame')
        return body_present({**item, 'html': frame.attrs.get('srcdoc')})
    if provider in {'tempmail_ai', 'mailsink'}:
        # The detail route uses a global ID; reconfirm membership in the saved inbox.
        if not any(row['id'] == message_id for row in client.list_messages()):
            raise ProviderError('message_not_in_mailbox', 'Message is no longer in the original mailbox')
        if provider == 'tempmail_ai':
            message = tempmail_ai_data(client.api('GET', base + '/mail/' + mid,
                                                  params={'local_part': client.config['inbox_id']}))
            same(identity(message.get('id')), message_id)
            return body_present({**item, 'text': message.get('text_body'), 'html': message.get('html')})
        message = obj(client.api('GET', base + '/messages/' + mid, headers=headers))
        content = obj(message.get('content'))
        if 'id' in message:
            same(identity(message['id']), message_id)
        return body_present({**item, 'text': content.get('text')})
    if provider == 'temp_mail_ai':
        message = obj(acknowledged(client.api('GET', base + '/mailbox/' + quote(address, safe='')
                                             + '/message/' + mid)).get('message'))
        same(identity(message.get('id')), message_id)
        recipients = [value.lower() for _, value in getaddresses([field(message, 'to')])]
        if address.lower() not in recipients:
            raise ProviderError('mailbox_identity_changed', 'Message recipient changed')
        return body_present({**item, **message})
    if provider == 'freecustom':
        response = acknowledged(client.api('GET', base + '/inboxes/' + quote(address, safe='') + '/messages/' + mid,
                                          headers=headers))
        message = obj(response.get('data'))
        same(identity(message.get('id')), message_id)
        recipients = [value for _, value in getaddresses([field(message, 'to')])]
        if address.lower() not in {value.lower() for value in recipients}:
            raise ProviderError('mailbox_identity_changed', 'Message recipient changed')
        return body_present({**item, **message})
    if provider == 'ninemail':
        message = obj(obj(client.api('GET', base + '/messages/' + mid, headers=headers)).get('data'))
        same(field(message, 'uuid'), message_id)
        recipients = array(message.get('to_addresses'))
        if address.lower() not in {field(recipient, 'email').lower() for recipient in recipients}:
            raise ProviderError('mailbox_identity_changed', 'Message recipient changed')
        return body_present({**item, 'text': message.get('text_body'), 'html': message.get('html_body')})
    hissline_identity(client)
    response = obj(obj(client.api('GET', base + '/email_addresses/' + quote(client.config['inbox_id'], safe='')
                                 + '/inbound_emails/' + mid, headers=headers)).get('data'))
    same(identity(response.get('id')), message_id)
    if response.get('type') != 'inbound_email':
        raise ProviderError('unexpected_response', 'Expected an inbound-email resource')
    attributes = obj(response.get('attributes'))
    return body_present({**item, 'text': attributes.get('text_body'), 'html': attributes.get('html_body')})
