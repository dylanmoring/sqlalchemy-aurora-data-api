"""
sqlalchemy-aurora-data-api
"""

import json, datetime, re

from sqlalchemy import cast, func, util
import sqlalchemy.sql.sqltypes as sqltypes
from sqlalchemy.dialects.postgresql.base import PGDialect
from sqlalchemy.dialects.postgresql import JSON, JSONB, UUID, DATE, TIME, TIMESTAMP, ARRAY, ENUM
from sqlalchemy.dialects.mysql.base import MySQLDialect

import aurora_data_api


class _ADA_SA_JSON(sqltypes.JSON):
    def bind_expression(self, value):
        return cast(value, sqltypes.JSON)


class _ADA_JSON(JSON):
    def bind_expression(self, value):
        return cast(value, JSON)


class _ADA_JSONB(JSONB):
    def bind_expression(self, value):
        return cast(value, JSONB)


class _ADA_ENUM(ENUM):
    def bind_expression(self, value):
        return cast(value, self)


# TODO: is TZ awareness needed here?
class _ADA_DATETIME_MIXIN:
    iso_ts_re = re.compile(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+")

    @staticmethod
    def ms(value):
        # Six-digit zero-padded microsecond component. (The old version
        # truncated to milliseconds — ``[:-3]`` — which silently lost the
        # bottom three digits of the input timestamp and made round-tripping
        # ``datetime`` values lossy. Data API accepts six-digit fractional
        # seconds; SQLAlchemy's compliance suite verifies full precision.)
        return str(value.microsecond).zfill(6)

    def bind_processor(self, dialect):
        def process(value):
            return value.isoformat() if isinstance(value, self.py_type) else value

        return process

    def bind_expression(self, value):
        return cast(value, self.sa_type)

    def result_processor(self, dialect, coltype):
        def process(value):
            # When the microsecond component ends in zeros, they are omitted from the return value,
            # and datetime.datetime.fromisoformat can't parse the result (example: '2019-10-31 09:37:17.31869
            # '). Pad it.
            if isinstance(value, str) and self.iso_ts_re.match(value):
                value = self.iso_ts_re.sub(lambda match: match.group(0).ljust(26, "0"), value)
            if isinstance(value, str):
                try:
                    return self.py_type.fromisoformat(value)
                except AttributeError:  # fromisoformat not supported on Python < 3.7
                    if self.py_type == datetime.date:
                        return datetime.datetime.strptime(value, "%Y-%m-%d").date()
                    if self.py_type == datetime.time:
                        return datetime.datetime.strptime(value, "%H:%M:%S").time()
                    if "." in value:
                        return datetime.datetime.strptime(value, "%Y-%m-%d %H:%M:%S.%f")
                    return datetime.datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
            return value

        return process


class _ADA_DATE(_ADA_DATETIME_MIXIN, DATE):
    py_type = datetime.date
    sa_type = sqltypes.Date

    def bind_processor(self, dialect):
        def process(value):
            return value.strftime("%Y-%m-%d") if isinstance(value, self.py_type) else value

        return process


class _ADA_TIME(_ADA_DATETIME_MIXIN, TIME):
    py_type = datetime.time
    sa_type = sqltypes.Time

    def bind_processor(self, dialect):
        def process(value):
            return value.strftime("%H:%M:%S.") + self.ms(value) if isinstance(value, self.py_type) else value

        return process


class _ADA_TIMESTAMP(_ADA_DATETIME_MIXIN, TIMESTAMP):
    py_type = datetime.datetime
    sa_type = sqltypes.DateTime

    def bind_processor(self, dialect):
        def process(value):
            return value.strftime("%Y-%m-%d %H:%M:%S.") + self.ms(value) if isinstance(value, self.py_type) else value

        return process


class _ADA_ARRAY(ARRAY):
    def bind_processor(self, dialect):
        def process(value):
            # FIXME: escape strings properly here
            return "\v".join(value) if isinstance(value, list) else value

        return process

    def bind_expression(self, value):
        return func.string_to_array(value, "\v")


class AuroraMySQLDataAPIDialect(MySQLDialect):
    # See https://docs.sqlalchemy.org/en/13/core/internals.html#sqlalchemy.engine.interfaces.Dialect
    driver = "aurora_data_api"
    default_schema_name = None
    supports_native_decimal = True
    colspecs = util.update_copy(
        MySQLDialect.colspecs,
        {
            sqltypes.Date: _ADA_DATE,
            sqltypes.Time: _ADA_TIME,
            sqltypes.DateTime: _ADA_TIMESTAMP,
        },
    )
    supports_statement_cache = True

    @classmethod
    def import_dbapi(cls):
        return aurora_data_api

    def _detect_charset(self, connection):
        return connection.execute("SHOW VARIABLES LIKE 'character_set_client'").fetchone()[1]

    def _extract_error_code(self, exception):
        return exception.args[0].value

    def do_begin(self, dbapi_connection):
        dbapi_connection.start_transaction()

    # (optional but explicit) passthroughs; SA defaults would call these anyway
    def do_commit(self, dbapi_connection):
        dbapi_connection.commit()

    def do_rollback(self, dbapi_connection):
        dbapi_connection.rollback()


def _patch_generate_subscripts_for_data_api():
    """Aurora Data API marshals Python ``int`` values as ``bigint``. PG's
    ``generate_subscripts`` only has overloads taking ``anyarray, integer[,
    integer]`` — no ``bigint`` overload — so SA's catalog queries that pass
    a literal ``1`` (in ``_constraint_query`` / ``_index_query``) fail with
    ``function generate_subscripts(int2vector, bigint) does not exist``.

    Diagnosed and worked around the same way at
    https://github.com/sqlalchemy/sqlalchemy/discussions/11269 — wrap the
    literal in ``sql.cast(1, INTEGER)`` so PG sees an explicit integer.

    We patch ``sql.func.generate_subscripts`` invocations at SQL emit time
    via a SQLAlchemy compiler hook bound to our dialect names. This avoids
    copy-pasting ~200 LOC of upstream catalog-query builders and survives
    upstream changes to those builders.
    """
    from sqlalchemy.sql import functions as sql_functions
    from sqlalchemy.sql.elements import BindParameter
    from sqlalchemy.ext.compiler import compiles
    from sqlalchemy import INTEGER, cast as sql_cast

    # ``sql.func.generate_subscripts`` produces an instance of the generic
    # ``Function`` class with ``name="generate_subscripts"``. We can't
    # ``@compiles`` against a generic Function by name, but we CAN hook
    # ``visit_function`` on our compiler. Define a per-dialect compiler
    # class below.

    # Register the compiler against both async + sync dialect names.

    from sqlalchemy.dialects.postgresql.base import PGCompiler

    class _AuroraDataAPIPGCompiler(PGCompiler):
        def visit_function(self, func, *args, **kw):
            if (
                func.name == "generate_subscripts"
                and len(func.clauses.clauses) == 2
            ):
                second = func.clauses.clauses[1]
                # Only wrap if it's a bare-int bind parameter or an integer
                # literal — avoids double-casting if someone already cast it.
                if isinstance(second, BindParameter) and isinstance(
                    second.value, int
                ):
                    new_clauses = [
                        func.clauses.clauses[0],
                        sql_cast(second, INTEGER),
                    ]
                    new_func = sql_functions.Function(
                        "generate_subscripts", *new_clauses
                    )
                    return super().visit_function(new_func, *args, **kw)
            return super().visit_function(func, *args, **kw)

    return _AuroraDataAPIPGCompiler


_AuroraDataAPIPGCompiler = _patch_generate_subscripts_for_data_api()


def _patch_pg_catalog_char_columns_for_data_api():
    """Aurora Data API rejects result sets that contain Postgres' internal
    one-byte ``"char"`` type with ``UnsupportedResultException: The result
    contains the unsupported data type "CHAR"``. AWS documents the
    workaround as casting to TEXT:
    https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/data-api.troubleshooting.html#data-api.troubleshooting.unsupported-result

    SA's ``sqlalchemy.dialects.postgresql.pg_catalog`` Table objects declare
    18 such columns (``pg_class.relkind``, ``pg_attribute.attidentity``,
    ``pg_constraint.contype``, etc.) as ``Column("...", CHAR)``. Every
    catalog SELECT that includes these columns trips the Data API.

    The fix: subclass the SA ``CHAR`` type and override
    ``column_expression`` — SA calls that hook on every column in a SELECT
    list to give the type a chance to wrap itself (CAST, COALESCE,
    decode, etc.). Returning ``sql.cast(col, Text)`` makes every catalog
    query emit ``CAST(pg_class.relkind AS TEXT)``. Then we walk the
    ``pg_catalog`` tables once at import and replace the ``.type`` on
    each affected column.

    Critically, this is dialect-scoped (we only mutate the type to our
    subclass — the original ``CHAR`` semantics are preserved for any
    other dialect that imports SA in the same process), and it leaves
    WHERE-clause / JOIN-on column references untouched (those don't go
    through ``column_expression``).
    """
    from sqlalchemy.dialects.postgresql import pg_catalog
    from sqlalchemy.sql.sqltypes import CHAR
    from sqlalchemy import sql, Text

    class _CharCastedToText(CHAR):
        """``CHAR`` that emits ``CAST(<col> AS TEXT)`` in SELECT lists."""
        def column_expression(self, col):
            return sql.cast(col, Text)

    # The catalog columns SA's PG dialect declares with CHAR. Reference:
    # sqlalchemy/dialects/postgresql/pg_catalog.py (lines 120, 121, 130,
    # 145, 146, 149, 160, 161, 209, 210, 214, 215, 228, 237, 238, 239, 294,
    # 304 in SA 2.0.50).
    targets = [
        (pg_catalog.pg_class, "relkind"),
        (pg_catalog.pg_class, "relpersistence"),
        (pg_catalog.pg_class, "relreplident"),
        (pg_catalog.pg_type, "typtype"),
        (pg_catalog.pg_type, "typcategory"),
        (pg_catalog.pg_type, "typdelim"),
        (pg_catalog.pg_type, "typalign"),
        (pg_catalog.pg_type, "typstorage"),
        (pg_catalog.pg_attribute, "attstorage"),
        (pg_catalog.pg_attribute, "attalign"),
        (pg_catalog.pg_attribute, "attidentity"),
        (pg_catalog.pg_attribute, "attgenerated"),
        (pg_catalog.pg_constraint, "contype"),
        (pg_catalog.pg_constraint, "confupdtype"),
        (pg_catalog.pg_constraint, "confdeltype"),
        (pg_catalog.pg_constraint, "confmatchtype"),
        (pg_catalog.pg_am, "amtype"),
        (pg_catalog.pg_collation, "collprovider"),
    ]
    casted = _CharCastedToText()
    for table, colname in targets:
        col = table.c.get(colname)
        if col is not None and isinstance(col.type, CHAR):
            col.type = casted


_patch_pg_catalog_char_columns_for_data_api()


class AuroraPostgresDataAPIDialect(PGDialect):
    # See https://docs.sqlalchemy.org/en/13/core/internals.html#sqlalchemy.engine.interfaces.Dialect
    driver = "aurora_data_api"
    default_schema_name = None
    # Data API returns numeric/decimal columns as Decimal objects natively.
    # Without this flag, ``Numeric(asdecimal=False)`` columns return Decimal
    # because SQLAlchemy doesn't apply ``to_float`` in its result processor.
    supports_native_decimal = True
    # Wraps ``generate_subscripts(int2vector, 1)`` so Data API's bigint
    # marshalling of the literal ``1`` doesn't break PG catalog reflection.
    statement_compiler = _AuroraDataAPIPGCompiler
    colspecs = util.update_copy(
        PGDialect.colspecs,
        {
            sqltypes.JSON: _ADA_SA_JSON,
            JSON: _ADA_JSON,
            JSONB: _ADA_JSONB,
            sqltypes.Date: _ADA_DATE,
            sqltypes.Time: _ADA_TIME,
            sqltypes.DateTime: _ADA_TIMESTAMP,
            sqltypes.Enum: _ADA_ENUM,
            ARRAY: _ADA_ARRAY,
        },
    )
    supports_sane_multi_rowcount = False
    supports_statement_cache = True


    @classmethod
    def import_dbapi(cls):
        return aurora_data_api

    def _extract_error_code(self, exception):
        return exception.args[0].value

    def do_begin(self, dbapi_connection):
        dbapi_connection.start_transaction()

    def do_commit(self, dbapi_connection):
        dbapi_connection.commit()

    def do_rollback(self, dbapi_connection):
        dbapi_connection.rollback()


import importlib
from sqlalchemy import pool
from sqlalchemy.util.concurrency import await_only

# Explicitly load the base postgres and mysql provisioning so their
# ``@for_db(...)`` registrations (e.g. ``temp_table_keyword_args``,
# ``create_db``, ``drop_db``) are available for compliance-suite fixtures.
# Without this they never load — our dialect's ``__module__`` is the bare
# ``sqlalchemy_aurora_data_api``, so ``cls.load_provisioning`` resolves
# its package to ``""`` and silently fails to import any provision module.
from sqlalchemy.dialects.postgresql import provision as _pg_provision  # noqa: F401
from sqlalchemy.dialects.mysql import provision as _mysql_provision  # noqa: F401

# ───────────────────────────────────────────────────────────────
# 1) Async MySQL variant
class AuroraMySQLDataAPIAsyncDialect(AuroraMySQLDataAPIDialect):
    """AsyncIO variant of the DataAPI MySQL dialect."""
    driver = "aurora_data_api.async_driver"
    is_async = True   # signal that this dialect is meant for asyncio
    supports_statement_cache = True
    supports_sane_rowcount = False
    supports_sane_rowcount_returning = True

    @classmethod
    def import_dbapi(cls):
        # pull in your async driver module instead of the sync one
        return importlib.import_module("aurora_data_api.async_driver")

    @classmethod
    def get_pool_class(cls, url):
        # Match SQLAlchemy's reference shape for async dialects (see
        # PGDialect_asyncpg.get_pool_class). Without this override the
        # create_engine guard refuses QueuePool against an is_async dialect.
        return pool.AsyncAdaptedQueuePool

    def connect(self, *cargs, **cparams):
        dbapi = self.dbapi  # the async_driver module above
        async_conn = await_only(dbapi.connect(**cparams))
        return dbapi.AuroraDataAPIAsyncAdaptConnection(dbapi, async_conn)

    def do_begin(self, dbapi_connection):
        dbapi_connection.start_transaction()

    def do_commit(self, dbapi_connection):
        dbapi_connection.commit()

    def do_rollback(self, dbapi_connection):
        dbapi_connection.rollback()


# 2) Async Postgres variant
class AuroraPostgresDataAPIAsyncDialect(AuroraPostgresDataAPIDialect):
    """AsyncIO variant of the DataAPI Postgres dialect."""
    driver = "aurora_data_api.async_driver"
    is_async = True
    supports_statement_cache = True
    supports_sane_rowcount = False
    supports_sane_rowcount_returning = True

    @classmethod
    def import_dbapi(cls):
        return importlib.import_module("aurora_data_api.async_driver")

    @classmethod
    def get_pool_class(cls, url):
        return pool.AsyncAdaptedQueuePool

    def connect(self, *cargs, **cparams):
        dbapi = self.dbapi  # the async_driver module above
        async_conn = await_only(dbapi.connect(**cparams))
        return dbapi.AuroraDataAPIAsyncAdaptConnection(dbapi, async_conn)

    def do_begin(self, dbapi_connection):
        dbapi_connection.start_transaction()

    def do_commit(self, dbapi_connection):
        dbapi_connection.commit()

    def do_rollback(self, dbapi_connection):
        dbapi_connection.rollback()


def register_dialects():
    from sqlalchemy.dialects import registry
    # sync variants (already present)
    registry.register(
        "mysql.auroradataapi", __name__, AuroraMySQLDataAPIDialect.__name__
    )
    registry.register(
        "postgresql.auroradataapi", __name__, AuroraPostgresDataAPIDialect.__name__
    )

    # async variants
    registry.register(
        "mysql.auroradataapiasync", __name__, AuroraMySQLDataAPIAsyncDialect.__name__
    )
    registry.register(
        "postgresql.auroradataapiasync",
        __name__,
        AuroraPostgresDataAPIAsyncDialect.__name__,
    )
    print("Registered aurora_data_api dialects:")