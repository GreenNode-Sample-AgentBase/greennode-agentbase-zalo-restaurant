"""Unit tests cho zalo.py — parser 2 shape + fit_zalo + secret + dedupe."""
import importlib

import pytest


@pytest.fixture()
def zalo_mod(monkeypatch):
    monkeypatch.setenv("ZALO_WEBHOOK_SECRET", "unit-test-secret")
    monkeypatch.setenv("ZALO_BOT_TOKEN", "123:fake-token")
    import zalo
    return importlib.reload(zalo)


def test_parse_webhook_top_level_shape(zalo_mod):
    """Zalo thực tế gửi event_name/message ở TOP-LEVEL (không nằm trong result)."""
    payload = {
        "event_name": "message.text.received",
        "message": {
            "date": 1790849894761,
            "chat": {"chat_type": "PRIVATE", "id": "chat-123"},
            "text": "Cho em xem menu",
            "message_id": "msg-1",
            "from": {"id": "user-1", "display_name": "Hung", "is_bot": False},
        },
    }
    ev = zalo_mod.parse_webhook(payload)
    assert ev["event_name"] == "message.text.received"
    assert ev["chat_id"] == "chat-123"
    assert ev["text"] == "Cho em xem menu"
    assert ev["sender_id"] == "user-1"
    assert ev["display_name"] == "Hung"


def test_parse_webhook_nested_result_shape(zalo_mod):
    """Docs Zalo mô tả shape nested trong result — hỗ trợ cả 2."""
    payload = {
        "ok": True,
        "result": {
            "event_name": "message.text.received",
            "message": {
                "chat": {"id": "chat-9"},
                "text": "hi",
                "message_id": "msg-2",
                "from": {"id": "user-9"},
            },
        },
    }
    ev = zalo_mod.parse_webhook(payload)
    assert ev["chat_id"] == "chat-9"
    assert ev["text"] == "hi"


def test_parse_webhook_ignores_non_text_events(zalo_mod):
    assert zalo_mod.parse_webhook({"event_name": "webhook.test", "message": {"text": "ping"}}) is None
    assert zalo_mod.parse_webhook({"event_name": "message.sticker.received", "message": {}}) is None
    assert zalo_mod.parse_webhook({}) is None


def test_webhook_secret_ok(zalo_mod):
    assert zalo_mod.webhook_secret_ok("unit-test-secret") is True
    assert zalo_mod.webhook_secret_ok("wrong") is False
    assert zalo_mod.webhook_secret_ok("") is False


def test_fit_zalo_short_text_untouched(zalo_mod):
    assert zalo_mod.fit_zalo("ngắn gọn") == "ngắn gọn"
    assert len(zalo_mod.fit_zalo("x" * 2000)) == 2000


def test_fit_zalo_cuts_at_paragraph_boundary(zalo_mod):
    para = "đoạn văn. " * 30  # ~300 chars/đoạn
    text = "\n\n".join([para] * 12)  # > 2000 chars
    out = zalo_mod.fit_zalo(text)
    assert len(out) <= 2000
    assert out.endswith("(…còn tiếp — nhắn \"tiếp\" để xem phần còn lại nhé ạ)")
    # phần nội dung phải kết thúc tại biên đoạn NGUYÊN VẸN (đoạn văn. hoàn chỉnh + \n\n)
    body = out[: out.index("\n\n(…còn tiếp")]
    # không đứt giữa chừng: mọi token phải là đơn vị hoàn chỉnh "đoạn" / "văn."
    assert set(body.split()) <= {"đoạn", "văn."}
    assert body.rstrip().endswith("văn.")  # kết thúc bằng câu hoàn chỉnh
