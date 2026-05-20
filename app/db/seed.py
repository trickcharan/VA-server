"""
Seed script: creates super-admin and migrates existing file-based agent configs into DB.

Run: python -m app.db.seed
"""

import json
import logging
import os
import sys

# Ensure project root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.db.database import SessionLocal, init_db
from app.db.models import Org, User, Agent, UserRole
from app.db.auth import hash_password

logger = logging.getLogger("seed")
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

_AGENTS_DIR = os.path.join(os.path.dirname(__file__), "..", "config", "agents")

# Mapping of existing file-based agents to seed data
SEED_ORGS = [
    {
        "name": "Acme Travel",
        "agents_dir": "agent_1",
        "admin_username": "acme_admin",
        "admin_password": "acme123",
    },
    {
        "name": "Rameswaram Cafe",
        "agents_dir": "agent_2",
        "admin_username": "rameswaram_admin",
        "admin_password": "rameswaram123",
    },
]


def _read_file(path: str) -> str:
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return ""


def _read_tools(path: str) -> list:
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data.get("tools", [])
        if isinstance(data, list):
            return data
        return []
    except (json.JSONDecodeError, OSError):
        return []


def seed():
    init_db()
    db = SessionLocal()

    try:
        # 1. Create super-admin if not exists
        existing_super = db.query(User).filter(User.role == UserRole.super_admin).first()
        if not existing_super:
            super_admin = User(
                username="admin",
                password_hash=hash_password("admin123"),
                role=UserRole.super_admin,
                org_id=None,
            )
            db.add(super_admin)
            db.flush()
            logger.info("Created super-admin: username=admin, password=admin123")
        else:
            logger.info("Super-admin already exists: %s", existing_super.username)

        # 2. Seed orgs and agents from existing file configs
        for org_data in SEED_ORGS:
            existing_org = db.query(Org).filter(Org.name == org_data["name"]).first()
            if existing_org:
                logger.info("Org '%s' already exists, skipping", org_data["name"])
                continue

            org = Org(name=org_data["name"])
            db.add(org)
            db.flush()

            # Create org admin
            org_admin = User(
                org_id=org.id,
                username=org_data["admin_username"],
                password_hash=hash_password(org_data["admin_password"]),
                role=UserRole.org_admin,
            )
            db.add(org_admin)

            # Load agent config from files
            agent_dir = os.path.join(_AGENTS_DIR, org_data["agents_dir"])
            system_instruction = _read_file(os.path.join(agent_dir, "system_instruction.txt"))
            context = _read_file(os.path.join(agent_dir, "context.txt"))
            tools = _read_tools(os.path.join(agent_dir, "tools.json"))

            agent = Agent(
                org_id=org.id,
                name=org_data["name"] + " Agent",
                provider="google_live",
                system_instruction=system_instruction,
                context=context,
                tools=tools,
            )
            db.add(agent)
            logger.info("Seeded org '%s' with admin '%s' and 1 agent",
                        org_data["name"], org_data["admin_username"])

        db.commit()
        logger.info("Seed complete.")
    except Exception as e:
        db.rollback()
        logger.error("Seed failed: %s", e, exc_info=True)
        raise
    finally:
        db.close()


if __name__ == "__main__":
    seed()
