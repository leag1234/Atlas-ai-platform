"""Run each test once: in the module that defines it.

Many test modules borrow a fixture class from another test module, such as
test_project_isolation.ProjectIsolationTests (a real PostgreSQL database and
signed identities) or test_project_work.ProjectWorkTests, by subclassing or
importing it. Default discovery then runs every borrowed test again in each
borrowing module, once for the imported class and once per subclass; the
isolation tests ran eighteen times per suite.

A module that sets ``load_tests = own_tests.only_own(__name__)`` runs every
test case class it defines, with the tests defined in this module or in a
helper module, and leaves tests defined in another test_* module to that
module, where discovery runs them unchanged.
"""

from __future__ import annotations

import sys
import unittest
from collections.abc import Callable

LoadTests = Callable[
    [unittest.TestLoader, unittest.TestSuite, str | None], unittest.TestSuite
]


def _test_module(name: str) -> bool:
    return name.rsplit(".", 1)[-1].startswith("test_")


def only_own(module_name: str) -> LoadTests:
    def load_tests(
        loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None
    ) -> unittest.TestSuite:
        module = sys.modules[module_name]
        suite = unittest.TestSuite()
        for value in list(vars(module).values()):
            if not (
                isinstance(value, type)
                and issubclass(value, unittest.TestCase)
                and value.__module__ == module_name
            ):
                continue
            for name in loader.getTestCaseNames(value):
                owner = next(k for k in value.__mro__ if name in vars(k))
                if owner.__module__ == module_name or not _test_module(
                    owner.__module__
                ):
                    suite.addTest(value(name))
        return suite

    return load_tests
