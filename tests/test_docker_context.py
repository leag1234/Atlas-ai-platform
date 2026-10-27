"""Every COPY source in the Dockerfiles must be inside the .dockerignore build context."""

from pathlib import Path
import fnmatch
import re
import unittest


def included(path: str, rules: list[str]) -> bool:
    """Docker semantics: rules apply in order and the last matching rule wins."""
    keep = True
    for rule in rules:
        negate = rule.startswith("!")
        pattern = (rule[1:] if negate else rule).rstrip("/")
        if fnmatch.fnmatch(path, pattern) or path.startswith(pattern + "/"):
            keep = negate
    return keep


class DockerContextTests(unittest.TestCase):
    def test_copy_sources_are_in_the_build_context(self) -> None:
        rules = [
            line.strip()
            for line in Path(".dockerignore").read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
        missing = []
        for dockerfile in Path("infra/docker").glob("Dockerfile*"):
            for line in dockerfile.read_text().splitlines():
                match = re.match(
                    r"\s*COPY\s+(?!--from)(?:--\S+\s+)*(.+)\s+\S+\s*$", line
                )
                if not match:
                    continue
                for source in match.group(1).split():
                    for path in (
                        Path(".").glob(source.rstrip("/"))
                        if any(c in source for c in "*?[")
                        else [Path(source.rstrip("/"))]
                    ):
                        files = (
                            [path]
                            if path.is_file()
                            else [p for p in path.rglob("*") if p.is_file()]
                        )
                        for file in files:
                            if not included(file.as_posix(), rules):
                                missing.append(f"{dockerfile.name}: {file.as_posix()}")
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
