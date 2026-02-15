"""MCP Server for PaperWeight AI inventory monitoring.

Creates a FastMCP instance with tools and resources for querying
stores, inventory, stock events, devices, and statistics.
"""

import logging
from typing import Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import tools, resources

logger = logging.getLogger("api.mcp.server")


def create_mcp_server() -> FastMCP:
    """Create and configure the MCP server with all tools and resources."""

    mcp = FastMCP(
        "PaperWeight AI",
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[
                "localhost", "127.0.0.1", "[::1]",
                "localhost:*", "127.0.0.1:*", "[::1]:*",
            ],
            allowed_origins=[
                "http://127.0.0.1:*",
                "http://localhost:*",
                "http://[::1]:*",
                "https://127.0.0.1:*",
                "https://localhost:*",
                "https://[::1]:*",
            ],
        ),
    )

    # ===================================================================
    # TOOLS — Read-only queries
    # ===================================================================

    @mcp.tool(
        description="List all stores with their names, locations, working modes, "
        "and display settings."
    )
    async def list_stores() -> list[dict]:
        return await tools.list_stores()

    @mcp.tool(
        description="Get detailed information about a specific store including "
        "thresholds, working mode, and display configuration."
    )
    async def get_store_info(store_id: int) -> dict:
        return await tools.get_store_info(store_id)

    @mcp.tool(
        description="Get current inventory status for all products in a store. "
        "Shows stock levels, states (OUT_OF_STOCK, LOW, NORMAL, FULL), "
        "and product locations. Optionally filter by status."
    )
    async def get_store_inventory(
        store_id: int, status_filter: Optional[str] = None
    ) -> list[dict]:
        return await tools.get_store_inventory(store_id, status_filter)

    @mcp.tool(
        description="Get dashboard statistics for a store: stock state distribution "
        "(counts by state), anomaly count, average restock period."
    )
    async def get_store_stats(store_id: int) -> dict:
        return await tools.get_store_stats(store_id)

    @mcp.tool(
        description="Get stock events (RESTOCK, THEFT, LEVEL_UPDATE, STOCK_CRITICAL, "
        "STOCK_RECOVERY) with optional filters by shelf, product, event type, "
        "or date range. Dates should be ISO format (YYYY-MM-DD)."
    )
    async def get_stock_events(
        store_id: int,
        shelf_id: Optional[int] = None,
        product_id: Optional[int] = None,
        event_type: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        return await tools.get_stock_events(
            store_id, shelf_id, product_id, event_type, start_date, end_date, limit
        )

    @mcp.tool(
        description="List all products in a store with name, SKU, barcode, "
        "description, weight, and color."
    )
    async def get_products(store_id: int) -> list[dict]:
        return await tools.get_products(store_id)

    @mcp.tool(
        description="List all shelves in a store with bay assignment, device ID, "
        "sensor count, and depth."
    )
    async def get_shelves(store_id: int) -> list[dict]:
        return await tools.get_shelves(store_id)

    @mcp.tool(
        description="List all IoT devices for a store with their status "
        "(online/offline), health status, MQTT connectivity, and InfluxDB reporting."
    )
    async def get_devices(store_id: int) -> list[dict]:
        return await tools.get_devices(store_id)

    @mcp.tool(
        description="Get detailed health information for a specific IoT device "
        "including connectivity status, last seen timestamp, and health message."
    )
    async def get_device_health(device_id: int) -> dict:
        return await tools.get_device_health(device_id)

    @mcp.tool(
        description="Get the full store topology hierarchy: aisles, bays, and shelves."
    )
    async def get_store_topology(store_id: int) -> dict:
        return await tools.get_store_topology(store_id)

    @mcp.tool(
        description="Get historical stock level changes for a specific shelf, "
        "useful for analysing trends over time."
    )
    async def get_stock_history(shelf_id: int, limit: int = 100) -> list[dict]:
        return await tools.get_stock_history(shelf_id, limit)

    # ===================================================================
    # TOOLS — Actions
    # ===================================================================

    @mcp.tool(
        description="Override the stock level for a product on a shelf. "
        "action must be FULL (set to max), EMPTY (set to 0), or MANUAL "
        "(set to a specific quantity — requires the quantity parameter)."
    )
    async def override_stock(
        shelf_id: int,
        product_id: int,
        action: str,
        quantity: Optional[int] = None,
    ) -> dict:
        return await tools.override_stock(shelf_id, product_id, action, quantity)

    @mcp.tool(
        description="Trigger a manual metadata sync from the Retail service to the "
        "Stock service. Use this when shelf or product configurations have changed."
    )
    async def trigger_metadata_sync() -> dict:
        return await tools.trigger_metadata_sync()

    # ===================================================================
    # RESOURCES
    # ===================================================================

    @mcp.resource("paperweight://stores", description="All stores")
    async def stores_resource() -> str:
        return await resources.stores_resource()

    @mcp.resource(
        "paperweight://stores/{store_id}",
        description="Store details including configuration and thresholds",
    )
    async def store_detail_resource(store_id: int) -> str:
        return await resources.store_detail_resource(store_id)

    @mcp.resource(
        "paperweight://stores/{store_id}/stats",
        description="Dashboard statistics for a store",
    )
    async def store_stats_resource(store_id: int) -> str:
        return await resources.store_stats_resource(store_id)

    @mcp.resource(
        "paperweight://stores/{store_id}/inventory",
        description="Full inventory status for a store",
    )
    async def store_inventory_resource(store_id: int) -> str:
        return await resources.store_inventory_resource(store_id)

    @mcp.resource(
        "paperweight://stores/{store_id}/events",
        description="Recent stock events for a store (last 50)",
    )
    async def store_events_resource(store_id: int) -> str:
        return await resources.store_events_resource(store_id)

    @mcp.resource(
        "paperweight://stores/{store_id}/devices",
        description="IoT device list and status for a store",
    )
    async def store_devices_resource(store_id: int) -> str:
        return await resources.store_devices_resource(store_id)

    @mcp.resource(
        "paperweight://stores/{store_id}/topology",
        description="Store topology: aisles, bays, and shelves hierarchy",
    )
    async def store_topology_resource(store_id: int) -> str:
        return await resources.store_topology_resource(store_id)

    return mcp
