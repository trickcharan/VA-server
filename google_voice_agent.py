"""
Virtual Agent - Google Gemini Live API Integration
Webex Contact Center BYOVA gRPC server powered by Google Gemini Live API.

Usage:
  python google_voice_agent.py   # Start the gRPC server on port 8086
"""

import logging

from dotenv import load_dotenv
load_dotenv()

from downstream.server.AIAgentServer import serve

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("virtual-agent")


if __name__ == "__main__":
    logger.info("Starting Virtual Agent gRPC server (Google Gemini Live)...")
    serve()
