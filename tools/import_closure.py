"""For every test file, which sqlglot/tests modules does importing it load?

Run in the scorer image from the checkout root. Prints JSON {test_file: [modules]}.
Each file is imported in a fresh interpreter so the answer does not depend on order.
"""

import glob
import json
import os
import subprocess
import sys

CODE = r"""
import importlib, json, sys
importlib.import_module(sys.argv[1])
print(json.dumps(sorted(m for m in sys.modules if m == 'sqlglot' or m.startswith(('sqlglot.', 'tests')))))
"""

out = {}
files = sorted(glob.glob("tests/test_*.py") + glob.glob("tests/dialects/test_*.py"))
for f in files:
    mod = f[:-3].replace("/", ".")
    r = subprocess.run([sys.executable, "-c", CODE, mod], capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": os.getcwd()})
    if r.returncode != 0:
        print(f"{f}: {r.stderr[-500:]}", file=sys.stderr)
        continue
    out[f] = json.loads(r.stdout.strip().splitlines()[-1])
print(json.dumps(out))
