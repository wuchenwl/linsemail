"""Bounded Socket.IO polling snapshots for the public TempMail.cc protocol.

The server's recovery event follows boot/history/address events and provides
the snapshot boundary. Expired recovery and changed server generations fail;
neither condition allocates a replacement mailbox.
"""
from email.utils import getaddresses
import json
import time
from urllib.parse import urlencode
import uuid

import httpx

from mailbox_clients import ProviderError
from .expansion_clients import array, body_present, email, field, identity, obj, same


class PollingSocket:
    def __init__(self):
        self.client = httpx.Client(trust_env=False, timeout=15, follow_redirects=False,
                                   headers={'User-Agent': 'Linsemail/0.5', 'Origin': 'https://tempmail.cc'})
        self.sid = None
        self.deadline = time.monotonic() + 35
        self.received_bytes = 0

    def raw(self, method, packet=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderError('upstream_timeout', 'Mailbox snapshot did not complete')
        params = {'EIO': '4', 'transport': 'polling'}
        if self.sid:
            params['sid'] = self.sid
        try:
            with self.client.stream(method, 'https://tempmail.cc/socket.io/?' + urlencode(params),
                                    content=packet, timeout=min(remaining, 15),
                                    headers={'Content-Type': 'text/plain;charset=UTF-8'} if packet is not None else None) as response:
                data = bytearray()
                for chunk in response.iter_bytes():
                    self.received_bytes += len(chunk)
                    if self.received_bytes > 2_000_000:
                        raise ProviderError('response_too_large', 'Mailbox snapshot exceeded the receive limit')
                    data.extend(chunk)
                status = response.status_code
        except httpx.HTTPError:
            raise ProviderError('upstream_network_error', 'Socket mailbox request failed') from None
        if not 200 <= status < 300:
            code = 'rate_limited' if status == 429 else 'upstream_access_denied' if status in (401, 403) else 'upstream_http_error'
            raise ProviderError(code, 'Socket mailbox request failed', status)
        try:
            return data.decode('utf-8')
        except UnicodeError:
            raise ProviderError('unexpected_response', 'Expected UTF-8 socket packets') from None

    def packets(self):
        source = self.raw('GET')
        if not source:
            raise ProviderError('unexpected_response', 'Empty socket transport response')
        events = []
        for packet in source.split('\x1e'):
            if packet == '2':
                if self.raw('POST', '3') != 'ok':
                    raise ProviderError('unexpected_response', 'Heartbeat was not acknowledged')
            elif packet.startswith('40'):
                events.append(['connected'])
            elif packet.startswith('42'):
                try:
                    value = json.loads(packet[2:])
                except ValueError:
                    raise ProviderError('unexpected_response', 'Malformed mailbox event') from None
                if not isinstance(value, list) or not value or not isinstance(value[0], str):
                    raise ProviderError('unexpected_response', 'Invalid mailbox event')
                events.append(value)
            elif packet == '6':
                continue
            elif packet.startswith(('1', '41', '44')):
                raise ProviderError('mailbox_session_lost', 'Mailbox socket disconnected or rejected')
            else:
                raise ProviderError('unexpected_response', 'Unsupported socket transport packet')
        return events

    def connect(self):
        source = self.raw('GET')
        try:
            handshake = obj(json.loads(source[1:])) if source.startswith('0') else None
        except ValueError:
            handshake = None
        if handshake is None:
            raise ProviderError('unexpected_response', 'Socket handshake missing')
        self.sid = field(handshake, 'sid')
        if self.raw('POST', '40') != 'ok':
            raise ProviderError('unexpected_response', 'Socket connection was not acknowledged')
        for _ in range(6):
            if any(event[0] == 'connected' for event in self.packets()):
                return
        raise ProviderError('upstream_timeout', 'Socket namespace did not connect')

    def emit(self, *args):
        packet = '42' + json.dumps(list(args), separators=(',', ':'))
        if self.raw('POST', packet) != 'ok':
            raise ProviderError('unexpected_response', 'Mailbox request was not acknowledged')

    def close(self):
        try:
            if self.sid:
                self.raw('POST', '41\x1e1')
        except ProviderError:
            pass
        finally:
            self.client.close()


def snapshot(saved, *, create=False):
    socket = PollingSocket()
    try:
        socket.connect()
        client_token = str(uuid.uuid4()) if create else field(saved, 'client_token')
        if create:
            socket.emit('request shortid', client_token)
        else:
            socket.emit('recover', field(saved, 'recovery_token'), client_token)
        boot, address, recovery, expires = None, None, None, None
        messages = {}
        for _ in range(20):
            for event in socket.packets():
                name = event[0]
                value = event[1] if len(event) > 1 else None
                if name in {'recovery rejected', 'shortid rejected'}:
                    raise ProviderError('mailbox_session_lost', 'Original mailbox recovery was rejected')
                if name == 'server error':
                    raise ProviderError('upstream_api_error', 'Mailbox server reported a receive error')
                if name == 'boot':
                    candidate = field(obj(value), 'id')
                    if boot is not None and boot != candidate or not create and saved.get('server_boot') != candidate:
                        raise ProviderError('mailbox_generation_changed', 'Mailbox server generation changed')
                    boot = candidate
                elif name == 'shortid':
                    if not isinstance(value, str) or '@' in value:
                        raise ProviderError('unexpected_response', 'Invalid allocated mailbox name')
                    candidate = email(value + '@tempmail.cc')
                    if address is not None:
                        same(candidate, address)
                    if not create:
                        same(candidate, field(saved, 'address'))
                    address = candidate
                elif name in {'mail history', 'mail'}:
                    rows = array(value) if name == 'mail history' else [obj(value)]
                    if name == 'mail history' and len(rows) >= 20:
                        raise ProviderError('incomplete_mailbox_observation', 'Mailbox reached its replay buffer limit')
                    for row in rows:
                        mid = row.get('_id')
                        if type(mid) is not int or mid <= 0:
                            raise ProviderError('unexpected_response', 'Server mail identity missing')
                        if mid in messages and messages[mid] != row:
                            raise ProviderError('unexpected_response', 'Conflicting replay identity')
                        messages[mid] = row
                elif name == 'recovery':
                    recovery = field(obj(value), 'token')
                    expires = value.get('expiresAt')
                    if not create:
                        same(recovery, field(saved, 'recovery_token'))
            if recovery is not None:
                if boot is None or address is None or type(expires) not in (int, float) or expires <= time.time()*1000:
                    raise ProviderError('unexpected_response', 'Mailbox snapshot boundary was incomplete or expired')
                items = []
                for mid, row in messages.items():
                    headers = obj(row.get('headers'))
                    recipients = row.get('recipients')
                    if not isinstance(recipients, list) or any(not isinstance(v, str) for v in recipients):
                        recipients = [v for _, v in getaddresses([str(headers.get('to', ''))])]
                    if address.lower() not in {v.lower() for v in recipients}:
                        raise ProviderError('mailbox_identity_changed', 'Replay message belongs to another recipient')
                    items.append(body_present({'id': identity(boot + ':' + str(mid)), 'from': headers.get('from'),
                                               'subject': headers.get('subject'), 'received_at': headers.get('date'),
                                               'text': row.get('text'), 'html': row.get('html')}))
                return {'address': address, 'client_token': client_token, 'recovery_token': recovery,
                        'server_boot': boot, 'expires_at': expires}, items
        raise ProviderError('incomplete_mailbox_observation', 'Mailbox snapshot boundary did not arrive')
    finally:
        socket.close()
