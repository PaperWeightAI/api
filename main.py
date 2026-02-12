#!/usr/bin/env python3
"""
API Service - WebSocket Aggregator for PaperWeight AI

This service acts as a unified WebSocket endpoint that aggregates data from:
- Stock Service (stats, inventory, real-time events)
- Retail Service (stores, aisles, bays, shelves, products)
- IoT Service (device counts, sensor data)

Clients connect to a single WebSocket endpoint and receive complete initial data
followed by real-time updates, eliminating race conditions between REST API calls
and WebSocket messages.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import make_asgi_app
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from common.database import async_engine, Base, get_async_db
from common.http_client import get_http_client
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

# Import routers
from routers import websocket
from utils.websocket_manager import WebSocketManager

# Configure logging
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("api")

# Environment configuration
SERVICE_NAME = os.getenv("SERVICE_NAME", "api")
SERVICE_VERSION = os.getenv("SERVICE_VERSION", "1.0.0")
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8000"))
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*").split(",")

STOCK_SERVICE_URL = os.getenv("STOCK_SERVICE_URL", "http://stock:8000")
RETAIL_SERVICE_URL = os.getenv("RETAIL_SERVICE_URL", "http://retail:8000")
IOT_SERVICE_URL = os.getenv("IOT_SERVICE_URL", "http://iot:8000")
LOGIN_SERVICE_URL = os.getenv("LOGIN_SERVICE_URL", "http://login:8005")

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")
INTERNAL_API_SECRET = os.getenv("INTERNAL_API_SECRET", "")


async def _get_startup_token(client: httpx.AsyncClient) -> str | None:
    """Fetch admin token from login service on startup."""
    try:
        logger.info(f"Fetching startup token from {LOGIN_SERVICE_URL}...")
        response = await client.post(
            f"{LOGIN_SERVICE_URL}/oauth/token",
            data={
                "grant_type": "password",
                "username": ADMIN_USERNAME,
                "password": ADMIN_PASSWORD
            },
            timeout=10.0
        )
        if response.status_code == 200:
            token = response.json().get("access_token")
            logger.info("✓ Startup token acquired")
            return token
        else:
            logger.error(f"Token fetch failed: {response.status_code} - {response.text}")
    except Exception as e:
        logger.error(f"Token fetch error: {e}")
    return None


async def _check_service_health(client: httpx.AsyncClient, service_url: str, service_name: str) -> bool:
    """Check if a backend service is healthy."""
    try:
        response = await client.get(f"{service_url}/health", timeout=5.0)
        is_healthy = response.status_code == 200
        logger.info(f"{'✓' if is_healthy else '✗'} {service_name} health check: {response.status_code}")
        return is_healthy
    except Exception as e:
        logger.error(f"✗ {service_name} health check failed: {e}")
        return False


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator:
    """
    Lifespan context manager for startup and shutdown tasks.
    """
    logger.info(f"🚀 Starting {SERVICE_NAME} v{SERVICE_VERSION}")

    # ===== STARTUP =====

    # 1. Initialize database tables
    try:
        async with async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("✓ Database tables initialized")
    except Exception as e:
        logger.error(f"Database initialization error: {e}")

    # 2. Create shared HTTP client
    http_client = await get_http_client()
    app.state.http_client = http_client
    logger.info("✓ HTTP client pool created")

    # 3. Fetch startup token
    startup_token = await _get_startup_token(http_client)
    app.state.startup_token = startup_token
    if startup_token:
        logger.info("✓ Startup authentication configured")
    else:
        logger.warning("⚠ No startup token - service may have limited functionality")

    # 4. Health check backend services
    await asyncio.gather(
        _check_service_health(http_client, STOCK_SERVICE_URL, "Stock"),
        _check_service_health(http_client, RETAIL_SERVICE_URL, "Retail"),
        _check_service_health(http_client, IOT_SERVICE_URL, "IoT"),
        return_exceptions=True
    )

    # 5. Initialize WebSocket manager (efficient connection pooling)
    stock_ws_url = STOCK_SERVICE_URL.replace("http://", "ws://").replace("https://", "wss://") + "/ws/stock/events"
    ws_manager = WebSocketManager(stock_ws_url)
    app.state.ws_manager = ws_manager
    logger.info("✓ WebSocket manager initialized (connection pooling enabled)")

    logger.info(f"✓ {SERVICE_NAME} is ready")

    yield

    # ===== SHUTDOWN =====
    logger.info(f"🛑 Shutting down {SERVICE_NAME}")

    # Close HTTP client
    if hasattr(app.state, 'http_client'):
        await app.state.http_client.aclose()
        logger.info("✓ HTTP client closed")

    logger.info(f"✓ {SERVICE_NAME} shutdown complete")


# Initialize FastAPI application
app = FastAPI(
    title="PaperWeight AI - API Service",
    description="WebSocket Aggregator for unified real-time data streaming",
    version=SERVICE_VERSION,
    lifespan=lifespan
)

# ===== MIDDLEWARE =====

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Rate limiting
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ===== ROUTERS =====

app.include_router(websocket.router)

# ===== CORE ENDPOINTS =====

@app.get("/")
async def root():
    """Root endpoint with service information."""
    return {
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "status": "operational",
        "endpoints": {
            "websocket_dashboard": "/ws/dashboard/{store_id}",
            "websocket_events": "/ws/events/{store_id}",
            "health": "/health",
            "metrics": "/metrics"
        }
    }


@app.get("/health")
async def health_check(request: Request, db: AsyncSession = Depends(get_async_db)):
    """
    Comprehensive health check.
    Checks database connectivity and backend services.
    """
    health_status = {
        "service": SERVICE_NAME,
        "status": "healthy",
        "checks": {}
    }

    checks = []

    # Check database
    async def check_db():
        try:
            await db.execute(text("SELECT 1"))
            return ("database", True, None)
        except Exception as e:
            return ("database", False, str(e))

    # Check backend services
    async def check_service(service_url: str, service_name: str):
        try:
            client = request.app.state.http_client
            response = await client.get(f"{service_url}/health", timeout=5.0)
            return (service_name, response.status_code == 200, None)
        except Exception as e:
            return (service_name, False, str(e))

    # Run all checks in parallel
    results = await asyncio.gather(
        check_db(),
        check_service(STOCK_SERVICE_URL, "stock"),
        check_service(RETAIL_SERVICE_URL, "retail"),
        check_service(IOT_SERVICE_URL, "iot"),
        return_exceptions=True
    )

    # Process results
    all_healthy = True
    for result in results:
        if isinstance(result, Exception):
            health_status["checks"]["error"] = str(result)
            all_healthy = False
            continue

        check_name, is_healthy, error = result
        health_status["checks"][check_name] = {
            "status": "healthy" if is_healthy else "unhealthy",
            "error": error
        }
        if not is_healthy:
            all_healthy = False

    health_status["status"] = "healthy" if all_healthy else "degraded"
    status_code = 200 if all_healthy else 503

    return JSONResponse(status_code=status_code, content=health_status)


# Mount Prometheus metrics
app.mount("/metrics", make_asgi_app())


if __name__ == "__main__":
    import uvicorn

    logger.info(f"Starting {SERVICE_NAME} on {API_HOST}:{API_PORT}")
    uvicorn.run(
        "main:app",
        host=API_HOST,
        port=API_PORT,
        reload=os.getenv("ENVIRONMENT") == "local",
        log_level=os.getenv("LOG_LEVEL", "INFO").lower()
    )
