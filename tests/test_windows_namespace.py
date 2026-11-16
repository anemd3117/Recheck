"""Local extended file paths must not be confused with network namespaces."""

import asyncio
import os
from pathlib import Path

import pytest

from recheck import setup_discovery as setup
from recheck.models import Settings
from recheck.process import run_process

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows path namespaces')


def extended(path):
    return Path('\\\\?\\' + str(path.absolute()))


def test_short_local_extended_batch_path_preserves_spaces_and_non_path_arguments(tmp_path):
    folder = tmp_path / '한글 공백'
    folder.mkdir()
    script = folder / 'launcher fixture.cmd'
    script.write_text('@echo off\r\necho ARG [%~1]\r\necho TEXT [%~2]\r\n', encoding='ascii')
    literal = '\\\\?\\C:\\a literal argument'
    result = asyncio.run(run_process([str(extended(script)), 'argument with spaces', literal]))
    assert result['returncode'] == 0, result
    assert 'ARG [argument with spaces]' in result['stdout']
    assert 'TEXT [' + literal + ']' in result['stdout']


def test_short_local_extended_batch_working_directory_is_usable(tmp_path):
    folder = tmp_path / 'working space'
    folder.mkdir()
    script = folder / 'cwd fixture.cmd'
    script.write_text('@echo off\r\nif exist marker.txt (echo FOUND) else (exit /b 9)\r\n', encoding='ascii')
    (folder / 'marker.txt').write_text('owned fixture')
    result = asyncio.run(run_process([str(extended(script))], cwd=extended(folder)))
    assert result['returncode'] == 0, result
    assert 'FOUND' in result['stdout']


def test_batch_path_too_long_for_cmd_is_rejected_before_attempt():
    path = '\\\\?\\C:\\' + '\\'.join(['owned-fixture-' + 'x' * 40] * 6) + '\\tool.cmd'
    with pytest.raises(ValueError, match='짧은 경로'):
        asyncio.run(run_process([path]))


def test_local_extended_installations_remain_discoverable(tmp_path, monkeypatch):
    g = extended(tmp_path / 'Tools/Ghidra')
    j = extended(tmp_path / 'Tools/JDK')
    (g / 'support').mkdir(parents=True)
    (g / 'Ghidra').mkdir()
    (g / 'support/analyzeHeadless.bat').write_text('fixture; not executed')
    (g / 'Ghidra/application.properties').write_text(
        'application.version=12.1\napplication.java.min=21\n', encoding='utf-8')
    (j / 'bin').mkdir(parents=True)
    (j / 'bin/java.exe').write_text('fixture; not executed')
    monkeypatch.setattr(setup, '_registry_locations', lambda: [])
    monkeypatch.setattr(setup.GhidraAdapter, 'discover', staticmethod(lambda: None))
    monkeypatch.setattr(setup.shutil, 'which', lambda name: None)
    for variable in ('GHIDRA_HOME', 'GHIDRA_INSTALL_DIR', 'JAVA_HOME', 'JAVA_HOME_OVERRIDE'):
        monkeypatch.delenv(variable, raising=False)

    found = setup.find_candidates(Settings(), search_roots=[extended(tmp_path)], max_depth=4)
    assert str(g) in found['ghidra']
    assert str(j) in found['java']
    assert setup._ghidra_properties(g)['application.java.min'] == '21'
    assert setup._java_executable(j) == j / 'bin/java.exe'


@pytest.mark.parametrize('namespace', [
    '\\\\unreachable-recheck-fixture\\share',
    '\\\\?\\UNC\\unreachable-recheck-fixture\\share',
    '\\\\.\\C:\\unreachable-recheck-fixture',
    '\\\\?\\GLOBALROOT\\Device\\HarddiskVolume1',
])
def test_network_and_other_device_namespaces_are_not_probed(namespace, monkeypatch):
    def forbidden_io(*args, **kwargs):
        raise AssertionError('Network/device file I/O must not be attempted')

    monkeypatch.setattr(Path, 'is_file', forbidden_io)
    assert setup._java_executable(Path(namespace)) is None
    assert setup._ghidra_properties(Path(namespace)) is None


def test_discovery_deduplicates_case_and_local_prefix_without_rewriting_first_value(tmp_path):
    first = extended(tmp_path / 'Ghidra')
    alternate = str(tmp_path / 'Ghidra').upper()
    assert setup._unique([first, alternate]) == [first]
