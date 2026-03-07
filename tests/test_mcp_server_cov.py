#!/usr/bin/env python3
"""
Coverage tests for mcp_server/server.py.

Verifies that create_mcp_server() creates a FastMCP instance with all
expected tools and resources registered, and that each wrapper delegates
to the corresponding function in tools.py / resources.py.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock


# ---------------------------------------------------------------------------
# Server creation and registration
# ---------------------------------------------------------------------------

class TestCreateMcpServer:

    def test_returns_fastmcp_instance(self):
        from mcp_server.server import create_mcp_server
        from mcp.server.fastmcp import FastMCP

        mcp = create_mcp_server()
        assert isinstance(mcp, FastMCP)

    def test_server_name(self):
        from mcp_server.server import create_mcp_server

        mcp = create_mcp_server()
        assert mcp.name == "PaperWeight AI"

    def test_all_tools_registered(self):
        from mcp_server.server import create_mcp_server

        mcp = create_mcp_server()

        expected_tools = [
            "list_stores",
            "get_store_info",
            "get_store_inventory",
            "get_store_stats",
            "get_stock_events",
            "get_products",
            "get_shelves",
            "get_devices",
            "get_device_health",
            "get_store_topology",
            "get_stock_history",
            "override_stock",
            "trigger_metadata_sync",
        ]

        # FastMCP stores tools internally; we check via _tool_manager
        registered = set(mcp._tool_manager._tools.keys())
        for tool_name in expected_tools:
            assert tool_name in registered, f"Tool '{tool_name}' not registered"

        assert len(registered) == len(expected_tools)

    def test_all_resources_registered(self):
        from mcp_server.server import create_mcp_server

        mcp = create_mcp_server()

        expected_uris = [
            "paperweight://stores",
            "paperweight://stores/{store_id}",
            "paperweight://stores/{store_id}/stats",
            "paperweight://stores/{store_id}/inventory",
            "paperweight://stores/{store_id}/events",
            "paperweight://stores/{store_id}/devices",
            "paperweight://stores/{store_id}/topology",
        ]

        registered = set(mcp._resource_manager._templates.keys())
        # Static resources (no template params) are in _resources
        registered |= set(mcp._resource_manager._resources.keys())

        for uri in expected_uris:
            assert uri in registered, f"Resource '{uri}' not registered"


# ---------------------------------------------------------------------------
# Tool delegation — each registered tool calls the right tools.py function
# ---------------------------------------------------------------------------

class TestToolDelegation:

    @pytest.mark.asyncio
    async def test_list_stores_delegates(self):
        with patch("mcp_server.server.tools.list_stores", new_callable=AsyncMock) as mock:
            mock.return_value = [{"id": 1}]
            from mcp_server.server import create_mcp_server
            mcp = create_mcp_server()
            tool_fn = mcp._tool_manager._tools["list_stores"].fn
            result = await tool_fn()
        mock.assert_awaited_once()
        assert result == [{"id": 1}]

    @pytest.mark.asyncio
    async def test_get_store_info_delegates(self):
        with patch("mcp_server.server.tools.get_store_info", new_callable=AsyncMock) as mock:
            mock.return_value = {"id": 1}
            from mcp_server.server import create_mcp_server
            mcp = create_mcp_server()
            tool_fn = mcp._tool_manager._tools["get_store_info"].fn
            result = await tool_fn(store_id=1)
        mock.assert_awaited_once_with(1)

    @pytest.mark.asyncio
    async def test_get_store_inventory_delegates(self):
        with patch("mcp_server.server.tools.get_store_inventory", new_callable=AsyncMock) as mock:
            mock.return_value = []
            from mcp_server.server import create_mcp_server
            mcp = create_mcp_server()
            tool_fn = mcp._tool_manager._tools["get_store_inventory"].fn
            result = await tool_fn(store_id=1, status_filter="LOW")
        mock.assert_awaited_once_with(1, "LOW")

    @pytest.mark.asyncio
    async def test_get_store_stats_delegates(self):
        with patch("mcp_server.server.tools.get_store_stats", new_callable=AsyncMock) as mock:
            mock.return_value = {"counts": {}}
            from mcp_server.server import create_mcp_server
            mcp = create_mcp_server()
            tool_fn = mcp._tool_manager._tools["get_store_stats"].fn
            await tool_fn(store_id=1)
        mock.assert_awaited_once_with(1)

    @pytest.mark.asyncio
    async def test_get_stock_events_delegates(self):
        with patch("mcp_server.server.tools.get_stock_events", new_callable=AsyncMock) as mock:
            mock.return_value = []
            from mcp_server.server import create_mcp_server
            mcp = create_mcp_server()
            tool_fn = mcp._tool_manager._tools["get_stock_events"].fn
            await tool_fn(
                store_id=1, shelf_id=2, product_id=3,
                event_type="THEFT", start_date="2026-01-01",
                end_date="2026-01-31", limit=10,
            )
        mock.assert_awaited_once_with(1, 2, 3, "THEFT", "2026-01-01", "2026-01-31", 10)

    @pytest.mark.asyncio
    async def test_get_products_delegates(self):
        with patch("mcp_server.server.tools.get_products", new_callable=AsyncMock) as mock:
            mock.return_value = []
            from mcp_server.server import create_mcp_server
            mcp = create_mcp_server()
            tool_fn = mcp._tool_manager._tools["get_products"].fn
            await tool_fn(store_id=1)
        mock.assert_awaited_once_with(1)

    @pytest.mark.asyncio
    async def test_get_shelves_delegates(self):
        with patch("mcp_server.server.tools.get_shelves", new_callable=AsyncMock) as mock:
            mock.return_value = []
            from mcp_server.server import create_mcp_server
            mcp = create_mcp_server()
            tool_fn = mcp._tool_manager._tools["get_shelves"].fn
            await tool_fn(store_id=1)
        mock.assert_awaited_once_with(1)

    @pytest.mark.asyncio
    async def test_get_devices_delegates(self):
        with patch("mcp_server.server.tools.get_devices", new_callable=AsyncMock) as mock:
            mock.return_value = []
            from mcp_server.server import create_mcp_server
            mcp = create_mcp_server()
            tool_fn = mcp._tool_manager._tools["get_devices"].fn
            await tool_fn(store_id=1)
        mock.assert_awaited_once_with(1)

    @pytest.mark.asyncio
    async def test_get_device_health_delegates(self):
        with patch("mcp_server.server.tools.get_device_health", new_callable=AsyncMock) as mock:
            mock.return_value = {}
            from mcp_server.server import create_mcp_server
            mcp = create_mcp_server()
            tool_fn = mcp._tool_manager._tools["get_device_health"].fn
            await tool_fn(device_id=5)
        mock.assert_awaited_once_with(5)

    @pytest.mark.asyncio
    async def test_get_store_topology_delegates(self):
        with patch("mcp_server.server.tools.get_store_topology", new_callable=AsyncMock) as mock:
            mock.return_value = {}
            from mcp_server.server import create_mcp_server
            mcp = create_mcp_server()
            tool_fn = mcp._tool_manager._tools["get_store_topology"].fn
            await tool_fn(store_id=1)
        mock.assert_awaited_once_with(1)

    @pytest.mark.asyncio
    async def test_get_stock_history_delegates(self):
        with patch("mcp_server.server.tools.get_stock_history", new_callable=AsyncMock) as mock:
            mock.return_value = []
            from mcp_server.server import create_mcp_server
            mcp = create_mcp_server()
            tool_fn = mcp._tool_manager._tools["get_stock_history"].fn
            await tool_fn(shelf_id=1, limit=50)
        mock.assert_awaited_once_with(1, 50)

    @pytest.mark.asyncio
    async def test_override_stock_delegates(self):
        with patch("mcp_server.server.tools.override_stock", new_callable=AsyncMock) as mock:
            mock.return_value = {"ok": True}
            from mcp_server.server import create_mcp_server
            mcp = create_mcp_server()
            tool_fn = mcp._tool_manager._tools["override_stock"].fn
            await tool_fn(shelf_id=1, product_id=2, action="FULL", quantity=None)
        mock.assert_awaited_once_with(1, 2, "FULL", None)

    @pytest.mark.asyncio
    async def test_trigger_metadata_sync_delegates(self):
        with patch("mcp_server.server.tools.trigger_metadata_sync", new_callable=AsyncMock) as mock:
            mock.return_value = {"success": True}
            from mcp_server.server import create_mcp_server
            mcp = create_mcp_server()
            tool_fn = mcp._tool_manager._tools["trigger_metadata_sync"].fn
            await tool_fn()
        mock.assert_awaited_once()


# ---------------------------------------------------------------------------
# Resource delegation
# ---------------------------------------------------------------------------

class TestResourceDelegation:

    @pytest.mark.asyncio
    async def test_stores_resource_delegates(self):
        with patch("mcp_server.server.resources.stores_resource", new_callable=AsyncMock) as mock:
            mock.return_value = "[]"
            from mcp_server.server import create_mcp_server
            mcp = create_mcp_server()
            # Static resource
            res_fn = mcp._resource_manager._resources["paperweight://stores"].fn
            result = await res_fn()
        mock.assert_awaited_once()
        assert result == "[]"

    @pytest.mark.asyncio
    async def test_store_detail_resource_delegates(self):
        with patch("mcp_server.server.resources.store_detail_resource", new_callable=AsyncMock) as mock:
            mock.return_value = "{}"
            from mcp_server.server import create_mcp_server
            mcp = create_mcp_server()
            res_fn = mcp._resource_manager._templates["paperweight://stores/{store_id}"].fn
            result = await res_fn(store_id=1)
        mock.assert_awaited_once_with(1)

    @pytest.mark.asyncio
    async def test_store_stats_resource_delegates(self):
        with patch("mcp_server.server.resources.store_stats_resource", new_callable=AsyncMock) as mock:
            mock.return_value = "{}"
            from mcp_server.server import create_mcp_server
            mcp = create_mcp_server()
            res_fn = mcp._resource_manager._templates["paperweight://stores/{store_id}/stats"].fn
            await res_fn(store_id=1)
        mock.assert_awaited_once_with(1)

    @pytest.mark.asyncio
    async def test_store_inventory_resource_delegates(self):
        with patch("mcp_server.server.resources.store_inventory_resource", new_callable=AsyncMock) as mock:
            mock.return_value = "[]"
            from mcp_server.server import create_mcp_server
            mcp = create_mcp_server()
            res_fn = mcp._resource_manager._templates["paperweight://stores/{store_id}/inventory"].fn
            await res_fn(store_id=1)
        mock.assert_awaited_once_with(1)

    @pytest.mark.asyncio
    async def test_store_events_resource_delegates(self):
        with patch("mcp_server.server.resources.store_events_resource", new_callable=AsyncMock) as mock:
            mock.return_value = "[]"
            from mcp_server.server import create_mcp_server
            mcp = create_mcp_server()
            res_fn = mcp._resource_manager._templates["paperweight://stores/{store_id}/events"].fn
            await res_fn(store_id=1)
        mock.assert_awaited_once_with(1)

    @pytest.mark.asyncio
    async def test_store_devices_resource_delegates(self):
        with patch("mcp_server.server.resources.store_devices_resource", new_callable=AsyncMock) as mock:
            mock.return_value = "[]"
            from mcp_server.server import create_mcp_server
            mcp = create_mcp_server()
            res_fn = mcp._resource_manager._templates["paperweight://stores/{store_id}/devices"].fn
            await res_fn(store_id=1)
        mock.assert_awaited_once_with(1)

    @pytest.mark.asyncio
    async def test_store_topology_resource_delegates(self):
        with patch("mcp_server.server.resources.store_topology_resource", new_callable=AsyncMock) as mock:
            mock.return_value = "{}"
            from mcp_server.server import create_mcp_server
            mcp = create_mcp_server()
            res_fn = mcp._resource_manager._templates["paperweight://stores/{store_id}/topology"].fn
            await res_fn(store_id=1)
        mock.assert_awaited_once_with(1)
