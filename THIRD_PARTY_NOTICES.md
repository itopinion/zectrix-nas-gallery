# Third-party software

The application source is MIT licensed. Built releases use these components:

| Component | Role | License / upstream |
| --- | --- | --- |
| Python | Application runtime, standard library | Python Software Foundation License; <https://www.python.org/psf/license/> |
| Pillow | Image decoding, cropping and date text | HPND and component licenses; <https://github.com/python-pillow/Pillow/blob/main/LICENSE> |
| Aileron Regular | Pillow's embedded default date-stamp font | No Rights Reserved; [author's permission](https://dotcolon.net/fonts/aileron/), [local notice](licenses/Aileron-NOTICE.txt) |
| PyInstaller | Self-contained application packaging | GPL with the PyInstaller exception permitting bundled applications; <https://pyinstaller.org/en/stable/license.html> |
| Swift runtime | Native helper runtime | Apache 2.0 with Runtime Library Exception; <https://www.swift.org/LICENSE.txt> |

Pillow's default scalable font is Aileron. Its author's permission is recorded
separately in `licenses/Aileron-NOTICE.txt`; the font is not relicensed by the
application's MIT license. The build also collects Pillow's full upstream
license rather than replacing it with this summary. The PyInstaller license
copy includes the bootloader exception; the Python runtime's complete
`LICENSE.txt` is also collected.
These notices and exact build versions are embedded under `licenses/` inside
the self-contained executable.

Apple Vision, ImageIO, CoreGraphics, Foundation and system fonts are provided by
macOS. They are accessed on the user's Mac and are not redistributed by this
source repository. The installer is for Apple Silicon macOS.

The application communicates with the separately installed ZECTRIX Hub Runtime.
This repository does not bundle a device SDK, device firmware, speech models,
private fonts, user photos, photo indexes, user settings, cache databases or
device identifiers.

Review the collected upstream license files when changing the build environment
or distributing a modified binary. The collection step fails if a required
upstream license is missing.
