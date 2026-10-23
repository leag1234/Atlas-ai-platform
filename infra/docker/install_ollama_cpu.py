"""Extract CPU files from the official pinned release without retaining GPU libraries.

Source: https://github.com/ollama/ollama/releases/tag/v0.13.5, fetched 2026-10-22.
The CPU build target is optional; the default image retains GPU support.
"""

import hashlib
from pathlib import Path, PurePosixPath
import platform
import shutil
import tarfile
from urllib.request import urlopen

if platform.machine() != "x86_64":
    raise SystemExit("The optional CPU build target currently supports x86_64 only.")
url = (
    "https://github.com/ollama/ollama/releases/download/v0.13.5/ollama-linux-amd64.tgz"
)
expected = "41fb93ff8be35e4d2d22bafd1c42b487efb15b766076d976766bd1ee4db3f8e2"
digest = hashlib.sha256()
with urlopen(url, timeout=120) as response:

    class Reader:
        def read(self, size: int) -> bytes:
            value = response.read(size)
            digest.update(value)
            return value

    reader = Reader()
    with tarfile.open(fileobj=reader, mode="r|gz") as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts:
                raise ValueError("invalid_archive_path")
            selected = str(name) == "bin/ollama" or (
                str(name).startswith("lib/ollama/")
                and not any(
                    part.startswith(("cuda", "rocm", "vulkan")) for part in name.parts
                )
            )
            if not selected or not member.isfile():
                continue
            target = Path("/usr") / str(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            with (
                archive.extractfile(member) as source,
                target.open("wb") as destination,
            ):
                shutil.copyfileobj(source, destination)
            target.chmod(0o755 if str(name) == "bin/ollama" else 0o644)
    while reader.read(1024 * 1024):
        pass
if digest.hexdigest() != expected:
    raise ValueError("ollama_release_checksum_mismatch")
