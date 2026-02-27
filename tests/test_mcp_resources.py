"""Tests for MCP resource handlers.

Verifies that each resource returns valid JSON strings
with the expected data structure.
"""

import json
import pytest
import httpx

from mcp_server.dependencies import deps
from mcp_server import resources


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _transport(routes: dict[str, tuple[int, dict | list]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for pattern, (status, body) in routes.items():
            if pattern in url:
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={"detail": "Not Found"})
    return httpx.MockTransport(handler)


def _setup(transport):
    deps.auth_token = "test-token"
    deps.ws_manager = None
    deps.stock_service_url = "http://stock"
    deps.retail_service_url = "http://retail"
    deps.iot_service_url = "http://iot"
    return httpx.AsyncClient(transport=transport)


# ---------------------------------------------------------------------------
# stores_resource
# ---------------------------------------------------------------------------

class TestStoresResource:
    @pytest.mark.asyncio
    async def test_returns_json_string(self):
        stores = [{"id": 1, "name": "Store 1"}]
        transport = _transport({"/retail/stores": (200, stores)})
        async with _setup(transport) as client:
            deps.http_client = client
            result = await resources.stores_resource()
        parsed = json.loads(result)
        assert len(parsed) == 1
        assert parsed[0]["name"] == "Store 1"


# ---------------------------------------------------------------------------
# store_detail_resource
# ---------------------------------------------------------------------------

class TestStoreDetailResource:
    @pytest.mark.asyncio
    async def test_returns_json_string(self):
        store = {"id": 1, "name": "Main", "workingMode": "LEVEL"}
        transport = _transport({"/retail/stores/1": (200, store)})
        async with _setup(transport) as client:
            deps.http_client = client
            result = await resources.store_detail_resource(1)
        parsed = json.loads(result)
        assert parsed["workingMode"] == "LEVEL"


# ---------------------------------------------------------------------------
# store_stats_resource
# ---------------------------------------------------------------------------

class TestStoreStatsResource:
    @pytest.mark.asyncio
    async def test_returns_json_string(self):
        stats = {"counts": {"FULL": 3}, "anomalies_detected": 1}
        transport = _transport({"/stats": (200, stats)})
        async with _setup(transport) as client:
            deps.http_client = client
            result = await resources.store_stats_resource(1)
        parsed = json.loads(result)
        assert parsed["counts"]["FULL"] == 3


# ---------------------------------------------------------------------------
# store_inventory_resource
# ---------------------------------------------------------------------------

class TestStoreInventoryResource:
    @pytest.mark.asyncio
    async def test_returns_json_string(self):
        inventory = [{"productId": 1, "status": "LOW"}]
        transport = _transport({"/stock": (200, inventory)})
        async with _setup(transport) as client:
            deps.http_client = client
            result = await resources.store_inventory_resource(1)
        parsed = json.loads(result)
        assert len(parsed) == 1


# ---------------------------------------------------------------------------
# store_events_resource
# ---------------------------------------------------------------------------

class TestStoreEventsResource:
    @pytest.mark.asyncio
    async def test_returns_json_string(self):
        events = [{"eventType": "THEFT", "unitChange": -3}]
        transport = _transport({"/events": (200, events)})
        async with _setup(transport) as client:
            deps.http_client = client
            result = await resources.store_events_resource(1)
        parsed = json.loads(result)
        assert parsed[0]["eventType"] == "THEFT"


# ---------------------------------------------------------------------------
# store_devices_resource
# ---------------------------------------------------------------------------

class TestStoreDevicesResource:
    @pytest.mark.asyncio
    async def test_returns_json_string(self):
        devices = [{"id": 1, "status": "online"}]
        transport = _transport({"/sensors/devices": (200, devices)})
        async with _setup(transport) as client:
            deps.http_client = client
            result = await resources.store_devices_resource(1)
        parsed = json.loads(result)
        assert parsed[0]["status"] == "online"


# ---------------------------------------------------------------------------
# store_topology_resource
# ---------------------------------------------------------------------------

class TestStoreTopologyResource:
    @pytest.mark.asyncio
    async def test_returns_json_string(self):
        transport = _transport({
            "/retail/aisles": (200, [{"id": 1}]),
            "/retail/bays": (200, [{"id": 1}]),
            "/retail/shelves": (200, [{"id": 1}, {"id": 2}]),
        })
        async with _setup(transport) as client:
            deps.http_client = client
            result = await resources.store_topology_resource(1)
        parsed = json.loads(result)
        assert len(parsed["aisles"]) == 1
        assert len(parsed["shelves"]) == 2

    @pytest.mark.asyncio
    async def test_empty_on_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down", request=request)
        transport = httpx.MockTransport(handler)
        async with _setup(transport) as client:
            deps.http_client = client
            result = await resources.store_topology_resource(1)
        parsed = json.loads(result)
        assert parsed == {"aisles": [], "bays": [], "shelves": []}
