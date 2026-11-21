"""pytest plugin form of tools/cmp_trace.py: instrument sqlglot comparisons in this process.

    CMP_OUT=/out/x.json python -m pytest -p tools.cmp_plugin tests/test_x.py

Only whole-suite agreement is used (did an alternative operator ever give a different
result anywhere?), and that does not depend on which test first ran a cached computation,
so ordinary per-file processes are enough.
"""

import json
import os
import sys

from tools import cmp_trace as ct

if not any(isinstance(f, ct._Finder) for f in sys.meta_path):
    sys.meta_path.insert(0, ct._Finder())
    ct.tp_pools = __import__("tools.trace_plugin", fromlist=["_inline_process_pools"])
    ct.tp_pools._inline_process_pools()


def pytest_collection_finish(session):
    ct._phase["import"] = False


def pytest_sessionfinish(session, exitstatus):
    out = os.environ.get("CMP_OUT")
    if out:
        with open(out, "w") as fh:
            json.dump({"files": ct.FILES, "dis": sorted(ct.DIS), "import_dis": sorted(ct.IMPORT_DIS),
                       "eval": sorted(ct.EVAL)}, fh)
