"""
SQLAlchemy compliance suite conftest for the *sync* dialect.

Drives ``postgresql+auroradataapi://`` (no async suffix). No AsyncEngine,
no async fixture routing patches. What's left is genuine dialect/driver
behavior: SQL generation, type coercion, DBAPI surface, Data API limits.
"""
import os
from pathlib import Path

import pytest


def _load_dotenv() -> None:
    env_path = Path(__file__).parent.parent / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()


def _patch_pg_post_configure_to_create_schemas() -> None:
    """The compliance suite hard-codes ``test_schema`` / ``test_schema_2``
    (``config.py:329``) and assumes they're pre-provisioned. The async
    sister conftest does this AND wraps the hook in a sync_engine
    unwrapper; the sync side just needs the CREATE SCHEMA part.
    """
    import sqlalchemy.dialects.postgresql.provision  # noqa: F401
    from sqlalchemy import text
    from sqlalchemy.testing.provision import post_configure_engine

    original = post_configure_engine.fns.get("postgresql")
    if original is None:
        return

    def patched(url, engine, follower_ident):
        original(url, engine, follower_ident)
        with engine.connect() as conn:
            for schema in ("test_schema", "test_schema_2"):
                conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
            conn.commit()

    post_configure_engine.fns["postgresql"] = patched


_patch_pg_post_configure_to_create_schemas()


from sqlalchemy.dialects import registry  # noqa: E402

registry.register(
    "postgresql.auroradataapi",
    "sqlalchemy_aurora_data_api",
    "AuroraPostgresDataAPIDialect",
)


pytest.register_assert_rewrite("sqlalchemy.testing.assertions")
from sqlalchemy.testing.plugin.pytestplugin import *  # noqa: E402, F401, F403


# SA's pytestplugin reads ``setup.cfg`` and populates
# ``plugin_base.file_config`` during its ``pytest_configure``. ``_engine_uri``
# (the hook that builds the test engine) then reads
# ``file_config["db"]["default"]`` at ``post_begin`` time. Our setup.cfg
# default URL is the async one (shared with the async compliance suite);
# remap default -> the ``sync`` entry so this directory actually drives
# the sync dialect / driver (``postgresql+auroradataapi://``).
_plugin_pytest_configure = pytest_configure  # noqa: F405


def pytest_configure(config):
    _plugin_pytest_configure(config)
    from sqlalchemy.testing.plugin import plugin_base
    sync_url = plugin_base.file_config.get("db", "sync")
    plugin_base.file_config.set("db", "default", sync_url)
