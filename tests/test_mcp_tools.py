"""Tests for MCP tool handlers.

Uses httpx.MockTransport following the existing test_data_aggregator.py pattern.
Tests each tool with success, HTTP error, and connection error scenarios.
"""

import pytest
import httpx

from mcp_server.dependencies import deps, MCPDependencies
from mcp_server import tools


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _transport(routes: dict[str, tuple[int, dict | list]]) -> httpx.MockTransport:
    """Mock transport returning predefined responses for URL substrings."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for pattern, (status, body) in routes.items():
            if pattern in url:
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={"detail": "Not Found"})
    return httpx.MockTransport(handler)


def _error_transport(exc_class=httpx.ConnectError) -> httpx.MockTransport:
    """Mock transport that raises an exception for all requests."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc_class("Simulated error", request=request)
    return httpx.MockTransport(handler)


def _setup_deps(transport, **overrides):
    """Configure the module-level deps singleton for testing."""
    defaults = {
        "auth_token": "test-token",
        "ws_manager": None,
        "stock_service_url": "http://stock",
        "retail_service_url": "http://retail",
        "iot_service_url": "http://iot",
        "internal_api_secret": "",
    }
    defaults.update(overrides)
    deps.auth_token = defaults["auth_token"]
    deps.ws_manager = defaults["ws_manager"]
    deps.stock_service_url = defaults["stock_service_url"]
    deps.retail_service_url = defaults["retail_service_url"]
    deps.iot_service_url = defaults["iot_service_url"]
    deps.internal_api_secret = defaults["internal_api_secret"]
    return httpx.AsyncClient(transport=transport)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def stores_data():
    return [
        {"id": 1, "name": "Main Store", "city": "London", "workingMode": "LEVEL"},
        {"id": 2, "name": "Branch", "city": "Paris", "workingMode": "EVENT"},
    ]


@pytest.fixture
def store_data():
    return {"id": 1, "name": "Main Store", "workingMode": "LEVEL", "emptyStockThreshold": 0.05}


@pytest.fixture
def inventory_data():
    return [
        {"productId": 1, "productName": "Doritos", "status": "LOW", "stockPercentage": 0.15},
        {"productId": 2, "productName": "Lays", "status": "NORMAL", "stockPercentage": 0.75},
    ]


@pytest.fixture
def stats_data():
    return {
        "counts": {"FULL": 2, "NORMAL": 5, "LOW": 1, "OUT_OF_STOCK": 0},
        "anomalies_detected": 3,
        "avg_restock_period": "45m",
    }


@pytest.fixture
def events_data():
    return [
        {"storeId": 1, "shelfId": 1, "productId": 1, "eventType": "RESTOCK", "unitChange": 5},
        {"storeId": 1, "shelfId": 2, "productId": 2, "eventType": "THEFT", "unitChange": -2},
    ]


@pytest.fixture
def products_data():
    return [
        {"id": 1, "name": "Doritos", "sku": "DOR-001", "barcode": "1234567890"},
        {"id": 2, "name": "Lays", "sku": "LAY-001", "barcode": "0987654321"},
    ]


@pytest.fixture
def shelves_data():
    return [
        {"id": 1, "name": "Shelf 1", "bayId": 1, "numberOfSensors": 8},
        {"id": 2, "name": "Shelf 2", "bayId": 1, "numberOfSensors": 12},
    ]


@pytest.fixture
def devices_data():
    return [
        {"id": 1, "macAddress": "AA:BB:CC:DD:EE:01", "status": "online", "healthStatus": "healthy"},
        {"id": 2, "macAddress": "AA:BB:CC:DD:EE:02", "status": "offline", "healthStatus": "error"},
    ]


@pytest.fixture
def device_health_data():
    return {"id": 1, "status": "online", "healthStatus": "healthy", "mqttConnected": "yes"}


@pytest.fixture
def aisles_data():
    return [{"id": 1, "name": "Aisle 1", "storeId": 1}]


@pytest.fixture
def bays_data():
    return [{"id": 1, "name": "Bay A", "aisleId": 1}]


@pytest.fixture
def history_data():
    return [
        {"shelfId": 1, "productId": 1, "stockAfter": 10, "timestamp": "2026-02-15T10:00:00Z"},
    ]


# ---------------------------------------------------------------------------
# list_stores
# ---------------------------------------------------------------------------

class TestListStores:
    @pytest.mark.asyncio
    async def test_success(self, stores_data):
        transport = _transport({"/retail/stores": (200, stores_data)})
        async with _setup_deps(transport) as client:
            deps.http_client = client
            result = await tools.list_stores()
        assert len(result) == 2
        assert result[0]["name"] == "Main Store"

    @pytest.mark.asyncio
    async def test_service_down(self):
        async with _setup_deps(_error_transport()) as client:
            deps.http_client = client
            result = await tools.list_stores()
        assert result == []

    @pytest.mark.asyncio
    async def test_http_500(self):
        transport = _transport({"/retail/stores": (500, {"error": "Internal"})})
        async with _setup_deps(transport) as client:
            deps.http_client = client
            result = await tools.list_stores()
        assert result == []


# ---------------------------------------------------------------------------
# get_store_info
# ---------------------------------------------------------------------------

class TestGetStoreInfo:
    @pytest.mark.asyncio
    async def test_success(self, store_data):
        transport = _transport({"/retail/stores/1": (200, store_data)})
        async with _setup_deps(transport) as client:
            deps.http_client = client
            result = await tools.get_store_info(1)
        assert result["id"] == 1
        assert result["workingMode"] == "LEVEL"

    @pytest.mark.asyncio
    async def test_not_found(self):
        transport = _transport({})  # 404 for everything
        async with _setup_deps(transport) as client:
            deps.http_client = client
            result = await tools.get_store_info(999)
        assert result == {}


# ---------------------------------------------------------------------------
# get_store_inventory
# ---------------------------------------------------------------------------

class TestGetStoreInventory:
    @pytest.mark.asyncio
    async def test_success(self, inventory_data):
        transport = _transport({"/restock-needs": (200, inventory_data)})
        async with _setup_deps(transport) as client:
            deps.http_client = client
            result = await tools.get_store_inventory(1)
        assert len(result) == 2
        assert result[0]["status"] == "LOW"

    @pytest.mark.asyncio
    async def test_with_status_filter(self, inventory_data):
        captured_urls = []
        def handler(request: httpx.Request) -> httpx.Response:
            captured_urls.append(str(request.url))
            return httpx.Response(200, json=inventory_data)
        transport = httpx.MockTransport(handler)
        async with _setup_deps(transport) as client:
            deps.http_client = client
            await tools.get_store_inventory(1, status_filter="LOW,OUT_OF_STOCK")
        assert "status=LOW,OUT_OF_STOCK" in captured_urls[0]

    @pytest.mark.asyncio
    async def test_service_down(self):
        async with _setup_deps(_error_transport()) as client:
            deps.http_client = client
            result = await tools.get_store_inventory(1)
        assert result == []


# ---------------------------------------------------------------------------
# get_store_stats
# ---------------------------------------------------------------------------

class TestGetStoreStats:
    @pytest.mark.asyncio
    async def test_success(self, stats_data):
        transport = _transport({"/stats": (200, stats_data)})
        async with _setup_deps(transport) as client:
            deps.http_client = client
            result = await tools.get_store_stats(1)
        assert result["counts"]["FULL"] == 2
        assert result["anomalies_detected"] == 3

    @pytest.mark.asyncio
    async def test_service_down(self):
        async with _setup_deps(_error_transport()) as client:
            deps.http_client = client
            result = await tools.get_store_stats(1)
        assert "counts" in result


# ---------------------------------------------------------------------------
# get_stock_events
# ---------------------------------------------------------------------------

class TestGetStockEvents:
    @pytest.mark.asyncio
    async def test_success(self, events_data):
        transport = _transport({"/events": (200, events_data)})
        async with _setup_deps(transport) as client:
            deps.http_client = client
            result = await tools.get_stock_events(1)
        assert len(result) == 2
        assert result[0]["eventType"] == "RESTOCK"

    @pytest.mark.asyncio
    async def test_with_filters(self, events_data):
        captured_urls = []
        def handler(request: httpx.Request) -> httpx.Response:
            captured_urls.append(str(request.url))
            return httpx.Response(200, json=events_data)
        transport = httpx.MockTransport(handler)
        async with _setup_deps(transport) as client:
            deps.http_client = client
            await tools.get_stock_events(
                store_id=1, shelf_id=5, product_id=10,
                event_type="THEFT", limit=25
            )
        url = captured_urls[0]
        assert "storeId=1" in url
        assert "shelfId=5" in url
        assert "productId=10" in url
        assert "eventType=THEFT" in url
        assert "limit=25" in url

    @pytest.mark.asyncio
    async def test_service_down(self):
        async with _setup_deps(_error_transport()) as client:
            deps.http_client = client
            result = await tools.get_stock_events(1)
        assert result == []


# ---------------------------------------------------------------------------
# get_products
# ---------------------------------------------------------------------------

class TestGetProducts:
    @pytest.mark.asyncio
    async def test_success(self, products_data):
        transport = _transport({"/retail/products": (200, products_data)})
        async with _setup_deps(transport) as client:
            deps.http_client = client
            result = await tools.get_products(1)
        assert len(result) == 2
        assert result[0]["sku"] == "DOR-001"

    @pytest.mark.asyncio
    async def test_service_down(self):
        async with _setup_deps(_error_transport()) as client:
            deps.http_client = client
            result = await tools.get_products(1)
        assert result == []


# ---------------------------------------------------------------------------
# get_shelves
# ---------------------------------------------------------------------------

class TestGetShelves:
    @pytest.mark.asyncio
    async def test_success(self, shelves_data):
        transport = _transport({"/retail/shelves": (200, shelves_data)})
        async with _setup_deps(transport) as client:
            deps.http_client = client
            result = await tools.get_shelves(1)
        assert len(result) == 2
        assert result[0]["numberOfSensors"] == 8

    @pytest.mark.asyncio
    async def test_service_down(self):
        async with _setup_deps(_error_transport()) as client:
            deps.http_client = client
            result = await tools.get_shelves(1)
        assert result == []


# ---------------------------------------------------------------------------
# get_devices
# ---------------------------------------------------------------------------

class TestGetDevices:
    @pytest.mark.asyncio
    async def test_success(self, devices_data):
        transport = _transport({"/sensors/devices": (200, devices_data)})
        async with _setup_deps(transport) as client:
            deps.http_client = client
            result = await tools.get_devices(1)
        assert len(result) == 2
        assert result[0]["status"] == "online"

    @pytest.mark.asyncio
    async def test_service_down(self):
        async with _setup_deps(_error_transport()) as client:
            deps.http_client = client
            result = await tools.get_devices(1)
        assert result == []


# ---------------------------------------------------------------------------
# get_device_health
# ---------------------------------------------------------------------------

class TestGetDeviceHealth:
    @pytest.mark.asyncio
    async def test_success(self, device_health_data):
        transport = _transport({"/sensors/devices/1/health": (200, device_health_data)})
        async with _setup_deps(transport) as client:
            deps.http_client = client
            result = await tools.get_device_health(1)
        assert result["healthStatus"] == "healthy"

    @pytest.mark.asyncio
    async def test_not_found(self):
        transport = _transport({})
        async with _setup_deps(transport) as client:
            deps.http_client = client
            result = await tools.get_device_health(999)
        assert result == {}


# ---------------------------------------------------------------------------
# get_store_topology
# ---------------------------------------------------------------------------

class TestGetStoreTopology:
    @pytest.mark.asyncio
    async def test_success(self, aisles_data, bays_data, shelves_data):
        transport = _transport({
            "/retail/aisles": (200, aisles_data),
            "/retail/bays": (200, bays_data),
            "/retail/shelves": (200, shelves_data),
        })
        async with _setup_deps(transport) as client:
            deps.http_client = client
            result = await tools.get_store_topology(1)
        assert len(result["aisles"]) == 1
        assert len(result["bays"]) == 1
        assert len(result["shelves"]) == 2

    @pytest.mark.asyncio
    async def test_partial_failure(self, aisles_data):
        transport = _transport({
            "/retail/aisles": (200, aisles_data),
            # bays and shelves return 404
        })
        async with _setup_deps(transport) as client:
            deps.http_client = client
            result = await tools.get_store_topology(1)
        assert len(result["aisles"]) == 1
        assert result["bays"] == []
        assert result["shelves"] == []

    @pytest.mark.asyncio
    async def test_all_down(self):
        async with _setup_deps(_error_transport()) as client:
            deps.http_client = client
            result = await tools.get_store_topology(1)
        assert result == {"aisles": [], "bays": [], "shelves": []}


# ---------------------------------------------------------------------------
# get_stock_history
# ---------------------------------------------------------------------------

class TestGetStockHistory:
    @pytest.mark.asyncio
    async def test_success(self, history_data):
        transport = _transport({"/history": (200, history_data)})
        async with _setup_deps(transport) as client:
            deps.http_client = client
            result = await tools.get_stock_history(1)
        assert len(result) == 1
        assert result[0]["stockAfter"] == 10

    @pytest.mark.asyncio
    async def test_service_down(self):
        async with _setup_deps(_error_transport()) as client:
            deps.http_client = client
            result = await tools.get_stock_history(1)
        assert result == []


# ---------------------------------------------------------------------------
# override_stock
# ---------------------------------------------------------------------------

class TestOverrideStock:
    @pytest.mark.asyncio
    async def test_success(self):
        response_body = {"message": "Stock updated", "new_stock": 20, "state": "FULL"}
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            return httpx.Response(200, json=response_body)
        transport = httpx.MockTransport(handler)
        async with _setup_deps(transport) as client:
            deps.http_client = client
            result = await tools.override_stock(1, 1, "FULL")
        assert result["new_stock"] == 20

    @pytest.mark.asyncio
    async def test_with_quantity(self):
        captured_body = {}
        def handler(request: httpx.Request) -> httpx.Response:
            import json
            captured_body.update(json.loads(request.content))
            return httpx.Response(200, json={"message": "OK"})
        transport = httpx.MockTransport(handler)
        async with _setup_deps(transport) as client:
            deps.http_client = client
            await tools.override_stock(1, 2, "MANUAL", quantity=15)
        assert captured_body["action"] == "MANUAL"
        assert captured_body["quantity"] == 15

    @pytest.mark.asyncio
    async def test_service_down(self):
        async with _setup_deps(_error_transport()) as client:
            deps.http_client = client
            result = await tools.override_stock(1, 1, "FULL")
        assert "error" in result


# ---------------------------------------------------------------------------
# trigger_metadata_sync
# ---------------------------------------------------------------------------

class TestTriggerMetadataSync:
    @pytest.mark.asyncio
    async def test_success(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            return httpx.Response(200, json={"success": True, "message": "Synced"})
        transport = httpx.MockTransport(handler)
        async with _setup_deps(transport) as client:
            deps.http_client = client
            result = await tools.trigger_metadata_sync()
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_service_down(self):
        async with _setup_deps(_error_transport()) as client:
            deps.http_client = client
            result = await tools.trigger_metadata_sync()
        assert "error" in result


# ---------------------------------------------------------------------------
# Header propagation
# ---------------------------------------------------------------------------

class TestHeaders:
    @pytest.mark.asyncio
    async def test_bearer_token_sent(self):
        captured_headers = {}
        def handler(request: httpx.Request) -> httpx.Response:
            captured_headers.update(dict(request.headers))
            return httpx.Response(200, json=[])
        transport = httpx.MockTransport(handler)
        async with _setup_deps(transport, auth_token="my-jwt") as client:
            deps.http_client = client
            await tools.list_stores()
        assert captured_headers.get("authorization") == "Bearer my-jwt"

    @pytest.mark.asyncio
    async def test_internal_secret_sent(self):
        captured_headers = {}
        def handler(request: httpx.Request) -> httpx.Response:
            captured_headers.update(dict(request.headers))
            return httpx.Response(200, json=[])
        transport = httpx.MockTransport(handler)
        async with _setup_deps(transport, internal_api_secret="secret123") as client:
            deps.http_client = client
            await tools.list_stores()
        assert captured_headers.get("x-internal-secret") == "secret123"

    @pytest.mark.asyncio
    async def test_ws_manager_token_preferred(self):
        """When ws_manager has a refreshed token, it should be preferred."""
        captured_headers = {}
        def handler(request: httpx.Request) -> httpx.Response:
            captured_headers.update(dict(request.headers))
            return httpx.Response(200, json=[])
        transport = httpx.MockTransport(handler)

        class FakeWSManager:
            _service_token = "refreshed-token"

        async with _setup_deps(
            transport, auth_token="stale-token", ws_manager=FakeWSManager()
        ) as client:
            deps.http_client = client
            await tools.list_stores()
        assert captured_headers.get("authorization") == "Bearer refreshed-token"
