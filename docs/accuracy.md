# Accuracy on a corpus of realistic code

How often does pytest-queryguard report code that is fine, and how often does it
miss code that is not? This page measures that on a corpus of 268 small
scenarios, publishes the numbers, and lists every disagreement. The scenarios
were written by the author of the plugin, so read the numbers as a regression
baseline and a list of known limits, not as a benchmark of how the plugin does on
your code.

## Method

- The corpus is in `tests/corpus/scenarios`: one module per scenario, 132 marked
  **BUG** (an N+1, a row-by-row write, a cartesian load, an eager load or columns
  loaded for nothing: what a reviewer would ask to fix) and 136 marked **CLEAN**
  (code that is fine, including the fixes: `selectinload`, `joinedload`,
  `contains_eager`, `load_only`, bulk statements, pagination, caches). The
  docstring of each module says which, and why. The verdict is mine, from reading
  the code; for a BUG the module also names the finding kinds a perfect tool
  would report.
- The code imitates what people write: list views, hand-written serializers,
  Pydantic `from_attributes` models, FastAPI endpoints under `TestClient`,
  Flask-SQLAlchemy views, SQLModel relationships, repository classes, `__str__`
  and properties that hide a query, hybrid properties, many-to-many tables,
  joined-table inheritance, trees, `with_parent`, deferred columns, commits that
  expire objects, `get_or_create` loops, `session.merge`, savepoints, bulk
  inserts, pagination, `IN` chunks of 500, polling loops, Factory Boy seeding,
  GraphQL-style field resolvers and DataLoaders, `asyncio.gather` fan-out,
  Celery-style tasks run eagerly, caching repositories, `yield_per` with and
  without `selectinload`, `with_for_update`, nested transactions, upserts
  (`ON CONFLICT DO UPDATE`), ORM bulk `INSERT` and `UPDATE` with a list of
  rows, and async versions (`AsyncSession`, `awaitable_attrs`, `run_sync`,
  `stream_scalars`). Since 2026-11-06 also: detail and export pages that
  join two or three collections, `joinedload` chains, a collection next to a
  many-to-one, `contains_eager` over a filtering join, `with_polymorphic` with a
  collection per subclass, eager loads that nothing reads (a repository method
  reused by a page that needs less, a chain whose second link is never read)
  and eager loads that something does read (Pydantic, an `association_proxy`,
  a delete cascade), wide rows of which a page reads two columns, `load_only`,
  `deferred` columns and `column_property`, association objects, a
  marshmallow-style nested schema, an `except Exception` around a view,
  `ignore()` blocks (around a loop, as a decorator, around `asyncio.gather`,
  and one that a later access escapes), and parents loaded in a fixture.
  Since 2026-11-07 also: single-table and concrete-table inheritance,
  `selectin_polymorphic`, `polymorphic_load="selectin"`, polymorphic
  collections and subclass relationships without rows, `association_proxy`
  over a lazy collection and through an eager chain, `deferred_group` and
  `undefer_group`, a column `load_only` left out, `query_expression`,
  `WriteOnlyMapped` collections, `viewonly` relationships with a custom
  `primaryjoin`, a "generic foreign key" recipe, a self-referential
  many-to-many, composite primary and foreign keys, `lazy="immediate"`,
  `"subquery"` and `"noload"` on the mapping, `populate_existing` (per row,
  once, and dropping an eager load), the identity map across two queries and
  across two Sessions, a `commit()` per row, `refresh(obj, ["rel"])` per
  object, `merge(load=False)`, `bulk_save_objects`, `MappedAsDataclass` and
  imperative mappings, `awaitable_attrs` on a many-to-one, two foreign keys
  and two chains to one table, a custom `__eq__`, pickled results, `IN` lists
  of varying length, select lists that differ only in a subquery, and lookups
  through batch APIs, "attach related" helpers and two helpers in turn.
  Since the second 2026-11-07 run also: shapes for the static check (see
  [below](#the-static-check-scenarios-2026-11-07)).
  Most of the shapes added on 2026-11-07 come from false positives and false
  negatives reported to other N+1 tools (Prosopite, Bullet, nplusone,
  django-zeal, django-perf-rec, django-nplus1, orm-lens); each such scenario
  names its issue in its docstring. Each scenario seeds a SQLite file in a setup step that is
  not measured; only `run()` is.
- Every scenario runs as a test of a real pytest session with the plugin
  installed, once per configuration: the defaults, then each opt-in rule on its
  own (`queryguard_small_repeat_threshold = 2`, `queryguard_write_loop_threshold = 2`,
  `queryguard_column_load_threshold = 2`, `queryguard_unused_eager_load = fail`,
  `queryguard_unused_columns = fail`), then `queryguard_scope = session`,
  then `queryguard_strict = true`, then all opt-in rules together.
  The findings of each test are read back from the plugin, those that fail the
  test and those that only warn: `cartesian-load` warns by default, and a
  warning is a finding the user sees, so it is measured in every configuration.
- A scenario is a true positive for a kind when the kind is in its expected set
  and the plugin reports it; a false positive when the plugin reports a kind that
  is not expected; a false negative when an expected kind is not reported.
  Kinds are counted only in the configurations that can report them (the
  write-loop kind in the configurations that turn it on, and so on).
- The same scenarios ran under queryspy 0.5.0 and pytest-querycount 0.4.0 in a
  separate virtual environment (not a dependency of this project). They report
  pass or fail per test, not a finding kind, so that comparison is by scenario.
- In the regular test suite `tests/corpus/test_corpus.py` turns each
  (scenario, configuration) pair into a test. A disagreement that is a documented
  limit is a strict `xfail` with its reason, so a change in the plugin's
  behaviour shows up as a failing test and the entry must be removed.
  `scripts/accuracy_report.py` prints the tables below.
- `tests/corpus/test_fix_patches.py` runs every BUG scenario with a
  relationship-load with `--queryguard-write-patch`, applies the patch with
  `git apply` and runs the scenarios again (see
  [The patch of `--queryguard-write-patch`](#the-patch-of-queryguard-write-patch)).

Measured on 2026-11-07 on Linux 7.0.0 (x86_64, 13th Gen Intel Core i5-13500H, 16
threads), Python 3.13.18, SQLAlchemy 2.1.1, pytest 9.1.1, Flask-SQLAlchemy 3.1.1,
FastAPI 0.141.1, pytest-queryguard 0.8.0 at `main` e527a3a plus the changes of
its pull request (the scenarios and the two fixes below), with
`uv run python scripts/accuracy_report.py`. The default tables, the opt-in
tables and the release thresholds were measured again on the same day after
the review of every disagreement (see
[Every disagreement reviewed](#every-disagreement-reviewed-2026-11-07)), on the
same machine and packages with Python 3.14.4; the two runs differ only in the
scenarios that review changed.
SQLModel was not installed (it needs SQLAlchemy below 2.1 and is only installed
in the SQLAlchemy 2.0 CI job), so 266 of the 268 scenarios ran: 131 BUG and 135
CLEAN. All numbers on this page are for those 266 and SQLite, except the
PostgreSQL run, which is the earlier 141-scenario measurement and says so,
and the comparison with other tools, which ran the 252 scenarios the corpus
had on that day in GitHub Actions.

## Results, default configuration

114 of the 117 BUG scenarios that the default rules can report are flagged
(the other 14 BUG scenarios are write loops, unused eager loads and unused
columns, which are opt-in), and 2 of the 149 scenarios without an expected
default kind are flagged.

| kind | TP | FP | FN | precision | recall |
|---|---|---|---|---|---|
| relationship-load | 51 | 0 | 0 | 100% | 100% |
| repeated-statement | 45 | 1 | 2 | 98% | 96% |
| column-load | 12 | 0 | 1 | 100% | 92% |
| cartesian-load | 7 | 1 | 0 | 88% | 100% |

The small counts (51, 47, 13, 7) mean that one scenario moves a percentage by
several points. The relationship-load row is 51 scenarios, not a claim about
every ORM pattern.

### The static-check scenarios (2026-11-07)

14 scenarios (8 BUG, 6 CLEAN) were added later on 2026-11-07 to measure the
[static check](static-check.md#accuracy): shapes reported to other static
checks (orm-lens #72, #85, #146) and common ones (a repository with a
service, FastAPI and Pydantic lists, a template-like loop, a statement built
over several lines, an `aliased()` entity, an early `return`, an `if`
guard), `bug_125` to `bug_132` and `clean_131` to `clean_136`. All are
relationship-load BUG or CLEAN, and all agreed with their verdict in every
configuration, so they add 8 relationship-load true positives (51 instead of
43) and true negatives only; 6 of the 8 get a patch from
`--queryguard-write-patch` (26 of 51 in all). The other tables on this page
were not run again; only the relationship-load counts were updated.

### Every disagreement reviewed (2026-11-07)

After the scenarios of 2026-11-07 were added, each disagreement of the
default configuration and each documented limit (73 strict `xfail`s of
(scenario, configuration)) was read again and put in one of three classes: a
bug of the plugin, fixed at its cause with a unit test first; a limit of the
approach, explained in the user documentation with a workaround; or a wrong
verdict, corrected. No verdict was wrong. Two causes were bugs:

- `bug_77_cartesian_mapping_lazy_joined`: two collections with
  `relationship(lazy="joined")` on the mapping make the same product as two
  `joinedload()` options, but the cartesian-load rule read only the options
  of the query. **Fixed**: it now follows the loader strategies of the
  mapping for each entity the SELECT loads, as SQLAlchemy does (an option or
  a wildcard such as `lazyload("*")` wins, a lazy relationship is not
  followed, a cycle stops unless `join_depth` allows it). 9 `xfail`s fewer,
  one in each configuration.
- `bug_76_cartesian_async_endpoint` and `clean_78_joined_collection_and_scalar`:
  a page for one article, `select(Article).where(Article.id == id)`, reads
  three of twelve columns, and the unused-columns check reported it. Such a
  query returns one row by construction, so `load_only()` would save one
  row's width; a reviewer would not ask for it, and a lookup per item is the
  repeated-statement rule's finding anyway. **Fixed**: a group whose every
  query pins the whole primary key of the class with `=` (`session.get()`
  included) is not reported. 4 `xfail`s fewer, with the check on and with
  all opt-in rules.

The other 58 are limits, each explained where a user looks for it (the page
in brackets) with what to do. (`bug_04` and `bug_06` in strict mode, a strict
error that Pydantic wraps, were limits too; they are fixed since.)

| scenarios | configurations | limit | what to do |
|---|---|---|---|
| `clean_16_polling_loop` | all | a polling loop and a loop-invariant query repeat one statement with the same parameters ([Limitations](limitations.md)) | the `queryguard_ignore` marker on the test |
| `clean_82_two_small_collections_joined` | all | the cartesian-load rule cannot know that collections stay small ([How detection works](how-detection-works.md#cartesian-load)) | it only warns by default; an ignore pattern or `ignore("cartesian-load")` for a product that stays small by design |
| `bug_42`, `bug_45` (repeats), `bug_44` (column loads) | all but the threshold-2 ones | below the default thresholds of 5 ([Limitations](limitations.md)) | `queryguard_small_repeat_threshold`, `queryguard_column_load_threshold` |
| `clean_20`, `clean_22` | small-repeat, column-load-2 | what those lower thresholds also report ([Limitations](limitations.md)) | keep the defaults, or ignore the test |
| `clean_18`, `clean_30`, `clean_77` | write-loop, all opt-in | seeding and per-row writes on purpose look like a write loop ([Limitations](limitations.md), [Factory Boy](recipes-factory-boy.md)) | the rule is opt-in for that reason |
| `bug_47`, `bug_48`, `bug_52`, `bug_53` | session-scope | each Session is counted on its own, by design ([Several sessions](several-sessions.md)) | the default scope |
| `bug_86_unused_columns_via_selectinload` | unused-columns, all opt-in | rows loaded through a relationship are not grouped ([Unused columns](unused-columns.md)) | `selectinload(...).load_only(...)` by hand |
| `clean_124_collection_checked_through_inspect` | unused-eager-load, all opt-in | reads through `inspect()` are invisible ([Unused eager loads](unused-eager-load.md)) | ignore it |
| `bug_88_strict_lazy_then_lookup_loop` | strict | strict mode stops the test at the first N+1 ([When a test fails](when-a-test-fails.md#strict-mode-fail-at-the-access)) | fix it and run again, or run without strict mode |

Before and after, every configuration, on the 252 scenarios that ran (no
other finding of any scenario changed in any configuration):

| configuration | kind | before: TP, FP, FN | after: TP, FP, FN |
|---|---|---|---|
| every configuration | `cartesian-load` | 6, 1, 1 (86%, 86%) | 7, 1, 0 (88%, 100%) |
| unused-columns = fail, all five rules | `unused-columns` | 8, 2, 1 (80%, 89%) | 8, 0, 1 (100%, 89%) |

### The scenarios of 2026-11-07, before and after

57 scenarios were added on 2026-11-07 (28 BUG, 29 CLEAN) for mapping and
loading features the corpus did not have, and for shapes reported to other
tools (see the method). Before the two fixes of the same pull request they
disagreed with their verdict in two places, and one verdict had to be
corrected:

- `clean_123_polymorphic_subclass_without_rows` (nplusone #36):
  `with_polymorphic(Employee, [Manager])` with `selectinload` of
  `Manager.reports`, and no manager in the data. The unused-eager-load check
  reported `Manager.reports`: it counted the reads of the `Employee` rows as
  reads of instances that could have had the relationship. **Fixed**: only an
  instance of the relationship's class or a subclass counts now. With managers
  in the data and the reports not read, it is still reported.
- `clean_124_collection_checked_through_inspect` (Bullet #642): a helper reads
  the collection through `inspect(author).attrs.articles.loaded_value`, which
  the check cannot see (as with `__dict__`). **Not fixed**: a documented limit,
  an `xfail` with its reason.
- The single-table scenario was written as CLEAN, on the belief that
  `select(Vehicle)` loads the subclass columns of the shared table. It does
  not in SQLAlchemy 2.x: each `Car.seats` is a SELECT per row. The plugin was
  right to report a column-load, so the scenario is `bug_114`, and
  `clean_102` reads them through `with_polymorphic`. The report was wrong in
  another way: it said `Car (expired)` and advised `expire_on_commit=False`,
  for a test that committed nothing; so did `bug_18` (joined-table
  inheritance) since it was written. **Fixed**: the subclass columns that a
  query of the base class leaves out are a cause of their own, labelled
  `Car (polymorphic)`, with `selectin_polymorphic` as the hint. The finding
  kinds, and so the tables on this page, do not change.

All other new scenarios agreed with their verdict in every configuration.
Before and after, for the default configuration, on the whole corpus:

| kind | 2026-11-06, 195 ran: TP, FP, FN | 2026-11-07, 252 ran: TP, FP, FN |
|---|---|---|
| `relationship-load` | 29, 0, 0 | 43, 0, 0 |
| `repeated-statement` | 37, 1, 2 | 45, 1, 2 |
| `column-load` | 6, 0, 1 | 12, 0, 1 |
| `cartesian-load` | 6, 1, 1 | 6, 1, 1 |

The new scenarios add true positives and true negatives only, because they
were checked against the plugin while they were written: read them as a
regression baseline for these features, not as a measurement of new
precision.

### The newer kinds, before and after 2026-11-06

Before 2026-11-06 the corpus had 147 scenarios (145 ran) and did not measure
`cartesian-load`, `unused-columns` or strict mode at all; `unused-eager-load`
was one BUG scenario. 50 scenarios were added for them. The plugin's findings
did not change: the one fix of that change is to the patch file (see
[The patch of `--queryguard-write-patch`](#the-patch-of-queryguard-write-patch)).
The relationship-load rule changed right after, in
[#307](https://github.com/MaciejZiel/pytest-queryguard/issues/307); the "after"
column is measured with it.

| kind (configuration) | before: TP, FP, FN | after: TP, FP, FN | precision, recall after |
|---|---|---|---|
| `cartesian-load` (default, warns) | not measured | 6, 1, 1 | 86%, 86% |
| `unused-eager-load` (`= fail`) | 1, 0, 0 | 5, 0, 0 | 100%, 100% |
| `unused-columns` (`= fail`) | not measured | 8, 2, 1 | 80%, 89% |
| `relationship-load` (strict) | not measured | 27, 0, 2 | 100%, 93% |
| `repeated-statement` (strict) | not measured | 36, 1, 3 | 97%, 92% |
| `relationship-load` (default) | 20, 0, 0 | 29, 0, 0 | 100%, 100% |
| `repeated-statement` (default) | 36, 1, 2 | 37, 1, 2 | 97%, 95% |
| `column-load` (default) | 5, 0, 1 | 6, 0, 1 | 100%, 86% |

The "before" column for the default kinds is the 145-scenario run of
2026-11-06 on the same machine and packages, before the new scenarios. One new
CLEAN scenario, `clean_93_two_single_object_lookups` (one order from
`session.get`, another from a `select`, each showing its customer), was a
relationship-load false positive in every configuration, strict mode included:
the rule counted the distinct parents of a relationship wherever they were
loaded. Since #307 the report groups the parents by the query line that loaded
them, as strict mode does, and the scenario passes; no other scenario's
findings changed.

The repeated-statement precision went from 83% to 89% when 13 BUG and 10 CLEAN
scenarios were added on 2026-10-13, with the plugin unchanged: the same 4 false
positives against 12 more true positives. That is a change of the corpus, not of
the plugin. The 12 new repeated-statement BUG scenarios include shapes chosen to
look like batching or pagination (`LIMIT 1 OFFSET n` per row, the first page of
each customer's orders, `IN` lists of one id or built from each item) and a
loop-invariant query with the same parameters on every run. Five more BUG
scenarios, `bug_64` to `bug_68`, were added while reviewing the rule for
pagination and chunks: loops that it first left alone (an `IN` list of each
customer's ten orders, `WHERE id > :row_id LIMIT 3` per row, the same on a
string column, a paginated loop-invariant lookup per order) and a moving
window read with `LIMIT 3 OFFSET i`.

### Before and after the rule for pagination and chunks

On 2026-10-13 the repeat rules learned to leave pagination and chunked `IN`
loads alone when their parameters show them (see "Pagination and chunks" in
[How detection works](how-detection-works.md)). The same 141 scenarios, before
and after:

| configuration | repeated-statement before | after |
|---|---|---|
| default | 36 TP, 9 FP, 2 FN (80%, 95%) | 36 TP, 1 FP, 2 FN (97%, 95%) |
| small-repeat = 2 | 38 TP, 11 FP, 0 FN (78%, 100%) | 38 TP, 2 FP, 0 FN (95%, 100%) |
| repeat scope `session` | 32 TP, 8 FP, 6 FN (80%, 84%) | 32 TP, 1 FP, 6 FN (97%, 84%) |

No BUG scenario was lost in any configuration: the findings of every BUG
scenario are the same before and after in all seven configurations, and the
BUG scenarios that look like pagination or batching (`bug_58` to `bug_60`,
`bug_63` to `bug_68`) are still reported.
Read the "before" column with care: five of the CLEAN scenarios
(`clean_62_dataloader_max_batch_size`, `clean_69_celery_batch_task_chunks`,
`clean_72_keyset_pagination_desc`, `clean_73_paginate_twice`,
`clean_74_chunked_in_string_keys`) were written together with the rule, to test
it, so they are false positives before it by construction. Without them, on the
136 scenarios without them, the default row goes from 36 TP, 4 FP (90%) to
36 TP, 1 FP (97%): the three false positives removed are
`clean_12_offset_pagination`, `clean_13_keyset_pagination` and
`clean_15_chunked_in_six_chunks`. `clean_14_chunked_in_three_chunks` is no longer
reported by the small-repeat rule either.

## Results with the opt-in rules

| configuration | kind | TP | FP | FN | precision | recall |
|---|---|---|---|---|---|---|
| small-repeat = 2 | repeated-statement | 47 | 2 | 0 | 96% | 100% |
| write-loop = 2 | write-loop | 8 | 3 | 0 | 73% | 100% |
| column-load threshold 2 | column-load | 13 | 1 | 0 | 93% | 100% |
| unused-eager-load = fail | unused-eager-load | 5 | 1 | 0 | 83% | 100% |
| unused-columns = fail | unused-columns | 8 | 0 | 1 | 100% | 89% |
| repeat scope `session` | repeated-statement | 41 | 1 | 6 | 98% | 87% |
| strict = true | relationship-load | 51 | 0 | 0 | 100% | 100% |
| strict = true | repeated-statement | 44 | 1 | 3 | 98% | 94% |
| all five rules | repeated-statement | 47 | 2 | 0 | 96% | 100% |
| all five rules | column-load | 13 | 1 | 0 | 93% | 100% |
| all five rules | write-loop | 8 | 3 | 0 | 73% | 100% |
| all five rules | unused-eager-load | 5 | 1 | 0 | 83% | 100% |
| all five rules | unused-columns | 8 | 0 | 1 | 100% | 89% |

`relationship-load` stays at 51 TP, 0 FP, 0 FN in every configuration,
strict included since the fix for a strict error that a library wraps (see
below), and `cartesian-load` at 7 TP, 1 FP, 0 FN in every configuration. The unused-eager-load row is
five BUG scenarios (a joined many-to-one of which only the foreign key is
read, the unread second link of a `selectinload` chain, a repository method
reused by a page that needs less, an async handler) against CLEAN scenarios
where the eager load is read by some rows only, by Pydantic, by an
`association_proxy`, or by a delete cascade. Its one false positive is
`clean_124_collection_checked_through_inspect` (a read through `inspect()`,
see below). With all rules on, 130 of 131 BUG scenarios are flagged, and 8 of
135 CLEAN scenarios are.

**Effect of `queryguard_scope = session`** (added after the first run of this
corpus): the scenarios that run each lookup in its own `Session`
(`bug_47_session_per_item`, `bug_48_threadpool_lookups`,
`bug_52_async_gather_session_per_task`, `bug_53_celery_task_per_id`) are no longer
reported, which is what the option is for; they were reported before, and the four
false negatives are the price. In both scopes the only CLEAN scenario the default
rules report is the polling loop, which repeats one statement inside one `Session`.

## Every disagreement

| scenario | verdict | the plugin | why |
|---|---|---|---|
| `bug_42_small_get_loop_below_threshold`, `bug_45_async_session_get_small` | BUG: 4 and 3 `get` calls | not reported | The repeat threshold is 5. The small-repeat rule reports both. |
| `bug_44_expired_after_commit_three` | BUG: 3 objects reload after a commit | not reported | The column-load threshold is 5. Threshold 2 reports it. |
| `bug_50_cascade_delete_loop` | BUG: delete cascade loads per parent | `relationship-load` and also `repeated-statement` (before the fix) | Bug [#59](https://github.com/MaciejZiel/pytest-queryguard/issues/59), fixed: an autoflush before the lazy load took over its relationship attribution. After the fix the repeated-statement false positive of this scenario is gone, and the repeated-statement rows on this page were re-measured. |
| `clean_12_offset_pagination`, `clean_13_keyset_pagination`, `clean_15_chunked_in_six_chunks` | CLEAN: pages and chunks | `repeated-statement` before 2026-10-13, nothing after | Fixed by the rule for pagination and chunks ([#60](https://github.com/MaciejZiel/pytest-queryguard/issues/60)). `clean_14_chunked_in_three_chunks` was reported by the small-repeat rule before it. |
| `clean_16_polling_loop` | CLEAN: polling a status on purpose | `repeated-statement` | Every run has the same parameters, like the loop-invariant query of `bug_55_loop_invariant_query`, which is a bug. The statements cannot tell them apart. Use the `queryguard_ignore` marker. |
| `clean_20_cached_lookup` | CLEAN | `repeated-statement` with small-repeat on | Two statements of one shape from one line are what the rule reports. |
| `clean_22_expire_on_commit_two_objects` | CLEAN | `column-load` with threshold 2 | Two reloads after a commit are the threshold. |
| `clean_18_factory_boy_seed_in_body`, `clean_30_savepoint_per_row_insert`, `clean_77_refresh_after_each_create` | CLEAN: seeding, per-row inserts with their own error handling, eight creates each refreshed | `write-loop` with the rule on | The rule cannot tell them from a write loop. |
| `bug_47_session_per_item`, `bug_48_threadpool_lookups`, `bug_52_async_gather_session_per_task`, `bug_53_celery_task_per_id` | BUG | not reported with scope `session` | By design of the option. |
| `clean_93_two_single_object_lookups` | CLEAN: one order from `session.get`, another from a `select`, each showing its customer | `relationship-load` in every configuration before #307, nothing after | Fixed by [#307](https://github.com/MaciejZiel/pytest-queryguard/issues/307): the report groups the parents of a relationship by the query line that loaded them, as strict mode does, so two single-object lookups at two lines are no N+1. |
| `clean_82_two_small_collections_joined` | CLEAN: a user's two roles and two e-mail addresses joined in one login query | `cartesian-load` (a warning) | The rule reads the loader strategies, not how many rows the collections hold. A product of 2 x 2 rows is reported like one of 20 x 20. |
| `bug_77_cartesian_mapping_lazy_joined` | BUG: two collections with `lazy="joined"` on the mapping | not reported before the fix; `cartesian-load` after | Fixed on 2026-11-07: the rule follows the loader strategies of the mapping, not only the options of the query. |
| `bug_76_cartesian_async_endpoint`, `clean_78_joined_collection_and_scalar` | the cartesian verdict, and no unused columns: a page for one article | `unused-columns` with the check on, before the fix; nothing after | Fixed on 2026-11-07: a query that pins the whole primary key returns one row, and the check no longer reports a group of such queries. |
| `bug_86_unused_columns_via_selectinload` | BUG: `selectinload(Author.articles)`, only `title` read of each article | not reported by the unused-columns check | The check groups only the instances a query returns itself, not those loaded through a relationship (documented in [Unused columns](unused-columns.md)). |
| `bug_04_pydantic_from_attributes`, `bug_06_fastapi_response_model` | BUG: Pydantic `from_attributes` and a FastAPI `response_model` over lazy relationships | no finding in strict mode before the fix; `relationship-load` after | Pydantic catches the `NPlusOneDetected` that strict mode raises at the access and raises its own validation error, with the strict message inside. The plugin saw that error, not its own, so the report at the end (and SARIF, `--queryguard-json`, the summary) did not run. Fixed after 0.8.0: when strict mode raised in a test that then fails with another error, the analysis still runs and records the N+1; the test keeps failing with the library's error. |
| `bug_88_strict_lazy_then_lookup_loop` | BUG: a lazy loop, then a `session.get` loop | only `relationship-load` in strict mode | Strict mode stops the test at the first N+1, so the second loop never runs. |
| `clean_123_polymorphic_subclass_without_rows` | CLEAN: `selectinload` of a subclass relationship, no subclass rows in the data | `unused-eager-load` with the check on, before the fix; nothing after | Fixed on 2026-11-07: reads of base-class rows no longer count as reads of instances that could have the subclass's relationship. |
| `clean_124_collection_checked_through_inspect` | CLEAN: the collection is read through `inspect(obj).attrs.x.loaded_value` | `unused-eager-load` with the check on | The check sees reads through the attribute, not through `inspect()` or `__dict__` (documented in [Unused eager loads](unused-eager-load.md)). |
| `bug_18_joined_inheritance_columns`, `bug_99_polymorphic_collection_subclass_columns`, `bug_114_single_table_subclass_columns` | BUG: subclass columns loaded per row after a query of the base class | `column-load`, as expected, but labelled `(expired)` with the `expire_on_commit=False` hint, before the fix | Fixed on 2026-11-07: labelled `(polymorphic)`, with the `selectin_polymorphic` hint. The kind did not change, so the tables did not either. |

The three BUG scenarios below the default thresholds, and the BUG scenarios that
are only write loops, unused eager loads or unused columns, are the "FN" column of
the default tables or are outside their scope; they are listed here rather than
hidden.

Not shown because they agreed with the verdict: lazy-load loops through views,
serializers, Pydantic, FastAPI (sync and async), Flask-SQLAlchemy, SQLModel,
`__str__`, properties, hybrids, many-to-many, nested relationships, trees, Jinja
templates, `with_parent`, joined-table inheritance, deferred columns, expired
columns, `session.refresh`, Core and `text()` and driver SQL loops, `merge` loops,
dynamic relationships, threads and a session per item, GraphQL-style resolvers, a
DataLoader awaited key by key, `asyncio.gather` and Celery-style fan-out, caching
repositories that miss, `with_for_update` per row, `yield_per` with a query per row,
`LIMIT 1 OFFSET n` walks, a first page per customer, `IN` lists of one id or built
per item (also ten ids per customer), a keyset-shaped query per row on an integer
and on a string column, a paginated lookup per order, a moving window with
`LIMIT 3 OFFSET i`, a loop-invariant query and per-row upserts (with the write-loop rule on);
and, as CLEAN, all the eager loading fixes, loader options on the mapping,
`raiseload`, identity-map hits, new objects, backref assignment, executemany,
multi-row `VALUES`, `add_all`, streaming, savepoints, recursive CTEs, bulk updates,
a batching DataLoader, `selectinload` two levels deep over 1500 parents, `yield_per`
with `selectinload`, ORM bulk `UPDATE` and `INSERT` with a list of rows, a multi-row
upsert, one `FOR UPDATE` for a batch, a nested transaction around a batch, a
prefetching repository, `asyncio.gather` over different statements, and pagination
and chunked loads: offset and keyset pages (also newest first, and the same walk
twice), `IN` chunks of ids and of string keys, a DataLoader with a maximum batch
size, and Celery-style tasks per chunk.

Of the scenarios added on 2026-11-07, all but `clean_124` agree with the
verdict in every configuration (`clean_123` since its fix). As BUG: lazy loads
under single- and concrete-table inheritance, through an `association_proxy`,
a `viewonly` relationship with a custom `primaryjoin`, a "generic foreign
key", a self-referential many-to-many, a composite foreign key,
`lazy="immediate"`, `MappedAsDataclass` and imperative mappings, an async
`awaitable_attrs` many-to-one, a loop over a `limit(10)` query, an eager load
dropped by `populate_existing` or made in another Session; column loads of a
`deferred_group`, of a column `load_only` left out, of subclass columns (a
polymorphic collection, single-table inheritance), of a `refresh(obj,
["orders"])` per customer and of a `commit()` per row (with its `UPDATE`s
under the write-loop rule); repeated statements of a `WriteOnlyMapped`
select per author, `session.get` with a composite key, a `populate_existing`
select per row, an `exists()` check per imported row, `IN` lists of one to
four ids, a batch loader called with one id, an "attach related" helper and
one lookup reached through two helpers in turn. As CLEAN:
`selectin_polymorphic`, `polymorphic_load="selectin"`, single-table
`with_polymorphic`, `undefer_group`, `query_expression` with
`with_expression`, appending to `WriteOnlyMapped` collections, a `viewonly`
and a self-referential many-to-many relationship loaded with `selectinload`,
`session.get` with a composite key answered by the identity map,
`lazy="subquery"` and `lazy="noload"` mappings, one `populate_existing` query
after a bulk `UPDATE`, `merge(load=False)`, `bulk_save_objects`, two foreign
keys and two `selectinload` chains to one table, a five-check sign-up
validator, tags assigned through a two-way many-to-many, eager loads of empty
collections, `contains_eager` over an outer join that keeps empty
collections, an eager chain read only by a proxy and a property, a second
query answered by the identity map, a custom `__eq__` over a joined
relationship, pickled eager results and six dashboard queries that differ
only in a select-list subquery.

Of the scenarios added on 2026-11-06, these agreed with the verdict in every
configuration: two and three collections joined side by side (a detail page,
an admin CSV export, the legacy `session.query()` API, an async endpoint),
two joined collections inside the query of a `selectinload`, a many-to-one on
the way to one of them; as CLEAN, a joined collection with a joined
many-to-one, a `joinedload` chain, `joinedload` with `selectinload`,
`contains_eager` over a filtering join, two `selectinload` chains in a
FastAPI endpoint, one joined collection on a paginated query and a
collection of each subclass under `with_polymorphic`; the unused-eager-load
scenarios above; unused columns in a sitemap, a three-field Pydantic summary,
an async background job and a paginated API, and, as CLEAN, `load_only`, a
`to_dict` of every column, a select of three columns, an edit form that
reads seven of twelve, and `deferred` columns with a `column_property`; in
strict mode, a view whose `except Exception` swallows the strict error (the
report at the end still fails the test); `ignore()` around a loop, as a
decorator and around `asyncio.gather` (CLEAN), and one that a later access
outside it escapes (reported in full); an N+1 in a fixture only (CLEAN) and
a loop in the test over parents a fixture loaded (reported); an association
object walked lazily, a marshmallow-style nested schema, a paginated API
reading each author, a self-referential comment thread, a polymorphic list
reading a subclass's collection and a deferred `column_property` read per row.

## The patch of `--queryguard-write-patch`

`tests/corpus/test_fix_patches.py` copies the corpus into the rootdir of a
pytest session, runs the 51 BUG scenarios with a relationship-load with
`--queryguard-write-patch`, applies the patch with `git apply --check` and
`git apply`, and runs them again. A scenario module with `PATCH = True`
expects a patch; every other one expects none, so a patch that appears or
disappears fails the test.

Measured on 2026-11-07: **26 of the 51 get a patch; every patch applies, and
in all 26 the relationship-load is gone after it, with no new finding** (a
`joinedload` next to another joined collection would be a cartesian-load).
The other finding of `bug_88_strict_lazy_then_lookup_loop`, a
repeated-statement, stays, as it should. The 25 without a patch, each a case
where the patch cannot be certain of the change:

| why no patch | scenarios |
|---|---|
| the query runs inside a comprehension or generator expression | 14 (`bug_03`, `bug_13`, `bug_16`, `bug_19`, `bug_41`, `bug_43`, `bug_92`, `bug_100`, `bug_104`, `bug_105`, `bug_112`, `bug_113`, `bug_122`, `bug_123`) |
| a recursive walk of a self-referential relationship: the deeper parents were loaded by the relationship itself, so no one query started them | 2 (`bug_37`, `bug_95`) |
| the objects were first loaded by another query that already eager-loads the relationship, and `populate_existing` or a second Session undid it | 2 (`bug_116`, `bug_117`) |
| Flask-SQLAlchemy's `db.select(Author)` | 1 (`bug_07`) |
| `select(with_polymorphic(...))` or `select(aliased(...))` | 2 (`bug_96`, `bug_130`) |
| a statement assigned more than once (`stmt = stmt.options(...)`) | 1 (`bug_129`) |
| the parents were loaded by a fixture, before the measurement | 1 (`bug_90`) |
| the query selects the base class of a concrete-table hierarchy (`select(Payment)`), the relationship is a subclass's own | 1 (`bug_98`) |

The `fix:` line of the report still shows the option for each of them.

**Fixed with the 2026-11-07 measurement:** 10 scenarios that got no patch now
get one. A class named through its module (`select(shop.Customer)`, 14
scenarios before) is patched as `selectinload(shop.Customer.orders)`; a
statement built on an earlier line (`stmt = select(...)`, then
`session.scalars(stmt)`, 3 scenarios before) is patched where it is built
when the function assigns it once and reads it only there; and
`session.execute(select(...)).scalars()` no longer counts the `.scalars()`
of the result as a second query. The remaining scenarios of those two rows
moved to the rows above: their query runs in a comprehension, or walks a
tree.

**Fixed with the 2026-11-06 measurement:** when one query got two findings, a
relationship and a chain through it (`Playlist.entries` and
`PlaylistEntry.track` in `bug_91_association_object_loop`), the combined patch
added both options as two `.options()` calls,
`.options(selectinload(Playlist.entries)).options(selectinload(Playlist.entries).joinedload(PlaylistEntry.track))`.
That ran and fixed the N+1, but the first call repeats the start of the
second. The patch now keeps only the chain.

## Release thresholds

A release does not go out when the corpus gets worse. The release gate
(`scripts/release_gate.py accuracy`, see "Releasing" in `CONTRIBUTING.md`) runs
the corpus with the default configuration and fails when the precision or the
recall of a finding kind is below the minimum recorded in
`scripts/accuracy_thresholds.json`, when fewer than 250 scenarios ran, or when a
disagreement with a verdict is not documented in the scenario. The minimums are
the values measured on 2026-11-07 (SQLAlchemy 2.1.1, pytest 9.1.1, SQLModel not
installed, 252 of the 254 scenarios), rounded down to a multiple of 10%:

| kind | minimum precision | minimum recall | measured |
|---|---|---|---|
| `relationship-load` | 100% | 100% | 43 TP, 0 FP, 0 FN (100% and 100%) |
| `repeated-statement` | 90% | 90% | 45 TP, 1 FP, 2 FN (98% and 96%) |
| `column-load` | 100% | 90% | 12 TP, 0 FP, 1 FN (100% and 92%) |
| `cartesian-load` | 80% | 100% | 7 TP, 1 FP, 0 FN (88% and 100%) |

The repeated-statement minimum precision went from 80% to 90% with the rule for
pagination and chunks, in the same change. The relationship-load minimum
precision went from 100% to 90% on 2026-11-06, when
`clean_93_two_single_object_lookups` was added (a CLEAN scenario the rule then
reported), and back to 100% with #307, which fixed it. `cartesian-load` got
its minimums on 2026-11-06, the first time it was measured; its minimum
recall went from 80% to 100% on 2026-11-07, when the rule learned the loader
strategies of the mapping. The
`column-load` minimum recall went from 80% to 90%, and the minimum number
of scenarios from 195 to 250, with the scenarios of 2026-11-07. The minimums
are a floor against regressions, not a goal. Raise one in the same pull request
that improves the corpus result; lower one only with a reason in that pull
request, because every lowered minimum is a release that detects less.

## Comparison with other tools

Measured on 2026-11-07 by the [head-to-head workflow](https://github.com/MaciejZiel/pytest-queryguard/actions/runs/42391426710)
(`.github/workflows/head-to-head.yml`, `scripts/head_to_head/`) in GitHub
Actions: the same 252 scenarios, one job per tool, each tool pinned and set
up the way its own documentation says to fail a test on an N+1, with its
defaults otherwise. Python 3.13.18, SQLAlchemy 2.1.1, pytest 9.1.1;
pytest-queryguard 0.8.0 at the commit of the run. The method, the settings
of each tool and the overhead of each are in
[Comparison](comparison.md#head-to-head-in-github-actions).

| tool | BUG flagged | BUG missed | BUG error | CLEAN passed | CLEAN flagged | CLEAN error | precision | recall (all BUG) | recall (kinds it claims) |
|---|---|---|---|---|---|---|---|---|---|
| pytest-queryguard 0.8.0, defaults | 106 | 17 | 0 | 127 | 2 | 0 | 98% | 86% | 97% |
| pytest-queryguard 0.8.0, all opt-in rules | 122 | 1 | 0 | 121 | 8 | 0 | 94% | 99% | 99% |
| queryspy 0.6.0, `--queryspy-strict` | 98 | 25 | 0 | 102 | 27 | 0 | 78% | 80% | 95% |
| pytest-querycount 0.4.0, `no_n_plus_one` on every test | 103 | 20 | 0 | 102 | 27 | 0 | 79% | 84% | 100% |
| n-plus-one-sniffer (git `d4790f8`), `no_n_plus_one` on every test | 103 | 20 | 0 | 104 | 25 | 0 | 80% | 84% | 97% |
| nplusone 1.0.0 on SQLAlchemy 2.1.1 | 0 | 0 | 123 | 0 | 0 | 129 | n/a | 0% | 0% |
| nplusone 1.0.0 on SQLAlchemy 2.0.54 | 1 | 122 | 0 | 127 | 0 | 2 | 100% | 1% | 2% |

Precision is flagged BUG over everything flagged. "Recall (kinds it claims)"
counts only the BUG scenarios with a kind the tool says it detects:
relationship, column and repeated-statement loads for queryspy and
pytest-querycount, those and write loops for n-plus-one-sniffer, lazy loads
and unused eager loads for nplusone. A test that failed for another reason
than the tool's report is an error, never a pass. Per kind, precision /
recall; `n/a` where the tool does not claim the kind (or, for the defaults
of pytest-queryguard, where the rule is off by default), `-` where the tool
does not say which kind it found or reported nothing of that kind:

| tool | relationship-load | repeated-statement | column-load | write-loop | unused-eager-load | cartesian-load | unused-columns |
|---|---|---|---|---|---|---|---|
| pytest-queryguard, defaults | 100% / 100% | 98% / 96% | 100% / 92% | n/a | n/a | 88% / 100% | n/a |
| pytest-queryguard, all opt-in rules | 100% / 100% | 96% / 100% | 93% / 100% | 73% / 100% | 83% / 100% | 88% / 100% | 100% / 89% |
| queryspy | 90% / 100% | 64% / 89% | 81% / 100% | n/a | n/a | n/a | n/a |
| pytest-querycount | - / 100% | - / 100% | - / 100% | n/a | n/a | n/a | n/a |
| n-plus-one-sniffer | - / 98% | - / 98% | - / 92% | - / 100% | n/a | n/a | n/a |
| nplusone, SQLAlchemy 2.0 | - / 0% | n/a | n/a | n/a | 100% / 20% | n/a | n/a |

- **The corpus was written by the author of pytest-queryguard, for
  pytest-queryguard**, and that favours it. Its CLEAN scenarios include the
  shapes its rules were taught to leave alone (pagination, `IN` chunks,
  `selectinload` batches over many parents, `yield_per`), and three of them
  (`clean_94` to `clean_96`) use pytest-queryguard's own `ignore()` API,
  which the other tools cannot know. Each of the other tools has options I
  did not tune (thresholds, budgets, ignore blocks).
- The other three plugins find the N+1 shapes they claim: pytest-querycount
  flagged every BUG scenario of its kinds, n-plus-one-sniffer missed three
  (loops of two or three statements, not above its threshold of 3), queryspy missed five (the
  Core `select()`, `text()`, driver-SQL and f-string loops and the
  thread-pool lookups, as in 2026-10). Their cost is the CLEAN side: 25 to
  27 of the 129 CLEAN scenarios fail, against 2 for the defaults of
  pytest-queryguard. A tool that reports every repeated statement shape
  catches small loops and flags batching too.
- pytest-queryguard's defaults miss 17 BUG scenarios: 14 are write loops,
  unused eager loads and unused columns, which are opt-in, and three are
  loops of two or three statements below its thresholds. With every opt-in
  rule on it misses one and flags 8 CLEAN scenarios.
- queryspy names the kind. Its lower per-kind precision for
  `repeated-statement` comes from the CLEAN scenarios it flags, nearly all
  as repeated statements, and from BUG scenarios of other kinds (write loops,
  cartesian loads) that it reports as repeated statements.
- nplusone 1.0.0 does not import on SQLAlchemy 2.1 (`strategies.LazyLoader`
  moved). On 2.0.54 it runs, reports one unused eager load, no lazy load in
  any scenario (the 2.0-style `select()` does not go through the
  `Query.__iter__` it patches), and raises `AttributeError` in its
  `Query` patch on two `subqueryload` scenarios.
- The table of 2026-10-13 (141 scenarios, queryspy 0.5.0) is replaced by
  this one; the conclusions did not change.

## On PostgreSQL

Not run again on 2026-11-06: this section is the 141-scenario measurement of
2026-10-13. The same 141 scenarios, every configuration, with the database on PostgreSQL
instead of an SQLite file.

- Method: `QUERYGUARD_CORPUS_PG_URL=postgresql+pg8000://.../qg_corpus uv run
  python scripts/accuracy_report.py`. Each scenario starts from an empty
  `public` schema; sync scenarios use pg8000 and async ones asyncpg (the same
  URL with `+asyncpg`). The index check is off, as in the SQLite run: this
  page covers the N+1, repeat, column-load, write-loop and unused-eager-load
  rules. The run uses one database, so it cannot be split over several
  `pytest-xdist` workers; the regular `tests/corpus` tests also ran there
  (`pytest -n 0 tests/corpus`: 1096 passed, 34 xfailed as documented limits).
- Server: one PostgreSQL 17.11 in the official `postgres:17` Docker image on
  the machine of the SQLite run (Linux 7.0.0, x86_64, 13th Gen Intel Core
  i5-13500H, 16 threads). Other PostgreSQL versions were not run.
- Software: Python 3.13.13, SQLAlchemy 2.1.1, pytest 9.1.1,
  pg8000 1.31.5, asyncpg 0.31.0, Flask-SQLAlchemy 3.1.1, FastAPI 0.141.1,
  SQLModel not installed (141 of 143 scenarios ran), pytest-queryguard
  0.4.0rc2 at `main` a694bba plus the corpus changes of this page's pull
  request, run on 2026-10-13.

**Result: the output of `scripts/accuracy_report.py` on PostgreSQL 17 is
identical to the output on SQLite** (same commit, Python and packages): the same TP, FP and FN in every table and the same disagreements,
scenario by scenario. With the default configuration, 59 of 62 BUG scenarios
that its rules can report are flagged and 1 of 79 CLEAN scenarios are; with
all opt-in rules, 67 of 67 and 5 of 74. The report took 3 minutes 36 seconds on
PostgreSQL.

**What the first PostgreSQL run found.** Before the fix, two BUG scenarios
(`bug_23_get_or_create_loop`, `bug_30_flush_per_insert`) were listed as
`UNEXPLAINED` without a finding: their first `INSERT` raised `duplicate key
value violates unique constraint "tag_pkey"`. The seed data inserts explicit
primary keys; SQLite numbers the next row after the largest one, but a
PostgreSQL sequence still starts at 1. That was a bug of the corpus
([#120](https://github.com/MaciejZiel/pytest-queryguard/issues/120)), not of
the plugin: the scenario never reached its loop. The corpus now moves every
serial and identity sequence past the seeded rows after a scenario's setup
(`Ctx.sync_sequences()`). The run showed no difference between PostgreSQL and
SQLite that comes from the plugin.

## Real projects

The corpus is written by the author of the plugin. To see what the plugin does
on code nobody wrote for it, the workflow `.github/workflows/realworld.yml`
runs the test suites of open-source projects that use the SQLAlchemy 2 ORM:
SQLModel, the FastAPI full-stack template (on PostgreSQL), MLflow's
SQLAlchemy tracking store, SQLAdmin, Advanced Alchemy (its unit tests) and the
SQLAlchemy adapter of FastAPI Users. Each project is pinned to a release tag in
`scripts/realworld_projects.json`, which also holds its install and test
commands and what to reset (a fresh database) before each run.

Each suite runs twice in the same environment: once without the plugin
(`-p no:queryguard`) and once with it in warn mode, with the experimental
`unused-eager-load` and `unused-columns` kinds on. `scripts/realworld_summary.py`
compares the two runs' JUnit XML and reads the plugin's JSON file:

- tests that pass without the plugin and fail or are skipped with it (the
  plugin broke them), and tests that ran in only one of the two runs;
- `QueryGuardInternalWarning`s and pytest internal errors, recorded by a small
  probe plugin (`scripts/realworld_probe.py`);
- the findings by kind, with `file:line` samples, to be checked by hand for
  false positives;
- the extra wall time of the run with the plugin (one run each, so noisy).

The workflow is started by hand only, never on pushes or pull requests,
because it installs and runs third-party code. Findings never fail a job; a
job fails when the plugin broke something or when the project's tests did not
run without the plugin either (a harness problem), and a last job puts one
table of all projects on the run's page.

### Results of 2026-11-07

Plugin `main` at 4e1a888 (0.8.0 plus the unreleased changes), runs
[42389509168](https://github.com/MaciejZiel/pytest-queryguard/actions/runs/42389509168)
(four projects; MLflow and SQLAdmin stopped at collection under SQLAlchemy
2.1, since capped below 2.1) and
[42390905254](https://github.com/MaciejZiel/pytest-queryguard/actions/runs/42390905254)
(MLflow and SQLAdmin), on GitHub's `ubuntu-latest`. Every finding was read
against the project's code at its tag.

| project | tag (commit) | Python, SQLAlchemy | tests without the plugin (pass/fail/skip) | changed by the plugin | internal warnings/errors | findings | false | debatable |
|---|---|---|---|---|---|---|---|---|
| SQLModel | 0.0.24 (de78b8c) | 3.12, 2.0.57 | 220/35/3 | 0 | 0/0 | 9 | 9 | 0 |
| full-stack FastAPI template | 0.8.0 (2eb8f5b) | 3.12, 2.0.35, PostgreSQL 17 | 53/2/0 | 0 | 0/0 | 0 | 0 | 0 |
| MLflow tracking store | v2.22.0 (fd2d986) | 3.11, 2.0.57 | 219/1/16 | 0 | 0/0 | 179 | 78 | 54 |
| SQLAdmin | 0.20.1 (ab2d48a) | 3.12, 2.0.57 | 177/2/3 | 0 | 0/0 | 51 | 16 | 1 |
| Advanced Alchemy (unit tests) | v1.4.4 (6cc26ef) | 3.12, 2.0.41 | 564/0/3 | 0 | 0/0 | 0 | 0 | 0 |
| FastAPI Users SQLAlchemy | v7.0.0 (6397ace) | 3.12, 2.0.57 | 6/0/0 | 0 | 0/0 | 0 | 0 | 0 |

The failures without the plugin come from the projects' unpinned test
dependencies (SQLModel's OpenAPI snapshots, the template's `called_once_with`
on a newer `mock`); they fail the same way with the plugin. The plugin
changed no test's outcome, raised no internal warning or error, and its wall
time was within the noise of one run each (from −28 % to +4 %).

What the findings were:

- **SQLModel, 9 relationship-loads, all false.** The many-to-many tutorials
  create three heroes, commit, refresh each and print each hero's teams on a
  line of its own. No query loaded those objects, and all parents of unknown
  origin counted as one group. Fixed: such parents are grouped by the line
  that read the relationship (scenario `clean_132`). The findings were also
  located in SQLModel's `Session.execute` override, an editable install, not
  in the tutorial; methods of a `Session` subclass are no longer call sites
  (scenario `clean_131`). With both fixes the branch's run
  [42393464145](https://github.com/MaciejZiel/pytest-queryguard/actions/runs/42393464145)
  has no SQLModel finding and the same findings elsewhere.
- **MLflow, 40 relationship-loads, true.** `search_traces` loads a page of
  traces and `to_mlflow_entity()` reads each trace's `tags` and
  `request_metadata` lazily (`sqlalchemy_store.py:1744`,
  `dbmodels/models.py:693-694`): two queries per trace.
- **MLflow, 7 unused-columns, true.** `_get_deleted_runs` loads whole runs to
  return their ids, `_log_inputs_impl` loads whole datasets (with their
  schema and profile) to read name, digest and id; one is in a test.
- **MLflow, 132 findings (113 repeated-statements, 19 relationship-loads)
  across as many Sessions as statements.** Every store method opens its own
  Session, and the tests call the store many times. About 54 come from a loop
  in the test (`test_log_batch_limits` calls `get_metric_history` for 1000
  keys; tests seed traces and runs in loops): the shape of
  `bug_47_session_per_item`, an N+1 of the test's own loop, counted as
  debatable. About 78 come from calls on separate lines with no loop
  (`test_set_experiment_tag`): false, a known limit (scenario `clean_133`):
  the parents are grouped by the query line in the shared `_get_experiment()`,
  not by the lines that called it, and by default the test is one unit. The
  split is by reading the first stack of each finding, so approximate.
  `queryguard_scope = session` removes all 132.
- **SQLAdmin, 5 relationship-loads, true.** The list and export pages read a
  relationship that the query did not load for each row through
  `_lazyload_prop`, which opens a new Session per row (`models.py:883`,
  `887`); "delete selected" deletes each row in a Session of its own and
  loads its `complaints` for the cascade.
- **SQLAdmin, 29 unused-eager-loads, true (experimental kind).** The export
  loads every relationship of the list view with `selectinload` but writes
  only `name` and `status`; the edit form loads relationships the request does
  not use. 15 of them, in the async tests, have no location
  (`<unknown location>`), not explained yet.
- **SQLAdmin, 16 false and 1 debatable.** The tests post several forms in a
  row; each request loads the form's options (`SELECT addresses.id ... FROM
  addresses`) and the edited user's relationships again: separate requests
  counted as one unit by default ([Tests with several sessions or
  requests](several-sessions.md)). The options query runs through
  `anyio.to_thread.run_sync(session.execute, stmt)`, so it has no call site
  (a documented [limitation](limitations.md)). The debatable one mixes the
  per-row deletes with separate page requests.

The true findings were not reported to the projects.

## Known limits

See also [Limitations](limitations.md).

- Deliberate polling is reported by the default repeat rule, and so are the
  pagination and chunk shapes the rule for them does not read (composite keyset
  conditions, string or date keyset bounds, MySQL's `LIMIT offset, count`,
  `IN` lists shorter than 50 values, the same keys loaded twice).
- The rule for pagination and chunks reads parameters only, so a loop that
  looks exactly like paging or chunks is not reported: 50 or more keys per item,
  the same number for every item, or an integer keyset bound per row whose rows
  are at least a page apart. No scenario in the corpus has that shape.
- Below its thresholds a loop is not reported by default; the opt-in rules that
  lower them report legitimate code too (a cache, two objects after a commit).
- The write-loop rule cannot tell seeding or per-row inserts with error handling
  from a write loop.
- The cartesian-load rule reads the loader strategies, not the rows: it
  reports two small collections joined on purpose.
- The unused-eager-load check (experimental) does not see a collection read
  through `inspect(obj).attrs` or `__dict__`, and reports it as unused.
- The unused-columns check (experimental) does not see instances loaded
  through a relationship. It leaves out queries that pin the primary key (one
  row each), but otherwise reports three rows as readily as a thousand.
- The parents of objects the test made (or loaded before the test body) are
  grouped by the line that read the relationship: reading it through one
  helper for a few objects on separate lines is reported like a loop, and a
  loop that reads it on two lines (two branches) counts each line on its own,
  so each needs the threshold. A service that opens a Session per call and is
  called on separate lines is reported under the default scope
  (`clean_133`).
- Strict mode stops the test at the first N+1, so later ones are not
  reported.
- `--queryguard-write-patch` writes a patch only for a `select(Class)` (or
  `select(module.Class)`) written on the line that ran it, or assigned once to
  the variable that line runs, and not inside a comprehension: 26 of the 51
  relationship-load BUG scenarios.
- The corpus is small and written by one person; precision and recall from 268
  scenarios have wide error bars. It ran on SQLite, and its first 141 scenarios
  on PostgreSQL 17 (pg8000 and asyncpg); other PostgreSQL versions, MySQL and
  other drivers were not run.
- The SQLModel scenarios run only where SQLModel is installed.
