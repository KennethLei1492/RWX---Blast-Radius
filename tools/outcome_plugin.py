"""pytest plugin: write the nodeids that passed cleanly (no failed phase, no failed subtest)."""

import json
import os

_bad = set()
_seen = set()


def pytest_runtest_logreport(report):
    _seen.add(report.nodeid)
    if report.failed:
        _bad.add(report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    out = os.environ.get("OUTCOME_OUT")
    if out:
        with open(out, "w") as f:
            json.dump(sorted(_seen - _bad), f)
