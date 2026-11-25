"""Simple multi-agent concurrency test focusing on isolation."""

import pytest
import asyncio
from unittest.mock import patch, AsyncMock
from packages.core.multi_expert import call_expert


class TestSimpleConcurrency:
    """Simple concurrency tests focusing on core isolation."""
    
    @pytest.fixture
    def simple_candles(self):
        """Simple candle data that will pass validation."""
        # Small range candles for predictable validation
        c5_candles = [{"high": 105, "low": 95, "close": 100, "open": 99} for _ in range(10)]
        c15_candles = [{"high": 105, "low": 95, "close": 100, "open": 99} for _ in range(5)]
        return c5_candles, c15_candles
    
    @pytest.mark.asyncio
    async def test_concurrent_expert_calls_isolation(self, simple_candles):
        """Test that concurrent expert calls maintain isolation and complete successfully."""
        
        c5_candles, c15_candles = simple_candles
        
        # Create 3 different agent configurations
        agents = [
            {"agent_id": "agent_A", "asset": "ASSET_A", "base_price": 100.0},
            {"agent_id": "agent_B", "asset": "ASSET_B", "base_price": 200.0},
            {"agent_id": "agent_C", "asset": "ASSET_C", "base_price": 300.0},
        ]
        
        # Function to simulate single expert call for an agent
        async def simulate_expert_call(agent_info):
            agent_id = agent_info["agent_id"]
            asset = agent_info["asset"]
            base_price = agent_info["base_price"]
            
            gpt_config = {
                "asset": asset,
                "agent_id": agent_id,
                "broker_type": f"broker_{agent_id}",
                "prompt_hash": f"hash_{agent_id}",
                "model": "gpt-4o-mini",
            }
            
            # Agent-specific response
            agent_response = {
                "signal": "buy",
                "entry": base_price,
                "stop": base_price - 5.0,
                "target": base_price + 15.0,  # Good RR
                "ttl_minutes": 90,
                "confidence": 0.8,
                "active_window_c5": 50,
                "active_window_c15": 30,
                "need_more_candles": False,
                "rationale": f"Analysis for {agent_id} at price {base_price}",
            }
            
            # Use isolated mock for this specific agent
            with patch('packages.core.multi_expert.complete') as mock_complete:
                mock_complete.return_value = agent_response
                
                # Call individual expert (simpler than full multi-expert)
                signal, expert_ms = await call_expert("trend", c5_candles, c15_candles, gpt_config)
                
                return {
                    "agent_id": agent_id,
                    "asset": asset,
                    "signal": signal,
                    "expert_ms": expert_ms,
                    "expected_entry": base_price,
                }
        
        # Launch all expert calls concurrently
        tasks = [simulate_expert_call(agent) for agent in agents]
        results = await asyncio.gather(*tasks)
        
        # Verify all completed successfully
        assert len(results) == 3, "All 3 expert calls should complete"
        
        # Verify isolation - each agent should have its own data
        for i, result in enumerate(results):
            expected_agent = agents[i]
            
            # Verify agent identity
            assert result["agent_id"] == expected_agent["agent_id"]
            assert result["asset"] == expected_agent["asset"]
            
            # Verify agent-specific data (no cross-talk)
            assert result["signal"].entry == expected_agent["base_price"]
            assert result["expected_entry"] == expected_agent["base_price"]
            
            # Verify timing was captured (may be 0.0 with mocks)
            assert result["expert_ms"] >= 0
            
        # Verify uniqueness (no interference)
        entry_prices = [result["signal"].entry for result in results]
        assert len(set(entry_prices)) == 3, "Each agent should have unique entry price"
        assert 100.0 in entry_prices
        assert 200.0 in entry_prices
        assert 300.0 in entry_prices
        
        print("✅ Concurrent expert calls: All agents isolated successfully")
    
    @pytest.mark.asyncio
    async def test_high_concurrency_stress(self, simple_candles):
        """Stress test with many concurrent agents to verify orchestrator reentrancy."""
        
        c5_candles, c15_candles = simple_candles
        num_agents = 20
        
        async def agent_task(agent_num):
            """Single agent task."""
            agent_id = f"stress_agent_{agent_num:03d}"
            base_price = 100.0 + agent_num
            
            gpt_config = {
                "asset": f"STRESS_ASSET_{agent_num}",
                "agent_id": agent_id,
                "model": "gpt-4o-mini",
            }
            
            response = {
                "signal": "buy",
                "entry": base_price,
                "stop": base_price - 5.0,
                "target": base_price + 15.0,
                "ttl_minutes": 90,
                "confidence": 0.8,
                "active_window_c5": 50,
                "active_window_c15": 30,
                "need_more_candles": False,
            }
            
            with patch('packages.core.multi_expert.complete') as mock:
                mock.return_value = response
                signal, timing = await call_expert("trend", c5_candles, c15_candles, gpt_config)
                return {"agent_num": agent_num, "entry": signal.entry, "timing": timing}
        
        # Launch many concurrent tasks
        tasks = [agent_task(i) for i in range(num_agents)]
        start_time = asyncio.get_event_loop().time()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        end_time = asyncio.get_event_loop().time()
        
        # Verify no exceptions
        exceptions = [r for r in results if isinstance(r, Exception)]
        assert len(exceptions) == 0, f"No exceptions in stress test: {exceptions}"
        
        # Verify all completed
        successful = [r for r in results if not isinstance(r, Exception)]
        assert len(successful) == num_agents, f"All {num_agents} should complete"
        
        # Verify isolation
        entries = [r["entry"] for r in successful]
        expected_entries = [100.0 + i for i in range(num_agents)]
        assert sorted(entries) == sorted(expected_entries), "Each agent should have unique entry"
        
        print(f"✅ High concurrency stress: {num_agents} agents completed in {end_time - start_time:.2f}s")
    
    @pytest.mark.asyncio 
    async def test_concurrent_different_expert_types(self, simple_candles):
        """Concurrent calls to the single unified ('trend') expert stay isolated.

        There is only one expert kind now; this verifies that several concurrent
        invocations across distinct assets do not cross-contaminate results.
        """
        c5_candles, c15_candles = simple_candles

        expert_types = ["trend", "trend", "trend"]
        base_prices = [150.0, 250.0, 350.0]
        
        async def expert_type_task(expert_type, base_price, agent_num):
            """Task for specific expert type."""
            gpt_config = {
                "asset": f"EXPERT_ASSET_{agent_num}",
                "agent_id": f"expert_{expert_type}_{agent_num}",
                "model": "gpt-4o-mini",
            }
            
            response = {
                "signal": "buy",
                "entry": base_price,
                "stop": base_price - 5.0,
                "target": base_price + 15.0,
                "ttl_minutes": 90,
                "confidence": 0.8,
                "active_window_c5": 50,
                "active_window_c15": 30,
                "need_more_candles": False,
            }
            
            with patch('packages.core.multi_expert.complete') as mock:
                mock.return_value = response
                signal, timing = await call_expert(expert_type, c5_candles, c15_candles, gpt_config)
                return {
                    "expert_type": expert_type,
                    "agent_num": agent_num,
                    "entry": signal.entry,
                    "expected_entry": base_price,
                    "timing": timing
                }
        
        # Launch different expert types concurrently
        tasks = [
            expert_type_task(expert_types[i], base_prices[i], i)
            for i in range(len(expert_types))
        ]
        
        results = await asyncio.gather(*tasks)
        
        # Verify each expert type worked correctly
        for i, result in enumerate(results):
            assert result["expert_type"] == expert_types[i]
            assert result["entry"] == base_prices[i]
            assert result["expected_entry"] == base_prices[i]
            assert result["timing"] >= 0  # May be 0.0 with mocks
        
        # Verify different expert types don't interfere
        entries = [r["entry"] for r in results]
        assert len(set(entries)) == 3, "Each expert type should have unique entry"
        
        print("✅ Concurrent different expert types: All expert types isolated successfully")
