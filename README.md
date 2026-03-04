# API Service - WebSocket Aggregator

The API Service is a unified WebSocket gateway for PaperWeight AI that aggregates data from multiple backend services (Stock, Retail, IoT) and provides a single WebSocket endpoint for frontend applications.

## Purpose

This service eliminates race conditions between REST API calls and WebSocket updates by:
1. Sending **complete initial data** immediately upon WebSocket connection
2. Following up with **real-time updates** from the Stock service
3. Providing a **single connection point** for all frontend needs

## Architecture

```
Frontend (Browser)
      ↓ WebSocket
   API Service
      ↓ REST + WebSocket
Stock + Retail + IoT Services
```

## Features

- **Unified WebSocket Endpoints**: Single connection for dashboard and events pages
- **Connection Pooling**: ONE Stock WS per store (99% reduction in connections)
- **Initial Data Caching**: 30s TTL cache (97% reduction in REST calls)
- **Multi-Client Broadcast**: All clients share one connection, receive same updates
- **Access Control**: Users can only access stores they have permissions for
- **Data Filtering**: Automatic filtering based on user permissions
- **Real-time Updates**: Forwards stats, inventory, and events from Stock service
- **Authentication**: JWT-based auth with role-based access control
- **Health Monitoring**: Comprehensive health checks for all backend services
- **High Performance**: Async/await throughout, connection pooling, parallel data fetching

## Endpoints

### WebSocket Endpoints

- **`WS /ws/dashboard/{store_id}`** - Dashboard page WebSocket
  - **Auth**: Required (JWT token)
  - **Access Control**: User must have access to store_id
  - **Initial data**: store, aisles, bays, shelves, products, devices, stats, inventory
  - **Updates**: stats_update, inventory_update, event_pushed (broadcasted to all clients)
  - **Pooling**: Shared Stock WS connection per store

- **`WS /ws/events/{store_id}`** - Retail events page WebSocket
  - **Auth**: Required (JWT token)
  - **Access Control**: User must have access to store_id
  - **Initial data**: stats, configuration, restock needs
  - **Updates**: stats_update, inventory_update, event_pushed (broadcasted to all clients)
  - **Pooling**: Shared Stock WS connection per store

### HTTP Endpoints

- **`GET /`** - Service information and available endpoints
- **`GET /health`** - Health check (database + backend services)
- **`GET /metrics`** - Prometheus metrics
- **`GET /api/aggregator/stats`** - WebSocket manager statistics (monitoring)

## WebSocket Message Protocol

### Client → Server

Connect with authentication:
```
ws://api:8000/ws/dashboard/{store_id}?token={jwt_token}
```

Or use Authorization header or cookie.

### Server → Client

**Initial Data** (sent immediately after connection):
```json
{
  "type": "initial_data",
  "storeId": 1,
  "data": {
    "store": {...},
    "aisles": [...],
    "bays": [...],
    "shelves": [...],
    "products": [...],
    "devices": {"total": 10, "online": 8, "offline": 2},
    "stats": {"counts": {...}, "config": {...}},
    "inventory": [...]
  }
}
```

**Stats Update** (periodic, ~1s):
```json
{
  "type": "stats_update",
  "storeId": 1,
  "data": {
    "counts": {"OUT_OF_STOCK": 5, "LOW": 12, ...},
    "anomalies_detected": 3,
    "avg_restock_period": "2h 15m",
    "config": {...}
  }
}
```

**Inventory Update** (periodic, ~1s):
```json
{
  "type": "inventory_update",
  "storeId": 1,
  "data": [
    {
      "shelf_id": 1,
      "product_id": 10,
      "stock_number": 8,
      "stock_maximum": 20,
      "stock_percentage": 0.4,
      "stock_state": "LOW",
      "product_name": "Product A"
    },
    ...
  ]
}
```

**Real-time Event** (on occurrence; theft and restock have dedicated channels for ASAP delivery):
```json
{
  "type": "event_pushed",
  "channel": "store/1/theft",
  "storeId": 1,
  "data": {
    "eventType": "THEFT",
    "shelfId": 1,
    "productId": 10,
    "productName": "Product A",
    "timestamp": "2026-02-12T10:30:00Z"
  }
}
```
Channels: `store/{id}/events` (all), `store/{id}/theft`, `store/{id}/restock`, `store/{id}/stock_state`, `store/{id}/shop_info` (future). Frontends can subscribe only to the channels they need.

## Access Control

### User Permissions

**Admin Users** (`is_admin: true`):
- Access to ALL stores
- See all data unfiltered

**Regular Users** (`is_admin: false`):
- Only access stores in their `store_ids` array
- Attempting to access unauthorized store → Connection rejected (403)
- Data automatically filtered to their permissions

### JWT Token Structure

```json
{
  "sub": "username",
  "user_id": 123,
  "is_admin": false,
  "store_ids": [1, 3, 5],
  "exp": 1234567890
}
```

### Permission Checks

1. **Connection**: User must have access to requested store_id
2. **Data Filtering**: Non-admins only see data from their stores
3. **Future**: Can add aisle-level, shelf-level permissions

## Configuration

Environment variables (see `env.example`):

### Service Configuration
```bash
SERVICE_NAME=api
API_HOST=0.0.0.0
API_PORT=8000
LOG_LEVEL=INFO
```

### Authentication
```bash
JWT_PUBLIC_KEY_PATH=jwt_public_key.pem
LOGIN_SERVICE_URL=http://login:8005
SERVICE_CLIENT_ID=api-service
SERVICE_CLIENT_SECRET=your-service-secret
```

The API service authenticates to backend services using OAuth2 `client_credentials` grant via `ServiceAuthClient`. Tokens are cached and auto-refreshed.

### Backend Services
```bash
STOCK_SERVICE_URL=http://stock:8000
RETAIL_SERVICE_URL=http://retail:8000
IOT_SERVICE_URL=http://iot:8000
```

### WebSocket Configuration (API aggregator – flexible, efficient, safe)
```bash
WS_PING_INTERVAL=20           # Ping interval (seconds)
WS_PING_TIMEOUT=10            # Pong timeout before disconnect
WS_OPEN_TIMEOUT=10            # Stock WS connection timeout
WS_CLEANUP_GRACE_SECONDS=30   # Delay before closing idle Stock WS
WS_RECONNECT_BASE_DELAY=2.0   # Reconnect backoff base (seconds)
WS_RECONNECT_MAX_DELAY=30.0   # Reconnect backoff cap
WS_INITIAL_DATA_CACHE_TTL=30  # Initial data cache TTL (seconds)
WS_MAX_CLIENTS_PER_POOL=500   # Max clients per store (reject when full)
WS_SEND_TIMEOUT=5.0           # Per-client send timeout (slow client protection)
```

## Development

### Prerequisites
- Python 3.13+
- PostgreSQL (optional, used by stock service)
- Access to Stock, Retail, IoT services

### Setup
```bash
# Clone with submodules
git clone --recurse-submodules https://github.com/PaperWeight-AI/api.git
cd api

# Install dependencies
pip install -r requirements.txt

# Copy environment template
cp env.example .env

# Edit .env with your configuration
nano .env
```

### Running Locally
```bash
# Run with uvicorn
python main.py

# Or use uvicorn directly
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

### Testing
```bash
# Run tests
pytest

# With coverage
pytest --cov=. --cov-report=html

# Run specific test file
pytest tests/test_websocket.py -v
```

### Docker
```bash
# Build image
docker build -t api:latest .

# Run container
docker run -p 8000:8000 --env-file .env api:latest
```

## Production Deployment

### Docker Compose

Add to your `compose.yml`:

```yaml
services:
  api:
    build: ./api
    container_name: paperweight-api
    ports:
      - "9008:8000"
    environment:
      - DATABASE_URL=postgresql://postgres:password@postgres:5432/postgres
      - STOCK_SERVICE_URL=http://stock:8000
      - RETAIL_SERVICE_URL=http://retail:8000
      - IOT_SERVICE_URL=http://iot:8000
      - LOGIN_SERVICE_URL=http://login:8005
      - JWT_PUBLIC_KEY_PATH=/app/jwt_public_key.pem
      - INTERNAL_API_SECRET=${INTERNAL_API_SECRET}
    volumes:
      - ./jwt_public_key.pem:/app/jwt_public_key.pem:ro
    depends_on:
      - postgres
      - stock
      - retail
      - iot
      - login
    networks:
      - paperweight
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 30s
```

### Frontend Integration

Update frontend to use API service WebSocket:

```javascript
// OLD: Direct connection to stock service
const ws = new WebSocket(`ws://stock:8000/ws/stock/events?token=${token}`);

// NEW: Connection to API service
const ws = new WebSocket(`ws://api:8000/ws/dashboard/${storeId}?token=${token}`);

ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);

  if (msg.type === 'initial_data') {
    // Complete initial data received - render page
    renderInitialData(msg.data);
  } else if (msg.type === 'stats_update') {
    // Update stats
    updateStats(msg.data);
  } else if (msg.type === 'inventory_update') {
    // Update inventory
    updateInventory(msg.data);
  } else if (msg.type === 'event_pushed') {
    // Handle real-time event
    handleEvent(msg.data);
  }
};
```

## Monitoring

### Health Checks
```bash
# Check service health
curl http://localhost:8000/health

# Response:
{
  "service": "api",
  "status": "healthy",
  "checks": {
    "database": {"status": "healthy"},
    "stock": {"status": "healthy"},
    "retail": {"status": "healthy"},
    "iot": {"status": "healthy"}
  }
}
```

### Metrics
Prometheus metrics available at `/metrics`:
- HTTP request durations
- WebSocket connection counts
- Backend service health
- Error rates

## Troubleshooting

### WebSocket Connection Fails
- Check authentication token is valid
- Verify backend services are running and healthy
- Check CORS configuration if connecting from browser
- Review logs: `docker logs paperweight-api`

### Initial Data Not Received
- Verify all backend services (Stock, Retail, IoT) are accessible
- Check service URLs in environment configuration
- Review aggregator logs for HTTP errors

### Real-time Updates Not Working
- Verify Stock service WebSocket is operational
- Check Stock service URL configuration
- Verify service credentials are configured (`SERVICE_CLIENT_ID`, `SERVICE_CLIENT_SECRET`)

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## License

Copyright © 2026 PaperWeight AI. All rights reserved.
