"""Đặt env dummy TRƯỚC khi import module backend (agent.py raise nếu thiếu key).

MCP_DB_PATH trỏ sang tmp → test mcp-server không đụng DB thật.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_MODEL", "test-model")
os.environ.setdefault("AGENTBASE_MEMORY_ID", "memory-test")
os.environ.setdefault("MEMORY_STRATEGY_ID", "ltms-cust-test")
os.environ.setdefault("MCP_RESTAURANT_URL", "https://gw.example/restaurant")

BACKEND = Path(__file__).resolve().parent.parent / "src" / "backend"
MCP_SERVER = Path(__file__).resolve().parent.parent / "src" / "mcp-server"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(MCP_SERVER))
