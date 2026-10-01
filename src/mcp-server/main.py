"""Restaurant MCP Server — business API của quán, expose thành MCP tools.

Deploy như 1 Custom Agent runtime trên AgentBase (port 8080, GET /health).
Sau đó kết nối vào MCP Gateway qua **Add Custom Connector** (auth: No authorization)
→ agent gọi các tool này qua gateway với Policy kiểm soát.

State là in-memory (demo) — production hãy thay bằng DB.
"""

from __future__ import annotations

import json
import time as _time
import uuid

from starlette.routing import Route
from starlette.responses import JSONResponse
from starlette.routing import Route
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("restaurant", stateless_http=True, json_response=True, host="0.0.0.0")

# ------------------------- Dữ liệu demo -------------------------
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

bookings: dict[str, dict] = {}
loyalty: dict[str, dict] = {}


def _ok(payload: dict) -> str:
    return json.dumps({"ok": True, **payload}, ensure_ascii=False)


# ------------------------- MCP Tools -------------------------
@mcp.tool()
def get_menu(category: str = "") -> str:
    """Xem menu quán. category để trống = tất cả (khai-vi, mon-chinh, nuoc, trang-mieng)."""
    if category:
        items = MENU.get(category)
        if items is None:
            return json.dumps({"ok": False, "error": f"không có category '{category}'"}, ensure_ascii=False)
        return _ok({"category": category, "items": items})
    return _ok({"menu": MENU})


@mcp.tool()
def check_availability(date: str, time: str, party_size: int) -> str:
    """Kiểm tra bàn trống cho ngày/giờ/số khách. date 'YYYY-MM-DD', time 'HH:MM'."""
    if party_size <= 0 or party_size > 20:
        return json.dumps({"ok": False, "error": "party_size phải từ 1 đến 20"}, ensure_ascii=False)
    busy = {
        b["table"]
        for b in bookings.values()
        if b["date"] == date and b["time"] == time and b["status"] == "CONFIRMED"
    }
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
    if table and table not in TABLES:
        return json.dumps({"ok": False, "error": f"bàn {table} không tồn tại"}, ensure_ascii=False)
    busy = {
        b["table"]
        for b in bookings.values()
        if b["date"] == date and b["time"] == time and b["status"] == "CONFIRMED"
    }
    if table and table in busy:
        return json.dumps({"ok": False, "error": f"bàn {table} đã có người đặt khung này"}, ensure_ascii=False)
    if not table:
        free = [t for t in sorted(TABLES) if t not in busy and TABLES[t] >= party_size]
        if not free:
            return json.dumps({"ok": False, "error": "hết bàn phù hợp khung này"}, ensure_ascii=False)
        table = free[0]

    bid = f"bk-{uuid.uuid4().hex[:8]}"
    bookings[bid] = {
        "id": bid,
        "customer": customer,
        "date": date,
        "time": time,
        "party_size": party_size,
        "table": table,
        "status": "CONFIRMED",
        "notes": notes,
        "created_at": _time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    # cộng điểm loyalty cơ bản
    prof = loyalty.setdefault(customer, {"points": 0, "history": []})
    prof["points"] += 10
    prof["history"].append({"time": bookings[bid]["created_at"], "reason": f"đặt bàn {bid} +10 điểm"})
    return _ok({"booking": bookings[bid]})


@mcp.tool()
def list_bookings(customer: str = "") -> str:
    """Danh sách đặt bàn (lọc theo khách nếu truyền customer)."""
    out = [
        b
        for b in bookings.values()
        if not customer or b["customer"].lower() == customer.lower()
    ]
    return _ok({"bookings": sorted(out, key=lambda b: b["created_at"], reverse=True)})


@mcp.tool()
def cancel_booking(booking_id: str) -> str:
    """Huỷ đặt bàn theo id (bk-xxxx)."""
    b = bookings.get(booking_id)
    if not b:
        return json.dumps({"ok": False, "error": f"không tìm thấy {booking_id}"}, ensure_ascii=False)
    b["status"] = "CANCELLED"
    return _ok({"booking": b})


@mcp.tool()
def get_loyalty(customer: str) -> str:
    """Xem điểm thân thiết của khách."""
    prof = loyalty.get(customer, {"points": 0, "history": []})
    return _ok({"customer": customer, **prof})


@mcp.tool()
def add_loyalty_points(customer: str, points: int, reason: str = "") -> str:
    """Cộng điểm thân thiết cho khách (ví dụ thưởng sinh nhật)."""
    prof = loyalty.setdefault(customer, {"points": 0, "history": []})
    prof["points"] += points
    prof["history"].append({"time": _time.strftime("%Y-%m-%dT%H:%M:%S"), "reason": reason or "cộng điểm"})
    return _ok({"customer": customer, "points": prof["points"]})


# ------------------------- HTTP app -------------------------
async def health(request):
    return JSONResponse({"status": "ok", "server": "restaurant-mcp", "tools": 7})


async def root(request):
    return JSONResponse(
        {
            "server": "restaurant-mcp",
            "mcp_endpoint": "/mcp",
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