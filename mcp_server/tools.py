"""MCP Tool handler implementations.

Each function is a plain async function that uses the shared `deps` singleton.
Follows the same error-handling pattern as services/aggregator.py.
"""

import asyncio
import logging
from typing import Any, Optional

from .dependencies import deps

logger = logging.getLogger("api.mcp.tools")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _get(url: str, default: Any = None, timeout: float = 10.0) -> Any:
    """GET JSON from a backend service, returning *default* on any error."""
    try:
        resp = await deps.http_client.get(url, headers=deps.headers, timeout=timeout)
        if resp.status_code != 200:
            logger.warning(f"MCP GET {url} → {resp.status_code}")
            return default
        return resp.json()
    except Exception as e:
        logger.error(f"MCP GET error {url}: {e}")
        return default


async def _post(url: str, body: dict, default: Any = None, timeout: float = 10.0) -> Any:
    """POST JSON to a backend service, returning *default* on any error."""
    try:
        resp = await deps.http_client.post(url, json=body, headers=deps.headers, timeout=timeout)
        if resp.status_code not in (200, 201):
            logger.warning(f"MCP POST {url} → {resp.status_code}")
            return default
        return resp.json()
    except Exception as e:
        logger.error(f"MCP POST error {url}: {e}")
        return default


# ---------------------------------------------------------------------------
# Read-only query tools
# ---------------------------------------------------------------------------

async def list_stores() -> list[dict]:
    """List all stores."""
    url = f"{deps.retail_service_url}/retail/stores"
    return await _get(url, default=[])


async def get_store_info(store_id: int) -> dict:
    """Get detailed store information."""
    url = f"{deps.retail_service_url}/retail/stores/{store_id}"
    return await _get(url, default={})


async def get_store_inventory(store_id: int, status_filter: Optional[str] = None) -> list[dict]:
    """Get inventory / restock needs for a store."""
    url = f"{deps.stock_service_url}/restock-needs?storeId={store_id}"
    if status_filter:
        url += f"&status={status_filter}"
    return await _get(url, default=[])


async def get_store_stats(store_id: int) -> dict:
    """Get dashboard statistics for a store."""
    url = f"{deps.stock_service_url}/stats?storeId={store_id}"
    return await _get(url, default={"counts": {}, "anomalies_detected": 0})


async def get_stock_events(
    store_id: int,
    shelf_id: Optional[int] = None,
    product_id: Optional[int] = None,
    event_type: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """Get stock events with optional filters."""
    params = [f"storeId={store_id}", f"limit={limit}"]
    if shelf_id is not None:
        params.append(f"shelfId={shelf_id}")
    if product_id is not None:
        params.append(f"productId={product_id}")
    if event_type:
        params.append(f"eventType={event_type}")
    if start_date:
        params.append(f"startDate={start_date}")
    if end_date:
        params.append(f"endDate={end_date}")
    url = f"{deps.stock_service_url}/events?{'&'.join(params)}"
    return await _get(url, default=[])


async def get_products(store_id: int) -> list[dict]:
    """List all products in a store."""
    url = f"{deps.retail_service_url}/retail/products?store_id={store_id}"
    return await _get(url, default=[])


async def get_shelves(store_id: int) -> list[dict]:
    """List all shelves in a store."""
    url = f"{deps.retail_service_url}/retail/shelves?store_id={store_id}"
    return await _get(url, default=[])


async def get_devices(store_id: int) -> list[dict]:
    """List all IoT devices for a store with status and health info."""
    url = f"{deps.iot_service_url}/sensors/devices?store_id={store_id}"
    return await _get(url, default=[])


async def get_device_health(device_id: int) -> dict:
    """Get detailed health info for a specific device."""
    url = f"{deps.iot_service_url}/sensors/devices/{device_id}/health"
    return await _get(url, default={})


async def get_store_topology(store_id: int) -> dict:
    """Get the full store hierarchy: aisles → bays → shelves."""
    aisles, bays, shelves = await asyncio.gather(
        _get(f"{deps.retail_service_url}/retail/aisles?store_id={store_id}", default=[]),
        _get(f"{deps.retail_service_url}/retail/bays?store_id={store_id}", default=[]),
        _get(f"{deps.retail_service_url}/retail/shelves?store_id={store_id}", default=[]),
    )
    return {"aisles": aisles, "bays": bays, "shelves": shelves}


async def get_stock_history(shelf_id: int, limit: int = 100) -> list[dict]:
    """Get historical stock changes for a shelf."""
    url = f"{deps.stock_service_url}/history?shelfId={shelf_id}&limit={limit}"
    return await _get(url, default=[])


# ---------------------------------------------------------------------------
# Action tools
# ---------------------------------------------------------------------------

async def override_stock(
    shelf_id: int, product_id: int, action: str, quantity: Optional[int] = None
) -> dict:
    """Override stock level for a product on a shelf."""
    url = f"{deps.stock_service_url}/override"
    body: dict[str, Any] = {
        "shelf_id": shelf_id,
        "product_id": product_id,
        "action": action,
    }
    if quantity is not None:
        body["quantity"] = quantity
    return await _post(url, body, default={"error": "Override failed"})


async def trigger_metadata_sync() -> dict:
    """Trigger manual metadata sync from Retail → Stock."""
    url = f"{deps.stock_service_url}/debug/sync-metadata"
    return await _post(url, {}, default={"error": "Sync failed"})
