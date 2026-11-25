from __future__ import annotations

"""Router handling MetaTrader5 broker connectivity."""

from datetime import datetime, timedelta, timezone
import asyncio
import os

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from dataclasses import asdict

from packages.adapters.mt5_adapter import MT5Adapter
from packages.db.client import get_client
from packages.db.models import BrokerSessionDoc, WalletSnapshotDoc
from packages.db.repositories import configs as config_repo, insert_wallet_snapshot
from packages.core.config import (
    MONGO_DB_NAME,
    MT5_RETRY_BASE_DELAY,
    MT5_DEFAULT_MAX_RETRIES,
    MT5_KEEPALIVE_INTERVAL_SECONDS,
)
from packages.core.services.mt5_credentials import (
    load_mt5_credentials,
    save_mt5_credentials,
    selected_mt5_mode,
)
from packages.core.models import Decision, Side, PriceType, Order
from ..utils.datetimes import to_aware_utc
from src.common import logger

router = APIRouter()
adapter = MT5Adapter()
CONFIG_ENV = os.getenv("CONFIG_ENV", "dev")


class Mt5Credentials(BaseModel):
    """Credentials payload for MT5 connection."""

    server: str
    loginId: str
    password: str


async def _get_session(request: Request) -> dict | None:
    """Return cached MT5 session if still valid."""
    now = datetime.now(timezone.utc)
    tokens = getattr(request.app.state, "broker_tokens", {})
    session = tokens.get("mt5")
    mode = selected_mt5_mode()
    if mode:
        creds = load_mt5_credentials()
        if not creds or not session or any(session.get(key) != creds[key] for key in ("server", "login")):
            return None
        if not await asyncio.to_thread(_account_connected, mode, creds):
            return None
    if session:
        try:
            exp = to_aware_utc(session.get("expires_at"))
        except Exception:  # pragma: no cover - defensive
            logger.warning({"broker": "mt5"}, "token expired or invalid, refreshing")
            exp = None
        if exp and exp > now:
            return session

    if mode:
        return None
    client = get_client()
    coll = client[MONGO_DB_NAME]["broker_sessions"]
    doc = await coll.find_one({"broker": "mt5", "active": True}, sort=[("created_at", -1)])
    if doc:
        try:
            exp = to_aware_utc(doc.get("expires_at"))
        except Exception:  # pragma: no cover - defensive
            logger.warning({"broker": "mt5"}, "token expired or invalid, refreshing")
            exp = None
        if exp and exp > now:
            if not hasattr(request.app.state, "broker_tokens"):
                request.app.state.broker_tokens = {}
            doc["expires_at"] = exp.isoformat().replace("+00:00", "Z")
            request.app.state.broker_tokens["mt5"] = doc
            return doc
    return None


def _account_connected(mode: str, creds: dict | None) -> bool:
    if not mode:
        return adapter.is_connected()
    return bool(creds and adapter.is_connected_to(server=creds["server"], login=creds["login"], mode=mode))


@router.get("/status")
async def mt5_status(request: Request):
    """Return the REAL MT5 connection status.

    Reports the live terminal health of the shared adapter (``is_connected``),
    not merely "a session token exists". This is what fixes the dashboard
    showing *Disconnected* while the system is actually connected and trading:
    the adapter is auto-connected at startup and kept alive, so a healthy link
    reads True regardless of the 12-hour token TTL.
    """
    from packages.core.offline_replay import offline_mode

    if offline_mode():
        return {"connected": False, "offline": True}
    mode = selected_mt5_mode()
    try:
        connected = await asyncio.to_thread(_account_connected, mode, load_mt5_credentials() if mode else None)
    except Exception:  # pragma: no cover - defensive; never 500 the badge
        connected = False
    result = {"connected": bool(connected)}
    if mode:
        result["mode"] = mode
    return result


async def _login_and_cache(server: str, login: str, password: str, request: Request):
    """Login to MT5 and cache session details."""

    try:
        adapter.login(server=server, login=login, password=password)
    except Exception as exc:  # pragma: no cover - network failures
        raise HTTPException(status_code=502, detail=str(exc))

    # Persist creds to the local (gitignored) cache so the backend auto-connects
    # on the next startup with no manual click. Best-effort — a disk error must
    # never fail an otherwise-successful login.
    try:
        save_mt5_credentials(server=server, login=login, password=password)
    except Exception as exc:  # pragma: no cover - disk failures
        logger.warning({"broker": "mt5"}, "cred cache write failed", error=str(exc))

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=12)
    session = BrokerSessionDoc(
        broker="mt5",
        server=server,
        login=login,
        expires_at=expires_at,
        active=True,
        created_at=now,
    )
    doc = session.model_dump(mode="json")
    doc["expires_at"] = doc["expires_at"].replace("+00:00", "Z")
    doc["created_at"] = doc["created_at"].replace("+00:00", "Z")
    
    # Store password temporarily in memory for agent reuse (not in DB for security)
    doc["password"] = password
    
    client = get_client()
    coll = client[MONGO_DB_NAME]["broker_sessions"]
    # Don't store password in DB - remove it before saving
    db_doc = doc.copy()
    db_doc.pop("password", None)
    await coll.insert_one(db_doc)

    if not hasattr(request.app.state, "broker_tokens"):
        request.app.state.broker_tokens = {}
    request.app.state.broker_tokens["mt5"] = doc
    return {"status": "ok"}


@router.get("/connect")
async def mt5_connect(request: Request):
    """Attempt login to MT5 using stored configuration."""
    if selected_mt5_mode():
        creds = load_mt5_credentials()
        if creds is None:
            raise HTTPException(status_code=400, detail="Selected MT5 credentials are missing or invalid")
        try:
            await asyncio.to_thread(
                adapter.login, server=creds["server"], login=creds["login"], password=creds["password"],
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        _seed_session(request.app, creds)
        return {"status": "ok"}

    cfg_doc, _ = await config_repo.get_current("brokers", CONFIG_ENV)
    cfg = cfg_doc.get("mt5") if isinstance(cfg_doc, dict) else None
    if not cfg:
        raise HTTPException(status_code=400, detail="mt5 config missing")

    server = cfg.get("server")
    login = cfg.get("loginId") or cfg.get("login")
    password = cfg.get("password")
    if not all(isinstance(x, str) for x in (server, login, password)):
        raise HTTPException(status_code=400, detail="invalid mt5 config")

    return await _login_and_cache(server, login, password, request)


@router.post("/connect")
async def mt5_connect_with_creds(payload: Mt5Credentials, request: Request):
    """Persist credentials then login to MT5."""
    if selected_mt5_mode():
        raise HTTPException(status_code=409, detail="Restart the launcher to switch the selected MT5 account")
    cfg_doc, checksum = await config_repo.get_current("brokers", CONFIG_ENV)
    new_cfg = dict(cfg_doc or {})
    new_cfg["mt5"] = payload.model_dump()
    await config_repo.put("brokers", CONFIG_ENV, new_cfg, if_match_checksum=checksum)

    return await _login_and_cache(payload.server, payload.loginId, payload.password, request)


@router.get("/wallet")
async def mt5_wallet(request: Request):
    """Return the current MT5 wallet snapshot."""
    session = await _get_session(request)
    if session is None:
        raise HTTPException(status_code=401, detail={"needs_reauth": True})

    try:
        info = adapter.wallet_info()
    except Exception:
        # Self-heal: the shared MT5 link may have dropped between keepalive
        # ticks. Reconnect once from the cred cache and retry before surfacing
        # an error, so a transient blip doesn't blank the wallet card.
        creds = load_mt5_credentials()
        if not creds:
            raise HTTPException(
                status_code=502, detail="MT5 not connected and no cached credentials"
            )
        try:
            await adapter.ensure_connection(
                server=creds["server"], login=creds["login"], password=creds["password"]
            )
            info = adapter.wallet_info()
        except Exception as exc:  # pragma: no cover - upstream failures
            raise HTTPException(status_code=502, detail=str(exc))

    snapshot = WalletSnapshotDoc(
        broker="mt5",
        as_of=datetime.now(timezone.utc),
        equity={
            "available_margin": info.get("free_margin", 0.0),
            "used_margin": info.get("margin", 0.0),
            "payin_amount": info.get("balance", 0.0),
        },
    )

    await insert_wallet_snapshot(snapshot)
    if not hasattr(request.app.state, "wallet_snapshots"):
        request.app.state.wallet_snapshots = {}
    request.app.state.wallet_snapshots["mt5"] = snapshot.model_dump()
    return info


@router.post("/disconnect")
async def mt5_disconnect(request: Request):
    """Invalidate the MT5 session and clear cached token."""
    try:
        tokens = getattr(request.app.state, "broker_tokens", {})
        tokens.pop("mt5", None)
    except Exception as exc:  # pragma: no cover - defensive
        logger.error({"broker": "mt5"}, "state cleanup failed", error=str(exc))

    try:
        client = get_client()
        coll = client[MONGO_DB_NAME]["broker_sessions"]
        await coll.update_many({"broker": "mt5", "active": True}, {"$set": {"active": False}})
    except Exception as exc:  # pragma: no cover - defensive
        logger.error({"broker": "mt5"}, "db cleanup failed", error=str(exc))

    return {"disconnected": True}


# ──────────────────────────────────────────────────────────────────────────
# Startup auto-connect + keepalive (the shared dashboard/trading MT5 link)
# ──────────────────────────────────────────────────────────────────────────
def _build_session_token(server: str, login: str, password: str) -> dict:
    """Build the in-memory broker session token (12 h TTL) consumed by
    ``/wallet``, ``/orders`` etc. Mirrors the shape ``_login_and_cache`` writes."""
    now = datetime.now(timezone.utc)
    return {
        "broker": "mt5",
        "server": server,
        "login": login,
        "password": password,
        "expires_at": (now + timedelta(hours=12)).isoformat().replace("+00:00", "Z"),
        "active": True,
        "created_at": now.isoformat().replace("+00:00", "Z"),
    }


def _seed_session(app, creds: dict) -> None:
    """Seed ``app.state.broker_tokens['mt5']`` from cached creds so the wallet
    and order endpoints work immediately after an auto-connect."""
    if not getattr(app.state, "broker_tokens", None):
        app.state.broker_tokens = {}
    app.state.broker_tokens["mt5"] = _build_session_token(
        creds["server"], creds["login"], creds["password"]
    )


async def startup_autoconnect(app) -> None:
    """Log the shared router adapter in from the local cred cache and launch the
    keepalive task. Best-effort: never raises (startup must not crash). If the
    first login fails, the keepalive keeps retrying with backoff."""
    creds = load_mt5_credentials()
    if not creds:
        logger.info({"broker": "mt5"}, "no cached MT5 credentials — skipping auto-connect")
        return
    try:
        await asyncio.to_thread(
            adapter.login,
            server=creds["server"], login=creds["login"], password=creds["password"],
        )
        _seed_session(app, creds)
        logger.info(
            {"broker": "mt5", "server": creds["server"]},
            "MT5 auto-connected from cred cache",
        )
    except Exception as exc:
        logger.error(
            {"broker": "mt5"},
            "MT5 auto-connect failed (keepalive will retry)",
            error=str(exc),
        )
    app.state.mt5_keepalive_task = asyncio.create_task(_keepalive_loop(app))

    # Ghost-position guardian: force-close any broker position this app did not
    # open (manual/foreign trades). Shares the live adapter; self-gates on
    # live-arming + connection, so launching it unconditionally here is safe.
    from packages.core.risk.ghost_guard import ghost_guard_loop
    app.state.mt5_ghost_guard_task = asyncio.create_task(ghost_guard_loop(adapter, app))


async def _keepalive_loop(app) -> None:
    """Periodically verify the shared MT5 link; reconnect with capped backoff
    and re-mint the session token so a healthy link never reports
    'Disconnected'. Survives any per-iteration error — only cancellation stops it."""
    interval = MT5_KEEPALIVE_INTERVAL_SECONDS
    backoff = 0
    while True:
        try:
            await asyncio.sleep(interval)
            creds = load_mt5_credentials()
            if not creds:
                continue
            mode = selected_mt5_mode()
            connected = await asyncio.to_thread(_account_connected, mode, creds)
            if not connected:
                if mode:
                    getattr(app.state, "broker_tokens", {}).pop("mt5", None)
                    try:
                        await adapter.connect(server=creds["server"], login=creds["login"], password=creds["password"])
                        ok = await asyncio.to_thread(_account_connected, mode, creds)
                    except Exception:
                        ok = False
                else:
                    ok = await adapter.ensure_connection(
                        server=creds["server"], login=creds["login"], password=creds["password"]
                    )
                if not ok:
                    backoff = min(backoff + 1, MT5_DEFAULT_MAX_RETRIES + 3)
                    await asyncio.sleep(MT5_RETRY_BASE_DELAY * backoff)
                    continue
                logger.info({"broker": "mt5"}, "MT5 keepalive reconnected")
            backoff = 0
            # Re-mint the token so the 12 h TTL never flips a live link to Disconnected.
            try:
                _seed_session(app, creds)
            except Exception:  # pragma: no cover - defensive
                pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never let the keepalive die
            logger.error({"broker": "mt5"}, "MT5 keepalive iteration failed", error=str(exc))


EQUITY_SAMPLE_INTERVAL_SECONDS = 60


def launch_equity_sampler(app) -> None:
    """Start the background equity sampler if it isn't already running.

    Idempotent; safe to call once from startup. The sampler runs entirely off
    the request/trade path, so it adds no latency to trading."""
    existing = getattr(app.state, "mt5_equity_sampler_task", None)
    if existing is not None and not existing.done():
        return
    app.state.mt5_equity_sampler_task = asyncio.create_task(_equity_sampler_loop(app))


async def _equity_sampler_loop(app) -> None:
    """Periodically snapshot account equity into the ``equity_samples`` series so
    the dashboard can draw a real intraday equity curve.

    Reads live equity from the shared MT5 adapter, keeps a small in-memory ring
    buffer for instant reads, and persists to Mongo (when enabled) for
    durability. Best-effort and fully isolated — only cancellation stops it, and
    it never touches the trading path."""
    from collections import deque
    from packages.db.repositories.equity_samples import insert_equity_sample

    interval = EQUITY_SAMPLE_INTERVAL_SECONDS
    while True:
        try:
            await asyncio.sleep(interval)
            equity = balance = None

            mode = selected_mt5_mode()
            connected = await asyncio.to_thread(_account_connected, mode, load_mt5_credentials() if mode else None)
            if connected:
                bal = await asyncio.to_thread(adapter.balances)
                equity = bal.get("equity")
                balance = bal.get("cash")

            if equity is None:
                continue

            ts = datetime.now(timezone.utc)
            buf = getattr(app.state, "equity_samples_mem", None)
            if buf is None:
                buf = app.state.equity_samples_mem = deque(maxlen=2880)  # ~2 days @60s
            buf.append(
                {
                    "timestamp": ts,
                    "equity": float(equity),
                    "balance": float(balance) if balance is not None else None,
                }
            )

            await insert_equity_sample("mt5", float(equity), balance, ts)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never let the sampler die
            logger.error(
                {"service": "equity_sampler"}, "equity sample iteration failed", error=str(exc)
            )


class OrderPayload(BaseModel):
    symbol: str
    side: str
    quantity: float
    order_type: str = "market"
    price: float | None = None
    sl: float | None = None
    tp: float | None = None
    client_order_id: str | None = None


@router.post("/orders")
async def place_order(payload: OrderPayload, request: Request):
    """Place an order through the MT5 adapter."""
    decision = Decision(
        asset=payload.symbol,
        symbol=payload.symbol,
        side=Side(payload.side.lower()),
        price_type=PriceType(payload.order_type.lower()),
        entry=float(payload.price or 0.0),
        quantity=float(payload.quantity),
        sl=payload.sl,
        tp=payload.tp,
        client_order_id=payload.client_order_id,
    )

    session = await _get_session(request)
    if session is None:
        raise HTTPException(status_code=401, detail={"needs_reauth": True})

    try:
        order, _ = adapter.place(decision)
    except Exception as exc:  # pragma: no cover - upstream failures
        raise HTTPException(status_code=502, detail=str(exc))
    return asdict(order)


@router.put("/orders/{order_id}")
async def modify_order(order_id: str, payload: OrderPayload, request: Request):
    """Modify an existing order via the MT5 adapter."""
    decision = Decision(
        asset=payload.symbol,
        symbol=payload.symbol,
        side=Side(payload.side.lower()),
        price_type=PriceType(payload.order_type.lower()),
        entry=float(payload.price or 0.0),
        quantity=float(payload.quantity),
        sl=payload.sl,
        tp=payload.tp,
        client_order_id=payload.client_order_id,
    )

    session = await _get_session(request)
    if session is None:
        raise HTTPException(status_code=401, detail={"needs_reauth": True})

    order = Order(id=order_id, decision=decision, status="")
    try:
        result = adapter.modify(order, decision)
    except Exception as exc:  # pragma: no cover - upstream failures
        raise HTTPException(status_code=502, detail=str(exc))
    return asdict(result)


@router.delete("/orders/{order_id}")
async def cancel_order(order_id: str, request: Request):
    """Cancel an existing order via the MT5 adapter."""
    session = await _get_session(request)
    if session is None:
        raise HTTPException(status_code=401, detail={"needs_reauth": True})

    dummy_decision = Decision(
        asset="",
        symbol="",
        side=Side.BUY,
        price_type=PriceType.MARKET,
        entry=0.0,
        quantity=0.0,
    )
    order = Order(id=order_id, decision=dummy_decision, status="")
    try:
        adapter.cancel(order)
    except Exception as exc:  # pragma: no cover - upstream failures
        raise HTTPException(status_code=502, detail=str(exc))
    return {"status": "ok"}


@router.get("/positions")
async def list_positions(request: Request):
    """Return open MT5 positions."""
    session = await _get_session(request)
    if session is None:
        raise HTTPException(status_code=401, detail={"needs_reauth": True})
    try:
        positions = adapter.positions()
    except Exception as exc:  # pragma: no cover - upstream failures
        raise HTTPException(status_code=502, detail=str(exc))
    return {"positions": [asdict(p) for p in positions]}


@router.get("/balances")
async def get_balances(request: Request):
    """Return current MT5 account balances."""
    session = await _get_session(request)
    if session is None:
        raise HTTPException(status_code=401, detail={"needs_reauth": True})
    try:
        bal = adapter.balances()
    except Exception as exc:  # pragma: no cover - upstream failures
        raise HTTPException(status_code=502, detail=str(exc))
    return bal


@router.get("/symbol-info")
async def get_symbol_info(symbol: str, request: Request):
    """Return metadata for *symbol* from MT5."""

    session = await _get_session(request)
    if session is None:
        raise HTTPException(status_code=401, detail={"needs_reauth": True})
    try:
        info = adapter.symbol_info(symbol)
    except Exception as exc:  # pragma: no cover - upstream failures
        raise HTTPException(status_code=502, detail=str(exc))
    return info
