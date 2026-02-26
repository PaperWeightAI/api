#!/usr/bin/env python3
"""
Tests for DataAggregator.

Uses httpx.MockTransport for realistic HTTP testing without external services.
Verifies:
- Individual fetch methods (store, aisles, bays, shelves, products, devices, stats)
- Parallel aggregation (aggregate_initial_data)
- Error handling (timeouts, HTTP errors, malformed responses)
- Header propagation (Authorization)
- Inventory bulk fetch
"""

import json
import pytest
import httpx

from services.aggregator import DataAggregator


def _transport(routes: dict[str, tuple[int, dict | list]]) -> httpx.MockTransport:
    """
    Create a mock transport that returns predefined responses for URL patterns.

    routes: dict mapping URL substring → (status_code, json_body)
    """
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for pattern, (status, body) in routes.items():
            if pattern in url:
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={"detail": "Not Found"})

    return httpx.MockTransport(handler)


def _error_transport(exc_class=httpx.TimeoutException) -> httpx.MockTransport:
    """Create a transport that raises an exception for all requests."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc_class("Simulated timeout", request=request)
    return httpx.MockTransport(handler)


@pytest.fixture
def store_data():
    return {"id": 1, "name": "Main Store", "workingMode": "LEVEL"}


@pytest.fixture
def aisles_data():
    return [{"id": 1, "name": "Aisle 1", "storeId": 1}]


@pytest.fixture
def bays_data():
    return [{"id": 1, "name": "Bay A", "aisleId": 1}]


@pytest.fixture
def shelves_data():
    return [{"id": 1, "name": "Shelf 1", "bayId": 1}, {"id": 2, "name": "Shelf 2", "bayId": 1}]


@pytest.fixture
def products_data():
    return [
        {"id": 1, "name": "Doritos", "shelfId": 1},
        {"id": 2, "name": "Lays", "shelfId": 2},
    ]


@pytest.fixture
def devices_data():
    return {"total": 5, "online": 4, "offline": 1}


@pytest.fixture
def stats_data():
    return {
        "counts": {"FULL": 2, "NORMAL": 5, "LOW": 1},
        "anomalies_detected": 3,
        "avg_restock_period": "45m",
        "config": {"mode": "LEVEL"},
        "store_config": {},
    }


@pytest.fixture
def restock_data():
    return [
        {"productId": 1, "productName": "Doritos", "status": "LOW", "stockPercentage": 0.15},
    ]


class TestIndividualFetchers:
    """Test each fetch method independently."""

    @pytest.mark.asyncio
    async def test_fetch_store_info(self, store_data):
        transport = _transport({"/retail/stores/1": (200, store_data)})
        async with httpx.AsyncClient(transport=transport) as client:
            agg = DataAggregator(client, token="test-token")
            result = await agg.fetch_store_info(1)
        assert result["id"] == 1
        assert result["name"] == "Main Store"

    @pytest.mark.asyncio
    async def test_fetch_aisles(self, aisles_data):
        transport = _transport({"/retail/aisles": (200, aisles_data)})
        async with httpx.AsyncClient(transport=transport) as client:
            agg = DataAggregator(client)
            result = await agg.fetch_aisles(1)
        assert len(result) == 1
        assert result[0]["name"] == "Aisle 1"

    @pytest.mark.asyncio
    async def test_fetch_bays(self, bays_data):
        transport = _transport({"/retail/bays": (200, bays_data)})
        async with httpx.AsyncClient(transport=transport) as client:
            agg = DataAggregator(client)
            result = await agg.fetch_bays(1)
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_fetch_shelves(self, shelves_data):
        transport = _transport({"/retail/shelves": (200, shelves_data)})
        async with httpx.AsyncClient(transport=transport) as client:
            agg = DataAggregator(client)
            result = await agg.fetch_shelves(1)
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_fetch_products(self, products_data):
        transport = _transport({"/retail/products": (200, products_data)})
        async with httpx.AsyncClient(transport=transport) as client:
            agg = DataAggregator(client)
            result = await agg.fetch_products(1)
        assert len(result) == 2
        assert result[0]["name"] == "Doritos"

    @pytest.mark.asyncio
    async def test_fetch_devices(self, devices_data):
        transport = _transport({"/sensors/devices/counts": (200, devices_data)})
        async with httpx.AsyncClient(transport=transport) as client:
            agg = DataAggregator(client)
            result = await agg.fetch_devices(1)
        assert result["total"] == 5
        assert result["online"] == 4

    @pytest.mark.asyncio
    async def test_fetch_stats(self, stats_data):
        transport = _transport({"/stats": (200, stats_data)})
        async with httpx.AsyncClient(transport=transport) as client:
            agg = DataAggregator(client)
            result = await agg.fetch_stats(1)
        assert result["counts"]["FULL"] == 2

    @pytest.mark.asyncio
    async def test_fetch_restock_needs(self, restock_data):
        transport = _transport({"/restock-needs": (200, restock_data)})
        async with httpx.AsyncClient(transport=transport) as client:
            agg = DataAggregator(client)
            result = await agg.fetch_restock_needs(1)
        assert len(result) == 1
        assert result[0]["status"] == "LOW"

    @pytest.mark.asyncio
    async def test_fetch_inventory_bulk(self):
        inventory = [
            {"shelfId": 1, "productId": 1, "quantity": 10},
            {"shelfId": 2, "productId": 2, "quantity": 5},
        ]
        transport = _transport({"/retail/inventory/shelves/bulk": (200, inventory)})
        async with httpx.AsyncClient(transport=transport) as client:
            agg = DataAggregator(client)
            result = await agg.fetch_inventory([1, 2])
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_fetch_inventory_empty_list(self):
        async with httpx.AsyncClient(transport=_transport({})) as client:
            agg = DataAggregator(client)
            result = await agg.fetch_inventory([])
        assert result == []


class TestErrorHandling:
    """Test error scenarios for individual fetchers."""

    @pytest.mark.asyncio
    async def test_http_500_returns_default(self):
        transport = _transport({"/retail/stores/1": (500, {"error": "Internal"})})
        async with httpx.AsyncClient(transport=transport) as client:
            agg = DataAggregator(client)
            result = await agg.fetch_store_info(1)
        assert result == {}

    @pytest.mark.asyncio
    async def test_http_404_returns_default(self):
        transport = _transport({})  # All URLs return 404
        async with httpx.AsyncClient(transport=transport) as client:
            agg = DataAggregator(client)
            result = await agg.fetch_aisles(1)
        assert result == []

    @pytest.mark.asyncio
    async def test_timeout_returns_default(self):
        transport = _error_transport(httpx.TimeoutException)
        async with httpx.AsyncClient(transport=transport) as client:
            agg = DataAggregator(client)
            result = await agg.fetch_products(1)
        assert result == []

    @pytest.mark.asyncio
    async def test_connection_error_returns_default(self):
        transport = _error_transport(httpx.ConnectError)
        async with httpx.AsyncClient(transport=transport) as client:
            agg = DataAggregator(client)
            result = await agg.fetch_devices(1)
        assert result == {"total": 0, "online": 0, "offline": 0}

    @pytest.mark.asyncio
    async def test_stats_default_on_error(self):
        transport = _error_transport(httpx.ConnectError)
        async with httpx.AsyncClient(transport=transport) as client:
            agg = DataAggregator(client)
            result = await agg.fetch_stats(1)
        assert "counts" in result
        assert "anomalies_detected" in result


class TestHeaderPropagation:
    """Test that authorization headers are correctly propagated."""

    @pytest.mark.asyncio
    async def test_bearer_token_sent(self):
        captured_headers = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured_headers.update(dict(request.headers))
            return httpx.Response(200, json={"id": 1})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            agg = DataAggregator(client, token="my-jwt-token")
            await agg.fetch_store_info(1)

        assert "authorization" in captured_headers
        assert captured_headers["authorization"] == "Bearer my-jwt-token"

    @pytest.mark.asyncio
    async def test_no_token_no_auth_header(self):
        captured_headers = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured_headers.update(dict(request.headers))
            return httpx.Response(200, json=[])

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            agg = DataAggregator(client, token=None)
            await agg.fetch_aisles(1)

        assert "authorization" not in captured_headers


class TestAggregateInitialData:
    """Test the aggregate_initial_data parallel fetch."""

    @pytest.mark.asyncio
    async def test_all_services_healthy(
        self, store_data, aisles_data, bays_data, shelves_data,
        products_data, devices_data, stats_data, restock_data
    ):
        transport = _transport({
            "/retail/stores/1": (200, store_data),
            "/retail/aisles": (200, aisles_data),
            "/retail/bays": (200, bays_data),
            "/retail/shelves": (200, shelves_data),
            "/retail/products": (200, products_data),
            "/sensors/devices/counts": (200, devices_data),
            "/stats": (200, stats_data),
            "/retail/inventory/shelves/bulk": (200, []),
            "/restock-needs": (200, restock_data),
        })
        async with httpx.AsyncClient(transport=transport) as client:
            agg = DataAggregator(client, token="tok")
            result = await agg.aggregate_initial_data(1)

        assert result["store"]["id"] == 1
        assert len(result["aisles"]) == 1
        assert len(result["bays"]) == 1
        assert len(result["shelves"]) == 2
        assert len(result["products"]) == 2
        assert result["devices"]["total"] == 5
        assert "counts" in result["stats"]
        assert len(result["restock_needs"]) == 1

    @pytest.mark.asyncio
    async def test_partial_service_failure(self, store_data, shelves_data):
        """When some services fail, others should still return data."""
        transport = _transport({
            "/retail/stores/1": (200, store_data),
            "/retail/shelves": (200, shelves_data),
            # Other services return 404 (not found)
        })
        async with httpx.AsyncClient(transport=transport) as client:
            agg = DataAggregator(client)
            result = await agg.aggregate_initial_data(1)

        # Store and shelves should be present
        assert result["store"]["id"] == 1
        assert len(result["shelves"]) == 2
        # Others should have safe defaults
        assert result["aisles"] == []
        assert result["bays"] == []
        assert result["products"] == []
        assert result["devices"]["total"] == 0

    @pytest.mark.asyncio
    async def test_all_services_down(self):
        """When all services fail, should return safe defaults."""
        transport = _error_transport(httpx.ConnectError)
        async with httpx.AsyncClient(transport=transport) as client:
            agg = DataAggregator(client)
            result = await agg.aggregate_initial_data(1)

        assert result["store"] == {}
        assert result["aisles"] == []
        assert result["shelves"] == []
        assert result["products"] == []
        assert result["devices"]["total"] == 0
        assert result["inventory"] == []
        assert result["restock_needs"] == []

    @pytest.mark.asyncio
    async def test_inventory_fetched_for_shelves(self, shelves_data):
        """Inventory fetch uses shelf IDs from shelves response."""
        inventory = [{"shelfId": 1, "productId": 1, "qty": 10}]
        transport = _transport({
            "/retail/stores/1": (200, {}),
            "/retail/aisles": (200, []),
            "/retail/bays": (200, []),
            "/retail/shelves": (200, shelves_data),
            "/retail/products": (200, []),
            "/sensors/devices/counts": (200, {"total": 0, "online": 0, "offline": 0}),
            "/stats": (200, {}),
            "/retail/inventory/shelves/bulk": (200, inventory),
            "/restock-needs": (200, []),
        })
        async with httpx.AsyncClient(transport=transport) as client:
            agg = DataAggregator(client)
            result = await agg.aggregate_initial_data(1)

        assert len(result["inventory"]) == 1
        assert result["inventory"][0]["shelfId"] == 1

    @pytest.mark.asyncio
    async def test_no_shelves_skips_inventory(self):
        """If no shelves returned, inventory fetch should be skipped."""
        request_urls = []

        def handler(request: httpx.Request) -> httpx.Response:
            request_urls.append(str(request.url))
            if "/retail/stores" in str(request.url):
                return httpx.Response(200, json={})
            if "/stats" in str(request.url):
                return httpx.Response(200, json={})
            if "/restock-needs" in str(request.url):
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[])

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            agg = DataAggregator(client)
            result = await agg.aggregate_initial_data(1)

        assert result["inventory"] == []
        # Inventory endpoint should not have been called
        assert not any("/retail/inventory" in url for url in request_urls)
