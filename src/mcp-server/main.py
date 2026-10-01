"""Restaurant MCP Server — business API của quán, expose thành MCP tools.

Deploy như 1 Custom Agent runtime trên AgentBase (port 8080, GET /health).
Sau đó kết nối vào MCP Gateway qua **Add Custom Connector** (auth: No authorization)
→ agent gọi các tool này qua gateway với Policy kiểm soát.

State được lưu **SQLite** (bookings + loyalty) → restart runtime không mất dữ liệu.
Đường dẫn DB: env `MCP_DB_PATH` (mặc định `data/restaurant.db` cạnh file này,
fallback `/tmp/restaurant.db` nếu thư mục không ghi được).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time as _time
import uuid
from pathlib import Path

from starlette.routing import Route
from starlette.responses import JSONResponse
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("restaurant", stateless_http=True, json_response=True, host="0.0.0.0")

# ------------------------- Dữ liệu tĩnh -------------------------

MENU = {
    "khai-vi": [
        {"id": "A1", "name": "Gỏi cuốn tôm thịt", "price": 45000, "note": "có nước chấm chay"},
        {"id": "A2", "name": "Chả giò rế", "price": 55000, "note": ""},
        {"id": "A3", "name": "Nộm xoài khô bò", "price": 50000, "note": "cay"},
    ],
    "mon-chinh": [
        {"id": "M1", "name": "Bò bò kho bánh mì", "price": 89000, "note": ""},
        {"id": "M2", "name": "Cá kho tộ", "price": 120000, "note": ""},
        {"id": "M3", "name": "Cơm cháy cá sặc", "price": 95000, "note": ""},
        {"id": "M4", "name": "Lẩu gà lá chanh", "price": 350000, "note": "cho 4 người"},
        {"id": "M5", "name": "Cơm chay thập cẩm", "price": 70000, "note": "chay"},
    ],
    "nuoc": [
        {"id": "N1", "name": "Cà phê sữa đá", "price": 30000, "note": ""},
        {"id": "N2", "name": "Trà tắc", "price": 25000, "note": ""},
        {"id": "N3", "name": "Soda chanh", "price": 35000, "note": ""},
    ],
    "trang-mieng": [
        {"id": "D1", "name": "Chè ba màu", "price": 35000, "note": ""},
        {"id": "D2", "name": "Bánh flan", "price": 30000, "note": ""},
    ],
}

# Bàn: T1..T12, sức chứa khác nhau
TABLES = {f"T{i}": seats for i, seats in enumerate([2, 2, 4, 4, 4, 4, 6, 6, 8, 8, 10, 12], start=1)}

# ------------------------- SQLite persistence -------------------------


def _resolve_db_path() -> str:
    env_path = os.environ.get("MCP_DB_PATH", "").strip()
    if env_path:
        return env_path
    default = Path(__file__).resolve().parent / "data" / "restaurant.db"
    try:
        default.parent.mkdir(parents=True, exist_ok=True)
        probe = default.parent / ".probe"
        probe.touch()
        probe.unlink()
        return str(default)
    except OSError:
        return "/tmp/restaurant.db"  # filesystem read-only → fallback


DB_PATH = _resolve_db_path()
_write_lock = threading.Lock()  # FastMCP tools chạy threadpool → serialise writes


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_db() -> None:
    with _db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS bookings (
                id         TEXT PRIMARY KEY,
                customer   TEXT NOT NULL,
                date       TEXT NOT NULL,
                time       TEXT NOT NULL,
                party_size INTEGER NOT NULL,
                table_id   TEXT NOT NULL,
                status     TEXT NOT NULL DEFAULT 'CONFIRMED',
                notes      TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_bookings_slot ON bookings(date, time, status);
            CREATE TABLE IF NOT EXISTS loyalty (
                customer TEXT PRIMARY KEY,
                points   INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS loyalty_history (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                customer TEXT NOT NULL,
                ts       TEXT NOT NULL,
                reason   TEXT NOT NULL
            );
            """
        )


_init_db()


def _row_to_booking(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "customer": r["customer"],
        "date": r["date"],
        "time": r["time"],
        "party_size": r["party_size"],
        "table": r["table_id"],
        "status": r["status"],
        "notes": r["notes"],
        "created_at": r["created_at"],
    }


def _busy_tables(conn: sqlite3.Connection, date: str, time: str) -> set[str]:
    rows = conn.execute(
        "SELECT table_id FROM bookings WHERE date=? AND time=? AND status='CONFIRMED'",
        (date, time),
    ).fetchall()
    return {r["table_id"] for r in rows}


def _loyalty_add(conn: sqlite3.Connection, customer: str, points: int, reason: str) -> None:
    conn.execute(
        "INSERT INTO loyalty(customer, points) VALUES(?, ?) "
        "ON CONFLICT(customer) DO UPDATE SET points = points + ?",
        (customer, points, points),
    )
    conn.execute(
        "INSERT INTO loyalty_history(customer, ts, reason) VALUES(?, ?, ?)",
        (customer, _time.strftime("%Y-%m-%dT%H:%M:%S"), reason),
    )


def _ok(payload: dict) -> str:
    return json.dumps({"ok": True, **payload}, ensure_ascii=False)


def _err(msg: str) -> str:
    return json.dumps({"ok": False, "error": msg}, ensure_ascii=False)


# ------------------------- MCP Tools -------------------------


@mcp.tool()
def get_menu(category: str = "") -> str:
    """Xem menu quán. category để trống = tất cả (khai-vi, mon-chinh, nuoc, trang-mieng)."""
    if category:
        items = MENU.get(category)
        if items is None:
            return _err(f"không có category '{category}'")
        return _ok({"category": category, "items": items})
    return _ok({"menu": MENU})


@mcp.tool()
def check_availability(date: str, time: str, party_size: int) -> str:
    """Kiểm tra bàn trống cho ngày/giờ/số khách. date 'YYYY-MM-DD', time 'HH:MM'."""
    if party_size <= 0 or party_size > 20:
        return _err("party_size phải từ 1 đến 20")
    with _db() as conn:
        busy = _busy_tables(conn, date, time)
    free = [
        {"table": t, "seats": seats}
        for t, seats in sorted(TABLES.items())
        if t not in busy and seats >= party_size
    ]
    return _ok({"date": date, "time": time, "party_size": party_size, "available": free[:5]})


@mcp.tool()
def create_booking(
    customer: str, date: str, time: str, party_size: int, table: str = "", notes: str = ""
) -> str:
    """Tạo đặt bàn. notes ghi riêng: dị ứng, món ưa thích, dịp đặc biệt..."""
    if party_size <= 0 or party_size > 20:
        return _err("party_size phải từ 1 đến 20")
    if table and table not in TABLES:
        return _err(f"bàn {table} không tồn tại")
    if not customer.strip():
        return _err("thiếu tên khách")

    bid = f"bk-{uuid.uuid4().hex[:8]}"
    created_at = _time.strftime("%Y-%m-%dT%H:%M:%S")

    with _write_lock, _db() as conn:
        busy = _busy_tables(conn, date, time)
        if table and table in busy:
            return _err(f"bàn {table} đã có người đặt khung này")
        if not table:
            free = [t for t in sorted(TABLES) if t not in busy and TABLES[t] >= party_size]
            if not free:
                return _err("hết bàn phù hợp khung này")
            table = free[0]
        conn.execute(
            "INSERT INTO bookings(id, customer, date, time, party_size, table_id, status, notes, created_at) "
            "VALUES(?,?,?,?,?,?,'CONFIRMED',?,?)",
            (bid, customer.strip(), date, time, party_size, table, notes, created_at),
        )
        _loyalty_add(conn, customer.strip(), 10, f"đặt bàn {bid} +10 điểm")

    booking = {
        "id": bid, "customer": customer.strip(), "date": date, "time": time,
        "party_size": party_size, "table": table, "status": "CONFIRMED",
        "notes": notes, "created_at": created_at,
    }
    return _ok({"booking": booking})


@mcp.tool()
def list_bookings(customer: str = "") -> str:
    """Danh sách đặt bàn (lọc theo khách nếu truyền customer)."""
    with _db() as conn:
        if customer:
            rows = conn.execute(
                "SELECT * FROM bookings WHERE lower(customer)=lower(?) ORDER BY created_at DESC LIMIT 100",
                (customer.strip(),),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM bookings ORDER BY created_at DESC LIMIT 100"
            ).fetchall()
    return _ok({"bookings": [_row_to_booking(r) for r in rows]})


@mcp.tool()
def cancel_booking(booking_id: str) -> str:
    """Huỷ đặt bàn theo id (bk-xxxx)."""
    with _write_lock, _db() as conn:
        row = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
        if row is None:
            return _err(f"không tìm thấy {booking_id}")
        conn.execute("UPDATE bookings SET status='CANCELLED' WHERE id=?", (booking_id,))
    booking = _row_to_booking(row)
    booking["status"] = "CANCELLED"
    return _ok({"booking": booking})


@mcp.tool()
def get_loyalty(customer: str) -> str:
    """Xem điểm thân thiết của khách."""
    with _db() as conn:
        row = conn.execute("SELECT points FROM loyalty WHERE customer=?", (customer,)).fetchone()
        hist = conn.execute(
            "SELECT ts, reason FROM loyalty_history WHERE customer=? ORDER BY id DESC LIMIT 20",
            (customer,),
        ).fetchall()
    return _ok(
        {
            "customer": customer,
            "points": row["points"] if row else 0,
            "history": [{"time": h["ts"], "reason": h["reason"]} for h in hist],
        }
    )


@mcp.tool()
def add_loyalty_points(customer: str, points: int, reason: str = "") -> str:
    """Cộng điểm thân thiết cho khách (ví dụ thưởng sinh nhật)."""
    if points == 0:
        return _err("points phải khác 0")
    with _write_lock, _db() as conn:
        _loyalty_add(conn, customer, points, reason or "cộng điểm")
        row = conn.execute("SELECT points FROM loyalty WHERE customer=?", (customer,)).fetchone()
    return _ok({"customer": customer, "points": row["points"]})


# ------------------------- HTTP app -------------------------


async def health(request):
    return JSONResponse(
        {
            "status": "ok",
            "server": "restaurant-mcp",
            "tools": 7,
            "db": os.path.basename(DB_PATH),
            "db_ok": os.path.exists(DB_PATH),
        }
    )


async def root(request):
    return JSONResponse(
        {
            "server": "restaurant-mcp",
            "mcp_endpoint": "/mcp",
            "persistence": f"sqlite ({DB_PATH})",
            "tools": ["get_menu", "check_availability", "create_booking", "list_bookings",
                      "cancel_booking", "get_loyalty", "add_loyalty_points"],
        }
    )


# streamable_http_app() trả Starlette app với lifespan khởi động session manager.
# MCP streamable HTTP mặc định tại /mcp. Thêm routes phụ trợ vào chính app này
# (KHÔNG mount vào app khác — Mount không chạy lifespan của sub-app).
app = mcp.streamable_http_app()
app.router.routes.append(Route("/health", health, methods=["GET"]))
app.router.routes.append(Route("/", root, methods=["GET"]))

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
