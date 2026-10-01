"""Tích hợp Zalo Bot Platform (bot.zaloplatforms.com).

API chính thức: https://bot-api.zaloplatforms.com/bot${BOT_TOKEN}/<function>
- sendMessage: POST {chat_id, text, parse_mode}
- setWebhook:  POST {url, secret_token} → Zalo gọi kèm header X-Bot-Api-Secret-Token
- Webhook body: {"ok": true, "result": {"event_name": "message.text.received",
                 "message": {"from": {"id": "...", "display_name": "..."},
                             "chat": {"id": "...", "chat_type": "PRIVATE"}, "text": "..."}}}

Docs: https://bot.zaloplatforms.com/docs/build-your-bot-with-webhook/
"""
import os

import httpx

ZALO_BOT_TOKEN = os.getenv("ZALO_BOT_TOKEN", "")
ZALO_WEBHOOK_SECRET = os.getenv("ZALO_WEBHOOK_SECRET", "")
API_BASE = os.getenv("ZALO_API_BASE", "https://bot-api.zaloplatforms.com")

_seen_message_ids: set = set()  # chống Zalo retry gửi trùng


def zalo_configured() -> bool:
    return bool(ZALO_BOT_TOKEN)


def _api(fn: str) -> str:
    return f"{API_BASE}/bot{ZALO_BOT_TOKEN}/{fn}"


def get_me() -> dict:
    """getMe — kiểm tra token + lấy thông tin bot (cache đơn giản)."""
    global _me_cache
    try:
        return _me_cache
    except NameError:
        pass
    with httpx.Client(timeout=15) as c:
        r = c.get(_api("getMe"))
        data = r.json() if r.status_code == 200 else {}
    _me_cache = data if data.get("ok") else {}
    return _me_cache


def bot_name() -> str:
    r = get_me().get("result") or {}
    return r.get("display_name") or r.get("account_name") or ""


def webhook_secret_ok(header_value: str) -> bool:
    """Verify header X-Bot-Api-Secret-Token (chỉ bắt buộc khi đã cấu hình secret)."""
    if not ZALO_WEBHOOK_SECRET:
        return True  # chưa cấu hình secret → cho qua (dev mode)
    return header_value == ZALO_WEBHOOK_SECRET


def parse_webhook(payload: dict) -> dict | None:
    """Trích (sender_id, chat_id, text, display_name) từ webhook body của Zalo.

    Trả None nếu không phải tin nhắn văn bản (image/sticker/voice/unsupported).
    """
    result = payload.get("result") or payload.get("data") or {}
    event_name = result.get("event_name") or result.get("eventName") or ""
    if "message.text" not in event_name:
        return None
    msg = result.get("message") or {}
    text = msg.get("text") or ""
    if not text:
        return None
    sender = msg.get("from") or {}
    chat = msg.get("chat") or {}
    return {
        "event_name": event_name,
        "message_id": msg.get("message_id") or msg.get("messageId") or "",
        "sender_id": str(sender.get("id") or chat.get("id") or ""),
        "chat_id": str(chat.get("id") or sender.get("id") or ""),
        "display_name": sender.get("display_name") or sender.get("displayName") or "khách",
        "text": text,
        "already_seen": str(msg.get("message_id") or "") in _seen_message_ids,
    }


def send_message(chat_id: str, text: str) -> dict:
    """Gửi tin nhắn văn bản (parse_mode=markdown → bold/list hiển thị đẹp trên Zalo)."""
    body = {"chat_id": chat_id, "text": text[:2000], "parse_mode": "markdown"}
    with httpx.Client(timeout=20) as c:
        try:
            r = c.post(_api("sendMessage"), json=body)
            return r.json() if r.status_code == 200 else {"ok": False, "http": r.status_code, "body": r.text[:300]}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def mark_seen(message_id: str) -> None:
    if message_id:
        _seen_message_ids.add(message_id)
        if len(_seen_message_ids) > 1000:
            _seen_message_ids.clear()
