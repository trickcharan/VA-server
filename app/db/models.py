"""
SQLAlchemy models for orgs, users, and agents.
"""

from datetime import datetime, timezone
from sqlalchemy import (
    Column, Integer, String, Text, Boolean, DateTime,
    ForeignKey, Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import JSON
from sqlalchemy.orm import relationship

from app.db.database import Base, engine

import enum


class UserRole(str, enum.Enum):
    super_admin = "super_admin"
    org_admin = "org_admin"


def _json_column():
    """Use JSONB on PostgreSQL, plain JSON on SQLite."""
    if engine.dialect.name == "postgresql":
        return JSONB
    return JSON


class Org(Base):
    __tablename__ = "orgs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), unique=True, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    users = relationship("User", back_populates="org", cascade="all, delete-orphan")
    agents = relationship("Agent", back_populates="org", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Org id={self.id} name={self.name!r}>"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    org_id = Column(Integer, ForeignKey("orgs.id"), nullable=True)  # NULL for super-admin
    username = Column(String(255), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(SAEnum(UserRole), nullable=False, default=UserRole.org_admin)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    org = relationship("Org", back_populates="users")

    def __repr__(self):
        return f"<User id={self.id} username={self.username!r} role={self.role}>"


class Agent(Base):
    __tablename__ = "agents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    org_id = Column(Integer, ForeignKey("orgs.id"), nullable=False)
    name = Column(String(255), nullable=False)
    provider = Column(String(100), default="google_live", nullable=False)
    system_instruction = Column(Text, default="", nullable=False)
    context = Column(Text, default="", nullable=False)
    tools = Column(_json_column(), default=list, nullable=False)
    api_base_url = Column(String(512), nullable=True)
    api_docs = Column(Text, default="", nullable=False)
    welcome_audio_path = Column(String(512), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    org = relationship("Org", back_populates="agents")

    def __repr__(self):
        return f"<Agent id={self.id} name={self.name!r} org_id={self.org_id}>"
