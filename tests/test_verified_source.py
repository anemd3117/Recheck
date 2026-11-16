"""Source identity must describe the very files that passed verification."""
import importlib
import json
import subprocess

import pytest


def test_tests_changing_runtime_source_cannot_receive_verified_identity(tmp_path):
    assert importlib.util.find_spec('scripts.verify_source'), 'source verification entrypoint missing'
    m = importlib.import_module('scripts.verify_source')
    (tmp_path / 'recheck').mkdir()
    path = tmp_path / 'recheck/__init__.py'
    path.write_text("__version__ = '1.2.0-prototype.2'\n")

    def run(command, **kwargs):
        path.write_text("__version__ = 'changed during tests'\n")
        return subprocess.CompletedProcess(command, 0)

    with pytest.raises(ValueError, match='changed'):
        m.verify(tmp_path, tmp_path / 'records', runner=run)
    assert not (tmp_path / 'RECHECK_SOURCE_IDENTITY.json').exists()


def test_failed_verification_preserves_existing_source_identity(tmp_path):
    assert importlib.util.find_spec('scripts.verify_source'), 'source verification entrypoint missing'
    m = importlib.import_module('scripts.verify_source')
    (tmp_path / 'recheck').mkdir()
    (tmp_path / 'recheck/__init__.py').write_text("__version__ = '1.2.0-prototype.2'\n")
    identity = tmp_path / 'RECHECK_SOURCE_IDENTITY.json'
    identity.write_text(json.dumps({'old': 'verified'}))

    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1)

    with pytest.raises(RuntimeError, match='failed'):
        m.verify(tmp_path, tmp_path / 'records', runner=run)
    assert json.loads(identity.read_text()) == {'old': 'verified'}


@pytest.mark.parametrize('launcher', ['Setup-Recheck.ps1', 'Run-Recheck.cmd'])
def test_changed_source_bootstrap_cannot_receive_verified_identity(tmp_path, launcher):
    m = importlib.import_module('scripts.verify_source')
    (tmp_path / 'recheck').mkdir()
    (tmp_path / 'resources').mkdir()
    (tmp_path / 'recheck/__init__.py').write_text("__version__ = '1.2.2-prototype.1'\n")
    (tmp_path / 'pyproject.toml').write_text('[project]\nversion="1.2.2a1"\n')
    (tmp_path / 'resources/version_info.txt').write_text('filevers=(1,2,2,1)')
    bootstrap = tmp_path / launcher
    bootstrap.write_text('original reviewed bootstrap')

    def run(command, **kwargs):
        bootstrap.write_text('changed during tests')
        return subprocess.CompletedProcess(command, 0)

    with pytest.raises(ValueError, match='changed'):
        m.verify(tmp_path, tmp_path / 'records', runner=run)
    assert not (tmp_path / 'RECHECK_SOURCE_IDENTITY.json').exists()
