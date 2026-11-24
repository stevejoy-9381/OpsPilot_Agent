from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict

import redis
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from apps.api.auth.routes import router as auth_router
from apps.api.domain.errors import (
    BudgetExhaustedError,
    ConcurrentModificationError,
    ForbiddenError,
    IdempotencyConflictError,
    IllegalTransitionError,
    NotFoundError,
)
from apps.api.logging import get_logger, setup_logging
from apps.api.routers import incidents_router, plans_router, runs_router
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

MAX_REQUEST_BODY_SIZE_BYTES = 1_048_576  # 1 MB


@app.middleware("http")
async def enforce_request_size_limit(request: Request, call_next: Any) -> Response:
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_REQUEST_BODY_SIZE_BYTES:
        return JSONResponse(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            content={
                "detail": f"Request entity too large: max allowed is {MAX_REQUEST_BODY_SIZE_BYTES} bytes",
                "error": {
                    "code": "payload_too_large",
                    "message": f"Request entity too large: max allowed is {MAX_REQUEST_BODY_SIZE_BYTES} bytes",
                    "details": {"max_bytes": MAX_REQUEST_BODY_SIZE_BYTES},
                },
            },
        )
    response: Response = await call_next(request)
    return response


# ---------------------------------------------------------------------------
# Consistent Error Envelope Exception Handlers
# ---------------------------------------------------------------------------


@app.exception_handler(HTTPException)
async def http_exception_envelope_handler(request: Request, exc: HTTPException) -> JSONResponse:
    code_map = {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        409: "conflict",
        413: "payload_too_large",
        422: "unprocessable_entity",
        429: "rate_limited",
        500: "internal_error",
        501: "not_implemented",
    }
    code = code_map.get(exc.status_code, "error")
    msg = str(exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": msg,
            "error": {
                "code": code,
                "message": msg,
                "details": getattr(exc, "headers", {}) or {},
            },
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_envelope_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    msg = "Request validation failed"
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": msg,
            "error": {
                "code": "validation_error",
                "message": msg,
                "details": {"errors": jsonable_encoder(exc.errors())},
            },
        },
    )


@app.exception_handler(NotFoundError)
async def not_found_error_handler(request: Request, exc: NotFoundError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={
            "detail": exc.message,
            "error": {
                "code": "not_found",
                "message": exc.message,
                "details": exc.details,
            },
        },
    )


@app.exception_handler(ForbiddenError)
async def forbidden_error_handler(request: Request, exc: ForbiddenError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_403_FORBIDDEN,
        content={
            "detail": exc.message,
            "error": {
                "code": "forbidden",
                "message": exc.message,
                "details": exc.details,
            },
        },
    )


@app.exception_handler(IllegalTransitionError)
async def illegal_transition_error_handler(
    request: Request, exc: IllegalTransitionError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={
            "detail": exc.message,
            "error": {
                "code": "illegal_transition",
                "message": exc.message,
                "details": exc.details,
            },
        },
    )


@app.exception_handler(ConcurrentModificationError)
async def concurrent_modification_error_handler(
    request: Request, exc: ConcurrentModificationError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "detail": exc.message,
            "error": {
                "code": "concurrent_modification",
                "message": exc.message,
                "details": exc.details,
            },
        },
    )


@app.exception_handler(IdempotencyConflictError)
async def idempotency_conflict_error_handler(
    request: Request, exc: IdempotencyConflictError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "detail": exc.message,
            "error": {
                "code": "idempotency_conflict",
                "message": exc.message,
                "details": exc.details,
            },
        },
    )


@app.exception_handler(BudgetExhaustedError)
async def budget_exhausted_error_handler(
    request: Request, exc: BudgetExhaustedError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={
            "detail": exc.message,
            "error": {
                "code": "budget_exhausted",
                "message": exc.message,
                "details": exc.details,
            },
        },
    )


app.include_router(auth_router)
app.include_router(incidents_router)
app.include_router(runs_router)
app.include_router(plans_router)


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
