"""실제 Windows 사용자 DPAPI와 테스트 전용 값의 저장·격리·실패 보존 검사."""

import importlib
import json
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest


def module():
    return importlib.import_module('ai_providers.credentials')


def test_dpapi_roundtrip_binds_owner_origin_and_masks_representation(tmp_path):
    auth = module()
    store = auth.CredentialStore(tmp_path)
    result = store.put(
        'profile-a', 'https://api.openai.com/v1', 'api_key', 'fixture-key-never-real'
    )
    record = store.get(result['ref'], 'profile-a', 'https://api.openai.com')
    assert record.value.get_secret_value() == 'fixture-key-never-real'
    assert record.version == 1 and 'fixture-key-never-real' not in repr(record)
    assert all(
        b'fixture-key-never-real' not in p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()
    )
    for owner, origin in [
        ('profile-b', 'https://api.openai.com'),
        ('profile-a', 'https://different.example'),
    ]:
        with pytest.raises(auth.AuthError):
            store.get(result['ref'], owner, origin)


def test_metadata_contains_no_secret_or_token_payload(tmp_path):
    auth = module()
    store = auth.CredentialStore(tmp_path)
    result = store.put(
        'profile-a',
        'https://api.openai.com',
        'oauth_chatgpt',
        {'access_token': 'fixture-access', 'refresh_token': 'fixture-refresh'},
    )
    meta = store.metadata(result['ref'], 'profile-a')
    assert meta['version'] == 1 and meta['origin'] == 'https://api.openai.com'
    assert 'fixture-access' not in json.dumps(meta) and 'value' not in meta
    assert (
        json.loads(
            store.get(result['ref'], 'profile-a', 'https://api.openai.com').value.get_secret_value()
        )['refresh_token']
        == 'fixture-refresh'
    )


def test_replacement_uses_atomic_version_check_and_preserves_old_on_conflict(tmp_path):
    auth = module()
    store = auth.CredentialStore(tmp_path)
    first = store.put('profile-a', 'https://api.openai.com', 'api_key', 'old-value')
    second = store.put(
        'profile-a',
        'https://api.openai.com',
        'api_key',
        'new-value',
        ref=first['ref'],
        expected_version=1,
    )
    assert second == dict(ref=first['ref'], version=2)
    with pytest.raises(auth.AuthError):
        store.put(
            'profile-a',
            'https://api.openai.com',
            'api_key',
            'stale-value',
            ref=first['ref'],
            expected_version=1,
        )
    assert (
        store.get(first['ref'], 'profile-a', 'https://api.openai.com').value.get_secret_value()
        == 'new-value'
    )


def test_existing_ref_cannot_be_rebound_to_another_owner_or_origin(tmp_path):
    auth = module()
    store = auth.CredentialStore(tmp_path)
    first = store.put('profile-a', 'https://api.openai.com', 'api_key', 'preserved')
    for owner, origin in [
        ('profile-b', 'https://api.openai.com'),
        ('profile-a', 'https://other.example'),
    ]:
        with pytest.raises(auth.AuthError):
            store.put(owner, origin, 'api_key', 'forbidden', ref=first['ref'])
    assert store.get(first['ref'], 'profile-a', 'https://api.openai.com').version == 1


def test_protection_failure_has_no_plaintext_fallback_or_secret_exception(tmp_path):
    auth = module()

    class BrokenProtector:
        def protect(self, *args):
            raise RuntimeError('fixture-value-must-not-appear')

        def unprotect(self, *args):
            raise AssertionError('unused')

    store = auth.CredentialStore(tmp_path, protector=BrokenProtector())
    with pytest.raises(auth.AuthError) as raised:
        store.put('profile-a', 'https://api.openai.com', 'api_key', 'fixture-value-must-not-appear')
    assert 'fixture-value-must-not-appear' not in str(raised.value)
    assert not list(tmp_path.glob('cred_*.json'))


def test_atomic_replace_failure_preserves_existing_encrypted_record(tmp_path, monkeypatch):
    auth = module()
    store = auth.CredentialStore(tmp_path)
    first = store.put('profile-a', 'https://api.openai.com', 'api_key', 'preserved')

    def fail(*args):
        raise PermissionError('write failure')

    monkeypatch.setattr(auth.os, 'replace', fail)
    with pytest.raises(auth.AuthError):
        store.put(
            'profile-a', 'https://api.openai.com', 'api_key', 'not-committed', ref=first['ref']
        )
    assert (
        store.get(first['ref'], 'profile-a', 'https://api.openai.com').value.get_secret_value()
        == 'preserved'
    )
    assert not list(tmp_path.glob('*.tmp-*'))


def test_tampered_metadata_or_ciphertext_cannot_be_decrypted(tmp_path):
    auth = module()
    store = auth.CredentialStore(tmp_path)
    first = store.put('profile-a', 'https://api.openai.com', 'api_key', 'preserved')
    path = tmp_path / (first['ref'] + '.json')
    original = path.read_text(encoding='utf-8')
    for field, replacement in [
        ('origin', 'https://other.example'),
        ('version', 9),
        ('ciphertext', 'AAAA'),
    ]:
        value = json.loads(original)
        value[field] = replacement
        path.write_text(json.dumps(value), encoding='utf-8')
        with pytest.raises(auth.AuthError):
            store.get(first['ref'], 'profile-a', 'https://api.openai.com')
    path.write_text(original, encoding='utf-8')
    assert (
        store.get(first['ref'], 'profile-a', 'https://api.openai.com').value.get_secret_value()
        == 'preserved'
    )


@pytest.mark.parametrize(
    'reference', ['../outside', 'cred_bad', 'C:/outside', 'cred_' + 'a' * 32 + '/child']
)
def test_references_cannot_escape_the_store(tmp_path, reference):
    auth = module()
    store = auth.CredentialStore(tmp_path)
    with pytest.raises(auth.AuthError):
        store.get(reference, 'profile-a', 'https://api.openai.com')


def test_delete_checks_owner_and_is_idempotent(tmp_path):
    auth = module()
    store = auth.CredentialStore(tmp_path)
    ref = store.put('profile-a', 'https://api.openai.com', 'api_key', 'fixture')['ref']
    with pytest.raises(auth.AuthError):
        store.delete(ref, 'profile-b')
    assert store.delete(ref, 'profile-a') is True
    assert store.delete(ref, 'profile-a') is False


def test_parallel_store_instances_do_not_lose_versions(tmp_path):
    auth = module()
    first = auth.CredentialStore(tmp_path).put(
        'profile-a', 'https://api.openai.com', 'api_key', 'initial'
    )

    def replace(index):
        return auth.CredentialStore(tmp_path).put(
            'profile-a', 'https://api.openai.com', 'api_key', str(index), ref=first['ref']
        )['version']

    with ThreadPoolExecutor(max_workers=4) as pool:
        versions = list(pool.map(replace, range(8)))
    assert sorted(versions) == [2, 3, 4, 5, 6, 7, 8, 9]
    assert auth.CredentialStore(tmp_path).metadata(first['ref'], 'profile-a')['version'] == 9


def test_explicit_transaction_allows_nested_owner_bound_operations(tmp_path):
    auth = module()
    store = auth.CredentialStore(tmp_path)
    first = store.put('profile-a', 'https://api.openai.com', 'api_key', 'initial')
    with store.transaction(first['ref'], 'profile-a'):
        current = store.get(first['ref'], 'profile-a', 'https://api.openai.com')
        store.put(
            'profile-a',
            'https://api.openai.com',
            'api_key',
            'rotated',
            ref=first['ref'],
            expected_version=current.version,
        )
    assert store.metadata(first['ref'], 'profile-a')['version'] == 2


def test_transaction_excludes_another_process_without_exposing_plaintext(tmp_path):
    auth = module()
    store = auth.CredentialStore(tmp_path)
    ref = store.put('profile-a', 'https://api.openai.com', 'api_key', 'fixture-only')['ref']
    script = "from ai_providers.credentials import CredentialStore,AuthError; import sys; s=CredentialStore(sys.argv[1]);\ntry:\n with s.transaction(sys.argv[2],'profile-a',timeout=0): print('unexpected-access')\nexcept AuthError as e: print(e.code)"
    with store.transaction(ref, 'profile-a'):
        child = subprocess.run(
            [sys.executable, '-c', script, str(tmp_path), ref],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=8,
        )
    assert child.returncode == 0 and child.stdout.strip() == 'credential_busy'
