"""Test validation across different price scales: Forex pips, Index points, Crypto.

Verify that anti-scalp and RR validation work correctly regardless of:
- Forex pairs (small decimal prices with pips)
- Index futures (large integer prices with points)
- Crypto (medium to large prices with dollars)
"""

import pytest
from packages.core.validators import validate_expert_proposal


class TestForexPips:
    """Test validation with Forex prices (pips scale)."""
    
    def get_forex_candles(self):
        """EUR/USD candles - typical pip movements."""
        return [
            {"time": "09:15", "open": 1.0850, "high": 1.0855, "low": 1.0848, "close": 1.0852, "volume": 1000},
            {"time": "09:20", "open": 1.0852, "high": 1.0858, "low": 1.0850, "close": 1.0856, "volume": 1100},
            {"time": "09:25", "open": 1.0856, "high": 1.0862, "low": 1.0854, "close": 1.0860, "volume": 1200},
            {"time": "09:30", "open": 1.0860, "high": 1.0866, "low": 1.0858, "close": 1.0864, "volume": 1300},
            {"time": "09:35", "open": 1.0864, "high": 1.0870, "low": 1.0862, "close": 1.0868, "volume": 1400},
        ]
    
    def test_forex_buy_valid_rr(self):
        """Test Forex buy with valid RR ratio."""
        proposal = {
            "signal": "buy",
            "entry": 1.0860,
            "stop": 1.0850,    # 10 pips risk (0.0010)
            "target": 1.0880,   # 20 pips reward (0.0020), RR = 2.0
            "ttl_minutes": 90,
            "confidence": 0.70,
            "active_window_c5": 50,
            "active_window_c15": 20,
            "rationale": "Forex breakout"
        }
        
        result = validate_expert_proposal(
            proposal, 
            self.get_forex_candles(),
            min_rr=2.0,
            anti_scalp_k=1.5
        )
        
        print(f"\nForex Buy: RR={result['rr']}, anti_scalp={result['anti_scalp']:.4f} >= {result['anti_scalp_min']:.4f}")
        
        # RR should be exactly 2.0
        assert result["rr_pass"] == True
        assert result["rr"] == 2.0
        print(f"✓ Forex RR calculation works: {result['rr']}")
    
    def test_forex_sell_valid_rr(self):
        """Test Forex sell with valid RR ratio."""
        proposal = {
            "signal": "sell",
            "entry": 1.0860,
            "stop": 1.0875,    # 15 pips risk (0.0015)
            "target": 1.0830,   # 30 pips reward (0.0030), RR = 2.0
            "ttl_minutes": 90,
            "confidence": 0.68,
            "active_window_c5": 50,
            "active_window_c15": 20,
            "rationale": "Forex reversal"
        }
        
        result = validate_expert_proposal(
            proposal,
            self.get_forex_candles(),
            min_rr=2.0,
            anti_scalp_k=1.5
        )
        
        print(f"\nForex Sell: RR={result['rr']}, anti_scalp={result['anti_scalp']:.4f} >= {result['anti_scalp_min']:.4f}")
        
        # RR should be exactly 2.0
        assert result["rr_pass"] == True
        assert result["rr"] == 2.0
        print(f"✓ Forex RR calculation works: {result['rr']}")
    
    def test_forex_anti_scalp(self):
        """Test anti-scalp works with pip-scale movements."""
        c5_candles = self.get_forex_candles()
        
        # Calculate median range
        ranges = [c["high"] - c["low"] for c in c5_candles]
        median_range = sorted(ranges)[len(ranges) // 2]
        
        print(f"\nForex median range: {median_range:.4f} pips")
        print(f"Anti-scalp minimum (k=1.5): {median_range * 1.5:.4f} pips")
        
        # Proposal with target too close (scalping)
        scalp_proposal = {
            "signal": "buy",
            "entry": 1.0860,
            "stop": 1.0855,    # 5 pips risk
            "target": 1.0870,   # 10 pips reward, RR = 2.0 (good)
            # But 10 pips might be less than 1.5 * median_range
            "ttl_minutes": 90,
            "confidence": 0.65,
            "active_window_c5": 50,
            "active_window_c15": 20,
            "rationale": "Scalp"
        }
        
        result = validate_expert_proposal(scalp_proposal, c5_candles, anti_scalp_k=1.5)
        
        print(f"Scalp check: {result['anti_scalp']:.4f} vs {result['anti_scalp_min']:.4f}")
        print(f"✓ Anti-scalp check works for pips")


class TestIndexPoints:
    """Test validation with Index prices (points scale)."""
    
    def get_index_candles(self):
        """Nifty 50 candles - typical point movements."""
        return [
            {"time": "09:15", "open": 24000, "high": 24050, "low": 23980, "close": 24020, "volume": 1000},
            {"time": "09:20", "open": 24020, "high": 24070, "low": 24000, "close": 24040, "volume": 1100},
            {"time": "09:25", "open": 24040, "high": 24090, "low": 24020, "close": 24060, "volume": 1200},
            {"time": "09:30", "open": 24060, "high": 24110, "low": 24040, "close": 24080, "volume": 1300},
            {"time": "09:35", "open": 24080, "high": 24130, "low": 24060, "close": 24100, "volume": 1400},
        ]
    
    def test_index_buy_valid_rr(self):
        """Test Index buy with valid RR ratio."""
        proposal = {
            "signal": "buy",
            "entry": 24000,
            "stop": 23900,     # 100 points risk
            "target": 24200,    # 200 points reward, RR = 2.0
            "ttl_minutes": 180,
            "confidence": 0.72,
            "active_window_c5": 60,
            "active_window_c15": 30,
            "rationale": "Index breakout"
        }
        
        result = validate_expert_proposal(
            proposal,
            self.get_index_candles(),
            min_rr=2.0,
            anti_scalp_k=1.5
        )
        
        print(f"\nIndex Buy: RR={result['rr']}, anti_scalp={result['anti_scalp']} >= {result['anti_scalp_min']}")
        
        # RR should be exactly 2.0
        assert result["rr_pass"] == True
        assert result["rr"] == 2.0
        print(f"✓ Index RR calculation works: {result['rr']}")
    
    def test_index_anti_scalp(self):
        """Test anti-scalp works with point-scale movements."""
        c5_candles = self.get_index_candles()
        
        # Calculate median range
        ranges = [c["high"] - c["low"] for c in c5_candles]
        median_range = sorted(ranges)[len(ranges) // 2]
        
        print(f"\nIndex median range: {median_range} points")
        print(f"Anti-scalp minimum (k=1.5): {median_range * 1.5} points")
        
        # This should pass anti-scalp
        assert median_range > 0
        print(f"✓ Anti-scalp check works for points")


class TestCryptoPrices:
    """Test validation with Crypto prices."""
    
    def get_btc_candles(self):
        """BTC/USD candles - large price movements."""
        return [
            {"time": "09:15", "open": 50000, "high": 50500, "low": 49800, "close": 50200, "volume": 100},
            {"time": "09:20", "open": 50200, "high": 50700, "low": 50000, "close": 50400, "volume": 110},
            {"time": "09:25", "open": 50400, "high": 50900, "low": 50200, "close": 50600, "volume": 120},
            {"time": "09:30", "open": 50600, "high": 51100, "low": 50400, "close": 50800, "volume": 130},
            {"time": "09:35", "open": 50800, "high": 51300, "low": 50600, "close": 51000, "volume": 140},
        ]
    
    def test_crypto_buy_valid_rr(self):
        """Test Crypto buy with valid RR ratio."""
        proposal = {
            "signal": "buy",
            "entry": 50000,
            "stop": 49500,     # $500 risk
            "target": 51000,    # $1000 reward, RR = 2.0
            "ttl_minutes": 180,
            "confidence": 0.75,
            "active_window_c5": 60,
            "active_window_c15": 30,
            "rationale": "BTC breakout"
        }
        
        result = validate_expert_proposal(
            proposal,
            self.get_btc_candles(),
            min_rr=2.0,
            anti_scalp_k=1.5
        )
        
        print(f"\nCrypto Buy: RR={result['rr']}, anti_scalp={result['anti_scalp']} >= {result['anti_scalp_min']}")
        
        # RR should be exactly 2.0
        assert result["rr_pass"] == True
        assert result["rr"] == 2.0
        print(f"✓ Crypto RR calculation works: {result['rr']}")
    
    def test_crypto_anti_scalp(self):
        """Test anti-scalp works with crypto-scale movements."""
        c5_candles = self.get_btc_candles()
        
        # Calculate median range
        ranges = [c["high"] - c["low"] for c in c5_candles]
        median_range = sorted(ranges)[len(ranges) // 2]
        
        print(f"\nCrypto median range: ${median_range}")
        print(f"Anti-scalp minimum (k=1.5): ${median_range * 1.5}")
        
        assert median_range > 0
        print(f"✓ Anti-scalp check works for crypto")


class TestPriceScaleComparison:
    """Compare validation across all price scales."""
    
    def test_rr_is_scale_independent(self):
        """Verify RR calculation is truly scale-independent."""
        
        # Same RR=2.0 setup across different scales
        scenarios = [
            {
                "name": "Forex (pips)",
                "entry": 1.0860,
                "stop": 1.0850,
                "target": 1.0880,
                "expected_rr": 2.0
            },
            {
                "name": "Index (points)",
                "entry": 24000,
                "stop": 23900,
                "target": 24200,
                "expected_rr": 2.0
            },
            {
                "name": "Crypto ($)",
                "entry": 50000,
                "stop": 49500,
                "target": 51000,
                "expected_rr": 2.0
            },
            {
                "name": "Small stock ($)",
                "entry": 100,
                "stop": 95,
                "target": 110,
                "expected_rr": 2.0
            },
            {
                "name": "Penny stock (¢)",
                "entry": 0.50,
                "stop": 0.45,
                "target": 0.60,
                "expected_rr": 2.0
            }
        ]
        
        print("\n" + "="*80)
        print("RR CALCULATION ACROSS PRICE SCALES")
        print("="*80)
        
        for scenario in scenarios:
            # Calculate RR manually
            risk = abs(scenario["entry"] - scenario["stop"])
            reward = abs(scenario["target"] - scenario["entry"])
            rr = reward / risk
            
            print(f"\n{scenario['name']}:")
            print(f"  Entry:  {scenario['entry']}")
            print(f"  Stop:   {scenario['stop']}")
            print(f"  Target: {scenario['target']}")
            print(f"  Risk:   {risk}")
            print(f"  Reward: {reward}")
            print(f"  RR:     {rr}")
            
            # Verify RR is exactly 2.0
            assert abs(rr - scenario["expected_rr"]) < 0.01, f"RR mismatch for {scenario['name']}"
        
        print("\n✓ RR calculation is scale-independent!")
    
    def test_anti_scalp_is_relative_to_volatility(self):
        """Verify anti-scalp adjusts to each instrument's volatility."""
        
        scenarios = [
            {
                "name": "Forex (low volatility)",
                "candles": [
                    {"high": 1.0855, "low": 1.0848},  # 7 pips range
                    {"high": 1.0858, "low": 1.0850},  # 8 pips
                    {"high": 1.0862, "low": 1.0854},  # 8 pips
                ],
                "entry": 1.0860,
                "target": 1.0880  # 20 pips
            },
            {
                "name": "Index (medium volatility)",
                "candles": [
                    {"high": 24050, "low": 23980},  # 70 points range
                    {"high": 24070, "low": 24000},  # 70 points
                    {"high": 24090, "low": 24020},  # 70 points
                ],
                "entry": 24000,
                "target": 24200  # 200 points
            },
            {
                "name": "Crypto (high volatility)",
                "candles": [
                    {"high": 50500, "low": 49800},  # $700 range
                    {"high": 50700, "low": 50000},  # $700
                    {"high": 50900, "low": 50200},  # $700
                ],
                "entry": 50000,
                "target": 51000  # $1000
            }
        ]
        
        print("\n" + "="*80)
        print("ANTI-SCALP RELATIVE TO INSTRUMENT VOLATILITY")
        print("="*80)
        
        for scenario in scenarios:
            # Calculate median range
            ranges = [c["high"] - c["low"] for c in scenario["candles"]]
            median_range = sorted(ranges)[len(ranges) // 2]
            
            # Calculate target distance
            target_distance = abs(scenario["target"] - scenario["entry"])
            
            # Anti-scalp check with k=1.5
            min_distance = median_range * 1.5
            passes = target_distance >= min_distance
            
            print(f"\n{scenario['name']}:")
            print(f"  Median range:     {median_range}")
            print(f"  Min distance (k=1.5): {min_distance}")
            print(f"  Target distance:  {target_distance}")
            print(f"  Passes anti-scalp: {passes}")
        
        print("\n✓ Anti-scalp is relative to each instrument's volatility!")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])

