from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from apps.api.main import app


@pytest.mark.asyncio
async def test_healthz() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_readyz_healthy() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/readyz")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["components"]["postgres"] == "ok"
        assert data["components"]["redis"] == "ok"


@pytest.mark.asyncio
async def test_readyz_degraded_when_postgres_fails() -> None:
    transport = ASGITransport(app=app)
    with patch("apps.api.main.check_postgres", return_value=False):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/readyz")
            assert response.status_code == 503
            data = response.json()
            assert data["status"] == "degraded"
            assert data["components"]["postgres"] == "degraded"
            assert data["components"]["redis"] == "ok"


@pytest.mark.asyncio
async def test_readyz_degraded_when_redis_fails() -> None:
    transport = ASGITransport(app=app)
    with patch("apps.api.main.check_redis", return_value=False):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/readyz")
            assert response.status_code == 503
            data = response.json()
            assert data["status"] == "degraded"
            assert data["components"]["postgres"] == "ok"
            assert data["components"]["redis"] == "degraded"
