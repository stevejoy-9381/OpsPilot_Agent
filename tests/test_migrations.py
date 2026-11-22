from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def test_migrations_on_clean_database(tmp_path: Path) -> None:
    db_file = tmp_path / "test_clean.db"
    db_url = f"sqlite:///{db_file}"

    # Verify database has no tables initially
    engine = create_engine(db_url)
    with engine.connect() as conn:
        inspector = inspect(conn)
        assert len(inspector.get_table_names()) == 0

    # Configure Alembic to point to this clean database
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)

    # Run upgrade to head
    command.upgrade(alembic_cfg, "head")

    # Verify alembic_version table exists and has 0001_baseline
    with engine.connect() as conn:
        inspector = inspect(conn)
        tables = inspector.get_table_names()
        assert "alembic_version" in tables

        result = conn.execute(text("SELECT version_num FROM alembic_version"))
        version = result.scalar()
        assert version == "0001_baseline"

    # Verify downgrade to base runs cleanly
    command.downgrade(alembic_cfg, "base")

    with engine.connect() as conn:
        result = conn.execute(text("SELECT version_num FROM alembic_version"))
        assert result.scalar() is None
