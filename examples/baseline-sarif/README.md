# Baseline and SARIF

A project adopts pytest-queryguard when it already has findings.
`tests/test_legacy.py` calls two functions with an N+1 each
(`Book.author` and `Author.books`); `tests/test_new.py` calls newly written
code with another one.

```console
$ pip install pytest-queryguard
$ pytest tests -o queryguard_baseline=
3 failed
```

Record the existing findings, for the legacy tests only, and commit the file
(`queryguard-baseline.json` is already in this directory):

```console
$ pytest tests/test_legacy.py --queryguard-update-baseline
========================= queryguard: baseline updated =========================
.../queryguard-baseline.json: 2 entries (2 added, 0 removed)
2 passed
```

`pytest.ini` sets `queryguard_baseline = queryguard-baseline.json`. Now only the
new finding fails, and `--queryguard-sarif` writes it as SARIF for GitHub code
scanning:

```console
$ pytest tests --queryguard-sarif=queryguard.sarif
...
queryguard: N+1 detected

  Book.author was lazy-loaded 7 times
  first at app/books.py:27 in titles_of
  parents loaded at app/books.py:26 in titles_of
  kind: relationship-load  label: Book.author
  docs: https://maciejziel.github.io/pytest-queryguard/how-detection-works.html#relationship-load
  sql: SELECT author.id, author.name FROM author WHERE author.id = ?
  fix: load it with the parent query, e.g.
       select(Book).options(joinedload(Book.author))
========================== queryguard: SARIF written ===========================
queryguard.sarif: 1 results
1 failed, 2 passed
```

The SARIF file has one result (`queryguard/relationship-load`, in
`app/books.py`). To upload it from GitHub Actions, see
[CI and GitHub Action](../../docs/ci.md).
