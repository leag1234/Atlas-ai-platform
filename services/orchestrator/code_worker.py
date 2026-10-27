"""Standard-library worker sent over stdin/argv to a credential-free container."""

import base64
import difflib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import resource
import selectors
import signal
import stat
import subprocess
import tarfile
import time
from typing import Any
import zipfile

MAX_BYTES = 200 * 1024 * 1024
MAX_FILES = 20000
FILE_CAP = 256 * 1024
OUTPUT_CAP = 32768


def target(root: Path, name: str, *, archive_path: bool = False) -> Path:
    path = PurePosixPath(name)
    if path.is_absolute() and not archive_path and ".." not in path.parts:
        try:
            path = path.relative_to("/workspace")
            name = str(path)
        except ValueError:
            raise ValueError("absolute_path_must_be_inside_workspace") from None
    if path.is_absolute() or ".." in path.parts or "\\" in name or "\x00" in name:
        raise ValueError("path_must_be_relative_and_inside_workspace")
    result = root / name
    if not result.resolve().is_relative_to(root.resolve()):
        raise ValueError("path_leaves_workspace")
    if any(p.is_symlink() for p in [result, *result.parents] if p != root.parent):
        raise ValueError("symlinks_are_not_supported")
    return result


def extract(raw: bytes, root: Path) -> int:
    """Validate all entries before extracting; never trust archive extract methods."""
    entries: list[tuple[str, bytes, int]] = []
    total = 0
    count = 0

    def add(name: str, size: int, mode: int, stream: Any, directory: bool) -> None:
        nonlocal total, count
        target(root, name, archive_path=True)
        if ".git" in PurePosixPath(name).parts:
            raise ValueError("archive_git_metadata_rejected")
        count += 1
        total += size
        if total > MAX_BYTES or count > MAX_FILES:
            raise ValueError(
                f"Archive expanded bytes {total}; limit {MAX_BYTES}; entries {count}; limit {MAX_FILES}"
            )
        if directory:
            entries.append((name, b"", -1))
            return
        content = stream.read(size + 1)
        if len(content) != size:
            raise ValueError("archive_size_mismatch")
        entries.append((name, content, mode))

    if zipfile.is_zipfile(io.BytesIO(raw)):
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            for entry in archive.infolist():
                mode = entry.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise ValueError("archive_links_or_special_files_rejected")
                with archive.open(entry) as stream:
                    add(entry.filename, entry.file_size, mode, stream, entry.is_dir())
    else:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as tar:
            for member in tar:
                if not (member.isfile() or member.isdir()):
                    raise ValueError("archive_links_or_special_files_rejected")
                member_stream = tar.extractfile(member) if member.isfile() else None
                add(
                    member.name, member.size, member.mode, member_stream, member.isdir()
                )
    if any(root.iterdir()):
        raise ValueError("workspace_already_initialized")
    for name, content, mode in entries:
        path = target(root, name)
        if mode == -1:
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            path.chmod(0o755 if mode & 0o111 else 0o644)
    return count


def git(root: Path, *args: str, raw: bool = False) -> str:
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *args],
        cwd=root,
        capture_output=True,
        timeout=30,
        text=True,
    )
    if result.returncode:
        raise ValueError("workspace_git_operation_failed")
    return result.stdout if raw else result.stdout.strip()


def read_text(path: Path) -> str:
    if not path.is_file() or path.stat().st_size > FILE_CAP:
        raise ValueError(f"File must be regular and at most {FILE_CAP} bytes")
    return path.read_text()


def workspace_digest(root: Path) -> str:
    """Hash workspace bytes/modes without trusting a shell-writable Git index."""
    import hashlib

    digest = hashlib.sha256()
    count = size = 0
    for parent, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(
            d for d in directories if d not in {".git", "__pycache__", ".pytest_cache"}
        )
        for name in sorted([*directories, *files]):
            path = Path(parent) / name
            mode = path.lstat().st_mode
            count += 1
            digest.update(str(path.relative_to(root)).encode())
            digest.update(str(mode).encode())
            if stat.S_ISREG(mode):
                size += path.stat().st_size
                if size > MAX_BYTES or count > MAX_FILES:
                    raise ValueError("verification_workspace_limit")
                with path.open("rb") as stream:
                    while chunk := stream.read(65536):
                        digest.update(chunk)
            elif stat.S_ISLNK(mode):
                digest.update(os.readlink(path).encode())
    return digest.hexdigest()


def command(root: Path, text: str, timeout: float, memory_mb: int) -> dict[str, object]:
    before = workspace_digest(root)
    started = time.monotonic()
    output = bytearray()
    truncated = False
    timed_out = False

    def limits() -> None:
        resource.setrlimit(resource.RLIMIT_AS, (memory_mb * 1024**2,) * 2)
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_BYTES,) * 2)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    process = subprocess.Popen(
        ["/bin/sh", "-c", text],
        cwd=root,
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": "/tmp",
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        preexec_fn=limits,
    )
    assert process.stdout is not None
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        try:
            while selector.get_map():
                if time.monotonic() - started >= timeout:
                    timed_out = True
                    break
                for key, _ in selector.select(min(0.1, timeout)):
                    chunk = os.read(key.fd, 16384)
                    if not chunk:
                        selector.unregister(key.fd)
                        continue
                    remaining = OUTPUT_CAP - len(output)
                    output.extend(chunk[:remaining])
                    truncated |= len(chunk) > remaining
        finally:
            # Kill descendants as well, even after a shell exits successfully.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            process.stdout.close()
    return {
        "workspace_changed": before != workspace_digest(root),
        "exit_code": process.returncode,
        "output": output.decode("utf-8", "replace"),
        "truncated": truncated,
        "timed_out": timed_out,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "timeout_seconds": timeout,
        "memory_mb": memory_mb,
        "output_limit_bytes": OUTPUT_CAP,
    }


def perform(root: Path, action: str, args: dict[str, Any]) -> dict[str, Any]:
    if action in {"read_document", "convert_document", "compare_documents"}:
        return document_tool(root, action, args)
    if action == "import":
        count = extract(base64.b64decode(args["archive"], validate=True), root)
        git(root, "init")
        git(root, "add", "--force", "--all")
        git(
            root,
            "-c",
            "user.name=Atlas",
            "-c",
            "user.email=atlas@localhost",
            "commit",
            "--allow-empty",
            "-m",
            "Imported baseline",
        )
        return {"baseline": git(root, "rev-parse", "HEAD"), "entries": count}
    if action == "list_dir":
        directory = target(root, args.get("path", "."))
        names = sorted(
            p.name + ("/" if p.is_dir() else "") for p in directory.iterdir()
        )
        return {"entries": names[:500], "truncated": len(names) > 500}
    if action == "read_file":
        text = read_text(target(root, args["path"]))
        lines = text.splitlines()
        start, end = args.get("start_line", 1), args.get("end_line", 200)
        if not 1 <= start <= end or end - start > 1000:
            raise ValueError("line_range_invalid_or_exceeds_1001_lines")
        output = "\n".join(
            f"{n}: {lines[n - 1]}" for n in range(start, min(end, len(lines)) + 1)
        )
        return {
            "output": output[:OUTPUT_CAP],
            "truncated": len(output) > OUTPUT_CAP,
            "total_lines": len(lines),
        }
    if action == "search":
        needle = args["pattern"]
        results: list[str] = []
        size = 0
        scanned = 0
        for parent, directories, files in os.walk(target(root, args.get("path", "."))):
            directories[:] = [
                d
                for d in directories
                if d != ".git" and not (Path(parent) / d).is_symlink()
            ]
            for name in files:
                scanned += 1
                if scanned > MAX_FILES:
                    return {"matches": results, "truncated": True}
                path = Path(parent) / name
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or path.stat().st_size > FILE_CAP
                ):
                    continue
                try:
                    lines = path.read_text().splitlines()
                except UnicodeDecodeError:
                    continue
                for number, line in enumerate(lines, 1):
                    if needle in line:
                        row = f"{path.relative_to(root)}:{number}:{line}"[:2000]
                        size += len(row)
                        if size > OUTPUT_CAP or len(results) >= 200:
                            return {"matches": results, "truncated": True}
                        results.append(row)
        return {"matches": results, "truncated": False}
    if action in {"edit_file", "write_file"}:
        path = target(root, args["path"])
        if ".git" in path.relative_to(root).parts:
            raise ValueError("git_metadata_edit_rejected")
        old = read_text(path) if path.exists() else ""
        if action == "edit_file":
            before = args["old_text"]
            if not before or old.count(before) != 1:
                raise ValueError("old_text_must_exist_exactly_once")
            updated = old.replace(before, args["new_text"], 1)
        else:
            updated = args["content"]
        if len(updated.encode()) > FILE_CAP:
            raise ValueError(f"File bytes {len(updated.encode())}; limit {FILE_CAP}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(updated)
        diff = "".join(
            difflib.unified_diff(
                old.splitlines(True),
                updated.splitlines(True),
                fromfile=args["path"],
                tofile=args["path"],
            )
        )
        return {"diff": diff[:8000], "truncated": len(diff) > 8000}
    if action == "run_command":
        return command(
            root,
            args["command"],
            min(120, max(0.01, float(args.get("timeout_seconds", 30)))),
            min(512, max(32, int(args.get("memory_mb", 256)))),
        )
    if action == "export":
        baseline = args["baseline"]
        if not re.fullmatch(r"[a-f0-9]{40,64}", baseline):
            raise ValueError("invalid_baseline")
        git(root, "add", "--force", "-N", "--all")
        patch = git(
            root,
            "diff",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--binary",
            baseline,
            raw=True,
        )
        archive_bytes = io.BytesIO()
        size = 0
        count = 0
        with tarfile.open(
            fileobj=archive_bytes, mode="w:gz", dereference=False
        ) as archive:
            for parent, directories, files in os.walk(root, followlinks=False):
                for name in [*directories, *files]:
                    path = Path(parent) / name
                    mode = path.lstat().st_mode
                    if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                        raise ValueError("export_links_or_special_files_rejected")
                    size += path.stat().st_size if path.is_file() else 0
                    count += 1
                    if size > MAX_BYTES or count > MAX_FILES:
                        raise ValueError(
                            f"Export bytes {size}; limit {MAX_BYTES}; entries {count}; limit {MAX_FILES}"
                        )
                    archive.add(
                        path, arcname=str(path.relative_to(root)), recursive=False
                    )
        return {
            "archive": base64.b64encode(archive_bytes.getvalue()).decode(),
            "patch": patch or "\n",
        }
    raise ValueError("unknown_code_tool")


def document_tool(root: Path, action: str, args: dict[str, Any]) -> dict[str, Any]:
    from packages.attachment_reader import failure_reason

    try:
        if action == "compare_documents":
            left = document_read(args["left"])
            right = document_read(args["right"])
            changes = []
            left_segments, right_segments = left["segments"], right["segments"]
            matcher = difflib.SequenceMatcher(
                a=[
                    json.dumps(
                        {k: v for k, v in item.items() if k != "locator"},
                        sort_keys=True,
                    )
                    for item in left_segments
                ],
                b=[
                    json.dumps(
                        {k: v for k, v in item.items() if k != "locator"},
                        sort_keys=True,
                    )
                    for item in right_segments
                ],
                autojunk=False,
            )
            size = 0
            truncated = False
            for kind, a, b, c, d in matcher.get_opcodes():
                if kind == "equal":
                    continue
                change = dict(
                    kind=kind, left=left_segments[a:b], right=right_segments[c:d]
                )
                count = len(json.dumps(change, ensure_ascii=False))
                if size + count > 24000:
                    truncated = True
                    break
                size += count
                changes.append(change)
            return dict(
                left=dict(
                    file_id=args["left"]["file_id"],
                    version=args["left"]["version"],
                    coverage=left["coverage"],
                    next_cursor=left["next_cursor"],
                ),
                right=dict(
                    file_id=args["right"]["file_id"],
                    version=args["right"]["version"],
                    coverage=right["coverage"],
                    next_cursor=right["next_cursor"],
                ),
                changes=changes,
                complete=left["coverage"]["complete"]
                and right["coverage"]["complete"]
                and not truncated,
                truncated=truncated,
                warnings=list(dict.fromkeys(left["warnings"] + right["warnings"])),
            )
        if action == "read_document":
            return document_read(args)
        return document_convert(root, args)
    except Exception as exc:
        # Third-party parser messages can contain source bytes. Only our bounded
        # validation errors are returned verbatim; unexpected failures are typed.
        if type(exc) is ValueError and str(exc).startswith(
            (
                "Document ",
                "Package ",
                "Expanded document ",
                "Extraction ",
                "Located unit ",
                "Conversion ",
                "Unsupported ",
                "Invalid ",
                "Duplicate ",
                "Active ",
                "External ",
                "DTD ",
                "Macros ",
                "Encrypted ",
                "Cell ",
                "Reversed ",
                "Unknown document ",
            )
        ):
            return dict(error=str(exc))
        return dict(error=failure_reason(exc))


def document_bytes(args: dict[str, Any]) -> bytes:
    from packages.attachment_reader import MAX_BYTES as DOCUMENT_BYTES

    encoded = args["content"]
    if len(encoded) > (DOCUMENT_BYTES + 2) // 3 * 4:
        raise ValueError("Document encoded bytes exceed 16 MiB input limit")
    return base64.b64decode(encoded, validate=True)


def document_read(args: dict[str, Any]) -> dict[str, Any]:
    from packages.attachment_reader import read_structured

    result = read_structured(
        document_bytes(args),
        args["extension"],
        args.get("selector"),
        args.get("cursor"),
    )
    return dict(result, file_id=args["file_id"], version=args["version"])


def document_convert(root: Path, args: dict[str, Any]) -> dict[str, Any]:
    import csv
    import hashlib
    from uuid import uuid4
    from packages.attachment_reader import structured_units, select_units, cell_position

    extension, output = args["extension"], args["target"]
    supported = {
        ".pdf": {"text", "markdown"},
        ".docx": {"text", "pdf"},
        ".xlsx": {"csv", "pdf"},
    }
    if (
        output not in supported.get(extension, set())
        or args.get("options", {}).get("fidelity", "text") != "text"
    ):
        raise ValueError("Unsupported conversion fidelity or format")
    units, warnings = structured_units(document_bytes(args), extension)
    selected = select_units(units, args.get("selector", {}))
    if not selected:
        raise ValueError("Conversion contains no text units")
    if output == "csv":
        sheets = {item["locator"]["sheet"] for item in selected}
        if len(sheets) != 1:
            raise ValueError("Conversion to CSV requires exactly one selected sheet")
        cells = {cell_position(item["locator"]["cell"]): item for item in selected}
        rows = [position[0] for position in cells]
        columns = [position[1] for position in cells]
        minimum_row, maximum_row = min(rows), max(rows)
        minimum_column, maximum_column = min(columns), max(columns)
        area = (maximum_row - minimum_row + 1) * (maximum_column - minimum_column + 1)
        if area > 100000:
            raise ValueError(f"Conversion cells: {area}; limit 100000")
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        for row in range(minimum_row, maximum_row + 1):
            values = []
            for column in range(minimum_column, maximum_column + 1):
                item = cells.get((row, column), {})
                value = (
                    item.get("cached_value")
                    if item.get("formula") is not None
                    else item.get("text", "")
                )
                text = str(value) if value is not None else ""
                # Keep generated CSV inert when opened in spreadsheet software.
                if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(
                    ("\t", "\r", "\n")
                ):
                    text = "'" + text
                values.append(text)
            writer.writerow(values)
        raw = stream.getvalue().encode("utf-8")
        warnings.append(
            "CSV uses cached formula values; missing caches become empty cells; formula-like strings are escaped; origin is the minimum selected row and column"
        )
    elif output == "pdf":
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
        import textwrap

        writer_pdf = PdfWriter()
        lines: list[str] = []
        for unit in selected:
            text = (
                json.dumps(unit["locator"], ensure_ascii=True)
                + "\n"
                + str(unit["text"])
            )
            try:
                text.encode("cp1252")
            except UnicodeEncodeError:
                raise ValueError(
                    "Unsupported PDF text fidelity: characters require an unavailable embedded font"
                ) from None
            for line in text.splitlines():
                lines.extend(
                    textwrap.wrap(
                        line.expandtabs(4), width=85, replace_whitespace=False
                    )
                    or [""]
                )
        page_count = (len(lines) + 53) // 54
        if page_count > 1000:
            raise ValueError(f"Conversion pages: {page_count}; limit 1000")
        for offset in range(0, len(lines), 54):
            page = writer_pdf.add_blank_page(width=595, height=842)
            font = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                    NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
                }
            )
            page[NameObject("/Resources")] = DictionaryObject(
                {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
            )
            stream_object = DecodedStreamObject()
            commands = ["BT /F1 10 Tf 35 800 Td 14 TL"]
            commands.extend(
                "<" + line.encode("cp1252").hex() + "> Tj T*"
                for line in lines[offset : offset + 54]
            )
            commands.append("ET")
            stream_object.set_data("\n".join(commands).encode("ascii"))
            page[NameObject("/Contents")] = writer_pdf._add_object(stream_object)
        stream_bytes = io.BytesIO()
        writer_pdf.write(stream_bytes)
        raw = stream_bytes.getvalue()
        if len(PdfReader(io.BytesIO(raw), strict=True).pages) != page_count:
            raise ValueError("Conversion PDF validation failed")
        warnings.append(
            "PDF is a text reflow with locators; original layout, graphics and pagination are not preserved"
        )
    else:
        chunks = []
        for unit in selected:
            heading = json.dumps(unit["locator"], ensure_ascii=False)
            chunks.append(
                ("## " if output == "markdown" else "")
                + heading
                + "\n"
                + str(unit["text"])
            )
        raw = "\n\n".join(chunks).encode("utf-8")
        warnings.append("Text conversion excludes graphics and original layout")
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError(f"Conversion bytes: {len(raw)}; limit 16777216")
    total = count = 0
    for parent, directories, names in os.walk(root):
        directories[:] = [
            name
            for name in directories
            if name != ".git" and not (Path(parent) / name).is_symlink()
        ]
        for name in names:
            path = Path(parent) / name
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
                count += 1
    if total + len(raw) > MAX_BYTES or count + 1 > MAX_FILES:
        raise ValueError(
            f"Conversion workspace bytes: {total + len(raw)}; limit {MAX_BYTES}; entries: {count + 1}; limit {MAX_FILES}"
        )
    suffix = {"text": "txt", "markdown": "md", "csv": "csv", "pdf": "pdf"}[output]
    staged = str(uuid4())
    destination = target(root, "outputs/" + staged + "." + suffix)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as handle:
        handle.write(raw)
    return dict(
        staged_id=staged,
        path=str(destination.relative_to(root)),
        size_bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        mime={
            "text": "text/plain",
            "markdown": "text/markdown",
            "csv": "text/csv",
            "pdf": "application/pdf",
        }[output],
        source=dict(file_id=args["file_id"], version=args["version"]),
        coverage=dict(
            total_units=len(units),
            selected_units=len(selected),
            converted_units=len(selected),
            complete=True,
            scope="selected stored text units",
        ),
        validation=dict(readable=True, fidelity="text", published=False),
        warnings=warnings[:20],
    )


if __name__ == "__main__":
    payload = json.load(__import__("sys").stdin)
    try:
        result = perform(Path("/workspace"), payload["action"], payload["args"])
    except (
        ValueError,
        OSError,
        KeyError,
        TypeError,
        tarfile.TarError,
        zipfile.BadZipFile,
        subprocess.TimeoutExpired,
    ) as exc:
        result = {"error": str(exc)}
    print(json.dumps(result))
