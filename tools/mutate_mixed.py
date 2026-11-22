"""Combine single-file labeled patches into mixed commits (a function-body edit plus a class-body
or module-level edit, in different files) to check the mixed path of the TPC-DS planning check.

    python tools/mutate_mixed.py OUT_DIR COUNT SEED BODY_PATCH_LIST IMPORT_PATCH_LIST

Each list file holds one patch path per line. Only used to label patches locally; nothing
derived from them ships.
"""
import os
import random
import re
import sys

out, count, seed = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
body = [l.strip() for l in open(sys.argv[4]) if l.strip()]
imp = [l.strip() for l in open(sys.argv[5]) if l.strip()]
rng = random.Random(seed)
os.makedirs(os.path.join(out, "commits"), exist_ok=True)


def files(p):
    return set(re.findall(r"^\+\+\+ b/(\S+)", open(p, encoding="utf-8").read(), re.M))


made = 0
tries = 0
while made < count and tries < 10000:
    tries += 1
    a, b = rng.choice(body), rng.choice(imp)
    if files(a) & files(b):
        continue
    text = open(a, encoding="utf-8").read().rstrip("\n") + "\n" + open(b, encoding="utf-8").read()
    name = f"x{seed:02d}{made:03d}"
    with open(os.path.join(out, "commits", name + ".patch"), "w", encoding="utf-8") as fh:
        fh.write(text)
    with open(os.path.join(out, "sources.txt"), "a", encoding="utf-8") as fh:
        fh.write(f"{name} {os.path.basename(a)} {os.path.basename(b)}\n")
    made += 1
print(f"wrote {made} mixed patches")
