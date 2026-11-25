"""Manual E2E check: drive the full multi_expert pipeline with context flags ON,
real candles, mocked GPT calls — and print the exact expert & judge prompts.

Run: python scripts/e2e_context_check.py
"""
import asyncio, os, sys, json
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.stdout.reconfigure(encoding="utf-8")
import pandas as pd
from unittest.mock import patch
import packages.core.config as cfg
import packages.core.multi_expert as me


def load(tf, n):
    df = pd.read_csv(f"data/processed/btcusd_{tf}_recent.csv").tail(n)
    return [{"time": r.time, "open": r.open, "high": r.high, "low": r.low,
             "close": r.close, "volume": r.volume} for r in df.itertuples()]


C5 = load("5m", 120)
C15 = load("15m", 80)
HTF = {"1h": load("1h", 120), "4h": load("4h", 90), "1d": load("1d", 60)}
PRICE = float(C5[-1]["close"])
captured = {}


async def fake_complete(*args, **kwargs):
    mode = kwargs.get("gpt_mode", "")
    captured[mode] = {"system": kwargs.get("system_prompt"), "user": kwargs.get("user_prompt")}
    if mode.startswith("multi_expert_trend"):
        return {
            "proposals": [
                {"signal": "buy", "entry": PRICE, "stop": PRICE - 200, "target": PRICE + 500,
                 "ttl_minutes": 90, "confidence": 0.7, "rationale": "buy retest (counter-trend)"},
                {"signal": "sell", "entry": PRICE, "stop": PRICE + 200, "target": PRICE - 500,
                 "ttl_minutes": 90, "confidence": 0.7, "rationale": "sell aligned"},
            ],
            "candel_required_for_judge": 20,
        }
    if "judge" in mode:
        return {
            "code": "def run(candles_5, now):\n    return {'direction':'sell','entry':%f,'stop_loss':%f,'take_profit_1':%f,'take_profit_2':%f,'confidence':0.7,'rationale':'x'}" % (
                PRICE, PRICE + 200, PRICE - 500, PRICE - 800),
            "metadata": {"strategy_type": "retest", "decision": "accepted", "rationale": "ok", "confluence_score": 4},
            "ttl_minutes": 90,
            "confidence": 0.7,
        }
    return {}


async def main():
    print("FLAGS:", dict(HTF=cfg.HTF_CONTEXT_ENABLED, ATR=cfg.ATR_STOP_ENABLED,
                         CT=cfg.COUNTER_TREND_MODE, real_macd=cfg.CONTEXT_USE_REAL_MACD,
                         breadth=cfg.CONTEXT_INCLUDE_MARKET_BREADTH))
    gpt_cfg = {"asset": "BTCUSD", "agent_id": "e2e", "htf_candles": HTF}
    with patch.object(me, "complete", side_effect=fake_complete):
        decision, _, _ = await me.multi_expert_decision(C5, C15, gpt_cfg, use_code_sniper=True)
    print("\nDECISION:", type(decision).__name__,
          "| signal/dir:", getattr(decision, "signal", getattr(decision, "code", "")[:0] or "code_sniper"))

    for tag in ("multi_expert_trend", "multi_expert_judge_code_sniper"):
        p = captured.get(tag)
        print("\n" + "=" * 30, tag, "=" * 30)
        if not p:
            print("  (not called)"); continue
        print("--- USER PROMPT ---")
        print(p["user"])

    # ---- automated assertions (manual checks codified) ----
    exp = captured["multi_expert_trend"]["user"]
    jud = captured["multi_expert_judge_code_sniper"]["user"]
    atr_on = cfg.ATR_STOP_ENABLED
    ct_on = cfg.COUNTER_TREND_MODE == "soft"
    checks = {
        "expert has HTF_CONTEXT": "HTF_CONTEXT" in exp,
        "judge has HTF_CONTEXT": "HTF_CONTEXT" in jud,
        "STOP_GUIDANCE presence matches ATR flag (expert)": ("STOP_GUIDANCE" in exp) == atr_on,
        "STOP_GUIDANCE presence matches ATR flag (judge)": ("STOP_GUIDANCE" in jud) == atr_on,
        "judge htf_trend down": '"htf_trend":"down"' in jud,
        "judge CONTEXT has source_timeframe": '"source_timeframe":"5m"' in jud,
        "judge CONTEXT NO market_breadth": "market_breadth" not in jud,
        "judge CONTEXT NO pivot_points": "pivot_points" not in jud,
        "judge CONTEXT NO fibonacci": "fibonacci_levels" not in jud,
        "judge momentum has macd_hist (real MACD)": "macd_hist" in jud,
        "counter-trend haircut buy 0.595 (soft)": ('"confidence":0.595' in jud.replace(" ", "")) == ct_on,
    }
    print("\n" + "=" * 25, "MANUAL CHECKS", "=" * 25)
    ok = True
    for k, v in checks.items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
        ok = ok and v
    print("\nRESULT:", "ALL PASS" if ok else "SOME FAILED")


if __name__ == "__main__":
    asyncio.run(main())
