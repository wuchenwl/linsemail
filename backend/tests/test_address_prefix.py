import re
from unittest.mock import Mock

import pytest

from backend.api import CreateMailbox
from backend.registry import PROVIDERS
from mailbox_clients import Client, CUSTOM_PREFIX_PROVIDERS, ProviderError


@pytest.mark.parametrize('provider', sorted(CUSTOM_PREFIX_PROVIDERS))
@pytest.mark.parametrize('requested', ['my-test-', '', None, '   ', 'A' * 48])
def test_prefix_in_real_create_branches(tmp_path, provider, requested):
    client = Client(tmp_path)
    client.config['address_prefix'] = requested
    client.list_messages = Mock(return_value=[])
    client.json = Mock(return_value={'public': ['grabmail.io']})
    client.request = Mock(return_value='<button type="button" class="dropdown-item">mailto.plus</button>')
    result = client.create(provider)
    local = result['address'].split('@')[0]
    expected = (requested or '').strip()
    assert local.startswith(expected)
    assert re.fullmatch(r'[a-f0-9]{16}', local[len(expected):])
    assert len(local) <= 64
    assert 'ls-tasklet-' not in local
    # Reopening a session retains its address; create can never overwrite it.
    reopened = Client(tmp_path)
    assert reopened.config['address'] == result['address']
    with pytest.raises(ProviderError, match='Existing mailbox'):
        reopened.create(provider)


@pytest.mark.parametrize('requested', ['@bad', 'a/b', 'a@b', '-bad', 'two words', 'a\nb', '前缀', 'a' * 49, 123, {}, []])
def test_invalid_prefix_rejected_by_api_and_client(tmp_path, requested):
    with pytest.raises(ValueError):
        CreateMailbox(provider_id='grabmail', address_prefix=requested)
    client = Client(tmp_path)
    client.config['address_prefix'] = requested
    client.json = Mock()
    with pytest.raises(ProviderError) as error:
        client.create('grabmail')
    assert error.value.code == 'invalid_address_prefix'
    client.json.assert_not_called()


def test_unsupported_prefix_fails_before_network(tmp_path):
    client = Client(tmp_path)
    client.config['address_prefix'] = 'my-test-'
    client.json = Mock()
    with pytest.raises(ProviderError) as error:
        client.create('guerrillamail')
    assert error.value.code == 'custom_prefix_unsupported'
    client.json.assert_not_called()


def test_registry_advertises_only_supported_prefix_protocols():
    supported = {row['id'] for row in PROVIDERS if row['capabilities']['custom_prefix'] == 'supported'}
    assert supported == CUSTOM_PREFIX_PROVIDERS


def test_random_suffix_is_new_for_each_creation(tmp_path):
    addresses = []
    for index in range(3):
        client = Client(tmp_path / str(index))
        client.config['address_prefix'] = 'same-'
        client.list_messages = Mock(return_value=[])
        client.create('maildrop')
        addresses.append(client.config['address'])
    assert len(set(addresses)) == 3
