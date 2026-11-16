# Third-party components and redistribution status

Recheck's original material is covered by LICENSE. Third-party code remains
under its upstream terms; Recheck's rights notice does not replace or restrict
those terms. This file identifies major components and review boundaries. It
is not a complete license bundle or a declaration that binary redistribution
requirements have been met.

| Component | Identity/source | Upstream licensing reference |
| --- | --- | --- |
| CPython | Build/runtime version recorded with each artifact | [Python license and history](https://docs.python.org/3/license.html) |
| PySide6 Essentials / Shiboken6 / Qt | Current source lock: 6.12.0 | [Qt for Python licenses](https://doc.qt.io/qtforpython-6/licenses.html), [Qt licensing](https://doc.qt.io/qt-6/licensing.html) |
| PyInstaller | Current source lock: 6.22.3 | [License and bootloader exception](https://pyinstaller.org/en/stable/license.html) |
| REA | Managed image manifest: 6.7.0 | [Upstream repository](https://github.com/morluto/rea) |
| Ghidra | Managed image manifest: 12.1.4 | [Upstream license and bundled components](https://github.com/NationalSecurityAgency/ghidra/blob/master/LICENSE) |
| Eclipse Temurin / OpenJDK | Manifest Java 21.0.9+10 | [Adoptium legal materials](https://adoptium.net/about/legal/) |
| Node.js | Managed image manifest: 24.22.1 | [Upstream licenses](https://github.com/nodejs/node/blob/main/LICENSE) |
| Ollama | Managed image manifest: 0.40.16 | [MIT license](resources/docker/ollama/LICENSE), [upstream](https://github.com/ollama/ollama) |
| Ubuntu and system packages | Image base and package lists vary by image | Package copyright/license and corresponding source requirements must be reviewed individually |
| Python dependencies | Exact versions in requirements-lock.txt | Each distribution's original metadata and license materials apply |

The managed image manifest at resources/runtime/environment.json records image
and archive hashes. Model weights are not included in Git or release archives;
any separately downloaded model retains its own terms.

## Existing Windows archives

The public-readiness inspection found a notice-packaging regression: the three
1.2.x archives contain dependency notices but omit the earlier CPython, Qt,
PySide6/Shiboken6 notice bundle. The 1.1.0 archive has more license texts; its
complete corresponding-source/replacement/relinking readiness has not been
established either. Function tests and SHA-256 checks do not resolve these gaps.

Accordingly, existing executable archives, bundled images and their original
release records are retained in a separate private archive repository. They are
not copied into this public repository. Before a new
binary release, collect the exact component/license inventory, original notices,
and any required corresponding source, replacement/relinking materials and
instructions. Include reviewed materials in the actual distribution and verify
their presence. General upstream links alone are not a substitute.

See docs/PUBLICATION_SCOPE.md for the publication boundary. Historical source
tags and artifact digests remain unchanged in that private archive; new notices do not silently modify
or retroactively certify old archives.
