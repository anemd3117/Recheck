import json
import zipfile

import pytest

from recheck import cli


def make_zip(path, value):
    with zipfile.ZipFile(path, 'w') as stream:
        stream.writestr('file.txt', value)
    return str(path)


def test_package_command_works_without_ai_settings_or_engine_and_writes_json(
    monkeypatch, tmp_path, capsys
):
    one = make_zip(tmp_path / 'original.jar', b'before')
    two = make_zip(tmp_path / 'changed.jar', b'after')
    output = tmp_path / 'out' / 'result.json'

    def forbidden(*args, **kwargs):
        pytest.fail('Package inventory must not initialize Engine or load AI settings')

    monkeypatch.setattr(cli, 'Engine', forbidden)
    monkeypatch.setattr(cli, 'load_settings', forbidden)
    assert cli.main(['--data-dir', str(tmp_path / 'no-data'), 'compare-package',
                     '--original', one, '--reimplementation', two,
                     '--json-output', str(output)]) == 0
    result = json.loads(output.read_text(encoding='utf-8'))
    assert result == json.loads(capsys.readouterr().out)
    assert result['scope'] == 'package_inventory'
    assert result['comparison']['changed'] == ['file.txt']
    assert not (tmp_path / 'no-data').exists()


def test_package_failure_returns_nonzero_and_preserves_inventory_details(tmp_path, capsys):
    invalid = tmp_path / 'invalid.zip'
    invalid.write_bytes(b'not a zip')
    try:
        code = cli.main(['compare-package', '--original', str(invalid),
                         '--reimplementation', str(invalid)])
    except SystemExit as exc:
        pytest.fail(f'Package subcommand is not implemented: {exc}')
    result = json.loads(capsys.readouterr().out)
    assert code == 2 and result['status'] == 'failed'
    assert result['code'] == 'package_invalid' and result['comparison'] is None
    assert result['original']['complete'] is False
