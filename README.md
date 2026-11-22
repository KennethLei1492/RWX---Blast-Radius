# Blast Radius: test selection for sqlglot

For each patch to sqlglot (pinned at `eb11aec`), this entry prints the tests the patch can
break, packed onto at most 8 workers. It uses no network. It reads maps built once from the
pinned commit and does a few milliseconds of static analysis per patch; where that pays for
its CPU, it also imports the patched tree, plans queries on it, or runs a cheap selection.

## How it decides

**Maps (built offline from the pinned commit, committed under `maps/`).** Every test was
run alone in a fresh pytest process inside the scorer's image (`python:3.13-slim` at the
pinned digest, with `requirements.lock`) under a tracer (`tools/trace_plugin.py`) that
records, for that test:

* every line of sqlglot and of the tests that executed (`sys.monitoring`), including the
  work `test_optimizer` and `test_executor` send to process pools. During tracing those
  pools run inline, with the same pickle round trips, so `sqlglot.serde` is seen;
* every sqlglot class whose instances it created (with each class's MRO);
* every non-source file it opened or listed (fixtures), and whether it spawned a
  `python -c "import sqlglot"` subprocess.

One process per test matters: sqlglot builds dispatch tables and caches lazily, and in a
shared process only the first test to trigger that work would be credited with it.
Lines that run while modules are imported are kept separately, as is a static index of
where each name is read (in function bodies, class bodies, module level and def headers).

**Per patch (`blast/`).** Each changed file is turned into *impacts*:

| Change | Tests selected |
| --- | --- |
| fixture or other data file | tests that opened it (or listed its directory, if a file is added or removed) |
| `.py` edit that only touches comments or docstrings | none, unless a test read that file as text |
| statement inside a function | tests that executed that statement (or, for an inserted statement, its neighbours) |
| `def` header, decorator, default | tests that ran the function's body |
| body of a function that runs at import time | tests using whatever was built from it (its callers' classes and constants) |
| class-body attribute | tests that instantiated the class or a subclass, plus classes and constants derived from the attribute at import time. For an UPPERCASE setting read only by code that runs during tests (e.g. `TSQL.WEEK_OFFSET`), only those users that executed a read of it |
| new method or attribute in a class | users of the class (it may override something) |
| module-level constant | tests that executed a read of it, plus anything derived from it at import time |
| import, other module-level code, syntax error, deleted or renamed module | every test that uses the module or imports it, directly or through other modules |
| pytest/packaging configuration, `conftest.py` | everything |
| a comparison operator swapped (`==`/`!=`/`is`/`is not`/`<`/`<=`/`>`/`>=`) | the tests in whose own run the new operator ever gave a different result (`maps/cmpt.bin`, `tools/cmp_trace.py`) |
| `and` swapped with `or` | the tests in whose own run the swapped chain could have evaluated differently: it would need an operand the original skipped, skip a call the original made, or give a different result (`maps/cmpt.bin`, `tools/bool_trace.py`) |
| an existing class-body assignment (a Dialect flag, a Parser or Generator setting) | exactly the tests that read that (class, attribute), recorded per test by rewriting attribute loads at import (`maps/attr.bin`, `tools/attr_trace.py`); the broader class rule above still applies if it is read while importing, copied by another class body, renamed or new. A Tokenizer setting (or one of the three Dialect settings it takes) reaches tokenizing only through the `TokenizerCore` that `Tokenizer._init_core` builds, which stores each value in a field and reads it at one place (comment, number, string, heredoc scanning): its readers are the tests that read that field of a core built for that class, not every test that built a tokenizer |
| edits inside functions that leave each touched function's compiled code unchanged: reformatting, type hints (under `from __future__ import annotations`), renamed plain locals, comments, docstrings (`blast/equiv.py`) | none, unless a test read that file as text |
| a parameter default replaced by another simple token (`copy: bool = True` -> `False`) | the tests that entered that function with the parameter holding its default (`maps/dflt.bin`, `tools/defaults_trace.py`); defaults used while importing keep the def-header rule above until the import diff below shows that the import leaves the pinned state apart from the function itself, and then also come down to the tests that relied on them after the import (plus the tests that import sqlglot in a subprocess) |
| a `True`/`False` flipped inside an expression's `arg_types` | the tests that validated an instance of that class with that argument missing: the only reader of those values is validation (`maps/args.bin`, `tools/args_trace.py`) |

Each of these per-test maps comes from running every test alone in a forked child with the
pinned code instrumented; none is built from patches. Each was checked by applying real edits
of that kind and running the tests the map pruned (`tools/check_cmpt.py`,
`tools/check_one_test.py`, `tools/oracle_exp.py` + `tools/recheck.py`): no pruned test failed.

Opening a `.py` file only counts as a text dependency when a test reads it while running
(e.g. `test_typing` reading `sqlglot/typing/*.py`); reads made while importing modules are
already covered by line coverage.

**Import diff (`blast/imstate.py`, `Verifier.import_diff`).** Some changes act while sqlglot
is imported: a class-level table that a metaclass turns into other tables, a function that
runs at import, a decorator. The static rules above answer those with broad fallbacks, up to
"every test". When such a fallback is expensive, the selector instead imports the patched
tree in a forked child and fingerprints everything the import leaves behind (every module
global and class attribute, structurally, with functions identified by where they are
defined rather than by their bytecode) and compares it with the pinned import
(`maps/imstate.bin`, built from eight perturbed runs so that values whose order varies from
process to process are compared order-insensitively). The selection is then the tests that
run the changed lines plus the recorded readers (`maps/attr.bin`) of each class attribute
whose fingerprint differs, and the tests that run lines naming each changed module global.
If nothing differs, the change has no import-time effect at all. It is used only when every
differing key has a reader set and the result is cheaper. Checked against the oracle on 760
labeled patches: no known-failing test left out.

**Entries of class-level and module-level tables (`blast/keytab.py`, `maps/keys.bin`).** Much
of sqlglot's behaviour sits in dict tables assigned in class bodies: a Generator's `TRANSFORMS` and
`TYPE_MAPPING`, a Parser's `FUNCTIONS`, a Tokenizer's `KEYWORDS` and `NUMERIC_ESCAPES`. Every
test of a dialect loads those attributes, but a patch to one entry only matters to a test
that reads that entry. `tools/key_trace.py` reruns every test with each such table replaced,
at class creation, by a dict subclass with the same content that records, wherever the object
flows (a Tokenizer hands its tables to the tokenizer core; the generator copies `TRANSFORMS`
into its dispatch table, and the copy keeps recording against the table): which keys had
their value read, which were only tested for presence, whether only the length or only the
truth value was read, and whether the table was read as a whole (iteration, comparison,
merging, mutation). Class-level
sets (reserved keywords, sets of token types) are traced the same way: membership is a
presence read of that element, anything else reads the whole set, and since C code that is
handed a set (`set(x)`, `a.update(x)`, `a.issubset(x)`, `{*x}`, `a |= x`, the right operand
of `a | x`, `a - x`, `a == x`, `a <= x`) reads it without calling any of its methods, the
tracer's import rewriter marks those places in sqlglot and in the tests.
Lists are traced too (an element read by index records that index; a negative index, the
length; `bool(lst)`, only the truth value; anything else, the whole list), which matters for
settings such as a tokenizer's `HEX_STRINGS` that tests only test for emptiness.
Tables assigned at module level are traced as well, which matters most for the
type-annotation tables in `sqlglot/typing/*.py` that every dialect copies at import, and so
are the tables a metaclass builds from the class body and assigns inside a function
(`cls._KEYWORD_TRIE = new_trie(...)`, `klass.ESCAPED_SEQUENCES = {...}`, `_FORMAT_STRINGS`,
the merged `UNESCAPED_SEQUENCES`, time-format tries). A nested dict held only by its parent is
traced as part of the same table, by key path: the tokenizer walks its keyword trie one
character at a time (`trie.get(char)`, `0 in trie`), so a test depends on a changed keyword
only if the text it tokenized led it into that branch. A value is wrapped only when nothing
else holds it (checked by reference count), since a read through another reference would go
unrecorded; the tracer lists the nested dicts it wrapped, and the import diff enters exactly
those and treats anything else as one value. `tools/key_selftest.py` runs 216 ways of reading
a dict, a set, a list or a trie through the rewritten source and checks that each leaves a
sufficient record. 2,834 tables with 117,549 entries are traced this way; anything else keeps
the attribute-level rule.
When the import diff reports such a table as changed, the child also compares it entry by
entry with fingerprints of the pinned entries, and the table's readers become: the tests
that read the value of a changed entry, those that read the value or presence of an added or
removed key, those that read the length if it changed (or its truth value, if it became
empty or stopped being empty), and those that read it whole. On the
official sample set this takes `4d63` (one value inside Snowflake's `NUMERIC_ESCAPES`) from
273 tests to none; renaming one BigQuery keyword goes from 222 tests to 1. For a table edit the
import diff runs whenever the ordinary selection is not empty (otherwise only when it costs
3.5 s or more). Checked with `tools/recheck_isd.py` on the 986 labeled commits and on
table mutations made for this (`tools/mutate_tables.py`: one element of a set, or one key or
value of a dict, replaced by another of the same table), labeled by running every reader:
no known-failing test left out.

**Fixtures loaded in a class set-up (`blast/fixtures.py`, `maps/setupreads.json`).** The
executor tests load the TPC-H and TPC-DS query files in `setUpClass`
(`cls.tpcds_sqls = list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))`), so all 60 of
them open both files, and an edit to either selected all 60 at 25.5 s. A test of that class
can only be affected by the file through the attribute it is loaded into, or by the set-up
failing. So for a patch to such a fixture, the class's tests that never read the attribute are
dropped, provided the patched file still loads with the pinned loader (run on the patched
text). Which tests read the attribute comes from `tools/setup_trace.py`, which runs each test of
the class with the attribute replaced by a recording list (only `test_execute_tpcds` reads the
TPC-DS queries; `test_execute_tpch` and `test_optimized_tpch` the TPC-H ones); without that map,
line coverage of the lines that read it is used. The set-ups are found in the pinned test
source: an assignment from a call of a `tests.helpers` loader with a literal path, the file
named nowhere else in the module. For a TPC-DS edit, `test_execute_tpcds` then goes through
the planning check below, which reads the query file from the patched tree as the test does.

**Planning-only check for the dearest test (`blast/planfp.py`, `Verifier.plan_same`).**
`test_execute_tpcds` is recorded at 21 s, so a commit that selects it without breaking it
costs about 19 s. The test has two parts: for each TPC-DS query sqlglot parses, optimizes and
plans it (cheap), then the Python executor runs the plan over the fixture tables and the
result is compared with DuckDB's (nearly all of the time). `maps/tpcds.bin` holds the lines
of the pinned tree that run while the test class loads the fixture tables and in the second
part (from the entry of `PythonExecutor.execute` until the result has been compared),
recorded in two runs with different hash seeds; a structural fingerprint of everything the
second part is handed (the executor object, each query's plan, and the DuckDB SQL the
expected result comes from), identical in three runs with different hash seeds; and each
query's share of the second part's time.
For a commit that selects the test and only edits statements inside functions, or flips
parameter defaults, where none of the edited functions runs while the tables load or during
that second part, or edits the TPC-DS query file itself (of each entry the test uses the
metadata and the query, never the expected output), the selector runs the first part only on the patched tree, without loading
the tables (their rows are never read while planning, and no edited code fills them). If
every fingerprint equals the pinned one, the second part runs the same code on the same
input, so the test cannot change its outcome and is dropped. If a few queries differ (at
most six, and running them estimated at no more than 0.4 x the saving less 2 s), the probe
loads the tables and runs just those queries the way the test does, each result compared
with DuckDB's; the test is dropped when they all pass. That is sound because every other
query has the pinned plan and runs pinned code on pinned input, and the executor keeps no
state from one query to the next except a dispatch cache keyed by generator class; should an
edited function start while the differing queries execute, the test is kept. A mismatch
early in planning ends the probe in about 0.3 s; the whole probe is limited to about the
time dropping the test would save. If an edited function also runs while modules are
imported, the import diff above must first show that the import leaves exactly the pinned
state, apart from the functions whose parameter defaults the patch changes (the probe runs
the patched functions in any case). Other files in the same patch (a fixture, a docstring, another test file) are left out
of this when their own selection does not hold the test; the probe still runs on the whole
patch. Checked with `tools/check_tpcds.py` on 212 labeled commits that select the test
(code mutations, class and module table edits, TPC-DS query file edits): it was dropped in
49, none of which fails it (6 of them by running 1 to 6 differing queries), and kept in all
64 where it fails.

**Class-body and module-level edits (`maps/tpcds.bin` "xattrs"/"xnames", `selector.read_in_execution`).**
A change to a class attribute or a module constant (a parser table, a type set, a setting)
whose readers include the test used to keep it outright. Such statements run only at
import, so their whole effect is the import-state difference the import diff reports. The
map records, from one traced run of the test, every (class, attribute) read while the class
set-up loads the tables and while plans execute and results are compared
(`tools/build_tpcds.py xattr`, using `tools/attr_trace.py`'s rewriting), and every name loaded
on the lines those phases run. When no changed key is among them (a class attribute neither
recorded nor named there, a module global never named there), the execution part reads none
of the change, and the same planning probe decides the test.

**Edits that run while queries execute (`maps/tpcds.bin` "xlines", `selector.queries_executing`).**
An edited function that runs in the execution part used to keep the test outright. The map
also records, per query, the lines its execution runs (`tools/build_tpcds.py qexec`, all
queries in order in one process). When the edited statements run in the execution of only a
few queries, cheap enough to run within the probe's budget, and never in the class set-up or
on an execution line no single query is credited with, the probe runs those queries for real
(whatever their plans), the edited code being expected there; every other query keeps the
argument above.

**Only the queries an edit reaches (`maps/tpcds.bin` "qlines", `selector.queries_reaching`).**
Planning the queries is nearly all of the probe (sqlglot's optimizer about half, the
fingerprints a fifth), yet an edited statement is often run by only some of them, and an edit
to the query file changes only a few. `tools/build_tpcds.py qcov` records, for
each query alone in a fresh process after the imports, the lines of the pinned tree that run
while it is parsed, passed through pickle, optimized, planned up to the executor and
transpiled for DuckDB; `qseq` records the same with all queries in one process, in order, as
the test runs them. A query that ran none of the edited statements in either trace (the same
statement regions the test selection matches against coverage; for a changed parameter
default, the function's body) is not planned: with the import state established as pinned
and its text unchanged, it reaches the executor as on the pinned commit, so its pinned
fingerprints stand. Lines some query runs only after other queries ran before it (a cache
hit, say) switch this off, since which queries reach them depends on the order (only 11
lines, all in the test module, are like that). A TPC-DS query whose text an edit changes is
always planned. On the labeled commits above, 15 probes plan no query at all and 10 more
plan fewer than 20; on the TPC-DS query-file set the selector's CPU fell from 73 s to 13 s and its
simulated time from 335 s to 250 s, as probes that used to run out of time now finish. The
probe also leaves duckdb, numpy and
pandas unloaded unless it runs differing queries: the test module imports them at module
level, but planning never uses them, so stand-ins take their place while it loads and the
first real use swaps in the real modules (`planfp._lazy_heavy`). That saves about two seconds
of CPU per run of the check, which the selector used to pay up front to share them with its
children.

**Children start after `import sqlglot` (`selector.run_stages`, `verify.preload_core`).** Every
child that imports the patched tree (an import diff, a planning check, a test run) starts with
`import sqlglot`, which loads 36 modules. Most patches change none of them (27 of the 30
sample commits). The selector first does all the work for commits that do change them, then
imports sqlglot and `tests.helpers` once in its own process, from the pinned tree and after
the same set-up a child gets (`SKIP_INTEGRATION`, `unittest` loaded: sqlglot records at import
whether it is), and forks the children for every other commit from there. They start where a
fresh child is after its first import, without paying for it. Checked by fingerprinting
everything an unpatched import leaves behind through both paths: identical, key for key.

**Running cheap selections (`blast/verify.py`, `blast/policy.py`).** The rules allow running
sqlglot's tests while deciding (the CPU is charged). For a commit whose selection is cheap
to run, the selector runs it on the patched tree and prints only the tests that really fail,
so a commit that breaks nothing costs its CPU instead of at least 2 s of worker. It runs
only when `0.3 * (printing cost saved) > estimated CPU`; the estimate uses each test file's
fixed cost measured in the image (`tools/verify_calibrate.py`). Each run happens in a forked
child that imports sqlglot fresh: patched modules load from the patched text through an
import hook, patched fixtures are served from `/tmp`, process pools run inline with the same
pickle round trips, and tests run under `unittest`. Failures, errors, failing subtests,
set-up errors and import errors fail a test; skips pass, as they do under pytest. Tests that
spawn a Python subprocess are never run this way, and any timeout, crash or patch that does
not apply keeps the static selection. `tools/verify_check.py` checks this against labels: on
45 labeled commits it reproduced every failing set.

**Packing.** Longest test first onto the least-loaded worker, trying 1 to 8 workers and
keeping the cheapest `max load + 2 s × workers`.

If anything goes wrong while handling a commit, that commit selects every test.

## Validation

`tools/mutate.py` generates patches in the styles of the sample set (operator and constant
flips, comment insertions, fixture edits, combinations) and kinds it never had (class
constants, deleted statements, new overriding methods, test-file edits, docstrings, syntax
errors). `tools/label.py` labels each patch by running the suite on it inside the scorer's
image, and `tools/evaluate.py` scores the selector against any labeled set.

## Layout

* `selector.py`, `blast/`: the program the image runs
* `maps/`: the committed maps (built by `tools/build_maps.py`); the text of the pinned files a
  patch touches is read from the sqlglot checkout the `Dockerfile` puts in the image
* `tools/`: tracing, map building, mutation, labeling and evaluation (not used at run time)
* `KIT_README.md`, `SCORING.md`, `score.py`, `sample/`, `Makefile`: the organizers' starter kit
