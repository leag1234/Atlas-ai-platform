Segno 1.6.6, BSD-3-Clause, from the PyPI wheel (https://pypi.org/project/segno/1.6.6/).
Unmodified Python sources and LICENSE, 269124 bytes. Pure Python; no runtime download.
Chosen for bounded QR generation without a network service or native dependency.
Pillow is already a project dependency. qrcode was not installed and requires extra
packaging; writing a QR encoder ourselves would duplicate a mature implementation.
Only the encoder/matrix interface is used; upgrades require QR regression checks.
