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
from datetime import date as date_cls, datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from claudenometer.cronometer import CronometerClient, CronometerError

load_dotenv()

mcp = FastMCP("Claudenometer", instructions=(
    "You are a food logging assistant connected to Cronometer. "
    "When a user logs food, call search_food first. "
    "If the serving size is unclear call get_food_details to see all measures. "
    "Confirm the food and amount with the user, then call add_food_entry. "
    "When asked about progress, call get_daily_nutrition and show a clear summary."
))

_client: Optional[CronometerClient] = None


def _local_today() -> date_cls:
    """Return today's date in the user's local timezone (from CRONOMETER_TZ_OFFSET)."""
    tz_offset = int(os.environ.get("CRONOMETER_TZ_OFFSET", "0"))
    tz = timezone(timedelta(minutes=tz_offset))
    return datetime.now(tz).date()


def _get_client() -> CronometerClient:
    global _client
    if _client is None:
        email = os.environ.get("CRONOMETER_EMAIL", "")
        password = os.environ.get("CRONOMETER_PASSWORD", "")
        if not email or not password:
            raise CronometerError(
                "CRONOMETER_EMAIL and CRONOMETER_PASSWORD must be set in .env"
            )
        _client = CronometerClient(email, password)
    return _client


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

@mcp.tool()
def search_food(query: str) -> list[dict]:
    """
    Search the Cronometer food database.

    Returns up to 25 foods matching the query.  Each item contains:
      - food_id: pass to add_food_entry
      - food_source_id: pass to get_food_details or add_food_entry
      - name: human-readable food name
      - measure_desc: default serving description (e.g. "1 large - 50g")
      - score: relevance score
    """
    client = _get_client()
    return client.find_foods(query)


@mcp.tool()
def get_food_details(food_source_id: int) -> dict:
    """
    Return all available serving size options for a specific food.

    Use this after search_food when you need the full list of measures
    (e.g. "1 cup", "100g", "1 slice") with their weight_grams before logging.

    Each measure contains:
      - description: human-readable measure (e.g. "1 large - 50g")
      - weight_grams: weight in grams for this measure (pass this to add_food_entry)
    """
    client = _get_client()
    return client.get_food(food_source_id)


@mcp.tool()
def add_food_entry(
    food_id: int,
    food_source_id: int,
    weight_grams: float,
    date: str = "today",
    diary_group: int = 1,
) -> dict:
    """
    Log a food serving to the Cronometer diary.

    Args:
        food_id:        from search_food()
        food_source_id: from search_food()
        weight_grams:   total weight in grams for this serving.
                        Multiply the per-serving grams from measure_desc by the
                        number of servings (e.g. "1 large - 50g" × 2 = 100g).
                        Call get_food_details() to see all available measures.
        date:           "YYYY-MM-DD" or "today" (default)
        diary_group:    1=Breakfast, 2=Lunch, 3=Dinner, 4=Snacks (default 1)

    Returns {"serving_id": ..., "food_id": ..., "food_source_id": ..., "date": ...}
    """
    client = _get_client()
    diary_date = _local_today() if date == "today" else date_cls.fromisoformat(date)
    result = client.add_serving(
        food_id=food_id,
        food_source_id=food_source_id,
        measure_id=0,  # always use UNIVERSAL_MEASURE_ID; food-specific IDs cause ghost entries
        quantity=weight_grams,
        weight_grams=weight_grams,
        diary_date=diary_date,
        diary_group=diary_group,
    )
    result["date"] = date
    return result


@mcp.tool()
def create_custom_food(
    name: str,
    serving_name: str,
    serving_grams: float,
    energy_kcal: float,
    protein_g: float,
    fat_g: float,
    carbs_g: float,
    fiber_g: float = 0.0,
) -> dict:
    """
    Create a custom food entry in the user's Cronometer account.

    Use this when search_food returns no close match.  Estimate the nutrients
    from your nutritional knowledge and create a named entry so it appears
    correctly in the diary.  Net Carbs is computed automatically by Cronometer
    as (carbs_g - fiber_g), so enter total carbs and fiber separately.

    Args:
        name:          Food name (e.g. "Publix Rotisserie Chicken Wrap")
        serving_name:  Serving description (e.g. "1 wrap", "100g")
        serving_grams: Weight of one serving in grams
        energy_kcal:   Calories per serving
        protein_g:     Protein per serving in grams
        fat_g:         Total fat per serving in grams
        carbs_g:       Total carbohydrates per serving in grams
        fiber_g:       Dietary fiber per serving in grams (default 0)

    Returns {"food_id": ..., "food_source_id": ..., "name": ...}
    """
    client = _get_client()
    return client.create_custom_food(
        name=name,
        serving_name=serving_name,
        serving_grams=serving_grams,
        energy_kcal=energy_kcal,
        protein_g=protein_g,
        fat_g=fat_g,
        carbs_g=carbs_g,
        fiber_g=fiber_g,
    )


@mcp.tool()
def remove_food_entry(serving_id: str) -> dict:
    """
    Remove a food entry from the Cronometer diary.

    Args:
        serving_id: the serving_id returned by add_food_entry

    Returns {"success": True, "serving_id": ...}
    """
    client = _get_client()
    client.remove_serving(serving_id)
    return {"success": True, "serving_id": serving_id}


@mcp.tool()
def get_daily_nutrition(date: str = "today") -> dict:
    """
    Return macro totals for a given date.

    Args:
        date: "YYYY-MM-DD" or "today" (default)

    Returns:
        {date, energy_kcal, protein_g, carbs_g, fat_g, fiber_g}
    """
    client = _get_client()
    diary_date = _local_today() if date == "today" else date_cls.fromisoformat(date)
    return client.get_daily_nutrition(diary_date)


@mcp.tool()
def get_food_log(date: str = "today") -> list[dict]:
    """
    List all diary entries logged for a given date.

    Args:
        date: "YYYY-MM-DD" or "today" (default)

    Returns a list of {name, amount, unit, meal, energy_kcal, protein_g, carbs_g, fat_g}.
    """
    client = _get_client()
    diary_date = _local_today() if date == "today" else date_cls.fromisoformat(date)
    return client.get_food_log(diary_date)


@mcp.tool()
def refresh_connection() -> dict:
    """
    Re-fetch the Cronometer GWT hashes and re-authenticate.

    Call this if other tools start returning errors after a Cronometer
    frontend redeploy (which changes the internal hashes).  No restart needed.
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
    import anyio
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

    async def serve():
        async with session_manager.run():
            config = uvicorn.Config(app, host=host, port=port, log_level="info")
            server = uvicorn.Server(config)
            await server.serve()

    print(
        f"Claudenometer MCP server listening on http://{host}:{port}/mcp",
        f"{'(API key auth enabled)' if api_key else '(WARNING: no API key set)'}",
        file=sys.stderr,
    )
    anyio.run(serve)


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
