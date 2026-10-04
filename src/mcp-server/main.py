"""Restaurant MCP Server: the restaurant's business API, exposed as MCP tools.

Deployment model: this server does NOT run on AgentBase. Run it inside the customer VPC
(vServer with docker compose, or VKS) and register it as a connector `restaurant` on a
Private MCP Gateway (Outbound Auth = API Key, header `X-Api-Key`). The agent runtime reaches
it only through the gateway: Agent -> MCP Gateway -> Policy Group -> connector -> this server.

Auth (fail-closed): /mcp requires an API key.
  - MCP_API_KEYS="key1,key2"  (several keys allow rotation without downtime)
  - Header: `X-Api-Key: <key>` or `Authorization: Bearer <key>`
  - No key configured -> /mcp returns 503 (it never opens itself). Set ALLOW_ANONYMOUS=true
    only for local development.
  - A wrong or missing key -> 401. /health and / are always open (health probes).
  The gateway attaches the key when it forwards a call (the secret lives in Access Control),
  so the agent never sees it.

State is stored in **SQLite** (bookings + loyalty), so a restart does not lose data. The DB
path comes from env `MCP_DB_PATH` (default `data/restaurant.db` next to this file, which is
`/app/data/restaurant.db` in the container). Mount a persistent volume on `/app/data`.
Environment: PORT (default 8080), MCP_DB_PATH, MCP_API_KEYS, ALLOW_ANONYMOUS.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
import threading
import time as _time
import uuid
from pathlib import Path

from starlette.routing import Route
from starlette.responses import JSONResponse
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("restaurant-mcp")

# ------------------------- Auth configuration -------------------------


def _load_api_keys() -> list[str]:
    raw = os.environ.get("MCP_API_KEYS", "")
    return [k.strip() for k in raw.split(",") if k.strip()]


API_KEYS = _load_api_keys()
ALLOW_ANONYMOUS = os.environ.get("ALLOW_ANONYMOUS", "").strip().lower() in ("1", "true", "yes")
for _k in API_KEYS:
    if len(_k) < 24:
        log.warning("MCP_API_KEYS contains a key shorter than 24 characters; use `openssl rand -hex 32`")

mcp = FastMCP("restaurant", stateless_http=True, json_response=True, host="0.0.0.0")

# ------------------------- Static data -------------------------
# Dish names are the restaurant's Vietnamese menu names (proper nouns); notes are in English.

MENU = {
    "khai-vi": [
        {"id": "A1", "name": "Gỏi cuốn tôm thịt", "price": 45000, "note": "fresh shrimp and pork spring rolls; vegetarian dipping sauce available"},
        {"id": "A2", "name": "Chả giò rế", "price": 55000, "note": "crispy lattice spring rolls"},
        {"id": "A3", "name": "Nộm xoài khô bò", "price": 50000, "note": "spicy dried-beef mango salad"},
    ],
    "mon-chinh": [
        {"id": "M1", "name": "Bò bò kho bánh mì", "price": 89000, "note": "beef stew with bread"},
        {"id": "M2", "name": "Cá kho tộ", "price": 120000, "note": "caramelised fish in clay pot"},
        {"id": "M3", "name": "Cơm cháy cá sặc", "price": 95000, "note": "crispy rice with snakeskin gourami fish"},
        {"id": "M4", "name": "Lẩu gà lá chanh", "price": 350000, "note": "lime-leaf chicken hotpot, serves 4"},
        {"id": "M5", "name": "Cơm chay thập cẩm", "price": 70000, "note": "vegetarian mixed rice"},
    ],
    "nuoc": [
        {"id": "N1", "name": "Cà phê sữa đá", "price": 30000, "note": "iced milk coffee"},
        {"id": "N2", "name": "Trà tắc", "price": 25000, "note": "iced kumquat tea"},
        {"id": "N3", "name": "Soda chanh", "price": 35000, "note": "lime soda"},
    ],
    "trang-mieng": [
        {"id": "D1", "name": "Chè ba màu", "price": 35000, "note": "three-colour sweet dessert"},
        {"id": "D2", "name": "Bánh flan", "price": 30000, "note": "caramel flan"},
    ],
}

# Tables T1..T12 with different capacities
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
        # Read-only filesystem fallback. In production set MCP_DB_PATH to a path on the
        # persistent volume so data is never silently written to ephemeral storage.
        logging.getLogger("restaurant-mcp").warning(
            "default data dir %s is not writable; falling back to /tmp (data is NOT persistent)",
            default.parent)
        return "/tmp/restaurant.db"


DB_PATH = _resolve_db_path()
_write_lock = threading.Lock()  # FastMCP tools run in a thread pool: serialise writes


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
    """Show the menu. An empty category returns everything (khai-vi, mon-chinh, nuoc, trang-mieng)."""
    if category:
        items = MENU.get(category)
        if items is None:
            return _err(f"unknown category '{category}'")
        return _ok({"category": category, "items": items})
    return _ok({"menu": MENU})


@mcp.tool()
def check_availability(date: str, time: str, party_size: int) -> str:
    """Check free tables for a date, time and party size. date 'YYYY-MM-DD', time 'HH:MM'."""
    if party_size <= 0 or party_size > 20:
        return _err("party_size must be between 1 and 20")
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
    """Create a booking. Use notes for allergies, favourite dishes, special occasions, etc."""
    if party_size <= 0 or party_size > 20:
        return _err("party_size must be between 1 and 20")
    if table and table not in TABLES:
        return _err(f"table {table} does not exist")
    if not customer.strip():
        return _err("customer name is required")

    bid = f"bk-{uuid.uuid4().hex[:8]}"
    created_at = _time.strftime("%Y-%m-%dT%H:%M:%S")

    with _write_lock, _db() as conn:
        busy = _busy_tables(conn, date, time)
        if table and table in busy:
            return _err(f"table {table} is already booked for this slot")
        if not table:
            free = [t for t in sorted(TABLES) if t not in busy and TABLES[t] >= party_size]
            if not free:
                return _err("no suitable table is free for this slot")
            table = free[0]
        conn.execute(
            "INSERT INTO bookings(id, customer, date, time, party_size, table_id, status, notes, created_at) "
            "VALUES(?,?,?,?,?,?,'CONFIRMED',?,?)",
            (bid, customer.strip(), date, time, party_size, table, notes, created_at),
        )
        _loyalty_add(conn, customer.strip(), 10, f"booking {bid} +10 points")

    booking = {
        "id": bid, "customer": customer.strip(), "date": date, "time": time,
        "party_size": party_size, "table": table, "status": "CONFIRMED",
        "notes": notes, "created_at": created_at,
    }
    return _ok({"booking": booking})


@mcp.tool()
def list_bookings(customer: str = "") -> str:
    """List bookings (filtered by guest when customer is given)."""
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
    """Cancel a booking by id (bk-xxxx)."""
    with _write_lock, _db() as conn:
        row = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
        if row is None:
            return _err(f"booking {booking_id} not found")
        conn.execute("UPDATE bookings SET status='CANCELLED' WHERE id=?", (booking_id,))
    booking = _row_to_booking(row)
    booking["status"] = "CANCELLED"
    return _ok({"booking": booking})


@mcp.tool()
def get_loyalty(customer: str) -> str:
    """Show a guest's loyalty points."""
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
    """Add loyalty points for a guest (for example a birthday bonus). Negative values deduct."""
    if points == 0:
        return _err("points must not be 0")
    with _write_lock, _db() as conn:
        _loyalty_add(conn, customer, points, reason or "points added")
        row = conn.execute("SELECT points FROM loyalty WHERE customer=?", (customer,)).fetchone()
    return _ok({"customer": customer, "points": row["points"]})


# ------------------------- HTTP app -------------------------

TOOL_NAMES = ["get_menu", "check_availability", "create_booking", "list_bookings",
              "cancel_booking", "get_loyalty", "add_loyalty_points"]


def _auth_mode() -> str:
    if API_KEYS:
        return f"api-key ({len(API_KEYS)} key)"
    return "anonymous (ALLOW_ANONYMOUS, local use only)" if ALLOW_ANONYMOUS else "locked (MCP_API_KEYS not set)"


async def health(request):
    return JSONResponse(
        {
            "status": "ok",
            "server": "restaurant-mcp",
            "tools": len(TOOL_NAMES),
            "db": os.path.basename(DB_PATH),
            "db_ok": os.path.exists(DB_PATH),
            "mcp_auth": _auth_mode(),
        }
    )


async def root(request):
    return JSONResponse(
        {
            "server": "restaurant-mcp",
            "mcp_endpoint": "/mcp",
            "auth": "X-Api-Key: <key>  or  Authorization: Bearer <key>",
            "persistence": f"sqlite ({DB_PATH})",
            "tools": TOOL_NAMES,
        }
    )


# streamable_http_app() returns a Starlette app whose lifespan starts the session manager.
# MCP streamable HTTP is served at /mcp by default. Extra routes are appended to this same
# app (do NOT mount it into another app: Mount does not run the sub-app's lifespan).
asgi_app = mcp.streamable_http_app()
asgi_app.router.routes.append(Route("/health", health, methods=["GET"]))
asgi_app.router.routes.append(Route("/", root, methods=["GET"]))


def _extract_key(headers) -> str:
    h = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in headers or []}
    if h.get("x-api-key"):
        return h["x-api-key"].strip()
    auth = h.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return ""


def _key_valid(supplied: str) -> bool:
    if not supplied:
        return False
    ok = False
    for good in API_KEYS:  # check every key so timing does not reveal which one matched
        ok |= secrets.compare_digest(supplied.encode(), good.encode())
    return ok


class RequireApiKeyMiddleware:
    """Fail-closed ASGI middleware for /mcp.

    - MCP_API_KEYS set -> a valid key is mandatory (401 when missing or wrong).
    - No key and ALLOW_ANONYMOUS -> open (local development only).
    - No key and no ALLOW_ANONYMOUS -> 503; the server never opens itself.
    - /health and / are always open (health probes must return 200).
    """

    def __init__(self, app):
        self.app = app

    @staticmethod
    async def _reply(send, status: int, message: str, extra=()):
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), *extra]})
        await send({"type": "http.response.body",
                    "body": json.dumps({"error": message}, ensure_ascii=False).encode()})

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope.get("type") == "http" and (path.rstrip("/") == "/mcp" or path.startswith("/mcp/")):
            if not API_KEYS:
                if not ALLOW_ANONYMOUS:
                    return await self._reply(send, 503, "MCP_API_KEYS is not configured (fail-closed)")
            elif not _key_valid(_extract_key(scope.get("headers"))):
                client = (scope.get("client") or ("?",))[0]
                log.warning("401 on /mcp from %s: missing or invalid API key", client)
                return await self._reply(send, 401, "missing or invalid API key (X-Api-Key / Bearer)",
                                         [(b"www-authenticate", b'Bearer realm="restaurant-mcp"')])
        await self.app(scope, receive, send)


app = RequireApiKeyMiddleware(asgi_app)


if __name__ == "__main__":
    import uvicorn

    log.info("restaurant-mcp | %d tools | auth: %s | db: %s", len(TOOL_NAMES), _auth_mode(), DB_PATH)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
