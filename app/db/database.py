"""
Database engine and session factory.

Uses DB_URL env var:
  - PostgreSQL (Docker):  postgresql://user:pass@postgres:5432/vadb
  - SQLite (local dev):   sqlite:///./va.db  (default)
"""

import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DB_URL = os.environ.get("DB_URL", "sqlite:///./va.db")

# SQLite needs check_same_thread=False for multi-threaded access
connect_args = {"check_same_thread": False} if DB_URL.startswith("sqlite") else {}

engine = create_engine(DB_URL, connect_args=connect_args, echo=False)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """Dependency for FastAPI — yields a DB session, auto-closes."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create all tables if they don't exist."""
    Base.metadata.create_all(bind=engine)
