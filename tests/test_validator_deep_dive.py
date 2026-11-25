"""Deep dive into validator behavior to find the exact failure point."""

from packages.core.validators import rr, anti_scalp, inside_window, median_range_5m
from packages.core.schemas_trade import MultiExpertSignal


def create_test_candles(count=100, start_price=121000):
    """Create test candle data."""
    candles = []
    price = start_price
    for i in range(count):
        candles.append({
            'time': f'2025-10-07T{10 + i//12:02d}:{(i % 12) * 5:02d}:00',
            'open': price,
            'high': price + 100,
            'low': price - 100,
            'close': price + 50,
            'volume': 1000
        })
        price += 10
    return candles


print("="*80)
print("VALIDATOR DEEP DIVE - Finding The Exact Failure Point")
print("="*80)

# Create test data
c5_superset = create_test_candles(100, 121000)
c15_superset = create_test_candles(40, 121000)

# Test proposal (from actual GPT response)
proposal = MultiExpertSignal(
    signal="buy",
    entry=120950,
    stop=120660,
    target=121700,
    ttl_minutes=180,
    confidence=0.68,
    active_window_c5=75,
    active_window_c15=30,
    need_more_candles=False,
    more_c5=None,
    more_c15=None,
    rationale="Test"
)

print(f"\nProposal Details:")
print(f"  Signal: {proposal.signal}")
print(f"  Entry: {proposal.entry}")
print(f"  Stop: {proposal.stop}")
print(f"  Target: {proposal.target}")
print(f"  Active Window C5: {proposal.active_window_c5}")
print(f"  Active Window C15: {proposal.active_window_c15}")

print(f"\nCandle Data:")
print(f"  C5 Superset: {len(c5_superset)} candles")
print(f"  C15 Superset: {len(c15_superset)} candles")

# Test 1: RR Check
print(f"\n{'='*80}")
print("TEST 1: Risk/Reward Ratio")
print("="*80)
try:
    rr_value = rr(proposal.entry, proposal.stop, proposal.target, proposal.signal)
    print(f"✅ RR Value: {rr_value:.2f}")
    print(f"   Threshold: 2.0")
    print(f"   Result: {'PASS' if rr_value >= 2.0 else 'FAIL'}")
except Exception as e:
    print(f"❌ RR Check Failed: {e}")

# Test 2: Anti-Scalp Check
print(f"\n{'='*80}")
print("TEST 2: Anti-Scalp Check")
print("="*80)
try:
    median_rng = median_range_5m(c5_superset)
    min_distance = 1.5 * median_rng
    actual_distance = abs(proposal.target - proposal.entry)
    
    print(f"  Median 5m Range: {median_rng:.2f}")
    print(f"  K Factor: 1.5")
    print(f"  Min Distance Required: {min_distance:.2f}")
    print(f"  Actual Distance: {actual_distance:.2f}")
    
    anti_scalp_result = anti_scalp(proposal.entry, proposal.target, proposal.signal, c5_superset, k=1.5)
    print(f"  Result: {'✅ PASS' if anti_scalp_result else '❌ FAIL'}")
except Exception as e:
    print(f"❌ Anti-Scalp Check Failed: {e}")

# Test 3: Inside Window Check (WITH window slicing)
print(f"\n{'='*80}")
print("TEST 3: Inside Window Check (WITH correct slicing)")
print("="*80)
try:
    # Slice windows based on proposal specification
    c5_window = c5_superset[-proposal.active_window_c5:] if len(c5_superset) >= proposal.active_window_c5 else c5_superset
    c15_window = c15_superset[-proposal.active_window_c15:] if len(c15_superset) >= proposal.active_window_c15 else c15_superset
    
    print(f"  C5 Window: Last {len(c5_window)} candles (requested {proposal.active_window_c5})")
    print(f"  C15 Window: Last {len(c15_window)} candles (requested {proposal.active_window_c15})")
    
    # Get window range
    c5_highs = [c['high'] for c in c5_window]
    c5_lows = [c['low'] for c in c5_window]
    c15_highs = [c['high'] for c in c15_window]
    c15_lows = [c['low'] for c in c15_window]
    
    window_high = max(c5_highs + c15_highs)
    window_low = min(c5_lows + c15_lows)
    
    print(f"  Window Range: {window_low:.2f} - {window_high:.2f}")
    print(f"  Entry {proposal.entry}: {'✅ IN' if window_low <= proposal.entry <= window_high else '❌ OUT'}")
    print(f"  Stop {proposal.stop}: {'✅ IN' if window_low <= proposal.stop <= window_high else '❌ OUT'}")
    print(f"  Target {proposal.target}: {'✅ IN' if window_low <= proposal.target <= window_high else '❌ OUT'}")
    
    window_result = inside_window(proposal.entry, proposal.stop, proposal.target, c5_window, c15_window)
    print(f"  Result: {'✅ PASS' if window_result else '❌ FAIL'}")
except Exception as e:
    print(f"❌ Inside Window Check Failed: {e}")

# Test 4: Inside Window Check (WITHOUT window slicing - old way)
print(f"\n{'='*80}")
print("TEST 4: Inside Window Check (WITHOUT slicing - old buggy way)")
print("="*80)
try:
    # OLD WAY: Use entire superset (BUG!)
    all_highs = [c['high'] for c in c5_superset + c15_superset]
    all_lows = [c['low'] for c in c5_superset + c15_superset]
    
    old_window_high = max(all_highs)
    old_window_low = min(all_lows)
    
    print(f"  Window Range: {old_window_low:.2f} - {old_window_high:.2f} (using ALL {len(c5_superset)+len(c15_superset)} candles)")
    print(f"  Entry {proposal.entry}: {'✅ IN' if old_window_low <= proposal.entry <= old_window_high else '❌ OUT'}")
    print(f"  Stop {proposal.stop}: {'✅ IN' if old_window_low <= proposal.stop <= old_window_high else '❌ OUT'}")
    print(f"  Target {proposal.target}: {'✅ IN' if old_window_low <= proposal.target <= old_window_high else '❌ OUT'}")
    
    old_window_result = inside_window(proposal.entry, proposal.stop, proposal.target, c5_superset, c15_superset)
    print(f"  Result: {'✅ PASS' if old_window_result else '❌ FAIL'}")
    
    if window_result != old_window_result:
        print(f"\n  ⚠️  DIFFERENT RESULTS: Sliced={window_result}, Full={old_window_result}")
        print(f"  This confirms the window slicing fix is critical!")
except Exception as e:
    print(f"❌ Old Window Check Failed: {e}")

print(f"\n{'='*80}")
print("SUMMARY")
print("="*80)
print("Run the next cycle and check logs for detailed validation_errors array")
print("It will show EXACTLY which validator is failing and why")
print("="*80)

