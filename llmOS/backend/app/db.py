from __future__ import annotations

from pathlib import Path

from sqlalchemy import JSON, Column, DateTime, Integer, String, Text, create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.sql import func

from app.config import DATABASE_URL, MYSQL_DB, MYSQL_HOST, MYSQL_PASSWORD, MYSQL_PORT, MYSQL_USER

SQLITE_PATH = Path(__file__).resolve().parents[1] / "data" / "llmos.sqlite3"
SQLITE_URL = f"sqlite:///{SQLITE_PATH}"


class Base(DeclarativeBase):
    pass


class ExperimentRun(Base):
    __tablename__ = "experiment_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    exp_id = Column(String(16), nullable=False, index=True)
    title = Column(String(128), nullable=False)
    seed = Column(Integer, nullable=False, default=42)
    verdict = Column(Text, nullable=True)
    payload = Column(JSON, nullable=False)
    created_at = Column(DateTime, server_default=func.now())


engine = None
SessionLocal = None
DB_BACKEND = "mysql"


def _mysql_engine():
    admin_url = (
        f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}"
        f"@{MYSQL_HOST}:{MYSQL_PORT}/mysql?charset=utf8mb4"
    )
    admin = create_engine(admin_url, pool_pre_ping=True)
    with admin.connect() as conn:
        conn.execute(
            text(
                f"CREATE DATABASE IF NOT EXISTS `{MYSQL_DB}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        )
        conn.commit()
    admin.dispose()
    return create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=3600)


def init_db() -> None:
    global engine, SessionLocal, DB_BACKEND
    try:
        engine = _mysql_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        DB_BACKEND = "mysql"
    except Exception:
        SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(SQLITE_URL, connect_args={"check_same_thread": False})
        DB_BACKEND = "sqlite"
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
