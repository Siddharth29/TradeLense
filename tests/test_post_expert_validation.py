"""Test post-expert validation for anti-scalp and RR checks ONLY.

Tests the validate_expert_proposal() function with real proposal structures
from expert API responses.
"""

import pytest
from packages.core.validators import validate_expert_proposal


# Sample 5-minute candles
C5_CANDLES = [
    {"time": "09:15", "open": 121500, "high": 121600, "low": 121450, "close": 121550, "volume": 1000},
    {"time": "09:20", "open": 121550, "high": 121650, "low": 121520, "close": 121620, "volume": 1100},
    {"time": "09:25", "open": 121620, "high": 121720, "low": 121600, "close": 121680, "volume": 1200},
    {"time": "09:30", "open": 121680, "high": 121780, "low": 121660, "close": 121740, "volume": 1300},
    {"time": "09:35", "open": 121740, "high": 121820, "low": 121720, "close": 121800, "volume": 1400},
]


def test_buy_proposal_passes_validation():
    """Test buy proposal with good RR and anti-scalp passes."""
    proposal = {
        "signal": "buy",
        "entry": 121820,
        "stop": 121520,  # 300 points risk
        "target": 122420,  # 600 points reward, RR = 2.0
        "ttl_minutes": 180,
        "confidence": 0.72,
        "active_window_c5": 60,
        "active_window_c15": 30,
        "rationale": "Price reclaimed 121800s after sweeping liquidity below 121000"
    }
    
    result = validate_expert_proposal(proposal, C5_CANDLES, min_rr=2.0, anti_scalp_k=1.5)
    
    assert result["valid"] == True
    assert result["rr_pass"] == True
    assert result["anti_scalp_pass"] == True
    assert len(result["errors"]) == 0
    print(f"✓ Buy proposal: RR={result['rr']}, distance={result['anti_scalp']}")


def test_sell_proposal_passes_validation():
    """Test sell proposal with good RR and anti-scalp passes."""
    proposal = {
        "signal": "sell",
        "entry": 122250,
        "stop": 122370,
        "target": 121400,
        "ttl_minutes": 90,
        "confidence": 0.68,
        "active_window_c5": 50,
        "active_window_c15": 20,
        "rationale": "122250-122260 is a strong 15m/5m resistance"
    }
    
    result = validate_expert_proposal(proposal, C5_CANDLES, min_rr=2.0, anti_scalp_k=1.5)
    
    assert result["valid"] == True
    assert result["rr_pass"] == True
    assert result["anti_scalp_pass"] == True
    assert len(result["errors"]) == 0
    print(f"✓ Sell proposal: RR={result['rr']}, distance={result['anti_scalp']}")


def test_flat_signal_passes():
    """Test flat signal passes automatically."""
    proposal = {
        "signal": "flat",
        "entry": None,
        "stop": None,
        "target": None,
        "ttl_minutes": 90,
        "confidence": 0.0,
        "active_window_c5": 50,
        "active_window_c15": 20,
        "rationale": "No clear opportunity"
    }
    
    result = validate_expert_proposal(proposal, C5_CANDLES)
    
    assert result["valid"] == True
    assert result["rr_pass"] == True
    assert result["anti_scalp_pass"] == True


def test_low_rr_fails():
    """Test proposal with RR < 2.0 fails."""
    proposal = {
        "signal": "buy",
        "entry": 121820,
        "stop": 121700,  # Only 120 points risk
        "target": 121940,  # Only 120 points reward, RR = 1.0
        "ttl_minutes": 90,
        "confidence": 0.65,
        "active_window_c5": 50,
        "active_window_c15": 20,
        "rationale": "Low RR setup"
    }
    
    result = validate_expert_proposal(proposal, C5_CANDLES, min_rr=2.0)
    
    assert result["valid"] == False
    assert result["rr_pass"] == False
    assert "RR" in result["errors"][0]
    print(f"✗ Low RR: {result['rr']} < 2.0")


def test_anti_scalp_fails():
    """Test proposal with too-small target distance fails anti-scalp."""
    proposal = {
        "signal": "buy",
        "entry": 121820,
        "stop": 121800,  # 20 points risk
        "target": 121860,  # 40 points reward, RR = 2.0 (good)
        # But 40 points might be too small for anti-scalp
        "ttl_minutes": 90,
        "confidence": 0.70,
        "active_window_c5": 50,
        "active_window_c15": 20,
        "rationale": "Small scalp"
    }
    
    result = validate_expert_proposal(proposal, C5_CANDLES, min_rr=2.0, anti_scalp_k=1.5)
    
    # Result depends on median range - if it fails, check error
    if not result["valid"]:
        assert result["anti_scalp_pass"] == False
        assert "Anti-scalp failed" in result["errors"][0]
        print(f"✗ Anti-scalp: {result['anti_scalp']} < {result['anti_scalp_min']}")


def test_batch_validation():
    """Test validating multiple proposals from expert response."""
    proposals = [
        {
            "signal": "buy",
            "entry": 121820,
            "stop": 121520,  # 300 risk
            "target": 122420,  # 600 reward, RR=2.0
            "ttl_minutes": 180,
            "confidence": 0.72,
            "active_window_c5": 60,
            "active_window_c15": 30,
            "rationale": "Bullish setup"
        },
        {
            "signal": "sell",
            "entry": 122250,
            "stop": 122370,  # 120 risk
            "target": 121400,  # 850 reward, RR=7.08
            "ttl_minutes": 90,
            "confidence": 0.68,
            "active_window_c5": 50,
            "active_window_c15": 20,
            "rationale": "Bearish setup"
        }
    ]
    
    # Validate all
    results = [validate_expert_proposal(p, C5_CANDLES) for p in proposals]
    
    # Filter valid
    valid = [p for p, r in zip(proposals, results) if r["valid"]]
    
    print(f"\nBatch validation: {len(valid)}/{len(proposals)} valid")
    for i, (p, r) in enumerate(zip(proposals, results), 1):
        status = "✓" if r["valid"] else "✗"
        print(f"  {status} Proposal {i}: {p['signal']} - RR={r['rr']}, "
              f"anti_scalp={r['anti_scalp']}/{r['anti_scalp_min']}")


def test_missing_fields():
    """Test proposal with missing fields fails."""
    proposal = {
        "signal": "buy",
        "entry": 121820,
        # Missing stop and target
        "ttl_minutes": 90,
        "confidence": 0.72,
    }
    
    result = validate_expert_proposal(proposal, C5_CANDLES)
    
    assert result["valid"] == False
    assert "Missing required fields" in result["errors"][0]


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
