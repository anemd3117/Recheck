"""Long Python file paths remain separate from paths supplied to external tools."""

import importlib
import json
import os
from pathlib import Path

import pytest
from recheck.models import atomic_json
from windows_path_support import legacy_windows_io


@pytest.mark.skipif(os.name != 'nt', reason='Windows filesystem namespace')
def test_atomic_json_writes_long_ordinary_path_without_machine_policy_change(tmp_path, monkeypatch):
    destination = tmp_path / ('ordinary-path-' * 10) / ('nested-result-' * 8) / 'result.json'
    assert len(str(destination)) > 260
    with legacy_windows_io(monkeypatch):
        atomic_json(destination, {'message': '한글 결과', 'status': 'success'})
    assert json.loads(destination.read_text(encoding='utf-8')) == {
        'message': '한글 결과', 'status': 'success'}
    assert not destination.with_suffix('.json.tmp').exists()


@pytest.mark.skipif(os.name != 'nt', reason='Windows filesystem namespace')
@pytest.mark.parametrize('source,expected', [
    ('\\\\?\\C:\\Tools\\app.cmd', 'C:\\Tools\\app.cmd'),
    ('\\\\server\\share\\app.cmd', '\\\\server\\share\\app.cmd'),
    ('\\\\?\\UNC\\server\\share\\app.cmd', '\\\\?\\UNC\\server\\share\\app.cmd'),
    ('\\\\.\\device', '\\\\.\\device'),
])
def test_only_local_drive_namespace_is_unwrapped_for_tool_interoperation(source, expected):
    assert importlib.util.find_spec('recheck.paths'), 'Filesystem-only path support is missing'
    module = importlib.import_module('recheck.paths')
    assert str(module.plain_local_path(source)) == expected


def test_short_filesystem_path_is_not_changed(tmp_path):
    assert importlib.util.find_spec('recheck.paths'), 'Filesystem-only path support is missing'
    module = importlib.import_module('recheck.paths')
    assert module.filesystem_path(tmp_path / 'input') == tmp_path / 'input'


@pytest.mark.skipif(os.name != 'nt', reason='Windows extended path normalization')
def test_long_atomic_destination_resolves_parent_segments_before_prefixing(tmp_path, monkeypatch):
    destination = tmp_path / ('normalized-folder-' * 9) / ('nested-folder-' * 7)
    with legacy_windows_io(monkeypatch):
        atomic_json(destination / 'unused' / '..' / 'result.json', {'value': 42})
    assert json.loads((destination / 'result.json').read_text(encoding='utf-8')) == {'value': 42}
    assert not (destination / 'unused').exists()
