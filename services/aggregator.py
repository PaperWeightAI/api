#!/usr/bin/env python3
"""
Data Aggregator Service

Fetches and aggregates data from Stock, Retail, and IoT services.
Provides complete initial data payloads for WebSocket clients.
"""

import asyncio
import logging
import os
from typing import Any, Optional

import httpx

logger = logging.getLogger("api.aggregator")

# Service URLs
STOCK_SERVICE_URL = os.getenv("STOCK_SERVICE_URL", "http://stock:8000")
RETAIL_SERVICE_URL = os.getenv("RETAIL_SERVICE_URL", "http://retail:8000")
IOT_SERVICE_URL = os.getenv("IOT_SERVICE_URL", "http://iot:8000")


class DataAggregator:
    """
    Aggregates data from multiple backend services.
    """

    def __init__(self, http_client: httpx.AsyncClient, token: Optional[str] = None):
        """
        Initialize the aggregator.

        Args:
            http_client: Shared HTTP client instance
            token: Optional bearer token for authenticated requests
        """
        self.client = http_client
        self.token = token
        self.headers = self._build_headers()

    def _build_headers(self) -> dict:
        """Build request headers with authentication."""
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def _fetch_json(
        self,
        url: str,
        service_name: str,
        timeout: float = 10.0,
        default: Any = None
    ) -> Any:
        """
        Fetch JSON data from a URL with error handling.

        Args:
            url: Full URL to fetch
            service_name: Service name for logging
            timeout: Request timeout in seconds
            default: Default value to return on error

        Returns:
            JSON response data or default value on error
        """
        try:
            logger.debug(f"Fetching {service_name}: {url}")
            response = await self.client.get(url, headers=self.headers, timeout=timeout)

            if response.status_code != 200:
                logger.warning(
                    f"{service_name} returned {response.status_code}: {url}"
                )
                return default

            return response.json()

        except asyncio.TimeoutError:
            logger.error(f"{service_name} timeout: {url}")
            return default
        except Exception as e:
            logger.error(f"{service_name} error: {e} - {url}")
            return default

    async def fetch_store_info(self, store_id: int) -> Optional[dict]:
        """Fetch store information from Retail service."""
        url = f"{RETAIL_SERVICE_URL}/retail/stores/{store_id}"
        return await self._fetch_json(url, "Retail/Store", default={})

    async def fetch_aisles(self, store_id: int) -> list[dict]:
        """Fetch aisles from Retail service."""
        url = f"{RETAIL_SERVICE_URL}/retail/aisles?store_id={store_id}"
        return await self._fetch_json(url, "Retail/Aisles", default=[])

    async def fetch_bays(self, store_id: int) -> list[dict]:
        """Fetch bays from Retail service."""
        url = f"{RETAIL_SERVICE_URL}/retail/bays?store_id={store_id}"
        return await self._fetch_json(url, "Retail/Bays", default=[])

    async def fetch_shelves(self, store_id: int) -> list[dict]:
        """Fetch shelves from Retail service."""
        url = f"{RETAIL_SERVICE_URL}/retail/shelves?store_id={store_id}"
        return await self._fetch_json(url, "Retail/Shelves", default=[])

    async def fetch_products(self, store_id: int) -> list[dict]:
        """Fetch products from Retail service."""
        url = f"{RETAIL_SERVICE_URL}/retail/products?store_id={store_id}"
        return await self._fetch_json(url, "Retail/Products", default=[])

    async def fetch_devices(self, store_id: int) -> dict:
        """Fetch device counts from IoT service."""
        url = f"{IOT_SERVICE_URL}/sensors/devices/counts?store_id={store_id}"
        return await self._fetch_json(
            url,
            "IoT/Devices",
            default={"total": 0, "online": 0, "offline": 0}
        )

    async def fetch_inventory(self, shelf_ids: list[int]) -> list[dict]:
        """
        Fetch inventory for multiple shelves from Retail service.

        Args:
            shelf_ids: List of shelf IDs to fetch inventory for

        Returns:
            List of inventory items
        """
        if not shelf_ids:
            return []

        shelf_ids_str = ",".join(map(str, shelf_ids))
        url = f"{RETAIL_SERVICE_URL}/retail/inventory/shelves/bulk?shelf_ids={shelf_ids_str}"
        return await self._fetch_json(url, "Retail/Inventory", default=[])

    async def fetch_stats(self, store_id: int) -> dict:
        """
        Fetch stats from Stock service.

        Note: This is a REST fallback. Normally, stats come via WebSocket subscription.
        """
        url = f"{STOCK_SERVICE_URL}/stats?storeId={store_id}"
        return await self._fetch_json(url, "Stock/Stats", default={
            "counts": {},
            "anomalies_detected": 0,
            "avg_restock_period": "0m",
            "config": {},
            "store_config": {}
        })

    async def fetch_restock_needs(self, store_id: int, status_filter: Optional[str] = None) -> list[dict]:
        """
        Fetch restock needs from Stock service.

        Note: This is a REST fallback. Normally, inventory comes via WebSocket subscription.
        """
        url = f"{STOCK_SERVICE_URL}/stock?storeId={store_id}"
        if status_filter:
            url += f"&status={status_filter}"
        return await self._fetch_json(url, "Stock/RestockNeeds", default=[])

    async def aggregate_initial_data(self, store_id: int) -> dict:
        """
        Aggregate all initial data for a store.

        This method fetches data from multiple services in parallel and returns
        a complete initial payload for WebSocket clients.

        Args:
            store_id: Store ID to fetch data for

        Returns:
            Dictionary containing all aggregated data
        """
        logger.info(f"Aggregating initial data for store {store_id}")

        # Fetch data in parallel for efficiency
        (
            store_info,
            aisles,
            bays,
            shelves,
            products,
            devices,
            stats
        ) = await asyncio.gather(
            self.fetch_store_info(store_id),
            self.fetch_aisles(store_id),
            self.fetch_bays(store_id),
            self.fetch_shelves(store_id),
            self.fetch_products(store_id),
            self.fetch_devices(store_id),
            self.fetch_stats(store_id),
            return_exceptions=True
        )

        # Handle exceptions
        def safe_value(value, default):
            return default if isinstance(value, Exception) else value

        store_info = safe_value(store_info, {})
        aisles = safe_value(aisles, [])
        bays = safe_value(bays, [])
        shelves = safe_value(shelves, [])
        products = safe_value(products, [])
        devices = safe_value(devices, {"total": 0, "online": 0, "offline": 0})
        stats = safe_value(stats, {
            "counts": {},
            "anomalies_detected": 0,
            "avg_restock_period": "0m",
            "config": {},
            "store_config": {}
        })

        # Fetch inventory (Retail bulk, for dashboard) and restock_needs (Stock, for events page table)
        shelf_ids = [shelf.get("id") for shelf in shelves if shelf.get("id")]

        async def empty_list() -> list:
            return []

        inv_coro = self.fetch_inventory(shelf_ids) if shelf_ids else empty_list()
        inv_result, restock_result = await asyncio.gather(
            inv_coro,
            self.fetch_restock_needs(store_id),
        )
        inventory = inv_result if not isinstance(inv_result, Exception) else []
        if isinstance(inv_result, Exception):
            logger.error(f"Inventory fetch failed: {inv_result}")
        restock_needs = restock_result if not isinstance(restock_result, Exception) else []
        if isinstance(restock_result, Exception):
            logger.error(f"Restock needs fetch failed: {restock_result}")

        logger.info(
            f"Initial data aggregated: {len(aisles)} aisles, {len(shelves)} shelves, "
            f"{len(products)} products, {len(inventory)} inventory items, {len(restock_needs)} restock items"
        )

        return {
            "store": store_info,
            "aisles": aisles,
            "bays": bays,
            "shelves": shelves,
            "products": products,
            "devices": devices,
            "stats": stats,
            "inventory": inventory,
            "restock_needs": restock_needs,
        }
