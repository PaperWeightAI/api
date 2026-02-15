"""MCP Resource handler implementations.

Each function returns a JSON string for the MCP resource protocol.
"""

import json
from typing import Any

from . import tools


def _json(data: Any) -> str:
    """Serialise to JSON, handling datetimes and other non-standard types."""
    return json.dumps(data, default=str)


async def stores_resource() -> str:
    return _json(await tools.list_stores())


async def store_detail_resource(store_id: int) -> str:
    return _json(await tools.get_store_info(store_id))


async def store_stats_resource(store_id: int) -> str:
    return _json(await tools.get_store_stats(store_id))


async def store_inventory_resource(store_id: int) -> str:
    return _json(await tools.get_store_inventory(store_id))


async def store_events_resource(store_id: int) -> str:
    return _json(await tools.get_stock_events(store_id, limit=50))


async def store_devices_resource(store_id: int) -> str:
    return _json(await tools.get_devices(store_id))


async def store_topology_resource(store_id: int) -> str:
    return _json(await tools.get_store_topology(store_id))
