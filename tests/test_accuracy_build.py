"""Accuracy artifacts need the target compiler and the recorded binary format."""

import json
from pathlib import Path
import struct

import pytest

from scripts import evaluate_accuracy
from test_binary_admission import portable_executable


@pytest.fixture
def controlled_compilers(tmp_path, monkeypatch):
    sources = tmp_path / 'sources'
    sources.mkdir()
    (sources / 'many_functions.cpp').write_text('int main() { return 0; }', encoding='utf-8')
    output = tmp_path / 'build'
    output.mkdir()
    commands = []
    monkeypatch.setattr(evaluate_accuracy, 'ROOT', tmp_path)
    monkeypatch.setattr(evaluate_accuracy, 'SOURCES', sources)
    elf = bytearray(64)
    elf[:7] = b'\x7fELF\x02\x01\x01'
    struct.pack_into('<HH', elf, 16, 2, 62)

    def configure(*, cross_cpp, pe_format='PE'):
        installed = {
            'x86_64-w64-mingw32-gcc': 'cross-gcc',
            'x86_64-w64-mingw32-g++': 'cross-g++' if cross_cpp else None,
            'g++': 'host-g++',
        }
        monkeypatch.setattr(evaluate_accuracy.shutil, 'which', installed.get)

        def command(argv, receipt):
            commands.append(argv)
            receipt.append(dict(argv=argv, exit_code=0))
            if argv[:3] == ['docker', 'image', 'inspect']:
                return json.dumps([{'Id': 'sha256:' + '1' * 64}])
            if '--version' in argv:
                return 'controlled compiler version'
            if argv[0] == 'docker':
                target = output / 'elf' / Path(argv[-1]).name
                target.write_bytes(elf)
            else:
                target = tmp_path / argv[argv.index('-o') + 1]
                target.write_bytes(
                    portable_executable() if argv[0] == 'cross-g++' and pe_format == 'PE' else elf
                )
            return ''

        monkeypatch.setattr(evaluate_accuracy, 'command', command)
        return output, commands

    return configure


def test_missing_pe_cpp_compiler_is_unavailable_even_when_host_cpp_exists(controlled_compilers):
    output, commands = controlled_compilers(cross_cpp=False)

    evaluate_accuracy.build(output, 'compiler-image')

    assert not list((output / 'pe').iterdir())
    assert all(argv[0] != 'host-g++' for argv in commands)
    receipt = json.loads((output / 'build-receipt.json').read_text(encoding='utf-8'))
    assert receipt['pe_compilation']['cpp']['status'] == 'unavailable'
    assert len(list((output / 'elf').iterdir())) == 2


def test_cpp_pe_build_uses_cross_cpp_compiler_and_records_real_pe(controlled_compilers):
    output, commands = controlled_compilers(cross_cpp=True)

    evaluate_accuracy.build(output, 'compiler-image')

    assert len([argv for argv in commands if argv[0] == 'cross-g++' and '-o' in argv]) == 2
    assert all(argv[0] != 'host-g++' for argv in commands)
    assert len(list((output / 'pe').iterdir())) == 2
    assert all(path.read_bytes().startswith(b'MZ') for path in (output / 'pe').iterdir())


def test_pe_artifact_rejects_an_elf_named_exe(controlled_compilers):
    output, _ = controlled_compilers(cross_cpp=True, pe_format='ELF')

    with pytest.raises(ValueError, match='PE x64'):
        evaluate_accuracy.build(output, 'compiler-image')

    assert not (output / 'binary-identities.json').exists()
