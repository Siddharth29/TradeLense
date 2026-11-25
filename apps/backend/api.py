import json
from typing import Any, Mapping, Set, Literal, List, Optional
import glob
import asyncio

# Load .env from repo root before any other imports read os.environ.
# This is a no-op if python-dotenv is not installed or the file doesn't exist.
try:
    from dotenv import load_dotenv as _load_dotenv
    import pathlib as _pathlib
    _load_dotenv(_pathlib.Path(__file__).resolve().parents[2] / ".env", override=False)
except ImportError:
    pass
from fastapi import (
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    Body,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse
from datetime import datetime, timezone, timedelta, date
from packages.core.datetime_utils import serialize_for_json
from pydantic import BaseModel, ValidationError
import statistics
import logging
import os




# NOTE: Keep imports consistent with our monorepo layout where reusable
# libraries live under the top-level `packages` namespace.
from apps.backend.agents import Supervisor
from packages.core.metrics import metrics as agent_metrics, AgentMetrics
from apps.backend.routers import stream as stream_router
from apps.backend.routers import mt5 as mt5_router
from apps.backend.routers import configs as configs_router
from apps.backend.routers import trading_filters as trading_filters_router
from apps.backend.routers import gpt_threads as gpt_threads_router
from apps.backend.routers import prompts as prompts_router
from apps.backend.routers import backtest as backtest_router
from packages.core.candle_service import CandleService
from packages.core.offline_replay import offline_mode
from packages.db.client import get_client
from packages.core.config import (
    MONGO_DB_NAME, LOGS_DIR, OPENAI_API_KEY, AI_PROVIDER, GEMINI_API_KEY,
    CORS_ALLOWED_ORIGINS, CORS_ALLOWED_METHODS, CORS_ALLOWED_HEADERS,
    CORS_EXPOSE_HEADERS, CORS_MAX_AGE,
)
from packages.db.repositories import read
from packages.db.repositories import configs as config_repo
from packages.db.repositories import agents as agents_repo
from packages.db.repositories import gpt_responses as gpt_repo
from packages.db.repositories.agents import AgentRecord
from packages.db.config_schemas import (
    AgentConfig,
    GptConfig,
    FeatureFlagsConfig,
    AdminConfig,
    UiPrefsConfig,
    BrokerConfigMt5,
    BrokerType,
    Env,
)
from src.common import logger

# in-memory trade lifecycle events
from apps.backend import trade_events
from packages.adapters.factory import create_broker
from .utils.datetimes import to_aware_utc


async def _apply_saved_model_config(cfg: dict[str, Any], symbol: str) -> None:
    try:
        model_doc, _ = await config_repo.get_model_config()
    except Exception as exc:
        logger.warning({"symbol": symbol}, f"Could not refresh model configuration: {exc}")
        return

    if model_doc:
        cfg["gpt_config"] = dict(model_doc)
        logger.info({"symbol": symbol, "model": model_doc.get("model")}, "Loaded global model config")


async def _agent_runner(asset: str, N: int, cfg: Mapping[str, Any]) -> Any:
    """Wrapper to execute an agent run with ``AgentRunner``.

    ``AgentRunner`` is imported lazily to avoid heavy dependencies during
    application startup and testing.
    """

    from apps.backend.agents import AgentRunner  # local import
    from packages.adapters.factory import create_broker

    cfg = dict(cfg)

    # Extract symbol from instrument config if not directly available
    if not cfg.get("symbol"):
        instrument = cfg.get("instrument", {})
        if isinstance(instrument, dict) and instrument.get("symbol"):
            cfg["symbol"] = instrument["symbol"]
        else:
            raise HTTPException(status_code=400, detail="Symbol is required for agent execution")

    missing: List[str] = []
    for key in ("symbol", "candle_count_5m", "broker"):
        if not cfg.get(key):
            missing.append(key)
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing config fields: {', '.join(missing)}")

    # Validate candle counts are greater than 0
    candle_count_5m = int(cfg.get("candle_count_5m", 0))
    if candle_count_5m <= 0:
        raise HTTPException(status_code=400, detail=f"candle_count_5m must be greater than 0, got {candle_count_5m}")

    # Derive 15m candle count from configuration; raise if missing.
    try:
        candle_count_15m = cfg["candles_15_count"]
    except KeyError as exc:
        raise KeyError("candles_15_count is required in config") from exc
    
    candle_count_15m = int(candle_count_15m)
    if candle_count_15m <= 0:
        raise HTTPException(status_code=400, detail=f"candles_15_count must be greater than 0, got {candle_count_15m}")
    
    cfg["candle_count_15m"] = candle_count_15m

    # If broker is provided as a name, create and configure it.
    broker = cfg["broker"]
    if isinstance(broker, str):
        broker_name = broker

        # Always create the market data broker (MT5) for market data access.
        broker_config = {}
        
        if broker_name.lower() == "mt5":
            # Create a new MT5 adapter instance for each agent to avoid race conditions
            # But reuse the login credentials from the existing session
            tokens = getattr(app.state, "broker_tokens", {})
            mt5_session = tokens.get("mt5", {})
            
            # Create a new MT5 adapter instance for this agent
            broker = create_broker("mt5")
            
            # If there's an active MT5 session, use the stored credentials to connect
            if mt5_session:
                logger.info({"broker": "mt5", "asset": asset}, "Creating new MT5 adapter instance for agent with reused credentials")
                # Extract credentials from the stored session
                required_fields = ["server", "login", "password"]
                missing_fields = [field for field in required_fields if not mt5_session.get(field)]
                
                if missing_fields:
                    logger.error({
                        "broker": "mt5", 
                        "asset": asset, 
                        "missing_fields": missing_fields
                    }, "MT5 session missing required credentials")
                    raise HTTPException(
                        status_code=400, 
                        detail=f"MT5 session incomplete, missing: {', '.join(missing_fields)}"
                    )
                
                broker_config.update({
                    "server": mt5_session["server"],
                    "login": mt5_session["login"],
                    "password": mt5_session["password"],
                })
            else:
                logger.warning({"broker": "mt5", "asset": asset}, "No active MT5 session found, broker may not be connected")

        # Connect the market data broker with reused credentials
        if broker_config:
            logger.info(
                {"broker": "mt5", "asset": asset},
                "Connecting MT5 broker for agent — this may take up to 30 s",
                server=broker_config.get("server"),
                login=broker_config.get("login"),
            )
            await broker.connect(**broker_config)
            logger.info(
                {"broker": "mt5", "asset": asset},
                "MT5 broker connected successfully",
            )

        # MT5 is the only broker and always trades live: the order broker is the
        # MT5 adapter itself, which also serves as the market-data source.
        market_data_broker = broker

        cfg["broker"] = broker
    else:
        # If broker is already an object, determine its name for logging
        broker_name = getattr(broker.__class__, "__name__", "unknown").lower()

        # For object brokers, the broker itself is the market data broker
        market_data_broker = broker

    # Resolve candle service
    candle_service = cfg.get("candle_service")
    
    # Configure candle service after broker is properly set up
    # Use the market data broker for candle service
    if not candle_service:
        # Create candle service with the market data broker
        cfg["candle_service"] = CandleService(
            broker=market_data_broker if hasattr(market_data_broker, "candles") else None,
        )
    elif isinstance(candle_service, str) and candle_service.lower() == "mt5":
        # For MT5 candle service, use the market data broker instance
        logger.info({"broker": "mt5", "asset": asset}, "Using market data MT5 broker for candle service")
        if "mt5_service" not in cfg:
            cfg["mt5_service"] = market_data_broker
        cfg["candle_service"] = CandleService(broker=market_data_broker)

    # Final broker validation and service setup
    broker = cfg.get("broker")
    if not hasattr(broker, "place"):
        raise HTTPException(status_code=400, detail="Broker is not functional")
    
    # Ensure broker services are set up for MT5
    if broker and hasattr(broker, "place"):
        broker_type = getattr(broker.__class__, "__name__", "").lower()
        
        # Set up MT5 service if broker is MT5Adapter
        if "mt5" in broker_type and "mt5_service" not in cfg:
            cfg["mt5_service"] = broker
            logger.info({"asset": asset, "broker": broker_type}, "MT5 service configured for real-time monitoring")
    
    # Set up journal service for trade logging
    if "journal" not in cfg:
        from packages.core.journal import AssetJournal
        from packages.core.config import LOGS_DIR
        
        # Get agent_id for journal naming
        agent_id = cfg.get("agent_id", f"agent_{asset}")
        
        cfg["journal"] = AssetJournal(
            root=LOGS_DIR,
            asset_id=asset,
            agent_name=agent_id
        )
        logger.info({"asset": asset, "agent_id": agent_id}, "Journal service initialized for trade logging")
    
    # Final validation before creating AgentRunner
    required_fields = {
        "symbol": cfg.get("symbol"),
        "agent_id": cfg.get("agent_id"),
        "candle_count_5m": cfg.get("candle_count_5m"),
        "candle_count_15m": cfg.get("candle_count_15m"),
        "candle_service": cfg.get("candle_service"),
        "broker": cfg.get("broker"),
    }
    
    missing_fields = [k for k, v in required_fields.items() if v is None]
    if missing_fields:
        raise HTTPException(
            status_code=400, 
            detail=f"Missing required fields for agent runner: {', '.join(missing_fields)}"
        )
    
    # Log final configuration for debugging
    logger.info(
        {"asset": asset, "agent_id": cfg.get("agent_id")},
        "Starting agent runner with configuration",
        symbol=cfg.get("symbol"),
        candle_counts={"5m": cfg.get("candle_count_5m"), "15m": cfg.get("candle_count_15m")},
        broker_type=getattr(broker.__class__, "__name__", "Unknown") if broker else "None"
    )
    
    # The supervisor holds the start configuration across cycles. Read the
    # saved model again here so each new cycle uses one current snapshot.
    await _apply_saved_model_config(cfg, cfg["symbol"])
    runner = AgentRunner(cfg)
    
    # Set execution mode and scheduled time if provided
    execution_mode = cfg.get("execution_mode", "run_now")
    scheduled_time = cfg.get("scheduled_time")
    runner.set_execution_mode(execution_mode, scheduled_time)
    
    try:
        return await runner.run()
    finally:
        # Ensure proper cleanup of resources
        try:
            await runner.cleanup_resources()
        except Exception as e:
            logger.error({"domain": "agents"}, f"Error cleaning up AgentRunner for {asset}: {e}")


supervisor = Supervisor(_agent_runner)


app = FastAPI(
    title="TradeLense API",
    description="Backend for TradeLense — AI-proposed strategies, deterministic risk gates, audited execution.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_methods=CORS_ALLOWED_METHODS,
    allow_headers=CORS_ALLOWED_HEADERS,
    allow_credentials=True,
    expose_headers=CORS_EXPOSE_HEADERS,
    max_age=CORS_MAX_AGE,
)
app.include_router(stream_router.router, prefix="/stream")
app.include_router(mt5_router.router, prefix="/brokers/mt5")
app.include_router(configs_router.router, prefix="/configs")
app.include_router(trading_filters_router.router)
app.include_router(gpt_threads_router.router)
app.include_router(prompts_router.router)
app.include_router(backtest_router.router, prefix="/api")


# Duplicate startup/shutdown handlers removed - consolidated below


@app.middleware("http")
async def _cors_preflight_middleware(request: Request, call_next):
    """Handle CORS preflight requests explicitly."""
    if request.method == "OPTIONS":
        # Log the preflight request for debugging
        logger.info(
            {
                "method": request.method,
                "path": request.url.path,
                "origin": request.headers.get("origin"),
                "access_control_request_method": request.headers.get("access-control-request-method"),
                "access_control_request_headers": request.headers.get("access-control-request-headers"),
            },
            "CORS preflight request"
        )
        
        # Create a response with CORS headers
        response = Response()
        origin = request.headers.get("origin")
        
        if origin in CORS_ALLOWED_ORIGINS:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Allow-Methods"] = ", ".join(CORS_ALLOWED_METHODS)
            response.headers["Access-Control-Allow-Headers"] = ", ".join(CORS_ALLOWED_HEADERS)
            response.headers["Access-Control-Max-Age"] = str(CORS_MAX_AGE)
            response.headers["Access-Control-Expose-Headers"] = ", ".join(CORS_EXPOSE_HEADERS)
        else:
            logger.warning(
                {"origin": origin, "allowed_origins": CORS_ALLOWED_ORIGINS},
                "CORS preflight request from disallowed origin"
            )
            response.headers["Access-Control-Allow-Origin"] = "*"
        
        return response
    
    path = request.url.path.rstrip("/")
    parts = path.split("/")
    offline_broker_action = (
        (path.startswith("/brokers/mt5/") and path != "/brokers/mt5/status")
        or path in {"/configs/brokers/test", "/api/backtest/fetch-historical-data"}
        or (request.method == "POST" and len(parts) == 4
            and parts[1] == "agents" and parts[3] in {"start", "run-now", "restart"})
    )
    if offline_mode() and offline_broker_action:
        response = JSONResponse(status_code=409, content={"detail": "Broker actions require connected mode."})
    else:
        response = await call_next(request)
    
    # Add CORS headers to all responses
    origin = request.headers.get("origin")
    if origin:
        if origin in CORS_ALLOWED_ORIGINS:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
        else:
            response.headers["Access-Control-Allow-Origin"] = "*"
    
    return response


class EquityPoint(BaseModel):
    time: datetime
    equity: float


class PnlPoint(BaseModel):
    time: datetime
    pnl: float


class BookedPnl(BaseModel):
    gross: float
    charges: float
    net: float


class AccountPnl(BaseModel):
    currency: str
    all_time: BookedPnl
    today: BookedPnl
    floating: float
    daily_curve: list[PnlPoint]


class SystemMetrics(BaseModel):
    account_pnl: AccountPnl | None = None
    equity_curve: list[EquityPoint]
    daily_pnl: list[PnlPoint]
    win_rate: float
    latency_p50: float | None = None
    latency_p95: float | None = None
    token_spend: float | None = None


class HealthItem(BaseModel):
    broker: bool


class HealthResponse(BaseModel):
    time: datetime
    status: dict[str, HealthItem]


class Readiness(BaseModel):
    ready: bool
    blockers: dict[str, str]
    time: datetime | None = None


class FlagUpdate(BaseModel):
    value: bool


class Mt5Config(BaseModel):
    server: str
    loginId: str
    password: str
    walletUrl: str


class BrokerTestPayload(BaseModel):
    broker: str
    config: dict[str, Any]


class AgentEvent(BaseModel):
    """Payload schema for real-time agent events."""

    asset: str
    type: str
    data: dict[str, Any] | None = None


class LogLine(BaseModel):
    """Schema for streamed log entries."""

    time: datetime | None = None
    level: str | None = None
    asset: str | None = None
    msg: str | None = None

    class Config:
        extra = "allow"


class LogFileInfo(BaseModel):
    """Information about an available log file."""
    filename: str
    path: str
    size: int
    modified: datetime


class LogFilesResponse(BaseModel):
    """Response containing available log files."""
    files: list[LogFileInfo]


class LogHistoryResponse(BaseModel):
    """Response containing historical log entries."""
    logs: list[dict[str, Any]]
    total_entries: int


class Candle(BaseModel):
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None


class CandlesResponse(BaseModel):
    candles: list[Candle]
    nextCursor: str | None = None


class JournalListResponse(BaseModel):
    entries: list[dict[str, Any]]
    nextCursor: str | None = None


def _parse_fixture(name: str) -> list[dict[str, Any]] | None:
    data = os.getenv(name)
    if not data:
        return None
    try:
        return json.loads(data)
    except Exception:  # pragma: no cover - defensive
        return None


_FAKE_TRADES = _parse_fixture("JOURNAL_TRADES_FIXTURE")
_FAKE_CANDLES = _parse_fixture("JOURNAL_CANDLES_FIXTURE")


@app.on_event("startup")
async def startup_event():
    app.state.flags = {}
    app.state.event_subs: Set[asyncio.Queue] = set()
    app.state.log_subs: Set[asyncio.Queue] = set()
    app.state.broker_tokens = {}
    app.state.oauth_states = {}
    # CandleStore removed - no longer storing candles in MongoDB
    # Short-TTL cache for the broker unrealized-P&L read in /metrics/agents.
    app.state._unrealized_cache = None

    # Ensure the logs directory exists
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    # Keep a reference to the in-memory logs for historical access
    app.state.memory_logs = []

    # Initialize structured logger and file logging
    logger.start_logger()
    path = logger.start_file_logging()
    # Emit a startup breadcrumb to confirm logging is working
    logger.info(
        {"service": "api"}, "startup", log_file=str(path),
        release_sha=os.getenv("TRADELENS_RELEASE_SHA") or None,
    )

    def _log_sink(rec: dict[str, Any]) -> None:
        payload = rec.copy()
        payload["time"] = payload.pop("ts", None)
        # Only set level if it's not already present
        if "level" not in payload:
            payload["level"] = payload.pop("lvl", "").lower()
        else:
            # Remove lvl if it exists to avoid confusion
            payload.pop("lvl", None)
        ctx = payload.get("ctx")
        if isinstance(ctx, dict) and "asset" in ctx and "asset" not in payload:
            payload["asset"] = ctx["asset"]
        
        # Store in memory for recent history access (limit to 10000 entries)
        app.state.memory_logs.append(payload)
        if len(app.state.memory_logs) > 10000:
            app.state.memory_logs.pop(0)
            
        _broadcast(app.state.log_subs, payload)

    previous_sink = getattr(app.state, "log_sink", None)
    if previous_sink is not None:
        logger.remove_sink(previous_sink)
    app.state.log_sink = _log_sink
    logger.add_sink(_log_sink)

    class _StdLogHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - simple forwarding
            rec: dict[str, Any] = {
                "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                "lvl": record.levelname,
                "msg": record.getMessage(),
                "ctx": {
                    k: v
                    for k, v in record.__dict__.items()
                    if k
                    not in {
                        "name",
                        "msg",
                        "args",
                        "levelname",
                        "levelno",
                        "level",
                        "pathname",
                        "filename",
                        "module",
                        "exc_info",
                        "exc_text",
                        "stack_info",
                        "lineno",
                        "funcName",
                        "created",
                        "msecs",
                        "relativeCreated",
                        "thread",
                        "threadName",
                        "processName",
                        "process",
                    }
                },
            }
            # Also persist to the durable daily log file. Live-trading components
            # (code_sniper_monitor, risk guards, mt5_adapter, trade_monitor, …)
            # log via stdlib ``logging`` and reach the frontend through this
            # handler; without this they would stream but never be stored. The
            # call is non-blocking (queued to a background writer), so it adds no
            # latency to the trading thread.
            logger.store_record(rec)
            app.state.log_sink(rec)

    root_logger = logging.getLogger()
    old_handler = getattr(app.state, "stdlib_log_handler", None)
    if old_handler is not None:
        root_logger.removeHandler(old_handler)
        old_handler.close()
    app.state.stdlib_log_handler = _StdLogHandler()
    root_logger.addHandler(app.state.stdlib_log_handler)
    root_logger.setLevel(logging.INFO)

    # Auto-connect the shared MT5 link from the local cred cache so the dashboard
    # reflects the live connection immediately (no manual "Connect" click) and a
    # background keepalive reconnects on any drop. Skipped under pytest so the
    # test suite never opens a real MetaTrader5 session on this machine.
    if (not offline_mode() and not os.getenv("PYTEST_CURRENT_TEST")
            and os.getenv("TRADELENS_MT5_AUTOCONNECT", "true").lower() == "true"):
        try:
            await mt5_router.startup_autoconnect(app)
        except Exception as exc:  # pragma: no cover - never crash startup
            logger.error({"service": "api"}, "MT5 startup auto-connect failed", error=str(exc))

        # Background equity sampler → real intraday equity curve for the
        # dashboard. Runs off the request/trade path (no added latency) and is
        # fully failure-isolated. Independent of MT5 cred presence so the curve
        # still populates before the first connect.
        try:
            mt5_router.launch_equity_sampler(app)
        except Exception as exc:  # pragma: no cover - never crash startup
            logger.error({"service": "api"}, "equity sampler launch failed", error=str(exc))


@app.on_event("shutdown")
async def shutdown_event():
    task = getattr(app.state, "mt5_keepalive_task", None)
    if task is not None:
        task.cancel()
    sampler = getattr(app.state, "mt5_equity_sampler_task", None)
    if sampler is not None:
        sampler.cancel()
    sink = getattr(app.state, "log_sink", None)
    if sink:
        logger.remove_sink(sink)
    handler = getattr(app.state, "stdlib_log_handler", None)
    if handler is not None:
        logging.getLogger().removeHandler(handler)
        handler.close()
        app.state.stdlib_log_handler = None
    logger.shutdown_logger()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ist_day_start_utc() -> datetime:
    """UTC instant of the most recent IST (UTC+5:30) midnight.

    This is the 'current trading day' boundary used by the dashboard's
    today-scoped aggregations (equity curve, daily P&L, win rate, token spend).
    """
    ist = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(ist)
    midnight_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight_ist.astimezone(timezone.utc)


CONFIG_ENV = os.getenv("CONFIG_ENV", "dev")

_BROKER_CFG_MODELS = {
    "mt5": BrokerConfigMt5,
}

_CFG_MODELS = {
    "gpt": GptConfig,
    "flags": FeatureFlagsConfig,
    "admin": AdminConfig,
    "ui": UiPrefsConfig,
}

SECRET_PLACEHOLDER = "********"
_SECRET_FIELDS = {"token", "password", "api_key"}


def _redact_secrets(data: Any) -> Any:
    if isinstance(data, Mapping):
        return {
            k: (
                SECRET_PLACEHOLDER
                if k in _SECRET_FIELDS and isinstance(v, str)
                else _redact_secrets(v)
            )
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [_redact_secrets(v) for v in data]
    return data


async def _get_config(domain: str, env: str) -> Any:
    if domain == "agents":
        try:
            cfg, _ = await config_repo.get_agents_config(env)
        except Exception:
            cfg = None
        return _redact_secrets(cfg.model_dump() if cfg else {})
    try:
        doc, _ = await config_repo.get_current(domain, env)
    except Exception:
        doc = None
    if domain == "brokers":
        result: dict[str, Any] = {}
        if isinstance(doc, Mapping):
            for name, model in _BROKER_CFG_MODELS.items():
                if name in doc:
                    try:
                        cfg = model.model_validate(doc[name], strict=False)
                    except ValidationError:
                        logger.error(
                            {"broker": name},
                            "invalid broker config",
                            exc_info=True,
                        )
                        continue
                    payload = cfg.model_dump(by_alias=True)
                    result[name] = payload
        return _redact_secrets(result)
    if domain in _CFG_MODELS:
        if not doc:
            return {}
        try:
            cfg = _CFG_MODELS[domain].model_validate(doc, strict=False)
        except ValidationError:
            raise HTTPException(status_code=500, detail="stored config invalid")
        return _redact_secrets(cfg.model_dump())
    return _redact_secrets(doc or {})


async def _put_config(domain: str, cfg: Any, env: str) -> None:
    if domain == "agents":
        if not isinstance(cfg, Mapping):
            raise HTTPException(status_code=400, detail="config must be an object")
        try:
            agent_cfg = AgentConfig.model_validate(cfg, strict=False)
        except ValidationError as exc:
            errs = exc.errors()
            for err in errs:
                ctx = err.get("ctx")
                if isinstance(ctx, dict) and isinstance(ctx.get("error"), Exception):
                    ctx["error"] = str(ctx["error"])
            raise HTTPException(status_code=400, detail=errs)
        except ValueError as exc:  # from custom validators
            raise HTTPException(status_code=400, detail=str(exc))
        await config_repo.put_agents_config(agent_cfg, env)
        return
    if domain == "brokers":
        if not isinstance(cfg, Mapping):
            raise HTTPException(status_code=400, detail="config must be an object")
        payload: dict[str, Any] = {}
        for name, val in cfg.items():
            model = _BROKER_CFG_MODELS.get(name)
            if model is None:
                raise HTTPException(status_code=400, detail=f"unknown broker '{name}'")
            try:
                model_cfg = model.model_validate(val, strict=False)
            except ValidationError as exc:
                raise HTTPException(status_code=400, detail=exc.errors(include_context=False))
            payload[name] = model_cfg.model_dump(by_alias=True)
        await config_repo.put(domain, env, payload)
        return
    if domain in _CFG_MODELS:
        if not isinstance(cfg, Mapping):
            raise HTTPException(status_code=400, detail="config must be an object")
        model = _CFG_MODELS[domain]
        try:
            model_cfg = model.model_validate(cfg, strict=False)
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=exc.errors(include_context=False))
        await config_repo.put(domain, env, model_cfg.model_dump())
        return
    if not isinstance(cfg, Mapping):
        raise HTTPException(status_code=400, detail="config must be an object")
    await config_repo.put(domain, env, cfg)


def _convert_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _normalize_trade(doc: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(doc)
    t = out.pop("time", None)
    if t is not None:
        out["timestamp"] = _convert_time(t).isoformat()
    for k, v in list(out.items()):
        if isinstance(v, datetime):
            out[k] = v.isoformat()
    return out


def _build_time_filter(
    start: datetime | None = None, end: datetime | None = None, cursor: datetime | None = None
) -> dict[str, Any] | None:
    cond: dict[str, Any] = {}
    if start:
        cond["$gte"] = start
    if end:
        cond["$lt"] = end
    if cursor:
        cond["$gt"] = cursor
    return cond or None


@app.get("/journal/list", response_model=JournalListResponse)
async def list_journal(
    asset: str | None = None,
    side: str | None = None,
    result: str | None = None,
    rr: float | None = None,
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = None,
    cursor: str | None = None,
    limit: int = Query(100, gt=0),
):
    cur_dt = datetime.fromisoformat(cursor) if cursor else None
    if _FAKE_TRADES is not None:
        docs = [d for d in _FAKE_TRADES]
        if asset:
            docs = [d for d in docs if d.get("asset") == asset]
        if side:
            docs = [d for d in docs if str(d.get("side")) == side]
        if result:
            docs = [d for d in docs if str(d.get("result")) == result]
        if rr is not None:
            docs = [d for d in docs if float(d.get("rr", 0)) == rr]
        if from_:
            docs = [d for d in docs if _convert_time(d["time"]) >= from_]
        if to:
            docs = [d for d in docs if _convert_time(d["time"]) < to]
        if cur_dt:
            docs = [d for d in docs if _convert_time(d["time"]) > cur_dt]
        docs.sort(key=lambda d: _convert_time(d["time"]))
    else:
        # Use comprehensive trades instead of old trades collection
        from packages.db.repositories import comprehensive_logging as comp_repo
        
        # Get comprehensive trades data
        if asset:
            docs = await comp_repo.get_comprehensive_trades(asset=asset, limit=limit + 1)
        else:
            docs = await comp_repo.get_all_comprehensive_trades(limit=limit + 1)
        
        # Apply filters
        filtered_docs = []
        for doc in docs:
            # Convert comprehensive trade to journal format
            journal_doc = {
                "asset": doc.get("asset"),
                "symbol": doc.get("symbol"),
                "side": doc.get("side"),
                "time": doc.get("trade_executed_at"),
                "timestamp": doc.get("trade_executed_at").isoformat() if doc.get("trade_executed_at") else None,
                "entry_price": doc.get("entry_price"),
                "exit_price": doc.get("exit_price"),
                "quantity": doc.get("quantity"),
                "pnl": doc.get("pnl"),
                "pattern": doc.get("pattern"),
                "confidence": doc.get("confidence"),
                "trade_status": doc.get("trade_status"),
                "result": doc.get("exit_reason") or "open",
                "stop_loss": doc.get("stop_loss"),
                "take_profit": doc.get("take_profit"),
                "agent_id": doc.get("agent_id"),
                "broker_type": doc.get("broker_type"),
                "trade_id": doc.get("_id"),
            }
            
            # Apply filters
            if asset and journal_doc.get("asset") != asset:
                continue
            if side and str(journal_doc.get("side")) != side:
                continue
            if result and str(journal_doc.get("result")) != result:
                continue
            if from_ and journal_doc.get("time") and journal_doc.get("time") < from_:
                continue
            if to and journal_doc.get("time") and journal_doc.get("time") >= to:
                continue
            if cur_dt and journal_doc.get("time") and journal_doc.get("time") <= cur_dt:
                continue
                
            filtered_docs.append(journal_doc)
        
        docs = filtered_docs
        docs.sort(key=lambda d: d.get("time") or datetime.min.replace(tzinfo=timezone.utc))

    next_cursor: str | None = None
    if len(docs) > limit:
        docs = docs[:limit]
        if docs and docs[-1].get("time"):
            next_cursor = _convert_time(docs[-1]["time"]).isoformat()
    entries = [_normalize_trade(d) for d in docs]
    return JournalListResponse(entries=entries, nextCursor=next_cursor)


@app.get("/journal/{asset}/candles", response_model=CandlesResponse)
async def get_candles_endpoint(
    asset: str,
    cursor: str | None = None,
    limit: int = Query(100, gt=0),
):
    cur_dt = datetime.fromisoformat(cursor) if cursor else None
    if _FAKE_CANDLES is not None:
        docs = [d for d in _FAKE_CANDLES if d.get("asset") == asset]
        if cur_dt:
            docs = [d for d in docs if _convert_time(d["time"]) > cur_dt]
        docs.sort(key=lambda d: _convert_time(d["time"]))
    else:
        start = cur_dt
        docs = await read.get_candles(asset, start=start, limit=limit + 1)

    next_cursor: str | None = None
    if len(docs) > limit:
        docs = docs[:limit]
        next_cursor = _convert_time(docs[-1]["time"]).isoformat()
    candles: list[Candle] = []
    for d in docs:
        ts = _convert_time(d["time"])
        candles.append(
            Candle(
                time=int(ts.timestamp()),
                open=float(d.get("open", 0)),
                high=float(d.get("high", 0)),
                low=float(d.get("low", 0)),
                close=float(d.get("close", 0)),
                volume=float(d.get("volume", 0)) if d.get("volume") is not None else None,
            )
        )
    return CandlesResponse(candles=candles, nextCursor=next_cursor)


@app.get("/journal/{asset}/trades", response_model=list[dict[str, Any]])
async def get_asset_trades(asset: str):
    if _FAKE_TRADES is not None:
        docs = [d for d in _FAKE_TRADES if d.get("asset") == asset]
        docs.sort(key=lambda d: _convert_time(d.get("time") or d.get("timestamp")))
        return [_normalize_trade(d) for d in docs]
    
    # Use comprehensive trades instead of in-memory trade events
    from packages.db.repositories import comprehensive_logging as comp_repo
    
    docs = await comp_repo.get_comprehensive_trades(asset=asset, limit=100)
    
    # Convert to trade events format for compatibility
    trade_events = []
    for doc in docs:
        event = {
            "asset": doc.get("asset"),
            "symbol": doc.get("symbol"),
            "trade_id": str(doc.get("_id")),
            "type": "trade_executed",
            "timestamp": doc.get("trade_executed_at").isoformat() if doc.get("trade_executed_at") else None,
            "agent_id": doc.get("agent_id"),
            "prompt_hash": doc.get("prompt_hash"),
            "entry_price": doc.get("entry_price"),
            "exit_price": doc.get("exit_price"),
            "side": doc.get("side"),
            "pattern": doc.get("pattern"),
            "confidence": doc.get("confidence"),
            "stop_loss": doc.get("stop_loss"),
            "take_profit": doc.get("take_profit"),
            "quantity": doc.get("quantity"),
            "pnl": doc.get("pnl"),
            "trade_status": doc.get("trade_status"),
            "broker_type": doc.get("broker_type"),
        }
        trade_events.append(event)
    
    trade_events.sort(key=lambda d: _convert_time(d["timestamp"]) if d.get("timestamp") else datetime.min.replace(tzinfo=timezone.utc))
    return trade_events


@app.get("/journal/{asset}/orders", response_model=JournalListResponse)
async def get_asset_orders(
    asset: str,
    cursor: str | None = None,
    limit: int = Query(100, gt=0),
):
    cur_dt = datetime.fromisoformat(cursor) if cursor else None
    docs = await read.get_orders(asset, start=cur_dt, limit=limit + 1)
    if cur_dt and docs and _convert_time(docs[0]["time"]) == cur_dt:
        docs = docs[1:]
    next_cursor: str | None = None
    if len(docs) > limit:
        docs = docs[:limit]
        next_cursor = _convert_time(docs[-1]["time"]).isoformat()
    entries = [_normalize_trade(d) for d in docs]
    return JournalListResponse(entries=entries, nextCursor=next_cursor)


@app.get("/journal/{asset}/fills", response_model=JournalListResponse)
async def get_asset_fills(
    asset: str,
    cursor: str | None = None,
    limit: int = Query(100, gt=0),
):
    cur_dt = datetime.fromisoformat(cursor) if cursor else None
    docs = await read.get_fills(asset, start=cur_dt, limit=limit + 1)
    if cur_dt and docs and _convert_time(docs[0]["time"]) == cur_dt:
        docs = docs[1:]
    next_cursor: str | None = None
    if len(docs) > limit:
        docs = docs[:limit]
        next_cursor = _convert_time(docs[-1]["time"]).isoformat()
    entries = [_normalize_trade(d) for d in docs]
    return JournalListResponse(entries=entries, nextCursor=next_cursor)


@app.get("/journal/{asset}/signals", response_model=JournalListResponse)
async def get_asset_signals(
    asset: str,
    cursor: str | None = None,
    limit: int = Query(100, gt=0),
):
    cur_dt = datetime.fromisoformat(cursor) if cursor else None
    docs = await read.list_signals(asset, start=cur_dt, limit=limit + 1)
    if cur_dt and docs and _convert_time(docs[0]["time"]) == cur_dt:
        docs = docs[1:]
    next_cursor: str | None = None
    if len(docs) > limit:
        docs = docs[:limit]
        next_cursor = _convert_time(docs[-1]["time"]).isoformat()
    entries = [_normalize_trade(d) for d in docs]
    return JournalListResponse(entries=entries, nextCursor=next_cursor)


# ---------------------------------------------------------------------------
# streaming utilities
#
# Heartbeat messages are JSON objects of the form
# ``{"type": "heartbeat", "time": "<ISO8601>"}`` and are emitted every
# 30 seconds to allow clients to detect stale connections.


def _register(subs: Set[asyncio.Queue]) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    subs.add(q)
    return q


def _unregister(subs: Set[asyncio.Queue], q: asyncio.Queue) -> None:
    subs.discard(q)


def _broadcast(subs: Set[asyncio.Queue], msg: dict[str, Any]) -> None:
    for q in list(subs):
        q.put_nowait(msg)


@app.get("/metrics", response_model=SystemMetrics)
async def system_metrics_endpoint():
    """System-level metrics for the dashboard, scoped to the current trading day.

    Sources (see plan): equity curve from persisted ``equity_samples`` (with an
    in-memory fallback), daily P&L + win rate from closed ``comprehensive_trades``,
    latency from the in-memory registry, token spend from ``gpt_responses``.
    Each source degrades to empty/zero independently so the endpoint never fails.
    """
    since = _ist_day_start_utc()

    # --- Equity curve (current day) --------------------------------------
    try:
        from packages.db.repositories.equity_samples import get_equity_samples
        samples = await get_equity_samples(since)
    except Exception:
        samples = []
    if not samples:
        buf = getattr(app.state, "equity_samples_mem", None)
        if buf:
            samples = [
                s for s in list(buf)
                if s.get("timestamp") is not None and to_aware_utc(s["timestamp"]) >= since
            ]
    equity_curve = [
        EquityPoint(time=to_aware_utc(s["timestamp"]), equity=float(s.get("equity") or 0.0))
        for s in samples
        if s.get("timestamp") is not None
    ]

    # --- Daily realized P&L curve + win rate (closed trades) -------------
    try:
        from packages.db.repositories.comprehensive_logging import (
            get_daily_pnl_curve,
            aggregate_realized_pnl,
        )
        pnl_points = await get_daily_pnl_curve(since)
        agg = await aggregate_realized_pnl(since=since)
    except Exception:
        pnl_points, agg = [], {}
    daily_pnl = [
        PnlPoint(time=to_aware_utc(p["time"]), pnl=float(p["pnl"]))
        for p in pnl_points
        if p.get("time") is not None
    ]
    wins = sum(v.get("wins", 0) for v in agg.values())
    losses = sum(v.get("losses", 0) for v in agg.values())
    decided = wins + losses
    win_rate = (wins / decided) if decided else 0.0

    # --- Latency from the in-memory registry (all agents combined) -------
    all_lat: list[float] = []
    for m in agent_metrics().values():
        all_lat.extend(m.latencies)
    if not all_lat:
        p50 = p95 = None
    elif len(all_lat) == 1:
        p50 = p95 = all_lat[0]
    else:
        qs = statistics.quantiles(all_lat, n=100, method="inclusive")
        p50, p95 = qs[49], qs[94]

    # --- Token spend (current day) ---------------------------------------
    try:
        from packages.db.repositories.gpt_responses import aggregate_token_spend
        token_spend = await aggregate_token_spend(since)
    except Exception:
        token_spend = 0.0

    account_pnl = None
    if not offline_mode() and await asyncio.to_thread(mt5_router.adapter.is_connected):
        try:
            account_pnl = await asyncio.to_thread(mt5_router.adapter.account_pnl_report, since)
        except Exception as exc:
            logger.warning({"endpoint": "metrics"}, f"MT5 account P&L unavailable: {exc}")
    return SystemMetrics(
        account_pnl=account_pnl,
        equity_curve=equity_curve,
        daily_pnl=daily_pnl,
        win_rate=win_rate,
        latency_p50=p50,
        latency_p95=p95,
        token_spend=token_spend,
    )


@app.get("/health", response_model=HealthResponse)
async def health_endpoint():
    return HealthResponse(time=_now(), status={})


@app.get("/readiness", response_model=Readiness)
async def readiness_endpoint():
    return Readiness(ready=True, blockers={}, time=_now())


@app.get("/flags", response_model=dict[str, bool])
async def get_flags():
    return app.state.flags


@app.post("/flags/{name}", response_model=dict[str, bool])
async def set_flag(name: str, upd: FlagUpdate):
    app.state.flags[name] = upd.value
    return {name: app.state.flags[name]}


# ---------------------------------------------------------------------------
# Log endpoints
# ---------------------------------------------------------------------------

@app.get("/logs/files", response_model=LogFilesResponse)
async def list_log_files():
    """Return a list of available log files."""
    log_files = []
    
    # Ensure the logs directory exists
    if not LOGS_DIR.exists():
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        
    # List all log files
    for path in LOGS_DIR.glob("*.log"):
        try:
            stat = path.stat()
            log_files.append(
                LogFileInfo(
                    filename=path.name,
                    path=str(path.relative_to(LOGS_DIR.parent.parent)),
                    size=stat.st_size,
                    modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                )
            )
        except Exception as e:
            logger.error({"service": "logs"}, f"Error reading log file info: {str(e)}")
    
    # Sort by modified time, newest first
    log_files.sort(key=lambda f: f.modified, reverse=True)
    return LogFilesResponse(files=log_files)


@app.get("/logs/history", response_model=LogHistoryResponse)
async def get_log_history(
    filename: str | None = None,
    limit: int = Query(1000, gt=0, le=10000),
    level: str | None = None,
    asset: str | None = None,
    search: str | None = None,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
):
    """Return historical log entries from files or memory."""
    logs = []
    total_entries = 0
    
    # First, check memory logs
    memory_logs = getattr(app.state, "memory_logs", [])
    if not filename:
        logs = memory_logs.copy()
    
    # If a filename is specified or we need more logs, read from file
    if filename or (not logs and not filename):
        try:
            # If no specific file is requested, use the most recent one
            if not filename:
                current_log = logger.get_current_log_file()
                if current_log and current_log.exists():
                    filename = current_log.name
                else:
                    # Find the most recent log file
                    log_files = list(LOGS_DIR.glob("*.log"))
                    if log_files:
                        log_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                        filename = log_files[0].name
            
            if filename:
                file_path = LOGS_DIR / filename
                if file_path.exists():
                    file_logs = []
                    with open(file_path, "r", encoding="utf-8") as f:
                        for line in f:
                            try:
                                record = json.loads(line.strip())
                                # Convert ts to time for consistency
                                if "ts" in record and "time" not in record:
                                    record["time"] = record.pop("ts")
                                # Convert lvl to level for consistency
                                if "lvl" in record and "level" not in record:
                                    record["level"] = record.pop("lvl").lower()
                                # Add record context to top level for easier filtering
                                ctx = record.get("ctx", {})
                                if isinstance(ctx, dict):
                                    if "asset" in ctx and "asset" not in record:
                                        record["asset"] = ctx["asset"]
                                file_logs.append(record)
                            except json.JSONDecodeError:
                                continue
                    
                    # If we're reading from file only, use these logs
                    if not logs:
                        logs = file_logs
                    # Otherwise append non-duplicate entries from file
                    else:
                        # Simple deduplication by checking message and timestamp
                        memory_times = {(log.get("time"), log.get("msg")) for log in logs}
                        for file_log in file_logs:
                            key = (file_log.get("time"), file_log.get("msg"))
                            if key not in memory_times:
                                logs.append(file_log)
        
        except Exception as e:
            logger.error({"service": "logs"}, f"Error reading log file: {str(e)}")
            # If we failed to read the file but have memory logs, continue with those
            if not logs:
                return LogHistoryResponse(logs=[], total_entries=0)
    
    total_entries = len(logs)
    
    # Apply filters
    if level:
        level_lower = level.lower()
        logs = [log for log in logs if log.get("level", "").lower() == level_lower]
    
    if asset:
        logs = [log for log in logs if log.get("asset") == asset]
    
    if search:
        search_lower = search.lower()
        logs = [
            log for log in logs 
            if (
                (log.get("msg") and search_lower in str(log.get("msg")).lower()) or
                (log.get("asset") and search_lower in str(log.get("asset")).lower()) or
                any(search_lower in str(v).lower() for k, v in log.items() if isinstance(v, str))
            )
        ]
    
    if from_time:
        logs = [
            log for log in logs 
            if log.get("time") and _convert_time(log["time"]) >= from_time
        ]
    
    if to_time:
        logs = [
            log for log in logs 
            if log.get("time") and _convert_time(log["time"]) <= to_time
        ]
    
    # Sort logs by time
    logs.sort(key=lambda log: _convert_time(log.get("time", "1970-01-01T00:00:00Z")))
    
    # Limit the number of logs returned
    if len(logs) > limit:
        logs = logs[-limit:]  # Return the most recent logs
    
    return LogHistoryResponse(logs=logs, total_entries=total_entries)


@app.post("/logs/filter")
async def filter_log_file(
    filename: str,
    level: str | None = None,
    asset: str | None = None,
    search: str | None = None,
    limit: int = 1000,
):
    """Filter logs from a specific file and return matching entries."""
    try:
        file_path = LOGS_DIR / filename
        if not file_path.exists():
            raise HTTPException(status_code=404, detail=f"Log file {filename} not found")
        
        filtered_logs = []
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    record = json.loads(line.strip())
                    # Convert ts to time for consistency
                    if "ts" in record and "time" not in record:
                        record["time"] = record.pop("ts")
                    # Convert lvl to level for consistency
                    if "lvl" in record and "level" not in record:
                        record["level"] = record.pop("lvl").lower()
                    
                    # Add record context to top level for easier filtering
                    ctx = record.get("ctx", {})
                    if isinstance(ctx, dict):
                        if "asset" in ctx and "asset" not in record:
                            record["asset"] = ctx["asset"]
                    
                    # Apply filters
                    if level and record.get("level", "").lower() != level.lower():
                        continue
                    
                    if asset and record.get("asset") != asset:
                        continue
                    
                    if search:
                        search_lower = search.lower()
                        msg_match = record.get("msg") and search_lower in str(record.get("msg")).lower()
                        asset_match = record.get("asset") and search_lower in str(record.get("asset")).lower()
                        other_match = any(search_lower in str(v).lower() for k, v in record.items() if isinstance(v, str))
                        if not (msg_match or asset_match or other_match):
                            continue
                    
                    filtered_logs.append(record)
                    if len(filtered_logs) >= limit:
                        break
                except json.JSONDecodeError:
                    continue
        
        return {"logs": filtered_logs, "total": len(filtered_logs)}
    except Exception as e:
        logger.error({"service": "logs"}, f"Error filtering log file: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error filtering log file: {str(e)}")


# ---------------------------------------------------------------------------
# streaming endpoints
#
# The legacy ``/stream`` endpoint was removed. Clients should use the explicit
# ``/events`` and ``/logs/stream`` routes below for real-time updates.


@app.post("/events")
async def post_event(evt: AgentEvent) -> Response:
    payload = evt.model_dump() if hasattr(evt, "model_dump") else evt.dict()
    _broadcast(app.state.event_subs, payload)
    return Response(status_code=204)


@app.get("/events")
async def events_sse() -> EventSourceResponse:
    queue = _register(app.state.event_subs)

    async def gen():
        try:
            while True:
                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=30)
                    yield {"data": json.dumps(evt, default=str)}
                except asyncio.TimeoutError:
                    hb = {"type": "heartbeat", "time": _now().isoformat()}
                    yield {"data": json.dumps(hb)}
        finally:
            _unregister(app.state.event_subs, queue)

    return EventSourceResponse(gen(), headers={"Cache-Control": "no-cache"})


@app.websocket("/events")
async def events_ws(ws: WebSocket) -> None:
    queue = _register(app.state.event_subs)
    await ws.accept()
    try:
        while True:
            try:
                evt = await asyncio.wait_for(queue.get(), timeout=30)
                await ws.send_text(json.dumps(evt, default=str))
            except asyncio.TimeoutError:
                hb = {"type": "heartbeat", "time": _now().isoformat()}
                await ws.send_text(json.dumps(hb))
    except WebSocketDisconnect:
        pass
    finally:
        _unregister(app.state.event_subs, queue)


@app.post("/logs")
async def post_log(line: LogLine) -> Response:
    payload = line.model_dump() if hasattr(line, "model_dump") else line.dict()
    _broadcast(app.state.log_subs, payload)
    return Response(status_code=204)


@app.get("/logs/stream")
async def logs_stream_sse() -> EventSourceResponse:
    queue = _register(app.state.log_subs)
    
    # Generate an initial log entry to confirm the connection is established
    initial_log = {
        "time": _now().isoformat(),
        "level": "info",
        "msg": "Log stream connection established",
    }
    queue.put_nowait(initial_log)

    async def gen():
        try:
            while True:
                try:
                    line = await asyncio.wait_for(queue.get(), timeout=30)
                    yield {"data": json.dumps(line, default=str)}
                except asyncio.TimeoutError:
                    hb = {"type": "heartbeat", "time": _now().isoformat()}
                    yield {"data": json.dumps(hb)}
        finally:
            _unregister(app.state.log_subs, queue)

    return EventSourceResponse(gen(), headers={"Cache-Control": "no-cache"})


@app.websocket("/logs/stream")
async def logs_stream_ws(ws: WebSocket) -> None:
    queue = _register(app.state.log_subs)
    await ws.accept()
    
    # Send an initial log entry to confirm the connection is established
    initial_log = {
        "time": _now().isoformat(),
        "level": "info",
        "msg": "Log stream connection established",
    }
    await ws.send_text(json.dumps(initial_log, default=str))
    
    try:
        while True:
            try:
                line = await asyncio.wait_for(queue.get(), timeout=30)
                await ws.send_text(json.dumps(line, default=str))
            except asyncio.TimeoutError:
                hb = {"type": "heartbeat", "time": _now().isoformat()}
                await ws.send_text(json.dumps(hb))
    except WebSocketDisconnect:
        pass
    finally:
        _unregister(app.state.log_subs, queue)


@app.post("/configs/brokers/test")
async def test_broker_settings(payload: BrokerTestPayload):
    try:
        broker = create_broker(payload.broker)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        await broker.connect(**payload.config)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "ok"}


ALLOWED_CONFIG_DOMAINS = {
    "agents",
    "brokers",
    "gpt",
    "flags",
    "admin",
    "ui",
}


@app.get("/configs/agents", response_model=AgentConfig | dict[str, Any])
async def get_agents_config(
    env: str = Query(CONFIG_ENV), agent_id: str | None = None
):
    """Return the ``AgentConfig`` for the given environment or agent."""

    if agent_id:
        try:
            cfg, _ = await config_repo.get_agent_config(agent_id, env)
        except Exception:
            cfg = None
        return _redact_secrets(cfg.model_dump() if cfg else {})
    return await _get_config("agents", env)


@app.put("/configs/agents")
async def put_agents_config(
    cfg: Mapping[str, Any],
    env: str = Query(CONFIG_ENV),
    agent_id: str | None = None,
):
    """Persist *cfg* for the given environment or agent."""

    if agent_id:
        if not isinstance(cfg, Mapping):
            raise HTTPException(status_code=400, detail="config must be an object")
        try:
            agent_cfg = AgentConfig.model_validate(cfg, strict=False)
        except ValidationError as exc:
            errs = exc.errors()
            for err in errs:
                ctx = err.get("ctx")
                if isinstance(ctx, dict) and isinstance(ctx.get("error"), Exception):
                    ctx["error"] = str(ctx["error"])
            raise HTTPException(status_code=400, detail=errs)
        await config_repo.put_agent_config(agent_id, agent_cfg, env)
        return {"status": "ok"}
    await _put_config("agents", cfg, env)
    return {"status": "ok"}


def _ensure_domain(domain: str) -> str:
    if domain not in ALLOWED_CONFIG_DOMAINS:
        raise HTTPException(status_code=404, detail="unknown config domain")
    return domain


@app.get("/configs/{domain}")
async def get_config_domain(domain: str, env: str = Query(CONFIG_ENV)):
    domain = _ensure_domain(domain)
    return await _get_config(domain, env)


@app.put("/configs/{domain}")
async def put_config_domain(domain: str, cfg: Any = Body(...), env: str = Query(CONFIG_ENV)):
    domain = _ensure_domain(domain)
    await _put_config(domain, cfg, env)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# GPT Analysis endpoints
# ---------------------------------------------------------------------------


@app.get("/api/test-gpt")
async def test_gpt_endpoint():
    """Test endpoint to verify GPT functionality."""
    try:
        # Test basic MongoDB connection
        client = get_client()
        db = client[MONGO_DB_NAME]
        collections = await db.list_collection_names()
        
        # Check data counts
        gpt_responses_count = 0
        trigger_checks_count = 0
        call_summaries_count = 0
        
        if "gpt_responses" in collections:
            gpt_responses_count = await db["gpt_responses"].count_documents({})
        if "trigger_checks" in collections:
            trigger_checks_count = await db["trigger_checks"].count_documents({})
        if "gpt_call_summaries" in collections:
            call_summaries_count = await db["gpt_call_summaries"].count_documents({})
        
        return {
            "status": "ok",
            "collections": collections,
            "gpt_responses_exists": "gpt_responses" in collections,
            "trigger_checks_exists": "trigger_checks" in collections,
            "gpt_call_summaries_exists": "gpt_call_summaries" in collections,
            "gpt_responses_count": gpt_responses_count,
            "trigger_checks_count": trigger_checks_count,
            "call_summaries_count": call_summaries_count
        }
    except Exception as e:
        logger.error({"endpoint": "test"}, f"Error in test endpoint: {str(e)}")
        return {"status": "error", "message": str(e)}


@app.get("/api/gpt-responses-simple")
async def get_gpt_responses_simple():
    """Simple test endpoint for GPT responses."""
    try:
        # Direct MongoDB query
        client = get_client()
        db = client[MONGO_DB_NAME]
        collection = db["gpt_responses"]
        
        # Get first 5 documents
        cursor = collection.find({}).limit(5)
        responses = await cursor.to_list(length=None)
        
        # Convert ObjectId and datetime to strings for JSON serialization
        for response in responses:
            if '_id' in response:
                response['_id'] = str(response['_id'])
            if 'timestamp' in response:
                response['timestamp'] = response['timestamp'].isoformat()
        
        return {"responses": responses, "count": len(responses)}
    except Exception as e:
        logger.error({"endpoint": "gpt-responses"}, f"Error in simple GPT responses: {str(e)}")
        return {"error": str(e), "responses": [], "count": 0}


@app.get("/api/gpt-responses-debug")
async def get_gpt_responses_debug():
    """Debug endpoint for GPT responses."""
    try:
        # Test step by step
        client = get_client()
        db = client[MONGO_DB_NAME]
        
        # Test collection access
        collection = db["gpt_responses"]
        
        # Test count
        count = await collection.count_documents({})
        
        # Test find
        cursor = collection.find({}).limit(1)
        first_doc = await cursor.to_list(length=1)
        
        return {
            "status": "ok",
            "count": count,
            "first_doc": first_doc[0] if first_doc else None
        }
    except Exception as e:
        logger.error({"endpoint": "gpt-responses"}, f"Error in debug GPT responses: {str(e)}")
        return {"error": str(e), "traceback": str(e.__traceback__)}


@app.get("/api/gpt-responses-test")
async def get_gpt_responses_test():
    """Test endpoint with static data."""
    return {
        "status": "ok",
        "responses": [
            {
                "_id": "test_id_1",
                "asset": "NIFTY",
                "agent_id": "test_agent",
                "timestamp": "2025-01-01T00:00:00Z",
                "response_content": "Test response",
                "confidence": 0.85
            }
        ],
        "count": 1
    }


@app.get("/api/gpt-responses-debug-count")
async def get_gpt_responses_debug_count():
    """Debug endpoint to check database count."""
    try:
        from packages.db.repositories import gpt_responses as gpt_repo
        
        # Test direct database access
        collection = await gpt_repo.get_gpt_responses_collection()
        count = await collection.count_documents({})
        
        # Test our repository function
        responses = await gpt_repo.get_all_gpt_responses(limit=5, skip=0)
        
        return {
            "status": "ok",
            "total_count": count,
            "sample_responses": len(responses),
            "sample_data": responses[:2] if responses else []
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "traceback": str(e.__traceback__)
        }


@app.get("/api/gpt-responses")
async def get_gpt_responses(
    asset: str = Query(None, description="Asset to filter by (or 'ALL' for all assets)"),
    limit: int = Query(50, ge=1, le=1000, description="Number of records to return"),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    start_time: Optional[str] = Query(None, description="Start time filter (ISO format)"),
    end_time: Optional[str] = Query(None, description="End time filter (ISO format)"),
):
    """Retrieve GPT responses from the database."""
    try:
        logger.info({"endpoint": "gpt-responses"}, "Fetching GPT responses", asset=asset, limit=limit, skip=skip)
        
        # Parse time filters if provided
        start_dt = None
        end_dt = None
        if start_time:
            start_dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
        if end_time:
            end_dt = datetime.fromisoformat(end_time.replace('Z', '+00:00'))
        
        if asset and asset != "ALL":
            # Get responses for specific asset
            logger.info({"endpoint": "gpt-responses"}, "Fetching responses for asset", asset=asset)
            responses = await gpt_repo.get_gpt_responses_by_asset(
                asset=asset,
                limit=limit,
                skip=skip,
                start_time=start_dt,
                end_time=end_dt
            )
        else:
            # Get responses for all assets
            logger.info({"endpoint": "gpt-responses"}, "Fetching responses for all assets")
            responses = await gpt_repo.get_all_gpt_responses(
                limit=limit,
                skip=skip,
                start_time=start_dt,
                end_time=end_dt
            )
        
        logger.info({"endpoint": "gpt-responses"}, "Retrieved responses from database", count=len(responses))
        
        # Convert ObjectId and datetime to strings for JSON serialization
        for response in responses:
            if '_id' in response:
                response['_id'] = str(response['_id'])
            if 'timestamp' in response and hasattr(response['timestamp'], 'isoformat'):
                response['timestamp'] = response['timestamp'].isoformat()
        
        logger.info({"endpoint": "gpt-responses"}, "Returning responses to client", count=len(responses))
        return {"responses": responses, "count": len(responses)}
    except Exception as e:
        logger.error({"endpoint": "gpt-responses"}, "Error fetching GPT responses", error=str(e))
        return {"error": str(e), "responses": [], "count": 0}


@app.delete("/api/gpt-responses/{response_id}")
async def delete_gpt_response(response_id: str):
    """Delete a GPT response by ID."""
    try:
        from bson import ObjectId
        
        # Validate ObjectId format
        try:
            ObjectId(response_id)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid response ID format")
        
        # Delete the response
        deleted = await gpt_repo.delete_gpt_response(response_id)
        
        if not deleted:
            raise HTTPException(status_code=404, detail=f"GPT response with ID {response_id} not found")
        
        logger.info({"endpoint": "gpt-responses"}, f"Deleted GPT response {response_id}")
        return {"message": f"GPT response {response_id} deleted successfully"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error({"endpoint": "gpt-responses"}, f"Error deleting GPT response {response_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to delete GPT response: {str(e)}")


@app.get("/api/gpt-responses/{asset}")
async def get_gpt_responses_by_asset(
    asset: str,
    limit: int = Query(50, ge=1, le=1000, description="Number of records to return"),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    start_time: Optional[str] = Query(None, description="Start time filter (ISO format)"),
    end_time: Optional[str] = Query(None, description="End time filter (ISO format)"),
):
    """Retrieve GPT responses for a specific asset."""
    try:
        # Parse time filters if provided
        start_dt = None
        end_dt = None
        if start_time:
            start_dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
        if end_time:
            end_dt = datetime.fromisoformat(end_time.replace('Z', '+00:00'))
        
        responses = await gpt_repo.get_gpt_responses_by_asset(
            asset=asset,
            limit=limit,
            skip=skip,
            start_time=start_dt,
            end_time=end_dt
        )
        
        return {"responses": responses, "count": len(responses)}
    except Exception as e:
        logger.error({"endpoint": "gpt-responses"}, f"Error fetching GPT responses for {asset}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error fetching GPT responses: {str(e)}")


@app.get("/api/trigger-checks")
async def get_trigger_checks(
    asset: str = Query(None, description="Asset to filter by (or 'ALL' for all assets)"),
    limit: int = Query(50, ge=1, le=1000, description="Number of records to return"),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    start_time: Optional[str] = Query(None, description="Start time filter (ISO format)"),
    end_time: Optional[str] = Query(None, description="End time filter (ISO format)"),
):
    """Retrieve trigger checks from the database."""
    try:
        # Parse time filters if provided
        start_dt = None
        end_dt = None
        if start_time:
            start_dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
        if end_time:
            end_dt = datetime.fromisoformat(end_time.replace('Z', '+00:00'))
        
        if asset and asset != "ALL":
            # Get trigger checks for specific asset
            checks = await gpt_repo.get_trigger_checks_by_asset(
                asset=asset,
                limit=limit,
                skip=skip,
                start_time=start_dt,
                end_time=end_dt
            )
        else:
            # Get trigger checks for all assets
            checks = await gpt_repo.get_all_trigger_checks(
                limit=limit,
                skip=skip,
                start_time=start_dt,
                end_time=end_dt
            )
        
        # Convert ObjectId and datetime to strings for JSON serialization
        for check in checks:
            if '_id' in check:
                check['_id'] = str(check['_id'])
            if 'timestamp' in check:
                check['timestamp'] = check['timestamp'].isoformat()
            if 'trigger_time' in check and check['trigger_time'] is not None:
                check['trigger_time'] = check['trigger_time'].isoformat()
        
        return {"checks": checks, "count": len(checks)}
    except Exception as e:
        logger.error({"endpoint": "trigger-checks"}, f"Error fetching trigger checks: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error fetching trigger checks: {str(e)}")


@app.delete("/api/trigger-checks/{trigger_id}")
async def delete_trigger_check(trigger_id: str):
    """Delete a trigger check by ID."""
    try:
        from bson import ObjectId
        
        # Validate ObjectId format
        try:
            ObjectId(trigger_id)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid trigger ID format")
        
        # Delete the trigger check
        deleted = await gpt_repo.delete_trigger_check(trigger_id)
        
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Trigger check with ID {trigger_id} not found")
        
        logger.info({"endpoint": "trigger-checks"}, f"Deleted trigger check {trigger_id}")
        return {"message": f"Trigger check {trigger_id} deleted successfully"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error({"endpoint": "trigger-checks"}, f"Error deleting trigger check {trigger_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to delete trigger check: {str(e)}")


@app.get("/api/trigger-checks/{asset}")
async def get_trigger_checks_by_asset(
    asset: str,
    limit: int = Query(50, ge=1, le=1000, description="Number of records to return"),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    start_time: Optional[str] = Query(None, description="Start time filter (ISO format)"),
    end_time: Optional[str] = Query(None, description="End time filter (ISO format)"),
):
    """Retrieve trigger checks for a specific asset."""
    try:
        # Parse time filters if provided
        start_dt = None
        end_dt = None
        if start_time:
            start_dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
        if end_time:
            end_dt = datetime.fromisoformat(end_time.replace('Z', '+00:00'))
        
        checks = await gpt_repo.get_trigger_checks_by_asset(
            asset=asset,
            limit=limit,
            skip=skip,
            start_time=start_dt,
            end_time=end_dt
        )
        
        # Convert ObjectId and datetime to strings for JSON serialization
        for check in checks:
            if '_id' in check:
                check['_id'] = str(check['_id'])
            if 'timestamp' in check:
                check['timestamp'] = check['timestamp'].isoformat()
            if 'trigger_time' in check and check['trigger_time'] is not None:
                check['trigger_time'] = check['trigger_time'].isoformat()
        
        return {"checks": checks, "count": len(checks)}
    except Exception as e:
        logger.error({"endpoint": "trigger-checks"}, f"Error fetching trigger checks for {asset}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error fetching trigger checks: {str(e)}")


@app.get("/api/gpt-call-summaries")
async def get_gpt_call_summaries(
    asset: str = Query(None, description="Asset to filter by (or 'ALL' for all assets)"),
    limit: int = Query(50, ge=1, le=1000, description="Number of records to return"),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    start_time: Optional[str] = Query(None, description="Start time filter (ISO format)"),
    end_time: Optional[str] = Query(None, description="End time filter (ISO format)"),
):
    """Retrieve GPT call summaries from the database."""
    try:
        # Parse time filters if provided
        start_dt = None
        end_dt = None
        if start_time:
            start_dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
        if end_time:
            end_dt = datetime.fromisoformat(end_time.replace('Z', '+00:00'))
        
        if asset and asset != "ALL":
            # Get call summaries for specific asset
            summaries = await gpt_repo.get_gpt_call_summaries_by_asset(
                asset=asset,
                limit=limit,
                skip=skip,
                start_time=start_dt,
                end_time=end_dt
            )
        else:
            # Get call summaries for all assets
            summaries = await gpt_repo.get_all_gpt_call_summaries(
                limit=limit,
                skip=skip,
                start_time=start_dt,
                end_time=end_dt
            )
        
        # Convert ObjectId and datetime to strings for JSON serialization
        for summary in summaries:
            if '_id' in summary:
                summary['_id'] = str(summary['_id'])
            if 'timestamp' in summary:
                summary['timestamp'] = summary['timestamp'].isoformat()
        
        return {"summaries": summaries, "count": len(summaries)}
    except Exception as e:
        logger.error({"endpoint": "gpt-call-summaries"}, f"Error fetching GPT call summaries: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error fetching GPT call summaries: {str(e)}")


@app.get("/api/gpt-call-summaries/{asset}")
async def get_gpt_call_summaries_by_asset(
    asset: str,
    limit: int = Query(50, ge=1, le=1000, description="Number of records to return"),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    start_time: Optional[str] = Query(None, description="Start time filter (ISO format)"),
    end_time: Optional[str] = Query(None, description="End time filter (ISO format)"),
):
    """Retrieve GPT call summaries for a specific asset."""
    try:
        # Parse time filters if provided
        start_dt = None
        end_dt = None
        if start_time:
            start_dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
        if end_time:
            end_dt = datetime.fromisoformat(end_time.replace('Z', '+00:00'))
        
        summaries = await gpt_repo.get_gpt_call_summaries_by_asset(
            asset=asset,
            limit=limit,
            skip=skip,
            start_time=start_dt,
            end_time=end_dt
        )
        
        return {"summaries": summaries, "count": len(summaries)}
    except Exception as e:
        logger.error({"endpoint": "gpt-call-summaries"}, f"Error fetching GPT call summaries for {asset}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error fetching GPT call summaries: {str(e)}")


# ---------------------------------------------------------------------------
# Multi-Expert Cycles endpoints
# ---------------------------------------------------------------------------


@app.get("/api/multi-expert/cycles")
async def get_multi_expert_cycles(
    agent_id: str | None = Query(None, description="Filter by agent ID"),
    asset: str | None = Query(None, description="Filter by asset"),
    mode: str | None = Query(None, description="Filter by mode (multi_expert, normal)"),
    status: str | None = Query(None, description="Filter by status (success, failed, skipped)"),
    start_time: Optional[str] = Query(None, description="Start time filter (ISO format)"),
    end_time: Optional[str] = Query(None, description="End time filter (ISO format)"),
    limit: int = Query(50, ge=1, le=1000, description="Number of records to return"),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
):
    """Retrieve multi-expert cycles with optional filters."""
    try:
        from packages.db.repositories.multi_expert_cycles import multi_expert_cycles_repo
        
        # Parse time filters if provided
        start_dt = None
        end_dt = None
        if start_time:
            start_dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
        if end_time:
            end_dt = datetime.fromisoformat(end_time.replace('Z', '+00:00'))
        
        cycles = await multi_expert_cycles_repo.list_cycles(
            agent_id=agent_id,
            asset=asset,
            mode=mode,
            status=status,
            start_time=start_dt,
            end_time=end_dt,
            limit=limit,
            skip=skip
        )
        
        # Convert to dict format for JSON serialization
        cycles_data = []
        for cycle in cycles:
            cycle_dict = cycle.model_dump()
            # Convert datetime objects to ISO strings
            if cycle_dict.get('started_at'):
                cycle_dict['started_at'] = cycle_dict['started_at'].isoformat()
            if cycle_dict.get('completed_at'):
                cycle_dict['completed_at'] = cycle_dict['completed_at'].isoformat()
            cycles_data.append(cycle_dict)
        
        return {"cycles": cycles_data, "count": len(cycles_data)}
        
    except Exception as e:
        logger.error({"endpoint": "multi-expert-cycles"}, f"Error fetching multi-expert cycles: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error fetching cycles: {str(e)}")


@app.get("/api/multi-expert/cycles/{cycle_id}")
async def get_multi_expert_cycle(cycle_id: str):
    """Retrieve a specific multi-expert cycle by ID."""
    try:
        from packages.db.repositories.multi_expert_cycles import multi_expert_cycles_repo
        
        cycle = await multi_expert_cycles_repo.get_cycle(cycle_id)
        
        if not cycle:
            raise HTTPException(status_code=404, detail=f"Cycle {cycle_id} not found")
        
        # Convert to dict format for JSON serialization
        cycle_dict = cycle.model_dump()
        # Convert datetime objects to ISO strings
        if cycle_dict.get('started_at'):
            cycle_dict['started_at'] = cycle_dict['started_at'].isoformat()
        if cycle_dict.get('completed_at'):
            cycle_dict['completed_at'] = cycle_dict['completed_at'].isoformat()
        
        return cycle_dict
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error({"endpoint": "multi-expert-cycles"}, f"Error fetching multi-expert cycle {cycle_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error fetching cycle: {str(e)}")


@app.delete("/api/multi-expert/cycles/{cycle_id}")
async def delete_multi_expert_cycle(cycle_id: str):
    """Delete a multi-expert cycle by ID."""
    try:
        from packages.db.repositories.multi_expert_cycles import multi_expert_cycles_repo
        
        # Delete the cycle
        deleted = await multi_expert_cycles_repo.delete_cycle(cycle_id)
        
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Multi-expert cycle {cycle_id} not found")
        
        logger.info({"endpoint": "multi-expert-cycles"}, f"Deleted multi-expert cycle {cycle_id}")
        return {"message": f"Multi-expert cycle {cycle_id} deleted successfully"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error({"endpoint": "multi-expert-cycles"}, f"Error deleting multi-expert cycle {cycle_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to delete multi-expert cycle: {str(e)}")


@app.post("/api/export/multi-expert-cycles")
async def export_multi_expert_cycles(
    asset: str = Query('ALL', description="Asset to filter by (or 'ALL' for all assets)"),
    format: str = Query('json', description="Export format (json, csv)"),
    start_time: Optional[str] = Query(None, description="Start time filter (ISO format)"),
    end_time: Optional[str] = Query(None, description="End time filter (ISO format)"),
):
    """Export multi-expert cycles data."""
    try:
        from packages.db.repositories.multi_expert_cycles import multi_expert_cycles_repo
        import csv
        import json as json_lib
        from datetime import datetime
        
        # Parse time filters if provided
        start_dt = None
        end_dt = None
        if start_time:
            start_dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
        if end_time:
            end_dt = datetime.fromisoformat(end_time.replace('Z', '+00:00'))
        
        # Get all cycles (with large limit for export)
        cycles = await multi_expert_cycles_repo.list_cycles(
            asset=asset if asset != 'ALL' else None,
            start_time=start_dt,
            end_time=end_dt,
            limit=10000,  # Large limit for export
            skip=0
        )
        
        # Generate filename
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if format.lower() == 'csv':
            filename = f"multi_expert_cycles_{asset}_{timestamp}.csv"
            filepath = f"exports/{filename}"
            
            # Create exports directory if it doesn't exist
            import os
            os.makedirs("exports", exist_ok=True)
            
            # Write CSV
            with open(filepath, 'w', newline='', encoding='utf-8') as csvfile:
                if cycles:
                    # Flatten cycle data for CSV
                    fieldnames = [
                        'cycle_id', 'mode', 'agent_id', 'asset', 'symbol', 
                        'started_at', 'completed_at', 'status', 'error',
                        'experts_count', 'valid_proposals', 'selected_expert'
                    ]
                    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                    writer.writeheader()
                    
                    for cycle in cycles:
                        row = {
                            'cycle_id': cycle.cycle_id,
                            'mode': cycle.mode,
                            'agent_id': cycle.agent_id,
                            'asset': cycle.asset,
                            'symbol': cycle.symbol,
                            'started_at': cycle.started_at.isoformat() if cycle.started_at else '',
                            'completed_at': cycle.completed_at.isoformat() if cycle.completed_at else '',
                            'status': cycle.status,
                            'error': cycle.error or '',
                            'experts_count': 0,  # Expert insights no longer stored
                            'valid_proposals': (cycle.judge.validation_summary.get('valid_proposals', 0) if cycle.judge and isinstance(cycle.judge.validation_summary, dict) else 0),
                            'selected_expert': cycle.judge.selected if cycle.judge else ''
                        }
                        writer.writerow(row)
        else:
            filename = f"multi_expert_cycles_{asset}_{timestamp}.json"
            filepath = f"exports/{filename}"
            
            # Create exports directory if it doesn't exist
            import os
            os.makedirs("exports", exist_ok=True)
            
            # Write JSON
            cycles_data = []
            for cycle in cycles:
                cycle_dict = cycle.model_dump()
                # Convert datetime objects to ISO strings
                if cycle_dict.get('started_at'):
                    cycle_dict['started_at'] = cycle_dict['started_at'].isoformat()
                if cycle_dict.get('completed_at'):
                    cycle_dict['completed_at'] = cycle_dict['completed_at'].isoformat()
                cycles_data.append(cycle_dict)
            
            with open(filepath, 'w', encoding='utf-8') as jsonfile:
                json_lib.dump(cycles_data, jsonfile, indent=2, ensure_ascii=False)
        
        return {
            "message": f"Exported {len(cycles)} multi-expert cycles",
            "filepath": filepath,
            "format": format,
            "asset": asset,
            "count": len(cycles)
        }
        
    except Exception as e:
        logger.error({"endpoint": "multi-expert-cycles"}, f"Error exporting multi-expert cycles: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error exporting cycles: {str(e)}")


# ---------------------------------------------------------------------------
# Legacy agent-settings compatibility for current frontend

class AgentSetting(BaseModel):
    asset: str
    mode: str
    model: str
    minBars: int
    intervalSec: int
    rateLimit: int
    timeoutMs: int
    apiKey: str | None = None


_SETTINGS_PATH = os.path.join(os.path.dirname(__file__), "agent_settings.json")


def _load_agent_settings() -> list[dict[str, Any]]:
    try:
        if not os.path.exists(_SETTINGS_PATH):
            return []
        with open(_SETTINGS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_agent_settings(settings: list[dict[str, Any]]) -> None:
    with open(_SETTINGS_PATH, "w", encoding="utf-8") as fh:
        json.dump(settings, fh)


@app.get("/agent-settings", response_model=list[AgentSetting])
async def get_agent_settings():
    return _load_agent_settings()


@app.post("/agent-settings/{asset}", response_model=AgentSetting)
async def save_agent_setting(asset: str, cfg: Mapping[str, Any]):
    settings = _load_agent_settings()
    updated = dict(cfg)
    updated["asset"] = asset
    try:
        AgentSetting(**updated)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    for i, s in enumerate(settings):
        if s.get("asset") == asset:
            settings[i] = updated
            break
    else:
        settings.append(updated)
    _save_agent_settings(settings)
    return updated


@app.post("/agent-settings/{asset}/test")
async def test_agent_settings(asset: str):
    cfg = next((s for s in _load_agent_settings() if s.get("asset") == asset), None)
    if not cfg:
        raise HTTPException(status_code=404, detail="Agent not configured")
    try:
        AgentSetting(**cfg)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "ok"}


@app.post("/kill")
async def kill_switch():
    import asyncio, os

    loop = asyncio.get_event_loop()
    loop.call_later(0.1, lambda: os._exit(0))
    return {"status": "shutting down"}


@app.get("/agents")
async def list_agents() -> dict[str, list[dict[str, object]]]:
    """Return all persisted agents regardless of runtime state."""

    agents = await agents_repo.list_agents()
    return {"agents": [a.model_dump() for a in agents]}


@app.put("/agents/{agent_id}")
async def upsert_agent(agent_id: str, info: AgentRecord) -> dict[str, object]:
    """Create or update an agent record.

    Returns the persisted ``AgentRecord`` so callers always receive a JSON
    response. Previously this endpoint returned ``204 No Content`` which caused
    the frontend to attempt to read a ``.data`` property from an empty
    response. By returning the agent information (and defaulting to a ``200``
    status) we align the backend with typical REST expectations and simplify
    client handling.
    """

    # Allow the payload to override the URL agent_id if they differ
    if agent_id != info.id:
        # Update the info object to use the URL parameter as the canonical ID
        info.id = agent_id
        
    await agents_repo.upsert_agent(info)
    # Return the agent using field names (``id`` instead of ``_id``)
    return info.model_dump()


@app.delete("/agents/{agent_id}", status_code=204)
async def delete_agent(agent_id: str):
    await agents_repo.delete_agent(agent_id)
    return Response(status_code=204)


# The agents page polls /metrics/agents every ~2s; cache the broker positions
# read this long so frequent polls don't add per-request broker load.
_UNREALIZED_CACHE_TTL_SECONDS = 3.0


def _position_asset(symbol: str, assets: set[str]) -> str:
    """Map a broker position *symbol* (e.g. ``BTCUSDm``) to a known asset key."""
    if symbol in assets:
        return symbol
    stripped = symbol[:-1] if symbol.endswith("m") else symbol
    if stripped in assets:
        return stripped
    for a in assets:
        if symbol.startswith(a):
            return a
    return stripped


@app.get("/metrics/agents")
async def agent_metrics_endpoint():
    """Return live runtime metrics for each agent.

    Operational metrics (runs, latency, drift, ETA, …) come from the in-memory
    registry. **P&L is corrected here**: ``realized_pnl`` is the contract-size-
    correct money total from closed ``comprehensive_trades``; ``unrealized_pnl``
    is summed live from open broker positions; the surfaced ``pnl`` is their sum.
    The broken in-memory ``m.pnl`` (raw price-diff) is no longer displayed. The
    in-memory recording path is left untouched (it still feeds daily guards)."""

    now = datetime.now(timezone.utc)
    registry = agent_metrics()
    assets = set(registry.keys()) | set(supervisor._cfg.keys())

    # Realized P&L / outcomes per asset from persisted closed trades (all-time).
    try:
        from packages.db.repositories.comprehensive_logging import aggregate_realized_pnl
        realized_agg = await aggregate_realized_pnl()
    except Exception:
        realized_agg = {}
    # Historical agent outcomes remain reportable after the runtime registry resets.
    assets.update(realized_agg)

    # Live unrealized P&L per asset from open broker positions.
    # This endpoint is polled every ~2s by the agents page, so the broker read
    # is cached briefly on app.state to avoid hammering MT5 (no per-poll load).
    unrealized_by_asset: dict[str, float] = {}
    cache = getattr(app.state, "_unrealized_cache", None)
    now_ts = now.timestamp()
    if cache is not None and (now_ts - cache.get("at", 0.0)) < _UNREALIZED_CACHE_TTL_SECONDS:
        unrealized_by_asset = dict(cache.get("data", {}))
    elif not offline_mode():
        try:
            raw_positions = await asyncio.to_thread(mt5_router.adapter.positions_raw)
            for p in raw_positions:
                sym = str(p.get("symbol", ""))
                profit = float(p.get("profit", 0.0) or 0.0)
                key = _position_asset(sym, assets)
                unrealized_by_asset[key] = unrealized_by_asset.get(key, 0.0) + profit
            app.state._unrealized_cache = {"at": now_ts, "data": dict(unrealized_by_asset)}
        except Exception:
            unrealized_by_asset = {}

    out: dict[str, Any] = {}
    for asset in assets:
        m = registry.get(asset, AgentMetrics())
        lats = list(m.latencies)
        if not lats:
            p50 = p95 = None
        elif len(lats) == 1:
            p50 = p95 = lats[0]
        else:
            qs = statistics.quantiles(lats, n=100, method="inclusive")
            p50, p95 = qs[49], qs[94]

        drifts = list(m.drift_ms)
        if not drifts:
            d50 = d95 = None
        elif len(drifts) == 1:
            d50 = d95 = drifts[0]
        else:
            qs = statistics.quantiles(drifts, n=100, method="inclusive")
            d50, d95 = qs[49], qs[94]

        hits = sum(1 for d in m.slot_decisions if d > 0)
        hit_rate = hits / len(m.slot_decisions) if m.slot_decisions else None

        last_run = m.last_run.isoformat() if m.last_run else None
        cfg = supervisor._cfg.get(asset, {})
        agent_id = cfg.get("agent_id")
        interval = cfg.get("intervalSec") or cfg.get("interval_sec")
        eta = usage = None
        if m.last_run and interval:
            next_run = m.last_run + timedelta(seconds=int(interval))
            eta = next_run.isoformat()
            usage = min(
                max((now - m.last_run).total_seconds() / int(interval), 0.0), 1.0
            )
        status = supervisor.status(asset)

        # Corrected P&L: realized (persisted closed trades) + unrealized (live).
        agg = realized_agg.get(asset)
        if agg is not None:
            realized_pnl = float(agg.get("realized_pnl", 0.0))
            wins = int(agg.get("wins", m.wins))
            losses = int(agg.get("losses", m.losses))
        else:
            # No persisted data (e.g. log-only mode): fall back to the
            # sign-correct in-memory win/loss counts; realized money is unknown.
            realized_pnl = 0.0
            wins = m.wins
            losses = m.losses
        unrealized_pnl = float(unrealized_by_asset.get(asset, 0.0))
        total_pnl = realized_pnl + unrealized_pnl

        summary = (
            f"runs={m.runs}, trades={m.orders}, open={m.open_trades},"
            f" wins={wins}, losses={losses}, pnl={total_pnl:.2f}"
        )

        out[asset] = {
            "agent_id": agent_id,
            "runs": m.runs,
            "decisions": m.decisions,
            "orders": m.orders,
            "wins": wins,
            "losses": losses,
            "pnl": total_pnl,
            "realized_pnl": realized_pnl,
            "closed_trades": int(agg.get("trades", wins + losses)) if agg is not None else wins + losses,
            "unrealized_pnl": unrealized_pnl,
            "open_trades": m.open_trades,
            "budget_skips": m.budget_skips,
            "latency_p50": p50,
            "latency_p95": p95,
            "drift_p50": d50,
            "drift_p95": d95,
            "hit_rate": hit_rate,
            "last_run": last_run,
            "eta": eta,
            "summary": summary,
            "running": bool(status.get("running")),
            "usage": usage,
            "run_interval": cfg.get("run_interval"),
            "risk_percent": cfg.get("risk_percent"),
        }
    return out


@app.get("/agents/{symbol}/status")
async def agent_status(symbol: str):
    """Return execution state for ``symbol`` or 404 if unknown."""

    known = symbol in supervisor._tasks or symbol in supervisor._errors or symbol in supervisor._cfg
    if not known:
        raise HTTPException(status_code=404, detail="Agent not found")

    status = supervisor.status(symbol)
    err = status.get("error")
    if err is not None:
        status["error"] = str(err)
    cfg = supervisor._cfg.get(symbol, {})
    status["run_interval"] = cfg.get("run_interval")
    status["risk_percent"] = cfg.get("risk_percent")
    return status


def _deep_merge(base: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge ``overrides`` into ``base``."""

    merged = dict(base)
    for key, value in overrides.items():
        if (
            isinstance(value, Mapping)
            and isinstance(merged.get(key), Mapping)
        ):
            merged[key] = _deep_merge(merged[key], value)  # type: ignore[arg-type]
        else:
            merged[key] = value
    return merged



# Remove sensitive GPT/api key fields from override payloads
def _strip_runtime_overrides(overrides: Mapping[str, Any]) -> dict[str, Any]:
    def _strip(obj: Mapping[str, Any]) -> dict[str, Any]:
        clean: dict[str, Any] = {}
        for k, v in obj.items():
            if k in {"api_key"}:
                continue
            if k == "gpt":
                if isinstance(v, Mapping):
                    g = _strip(v)
                    if g:
                        clean[k] = g
                continue
            clean[k] = _strip(v) if isinstance(v, Mapping) else v
        return clean

    return _strip(overrides)


async def _create_services_for_agent(trading_symbol: str, runtime_cfg: dict[str, Any]) -> dict[str, Any]:
    """Create all required services for agent execution.
    
    This function extracts the service creation logic from _agent_runner
    to be used by _prepare_runtime_cfg for service initialization.
    """
    from packages.adapters.factory import create_broker
    from packages.core.candle_service import CandleService
    
    cfg = dict(runtime_cfg)
    
    # Extract symbol from instrument config if not directly available
    if not cfg.get("symbol"):
        instrument = cfg.get("instrument", {})
        if isinstance(instrument, dict) and instrument.get("symbol"):
            cfg["symbol"] = instrument["symbol"]
        else:
            raise HTTPException(status_code=400, detail="Symbol is required for agent execution")

    missing: list[str] = []
    for key in ("symbol", "candle_count_5m", "broker"):
        if not cfg.get(key):
            missing.append(key)
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing config fields: {', '.join(missing)}")

    # Validate candle counts are greater than 0
    candle_count_5m = int(cfg.get("candle_count_5m", 0))
    if candle_count_5m <= 0:
        raise HTTPException(status_code=400, detail=f"candle_count_5m must be greater than 0, got {candle_count_5m}")

    # Derive 15m candle count from configuration; raise if missing.
    try:
        candle_count_15m = cfg["candles_15_count"]
    except KeyError as exc:
        raise KeyError("candles_15_count is required in config") from exc
    
    candle_count_15m = int(candle_count_15m)
    if candle_count_15m <= 0:
        raise HTTPException(status_code=400, detail=f"candles_15_count must be greater than 0, got {candle_count_15m}")
    
    cfg["candle_count_15m"] = candle_count_15m

    # If broker is provided as a name, create and configure it.
    broker = cfg["broker"]
    if isinstance(broker, str):
        broker_name = broker

        # Always create the market data broker (MT5) for market data access.
        broker_config = {}

        if broker_name.lower() == "mt5":
            # Create a new MT5 adapter instance for each agent to avoid race conditions
            # But reuse the login credentials from the existing session
            tokens = getattr(app.state, "broker_tokens", {})
            mt5_session = tokens.get("mt5", {})

            # Create a new MT5 adapter instance for this agent
            broker = create_broker("mt5")

            # If there's an active MT5 session, use the stored credentials to connect
            if mt5_session:
                logger.info({"broker": "mt5", "asset": trading_symbol}, "Creating new MT5 adapter instance for agent with reused credentials")
                # Extract credentials from the stored session
                required_fields = ["server", "login", "password"]
                missing_fields = [field for field in required_fields if not mt5_session.get(field)]
                
                if missing_fields:
                    logger.error({
                        "broker": "mt5", 
                        "asset": trading_symbol, 
                        "missing_fields": missing_fields
                    }, "MT5 session missing required credentials")
                    raise HTTPException(
                        status_code=400, 
                        detail=f"MT5 session incomplete, missing: {', '.join(missing_fields)}"
                    )
                
                broker_config.update({
                    "server": mt5_session["server"],
                    "login": mt5_session["login"],
                    "password": mt5_session["password"],
                })
            else:
                logger.warning({"broker": "mt5", "asset": trading_symbol}, "No active MT5 session found, broker may not be connected")

        # Connect the market data broker with reused credentials
        if broker_config:
            logger.info(
                {"broker": "mt5", "asset": trading_symbol},
                "Connecting MT5 broker for agent — this may take up to 30 s",
                server=broker_config.get("server"),
                login=broker_config.get("login"),
            )
            await broker.connect(**broker_config)
            logger.info(
                {"broker": "mt5", "asset": trading_symbol},
                "MT5 broker connected successfully",
            )

        # MT5 is the only broker and always trades live: the order broker is the
        # MT5 adapter itself, which also serves as the market-data source.
        market_data_broker = broker

        cfg["broker"] = broker
    else:
        # If broker is already an object, determine its name for logging
        broker_name = getattr(broker.__class__, "__name__", "unknown").lower()

        # For object brokers, the broker itself is the market data broker
        market_data_broker = broker

    # Resolve candle service with proper token and broker setup
    candle_service = cfg.get("candle_service")
    
    # Configure candle service after broker is properly set up
    # Use the market data broker for candle service
    if not candle_service:
        # Create candle service with the market data broker
        cfg["candle_service"] = CandleService(
            broker=market_data_broker if hasattr(market_data_broker, "candles") else None,
        )
    elif isinstance(candle_service, str) and candle_service.lower() == "mt5":
        # For MT5 candle service, use the market data broker instance
        logger.info({"broker": "mt5", "asset": trading_symbol}, "Using market data MT5 broker for candle service")
        if "mt5_service" not in cfg:
            cfg["mt5_service"] = market_data_broker
        cfg["candle_service"] = CandleService(broker=market_data_broker)

    # Final broker validation and service setup
    broker = cfg.get("broker")
    if not hasattr(broker, "place"):
        raise HTTPException(status_code=400, detail="Broker is not functional")
    
    # Ensure broker services are set up for MT5
    if broker and hasattr(broker, "place"):
        # Use market data broker type for service setup
        market_broker_type = getattr(market_data_broker.__class__, "__name__", "").lower()
        
        # Set up MT5 service if market data broker is MT5Adapter
        if "mt5" in market_broker_type and "mt5_service" not in cfg:
            cfg["mt5_service"] = market_data_broker
            logger.info({"asset": trading_symbol, "broker": market_broker_type}, "MT5 service configured for real-time monitoring")
    
    # Set up journal service for trade logging
    if "journal" not in cfg:
        from packages.core.journal import AssetJournal
        from packages.core.config import LOGS_DIR
        
        # Get agent_id for journal naming
        agent_id = cfg.get("agent_id", f"agent_{trading_symbol}")
        
        cfg["journal"] = AssetJournal(
            root=LOGS_DIR,
            asset_id=trading_symbol,
            agent_name=agent_id
        )
        logger.info({"asset": trading_symbol, "agent_id": agent_id}, "Journal service initialized for trade logging")
    
    return cfg


async def _prepare_runtime_cfg(symbol: str, overrides: Mapping[str, Any] | None) -> dict[str, Any]:
    # Look up agent by symbol (symbol parameter is the trading symbol)
    try:
        # Get all agents and find the one with matching symbol
        all_agents = await agents_repo.list_agents()
        agent_rec = None
        for agent in all_agents:
            if agent.symbol == symbol:  # symbol parameter is the trading symbol
                agent_rec = agent
                break
        
        if not agent_rec:
            raise HTTPException(status_code=404, detail=f"Agent with symbol '{symbol}' not found")
            
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Agent with symbol '{symbol}' not found: {str(e)}")

    # Build runtime configuration from agent record
    runtime_cfg = {}

    # Build runtime configuration from agent record
    runtime_cfg.update(
        {
            "candles_5_count": agent_rec.candle_count_5m,
            "candles_15_count": agent_rec.candle_count_15m,
            "broker": agent_rec.broker,
            "risk_percent": agent_rec.risk_percent,
            "run_interval": agent_rec.run_interval,
            "prompt_id": agent_rec.prompt_id,
            "agent_id": agent_rec.id,
            "symbol": agent_rec.symbol,
        }
    )
    
    # Set up instrument configuration
    runtime_cfg["instrument"] = {"symbol": agent_rec.symbol}

    clean: dict[str, Any] = {}
    if overrides:
        clean = _strip_runtime_overrides(overrides)
        runtime_cfg = _deep_merge(runtime_cfg, clean)

    def _normalize(fields: list[str]) -> int:
        for f in fields:
            if f in clean:
                val = clean[f]
                if val is None:
                    raise HTTPException(status_code=400, detail=f"{fields[0]} is required")
                for k in fields:
                    runtime_cfg[k] = val
                return val
        for f in fields:
            val = runtime_cfg.get(f)
            if val is not None:
                for k in fields:
                    runtime_cfg[k] = val
                return val
        raise HTTPException(status_code=400, detail=f"{fields[0]} is required")

    _normalize(["candles_5_count", "candle_count_5m", "N"])
    _normalize(["candles_15_count", "candle_count_15m"])
    
    # Ensure AgentRunner-compatible field names
    if "candles_5_count" in runtime_cfg:
        runtime_cfg["candle_count_5m"] = runtime_cfg["candles_5_count"]
    if "candles_15_count" in runtime_cfg:
        runtime_cfg["candle_count_15m"] = runtime_cfg["candles_15_count"]

    gpt_cfg = runtime_cfg.get("gpt") or {}
    if OPENAI_API_KEY:
        gpt_cfg["api_key"] = OPENAI_API_KEY
    if GEMINI_API_KEY:
        gpt_cfg["gemini_api_key"] = GEMINI_API_KEY
    # Set the active AI provider from env or config
    gpt_cfg.setdefault("provider", AI_PROVIDER)
    runtime_cfg["gpt"] = gpt_cfg

    await _apply_saved_model_config(runtime_cfg, symbol)

    if not runtime_cfg.get("agent_id"):
        raise HTTPException(status_code=400, detail="agent_id is required")

    # Plain-data snapshot BEFORE service objects are injected — the override
    # validation in start_agent must run against data, not live broker/journal
    # instances (validating post-services made every override-start fail 400).
    runtime_cfg["_pre_services_cfg"] = dict(runtime_cfg)

    # CRITICAL FIX: Create all required services
    # This ensures broker, candle_service, and other services are properly initialized
    try:
        # Get the actual trading symbol for service creation
        trading_symbol = runtime_cfg.get("symbol")
        if not trading_symbol:
            raise HTTPException(status_code=400, detail="Trading symbol is required for service creation")
        
        # Create all services using _create_services_for_agent
        runtime_cfg = await _create_services_for_agent(trading_symbol, runtime_cfg)
        
        logger.info({"agent_id": symbol, "trading_symbol": trading_symbol}, "Successfully created all required services")
        
    except Exception as e:
        logger.error({"agent_id": symbol, "error": str(e)}, "Failed to create services")
        raise HTTPException(status_code=500, detail=f"Failed to create services: {str(e)}")

    return runtime_cfg


@app.post("/agents/{symbol}/start")
async def start_agent(symbol: str, cfg: Mapping[str, Any] | None = None):
    """Start an agent for ``symbol`` using configuration from the repository.
    
    This will wait for the scheduled time and then run the agent.
    """
    logger.info({"symbol": symbol}, f"Starting scheduled agent for {symbol}")
    try:
        runtime_cfg = await _prepare_runtime_cfg(symbol, cfg)
        
        # Set execution mode to 'scheduled' for scheduled execution
        runtime_cfg["execution_mode"] = "scheduled"
        
        # Extract scheduled time from config if provided
        scheduled_time = None
        if cfg and "scheduled_time" in cfg:
            try:
                from datetime import datetime
                scheduled_time = datetime.fromisoformat(cfg["scheduled_time"].replace('Z', '+00:00'))
                runtime_cfg["scheduled_time"] = scheduled_time
                logger.info({"symbol": symbol}, f"Agent scheduled to run at: {scheduled_time}")
            except Exception as e:
                logger.warning({"symbol": symbol}, f"Invalid scheduled_time format: {e}")

        if cfg:
            validation_source = runtime_cfg.get("_pre_services_cfg") or runtime_cfg
            cfg_for_validation = {
                k: v
                for k, v in validation_source.items()
                if k
                not in {
                    "N",
                    "risk_percent",
                    "run_interval",
                    "candle_count_5m",
                    "candle_count_15m",
                    "symbol",
                    "candle_service",
                    "expiry_date",
                    "execution_mode",
                    "scheduled_time",
                    "prompt_id",
                    "gpt_config",
                    "_pre_services_cfg",
                }
            }
            try:
                agent_cfg = AgentConfig.model_validate(cfg_for_validation, strict=False)
            except ValidationError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            try:
                await config_repo.put_agent_config(symbol, agent_cfg, CONFIG_ENV)
            except Exception:
                pass

        supervisor.start(symbol, runtime_cfg)
        logger.info({"symbol": symbol}, f"Scheduled agent started successfully for {symbol}")
    except HTTPException:
        logger.error({"symbol": symbol}, f"Failed to start scheduled agent - HTTP exception")
        raise
    except Exception as exc:  # pragma: no cover - defensive
        logger.error({"symbol": symbol}, f"Failed to start scheduled agent: {str(exc)}")
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "started", "execution_mode": "scheduled", "scheduled_time": scheduled_time.isoformat() if scheduled_time else None}


# Using the utility function from packages.core.datetime_utils

# Strong references to background run-now cycles so they aren't garbage
# collected mid-run (asyncio only keeps weak refs to tasks).
_background_run_now_tasks: dict[str, asyncio.Task] = {}


def _spawn_background_run_now(symbol: str, runtime_cfg: Mapping[str, Any]) -> None:
    """Run one agent cycle as a background task, logging (never raising) errors."""
    if symbol in _background_run_now_tasks:
        return

    async def _cycle() -> None:
        try:
            await supervisor.run_once(symbol, runtime_cfg)
            logger.info({"symbol": symbol}, f"Background run-now cycle finished for {symbol}")
        except Exception as exc:
            logger.error({"symbol": symbol}, f"Background run-now cycle failed for {symbol}: {exc}")

    task = asyncio.create_task(_cycle(), name=f"run-now-bg:{symbol}")
    _background_run_now_tasks[symbol] = task
    task.add_done_callback(lambda _: _background_run_now_tasks.pop(symbol, None))


async def _stop_agent_runtime(symbol: str) -> None:
    immediate = _background_run_now_tasks.get(symbol)
    if immediate is not None:
        immediate.cancel()
    await supervisor.stop(symbol)
    if immediate is not None:
        await asyncio.gather(immediate, return_exceptions=True)


@app.post("/agents/{symbol}/run-now")
async def run_now(
    symbol: str,
    cfg: Mapping[str, Any] | None = None,
    background: bool = False,
):
    """Trigger an immediate agent cycle for ``symbol``.

    With ``background=true`` the endpoint acknowledges with ``{"status":
    "started"}`` as soon as the supervisor task exists and the first cycle has
    been scheduled, instead of blocking until the full cycle (GPT call, order
    placement) completes. Sequential launchers (start_dev.bat) rely on this so
    one slow/hung cycle can never block starting the remaining agents.
    """
    try:
        logger.info({"symbol": symbol}, f"Manually triggering immediate agent run for {symbol}")
    except Exception as log_e:
        # If logging fails due to serialization, use a simple message
        print(f"Manually triggering immediate agent run for {symbol}")

    result: Any | None = None
    try:
        runtime_cfg = await _prepare_runtime_cfg(symbol, cfg)

        # Set execution mode to 'run_now' for immediate execution
        runtime_cfg["execution_mode"] = "run_now"

        if symbol not in supervisor._tasks:
            supervisor.start(symbol, runtime_cfg)
        if background:
            _spawn_background_run_now(symbol, runtime_cfg)
            return {"status": "started", "symbol": symbol, "background": True}
        result = await supervisor.run_once(symbol, runtime_cfg)
    except HTTPException:
        raise
    except Exception as e:  # pragma: no cover - defensive
        # Safely serialize the error message to avoid JSON serialization issues
        error_msg = str(e)
        try:
            # Test if the error message contains non-serializable objects
            json.dumps(error_msg)
        except (TypeError, ValueError):
            error_msg = "Agent execution failed with non-serializable error"
        
        # Safely serialize the symbol to avoid JSON serialization issues
        safe_symbol = str(symbol) if symbol else "unknown"
        try:
            # Test if the symbol can be serialized
            json.dumps(safe_symbol)
        except (TypeError, ValueError):
            safe_symbol = "unknown"
        
        try:
            logger.error({"symbol": safe_symbol}, f"Error during manual agent run: {error_msg}")
        except Exception as log_e:
            # If logging fails due to serialization, use a simple message
            print(f"Error during manual agent run for {safe_symbol}: {error_msg}")
        
        raise HTTPException(status_code=500, detail="agent error")
    
    # Serialize result to handle datetime objects
    try:
        serialized_result = serialize_for_json(result)
        serialized_config = serialize_for_json(runtime_cfg)
    except Exception as serialize_e:
        # If serialization fails, return a safe fallback
        print(f"Serialization failed: {serialize_e}")
        return {"result": None, "config": {}, "error": "Serialization failed"}
    
    return {"result": serialized_result, "config": serialized_config}


@app.post("/agents/{symbol}/stop")
async def stop_agent(symbol: str):
    """Stop the running agent for ``symbol``."""
    logger.info({"symbol": symbol}, f"Stopping agent for {symbol}")
    try:
        await _stop_agent_runtime(symbol)
        logger.info({"symbol": symbol}, f"Agent stopped successfully for {symbol}")
    except Exception as exc:
        logger.error({"symbol": symbol}, f"Failed to stop agent: {str(exc)}")
        raise HTTPException(status_code=500, detail=str(exc))
    return {"status": "stopped"}


@app.get("/agents/health")
async def get_agents_health():
    """Get comprehensive health status of all agents."""
    try:
        health_status = supervisor.get_health_status()
        return health_status
    except Exception as exc:
        logger.error({"endpoint": "agents-health"}, f"Failed to get agents health status: {str(exc)}")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/agents/{symbol}/restart")
async def restart_agent(symbol: str):
    """Manually restart a failed agent."""
    logger.info({"symbol": symbol}, f"Manually restarting agent for {symbol}")
    try:
        # Stop the agent if it's running
        await _stop_agent_runtime(symbol)
        
        # Get configuration and restart
        runtime_cfg = await _prepare_runtime_cfg(symbol, None)
        supervisor.start(symbol, runtime_cfg)
        
        logger.info({"symbol": symbol}, f"Agent restarted successfully for {symbol}")
        return {"status": "restarted"}
    except Exception as exc:
        logger.error({"symbol": symbol}, f"Failed to restart agent: {str(exc)}")
        raise HTTPException(status_code=500, detail=str(exc))

class Trade(BaseModel):
    entry_time: datetime | None = None
    exit_time: datetime | None = None
    entry_price: float | None = None
    exit_price: float | None = None
    stop_loss: float | None = None
    target: float | None = None
    result: str | None = None
    points: float | None = None
    risk_to_reward: float | None = None
    api_call_id: str | None = None


class Report(BaseModel):
    test_name: str
    trades: list[Trade]


@app.get("/api/gpt-response/{response_id}")
async def get_gpt_response(response_id: str):
    """Get a specific GPT response by ID."""
    try:
        from packages.db.repositories import gpt_responses as gpt_repo
        
        response = await gpt_repo.get_gpt_response_by_id(response_id)
        if not response:
            raise HTTPException(status_code=404, detail="GPT response not found")
        
        # Convert ObjectId to string for JSON serialization
        response["_id"] = str(response["_id"])
        if response.get("timestamp"):
            response["timestamp"] = response["timestamp"].isoformat()
        
        return response
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch GPT response: {str(e)}")


@app.get("/api/trigger-check/{trigger_id}")
async def get_trigger_check(trigger_id: str):
    """Get a specific trigger check by ID."""
    try:
        from packages.db.repositories import gpt_responses as gpt_repo
        
        check = await gpt_repo.get_trigger_check_by_id(trigger_id)
        if not check:
            raise HTTPException(status_code=404, detail="Trigger check not found")
        
        # Convert ObjectId to string for JSON serialization
        check["_id"] = str(check["_id"])
        if check.get("timestamp"):
            check["timestamp"] = check["timestamp"].isoformat()
        if check.get("trigger_time"):
            check["trigger_time"] = check["trigger_time"].isoformat()
        
        return check
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch trigger check: {str(e)}")


# Export endpoints
@app.post("/api/export/gpt-responses")
async def export_gpt_responses(
    asset: Optional[str] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    limit: int = Query(1000, gt=0, le=10000),
    format: str = Query("json", regex="^(json|csv)$"),
):
    """Export GPT responses to file."""
    try:
        from packages.core.services.export_service import get_export_service
        
        export_service = get_export_service()
        filepath = await export_service.export_gpt_responses(
            asset=asset,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
            format=format,
        )
        
        if not filepath:
            raise HTTPException(status_code=404, detail="No data found for export")
        
        return {
            "message": "Export completed successfully",
            "filepath": filepath,
            "format": format,
            "asset": asset,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Export failed: {str(e)}")


@app.post("/api/export/comprehensive-trades")
async def export_comprehensive_trades(
    asset: Optional[str] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    limit: int = Query(1000, gt=0, le=10000),
    format: str = Query("json", regex="^(json|csv)$"),
):
    """Export comprehensive trades to file."""
    try:
        from packages.core.services.export_service import get_export_service
        
        export_service = get_export_service()
        filepath = await export_service.export_comprehensive_trades(
            asset=asset,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
            format=format,
        )
        
        if not filepath:
            raise HTTPException(status_code=404, detail="No data found for export")
        
        return {
            "message": "Export completed successfully",
            "filepath": filepath,
            "format": format,
            "asset": asset,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Export failed: {str(e)}")


@app.post("/api/export/slot-candle-matches")
async def export_slot_candle_matches(
    asset: Optional[str] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    limit: int = Query(1000, gt=0, le=10000),
    format: str = Query("json", regex="^(json|csv)$"),
):
    """Export slot-candle matches to file."""
    try:
        from packages.core.services.export_service import get_export_service
        
        export_service = get_export_service()
        filepath = await export_service.export_slot_candle_matches(
            asset=asset,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
            format=format,
        )
        
        if not filepath:
            raise HTTPException(status_code=404, detail="No data found for export")
        
        return {
            "message": "Export completed successfully",
            "filepath": filepath,
            "format": format,
            "asset": asset,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Export failed: {str(e)}")


@app.post("/api/export/trading-report")
async def export_trading_report(
    asset: Optional[str] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    format: str = Query("json", regex="^(json|csv)$"),
):
    """Export comprehensive trading report."""
    try:
        from packages.core.services.export_service import get_export_service
        
        export_service = get_export_service()
        filepath = await export_service.export_trading_report(
            asset=asset,
            start_time=start_time,
            end_time=end_time,
            format=format,
        )
        
        if not filepath:
            raise HTTPException(status_code=404, detail="No data found for export")
        
        return {
            "message": "Trading report exported successfully",
            "filepath": filepath,
            "format": format,
            "asset": asset,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Export failed: {str(e)}")


@app.post("/api/export/gpt-sniper-levels")
async def export_gpt_sniper_levels(
    asset: Optional[str] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    limit: int = Query(1000, gt=0, le=10000),
    format: str = Query("json", regex="^(json|csv)$"),
):
    """Export GPT sniper asset levels to file."""
    try:
        from packages.core.services.export_service import get_export_service
        
        export_service = get_export_service()
        filepath = await export_service.export_gpt_sniper_levels(
            asset=asset,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
            format=format,
        )
        
        if not filepath:
            raise HTTPException(status_code=404, detail="No data found for export")
        
        return {
            "message": "GPT sniper levels exported successfully",
            "filepath": filepath,
            "format": format,
            "asset": asset,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Export failed: {str(e)}")


@app.get("/api/available-assets")
async def get_available_assets():
    """Get list of available assets from the database."""
    try:
        from packages.db.repositories import gpt_responses as gpt_repo
        
        # Get unique assets from GPT responses
        gpt_assets = await gpt_repo.get_unique_assets()
        
        # Get unique assets from trigger checks
        trigger_assets = await gpt_repo.get_unique_trigger_assets()
        
        # Combine and deduplicate
        all_assets = list(set(gpt_assets + trigger_assets))
        all_assets.sort()
        
        return {
            "assets": all_assets,
            "count": len(all_assets)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get available assets: {str(e)}")


@app.get("/api/export/list")
async def list_exports():
    """List all exported files."""
    try:
        from packages.core.services.export_service import get_export_service
        
        export_service = get_export_service()
        exports = export_service.list_exports()
        
        return {
            "exports": exports,
            "count": len(exports)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list exports: {str(e)}")


@app.get("/api/comprehensive-trades")
async def get_comprehensive_trades(
    asset: str = Query("ALL", description="Asset to filter by"),
    limit: int = Query(50, description="Number of records to return"),
):
    """Get stored comprehensive trades data."""
    try:
        from packages.db.repositories import comprehensive_logging as comp_repo
        
        # Simple data retrieval
        if asset == "ALL":
            trades = await comp_repo.get_all_comprehensive_trades(limit=limit)
        else:
            trades = await comp_repo.get_comprehensive_trades(asset=asset, limit=limit)
        
        # Convert ObjectId and datetime to strings
        for trade in trades:
            if trade.get("_id"):
                trade["_id"] = str(trade["_id"])
            if isinstance(trade.get("timestamp"), datetime):
                trade["timestamp"] = trade["timestamp"].isoformat()
            if isinstance(trade.get("signal_received_at"), datetime):
                trade["signal_received_at"] = trade["signal_received_at"].isoformat()
            if isinstance(trade.get("trade_executed_at"), datetime):
                trade["trade_executed_at"] = trade["trade_executed_at"].isoformat()
            if isinstance(trade.get("trade_closed_at"), datetime):
                trade["trade_closed_at"] = trade["trade_closed_at"].isoformat()
        
        return {"trades": trades, "count": len(trades)}
    except Exception as e:
        return {"error": str(e), "trades": [], "count": 0}


@app.delete("/api/comprehensive-trades/{trade_id}")
async def delete_comprehensive_trade(trade_id: str):
    """Delete a comprehensive trade by ID."""
    try:
        from bson import ObjectId
        from packages.db.repositories import comprehensive_logging as comp_repo
        
        # Validate ObjectId format
        try:
            ObjectId(trade_id)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid trade ID format")
        
        # Delete the trade
        deleted = await comp_repo.delete_comprehensive_trade(trade_id)
        
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Trade with ID {trade_id} not found")
        
        logger.info({"endpoint": "trades"}, f"Deleted comprehensive trade {trade_id}")
        return {"message": f"Trade {trade_id} deleted successfully"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error({"endpoint": "trades"}, f"Error deleting trade {trade_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to delete trade: {str(e)}")


@app.get("/api/slot-candle-matches/{asset}")
async def get_slot_candle_matches(
    asset: str,
    limit: int = Query(50, description="Number of records to return"),
):
    """Get stored slot-candle matches data."""
    try:
        from packages.db.repositories import comprehensive_logging as comp_repo
        
        # Handle "ALL" case to get matches from all assets
        if asset == "ALL":
            matches = await comp_repo.get_all_slot_candle_matches(limit=limit)
        else:
            matches = await comp_repo.get_slot_candle_matches(asset=asset, limit=limit, skip=0)
        
        # Convert ObjectId and datetime to strings
        for match in matches:
            if "_id" in match:
                match["_id"] = str(match["_id"])
            if match.get("timestamp"):
                match["timestamp"] = match["timestamp"].isoformat()
            if match.get("level_drawn_at"):
                match["level_drawn_at"] = match["level_drawn_at"].isoformat()
            if match.get("trigger_timestamp"):
                match["trigger_timestamp"] = match["trigger_timestamp"].isoformat()
            if match.get("trigger_candle_timestamp"):
                match["trigger_candle_timestamp"] = match["trigger_candle_timestamp"].isoformat()
        
        return {"matches": matches, "count": len(matches)}
    except Exception as e:
        return {"error": str(e), "matches": [], "count": 0}


@app.delete("/api/slot-candle-matches/{match_id}")
async def delete_slot_candle_match(match_id: str):
    """Delete a slot-candle match by ID."""
    try:
        from bson import ObjectId
        from packages.db.repositories import comprehensive_logging as comp_repo
        
        # Validate ObjectId format
        try:
            ObjectId(match_id)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid match ID format")
        
        # Delete the match
        deleted = await comp_repo.delete_slot_candle_match(match_id)
        
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Slot-candle match with ID {match_id} not found")
        
        logger.info({"endpoint": "slot-candle-matches"}, f"Deleted slot-candle match {match_id}")
        return {"message": f"Slot-candle match {match_id} deleted successfully"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error({"endpoint": "slot-candle-matches"}, f"Error deleting slot-candle match {match_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to delete slot-candle match: {str(e)}")


@app.get("/api/hourly-candles/{asset}")
async def get_hourly_candles(
    asset: str,
    limit: int = Query(50, description="Number of records to return"),
):
    """Get stored hourly candles data."""
    try:
        from packages.db.repositories import comprehensive_logging as comp_repo
        
        # Simple data retrieval
        candles = await comp_repo.get_hourly_candles(asset=asset, limit=limit, skip=0)
        
        # Convert ObjectId and datetime to strings
        for candle in candles:
            candle["_id"] = str(candle["_id"])
            if candle.get("timestamp"):
                candle["timestamp"] = candle["timestamp"].isoformat()
            if candle.get("candle_timestamp"):
                candle["candle_timestamp"] = candle["candle_timestamp"].isoformat()
            if candle.get("slot_timestamp"):
                candle["slot_timestamp"] = candle["slot_timestamp"].isoformat()
        
        return {"candles": candles, "count": len(candles)}
    except Exception as e:
        return {"error": str(e), "candles": [], "count": 0}


# ---------------------------------------------------------------------------
# Trading Analysis - Trades with Decision Flow
# ---------------------------------------------------------------------------


@app.get("/api/trading-analysis/trades")
async def get_trading_analysis_trades(
    asset: str | None = Query(None, description="Filter by asset"),
    status: str | None = Query(None, description="Filter by trade status (open, closed)"),
    side: str | None = Query(None, description="Filter by side (buy, sell)"),
    start: datetime | None = Query(None, description="Filter trades executed at/after this time"),
    end: datetime | None = Query(None, description="Filter trades executed before this time"),
    limit: int = Query(50, ge=1, le=500),
):
    """Get trades with linked decision flow (GPT → Expert → Judge → Trade).

    ``start``/``end`` filter on ``trade_executed_at`` (``end`` is exclusive) via
    the repository's indexed time query.
    """
    try:
        from packages.db.repositories import comprehensive_logging as comp_repo
        from packages.db.repositories.multi_expert_cycles import multi_expert_cycles_repo

        # Fetch comprehensive trades (time-window filtered DB-side when provided)
        if asset:
            trades = await comp_repo.get_comprehensive_trades(
                asset=asset, start_time=start, end_time=end, limit=limit
            )
        else:
            trades = await comp_repo.get_all_comprehensive_trades(
                start_time=start, end_time=end, limit=limit
            )

        # Apply filters
        if status:
            trades = [t for t in trades if t.get("trade_status") == status]
        if side:
            trades = [t for t in trades if t.get("side", "").lower() == side.lower()]

        partials = [trade for trade in trades if comp_repo.partial_trade(trade)]
        covered = comp_repo.covered_partials(trades, partials)
        # For each trade, try to find the matching multi-expert cycle
        enriched_trades = []
        for trade in trades:
            try:
                trade_time = trade.get("trade_executed_at") or trade.get("signal_received_at")
                trade_asset = trade.get("asset")

                # Build enriched trade object
                mongo_id = str(trade.get("_id") or "")
                enriched = {
                    "id": mongo_id,
                    "asset": trade_asset,
                    "symbol": trade.get("symbol"),
                    "side": trade.get("side"),
                    "entry_price": trade.get("entry_price"),
                    "exit_price": trade.get("exit_price"),
                    "stop_loss": trade.get("stop_loss"),
                    "take_profit": trade.get("take_profit"),
                    # Trailed-SL progression (initial stop is ``stop_loss``; each
                    # later move — trail steps + breakeven — is appended here).
                    "sl_trail": [
                        {
                            "sl": s.get("sl"),
                            "label": s.get("label"),
                            "at": s.get("at").isoformat() if isinstance(s.get("at"), datetime) else (str(s.get("at")) if s.get("at") else None),
                        }
                        for s in (trade.get("sl_trail") or [])
                        if isinstance(s, Mapping)
                    ],
                    "quantity": trade.get("quantity"),
                    "pnl": trade.get("pnl"),
                    "pnl_in_parent": trade.get("_id") in covered,
                    "trade_status": trade.get("trade_status"),
                    "exit_reason": trade.get("exit_reason"),
                    "pattern": trade.get("pattern"),
                    "confidence": trade.get("confidence"),
                    "trade_type": trade.get("trade_type"),
                    "broker_type": trade.get("broker_type"),
                    "gpt_mode": trade.get("gpt_mode"),
                    "signal_received_at": trade.get("signal_received_at").isoformat() if isinstance(trade.get("signal_received_at"), datetime) else str(trade.get("signal_received_at")) if trade.get("signal_received_at") else None,
                    "trade_executed_at": trade_time.isoformat() if isinstance(trade_time, datetime) else str(trade_time) if trade_time else None,
                    "trade_closed_at": trade.get("trade_closed_at").isoformat() if isinstance(trade.get("trade_closed_at"), datetime) else str(trade.get("trade_closed_at")) if trade.get("trade_closed_at") else None,
                    "gpt_response_id": trade.get("gpt_response_id"),
                    "cycle_id": trade.get("cycle_id"),
                    "position_ticket": trade.get("position_ticket"),
                    "parent_trade_id": trade.get("parent_trade_id"),
                    "trade_reason": trade.get("trade_reason"),
                    "trigger_reason": trade.get("trigger_reason"),
                    "ai_cost": trade.get("ai_cost"),
                    "broker_cost": trade.get("broker_cost"),
                    "decision_flow": None,
                }

                # Find matching cycle: prefer direct cycle_id, fall back to time-window
                cycle = None
                trade_cycle_id = trade.get("cycle_id")
                if trade_cycle_id:
                    try:
                        cycle = await multi_expert_cycles_repo.get_cycle(trade_cycle_id)
                    except Exception:
                        pass

                if cycle is None and trade_asset and trade_time:
                    try:
                        window_start = trade_time - timedelta(minutes=5)
                        window_end = trade_time + timedelta(minutes=1)
                        matching_cycles = await multi_expert_cycles_repo.list_cycles(
                            asset=trade_asset,
                            start_time=window_start,
                            end_time=window_end,
                            limit=1,
                            sort_order=-1,
                        )
                        if matching_cycles:
                            cycle = matching_cycles[0]
                    except Exception as e:
                        logger.warning({"endpoint": "trading-analysis"}, f"Failed to match cycle for trade: {e}")

                if cycle is not None:
                    experts_summary = []
                    for exp in cycle.experts:
                        experts_summary.append({
                            "name": exp.name,
                            "passed": exp.passed,
                            "proposal": exp.proposal,
                            "market_analysis": exp.market_analysis,
                            "validation": exp.validation,
                            "duration_ms": exp.duration_ms,
                        })

                    judge_summary = None
                    if cycle.judge:
                        judge_summary = {
                            "selected": cycle.judge.selected,
                            "rationale": cycle.judge.rationale,
                            "final_signal": cycle.judge.final_signal,
                            "validation_summary": cycle.judge.validation_summary,
                        }

                    enriched["decision_flow"] = {
                        "cycle_id": cycle.cycle_id,
                        "mode": cycle.mode,
                        "status": cycle.status,
                        "started_at": cycle.started_at.isoformat() if cycle.started_at else None,
                        "completed_at": cycle.completed_at.isoformat() if cycle.completed_at else None,
                        "experts": experts_summary,
                        "judge": judge_summary,
                    }

                enriched_trades.append(enriched)
            except Exception as e:
                logger.warning({"endpoint": "trading-analysis"}, f"Failed to enrich trade: {e}")
                continue

        return {
            "trades": enriched_trades,
            "count": len(enriched_trades),
        }

    except Exception as e:
        logger.error({"endpoint": "trading-analysis"}, f"Error fetching trading analysis: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error fetching trading analysis: {str(e)}")


@app.delete("/api/trading-analysis/trades/{trade_id}")
async def delete_trading_analysis_trade(trade_id: str):
    """Delete a trade and its linked decision cycle."""
    try:
        from bson import ObjectId
        from packages.db.client import get_client
        from packages.core.config import MONGO_DB_NAME
        from packages.db.repositories import comprehensive_logging as comp_repo
        from packages.db.repositories.multi_expert_cycles import multi_expert_cycles_repo

        client = get_client()
        db = client[MONGO_DB_NAME]

        # Look up the trade first to find linked cycle_id
        trade_doc = await db["comprehensive_trades"].find_one({"_id": ObjectId(trade_id)})
        if not trade_doc:
            # Also try client_order_id
            trade_doc = await db["comprehensive_trades"].find_one({"client_order_id": trade_id})
        if not trade_doc:
            raise HTTPException(status_code=404, detail="Trade not found")

        cycle_id = trade_doc.get("cycle_id")

        # Delete the trade
        deleted = await comp_repo.delete_comprehensive_trade(trade_id)

        # Cascade: delete linked cycle if exists
        cycle_deleted = False
        if cycle_id:
            try:
                cycle_deleted = await multi_expert_cycles_repo.delete_cycle(cycle_id)
            except Exception:
                pass

        # Cascade: clean up active_trades entry
        try:
            symbol = trade_doc.get("symbol")
            agent_id = trade_doc.get("agent_id")
            if symbol and agent_id:
                active_col = db["active_trades"]
                await active_col.delete_many({"symbol": symbol, "agent_id": agent_id})
        except Exception:
            pass

        return {
            "deleted": True,
            "trade_id": trade_id,
            "cycle_deleted": cycle_deleted,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete trade: {str(e)}")


@app.delete("/api/trading-analysis/trades")
async def delete_test_trades(symbol: str = Query("AAA", description="Symbol to purge")):
    """Delete all trades matching a symbol (e.g. test 'AAA' data)."""
    try:
        from packages.db.client import get_client
        from packages.core.config import MONGO_DB_NAME
        client = get_client()
        collection = client[MONGO_DB_NAME]["comprehensive_trades"]
        result = await collection.delete_many({"symbol": symbol})
        return {"deleted_count": result.deleted_count, "symbol": symbol}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete trades: {str(e)}")


@app.delete("/api/trading-analysis/purge")
async def purge_all_trade_data():
    """Delete ALL trade-lifecycle data — destructive, irreversible.

    Wipes every document from the trade-related collections:
    ``comprehensive_trades`` (executed trades + partial legs),
    ``multi_expert_cycles`` (their decision flows) and ``active_trades``
    (live-tracking rows). This only clears persisted analytics — it does NOT
    touch any open position at the broker. Distinct path (``/purge``) so it
    never collides with ``DELETE /trades/{trade_id}``.
    """
    try:
        from packages.db.client import get_client
        from packages.core.config import MONGO_DB_NAME
        client = get_client()
        db = client[MONGO_DB_NAME]
        counts: dict[str, int] = {}
        for coll_name in ("comprehensive_trades", "multi_expert_cycles", "active_trades"):
            try:
                res = await db[coll_name].delete_many({})
                counts[coll_name] = int(res.deleted_count)
            except Exception as exc:  # pragma: no cover - per-collection isolation
                logger.error({"endpoint": "purge"}, f"purge {coll_name} failed: {exc}")
                counts[coll_name] = 0
        total = sum(counts.values())
        logger.info({"endpoint": "purge"}, "purged all trade data", **counts)
        return {"deleted": True, "deleted_count": total, "by_collection": counts}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to purge trade data: {str(e)}")


app.mount("/", StaticFiles(directory="apps/frontend", html=True), name="frontend")
