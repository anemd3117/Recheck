# Standalone Recheck build; no Rebuild or target-worker dependency.
from pathlib import Path
import hashlib
import json
import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata
from scripts.collect_distribution_notices import reviewed_qt_binary

root = Path(SPECPATH)
identity_path = Path(os.environ.get('RECHECK_BUILD_IDENTITY', ''))
if not identity_path.is_file():
    raise RuntimeError('RECHECK_BUILD_IDENTITY must name the verified standalone source identity.')
identity = json.loads(identity_path.read_text(encoding='utf-8'))['runtime_identity']
for name, expected in identity.items():
    candidate = (root / name).resolve(strict=True)
    if (
        not candidate.is_relative_to(root.resolve())
        or hashlib.sha256(candidate.read_bytes()).hexdigest() != expected
    ):
        raise RuntimeError('Verified Recheck source changed before packaging: ' + name)
datas = [
    (str(root / 'resources'), 'resources'),
    (str(root / 'requirements-lock.txt'), '.'),
    (str(identity_path), '.'),
]
for package in ('jsonschema', 'jsonschema_specifications', 'referencing', 'cryptography'):
    datas += collect_data_files(package)
for distribution in (
    'pydantic',
    'PyJWT',
    'cryptography',
    'jsonschema',
    'jsonschema-specifications',
    'referencing',
    'rpds-py',
):
    datas += copy_metadata(distribution)
hidden = [
    'recheck.cli',
    'recheck.agent',
    'recheck.provider_ui',
    'jwt',
    'cryptography.hazmat.bindings._rust',
    'jsonschema_specifications',
    'referencing.jsonschema',
]
hidden += collect_submodules('ai_providers')
a = Analysis(
    [str(root / 'launch.py')],
    pathex=[str(root)],
    binaries=[],
    datas=datas,
    hiddenimports=sorted(set(hidden)),
    excludes=[
        'PySide6.QtWebEngineCore',
        'PySide6.QtWebEngineWidgets',
        'PySide6.QtQml',
        'PySide6.QtQuick',
        'PySide6.QtNetwork',
        'tkinter',
    ],
    noarchive=False,
)
# Widgets use the Windows raster backend. Do not pick up the optional Mesa
# software renderer or host applications' OpenSSL DLLs through unused QtNetwork.
a.binaries = [row for row in a.binaries if reviewed_qt_binary(row[0])]
a.datas = [row for row in a.datas if reviewed_qt_binary(row[0])]
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Recheck',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    version=str(root / 'resources/version_info.txt'),
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='Recheck')
