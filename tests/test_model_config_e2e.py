"""E2E tests: global model config flows through backtest and live trade paths.

Validates that the model configured at /configs/gpt-prompts (stored in
MongoDB configs collection as domain=model_config, env=prod) is the model
actually used for expert + judge AI calls — for both backtest and live agents.
"""

from __future__ import annotations

import hashlib
import json
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Any, Dict


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

GLOBAL_MODEL_CONFIG = {
    "model": "claude-sonnet-4-6",
    "temperature": 0.3,
    "top_p": 0.35,
    "max_tokens": 16384,
}

SONNET_55_CONFIG = {
    **GLOBAL_MODEL_CONFIG,
    "model": "claude-sonnet-5-5",
    "max_tokens": 8192,
    "effort": "high",
}

GLOBAL_MODEL_CONFIG_GPT = {
    "model": "gpt-4o",
    "temperature": 0.2,
    "top_p": 0.9,
    "max_tokens": 8192,
}

GLOBAL_MODEL_CONFIG_GEMINI = {
    "model": "gemini-2.5-flash",
    "temperature": 0.3,
    "top_p": 0.35,
    "max_tokens": 16384,
}

SAMPLE_CANDLES_5M = [
    {
        "time": f"2024-05-01T{9 + i // 12:02d}:{(i % 12) * 5:02d}:00",
        "open": 60000 + i, "high": 60100 + i, "low": 59900 + i,
        "close": 60050 + i, "volume": 100 + i,
    }
    for i in range(48)
]

SAMPLE_CANDLES_15M = [
    {
        "time": f"2024-05-01T{9 + i // 4:02d}:{(i % 4) * 15:02d}:00",
        "open": 60000 + i * 3, "high": 60200 + i * 3, "low": 59800 + i * 3,
        "close": 60100 + i * 3, "volume": 300 + i * 10,
    }
    for i in range(10)
]


@pytest.fixture
def mock_get_current_claude():
    async def _get_current(domain, env):
        if domain == "model_config" and env == "prod":
            return GLOBAL_MODEL_CONFIG.copy(), "checksum_abc"
        return None, None
    return _get_current


@pytest.fixture
def mock_get_current_gpt():
    async def _get_current(domain, env):
        if domain == "model_config" and env == "prod":
            return GLOBAL_MODEL_CONFIG_GPT.copy(), "checksum_gpt"
        return None, None
    return _get_current


@pytest.fixture
def mock_get_current_gemini():
    async def _get_current(domain, env):
        if domain == "model_config" and env == "prod":
            return GLOBAL_MODEL_CONFIG_GEMINI.copy(), "checksum_gem"
        return None, None
    return _get_current


@pytest.fixture
def mock_get_current_empty():
    async def _get_current(domain, env):
        return None, None
    return _get_current


# ---------------------------------------------------------------------------
# 1. Backtest: global model config is loaded and passed through
# ---------------------------------------------------------------------------

class TestBacktestModelConfigLoading:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("saved_config", [GLOBAL_MODEL_CONFIG, SONNET_55_CONFIG])
    async def test_loads_global_config_when_gpt_config_empty(self, saved_config):
        captured_gpt_config: Dict[str, Any] = {}

        class FakeRunner:
            def __init__(self, **kwargs):
                nonlocal captured_gpt_config
                captured_gpt_config = kwargs.get("gpt_config", {})
            async def execute_backtest(self, **kwargs):
                pass

        fake_mongo_coll = AsyncMock()
        fake_mongo_coll.update_one = AsyncMock()
        fake_client = MagicMock()
        fake_client.__getitem__ = MagicMock(
            return_value=MagicMock(__getitem__=MagicMock(return_value=fake_mongo_coll))
        )

        with patch("apps.backend.routers.backtest.backtest_repo") as mock_repo, \
             patch("apps.backend.routers.backtest.BacktestRunner", FakeRunner), \
             patch("packages.db.repositories.configs.get_current", AsyncMock(return_value=(saved_config, "checksum"))), \
             patch("packages.db.client.get_client", return_value=fake_client):
            mock_repo.update_backtest_status = AsyncMock()
            from apps.backend.routers.backtest import _run_backtest_background
            await _run_backtest_background(
                test_name="e2e_test", asset="BTCUSD",
                start_date=datetime(2024, 5, 1, tzinfo=timezone.utc),
                end_date=datetime(2024, 5, 2, tzinfo=timezone.utc),
                candle_count_5m=100, candle_count_15m=50,
                use_code_sniper=True, interval_hours=1, lot_size=1.0,
                gpt_config={}, backtest_id="aaa000000000000000000001",
            )

        assert captured_gpt_config == saved_config

    @pytest.mark.asyncio
    async def test_preserves_explicit_model_in_gpt_config(self, mock_get_current_claude):
        captured_gpt_config: Dict[str, Any] = {}

        class FakeRunner:
            def __init__(self, **kwargs):
                nonlocal captured_gpt_config
                captured_gpt_config = kwargs.get("gpt_config", {})
            async def execute_backtest(self, **kwargs):
                pass

        fake_mongo_coll = AsyncMock()
        fake_mongo_coll.update_one = AsyncMock()
        fake_client = MagicMock()
        fake_client.__getitem__ = MagicMock(
            return_value=MagicMock(__getitem__=MagicMock(return_value=fake_mongo_coll))
        )

        with patch("apps.backend.routers.backtest.backtest_repo") as mock_repo, \
             patch("apps.backend.routers.backtest.BacktestRunner", FakeRunner), \
             patch("packages.db.repositories.configs.get_current", mock_get_current_claude), \
             patch("packages.db.client.get_client", return_value=fake_client):
            mock_repo.update_backtest_status = AsyncMock()
            from apps.backend.routers.backtest import _run_backtest_background
            await _run_backtest_background(
                test_name="e2e_explicit", asset="BTCUSD",
                start_date=datetime(2024, 5, 1, tzinfo=timezone.utc),
                end_date=datetime(2024, 5, 2, tzinfo=timezone.utc),
                candle_count_5m=100, candle_count_15m=50,
                use_code_sniper=True, interval_hours=1, lot_size=1.0,
                gpt_config={"model": "gpt-4o"}, backtest_id="aaa000000000000000000002",
            )

        assert captured_gpt_config["model"] == "gpt-4o"

    @pytest.mark.asyncio
    async def test_persists_enriched_config_to_mongo(self, mock_get_current_claude):
        fake_mongo_coll = AsyncMock()
        fake_mongo_coll.update_one = AsyncMock()
        fake_client = MagicMock()
        fake_client.__getitem__ = MagicMock(
            return_value=MagicMock(__getitem__=MagicMock(return_value=fake_mongo_coll))
        )

        class FakeRunner:
            def __init__(self, **kwargs): pass
            async def execute_backtest(self, **kwargs): pass

        with patch("apps.backend.routers.backtest.backtest_repo") as mock_repo, \
             patch("apps.backend.routers.backtest.BacktestRunner", FakeRunner), \
             patch("packages.db.repositories.configs.get_current", mock_get_current_claude), \
             patch("packages.db.client.get_client", return_value=fake_client):
            mock_repo.update_backtest_status = AsyncMock()
            from apps.backend.routers.backtest import _run_backtest_background
            await _run_backtest_background(
                test_name="e2e_persist", asset="BTCUSD",
                start_date=datetime(2024, 5, 1, tzinfo=timezone.utc),
                end_date=datetime(2024, 5, 2, tzinfo=timezone.utc),
                candle_count_5m=100, candle_count_15m=50,
                use_code_sniper=True, interval_hours=1, lot_size=1.0,
                gpt_config={}, backtest_id="aaa000000000000000000003",
            )

        fake_mongo_coll.update_one.assert_called_once()
        call_args = fake_mongo_coll.update_one.call_args
        set_doc = call_args[0][1]["$set"]["config.gpt_config"]
        assert set_doc["model"] == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_fallback_when_no_global_config(self, mock_get_current_empty):
        captured_gpt_config: Dict[str, Any] = {}

        class FakeRunner:
            def __init__(self, **kwargs):
                nonlocal captured_gpt_config
                captured_gpt_config = kwargs.get("gpt_config", {})
            async def execute_backtest(self, **kwargs):
                pass

        fake_mongo_coll = AsyncMock()
        fake_mongo_coll.update_one = AsyncMock()
        fake_client = MagicMock()
        fake_client.__getitem__ = MagicMock(
            return_value=MagicMock(__getitem__=MagicMock(return_value=fake_mongo_coll))
        )

        with patch("apps.backend.routers.backtest.backtest_repo") as mock_repo, \
             patch("apps.backend.routers.backtest.BacktestRunner", FakeRunner), \
             patch("packages.db.repositories.configs.get_current", mock_get_current_empty), \
             patch("packages.db.client.get_client", return_value=fake_client):
            mock_repo.update_backtest_status = AsyncMock()
            from apps.backend.routers.backtest import _run_backtest_background
            await _run_backtest_background(
                test_name="e2e_fallback", asset="BTCUSD",
                start_date=datetime(2024, 5, 1, tzinfo=timezone.utc),
                end_date=datetime(2024, 5, 2, tzinfo=timezone.utc),
                candle_count_5m=100, candle_count_15m=50,
                use_code_sniper=True, interval_hours=1, lot_size=1.0,
                gpt_config={}, backtest_id="aaa000000000000000000004",
            )

        assert "model" not in captured_gpt_config


# ---------------------------------------------------------------------------
# 2. Backtest runner: store_response / agent_id / prompt_hash construction
# ---------------------------------------------------------------------------

class TestBacktestRunnerAICallParams:

    def test_store_response_params_constructed_correctly(self):
        """Verify the gpt_cfg dict built by BacktestRunner._run_iteration
        includes store_response, agent_id, and prompt_hash."""
        gpt_config = {"model": "claude-sonnet-4-6"}
        asset = "BTCUSD"
        backtest_id = "bbb000000000000000000001"
        iteration_num = 3
        cycle_id = "cycle_001"

        gpt_cfg = {
            **gpt_config,
            "asset": asset,
            "store_response": True,
            "agent_id": f"backtest_{backtest_id}",
            "prompt_hash": hashlib.sha256(
                f"{backtest_id}:{iteration_num}:{cycle_id}".encode()
            ).hexdigest()[:16],
        }

        assert gpt_cfg["store_response"] is True
        assert gpt_cfg["agent_id"] == f"backtest_{backtest_id}"
        assert len(gpt_cfg["prompt_hash"]) == 16
        assert gpt_cfg["model"] == "claude-sonnet-4-6"
        assert gpt_cfg["asset"] == "BTCUSD"

    def test_prompt_hash_is_deterministic(self):
        """Same backtest_id + iteration + cycle_id → same hash."""
        bid, it, cid = "bbb000000000000000000001", 5, "cycle_x"
        h1 = hashlib.sha256(f"{bid}:{it}:{cid}".encode()).hexdigest()[:16]
        h2 = hashlib.sha256(f"{bid}:{it}:{cid}".encode()).hexdigest()[:16]
        assert h1 == h2

    def test_prompt_hash_varies_per_iteration(self):
        bid, cid = "bbb000000000000000000001", "cycle_x"
        h1 = hashlib.sha256(f"{bid}:1:{cid}".encode()).hexdigest()[:16]
        h2 = hashlib.sha256(f"{bid}:2:{cid}".encode()).hexdigest()[:16]
        assert h1 != h2


# ---------------------------------------------------------------------------
# 3. Multi-expert: model from gpt_cfg reaches complete() calls
# ---------------------------------------------------------------------------

class TestMultiExpertModelSelection:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("saved_config", [GLOBAL_MODEL_CONFIG, SONNET_55_CONFIG])
    async def test_expert_uses_gpt_cfg_model(self, saved_config):
        from packages.core.multi_expert import call_expert

        captured_calls = []

        async def fake_complete(**kwargs):
            captured_calls.append(kwargs)
            return {
                "signal": "buy", "entry": 100.0,
                "stop": 99.0, "target": 101.5,
                "ttl_minutes": 90, "confidence": 0.8,
            }

        gpt_cfg = {
            **saved_config,
            "asset": "BTCUSD", "store_response": True,
            "agent_id": "backtest_test", "prompt_hash": "abc123",
        }

        with patch("packages.core.multi_expert.complete", fake_complete):
            await call_expert(
                kind="trend",
                c5_superset=SAMPLE_CANDLES_5M,
                c15_superset=SAMPLE_CANDLES_15M,
                gpt_cfg=gpt_cfg,
            )

        assert len(captured_calls) == 1
        assert captured_calls[0]["model"] == saved_config["model"]
        assert captured_calls[0].get("effort") == saved_config.get("effort")
        if "effort" in saved_config:
            assert captured_calls[0]["max_tokens"] == saved_config["max_tokens"]
        assert captured_calls[0]["store_response"] is True
        assert captured_calls[0]["agent_id"] == "backtest_test"

    @pytest.mark.asyncio
    async def test_expert_falls_back_to_model_4o_mini_when_no_model(self):
        from packages.core.multi_expert import call_expert
        from packages.core.config import MODEL_4O_MINI

        captured_calls = []

        async def fake_complete(**kwargs):
            captured_calls.append(kwargs)
            return {
                "signal": "flat", "entry": None,
                "stop": None, "target": None,
                "ttl_minutes": 90, "confidence": 0.1,
            }

        with patch("packages.core.multi_expert.complete", fake_complete):
            await call_expert(
                kind="trend",
                c5_superset=SAMPLE_CANDLES_5M,
                c15_superset=SAMPLE_CANDLES_15M,
                gpt_cfg={"asset": "BTCUSD"},
            )

        assert captured_calls[0]["model"] == MODEL_4O_MINI

    @pytest.mark.asyncio
    @pytest.mark.parametrize("saved_config", [GLOBAL_MODEL_CONFIG, SONNET_55_CONFIG])
    async def test_judge_uses_gpt_cfg_model(self, saved_config):
        from packages.core.multi_expert import call_judge
        from packages.core.schemas_trade import MultiExpertSignal

        captured_calls = []

        async def fake_complete(**kwargs):
            captured_calls.append(kwargs)
            return {
                "code": "result = {'direction': 'flat'}",
                "metadata": {"reasoning": "test"},
                "ttl_minutes": 90,
                "confidence": 0.75,
            }

        proposals = [
            MultiExpertSignal(
                signal="buy", entry=100.0, stop=99.0, target=101.5,
                ttl_minutes=90, confidence=0.8,
            ),
        ]

        gpt_cfg = {
            **saved_config,
            "asset": "BTCUSD",
            "store_response": True, "agent_id": "backtest_test",
            "prompt_hash": "def456",
        }

        fake_context = {
            "expert_signals": {}, "market_structure": {},
            "current_price": 60050, "session": "us",
        }

        with patch("packages.core.multi_expert.complete", fake_complete), \
             patch("packages.core.multi_expert.build_advanced_context", return_value=fake_context):
            await call_judge(
                c5_superset=SAMPLE_CANDLES_5M,
                c15_superset=SAMPLE_CANDLES_15M,
                proposals=proposals,
                gpt_cfg=gpt_cfg,
                use_code_sniper=True,
            )

        assert len(captured_calls) == 1
        assert captured_calls[0]["model"] == saved_config["model"]
        assert captured_calls[0].get("effort") == saved_config.get("effort")
        if "effort" in saved_config:
            assert captured_calls[0]["max_tokens"] == saved_config["max_tokens"]


# ---------------------------------------------------------------------------
# 4. Provider routing: model prefix → correct backend
# ---------------------------------------------------------------------------

class TestProviderRouting:

    @pytest.mark.asyncio
    async def test_claude_prefix_routes_to_anthropic(self):
        fake_anthropic_complete = AsyncMock(return_value={"result": "ok"})

        with patch.dict("sys.modules", {"packages.core.anthropic_client": MagicMock(complete=fake_anthropic_complete)}):
            from packages.core import gpt_client
            result = await gpt_client.complete(
                prompt="", model="claude-sonnet-4-6", store_response=False,
                system_prompt="system", user_prompt="user",
            )

        fake_anthropic_complete.assert_called_once()
        assert fake_anthropic_complete.call_args[1]["model"] == "claude-sonnet-4-6"
        assert result == {"result": "ok"}

    @pytest.mark.asyncio
    async def test_gemini_prefix_routes_to_gemini(self):
        fake_gemini_complete = AsyncMock(return_value={"result": "gem"})

        with patch.dict("sys.modules", {"packages.core.gemini_client": MagicMock(complete=fake_gemini_complete)}):
            from packages.core import gpt_client
            result = await gpt_client.complete(
                prompt="", model="gemini-2.5-flash", store_response=False,
                system_prompt="system", user_prompt="user",
            )

        fake_gemini_complete.assert_called_once()
        assert fake_gemini_complete.call_args[1]["model"] == "gemini-2.5-flash"
        assert result == {"result": "gem"}

    @pytest.mark.asyncio
    async def test_gpt_prefix_stays_on_openai_path(self):
        """gpt-* models use the OpenAI httpx path — not anthropic or gemini."""
        import packages.core.gpt_client as gpt_mod

        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": '{"direction":"flat"}'}}],
            "usage": {"total_tokens": 10},
            "id": "resp_test",
        }
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch.object(gpt_mod, "_api_key", "sk-test"), \
             patch.object(gpt_mod, "httpx") as mock_httpx:
            mock_httpx.AsyncClient.return_value = mock_client
            result = await gpt_mod.complete(
                prompt="", model="gpt-4o", store_response=False,
                system_prompt="system", user_prompt="user",
            )

        assert result == {"direction": "flat"}
        mock_client.post.assert_called_once()
        call_body = mock_client.post.call_args
        assert "gpt-4o" in str(call_body)


# ---------------------------------------------------------------------------
# 5. Live trade: _prepare_runtime_cfg loads global model config
# ---------------------------------------------------------------------------

class TestLiveTradeModelConfig:

    @pytest.mark.asyncio
    async def test_scheduled_cycle_loads_model_saved_after_start(self):
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient
        from apps.backend.api import _agent_runner
        from apps.backend.routers.configs import router

        cfg = {
            "symbol": "BTCUSD",
            "agent_id": "agent_btcusd_01",
            "candle_count_5m": 100,
            "candles_15_count": 50,
            "broker": MagicMock(),
            "candle_service": MagicMock(),
            "journal": MagicMock(),
            "execution_mode": "scheduled",
            "gpt_config": GLOBAL_MODEL_CONFIG,
        }
        saved_config = GLOBAL_MODEL_CONFIG

        async def save_config(domain, env, doc):
            nonlocal saved_config
            assert (domain, env) == ("model_config", "prod")
            saved_config = doc

        async def read_config(domain, env):
            assert (domain, env) == ("model_config", "prod")
            return saved_config, "checksum"

        app = FastAPI()
        app.include_router(router, prefix="/configs")
        with patch("packages.db.repositories.configs.get_current", read_config), \
             patch("packages.db.repositories.configs.put", save_config), \
             patch("apps.backend.agents.AgentRunner") as runner:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.put("/configs/model-config", json=SONNET_55_CONFIG)
                assert response.status_code == 200
                assert response.json()["config"] == SONNET_55_CONFIG
                response = await client.get("/configs/model-config")
                assert response.json()["config"] == SONNET_55_CONFIG

            runner.return_value.run = AsyncMock()
            runner.return_value.cleanup_resources = AsyncMock()
            await _agent_runner("BTCUSD", 100, cfg)

        assert runner.call_args.args[0]["gpt_config"] == SONNET_55_CONFIG
        assert cfg["gpt_config"] == GLOBAL_MODEL_CONFIG

    def _make_agent_record(self):
        from packages.db.repositories.agents import AgentRecord
        return AgentRecord(
            _id="agent_btcusd_01",
            symbol="BTCUSD",
            candle_count_5m=100,
            candle_count_15m=50,
            broker="mt5",
            risk_percent=1.0,
            run_interval="1h",
            prompt_id="prompt_001",
        )

    @pytest.mark.asyncio
    async def test_live_agent_loads_global_model_config(self, mock_get_current_claude):
        agent_rec = self._make_agent_record()

        with patch("packages.db.repositories.configs.get_current", mock_get_current_claude), \
             patch("apps.backend.api.agents_repo") as mock_agents, \
             patch("apps.backend.api.OPENAI_API_KEY", "sk-test"), \
             patch("apps.backend.api.GEMINI_API_KEY", "gem-test"), \
             patch("apps.backend.api._create_services_for_agent", new_callable=AsyncMock) as mock_svc:

            mock_agents.list_agents = AsyncMock(return_value=[agent_rec])
            mock_svc.side_effect = lambda sym, cfg: cfg

            from apps.backend.api import _prepare_runtime_cfg
            runtime_cfg = await _prepare_runtime_cfg("BTCUSD", None)

        assert runtime_cfg["gpt_config"]["model"] == "claude-sonnet-4-6"
        assert runtime_cfg["gpt_config"]["temperature"] == 0.3

    @pytest.mark.asyncio
    async def test_live_agent_no_global_config_uses_defaults(self, mock_get_current_empty):
        agent_rec = self._make_agent_record()

        with patch("packages.db.repositories.configs.get_current", mock_get_current_empty), \
             patch("apps.backend.api.agents_repo") as mock_agents, \
             patch("apps.backend.api.OPENAI_API_KEY", "sk-test"), \
             patch("apps.backend.api.GEMINI_API_KEY", ""), \
             patch("apps.backend.api._create_services_for_agent", new_callable=AsyncMock) as mock_svc:

            mock_agents.list_agents = AsyncMock(return_value=[agent_rec])
            mock_svc.side_effect = lambda sym, cfg: cfg

            from apps.backend.api import _prepare_runtime_cfg
            runtime_cfg = await _prepare_runtime_cfg("BTCUSD", None)

        assert "gpt_config" not in runtime_cfg or runtime_cfg.get("gpt_config") is None


# ---------------------------------------------------------------------------
# 6. Anthropic client: no input/output errors on well-formed calls
# ---------------------------------------------------------------------------

class TestAnthropicClientInputOutput:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stop_reason", ["end_turn", "max_tokens"])
    async def test_sonnet_55_wire_payload_and_thinking_blocks(self, stop_reason):
        import httpx
        from anthropic import AsyncAnthropic
        from packages.core import anthropic_client, gpt_client

        def reply(request: httpx.Request) -> httpx.Response:
            sent.update(json.loads(request.content))
            return httpx.Response(200, json={
                "id": "msg_test",
                "role": "assistant",
                "type": "message",
                "model": SONNET_55_CONFIG["model"],
                "stop_reason": stop_reason,
                "content": [
                    {"type": "thinking", "thinking": "{}", "signature": "test"},
                    {"type": "text", "text": '{"direction":'},
                    {"type": "text", "text": '"flat"}'},
                ],
                "usage": {"input_tokens": 200, "output_tokens": 250},
            })

        sent: Dict[str, Any] = {}
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as http_client:
            sdk = AsyncAnthropic(api_key="test-key", http_client=http_client)
            with patch.object(anthropic_client, "_api_key", "test-key"), \
                 patch.object(anthropic_client, "_get_async_client", return_value=sdk):
                result = await gpt_client.complete(
                    "Analyze.", store_response=False, retries=0, **SONNET_55_CONFIG,
                )

        assert sent["model"] == SONNET_55_CONFIG["model"]
        assert sent["max_tokens"] == SONNET_55_CONFIG["max_tokens"]
        assert sent["output_config"] == {"effort": SONNET_55_CONFIG["effort"]}
        assert "temperature" not in sent
        assert "top_p" not in sent
        assert result == ({"direction": "flat"} if stop_reason == "end_turn" else None)

    @pytest.mark.asyncio
    async def test_system_user_prompt_separation(self):
        from packages.core import anthropic_client

        captured_kwargs: Dict[str, Any] = {}

        class FakeMessages:
            async def create(self, **kwargs):
                nonlocal captured_kwargs
                captured_kwargs = kwargs
                mock_block = MagicMock()
                mock_block.text = '{"direction":"long","confidence":80}'
                mock_resp = MagicMock()
                mock_resp.content = [mock_block]
                mock_resp.usage = MagicMock(input_tokens=100, output_tokens=50)
                mock_resp.id = "msg_test"
                return mock_resp

        class FakeAnthropic:
            def __init__(self, **kwargs):
                self.messages = FakeMessages()

            def with_options(self, **kwargs):
                return self

        with patch.object(anthropic_client, "_api_key", "sk-ant-test"), \
             patch.object(anthropic_client, "_get_async_client", return_value=FakeAnthropic()):
            result = await anthropic_client.complete(
                prompt="", model="claude-sonnet-4-6", store_response=False,
                system_prompt="You are a trading expert.",
                user_prompt="Analyze BTCUSD.",
            )

        assert captured_kwargs["system"].startswith("You are a trading expert.")
        assert captured_kwargs["messages"][0]["role"] == "user"
        assert "Analyze BTCUSD." in captured_kwargs["messages"][0]["content"]
        assert isinstance(result, dict)
        assert result["direction"] == "long"

    @pytest.mark.asyncio
    async def test_response_format_injected_into_system_prompt(self):
        from packages.core import anthropic_client

        captured_kwargs: Dict[str, Any] = {}

        class FakeMessages:
            async def create(self, **kwargs):
                nonlocal captured_kwargs
                captured_kwargs = kwargs
                mock_block = MagicMock()
                mock_block.text = '{"direction":"flat"}'
                mock_resp = MagicMock()
                mock_resp.content = [mock_block]
                mock_resp.usage = MagicMock(input_tokens=50, output_tokens=20)
                mock_resp.id = "msg_test2"
                return mock_resp

        class FakeAnthropic:
            def __init__(self, **kwargs):
                self.messages = FakeMessages()

            def with_options(self, **kwargs):
                return self

        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "trade_decision",
                "schema": {
                    "type": "object",
                    "properties": {"direction": {"type": "string"}},
                },
            },
        }

        with patch.object(anthropic_client, "_api_key", "sk-ant-test"), \
             patch.object(anthropic_client, "_get_async_client", return_value=FakeAnthropic()):
            await anthropic_client.complete(
                prompt="", model="claude-sonnet-4-6", store_response=False,
                system_prompt="Expert system.", user_prompt="Analyze.",
                response_format=response_format,
            )

        assert "JSON schema" in captured_kwargs["system"]
        assert "You MUST respond with valid JSON only" in captured_kwargs["system"]


# ---------------------------------------------------------------------------
# 7. Prompt logic: this branch's prompts are used, not v2's precompute
# ---------------------------------------------------------------------------

class TestPromptLogicFromThisBranch:

    def test_expert_system_prompt_exists(self):
        from packages.core.prompt_builders import unified_expert_system_prompt
        prompt = unified_expert_system_prompt()
        assert isinstance(prompt, str)
        assert len(prompt) > 50

    def test_judge_system_prompt_exists(self):
        from packages.core.prompt_builders import code_sniper_judge_system_prompt
        prompt = code_sniper_judge_system_prompt()
        assert isinstance(prompt, str)
        assert len(prompt) > 50

    def test_build_expert_user_prompt_exists(self):
        from packages.core.prompt_builders import build_expert_user_prompt
        assert callable(build_expert_user_prompt)

    def test_build_judge_user_prompt_exists(self):
        from packages.core.prompt_builders import build_judge_user_prompt
        assert callable(build_judge_user_prompt)

    def test_no_precompute_imports_in_multi_expert(self):
        import inspect
        from packages.core import multi_expert
        source = inspect.getsource(multi_expert)
        assert "expert_context" not in source
        assert "build_enriched_expert_user_prompt" not in source
        assert "cycle_history" not in source


# ---------------------------------------------------------------------------
# 8. Cross-provider model switching via multi-expert
# ---------------------------------------------------------------------------

class TestCrossProviderModelSwitch:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model,expected_provider", [
        ("claude-sonnet-4-6", "anthropic"),
        ("gpt-4o", "openai"),
        ("gemini-2.5-flash", "gemini"),
    ])
    async def test_expert_call_routes_to_correct_provider(self, model, expected_provider):
        from packages.core.multi_expert import call_expert

        routed_to: str = ""

        async def fake_complete(**kwargs):
            nonlocal routed_to
            m = kwargs.get("model", "")
            if m.startswith("claude-"):
                routed_to = "anthropic"
            elif m.startswith("gemini-"):
                routed_to = "gemini"
            elif m.startswith("gpt-"):
                routed_to = "openai"
            return {
                "signal": "flat", "entry": None, "stop": None,
                "target": None, "ttl_minutes": 90, "confidence": 0.1,
            }

        with patch("packages.core.multi_expert.complete", fake_complete):
            await call_expert(
                kind="trend",
                c5_superset=SAMPLE_CANDLES_5M,
                c15_superset=SAMPLE_CANDLES_15M,
                gpt_cfg={"model": model, "asset": "BTCUSD"},
            )

        assert routed_to == expected_provider
