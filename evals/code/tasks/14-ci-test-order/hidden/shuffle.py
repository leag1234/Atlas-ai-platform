import os
import random
import sys
import unittest

sys.path.insert(0, os.getcwd())


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


loader = unittest.TestLoader()
cases = list(flatten(loader.loadTestsFromNames(["tests.test_aliases", "tests.test_tags"])))
for seed in range(12):
    order = cases[:]
    random.Random(seed).shuffle(order)
    result = unittest.TextTestRunner(stream=open("/dev/null", "w"), verbosity=0).run(unittest.TestSuite(order))
    if not result.wasSuccessful():
        print("order-dependent failure with seed", seed, [str(t) for t in order])
        sys.exit(1)
