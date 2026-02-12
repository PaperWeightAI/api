#!/usr/bin/env python3
"""
Pytest configuration and fixtures for API service tests.
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from fastapi.testclient import TestClient


@pytest.fixture
def mock_http_client():
    """Mock HTTP client for testing."""
    client = AsyncMock()
    client.get = AsyncMock()
    client.post = AsyncMock()
    client.aclose = AsyncMock()
    return client


@pytest.fixture
def jwt_keypair():
    """Generate RSA keypair for JWT testing."""
    from joserfc.jwk import RSAKey

    private_key = RSAKey.generate_key(2048)
    public_key = private_key.as_public()

    return {
        "private_key": private_key,
        "public_key": public_key
    }


@pytest.fixture
def mock_admin_token(jwt_keypair):
    """Generate a valid admin JWT token for testing."""
    from joserfc import jwt
    import time

    payload = {
        "sub": "testadmin",
        "user_id": 1,
        "is_admin": True,
        "is_operator": False,
        "exp": int(time.time()) + 3600
    }

    token = jwt.encode({"alg": "RS256"}, payload, jwt_keypair["private_key"])
    return token


@pytest.fixture
def mock_user_token(jwt_keypair):
    """Generate a valid non-admin JWT token for testing."""
    from joserfc import jwt
    import time

    payload = {
        "sub": "testuser",
        "user_id": 2,
        "is_admin": False,
        "is_operator": False,
        "exp": int(time.time()) + 3600
    }

    token = jwt.encode({"alg": "RS256"}, payload, jwt_keypair["private_key"])
    return token


@pytest.fixture
def test_client():
    """Create FastAPI test client."""
    from main import app
    return TestClient(app)


@pytest.fixture
def mock_aggregator():
    """Mock DataAggregator for testing."""
    with patch("services.aggregator.DataAggregator") as mock:
        instance = mock.return_value
        instance.aggregate_initial_data = AsyncMock(return_value={
            "store": {"id": 1, "name": "Test Store"},
            "aisles": [],
            "bays": [],
            "shelves": [],
            "products": [],
            "devices": {"total": 0, "online": 0, "offline": 0},
            "stats": {"counts": {}, "config": {}},
            "inventory": []
        })
        yield instance
