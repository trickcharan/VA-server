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


@dataclass
class AgentConfig:
    """Loaded configuration for a single virtual agent."""

    agent_id: str
    provider: str                       # STS provider name, e.g. "google_live"
    system_instruction: str             # merged system instruction + context
    tools: list[dict[str, Any]] = field(default_factory=list)
    welcome_audio_path: str | None = None

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @staticmethod
    def load(agent_id: str) -> "AgentConfig":
        """Load config for *agent_id*, falling back to ``default/``.

        Resolution order for each file:
          1. ``agents/agent_{agent_id}/{file}``
          2. ``agents/default/{file}``
        """
        agent_dir = os.path.join(_AGENTS_DIR, f"agent_{agent_id}")
        default_dir = os.path.join(_AGENTS_DIR, "default")

        system_instruction = _read_text(agent_dir, default_dir, "system_instruction.txt")
        context = _read_text(agent_dir, default_dir, "context.txt")
        provider = _read_text(agent_dir, default_dir, "provider.txt").strip() or "google_live"
        tools = _read_json(agent_dir, default_dir, "tools.json")

        # Merge system instruction + context into one prompt
        merged = system_instruction
        if context:
            merged = f"{system_instruction}\n\nContext:\n{context}"

        # Substitute template variables
        merged = _substitute_variables(merged)

        # Resolve welcome audio — per-agent first, then default
        welcome_audio_path = _resolve_file(agent_dir, default_dir, "welcome_audio.wav")

        config = AgentConfig(
            agent_id=str(agent_id),
            provider=provider,
            system_instruction=merged,
            tools=tools,
            welcome_audio_path=welcome_audio_path,
        )

        logger.info(
            "Loaded config for agent %s: provider=%s, instruction=%d chars, "
            "tools=%d, welcome_audio=%s",
            agent_id, config.provider, len(config.system_instruction),
            len(config.tools), config.welcome_audio_path is not None,
        )
        return config


# ------------------------------------------------------------------
# Internal helpers
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
