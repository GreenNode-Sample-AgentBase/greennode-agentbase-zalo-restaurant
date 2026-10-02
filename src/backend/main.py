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

import asyncio
import json
import logging
import os
import threading
import uuid
from datetime import datetime
from contextlib import nullcontext
from pathlib import Path
from zoneinfo import ZoneInfo

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logger = logging.getLogger("zalo-restaurant-bot")

app = GreenNodeAgentBaseApp()

MEMORY_ID = os.environ.get("AGENTBASE_MEMORY_ID", "")
MCP_RESTAURANT_URL = os.environ.get("MCP_RESTAURANT_URL", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
# AGENT_API_KEY (optional): bảo vệ REST API trong production (webhook dùng secret riêng)
AGENT_API_KEY = os.environ.get("AGENT_API_KEY", "").strip()
# DEBUG_OPS=1: bật op whoami (lộ identity runtime — chỉ dùng lúc setup policy)
DEBUG_OPS = os.environ.get("DEBUG_OPS", "0").strip() in ("1", "true", "yes")

# A2A (Agent-to-Agent protocol): URL public của runtime này để ghi vào agent card
A2A_PUBLIC_URL = os.environ.get("A2A_PUBLIC_URL", "").rstrip("/")

# LangFuse tracing (optional): LANGFUSE_PUBLIC_KEY / SECRET_KEY / HOST


def _lf_tracing() -> bool:
    """LangFuse v4 tracing bật khi đủ 3 env (SDK v4 client tự đọc, auth qua env)."""
    return bool(
        os.environ.get("LANGFUSE_PUBLIC_KEY")
        and os.environ.get("LANGFUSE_SECRET_KEY")
        and os.environ.get("LANGFUSE_HOST")
    )


def _lf_scope(trace_name: str, user_id: str = "", session_id: str = "", tags: list | None = None):
    """LangFuse v4: scope `propagate_attributes` — trace_name/user/session/tags áp cho
    root observation VÀ mọi child (kể cả generation chịu chi phí).

    Phải vào scope TRƯỚC khi tạo CallbackHandler và chạy agent (cùng thread/context).
    Tracing tắt → nullcontext (chạy bình thường)."""
    if not _lf_tracing():
        return nullcontext()
    try:
        from langfuse import propagate_attributes

        kwargs: dict = {"trace_name": trace_name, "tags": tags or []}
        if user_id:
            kwargs["user_id"] = user_id
        if session_id:
            kwargs["session_id"] = session_id
        return propagate_attributes(**kwargs)
    except Exception as e:
        logger.warning("LangFuse scope tắt: %s", e)
        return nullcontext()


def _lf_callback():
    """LangFuse v4 CallbackHandler (OTel, auth qua env) — tạo BÊN TRONG scope
    để kế thừa trace context; None = tracing tắt."""
    if not _lf_tracing():
        return None
    try:
        from langfuse.langchain import CallbackHandler

        return CallbackHandler()
    except Exception as e:
        logger.warning("LangFuse callback tắt: %s", e)
        return None

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
TZ_VN = ZoneInfo("Asia/Ho_Chi_Minh")


def _now() -> str:
    return datetime.now(TZ_VN).isoformat()


# ── API-key middleware: bảo vệ /invocations + /api/* (trừ /api/info) ──
class ApiKeyMiddleware:
    """Pure-ASGI middleware. Không đặt AGENT_API_KEY → mở (local dev).
    /webhook/zalo KHÔNG bị chặn (dùng X-Bot-Api-Secret-Token riêng của Zalo)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and AGENT_API_KEY:
            path = scope.get("path", "")
            protected = path == "/invocations" or (
                path.startswith("/api/") and path != "/api/info"
            )
            if protected:
                headers = {
                    k.decode("latin-1").lower(): v.decode("latin-1")
                    for k, v in scope.get("headers", [])
                }
                if headers.get("x-api-key") != AGENT_API_KEY:
                    resp = JSONResponse(
                        {"status": "error", "error": "Unauthorized — thiếu/sai header X-API-Key"},
                        status_code=401,
                    )
                    await resp(scope, receive, send)
                    return
        await self.app(scope, receive, send)


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


async def _chat_turn(actor_id: str, session_id: str, message: str, trace_name: str = "zalo-chat") -> dict:
    """1 turn hội thoại qua agent (dùng cho /invocations, webhook và A2A)."""
    try:
        # LangFuse v4: scope propagate_attributes bọc cả ainvoke (cùng context)
        with _lf_scope(
            trace_name,
            actor_id,
            session_id,
            ["chat", "a2a"] if trace_name.startswith("a2a") else ["chat"],
        ):
            cb = _lf_callback()
            result = await agent_mod.get_agent().ainvoke(
                {"messages": [{"role": "user", "content": message}]},
                config={
                    "callbacks": [cb] if cb else [],
                    "configurable": {"thread_id": session_id, "actor_id": actor_id},
                },
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


# ── A2A (Agent-to-Agent protocol): agent card + JSON-RPC /a2a ──


def _a2a_card() -> dict:
    return {
        "name": "zalo-restaurant-bot",
        "description": (
            "Agent tư vấn nhà hàng/đặt bàn qua Zalo: menu, đặt chỗ, tích điểm thân thiết, "
            "chăm sóc khách hàng — có memory từng khách."
        ),
        "url": f"{A2A_PUBLIC_URL}/a2a" if A2A_PUBLIC_URL else "/a2a",
        "version": "1.0.0",
        "protocolVersion": "0.3.0",
        "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "restaurant-consultation",
                "name": "Tư vấn nhà hàng & đặt bàn",
                "description": "Tư vấn menu, đặt bàn, câu hỏi về mở cửa/giá/địa điểm, chương trình thành viên.",
                "tags": ["restaurant", "booking", "zalo"],
                "examples": ["Đặt bàn 4 người tối thứ 7, cần món chay", "Mình tích được bao nhiêu điểm rồi?"],
            },
        ],
        "preferredTransport": "JSONRPC",
    }


async def _agent_card_route(request: Request) -> JSONResponse:
    return JSONResponse(_a2a_card())


def _a2a_text(params: dict) -> str:
    msg = (params or {}).get("message") or {}
    return "".join(
        str(p.get("text", ""))
        for p in msg.get("parts", [])
        if p.get("kind") == "text" or "text" in p
    ).strip()


async def _a2a_route(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        )
    method = body.get("method", "")
    rid = body.get("id")
    if method != "message/send":
        return JSONResponse(
            {"jsonrpc": "2.0", "id": rid,
             "error": {"code": -32601, "message": f"Method not found: {method}"}}
        )
    text = _a2a_text(body.get("params"))
    if not text:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": rid,
             "error": {"code": -32602, "message": "params.message.parts không có text"}}
        )
    msg = (body.get("params") or {}).get("message") or {}
    ctx = msg.get("contextId") or f"a2a-{uuid.uuid4().hex[:12]}"
    result = await asyncio.to_thread(
        run_coro, _chat_turn("a2a", ctx, text, trace_name="a2a-zalo-turn")
    )
    if result.get("status") != "success":
        return JSONResponse(
            {"jsonrpc": "2.0", "id": rid,
             "error": {"code": -32603, "message": result.get("error", "agent error")}}
        )
    return JSONResponse({
        "jsonrpc": "2.0",
        "id": rid,
        "result": {
            "kind": "message",
            "messageId": f"msg-{uuid.uuid4()}",
            "contextId": ctx,
            "role": "agent",
            "parts": [{"kind": "text", "text": str(result.get("response") or "")}],
        },
    })


@app.entrypoint
def handler(payload: dict, context: RequestContext) -> dict:
    if payload.get("op") == "whoami":
        if not DEBUG_OPS:
            return {
                "status": "error",
                "error": "whoami bị tắt. Set DEBUG_OPS=1 (chỉ dùng lúc setup policy) rồi restart runtime.",
            }
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

    logger.info("webhook POST received, body keys: %s", list(payload.keys()))
    try:
        ev = zalo.parse_webhook(payload)
    except Exception as e:
        logger.warning("webhook parse error: %s", e)
        return JSONResponse({"status": "ignored", "reason": f"parse error: {e}"})
    if not ev:
        logger.info("webhook ignored (không phải message.text) — body: %s", str(payload)[:200])
        return JSONResponse({"message": "Success"})  # image/sticker/voice → bỏ qua
    if ev.get("already_seen"):
        return JSONResponse({"message": "Success"})  # Zalo retry trùng → bỏ qua

    # Đánh dấu seen NGAY (trước khi xử lý) — chặn retry trùng trong lúc LLM chạy
    zalo.mark_seen(ev.get("message_id", ""))

    actor_id = ev["sender_id"]
    chat_id = ev["chat_id"]
    text = ev["text"]
    session_id = f"zalo-{chat_id}"  # 1 thread liên tục per khách

    def _process() -> None:
        try:
            result = run_coro(_chat_turn(actor_id, session_id, text))
            reply = result.get("response") or f"Xin lỗi, có lỗi xảy ra: {result.get('error', 'không rõ')}"
            send_result = zalo.send_message(chat_id, reply)
            logger.info(
                "replied to %s | sent=%s | memories=%s | result=%s",
                chat_id, bool(send_result.get("ok")), result.get("memories_used"), str(send_result)[:150],
            )
        except Exception:
            logger.exception("webhook processing failed (chat_id=%s)", chat_id)
            try:
                zalo.send_message(chat_id, "Xin lỗi quý khách, hệ thống đang bận — vui lòng nhắn lại sau ít phút ạ 🙏")
            except Exception:
                pass

    # ACK 200 cho Zalo NGAY LẬP TỨC — turn LLM chạy background (3–10s),
    # tránh Zalo timeout/retry khi phải chờ LLM trả lời xong.
    threading.Thread(
        target=_process, daemon=True, name=f"zalo-msg-{ev.get('message_id', '')[:12]}"
    ).start()
    return JSONResponse({"message": "Success", "accepted": True})


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
            "auth_required": bool(AGENT_API_KEY),
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


# ── /ready: health check sâu (memory + gateway + zalo) cho ops ──
async def _ready(request: Request) -> JSONResponse:
    checks: dict = {}
    try:
        memory_tools.list_actors_sync()
        checks["memory"] = {"ok": True}
    except Exception as e:
        checks["memory"] = {"ok": False, "error": str(e)[:150]}
    try:
        tools = agent_mod.get_mcp_tools()
        checks["gateway"] = {"ok": bool(tools), "tools": len(tools)}
    except Exception as e:
        checks["gateway"] = {"ok": False, "error": str(e)[:150]}
    checks["llm"] = {"ok": bool(LLM_API_KEY), "model": LLM_MODEL}
    checks["zalo"] = {"ok": zalo.zalo_configured(), "bot": zalo.bot_name()}
    ok = checks["memory"].get("ok") and checks["gateway"].get("ok") and checks["llm"].get("ok")
    return JSONResponse({"status": "ok" if ok else "degraded", "checks": checks}, status_code=200 if ok else 503)


app.add_route("/ready", _ready, methods=["GET"])
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
app.add_route("/.well-known/agent-card.json", _agent_card_route, methods=["GET"])
app.add_route("/a2a", _a2a_route, methods=["POST"])
app.add_middleware(ApiKeyMiddleware)
if SERVE_UI:
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="ui")


if __name__ == "__main__":
    app.run(port=8080, host="0.0.0.0")