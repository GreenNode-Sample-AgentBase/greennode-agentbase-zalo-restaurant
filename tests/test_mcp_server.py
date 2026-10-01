"""Unit tests cho MCP server — SQLite persistence (DB tmp riêng per test).

Import bằng spec_from_file_location để không đụng độ tên `main.py`
(vì src/backend cũng có main.py trên sys.path).
"""
import importlib.util
import json
import sys
from pathlib import Path

MCP_MAIN = Path(__file__).resolve().parent.parent / "src" / "mcp-server" / "main.py"


def _load_with_db(tmp_path, monkeypatch):
    """Import lại mcp-server main với MCP_DB_PATH tmp mới (DB sạch)."""
    monkeypatch.setenv("MCP_DB_PATH", str(tmp_path / "restaurant.db"))
    sys.modules.pop("mcp_server_main", None)
    spec = importlib.util.spec_from_file_location("mcp_server_main", MCP_MAIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mcp_server_main"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_booking_crud_and_persistence(tmp_path, monkeypatch):
    srv = _load_with_db(tmp_path, monkeypatch)

    # tạo booking
    out = json.loads(srv.create_booking("Hung", "2026-10-02", "19:00", 4, notes="không cay"))
    assert out["ok"] is True
    bid = out["booking"]["id"]
    assert out["booking"]["status"] == "CONFIRMED"

    # loyalty +10 tự động
    loy = json.loads(srv.get_loyalty("Hung"))
    assert loy["ok"] is True
    assert loy["points"] == 10

    # list + filter
    lst = json.loads(srv.list_bookings("hung"))  # case-insensitive
    assert len(lst["bookings"]) == 1
    assert lst["bookings"][0]["id"] == bid

    # bàn vừa đặt → không còn trống khung đó
    av = json.loads(srv.check_availability("2026-10-02", "19:00", 4))
    tables = [t["table"] for t in av["available"]]
    assert out["booking"]["table"] not in tables

    # huỷ → bàn trống lại
    can = json.loads(srv.cancel_booking(bid))
    assert can["booking"]["status"] == "CANCELLED"
    av2 = json.loads(srv.check_availability("2026-10-02", "19:00", 4))
    assert out["booking"]["table"] in [t["table"] for t in av2["available"]]


def test_persistence_across_reload(tmp_path, monkeypatch):
    """Restart 'runtime' (import lại module) → booking vẫn còn (SQLite)."""
    srv = _load_with_db(tmp_path, monkeypatch)
    out = json.loads(srv.create_booking("Lan", "2026-10-03", "11:30", 6))
    assert out["ok"] is True

    srv2 = _load_with_db(tmp_path, monkeypatch)  # "restart"
    lst = json.loads(srv2.list_bookings("Lan"))
    assert len(lst["bookings"]) == 1
    assert lst["bookings"][0]["customer"] == "Lan"


def test_validation_errors(tmp_path, monkeypatch):
    srv = _load_with_db(tmp_path, monkeypatch)
    assert json.loads(srv.create_booking("X", "2026-10-02", "19:00", 99))["ok"] is False
    assert json.loads(srv.create_booking("X", "2026-10-02", "19:00", 2, table="T99"))["ok"] is False
    assert json.loads(srv.cancel_booking("bk-khong-ton-tai"))["ok"] is False


def test_add_loyalty_points(tmp_path, monkeypatch):
    srv = _load_with_db(tmp_path, monkeypatch)
    assert json.loads(srv.add_loyalty_points("Mai", 50, "sinh nhật"))["points"] == 50
    assert json.loads(srv.add_loyalty_points("Mai", 10))["points"] == 60
    hist = json.loads(srv.get_loyalty("Mai"))["history"]
    assert len(hist) == 2
