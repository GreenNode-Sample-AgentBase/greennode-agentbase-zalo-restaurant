"""Memory helpers cho restaurant (khách quen) — 1 strategy CUSTOM: customer-profile."""

from __future__ import annotations

import os
import threading

from langchain_core.tools import tool
from langgraph.config import get_config
from greennode_agentbase.memory import MemoryClient
from greennode_agentbase.memory.models import (
    MemoryRecordInsertDirectlyRequest,
    MemoryRecordSearchRequest,
)

MEMORY_ID = os.environ.get("AGENTBASE_MEMORY_ID", "")
MEMORY_STRATEGY_ID = os.environ.get("MEMORY_STRATEGY_ID", "")

_client: MemoryClient | None = None


def memory_client() -> MemoryClient:
    global _client
    if _client is None:
        _client = MemoryClient()
    return _client


def _field(r, key: str, default=""):
    """SDK 1.0.3 trả record/event dạng dict (hoặc object) — đọc field an toàn."""
    if isinstance(r, dict):
        v = r.get(key, default)
    else:
        v = getattr(r, key, default)
    return v if v is not None else default





# ── Persistent event loop: mọi SDK/memory call chạy trên 1 loop duy nhất ──
# (SDK cache client theo event loop; nhiều loop → "Event loop is closed")
_loop = None
_loop_lock = threading.Lock()


def _ensure_loop():
    global _loop
    with _loop_lock:
        if _loop is None or _loop.is_closed():
            import asyncio
            import threading

            _loop = asyncio.new_event_loop()
            threading.Thread(
                target=_loop.run_forever, daemon=True, name="agent-memory-loop"
            ).start()
    return _loop


def run_coro(coro, timeout: float = 600):
    """Chạy coroutine trên persistent loop, block tới khi xong."""
    import asyncio

    loop = _ensure_loop()
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=timeout)

async def _with_retry(coro_factory, attempts: int = 2, delay: float = 0.6):
    """Retry 1 lần cho lỗi tạm thời (HTTP 5xx) từ Memory service."""
    import asyncio

    try:
        return await coro_factory()
    except Exception as first:
        await asyncio.sleep(delay)
        try:
            return await coro_factory()
        except Exception:
            raise first


def get_actor_id() -> str:
    config = get_config()
    return (config.get("configurable") or {}).get("actor_id", "")


def build_namespace(actor_id: str) -> str:
    return f"/strategies/{MEMORY_STRATEGY_ID}/actors/{actor_id}"


@tool
async def remember(fact: str) -> str:
    """Ghi một thông tin về khách hàng vào hồ sơ khách quen (tên, món ưa thích, dị ứng, bàn, dịp đặc biệt...).

    Args:
        fact: Thông tin cần ghi nhớ, 1 câu hoàn chỉnh.
    """
    actor = get_actor_id()

    async def _go():
        return await memory_client().insert_memory_records_directly_async(
            id=MEMORY_ID,
            namespace=build_namespace(actor),
            request=MemoryRecordInsertDirectlyRequest(memoryRecords=[fact]),
        )

    await _with_retry(_go)
    return f"Đã nhớ: {fact}"


@tool
async def recall(query: str) -> str:
    """Tra cứu hồ sơ khách quen (sở thích, dị ứng, lịch sử đặt bàn...).

    Args:
        query: Câu truy vấn, VD: 'khách thích ăn gì', 'khách có dị ứng không'.
    """
    actor = get_actor_id()

    async def _go():
        return await memory_client().search_memory_records_async(
            id=MEMORY_ID,
            namespace=build_namespace(actor),
            request=MemoryRecordSearchRequest(query=query, limit=20),
        )

    try:
        results = await _with_retry(_go)
    except Exception as e:  # degrade gracefully — recall fail không phá turn
        return f"(Bộ nhớ tạm thời không khả dụng — {type(e).__name__}; hãy trả lời bình thường.)"
    if not results:
        return "Chưa có thông tin nào về khách này trong hồ sơ."
    return "\n".join(f"- {_field(r, 'memory')} (score: {float(_field(r, 'score', 0) or 0):.2f})" for r in results)


async def browse_group(actor_id: str, limit: int = 100) -> list[dict]:
    records = await memory_client().list_memory_records_async(
        id=MEMORY_ID, namespace=build_namespace(actor_id)
    )
    return [
        {
            "id": _field(r, "id"),
            "memory": _field(r, "memory"),
            "createdAt": str(_field(r, "created_at", "") or _field(r, "createdAt", "")),
        }
        for r in list(records)[:limit]
    ]

def browse_group_sync(actor_id: str, limit: int = 100) -> list:
    return run_coro(browse_group(actor_id, limit))


def list_events_sync(actor_id: str, session_id: str, size: int = 50) -> list:
    async def _go():
        result = await memory_client().list_events_async(
            id=MEMORY_ID, actorId=actor_id, sessionId=session_id, page=1, size=size
        )
        return list(result.list_data)

    return run_coro(_go())


def list_actors_sync() -> list:
    async def _go():
        result = await memory_client().list_actors_async(id=MEMORY_ID, page=1, size=50)
        out = []
        for a in list(result.list_data):
            aid = _field(a, "actor_id") or _field(a, "actorId")
            sessions = []
            try:
                sess = await memory_client().list_sessions_async(id=MEMORY_ID, actorId=aid, page=1, size=20)
                sessions = [_field(s, "session_id") or _field(s, "sessionId") for s in list(sess.list_data)]
            except Exception:
                pass
            out.append({"actorId": aid, "sessions": sorted({x for x in sessions if x})})
        return out

    return run_coro(_go())


async def add_chat_events(actor_id: str, session_id: str, user_text: str, bot_text: str) -> None:
    """Ghi 2 conversational events (user + assistant) — dùng khi ĐANG trên loop."""
    from greennode_agentbase.memory.models import EventCreateRequest, EventPayload

    c = memory_client()
    for role, msg in (("user", user_text), ("assistant", bot_text)):
        await c.create_event_async(
            id=MEMORY_ID,
            actorId=actor_id,
            sessionId=session_id,
            request=EventCreateRequest(
                payload=EventPayload(type="conversational", role=role, message=msg)
            ),
        )

