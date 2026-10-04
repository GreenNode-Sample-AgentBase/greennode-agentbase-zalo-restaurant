"""Unit tests for the MCP server: tools, SQLite persistence (a temp DB per test) and API-key auth.

The module is imported with spec_from_file_location to avoid clashing with the name `main.py`
(src/backend also has a main.py on sys.path).
"""
import importlib.util
import json
import sys
from pathlib import Path

MCP_MAIN = Path(__file__).resolve().parent.parent / "src" / "mcp-server" / "main.py"


def _load_with_db(tmp_path, monkeypatch):
    """Re-import the mcp-server main module with a fresh temp MCP_DB_PATH (clean DB)."""
    monkeypatch.setenv("MCP_DB_PATH", str(tmp_path / "restaurant.db"))
    sys.modules.pop("mcp_server_main", None)
    spec = importlib.util.spec_from_file_location("mcp_server_main", MCP_MAIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mcp_server_main"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_booking_crud_and_persistence(tmp_path, monkeypatch):
    srv = _load_with_db(tmp_path, monkeypatch)

    # create a booking
    out = json.loads(srv.create_booking("Hung", "2026-10-02", "19:00", 4, notes="no spicy food"))
    assert out["ok"] is True
    bid = out["booking"]["id"]
    assert out["booking"]["status"] == "CONFIRMED"

    # loyalty +10 is automatic
    loy = json.loads(srv.get_loyalty("Hung"))
    assert loy["ok"] is True
    assert loy["points"] == 10

    # list + filter
    lst = json.loads(srv.list_bookings("hung"))  # case-insensitive
    assert len(lst["bookings"]) == 1
    assert lst["bookings"][0]["id"] == bid

    # the booked table is no longer free for that slot
    av = json.loads(srv.check_availability("2026-10-02", "19:00", 4))
    tables = [t["table"] for t in av["available"]]
    assert out["booking"]["table"] not in tables

    # cancel -> the table is free again
    can = json.loads(srv.cancel_booking(bid))
    assert can["booking"]["status"] == "CANCELLED"
    av2 = json.loads(srv.check_availability("2026-10-02", "19:00", 4))
    assert out["booking"]["table"] in [t["table"] for t in av2["available"]]


def test_persistence_across_reload(tmp_path, monkeypatch):
    """A restart (module re-import) keeps the booking: SQLite persistence."""
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
    assert json.loads(srv.cancel_booking("bk-does-not-exist"))["ok"] is False


def test_add_loyalty_points(tmp_path, monkeypatch):
    srv = _load_with_db(tmp_path, monkeypatch)
    assert json.loads(srv.add_loyalty_points("Mai", 50, "birthday"))["points"] == 50
    assert json.loads(srv.add_loyalty_points("Mai", 10))["points"] == 60
    hist = json.loads(srv.get_loyalty("Mai"))["history"]
    assert len(hist) == 2


def test_menu_and_tools_register(tmp_path, monkeypatch):
    srv = _load_with_db(tmp_path, monkeypatch)
    assert sorted(srv.TOOL_NAMES) == sorted([
        "get_menu", "check_availability", "create_booking", "list_bookings",
        "cancel_booking", "get_loyalty", "add_loyalty_points"])
    assert len(srv.TOOL_NAMES) == 7
    menu = json.loads(srv.get_menu())
    assert set(menu["menu"]) == {"khai-vi", "mon-chinh", "nuoc", "trang-mieng"}
    assert json.loads(srv.get_menu("nuoc"))["category"] == "nuoc"
    assert json.loads(srv.get_menu("nope"))["ok"] is False


# ----------------------------- Auth (fail-closed API key) -----------------------------

ACCEPT = {"Accept": "application/json, text/event-stream"}
LIST_BODY = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
KEY_A, KEY_B = "a" * 32, "b" * 32


def test_load_api_keys_env(tmp_path, monkeypatch):
    srv = _load_with_db(tmp_path, monkeypatch)
    monkeypatch.setenv("MCP_API_KEYS", " k1 , ,k2 ")
    assert srv._load_api_keys() == ["k1", "k2"]
    monkeypatch.delenv("MCP_API_KEYS")
    assert srv._load_api_keys() == []


def test_extract_key_headers(tmp_path, monkeypatch):
    srv = _load_with_db(tmp_path, monkeypatch)
    assert srv._extract_key([(b"x-api-key", b" abc ")]) == "abc"
    assert srv._extract_key([(b"authorization", b"Bearer xyz")]) == "xyz"
    assert srv._extract_key([(b"authorization", b"Basic xyz")]) == ""
    assert srv._extract_key([]) == ""
    assert srv._key_valid("") is False


def test_mcp_auth_fail_closed(tmp_path, monkeypatch):
    """One test = one app lifespan (the MCP session manager cannot be restarted in a process)."""
    from starlette.testclient import TestClient

    srv = _load_with_db(tmp_path, monkeypatch)
    with TestClient(srv.app) as c:
        # 1. No key configured -> 503, the server never opens itself
        monkeypatch.setattr(srv, "API_KEYS", [])
        monkeypatch.setattr(srv, "ALLOW_ANONYMOUS", False)
        assert c.post("/mcp", json=LIST_BODY, headers=ACCEPT).status_code == 503
        assert c.get("/health").json()["mcp_auth"].startswith("locked")

        # 2. ALLOW_ANONYMOUS (local development) -> open, all 7 tools listed
        monkeypatch.setattr(srv, "ALLOW_ANONYMOUS", True)
        r = c.post("/mcp", json=LIST_BODY, headers=ACCEPT)
        assert r.status_code == 200
        assert len(r.json()["result"]["tools"]) == 7

        # 3. Keys configured -> a key is always required, even with ALLOW_ANONYMOUS
        monkeypatch.setattr(srv, "API_KEYS", [KEY_A, KEY_B])
        r = c.post("/mcp", json=LIST_BODY, headers=ACCEPT)
        assert r.status_code == 401 and "www-authenticate" in r.headers
        assert c.post("/mcp", json=LIST_BODY, headers={**ACCEPT, "X-Api-Key": "wrong"}).status_code == 401
        assert c.post("/mcp", json=LIST_BODY,
                      headers={**ACCEPT, "Authorization": "Bearer wrong"}).status_code == 401

        # 4. Both header styles are accepted, and both rotation keys work
        for hdr in ({"X-Api-Key": KEY_A}, {"Authorization": f"Bearer {KEY_B}"}):
            r = c.post("/mcp", json=LIST_BODY, headers={**ACCEPT, **hdr})
            assert r.status_code == 200, hdr
            assert len(r.json()["result"]["tools"]) == 7

        # 5. /health and / need no key and do not leak the key
        assert c.get("/health").status_code == 200
        assert c.get("/").status_code == 200
        health = c.get("/health").json()
        assert health["mcp_auth"] == "api-key (2 key)" and health["tools"] == 7
        assert KEY_A not in json.dumps(health) and KEY_A not in c.get("/").text
