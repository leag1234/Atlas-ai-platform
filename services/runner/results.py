"""Bounded untrusted summaries and test report counts."""

import json
import re
from typing import Any
from xml.etree import ElementTree


def report(content: bytes, name: str) -> dict[str, Any]:
    if len(content) > 1024 * 1024:
        return dict(warning="report_size_limit")
    if name.endswith(".xml"):
        if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
            return dict(warning="unsafe_report")
        try:
            root = ElementTree.fromstring(content)
        except ElementTree.ParseError:
            return dict(warning="invalid_report")
        cases = list(root.iter("testcase"))
        failures = [
            c
            for c in cases
            if c.find("failure") is not None or c.find("error") is not None
        ]
        return dict(
            tests=len(cases),
            failed=len(failures),
            skipped=sum(c.find("skipped") is not None for c in cases),
            first_failures=[c.attrib.get("name", "")[:200] for c in failures[:5]],
        )
    text = content.decode("utf-8", errors="replace")
    lines = re.findall(r"^(not ok|ok)\b(.*)$", text, re.MULTILINE)
    return dict(
        tests=len(lines),
        failed=sum(a == "not ok" for a, _ in lines),
        first_failures=[b.strip()[:200] for a, b in lines if a == "not ok"][:5],
    )


def bounded(value: dict[str, Any]) -> dict[str, Any]:
    result = dict(value, trust="untrusted")
    result.pop("approval", None)
    # File metadata is useful; opaque storage/implementation fields are not.
    for key in ("files",):
        if key in result:
            result[key] = [
                {
                    k: f[k]
                    for k in (
                        "id",
                        "version_id",
                        "relative_path",
                        "mime",
                        "size_bytes",
                        "sha256",
                    )
                    if k in f
                }
                for f in result[key]
            ]
    result.pop("log_file", None)
    if len(json.dumps(result, ensure_ascii=False).encode()) > 8192:
        result["output"] = (
            str(result.get("output", ""))
            .encode()[-2048:]
            .decode("utf-8", errors="replace")
        )
    if len(json.dumps(result, ensure_ascii=False).encode()) > 8192:
        result.pop("output", None)
        result["files"] = [
            dict(id=f["id"], version_id=f["version_id"])
            for f in result.get("files", [])
        ]
    if len(json.dumps(result, ensure_ascii=False).encode()) > 8192:
        raise ValueError("remote_summary_limit")
    return result
