"""
Claudenometer MCP server.

Supports two transports (set via TRANSPORT env var):
  stdio  — default; Claude Desktop/Code spawns this process directly.
  sse    — HTTP SSE server; used for Docker / always-on deployments.
           Requires API_KEY env var; every request must include
           "Authorization: Bearer <API_KEY>" or receives 401.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from claudenometer.cronometer import CronometerClient, CronometerError

load_dotenv()

mcp = FastMCP("Claudenometer", instructions=(
    "You are a food logging assistant connected to Cronometer. "
    "When a user logs food, search for it with search_food, pick the best match, "
    "and confirm with the user before calling add_food_entry. "
    "When asked about progress, call get_daily_nutrition and show a clear summary."
))

_client: Optional[CronometerClient] = None


def _get_client() -> CronometerClient:
    global _client
    if _client is None:
        email = os.environ.get("CRONOMETER_EMAIL", "")
        password = os.environ.get("CRONOMETER_PASSWORD", "")
        tz_offset = int(os.environ.get("CRONOMETER_TZ_OFFSET", "0"))
        if not email or not password:
            raise CronometerError(
                "CRONOMETER_EMAIL and CRONOMETER_PASSWORD must be set in .env"
            )
        _client = CronometerClient(email, password, tz_offset)
    return _client


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

@mcp.tool()
def search_food(query: str) -> list[dict]:
    """
    Search the Cronometer food database.

    Returns up to 25 foods matching the query.  Each item contains:
      - food_source_id: pass this to add_food_entry
      - name: human-readable food name
      - servings: list of {serving_id, measure, grams} options
    """
    client = _get_client()
    return client.search_foods(query)


@mcp.tool()
def get_food_details(food_source_id: str) -> dict:
    """
    Return the serving size options for a specific food.

    Use this when search_food results need more detail before logging.
    """
    client = _get_client()
    results = client.search_foods(food_source_id, max_results=5)
    for food in results:
        if food.get("food_source_id") == food_source_id:
            return food
    # Fall back to first result if exact ID isn't in the list
    return results[0] if results else {"error": "Food not found"}


@mcp.tool()
def add_food_entry(
    food_source_id: str,
    serving_id: int,
    amount: float,
    date: str = "today",
) -> dict:
    """
    Log a food serving to the Cronometer diary.

    Args:
        food_source_id: from search_food()
        serving_id:     from the servings list in search_food()
        amount:         number of servings (e.g. 1.5)
        date:           "YYYY-MM-DD" or "today" (default)

    Returns {"success": True, "food_source_id": ..., "amount": ..., "date": ...}
    """
    client = _get_client()
    diary_date = None if date == "today" else date
    client.add_serving(food_source_id, serving_id, amount, diary_date)
    return {
        "success": True,
        "food_source_id": food_source_id,
        "serving_id": serving_id,
        "amount": amount,
        "date": diary_date or "today",
    }


@mcp.tool()
def get_daily_nutrition(date: str = "today") -> dict:
    """
    Return macro totals for a given date.

    Args:
        date: "YYYY-MM-DD" or "today" (default)

    Returns:
        {date, energy_kcal, protein_g, carbs_g, fat_g, fiber_g}
    """
    from datetime import date as date_cls
    client = _get_client()
    diary_date = date_cls.today().isoformat() if date == "today" else date
    return client.get_daily_nutrition(diary_date)


@mcp.tool()
def get_food_log(date: str = "today") -> list[dict]:
    """
    List all diary entries logged for a given date.

    Args:
        date: "YYYY-MM-DD" or "today" (default)

    Returns a list of {name, amount, measure, energy_kcal}.
    """
    from datetime import date as date_cls
    client = _get_client()
    diary_date = date_cls.today().isoformat() if date == "today" else date
    return client.get_food_log(diary_date)


@mcp.tool()
def refresh_connection() -> dict:
    """
    Re-fetch the Cronometer GWT permutation hash and re-authenticate.

    Call this if other tools start returning errors after a Cronometer
    frontend redeploy (which changes the internal hash).  No restart needed.
    """
    global _client
    client = _get_client()
    user_id = client.refresh()
    return {"success": True, "user_id": user_id, "message": "Re-authenticated successfully."}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _run_http(host: str, port: int, api_key: Optional[str]) -> None:
    """Run the MCP server over Streamable HTTP (MCP spec 2025-03-26)."""
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import PlainTextResponse
    from starlette.routing import Mount
    import uvicorn

    class _APIKeyMiddleware(BaseHTTPMiddleware):
        def __init__(self, app, key: str) -> None:
            super().__init__(app)
            self._key = key

        async def dispatch(self, request, call_next):
            auth = request.headers.get("Authorization", "")
            token = request.query_params.get("api_key", "")
            if auth != f"Bearer {self._key}" and token != self._key:
                return PlainTextResponse("Unauthorized", status_code=401)
            return await call_next(request)

    session_manager = StreamableHTTPSessionManager(
        app=mcp._mcp_server,
        event_store=None,
        json_response=False,
        stateless=True,
    )

    async def handle_mcp(scope, receive, send):
        await session_manager.handle_request(scope, receive, send)

    middleware = [Middleware(_APIKeyMiddleware, key=api_key)] if api_key else []

    app = Starlette(
        routes=[Mount("/", app=handle_mcp)],
        middleware=middleware,
    )

    print(
        f"Claudenometer MCP server listening on http://{host}:{port}/mcp",
        f"{'(API key auth enabled)' if api_key else '(WARNING: no API key set)'}",
        file=sys.stderr,
    )
    uvicorn.run(app, host=host, port=port)


def main() -> None:
    transport = os.environ.get("TRANSPORT", "stdio").lower()

    if transport == "sse":
        host = os.environ.get("HOST", "0.0.0.0")
        port = int(os.environ.get("PORT", "8000"))
        api_key = os.environ.get("API_KEY") or None
        _run_http(host, port, api_key)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
