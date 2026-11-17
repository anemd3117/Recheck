"""Best-effort last connection outcome with no account or request identifiers.

Only fixed application labels and strict booleans reach this file. It records
the most recent connection attempt's state, not credentials or request data.
"""

from __future__ import annotations

import json
from pathlib import Path

from .models import atomic_json


JOURNAL_FILENAME = 'last-connection-status.json'
MAX_JOURNAL_BYTES = 4096
CONNECTION_PROVIDERS = frozenset((
    'ollama', 'openai', 'chatgpt', 'gemini', 'google', 'compatible', 'grok',
    'deepseek', 'anthropic', 'claude', 'unknown',
))
CONNECTION_AUTH_MODES = frozenset(('none', 'api_key', 'chatgpt', 'google_oauth', 'unknown'))
CONNECTION_STAGES = frozenset((
    'idle', 'login', 'browser_requested', 'callback_validated', 'token_exchange',
    'identity_verified', 'profile_committed', 'authorization_restart',
    'models', 'response', 'activate', 'complete', 'unknown',
))
CONNECTION_STATUSES = frozenset(('running', 'success', 'failed', 'cancelled'))
CONNECTION_ERROR_CODES = frozenset((
    '', 'operation_failed', 'cancelled',
    'oauth_account_mismatch', 'oauth_authorization', 'oauth_binding', 'oauth_browser',
    'oauth_configuration', 'oauth_discovery', 'oauth_host', 'oauth_http', 'oauth_id_token',
    'oauth_login', 'oauth_network', 'oauth_origin', 'oauth_reauthentication', 'oauth_redirect',
    'oauth_registration_required', 'oauth_response', 'oauth_timeout', 'oauth_token_response',
    'oauth_unsupported', 'access_denied', 'invalid_grant', 'invalid_client', 'invalid_scope',
    'temporarily_unavailable',
    'credential_auth', 'credential_binding', 'credential_busy', 'credential_cleanup',
    'credential_conflict', 'credential_kind', 'credential_lock', 'credential_missing',
    'credential_origin', 'credential_owner', 'credential_path', 'credential_protection',
    'credential_reference', 'credential_required', 'credential_storage', 'credential_value',
    'secure_store_unavailable', 'capability_proof', 'external_consent',
    'legacy_configuration', 'legacy_reserved', 'profile_configuration', 'profile_disabled',
    'profile_missing', 'profile_role', 'profile_stale', 'settings_configuration',
    'model_catalog_empty', 'provider_preset', 'free_model_required', 'free_model_unavailable',
    'gemini_profile_required', 'malformed_response', 'permission', 'setup_inference',
    'authentication', 'rate_limit', 'network', 'timeout', 'general_settings_unsaved',
))
_LABELS = {
    'provider': CONNECTION_PROVIDERS,
    'auth_mode': CONNECTION_AUTH_MODES,
    'stage': CONNECTION_STAGES,
    'status': CONNECTION_STATUSES,
    'code': CONNECTION_ERROR_CODES,
}
_FLAGS = ('account_saved', 'proof_saved', 'selected')
_FIELDS = frozenset(('schema_version', *_LABELS, *_FLAGS))


def _literal(value, allowed, fallback):
    return value if type(value) is str and value in allowed else fallback


def write_connection_outcome(
    root: Path,
    *,
    provider: str,
    auth_mode: str,
    stage: str,
    status: str,
    code: str = '',
    account_saved: bool = False,
    proof_saved: bool = False,
    selected: bool = False,
) -> None:
    """Write a fixed-label outcome; journal failures never affect authentication."""
    try:
        record = {
            'schema_version': 1,
            'provider': _literal(provider, CONNECTION_PROVIDERS, 'unknown'),
            'auth_mode': _literal(auth_mode, CONNECTION_AUTH_MODES, 'unknown'),
            'stage': _literal(stage, CONNECTION_STAGES, 'unknown'),
            'status': _literal(status, CONNECTION_STATUSES, 'failed'),
            'code': _literal(code, CONNECTION_ERROR_CODES, 'operation_failed'),
            'account_saved': account_saved if type(account_saved) is bool else False,
            'proof_saved': proof_saved if type(proof_saved) is bool else False,
            'selected': selected if type(selected) is bool else False,
        }
        atomic_json(Path(root) / JOURNAL_FILENAME, record)
    except Exception:
        # Never print exceptions: even an OS error may contain identifying paths.
        pass


def _unique_object(pairs):
    record = {}
    for key, value in pairs:
        if key in record:
            raise ValueError('Duplicate journal field')
        record[key] = value
    return record


def read_connection_outcome(root: Path) -> dict:
    """Return only a complete, bounded, current safe schema, or an empty state."""
    try:
        with (Path(root) / JOURNAL_FILENAME).open('rb') as stream:
            encoded = stream.read(MAX_JOURNAL_BYTES + 1)
        if len(encoded) > MAX_JOURNAL_BYTES:
            return {}
        record = json.loads(encoded.decode('utf-8'), object_pairs_hook=_unique_object)
        if not isinstance(record, dict) or set(record) != _FIELDS:
            return {}
        if type(record['schema_version']) is not int or record['schema_version'] != 1:
            return {}
        if any(type(record[name]) is not str or record[name] not in allowed
               for name, allowed in _LABELS.items()):
            return {}
        if any(type(record[name]) is not bool for name in _FLAGS):
            return {}
        return record
    except Exception:
        return {}
