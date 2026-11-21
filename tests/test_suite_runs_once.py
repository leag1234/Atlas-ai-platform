"""Each test runs once per suite, in the module that defines it.

A test module that binds a test case class of another test_* module at its top
level, by importing it or by subclassing it, makes discovery run the borrowed
tests again. Such a module defines load_tests, usually
own_tests.only_own(__name__), so that only its own tests run there.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent


CASES = {"TestCase", "IsolatedAsyncioTestCase"}


def _module(name: str) -> str:
    return name.removeprefix("tests.")


def _top_level(tree: ast.Module) -> list[ast.stmt]:
    """Statements that bind module names: the body and its if/try blocks."""
    found: list[ast.stmt] = []
    pending = list(tree.body)
    while pending:
        node = pending.pop(0)
        found.append(node)
        if isinstance(node, ast.If):
            pending.extend(node.body + node.orelse)
        elif isinstance(node, ast.Try):
            pending.extend(node.body + node.orelse + node.finalbody)
            for handler in node.handlers:
                pending.extend(handler.body)
    return found


class _Module:
    """Top-level classes, imports and load_tests of one test module."""

    def __init__(self, tree: ast.Module, modules: set[str]) -> None:
        self.classes: dict[str, tuple[list[ast.expr], bool]] = {}
        self.names: dict[str, tuple[str, str]] = {}  # local name -> (module, class)
        self.aliases: dict[str, str] = {}  # local name -> test module
        self.loads = False
        for node in _top_level(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if _module(node.module) in modules:
                    for alias in node.names:
                        local = alias.asname or alias.name
                        self.names[local] = (_module(node.module), alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if _module(alias.name) in modules:
                        self.aliases[alias.asname or alias.name] = _module(alias.name)
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                # getattr(importlib.import_module("tests.test_x"), "Class")
                texts = [
                    a.value
                    for a in ast.walk(node.value)
                    if isinstance(a, ast.Constant) and isinstance(a.value, str)
                ]
                if len(texts) == 2 and _module(texts[0]) in modules:
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            self.names[target.id] = (_module(texts[0]), texts[1])
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                own = any(
                    isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.name.startswith("test")
                    for item in node.body
                )
                self.classes[node.name] = (node.bases, own)
            if isinstance(node, ast.FunctionDef) and node.name == "load_tests":
                self.loads = True
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "load_tests" for t in node.targets
            ):
                self.loads = True


class _Suite:
    def __init__(self) -> None:
        names = {path.stem for path in TESTS.glob("test_*.py")}
        self.modules = {
            path.stem: _Module(ast.parse(path.read_text(encoding="utf-8")), names)
            for path in TESTS.glob("test_*.py")
        }

    def resolve(self, module: str, base: ast.expr) -> tuple[str, str] | None:
        """(module, class) of a base naming a class of a test module."""
        info = self.modules[module]
        if isinstance(base, ast.Name):
            if base.id in info.classes:
                return module, base.id
            return info.names.get(base.id)
        if isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name):
            source = info.aliases.get(base.value.id)
            if source is not None:
                return source, base.attr
        return None

    def runs_tests(self, module: str, name: str) -> bool:
        """Whether the class is a test case with at least one test method."""
        return self.is_case(module, name) and self.has_tests(module, name)

    def has_tests(
        self, module: str, name: str, seen: frozenset[str] = frozenset()
    ) -> bool:
        key = module + "." + name
        info = self.modules.get(module)
        if info is None or name not in info.classes or key in seen:
            return False
        bases, own = info.classes[name]
        if own:
            return True
        for base in bases:
            target = self.resolve(module, base)
            if target and self.has_tests(*target, seen | {key}):
                return True
        return False

    def is_case(
        self, module: str, name: str, seen: frozenset[str] = frozenset()
    ) -> bool:
        key = module + "." + name
        info = self.modules.get(module)
        if info is None or name not in info.classes or key in seen:
            return False
        for base in info.classes[name][0]:
            if ast.unparse(base).rsplit(".", 1)[-1] in CASES:
                return True
            target = self.resolve(module, base)
            if target and self.is_case(*target, seen | {key}):
                return True
        return False


class SuiteRunsOnceTests(unittest.TestCase):
    def test_borrowing_modules_define_load_tests(self) -> None:
        suite = _Suite()
        missing = []
        for name, info in sorted(suite.modules.items()):
            borrowed = any(
                source != name and suite.runs_tests(source, cls)
                for source, cls in info.names.values()
            ) or any(
                (target := suite.resolve(name, base)) is not None
                and target[0] != name
                and suite.runs_tests(*target)
                for bases, _ in info.classes.values()
                for base in bases
            )
            if borrowed and not info.loads:
                missing.append(name)
        self.assertEqual(missing, [], "add load_tests = own_tests.only_own(__name__)")

    def test_only_own_keeps_local_subclasses_and_drops_borrowed_tests(self) -> None:
        import test_project_isolation as isolation
        import test_project_work as work

        loader = unittest.TestLoader()
        suite = loader.loadTestsFromModule(work)
        names: list[tuple[str, str]] = []

        def walk(tests: unittest.TestSuite) -> None:
            for test in tests:
                if isinstance(test, unittest.TestSuite):
                    walk(test)
                else:
                    names.append((type(test).__name__, test.id().rsplit(".", 1)[-1]))

        walk(suite)
        classes = {cls for cls, _ in names}
        self.assertNotIn("ProjectIsolationTests", classes)
        self.assertIn("ProjectWorkTests", classes)
        self.assertIn("ProjectWorkHTTPTests", classes)
        borrowed = {
            name
            for name in dir(isolation.ProjectIsolationTests)
            if name.startswith("test_")
        }
        self.assertFalse(borrowed & {test for _, test in names})
        # The HTTP variant still runs every ProjectWorkTests test of this module.
        work_tests = {t for c, t in names if c == "ProjectWorkTests"}
        http_tests = {t for c, t in names if c == "ProjectWorkHTTPTests"}
        self.assertLessEqual(work_tests, http_tests)
        self.assertEqual(len(names), len(set(names)))
