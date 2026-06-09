"""
SQLAlchemy compliance suite Requirements declaration for the Aurora
Data API postgres dialect.

Two Aurora Data API quirks used to sink *every* SQLAlchemy reflection
query, so an earlier version of this file closed the reflection
requirements wholesale "until/unless we [...] swap the failing PG
queries for ones the Data API can run":

* ``generate_subscripts(int2vector, <bigint literal>)`` returned
  ``ER_UNDEF_FUNC`` — Data API marshals Python ``int`` as ``bigint`` and
  PG has no ``bigint`` overload. This sank any reflection query touching
  ``pg_index.indkey`` (primary keys, unique constraints, indexes,
  foreign keys, table introspection, ...).
* Result sets containing internal pg ``"char"`` / ``int2vector`` /
  ``oidvector`` columns (``pg_class.relkind``, ``pg_index.indoption``,
  etc.) failed with ``UnsupportedResultException``.

The dialect now fixes both at the type / compiler layer, so those
catalog queries run as-is — see ``sqlalchemy_aurora_data_api``:

* ``_AuroraDataAPIPGCompiler.visit_function`` wraps the integer args of
  ``generate_subscripts`` / ``pg_get_indexdef`` in ``CAST(... AS
  INTEGER)``.
* ``_patch_pg_catalog_char_columns_for_data_api`` rewrites the 18 pg
  ``"char"`` catalog columns to ``CAST(... AS TEXT)`` and the
  ``int2vector`` / ``oidvector`` columns on ``pg_index`` to ``int[]``.

That satisfies the original "until/unless" condition, so reflection is
no longer closed wholesale — this class inherits ``SuiteRequirements``
defaults and lets the compliance suite exercise (and validate) the
patches. Concretely, the constraint / index / table-reflection
requirements that the base suite enables (``index_reflection``,
``primary_key_constraint_reflection``, ``foreign_key_constraint_reflection``,
``unique_constraint_reflection``, ``table_reflection``,
``temp_table_reflection``, ...) are now exercised instead of skipped.

Note that several other reflection requirements (``view_reflection``,
``comment_reflection``, ``reflect_indexes_with_expressions``, ...)
remain *off* — but at the ``SuiteRequirements`` base default, not via a
Data-API exclusion. Third-party dialects must opt those in explicitly;
do so (with ``exclusions.open()``) once a live run confirms the patches
cover them.

If instead a live run surfaces a *genuine residual* Data API limitation
the patches don't cover — another catalog column of an unsupported
type, or a behavior the HTTPS surface can't model — close that specific
requirement with ``exclusions.closed()`` and a one-line note citing the
exact failure, rather than re-closing the whole reflection bucket.
"""
from sqlalchemy.testing.requirements import SuiteRequirements


class Requirements(SuiteRequirements):
    # Reflection requirements are intentionally left at SuiteRequirements
    # defaults: the dialect's catalog patches (see module docstring) make
    # the pg_catalog queries SA's reflection issues runnable through the
    # Data API. Add targeted ``exclusions.closed()`` overrides here only
    # for limitations a live suite run actually demonstrates.
    pass
