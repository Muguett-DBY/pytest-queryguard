# Releasing

A stable release should never need a fix made in a hurry afterwards, so
everything that can be checked is checked before the tag, by one workflow: the
release gate (`.github/workflows/release-gate.yml`). It does not tag, upload or
publish anything. The release workflow (`.github/workflows/release.yml`) starts
from a tag that a person pushes, and a person pushes it only after the gate is
green on the commit that gets the tag. The release workflow checks that too:
its `gate` job looks up the runs of `release-gate.yml` started by hand on
`main` for the tagged commit, and fails, so nothing is published, unless one
of them succeeded. A pull request's gate run does not count, since it runs the
pull request's copy of the gate.

## What the gate checks

| # | check | where |
|---|---|---|
| 1 | The whole CI matrix (`ci.yml`, reused through `workflow_call`): Python 3.10 to 3.14 and the 3.15 pre-release, SQLAlchemy 2.0 and latest, pytest 8.0 and latest, xdist, PostgreSQL, Windows and macOS. Plus the oldest SQLAlchemy series and pytest on Linux, Windows and macOS. | `ci`, `oldest-versions` jobs |
| 2 | Accuracy corpus: precision and recall of the default configuration are at least the minimums in `scripts/accuracy_thresholds.json` (see [Accuracy](accuracy.md#release-thresholds)), and no disagreement with a verdict is undocumented. | `release_gate.py accuracy` |
| 3 | Every Python block in `README.md` and `docs/` parses, and the projects a page spells out file by file (and `README.md`'s quick start, and the examples in `examples/`) run and give the output the page shows. | `tests/test_doc_examples.py`, `test_readme.py`, `test_docs.py`, `test_examples.py` |
| 4 | The wheel and the sdist install into clean virtual environments on Linux, Windows and macOS; `pip check` passes; the package imports; the `pytest11` entry point is registered; `pytest --co` works on a tiny project and the plugin reports an N+1 there. | `release_gate.py install` |
| 5 | Upgrade: the newest earlier final release from PyPI is installed, the new wheel is installed over it, and the tiny project runs before and after. | `release_gate.py upgrade` |
| 6 | Overhead: the per-statement overhead of the measurement is at most 125 % of `scripts/bench_baseline.json` (Core and ORM loops), measured as in [How the overhead is measured](#how-the-overhead-is-measured). | `release_gate.py bench` |
| 7 | The wheel has `py.typed`, `LICENSE`, the entry point and only the package; no tests, docs, scripts, caches or scratch files. The sdist has the license, README, changelog, `pyproject.toml` and sources and no scratch files. | `release_gate.py contents` |
| 8 | `__version__`, the top entry of `CHANGELOG.md`, its date (a valid date, not in the future, not older than the entry below it) and the link references at the bottom (`[Unreleased]` compares the newest tag with `HEAD`, each entry compares the previous tag with itself) agree, and so does the version the documentation says it describes (`docs/index.md`, `docs/getting-started.md`, `llms.txt`). The workflow in `docs/ci.md` pins the action to `@v` plus `__version__` (for a pre-release, the newest final release in the changelog). | `release_gate.py consistency` |

## Running it

On GitHub: Actions, "Release gate", "Run workflow", on `main`. It also runs on
a pull request that changes `src/pytest_queryguard/__init__.py` (the version),
the workflow or its scripts (including the overhead benchmark and its
baseline). The `gate` job is green only when every other job
is, so it is the one to require.

Locally, the checks that are not a plain `pytest` run:

```bash
uv build --out-dir dist
uv run python scripts/release_gate.py all dist
```

or one at a time: `consistency`, `accuracy`, `contents dist`, `install dist`,
`upgrade dist`, `bench`. `install` and `upgrade` create virtual environments
in a temporary directory and need network access to PyPI.
`QUERYGUARD_REQUIRE_DOC_EXAMPLES=1 uv run pytest tests/test_doc_examples.py`
makes a missing package a failure instead of a skip for the documentation
projects (only Litestar, pytest-asyncio and SQLModel may be missing).

## Steps of a release

1. A pull request bumps `__version__`, dates the changelog entry and fixes the
   link references. It also updates the version the documentation describes
   (`docs/index.md`, `docs/getting-started.md`, `llms.txt`) and drops the
   sentence there that says the "since X" parts are not on PyPI yet; for a
   pre-release (`X.Y.Zrc1`) both may stay as they are, since pip still
   installs the last final release. For a final release it also bumps the
   `MaciejZiel/pytest-queryguard/action@vX.Y.Z` pin in `docs/ci.md` to the new
   version (a pre-release keeps the last final one). The gate runs on it.
2. After it is merged, run the gate on `main` and wait for it to be green.
3. Tag that commit `vX.Y.Z` and push the tag; `release.yml` checks that a
   gate run on `main` at that commit succeeded, then builds and publishes.
   Without one its `gate` job fails and nothing is published; run the gate
   on that commit, then re-run the release workflow.

## How the overhead is measured

The ratio of the overhead check is (time per statement with the measurement
on − time with it off) / time with it off, for a Core and an ORM loop against
in-memory SQLite. A GitHub runner is not one machine: `ubuntu-latest` runs on
several CPU models, and the ratio depends on the model. The earlier method
(each configuration in its own process, the median of 15) also moved from
run to run, so with one baseline for all runners the gate failed about every
other run on unchanged code.

`scripts/bench_gate.py` therefore times the measurement off and on in the
same process: 7 processes (`--processes`), each with 20 rounds (`--rounds`)
of a block of 1000 statements off and a block on, in alternating order, with
a new collector for each "on" block and a garbage collection before every
block. A process's ratio is (on − off) / off of its fastest "off" and fastest
"on" block, since noise only adds time; the result is the median over the
processes. On one CPU model the result repeats within about 0.01. Between
models it does not (gate runs on pull request 219, 16 runs):

| CPU model | runs | Core | ORM |
|---|---|---|---|
| AMD EPYC 7763 | 9 | 0.275 to 0.291 | 0.233 to 0.245 |
| AMD EPYC 9V45 | 3 | 0.364 to 0.380 | 0.273 to 0.281 |
| AMD EPYC 9V74 | 2 | 0.276 to 0.277 | 0.318 to 0.321 |
| Intel Xeon 6973P-C | 1 | 0.280 | 0.263 |
| Intel Xeon 6975P-C | 1 | 0.277 | 0.267 |

So `scripts/bench_baseline.json` has an entry per CPU model (`machines`,
keyed by the `model name` in `/proc/cpuinfo`, which the job prints), and its
top-level `core` and `orm` are the highest of those entries: the baseline of a
CPU without an entry. When a loop is over the limit, the gate measures again,
up to 3 times in all (`--attempts`), and the best result of each loop counts.
The check takes one to two minutes on the runner, at most about four with all
three attempts.

A regression shows on every CPU and in every attempt. An overhead per
statement 1.5 times the current one is a ratio 1.5 times the baseline, over
the 125 % limit on every CPU model with an entry. An increase below 25 %
can pass; the margin is what keeps the gate quiet on unchanged code. On a
CPU model without an entry, the limit comes from the highest entry, so only a
larger regression fails there.

## When a threshold has to change

The accuracy minimums and the overhead baseline are floors against
regressions. Raise them in the pull request that improves the result. Lower
them only in a pull request that says why.

The overhead baseline is recorded on the GitHub runner (`ubuntu-latest`,
Python 3.12) that runs the gate, never on a laptop: each CPU model's entry is
the mean of the gate's first measurement on that model over several green
runs (the first, because the result the gate decides on is the best of its
attempts, which would pull the baseline down). To record it, save the log of
the `overhead` job of each green gate run, one job per file, and let the
script average them per CPU model:

```bash
gh run view RUN_ID --log --job JOB_ID > gate-1.log   # for each green run
uv run python scripts/bench_gate.py --record gate-*.log
```

This replaces the entries of the CPU models in the logs, keeps the others and
recomputes the top-level values. It refuses a log that is not one overhead
job of the release gate (the CI job's looser margin lets more through) or
that did not pass. When the gate fails on a CPU model that has no entry yet,
and the code did not change, record that model from the failed log with
`--record LOG --even-failed`; this is refused for a model that has an entry.
`--update` measures on the current machine and writes its entry, which is
only right on the runner.
