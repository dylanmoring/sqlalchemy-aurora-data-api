# Fork Analysis — `sqlalchemy-aurora-data-api` (dialect layer)

> Comprehensive analysis of this fork vs. its upstream baseline.
> Generated 2026-06-10. Companion document: `FORK-ANALYSIS.md` in the
> **aurora-data-api** repo (the DBAPI driver this dialect sits on top of).
> **Read both** — many features here require a matching change in the driver fork.

## Baseline & how to reproduce

- **Upstream:** `https://github.com/chanzuckerberg/sqlalchemy-aurora-data-api`
  (added as the `upstream` git remote during this analysis).
- **Merge-base:** `d68c2eeab531a1db54ec5677e918249486492dfb`
- **Divergence:** 18 custom commits, **+1280 / −15 lines**; fork is **0 commits
  behind** upstream `main` (contains all upstream work + ours). Unlike the driver
  fork, there are **no missing upstream commits** here.
- **Diff everything we changed:** `git diff d68c2ee..HEAD`

---

## 🔴 Action items (risks specific to the dialect, ranked)

| # | Severity | Location | Issue |
|---|----------|----------|-------|
| 1 | **Med** | `__init__.py` `_patch_pg_catalog_char_columns_for_data_api()` (called at import, ~line 341) | **Import-time, process-global mutation of SQLAlchemy internals.** Mutates `.type` on SA's *shared* `pg_catalog` Table singletons. The comment claims "dialect-scoped," but these are global SA module objects — importing this package changes catalog reflection for **every** PG dialect in the same process (plain psycopg2 included). The `column_expression` only fires in SELECT lists, limiting blast radius, but it is not truly isolated. |
| 2 | **Med** | `__init__.py:303-335` | **Hard-coded coupling to `pg_catalog` layout.** The 18 `CHAR` targets and 4 `int2vector` columns are enumerated by name with a comment pinning "SA 2.0.50" line numbers. Any upstream rename/restructure silently no-ops the patch (guards fail open) → reflection breaks again **at runtime, not import**. Pin or assert the SA version. |
| 3 | **Med** | `setup.py` | **Driver pin floats.** `install_requires` pins `aurora-data-api @ git+https://github.com/dylanmoring/aurora-data-api.git` with **no tag/commit** (commit `17c8050` dropped the `@async-data-api` ref). The dialect's runtime behavior tracks whatever that fork's default branch HEAD is — no reproducible builds, no version gate on the tight cross-repo contract. |
| 4 | **Low** | `__init__.py` `_INTEGER_CAST_POSITIONS` | **Compiler hook hardcodes internal SA catalog function names + arg positions.** Brittle if upstream changes its catalog-query builders (acknowledged tradeoff vs. copy-pasting ~200 LOC). |
| 5 | **Low** | `_AuroraDataAPIAsyncMixin` (~line 402-409) | **Mixin invariant is convention-only.** "No DB-specific attr on the mixin" is documented but unenforced; `supports_statement_cache` already had to be re-duplicated onto both concrete classes (`43cdbe8`) because SA reads `cls.__dict__` directly. A future DB-specific attr added to the mixin would silently clobber one backend. |

> The driver fork carries the higher-severity confirmed regressions (nested-array
> `KeyError`, sync auto-begin removal, async oversize-retry swallow). See its
> `FORK-ANALYSIS.md` — several affect this dialect at runtime.

---

## ✨ Features added

- **Async dialects (asyncpg-style AsyncAdapt facade)** — `_AuroraDataAPIAsyncMixin`
  (driver = `aurora_data_api.async_driver`, `is_async=True`) + concrete
  `AuroraMySQLDataAPIAsyncDialect` / `AuroraPostgresDataAPIAsyncDialect`.
  `connect()` greenlet-bridges `await_only(dbapi.connect(...))` and wraps the
  result in the **driver fork's** `AuroraDataAPIAsyncAdaptConnection`;
  `get_pool_class` returns `AsyncAdaptedQueuePool` to pass the is_async pool guard.
  Registered in `register_dialects()` + `setup.py` entry_points.
  *(`f9d5700`, `d548001`, `d6dfc14`)*
- **Explicit `do_*` transaction handling** — `do_begin` calls the **driver fork's**
  `dbapi_connection.start_transaction()`; `do_commit`/`do_rollback` are explicit
  passthroughs. Lets the driver avoid implicit auto-transactions. *(`8b9162e`)*
- **`pg_catalog` reflection patches for Data API** — `_patch_pg_catalog_char_columns_for_data_api()`:
  18 `CHAR` catalog columns → `_CharCastedToText` (`CAST(col AS TEXT)`, because the
  Data API rejects internal `"char"` results); 4 `int2vector`/`oidvector`
  `pg_index` columns → `_VectorCastedToIntArray`
  (`string_to_array(col::text,' ')::int[]`). Cast to **`int[]` not text**
  specifically so SA index reflection's bitwise ops on `indoption` keep working.
  *(`4cbf5c7`, `8d2305c`)*
- **Integer-cast compiler hook** — `_AuroraDataAPIPGCompiler.visit_function` wraps
  `int` args of `generate_subscripts` / `pg_get_indexdef` in `CAST(... AS INTEGER)`
  because the Data API marshals every Python `int` as `bigint` and those PG
  functions have no bigint overload. *(`d6dfc14`)*
- **`supports_native_decimal = True`** on both PG and MySQL dialects — the Data API
  returns `Decimal` natively, so `Numeric(asdecimal=False)` now correctly yields
  `float`. *(`d548001`)*
- **Datetime/timezone bind casts** — `_ADA_DATETIME_MIXIN.bind_expression` casts to
  the type *instance* (preserving `timezone=True`) → renders
  `CAST(... AS TIMESTAMP WITH TIME ZONE)`, standing in for the missing TIMESTAMPTZ
  Data API typeHint; binds use `isoformat(timespec="microseconds")` to carry the
  UTC offset. *(`b352cd9`)*
- **TIMETZ result workaround** — `_ADA_TIME.column_expression` emits
  `type_coerce(cast(col, Text), self)` for tz-aware times because the Data API
  rejects TIMETZ *result columns*; the result_processor parses the string back to a
  tz-aware `datetime.time`. *(`2f0259b`)*
- **JSON canonicalisation handling** — `JSONTest.test_round_trip_custom_json`
  override asserts semantic JSON equality instead of byte-exact text (the Data API
  strips whitespace / may reorder keys). *(`fada78c`)*

## 🐛 Bug fixes

- **Datetime microsecond truncation** — `_ADA_DATETIME_MIXIN.ms` did
  `zfill(6)[:-3]`, silently truncating every timestamp bind to milliseconds. Now
  full 6-digit. *(`d548001`)* — *(depends on the Data API accepting 6-digit
  microseconds; verify on the live service.)*
- **Timezone flag erased in bind cast** — old `bind_expression` cast to the bare
  `sa_type` class → `CAST(:p AS TIMESTAMP)`, dropping `timezone=True`. Now casts to
  the instance. *(`b352cd9`)*
- **Async rowcount** — `supports_sane_rowcount[_returning]` flipped from defensive
  `False` to `True` on the async mixin (was costing 28 skips); the async cursor
  reports rowcount identically to sync. *(`2f0259b`)*
- **`supports_statement_cache` placement** — SA's `_supports_statement_cache` reads
  `cls.__dict__` directly, so the flag must live on each concrete async dialect,
  not just the mixin; duplicated onto both subclasses. *(`43cdbe8`)*
- **`drop_all` sync_engine patch** — compliance conftest wraps
  `drop_all_tables_from_metadata` to swap `AsyncEngine`→`sync_engine`, fixing an
  `_AsyncGeneratorContextManager` mismatch. *(`af8aa49`)*
- **AsyncAdapt connection rename** — `SyncAdaptedConnection(async_conn)` replaced
  with `AuroraDataAPIAsyncAdaptConnection(dbapi, async_conn)` matching the renamed
  driver-fork class. *(`d548001`)*

## ♻️ Refactors

| Refactor | Verdict |
|----------|---------|
| `d6dfc14` "Simplify async dialects" — extract `_AuroraDataAPIAsyncMixin`, collapse the `_patch_generate_subscripts` factory into a module-level compiler, drop dead imports, reconcile `requirements.py` | **Worthwhile** — genuine de-duplication, not churn. |
| `af4d81c` "Tighten docs from the simplify pass" | Docstring/comment-only. |
| Integer-cast hook is table-driven (`_INTEGER_CAST_POSITIONS`) instead of forking ~200 LOC of catalog builders | **Worthwhile** (brittleness tradeoff noted in action #4). |

No churn-only/unnecessary refactors were found in this repo. (`_ADA_SA_JSON`
pre-existed at the merge-base — no SA_JSON bug was introduced or fixed here despite
the early commit subject.)

## 🔗 Cross-repo dependencies (this dialect requires the driver fork)

Every item below will break if used against **upstream** `aurora-data-api`:

1. **`aurora_data_api.async_driver` module** — imported by `import_dbapi`/`driver`
   on both async dialects. Upstream has no async driver.
2. **`AuroraDataAPIAsyncAdaptConnection(dbapi, async_conn)`** — must be a driver-fork
   subclass of SA's `AsyncAdapt_dbapi_connection`.
3. **`start_transaction()` on the DBAPI connection** — called by every `do_begin`;
   not standard DBAPI. This is the "avoid auto-transactions" contract.
4. **Async cursor rowcount semantics** — `supports_sane_rowcount=True` assumes the
   async cursor reports `numberOfRecordsUpdated`/`len(records)` like sync.
5. **Native `Decimal` return** — `supports_native_decimal=True` assumes the driver
   returns `Decimal`.
6. **`list` → Data API `arrayValue`/nested `arrayValues`** binding (driver-side).
7. **JSON wire canonicalisation** — the JSONTest override assumes non-byte-exact
   round-trip.
8. **Driver `setup.py` pin** — see action #3 (unpinned git source).

> ⚠️ None of these contracts are pinned by a version or asserted by a test. A push
> to the driver fork's default branch can change this dialect's behavior silently.

## 🧪 Compliance suite

- **`test/compliance` (async)** drives `postgresql+auroradataapiasync://`;
  **`test/compliance_sync`** drives `postgresql+auroradataapi://`. Both star-import
  `sqlalchemy.testing.suite` + the `JSONTest` override.
- The **async conftest** is heavy: forces `asyncio=True` on the testing engine,
  swaps `AsyncEngine`→`sync_engine` for citext/hstore install + schema pre-create,
  and in `pytest_sessionstart` rebinds `config.db` across three namespaces and
  overrides `TablesTest.setup_bind` / `TestBase.connection` /
  `drop_all_tables_from_metadata` to route through the sync proxy (greenlet-bridged).
  **Test-only, but highly SA-version-sensitive — will likely break on SA upgrades.**
- The **sync conftest** is minimal but includes a `pytest_configure` that remaps
  `[db] default` (the shared async URL in `setup.cfg`) → the `sync` URL, so the dir
  actually exercises the sync dialect rather than silently re-running async.
  *(`5df8203`)*
- **`requirements.py`** opens ~50 PG features the generic baseline closes (json,
  CTEs incl. recursive, window functions, computed/identity columns, bitwise
  and/or/xor/not, fetch-first/ties, views, uuid, materialized views, time_timezone,
  datetime/date historic, update_from/delete_from, …). Deliberately **closed**:
  `supports_bitwise_shift` (int4<<bigint), `temp_table_reflection` (stateless
  sessions), `unicode_ddl`/`percent_schema_names` (param charset `[A-Za-z0-9_]`),
  `array_type` (multi-dim *result* wall), collation reflection (Aurora naming).
- **`SKIPS.md`** documents 252 categorized skips (mirror of the driver fork):
  165 features PG genuinely lacks, 39 Data API service walls, 25 not-yet-implemented
  (autocommit/isolation_level, default_schema_name_switch), 23 tail. Records 66
  skips resolved 2026-06-10.

---

## Note on git remotes

An `upstream` remote was added to both repos during this analysis (pointing at
`chanzuckerberg`). Keep it — it makes future `git diff upstream/main...HEAD` and
upstream-bugfix tracking trivial.
