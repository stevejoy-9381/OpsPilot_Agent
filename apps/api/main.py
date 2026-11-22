from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict

import redis
from fastapi import FastAPI, Response, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from apps.api.logging import get_logger, setup_logging
from apps.api.settings import get_settings

logger = get_logger("opspilot.api")


def get_db_engine() -> Engine:
    settings = get_settings()
    raw_url = settings.database_url.get_secret_value()
    if raw_url.startswith("postgresql://"):
        raw_url = raw_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return create_engine(
        raw_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 3},
    )


def check_postgres() -> bool:
    try:
        engine = get_db_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.warning("postgres_readiness_check_failed", error=str(exc))
        return False


def check_redis() -> bool:
    try:
        settings = get_settings()
        r = redis.Redis.from_url(
            settings.redis_url.get_secret_value(),
            socket_connect_timeout=3,
            socket_timeout=3,
        )
        return bool(r.ping())
    except Exception as exc:
        logger.warning("redis_readiness_check_failed", error=str(exc))
        return False


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = get_settings()
    setup_logging(settings.log_level)
    logger.info("api_started", log_level=settings.log_level)
    yield
    logger.info("api_stopped")


app = FastAPI(
    title="OpsPilot API",
    description="Incident-investigation agent API",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/healthz")
async def healthz() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz(response: Response) -> Dict[str, Any]:
    pg_ok = check_postgres()
    redis_ok = check_redis()

    pg_status = "ok" if pg_ok else "degraded"
    redis_status = "ok" if redis_ok else "degraded"

    is_ready = pg_ok and redis_ok
    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ok" if is_ready else "degraded",
        "components": {
            "postgres": pg_status,
            "redis": redis_status,
        },
    }
