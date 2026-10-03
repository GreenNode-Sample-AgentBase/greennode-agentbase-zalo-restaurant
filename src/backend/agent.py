"""Restaurant agent: LLM + business MCP tools (qua Gateway) + memory khách quen."""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from langchain.agents import create_agent
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI
from greennode_agent_bridge import AgentBaseMemoryEvents

import mcp_client
from memory_tools import remember, recall, MEMORY_ID

LLM_MODEL = os.environ.get("LLM_MODEL", "z-ai/glm-5.3-flash")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://maas-llm-aiplatform-hcm.api.vngcloud.vn/v1")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
MCP_RESTAURANT_URL = os.environ.get("MCP_RESTAURANT_URL", "")

if not LLM_API_KEY:
    raise ValueError("LLM_API_KEY là bắt buộc.")
if not MEMORY_ID:
    raise ValueError("AGENTBASE_MEMORY_ID là bắt buộc.")
if not MCP_RESTAURANT_URL:
    raise ValueError("MCP_RESTAURANT_URL là bắt buộc (connector 'restaurant' trên MCP Gateway).")

logger = logging.getLogger("zalo-restaurant-bot")

TZ_VN = ZoneInfo("Asia/Ho_Chi_Minh")
MAX_HISTORY_MESSAGES = 40  # trim context session dài (bảo vệ token budget)

PY_TYPE = {"string": str, "integer": int, "number": float, "boolean": bool, "array": list, "object": dict}
_tools_cache: list | None = None
_agent_cache = None
_tools_lock = threading.Lock()   # lock riêng cho tools cache
_agent_lock = threading.Lock()   # lock riêng cho agent cache
# LƯU Ý: KHÔNG dùng chung 1 Lock — get_agent giữ lock rồi gọi get_mcp_tools
# xin lại lock đó trên cùng thread → self-deadlock.

def _trim_history(msgs: list) -> list:
    """Giữ system prompt + MAX_HISTORY_MESSAGES message cuối, cắt tại biên HumanMessage
    để không làm vỡ cặp tool_call/tool của LangGraph."""
    from langchain_core.messages import HumanMessage, SystemMessage

    msgs = list(msgs or [])
    if len(msgs) <= MAX_HISTORY_MESSAGES:
        return msgs
    tail = msgs[-MAX_HISTORY_MESSAGES:]
    for i, m in enumerate(tail):  # không bắt đầu giữa cặp AI-tool
        if isinstance(m, HumanMessage):
            tail = tail[i:]
            break
    sys_msgs = [m for m in msgs if isinstance(m, SystemMessage)][:1]
    return sys_msgs + tail


class _TrimmingEvents(AgentBaseMemoryEvents):
    """Checkpointer + trim context: mỗi lần put, cap messages bằng _trim_history.
    Hoạt động với mọi version langchain."""

    def put(self, config, checkpoint, metadata, new_versions):
        try:
            vals = (checkpoint or {}).get("channel_values") or {}
            if vals.get("messages"):
                vals["messages"] = _trim_history(vals["messages"])
        except Exception:
            pass  # trim là tối ưu — không được làm hỏng checkpoint
        return super().put(config, checkpoint, metadata, new_versions)


def _schema_to_model(tool_def: dict):
    from typing import Optional

    from pydantic import Field, create_model

    schema = tool_def.get("inputSchema") or {}
    fields = {}
    required = set(schema.get("required") or [])
    for pname, ps in (schema.get("properties") or {}).items():
        if not isinstance(ps, dict):
            continue
        ann = PY_TYPE.get(ps.get("type", "string"), str)
        desc = str(ps.get("description", ""))[:500]
        if pname in required:
            fields[pname] = (ann, Field(description=desc))
        else:
            fields[pname] = (Optional[ann], Field(default=None, description=desc))
    if not fields:
        fields["noop"] = (Optional[str], Field(default=None, description="unused"))
    safe = "".join(c if c.isalnum() or c == "_" else "_" for c in tool_def["name"])
    return create_model(f"{safe}_args", **fields)


def _make_tool(tool_def: dict) -> StructuredTool:
    name = tool_def["name"]
    desc = (tool_def.get("description") or name)[:1000]

    def _run(**kwargs):
        args = {k: v for k, v in kwargs.items() if v is not None}
        return mcp_client.call_tool(MCP_RESTAURANT_URL, name, args)

    async def _arun(**kwargs):
        import asyncio
        return await asyncio.to_thread(_run, **kwargs)

    return StructuredTool.from_function(
        func=_run, coroutine=_arun, name=name, description=desc,
        args_schema=_schema_to_model(tool_def),
    )


def get_mcp_tools() -> list:
    global _tools_cache
    with _tools_lock:
        if _tools_cache is None:
            try:
                _tools_cache = [_make_tool(d) for d in mcp_client.list_tools(MCP_RESTAURANT_URL)]
            except Exception as e:
                logger.warning("không nạp được MCP tools: %s", e)
                _tools_cache = []
        return _tools_cache



def get_agent():
    """LangChain agent + checkpointer (short-term memory + trim) + tools."""
    global _agent_cache
    with _agent_lock:
        if _agent_cache is not None:
            return _agent_cache
        llm = ChatOpenAI(model=LLM_MODEL, base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
        checkpointer = _TrimmingEvents(memory_id=MEMORY_ID)
        today = datetime.now(TZ_VN).strftime("%Y-%m-%d (%A)")
        _agent_cache = create_agent(
            llm,
            tools=[*get_mcp_tools(), remember, recall],
                system_prompt=(
                    f"Hôm nay là {today}.\n"
                    "Bạn là nhân viên CSKH thân thiện của 'Quán Ngon 123' — nhà hàng Việt, nói tiếng Việt.\n"
                    "Trước khi tư vấn/đặt bàn, LUÔN dùng tool 'recall' để xem hồ sơ khách quen "
                    "(tên gọi, món ưa thích, dị ứng, bàn yêu thích...). Nếu biết tên khách thì xưng hô đúng tên.\n"
                    "Khi khách nói sở thích/dị ứng/dịp đặc biệt → dùng tool 'remember' để ghi vào hồ sơ.\n"
                    "Quy trình đặt bàn: get_menu/check_availability → xác nhận với khách → create_booking "
                    "(ghi notes chi tiết: dị ứng, món, dịp). Dùng add_loyalty_points cho dịp đặc biệt.\n"
                    "Trả lời ngắn gọn, ấm áp, như nhân viên quán thật."
                ),
            checkpointer=checkpointer,
        )
        return _agent_cache


def _jwt_claims(token: str) -> dict:
    import base64
    import json

    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


def whoami() -> dict:
    """Decode IAM token của runtime — dùng debug policy/principal."""
    token = mcp_client.get_token()
    claims = _jwt_claims(token)
    return {
        "client_id": os.environ.get("GREENNODE_CLIENT_ID", ""),
        "token_sub": claims.get("sub", ""),
        "azp": claims.get("azp", ""),
        "authAccountId": claims.get("authAccountId", ""),
    }
