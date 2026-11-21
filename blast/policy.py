"""When is running a commit's candidate tests cheaper than printing them?

Printing a selection S costs pack(S): its busiest worker plus 2 s a worker. Running the
verifiable part V costs CPU, and then only the tests that fail (plus the unverifiable part
U) are printed. A commit that breaks nothing then costs CPU + pack(U) instead of pack(S).
We do not know beforehand whether it breaks anything, so the expected saving is weighed:

    verify  iff  p_benign * (pack(S) - pack(U))  >  estimated CPU of running V

p_benign is the share of such commits that break nothing (0.3 for code changes, tuned on
the sample and synthetic sets; fixture edits almost always break their test).

Estimated CPU = SCALE * (fixed cost of the child + extra fixed cost of each test file in V,
from maps/verify_costs.json measured by tools/verify_calibrate.py) + CPU_PER_RECORDED *
(recorded seconds of V).
"""

import json
import os

# Process-pool tests (test_optimizer, test_executor) run with the pools inline and reproduce
# the labels; tests that spawn a Python subprocess are excluded by the caller instead.
UNVERIFIABLE_FILES: set = set()

SCALE = 0.5              # calibration machine -> scorer CPU (calibration ran ~2.5x slower)
CPU_PER_RECORDED = 1.2   # scorer CPU per recorded test second
DEFAULT_FILE_MS = 250    # calibration-machine ms for a file missing from the table
P_BENIGN_CODE = float(os.environ.get("BLAST_P_BENIGN", "0.3"))
P_BENIGN_DATA = 0.05

_costs = None


def costs(maps_dir):
    global _costs
    if _costs is None:
        try:
            with open(os.path.join(maps_dir, "verify_costs.json"), encoding="utf-8") as fh:
                _costs = json.load(fh)
        except (OSError, ValueError):
            _costs = {"common_ms": 150, "file_ms": {}}
    return _costs


def estimate_cpu_ms(V, ms, file_of, table):
    common = table["common_ms"]
    files = {file_of[t] for t in V}
    fixed = common + sum(max(0, table["file_ms"].get(f, DEFAULT_FILE_MS) - common) for f in files)
    return SCALE * fixed + CPU_PER_RECORDED * sum(ms[t] for t in V)


def p_benign(patch_text):
    for line in patch_text.split("\n"):
        if line.startswith("+++ ") and line.rstrip().endswith(".py"):
            return P_BENIGN_CODE
    return P_BENIGN_DATA


def decide(patch_text, S, V, U, ms, pack, file_of, table):
    """(verify?, estimated CPU ms)."""
    if not V:
        return False, 0
    est = estimate_cpu_ms(V, ms, file_of, table)
    gain = p_benign(patch_text) * (pack(S) - pack(U))
    return gain > est, est
