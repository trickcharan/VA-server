"""
Per-agent configuration loader.

Each virtual agent has a directory under ``config/agents/agent_{id}/``
containing its system instruction, context, tool definitions, and
optionally a welcome audio file.  If a file is missing for a given
agent, the ``default/`` directory is used as a fallback.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger("agent-config")

# Root of the agents config tree
_AGENTS_DIR = os.path.join(os.path.dirname(__file__), "agents")

# Platform-level base system instruction (hidden from customers)
_BASE_INSTRUCTION_PATH = os.path.join(
    os.path.dirname(__file__), "..", "adapters", "google_live", "base_system_instruction.txt"
)


def _load_base_instruction() -> str:
    """Load the platform-level base system instruction."""
    try:
        with open(_BASE_INSTRUCTION_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        logger.warning("Base system instruction not found at %s", _BASE_INSTRUCTION_PATH)
        return ""


@dataclass
class AgentConfig:
    """Loaded configuration for a single virtual agent."""

    agent_id: str
    provider: str                       # STS provider name, e.g. "google_live"
    system_instruction: str             # merged system instruction + context + api_docs
    tools: list[dict[str, Any]] = field(default_factory=list)
    api_base_url: str | None = None     # base URL for execute_api tool
    welcome_audio_path: str | None = None

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @staticmethod
    def load(agent_id: str) -> "AgentConfig":
        """Load config for *agent_id*.

        Resolution order:
          1. Database (if available)
          2. File-based fallback (agents/agent_{id}/ → agents/default/)
        """
        # Try DB first
        config = _load_from_db(agent_id)
        if config:
            return config

        # Fall back to file-based config
        return _load_from_files(agent_id)


# ------------------------------------------------------------------
# DB loader
# ------------------------------------------------------------------

def _load_from_db(agent_id: str) -> AgentConfig | None:
    """Try to load agent config from the database. Returns None if DB is
    unavailable or the agent doesn't exist."""
    try:
        from app.db.database import SessionLocal
        from app.db.models import Agent
    except Exception:
        return None

    try:
        db = SessionLocal()
        try:
            agent = db.query(Agent).filter(Agent.id == int(agent_id), Agent.is_active == True).first()
            if not agent:
                return None

            base = _load_base_instruction()
            customer_instruction = agent.system_instruction or ""
            context = agent.context or ""
            api_docs = agent.api_docs or ""
            api_base_url = agent.api_base_url or None

            # Build: base prompt + customer persona + context + api docs
            merged = base + customer_instruction
            if context:
                merged = f"{merged}\n\nContext:\n{context}"
            if api_docs:
                merged = (
                    f"{merged}\n\n"
                    f"REST API Documentation:\n"
                    f"Base URL: {api_base_url or 'NOT CONFIGURED'}\n"
                    f"Use the `execute_api` tool to call these endpoints.\n\n"
                    f"{api_docs}"
                )
            merged = _substitute_variables(merged)

            config = AgentConfig(
                agent_id=str(agent.id),
                provider=agent.provider or "google_live",
                system_instruction=merged,
                tools=agent.tools or [],
                api_base_url=api_base_url,
                welcome_audio_path=agent.welcome_audio_path,
            )
            logger.info(
                "Loaded config from DB for agent %s: provider=%s, instruction=%d chars, tools=%d, api_base_url=%s",
                agent_id, config.provider, len(config.system_instruction), len(config.tools), config.api_base_url,
            )
            return config
        finally:
            db.close()
    except Exception as e:
        logger.debug("DB load failed for agent %s, will try files: %s", agent_id, e)
        return None


# ------------------------------------------------------------------
# File-based loader (fallback)
# ------------------------------------------------------------------

def _load_from_files(agent_id: str) -> AgentConfig:
    """Load config from the file system (original behavior)."""
    agent_dir = os.path.join(_AGENTS_DIR, f"agent_{agent_id}")
    default_dir = os.path.join(_AGENTS_DIR, "default")

    base = _load_base_instruction()
    customer_instruction = _read_text(agent_dir, default_dir, "system_instruction.txt")
    context = _read_text(agent_dir, default_dir, "context.txt")
    provider = _read_text(agent_dir, default_dir, "provider.txt").strip() or "google_live"
    tools = _read_json(agent_dir, default_dir, "tools.json")

    # Build: base prompt + customer persona + context
    merged = base + customer_instruction
    if context:
        merged = f"{merged}\n\nContext:\n{context}"
    merged = _substitute_variables(merged)

    welcome_audio_path = _resolve_file(agent_dir, default_dir, "welcome_audio.wav")

    config = AgentConfig(
        agent_id=str(agent_id),
        provider=provider,
        system_instruction=merged,
        tools=tools,
        welcome_audio_path=welcome_audio_path,
    )
    logger.info(
        "Loaded config from files for agent %s: provider=%s, instruction=%d chars, "
        "tools=%d, welcome_audio=%s",
        agent_id, config.provider, len(config.system_instruction),
        len(config.tools), config.welcome_audio_path is not None,
    )
    return config


# ------------------------------------------------------------------
# File helpers
# ------------------------------------------------------------------

def _resolve_file(agent_dir: str, default_dir: str, filename: str) -> str | None:
    """Return the first existing path for *filename*, or None."""
    for d in (agent_dir, default_dir):
        path = os.path.join(d, filename)
        if os.path.isfile(path):
            return path
    return None


def _read_text(agent_dir: str, default_dir: str, filename: str) -> str:
    """Read a text file with fallback; return empty string if missing."""
    path = _resolve_file(agent_dir, default_dir, filename)
    if not path:
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _read_json(agent_dir: str, default_dir: str, filename: str) -> list:
    """Read a JSON file and return the ``tools`` list (Phase 2)."""
    path = _resolve_file(agent_dir, default_dir, filename)
    if not path:
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Support both {"tools": [...]} and plain [...]
        if isinstance(data, list):
            return data
        return data.get("tools", [])
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load %s: %s", path, e)
        return []


def _substitute_variables(text: str) -> str:
    """Replace template variables in the system instruction.

    Supported variables:
      $date_time  — current date and time (e.g. "2026-04-28 16:30:00")
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return text.replace("$date_time", now)
