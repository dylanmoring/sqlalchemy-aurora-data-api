"""
SQLAlchemy compliance suite Requirements for the Aurora Data API
postgres dialect.

An earlier version closed all reflection requirements wholesale because
two Data API quirks sank every catalog query: ``generate_subscripts``
rejecting the ``bigint`` literal SA passes, and ``"char"`` /
``int2vector`` / ``oidvector`` result columns raising
``UnsupportedResultException``. The dialect now patches both at the
type / compiler layer (``_AuroraDataAPIPGCompiler.visit_function`` and
``_patch_pg_catalog_char_columns_for_data_api``), so those queries run
as-is and this class inherits ``SuiteRequirements`` defaults instead.

Reflection requirements the base suite enables by default are therefore
exercised again. Others it leaves off for third-party dialects
(``view_reflection``, ``comment_reflection``, ...) stay off — by base
default, not a Data-API exclusion.
"""
from sqlalchemy.testing.requirements import SuiteRequirements


class Requirements(SuiteRequirements):
    # Inherits SuiteRequirements defaults (see module docstring). Add a
    # targeted ``exclusions.closed()`` override here only for a residual
    # Data API limitation a live suite run actually demonstrates; open one
    # the base leaves off with ``exclusions.open()`` once a run confirms
    # the catalog patches cover it.
    pass
