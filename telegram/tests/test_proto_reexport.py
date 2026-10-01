"""
Regression test for #125: hannah_proto's own __init__ (in the published
package, since #60 moved Telegram off the git-submodule/local-codegen
pattern) patches every scope-split *_pb2 module's public names onto
hannah_pb2. This walks every *_pb2.py module in the installed hannah_proto
package and asserts nothing got left out of the patch — not just EventFilter.
Covers hannah.v1 (the N−1 path) and hannah.v2, which Telegram uses.
"""

import pkgutil

import pytest

import hannah_proto.v1
import hannah_proto.v2


def _scope_pb2_modules(package):
    for _, name, _ in pkgutil.iter_modules(package.__path__):
        if name.endswith("_pb2") and name != "hannah_pb2":
            yield name


@pytest.mark.parametrize("package", [hannah_proto.v1, hannah_proto.v2], ids=["hannah.v1", "hannah.v2"])
def test_every_scope_module_is_patched_onto_hannah_pb2(package):
    hannah_pb2 = __import__(f"{package.__name__}.hannah_pb2", fromlist=["_"])
    scope_modules = list(_scope_pb2_modules(package))
    assert scope_modules, "expected at least one scope-split *_pb2 module"

    missing = []
    for module_name in scope_modules:
        module = __import__(f"{package.__name__}.{module_name}", fromlist=["_"])
        for name in dir(module):
            if name.startswith("_"):
                continue
            if not hasattr(hannah_pb2, name):
                missing.append(f"{module_name}.{name}")

    assert not missing, f"not re-exported onto hannah_pb2: {missing}"
