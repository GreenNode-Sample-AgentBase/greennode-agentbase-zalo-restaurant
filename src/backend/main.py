"""Zalo Restaurant Bot — GreenNode AgentBase sample (backend).

Endpoints:
  POST /invocations      — chat (simulator/test) — cần headers user/session
  POST /webhook/zalo     — webhook thật từ Zalo Bot Platform
  GET  /webhook/zalo     — kiểm tra cấu hình + echo challenge nếu có
  GET  /health           — SDK health
  GET  /                 — serve frontend simulator
  GET  /api/info         — cấu hình (zalo_configured, memory, mcp...)
  GET  /api/memory       — hồ sơ khách quen (memory records per actor)
  GET  /api/history      — events hội thoại per actor+session
  GET  /api/actors       — khách đã có hồ sơ
  GET  /api/bookings     — danh sách đặt bàn (gọi MCP tool list_bookings)
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.staticfiles import StaticFiles

from greennode_agentbase import (
    GreenNodeAgentBaseApp,
    RequestContext,
    PingStatus,
)

import agent as agent_mod
import memory_tools
from memory_tools import run_coro
import zalo
from mcp_client import mcp_request

app = GreenNodeAgentBaseApp()

MEMORY_ID = os.environ.get("AGENTBASE_MEMORY_ID", "")
MCP_RESTAURANT_URL = os.environ.get("MCP_RESTAURANT_URL", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


def _now() -> str:
    return datetime.now().isoformat()


def _get_user_id(context) -> str:
    """user_id từ header (SDK 1.0.1 chưa expose context.user_id → đọc từ request)."""
    uid = getattr(context, "user_id", None)
    if uid:
        return uid
    req = getattr(context, "request", None)
    if req is not None:
        try:
            return req.headers.get("X-GreenNode-AgentBase-User-Id", "") or ""
        except Exception:
            return ""
    return ""


async def _chat_turn(actor_id: str, session_id: str, message: str) -> dict:
    """1 turn hội thoại qua agent (dùng cho cả /invocations và webhook)."""
    try:
        result = await agent_mod.get_agent().ainvoke(
            {"messages": [{"role": "user", "content": message}]},
            config={"configurable": {"thread_id": session_id, "actor_id": actor_id}},
        )
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}", "timestamp": _now()}

    ai_message = result["messages"][-1]
    memories_used: list[str] = []
    for m in result["messages"]:
        if type(m).__name__ != "ToolMessage":
            continue
        content = str(getattr(m, "content", ""))
        if content.startswith("Đã nhớ: "):
            memories_used.append(content[len("Đã nhớ: "):])
        elif "score:" in content and content.lstrip().startswith("- "):
            for line in content.splitlines():
                line = line.strip()
                if line.startswith("- ") and " (score:" in line:
                    memories_used.append(line[2:].split(" (score:")[0])
    reply = str(ai_message.content or "")
    if reply:
        await memory_tools.add_chat_events(actor_id, session_id, message, reply)
    return {
        "status": "success",
        "agent": "zalo-restaurant-bot",
        "response": ai_message.content,
        "memories_used": memories_used,
        "timestamp": _now(),
    }


@app.entrypoint
def handler(payload: dict, context: RequestContext) -> dict:
    if payload.get("op") == "whoami":
        return {"status": "success", "agent": "zalo-restaurant-bot", **agent_mod.whoami()}
    user_id = _get_user_id(context)
    if not user_id or not context.session_id:
        return {
            "status": "error",
            "error": (
                "Thiếu headers bắt buộc: X-GreenNode-AgentBase-User-Id và "
                "X-GreenNode-AgentBase-Session-Id."
            ),
        }
    message = payload.get("message") or payload.get("input") or "Hello"
    return run_coro(_chat_turn(user_id, context.session_id, message))


@app.ping
def health_check() -> PingStatus:
    return PingStatus.HEALTHY


# ---------- Zalo webhook ----------
async def _webhook_get(request: Request) -> JSONResponse:
    # Echo challenge nếu Zalo yêu cầu verify webhook
    challenge = request.query_params.get("challenge") or request.query_params.get("webhook_challenge")
    if challenge:
        return JSONResponse({"challenge": challenge})
    return JSONResponse({"status": "ok", "zalo_configured": zalo.zalo_configured()})


async def _webhook_post(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"status": "ignored", "reason": "invalid json"})

    # Verify secret từ Zalo Bot Platform (header X-Bot-Api-Secret-Token)
    if not zalo.webhook_secret_ok(request.headers.get("X-Bot-Api-Secret-Token", "")):
        return JSONResponse({"status": "denied"}, status_code=403)

    print(f"[zalo-webhook] POST received, body keys: {list(payload.keys())}", flush=True)
    try:
        ev = zalo.parse_webhook(payload)
    except Exception as e:
        print(f"[zalo-webhook] parse error: {e}", flush=True)
        return JSONResponse({"status": "ignored", "reason": f"parse error: {e}"})
    if not ev:
        print(f"[zalo-webhook] ignored (không phải message.text) — body: {str(payload)[:200]}", flush=True)
        return JSONResponse({"message": "Success"})  # image/sticker/voice → bỏ qua
    if ev.get("already_seen"):
        return JSONResponse({"message": "Success"})  # Zalo retry trùng → bỏ qua

    actor_id = ev["sender_id"]
    session_id = f"zalo-{ev['chat_id']}"  # 1 thread liên tục per khách
    result = run_coro(_chat_turn(actor_id, session_id, ev["text"]))
    reply = result.get("response") or f"Xin lỗi, có lỗi xảy ra: {result.get('error', 'không rõ')}"
    send_result = zalo.send_message(ev["chat_id"], reply)
    zalo.mark_seen(ev.get("message_id", ""))
    print(f"[zalo-webhook] replied to {ev['chat_id']} | sent={bool(send_result.get('ok'))} | result={str(send_result)[:200]}", flush=True)
    return JSONResponse({"message": "Success", "sent": bool(send_result.get("ok"))})


app.add_route("/webhook/zalo", _webhook_get, methods=["GET"])
app.add_route("/webhook/zalo", _webhook_post, methods=["POST"])


# ---------- REST helpers cho simulator ----------
async def _api_info(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "agent": "zalo-restaurant-bot",
            "memory_id": MEMORY_ID,
            "mcp_url": MCP_RESTAURANT_URL,
            "llm_model": LLM_MODEL,
            "zalo_configured": zalo.zalo_configured(),
            "zalo_bot": zalo.bot_name(),
        }
    )


async def _api_memory(request: Request) -> JSONResponse:
    actor = request.query_params.get("actor", "")
    if not actor:
        return JSONResponse({"error": "thiếu ?actor=<userId>"}, status_code=400)
    groups = []
    if memory_tools.MEMORY_STRATEGY_ID:
        try:
            records = memory_tools.browse_group_sync(actor)
            groups.append({"strategy_id": memory_tools.MEMORY_STRATEGY_ID, "strategy": "customer-profile", "records": records})
        except Exception as e:
            groups.append({"strategy_id": memory_tools.MEMORY_STRATEGY_ID, "strategy": "customer-profile", "error": str(e)[:200], "records": []})
    return JSONResponse({"actor": actor, "groups": groups})


async def _api_history(request: Request) -> JSONResponse:
    actor = request.query_params.get("actor", "")
    session = request.query_params.get("session", "")
    if not actor or not session:
        return JSONResponse({"error": "thiếu ?actor= và &session="}, status_code=400)
    try:
        raw = memory_tools.list_events_sync(actor, session)
        def _f(r, k, d=""):
            if isinstance(r, dict):
                v = r.get(k, d)
            else:
                v = getattr(r, k, d)
            return v if v is not None else d

        # Chỉ lấy conversational events (checkpoint binary của langgraph bị lọc bỏ)
        events = []
        for ev in raw:
            payload = _f(ev, "payload", None)
            if payload is None or _f(payload, "type", "") != "conversational":
                continue
            events.append(
                {
                    "role": _f(payload, "role", "user") or "user",
                    "message": _f(payload, "message", ""),
                    "createdAt": str(_f(ev, "event_timestamp") or _f(ev, "eventTimestamp") or _f(ev, "created_at")),
                }
            )
        events.reverse()  # API trả mới nhất trước → reverse
        return JSONResponse({"actor": actor, "session": session, "events": events})
    except Exception as e:
        return JSONResponse({"actor": actor, "session": session, "events": [], "error": str(e)[:200]})


async def _api_actors(request: Request) -> JSONResponse:
    try:
        return JSONResponse({"actors": memory_tools.list_actors_sync()})
    except Exception as e:
        return JSONResponse({"actors": [], "error": str(e)[:200]})


async def _api_bookings(request: Request) -> JSONResponse:
    """Gọi thẳng MCP tool list_bookings (bỏ qua LLM) cho panel đặt bàn."""
    try:
        st, body = mcp_request(MCP_RESTAURANT_URL, "tools/call", {"name": "list_bookings", "arguments": {}})
        if st != 200 or not isinstance(body, dict):
            return JSONResponse({"bookings": [], "error": str(body)[:200]})
        texts = [
            item.get("text", "")
            for item in body.get("result", {}).get("content", [])
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        data = json.loads("\n".join(texts)) if texts else {}
        return JSONResponse({"bookings": data.get("bookings", [])})
    except Exception as e:
        return JSONResponse({"bookings": [], "error": str(e)[:200]})


app.add_route("/api/info", _api_info, methods=["GET"])
app.add_route("/api/memory", _api_memory, methods=["GET"])
app.add_route("/api/history", _api_history, methods=["GET"])
app.add_route("/api/actors", _api_actors, methods=["GET"])
app.add_route("/api/bookings", _api_bookings, methods=["GET"])

# SERVE_UI=false → không serve frontend (chế độ Zalo-first: user chỉ tương tác qua Zalo)
SERVE_UI = os.getenv("SERVE_UI", "true").strip().lower() not in ("false", "0", "no")

async def _root(request: Request) -> JSONResponse:
    if SERVE_UI:
        return JSONResponse({"service": "zalo-restaurant-bot", "ui": "served at /index.html"})
    return JSONResponse(
        {
            "service": "zalo-restaurant-bot",
            "ui": "disabled (SERVE_UI=false) — người dùng tương tác qua Zalo",
            "zalo_configured": zalo.zalo_configured(),
        }
    )

app.add_route("/", _root, methods=["GET"])
if SERVE_UI:
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="ui")


if __name__ == "__main__":
    app.run(port=8080, host="0.0.0.0")