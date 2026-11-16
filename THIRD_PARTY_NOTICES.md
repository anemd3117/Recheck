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

## New portable Windows distribution

The public portable-build path now collects actual notices and corresponding
library sources into each new staging folder. It does not reuse the old private
archives. `scripts/package_verified.py` defaults to an application-only payload:
Recheck.exe, its Python/Qt runtime, selected guides, license texts and library
source archives. Offline container images and model weights are omitted.

The exact Qt 6.12.0 qtbase and qtsvg archives and PySide/Shiboken 6.12.0 source
archive are delivered in `third-party-sources/`, not merely linked. Their official
URLs, versions, sizes and SHA-256 values are pinned in
`resources/distribution/notice-sources.json`. The collector also preserves their
LICENSES, copyright/notice/attribution records and third-party README notices.
The distributed LGPL option includes the LGPLv3 and GPLv3 texts. Original
CPython and installed-package notices and version-specific CPython/OpenSSL
materials are collected under `licenses/`.

Only Qt Core/Gui/Widgets, SVG support and the selected qtbase/qtsvg plugins are
admitted. Unused QtNetwork, Mesa software OpenGL, image-format plugins from
other Qt modules and unneeded translation bundles are excluded. Each staged
license/source file is inventoried and checked again before publication.
`licenses/manifest.json` records the actual packaged material without build-PC
paths. Changes or missing source archives fail the build/publication checks.

The DLLs and Python extension modules remain replaceable in the onedir payload.
`licenses/REPLACEMENT.md` contains concrete paths and rebuilding/replacement
instructions, also available as [docs/BINARY_DISTRIBUTION.md](docs/BINARY_DISTRIBUTION.md).
The application LICENSE expressly permits reversing the application solely to
debug modifications to its LGPL libraries. No signing lock prevents running
compatible replacements. These materials cover the portable application, not
the separately prepared container images or independently installed tools.
