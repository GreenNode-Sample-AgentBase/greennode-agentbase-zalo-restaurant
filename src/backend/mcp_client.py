"""Thin stateless MCP client over an MCP Gateway / Connector endpoint.

Gửi JSON-RPC trực tiếp tới <MCP_URL> (không cần initialize/keep-alive).
IAM Bearer token lấy từ GREENNODE_CLIENT_ID / GREENNODE_CLIENT_SECRET
(tự động inject khi chạy trên AgentBase Runtime).
"""

from __future__ import annotations

import json
import os
import threading
import time

import httpx

IAM_TOKEN_URL = "https://iam.api.vngcloud.vn/accounts-api/v2/auth/token"

_token_lock = threading.Lock()
_token_cache: dict = {"token": None, "exp": 0.0}


def get_token(force: bool = False) -> str:
    """Lấy IAM token (client credentials), cache với margin 60s."""
    with _token_lock:
        now = time.time()
        if not force and _token_cache["token"] and now < _token_cache["exp"] - 60:
            return _token_cache["token"]

        cid = os.environ.get("GREENNODE_CLIENT_ID")
        sec = os.environ.get("GREENNODE_CLIENT_SECRET")
        if not cid or not sec:
            raise RuntimeError(
                "Thiếu GREENNODE_CLIENT_ID/GREENNODE_CLIENT_SECRET "
                "(trên AgentBase Runtime chúng được tự động inject)."
            )
        r = httpx.post(
            IAM_TOKEN_URL,
            auth=(cid, sec),
            data={"grant_type": "client_credentials"},
            timeout=30,
        )
        r.raise_for_status()
        token = r.json()["access_token"]
        _token_cache["token"] = token
        _token_cache["exp"] = float(
            _jwt_exp(token) if _jwt_exp(token) else now + 1500
        )
        return token


def _jwt_exp(token: str) -> float:
    try:
        import base64

        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        claims = json.loads(base64.urlsafe_b64decode(part))
        return float(claims.get("exp", 0))
    except Exception:
        return 0.0


def mcp_request(
    mcp_url: str, method: str, params: dict | None = None
) -> tuple[int, dict | str]:
    """POST một JSON-RPC request tới MCP URL. Trả về (http_status, parsed).

    Hỗ trợ cả response JSON thuần và SSE (data: lines).
    """
    body: dict = {
        "jsonrpc": "2.0",
        "id": int(time.time() * 1000) % 10**9,
        "method": method,
    }
    if params is not None:
        body["params"] = params

    headers = {
        "Authorization": f"Bearer {get_token()}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    with httpx.Client(timeout=120) as client:
        r = client.post(mcp_url, headers=headers, json=body)
        if r.status_code == 401:  # token hết hạn → refresh 1 lần rồi retry
            headers["Authorization"] = f"Bearer {get_token(force=True)}"
            r = client.post(mcp_url, headers=headers, json=body)

    if r.status_code != 200:
        return r.status_code, r.text[:2000]

    raw = r.text
    try:
        if raw.lstrip().startswith("{"):
            return 200, json.loads(raw)
        for line in raw.splitlines():  # SSE format
            if line.startswith("data:"):
                return 200, json.loads(line[5:].strip())
    except Exception:
        return 200, raw
    return 200, raw


def list_tools(mcp_url: str) -> list[dict]:
    """tools/list — trả về danh sách tool definitions."""
    st, body = mcp_request(mcp_url, "tools/list")
    if st != 200 or not isinstance(body, dict):
        raise RuntimeError(f"tools/list thất bại ({st}): {str(body)[:300]}")
    return body.get("result", {}).get("tools", [])


def call_tool(mcp_url: str, tool: str, arguments: dict) -> str:
    """tools/call — trả về text content; nhận diện DENIED_BY_POLICY.

    MCP Gateway trả deny dưới 2 dạng (tuỳ version):
      - HTTP 403 trực tiếp
      - HTTP 200 + result.isError=true + text chứa "denied by policy"
    """
    st, body = mcp_request(mcp_url, "tools/call", {"name": tool, "arguments": arguments})
    if st == 403:
        return (
            f"DENIED_BY_POLICY (HTTP 403): tool '{tool}' không được phép cho agent này "
            f"theo Policy Group của MCP Gateway. Body: {body}"
        )
    if st != 200:
        return f"MCP_ERROR (HTTP {st}) khi gọi tool '{tool}': {body}"
    if not isinstance(body, dict):
        return str(body)[:4000]
    if "error" in body:
        return f"MCP_RPC_ERROR: {json.dumps(body['error'])[:2000]}"

    result = body.get("result", {})
    texts = [
        item.get("text", "")
        for item in result.get("content", [])
        if isinstance(item, dict) and item.get("type") == "text"
    ]
    joined = "\n".join(texts)
    if result.get("isError") and "denied by policy" in joined.lower():
        return (
            f"DENIED_BY_POLICY: tool '{tool}' không được phép cho agent này "
            f"theo Policy Group của MCP Gateway."
        )
    return joined or json.dumps(result)[:4000]