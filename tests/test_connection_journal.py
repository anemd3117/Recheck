"""Connection diagnostics contain fixed labels and booleans, never account data."""

import importlib
import importlib.util
import itertools
import json

import pytest


def journal():
    assert importlib.util.find_spec('recheck.connection_journal') is not None, (
        'Safe connection diagnostics are missing'
    )
    return importlib.import_module('recheck.connection_journal')


def outcome(**updates):
    return {
        'schema_version': 1,
        'provider': 'chatgpt',
        'auth_mode': 'chatgpt',
        'stage': 'token_exchange',
        'status': 'failed',
        'code': 'oauth_network',
        'account_saved': False,
        'proof_saved': False,
        'selected': False,
        **updates,
    }


@pytest.mark.parametrize('flags', tuple(itertools.product((False, True), repeat=3)))
def test_round_trip_records_only_fixed_schema_and_exact_readiness_booleans(tmp_path, flags):
    module = journal()
    account_saved, proof_saved, selected = flags
    assert module.write_connection_outcome(
        tmp_path, provider='chatgpt', auth_mode='chatgpt', stage='token_exchange',
        status='failed', code='oauth_network', account_saved=account_saved,
        proof_saved=proof_saved, selected=selected,
    ) is None
    expected = outcome(account_saved=account_saved, proof_saved=proof_saved, selected=selected)
    assert module.read_connection_outcome(tmp_path) == expected
    path = tmp_path / 'last-connection-status.json'
    assert json.loads(path.read_text(encoding='utf-8')) == expected
    assert path.stat().st_size <= 4096
    assert set(tmp_path.iterdir()) == {path}


def test_later_outcome_replaces_single_journal_without_touching_settings(tmp_path):
    module = journal()
    settings = tmp_path / 'settings.json'
    settings.write_text('{"test_setting":true}', encoding='utf-8')
    module.write_connection_outcome(
        tmp_path, provider='openai', auth_mode='chatgpt', stage='login', status='running',
    )
    module.write_connection_outcome(
        tmp_path, provider='openai', auth_mode='chatgpt', stage='complete', status='success',
        account_saved=True, proof_saved=True, selected=True,
    )
    saved = module.read_connection_outcome(tmp_path)
    assert saved['stage'] == 'complete' and saved['status'] == 'success'
    assert saved['code'] == '' and saved['selected'] is True
    assert settings.read_text(encoding='utf-8') == '{"test_setting":true}'
    assert {path.name for path in tmp_path.iterdir()} == {'settings.json', 'last-connection-status.json'}


def test_fresh_authorization_recovery_stage_can_be_saved_without_registration_details(tmp_path):
    module = journal()
    module.write_connection_outcome(
        tmp_path, provider='chatgpt', auth_mode='chatgpt',
        stage='authorization_restart', status='running',
    )
    assert module.read_connection_outcome(tmp_path) == outcome(
        stage='authorization_restart', status='running', code='',
    )


@pytest.mark.parametrize('code', [
    'oauth_timeout?token=fixture-private',
    'oauth_network\nfixture-private',
    'Bearer fixture-private',
    'fixture-private@example.invalid',
    'https://auth.openai.com/?code=fixture-private',
    {'code': 'oauth_timeout', 'message': 'fixture-private'},
])
def test_unknown_or_sensitive_error_is_never_laundered_into_a_stored_code(tmp_path, code):
    module = journal()
    module.write_connection_outcome(
        tmp_path, provider='chatgpt', auth_mode='chatgpt', stage='login', status='failed', code=code,
    )
    assert module.read_connection_outcome(tmp_path)['code'] == 'operation_failed'
    assert 'fixture-private' not in (tmp_path / 'last-connection-status.json').read_text(encoding='utf-8')


def test_untrusted_labels_and_non_boolean_flags_cannot_enter_the_journal(tmp_path):
    module = journal()
    module.write_connection_outcome(
        tmp_path, provider='fixture-private@example.invalid', auth_mode='fixture-private',
        stage='https://auth.openai.com/?code=fixture-private', status='fixture-private',
        code='fixture-private', account_saved='fixture-private', proof_saved=1, selected=[],
    )
    saved = module.read_connection_outcome(tmp_path)
    assert saved == outcome(
        provider='unknown', auth_mode='unknown', stage='unknown', status='failed',
        code='operation_failed',
    )
    assert 'fixture-private' not in (tmp_path / 'last-connection-status.json').read_text(encoding='utf-8')


@pytest.mark.parametrize('key', [
    'account', 'email', 'subject', 'profile_id', 'token', 'url', 'error_description', 'extra',
])
def test_reader_rejects_unknown_or_sensitive_keys_instead_of_returning_them(tmp_path, key):
    module = journal()
    data = outcome(**{key: 'fixture-private'})
    (tmp_path / 'last-connection-status.json').write_text(json.dumps(data), encoding='utf-8')
    assert module.read_connection_outcome(tmp_path) == {}


@pytest.mark.parametrize('updates', [
    {'schema_version': 2}, {'schema_version': True},
    {'provider': 'private@example.invalid'}, {'auth_mode': 'private'},
    {'stage': 'private'}, {'status': 'private'}, {'code': 'oauth_timeout private'},
    {'account_saved': 1}, {'proof_saved': 'true'}, {'selected': None},
])
def test_reader_requires_current_schema_known_labels_and_strict_booleans(tmp_path, updates):
    module = journal()
    (tmp_path / 'last-connection-status.json').write_text(json.dumps(outcome(**updates)), encoding='utf-8')
    assert module.read_connection_outcome(tmp_path) == {}


@pytest.mark.parametrize('data', [
    b'not-json', b'[]', b'null', b'\xff\xfe', b'{}', b' ' * 4097,
])
def test_corrupt_incomplete_or_oversized_journal_returns_safe_empty_state(tmp_path, data):
    module = journal()
    (tmp_path / 'last-connection-status.json').write_bytes(data)
    assert module.read_connection_outcome(tmp_path) == {}


def test_duplicate_keys_cannot_hide_untrusted_codes_in_an_otherwise_valid_record(tmp_path):
    module = journal()
    encoded = json.dumps(outcome()).replace(
        '"code": "oauth_network"', '"code": "fixture-private", "code": "oauth_network"',
    )
    (tmp_path / 'last-connection-status.json').write_text(encoded, encoding='utf-8')
    assert module.read_connection_outcome(tmp_path) == {}


def test_missing_journal_does_not_create_the_root(tmp_path):
    module = journal()
    root = tmp_path / 'missing'
    assert module.read_connection_outcome(root) == {}
    assert not root.exists()


def test_journal_write_failure_is_benign_and_preserves_previous_outcome(tmp_path, monkeypatch, capsys):
    module = journal()
    module.write_connection_outcome(
        tmp_path, provider='chatgpt', auth_mode='chatgpt', stage='profile_committed',
        status='running', account_saved=True,
    )
    before = module.read_connection_outcome(tmp_path)

    def fail(*args, **kwargs):
        raise PermissionError('fixture-private-write-error')

    monkeypatch.setattr(module, 'atomic_json', fail)
    assert module.write_connection_outcome(
        tmp_path, provider='chatgpt', auth_mode='chatgpt', stage='complete', status='success',
        account_saved=True, proof_saved=True, selected=True,
    ) is None
    assert module.read_connection_outcome(tmp_path) == before
    assert capsys.readouterr() == ('', '')


def test_journal_read_error_returns_safe_empty_state(tmp_path, monkeypatch, capsys):
    module = journal()

    def fail(*args, **kwargs):
        raise PermissionError('fixture-private-read-error')

    monkeypatch.setattr(type(tmp_path), 'open', fail)
    assert module.read_connection_outcome(tmp_path) == {}
    assert capsys.readouterr() == ('', '')
