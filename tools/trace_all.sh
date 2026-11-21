#!/bin/bash
# Run inside the scorer image with the pinned checkout at /sqlglot and this repo at /proj.
# Traces every test file in its own pytest process and writes /work/cov/<file>.json.
set -u
cd /sqlglot
OUTDIR=${OUTDIR:-/work/cov}
mkdir -p "$OUTDIR"
export OMP_NUM_THREADS=1 SKIP_INTEGRATION=0 PYTHONPATH=/sqlglot:/proj SQLGLOT_ROOT=/sqlglot
ls tests/test_*.py tests/dialects/test_*.py | xargs -P "${JOBS:-12}" -I{} sh -c '
  f={}; out="$0/$(echo $f | tr / _).json"
  SQLGLOT_TRACE_OUT="$out" python -m pytest -p tools.trace_plugin "$f" -q -p no:cacheprovider > "$out.log" 2>&1
  echo "$f $(tail -1 $out.log)"
' "$OUTDIR"
