"""
Production AI Agent — Part 6 Final Project

Đủ 12 tiêu chí trong README.md, kiểm tra bằng:
    python check_production_ready.py

Luồng của POST /ask:
    verify_api_key (401) → check_rate_limit (429) → check_budget (402)
    → gọi LLM → record_cost
"""
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.auth import verify_api_key
from app.config import settings
from app.cost_guard import check_budget, record_cost
from app.rate_limiter import check_rate_limit
from app.redis_client import redis_healthy
from utils.mock_llm import ask as llm_ask


# ─────────────────────────────────────────────────────────
# Structured logging — mỗi dòng log là 1 JSON object
# ─────────────────────────────────────────────────────────
_RESERVED = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "lvl": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # các field truyền qua extra={...}
        for key, value in vars(record).items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
        return json.dumps(payload, ensure_ascii=False, default=str)


def _setup_logging() -> logging.Logger:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    log = logging.getLogger("agent")
    log.handlers = [handler]
    log.setLevel(settings.log_level)
    log.propagate = False
    return log


logger = _setup_logging()


def estimate_tokens(text: str) -> int:
    """Ước lượng ~4 ký tự / token (đủ dùng cho mock LLM)."""
    return max(1, len(text) // 4)


# ─────────────────────────────────────────────────────────
# Lifespan — startup / graceful shutdown
# uvicorn tự xử lý SIGTERM: ngừng nhận kết nối mới, chờ request in-flight xong,
# rồi chạy phần sau `yield` và thoát với exit code 0.
# ─────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info("startup", extra={"event": "startup", "environment": settings.environment,
                                  "port": settings.port,
                                  "rate_limit_per_minute": settings.rate_limit_per_minute,
                                  "daily_budget_usd": settings.daily_budget_usd,
                                  "redis_ok": redis_healthy()})
    yield
    logger.info("shutdown", extra={"event": "shutdown",
                                   "detail": "in-flight requests drained, exiting"})


app = FastAPI(title="Production AI Agent", version="1.0.0", lifespan=lifespan,
              docs_url=None if settings.environment == "production" else "/docs",
              redoc_url=None)
START_TIME = time.time()


@app.middleware("http")
async def access_log(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        logger.info("request", extra={"event": "request", "request_id": request_id,
                                      "method": request.method, "path": request.url.path,
                                      "status": status,
                                      "ms": round((time.perf_counter() - start) * 1000, 1)})


# ─────────────────────────────────────────────────────────
# Lỗi: trả JSON rõ ràng, không lộ stack trace
# ─────────────────────────────────────────────────────────
@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, exc: RequestValidationError):
    errors = [{"field": ".".join(str(p) for p in e.get("loc", [])[1:]), "msg": e.get("msg")}
              for e in exc.errors()]
    return JSONResponse(status_code=422, content={"error": "invalid_request", "details": errors})


@app.exception_handler(Exception)
async def unhandled_error(_request: Request, exc: Exception):
    logger.error("unhandled_error", extra={"event": "unhandled_error",
                                           "error_type": type(exc).__name__})
    return JSONResponse(status_code=500, content={"error": "internal_error"})


# ─────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────
class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    user_id: Optional[str] = Field(default=None, max_length=128)


@app.get("/")
def root():
    return {"service": "production-ai-agent", "endpoints": ["/health", "/ready", "POST /ask"]}


@app.get("/health")
def health():
    """Liveness — luôn 200 khi process còn chạy, không phụ thuộc Redis."""
    return {"status": "ok", "environment": settings.environment,
            "uptime_seconds": round(time.time() - START_TIME, 1)}


@app.get("/ready")
def ready():
    """Readiness — 503 khi Redis không kết nối được (LB ngừng gửi traffic tới instance này)."""
    if not redis_healthy():
        raise HTTPException(status_code=503, detail="Redis unavailable")
    return {"status": "ready"}


@app.post("/ask")
def ask(body: AskRequest, api_key: str = Depends(verify_api_key)):
    check_rate_limit(api_key)   # 429
    check_budget()              # 402

    answer = llm_ask(body.question)

    input_tokens = estimate_tokens(body.question)
    output_tokens = estimate_tokens(answer)
    cost = record_cost(input_tokens, output_tokens)
    logger.info("llm_call", extra={"event": "llm_call", "user_id": body.user_id,
                                   "input_tokens": input_tokens, "output_tokens": output_tokens,
                                   "cost_usd": round(cost, 8)})
    return {
        "question": body.question,
        "answer": answer,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens,
                  "cost_usd": round(cost, 8)},
    }
